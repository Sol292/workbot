import os
from json import JSONDecodeError
from datetime import datetime, timedelta

from fastapi import FastAPI, Request, HTTPException
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, MessageHandler,
    ConversationHandler, ContextTypes, filters
)

# ========= ENV =========
BOT_TOKEN = os.environ["BOT_TOKEN"]  # если нет — пусть падает
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "secret")
BASE_URL = os.environ.get("BASE_URL")

# ========= FASTAPI & PTB =========
app = FastAPI()
tg_app: Application = ApplicationBuilder().token(BOT_TOKEN).build()

# ========= IN-MEMORY STORAGE =========
# На прод базу добавим позже. Пока: {user_id: [jobs]}
JOBS: dict[int, list[dict]] = {}

# ========= NEW JOB WIZARD =========
(
    S_CAT,
    S_ADDR,
    S_WHEN,
    S_PAY,
    S_PHOTOS,
    S_CONFIRM,
) = range(6)

CATEGORIES = [
    "Разнорабочие (общие)",
    "Погрузка/разгрузка",
    "Демонтаж",
    "Клининг",
    "Курьер/доставка",
    "Подсобник на стройку",
]

def _main_menu_kb():
    return ReplyKeyboardMarkup(
        [["/newjob", "/myjobs"], ["/cancel"]],
        resize_keyboard=True
    )

def _categories_kb():
    # 2 столбца
    rows, row = [], []
    for i, name in enumerate(CATEGORIES, 1):
        row.append(name)
        if i % 2 == 0:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append(["Отмена"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я помогу найти разнорабочих рядом.\n"
        "Команды:\n"
        "• /newjob — создать задачу\n"
        "• /myjobs — мои задачи\n"
        "• /cancel — отменить диалог",
        reply_markup=_main_menu_kb()
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Команды: /newjob, /myjobs, /cancel",
        reply_markup=_main_menu_kb()
    )

async def newjob_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["photos"] = []
    await update.message.reply_text(
        "Выбери категорию задачи:",
        reply_markup=_categories_kb()
    )
    return S_CAT

async def newjob_cat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "Отмена":
        await update.message.reply_text("Отменено.", reply_markup=_main_menu_kb())
        return ConversationHandler.END
    if text not in CATEGORIES:
        await update.message.reply_text("Выбери из списка кнопок.", reply_markup=_categories_kb())
        return S_CAT
    context.user_data["category"] = text
    await update.message.reply_text(
        "Укажи адрес или локацию текстом (город, улица, дом).",
        reply_markup=ReplyKeyboardRemove()
    )
    return S_ADDR

async def newjob_addr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    addr = update.message.text.strip()
    if len(addr) < 5:
        await update.message.reply_text("Слишком коротко. Напиши адрес подробнее.")
        return S_ADDR
    context.user_data["address"] = addr
    # подсказка по времени
    dt_hint = (datetime.now() + timedelta(hours=2)).strftime("%d.%m %H:%M")
    await update.message.reply_text(
        f"Когда начать? Форматы примеров: 'сегодня 18:00', 'завтра 09:30', '{dt_hint}'."
    )
    return S_WHEN

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
        # dd.mm HH:MM
        if len(s) >= 5:
            return datetime.strptime(s, "%d.%m %H:%M").replace(year=now.year)
    except Exception:
        return None
    return None

async def newjob_when(update: Update, context: ContextTypes.DEFAULT_TYPE):
    dt = _parse_when(update.message.text)
    if not dt:
        await update.message.reply_text(
            "Не понял время. Примеры: 'сегодня 18:00', 'завтра 09:30', '25.10 14:00'."
        )
        return S_WHEN
    if dt < datetime.now() - timedelta(minutes=1):
        await update.message.reply_text("Это время уже прошло. Укажи будущее.")
        return S_WHEN
    context.user_data["when"] = dt.strftime("%d.%m %H:%M")
    await update.message.reply_text(
        "Бюджет/ставка? Например: '500 рублей/час' или '2000 фикс'."
    )
    return S_PAY

async def newjob_pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if len(txt) < 3:
        await update.message.reply_text("Укажи сумму/ставку текстом.")
        return S_PAY
    context.user_data["pay"] = txt
    await update.message.reply_text(
        "Прикрепи 0–3 фото (по одному сообщению). Когда хватит — отправь 'Готово'.\n"
        "Можно пропустить — напиши 'Пропустить'."
    )
    return S_PHOTOS

async def newjob_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photos = context.user_data.get("photos", [])
    if len(photos) >= 3:
        await update.message.reply_text("Максимум 3 фото. Напиши 'Готово' или 'Пропустить'.")
        return S_PHOTOS
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
        photos.append(file_id)
        context.user_data["photos"] = photos
        await update.message.reply_text(f"Фото принято ({len(photos)}/3). Ещё? Или напиши 'Готово'.")
        return S_PHOTOS
    # если пришёл не файл
    await update.message.reply_text("Отправь фото или напиши 'Готово' / 'Пропустить'.")
    return S_PHOTOS

async def newjob_photos_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _confirm(update, context)

async def _confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = context.user_data
    text = (
        "Проверь карточку задачи:\n\n"
        f"• Категория: {d['category']}\n"
        f"• Адрес: {d['address']}\n"
        f"• Когда: {d['when']}\n"
        f"• Бюджет: {d['pay']}\n\n"
        "Отправить? Напиши 'Да' или 'Нет'."
    )
    await update.message.reply_text(text)
    return S_CONFIRM

async def newjob_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ans = update.message.text.lower().strip()
    if ans not in ("да", "нет"):
        await update.message.reply_text("Ответь 'Да' или 'Нет'.")
        return S_CONFIRM
    if ans == "нет":
        await update.message.reply_text("Ок, отменил.", reply_markup=_main_menu_kb())
        context.user_data.clear()
        return ConversationHandler.END

    # Сохраняем задачу
    uid = update.effective_user.id
    job = {
        "id": int(datetime.now().timestamp() * 1000),
        "user_id": uid,
        "category": context.user_data["category"],
        "address": context.user_data["address"],
        "when": context.user_data["when"],
        "pay": context.user_data["pay"],
        "photos": context.user_data.get("photos", []),
        "status": "new",
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    JOBS.setdefault(uid, []).append(job)

    context.user_data.clear()
    await update.message.reply_text(
        f"Задача создана ✅\nID: {job['id']}\n"
        "Скоро добавим рассылку исполнителям.\n"
        "Посмотреть список: /myjobs",
        reply_markup=_main_menu_kb()
    )
    return ConversationHandler.END

async def list_myjobs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    items = JOBS.get(uid, [])
    if not items:
        await update.message.reply_text("У тебя пока нет задач.", reply_markup=_main_menu_kb())
        return
    lines = []
    for j in sorted(items, key=lambda x: x["created_at"], reverse=True)[:10]:
        lines.append(
            f"#{j['id']} • {j['category']} • {j['when']}\n"
            f"{j['address']}\n"
            f"{j['pay']}\n"
            f"Статус: {j['status']}\n"
        )
    await update.message.reply_text("Твои задачи:\n\n" + "\n".join(lines), reply_markup=_main_menu_kb())

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Отменено.", reply_markup=_main_menu_kb())
    return ConversationHandler.END

# ========= REGISTER HANDLERS =========
conv = ConversationHandler(
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
    fallbacks=[CommandHandler("cancel", cancel)],
    allow_reentry=True,
)

tg_app.add_handler(CommandHandler("start", cmd_start))
tg_app.add_handler(CommandHandler("help", cmd_help))
tg_app.add_handler(CommandHandler("myjobs", list_myjobs))
tg_app.add_handler(CommandHandler("cancel", cancel))
tg_app.add_handler(conv)

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

# ========= HEALTH / DEBUG =========
@app.get("/")
async def root():
    return {"ok": True}

@app.get("/debug")
async def debug():
    return {
        "has_bot_token": bool(BOT_TOKEN),
        "has_base_url": bool(BASE_URL),
        "base_url": BASE_URL,
        "jobs_count": sum(len(v) for v in JOBS.values()),
    }
