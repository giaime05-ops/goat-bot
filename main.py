"""
GOAT Bot - versione universale (multi-chat).

Nessun ID di gruppo è scritto nel codice: il bot funziona in QUALSIASI gruppo/supergruppo in cui
viene aggiunto e tiene i dati di ogni chat completamente separati (chiave "chats" -> chat_id nel
file goat_data.json, che è unico e viene salvato/ripristinato dal gruppo di backup su Telegram).

Ruoli (un bot = un ruolo, deciso dal token con cui parte):
  - TELEGRAM_TOKEN      -> "GoatBot": modalità GOAT in ogni gruppo (parola "goat", conteggio messaggi,
                           classifiche, riassunti IA, trascrizioni).
  - TELEGRAM_TOKEN_BIG  -> (facoltativo) "bot Stallone": modalità STALLONE in ogni gruppo in cui viene
                           aggiunto (parola "stallone", nessun conteggio, solo /stalloneboard,
                           qualsiasi altro comando ignorato in silenzio).
  Un gruppo in cui è già presente la sezione stallone nei dati è "dello stallone": il GoatBot lo ignora.
  Se TELEGRAM_TOKEN_BIG manca, il GoatBot gestisce da solo entrambe le modalità (in base a questa regola).
"""
import os
import json
import math
import time
import signal
import asyncio
import logging
from collections import deque
from datetime import datetime
from functools import wraps
from zoneinfo import ZoneInfo
from threading import Thread
from flask import Flask
from telegram.ext import Application, MessageHandler, CommandHandler, filters
import google.generativeai as genai

# --- LOGGING ---
logging.basicConfig(level=logging.INFO)
# httpx/httpcore, a livello INFO, scrivono nei log l'URL completo di ogni richiesta a Telegram,
# che contiene il TOKEN del bot: li silenziamo (restano visibili solo warning ed errori).
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# --- VARIABILI D'AMBIENTE ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")  # GoatBot: modalità GOAT
# Token del SECONDO bot (modalità STALLONE). Se manca, il GoatBot gestisce da solo entrambe le modalità.
TELEGRAM_TOKEN_BIG = os.environ.get("TELEGRAM_TOKEN_BIG")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
BACKUP_CHAT_ID = os.environ.get("BACKUP_CHAT_ID")  # Es. "-100xxxxxxxxxx"

# --- RUOLI E MODALITÀ ---
ROLE_AUTO = "auto"          # bot singolo: modalità scelta in base alla chat
ROLE_GOAT = "goat"          # GoatBot (con due bot)
ROLE_STALLONE = "stallone"  # bot Stallone (con due bot)
MODE_GOAT = "goat"
MODE_STALLONE = "stallone"
GROUP_TYPES = ("group", "supergroup")  # il bot lavora solo nei gruppi (non in privato)

# Dopo che il titolo STALLONE è stato assegnato, il messaggio "Già X è il Re Stallone" viene inviato
# al massimo UNA volta ogni tot secondi, così una raffica di "stallone" (es. a mezzanotte)
# non genera decine di risposte identiche.
ALREADY_REPLY_COOLDOWN = 20  # secondi
last_already_reply = {}      # chat_id -> istante dell'ultimo "Già X" inviato

# Protezione dai costi IA: un solo comando IA (riassunti/trascrizioni) ogni tot secondi per chat.
AI_COOLDOWN = 30  # secondi
last_ai_request = {}  # chat_id -> istante dell'ultimo comando IA

# Inizializzazione globale di Gemini con la chiave d'ambiente
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
# Storico messaggi per i riassunti: SEPARATO per chat (chat_id -> ultimi 500 messaggi di QUELLA chat).
chat_history = {}
HISTORY_MAX = 500

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

# Bot usato per i backup. Con due bot è sempre il GoatBot, così solo lui deve essere
# nel gruppo di backup (admin con permesso di fissare). Se None, si usa il bot del contesto.
BACKUP_BOT = None

class BotContext:
    """Contesto minimo (solo .bot) per le funzioni chiamate fuori da un handler."""
    def __init__(self, bot):
        self.bot = bot

def load_local_data():
    """Legge goat_data.json (struttura: {"chats": {chat_id: {...}}})."""
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("il file non contiene un oggetto JSON")
            return data
        except ValueError as e:
            # File illeggibile/corrotto: lo metto da parte invece di sovrascriverlo al prossimo salvataggio
            logging.error(f"File dati illeggibile ({e}): lo salvo come copia e riparto da zero.")
            try:
                os.replace(DATA_FILE, f"{DATA_FILE}.corrotto-{int(time.time())}")
            except OSError:
                pass
        except OSError as e:
            logging.error(f"Errore lettura file locale: {e}")
    return {"chats": {}}

def save_local_data(data):
    """Salvataggio atomico: scrive su un file temporaneo e poi lo sostituisce (mai file a metà)."""
    tmp_path = f"{DATA_FILE}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
        os.replace(tmp_path, DATA_FILE)
    except Exception as e:
        logging.error(f"Errore salvataggio dati: {e}")

def is_backup_chat(chat_id):
    return bool(BACKUP_CHAT_ID) and str(chat_id) == str(BACKUP_CHAT_ID).strip()

async def backup_to_telegram(context):
    """Invia il file JSON corrente (tutte le chat) al gruppo di backup e lo fissa"""
    if not BACKUP_CHAT_ID:
        return
    async with backup_lock:
        bot = BACKUP_BOT or context.bot
        try:
            if not os.path.exists(DATA_FILE):
                save_local_data(load_local_data())
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
    """Scarica all'avvio l'ultimo backup fissato nel gruppo di backup (solo se è un JSON valido)"""
    if not BACKUP_CHAT_ID:
        return
    try:
        logging.info("Tentativo di ripristino dati da Telegram...")
        chat = await app.bot.get_chat(chat_id=BACKUP_CHAT_ID)

        # Cerca il file tra i messaggi fissati
        pinned = getattr(chat, "pinned_message", None)
        if pinned and pinned.document:
            doc = pinned.document
            if doc.file_name and doc.file_name.endswith('.json'):
                tg_file = await app.bot.get_file(doc.file_id)
                tmp_path = f"{DATA_FILE}.restore"
                await tg_file.download_to_drive(tmp_path)
                try:
                    with open(tmp_path, "r", encoding="utf-8") as f:
                        restored = json.load(f)
                    if not isinstance(restored, dict):
                        raise ValueError("il file non contiene un oggetto JSON")
                except ValueError as e:
                    logging.error(f"Il backup fissato NON è un JSON valido ({e}): non lo uso.")
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
                    return
                os.replace(tmp_path, DATA_FILE)
                logging.info("Dati ripristinati con successo dal messaggio fissato!")
                return

        logging.info("Nessun file trovato nei messaggi fissati della chat di backup.")
    except Exception as e:
        logging.error(f"Errore durante il ripristino da Telegram: {e}")

# --- STRUTTURA DATI PER CHAT ---
# Ogni chat ha la sua voce in data["chats"][chat_id]. Le funzioni ensure_* aggiungono solo i campi
# mancanti, senza toccare né cancellare quelli già presenti (compatibile con i vecchi goat_data.json).
def ensure_chat(data, chat_id):
    chats = data.setdefault("chats", {})
    return chats.setdefault(chat_id, {})

def ensure_goat_section(data, chat_id):
    """Campi della modalità GOAT: GOAT del giorno, classifica vittorie, messaggi settimanali ed ever."""
    chat_info = ensure_chat(data, chat_id)
    chat_info.setdefault("last_date", None)
    chat_info.setdefault("today_winner", None)
    chat_info.setdefault("leaderboard", {})
    chat_info.setdefault("msg_weekly", {})
    chat_info.setdefault("msg_ever", {})
    return chat_info

def ensure_stallone_section(data, chat_id):
    """Campi della modalità STALLONE (nessun conteggio messaggi)."""
    chat_info = ensure_chat(data, chat_id)
    chat_info.setdefault("stallone_last_date", None)
    chat_info.setdefault("stallone_winner", None)
    chat_info.setdefault("stallone_leaderboard", {})
    return chat_info

def is_stallone_chat(data, chat_id):
    """Una chat è "dello stallone" se nei dati ha la sezione stallone."""
    chat_info = data.get("chats", {}).get(chat_id)
    return isinstance(chat_info, dict) and "stallone_leaderboard" in chat_info

def resolve_mode(role, data, chat_id):
    """Modalità con cui QUESTO bot tratta la chat: MODE_GOAT, MODE_STALLONE o None (ignorare)."""
    claimed = is_stallone_chat(data, chat_id)
    if role == ROLE_STALLONE:
        return MODE_STALLONE
    if role == ROLE_GOAT:
        return None if claimed else MODE_GOAT
    return MODE_STALLONE if claimed else MODE_GOAT  # ROLE_AUTO

def get_role(context):
    bot_data = getattr(context, "bot_data", None) or {}
    return bot_data.get("role", ROLE_AUTO)

def chat_is_allowed(update):
    """Solo gruppi/supergruppi, e mai il gruppo di backup."""
    chat = update.effective_chat
    if chat is None or chat.type not in GROUP_TYPES:
        return False
    return not is_backup_chat(chat.id)

# --- MIGRAZIONE GRUPPO -> SUPERGRUPPO (cambia l'ID della chat) ---
def migrate_chat_data(data, old_id, new_id):
    chats = data.setdefault("chats", {})
    if old_id not in chats:
        return False
    if new_id in chats:
        logging.warning(f"Migrazione chat {old_id} -> {new_id} saltata: il nuovo ID ha già dei dati.")
        return False
    chats[new_id] = chats.pop(old_id)
    return True

def handle_chat_migration(old_id, new_id):
    """Il gruppo è diventato supergruppo: porta con sé dati e storico, così non si perde nulla."""
    if is_backup_chat(old_id):
        logging.error(
            f"ATTENZIONE: il gruppo di backup è diventato un supergruppo con nuovo ID {new_id}. "
            f"Aggiorna la variabile BACKUP_CHAT_ID."
        )
        return
    data = load_local_data()
    if migrate_chat_data(data, old_id, new_id):
        save_local_data(data)
        logging.info(f"Chat migrata: {old_id} -> {new_id}")
    # Anche i dati in memoria (storico riassunti, anti-raffica) seguono la chat
    if old_id in chat_history and new_id not in chat_history:
        chat_history[new_id] = chat_history.pop(old_id)
    if old_id in last_already_reply:
        last_already_reply[new_id] = last_already_reply.pop(old_id)
    if old_id in last_ai_request:
        last_ai_request[new_id] = last_ai_request.pop(old_id)

# --- PERMESSI ---
async def is_chat_admin(update, context):
    """True se chi scrive è admin del gruppo (anche admin anonimo)."""
    msg = update.message
    chat = update.effective_chat
    if msg is None or chat is None:
        return False
    sender_chat = getattr(msg, "sender_chat", None)
    if sender_chat is not None and sender_chat.id == chat.id:
        return True  # admin anonimo: scrive "a nome del gruppo"
    user = msg.from_user
    if user is None:
        return False
    try:
        admins = await context.bot.get_chat_administrators(chat.id)
        return any(a.user.id == user.id for a in admins)
    except Exception as e:
        logging.error(f"Impossibile verificare gli admin della chat {chat.id}: {e}")
        return False

def admin_only(func):
    """Il comando può essere usato solo dagli admin del gruppo."""
    @wraps(func)
    async def wrapper(update, context):
        if not await is_chat_admin(update, context):
            await update.message.reply_text("⛔ Solo gli admin del gruppo possono usare questo comando.")
            return
        return await func(update, context)
    return wrapper

# --- FILTRI PER I COMANDI ---
def _command_for_mode(required_mode):
    """
    Il comando funziona solo in un gruppo trattato da QUESTO bot nella modalità richiesta.
    In ogni altro caso (chat privata, gruppo di backup, modalità diversa) viene IGNORATO in
    silenzio assoluto: nessuna risposta, nessun messaggio di errore.
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(update, context):
            if not update.message or not chat_is_allowed(update):
                return
            chat_id = str(update.effective_chat.id)
            mode = resolve_mode(get_role(context), load_local_data(), chat_id)
            if mode != required_mode:
                return
            return await func(update, context)
        return wrapper
    return decorator

goat_command = _command_for_mode(MODE_GOAT)
stallone_command = _command_for_mode(MODE_STALLONE)

# --- GESTIONE MESSAGGI (SMISTAMENTO PER CHAT) ---
seen_chats = set()

def log_chat_once(update, mode):
    chat = update.effective_chat
    key = (str(chat.id), mode)
    if key not in seen_chats:
        seen_chats.add(key)
        print(f"--- ID CHAT RILEVATO: {chat.id} ({getattr(chat, 'title', None) or '-'}) -> modalità {mode} ---", flush=True)

async def handle_message(update, context):
    msg = update.message
    if not msg:
        return

    # Il gruppo è diventato supergruppo: sposta i dati sul nuovo ID
    migrate_to = getattr(msg, "migrate_to_chat_id", None)
    if migrate_to:
        handle_chat_migration(str(update.effective_chat.id), str(migrate_to))
        return

    # IGNORA TUTTI I BOT
    if not msg.from_user or msg.from_user.is_bot:
        return

    # Solo gruppi, mai il gruppo di backup
    if not chat_is_allowed(update):
        return

    chat_id = str(update.effective_chat.id)
    data = load_local_data()
    mode = resolve_mode(get_role(context), data, chat_id)
    if mode is None:
        return

    log_chat_once(update, mode)
    if mode == MODE_GOAT:
        await handle_goat_message(update, context, data)
    else:
        await handle_stallone_message(update, context, data)

# --- MODALITÀ GOAT: GOAT DEL GIORNO + CONTEGGIO MESSAGGI (dati della SOLA chat corrente) ---
async def handle_goat_message(update, context, data):
    user = update.message.from_user
    chat_id = str(update.effective_chat.id)

    text_content = (update.message.text or update.message.caption or "").strip()
    display_name = user.first_name

    # 1. Salva messaggio per il riassunto di QUESTA chat (solo se contiene testo/didascalia)
    if text_content:
        history = chat_history.get(chat_id)
        if history is None:
            history = chat_history[chat_id] = deque(maxlen=HISTORY_MAX)
        history.append(f"{display_name}: {text_content}")

    # 2. Registra conteggio per QUALSIASI messaggio (Testo, Foto, GIF, Sticker, Vocali, ecc.)
    chat_info = ensure_goat_section(data, chat_id)
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

# --- MODALITÀ STALLONE (NESSUN CONTEGGIO MESSAGGI) ---
async def handle_stallone_message(update, context, data):
    user = update.message.from_user
    chat_id = str(update.effective_chat.id)

    # Il primo messaggio visto in una chat "prenota" la chat per lo stallone (il GoatBot la ignorerà)
    was_claimed = is_stallone_chat(data, chat_id)
    chat_info = ensure_stallone_section(data, chat_id)
    if not was_claimed:
        save_local_data(data)

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

# --- COMANDO STALLONE: CLASSIFICA ---
@stallone_command
async def show_stalloneboard(update, context):
    chat_id = str(update.effective_chat.id)
    data = load_local_data()
    was_claimed = is_stallone_chat(data, chat_id)
    chat_info = ensure_stallone_section(data, chat_id)
    if not was_claimed:
        save_local_data(data)

    leaderboard = chat_info["stallone_leaderboard"]
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

# --- COMANDI RESET (SOLO ADMIN, SOLO DATI DELLA CHAT CORRENTE) ---
@goat_command
@admin_only
async def reset_goatboard(update, context):
    chat_id = str(update.effective_chat.id)
    data = load_local_data()
    chat_info = ensure_goat_section(data, chat_id)

    chat_info["leaderboard"] = {}
    chat_info["last_date"] = None
    chat_info["today_winner"] = None
    save_local_data(data)

    await update.message.reply_text("🧹 Classifica GOAT azzerata con successo!", parse_mode='HTML')
    spawn(backup_to_telegram(context))

@goat_command
@admin_only
async def reset_msgboard(update, context):
    chat_id = str(update.effective_chat.id)
    data = load_local_data()
    chat_info = ensure_goat_section(data, chat_id)

    chat_info["msg_weekly"] = {}
    chat_info["msg_ever"] = {}
    save_local_data(data)

    await update.message.reply_text("🧹 Classifiche dei messaggi (settimanale ed ever) azzerate!", parse_mode='HTML')
    spawn(backup_to_telegram(context))

# --- COMANDO LISTA COMANDI ---
@goat_command
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
        "🧹 <b>/resetgoat</b> - Azzera la classifica GOAT del gruppo (solo admin).\n"
        "🧹 <b>/resetmsg</b> - Azzera le classifiche dei messaggi (solo admin).\n\n"
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

@goat_command
async def show_weekly_ranking(update, context):
    chat_id = str(update.effective_chat.id)
    chat_info = ensure_goat_section(load_local_data(), chat_id)

    text = generate_ranking_text("📊 <b>CLASSIFICA SETTIMANALE MESSAGGI</b> 📊", chat_info["msg_weekly"])
    await update.message.reply_text(text, parse_mode='HTML')

@goat_command
async def show_ever_ranking(update, context):
    chat_id = str(update.effective_chat.id)
    chat_info = ensure_goat_section(load_local_data(), chat_id)

    text = generate_ranking_text("👑 <b>CLASSIFICA GENERALE MESSAGGI (EVER)</b> 👑", chat_info["msg_ever"])
    await update.message.reply_text(text, parse_mode='HTML')

@goat_command
async def show_goatboard(update, context):
    chat_id = str(update.effective_chat.id)
    chat_info = ensure_goat_section(load_local_data(), chat_id)

    leaderboard = chat_info["leaderboard"]
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

# --- AUTONOMOUS TASK: RESET E PUBBLICAZIONE DOMENICA ALLE 15:00 (OGNI CHAT GOAT, CON I PROPRI DATI) ---
async def run_weekly_reset(app):
    snapshot = load_local_data()
    sent_ok, migrations = [], []

    for chat_id, chat_info in list(snapshot.get("chats", {}).items()):
        # Solo chat in modalità GOAT con messaggi della settimana; mai chat dello stallone o di backup
        if not isinstance(chat_info, dict) or is_stallone_chat(snapshot, chat_id) or is_backup_chat(chat_id):
            continue
        weekly = chat_info.get("msg_weekly") or {}
        if not weekly:
            continue
        try:
            text = generate_ranking_text(
                "🏆 <b>CLASSIFICA FINALE DELLA SETTIMANA</b> 🏆\n"
                "<i>I contatori della settimana sono stati appena azzerati!</i>",
                weekly
            )
            await app.bot.send_message(chat_id=int(chat_id), text=text, parse_mode='HTML')
            sent_ok.append(chat_id)
        except Exception as e:
            new_id = getattr(e, "new_chat_id", None)
            if new_id:
                migrations.append((chat_id, str(new_id)))  # il gruppo è diventato supergruppo
            else:
                logging.error(f"Errore invio classifica automatica alla chat {chat_id}: {e}")
        await asyncio.sleep(0.1)  # resta nei limiti di invio di Telegram

    if not sent_ok and not migrations:
        return

    # Ricarico i dati ADESSO (durante l'invio altri messaggi possono essere stati registrati)
    # e azzero solo le chat a cui la classifica è stata davvero inviata.
    fresh = load_local_data()
    for chat_id in sent_ok:
        info = fresh.get("chats", {}).get(chat_id)
        if isinstance(info, dict):
            info["msg_weekly"] = {}
    for old_id, new_id in migrations:
        migrate_chat_data(fresh, old_id, new_id)
    save_local_data(fresh)
    await backup_to_telegram(BotContext(app.bot))

async def weekly_auto_reset_task(app):
    while True:
        try:
            now = get_italian_now()
            if now.weekday() == 6 and now.hour == 15 and now.minute == 0:
                await run_weekly_reset(app)
                await asyncio.sleep(61)
            else:
                await asyncio.sleep(30)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logging.error(f"Errore nel reset settimanale: {e}")
            await asyncio.sleep(30)

# --- COMANDI IA (GEMINI) ---
def ai_wait_seconds(chat_id):
    """Secondi da attendere prima di un nuovo comando IA in questa chat (0 = via libera)."""
    last = last_ai_request.get(chat_id)
    if last is None:
        return 0
    remaining = AI_COOLDOWN - (time.monotonic() - last)
    return math.ceil(remaining) if remaining > 0 else 0

async def check_ai_cooldown(update):
    """True se si può procedere (e prenota lo slot), altrimenti avvisa e restituisce False."""
    chat_id = str(update.effective_chat.id)
    wait = ai_wait_seconds(chat_id)
    if wait > 0:
        await update.message.reply_text(f"⏳ Riprova tra {wait} secondi: l'IA è stata appena usata in questo gruppo.")
        return False
    last_ai_request[chat_id] = time.monotonic()
    return True

@goat_command
async def make_summary(update, context):
    await start_summary(update, limit=75, title="RIASSUNTO BREVE")

@goat_command
async def make_long_summary(update, context):
    await start_summary(update, limit=500, title="RIASSUNTO ESTESO")

async def start_summary(update, limit, title):
    chat_id = str(update.effective_chat.id)
    history = chat_history.get(chat_id)
    if not history or len(history) < 3:
        await update.message.reply_text("🤖 Ci sono troppi pochi messaggi recenti per fare un riassunto!")
        return
    if not await check_ai_cooldown(update):
        return
    # In background: il bot resta subito libero per gli altri messaggi
    spawn(generate_summary_response(update, limit, title))

async def generate_summary_response(update, limit, title):
    chat_id = str(update.effective_chat.id)
    # SOLO i messaggi di questa chat: lo storico è indicizzato per chat_id
    recent_messages = list(chat_history.get(chat_id, []))[-limit:]

    status_msg = await update.message.reply_text(f"🤖 L'IA sta leggendo gli ultimi messaggi per il {title.lower()}...")

    try:
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

# --- COMANDO TRASCRIZIONE AUDIO ---
@goat_command
async def transcribe_audio(update, context):
    reply_msg = update.message.reply_to_message
    if not reply_msg or not (reply_msg.voice or reply_msg.audio or reply_msg.video_note):
        await update.message.reply_text("⚠️ Rispondi a un messaggio vocale con il comando `/trans` per trascriverlo!", parse_mode='Markdown')
        return
    if not await check_ai_cooldown(update):
        return

    # In background: il bot resta subito libero per gli altri messaggi
    spawn(run_transcription(update, context, reply_msg))

async def run_transcription(update, context, reply_msg):
    status_msg = await update.message.reply_text("🎧 <i>Trascrizione del vocale in corso...</i>", parse_mode='HTML')

    # Nome file unico per chat e messaggio: due trascrizioni insieme non si pestano i piedi
    file_path = f"temp_audio_{abs(update.effective_chat.id)}_{update.message.message_id}.ogg"

    try:
        if not GEMINI_API_KEY:
            await status_msg.edit_text("❌ Errore: GEMINI_API_KEY non configurata!")
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

# --- REGISTRAZIONE HANDLER E AVVIO ---
def register_handlers(application, role):
    """Registra i gestori del bot in base al suo ruolo (goat / stallone / auto)."""
    application.bot_data["role"] = role

    if role in (ROLE_GOAT, ROLE_AUTO):
        # Ogni comando è protetto dai decoratori: fuori da un gruppo valido o in una modalità
        # diversa viene ignorato in silenzio.
        application.add_handler(CommandHandler("goatboard", show_goatboard))
        application.add_handler(CommandHandler("classificasettimana", show_weekly_ranking))
        application.add_handler(CommandHandler("classificaever", show_ever_ranking))
        application.add_handler(CommandHandler("riassunto", make_summary))
        application.add_handler(CommandHandler("riassuntolungo", make_long_summary))
        application.add_handler(CommandHandler("trans", transcribe_audio))
        application.add_handler(CommandHandler("goatcomm", show_commands))
        application.add_handler(CommandHandler("resetgoat", reset_goatboard))
        application.add_handler(CommandHandler("resetmsg", reset_msgboard))
    if role in (ROLE_STALLONE, ROLE_AUTO):
        application.add_handler(CommandHandler("stalloneboard", show_stalloneboard))

    # Message Handler (i comandi sono esclusi: non vengono mai contati)
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))

# --- INIZIALIZZAZIONE ASINCRONA TASK E RIPRISTINO (bot singolo) ---
async def post_init(application: Application):
    """Avvia il ripristino dei dati e i task in background dopo l'inizializzazione"""
    await restore_from_telegram(application)
    asyncio.create_task(weekly_auto_reset_task(application))

async def run_two_bots():
    """Avvia insieme il GoatBot (TELEGRAM_TOKEN) e il bot Stallone (TELEGRAM_TOKEN_BIG)."""
    global BACKUP_BOT

    goat_app = Application.builder().token(TELEGRAM_TOKEN).build()
    stallone_app = Application.builder().token(TELEGRAM_TOKEN_BIG).build()
    register_handlers(goat_app, ROLE_GOAT)
    register_handlers(stallone_app, ROLE_STALLONE)
    apps = [goat_app, stallone_app]

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

        # I backup partono sempre dal GoatBot (l'unico presente nel gruppo di backup)
        BACKUP_BOT = goat_app.bot
        await restore_from_telegram(goat_app)
        weekly_task = asyncio.create_task(weekly_auto_reset_task(goat_app))

        for a in apps:
            await a.start()
            started.append(a)
            await a.updater.start_polling()

        print("GOAT Bot attivo! (2 bot: GoatBot + bot Stallone, qualsiasi gruppo)", flush=True)
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

    # Modalità a bot singolo: il GoatBot gestisce sia i gruppi GOAT sia quelli dello stallone
    application = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    register_handlers(application, ROLE_AUTO)

    print("GOAT Bot attivo! (bot singolo, qualsiasi gruppo)", flush=True)
    application.run_polling()

if __name__ == '__main__':
    main()
