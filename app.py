import os
from json import JSONDecodeError
from datetime import datetime, timedelta

from fastapi import FastAPI, Request, HTTPException
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, MessageHandler,
    ConversationHandler, CallbackQueryHandler, ContextTypes, filters
)

# ========= ENV =========
BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "secret")
BASE_URL = os.environ.get("BASE_URL")

# ========= FASTAPI & PTB =========
app = FastAPI()
tg_app: Application = ApplicationBuilder().token(BOT_TOKEN).build()

# ========= IN-MEMORY STORAGE =========
JOBS: dict[int, list[dict]] = {}          # {customer_id: [job, ...]}
USERS: dict[int, dict] = {}               # {user_id: {chat_id, username, role}}
WORKERS: dict[int, dict] = {}             # {user_id: {city, cats:set, radius:int, available:bool}}
JOB_BY_ID: dict[int, dict] = {}           # {job_id: job}
# job = {id, user_id, category, address, when, pay, photos, status, created_at, bids:list[int], chosen_worker: int|None}

# ========= CONSTANTS =========
CATEGORIES = [
    "Разнорабочие (общие)",
    "Погрузка/разгрузка",
    "Демонтаж",
    "Клининг",
    "Курьер/доставка",
    "Подсобник на стройку",
]
RADIUS_CHOICES = [2, 5, 10, 25]

# ========= HELPERS =========
def _main_menu_kb():
    return ReplyKeyboardMarkup(
        [["/newjob", "/myjobs"], ["/workmode", "/go_on", "/go_off"], ["/help"]],
        resize_keyboard=True
    )

def _categories_kb():
    rows, row = [], []
    for i, name in enumerate(CATEGORIES, 1):
        row.append(name)
        if i % 2 == 0:
            rows.append(row); row = []
    if row: rows.append(row)
    rows.append(["Отмена"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def _yesno_kb():
    return ReplyKeyboardMarkup([["Да","Нет"]], resize_keyboard=True)

def _now():
    return datetime.now().isoformat(timespec="seconds")

def ensure_user(update: Update):
    u = update.effective_user
    USERS[u.id] = {
        "chat_id": update.effective_chat.id,
        "username": (u.username and "@"+u.username) or f"id{u.id}",
        "role": USERS.get(u.id,{}).get("role")
    }

# ========= BASIC COMMANDS =========
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update)
    await update.message.reply_text(
        "Привет! Я помогу связать заказчиков и исполнителей.\n"
        "• /newjob — создать задачу (заказчик)\n"
        "• /myjobs — мои задачи\n"
        "• /workmode — настроить профиль исполнителя\n"
        "• /go_on /go_off — вкл/выкл доступность исполнителя\n"
        "• /help — помощь",
        reply_markup=_main_menu_kb()
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Команды:\n"
        "/newjob — мастер создания задачи\n"
        "/myjobs — список твоих задач\n"
        "/workmode — онбординг исполнителя (город, категории, радиус)\n"
        "/go_on /go_off — включить/выключить доступность исполнителя",
        reply_markup=_main_menu_kb()
    )

async def list_myjobs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    items = JOBS.get(uid, [])
    if not items:
        await update.message.reply_text("У тебя пока нет задач.", reply_markup=_main_menu_kb())
        return
    lines = []
    for j in sorted(items, key=lambda x: x["created_at"], reverse=True)[:10]:
        chosen = j.get("chosen_worker")
        ch_txt = f"\nВыбран: @{USERS[chosen]['username'][1:]}" if chosen else ""
        bids_txt = f"Отклики: {len(j.get('bids', []))}"
        lines.append(
            f"#{j['id']} • {j['category']} • {j['when']}\n"
            f"{j['address']}\n{j['pay']}\nСтатус: {j['status']} • {bids_txt}{ch_txt}\n"
        )
    await update.message.reply_text("Твои задачи:\n\n" + "\n".join(lines), reply_markup=_main_menu_kb())

# ========= NEW JOB WIZARD =========
(S_CAT, S_ADDR, S_WHEN, S_PAY, S_PHOTOS, S_CONFIRM) = range(6)

def _parse_when(s: str) -> datetime | None:
    s = s.lower().strip()
    now = datetime.now()
    try:
        if s.startswith("сегодня"):
            t = s.replace("сегодня", "").strip()
            dt = datetime.strptime(t, "%H:%M")
            return now.replace(hour=dt.hour, minute=dt.minute, second=0, microsecond=0)
        if s.startswith("завтра"):
            t = s.replace("завтра", "").strip()
            dt = datetime.strptime(t, "%H:%M")
            return (now + timedelta(days=1)).replace(hour=dt.hour, minute=dt.minute, second=0, microsecond=0)
        return datetime.strptime(s, "%d.%m %H:%M").replace(year=now.year)
    except Exception:
        return None

async def newjob_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update)
    USERS[update.effective_user.id]["role"] = USERS.get(update.effective_user.id,{}).get("role") or "customer"
    context.user_data.clear(); context.user_data["photos"] = []
    await update.message.reply_text("Выбери категорию задачи:", reply_markup=_categories_kb())
    return S_CAT

async def newjob_cat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "Отмена":
        await update.message.reply_text("Отменено.", reply_markup=_main_menu_kb())
        return ConversationHandler.END
    if text not in CATEGORIES:
        await update.message.reply_text("Выбери из списка кнопок.", reply_markup=_categories_kb()); return S_CAT
    context.user_data["category"] = text
    await update.message.reply_text("Укажи адрес (город, улица, дом).", reply_markup=ReplyKeyboardRemove())
    return S_ADDR

async def newjob_addr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    addr = update.message.text.strip()
    if len(addr) < 5:
        await update.message.reply_text("Слишком коротко. Напиши адрес подробнее."); return S_ADDR
    context.user_data["address"] = addr
    dt_hint = (datetime.now() + timedelta(hours=2)).strftime("%d.%m %H:%M")
    await update.message.reply_text(f"Когда начать? Примеры: 'сегодня 18:00', 'завтра 09:30', '{dt_hint}'.")
    return S_WHEN

async def newjob_when(update: Update, context: ContextTypes.DEFAULT_TYPE):
    dt = _parse_when(update.message.text)
    if not dt or dt < datetime.now() - timedelta(minutes=1):
        await update.message.reply_text("Не понял время. Примеры: 'сегодня 18:00', 'завтра 09:30', '25.10 14:00'."); return S_WHEN
    context.user_data["when"] = dt.strftime("%d.%m %H:%M")
    await update.message.reply_text("Бюджет/ставка? Например: '500 ₽/час' или '2000 фикс'.")
    return S_PAY

async def newjob_pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if len(txt) < 3:
        await update.message.reply_text("Укажи сумму/ставку текстом."); return S_PAY
    context.user_data["pay"] = txt
    await update.message.reply_text("Прикрепи 0–3 фото. Когда хватит — напиши 'Готово'. Можно 'Пропустить'.")
    return S_PHOTOS

async def newjob_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photos = context.user_data.get("photos", [])
    if update.message.photo:
        if len(photos) >= 3:
            await update.message.reply_text("Максимум 3 фото. Напиши 'Готово'."); return S_PHOTOS
        photos.append(update.message.photo[-1].file_id)
        context.user_data["photos"] = photos
        await update.message.reply_text(f"Фото принято ({len(photos)}/3). Ещё? Или 'Готово'.")
    else:
        await update.message.reply_text("Отправь фото или напиши 'Готово' / 'Пропустить'.")
    return S_PHOTOS

async def newjob_photos_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = context.user_data
    txt = (
        "Проверь карточку задачи:\n\n"
        f"• Категория: {d['category']}\n"
        f"• Адрес: {d['address']}\n"
        f"• Когда: {d['when']}\n"
        f"• Бюджет: {d['pay']}\n\n"
        "Отправить? (Да/Нет)"
    )
    await update.message.reply_text(txt, reply_markup=_yesno_kb())
    return S_CONFIRM

async def newjob_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text.lower().strip() != "да":
        await update.message.reply_text("Ок, отменил.", reply_markup=_main_menu_kb()); context.user_data.clear()
        return ConversationHandler.END

    uid = update.effective_user.id
    d = context.user_data
    job = {
        "id": int(datetime.now().timestamp() * 1000),
        "user_id": uid,
        "category": d["category"],
        "address": d["address"],
        "when": d["when"],
        "pay": d["pay"],
        "photos": d.get("photos", []),
        "status": "new",
        "created_at": _now(),
        "bids": [],
        "chosen_worker": None,
    }
    JOBS.setdefault(uid, []).append(job)
    JOB_BY_ID[job["id"]] = job
    context.user_data.clear()

    await update.message.reply_text(
        f"Задача создана ✅\nID: {job['id']}\nНачинаю рассылку исполнителям.",
        reply_markup=_main_menu_kb()
    )

    # Рассылка
    await broadcast_job(job)
    return ConversationHandler.END

# ========= WORKER ONBOARDING =========
(W_CITY, W_CATS, W_RADIUS, W_DONE) = range(10,14)

async def cmd_workmode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update)
    USERS[update.effective_user.id]["role"] = "worker"
    await update.message.reply_text("Укажи город одним словом (например: 'Тверь').", reply_markup=ReplyKeyboardRemove())
    return W_CITY

async def w_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    city = update.message.text.strip()
    if not city or " " in city and len(city) < 3:
        await update.message.reply_text("Коротко, одним словом: 'Москва', 'Тверь'."); return W_CITY
    context.user_data["city"] = city
    context.user_data["cats"] = set()
    await update.message.reply_text("Выбирай категории (нажимай по очереди). Когда закончишь — 'Готово'.", reply_markup=_categories_kb())
    return W_CATS

async def w_cats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "Отмена":
        await update.message.reply_text("Отменено.", reply_markup=_main_menu_kb()); return ConversationHandler.END
    if text == "Готово":
        if not context.user_data["cats"]:
            await update.message.reply_text("Выбери хотя бы одну категорию."); return W_CATS
        # радиус
        rows = [[str(r) + " км" for r in RADIUS_CHOICES[:2]], [str(r) + " км" for r in RADIUS_CHOICES[2:]]]
        await update.message.reply_text("Выбери радиус работы:", reply_markup=ReplyKeyboardMarkup(rows, resize_keyboard=True))
        return W_RADIUS
    if text not in CATEGORIES:
        await update.message.reply_text("Выбирай кнопками или 'Готово'.", reply_markup=_categories_kb()); return W_CATS
    # toggle
    s = context.user_data["cats"]
    if text in s: s.remove(text)
    else: s.add(text)
    await update.message.reply_text("Текущие: " + ", ".join(s) + "\nКогда закончишь — 'Готово'.", reply_markup=_categories_kb())
    return W_CATS

async def w_radius(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.replace(" км","").strip()
    if not t.isdigit() or int(t) not in RADIUS_CHOICES:
        await update.message.reply_text("Выбери из кнопок радиус."); return W_RADIUS
    radius = int(t)
    uid = update.effective_user.id
    WORKERS[uid] = {
        "city": context.user_data["city"],
        "cats": set(context.user_data["cats"]),
        "radius": radius,
        "available": True,
    }
    USERS[uid]["role"] = "worker"
    context.user_data.clear()
    await update.message.reply_text(
        f"Готово ✅\nГород: {WORKERS[uid]['city']}\nКатегории: {', '.join(WORKERS[uid]['cats'])}\n"
        f"Радиус: {radius} км\nСтатус: Доступен\n\nКоманды: /go_off (выключить), /go_on (включить).",
        reply_markup=_main_menu_kb()
    )
    return ConversationHandler.END

async def go_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid in WORKERS:
        WORKERS[uid]["available"] = True
        await update.message.reply_text("Статус: Доступен ✅", reply_markup=_main_menu_kb())
    else:
        await update.message.reply_text("Сначала /workmode.", reply_markup=_main_menu_kb())

async def go_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid in WORKERS:
        WORKERS[uid]["available"] = False
        await update.message.reply_text("Статус: Недоступен ⛔", reply_markup=_main_menu_kb())
    else:
        await update.message.reply_text("Сначала /workmode.", reply_markup=_main_menu_kb())

# ========= MATCHING / BIDS =========
async def broadcast_job(job: dict):
    """Очень простой матчинг: по категории + по вхождению города в адрес (без геокодинга) + доступность."""
    cat = job["category"]
    address_low = job["address"].lower()
    notified = 0
    for wid, w in WORKERS.items():
        if not w["available"]:
            continue
        if cat not in w["cats"]:
            continue
        # грубо: если город исполнителя встречается в адресе
        if w["city"].lower() not in address_low:
            continue
        # отправляем карточку
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Откликнуться", callback_data=f"bid:{job['id']}")]])
        text = (
            f"Новая задача #{job['id']}\n"
            f"{job['category']} • {job['when']}\n"
            f"{job['address']}\n"
            f"Бюджет: {job['pay']}"
        )
        try:
            await tg_app.bot.send_message(chat_id=USERS.get(wid,{}).get("chat_id", wid), text=text, reply_markup=kb)
            notified += 1
        except Exception as e:
            print("[broadcast error]", wid, repr(e))
    # уведомим заказчика, сколько ушло
    try:
        await tg_app.bot.send_message(chat_id=USERS[job["user_id"]]["chat_id"], text=f"Рассылка отправлена {notified} исполнителям.")
    except Exception:
        pass

async def cbq_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка кнопок inline."""
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    if data.startswith("bid:"):
        job_id = int(data.split(":")[1])
        return await handle_bid(update, job_id)
    if data.startswith("pick:"):
        _, job_id_s, worker_id_s = data.split(":")
        return await handle_pick(update, int(job_id_s), int(worker_id_s))

async def handle_bid(update: Update, job_id: int):
    wid = update.effective_user.id
    job = JOB_BY_ID.get(job_id)
    if not job:
        return await update.callback_query.edit_message_text("Задача уже недоступна.")
    if wid not in WORKERS:
        return await update.callback_query.edit_message_text("Сначала /workmode, чтобы откликаться.")
    if wid in job["bids"]:
        return await update.callback_query.edit_message_text("Ты уже откликался на эту задачу.")
    job["bids"].append(wid)
    await update.callback_query.edit_message_text("Отклик отправлен ✅")
    # уведомить заказчика и предложить выбрать
    cust_id = job["user_id"]
    lines = []
    buttons = []
    for w_id in job["bids"][:5]:
        w = WORKERS.get(w_id, {})
        uname = USERS.get(w_id, {}).get("username", f"id{w_id}")
        lines.append(f"• {uname} — {', '.join(w.get('cats', []))} ({w.get('city')})")
        buttons.append([InlineKeyboardButton(f"Выбрать {uname}", callback_data=f"pick:{job_id}:{w_id}")])
    txt = f"Новые отклики по #{job_id}:\n" + "\n".join(lines)
    try:
        await tg_app.bot.send_message(chat_id=USERS[cust_id]["chat_id"], text=txt, reply_markup=InlineKeyboardMarkup(buttons))
    except Exception as e:
        print("[notify customer error]", repr(e))

async def handle_pick(update: Update, job_id: int, worker_id: int):
    job = JOB_BY_ID.get(job_id)
    if not job:
        return await update.callback_query.edit_message_text("Задача уже недоступна.")
    cust_id = update.effective_user.id
    if job["user_id"] != cust_id:
        return await update.callback_query.edit_message_text("Выбирать может только заказчик задачи.")
    if job.get("chosen_worker"):
        return await update.callback_query.edit_message_text("Исполнитель уже выбран.")
    job["chosen_worker"] = worker_id
    job["status"] = "assigned"
    # уведомить обе стороны (раскрываем контакты-юзернеймы)
    cust_un = USERS.get(cust_id,{}).get("username", f"id{cust_id}")
    work_un = USERS.get(worker_id,{}).get("username", f"id{worker_id}")
    await update.callback_query.edit_message_text(f"Выбран исполнитель: {work_un} ✅")
    try:
        await tg_app.bot.send_message(chat_id=USERS[worker_id]["chat_id"],
                                      text=f"Тебя выбрали на задачу #{job_id}. Связь с заказчиком: {cust_un}")
    except Exception: pass
    try:
        await tg_app.bot.send_message(chat_id=USERS[cust_id]["chat_id"],
                                      text=f"Контакты исполнителя по #{job_id}: {work_un}")
    except Exception: pass

# ========= CONVERSATIONS =========
conv_newjob = ConversationHandler(
    entry_points=[CommandHandler("newjob", newjob_start)],
    states={
        S_CAT: [MessageHandler(filters.TEXT & ~filters.COMMAND, newjob_cat)],
        S_ADDR: [MessageHandler(filters.TEXT & ~filters.COMMAND, newjob_addr)],
        S_WHEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, newjob_when)],
        S_PAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, newjob_pay)],
        S_PHOTOS: [
            MessageHandler(filters.PHOTO, newjob_photo),
            MessageHandler(filters.Regex("^(Готово|Пропустить)$"), newjob_photos_done),
            MessageHandler(filters.TEXT & ~filters.COMMAND, newjob_photo),
        ],
        S_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, newjob_confirm)],
    },
    fallbacks=[CommandHandler("cancel", lambda u,c: u.message.reply_text("Отменено.", reply_markup=_main_menu_kb()))],
    allow_reentry=True,
)

conv_worker = ConversationHandler(
    entry_points=[CommandHandler("workmode", cmd_workmode)],
    states={
        W_CITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, w_city)],
        W_CATS: [MessageHandler(filters.TEXT & ~filters.COMMAND, w_cats)],
        W_RADIUS: [MessageHandler(filters.TEXT & ~filters.COMMAND, w_radius)],
    },
    fallbacks=[CommandHandler("cancel", lambda u,c: u.message.reply_text("Отменено.", reply_markup=_main_menu_kb()))],
    allow_reentry=True,
)

tg_app.add_handler(CommandHandler("start", cmd_start))
tg_app.add_handler(CommandHandler("help", cmd_help))
tg_app.add_handler(CommandHandler("myjobs", list_myjobs))
tg_app.add_handler(CommandHandler("go_on", go_on))
tg_app.add_handler(CommandHandler("go_off", go_off))
tg_app.add_handler(conv_newjob)
tg_app.add_handler(conv_worker)
tg_app.add_handler(CallbackQueryHandler(cbq_handler))

# ========= WEBHOOK LIFECYCLE =========
@app.on_event("startup")
async def on_startup():
    await tg_app.initialize()
    if BASE_URL:
        url = f"{BASE_URL}/tg/webhook?secret={WEBHOOK_SECRET}"
        await tg_app.bot.set_webhook(url=url)
        print(f"[webhook] set to {url}")
    else:
        print("[webhook] BASE_URL не задан. Для локального теста используй polling.py.")

@app.on_event("shutdown")
async def on_shutdown():
    await tg_app.shutdown()

# ========= WEBHOOK ENDPOINT =========
@app.post("/tg/webhook")
async def telegram_webhook(request: Request, secret: str):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="bad secret")
    try:
        data = await request.json()
    except JSONDecodeError:
        return {"ok": True}
    if not isinstance(data, dict) or "update_id" not in data:
        return {"ok": True}
    update = Update.de_json(data, tg_app.bot)
    try:
        await tg_app.process_update(update)
    except Exception as e:
        print("[webhook error]", repr(e))
        return {"ok": False}
    return {"ok": True}

# ========= HEALTH =========
@app.get("/")
async def root():
    return {"ok": True}
