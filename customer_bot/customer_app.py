import os
import asyncio
import logging
import httpx
from fastapi import FastAPI
from pydantic import BaseModel
from uvicorn import Config, Server

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, MessageHandler,
    ConversationHandler, ContextTypes, filters
)

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
logger = logging.getLogger("customer")

CITIES = ["Москва", "Тверь", "Санкт-Петербург", "Зеленоград"]
CATEGORIES = ["Вентиляция", "Кондиционирование", "Электрика", "Сантехника"]

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
WORKER_API_URL = os.getenv("WORKER_API_URL", "")
JOBS_API_TOKEN = os.getenv("JOBS_API_TOKEN", "")

CITY, CAT, TITLE, DESC = range(4)

class Job(BaseModel):
    city: str
    category: str
    title: str
    description: str

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = ReplyKeyboardMarkup([[KeyboardButton(c)] for c in CITIES], one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text("Город?", reply_markup=kb)
    return CITY

async def city_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    city = update.message.text.strip()
    if city not in CITIES:
        await update.message.reply_text(f"Выберите из списка: {', '.join(CITIES)}")
        return CITY
    context.user_data["city"] = city
    kb = ReplyKeyboardMarkup([[KeyboardButton(c)] for c in CATEGORIES], one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text("Категория?", reply_markup=kb)
    return CAT

async def cat_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cat = update.message.text.strip()
    if cat not in CATEGORIES:
        await update.message.reply_text(f"Выберите из списка: {', '.join(CATEGORIES)}")
        return CAT
    context.user_data["category"] = cat
    await update.message.reply_text("Короткий заголовок задачи?")
    return TITLE

async def title_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["title"] = update.message.text.strip()
    await update.message.reply_text("Опишите задачу подробнее")
    return DESC

async def desc_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["description"] = update.message.text.strip()
    data = Job(**context.user_data)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{WORKER_API_URL}/api/push-job",
                headers={"X-API-Token": JOBS_API_TOKEN},
                json=data.dict(),
            )
        if r.status_code == 200:
            payload = r.json()
            await update.message.reply_text(
                f"Задача отправлена. Совпадений: {payload.get('matched', 0)}; "
                f"Уведомлений: {payload.get('sent', 0)}"
            )
        else:
            await update.message.reply_text(f"Ошибка отправки: {r.status_code}")
    except Exception as e:
        await update.message.reply_text(f"Не удалось отправить задачу: {e}")
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено")
    return ConversationHandler.END

customer_api = FastAPI(title="Customer API")

@customer_api.on_event("startup")
async def on_startup():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, city_step)],
            CAT: [MessageHandler(filters.TEXT & ~filters.COMMAND, cat_step)],
            TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, title_step)],
            DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, desc_step)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(conv)
    customer_api.state.tg_app = app
    asyncio.create_task(app.run_polling())

@customer_api.on_event("shutdown")
async def on_shutdown():
    app: Application = customer_api.state.tg_app
    await app.stop()

@customer_api.get("/api/health")
async def health():
    return {"ok": True}

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    server = Server(Config(app=customer_api, host="0.0.0.0", port=port, log_level="info"))
    asyncio.run(server.serve())
