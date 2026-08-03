import os
import json
from datetime import datetime
from threading import Thread
from flask import Flask
from telegram.ext import Application, MessageHandler, CommandHandler, filters
from google import genai

# --- CONFIGURAZIONE CHIAVI ---
TELEGRAM_TOKEN = "7703471186:AAHy6y8ZUQ07rKhIQRVtDptuhT5X7a5aF7I"
# Prende la chiave in modo sicuro da Render (Environment Variables):
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# Inizializza il client di Gemini
ai_client = genai.Client(api_key=GEMINI_API_KEY)

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

# --- MEMORIA DATI ---
DATA_FILE = "goat_data.json"
# Memoria temporanea degli ultimi messaggi del gruppo per il riassunto
chat_history = {} 

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {"chats": {}}

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=4)

# --- GESTIONE MESSAGGI (GOAT + MEMORIA IA) ---
async def handle_message(update, context):
    if not update.message or not update.message.text:
        return

    chat_id = str(update.effective_chat.id)
    text_content = update.message.text.strip()
    user = update.effective_user
    username_mention = f"@{user.username}" if user.username else user.first_name

    # 1. SALVA IL MESSAGGIO PER IL RIASSUNTO (Tieni solo gli ultimi 50)
    if chat_id not in chat_history:
        chat_history[chat_id] = []
    
    chat_history[chat_id].append(f"{user.first_name}: {text_content}")
    if len(chat_history[chat_id]) > 50:
        chat_history[chat_id].pop(0)

    # 2. LOGICA GOAT
    if text_content.lower() == "goat":
        today = str(datetime.now().date())
        data = load_data()
        
        if chat_id not in data["chats"]:
            data["chats"][chat_id] = {"last_date": None, "today_winner": None, "leaderboard": {}}

        chat_info = data["chats"][chat_id]

        if chat_info["last_date"] != today:
            chat_info["last_date"] = today
            chat_info["today_winner"] = username_mention
            
            current_score = chat_info["leaderboard"].get(username_mention, 0)
            chat_info["leaderboard"][username_mention] = current_score + 1
            save_data(data)
            
            total_wins = chat_info["leaderboard"][username_mention]
            await update.message.reply_text(f"Sei il GOAT del giorno 🐐!\n🏆 Vittorie totali: {total_wins}")
        else:
            current_goat = chat_info["today_winner"]
            await update.message.reply_text(f"Già {current_goat} è il Goat del giorno 🐐")

# --- COMANDO /CLASSIFICA ---
async def show_leaderboard(update, context):
    chat_id = str(update.effective_chat.id)
    data = load_data()

    if chat_id not in data["chats"] or not data["chats"][chat_id]["leaderboard"]:
        await update.message.reply_text("📊 La classifica è vuota! Scrivete 'goat' per iniziare.")
        return

    leaderboard = data["chats"][chat_id]["leaderboard"]
    sorted_board = sorted(leaderboard.items(), key=lambda x: x[1], reverse=True)

    text = "🏆 **CLASSIFICA GOAT DEL GRUPPO** 🏆\n\n"
    medals = ["🥇", "🥈", "🥉"]

    for index, (user, wins) in enumerate(sorted_board):
        icon = medals[index] if index < 3 else "👤"
        text += f"{icon} {user}: **{wins}** {'vittoria' if wins == 1 else 'vittorie'}\n"

    await update.message.reply_text(text, parse_mode="Markdown")

# --- COMANDO /RIASSUNTO (CON DEBUG ERRORE) ---
async def make_summary(update, context):
    chat_id = str(update.effective_chat.id)
    
    if chat_id not in chat_history or len(chat_history[chat_id]) < 3:
        await update.message.reply_text("🤖 Ci sono troppi pochi messaggi recenti per fare un riassunto! Parlate un altro po'.")
        return

    status_msg = await update.message.reply_text("🤖 L'IA sta leggendo la chat...")

    try:
        conversation_text = "\n".join(chat_history[chat_id])
        prompt = (
            "Sei l'assistente del gruppo Telegram. Fai un riassunto breve e divertente in italiano degli ultimi messaggi della chat:\n\n"
            f"{conversation_text}"
        )

        # Modello standard Gemini
        response = ai_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
        )
        
        summary_text = f"📝 **RIASSUNTO DELLA CHAT** 🤖\n\n{response.text}"
        await status_msg.edit_text(summary_text, parse_mode="Markdown")

    except Exception as e:
        print(f"ERRORE GEMINI: {e}")
        # Se c'è un errore, il bot ti scriverà in chat l'errore esatto!
        await status_msg.edit_text(f"❌ Errore IA: {str(e)[:150]}")

def main():
    keep_alive()

    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CommandHandler("classifica", show_leaderboard))
    application.add_handler(CommandHandler("riassunto", make_summary))
    
    print("Bot avviato...")
    application.run_polling()

if __name__ == '__main__':
    main()
