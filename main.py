import os
from datetime import datetime
from threading import Thread
from flask import Flask
from telegram.ext import Application, MessageHandler, filters

# --- KEEP ALIVE SERVER (Per UptimeRobot) ---
app = Flask('')

@app.route('/')
def home():
    return "GOAT Bot is Alive!"

def run_flask():
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_flask)
    t.start()

# --- LOGICA DEL BOT TELEGRAM ---
goat_data = {}

async def handle_message(update, context):
    if not update.message or not update.message.text:
        return

    # Controlla che il messaggio sia ESATTAMENTE "goat" (ignorando spazi e maiuscole/minuscole)
    if update.message.text.strip().lower() == "goat":
        chat_id = update.effective_chat.id
        user = update.effective_user
        
        # Genera la menzione (@username oppure Nome se non ha un username)
        username_mention = f"@{user.username}" if user.username else user.first_name
        
        today = datetime.now().date()
        chat_data = goat_data.get(chat_id, {"date": None, "winner": None})

        # Controllo reset giornaliero
        if chat_data["date"] != today:
            # Primo della giornata
            goat_data[chat_id] = {"date": today, "winner": username_mention}
            await update.message.reply_text("Sei il GOAT del giorno 🐐")
        else:
            # Titolo già preso oggi
            current_goat = chat_data["winner"]
            await update.message.reply_text(f"Già {current_goat} è il Goat del giorno 🐐")

def main():
    # Avvia il server Flask in background per il keep-alive
    keep_alive()

    # Token di BotFather
    token = "7703471186:AAHy6y8ZUQ07rKhIQRVtDptuhT5X7a5aF7I"

    # Avvia il bot Telegram
    application = Application.builder().token(token).build()
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("Bot avviato...")
    application.run_polling()

if __name__ == '__main__':
    main()
