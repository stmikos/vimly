# Vimly — Client Demo Bot

Готовый демонстрационный бот под бренд **Vimly**. Один файл `app.py`, FastAPI + aiogram v3. Работает на **Render (webhook)** и локально (**polling**).

## Что внутри демо
- Главное меню: Процесс • Кейсы (демо) • Квиз‑заявка • Пакеты и цены • Заказать • Контакты • Бриф (7 вопросов) • Подарок
- Квиз (3 вопроса) и «Заказать» → заявка в **админ‑чат**
- Админ‑панель: вкл/выкл приёма, статистика, тест‑рассылка
- Подарок: чек‑лист «7 экранов демо‑бота»

## Быстрый старт локально
```bash
pip install -r requirements.txt
cp .env.example .env  # заполните переменные
# Windows:
set MODE=polling && python app.py
# Linux/macOS:
MODE=polling python app.py
```

## Деплой на Render
1. Залейте репозиторий c `app.py`, `requirements.txt`, `render.yaml`, папкой `assets`.
2. Создайте **Web Service → Python**.
3. Build: `pip install -r requirements.txt`
4. Start: `uvicorn app:app --host 0.0.0.0 --port $PORT`
5. ENV vars:
   - `BOT_TOKEN` — токен бота
   - `ADMIN_CHAT_ID` — ваш Telegram ID (число)
   - `BASE_URL` — публичный URL сервиса (например, `https://xxx.onrender.com`)
   - `WEBHOOK_PATH` — `/telegram/webhook/vimly` (можно оставить)
   - `WEBHOOK_SECRET` — любая длинная строка
   - `BRAND_NAME` — по умолчанию `Vimly`
   - `BRAND_TAGLINE` — `Боты, которые продают`
   - `BRAND_TG` — `@Vimly_bot` (замените на свой)
   - `BRAND_SITE` — сайт/портфолио (опционально)
   - `LEADS_CHAT_ID` — чат, куда прилетают лиды
   - `LEADS_THREAD_ID` — ID темы в `LEADS_CHAT_ID`. Получить: в нужном топике скопируйте ссылку на сообщение и возьмите число после `topic=`.
     Обязателен, если в `LEADS_CHAT_ID` включены темы (форум). Пример добавления в `.env`:

     ```env
     LEADS_THREAD_ID=123
     ```
