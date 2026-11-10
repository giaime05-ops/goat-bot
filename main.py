"""
GOAT Bot - versione universale (multi-chat) con approvazione dei gruppi.

Due bot nello stesso programma, con ruoli separati (deciso dal token con cui parte ciascuno):

  - TELEGRAM_TOKEN      -> "GoatBot": modalità GOAT in OGNI gruppo approvato dal proprietario
                           (parola "goat", conteggio messaggi, classifiche, riassunti IA, trascrizioni).
                           Ha anche il pannello /admin (solo in chat privata col proprietario).
  - TELEGRAM_TOKEN_BIG  -> "bot Stallone" (facoltativo): lavora SOLO nei gruppi elencati in
                           STALLONE_CHAT_IDS (default: il gruppo grande). Parola "stallone",
                           nessun conteggio, solo /stalloneboard, qualsiasi altro comando ignorato.
                           Niente riassunti, niente goat, niente approvazioni: è un bot a parte.

I gruppi elencati in STALLONE_CHAT_IDS sono ignorati dal GoatBot. Se TELEGRAM_TOKEN_BIG manca, il
GoatBot gestisce da solo sia i gruppi GOAT sia quelli dello stallone.

Approvazione: quando il GoatBot viene aggiunto a un NUOVO gruppo, resta inattivo e avvisa il proprietario
(OWNER_ID) nella chat privata con i bottoni Consenti/Nega. I gruppi già presenti nei dati prima
dell'introduzione dell'approvazione restano attivi.

Tutti i dati (uno spazio separato per ogni chat) stanno in un unico goat_data.json, salvato/ripristinato
dal gruppo di backup su Telegram.
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
from html import escape
from zoneinfo import ZoneInfo
from threading import Thread
from flask import Flask
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, MessageHandler, CommandHandler, CallbackQueryHandler, ChatMemberHandler, filters
)
import google.generativeai as genai

# --- LOGGING ---
logging.basicConfig(level=logging.INFO)
# httpx/httpcore, a livello INFO, scrivono nei log l'URL completo di ogni richiesta a Telegram,
# che contiene il TOKEN del bot: li silenziamo (restano visibili solo warning ed errori).
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# --- VARIABILI D'AMBIENTE ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")  # GoatBot
# Token del SECONDO bot (bot Stallone). Se manca, il GoatBot gestisce da solo anche i gruppi dello stallone.
TELEGRAM_TOKEN_BIG = os.environ.get("TELEGRAM_TOKEN_BIG")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
BACKUP_CHAT_ID = os.environ.get("BACKUP_CHAT_ID")  # Es. "-100xxxxxxxxxx"

def _parse_owner_id(raw):
    raw = (raw or "").strip()
    return int(raw) if raw.lstrip("-").isdigit() else None

# ID Telegram numerico del proprietario (l'unico che può approvare i gruppi e usare /admin).
# Per scoprirlo: scrivi /myid al bot in chat privata.
OWNER_ID = _parse_owner_id(os.environ.get("OWNER_ID"))

# Gruppi in cui lavora SOLO il bot Stallone (separati da virgola). Default: il gruppo grande.
DEFAULT_STALLONE_CHAT_ID = "-1001520340812"
STALLONE_CHAT_IDS = {
    x.strip() for x in (os.environ.get("STALLONE_CHAT_IDS") or DEFAULT_STALLONE_CHAT_ID).split(",") if x.strip()
}

# Tipi di aggiornamento richiesti a Telegram (my_chat_member = il bot viene aggiunto/tolto da un gruppo)
ALLOWED_UPDATES = ["message", "callback_query", "my_chat_member"]

# --- RUOLI, MODALITÀ, STATI ---
ROLE_AUTO = "auto"          # bot singolo: modalità scelta in base alla chat
ROLE_GOAT = "goat"          # GoatBot (con due bot)
ROLE_STALLONE = "stallone"  # bot Stallone (con due bot)
MODE_GOAT = "goat"
MODE_STALLONE = "stallone"
GROUP_TYPES = ("group", "supergroup")  # il bot lavora solo nei gruppi (non in privato)

STATUS_APPROVED = "approved"
STATUS_PENDING = "pending"
STATUS_DENIED = "denied"
STATUS_ICONS = {STATUS_APPROVED: "✅", STATUS_PENDING: "⏳", STATUS_DENIED: "⛔"}
STATUS_LABELS = {STATUS_APPROVED: "✅ attivo", STATUS_PENDING: "⏳ in attesa di approvazione", STATUS_DENIED: "⛔ negato"}

# Funzioni attivabili/disattivabili per ogni gruppo dal pannello /admin (tutte attive di default).
FEATURES = {
    "goat": ("🐐 GOAT del giorno", "parola «goat», /goatboard, /resetgoat"),
    "conteggio": ("📊 Conteggio messaggi", "/classificasettimana, /classificaever, /resetmsg"),
    "riassunto": ("📝 Riassunti IA", "/riassunto, /riassuntolungo"),
    "trans": ("🎙️ Trascrizione vocali", "/trans"),
}

# Dopo che il titolo STALLONE è stato assegnato, il messaggio "Già X è il Re Stallone" viene inviato
# al massimo UNA volta ogni tot secondi, così una raffica di "stallone" (es. a mezzanotte)
# non genera decine di risposte identiche.
ALREADY_REPLY_COOLDOWN = 20  # secondi
last_already_reply = {}      # chat_id -> istante dell'ultimo "Già X" inviato

# Protezione dai costi IA: un solo comando IA (riassunti/trascrizioni) ogni tot secondi per chat.
AI_COOLDOWN = 30  # secondi
last_ai_request = {}  # chat_id -> istante dell'ultimo comando IA

# Le modifiche dal pannello /admin fanno partire un backup solo dopo questa pausa senza altre modifiche.
BACKUP_DEBOUNCE = 60  # secondi

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

_backup_timer = None

def request_backup_soon(context):
    """Backup ritardato: più modifiche ravvicinate dal pannello /admin generano un solo backup."""
    global _backup_timer
    if _backup_timer is not None and not _backup_timer.done():
        _backup_timer.cancel()

    async def _later():
        await asyncio.sleep(BACKUP_DEBOUNCE)
        await backup_to_telegram(context)

    _backup_timer = spawn(_later())

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

def feature_enabled(chat_info, key):
    """Una funzione è attiva se non è stata disattivata dal pannello /admin."""
    features = chat_info.get("features") if isinstance(chat_info, dict) else None
    if not isinstance(features, dict):
        return True
    return bool(features.get(key, True))

# --- APPROVAZIONE DEI GRUPPI ---
def chat_status(data, chat_id):
    """
    Stato di un gruppo per il GoatBot:
      - nessuna voce nei dati  -> in attesa (gruppo nuovo, serve l'approvazione del proprietario)
      - voce SENZA campo "approved" -> attivo (gruppo già presente prima dell'introduzione dell'approvazione)
      - "approved" True/False  -> attivo / in attesa (o negato se "denied")
    """
    info = data.get("chats", {}).get(chat_id)
    if not isinstance(info, dict):
        return STATUS_PENDING
    flag = info.get("approved")
    if flag is None or flag:
        return STATUS_APPROVED
    return STATUS_DENIED if info.get("denied") else STATUS_PENDING

# --- RUOLI E MODALITÀ ---
def resolve_mode(role, chat_id):
    """Modalità con cui QUESTO bot tratta la chat: MODE_GOAT, MODE_STALLONE o None (ignorare)."""
    is_stallone_chat = chat_id in STALLONE_CHAT_IDS
    if role == ROLE_STALLONE:
        return MODE_STALLONE if is_stallone_chat else None   # il bot Stallone lavora SOLO nei suoi gruppi
    if is_stallone_chat:
        return MODE_STALLONE if role == ROLE_AUTO else None  # il GoatBot (con due bot) li ignora
    return MODE_GOAT

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
    if old_id in STALLONE_CHAT_IDS:
        STALLONE_CHAT_IDS.discard(old_id)
        STALLONE_CHAT_IDS.add(new_id)
        logging.warning(
            f"Il gruppo dello stallone è diventato un supergruppo: nuovo ID {new_id}. "
            f"Aggiorna STALLONE_CHAT_IDS (ora il bot usa già il nuovo ID fino al riavvio)."
        )
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
def goat_command(feature=None):
    """
    Il comando funziona solo in un gruppo APPROVATO, trattato dal GoatBot in modalità GOAT, e (se indicata)
    con la funzione attiva nel pannello /admin. In ogni altro caso (chat privata, gruppo di backup,
    gruppo in attesa o negato, gruppo dello stallone, funzione spenta) viene IGNORATO in silenzio assoluto.
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(update, context):
            if not update.message or not chat_is_allowed(update):
                return
            chat_id = str(update.effective_chat.id)
            if resolve_mode(get_role(context), chat_id) != MODE_GOAT:
                return
            data = load_local_data()
            if chat_status(data, chat_id) != STATUS_APPROVED:
                return
            if feature and not feature_enabled(data.get("chats", {}).get(chat_id), feature):
                return
            return await func(update, context)
        return wrapper
    return decorator

def stallone_command(func):
    """
    Il comando funziona solo in un gruppo dello stallone trattato da QUESTO bot. In ogni altro caso
    viene IGNORATO in silenzio assoluto (nessuna risposta, nessun messaggio di errore).
    """
    @wraps(func)
    async def wrapper(update, context):
        if not update.message or not chat_is_allowed(update):
            return
        if resolve_mode(get_role(context), str(update.effective_chat.id)) != MODE_STALLONE:
            return
        return await func(update, context)
    return wrapper

# --- RICHIESTA DI ACCESSO (NUOVI GRUPPI) ---
def describe_user(user):
    if user is None:
        return None
    name = escape(getattr(user, "first_name", None) or "?")
    username = getattr(user, "username", None)
    return f"{name} (@{escape(username)})" if username else name

async def notify_owner_request(bot, chat_id, title, adder):
    """Avvisa il proprietario in chat privata, con i bottoni Consenti / Nega."""
    if OWNER_ID is None:
        logging.warning(
            f"Richiesta di accesso del gruppo «{title}» ({chat_id}) ma OWNER_ID non è impostato: "
            f"il bot resta inattivo lì. Scrivi /myid al bot in privato e imposta OWNER_ID."
        )
        return
    who = describe_user(adder)
    if who:
        intro = f"👤 {who} ha aggiunto il bot al gruppo «{escape(title or chat_id)}»"
    else:
        intro = f"Il bot è stato aggiunto al gruppo «{escape(title or chat_id)}» (non so da chi)"
    text = (
        "🔔 <b>Richiesta di accesso</b>\n\n"
        f"{intro}\n"
        f"🆔 <code>{chat_id}</code>\n\n"
        "Finché non decidi, il bot resta inattivo in quel gruppo."
    )
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Consenti", callback_data=f"a:{chat_id}"),
        InlineKeyboardButton("⛔ Nega", callback_data=f"d:{chat_id}"),
    ]])
    try:
        await bot.send_message(chat_id=OWNER_ID, text=text, reply_markup=markup, parse_mode='HTML')
    except Exception as e:
        logging.error(
            f"Impossibile avvisare il proprietario ({e}). Ha premuto /start sul bot in chat privata? "
            f"Il gruppo «{title}» ({chat_id}) resta in attesa: lo trovi in /admin."
        )

async def request_access(context, chat, adder):
    """Registra il gruppo come 'in attesa' e chiede l'approvazione al proprietario."""
    chat_id = str(chat.id)
    data = load_local_data()
    info = ensure_chat(data, chat_id)
    info["approved"] = False
    info["denied"] = False
    title = getattr(chat, "title", None)
    if title:
        info["title"] = title
    if adder is not None:
        info["added_by"] = {
            "id": getattr(adder, "id", None),
            "name": getattr(adder, "first_name", None),
            "username": getattr(adder, "username", None),
        }
    save_local_data(data)
    await notify_owner_request(context.bot, chat_id, title, adder)

async def handle_my_chat_member(update, context):
    """Il bot è stato aggiunto a (o tolto da) un gruppo."""
    change = getattr(update, "my_chat_member", None)
    if change is None:
        return
    chat = change.chat
    if chat.type not in GROUP_TYPES:
        return
    chat_id = str(chat.id)
    # Il bot Stallone non ha approvazioni; i gruppi dello stallone e di backup non passano da qui
    if get_role(context) == ROLE_STALLONE or chat_id in STALLONE_CHAT_IDS or is_backup_chat(chat_id):
        return

    in_states = ("member", "administrator")
    was_in = change.old_chat_member.status in in_states
    is_in = change.new_chat_member.status in in_states

    if is_in and not was_in:
        if chat_status(load_local_data(), chat_id) == STATUS_APPROVED:
            return  # gruppo già approvato (o già presente): riparte senza chiedere
        await request_access(context, chat, change.from_user)
    elif was_in and not is_in:
        logging.info(f"Il bot non è più nel gruppo «{getattr(chat, 'title', None)}» ({chat_id}).")

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
    mode = resolve_mode(get_role(context), chat_id)
    if mode is None:
        return

    data = load_local_data()

    if mode == MODE_GOAT:
        if chat_status(data, chat_id) != STATUS_APPROVED:
            # Gruppo non approvato: silenzio. Se non ho mai visto l'ingresso del bot (ad esempio il bot
            # era spento quando è stato aggiunto), registro ora la richiesta e avviso il proprietario.
            if chat_id not in data.get("chats", {}):
                await request_access(context, update.effective_chat, None)
            return
        log_chat_once(update, mode)
        await handle_goat_message(update, context, data)
    else:
        log_chat_once(update, mode)
        await handle_stallone_message(update, context, data)

# --- MODALITÀ GOAT: GOAT DEL GIORNO + CONTEGGIO MESSAGGI (dati della SOLA chat corrente) ---
async def handle_goat_message(update, context, data):
    user = update.message.from_user
    chat = update.effective_chat
    chat_id = str(chat.id)

    text_content = (update.message.text or update.message.caption or "").strip()
    display_name = user.first_name

    chat_info = ensure_goat_section(data, chat_id)
    dirty = False

    # Tiene aggiornato il titolo del gruppo (serve al pannello /admin)
    title = getattr(chat, "title", None)
    if title and chat_info.get("title") != title:
        chat_info["title"] = title
        dirty = True

    # 1. Salva messaggio per il riassunto di QUESTA chat (solo se contiene testo e i riassunti sono attivi)
    if text_content and feature_enabled(chat_info, "riassunto"):
        history = chat_history.get(chat_id)
        if history is None:
            history = chat_history[chat_id] = deque(maxlen=HISTORY_MAX)
        history.append(f"{display_name}: {text_content}")

    # 2. Registra conteggio per QUALSIASI messaggio (Testo, Foto, GIF, Sticker, Vocali, ecc.)
    if feature_enabled(chat_info, "conteggio"):
        chat_info["msg_weekly"][display_name] = chat_info["msg_weekly"].get(display_name, 0) + 1
        chat_info["msg_ever"][display_name] = chat_info["msg_ever"].get(display_name, 0) + 1
        dirty = True

    if dirty:
        save_local_data(data)

    # 3. Logica GOAT (solo se il contenuto testuale è la parola "goat")
    if feature_enabled(chat_info, "goat") and text_content.lower() == "goat":
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

    # Solo il TESTO del messaggio (niente didascalie di foto/video) e solo se è
    # esattamente la parola "stallone" (maiuscole/minuscole e spazi ai bordi ininfluenti).
    # Qualsiasi altra cosa -> il bot non risponde affatto.
    text_content = (update.message.text or "").strip()

    # Nessun salvataggio di messaggi, nessun conteggio, nessuna cronologia.
    if text_content.lower() != "stallone":
        return

    chat_info = ensure_stallone_section(data, chat_id)
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
    chat_info = ensure_stallone_section(load_local_data(), chat_id)

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

# --- COMANDI RESET (SOLO ADMIN DEL GRUPPO, SOLO DATI DELLA CHAT CORRENTE) ---
@goat_command("goat")
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

@goat_command("conteggio")
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

# --- COMANDO LISTA COMANDI (mostra solo le funzioni attive nel gruppo) ---
@goat_command()
async def show_commands(update, context):
    chat_id = str(update.effective_chat.id)
    info = ensure_goat_section(load_local_data(), chat_id)

    def on(key):
        return feature_enabled(info, key)

    lines = ["🤖 <b>LISTA COMANDI DI GOAT BOT</b> 🐐\n"]
    if on("goat"):
        lines.append("👑 <b>/goatboard</b> - Mostra la classifica storica delle vittorie GOAT giornaliere.")
    if on("conteggio"):
        lines.append("📊 <b>/classificasettimana</b> - Mostra la classifica dei messaggi scritti questa settimana.")
        lines.append("👑 <b>/classificaever</b> - Mostra la classifica generale dei messaggi di sempre.")
    if on("riassunto"):
        lines.append("📝 <b>/riassunto</b> - Genera un riassunto breve degli ultimi messaggi tramite IA.")
        lines.append("📜 <b>/riassuntolungo</b> - Genera un riassunto esteso e dettagliato tramite IA.")
    if on("trans"):
        lines.append("🎙️ <b>/trans</b> - Rispondi a un messaggio vocale per ottenerne la trascrizione testuale.")
    lines.append("ℹ️ <b>/goatcomm</b> - Mostra questo pannello con tutti i comandi.")
    if on("goat") or on("conteggio"):
        lines.append("")
        if on("goat"):
            lines.append("🧹 <b>/resetgoat</b> - Azzera la classifica GOAT del gruppo (solo admin).")
        if on("conteggio"):
            lines.append("🧹 <b>/resetmsg</b> - Azzera le classifiche dei messaggi (solo admin).")
    if on("goat"):
        lines.append("\n💡 <i>Curiosità: Scrivi semplicemente <b>goat</b> in chat per eleggere il GOAT del giorno!</i>")
    await update.message.reply_text("\n".join(lines), parse_mode='HTML')

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

@goat_command("conteggio")
async def show_weekly_ranking(update, context):
    chat_id = str(update.effective_chat.id)
    chat_info = ensure_goat_section(load_local_data(), chat_id)

    text = generate_ranking_text("📊 <b>CLASSIFICA SETTIMANALE MESSAGGI</b> 📊", chat_info["msg_weekly"])
    await update.message.reply_text(text, parse_mode='HTML')

@goat_command("conteggio")
async def show_ever_ranking(update, context):
    chat_id = str(update.effective_chat.id)
    chat_info = ensure_goat_section(load_local_data(), chat_id)

    text = generate_ranking_text("👑 <b>CLASSIFICA GENERALE MESSAGGI (EVER)</b> 👑", chat_info["msg_ever"])
    await update.message.reply_text(text, parse_mode='HTML')

@goat_command("goat")
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
        # Solo gruppi GOAT approvati con il conteggio attivo; mai gruppi dello stallone o di backup
        if not isinstance(chat_info, dict) or chat_id in STALLONE_CHAT_IDS or is_backup_chat(chat_id):
            continue
        if chat_status(snapshot, chat_id) != STATUS_APPROVED or not feature_enabled(chat_info, "conteggio"):
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

@goat_command("riassunto")
async def make_summary(update, context):
    await start_summary(update, limit=75, title="RIASSUNTO BREVE")

@goat_command("riassunto")
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
        logging.error(f"Errore riassunto (chat {chat_id}): {e}")
        await status_msg.edit_text(f"❌ Errore riscontrato: {str(e)}")

# --- COMANDO TRASCRIZIONE AUDIO ---
@goat_command("trans")
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

# --- CHAT PRIVATA COL PROPRIETARIO: /myid, /start, /admin ---
def _private_user(update):
    """Utente che scrive in chat privata (None se il messaggio non è in privato)."""
    chat = update.effective_chat
    msg = update.message
    if chat is None or msg is None or chat.type != "private" or msg.from_user is None:
        return None
    return msg.from_user

async def myid_command(update, context):
    """Mostra a chiunque il proprio ID Telegram (serve per impostare OWNER_ID)."""
    user = _private_user(update)
    if user is None:
        return
    await update.message.reply_text(f"🆔 Il tuo ID Telegram è <code>{user.id}</code>", parse_mode='HTML')

async def start_command(update, context):
    user = _private_user(update)
    if user is None or OWNER_ID is None or user.id != OWNER_ID:
        return
    await update.message.reply_text("👋 Ciao! Usa /admin per gestire i gruppi in cui è il bot.")

def managed_chats(data):
    """Gruppi gestiti dal GoatBot (esclusi quelli dello stallone e il gruppo di backup), per titolo."""
    result = []
    for chat_id, info in data.get("chats", {}).items():
        if not isinstance(info, dict) or chat_id in STALLONE_CHAT_IDS or is_backup_chat(chat_id):
            continue
        if info.get("merged_into"):
            continue  # doppione di un gruppo già migrato: non va mostrato
        result.append((chat_id, info))
    result.sort(key=lambda item: (item[1].get("title") or item[0]).lower())
    return result

def build_list_panel(data):
    rows = []
    chats = managed_chats(data)
    names = [info.get("title") for _, info in chats if info.get("title")]
    for chat_id, info in chats:
        icon = STATUS_ICONS[chat_status(data, chat_id)]
        name = info.get("title") or chat_id
        if info.get("title") and names.count(info["title"]) > 1:
            name += f" (…{chat_id[-4:]})"  # due gruppi con lo stesso nome: distinguo con le ultime cifre dell'ID
        label = f"{icon} {name}" + (" 💤" if info.get("unreachable") else "")
        rows.append([InlineKeyboardButton(label[:60], callback_data=f"g:{chat_id}")])
    text = (
        "🛠 <b>Pannello admin</b>\n\n"
        "Scegli un gruppo da gestire.\n"
        "✅ attivo · ⏳ in attesa · ⛔ negato · 💤 bot non più nel gruppo"
    )
    if not rows:
        text += "\n\n<i>Il bot non è ancora in nessun gruppo.</i>"
    return text, InlineKeyboardMarkup(rows)

def build_group_panel(data, chat_id):
    info = data.get("chats", {}).get(chat_id)
    back = [InlineKeyboardButton("⬅️ Indietro", callback_data="l")]
    if not isinstance(info, dict):
        return "Gruppo non trovato.", InlineKeyboardMarkup([back])

    status = chat_status(data, chat_id)
    title = escape(info.get("title") or chat_id)
    text = f"⚙️ <b>{title}</b>\n🆔 <code>{chat_id}</code>\nStato: {STATUS_LABELS[status]}\n"
    if info.get("unreachable"):
        text += "💤 Il bot non risulta più in questo gruppo (o il gruppo non esiste più).\n"
    rows = []

    if status == STATUS_APPROVED:
        text += "\nAttiva o disattiva le funzioni di questo gruppo (quelle spente restano in silenzio):\n"
        for key, (label, commands) in FEATURES.items():
            enabled = feature_enabled(info, key)
            text += f"\n• {label} — <i>{escape(commands)}</i>"
            rows.append([InlineKeyboardButton(f"{'✅' if enabled else '❌'} {label}", callback_data=f"t:{chat_id}:{key}")])
        rows.append([InlineKeyboardButton("⏸ Sospendi (resta ma inattivo)", callback_data=f"s:{chat_id}")])
        rows.append([InlineKeyboardButton("⛔ Revoca accesso (il bot esce)", callback_data=f"x:{chat_id}")])
    elif status == STATUS_PENDING:
        text += "\nIl bot è inattivo in questo gruppo finché non decidi."
        rows.append([
            InlineKeyboardButton("✅ Consenti", callback_data=f"a:{chat_id}"),
            InlineKeyboardButton("⛔ Nega", callback_data=f"d:{chat_id}"),
        ])
    else:
        text += (
            "\nAccesso revocato: il bot è uscito (o dovrebbe esserlo). Per riattivarlo aggiungilo di nuovo al gruppo: "
            "ti arriverà una nuova richiesta. Se è ancora nel gruppo, puoi riconsentire da qui."
        )
        rows.append([InlineKeyboardButton("✅ Consenti di nuovo", callback_data=f"a:{chat_id}")])
    rows.append(back)
    return text, InlineKeyboardMarkup(rows)

async def refresh_titles(context, data):
    """
    Aggiorna l'elenco dei gruppi interrogando Telegram: nomi mancanti o cambiati, gruppi diventati
    supergruppi (i dati passano al nuovo ID, niente doppioni) e gruppi dove il bot non c'è più (💤).
    True se qualcosa è cambiato.
    """
    changed = False
    for chat_id, info in managed_chats(data):
        try:
            chat = await context.bot.get_chat(chat_id=int(chat_id))
            title = getattr(chat, "title", None)
            if title and info.get("title") != title:
                info["title"] = title
                changed = True
            if info.pop("unreachable", None):
                changed = True
        except Exception as e:
            new_id = getattr(e, "new_chat_id", None)
            if new_id:
                new_id = str(new_id)
                if migrate_chat_data(data, chat_id, new_id):
                    logging.info(f"Gruppo {chat_id} diventato supergruppo {new_id}: dati spostati.")
                else:
                    # il nuovo ID ha già i suoi dati: il vecchio è un doppione, lo nascondo dall'elenco
                    info["merged_into"] = new_id
                changed = True
            elif type(e).__name__ in ("Forbidden", "BadRequest"):
                # il bot non è più nel gruppo (o il gruppo non esiste più)
                if not info.get("unreachable"):
                    info["unreachable"] = True
                    changed = True
            else:
                logging.info(f"Gruppo {chat_id}: verifica non riuscita ({e})")
        await asyncio.sleep(0.05)
    if changed:
        save_local_data(data)
    return changed

async def admin_command(update, context):
    """Pannello /admin: solo il proprietario, solo in chat privata."""
    user = _private_user(update)
    if user is None:
        return
    if OWNER_ID is None:
        await update.message.reply_text(
            f"⚠️ La variabile OWNER_ID non è impostata. Il tuo ID Telegram è <code>{user.id}</code>: "
            "impostalo come OWNER_ID e riavvia il bot.",
            parse_mode='HTML'
        )
        return
    if user.id != OWNER_ID:
        return  # silenzio: per gli altri il comando non esiste
    data = load_local_data()
    await refresh_titles(context, data)
    text, markup = build_list_panel(data)
    await update.message.reply_text(text, reply_markup=markup, parse_mode='HTML')

async def safe_edit(query, text, markup=None):
    try:
        await query.edit_message_text(text, reply_markup=markup, parse_mode='HTML')
    except Exception as e:  # ad esempio "message is not modified"
        logging.info(f"Modifica del messaggio ignorata: {e}")

async def leave_group(context, chat_id):
    try:
        await context.bot.leave_chat(chat_id=int(chat_id))
        return True
    except Exception as e:
        logging.error(f"Impossibile uscire dal gruppo {chat_id}: {e}")
        return False

async def handle_callback(update, context):
    """Bottoni inline (approvazioni e pannello /admin): li accetta SOLO dal proprietario."""
    query = getattr(update, "callback_query", None)
    if query is None:
        return
    user = query.from_user
    if OWNER_ID is None or user is None or user.id != OWNER_ID:
        try:
            await query.answer()
        except Exception:
            pass
        return
    await query.answer()

    parts = (query.data or "").split(":")
    action = parts[0]
    chat_id = parts[1] if len(parts) > 1 else None
    data = load_local_data()

    if action == "l":
        await refresh_titles(context, data)
        text, markup = build_list_panel(data)
        await safe_edit(query, text, markup)
        return

    # Tutte le altre azioni riguardano un gruppo gestito dal GoatBot
    if chat_id not in dict(managed_chats(data)):
        text, markup = build_list_panel(data)
        await safe_edit(query, "Gruppo non trovato.\n\n" + text, markup)
        return
    info = data["chats"][chat_id]
    title = escape(info.get("title") or chat_id)

    if action == "g":
        text, markup = build_group_panel(data, chat_id)
        await safe_edit(query, text, markup)

    elif action == "t" and len(parts) == 3 and parts[2] in FEATURES:
        features = info.setdefault("features", {})
        features[parts[2]] = not feature_enabled(info, parts[2])
        save_local_data(data)
        request_backup_soon(context)
        text, markup = build_group_panel(data, chat_id)
        await safe_edit(query, text, markup)

    elif action == "a":  # consenti
        info["approved"] = True
        info["denied"] = False
        save_local_data(data)
        try:
            await context.bot.send_message(
                chat_id=int(chat_id),
                text="✅ GOAT Bot attivato in questo gruppo! Scrivi /goatcomm per vedere i comandi."
            )
        except Exception as e:
            logging.error(f"Impossibile scrivere nel gruppo {chat_id} dopo l'approvazione: {e}")
        text, markup = build_group_panel(data, chat_id)
        await safe_edit(query, f"✅ Accesso consentito a «{title}».\n\n" + text, markup)
        spawn(backup_to_telegram(context))

    elif action == "d":  # nega (e esci)
        info["approved"] = False
        info["denied"] = True
        save_local_data(data)
        left = await leave_group(context, chat_id)
        _, markup = build_group_panel(data, chat_id)
        note = "sono uscito dal gruppo" if left else "non sono riuscito a uscire dal gruppo (puoi toglierlo a mano)"
        await safe_edit(query, f"⛔ Accesso negato a «{title}»: {note}.", markup)
        spawn(backup_to_telegram(context))

    elif action == "s":  # sospendi: resta nel gruppo ma torna "in attesa"
        info["approved"] = False
        info["denied"] = False
        save_local_data(data)
        text, markup = build_group_panel(data, chat_id)
        await safe_edit(query, f"⏸ «{title}» sospeso: il bot resta nel gruppo ma è inattivo.\n\n" + text, markup)
        spawn(backup_to_telegram(context))

    elif action == "x":  # conferma revoca
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("⛔ Sì, revoca", callback_data=f"xy:{chat_id}"),
            InlineKeyboardButton("↩️ Annulla", callback_data=f"g:{chat_id}"),
        ]])
        await safe_edit(query, f"Vuoi davvero revocare l'accesso a «{title}»?\nIl bot esce dal gruppo. I dati (classifiche, conteggi) restano salvati.", markup)

    elif action == "xy":  # uscita confermata
        info["approved"] = False
        info["denied"] = True
        save_local_data(data)
        left = await leave_group(context, chat_id)
        _, markup = build_group_panel(data, chat_id)
        note = "sono uscito dal gruppo" if left else "non sono riuscito a uscire dal gruppo (puoi toglierlo a mano)"
        await safe_edit(query, f"⛔ Accesso revocato a «{title}»: {note}.", markup)
        spawn(backup_to_telegram(context))

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
        # Chat privata col proprietario + approvazione dei nuovi gruppi
        application.add_handler(CommandHandler("admin", admin_command))
        application.add_handler(CommandHandler("myid", myid_command))
        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(CallbackQueryHandler(handle_callback))
        application.add_handler(ChatMemberHandler(handle_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
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
            await a.updater.start_polling(allowed_updates=ALLOWED_UPDATES)

        print("GOAT Bot attivo! (2 bot: GoatBot + bot Stallone)", flush=True)
        if OWNER_ID is None:
            print("ATTENZIONE: OWNER_ID non impostato: i nuovi gruppi non possono essere approvati. "
                  "Scrivi /myid al GoatBot in privato e imposta OWNER_ID.", flush=True)
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

    print("GOAT Bot attivo! (bot singolo)", flush=True)
    if OWNER_ID is None:
        print("ATTENZIONE: OWNER_ID non impostato: i nuovi gruppi non possono essere approvati. "
              "Scrivi /myid al bot in privato e imposta OWNER_ID.", flush=True)
    application.run_polling(allowed_updates=ALLOWED_UPDATES)

if __name__ == '__main__':
    main()
