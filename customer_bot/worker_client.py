# customer_bot/worker_client.py
import os
from typing import Optional, Dict, Any
import httpx

WORKER_API_URL = os.getenv("WORKER_API_URL")  # может быть None

async def call_worker(endpoint: str, payload: Dict[str, Any], timeout_s: float = 8.0) -> Optional[Dict[str, Any]]:
    """
    Вернёт JSON или None, если воркер не настроен/упал. Никаких исключений наружу.
    """
    if not WORKER_API_URL:
        return None
    url = f"{WORKER_API_URL.rstrip('/')}/{endpoint.lstrip('/')}"
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            r = await client.post(url, json=payload)
            r.raise_for_status()
            return r.json()
    except Exception:
        return None
