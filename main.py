import os
import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from groq import Groq

logging.basicConfig(level=logging.INFO)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

client = Groq(api_key=GROQ_API_KEY)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hola Mauri! Soy Charli-bot activo en Render 🤖")

async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_text = update.message.text
        completion = client.chat.completions.create(
            model="llama3-8b-8192",
            messages=[
                {"role": "system", "content": "Sos Charli, asistente del Taller Fiat en Santiago del Estero. Respondé corto y útil."},
                {"role": "user", "content": user_text}
            ],
            temperature=0.7,
            max_tokens=500
        )
        respuesta = completion.choices[0].message.content
        await update.message.reply_text(respuesta)
    except Exception as e:
        logging.error(f"Error: {e}")
        await update.message.reply_text(f"Error: {e}")

if __name__ == '__main__':
    if not TELEGRAM_TOKEN or not GROQ_API_KEY:
        raise ValueError("Faltan TELEGRAM_TOKEN o GROQ_API_KEY en variables de entorno")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))

    print("Charli-bot iniciando polling...")
    app.run_polling()
