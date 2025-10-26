# customer_app.py
# FastAPI — создаёт задачу и отправляет её на воркера с ретраями и диагностикой

import os, time, logging, asyncio
from typing import Dict, Any

import httpx
from fastapi import FastAPI, HTTPException

# ---------- конфиг ----------
BASE_URL        = os.getenv("BASE_URL")                         # https://workbot-production.up.railway.app
WORKER_API_URL  = os.getenv("WORKER_API_URL")                   # https://workbot-worker-production.up.railway.app
JOBS_API_TOKEN  = os.getenv("JOBS_API_TOKEN")                   # общий токен

if not BASE_URL or not WORKER_API_URL or not JOBS_API_TOKEN:
    missing = [k for k,v in {"BASE_URL":BASE_URL,"WORKER_API_URL":WORKER_API_URL,"JOBS_API_TOKEN":JOBS_API_TOKEN}.items() if not v]
    raise RuntimeError(f"Missing env vars: {', '.join(missing)}")

log = logging.getLogger("customer")
logging.basicConfig(level=logging.INFO)

# те же алиасы категорий → слуги (как у worker)
CATEGORY_MAP = {
    "handyman":"handyman","loader":"loader","courier":"courier","cleaning":"cleaning",
    "demontazh":"demontazh","electric":"electric","plumbing":"plumbing","furniture":"furniture",
    "garbage":"garbage","minor_repair":"minor_repair",
    "разнорабочие (общие)":"handyman","погрузка/разгрузка":"loader","курьер/доставка":"courier",
    "уборка после ремонта":"cleaning","демонтаж":"demontazh","электромонтаж (простые)":"electric",
    "сантехника (простые)":"plumbing","сборка мебели":"furniture","вынос мусора":"garbage",
    "малярные работы":"minor_repair","клининг":"cleaning","подсобник на стройку":"handyman",
    "персональные поручения":"handyman",
}
def norm_slug(s: str) -> str:
    s = (s or "").strip().lower()
    return CATEGORY_MAP.get(s, s)

app = FastAPI(title="workbot-customer")

@app.get("/api/health")
def health():
    return {"ok": True, "service": "customer"}

# простой вход — создать задачу
# пример тела: {"job_id":"123","city":"Тверь","category":"Разнорабочие (общие)","title":"Нужно помочь","details":"..." }
@app.post("/api/new-job")
async def api_new_job(body: Dict[str, Any]):
    job = body
    # добавим category_slug для устойчивого матчинга
    job["category_slug"] = job.get("category_slug") or norm_slug(job.get("category",""))
    if not job.get("job_id"):
        job["job_id"] = f"job-{int(time.time())}"

    res = await push_to_worker(job)
    return {"ok": True, "worker_response": res}

# тонкий прокси «как будет отправлять бот» (можно вызывать из бота, вебхука и тестов)
async def push_to_worker(job: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{WORKER_API_URL.rstrip('/')}/api/push-job"
    headers = {
        "Authorization": f"Bearer {JOBS_API_TOKEN}",
        "Content-Type": "application/json; charset=utf-8",
        "X-Job-Id": str(job.get("job_id") or ""),
    }
    # ретраи 1s, 2s, 4s
    last_exc: Exception | None = None
    for delay in (0, 1, 2, 4):
        if delay:
            await asyncio.sleep(delay)
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.0)) as client:
                r = await client.post(url, headers=headers, json=job)
            if r.status_code >= 500:
                raise RuntimeError(f"worker 5xx: {r.status_code} {r.text}")
            if r.status_code == 401:
                raise HTTPException(status_code=401, detail="bad api token (customer→worker)")
            if r.status_code == 404:
                raise HTTPException(status_code=502, detail="worker route not found (/api/push-job)")
            return r.json()
        except Exception as e:
            last_exc = e
            log.warning("push retry after %ss: %s", delay, e)
            continue
    # дед-леттер: если хотите — можно писать в БД для повторной доставки
    raise HTTPException(status_code=502, detail=f"push failed after retries: {last_exc}")

# маленький «ручной» тест (не обязателен)
@app.post("/api/test/push-smoke")
async def push_smoke():
    job = {
        "job_id": f"smoke-{int(time.time())}",
        "city": "Тверь",
        "category": "Разнорабочие (общие)",
        "category_slug": norm_slug("Разнорабочие (общие)"),
        "title": "Тестовая задача",
        "details": "smoke test from customer",
    }
    res = await push_to_worker(job)
    return {"ok": True, "worker_response": res}
