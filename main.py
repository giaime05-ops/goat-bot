import os
import json
from datetime import datetime
from zoneinfo import ZoneInfo
from threading import Thread
from flask import Flask
from telegram.ext import Application, MessageHandler, CommandHandler, filters
import google.generativeai as genai

# --- CONFIGURAZIONE CHIAVI ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

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
chat_history = {} 

def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {"chats": {}}
    return {"chats": {}}

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=4)

def get_italian_date():
    """Restituisce la data odierna esatta in Italia (Europe/Rome)"""
    return str(datetime.now(ZoneInfo("Europe/Rome")).date())

# --- GESTIONE MESSAGGI ---
async def handle_message(update, context):
    if not update.message or not update.message.text:
        return

    chat_id = str(update.effective_chat.id)
    text_content = update.message.text.strip()
    user = update.effective_user
    username_mention = f"@{user.username}" if user.username else user.first_name

    # 1. Salva messaggio per il riassunto (mantiene fino a 500 messaggi)
    if chat_id not in chat_history:
        chat_history[chat_id] = []
    
    chat_history[chat_id].append(f"{user.first_name}: {text_content}")
    
    # Limite massimo portato a 500 messaggi
    if len(chat_history[chat_id]) > 500:
        chat_history[chat_id].pop(0)

    # 2. Logica GOAT
    if text_content.lower() == "goat":
        today = get_italian_date()
        data = load_data()
        
        if "chats" not in data:
            data["chats"] = {}

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
            await update.message.reply_text(
                f"Sei il GOAT del giorno 🐐!\n🏆 Vittorie totali: <b>{total_wins}</b>",
                parse_mode='HTML'
            )
        else:
            current_goat = chat_info["today_winner"]
            await update.message.reply_text(
                f"Già <b>{current_goat}</b> è il Goat del giorno 🐐",
                parse_mode='HTML'
            )

# --- COMANDO /GOATBOARD ---
async def show_goatboard(update, context):
    chat_id = str(update.effective_chat.id)
    data = load_data()

    if "chats" not in data or chat_id not in data["chats"] or not data["chats"][chat_id]["leaderboard"]:
        await update.message.reply_text(
            "📊 La classifica è vuota! Scrivete 'goat' per iniziare.",
            parse_mode='HTML'
        )
        return

    leaderboard = data["chats"][chat_id]["leaderboard"]
    sorted_board = sorted(leaderboard.items(), key=lambda x: x[1], reverse=True)

    text = "🏆 <b>CLASSIFICA GOAT DEL GRUPPO</b> 🏆\n\n"
    medals = ["🥇", "🥈", "🥉"]

    for index, (user, wins) in enumerate(sorted_board):
        icon = medals[index] if index < 3 else "👤"
        unit = "vittoria" if wins == 1 else "vittorie"
        text += f"{icon} <b>{user}</b>: {wins} {unit}\n"

    await update.message.reply_text(text, parse_mode='HTML')

# --- COMANDO /RIASSUNTO (Ultimi 75 messaggi) ---
async def make_summary(update, context):
    await generate_summary_response(update, limit=75, title="RIASSUNTO BREVE")

# --- COMANDO /RIASSUNTOLUNGO (Ultimi 500 messaggi) ---
async def make_long_summary(update, context):
    await generate_summary_response(update, limit=500, title="RIASSUNTO ESTESO")

# --- FUNZIONE GENERICA RIASSUNTI ---
async def generate_summary_response(update, limit, title):
    chat_id = str(update.effective_chat.id)
    
    if chat_id not in chat_history or len(chat_history[chat_id]) < 3:
        await update.message.reply_text("🤖 Ci sono troppi pochi messaggi recenti per fare un riassunto! Parlate un altro po'.")
        return

    status_msg = await update.message.reply_text(f"🤖 L'IA sta leggendo gli ultimi messaggi per il {title.lower()}...")

    try:
        # Prende solo gli ultimi N messaggi in base al limite
        recent_messages = chat_history[chat_id][-limit:]
        conversation_text = "\n".join(recent_messages)
        
        prompt = (
            f"Sei l'assistente ufficiale di un gruppo Telegram. "
            f"Fai un {title.lower()} divertente e ben organizzato in italiano di questa conversazione.\n\n"
            "REGOLE TASSATIVE DI FORMATTAZIONE:\n"
            "- NON usare MAI la sintassi Markdown (niente asterischi **, cancelletti #, trattini bassi _).\n"
            "- Usa ESCLUSIVAMENTE i tag HTML <b> e </b> per mettere in grassetto concetti o nomi importanti.\n"
            "- Usa le emoji per organizzare il testo in modo elegante.\n\n"
            f"Ecco i messaggi:\n{conversation_text}"
        )

        model = genai.GenerativeModel('gemini-3.1-flash-lite')
        response = model.generate_content(prompt)
        
        summary_text = f"📝 <b>{title} DELLA CHAT</b> 🤖\n\n{response.text}"
        await status_msg.edit_text(summary_text, parse_mode='HTML')

    except Exception as e:
        await status_msg.edit_text(f"❌ Errore riscontrato: {str(e)}")

def main():
    keep_alive()

    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Registrazione comandi
    application.add_handler(CommandHandler("goatboard", show_goatboard))
    application.add_handler(CommandHandler("riassunto", make_summary))
    application.add_handler(CommandHandler("riassuntolungo", make_long_summary))
    
    # Handlers per messaggi di testo generici
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("Bot avviato...")
    application.run_polling()

if __name__ == '__main__':
    main()
