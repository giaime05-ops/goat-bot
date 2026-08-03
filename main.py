import os
import json
from datetime import datetime
from threading import Thread
from flask import Flask
from telegram.ext import Application, MessageHandler, CommandHandler, filters

# --- KEEP ALIVE SERVER ---
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

# --- GESTIONE DATI PERMANENTI (JSON) ---
DATA_FILE = "goat_data.json"

def load_data():
    """Carica i dati dal file JSON"""
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {"chats": {}}

def save_data(data):
    """Salva i dati nel file JSON"""
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=4)

# --- LOGICA DEL BOT TELEGRAM ---
async def handle_message(update, context):
    if not update.message or not update.message.text:
        return

    # Controlla se il messaggio è ESATTAMENTE "goat"
    if update.message.text.strip().lower() == "goat":
        chat_id = str(update.effective_chat.id)
        user = update.effective_user
        
        # Username o Nome
        username_mention = f"@{user.username}" if user.username else user.first_name
        today = str(datetime.now().date())

        data = load_data()
        
        # Inizializza i dati per questo gruppo se non esistono
        if chat_id not in data["chats"]:
            data["chats"][chat_id] = {
                "last_date": None,
                "today_winner": None,
                "leaderboard": {}
            }

        chat_info = data["chats"][chat_id]

        # Controllo reset giornaliero
        if chat_info["last_date"] != today:
            # Nuovo vincitore del giorno!
            chat_info["last_date"] = today
            chat_info["today_winner"] = username_mention
            
            # Aggiorna la classifica (+1 vittoria)
            current_score = chat_info["leaderboard"].get(username_mention, 0)
            chat_info["leaderboard"][username_mention] = current_score + 1
            
            save_data(data)
            
            total_wins = chat_info["leaderboard"][username_mention]
            await update.message.reply_text(
                f"Sei il GOAT del giorno 🐐!\n"
                f"🏆 Vittorie totali: {total_wins}"
            )
        else:
            # Titolo già preso oggi
            current_goat = chat_info["today_winner"]
            await update.message.reply_text(f"Già {current_goat} è il Goat del giorno 🐐")

async def show_leaderboard(update, context):
    """Comando /classifica"""
    chat_id = str(update.effective_chat.id)
    data = load_data()

    if chat_id not in data["chats"] or not data["chats"][chat_id]["leaderboard"]:
        await update.message.reply_text("📊 La classifica è ancora vuota! Scrivete 'goat' per iniziare.")
        return

    leaderboard = data["chats"][chat_id]["leaderboard"]
    
    # Ordina i giocatori per numero di vittorie (dal più alto al più basso)
    sorted_board = sorted(leaderboard.items(), key=lambda x: x[1], reverse=True)

    text = "🏆 **CLASSIFICA GOAT DEL GRUPPO** 🏆\n\n"
    medals = ["🥇", "🥈", "🥉"]

    for index, (user, wins) in enumerate(sorted_board):
        icon = medals[index] if index < 3 else "👤"
        text += f"{icon} {user}: **{wins}** {'vittoria' if wins == 1 else 'vittorie'}\n"

    await update.message.reply_text(text, parse_mode="Markdown")

def main():
    keep_alive()

    token = "7703471186:AAHy6y8ZUQ07rKhIQRVtDptuhT5X7a5aF7I"

    application = Application.builder().token(token).build()
    
    # Gestore per il messaggio "goat"
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Gestore per il comando /classifica
    application.add_handler(CommandHandler("classifica", show_leaderboard))
    
    print("Bot avviato...")
    application.run_polling()

if __name__ == '__main__':
    main()
