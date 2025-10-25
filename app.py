import os
from fastapi import FastAPI, Request, HTTPException
from pydantic_settings import BaseSettings
from telegram import Update
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, ContextTypes
)

# ----- Настройки из .env -----
class Settings(BaseSettings):
    BOT_TOKEN: str
    WEBHOOK_SECRET: str = "secret"
    BASE_URL: str | None = None  # зададим на проде

    class Config:
        env_file = ".env"
        extra = "ignore"

settings = Settings()

# ----- Telegram Application -----
app = FastAPI()
tg_app: Application = ApplicationBuilder().token(settings.BOT_TOKEN).build()

# Команды бота
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я помогу найти разнорабочих рядом.\n"
        "/newjob — создать задачу\n"
        "/help — помощь"
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Доступные команды:\n"
        "/start — приветствие\n"
        "/newjob — создать задачу (мастер шагов)"
    )

async def cmd_newjob(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Окей, давай опишем задачу. Напиши кратко, что нужно сделать.")

tg_app.add_handler(CommandHandler("start", cmd_start))
tg_app.add_handler(CommandHandler("help", cmd_help))
tg_app.add_handler(CommandHandler("newjob", cmd_newjob))

# ----- Webhook -----
@app.on_event("startup")
async def on_startup():
    # Если указан BASE_URL — ставим вебхук
    if settings.BASE_URL:
        url = f"{settings.BASE_URL}/tg/webhook?secret={settings.WEBHOOK_SECRET}"
        await tg_app.bot.set_webhook(url=url)
        print(f"[webhook] set to {url}")
    else:
        print("[webhook] BASE_URL не задан. Для локального теста используй polling.py.")

@app.post("/tg/webhook")
async def telegram_webhook(request: Request, secret: str):
    if secret != settings.WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="bad secret")
    data = await request.json()
    update = Update.de_json(data, tg_app.bot)
    await tg_app.process_update(update)
    return {"ok": True}

# healthcheck
@app.get("/")
async def root():
    return {"ok": True}
