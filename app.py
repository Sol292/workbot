import os
from fastapi import FastAPI, Request, HTTPException
from telegram import Update
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes

BOT_TOKEN = os.environ["BOT_TOKEN"]               # если нет — упадёт сразу, и это хорошо
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "secret")
BASE_URL = os.environ.get("BASE_URL")             # читаем переменную окружения напрямую

app = FastAPI()
tg_app: Application = ApplicationBuilder().token(BOT_TOKEN).build()

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! /newjob — создать задачу, /help — помощь")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Команды: /start, /newjob")

async def cmd_newjob(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Окей, опиши задачу коротко. Мастер шагов добавим позже.")

tg_app.add_handler(CommandHandler("start", cmd_start))
tg_app.add_handler(CommandHandler("help", cmd_help))
tg_app.add_handler(CommandHandler("newjob", cmd_newjob))

@app.on_event("startup")
async def on_startup():
    if BASE_URL:
        url = f"{BASE_URL}/tg/webhook?secret={WEBHOOK_SECRET}"
        await tg_app.bot.set_webhook(url=url)
        print(f"[webhook] set to {url}")
    else:
        print("[webhook] BASE_URL не задан. Для локального теста используй polling.py.")

@app.post("/tg/webhook")
async def telegram_webhook(request: Request, secret: str):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="bad secret")
    data = await request.json()
    update = Update.de_json(data, tg_app.bot)
    await tg_app.process_update(update)
    return {"ok": True}

@app.get("/")
async def root():
    return {"ok": True}

# ВРЕМЕННО для диагностики — можно удалить после проверки
@app.get("/debug")
async def debug():
    return {
        "has_bot_token": bool(BOT_TOKEN),
        "has_base_url": bool(BASE_URL),
        "base_url": BASE_URL,
    }
