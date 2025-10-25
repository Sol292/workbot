from pydantic_settings import BaseSettings, SettingsConfigDict
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, ContextTypes
)

class Settings(BaseSettings):
    BOT_TOKEN: str
    # разрешаем лишние переменные из .env и укажем сам .env
    model_config = SettingsConfigDict(env_file=".env", extra="allow")

settings = Settings()
app = ApplicationBuilder().token(settings.BOT_TOKEN).build()

async def cmd_start(update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Это локальный режим (polling).\n/newjob — создать задачу"
    )

async def cmd_newjob(update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Опиши задачу текстом — пока тестируем, что всё живо.")

app.add_handler(CommandHandler("start", cmd_start))
app.add_handler(CommandHandler("newjob", cmd_newjob))

if __name__ == "__main__":
    print("Bot polling started. Ctrl+C — выход.")
    app.run_polling()