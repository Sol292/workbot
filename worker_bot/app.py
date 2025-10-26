# WORKER BOT
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
JOBS_API_TOKEN  = os.getenv("JOBS_API_TOKEN")
WORKER_API_URL  = os.getenv("WORKER_API_URL")  # может быть None
CUSTOMER_API_URL=os.getenv("CUSTOMER_API_URL")
CUSTOMER_BOT_USERNAME=os.environ.get("CUSTOMER_BOT_USERNAME")

# Жёстко требуем только то, без чего сервер жить не может:
required = {
    "BOT_TOKEN": BOT_TOKEN,
    "WEBHOOK_SECRET": WEBHOOK_SECRET,
    "BASE_URL": BASE_URL,
}
missing = [k for k, v in required.items() if not v]
if missing:
    raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")

# А с WORKER_API_URL — мягко: если нет, просто отключаем интеграцию
if not WORKER_API_URL:
    print("[WARN] WORKER_API_URL is not set — worker-интеграция будет отключена.")

BTN_WORKMODE="Профиль исполнителя"; BTN_GO_ON="Доступен"; BTN_GO_OFF="Недоступен"; BTN_HELP="Помощь"; BTN_CANCEL="Отмена"; BTN_DONE="Готово"
CATEGORIES=["Разнорабочие (общие)","Погрузка/разгрузка","Демонтаж","КлининГ","Курьер/доставка","Подсобник на стройку","Сборка мебели","Малярные работы","Электромонтаж (простые)","Сантехника (простые)","Вынос мусора","Уборка после ремонта"]
RADIUS=[2,5,10,25]

app=FastAPI()
tg_app:Application=ApplicationBuilder().token(BOT_TOKEN).build()

USERS:dict[int,dict]={}      # {user_id:{chat_id,username}}
WORKERS:dict[int,dict]={}    # {user_id:{city,cats:set,radius,available}}

def kb_main(): return ReplyKeyboardMarkup([[BTN_WORKMODE, BTN_GO_ON, BTN_GO_OFF],[BTN_HELP]], resize_keyboard=True)
def kb_cats(exclude:set[str]|None=None):
    ex=exclude or set(); left=[c for c in CATEGORIES if c not in ex]
    rows,row=[],[]
    for i,n in enumerate(left,1):
        row.append(n); 
        if i%2==0: rows.append(row); row=[]
    if row: rows.append(row)
    rows.append([BTN_DONE, BTN_CANCEL]); return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def ensure_user(u:Update):
    USERS[u.effective_user.id]={"chat_id":u.effective_chat.id,"username":("@" + u.effective_user.username) if u.effective_user.username else f"id{u.effective_user.id}"}

async def start(u:Update,c):
    ensure_user(u)
    msg="Привет! Это бот исполнителя.\nНастрой профиль и включи доступность, чтобы получать новые задачи."
    if CUSTOMER_BOT_USERNAME: msg+=f"\nСоздавать задачи — в {CUSTOMER_BOT_USERNAME}"
    await u.message.reply_text(msg, reply_markup=kb_main())

async def help_cmd(u:Update,c): await u.message.reply_text("Используй кнопки внизу.", reply_markup=kb_main())

# --- онбординг
(W_CITY,W_CATS,W_RADIUS)=range(10,13)

async def workmode(u:Update,c):
    ensure_user(u); c.user_data.clear(); c.user_data["cats"]=set()
    await u.message.reply_text("Город одним словом:", reply_markup=ReplyKeyboardRemove()); return W_CITY

async def w_city(u:Update,c):
    city=(u.message.text or "").strip()
    if len(city)<2: await u.message.reply_text("Коротко: 'Москва', 'Тверь'."); return W_CITY
    c.user_data["city"]=city
    await u.message.reply_text("Выбирай категории. Когда закончишь — 'Готово'.", reply_markup=kb_cats(c.user_data["cats"])); return W_CATS

async def w_cats(u:Update,c):
    t=(u.message.text or "").strip()
    if t.lower()==BTN_CANCEL.lower(): await u.message.reply_text("Отменено.", reply_markup=kb_main()); return ConversationHandler.END
    if t.lower()==BTN_DONE.lower():
        if not c.user_data["cats"]: await u.message.reply_text("Выбери хотя бы одну категорию.", reply_markup=kb_cats()); return W_CATS
        rows=[[f"{r} км" for r in RADIUS[:2]],[f"{r} км" for r in RADIUS[2:]]]
        await u.message.reply_text("Радиус:", reply_markup=ReplyKeyboardMarkup(rows, resize_keyboard=True)); return W_RADIUS
    if t not in CATEGORIES: await u.message.reply_text("Выбирай кнопками.", reply_markup=kb_cats(c.user_data["cats"])); return W_CATS
    c.user_data["cats"].add(t)
    await u.message.reply_text("Добавлено: " + ", ".join(c.user_data["cats"]), reply_markup=kb_cats(c.user_data["cats"])); return W_CATS

async def w_radius(u:Update,c):
    t=(u.message.text or "").replace(" км","").strip()
    if not t.isdigit() or int(t) not in RADIUS: await u.message.reply_text("Выбери из кнопок радиус."); return W_RADIUS
    uid=u.effective_user.id
    WORKERS[uid]={"city":c.user_data["city"],"cats":set(c.user_data["cats"]),"radius":int(t),"available":True}
    c.user_data.clear()
    await u.message.reply_text(f"Готово ✅\nГород: {WORKERS[uid]['city']}\nКатегории: {', '.join(WORKERS[uid]['cats'])}\nРадиус: {WORKERS[uid]['radius']} км\nСтатус: Доступен",
                               reply_markup=kb_main())
    return ConversationHandler.END

async def go_on(u:Update,c):
    uid=u.effective_user.id
    if uid not in WORKERS: return await u.message.reply_text("Сначала настрой «Профиль исполнителя».", reply_markup=kb_main())
    WORKERS[uid]["available"]=True; await u.message.reply_text("Статус: Доступен ✅", reply_markup=kb_main())

async def go_off(u:Update,c):
    uid=u.effective_user.id
    if uid not in WORKERS: return await u.message.reply_text("Сначала настрой «Профиль исполнителя».", reply_markup=kb_main())
    WORKERS[uid]["available"]=False; await u.message.reply_text("Статус: Недоступен ⛔", reply_markup=kb_main())

# --- API: customer -> worker (рассылка и выбор)
@app.post("/api/push-job")
async def api_push_job(req:Request, authorization: str | None = Header(default=None)):
    if not (authorization and authorization.lower().startswith("bearer ") and authorization.split(" ",1)[1].strip()==JOBS_API_TOKEN):
        raise HTTPException(status_code=401, detail="bad api token")
    body=await req.json()
    job=body["job"]; customer_contact=body.get("customer_contact","")
    # матчинг: по доступности, категории и вхождению города в адрес (просто)
    cat=job["category"]; addr=job["address"].lower()
    sent=0
    for wid, w in WORKERS.items():
        if not w["available"]:  continue
        if cat not in w["cats"]: continue
        if w["city"].lower() not in addr: continue
        kb=InlineKeyboardMarkup([[InlineKeyboardButton("Откликнуться", callback_data=f"bid:{job['id']}:{customer_contact}")]])
        txt=f"Новая задача #{job['id']}\n{job['category']} • {job['when']}\n{job['address']}\nБюджет: {job['pay']}"
        try:
            await tg_app.bot.send_message(chat_id=USERS.get(wid,{}).get("chat_id", wid), text=txt, reply_markup=kb); sent+=1
        except Exception: pass
    return {"ok":True, "sent":sent}

@app.post("/api/chosen")
async def api_chosen(req:Request, authorization: str | None = Header(default=None)):
    if not (authorization and authorization.lower().startswith("bearer ") and authorization.split(" ",1)[1].strip()==JOBS_API_TOKEN):
        raise HTTPException(status_code=401, detail="bad api token")
    payload=await req.json()
    worker_username=str(payload["worker_username"]); customer_contact=str(payload["customer_contact"])
    # уведомим выбранного исполнителя (по username искать не будем — это демо)
    # В реале лучше передавать worker_id; здесь юзернейм нам прислали из customer-бота.
    for uid, info in USERS.items():
        if info.get("username")==worker_username:
            try:
                await tg_app.bot.send_message(chat_id=info["chat_id"], text=f"Тебя выбрали на задачу #{payload['job_id']}. Связь с заказчиком: {customer_contact}")
            except Exception: pass
    return {"ok":True}

# --- воркер жмёт «Откликнуться» → POST в customer /api/new-bid
async def cbq(update:Update, context:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer()
    if not q.data or not q.data.startswith("bid:"): return
    _, job_id_s, customer_contact = q.data.split(":")
    job_id=int(job_id_s); wid=update.effective_user.id
    worker_username=USERS.get(wid,{}).get("username",f"id{wid}")
    try:
        async with httpx.AsyncClient(timeout=10) as cli:
            await cli.post(f"{CUSTOMER_API_URL}/api/new-bid",
                           headers={"Authorization":f"Bearer {JOBS_API_TOKEN}"},
                           json={"job_id":job_id, "worker_username":worker_username})
        await q.edit_message_text("Отклик отправлен ✅")
    except Exception as e:
        await q.edit_message_text(f"Не удалось отправить отклик: {e}")

# --- PTB wiring
conv_worker=ConversationHandler(
    entry_points=[CommandHandler("workmode", workmode),
                 MessageHandler(filters.Regex(re.compile(rf"^{re.escape(BTN_WORKMODE)}$", re.IGNORECASE)), workmode)],
    states={10:[MessageHandler(filters.TEXT & ~filters.COMMAND, w_city)],
            11:[MessageHandler(filters.TEXT & ~filters.COMMAND, w_cats)],
            12:[MessageHandler(filters.TEXT & ~filters.COMMAND, w_radius)]},
    fallbacks=[], allow_reentry=True
)

tg_app.add_handler(CommandHandler("start", start))
tg_app.add_handler(CommandHandler("help", help_cmd))
tg_app.add_handler(MessageHandler(filters.Regex(re.compile(rf"^{re.escape(BTN_HELP)}$", re.IGNORECASE)), help_cmd))
tg_app.add_handler(MessageHandler(filters.Regex(re.compile(rf"^{re.escape(BTN_GO_ON)}$", re.IGNORECASE)), go_on))
tg_app.add_handler(MessageHandler(filters.Regex(re.compile(rf"^{re.escape(BTN_GO_OFF)}$", re.IGNORECASE)), go_off))
tg_app.add_handler(conv_worker)
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
