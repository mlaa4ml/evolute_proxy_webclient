# Запуск веб-клиента через GitHub Pages

Веб-клиент (`evolute_client.html`) представляет собой статический HTML/JS файл, который можно легко опубликовать на **GitHub Pages**, чтобы иметь доступ к интерфейсу управления автомобилем из любого браузера.

При этом сам **Flask-прокси** (`evolute_api.py`) должен работать на доступном в сети сервере (ваш домашний сервер, VPS, Raspberry Pi и т.д.), так как ему необходим постоянный фоновый процесс для обновления токенов, сохранения кэша и взаимодействия с облаком Evolute.

---

## Архитектура решения

1. **GitHub Pages**: хостит статический файл `evolute_client.html`. Пользователь открывает `https://<username>.github.io/<repository>/`.
2. **Flask-прокси (Ваш сервер)**: принимает запросы от веб-клиента, проксирует их к API Evolute и управляет фоновыми задачами.
3. **Безопасность и CORS**: поскольку браузер будет делать запросы с `https://<username>.github.io` на ваш прокси, на сервере прокси обязательно должен быть настроен заголовок CORS (`ALLOWED_ORIGIN`), разрешающий запросы с вашего адреса GitHub Pages.

---

## Пошаговая инструкция по настройке

### Шаг 1. Включение GitHub Pages в репозитории

1. Перейдите в ваш репозиторий на GitHub.
2. Откройте вкладку **Settings** (Настройки) -> **Pages**.
3. В разделе **Build and deployment** в поле **Source** выберите **GitHub Actions**.

### Шаг 2. Автоматический деплой

В репозитории уже настроен workflow деплоя (`.github/workflows/pages.yml`), который автоматически публикует `evolute_client.html` на GitHub Pages при каждом пуше в ветку `main`, а также позволяет запустить деплой вручную через вкладку **Actions** -> **pages** -> **Run workflow**.

После успешного выполнения Actions ваш сайт будет доступен по адресу:
`https://<username>.github.io/<repository-name>/`

---

### Шаг 3. Настройка и запуск Flask-прокси с поддержкой CORS

Для того чтобы веб-клиент мог обращаться к вашему прокси, запустите прокси с переменной окружения `ALLOWED_ORIGIN`, разрешающей CORS для вашего сайта на GitHub Pages.

Пример запуска через Docker Compose (`docker-compose.yml`):

```yaml
version: '3.8'

services:
  evolute_proxy:
    build: .
    restart: always
    ports:
      - "12321:12321"
    environment:
      - CAR_ID=your_car_id_here
      - API_KEY=your_read_api_key
      - API_KEY_RW=your_write_api_key
      - ALLOWED_ORIGIN=https://<username>.github.io
```

Или через обычный Docker:

```bash
docker run -d \
  -p 12321:12321 \
  -e CAR_ID=your_car_id \
  -e API_KEY=your_key \
  -e ALLOWED_ORIGIN=https://<username>.github.io \
  evolute_proxy_webclient
```

> **Важно про Mixed Content (HTTPS -> HTTP):**
> GitHub Pages всегда работает по протоколу **HTTPS**. Если ваш прокси запущен по обычному **HTTP** (например, `http://192.168.1.50:12321`), современный браузер заблокирует запросы из соображений безопасности (Mixed Content Error).
> Рекомендации:
> - Используйте HTTPS для прокси (например, через Nginx + Let's Encrypt / self-signed certificate или туннели вроде Cloudflare Tunnels / ngrok).
> - Либо открывайте веб-клиент локально (просто дважды кликнув на `evolute_client.html` или через `python -m http.server`), если вам не нужен публичный хостинг на GitHub Pages.

---

### Шаг 4. Настройка веб-клиента

1. Откройте опубликованную страницу GitHub Pages в браузере.
2. В настройках клиента укажите **Base URL** вашего запущенного прокси (например, `https://your-proxy-domain.com:12321` или `https://<tunnel-subdomain>.trycloudflare.com`).
3. При необходимости введите API-ключи (`API_KEY` / `API_KEY_RW`). Настройки и ключи сохраняются в браузере с помощью полифилла `localStorage`.
