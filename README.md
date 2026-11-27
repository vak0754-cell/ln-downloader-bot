# LN Downloader Bot + MCP Server

Просунутий асинхронний Telegram-бот для завантаження ранобе за посиланням із вбудованим **FastAPI REST API** та **FastMCP-сервером** — тож ним можеш керувати і ти в Telegram, і **Gemini напряму зі свого середовища** (через MCP-конектор або HTTP API).

## Що вміє
* **Telegram-бот (`bot.py`):** надішли будь-який URL → автовизначення (веб-книга зі змістом, відкритий каталог файлів або прямий `.epub`/`.pdf`/`.txt`/`.fb2`/`.zip`), вибір діапазону розділів (`1-50`, `останні 20`, `усі`), збірка в **`EPUB 3`** (зі змістом та обкладинкою) або **`TXT`**, SQLite-кеш розділів.
* **MCP-сервер для Gemini (`/mcp`):** підключається як Custom MCP Connector (аналогічно конектору Tabletki.ua на Render) і дає Gemini 5 готових інструментів:
  * `inspect_novel_url(url)` — аналіз сторінки, підрахунок розділів або списку файлів;
  * `start_novel_download(url, fmt, range_spec, upload_to_drive, notify_telegram)` — запуск асинхронного завантаження;
  * `get_download_status(job_id)` — прогрес, шляхи файлів і посилання Google Drive;
  * `list_download_jobs(limit)` — список задач;
  * `cancel_download_job(job_id)` — зупинка зі збереженням кешу.
* **REST API (`/api/*`):** захищений `Authorization: Bearer <API_KEY>` (`POST /api/inspect`, `POST /api/download`, `GET /api/jobs/{id}`, `GET /api/files/{filename}`).
* **Автоматична доставка результатів:** задачі, запущені з Gemini через MCP/API, можуть одразу відправляти готовий `.epub` у твій Telegram (`NOTIFY_CHAT_ID`) та завантажувати в `Переклади ранобе/<Title>/originals/` на Google Drive.

> **Важливо:** клієнт працює через стандартний HTTP з урахуванням `robots.txt` і не містить обходу Cloudflare чи антибот-захисту.

## Запуск

### Локально / VPS / Docker
```bash
cp .env.example .env
# Заповни BOT_TOKEN, ALLOWED_USER_IDS, NOTIFY_CHAT_ID та API_KEY
docker compose up -d --build
```
Або без Docker:
```bash
pip install -r requirements.txt
python3 main.py
```

### Деплой на Render (для підключення до Gemini як MCP)
1. Завантаж репозиторій на GitHub і підключи через **`render.yaml`** (Blueprint).
2. Задай у змінних середовища `BOT_TOKEN`, `ALLOWED_USER_IDS`, `NOTIFY_CHAT_ID` (і за потреби `GOOGLE_SERVICE_ACCOUNT_FILE`).
3. Підключи URL `https://<твій-сервіс>.onrender.com/mcp` у налаштуваннях MCP-конекторів Gemini — після цього я зможу сам викликати `inspect_novel_url` і `start_novel_download` прямо з чату.


## Render free (webhook-режим)
Якщо задано `RENDER_EXTERNAL_URL` (Render ставить сам) або `WEBHOOK_BASE_URL`, `main.py` працює через Telegram webhook (`/webhook/<hash>`, перевірка `WEBHOOK_SECRET`) на тому ж порту `$PORT`, що й API/MCP. Без URL — polling. `GET /` та `GET /health` повертають ok. Дані в `/tmp/data` ефемерні. Dockerfile збирає застосунок з `app.tar.gz` (плоский аплоад у GitHub).
