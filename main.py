import os
import json
import time
import signal
import asyncio
import logging
from datetime import datetime
from functools import wraps
from zoneinfo import ZoneInfo
from threading import Thread
from flask import Flask
from telegram.ext import Application, MessageHandler, CommandHandler, filters
import google.generativeai as genai

# --- LOGGING ---
logging.basicConfig(level=logging.INFO)

# --- VARIABILI D'AMBIENTE ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")  # Bot storico (GoatBot): gruppo storico
# Token del SECONDO bot, dedicato al gruppo grande (stallone). Se manca, il bot storico
# gestisce da solo entrambi i gruppi (modalità a bot singolo).
TELEGRAM_TOKEN_BIG = os.environ.get("TELEGRAM_TOKEN_BIG")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
BACKUP_CHAT_ID = os.environ.get("BACKUP_CHAT_ID")  # Es. "-100xxxxxxxxxx"

# --- ID DELLE CHAT GESTITE ---
# GRUPPO STORICO: parola "goat", conteggio messaggi, tutti i comandi.
HISTORIC_CHAT_ID = "-1001901647268"
# GRUPPO GRANDE: parola "stallone", nessun conteggio messaggi, solo /stalloneboard.
BIG_CHAT_ID = "-1001520340812"

# GRUPPO GRANDE: dopo che il titolo è stato assegnato, il messaggio "Già X è il Re Stallone"
# viene inviato al massimo UNA volta ogni tot secondi, così una raffica di "stallone"
# (es. a mezzanotte) non genera decine di risposte identiche.
ALREADY_REPLY_COOLDOWN = 20  # secondi
last_already_reply = {}      # chat_id -> istante dell'ultimo "Già X" inviato

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

def get_message_date(update):
    """
    Data italiana (YYYY-MM-DD) in cui l'utente ha SCRITTO il messaggio (orario di Telegram),
    non quella in cui il bot lo elabora: così un messaggio delle 23:59:59 resta del giorno prima
    anche se il bot lo processa con qualche secondo di ritardo (o dopo un riavvio).
    """
    msg_dt = getattr(update.message, "date", None)
    if msg_dt is None:
        return get_italian_date()
    return str(msg_dt.astimezone(ZoneInfo("Europe/Rome")).date())

# --- TASK IN BACKGROUND ---
# I lavori lenti (IA, backup) girano in background, così il bot resta subito libero
# di rispondere agli altri messaggi (es. "stallone" a mezzanotte).
background_tasks = set()

def spawn(coro):
    task = asyncio.create_task(coro)
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)
    return task

backup_lock = asyncio.Lock()  # un solo backup alla volta

# Bot usato per i backup. Con due bot è sempre il bot storico, così solo lui deve essere
# nel gruppo di backup (admin con permesso di fissare). Se None, si usa il bot del contesto.
BACKUP_BOT = None

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
    async with backup_lock:
        bot = BACKUP_BOT or context.bot
        try:
            data = load_local_data()
            save_local_data(data)
            if not os.path.exists(DATA_FILE):
                return

            # Ricorda il vecchio backup fissato (solo se è un file .json), per toglierlo dopo il nuovo pin.
            # Telegram NON sgancia da solo i pin precedenti: vanno tolti esplicitamente.
            old_pinned_id = None
            try:
                chat = await bot.get_chat(chat_id=BACKUP_CHAT_ID)
                pinned = getattr(chat, "pinned_message", None)
                if pinned and pinned.document and pinned.document.file_name \
                        and pinned.document.file_name.endswith(".json"):
                    old_pinned_id = pinned.message_id
            except Exception as e:
                logging.error(f"Impossibile leggere il messaggio fissato attuale: {e}")

            with open(DATA_FILE, "rb") as f:
                sent = await bot.send_document(
                    chat_id=BACKUP_CHAT_ID,
                    document=f,
                    caption=f"📦 Backup Automatico - {get_italian_now().strftime('%d/%m/%Y %H:%M:%S')}"
                )

            # Fissa in automatico l'ultimo backup (il ripristino all'avvio legge il messaggio fissato)
            # e toglie il pin al backup precedente. Serve che il bot sia admin con permesso di fissare.
            try:
                await bot.pin_chat_message(
                    chat_id=BACKUP_CHAT_ID,
                    message_id=sent.message_id,
                    disable_notification=True
                )
                if old_pinned_id and old_pinned_id != sent.message_id:
                    try:
                        await bot.unpin_chat_message(
                            chat_id=BACKUP_CHAT_ID,
                            message_id=old_pinned_id
                        )
                    except Exception as e:
                        logging.error(f"Impossibile togliere il pin al vecchio backup: {e}")
            except Exception as e:
                logging.error(
                    f"Backup inviato ma impossibile fissarlo (il bot è admin con permesso di fissare messaggi?): {e}"
                )
        except Exception as e:
            logging.error(f"Errore durante il backup su Telegram: {e}")

async def restore_from_telegram(app: Application):
    """Scarica l'ultimo file di backup inviato nella chat di backup di Telegram all'avvio"""
    if not BACKUP_CHAT_ID:
        return
    try:
        logging.info("Tentativo di ripristino dati da Telegram...")
        chat = await app.bot.get_chat(chat_id=BACKUP_CHAT_ID)

        # Cerca il file tra i messaggi fissati
        if hasattr(chat, 'pinned_message') and chat.pinned_message and chat.pinned_message.document:
            doc = chat.pinned_message.document
            if doc.file_name and doc.file_name.endswith('.json'):
                file = await app.bot.get_file(doc.file_id)
                await file.download_to_drive(DATA_FILE)
                logging.info("Dati ripristinati con successo dal messaggio fissato!")
                return

        logging.info("Nessun file trovato nei messaggi fissati della chat di backup.")
    except Exception as e:
        logging.error(f"Errore durante il ripristino da Telegram: {e}")

# --- INIT E STRUTTURA CHAT ---
def init_chat_structure(data, chat_id):
    """
    Crea/completa la struttura dati della chat in base al suo tipo.
    Compatibile con i vecchi goat_data.json: aggiunge solo i campi mancanti,
    senza toccare né cancellare quelli già presenti.
    """
    if "chats" not in data:
        data["chats"] = {}
    if chat_id not in data["chats"]:
        data["chats"][chat_id] = {}

    chat_info = data["chats"][chat_id]

    if chat_id == HISTORIC_CHAT_ID:
        # Logica GOAT + conteggio messaggi
        chat_info.setdefault("last_date", None)
        chat_info.setdefault("today_winner", None)
        chat_info.setdefault("leaderboard", {})
        chat_info.setdefault("msg_weekly", {})
        chat_info.setdefault("msg_ever", {})
    elif chat_id == BIG_CHAT_ID:
        # Logica STALLONE (nessun conteggio messaggi)
        chat_info.setdefault("stallone_last_date", None)
        chat_info.setdefault("stallone_winner", None)
        chat_info.setdefault("stallone_leaderboard", {})

# --- FILTRO CHAT PER I COMANDI ---
def only_in(allowed_chat_id):
    """
    Esegue il comando solo nella chat indicata.
    In qualsiasi altra chat il comando viene IGNORATO in silenzio assoluto
    (nessuna risposta, nessun messaggio di errore).
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(update, context, *args, **kwargs):
            chat = update.effective_chat
            if chat is None or str(chat.id) != allowed_chat_id:
                return
            return await func(update, context, *args, **kwargs)
        return wrapper
    return decorator

# --- GESTIONE MESSAGGI (SMISTAMENTO PER CHAT) ---
async def handle_message(update, context, allowed=None):
    """
    allowed: insieme di ID chat che QUESTO bot deve gestire (None = entrambi i gruppi).
    Con due bot, il bot storico gestisce solo il gruppo storico e il bot nuovo solo il gruppo grande.
    """
    if not update.message or not update.message.from_user:
        return

    chat_id = str(update.effective_chat.id)

    # UTILS: Stampa nei Log l'ID della chat
    print(f"--- ID CHAT RILEVATO: {chat_id} ---", flush=True)

    # IGNORA TUTTI I BOT
    if update.message.from_user.is_bot:
        return

    if allowed is not None and chat_id not in allowed:
        return

    if chat_id == HISTORIC_CHAT_ID:
        await handle_historic_message(update, context)
    elif chat_id == BIG_CHAT_ID:
        await handle_big_message(update, context)
    # Qualsiasi altra chat: ignorata.

# --- GRUPPO STORICO: GOAT + CONTEGGIO MESSAGGI ---
async def handle_historic_message(update, context):
    user = update.message.from_user
    chat_id = str(update.effective_chat.id)

    text_content = (update.message.text or update.message.caption or "").strip()
    display_name = user.first_name

    # 1. Salva messaggio per il riassunto (solo se contiene testo/didascalia)
    if text_content:
        if chat_id not in chat_history:
            chat_history[chat_id] = []
        chat_history[chat_id].append(f"{display_name}: {text_content}")
        if len(chat_history[chat_id]) > 500:
            chat_history[chat_id].pop(0)

    # 2. Registra conteggio per QUALSIASI messaggio (Testo, Foto, GIF, Sticker, Vocali, ecc.)
    data = load_local_data()
    init_chat_structure(data, chat_id)

    chat_info = data["chats"][chat_id]
    chat_info["msg_weekly"][display_name] = chat_info["msg_weekly"].get(display_name, 0) + 1
    chat_info["msg_ever"][display_name] = chat_info["msg_ever"].get(display_name, 0) + 1

    save_local_data(data)

    # 3. Logica GOAT (solo se il contenuto testuale è la parola "goat")
    if text_content.lower() == "goat":
        # Il giorno conta dall'orario in cui il messaggio è stato scritto (non da quando il bot lo elabora)
        today = get_message_date(update)
        last_date = chat_info["last_date"]

        if last_date is None or today > last_date:
            chat_info["last_date"] = today
            chat_info["today_winner"] = display_name

            current_score = chat_info["leaderboard"].get(display_name, 0)
            chat_info["leaderboard"][display_name] = current_score + 1
            save_local_data(data)

            total_wins = chat_info["leaderboard"][display_name]
            # Prima la risposta (subito), poi il backup in background
            await update.message.reply_text(
                f"Sei il GOAT del giorno 🐐!\n🏆 Vittorie totali: <b>{total_wins}</b>",
                parse_mode='HTML'
            )
            spawn(backup_to_telegram(context))
        elif today == last_date:
            current_goat = chat_info["today_winner"]
            await update.message.reply_text(
                f"Già <b>{current_goat}</b> è il Goat del giorno 🐐",
                parse_mode='HTML'
            )
        # Se il messaggio è di un giorno precedente all'ultimo assegnato: ignorato.

# --- GRUPPO GRANDE: STALLONE (NESSUN CONTEGGIO MESSAGGI) ---
async def handle_big_message(update, context):
    user = update.message.from_user
    chat_id = str(update.effective_chat.id)

    # Solo il TESTO del messaggio (niente didascalie di foto/video) e solo se è
    # esattamente la parola "stallone" (maiuscole/minuscole e spazi ai bordi ininfluenti).
    # Qualsiasi altra cosa -> il bot non risponde affatto.
    text_content = (update.message.text or "").strip()

    # Nessun salvataggio di messaggi, nessun conteggio, nessuna cronologia.
    if text_content.lower() != "stallone":
        return

    display_name = user.first_name
    # Il giorno conta dall'orario in cui il messaggio è stato scritto (non da quando il bot lo elabora)
    today = get_message_date(update)

    data = load_local_data()
    init_chat_structure(data, chat_id)
    chat_info = data["chats"][chat_id]
    last_date = chat_info["stallone_last_date"]

    if last_date is None or today > last_date:
        chat_info["stallone_last_date"] = today
        chat_info["stallone_winner"] = display_name

        current_score = chat_info["stallone_leaderboard"].get(display_name, 0)
        chat_info["stallone_leaderboard"][display_name] = current_score + 1
        save_local_data(data)

        total_wins = chat_info["stallone_leaderboard"][display_name]
        # Nuovo giorno, nuovo vincitore: azzera l'anti-raffica dei "Già X"
        last_already_reply.pop(chat_id, None)
        # Prima la risposta (subito), poi il backup in background
        await update.message.reply_text(
            f"Sei il Re Stallone del giorno! 🐎\n🏆 Vittorie totali: <b>{total_wins}</b>",
            parse_mode='HTML'
        )
        spawn(backup_to_telegram(context))
    elif today == last_date:
        # Una sola risposta "Già X" per raffica: i messaggi che arrivano entro
        # ALREADY_REPLY_COOLDOWN secondi dall'ultima risposta restano senza risposta.
        now_ts = time.monotonic()
        last_reply_ts = last_already_reply.get(chat_id)
        if last_reply_ts is None or now_ts - last_reply_ts >= ALREADY_REPLY_COOLDOWN:
            last_already_reply[chat_id] = now_ts
            current_stallone = chat_info["stallone_winner"]
            await update.message.reply_text(
                f"Già <b>{current_stallone}</b> è il Re Stallone del giorno! 🐎",
                parse_mode='HTML'
            )
    # Se il messaggio è di un giorno precedente all'ultimo assegnato: ignorato.

# --- COMANDO GRUPPO GRANDE: CLASSIFICA STALLONE ---
@only_in(BIG_CHAT_ID)
async def show_stalloneboard(update, context):
    chat_id = str(update.effective_chat.id)
    data = load_local_data()
    init_chat_structure(data, chat_id)

    leaderboard = data["chats"][chat_id]["stallone_leaderboard"]
    if not leaderboard:
        await update.message.reply_text("📊 La classifica Stallone è vuota! Scrivete 'stallone' per iniziare.", parse_mode='HTML')
        return

    sorted_board = sorted(leaderboard.items(), key=lambda x: x[1], reverse=True)
    text = "🏆 <b>CLASSIFICA RE STALLONE DEL GRUPPO</b> 🏆\n\n"
    medals = ["🥇", "🥈", "🥉"]

    for index, (user, wins) in enumerate(sorted_board):
        icon = medals[index] if index < 3 else "👤"
        unit = "vittoria" if wins == 1 else "vittorie"
        text += f"{icon} <b>{user}</b>: {wins} {unit}\n"

    await update.message.reply_text(text, parse_mode='HTML')

# --- COMANDI RESET (SOLO GRUPPO STORICO) ---
@only_in(HISTORIC_CHAT_ID)
async def reset_goatboard(update, context):
    chat_id = str(update.effective_chat.id)
    data = load_local_data()
    init_chat_structure(data, chat_id)

    data["chats"][chat_id]["leaderboard"] = {}
    data["chats"][chat_id]["last_date"] = None
    data["chats"][chat_id]["today_winner"] = None
    save_local_data(data)

    await backup_to_telegram(context)
    await update.message.reply_text("🧹 Classifica GOAT azzerata con successo!", parse_mode='HTML')

@only_in(HISTORIC_CHAT_ID)
async def reset_msgboard(update, context):
    chat_id = str(update.effective_chat.id)
    data = load_local_data()
    init_chat_structure(data, chat_id)

    data["chats"][chat_id]["msg_weekly"] = {}
    data["chats"][chat_id]["msg_ever"] = {}
    save_local_data(data)

    await backup_to_telegram(context)
    await update.message.reply_text("🧹 Classifiche dei messaggi (settimanale ed ever) azzerate!", parse_mode='HTML')

# --- COMANDO LISTA COMANDI (SOLO GRUPPO STORICO) ---
@only_in(HISTORIC_CHAT_ID)
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
        "🧹 <b>/resetgoat</b> - Azzera la classifica GOAT del gruppo.\n"
        "🧹 <b>/resetmsg</b> - Azzera le classifiche dei messaggi.\n\n"
        "💡 <i>Curiosità: Scrivi semplicemente <b>goat</b> in chat per eleggere il GOAT del giorno!</i>"
    )
    await update.message.reply_text(text, parse_mode='HTML')

# --- CLASSIFICHE MESSAGGI (SOLO GRUPPO STORICO) ---
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

@only_in(HISTORIC_CHAT_ID)
async def show_weekly_ranking(update, context):
    chat_id = str(update.effective_chat.id)
    data = load_local_data()
    init_chat_structure(data, chat_id)

    text = generate_ranking_text("📊 <b>CLASSIFICA SETTIMANALE MESSAGGI</b> 📊", data["chats"][chat_id]["msg_weekly"])
    await update.message.reply_text(text, parse_mode='HTML')

@only_in(HISTORIC_CHAT_ID)
async def show_ever_ranking(update, context):
    chat_id = str(update.effective_chat.id)
    data = load_local_data()
    init_chat_structure(data, chat_id)

    text = generate_ranking_text("👑 <b>CLASSIFICA GENERALE MESSAGGI (EVER)</b> 👑", data["chats"][chat_id]["msg_ever"])
    await update.message.reply_text(text, parse_mode='HTML')

@only_in(HISTORIC_CHAT_ID)
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

# --- AUTONOMOUS TASK: RESET E PUBBLICAZIONE DOMENICA ALLE 15:00 (SOLO GRUPPO STORICO) ---
async def weekly_auto_reset_task(app):
    while True:
        now = get_italian_now()
        if now.weekday() == 6 and now.hour == 15 and now.minute == 0:
            data = load_local_data()
            chat_info = data.get("chats", {}).get(HISTORIC_CHAT_ID)
            if chat_info is not None:
                try:
                    text = generate_ranking_text(
                        "🏆 <b>CLASSIFICA FINALE DELLA SETTIMANA</b> 🏆\n"
                        "<i>I contatori della settimana sono stati appena azzerati!</i>",
                        chat_info.get("msg_weekly", {})
                    )
                    await app.bot.send_message(chat_id=int(HISTORIC_CHAT_ID), text=text, parse_mode='HTML')
                    chat_info["msg_weekly"] = {}
                except Exception as e:
                    logging.error(f"Errore invio classifica automatica alla chat {HISTORIC_CHAT_ID}: {e}")

                save_local_data(data)
                class MockContext:
                    bot = app.bot
                await backup_to_telegram(MockContext())

            await asyncio.sleep(61)
        else:
            await asyncio.sleep(30)

# --- COMANDI RIASSUNTO GEMINI (SOLO GRUPPO STORICO) ---
@only_in(HISTORIC_CHAT_ID)
async def make_summary(update, context):
    # In background: il bot resta subito libero per gli altri messaggi
    spawn(generate_summary_response(update, limit=75, title="RIASSUNTO BREVE"))

@only_in(HISTORIC_CHAT_ID)
async def make_long_summary(update, context):
    # In background: il bot resta subito libero per gli altri messaggi
    spawn(generate_summary_response(update, limit=500, title="RIASSUNTO ESTESO"))

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
        # Chiamata bloccante eseguita in un thread, per non fermare il bot
        response = await asyncio.to_thread(model.generate_content, prompt)

        summary_text = f"📝 <b>{title} DELLA CHAT</b> 🤖\n\n{response.text}"
        await status_msg.edit_text(summary_text, parse_mode='HTML')

    except Exception as e:
        await status_msg.edit_text(f"❌ Errore riscontrato: {str(e)}")

# --- COMANDO TRASCRIZIONE AUDIO (SOLO GRUPPO STORICO) ---
@only_in(HISTORIC_CHAT_ID)
async def transcribe_audio(update, context):
    reply_msg = update.message.reply_to_message
    if not reply_msg or not (reply_msg.voice or reply_msg.audio or reply_msg.video_note):
        await update.message.reply_text("⚠️ Rispondi a un messaggio vocale con il comando `/trans` per trascriverlo!", parse_mode='Markdown')
        return

    # In background: il bot resta subito libero per gli altri messaggi
    spawn(run_transcription(update, context, reply_msg))

async def run_transcription(update, context, reply_msg):
    status_msg = await update.message.reply_text("🎧 <i>Trascrizione del vocale in corso...</i>", parse_mode='HTML')

    # Nome file unico per messaggio: due trascrizioni insieme non si pestano i piedi
    file_path = f"temp_audio_{update.message.message_id}.ogg"

    try:
        if not GEMINI_API_KEY:
            await status_msg.edit_text("❌ Errore: GEMINI_API_KEY non configurata su Render!")
            return

        audio_obj = reply_msg.voice or reply_msg.audio or reply_msg.video_note
        telegram_file = await context.bot.get_file(audio_obj.file_id)
        await telegram_file.download_to_drive(file_path)

        with open(file_path, "rb") as f:
            audio_bytes = f.read()

        audio_blob = {
            "mime_type": "audio/ogg",
            "data": audio_bytes
        }

        prompt = (
            "Trascrivi fedelmente questo audio in italiano. "
            "Restituisci solo ed esclusivamente il testo trascritto, senza commenti aggiuntivi. "
            "NON usare sintassi Markdown (no asterischi, cancelletti o trattini bassi)."
        )

        model = genai.GenerativeModel('gemini-3.1-flash-lite')
        response = await asyncio.to_thread(model.generate_content, [prompt, audio_blob])

        user_name = reply_msg.from_user.first_name if reply_msg.from_user else "Utente"
        transcript_text = f"🎙️ <b>TRASCRIZIONE VOCALE DI {user_name.upper()}</b> 📝\n\n{response.text.strip()}"

        await status_msg.edit_text(transcript_text, parse_mode='HTML')

    except Exception as e:
        logging.error(f"Errore trascrizione audio: {e}")
        await status_msg.edit_text(f"❌ Impossibile trascrivere l'audio: {str(e)}")
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

# --- INIZIALIZZAZIONE ASINCRONA TASK E RIPRISTINO ---
async def post_init(application: Application):
    """Avvia il ripristino dei dati e i task in background dopo l'inizializzazione"""
    await restore_from_telegram(application)
    asyncio.create_task(weekly_auto_reset_task(application))

def make_message_handler(allowed):
    """Crea il gestore messaggi limitato agli ID chat indicati."""
    async def _handler(update, context):
        await handle_message(update, context, allowed=allowed)
    return _handler

def register_historic_handlers(application, also_big):
    """Gestori del bot storico. also_big=True solo in modalità a bot singolo (anche gruppo grande)."""
    # Ogni comando è protetto da @only_in: fuori dalla chat autorizzata viene ignorato in silenzio.
    application.add_handler(CommandHandler("goatboard", show_goatboard))
    application.add_handler(CommandHandler("classificasettimana", show_weekly_ranking))
    application.add_handler(CommandHandler("classificaever", show_ever_ranking))
    application.add_handler(CommandHandler("riassunto", make_summary))
    application.add_handler(CommandHandler("riassuntolungo", make_long_summary))
    application.add_handler(CommandHandler("trans", transcribe_audio))
    application.add_handler(CommandHandler("goatcomm", show_commands))
    application.add_handler(CommandHandler("resetgoat", reset_goatboard))
    application.add_handler(CommandHandler("resetmsg", reset_msgboard))
    allowed = {HISTORIC_CHAT_ID}
    if also_big:
        application.add_handler(CommandHandler("stalloneboard", show_stalloneboard))
        allowed.add(BIG_CHAT_ID)
    # Message Handler (i comandi sono esclusi: non vengono mai contati)
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, make_message_handler(allowed)))

def register_big_handlers(application):
    """Gestori del bot del gruppo grande: solo /stalloneboard e la parola "stallone"."""
    application.add_handler(CommandHandler("stalloneboard", show_stalloneboard))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, make_message_handler({BIG_CHAT_ID})))

async def run_two_bots():
    """Avvia insieme il bot storico (TELEGRAM_TOKEN) e il bot del gruppo grande (TELEGRAM_TOKEN_BIG)."""
    global BACKUP_BOT

    historic_app = Application.builder().token(TELEGRAM_TOKEN).build()
    big_app = Application.builder().token(TELEGRAM_TOKEN_BIG).build()
    register_historic_handlers(historic_app, also_big=False)
    register_big_handlers(big_app)
    apps = [historic_app, big_app]

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass

    initialized, started = [], []
    weekly_task = None
    try:
        for a in apps:
            await a.initialize()
            initialized.append(a)

        # I backup partono sempre dal bot storico (l'unico presente nel gruppo di backup)
        BACKUP_BOT = historic_app.bot
        await restore_from_telegram(historic_app)
        weekly_task = asyncio.create_task(weekly_auto_reset_task(historic_app))

        for a in apps:
            await a.start()
            started.append(a)
            await a.updater.start_polling()

        print("GOAT Bot attivo! (2 bot: storico + gruppo grande)", flush=True)
        await stop_event.wait()
    finally:
        if weekly_task:
            weekly_task.cancel()
        for a in reversed(started):
            try:
                if a.updater and a.updater.running:
                    await a.updater.stop()
                await a.stop()
            except Exception as e:
                logging.error(f"Errore durante l'arresto del bot: {e}")
        for a in reversed(initialized):
            try:
                await a.shutdown()
            except Exception as e:
                logging.error(f"Errore durante lo shutdown del bot: {e}")

def main():
    keep_alive()

    if TELEGRAM_TOKEN_BIG:
        # Due bot nello stesso programma, stessi dati (goat_data.json) e stesso gruppo di backup
        asyncio.run(run_two_bots())
        return

    # Modalità a bot singolo: il bot storico gestisce entrambi i gruppi
    application = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    register_historic_handlers(application, also_big=True)

    print("GOAT Bot attivo!", flush=True)
    application.run_polling()

if __name__ == '__main__':
    main()
