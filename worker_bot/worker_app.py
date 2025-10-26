import os
import asyncio
import logging
from typing import Dict, List, Optional, Set

from fastapi import FastAPI, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from uvicorn import Config, Server

from telegram import Update
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, ContextTypes, filters
)

# ---------------------------------
# Config
# ---------------------------------
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
logger = logging.getLogger("worker")

CITIES: List[str] = ["Москва", "Тверь", "Санкт-Петербург", "Зеленоград"]
CATEGORIES: List[str] = ["Вентиляция", "Кондиционирование", "Электрика", "Сантехника"]

JOBS_API_TOKEN = os.getenv("JOBS_API_TOKEN", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

# ---------------------------------
# Models
# ---------------------------------
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

# ---------------------------------
# Auth
# ---------------------------------
async def verify_token(x_api_token: str = Header("")):
    if JOBS_API_TOKEN and x_api_token != JOBS_API_TOKEN:
        raise HTTPException(status_code=401, detail="invalid token")

# ---------------------------------
# Telegram handlers
# ---------------------------------
telegram_app: Optional[Application] = None

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    WORKERS[user.id] = WORKERS.get(user.id, WorkerProfile(user_id=user.id))
    await update.message.reply_text(
        "Вы зарегистрированы как исполнитель.\n"
        "Город: /setcity <город>\n"
        "Категории: /setcategories <список через запятую>\n"
        f"Доступные города: {', '.join(CITIES)}\n"
        f"Доступные категории: {', '.join(CATEGORIES)}"
    )

async def cmd_setcity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not context.args:
        await update.message.reply_text("Укажите город: /setcity Тверь")
        return
    city = " ".join(context.args)
    if city not in CITIES:
        await update.message.reply_text(f"Неизвестный город. Доступные: {', '.join(CITIES)}")
        return
    profile = WORKERS.get(user.id, WorkerProfile(user_id=user.id))
    profile.city = city
    WORKERS[user.id] = profile
    await update.message.reply_text(f"Город установлен: {city}")

async def cmd_setcategories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not context.args:
        await update.message.reply_text("Укажите категории через запятую: /setcategories Вентиляция, Кондиционирование")
        return
    raw = " ".join(context.args)
    chosen = {c.strip() for c in raw.split(",") if c.strip()}
    unknown = chosen - set(CATEGORIES)
    if unknown:
        await update.message.reply_text(f"Неизвестные категории: {', '.join(unknown)}")
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
        f"Профиль:\nГород: {p.city or 'не указан'}\nКатегории: {', '.join(sorted(p.categories)) or 'не указаны'}"
    )

# ---------------------------------
# Notify
# ---------------------------------
async def notify_workers(job: Job) -> int:
    if not telegram_app:
        logger.error("Telegram app not ready")
        return 0
    matched = [
        uid for uid, prof in WORKERS.items()
        if prof.city == job.city and job.category in prof.categories
    ]
    text = (
        f"Новая задача в городе {job.city}\n"
        f"Категория: {job.category}\n\n"
        f"{job.title}\n{job.description}"
    )
    sent = 0
    for uid in matched:
        try:
            await telegram_app.bot.send_message(uid, text)
            sent += 1
        except Exception as e:
            logger.warning(f"send to {uid} failed: {e}")
    return sent

# ---------------------------------
# FastAPI app
# ---------------------------------
worker_api = FastAPI(title="Worker API")

@worker_api.on_event("startup")
async def on_startup():
    global telegram_app
    telegram_app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", cmd_start))
    telegram_app.add_handler(CommandHandler("setcity", cmd_setcity))
    telegram_app.add_handler(CommandHandler("setcategories", cmd_setcategories))
    telegram_app.add_handler(CommandHandler("profile", cmd_profile))
    asyncio.create_task(telegram_app.run_polling())

@worker_api.on_event("shutdown")
async def on_shutdown():
    if telegram_app:
        await telegram_app.stop()

@worker_api.get("/api/health")
async def health():
    return {"ok": True}

@worker_api.get("/api/debug/workers")
async def debug_workers():
    return {"count": len(WORKERS), "items": [p.dict() for p in WORKERS.values()]}

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
    server = Server(Config(app=worker_api, host="0.0.0.0", port=port, log_level="info"))
    asyncio.run(server.serve())
