import os
import threading
from datetime import datetime
import pytz
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

# --- FLASK SERVER (Keep-Alive per UptimeRobot) ---
app = Flask(__name__)

@app.route('/')
def home():
    return "GOAT Bot 24/7 is active!", 200

def run_flask():
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)

# --- CONFIGURAZIONE E MEMORIA BOT ---
TOKEN = "7703471186:AAHy6y8ZUQ07rKhIQRVtDptuhT5X7a5aF7I"

# Salva lo stato: { chat_id: { "date": "YYYY-MM-DD", "username": "nome" } }
goat_data = {}

def get_today_date():
    tz = pytz.timezone('Europe/Rome')
    return datetime.now(tz).strftime('%Y-%m-%d')

# --- LOGICA DEL GOAT DEL GIORNO ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text.lower()
    
    # Intercetta se la parola 'goat' è presente nel messaggio
    if "goat" in text:
        chat_id = update.effective_chat.id
        today = get_today_date()
        user = update.effective_user
        username = f"@{user.username}" if user.username else user.first_name

        # Controlla se per questo gruppo esiste già un GOAT oggi
        current_goat = goat_data.get(chat_id)

        if not current_goat or current_goat["date"] != today:
            # Primo utente della giornata!
            goat_data[chat_id] = {
                "date": today,
                "username": username
            }
            await update.message.reply_text(f"🐐 {username} sei il GOAT del giorno!")
        else:
            # GOAT già assegnato per oggi
            winner = current_goat["username"]
            await update.message.reply_text(f"⛔ Spiacente! Il GOAT del giorno è già stato preso da {winner}!")

def main():
    # Avvia Flask su un thread separato
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    # Avvia Telegram Bot
    application = ApplicationBuilder().token(TOKEN).build()
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    
    print("Bot avviato...")
    application.run_polling()

if __name__ == "__main__":
    main()
