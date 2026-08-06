import os
import json
import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from threading import Thread
from flask import Flask
from telegram.ext import Application, MessageHandler, CommandHandler, filters
import google.generativeai as genai

# --- LOGGING ---
logging.basicConfig(level=logging.INFO)

# --- VARIABILI D'AMBIENTE ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
BACKUP_CHAT_ID = os.environ.get("BACKUP_CHAT_ID")  # Es. "-100xxxxxxxxxx"

# Inizializzazione globale di Gemini con la chiave di Render
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

# --- KEEP ALIVE SERVER (Flask) ---
app = Flask('')

@app.route('/')
def home():
    return "GOAT Bot is Alive!"

def run_flask():
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_flask, daemon=True)
    t.start()

# --- MEMORIA E BACKUP DATI ---
DATA_FILE = "goat_data.json"
chat_history = {} 

def get_italian_now():
    """Restituisce il datetime corrente con fuso orario italiano"""
    return datetime.now(ZoneInfo("Europe/Rome"))

def get_italian_date():
    """Restituisce la data odierna esatta in Italia (YYYY-MM-DD)"""
    return str(get_italian_now().date())

def load_local_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"Errore lettura file locale: {e}")
    return {"chats": {}}

def save_local_data(data):
    try:
        with open(DATA_FILE, "w") as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        logging.error(f"Errore salvataggio dati: {e}")

async def backup_to_telegram(context):
    """Invia il file JSON corrente al canale/gruppo di backup"""
    if not BACKUP_CHAT_ID:
        return
    try:
        data = load_local_data()
        save_local_data(data)
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, "rb") as f:
                await context.bot.send_document(
                    chat_id=BACKUP_CHAT_ID,
                    document=f,
                    caption=f"📦 Backup Automatico - {get_italian_now().strftime('%d/%m/%Y %H:%M:%S')}"
                )
    except Exception as e:
        logging.error(f"Errore durante il backup su Telegram: {e}")

# --- INIT E STRUTTURA CHAT ---
def init_chat_structure(data, chat_id):
    if "chats" not in data:
        data["chats"] = {}
    if chat_id not in data["chats"]:
        data["chats"][chat_id] = {
            "last_date": None,
            "today_winner": None,
            "leaderboard": {},
            "msg_weekly": {},
            "msg_ever": {}
        }
    else:
        chat_info = data["chats"][chat_id]
        if "msg_weekly" not in chat_info:
            chat_info["msg_weekly"] = {}
        if "msg_ever" not in chat_info:
            chat_info["msg_ever"] = {}

# --- GESTIONE MESSAGGI ---
async def handle_message(update, context):
    if not update.message or not update.message.from_user:
        return

    user = update.message.from_user
    chat_id = str(update.effective_chat.id)

    # UTILS: Stampa nei Log di Render l'ID della chat
    print(f"--- ID CHAT RILEVATO: {chat_id} ---", flush=True)

    # IGNORA TUTTI I BOT DAL CONTEGGIO MESSAGGI
    if user.is_bot:
        return

    text_content = (update.message.text or update.message.caption or "").strip()
    username_mention = f"@{user.username}" if user.username else user.first_name

    # 1. Salva messaggio per il riassunto (fino a 500)
    if text_content:
        if chat_id not in chat_history:
            chat_history[chat_id] = []
        chat_history[chat_id].append(f"{user.first_name}: {text_content}")
        if len(chat_history[chat_id]) > 500:
            chat_history[chat_id].pop(0)

    # 2. Registra conteggio messaggi (Settimanale & Ever)
    data = load_local_data()
    init_chat_structure(data, chat_id)
    
    chat_info = data["chats"][chat_id]
    chat_info["msg_weekly"][username_mention] = chat_info["msg_weekly"].get(username_mention, 0) + 1
    chat_info["msg_ever"][username_mention] = chat_info["msg_ever"].get(username_mention, 0) + 1
    
    save_local_data(data)

    # 3. Logica GOAT
    if text_content.lower() == "goat":
        today = get_italian_date()

        if chat_info["last_date"] != today:
            chat_info["last_date"] = today
            chat_info["today_winner"] = username_mention
            
            current_score = chat_info["leaderboard"].get(username_mention, 0)
            chat_info["leaderboard"][username_mention] = current_score + 1
            save_local_data(data)
            
            # Esegue il backup su Telegram
            await backup_to_telegram(context)

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

# --- COMANDO LISTA COMANDI ---
async def show_commands(update, context):
    text = (
        "🤖 <b>LISTA COMANDI DI GOAT BOT</b> 🐐\n\n"
        "👑 <b>/goatboard</b> - Mostra la classifica storica delle vittorie GOAT giornaliere.\n"
        "📊 <b>/classificasettimana</b> - Mostra la classifica dei messaggi scritti questa settimana.\n"
        "👑 <b>/classificaever</b> - Mostra la classifica generale dei messaggi di sempre.\n"
        "📝 <b>/riassunto</b> - Genera un riassunto breve degli ultimi messaggi tramite IA.\n"
        "📜 <b>/riassuntolungo</b> - Genera un riassunto esteso e dettagliato tramite IA.\n"
        "🎙️ <b>/trans</b> - Rispondi a un messaggio vocale per ottenerne la trascrizione testuale.\n"
        "ℹ️ <b>/goatcomm</b> - Mostra questo pannello con tutti i comandi.\n\n"
        "💡 <i>Curiosità: Scrivi semplicemente <b>goat</b> in chat per eleggere il GOAT del giorno!</i>"
    )
    await update.message.reply_text(text, parse_mode='HTML')

# --- CLASSIFICHE MESSAGGI ---
def generate_ranking_text(title, ranking_dict):
    if not ranking_dict:
        return f"{title}\n\n<i>Nessun messaggio registrato per ora.</i>"
    
    sorted_board = sorted(ranking_dict.items(), key=lambda x: x[1], reverse=True)
    text = f"{title}\n\n"
    medals = ["🥇", "🥈", "🥉"]

    for index, (user, count) in enumerate(sorted_board):
        icon = medals[index] if index < 3 else "💬"
        unit = "messaggio" if count == 1 else "messaggi"
        text += f"{icon} <b>{user}</b>: {count} {unit}\n"
    
    return text

async def show_weekly_ranking(update, context):
    chat_id = str(update.effective_chat.id)
    data = load_local_data()
    init_chat_structure(data, chat_id)
    
    text = generate_ranking_text("📊 <b>CLASSIFICA SETTIMANALE MESSAGGI</b> 📊", data["chats"][chat_id]["msg_weekly"])
    await update.message.reply_text(text, parse_mode='HTML')

async def show_ever_ranking(update, context):
    chat_id = str(update.effective_chat.id)
    data = load_local_data()
    init_chat_structure(data, chat_id)
    
    text = generate_ranking_text("👑 <b>CLASSIFICA GENERALE MESSAGGI (EVER)</b> 👑", data["chats"][chat_id]["msg_ever"])
    await update.message.reply_text(text, parse_mode='HTML')

async def show_goatboard(update, context):
    chat_id = str(update.effective_chat.id)
    data = load_local_data()
    init_chat_structure(data, chat_id)

    leaderboard = data["chats"][chat_id]["leaderboard"]
    if not leaderboard:
        await update.message.reply_text("📊 La classifica GOAT è vuota! Scrivete 'goat' per iniziare.", parse_mode='HTML')
        return

    sorted_board = sorted(leaderboard.items(), key=lambda x: x[1], reverse=True)
    text = "🏆 <b>CLASSIFICA GOAT DEL GRUPPO</b> 🏆\n\n"
    medals = ["🥇", "🥈", "🥉"]

    for index, (user, wins) in enumerate(sorted_board):
        icon = medals[index] if index < 3 else "👤"
        unit = "vittoria" if wins == 1 else "vittorie"
        text += f"{icon} <b>{user}</b>: {wins} {unit}\n"

    await update.message.reply_text(text, parse_mode='HTML')

# --- AUTONOMOUS TASK: RESET E PUBBLICAZIONE DOMENICA ALLE 15:00 ---
async def weekly_auto_reset_task(app):
    while True:
        now = get_italian_now()
        if now.weekday() == 6 and now.hour == 15 and now.minute == 0:
            data = load_local_data()
            if "chats" in data:
                for chat_id, chat_info in data["chats"].items():
                    try:
                        text = generate_ranking_text(
                            "🏆 <b>CLASSIFICA FINALE DELLA SETTIMANA</b> 🏆\n"
                            "<i>I contatori della settimana sono stati appena azzerati!</i>",
                            chat_info.get("msg_weekly", {})
                        )
                        await app.bot.send_message(chat_id=int(chat_id), text=text, parse_mode='HTML')
                        chat_info["msg_weekly"] = {}
                    except Exception as e:
                        logging.error(f"Errore invio classifica automatica alla chat {chat_id}: {e}")
                
                save_local_data(data)
                class MockContext:
                    bot = app.bot
                await backup_to_telegram(MockContext())

            await asyncio.sleep(61)
        else:
            await asyncio.sleep(30)

# --- COMANDI RIASSUNTO GEMINI ---
async def make_summary(update, context):
    await generate_summary_response(update, limit=75, title="RIASSUNTO BREVE")

async def make_long_summary(update, context):
    await generate_summary_response(update, limit=500, title="RIASSUNTO ESTESO")

async def generate_summary_response(update, limit, title):
    chat_id = str(update.effective_chat.id)
    if chat_id not in chat_history or len(chat_history[chat_id]) < 3:
        await update.message.reply_text("🤖 Ci sono troppi pochi messaggi recenti per fare un riassunto!")
        return

    status_msg = await update.message.reply_text(f"🤖 L'IA sta leggendo gli ultimi messaggi per il {title.lower()}...")

    try:
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

# --- COMANDO TRASCRIZIONE AUDIO ---
async def transcribe_audio(update, context):
    reply_msg = update.message.reply_to_message
    if not reply_msg or not (reply_msg.voice or reply_msg.audio or reply_msg.video_note):
        await update.message.reply_text("⚠️ Rispondi a un messaggio vocale con il comando `/trans` per trascriverlo!", parse_mode='Markdown')
        return

    status_msg = await update.message.reply_text("🎧 <i>Trascrizione del vocale in corso...</i>", parse_mode='HTML')

    file_path = "temp_audio.ogg"
    audio_file = None

    try:
        if not GEMINI_API_KEY:
            await status_msg.edit_text("❌ Errore: GEMINI_API_KEY non configurata su Render!")
            return

        audio_obj = reply_msg.voice or reply_msg.audio or reply_msg.video_note
        telegram_file = await context.bot.get_file(audio_obj.file_id)
        await telegram_file.download_to_drive(file_path)

        # Carica il file utilizzando la configurazione globale
        audio_file = genai.upload_file(path=file_path)

        # Attende l'elaborazione del file audio se in stato PROCESSING
        while audio_file.state.name == "PROCESSING":
            await asyncio.sleep(1)
            audio_file = genai.get_file(audio_file.name)

        if audio_file.state.name == "FAILED":
            await status_msg.edit_text("❌ Impossibile elaborare il file audio su Gemini.")
            return

        prompt = (
            "Trascrivi fedelmente questo audio in italiano. "
            "Restituisci solo ed esclusivamente il testo trascritto, senza commenti aggiuntivi. "
            "NON usare sintassi Markdown (no asterischi, cancelletti o trattini bassi)."
        )

        model = genai.GenerativeModel('gemini-3.1-flash-lite')
        response = model.generate_content([audio_file, prompt])

        user_name = reply_msg.from_user.first_name if reply_msg.from_user else "Utente"
        transcript_text = f"🎙️ <b>TRASCRIZIONE VOCALE DI {user_name.upper()}</b> 📝\n\n{response.text.strip()}"

        await status_msg.edit_text(transcript_text, parse_mode='HTML')

    except Exception as e:
        logging.error(f"Errore trascrizione audio: {e}")
        await status_msg.edit_text(f"❌ Impossibile trascrivere l'audio: {str(e)}")
    finally:
        # Pulizia file temporanei locali e remoti
        if audio_file:
            try:
                genai.delete_file(audio_file.name)
            except Exception:
                pass
        if os.path.exists(file_path):
            os.remove(file_path)

# --- INIZIALIZZAZIONE ASINCRONA TASK ---
async def post_init(application: Application):
    """Avvia i task in background dopo l'inizializzazione corretta dell'Event Loop"""
    asyncio.create_task(weekly_auto_reset_task(application))

def main():
    keep_alive()

    application = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    
    # Registrazione Comandi
    application.add_handler(CommandHandler("goatboard", show_goatboard))
    application.add_handler(CommandHandler("classificasettimana", show_weekly_ranking))
    application.add_handler(CommandHandler("classificaever", show_ever_ranking))
    application.add_handler(CommandHandler("riassunto", make_summary))
    application.add_handler(CommandHandler("riassuntolungo", make_long_summary))
    application.add_handler(CommandHandler("trans", transcribe_audio))
    application.add_handler(CommandHandler("goatcomm", show_commands))
    
    # Message Handlers
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))

    print("GOAT Bot attivo!", flush=True)
    application.run_polling()

if __name__ == '__main__':
    main()
