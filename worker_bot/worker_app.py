# worker_app.py
# FastAPI + Telegram Bot (PTB v20+) — принимает задачи от customer и рассылает воркерам по матчу

import os, re, time, logging
from typing import Dict, Any, List, Set

from fastapi import FastAPI, Request, Header, HTTPException
from pydantic import BaseModel
from telegram import Bot, InlineKeyboardMarkup, InlineKeyboardButton

# ---------- конфиг / окружение ----------
BOT_TOKEN       = os.getenv("BOT_TOKEN")                      # токен воркер-бота
JOBS_API_TOKEN  = os.getenv("JOBS_API_TOKEN")                 # общий API токен customer↔worker
BASE_URL        = os.getenv("BASE_URL")                       # https://workbot-worker-production.up.railway.app

if not BOT_TOKEN or not JOBS_API_TOKEN or not BASE_URL:
    missing = [k for k,v in {"BOT_TOKEN":BOT_TOKEN, "JOBS_API_TOKEN":JOBS_API_TOKEN, "BASE_URL":BASE_URL}.items() if not v]
    raise RuntimeError(f"Missing env vars: {', '.join(missing)}")

# ---------- данные (в реале — БД; здесь — память для простоты) ----------
# USERS: { worker_id: {"chat_id": int, "...": ...} }
USERS: Dict[str, Dict[str, Any]] = {}
# WORKERS: { worker_id: {"available": bool, "city": str, "city_aliases":[...], "cats":[slug|label,...]} }
WORKERS: Dict[str, Dict[str, Any]] = {}

# ---------- утилиты ----------
log = logging.getLogger("worker")
logging.basicConfig(level=logging.INFO)

def norm_city(s: str) -> str:
    s = (s or "").lower().replace("ё", "е")
    s = re.sub(r"\bг[.\s]+", "", s)  # г. тверь -> тверь
    s = re.sub(r"\s+", " ", s).strip()
    return s

# алиасы категорий → слаги
CATEGORY_MAP = {
    # слаги
    "handyman":"handyman","loader":"loader","courier":"courier","cleaning":"cleaning",
    "demontazh":"demontazh","electric":"electric","plumbing":"plumbing","furniture":"furniture",
    "garbage":"garbage","minor_repair":"minor_repair",
    # русские имена → слаги (под твой список)
    "разнорабочие (общие)":"handyman",
    "погрузка/разгрузка":"loader",
    "курьер/доставка":"courier",
    "уборка после ремонта":"cleaning",
    "демонтаж":"demontazh",
    "электромонтаж (простые)":"electric",
    "сантехника (простые)":"plumbing",
    "сборка мебели":"furniture",
    "вынос мусора":"garbage",
    "малярные работы":"minor_repair",
    "клининг":"cleaning",
    "подсобник на стройку":"handyman",
    "персональные поручения":"handyman",
}

def norm_slug(s: str) -> str:
    s = (s or "").strip().lower()
    return CATEGORY_MAP.get(s, s)

# ---------- Telegram bot ----------
bot = Bot(token=BOT_TOKEN)

# ---------- FastAPI ----------
app = FastAPI(title="workbot-worker")

class PushJobPayload(BaseModel):
    # принимаем либо плоский JSON, либо {"job": {...}}
    job: Dict[str, Any] | None = None
    customer_contact: str | None = None

@app.get("/api/health")
def health():
    return {"ok": True, "service": "worker"}

@app.get("/")
def root():
    return {"ok": True, "service": "worker"}

@app.get("/api/debug/workers")
def debug_workers():
    def row(wid: str, w: Dict[str, Any]):
        return {
            "wid": wid,
            "available": bool(w.get("available")),
            "city": w.get("city"),
            "city_norm": norm_city(w.get("city") or ""),
            "city_aliases": w.get("city_aliases") or [],
            "cats": w.get("cats") or [],
            "chat_id": (USERS.get(wid, {}) or {}).get("chat_id"),
        }
    return {"count": len(WORKERS), "workers": [row(wid,w) for wid,w in WORKERS.items()]}

@app.post("/api/debug/upsert-worker")
def upsert_worker(payload: Dict[str, Any]):
    """
    Быстро добавить/обновить исполнителя для теста.
    payload: {"wid":"999","city":"Тверь","cats":["Разнорабочие (общие)"],"available":true,"chat_id":123}
    """
    wid = str(payload.get("wid") or "test")
    w = WORKERS.setdefault(wid, {})
    w["available"] = bool(payload.get("available", True))
    w["city"] = payload.get("city", "Тверь")
    w["city_aliases"] = list(set((w.get("city_aliases") or []) + [norm_city(w["city"])]))
    w["cats"] = payload.get("cats") or ["Разнорабочие (общие)"]
    USERS.setdefault(wid, {})
    if payload.get("chat_id"):
        USERS[wid]["chat_id"] = payload["chat_id"]
    return {"ok": True, "worker": WORKERS[wid], "user": USERS.get(wid)}

@app.post("/api/push-job")
async def api_push_job(req: Request, authorization: str | None = Header(default=None)):
    # auth
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="bad api token")
    token = authorization.split(" ", 1)[1].strip()
    if token != JOBS_API_TOKEN:
        raise HTTPException(status_code=401, detail="bad api token")

    raw = await req.json()
    body: Dict[str, Any] = raw if isinstance(raw, dict) else {}
    job: Dict[str, Any] = body.get("job") or body
    customer_contact = str(body.get("customer_contact", ""))

    job_id  = str(job.get("job_id") or job.get("id") or f"tmp-{int(time.time())}")
    city_in = str(job.get("city") or "")
    addr    = str(job.get("address") or "")
    when    = str(job.get("when") or "")
    pay     = str(job.get("pay") or "")
    cat_in  = str(job.get("category") or job.get("category_slug") or "")

    city_norm = norm_city(city_in)
    cat_slug  = norm_slug(cat_in)

    matched: List[str] = []
    for wid, w in WORKERS.items():
        if not w.get("available"):
            continue
        worker_cats: Set[str] = { norm_slug(c) for c in (w.get("cats") or []) }
        if cat_slug not in worker_cats:
            continue
        aliases = (w.get("city_aliases") or []) + [norm_city(w.get("city") or "")]
        if city_norm not in aliases:
            continue
        matched.append(wid)

    sent = 0
    for wid in matched:
        chat_id = (USERS.get(wid, {}) or {}).get("chat_id")
        if not chat_id:
            log.warning("No chat_id for wid=%s — пропускаем отправку", wid)
            continue
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Откликнуться", callback_data=f"bid:{job_id}:{customer_contact}")]]
        )
        text = (
            f"Новая задача #{job_id}\n"
            f"Категория: {cat_in or '—'}\n"
            f"Где: {addr or city_in or '—'}\n"
            f"Когда: {when or 'по согласованию'}\n"
            f"Оплата: {pay or 'не указана'}"
        )
        try:
            await bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
            sent += 1
        except Exception as e:
            log.exception("send failed: wid=%s job_id=%s: %s", wid, job_id, e)

    return {
        "ok": True,
        "sent": sent,
        "matched_workers": len(matched),
        "city_in": city_in,
        "city_norm": city_norm,
        "category_in": cat_in,
        "category_slug": cat_slug,
        "reason": "OK" if sent else ("NO_MATCH" if matched == [] else "NO_CHAT_ID"),
    }
