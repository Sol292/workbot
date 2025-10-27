import sys
import os
from pathlib import Path

# Добавляем корень репо в PYTHONPATH, если нужно
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config_loader import load_catalog

import asyncio
import logging
from typing import Dict, Optional, Set

from fastapi import FastAPI, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from uvicorn import Config, Server

from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
)

from dotenv import load_dotenv
load_dotenv()

# Логирование
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
logger = logging.getLogger("worker")

# --- Каталоги ---
CITIES, CATEGORIES = load_catalog()

# --- Переменные окружения ---
JOBS_API_TOKEN = os.getenv("JOBS_API_TOKEN", "")
WORKER_BOT_TOKEN = os.getenv("WORKER_BOT_TOKEN", "")

if not WORKER_BOT_TOKEN:
    logger.error("WORKER_BOT_TOKEN is not set")
    raise RuntimeError("WORKER_BOT_TOKEN not set")

# --- Модели ---
class Job(BaseModel):
    city: str
    category: str
    title: str
    description: str

class PushJobRequest(Job):
    preview_only: bool = Field(False, description="Если True — только считать совпадения, без рассылки")

class PushJobResponse(BaseModel):
    ok: bool
    matched: int
    sent: int
    unmatched_reason: Optional[str] = None

class WorkerProfile(BaseModel):
    user_id: int
    city: Optional[str] = None
    categories: Set[str] = set()

WORKERS: Dict[int, WorkerProfile] = {}

# --- Проверка токена API ---
async def verify_token(x_api_token: str = Header("")):
    if JOBS_API_TOKEN and x_api_token != JOBS_API_TOKEN:
        raise HTTPException(status_code=401, detail="invalid token")

# --- Telegram бот ---
telegram_app = None  # type: Optional[ApplicationBuilder]

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"Received /start from user_id={user.id}, username={user.username}")
    WORKERS[user.id] = WorkerProfile(user_id=user.id)
    await update.message.reply_text(
        "Вы зарегистрированы как исполнитель.\n"
        "Город: /setcity <город>\n"
        "Категории: /setcategories <список через запятую>\n"
        f"Доступные города: {', '.join(CITIES)}\n"
        f"Доступные категории: {', '.join(CATEGORIES)}"
    )

async def cmd_setcity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args
    if not args:
        await update.message.reply_text("Укажите город: /setcity Тверь")
        return
    city = " ".join(args).strip()
    if city not in CITIES:
        await update.message.reply_text(f"Неизвестный город. Доступные: {', '.join(CITIES)}")
        return
    profile = WORKERS.get(user.id, WorkerProfile(user_id=user.id))
    profile.city = city
    WORKERS[user.id] = profile
    await update.message.reply_text(f"Город установлен: {city}")

async def cmd_setcategories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args
    if not args:
        await update.message.reply_text("Укажите категории: /setcategories Вентиляция, Кондиционирование")
        return
    raw = " ".join(args)
    chosen = {c.strip() for c in raw.split(",") if c.strip()}
    unknown = chosen - set(CATEGORIES)
    if unknown:
        await update.message.reply_text(f"Неизвестные категории: {', '.join(sorted(unknown))}")
        return
    profile = WORKERS.get(user.id, WorkerProfile(user_id=user.id))
    profile.categories = chosen
    WORKERS[user.id] = profile
    await update.message.reply_text(f"Категории сохранены: {', '.join(sorted(chosen))}")

async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    p = WORKERS.get(user.id)
    if not p:
        await update.message.reply_text("Вы ещё не зарегистрированы. Нажмите /start")
        return
    await update.message.reply_text(
        f"Профиль:\nГород: {p.city or 'не указан'}\n"
        f"Категории: {', '.join(sorted(p.categories)) if p.categories else 'не указаны'}"
    )

async def generic_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text if update.message else "<no text>"
    logger.info(f"Received message from user_id={user.id}, username={user.username}, text={text}")

async def notify_workers(job: Job) -> int:
    matched = [uid for uid, prof in WORKERS.items() if prof.city == job.city and job.category in prof.categories]
    text = (
        f"Новая задача в городе {job.city}\n"
        f"Категория: {job.category}\n\n"
        f"{job.title}\n{job.description}"
    )
    sent = 0
    for uid in matched:
        try:
            await telegram_app.bot.send_message(chat_id=uid, text=text)
            sent += 1
        except Exception as e:
            logger.warning(f"Failed to send to {uid}: {e}")
    return sent

# --- FastAPI приложение ---
worker_api = FastAPI(title="Worker API")

@worker_api.on_event("startup")
async def on_startup():
    global telegram_app
    telegram_app = ApplicationBuilder().token(WORKER_BOT_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", cmd_start))
    telegram_app.add_handler(CommandHandler("setcity", cmd_setcity))
    telegram_app.add_handler(CommandHandler("setcategories", cmd_setcategories))
    telegram_app.add_handler(CommandHandler("profile", cmd_profile))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, generic_message_handler))

    await telegram_app.initialize()
    await telegram_app.start()
    logger.info("Telegram app started (startup)")

@worker_api.on_event("shutdown")
async def on_shutdown():
    if telegram_app:
        await telegram_app.stop()
        await telegram_app.shutdown()
        logger.info("Telegram app stopped (shutdown)")

@worker_api.get("/api/health")
async def health():
    return {"ok": True}

@worker_api.get("/api/debug/workers")
async def debug_workers():
    return {
        "count": len(WORKERS),
        "items": [p.model_dump() for p in WORKERS.values()],
        "cities": CITIES,
        "categories": CATEGORIES
    }

@worker_api.post("/api/push-job", dependencies=[Depends(verify_token)])
async def push_job(req: PushJobRequest) -> PushJobResponse:
    if req.city not in CITIES:
        return PushJobResponse(ok=True, matched=0, sent=0, unmatched_reason="unknown city")
    if req.category not in CATEGORIES:
        return PushJobResponse(ok=True, matched=0, sent=0, unmatched_reason="unknown category")
    matched = sum(1 for p in WORKERS.values() if p.city == req.city and req.category in p.categories)
    if req.preview_only:
        return PushJobResponse(ok=True, matched=matched, sent=0)
    sent = await notify_workers(req)
    return PushJobResponse(ok=True, matched=matched, sent=sent)

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    config = Config(app=worker_api, host="0.0.0.0", port=port, log_level="info")
    server = Server(config)
    asyncio.run(server.serve())
