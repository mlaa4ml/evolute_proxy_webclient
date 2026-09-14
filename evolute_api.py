import os
import json
import re
import math
import time
import logging
import threading
import sqlite3
import sys
import requests
from flask import Flask, request, jsonify, abort
from datetime import datetime
from flask import Response
from urllib.parse import urljoin


LISTEN = os.getenv("LISTEN", "127.0.0.1")
PORT = int(os.getenv("PORT", 12321))
API_KEY = os.getenv("API_KEY", "change_me")
API_KEY_RW = os.getenv("API_KEY_RW", "change_me_rw")
TIMEOUT = int(os.getenv("TIMEOUT", 60))
REFRESH_INTERVAL = int(os.getenv("REFRESH_INTERVAL", 600))
SENSORS_REFRESH_INTERVAL = int(os.getenv("SENSORS_REFRESH_INTERVAL", 120))
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
# Доступные типы: movement, coords, ignition, connectivity, doors, battery_full
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_ADMIN_CHAT_ID = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

ALERT_TYPES = {"movement", "coords", "ignition", "connectivity", "doors", "battery_full"}

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
HISTORY_RETENTION_DAYS = int(os.getenv("HISTORY_RETENTION_DAYS", 90))

current_refresh_interval = REFRESH_INTERVAL

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/144.0.0.0 Safari/537.36"
)

DUMP_FILE = "dump.json"
STATUS_FILE = "status.json"

EVOLUTE_REFRESH_URL = "https://app.evassist.ru/id-service/auth/refresh-token"
EVOLUTE_SENSOR_URL = f"https://app.evassist.ru/car-service/tbox/{CAR_ID}/info"

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


def check_sensor_alerts(old_data: dict, new_data: dict):
    """Сравнивает предыдущий и новый снимок сенсоров и шлёт алерты
    только на смене состояния (edge-triggered), а не при каждом опросе:
      - movement      — скорость была 0/отсутствовала, стала > 0;
      - coords        — координаты изменились относительно прошлого снимка;
      - ignition      — зажигание включили/выключили;
      - connectivity  — машина потеряла/восстановила связь (isOnline);
      - doors         — дверь или багажник открылись, пока машина на стоянке;
      - battery_full  — заряд батареи достиг 100% (во время самой зарядки,
                         пока % ниже 100, ничего не шлём — только на финише).
    На самом первом снимке после старта сервиса (old_data пуст) ничего не
    шлём, чтобы не спамить при каждом рестарте."""
    if not old_data:
        return

    old_pos = old_data.get("positionData") or {}
    new_pos = new_data.get("positionData") or {}
    old_sensors = old_data.get("sensorsData") or {}
    new_sensors = new_data.get("sensorsData") or {}

    # 1. Начало движения
    old_speed = old_pos.get("speed")
    new_speed = new_pos.get("speed")
    if (old_speed in (0, None)) and isinstance(new_speed, (int, float)) and new_speed > 0:
        notify_admins("movement", f"🚗 Автомобиль начал движение\nСкорость: {new_speed} км/ч")

    # 2. Смена координат — только если реальное расстояние больше порога
    #    COORDS_MIN_DISTANCE_METERS (см. объяснение у константы выше);
    #    мелкий GPS-шум на стоянке молчит.
    old_lat, old_lon = old_pos.get("lat"), old_pos.get("lon")
    new_lat, new_lon = new_pos.get("lat"), new_pos.get("lon")
    if old_lat is not None and old_lon is not None and new_lat is not None and new_lon is not None:
        distance = haversine_meters(old_lat, old_lon, new_lat, new_lon)
        if distance >= COORDS_MIN_DISTANCE_METERS:
            notify_admins(
                "coords",
                f"📍 Координаты изменились (~{distance:.0f} м)\n"
                f"Было: {old_lat}, {old_lon}\nСтало: {new_lat}, {new_lon}",
            )

    # 3. Зажигание вкл/выкл
    old_ignition = old_sensors.get("ignitionStatus")
    new_ignition = new_sensors.get("ignitionStatus")
    if old_ignition is not None and new_ignition is not None and old_ignition != new_ignition:
        if new_ignition:
            notify_admins("ignition", "🔑 Зажигание включено")
        else:
            notify_admins("ignition", "🔑 Зажигание выключено")

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


# sqlite3-соединения не шарятся между потоками (у нас Flask-потоки запросов +
# отдельные threading.Timer для периодического refresh/fetch), поэтому проще
# и безопаснее открывать короткое соединение на каждую операцию, а не держать
# одно общее. Лок нужен, чтобы не ловить "database is locked" при параллельной
# записи из cron-потоков и запроса одновременно.
db_lock = threading.Lock()

def init_db():
    with db_lock, sqlite3.connect(DB_FILE) as conn:
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

def cleanup_old_history():
    """Раз в сутки подчищаем записи старше HISTORY_RETENTION_DAYS, чтобы
    файл базы не рос бесконечно на маленьком free-плане."""
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
    except Exception as e:
        logger.error(f"Failed to clean up history: {e}")
    t = threading.Timer(86400, cleanup_old_history)
    t.daemon = True
    t.start()

sensors_data = {}
status_info = {
    "start_time": datetime.utcnow().isoformat(),
    "last_token_update": None,
    "last_sensor_update": None,
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

def fetch_sensor_data():
    global sensors_data
    if not tokens_ok:
        logger.warning("Sensor data fetch skipped: tokens are not active")
        return
    try:
        tokens = get_tokens()
        cookies = {
            "evy-platform-access": tokens["access"],
            "evy-platform-refresh": tokens["refresh"]
        }
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

        try:
            check_sensor_alerts(sensors_data, data)
        except Exception as e:
            logger.error(f"Sensor alert check failed: {e}")

        sensors_data = data
        update_status("last_sensor_update")
        write_json_file(DUMP_FILE, sensors_data)
        log_sensor_snapshot(sensors_data)
        logger.info("Sensor data updated")
    except Exception as e:
        logger.error(f"Failed to fetch sensor data: {e}")

def periodic_refresh():
    refresh_tokens()
    t = threading.Timer(current_refresh_interval, periodic_refresh)
    t.daemon = True
    t.start()

def periodic_fetch():
    fetch_sensor_data()
    t = threading.Timer(SENSORS_REFRESH_INTERVAL, periodic_fetch)
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
        "current_refresh_interval": current_refresh_interval
    })

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
    cleanup_old_history()
    periodic_refresh()
    periodic_fetch()

    logger.info("App started")
    app.run(host=LISTEN, port=PORT)
