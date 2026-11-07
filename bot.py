#!/usr/bin/env python3
# Telegram Appeal Bot
# Copyright (C) 2026  Leproide
# Author: https://github.com/Leproide
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""
Bot Telegram per gli appelli di sban.

- Gli utenti scrivono al bot in privato (/start) e compilano un appello.
  L'account (username/ID) viene preso automaticamente: non si può appellare per altri.
- Un solo appello in attesa per utente (vincolo anche a livello DB).
- Gli appelli arrivano nel gruppo admin con pulsanti Approva / Nega / Nega e blocca.
- Gli utenti bloccati non possono più inviare appelli finché non vengono sbloccati.
"""

import html
import logging
import os
import re
import sqlite3
import time
from contextlib import closing

from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    ReplyParameters,
    Update,
)
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# --------------------------------------------------------------------------
# Configurazione (da variabili d'ambiente / file .env)
# --------------------------------------------------------------------------
# Il .env viene cercato accanto a bot.py, indipendentemente dalla cartella di avvio
ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(ENV_FILE)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise SystemExit(
        f"BOT_TOKEN non trovato. Controlla che esista il file {ENV_FILE} "
        "(nome esatto '.env', col punto) e che contenga BOT_TOKEN=..."
    )
# Gruppo amministratori: se assente il bot parte in "modalità setup" (solo /chatid)
ADMIN_CHAT_ID = int(os.environ["ADMIN_CHAT_ID"]) if os.environ.get("ADMIN_CHAT_ID") else None
MAIN_CHAT_ID = int(os.environ["MAIN_CHAT_ID"]) if os.environ.get("MAIN_CHAT_ID") else None
AUTO_UNBAN = os.environ.get("AUTO_UNBAN", "false").lower() in ("1", "true", "yes")
INVITE_LINK = os.environ.get("INVITE_LINK", "").strip()
REQUIRE_CHAT_ADMIN = os.environ.get("REQUIRE_CHAT_ADMIN", "false").lower() in ("1", "true", "yes")
DB_PATH = os.environ.get("DB_PATH", "appeals.db")

# Lunghezza massima dei campi (evita messaggi oltre il limite di 4096 caratteri)
LIMITS = {"nickname": 100, "ban_datetime": 100, "cause": 500, "comment": 1500, "reason": 1000}

BTN_SEND = "✅ Invia appello"
BTN_CANCEL = "❌ Annulla"

BANDATE, CAUSE, COMMENT, CONFIRM = range(4)

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("appeal-bot")


# --------------------------------------------------------------------------
# Utility
# --------------------------------------------------------------------------
def now_ts() -> int:
    return int(time.time())


def fmt_ts(ts: int) -> str:
    return time.strftime("%d/%m/%Y %H:%M", time.localtime(ts))


def esc(s) -> str:
    return html.escape(str(s)) if s is not None else ""


def user_label(user_id: int, username, full_name) -> str:
    """Etichetta HTML: @username se presente, altrimenti menzione col nome."""
    if username:
        return f"@{esc(username)}"
    return f'<a href="tg://user?id={user_id}">{esc(full_name or user_id)}</a>'


def user_plain(username, full_name) -> str:
    """Etichetta testuale salvata nel DB (campo nickname)."""
    return f"@{username}" if username else (full_name or "")


# --------------------------------------------------------------------------
# Database (SQLite, una connessione per operazione: semplice e sicuro)
# --------------------------------------------------------------------------
def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def db_init() -> None:
    with closing(db_connect()) as conn, conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS appeals (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id       INTEGER NOT NULL,
                username      TEXT,
                full_name     TEXT,
                nickname      TEXT NOT NULL,
                ban_datetime  TEXT NOT NULL,
                cause         TEXT,
                comment       TEXT NOT NULL,
                status        TEXT NOT NULL DEFAULT 'pending',  -- pending|approved|denied
                created_at    INTEGER NOT NULL,
                decided_at    INTEGER,
                decided_by    TEXT,
                admin_msg_id  INTEGER,
                reason        TEXT,  -- motivo del diniego (opzionale)
                log           TEXT   -- storico decisioni mostrato nel gruppo admin
            );
            -- Un solo appello 'pending' per utente, garantito dal DB
            CREATE UNIQUE INDEX IF NOT EXISTS one_pending_per_user
                ON appeals(user_id) WHERE status = 'pending';
            CREATE TABLE IF NOT EXISTS blocked (
                user_id     INTEGER PRIMARY KEY,
                blocked_at  INTEGER NOT NULL,
                blocked_by  TEXT
            );
            """
        )
        # Migrazione da versioni precedenti: aggiunge le colonne mancanti
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(appeals)")}
        for col in ("reason", "log"):
            if col not in cols:
                conn.execute(f"ALTER TABLE appeals ADD COLUMN {col} TEXT")


def db_one(sql: str, params=()):
    with closing(db_connect()) as conn:
        return conn.execute(sql, params).fetchone()


def db_all(sql: str, params=()):
    with closing(db_connect()) as conn:
        return conn.execute(sql, params).fetchall()


def db_exec(sql: str, params=()) -> tuple[int, int]:
    """Esegue una scrittura; ritorna (rowcount, lastrowid)."""
    with closing(db_connect()) as conn, conn:
        cur = conn.execute(sql, params)
        return cur.rowcount, cur.lastrowid


def is_blocked(user_id: int) -> bool:
    return db_one("SELECT 1 FROM blocked WHERE user_id = ?", (user_id,)) is not None


def get_pending(user_id: int):
    return db_one(
        "SELECT * FROM appeals WHERE user_id = ? AND status = 'pending'", (user_id,)
    )


def get_last_appeal(user_id: int):
    return db_one(
        "SELECT * FROM appeals WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user_id,)
    )


def get_appeal(appeal_id: int):
    return db_one("SELECT * FROM appeals WHERE id = ?", (appeal_id,))


def create_appeal(user, data: dict) -> int:
    """Inserisce l'appello. Solleva sqlite3.IntegrityError se ne esiste già uno pending."""
    _, rowid = db_exec(
        """INSERT INTO appeals
           (user_id, username, full_name, nickname, ban_datetime, cause, comment, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            user.id,
            user.username,
            user.full_name,
            data["nickname"],
            data["ban_datetime"],
            data.get("cause"),
            data["comment"],
            now_ts(),
        ),
    )
    return rowid


def delete_appeal(appeal_id: int) -> None:
    db_exec("DELETE FROM appeals WHERE id = ?", (appeal_id,))


def set_admin_msg(appeal_id: int, msg_id: int) -> None:
    db_exec("UPDATE appeals SET admin_msg_id = ? WHERE id = ?", (msg_id, appeal_id))


def decide(appeal_id: int, status: str, by: str, reason=None) -> bool:
    """Chiude l'appello solo se ancora pending (evita doppi click). True se aggiornato."""
    count, _ = db_exec(
        """UPDATE appeals SET status = ?, decided_at = ?, decided_by = ?, reason = ?
           WHERE id = ? AND status = 'pending'""",
        (status, now_ts(), by, reason, appeal_id),
    )
    return count > 0


def append_log(appeal_id: int, line: str) -> None:
    """Aggiunge una riga allo storico decisioni dell'appello."""
    db_exec(
        "UPDATE appeals SET log = COALESCE(log || char(10) || char(10), '') || ? WHERE id = ?",
        (line, appeal_id),
    )


def block_user(user_id: int, by: str) -> None:
    db_exec(
        "INSERT OR IGNORE INTO blocked (user_id, blocked_at, blocked_by) VALUES (?, ?, ?)",
        (user_id, now_ts(), by),
    )


def unblock_user(user_id: int) -> bool:
    count, _ = db_exec("DELETE FROM blocked WHERE user_id = ?", (user_id,))
    return count > 0


def previous_stats(user_id: int, exclude_id: int) -> tuple[int, int]:
    row = db_one(
        """SELECT COUNT(*) AS total,
                  COALESCE(SUM(status = 'denied'), 0) AS denied
           FROM appeals WHERE user_id = ? AND id < ?""",
        (user_id, exclude_id),
    )
    return row["total"], row["denied"]


# --------------------------------------------------------------------------
# Testi e tastiere
# --------------------------------------------------------------------------
def build_admin_text(appeal) -> str:
    total, denied = previous_stats(appeal["user_id"], appeal["id"])
    uname = f" (@{esc(appeal['username'])})" if appeal["username"] else ""
    cause = esc(appeal["cause"]) if appeal["cause"] else "<i>non indicata</i>"
    return (
        f"📨 <b>Appello #{appeal['id']}</b> — {fmt_ts(appeal['created_at'])}\n\n"
        f"👤 Utente: <a href=\"tg://user?id={appeal['user_id']}\">{esc(appeal['full_name'])}</a>"
        f"{uname}\n"
        f"🆔 ID: <code>{appeal['user_id']}</code>\n"
        f"🕒 Data/ora ban: {esc(appeal['ban_datetime'])}\n"
        f"⚠️ Causa: {cause}\n"
        f"💬 Commento:\n{esc(appeal['comment'])}\n\n"
        f"📊 Appelli precedenti: {total} (negati: {denied})"
        + (f"\n\n{appeal['log']}" if appeal["log"] else "")
    )


def kb_pending(appeal_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Approva", callback_data=f"a:ok:{appeal_id}"),
                InlineKeyboardButton("❌ Nega", callback_data=f"a:no:{appeal_id}"),
            ],
            [InlineKeyboardButton("🚫 Nega e blocca appelli", callback_data=f"a:blk:{appeal_id}")],
        ]
    )


def kb_block(appeal_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🚫 Blocca appelli utente", callback_data=f"a:blk:{appeal_id}")]]
    )


def kb_unblock(appeal_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔓 Sblocca appelli utente", callback_data=f"a:unb:{appeal_id}")]]
    )


def confirm_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[BTN_SEND, BTN_CANCEL]], resize_keyboard=True, one_time_keyboard=True
    )


async def notify_user(context: ContextTypes.DEFAULT_TYPE, user_id: int, text: str) -> bool:
    """Invia un messaggio all'utente; False se non raggiungibile (es. ha bloccato il bot)."""
    try:
        await context.bot.send_message(
            user_id, text, parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW
        )
        return True
    except TelegramError as e:
        log.warning("Impossibile notificare %s: %s", user_id, e)
        return False


# --------------------------------------------------------------------------
# Conversazione utente (chat privata)
# --------------------------------------------------------------------------
async def read_field(update: Update, field: str):
    """Valida un campo testuale; None se non valido (l'utente resta nello stesso step)."""
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("Il campo non può essere vuoto, riprova.")
        return None
    if len(text) > LIMITS[field]:
        await update.message.reply_text(
            f"Testo troppo lungo (massimo {LIMITS[field]} caratteri), riprova."
        )
        return None
    return text


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    if is_blocked(uid):
        await update.message.reply_text(
            "Non puoi più inviare appelli.", reply_markup=ReplyKeyboardRemove()
        )
        return ConversationHandler.END

    pending = get_pending(uid)
    if pending:
        await update.message.reply_text(
            f"Hai già un appello in attesa (#{pending['id']} del {fmt_ts(pending['created_at'])}).\n"
            "Attendi la decisione degli amministratori prima di inviarne un altro.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ConversationHandler.END

    user = update.effective_user
    context.user_data.clear()
    # L'account viene rilevato da Telegram: non è modificabile dall'utente
    context.user_data["nickname"] = user_plain(user.username, user.full_name)
    no_username = "" if user.username else " (nessun username impostato)"
    await update.message.reply_text(
        "Ciao! Qui puoi inviare un appello per lo sban dal gruppo.\n\n"
        f"👤 Account: {user_label(user.id, user.username, user.full_name)}{no_username}\n"
        f"🆔 ID: <code>{user.id}</code>\n"
        "L'appello vale solo per questo account Telegram.\n"
        "Puoi annullare in qualsiasi momento con /annulla.\n\n"
        "Passo 1/3 — Data e ora indicativa del ban (es. 05/11/2026 sera).",
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardRemove(),
    )
    return BANDATE


async def got_bandate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = await read_field(update, "ban_datetime")
    if value is None:
        return BANDATE
    context.user_data["ban_datetime"] = value
    await update.message.reply_text(
        "Passo 2/3 — Causa del ban (opzionale).\nScrivila oppure invia /salta."
    )
    return CAUSE


async def got_cause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = await read_field(update, "cause")
    if value is None:
        return CAUSE
    context.user_data["cause"] = value
    return await ask_comment(update)


async def skip_cause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["cause"] = None
    return await ask_comment(update)


async def ask_comment(update: Update) -> int:
    await update.message.reply_text(
        "Passo 3/3 — Commento libero: spiega perché dovresti essere sbannato."
    )
    return COMMENT


async def got_comment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = await read_field(update, "comment")
    if value is None:
        return COMMENT
    ud = context.user_data
    ud["comment"] = value
    cause = esc(ud["cause"]) if ud.get("cause") else "<i>non indicata</i>"
    await update.message.reply_text(
        "<b>Riepilogo appello</b>\n\n"
        f"👤 Account: {user_label(update.effective_user.id, update.effective_user.username, update.effective_user.full_name)}\n"
        f"🕒 Data/ora ban: {esc(ud['ban_datetime'])}\n"
        f"⚠️ Causa: {cause}\n"
        f"💬 Commento:\n{esc(ud['comment'])}\n\n"
        "Confermi l'invio? Potrai inviare un solo appello alla volta.",
        parse_mode=ParseMode.HTML,
        reply_markup=confirm_keyboard(),
        link_preview_options=NO_PREVIEW,
    )
    return CONFIRM


async def send_appeal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    ud = context.user_data

    # Ricontrollo: lo stato può essere cambiato durante la compilazione
    if is_blocked(user.id):
        await update.message.reply_text(
            "Non puoi più inviare appelli.", reply_markup=ReplyKeyboardRemove()
        )
        return ConversationHandler.END
    if not all(k in ud for k in ("nickname", "ban_datetime", "comment")):
        await update.message.reply_text(
            "Sessione scaduta, ricomincia con /start.", reply_markup=ReplyKeyboardRemove()
        )
        return ConversationHandler.END

    try:
        appeal_id = create_appeal(user, ud)
    except sqlite3.IntegrityError:
        await update.message.reply_text(
            "Hai già un appello in attesa.", reply_markup=ReplyKeyboardRemove()
        )
        return ConversationHandler.END

    appeal = get_appeal(appeal_id)
    try:
        msg = await context.bot.send_message(
            ADMIN_CHAT_ID,
            build_admin_text(appeal),
            parse_mode=ParseMode.HTML,
            reply_markup=kb_pending(appeal_id),
            link_preview_options=NO_PREVIEW,
        )
    except TelegramError as e:
        # Se non riesco a recapitarlo agli admin, annullo per non bloccare l'utente
        log.error("Invio al gruppo admin fallito: %s", e)
        delete_appeal(appeal_id)
        await update.message.reply_text(
            "Errore nell'invio dell'appello, riprova più tardi.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ConversationHandler.END

    set_admin_msg(appeal_id, msg.message_id)
    ud.clear()
    await update.message.reply_text(
        f"Appello #{appeal_id} inviato agli amministratori. "
        "Riceverai qui la risposta. Puoi controllare lo stato con /stato.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(
        "Appello annullato. Puoi ricominciare con /start.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


async def confirm_other(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Usa i pulsanti per confermare o annullare.", reply_markup=confirm_keyboard()
    )
    return CONFIRM


async def need_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Invia solo testo, per favore.")
    # Nessun return: lo stato della conversazione resta invariato


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if is_blocked(uid):
        await update.message.reply_text("Non puoi più inviare appelli.")
        return
    last = get_last_appeal(uid)
    if not last:
        await update.message.reply_text("Nessun appello inviato. Usa /start per inviarne uno.")
        return
    labels = {"pending": "⏳ in attesa", "approved": "✅ approvato", "denied": "❌ negato"}
    text = f"Ultimo appello #{last['id']} del {fmt_ts(last['created_at'])}: {labels.get(last['status'], last['status'])}."
    if last["status"] != "pending":
        text += "\nPuoi inviarne uno nuovo con /start."
    await update.message.reply_text(text)


async def private_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Usa /start per inviare un appello di sban, /stato per vederne lo stato.",
        reply_markup=ReplyKeyboardRemove(),
    )


# --------------------------------------------------------------------------
# Sban nel gruppo pubblico
# --------------------------------------------------------------------------
async def do_unban(context: ContextTypes.DEFAULT_TYPE, appeal) -> str:
    """
    Verifica che l'utente sia nella lista dei bannati del gruppo pubblico:
    - se bannato: sban + annuncio "Utente riammesso" nel gruppo pubblico;
    - se non bannato: nessuna azione + avviso nel gruppo admin.
    Ritorna la nota da aggiungere alla riga di stato dell'appello.
    """
    uid = appeal["user_id"]
    reply = (
        ReplyParameters(message_id=appeal["admin_msg_id"], allow_sending_without_reply=True)
        if appeal["admin_msg_id"]
        else None
    )
    try:
        member = await context.bot.get_chat_member(MAIN_CHAT_ID, uid)
    except TelegramError as e:
        return f" — ⚠️ verifica stato nel gruppo pubblico fallita: {esc(e)}"

    # Dati aggiornati dell'utente (l'username può essere cambiato dall'appello)
    label = user_label(uid, member.user.username, member.user.full_name)

    if member.status != ChatMemberStatus.BANNED:
        try:
            await context.bot.send_message(
                ADMIN_CHAT_ID,
                f"⚠️ Appello #{appeal['id']}: {label} (ID <code>{uid}</code>) "
                f"non è nella lista dei bannati del gruppo pubblico "
                f"(stato: {esc(member.status)}). Nessuno sban eseguito.",
                parse_mode=ParseMode.HTML,
                reply_parameters=reply,
            )
        except TelegramError as e:
            log.warning("Avviso admin fallito: %s", e)
        return " — ⚠️ utente non bannato, nessuno sban"

    try:
        # only_if_banned evita di espellere un utente che è già nel gruppo
        await context.bot.unban_chat_member(MAIN_CHAT_ID, uid, only_if_banned=True)
    except TelegramError as e:
        return f" — ⚠️ sban automatico fallito: {esc(e)}"

    try:
        await context.bot.send_message(
            MAIN_CHAT_ID, f"✅ Utente riammesso: {label}", parse_mode=ParseMode.HTML
        )
    except TelegramError as e:
        log.warning("Annuncio nel gruppo pubblico fallito: %s", e)
    return " — sban eseguito"


# --------------------------------------------------------------------------
# Azioni amministratori (pulsanti nel gruppo admin)
# --------------------------------------------------------------------------
# Il messaggio di richiesta motivo contiene l'ID dell'appello: così la risposta
# funziona anche dopo un riavvio del bot (nessuno stato in memoria).
PROMPT_RE = re.compile(r"Motivo del diniego(?P<blk> e blocco)? — appello #(?P<id>\d+)")


async def is_authorized(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    """Con REQUIRE_CHAT_ADMIN solo admin/owner del gruppo admin possono decidere."""
    if not REQUIRE_CHAT_ADMIN:
        return True
    try:
        member = await context.bot.get_chat_member(ADMIN_CHAT_ID, user_id)
    except TelegramError:
        return False
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)


def kb_reason(appeal_id: int, block: bool) -> InlineKeyboardMarkup:
    action = "dnb" if block else "dnn"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➡️ Procedi senza motivo", callback_data=f"a:{action}:{appeal_id}")],
            [InlineKeyboardButton("↩️ Annulla", callback_data=f"a:cnl:{appeal_id}")],
        ]
    )


def admin_stamp(admin) -> tuple[str, str, str]:
    """(chi per il DB, chi in HTML, data/ora)."""
    return f"{admin.full_name} ({admin.id})", esc(admin.full_name), fmt_ts(now_ts())


async def refresh_admin_message(
    context: ContextTypes.DEFAULT_TYPE, appeal_id: int, markup, status_line: str
) -> None:
    """Ridisegna il messaggio dell'appello nel gruppo admin (dati + storico dal DB)."""
    appeal = get_appeal(appeal_id)
    if not appeal or not appeal["admin_msg_id"]:
        return
    try:
        await context.bot.edit_message_text(
            build_admin_text(appeal),
            chat_id=ADMIN_CHAT_ID,
            message_id=appeal["admin_msg_id"],
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
            link_preview_options=NO_PREVIEW,
        )
    except BadRequest as e:
        # Es. messaggio troppo lungo: aggiorno solo i pulsanti e scrivo lo stato in risposta
        log.warning("Modifica messaggio fallita: %s", e)
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=ADMIN_CHAT_ID, message_id=appeal["admin_msg_id"], reply_markup=markup
            )
        except BadRequest:
            pass
        await context.bot.send_message(
            ADMIN_CHAT_ID,
            f"Appello #{appeal_id}: {status_line}",
            parse_mode=ParseMode.HTML,
            reply_parameters=ReplyParameters(
                message_id=appeal["admin_msg_id"], allow_sending_without_reply=True
            ),
        )


async def finalize_deny(
    context: ContextTypes.DEFAULT_TYPE, appeal_id: int, block: bool, admin, reason
):
    """Nega l'appello (ed eventualmente blocca), notifica l'utente. Ritorna un errore o None."""
    appeal = get_appeal(appeal_id)
    if appeal is None:
        return "Appello non trovato."
    by, by_html, stamp = admin_stamp(admin)
    if not decide(appeal_id, "denied", by, reason):
        return "Appello già gestito."

    uid = appeal["user_id"]
    if block:
        block_user(uid, by)
    reason_html = f"\n\n📝 Motivo: {esc(reason)}" if reason else ""

    if block:
        text_user = (
            f"🚫 Il tuo appello #{appeal_id} è stato <b>respinto</b> e sei stato "
            f"<b>bloccato</b>: non potrai inviare altri appelli.{reason_html}"
        )
        status_line = f"🚫 <b>Negato e appelli bloccati</b> da {by_html} il {stamp}"
        markup = kb_unblock(appeal_id)
    else:
        text_user = f"❌ Il tuo appello #{appeal_id} è stato <b>respinto</b>.{reason_html}"
        status_line = f"❌ <b>Negato</b> da {by_html} il {stamp}"
        markup = kb_unblock(appeal_id) if is_blocked(uid) else kb_block(appeal_id)

    if not await notify_user(context, uid, text_user):
        status_line += " (⚠️ utente non raggiungibile)"
    if reason:
        status_line += f"\n📝 Motivo: {esc(reason)}"

    append_log(appeal_id, status_line)
    await refresh_admin_message(context, appeal_id, markup, status_line)
    return None


async def delete_quietly(message) -> None:
    try:
        await message.delete()
    except TelegramError:
        pass


async def on_admin_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.message is None or query.message.chat.id != ADMIN_CHAT_ID:
        await query.answer("Non autorizzato.", show_alert=True)
        return
    admin = query.from_user
    if not await is_authorized(context, admin.id):
        await query.answer("Solo gli amministratori del gruppo possono decidere.", show_alert=True)
        return

    try:
        _, action, sid = query.data.split(":")
        appeal_id = int(sid)
    except ValueError:
        await query.answer("Dati non validi.", show_alert=True)
        return

    appeal = get_appeal(appeal_id)
    if appeal is None:
        await query.answer("Appello non trovato.", show_alert=True)
        return
    uid = appeal["user_id"]

    # Nega / Nega e blocca su appello in attesa: chiede il motivo (facoltativo)
    if action in ("no", "blk") and appeal["status"] == "pending":
        block = action == "blk"
        title = "Motivo del diniego e blocco" if block else "Motivo del diniego"
        await context.bot.send_message(
            ADMIN_CHAT_ID,
            f"✍️ <b>{title} — appello #{appeal_id}</b>\n"
            f"{esc(admin.full_name)}, <b>rispondi a questo messaggio</b> con il motivo "
            "(verrà inviato all'utente), oppure procedi senza motivo.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_reason(appeal_id, block),
            reply_parameters=ReplyParameters(
                message_id=query.message.message_id, allow_sending_without_reply=True
            ),
        )
        await query.answer("Scrivi il motivo o procedi senza.")
        return

    # Pulsanti del messaggio di richiesta motivo
    if action in ("dnn", "dnb"):
        err = await finalize_deny(context, appeal_id, action == "dnb", admin, None)
        await delete_quietly(query.message)
        await query.answer(err or "Fatto.", show_alert=bool(err))
        return
    if action == "cnl":
        await delete_quietly(query.message)
        await query.answer("Annullato.")
        return

    by, by_html, stamp = admin_stamp(admin)
    delivered = True

    if action == "ok":
        if not decide(appeal_id, "approved", by):
            await query.answer("Appello già gestito.", show_alert=True)
            return
        note = ""
        if AUTO_UNBAN and MAIN_CHAT_ID:
            note = await do_unban(context, appeal)
        msg = f"✅ Il tuo appello #{appeal_id} è stato <b>accettato</b>."
        if INVITE_LINK:
            msg += f"\nPuoi rientrare nel gruppo qui: {esc(INVITE_LINK)}"
        delivered = await notify_user(context, uid, msg)
        status_line = f"✅ <b>Approvato</b> da {by_html} il {stamp}{note}"
        markup = None

    elif action == "blk":
        # Blocco su appello già chiuso (es. dopo un diniego, se l'utente insiste)
        if is_blocked(uid):
            await query.answer("Utente già bloccato.")
            await query.edit_message_reply_markup(kb_unblock(appeal_id))
            return
        block_user(uid, by)
        delivered = await notify_user(
            context, uid, "🚫 Sei stato <b>bloccato</b>: non potrai inviare ulteriori appelli."
        )
        status_line = f"🚫 <b>Appelli bloccati</b> da {by_html} il {stamp}"
        markup = kb_unblock(appeal_id)

    elif action == "unb":
        if not unblock_user(uid):
            await query.answer("L'utente non era bloccato.")
        delivered = await notify_user(
            context, uid, "🔓 Puoi di nuovo inviare un appello con /start."
        )
        status_line = f"🔓 <b>Appelli sbloccati</b> da {by_html} il {stamp}"
        current = get_appeal(appeal_id)
        markup = kb_pending(appeal_id) if current["status"] == "pending" else kb_block(appeal_id)

    elif action == "no":
        await query.answer("Appello già gestito.", show_alert=True)
        return

    else:
        await query.answer("Azione sconosciuta.", show_alert=True)
        return

    if not delivered:
        status_line += " (⚠️ utente non raggiungibile)"
    append_log(appeal_id, status_line)
    await refresh_admin_message(context, appeal_id, markup, status_line)
    await query.answer("Fatto.")


async def on_reason_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Risposta testuale al messaggio 'Motivo del diniego': nega con il motivo indicato."""
    msg = update.message
    prompt = msg.reply_to_message
    if prompt is None or prompt.from_user is None or prompt.from_user.id != context.bot.id:
        return
    m = PROMPT_RE.search(prompt.text or "")
    if not m:
        return
    if not await is_authorized(context, msg.from_user.id):
        await msg.reply_text("Solo gli amministratori del gruppo possono decidere.")
        return
    reason = (msg.text or "").strip()
    if len(reason) > LIMITS["reason"]:
        await msg.reply_text(f"Motivo troppo lungo (massimo {LIMITS['reason']} caratteri), riprova.")
        return
    err = await finalize_deny(context, int(m["id"]), bool(m["blk"]), msg.from_user, reason)
    await delete_quietly(prompt)
    if err:
        await msg.reply_text(err)


# --------------------------------------------------------------------------
# Comandi amministratori (solo nel gruppo admin)
# --------------------------------------------------------------------------
async def cmd_unblock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or not re.fullmatch(r"-?\d+", context.args[0]):
        await update.message.reply_text("Uso: /sblocca <user_id>")
        return
    uid = int(context.args[0])
    if unblock_user(uid):
        await notify_user(context, uid, "🔓 Puoi di nuovo inviare un appello con /start.")
        await update.message.reply_text(f"Utente {uid} sbloccato.")
    else:
        await update.message.reply_text(f"L'utente {uid} non era bloccato.")


async def cmd_blocked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = db_all("SELECT * FROM blocked ORDER BY blocked_at DESC LIMIT 50")
    if not rows:
        await update.message.reply_text("Nessun utente bloccato.")
        return
    lines = [
        f"• <code>{r['user_id']}</code> — {fmt_ts(r['blocked_at'])} da {esc(r['blocked_by'])}"
        for r in rows
    ]
    await update.message.reply_text(
        "<b>Utenti bloccati</b> (ultimi 50):\n" + "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Utility per la configurazione: mostra l'ID della chat corrente."""
    await update.message.reply_text(
        f"Chat ID: <code>{update.effective_chat.id}</code>", parse_mode=ParseMode.HTML
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Errore non gestito", exc_info=context.error)


# --------------------------------------------------------------------------
# Avvio
# --------------------------------------------------------------------------
def main() -> None:
    db_init()
    app = Application.builder().token(BOT_TOKEN).build()

    # Modalità setup: senza ADMIN_CHAT_ID risponde solo a /chatid per ricavare gli ID
    if ADMIN_CHAT_ID is None:
        app.add_handler(CommandHandler("chatid", cmd_chatid))
        app.add_error_handler(on_error)
        log.warning(
            "ADMIN_CHAT_ID non impostato: modalità setup. "
            "Scrivi /chatid@<nome_bot> nei gruppi, compila .env e riavvia."
        )
        app.run_polling(allowed_updates=Update.ALL_TYPES)
        return

    private = filters.ChatType.PRIVATE
    text_only = filters.TEXT & ~filters.COMMAND

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start, filters=private)],
        states={
            BANDATE: [MessageHandler(text_only, got_bandate)],
            CAUSE: [
                CommandHandler("salta", skip_cause),
                MessageHandler(text_only, got_cause),
            ],
            COMMENT: [MessageHandler(text_only, got_comment)],
            CONFIRM: [
                MessageHandler(filters.Regex(f"^{re.escape(BTN_SEND)}$"), send_appeal),
                MessageHandler(filters.Regex(f"^{re.escape(BTN_CANCEL)}$"), cmd_cancel),
                MessageHandler(text_only, confirm_other),
            ],
        },
        fallbacks=[
            CommandHandler("annulla", cmd_cancel),
            MessageHandler(~filters.TEXT, need_text),
        ],
        allow_reentry=True,
    )
    app.add_handler(conv)

    admin_chat = filters.Chat(chat_id=ADMIN_CHAT_ID)
    app.add_handler(CallbackQueryHandler(on_admin_action, pattern=r"^a:"))
    app.add_handler(
        MessageHandler(
            admin_chat & filters.REPLY & filters.TEXT & ~filters.COMMAND, on_reason_reply
        )
    )
    app.add_handler(CommandHandler("sblocca", cmd_unblock, filters=admin_chat))
    app.add_handler(CommandHandler("bloccati", cmd_blocked, filters=admin_chat))
    app.add_handler(CommandHandler("stato", cmd_status, filters=private))
    app.add_handler(CommandHandler("chatid", cmd_chatid))
    app.add_handler(MessageHandler(private & ~filters.COMMAND, private_fallback))
    app.add_error_handler(on_error)

    log.info("Bot avviato.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
