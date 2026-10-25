import os
import json
import re
import math
import time
import logging
import threading
import sqlite3
import sys
import gzip
import tempfile
import shutil
import requests
from flask import Flask, request, jsonify, abort
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from flask import Response
from urllib.parse import urljoin


LISTEN = os.getenv("LISTEN", "127.0.0.1")
PORT = int(os.getenv("PORT", 12321))
API_KEY = os.getenv("API_KEY", "change_me")
API_KEY_RW = os.getenv("API_KEY_RW", "change_me_rw")
TIMEOUT = int(os.getenv("TIMEOUT", 60))
REFRESH_INTERVAL = int(os.getenv("REFRESH_INTERVAL", 600))
SENSORS_REFRESH_INTERVAL = int(os.getenv("SENSORS_REFRESH_INTERVAL", 120))
# Пока машина едет (зажигание включено ИЛИ скорость > 0 — второе условие
# ловит нештатное перемещение с выключенным зажиганием, например эвакуатор)
# опрашиваем чаще, чем на стоянке — это даёт заметно более точный маршрут
# и скорость, а не крупные "хорды" между редкими точками. На стоянке
# возвращаемся к медленному интервалу, чтобы не плодить лишние записи в БД
# и не грузить API машины без необходимости.
SENSORS_REFRESH_INTERVAL_DRIVING = int(os.getenv("SENSORS_REFRESH_INTERVAL_DRIVING", 20))
# Едем своим ходом или тащат на эвакуаторе — определяем как "движение"
# (зажигание включено ИЛИ скорость > 0). Но переключаться на медленный
# интервал СРАЗУ по первому же "стоим" нельзя — иначе пробка (в том числе
# когда сам эвакуатор стоит в пробке, а у машины при этом зажигание
# выключено) на минуту-две даст ложное "приехали, встали на стоянку",
# опрос уйдёт на 2 минуты, и можно прозевать момент, когда затор
# рассосался и движение возобновилось. Поэтому держим быстрый интервал ещё
# SENSORS_REFRESH_COOLDOWN_SECONDS после последнего момента, когда
# движение реально фиксировалось — и только если тишина продержалась
# дольше этого окна, считаем что действительно встали и остыли до
# медленного интервала.
SENSORS_REFRESH_COOLDOWN_SECONDS = int(os.getenv("SENSORS_REFRESH_COOLDOWN_SECONDS", 180))
JSON_SUB = os.getenv("JSON_SUB", ".sensors")
EVOLUTE_TOKEN_FILENAME = os.getenv("EVOLUTE_TOKEN_FILENAME", "evy-platform-access.txt")
EVOLUTE_REFRESH_TOKEN_FILENAME = os.getenv("EVOLUTE_REFRESH_TOKEN_FILENAME", "evy-platform-refresh.txt")
CAR_ID = os.getenv("CAR_ID", "SOME_CAR_ID_HASH_CHANGE_ME")

# Telegram-уведомления администраторам. Если BOT_TOKEN не задан или список
# админов пуст, алерты просто тихо отключаются (см. send_message).
#
# TELEGRAM_ADMIN_CHAT_ID — список получателей через запятую. У каждого можно
# указать свой набор включённых типов алертов через двоеточие и "+":
#   TELEGRAM_ADMIN_CHAT_ID=111111,222222:ignition+doors,333333:all
# - 111111       — без фильтра, получает вообще все типы алертов
# - 222222       — только "ignition" и "doors"
# - 333333:all   — то же самое, что без фильтра (явно "все")
# Доступные типы: movement, coords, coords_summary, ignition, connectivity,
# doors, battery_full, archive, voltage_low, voltage_charge
# "voltage_low" — 12V АКБ: низкое напряжение (мгновенно); "voltage_charge" —
# суточная сводка о подзарядках 12V (см. блок «Мониторинг 12V АКБ» ниже).
# Как и "archive", при явном списке типов у админа их нужно добавить отдельно.
# "archive" — короткая сводка после каждого запуска архивации БД (успех/
# отправлено N записей/ничего не найдено/ошибка) + время следующего запуска.
# Если у админа указан явный список типов (не "all"), "archive" в него нужно
# добавить отдельно, иначе сводки по архивации приходить не будут.
#
# coords / coords_summary — это два взаимоисключающих режима получения
# координат ИМЕННО во время поездки (зажигание включено):
#   - "coords"         — как раньше: сообщение на каждое значимое смещение
#                         GPS (может быть десятки сообщений за одну поездку)
#   - "coords_summary" — вместо потока сообщений одна сводка по итогам
#                         поездки (дистанция, длительность, старт/финиш),
#                         отправляется в момент выключения зажигания
# Если перемещение происходит ПРИ ВЫКЛЮЧЕННОМ зажигании (например машину
# везёт эвакуатор) — это считается нештатной ситуацией, и координаты в этом
# случае шлются как раньше, по каждому смещению, ОБОИМ типам подписчиков
# ("coords" и "coords_summary" одинаково получают полный поток) — сжатие
# применяется только к обычной поездке своим ходом.
# Если у админа указаны оба типа сразу — побеждает "coords" (полный поток),
# "coords_summary" в этом случае просто не имеет эффекта.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_ADMIN_CHAT_ID = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

ALERT_TYPES = {
    "movement", "coords", "coords_summary", "ignition",
    "connectivity", "doors", "battery_full", "archive",
    "voltage_low", "voltage_charge",
}

# Порог значимого перемещения для алерта "coords", в метрах. Обычный бытовой
# GPS-приёмник даёт погрешность порядка 5-15м (в городской застройке из-за
# отражений сигнала — иногда 20-30м), поэтому сравнивать координаты "в лоб"
# нельзя: стоящая на месте машина будет постоянно чуть "дрожать" в показаниях
# и генерировать ложные срабатывания. Считаем координаты изменившимися только
# если реальное расстояние между двумя снимками больше этого порога.
COORDS_MIN_DISTANCE_METERS = float(os.getenv("COORDS_MIN_DISTANCE_METERS", 20))

# По умолчанию — относительный путь (как раньше у токенов). На Railway задайте
# DB_FILE=/data/evolute.db в Variables, чтобы файл жил в уже подключённом Volume
# и переживал редеплои, как и токены.
DB_FILE = os.getenv("DB_FILE", "evolute_history.db")

# HISTORY_RETENTION_DAYS — абсолютная страховочная граница: записи старше
# этого возраста удаляются в любом случае, даже если архивация в Telegram
# ниже выключена или у неё что-то не получилось. Не главный механизм чистки.
HISTORY_RETENTION_DAYS = int(os.getenv("HISTORY_RETENTION_DAYS", 90))

# --- Архивация истории в Telegram --------------------------------------
# Раз в ARCHIVE_INTERVAL_DAYS суток: всё старше ARCHIVE_KEEP_DAYS выгружается
# из sensor_history/token_history в сжатые .jsonl.gz (порезанные под лимит
# файла бота — 50МБ, с запасом ARCHIVE_CHUNK_MAX_BYTES), отправляется в чат
# TELEGRAM_ARCHIVE_CHAT_ID, и ТОЛЬКО после успешной отправки всех частей эти
# строки удаляются из рабочей БД. Если TELEGRAM_ARCHIVE_CHAT_ID не задан,
# архивация просто выключена — работает только страховочная чистка выше.
TELEGRAM_ARCHIVE_CHAT_ID = os.getenv("TELEGRAM_ARCHIVE_CHAT_ID", "")
ARCHIVE_INTERVAL_DAYS = float(os.getenv("ARCHIVE_INTERVAL_DAYS", 2))
ARCHIVE_KEEP_DAYS = float(os.getenv("ARCHIVE_KEEP_DAYS", 3))
ARCHIVE_CHUNK_MAX_BYTES = int(os.getenv("ARCHIVE_CHUNK_MAX_BYTES", 45 * 1024 * 1024))

current_refresh_interval = REFRESH_INTERVAL
current_sensor_interval = SENSORS_REFRESH_INTERVAL
last_moving_ts = None  # time.time() последнего снимка, где moving было True

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/144.0.0.0 Safari/537.36"
)

DUMP_FILE = "dump.json"
STATUS_FILE = "status.json"

EVOLUTE_REFRESH_URL = "https://app.evassist.ru/id-service/auth/refresh-token"
EVOLUTE_SENSOR_URL = f"https://app.evassist.ru/car-service/tbox/{CAR_ID}/info"

# Новый источник данных датчиков — тот, которым пользуется сайт (раздел
# «Автопарк → Автомобиль»): /client-bff-service/telemetry/<IMEI>. Если CAR_IMEI
# задан, датчики берутся оттуда, а CAR_ID по-прежнему нужен для команд
# (/tbox/..., /tbox-i/...). Если CAR_IMEI не задан — работает прежний /info.
CAR_IMEI = os.getenv("CAR_IMEI", "").strip()
EVOLUTE_TELEMETRY_URL = (
    f"https://app.evassist.ru/client-bff-service/telemetry/{CAR_IMEI}" if CAR_IMEI else None
)
TELEMETRY_TIME_ZONE = os.getenv("TELEMETRY_TIME_ZONE", "3")  # заголовок time-zone, как у браузера

INTELLIGENT_ACTIONS = {
    "lock_close": ("centralLockingToggle", "centralLockingStatus", 1),
    "lock_open": ("centralLockingToggle", "centralLockingStatus", 0),
    "heating_off": ("heating", "climateStatus", 0),
    "heating_on": ("heating", "climateStatus", 1),
    "cooling_off": ("cooling", "climateStatus", 0),
    "cooling_on": ("cooling", "climateStatus", 1),
    "trunk_close": ("trunkOpen", "trunkStatus", 0),
    "trunk_open": ("trunkOpen", "trunkStatus", 1),
    "prepare_on": ("PREPARE", "ignitionStatus", 1),
    "prepare_off": ("CANCEL", "ignitionStatus", 0),
    "blink": ("blink", "ready", 1),
}


logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(asctime)s %(message)s"
)
logger = logging.getLogger(__name__)

app = Flask(__name__)


def send_message(chat_id: int | str, text: str, **extra) -> dict:
    """Отправить текстовое сообщение через Telegram Bot API. extra прокидывается
    как есть (например parse_mode='HTML', reply_markup=...).
    Никаких доп. библиотек не требуется — используем уже имеющийся requests,
    как и для остальных HTTP-вызовов в этом файле.
    Тихо ничего не делает, если TELEGRAM_BOT_TOKEN не сконфигурирован, и никогда
    не бросает исключение наружу — чтобы сбой Telegram не ронял основной цикл
    опроса сенсоров."""
    if not TELEGRAM_BOT_TOKEN:
        return {}
    payload = {"chat_id": chat_id, "text": text, **extra}
    try:
        resp = requests.post(f"{TELEGRAM_API_URL}/sendMessage", json=payload, timeout=TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error(f"Failed to send Telegram message: {e}")
        return {}


def send_document(chat_id: int | str, filepath: str, caption: str | None = None) -> dict | None:
    """Отправить файл через Telegram Bot API (sendDocument, multipart/form-data).
    Возвращает распарсенный JSON-ответ при успехе, иначе None — вызывающий код
    ориентируется на None, чтобы понять "отправка не удалась, старые данные
    из БД удалять нельзя". На файлы бот-API пока ограничивает загрузку 50МБ
    (см. https://core.telegram.org/bots/api#senddocument), поэтому чанки
    архива должны быть заведомо меньше этого лимита."""
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        return None
    try:
        with open(filepath, "rb") as f:
            data = {"chat_id": chat_id}
            if caption:
                data["caption"] = caption
            resp = requests.post(
                f"{TELEGRAM_API_URL}/sendDocument",
                data=data,
                files={"document": (os.path.basename(filepath), f)},
                timeout=120,
            )
        resp.raise_for_status()
        result = resp.json()
        if not result.get("ok"):
            logger.error(f"Telegram sendDocument returned not-ok: {result}")
            return None
        return result
    except Exception as e:
        logger.error(f"Failed to send document '{filepath}' to Telegram: {e}")
        return None


def parse_admin_subscriptions(raw: str) -> dict:
    """Разбирает TELEGRAM_ADMIN_CHAT_ID в {chat_id: set(алертов) | None}.
    None означает "все типы алертов включены" (админ без фильтра или с ":all").
    Некорректные/неизвестные типы алертов логируются и просто отбрасываются,
    чтобы опечатка в конфиге не роняла сервис."""
    subscriptions = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" in entry:
            chat_id, filter_part = entry.split(":", 1)
            chat_id = chat_id.strip()
            filter_part = filter_part.strip()
        else:
            chat_id, filter_part = entry, ""
        if not chat_id:
            continue
        if not filter_part or filter_part.lower() == "all":
            subscriptions[chat_id] = None
            continue
        requested = {t.strip() for t in filter_part.split("+") if t.strip()}
        unknown = requested - ALERT_TYPES
        if unknown:
            logger.warning(f"Admin {chat_id}: unknown alert types ignored: {sorted(unknown)}")
        subscriptions[chat_id] = requested & ALERT_TYPES
    return subscriptions


ADMIN_SUBSCRIPTIONS = parse_admin_subscriptions(TELEGRAM_ADMIN_CHAT_ID)


def notify_admins(alert_type: str, text: str, **extra):
    """Рассылает сообщение всем администраторам, у которых включён данный
    тип алерта (alert_type должен быть одним из ALERT_TYPES)."""
    for chat_id, allowed in ADMIN_SUBSCRIPTIONS.items():
        if allowed is None or alert_type in allowed:
            send_message(chat_id, text, **extra)


def notify_admins_any(alert_types: set, text: str, **extra):
    """Как notify_admins, но админ получает сообщение, если у него включён
    ХОТЯ БЫ ОДИН тип из alert_types. Нужно для координат при движении с
    выключенным зажиганием (эвакуатор и т.п.) — там полный поток координат
    шлётся и "coords", и "coords_summary" подписчикам одинаково, разница
    режимов применяется только к обычной поездке (см. комментарий у
    TELEGRAM_ADMIN_CHAT_ID)."""
    for chat_id, allowed in ADMIN_SUBSCRIPTIONS.items():
        if allowed is None or (allowed & alert_types):
            send_message(chat_id, text, **extra)


def notify_admins_coords_summary_only(text: str, **extra):
    """Шлёт итоговую сводку по поездке только тем, кто явно выбрал
    "coords_summary" и НЕ выбрал "coords" — те, кто и так получал полный
    поток координат в реальном времени, повторную сводку не получают."""
    for chat_id, allowed in ADMIN_SUBSCRIPTIONS.items():
        if allowed is not None and "coords_summary" in allowed and "coords" not in allowed:
            send_message(chat_id, text, **extra)


# Название сенсора двери/багажника -> подпись в уведомлении.
DOOR_LABELS = {
    "doorFLStatus": "Передняя левая дверь",
    "doorFRStatus": "Передняя правая дверь",
    "doorRLStatus": "Задняя левая дверь",
    "doorRRStatus": "Задняя правая дверь",
    "trunkStatus": "Багажник",
}
DOOR_LIKE_PATTERN = re.compile(r"^(door\w*status|trunkstatus)$", re.I)


def haversine_meters(lat1, lon1, lat2, lon2) -> float:
    """Расстояние между двумя точками на сфере (формула Хаверсина), в метрах.
    Используется только math из стандартной библиотеки, без гео-пакетов."""
    r = 6371000  # средний радиус Земли, м
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# Состояние текущей поездки (зажигание включено), нужно только для
# "coords_summary" — копится дистанция/точки, пока не выключится зажигание.
# Обновляется исключительно из periodic_fetch (один поток таймера), поэтому
# отдельный lock не нужен.
current_trip = None  # dict | None

# Уже отправляли "автомобиль начал движение" в рамках текущей поездки
# (с момента включения зажигания). Сбрасывается при старте/завершении
# поездки, чтобы остановки на светофоре (скорость 0 -> >0 -> 0 -> ...)
# не спамили этим алертом повторно, пока зажигание не выключили.
movement_notified_this_trip = False


def _start_trip(ts: str, pos: dict, sensors: dict):
    global current_trip, movement_notified_this_trip
    current_trip = {
        "start_ts": ts,
        "start_lat": pos.get("lat"),
        "start_lon": pos.get("lon"),
        "start_odometer": sensors.get("odometer"),
        "last_lat": pos.get("lat"),
        "last_lon": pos.get("lon"),
        "distance_m": 0.0,
        "points": 0,
    }
    movement_notified_this_trip = False


def _finish_trip(ts: str, pos: dict, sensors: dict):
    """Закрывает текущую поездку и шлёт сводку "coords_summary"-подписчикам.
    Ничего не делает, если поездки не было (например рестарт сервиса
    случился уже после выключения зажигания)."""
    global current_trip, movement_notified_this_trip
    movement_notified_this_trip = False
    if current_trip is None:
        return
    trip = current_trip
    current_trip = None

    try:
        start_dt = datetime.fromisoformat(trip["start_ts"])
        end_dt = datetime.fromisoformat(ts)
        duration_min = (end_dt - start_dt).total_seconds() / 60
    except (ValueError, TypeError):
        duration_min = None

    end_odometer = sensors.get("odometer")
    start_odometer = trip.get("start_odometer")
    distance_km = None
    if isinstance(start_odometer, (int, float)) and isinstance(end_odometer, (int, float)) \
            and end_odometer >= start_odometer:
        # одометр надёжнее суммы GPS-прыжков (не режет повороты по хорде)
        distance_km = end_odometer - start_odometer
    elif trip["distance_m"] > 0:
        distance_km = trip["distance_m"] / 1000

    lines = ["🏁 Поездка завершена"]
    if duration_min is not None:
        lines.append(f"Длительность: {duration_min:.0f} мин")
    if distance_km is not None:
        lines.append(f"Дистанция: {distance_km:.1f} км")
    lines.append(f"Точек GPS: {trip['points']}")
    if trip["start_lat"] is not None and trip["start_lon"] is not None:
        lines.append(f"Старт: {trip['start_lat']}, {trip['start_lon']}")
    end_lat, end_lon = pos.get("lat"), pos.get("lon")
    if end_lat is not None and end_lon is not None:
        lines.append(f"Финиш: {end_lat}, {end_lon}")

    notify_admins_coords_summary_only("\n".join(lines))


def check_sensor_alerts(old_data: dict, new_data: dict):
    """Сравнивает предыдущий и новый снимок сенсоров и шлёт алерты
    только на смене состояния (edge-triggered), а не при каждом опросе:
      - ignition       — зажигание включили/выключили;
      - movement       — скорость была 0/отсутствовала, стала > 0;
      - coords         — координаты изменились относительно прошлого снимка;
      - coords_summary — то же самое, но сжато: одна сводка по итогам
                          поездки вместо потока сообщений (см. комментарий у
                          TELEGRAM_ADMIN_CHAT_ID выше). Действует только
                          пока включено зажигание — при движении на
                          заглушенной машине (эвакуатор и т.п.) координаты
                          всё равно шлются сразу, без сжатия;
      - connectivity   — машина потеряла/восстановила связь (isOnline);
      - doors          — дверь или багажник открылись, пока машина на стоянке;
      - battery_full   — заряд батареи достиг 100% (во время самой зарядки,
                          пока % ниже 100, ничего не шлём — только на финише).
    Порядок проверок ниже (зажигание -> движение -> координаты) намеренно
    совпадает с логическим порядком событий реальной поездки: сначала
    включили зажигание, затем машина тронулась, затем появились новые
    координаты. Если несколько из этих событий попадают в один и тот же
    снимок сенсоров (частый случай — именно так машина обычно и стартует),
    уведомления должны прийти в Telegram именно в этом порядке, а не в
    порядке, случайно зависящем от того, в каком месте функции стоит код.
    Дополнительно это ещё и небольшой фикс: если сдвиг координат случился в
    том же снимке, где включилось зажигание, то поездка (current_trip) на
    момент проверки координат уже стартовала, и эта первая дистанция
    корректно попадёт в сводку coords_summary, а не потеряется.
    На самом первом снимке после старта сервиса (old_data пуст) уведомления
    не шлём, чтобы не спамить при каждом рестарте — но если сервис
    перезапустился прямо посреди поездки (зажигание уже включено),
    состояние поездки для coords_summary всё равно тихо инициализируем."""
    if not old_data:
        new_sensors = new_data.get("sensorsData") or {}
        if new_sensors.get("ignitionStatus") and current_trip is None:
            _start_trip(datetime.utcnow().isoformat(), new_data.get("positionData") or {}, new_sensors)
        return

    old_pos = old_data.get("positionData") or {}
    new_pos = new_data.get("positionData") or {}
    old_sensors = old_data.get("sensorsData") or {}
    new_sensors = new_data.get("sensorsData") or {}
    snapshot_ts = datetime.utcnow().isoformat()

    # 1. Зажигание вкл/выкл — идёт первым: это причина, а не следствие
    #    движения/координат, так что уведомление о нём должно приходить
    #    раньше остальных.
    old_ignition = old_sensors.get("ignitionStatus")
    new_ignition = new_sensors.get("ignitionStatus")
    if old_ignition is not None and new_ignition is not None and old_ignition != new_ignition:
        if new_ignition:
            notify_admins("ignition", "🔑 Зажигание включено")
            _start_trip(snapshot_ts, new_pos, new_sensors)
        else:
            notify_admins("ignition", "🔑 Зажигание выключено")
            _finish_trip(snapshot_ts, new_pos, new_sensors)

    # 2. Начало движения — только первый раз за поездку (см. movement_notified_this_trip
    #    выше): иначе каждая остановка на светофоре/в пробке (скорость падает
    #    до 0, потом снова растёт) заново триггерит этот алерт.
    global movement_notified_this_trip
    old_speed = old_pos.get("speed")
    new_speed = new_pos.get("speed")
    if (
        (old_speed in (0, None))
        and isinstance(new_speed, (int, float))
        and new_speed > 0
        and not movement_notified_this_trip
    ):
        notify_admins("movement", f"🚗 Автомобиль начал движение\nСкорость: {new_speed} км/ч")
        movement_notified_this_trip = True

    # 3. Смена координат — только если реальное расстояние больше порога
    #    COORDS_MIN_DISTANCE_METERS (см. объяснение у константы выше);
    #    мелкий GPS-шум на стоянке молчит.
    # Едем своим ходом (зажигание сейчас включено) -> "coords" получает
    # немедленное сообщение как раньше, "coords_summary" копит дистанцию и
    # получит одну сводку в конце поездки. Едем/тащат с выключенным
    # зажиганием (эвакуатор) -> оба типа получают полный поток немедленно,
    # это нештатная ситуация, тут сжимать нечего.
    old_lat, old_lon = old_pos.get("lat"), old_pos.get("lon")
    new_lat, new_lon = new_pos.get("lat"), new_pos.get("lon")
    driving = bool(new_sensors.get("ignitionStatus"))
    if old_lat is not None and old_lon is not None and new_lat is not None and new_lon is not None:
        distance = haversine_meters(old_lat, old_lon, new_lat, new_lon)
        if distance >= COORDS_MIN_DISTANCE_METERS:
            map_url = (
                f"https://yandex.ru/maps/?rtext={old_lat},{old_lon}~{new_lat},{new_lon}&rtt=auto"
            )
            text = (
                f"📍 Координаты изменились\n(~{distance:.0f} м по прямой)\n"
                f"Было: {old_lat}, {old_lon}\nСтало: {new_lat}, {new_lon}\n\n"
                f"{map_url}"
            )
            if driving:
                notify_admins("coords", text)
                if current_trip is not None:
                    current_trip["distance_m"] += distance
                    current_trip["last_lat"], current_trip["last_lon"] = new_lat, new_lon
                    current_trip["points"] += 1
            else:
                notify_admins_any({"coords", "coords_summary"}, text)

    # 4. Потеря/восстановление связи
    old_online = old_data.get("isOnline")
    new_online = new_data.get("isOnline")
    if old_online is not None and new_online is not None and old_online != new_online:
        if new_online:
            notify_admins("connectivity", "✅ Связь с автомобилем восстановлена")
        else:
            notify_admins("connectivity", "⚠️ Потеряна связь с автомобилем")

    # 5. Двери/багажник открылись на стоянке
    if new_data.get("isParked"):
        for key, new_val in new_sensors.items():
            if not DOOR_LIKE_PATTERN.match(key):
                continue
            if old_sensors.get(key) == 0 and new_val == 1:
                label = DOOR_LABELS.get(key, key)
                notify_admins("doors", f"🚪 {label}: открыт(а) на стоянке")

    # 6. Батарея зарядилась до 100% (ничего не шлём, пока идёт сама зарядка —
    #    только в момент, когда % впервые достиг 100)
    old_batt = old_sensors.get("batteryPercentage")
    new_batt = new_sensors.get("batteryPercentage")
    if isinstance(old_batt, (int, float)) and isinstance(new_batt, (int, float)) and old_batt < 100 <= new_batt:
        notify_admins("battery_full", "🔋 Зарядка завершена: батарея заряжена на 100%")


# ---------------------------------------------------------------------------
# Мониторинг 12V АКБ: низкое напряжение, подзарядка, суточная сводка.
#
# Что известно по реальной истории этой машины:
#   - в покое 12V стоит на ~12.75-12.85 В (после поездок первые часы чуть
#     ниже, ~12.55-12.6 В);
#   - подзарядка идёт от DC-DC и держится на 13.57-13.70 В в поездке и
#     13.73-13.76 В во время зарядки тяговой батареи на стоянке (зажигание
#     при этом выкл); в момент остановки DC-DC напряжение падает сразу;
#   - сразу после остановки DC-DC (конец поездки/зарядки) 12V на 1-3 минуты
#     проваливается до ~12.38-12.5 В и потом возвращается к 12.55+. Это не
#     разряд, поэтому "низкое напряжение" подтверждается по ВРЕМЕНИ, а не по
#     числу снимков.
#
# Алерты:
#   "voltage_low"    — мгновенные: низкое/критически низкое напряжение, восстановление;
#   "voltage_charge" — суточная сводка о подзарядках (VOLTAGE_DIGEST_TIME) и,
#                      если включено VOLTAGE_CHARGE_NOTIFY, мгновенные сообщения
#                      об окончании подзарядки.
# Каждая завершённая подзарядка пишется в таблицу voltage_charge_events.
# Как и "archive", админу с явным списком типов их нужно добавить отдельно.
# ---------------------------------------------------------------------------
VOLTAGE_SENSOR_KEY = os.getenv("VOLTAGE_SENSOR_KEY", "12VBatteryVoltage").strip()
VOLTAGE_LOW_WARN = float(os.getenv("VOLTAGE_LOW_WARN", 12.4))
VOLTAGE_LOW_CRIT = float(os.getenv("VOLTAGE_LOW_CRIT", 12.0))
VOLTAGE_LOW_RECOVER = float(os.getenv("VOLTAGE_LOW_RECOVER", 12.6))   # гистерезис выхода из "низкого"
VOLTAGE_LOW_CONFIRM_MINUTES = float(os.getenv("VOLTAGE_LOW_CONFIRM_MINUTES", 15))      # сколько держится <= WARN
VOLTAGE_LOW_CRIT_CONFIRM_MINUTES = float(os.getenv("VOLTAGE_LOW_CRIT_CONFIRM_MINUTES", 5))  # сколько держится <= CRIT
VOLTAGE_LOW_REMINDER_HOURS = float(os.getenv("VOLTAGE_LOW_REMINDER_HOURS", 6))  # 0 = без напоминаний
VOLTAGE_CHARGE_ON = float(os.getenv("VOLTAGE_CHARGE_ON", 13.3))      # >= этого — идёт подзарядка
VOLTAGE_CHARGE_OFF = float(os.getenv("VOLTAGE_CHARGE_OFF", 13.1))    # < этого — подзарядка закончилась
VOLTAGE_CONFIRM_SNAPSHOTS = int(os.getenv("VOLTAGE_CONFIRM_SNAPSHOTS", 2))  # свежих снимков подряд для старта/конца подзарядки
VOLTAGE_CHARGE_MIN_MINUTES = float(os.getenv("VOLTAGE_CHARGE_MIN_MINUTES", 2))  # короче — шум, в историю/сводку не попадает
# Мгновенное сообщение об окончании подзарядки: none | unknown (только если
# источник не определён — внешнее ЗУ/"прикуриватель") | all
VOLTAGE_CHARGE_NOTIFY = os.getenv("VOLTAGE_CHARGE_NOTIFY", "none").strip().lower()
VOLTAGE_DIGEST_TIME = os.getenv("VOLTAGE_DIGEST_TIME", "20:10").strip()   # "" = сводка выключена
VOLTAGE_DIGEST_TZ = os.getenv("VOLTAGE_DIGEST_TZ", "Europe/Moscow").strip()

voltage_state = {
    "key": None, "value": None, "ts": None, "stamp": None,
    "low_level": None, "low_seen": None, "low_since": None, "low_last_notified": 0.0,
    "charging": False, "on_count": 0, "off_count": 0, "charge": None,
}

VOLTAGE_KIND_LABELS = {
    "hv_charge": "зарядка тяговой на стоянке",
    "trip": "поездка",
    "unknown": "источник не определён (внешнее ЗУ/фон)",
}


def _find_voltage(sensors: dict):
    """(ключ, значение в вольтах) 12V-напряжения из sensorsData или (None, None).
    Сначала VOLTAGE_SENSOR_KEY, иначе первый ключ с 'volt' в названии (кроме
    явно высоковольтных). Значения вне 6-20 В считаем мусором."""
    candidates = []
    if VOLTAGE_SENSOR_KEY and VOLTAGE_SENSOR_KEY in sensors:
        candidates.append(VOLTAGE_SENSOR_KEY)
    candidates += [k for k in sensors
                   if k != VOLTAGE_SENSOR_KEY and re.search(r"volt", k, re.I)
                   and not re.search(r"hv|high|traction|cell", k, re.I)]
    for k in candidates:
        val = sensors.get(k)
        if isinstance(val, (int, float)) and 6 <= val <= 20:
            return k, float(val)
    return None, None


def _utc_iso(ts: float) -> str:
    return datetime.utcfromtimestamp(ts).isoformat()


def _save_voltage_event(ch: dict, kind: str, minutes: float):
    try:
        with db_lock, sqlite3.connect(DB_FILE) as conn:
            conn.execute(
                "INSERT INTO voltage_charge_events "
                "(ts_start, ts_end, kind, minutes, v_min, v_peak, pct_start, pct_end) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (_utc_iso(ch["start"]), _utc_iso(ch["last_high"]), kind, round(minutes, 1),
                 ch["v_min"], ch["v_peak"], ch["pct_start"], ch["pct_last"]),
            )
    except Exception as e:
        logger.error(f"Failed to save voltage event: {e}")


def _touch_charge(ch: dict, v: float, batt, powered: bool, now: float):
    ch["last_high"] = now
    ch["v_min"], ch["v_peak"] = min(ch["v_min"], v), max(ch["v_peak"], v)
    if batt is not None:
        ch["pct_last"] = batt
        if ch["pct_start"] is None:
            ch["pct_start"] = batt
    ch["ign"] = ch["ign"] or powered


def _finish_charge(ch: dict, now: float):
    minutes = max(ch["last_high"] - ch["start"], 0) / 60
    if minutes < VOLTAGE_CHARGE_MIN_MINUTES:
        return
    if ch["ign"]:
        kind = "trip"
    elif ch["pct_start"] is not None and ch["pct_last"] is not None and ch["pct_last"] - ch["pct_start"] >= 1:
        kind = "hv_charge"
    else:
        kind = "unknown"
    _save_voltage_event(ch, kind, minutes)
    if VOLTAGE_CHARGE_NOTIFY == "all" or (VOLTAGE_CHARGE_NOTIFY == "unknown" and kind == "unknown"):
        pct = (f", тяговая {ch['pct_start']:g}→{ch['pct_last']:g}%"
               if ch["pct_start"] is not None and ch["pct_last"] is not None else "")
        notify_admins("voltage_charge",
                      f"🔋 12V АКБ: подзарядка закончилась — {VOLTAGE_KIND_LABELS[kind]}\n"
                      f"{_fmt_minutes(minutes)}, {ch['v_min']:.2f}–{ch['v_peak']:.2f} В{pct}")


def check_voltage_alerts(data: dict):
    st = voltage_state
    if data.get("isOnline") is False:
        return  # машина офлайн — в данных последнее известное, не свежее значение
    # Один и тот же устаревший снимок не должен засчитываться как несколько
    # подтверждений подряд.
    stamp = data.get("carStateUpdatedAt") or data.get("lastOnlineTime")
    if stamp is not None and stamp == st["stamp"]:
        return
    st["stamp"] = stamp

    sensors = data.get("sensorsData") or {}
    key, v = _find_voltage(sensors)
    st["key"] = key
    if v is None:
        return
    now = time.time()
    st["value"], st["ts"] = v, datetime.utcnow().isoformat()

    batt = sensors.get("batteryPercentage")
    batt = batt if isinstance(batt, (int, float)) else None
    powered = bool(sensors.get("ignitionStatus")) or bool(sensors.get("ready"))

    # --- 1. Низкое напряжение: подтверждается ВРЕМЕНЕМ, с гистерезисом и напоминанием ---
    level = "crit" if v <= VOLTAGE_LOW_CRIT else "warn" if v <= VOLTAGE_LOW_WARN else None
    if level:
        if level != st["low_seen"]:
            st["low_seen"], st["low_since"] = level, now  # смена уровня подтверждаем заново
        need = (VOLTAGE_LOW_CRIT_CONFIRM_MINUTES if level == "crit" else VOLTAGE_LOW_CONFIRM_MINUTES) * 60
        if now - st["low_since"] >= need:
            remind = (VOLTAGE_LOW_REMINDER_HOURS > 0
                      and now - st["low_last_notified"] >= VOLTAGE_LOW_REMINDER_HOURS * 3600)
            escalated = level == "crit" and st["low_level"] == "warn"
            if st["low_level"] is None or escalated or remind:
                icon, title = ("🛑", "критически низкое") if level == "crit" else ("🪫", "низкое")
                lines = [f"{icon} 12V АКБ: {title} напряжение — {v:.2f} В "
                         f"(держится ~{(now - st['low_since']) / 60:.0f} мин)"]
                if powered:
                    lines.append("Машина включена, а напряжение низкое — возможно, не работает подзарядка (DC-DC).")
                else:
                    lines.append("Батарея разряжена — риск, что машина не проснётся/не заведётся.")
                notify_admins("voltage_low", "\n".join(lines))
                st["low_level"], st["low_last_notified"] = level, now
    else:
        st["low_seen"], st["low_since"] = None, None
        if st["low_level"] and v >= VOLTAGE_LOW_RECOVER:
            notify_admins("voltage_low", f"✅ 12V АКБ: напряжение восстановилось — {v:.2f} В")
            st["low_level"] = None

    # --- 2. Подзарядка 12V: по росту напряжения (ON/OFF с гистерезисом) ---
    if not st["charging"]:
        if v >= VOLTAGE_CHARGE_ON:
            if st["on_count"] == 0:
                st["charge"] = {"start": now, "last_high": now, "v_min": v, "v_peak": v,
                                "pct_start": batt, "pct_last": batt, "ign": powered}
            else:
                _touch_charge(st["charge"], v, batt, powered, now)
            st["on_count"] += 1
            if st["on_count"] >= VOLTAGE_CONFIRM_SNAPSHOTS:
                st["charging"], st["on_count"], st["off_count"] = True, 0, 0
        else:
            st["on_count"], st["charge"] = 0, None
    else:
        ch = st["charge"]
        if v >= VOLTAGE_CHARGE_OFF:
            _touch_charge(ch, v, batt, powered, now)
            st["off_count"] = 0
        else:
            st["off_count"] += 1
            if st["off_count"] >= VOLTAGE_CONFIRM_SNAPSHOTS:
                _finish_charge(ch, now)
                st["charging"], st["charge"], st["on_count"], st["off_count"] = False, None, 0, 0


# --- Суточная сводка -------------------------------------------------------
def _fmt_minutes(m: float) -> str:
    m = int(round(m))
    h, mm = divmod(m, 60)
    return f"{h} ч {mm:02d} мин" if h else f"{mm} мин"


def _digest_tz():
    try:
        return ZoneInfo(VOLTAGE_DIGEST_TZ)
    except Exception:
        logger.warning(f"Voltage digest: часовой пояс '{VOLTAGE_DIGEST_TZ}' недоступен "
                       f"(нет tzdata?), использую UTC+3")
        return timezone(timedelta(hours=3))


def _digest_hm():
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", VOLTAGE_DIGEST_TIME or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return None
    return int(m.group(1)), int(m.group(2))


def _meta_get(key: str):
    with db_lock, sqlite3.connect(DB_FILE) as conn:
        row = conn.execute("SELECT value FROM voltage_meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _meta_set(key: str, value: str):
    with db_lock, sqlite3.connect(DB_FILE) as conn:
        conn.execute("INSERT OR REPLACE INTO voltage_meta (key, value) VALUES (?, ?)", (key, value))


def build_voltage_digest(end_local: datetime) -> str:
    """Сводка за 24 часа, заканчивающиеся в end_local (aware datetime)."""
    tz = end_local.tzinfo
    start_local = end_local - timedelta(days=1)
    to_utc = lambda d: d.astimezone(timezone.utc).replace(tzinfo=None).isoformat()
    s_iso, e_iso = to_utc(start_local), to_utc(end_local)
    loc = lambda iso: datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).astimezone(tz).strftime("%H:%M")

    with db_lock, sqlite3.connect(DB_FILE) as conn:
        events = conn.execute(
            "SELECT ts_start, ts_end, kind, minutes, v_min, v_peak, pct_start, pct_end "
            "FROM voltage_charge_events WHERE ts_end > ? AND ts_end <= ? ORDER BY ts_start",
            (s_iso, e_iso)).fetchall()
        raw_rows = conn.execute(
            "SELECT data FROM sensor_history WHERE ts > ? AND ts <= ?", (s_iso, e_iso)).fetchall()

    items = [(a, b, k, m, lo, hi, p0, p1, False) for a, b, k, m, lo, hi, p0, p1 in events]
    ch = voltage_state["charge"] if voltage_state["charging"] else None
    if ch:  # подзарядка идёт прямо сейчас
        kind = ("trip" if ch["ign"] else "hv_charge" if ch["pct_start"] is not None and ch["pct_last"] is not None
                and ch["pct_last"] - ch["pct_start"] >= 1 else "unknown")
        items.append((_utc_iso(ch["start"]), _utc_iso(ch["last_high"]), kind,
                      (ch["last_high"] - ch["start"]) / 60, ch["v_min"], ch["v_peak"],
                      ch["pct_start"], ch["pct_last"], True))

    lines = [f"🔋 12V АКБ — сводка за сутки (до {end_local.strftime('%H:%M')})", ""]
    if items:
        total = sum(i[3] for i in items)
        lines.append(f"Подзарядок 12V: {len(items)}, всего {_fmt_minutes(total)}")
        for a, b, k, m, lo, hi, p0, p1, ongoing in items[:15]:
            pct = f" · тяговая {p0:g}→{p1:g}%" if p0 is not None and p1 is not None else ""
            warn = "⚠️ " if k == "unknown" else ""
            end = "идёт" if ongoing else loc(b)
            lines.append(f"• {warn}{loc(a)}–{end} · {VOLTAGE_KIND_LABELS.get(k, k)} · "
                         f"{_fmt_minutes(m)} · {lo:.2f}–{hi:.2f} В{pct}")
        if len(items) > 15:
            lines.append(f"… и ещё {len(items) - 15}")
    else:
        lines.append("Подзарядок за сутки не было.")

    rest = []
    for (raw,) in raw_rows:
        try:
            d = json.loads(raw)
            s = d.get("sensorsData") or {}
            _, val = _find_voltage(s)
            if val is not None and not s.get("ignitionStatus") and val < VOLTAGE_CHARGE_OFF:
                rest.append(val)
        except Exception:
            continue
    if len(rest) >= 10:
        rest.sort()
        lines += ["", f"В покое (вне подзарядки): медиана {rest[len(rest) // 2]:.2f} В, "
                      f"диапазон {rest[0]:.2f}–{rest[-1]:.2f} В"]
    st = voltage_state
    if st["value"] is not None:
        lines.append(f"Сейчас: {st['value']:.2f} В")
    if st["low_level"]:
        lines.append(f"⚠️ Активно предупреждение о низком напряжении ({st['low_level']}).")
    return "\n".join(lines)


def voltage_digest_tick():
    """Раз в минуту проверяет, не пора ли слать суточную сводку. Дата последней
    отправки хранится в БД, поэтому перезапуск сервиса не даёт дублей."""
    try:
        hm = _digest_hm()
        if hm:
            now_local = datetime.now(_digest_tz())
            today = now_local.date().isoformat()
            if (now_local.hour, now_local.minute) >= hm and _meta_get("digest_last_date") != today:
                end_local = now_local.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
                notify_admins("voltage_charge", build_voltage_digest(end_local))
                _meta_set("digest_last_date", today)
    except Exception as e:
        logger.error(f"Voltage digest failed: {e}")
    finally:
        t = threading.Timer(60, voltage_digest_tick)
        t.daemon = True
        t.start()


# sqlite3-соединения не шарятся между потоками (у нас Flask-потоки запросов +
# отдельные threading.Timer для периодического refresh/fetch), поэтому проще
# и безопаснее открывать короткое соединение на каждую операцию, а не держать
# одно общее. Лок нужен, чтобы не ловить "database is locked" при параллельной
# записи из cron-потоков и запроса одновременно.
db_lock = threading.Lock()

def init_db():
    with db_lock, sqlite3.connect(DB_FILE) as conn:
        # ВАЖНО: сам по себе DELETE в SQLite не уменьшает размер файла на
        # диске — освободившиеся страницы просто попадают в внутренний
        # freelist и переиспользуются под будущие INSERT, но файл не
        # усыхает. Чтобы место реально возвращалось ОС (а не только
        # переиспользовалось), включаем incremental auto_vacuum и после
        # каждой чистки дёргаем PRAGMA incremental_vacuum (см. ниже).
        # Режим применяется только к пустой БД или требует разового полного
        # VACUUM для уже существующей — поэтому проверяем текущий режим и
        # конвертируем один раз, если нужно (безопасно дергать при каждом
        # старте, VACUUM на уже сконвертированной БД просто skip'нется).
        cur_mode = conn.execute("PRAGMA auto_vacuum").fetchone()[0]
        if cur_mode != 2:  # 2 = INCREMENTAL
            conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
            conn.execute("VACUUM")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sensor_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                data TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS token_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                event TEXT NOT NULL,
                detail TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sensor_ts ON sensor_history(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_token_ts ON token_history(ts)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS voltage_charge_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_start TEXT NOT NULL,
                ts_end TEXT NOT NULL,
                kind TEXT NOT NULL,
                minutes REAL,
                v_min REAL,
                v_peak REAL,
                pct_start REAL,
                pct_end REAL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_vevent_end ON voltage_charge_events(ts_end)")
        conn.execute("CREATE TABLE IF NOT EXISTS voltage_meta (key TEXT PRIMARY KEY, value TEXT)")

def log_sensor_snapshot(data):
    ts = datetime.utcnow().isoformat()
    try:
        with db_lock, sqlite3.connect(DB_FILE) as conn:
            conn.execute(
                "INSERT INTO sensor_history (ts, data) VALUES (?, ?)",
                (ts, json.dumps(data)),
            )
    except Exception as e:
        logger.error(f"Failed to log sensor snapshot: {e}")

def log_token_event(event, detail=None):
    ts = datetime.utcnow().isoformat()
    try:
        with db_lock, sqlite3.connect(DB_FILE) as conn:
            conn.execute(
                "INSERT INTO token_history (ts, event, detail) VALUES (?, ?, ?)",
                (ts, event, detail),
            )
    except Exception as e:
        logger.error(f"Failed to log token event: {e}")

def _rows_to_jsonl_gz(rows: list, columns: list[str], path: str) -> int:
    """Пишет rows как JSON-lines (по объекту в строке) сразу в gzip-файл.
    Возвращает итоговый размер файла в байтах."""
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(dict(zip(columns, row)), ensure_ascii=False) + "\n")
    return os.path.getsize(path)


def _split_rows_to_gzip_chunks(rows: list, columns: list[str], tmp_dir: str, prefix: str) -> list[str]:
    """Делит rows на .jsonl.gz файлы, каждый из которых гарантированно не
    больше ARCHIVE_CHUNK_MAX_BYTES. Рекурсивно делит пополам, пока чанк не
    влезет — это не самый быстрый способ (файл может пересобираться
    несколько раз), зато не зависит от угадывания коэффициента сжатия:
    сколько бы JSON ни весил и как бы хорошо/плохо ни сжимался конкретный
    кусок истории, на выходе гарантированно получаем файлы под лимит."""
    chunks = []

    def _recurse(sub_rows, name):
        path = os.path.join(tmp_dir, f"{name}.jsonl.gz")
        size = _rows_to_jsonl_gz(sub_rows, columns, path)
        if size <= ARCHIVE_CHUNK_MAX_BYTES or len(sub_rows) <= 1:
            chunks.append(path)
            return
        os.remove(path)
        mid = len(sub_rows) // 2
        _recurse(sub_rows[:mid], name + "a")
        _recurse(sub_rows[mid:], name + "b")

    if rows:
        _recurse(rows, prefix)
    return chunks


def _archive_once() -> dict:
    """Один проход архивации+чистки, БЕЗ планирования следующего запуска.
    Вынесено в отдельную функцию из archive_and_cleanup_history(), чтобы
    её можно было безопасно дёрнуть вручную (например для ручного теста
    из консоли контейнера) — вызов archive_and_cleanup_history() напрямую
    для этого не годится, он в конце сам планирует свой следующий вызов
    через threading.Timer и в одноразовом скрипте это просто зависший
    процесс.
    Логика:
    1) выгружает из sensor_history/token_history всё старше ARCHIVE_KEEP_DAYS
       в сжатые .jsonl.gz (порезанные под лимит файла бота Telegram);
    2) отправляет получившиеся части в TELEGRAM_ARCHIVE_CHAT_ID;
    3) ТОЛЬКО если ВСЕ части успешно отправлены — удаляет эти строки из
       рабочей БД и возвращает освободившееся место на диск через
       incremental_vacuum. Если отправка чего-то не удалась — ничего не
       удаляется, попробуем снова при следующем запуске.
    Если TELEGRAM_ARCHIVE_CHAT_ID (или TELEGRAM_BOT_TOKEN) не задан, шаги
    1-3 пропускаются — это явно отражается в возвращаемом result, чтобы
    вызывающий код мог предупредить о неполной настройке, а не просто
    молчать. В любом случае в конце выполняется старая страховочная чистка
    по HISTORY_RETENTION_DAYS, чтобы БД не росла бесконечно, даже если
    архивация выключена или постоянно падает.
    Возвращает словарь с деталями прогона — для логов, /archive_status и
    Telegram-уведомления типа "archive"."""
    run_started = datetime.utcnow()
    result = {
        "started": run_started.isoformat(),
        "bot_token_set": bool(TELEGRAM_BOT_TOKEN),
        "archive_chat_set": bool(TELEGRAM_ARCHIVE_CHAT_ID),
        "config_ok": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_ARCHIVE_CHAT_ID),
        "sensor_rows": 0,
        "token_rows": 0,
        "chunks_sent": 0,
        "archived_ok": None,  # None = архивация в Telegram пропущена (не настроена)
        "error": None,
    }

    if TELEGRAM_ARCHIVE_CHAT_ID:
        cutoff = (run_started - timedelta(days=ARCHIVE_KEEP_DAYS)).isoformat()
        tmp_dir = tempfile.mkdtemp(prefix="evolute_archive_")
        try:
            with db_lock, sqlite3.connect(DB_FILE) as conn:
                sensor_rows = conn.execute(
                    "SELECT id, ts, data FROM sensor_history WHERE ts < ? ORDER BY ts",
                    (cutoff,),
                ).fetchall()
                token_rows = conn.execute(
                    "SELECT id, ts, event, detail FROM token_history WHERE ts < ? ORDER BY ts",
                    (cutoff,),
                ).fetchall()
            result["sensor_rows"] = len(sensor_rows)
            result["token_rows"] = len(token_rows)

            if sensor_rows or token_rows:
                date_tag = run_started.strftime("%Y%m%d_%H%M")
                chunks = _split_rows_to_gzip_chunks(
                    sensor_rows, ["id", "ts", "data"], tmp_dir, f"sensor_history_{date_tag}"
                ) + _split_rows_to_gzip_chunks(
                    token_rows, ["id", "ts", "event", "detail"], tmp_dir, f"token_history_{date_tag}"
                )

                all_ok = True
                for i, path in enumerate(chunks, 1):
                    caption = f"evolute архив {date_tag}: {os.path.basename(path)} ({i}/{len(chunks)})"
                    if send_document(TELEGRAM_ARCHIVE_CHAT_ID, path, caption) is None:
                        all_ok = False
                        logger.error(f"Archive chunk send failed on {path}, aborting this run")
                        break
                    result["chunks_sent"] += 1
                    # небольшая пауза между сообщениями, чтобы не словить
                    # flood control одного чата в Telegram
                    time.sleep(2)

                if all_ok:
                    with db_lock, sqlite3.connect(DB_FILE) as conn:
                        conn.execute("DELETE FROM sensor_history WHERE ts < ?", (cutoff,))
                        conn.execute("DELETE FROM token_history WHERE ts < ?", (cutoff,))
                        conn.execute("PRAGMA incremental_vacuum")
                    result["archived_ok"] = True
                    logger.info(
                        f"Archived & purged {len(sensor_rows)} sensor_history + "
                        f"{len(token_rows)} token_history rows older than {cutoff}"
                    )
                else:
                    result["archived_ok"] = False
                    result["error"] = "send_failed"
                    logger.error("Archive run incomplete — old rows kept in DB, will retry next run")
            else:
                # Не ошибка: конфигурация в порядке, просто ещё нет записей
                # старше ARCHIVE_KEEP_DAYS — самый частый случай, из-за
                # которого архив как будто "не приходит".
                result["archived_ok"] = True
                logger.info(f"Archive: nothing older than {cutoff} yet, nothing to send")
        except Exception as e:
            result["archived_ok"] = False
            result["error"] = str(e)
            logger.error(f"Archive job failed: {e}")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
    else:
        logger.warning(
            "Archive: TELEGRAM_ARCHIVE_CHAT_ID не задан — архивация БД в "
            "Telegram выключена, работает только страховочная чистка по "
            "HISTORY_RETENTION_DAYS"
        )

    # Страховочная чистка (старое поведение) — работает независимо от
    # архивации, чтобы совсем старые данные не копились бесконечно ни при
    # каких обстоятельствах.
    try:
        with db_lock, sqlite3.connect(DB_FILE) as conn:
            conn.execute(
                "DELETE FROM sensor_history WHERE ts < datetime('now', ?)",
                (f"-{HISTORY_RETENTION_DAYS} days",),
            )
            conn.execute(
                "DELETE FROM token_history WHERE ts < datetime('now', ?)",
                (f"-{HISTORY_RETENTION_DAYS} days",),
            )
            conn.execute("PRAGMA incremental_vacuum")
    except Exception as e:
        logger.error(f"Failed to clean up history: {e}")

    return result


def archive_and_cleanup_history():
    """Периодический драйвер: раз в ARCHIVE_INTERVAL_DAYS суток выполняет
    один проход _archive_once(), логирует и шлёт в Telegram (алерт типа
    "archive") короткую сводку о результате вместе со временем следующего
    запуска, обновляет status_info (виден через /archive_status), и
    планирует свой следующий вызов."""
    result = _archive_once()
    next_run = datetime.utcnow() + timedelta(days=ARCHIVE_INTERVAL_DAYS)
    next_run_str = next_run.strftime("%Y-%m-%d %H:%M UTC")

    if not result["config_ok"]:
        missing = "TELEGRAM_BOT_TOKEN" if not result["bot_token_set"] else "TELEGRAM_ARCHIVE_CHAT_ID"
        summary = (
            f"⚠️ Архивация БД в Telegram не настроена (не задан {missing}).\n"
            "Работает только страховочная чистка по HISTORY_RETENTION_DAYS, "
            "файлы архива отправляться не будут.\n"
            f"Следующая проверка: {next_run_str}"
        )
    elif result["archived_ok"] is False:
        summary = (
            f"❌ Архивация БД не удалась ({result['error']}). Старые записи "
            "оставлены в рабочей БД, повторим на следующем запуске.\n"
            f"Следующая попытка: {next_run_str}"
        )
    elif result["sensor_rows"] == 0 and result["token_rows"] == 0:
        summary = (
            "📦 Архивация БД: отправлять пока нечего "
            f"(нет записей старше {ARCHIVE_KEEP_DAYS:g} дн.).\n"
            f"Следующая проверка: {next_run_str}"
        )
    else:
        summary = (
            f"📦 Архив БД отправлен: {result['sensor_rows']} sensor + "
            f"{result['token_rows']} token записей, {result['chunks_sent']} файл(ов).\n"
            f"Следующая архивация: {next_run_str}"
        )

    logger.info(f"Archive run summary: {summary}")
    notify_admins("archive", summary)

    status_info["last_archive_run"] = result["started"]
    status_info["last_archive_summary"] = summary
    status_info["next_archive_run"] = next_run.isoformat()
    write_json_file(STATUS_FILE, status_info)

    t = threading.Timer(ARCHIVE_INTERVAL_DAYS * 86400, archive_and_cleanup_history)
    t.daemon = True
    t.start()

sensors_data = {}
status_info = {
    "start_time": datetime.utcnow().isoformat(),
    "last_token_update": None,
    "last_sensor_update": None,
    "last_archive_run": None,
    "last_archive_summary": None,
    "next_archive_run": None,
}
tokens_ok = False
start_timestamp = time.time()

def read_json_file(filename, default=None):
    try:
        with open(filename, "r") as f:
            return json.load(f)
    except Exception:
        return default or {}

def write_json_file(filename, data):
    try:
        with open(filename, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to write {filename}: {e}")

def load_token(filename):
    try:
        with open(filename, "r") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""

def save_token(filename, token):
    with open(filename, "w") as f:
        f.write(token.strip())

def get_tokens():
    return {
        "access": load_token(EVOLUTE_TOKEN_FILENAME),
        "refresh": load_token(EVOLUTE_REFRESH_TOKEN_FILENAME),
    }

def update_status(key):
    status_info[key] = datetime.utcnow().isoformat()
    write_json_file(STATUS_FILE, status_info)

def refresh_tokens():
    global tokens_ok, current_refresh_interval
    try:
        tokens = get_tokens()
        payload = {"refreshToken": tokens["refresh"]}
        response = requests.post(EVOLUTE_REFRESH_URL, json=payload, timeout=TIMEOUT)
        response.raise_for_status()

        data = response.json()
        save_token(EVOLUTE_TOKEN_FILENAME, data["accessToken"])
        save_token(EVOLUTE_REFRESH_TOKEN_FILENAME, data["refreshToken"])
        update_status("last_token_update")
        tokens_ok = True
        log_token_event("success")

        if current_refresh_interval != REFRESH_INTERVAL:
            logger.info(f"Token refresh successful. Resetting interval to {REFRESH_INTERVAL}s")
            current_refresh_interval = REFRESH_INTERVAL
        else:
            logger.info("Tokens refreshed successfully")

    except requests.exceptions.HTTPError as e:
        tokens_ok = False
        if e.response is not None and e.response.status_code == 403:
            new_interval = min(current_refresh_interval * 2, 3600)
            logger.warning(f"403 Forbidden during token refresh. Increasing cooldown from {current_refresh_interval}s to {new_interval}s")
            current_refresh_interval = new_interval
            log_token_event("403", f"cooldown->{new_interval}s")
        else:
            logger.error(f"HTTP error refreshing tokens: {e}")
            log_token_event("http_error", str(e))

    except Exception as e:
        tokens_ok = False
        logger.error(f"Failed to refresh tokens: {e}")
        log_token_event("error", str(e))

def _num(v, ndigits=3):
    """'56' -> 56, '12.65999984741211' -> 12.66. Всё, что не строка-число, возвращает как есть.
    ndigits=None — не округлять (для координат)."""
    if isinstance(v, str):
        try:
            return int(v)
        except ValueError:
            try:
                f = float(v)
            except ValueError:
                return v
            return f if ndigits is None else round(f, ndigits)
    return v


def _ms_to_iso(ms):
    try:
        return datetime.utcfromtimestamp(ms / 1000).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _estimate_speed_kmh(prev_pos, prev_at, pos, pos_at):
    """В telemetry нет скорости — оцениваем по смещению между двумя снимками.
    Это средняя скорость за интервал опроса, а не мгновенная."""
    try:
        coords = (prev_pos.get("lat"), prev_pos.get("lon"), pos.get("lat"), pos.get("lon"))
        if None in coords or prev_at is None or pos_at is None:
            return None
        dt = (pos_at - prev_at) / 1000.0
        if dt <= 0:
            return prev_pos.get("speed")  # новой GPS-точки не было
        if dt > 900:
            return None  # слишком большой разрыв, оценка бессмысленна
        dist = haversine_meters(*coords)
        if dist < 20:  # дрейф GPS на стоянке
            return 0
        return round(dist / dt * 3.6, 1)
    except Exception:
        return None


def normalize_telemetry(raw: dict, prev: dict) -> dict:
    """Приводит ответ /client-bff-service/telemetry/<IMEI> к тому же виду, что давал
    /info: positionData + sensorsData + isOnline/isParked/lastOnlineTime/prep*.
    Так /sensors/all, история в БД, алерты и веб-клиент работают без изменений."""
    src = dict(raw.get("sensors") or {})
    buttons = raw.get("buttons") or []

    pos = {}
    for k in ("lat", "lon", "course"):
        if k in src:
            pos[k] = _num(src.pop(k), None)
    speed = _num(src.pop("speed", None), None)
    src.pop("isOnline", None)

    sensors = {k: _num(v) for k, v in src.items()}

    # Имена, которые ждёт остальной код (алерты, /tbox-i, клиент)
    if "isIgnitionOn" in sensors:
        sensors["ignitionStatus"] = int(bool(sensors["isIgnitionOn"]))
    if "isCentralLockingOn" in sensors:
        sensors["centralLockingStatus"] = int(bool(sensors["isCentralLockingOn"]))
    if buttons:
        sensors["climateStatus"] = int(any(
            b.get("status") for b in buttons if b.get("activateCommand") in ("heatingOn", "coolingOn")))

    pos_at = raw.get("positionAt")
    if speed is None:
        speed = _estimate_speed_kmh((prev or {}).get("positionData") or {},
                                    (prev or {}).get("positionAt"), pos, pos_at)
        if speed is not None:
            pos["speedEstimated"] = True
    if speed is not None:
        pos["speed"] = speed

    prep = next((b for b in buttons if b.get("tag") == "trip-preparation"), {})
    return {
        "positionData": pos,
        "sensorsData": sensors,
        "isOnline": raw.get("isOnline"),
        "onlineState": raw.get("onlineState"),
        "isParked": sensors.get("isParkedOn"),
        "lastOnlineTime": _ms_to_iso(raw.get("carStateUpdatedAt")),
        "carStateUpdatedAt": raw.get("carStateUpdatedAt"),
        "positionAt": pos_at,
        "prepRunning": prep.get("status"),
        "prepAvailable": prep.get("enabled"),
    }


def fetch_sensor_data():
    global sensors_data, current_sensor_interval, last_moving_ts
    if not tokens_ok:
        logger.warning("Sensor data fetch skipped: tokens are not active")
        return
    try:
        tokens = get_tokens()
        cookies = {
            "evy-platform-access": tokens["access"],
            "evy-platform-refresh": tokens["refresh"]
        }
        if EVOLUTE_TELEMETRY_URL:
            headers = {
                "User-Agent": USER_AGENT,
                "x-app": "web",
                "time-zone": TELEMETRY_TIME_ZONE,
            }
            response = requests.get(EVOLUTE_TELEMETRY_URL, headers=headers, cookies=cookies, timeout=TIMEOUT)
            response.raise_for_status()
            data = normalize_telemetry(response.json(), sensors_data)
        else:
            headers = {
                "User-Agent": USER_AGENT
            }
            response = requests.get(EVOLUTE_SENSOR_URL, headers=headers, cookies=cookies, timeout=TIMEOUT)
            response.raise_for_status()
            raw = response.json()

            keys = JSON_SUB.strip(".").split(".")
            data = raw
            for k in keys:
                data = data.get(k, {})

            # JSON_SUB=".sensors" отбрасывает всё, кроме ветки sensors — а эти поля
            # лежат в корне raw-ответа рядом с ней. Подмешиваем их как соседей
            # sensorsData/positionData, чтобы забирать через уже существующий
            # /sensors/<name> (isOnline, isParked, lastOnlineTime, prepRunning, prepAvailable)
            # без отдельных новых эндпоинтов.
            prep = raw.get("preparation_script") or {}
            data["isOnline"] = raw.get("isOnline")
            data["isParked"] = raw.get("isParked")
            data["lastOnlineTime"] = raw.get("lastOnlineTime")
            data["prepRunning"] = prep.get("running")
            data["prepAvailable"] = prep.get("available")

            if not data.get("sensorsData"):
                logger.warning(
                    "В ответе нет sensorsData (JSON_SUB=%r): машина офлайн или изменилась "
                    "структура ответа — проверьте probe_api.py raw", JSON_SUB)

        try:
            check_sensor_alerts(sensors_data, data)
        except Exception as e:
            logger.error(f"Sensor alert check failed: {e}")
        try:
            check_voltage_alerts(data)
        except Exception as e:
            logger.error(f"Voltage alert check failed: {e}")

        sensors_data = data
        update_status("last_sensor_update")
        write_json_file(DUMP_FILE, sensors_data)
        log_sensor_snapshot(sensors_data)
        logger.info("Sensor data updated")

        # Адаптивный интервал опроса, с "остыванием" через
        # SENSORS_REFRESH_COOLDOWN_SECONDS (см. комментарий у константы) —
        # чтобы пробка (в том числе под эвакуатором) не сбрасывала опрос на
        # медленный раньше времени. Пересчитываем только после УСПЕШНОГО
        # снимка — если запрос упал с ошибкой, у нас нет свежих данных,
        # чтобы понять едет машина или нет, поэтому в этом случае интервал
        # не трогаем.
        new_sensors = data.get("sensorsData") or {}
        new_speed = (data.get("positionData") or {}).get("speed")
        moving = bool(new_sensors.get("ignitionStatus")) or (
            isinstance(new_speed, (int, float)) and new_speed > 0
        )
        now = time.time()
        if moving:
            last_moving_ts = now
            desired_interval = SENSORS_REFRESH_INTERVAL_DRIVING
        elif last_moving_ts is not None and (now - last_moving_ts) < SENSORS_REFRESH_COOLDOWN_SECONDS:
            # стоим, но недавно ещё двигались (пробка/светофор/затор под
            # эвакуатором) — остаёмся на быстром интервале "про запас"
            desired_interval = SENSORS_REFRESH_INTERVAL_DRIVING
        else:
            desired_interval = SENSORS_REFRESH_INTERVAL
        if desired_interval != current_sensor_interval:
            logger.info(
                f"Sensor poll interval switching to {desired_interval}s "
                f"({'driving/moving' if moving else 'cooldown' if desired_interval == SENSORS_REFRESH_INTERVAL_DRIVING else 'idle'})"
            )
            current_sensor_interval = desired_interval
    except Exception as e:
        logger.error(f"Failed to fetch sensor data: {e}")

def periodic_refresh():
    refresh_tokens()
    t = threading.Timer(current_refresh_interval, periodic_refresh)
    t.daemon = True
    t.start()

def periodic_fetch():
    fetch_sensor_data()
    t = threading.Timer(current_sensor_interval, periodic_fetch)
    t.daemon = True
    t.start()

# CORS: без этого браузер заблокирует запросы со страницы html-клиента,
# т.к. она открыта с другого origin, чем сам proxy.
# ALLOWED_ORIGIN можно сузить до конкретного домена, где лежит html-клиент;
# "*" годится, если ключи всё равно секретные и передаются в заголовке.
ALLOWED_ORIGIN = os.getenv("ALLOWED_ORIGIN", "*")

@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = ALLOWED_ORIGIN
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-API-Key"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
    return response

def check_auth(req):
    key = req.headers.get("X-API-Key") or req.args.get("api_key")
    if key != API_KEY:
        abort(jsonify({"error": "Unauthorized"}), 401)

def check_auth_rw(req):
    key = req.headers.get("X-API-Key") or req.args.get("api_key")
    if key != API_KEY_RW:
        abort(jsonify({"error": "Unauthorized"}), 401)

@app.route("/ping", methods=["GET"])
def ping():
    return jsonify({"status": "ok"}), 200

@app.route("/status", methods=["GET"])
def status():
    uptime_seconds = time.time() - start_timestamp
    return jsonify({
        "alive": True,
        "uptime": uptime_seconds,
        "start_time": status_info["start_time"],
        "last_token_update": status_info["last_token_update"],
        "last_sensor_update": status_info["last_sensor_update"],
        "tokens_active": tokens_ok,
        "current_refresh_interval": current_refresh_interval,
        "current_sensor_interval": current_sensor_interval
    })

@app.route("/archive_status", methods=["GET"])
def archive_status():
    """Статус архивации БД в Telegram — когда был последний запуск, что он
    сделал (сводка тем же текстом, что уходит в Telegram-алерт "archive"),
    и когда ожидается следующий. Плюс текущая конфигурация, чтобы сразу
    было видно, чего не хватает, без похода в переменные окружения."""
    return jsonify({
        "archive_chat_configured": bool(TELEGRAM_ARCHIVE_CHAT_ID),
        "bot_token_configured": bool(TELEGRAM_BOT_TOKEN),
        "archive_interval_days": ARCHIVE_INTERVAL_DAYS,
        "archive_keep_days": ARCHIVE_KEEP_DAYS,
        "last_archive_run": status_info["last_archive_run"],
        "last_archive_summary": status_info["last_archive_summary"],
        "next_archive_run": status_info["next_archive_run"],
    })

@app.route("/voltage_status", methods=["GET"])
def voltage_status():
    """Состояние мониторинга 12V АКБ + какие ключи с 'volt' реально есть в
    последнем снимке (чтобы быстро найти правильный VOLTAGE_SENSOR_KEY)."""
    check_auth(request)
    sensors = (sensors_data or {}).get("sensorsData") or {}
    st = voltage_state
    return jsonify({
        "sensor_key_used": st["key"],
        "voltage_keys_in_last_snapshot": {k: v for k, v in sensors.items() if re.search(r"volt", k, re.I)},
        "last_value": st["value"], "last_value_ts": st["ts"],
        "low_level": st["low_level"], "low_since_seconds_ago": (time.time() - st["low_since"]) if st["low_since"] else None,
        "charging": st["charging"], "charge_in_progress": st["charge"],
        "thresholds": {
            "low_warn": VOLTAGE_LOW_WARN, "low_crit": VOLTAGE_LOW_CRIT, "low_recover": VOLTAGE_LOW_RECOVER,
            "low_confirm_minutes": VOLTAGE_LOW_CONFIRM_MINUTES, "low_crit_confirm_minutes": VOLTAGE_LOW_CRIT_CONFIRM_MINUTES,
            "charge_on": VOLTAGE_CHARGE_ON, "charge_off": VOLTAGE_CHARGE_OFF,
            "confirm_snapshots": VOLTAGE_CONFIRM_SNAPSHOTS, "reminder_hours": VOLTAGE_LOW_REMINDER_HOURS,
        },
        "digest": {"time": VOLTAGE_DIGEST_TIME, "tz": VOLTAGE_DIGEST_TZ, "charge_notify": VOLTAGE_CHARGE_NOTIFY,
                   "last_sent_date": _meta_get("digest_last_date")},
    })

@app.route("/voltage_digest", methods=["GET"])
def voltage_digest():
    """Предпросмотр суточной сводки (за 24 ч до текущего момента). С ?send=1 —
    ещё и отправляет её админам (нужен RW-ключ)."""
    if request.args.get("send") == "1":
        check_auth_rw(request)
    else:
        check_auth(request)
    text = build_voltage_digest(datetime.now(_digest_tz()))
    if request.args.get("send") == "1":
        notify_admins("voltage_charge", text)
    return Response(text, mimetype="text/plain; charset=utf-8")

@app.route("/set_tokens", methods=["POST"])
def set_tokens():
    global current_refresh_interval
    check_auth(request)
    data = request.get_json(force=True)
    access = data.get("access")
    refresh = data.get("refresh")
    if access:
        save_token(EVOLUTE_TOKEN_FILENAME, access)
    if refresh:
        save_token(EVOLUTE_REFRESH_TOKEN_FILENAME, refresh)

    current_refresh_interval = REFRESH_INTERVAL

    return jsonify({"status": "tokens updated"})

@app.route("/history/sensors", methods=["GET"])
def history_sensors():
    check_auth(request)
    try:
        limit = min(int(request.args.get("limit", 100)), 1000)
    except ValueError:
        limit = 100
    since = request.args.get("since")  # ISO-строка, опционально

    query = "SELECT ts, data FROM sensor_history"
    params = []
    if since:
        query += " WHERE ts >= ?"
        params.append(since)
    query += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)

    with db_lock, sqlite3.connect(DB_FILE) as conn:
        rows = conn.execute(query, params).fetchall()

    return jsonify([
        {"ts": ts, "data": json.loads(data)} for ts, data in rows
    ])

@app.route("/history/tokens", methods=["GET"])
def history_tokens():
    check_auth(request)
    try:
        limit = min(int(request.args.get("limit", 100)), 1000)
    except ValueError:
        limit = 100

    with db_lock, sqlite3.connect(DB_FILE) as conn:
        rows = conn.execute(
            "SELECT ts, event, detail FROM token_history ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()

    return jsonify([
        {"ts": ts, "event": event, "detail": detail} for ts, event, detail in rows
    ])

@app.route("/manual_refresh", methods=["POST"])
def manual_refresh():
    check_auth(request)
    refresh_tokens()
    return jsonify({"status": "refreshed", "interval": current_refresh_interval})

@app.route("/sensors/all", methods=["GET"])
def get_all_sensors():
    check_auth(request)
    sensors = sensors_data.get("sensorsData")
    if sensors:
        return jsonify(sensors)
    else:
        return jsonify({"error": "No sensors data available"}), 404

@app.route("/position/all", methods=["GET"])
def get_all_positions():
    check_auth(request)
    position = sensors_data.get("positionData")
    if position:
        return jsonify(position)
    else:
        return jsonify({"error": "No position data available"}), 404

@app.route("/sensors/<string:sensor_name>", methods=["GET"])
def get_single_sensor(sensor_name):
    check_auth_rw(request)
    value = sensors_data.get(sensor_name)
    if value is None:
        # не нашли на верхнем уровне — ищем среди датчиков и позиции
        for group in ("sensorsData", "positionData"):
            value = (sensors_data.get(group) or {}).get(sensor_name)
            if value is not None:
                break
    if value is None:
        return jsonify({"error": "sensor not found"}), 404
    return jsonify({sensor_name: value})

@app.route("/proxy/<path:subpath>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def proxy(subpath):
    check_auth_rw(request)
    tokens = get_tokens()
    base_url = "https://app.evassist.ru/"
    target_url = urljoin(base_url, subpath)

    method = request.method
    headers = {
        "User-Agent": USER_AGENT
    }
    headers.update({
        k: v for k, v in request.headers.items()
        if k.lower() not in ["host", "content-length", "content-type", "x-api-key"]
    })

    if request.content_type:
        headers["Content-Type"] = request.content_type

    cookies = {
        "evy-platform-access": tokens["access"],
        "evy-platform-refresh": tokens["refresh"]
    }

    try:
        resp = requests.request(
            method,
            target_url,
            headers=headers,
            params=request.args,
            data=request.get_data(),
            cookies=cookies,
            timeout=TIMEOUT,
            allow_redirects=False
        )
        excluded_headers = ["content-encoding", "transfer-encoding", "connection"]
        response_headers = [
            (name, value) for (name, value) in resp.raw.headers.items()
            if name.lower() not in excluded_headers
        ]
        return Response(resp.content, resp.status_code, response_headers)
    except Exception as e:
        logger.error(f"Proxy request failed: {e}")
        return jsonify({"error": "Proxy failed"}), 500

@app.route("/tbox/<string:action>", methods=["POST"])
def tbox_action(action):
    check_auth_rw(request)
    tokens = get_tokens()
    target_url = f"https://app.evassist.ru/car-service/tbox/{CAR_ID}/{action}"

    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json"
    }

    cookies = {
        "evy-platform-access": tokens["access"],
        "evy-platform-refresh": tokens["refresh"]
    }

    try:
        resp = requests.post(
            target_url,
            headers=headers,
            data=request.get_data(),
            cookies=cookies,
            timeout=TIMEOUT
        )
        resp.raise_for_status()
        return jsonify({"status": "success"})
    except Exception as e:
        logger.error(f"TBox action request failed: {e}")
        return jsonify({"status": "error", "error": str(e)}), 500

@app.route("/tbox-i/<string:action>", methods=["POST"])
def tbox_i_action(action):
    check_auth_rw(request)

    if action not in INTELLIGENT_ACTIONS:
        return jsonify({"status": "error", "error": f"Unknown intelligent action: {action}"}), 400

    endpoint, status_key, skip_if_value = INTELLIGENT_ACTIONS[action]

    try:
        fetch_sensor_data()
        current_value = sensors_data.get("sensorsData", {}).get(status_key)

        if current_value == skip_if_value:
            logger.info(f"Intelligent action '{action}' skipped: already in desired state")
            return jsonify({"status": "already_ok"})

        target_url = f"https://app.evassist.ru/car-service/tbox/{CAR_ID}/{endpoint}"
        tokens = get_tokens()
        headers = {
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json"
        }
        cookies = {
            "evy-platform-access": tokens["access"],
            "evy-platform-refresh": tokens["refresh"]
        }

        resp = requests.post(
            target_url,
            headers=headers,
            data=request.get_data(),
            cookies=cookies,
            timeout=TIMEOUT
        )
        resp.raise_for_status()
        logger.info(f"Intelligent action '{action}' executed successfully")
        fetch_sensor_data()

        return jsonify({"status": "success"})

    except Exception as e:
        logger.error(f"Intelligent action '{action}' failed: {e}")
        return jsonify({"status": "error", "error": str(e)}), 500


@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Not found"}), 404

@app.errorhandler(405)
def method_not_allowed(e):
    return jsonify({"error": "Method Not Allowed"}), 405

@app.errorhandler(Exception)
def handle_exception(e):
    logger.error(f"Unhandled error: {e}")
    return jsonify({"error": "Internal Server Error"}), 500

if __name__ == "__main__":
    if CAR_ID == "SOME_CAR_ID_HASH_CHANGE_ME":
        logger.error("Critical environment variable CAR_ID is not set. Exiting.")
        sys.exit(1)

    start_timestamp = time.time()

    sensors_data = read_json_file(DUMP_FILE, default={})
    loaded_status = read_json_file(STATUS_FILE, default={})
    status_info.update({k: v for k, v in loaded_status.items() if k in status_info})

    init_db()
    archive_and_cleanup_history()
    periodic_refresh()
    periodic_fetch()
    voltage_digest_tick()

    logger.info("App started")
    app.run(host=LISTEN, port=PORT)
