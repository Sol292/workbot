# env.py
import os

BOT_TOKEN       = os.getenv("BOT_TOKEN")
WEBHOOK_SECRET  = os.getenv("WEBHOOK_SECRET")
BASE_URL        = os.getenv("BASE_URL")
WORKER_API_URL  = os.getenv("WORKER_API_URL")  # может быть None

required = {"BOT_TOKEN": BOT_TOKEN, "WEBHOOK_SECRET": WEBHOOK_SECRET, "BASE_URL": BASE_URL}
missing = [k for k, v in required.items() if not v]
if missing:
    raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")