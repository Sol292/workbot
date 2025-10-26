# CUSTOMER BOT
import os, re
from datetime import datetime, timedelta
from json import JSONDecodeError
import dateparser
from fastapi import FastAPI, Request, HTTPException, Header
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, ApplicationBuilder, CommandHandler, MessageHandler, ConversationHandler, CallbackQueryHandler, ContextTypes, filters
import httpx

BOT_TOKEN       = os.getenv("BOT_TOKEN")
WEBHOOK_SECRET  = os.getenv("WEBHOOK_SECRET")
BASE_URL        = os.getenv("BASE_URL")
JOBS_API_TOKEN = os.getenv["JOBS_API_TOKEN"]
WORKER_API_URL = os.getenv("WORKER_API_URL")
WORKER_BOT_USERNAME = os.getenv("WORKER_BOT_USERNAME")

# Жёстко требуем только то, без чего сервер жить не может:
required = {
    "BOT_TOKEN": BOT_TOKEN,
    "WEBHOOK_SECRET": WEBHOOK_SECRET,
    "BASE_URL": BASE_URL,
}
missing = [k for k, v in required.items() if not v]
if missing:
    raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")

from worker_client import call_worker   # импорт по пути из корня репо

# Пример FastAPI-роута
from fastapi import FastAPI, HTTPException
app = FastAPI()

@app.post("/match")
async def match(req: dict):
    data = await call_worker("match", req)
    if data is None:
        # воркера нет/упал — мягко отвечаем
        raise HTTPException(status_code=501, detail="Worker is not configured")
        # или: return {"status": "degraded", "items": []}
    return {"status": "ok", "items": data.get("items", [])}

# А с WORKER_API_URL — мягко: если нет, просто отключаем интеграцию
if not WORKER_API_URL:
    print("[WARN] WORKER_API_URL is not set — worker-интеграция будет отключена.")

BTN_NEWJOB="Создать задачу"; BTN_MYJOBS="Мои задачи"; BTN_HELP="Помощь"
BTN_CANCEL="Отмена"; BTN_DONE="Готово"; BTN_SKIP="Пропустить"

CATEGORIES=["Разнорабочие (общие)","Погрузка/разгрузка","Демонтаж","Клининг","Курьер/доставка","Подсобник на стройку","Сборка мебели","Малярные работы","Электромонтаж (простые)","Сантехника (простые)","Вынос мусора","Уборка после ремонта"]

app=FastAPI()
tg_app:Application=ApplicationBuilder().token(BOT_TOKEN).build()

USERS:dict[int,dict]={}           # {user_id:{chat_id,username}}
JOBS:dict[int,list[dict]]={}      # {customer_id:[job]}
JOB_BY_ID:dict[int,dict]={}

def kb_main(): return ReplyKeyboardMarkup([[BTN_NEWJOB, BTN_MYJOBS],[BTN_HELP]], resize_keyboard=True)
def kb_cats(): 
    rows,row=[],[]
    for i,n in enumerate(CATEGORIES,1):
        row.append(n); 
        if i%2==0: rows.append(row); row=[]
    if row: rows.append(row)
    rows.append([BTN_DONE, BTN_CANCEL]); 
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def ensure_user(u:Update):
    USERS[u.effective_user.id]={"chat_id":u.effective_chat.id,"username":("@" + u.effective_user.username) if u.effective_user.username else f"id{u.effective_user.id}"}

def parse_when(text:str):
    now=datetime.now()
    t=(text or "").strip()
    if re.fullmatch(r"\d{1,2}:\d{2}", t):
        hh,mm=t.split(":"); dt=now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        return dt if dt>now else dt+timedelta(days=1)
    return dateparser.parse(t, languages=["ru"], settings={"PREFER_DATES_FROM":"future","RELATIVE_BASE":now,"DATE_ORDER":"DMY"})

async def start(u:Update,c:ContextTypes.DEFAULT_TYPE):
    ensure_user(u)
    msg="Привет! Это бот для заказчиков.\nСоздавай задачи — я разошлю их исполнителям."
    if WORKER_BOT_USERNAME: msg+=f"\nБот исполнителя: {WORKER_BOT_USERNAME}"
    await u.message.reply_text(msg, reply_markup=kb_main())

async def help_cmd(u:Update,c): await u.message.reply_text("Используй кнопки внизу.", reply_markup=kb_main())

async def myjobs(u:Update,c):
    uid=u.effective_user.id; items=JOBS.get(uid,[])
    if not items: return await u.message.reply_text("У тебя пока нет задач.", reply_markup=kb_main())
    lines=[]
    for j in sorted(items,key=lambda x:x["created_at"],reverse=True)[:10]:
        chosen=j.get("chosen_worker"); ch_txt=f"\nВыбран: {chosen}" if chosen else ""
        lines.append(f"#{j['id']} • {j['category']} • {j['when']}\n{j['address']}\n{j['pay']}{ch_txt}\n")
    await u.message.reply_text("Твои задачи:\n\n"+"\n".join(lines), reply_markup=kb_main())

# --- мастер создания
(S_CAT,S_ADDR,S_WHEN,S_PAY,S_PHOTOS,S_CONFIRM)=range(6)

async def newjob_start(u:Update,c):
    ensure_user(u); c.user_data.clear(); c.user_data["photos"]=[]
    await u.message.reply_text("Выбери категорию:", reply_markup=kb_cats()); return S_CAT

async def newjob_cat(u:Update,c):
    t=(u.message.text or "").strip()
    if t.lower()==BTN_CANCEL.lower(): await u.message.reply_text("Отменено.", reply_markup=kb_main()); return ConversationHandler.END
    if t not in CATEGORIES: await u.message.reply_text("Выбери из кнопок.", reply_markup=kb_cats()); return S_CAT
    c.user_data["category"]=t
    await u.message.reply_text("Адрес (город, улица, дом):", reply_markup=ReplyKeyboardRemove()); return S_ADDR

async def newjob_addr(u:Update,c):
    a=(u.message.text or "").strip()
    if len(a)<5: await u.message.reply_text("Слишком коротко. Попробуй ещё."); return S_ADDR
    c.user_data["address"]=a
    hint=(datetime.now()+timedelta(hours=2)).strftime("%d.%m %H:%M")
    await u.message.reply_text(f"Когда начать? Примеры: 'сегодня 18:00', 'завтра 09:30', '{hint}', '18:00'."); return S_WHEN

async def newjob_when(u:Update,c):
    dt=parse_when(u.message.text)
    if not dt: await u.message.reply_text("Не понял время. Дай в удобной форме."); return S_WHEN
    c.user_data["when"]=dt.strftime("%d.%m %H:%M")
    await u.message.reply_text("Бюджет/ставка? (например: '800 ₽/час' или '2000 фикс')"); return S_PAY

async def newjob_pay(u:Update,c):
    txt=(u.message.text or "").strip()
    if len(txt)<3: await u.message.reply_text("Укажи сумму/ставку текстом."); return S_PAY
    c.user_data["pay"]=txt
    await u.message.reply_text(f"Прикрепи 0–3 фото. Когда хватит — напиши '{BTN_DONE}'. Можно '{BTN_SKIP}'."); return S_PHOTOS

async def newjob_photo(u:Update,c):
    text=(u.message.text or "").lower() if u.message and u.message.text else ""
    if text in (BTN_DONE.lower(), BTN_SKIP.lower()):
        d=c.user_data; await u.message.reply_text(f"Проверь:\n• {d['category']}\n• {d['address']}\n• {d['when']}\n• {d['pay']}\nОтправить? (Да/Нет)")
        return S_CONFIRM
    photos=c.user_data.get("photos",[])
    if u.message and u.message.photo:
        if len(photos)>=3: await u.message.reply_text("Максимум 3 фото. Напиши 'Готово'."); return S_PHOTOS
        photos.append(u.message.photo[-1].file_id); c.user_data["photos"]=photos
        await u.message.reply_text(f"Фото принято ({len(photos)}/3). Ещё? Или '{BTN_DONE}'.")
    else:
        await u.message.reply_text(f"Отправь фото или '{BTN_DONE}' / '{BTN_SKIP}'.")
    return S_PHOTOS

async def newjob_confirm(u:Update,c):
    if (u.message.text or "").strip().lower()!="да":
        await u.message.reply_text("Ок, отменил.", reply_markup=kb_main()); c.user_data.clear(); return ConversationHandler.END
    uid=u.effective_user.id; d=c.user_data
    job={"id":int(datetime.now().timestamp()*1000),"user_id":uid,"category":d["category"],"address":d["address"],"when":d["when"],"pay":d["pay"],"photos":d.get("photos",[]),"created_at":datetime.now().isoformat(timespec="seconds"),"bids":[],"chosen_worker":None}
    JOBS.setdefault(uid,[]).append(job); JOB_BY_ID[job["id"]]=job; c.user_data.clear()
    await u.message.reply_text(f"Задача создана ✅\nID: {job['id']}\nРассылаю исполнителям…", reply_markup=kb_main())
    # пуш в worker-сервис
    try:
        async with httpx.AsyncClient(timeout=10) as cli:
            await cli.post(f"{WORKER_API_URL}/api/push-job",
                           headers={"Authorization":f"Bearer {JOBS_API_TOKEN}"},
                           json={"job":job,"customer_contact":USERS[uid]["username"],"callback_url":f"{BASE_URL}/api/new-bid"})
    except Exception as e:
        await tg_app.bot.send_message(chat_id=USERS[uid]["chat_id"], text=f"Не смог разослать задачу исполнителям: {e}")
    return ConversationHandler.END

# --- колбэки/отклики из worker-сервиса
@app.post("/api/new-bid")
async def api_new_bid(req:Request, authorization: str | None = Header(default=None)):
    if not (authorization and authorization.lower().startswith("bearer ") and authorization.split(" ",1)[1].strip()==JOBS_API_TOKEN):
        raise HTTPException(status_code=401, detail="bad api token")
    payload=await req.json()
    job_id=int(payload["job_id"]); worker_username=str(payload["worker_username"])
    job=JOB_BY_ID.get(job_id); 
    if not job: return {"ok":True}
    # уведомим заказчика
    kb=InlineKeyboardMarkup([[InlineKeyboardButton(f"Выбрать {worker_username}", callback_data=f"pick:{job_id}:{worker_username}")]])
    await tg_app.bot.send_message(chat_id=USERS[job["user_id"]]["chat_id"],
                                  text=f"Новый отклик на #{job_id}: {worker_username}", reply_markup=kb)
    return {"ok":True}

async def cbq(update:Update, context:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer()
    data=q.data or ""
    if data.startswith("pick:"):
        _, job_id_s, worker_username = data.split(":"); job_id=int(job_id_s)
        job=JOB_BY_ID.get(job_id)
        if not job: return await q.edit_message_text("Задача не найдена.")
        if update.effective_user.id!=job["user_id"]: return await q.edit_message_text("Выбирать может только заказчик.")
        job["chosen_worker"]=worker_username
        await q.edit_message_text(f"Выбран исполнитель: {worker_username} ✅")
        # уведомим воркер-бот о выборе
        async with httpx.AsyncClient(timeout=10) as cli:
            await cli.post(f"{WORKER_API_URL}/api/chosen",
                           headers={"Authorization":f"Bearer {JOBS_API_TOKEN}"},
                           json={"job_id":job_id,"worker_username":worker_username,"customer_contact":USERS[job['user_id']]['username']})
        # и самого заказчика
        await tg_app.bot.send_message(chat_id=USERS[job["user_id"]]["chat_id"], text=f"Контакты исполнителя: {worker_username}")

# --- PTB wiring
conv_newjob=ConversationHandler(
    entry_points=[CommandHandler("newjob", newjob_start),
                 MessageHandler(filters.Regex(re.compile(rf"^{re.escape(BTN_NEWJOB)}$", re.IGNORECASE)), newjob_start)],
    states={
        S_CAT:[MessageHandler(filters.TEXT & ~filters.COMMAND, newjob_cat)],
        S_ADDR:[MessageHandler(filters.TEXT & ~filters.COMMAND, newjob_addr)],
        S_WHEN:[MessageHandler(filters.TEXT & ~filters.COMMAND, newjob_when)],
        S_PAY:[MessageHandler(filters.TEXT & ~filters.COMMAND, newjob_pay)],
        S_PHOTOS:[MessageHandler(filters.ALL & ~filters.COMMAND, newjob_photo)],
        S_CONFIRM:[MessageHandler(filters.TEXT & ~filters.COMMAND, newjob_confirm)],
    },
    fallbacks=[], allow_reentry=True
)

tg_app.add_handler(CommandHandler("start", start))
tg_app.add_handler(CommandHandler("help", help_cmd))
tg_app.add_handler(MessageHandler(filters.Regex(re.compile(rf"^{re.escape(BTN_HELP)}$", re.IGNORECASE)), help_cmd))
tg_app.add_handler(MessageHandler(filters.Regex(re.compile(rf"^{re.escape(BTN_MYJOBS)}$", re.IGNORECASE)), myjobs))
tg_app.add_handler(conv_newjob)
tg_app.add_handler(CallbackQueryHandler(cbq))

@app.on_event("startup")
async def on_startup():
    await tg_app.initialize()
    if BASE_URL:
        await tg_app.bot.set_webhook(url=f"{BASE_URL}/tg/webhook?secret={WEBHOOK_SECRET}")

@app.on_event("shutdown")
async def on_shutdown(): await tg_app.shutdown()

@app.post("/tg/webhook")
async def webhook(request:Request, secret:str):
    if secret!=WEBHOOK_SECRET: raise HTTPException(status_code=401, detail="bad secret")
    try: data=await request.json()
    except JSONDecodeError: return {"ok":True}
    if not isinstance(data,dict) or "update_id" not in data: return {"ok":True}
    await tg_app.process_update(Update.de_json(data, tg_app.bot)); return {"ok":True}

@app.get("/")
async def root():
    return {"ok":True}
