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

from config_loader import load_catalog

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
logger = logging.getLogger("customer")

CITIES, CATEGORIES = load_catalog()

CUSTOMER_BOT_TOKEN = os.getenv("CUSTOMER_BOT_TOKEN", "")
WORKER_API_URL = os.getenv("WORKER_API_URL", "")
JOBS_API_TOKEN = os.getenv("JOBS_API_TOKEN", "")

CITY, CAT, TITLE, DESC = range(4)

class Job(BaseModel):
    city: str
    category: str
    title: str
    description: str

# ---- Telegram embed lifecycle ----
tg_app: Application | None = None

async def tg_initialize_and_start():
    global tg_app
    tg_app = ApplicationBuilder().token(CUSTOMER_BOT_TOKEN).build()

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
    tg_app.add_handler(conv)

    await tg_app.initialize()
    await tg_app.start()
    logger.info("Customer Telegram app started")

async def tg_stop_and_shutdown():
    if tg_app:
        await tg_app.stop()
        await tg_app.shutdown()
        logger.info("Customer Telegram app stopped")

# ---- Handlers ----
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
                headers={"X-API-Token": JOBS_API_TOKEN, "Content-Type": "application/json"},
                json=data.model_dump(),
            )
        if r.status_code == 200:
            payload = r.json()
            await update.message.reply_text(
                f"Задача отправлена. Совпадений: {payload.get('matched', 0)}; "
                f"Уведомлений: {payload.get('sent', 0)}"
            )
        else:
            await update.message.reply_text(f"Ошибка отправки: {r.status_code} — {r.text[:200]}")
    except Exception as e:
        await update.message.reply_text(f"Не удалось отправить задачу: {e}")
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено")
    return ConversationHandler.END

# ---- FastAPI ----
customer_api = FastAPI(title="Customer API")

@customer_api.on_event("startup")
async def on_startup():
    await tg_initialize_and_start()

@customer_api.on_event("shutdown")
async def on_shutdown():
    await tg_stop_and_shutdown()

@customer_api.get("/api/health")
async def health():
    return {"ok": True}

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    server = Server(Config(app=customer_api, host="0.0.0.0", port=port, log_level="info"))
    asyncio.run(server.serve())