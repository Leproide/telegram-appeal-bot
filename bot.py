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
Telegram bot to handle unban appeals for a group.

- Users write to the bot in private (/start) and fill in a guided appeal.
  The account (username/ID) is taken automatically: nobody can appeal for others.
  A public @username is required to submit an appeal.
- Only one pending appeal per user (also enforced by the database).
- Appeals are posted to an admin group with Approve / Deny / Deny & block buttons.
  Denials accept an optional reason that is forwarded to the user.
- On approval the bot can unban the user in the main group, announce the
  readmission there and send an invite link to the user.
- Blocked users cannot send appeals until unblocked.
- Interface language: BOT_LANG=en (default) or BOT_LANG=it.
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
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
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
# Configuration (environment variables / .env file next to bot.py)
# --------------------------------------------------------------------------
ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(ENV_FILE)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise SystemExit(
        f"BOT_TOKEN not found. Make sure {ENV_FILE} exists "
        "(exact name '.env', with the dot) and contains BOT_TOKEN=..."
    )


def _env_int(name: str):
    value = os.environ.get(name, "").strip()
    return int(value) if value else None


def _env_bool(name: str) -> bool:
    return os.environ.get(name, "false").strip().lower() in ("1", "true", "yes")


# Admin group: if missing the bot starts in "setup mode" (only /chatid works)
ADMIN_CHAT_ID = _env_int("ADMIN_CHAT_ID")
MAIN_CHAT_ID = _env_int("MAIN_CHAT_ID")
AUTO_UNBAN = _env_bool("AUTO_UNBAN")
INVITE_LINK = os.environ.get("INVITE_LINK", "").strip()
REQUIRE_CHAT_ADMIN = _env_bool("REQUIRE_CHAT_ADMIN")
DB_PATH = os.environ.get("DB_PATH", "appeals.db").strip() or "appeals.db"
BOT_LANG = os.environ.get("BOT_LANG", "en").strip().lower() or "en"

# Maximum field lengths (keeps messages below Telegram's 4096-char limit)
LIMITS = {"ban_datetime": 100, "cause": 500, "comment": 1500, "reason": 1000}

BANDATE, CAUSE, COMMENT, CONFIRM = range(4)

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("appeal-bot")

# --------------------------------------------------------------------------
# Localization
# --------------------------------------------------------------------------
# Commands are registered under both names (English and Italian) so that they
# keep working whatever the language; texts show the name of the active language.
COMMANDS = {
    "skip": ["skip", "salta"],
    "cancel": ["cancel", "annulla"],
    "status": ["status", "stato"],
    "unblock": ["unblock", "sblocca"],
    "blocked": ["blocked", "bloccati"],
}

STRINGS = {
    "en": {
        "date_format": "%Y-%m-%d %H:%M",
        # --- user flow ---
        "blocked": "You can no longer submit appeals.",
        "already_pending": (
            "You already have a pending appeal (#{id}, {date}).\n"
            "Please wait for the admins' decision before sending another one."
        ),
        "start": (
            "Hi! Here you can submit an appeal to be unbanned from the group.\n\n"
            "👤 Account: {account}\n"
            "🆔 ID: <code>{uid}</code>\n"
            "The appeal applies only to this Telegram account.\n"
            "You can cancel at any time with /cancel.\n\n"
            "Step 1/3 — Approximate date and time of the ban (e.g. 2026-11-05 evening)."
        ),
        "username_required": (
            "To submit an appeal your Telegram account needs a public <b>username</b>.\n\n"
            "Set one in Telegram: <i>Settings → Edit profile → Username</i>, "
            "then start again with /start."
        ),
        "ask_cause": "Step 2/3 — Reason for the ban (optional).\nType it or send /skip.",
        "ask_comment": "Step 3/3 — Free comment: explain why you should be unbanned.",
        "empty_field": "This field cannot be empty, please try again.",
        "too_long": "Text too long (max {max} characters), please try again.",
        "not_specified": "<i>not specified</i>",
        "summary": (
            "<b>Appeal summary</b>\n\n"
            "👤 Account: {account}\n"
            "🕒 Ban date/time: {bandate}\n"
            "⚠️ Reason: {cause}\n"
            "💬 Comment:\n{comment}\n\n"
            "Do you confirm? You can only have one appeal at a time."
        ),
        "btn_send": "✅ Send appeal",
        "btn_cancel": "❌ Cancel",
        "session_expired": "Session expired, please start again with /start.",
        "pending_exists": "You already have a pending appeal.",
        "send_error": "Error while sending the appeal, please try again later.",
        "sent": (
            "Appeal #{id} sent to the admins. You will receive the answer here. "
            "You can check its status with /status."
        ),
        "cancelled": "Appeal cancelled. You can start again with /start.",
        "use_buttons": "Please use the buttons to confirm or cancel.",
        "text_only": "Please send text only.",
        "no_appeals": "No appeal sent yet. Use /start to submit one.",
        "last_appeal": "Last appeal #{id} ({date}): {status}.",
        "st_pending": "⏳ pending",
        "st_approved": "✅ approved",
        "st_denied": "❌ rejected",
        "can_send_new": "You can submit a new one with /start.",
        "private_fallback": "Use /start to submit an unban appeal, /status to check it.",
        # --- user notifications ---
        "n_approved": "✅ Your appeal #{id} has been <b>accepted</b>.",
        "n_invite": "\nYou can rejoin the group here: {link}",
        "n_denied": "❌ Your appeal #{id} has been <b>rejected</b>.",
        "n_denied_blocked": (
            "🚫 Your appeal #{id} has been <b>rejected</b> and you have been "
            "<b>blocked</b>: you will not be able to send further appeals."
        ),
        "n_reason": "\n\n📝 Reason: {reason}",
        "n_blocked": "🚫 You have been <b>blocked</b>: you will not be able to send further appeals.",
        "n_unblocked": "🔓 You can submit an appeal again with /start.",
        # --- admin group: appeal message ---
        "a_header": "📨 <b>Appeal #{id}</b> — {date}",
        "a_user": "👤 User",
        "a_bandate": "🕒 Ban date/time",
        "a_cause": "⚠️ Reason",
        "a_comment": "💬 Comment",
        "a_previous": "📊 Previous appeals: {total} (rejected: {denied})",
        "b_approve": "✅ Approve",
        "b_deny": "❌ Deny",
        "b_deny_block": "🚫 Deny and block appeals",
        "b_block": "🚫 Block user's appeals",
        "b_unblock": "🔓 Unblock user's appeals",
        "b_no_reason": "➡️ Proceed without reason",
        "b_abort": "↩️ Cancel",
        # --- admin group: denial reason prompt ---
        "p_title": "Denial reason — appeal #{id}",
        "p_title_block": "Denial and block reason — appeal #{id}",
        "p_body": (
            "{admin}, <b>reply to this message</b> with the reason "
            "(it will be sent to the user), or proceed without a reason."
        ),
        # --- admin group: decision log ---
        "l_approved": "✅ <b>Approved</b> by {by} on {date}",
        "l_denied": "❌ <b>Denied</b> by {by} on {date}",
        "l_denied_blocked": "🚫 <b>Denied and appeals blocked</b> by {by} on {date}",
        "l_blocked": "🚫 <b>Appeals blocked</b> by {by} on {date}",
        "l_unblocked": "🔓 <b>Appeals unblocked</b> by {by} on {date}",
        "l_unreachable": " (⚠️ user unreachable)",
        "l_reason": "\n📝 Reason: {reason}",
        "l_fallback": "Appeal #{id}: {status}",
        # --- unban ---
        "u_done": " — unbanned",
        "u_check_failed": " — ⚠️ status check in main group failed: {err}",
        "u_not_banned": " — ⚠️ user not banned, no unban",
        "u_failed": " — ⚠️ automatic unban failed: {err}",
        "u_warn_admin": (
            "⚠️ Appeal #{id}: {user} (ID <code>{uid}</code>) is not in the main "
            "group's ban list (status: {status}). No unban performed."
        ),
        "u_public": "✅ User readmitted: {user}",
        # --- callback answers ---
        "c_unauthorized": "Not authorized.",
        "c_admins_only": "Only the group admins can decide.",
        "c_invalid": "Invalid data.",
        "c_not_found": "Appeal not found.",
        "c_handled": "Appeal already handled.",
        "c_already_blocked": "User already blocked.",
        "c_not_blocked": "The user was not blocked.",
        "c_done": "Done.",
        "c_aborted": "Cancelled.",
        "c_write_reason": "Write the reason or proceed without it.",
        "c_unknown": "Unknown action.",
        "reason_too_long": "Reason too long (max {max} characters), please try again.",
        # --- admin commands ---
        "unblock_usage": "Usage: /unblock <user_id>",
        "unblock_ok": "User {uid} unblocked.",
        "unblock_none": "User {uid} was not blocked.",
        "blocked_none": "No blocked users.",
        "blocked_header": "<b>Blocked users</b> (last 50):",
        "blocked_item": "• <code>{uid}</code> — {date} by {by}",
        "chat_id": "Chat ID: <code>{id}</code>",
        # --- command menu descriptions ---
        "m_start": "Submit an unban appeal",
        "m_status": "Status of your last appeal",
        "m_cancel": "Cancel the current appeal",
        "m_unblock": "Unblock a user: /unblock <user_id>",
        "m_blocked": "List blocked users",
        "m_chatid": "Show this chat's ID",
    },
    "it": {
        "date_format": "%d/%m/%Y %H:%M",
        "blocked": "Non puoi più inviare appelli.",
        "already_pending": (
            "Hai già un appello in attesa (#{id} del {date}).\n"
            "Attendi la decisione degli amministratori prima di inviarne un altro."
        ),
        "start": (
            "Ciao! Qui puoi inviare un appello per lo sban dal gruppo.\n\n"
            "👤 Account: {account}\n"
            "🆔 ID: <code>{uid}</code>\n"
            "L'appello vale solo per questo account Telegram.\n"
            "Puoi annullare in qualsiasi momento con /annulla.\n\n"
            "Passo 1/3 — Data e ora indicativa del ban (es. 05/11/2026 sera)."
        ),
        "username_required": (
            "Per inviare un appello il tuo account Telegram deve avere un <b>username</b> pubblico.\n\n"
            "Impostalo da Telegram: <i>Impostazioni → Modifica profilo → Username</i>, "
            "poi ricomincia con /start."
        ),
        "ask_cause": "Passo 2/3 — Causa del ban (opzionale).\nScrivila oppure invia /salta.",
        "ask_comment": "Passo 3/3 — Commento libero: spiega perché dovresti essere sbannato.",
        "empty_field": "Il campo non può essere vuoto, riprova.",
        "too_long": "Testo troppo lungo (massimo {max} caratteri), riprova.",
        "not_specified": "<i>non indicata</i>",
        "summary": (
            "<b>Riepilogo appello</b>\n\n"
            "👤 Account: {account}\n"
            "🕒 Data/ora ban: {bandate}\n"
            "⚠️ Causa: {cause}\n"
            "💬 Commento:\n{comment}\n\n"
            "Confermi l'invio? Potrai inviare un solo appello alla volta."
        ),
        "btn_send": "✅ Invia appello",
        "btn_cancel": "❌ Annulla",
        "session_expired": "Sessione scaduta, ricomincia con /start.",
        "pending_exists": "Hai già un appello in attesa.",
        "send_error": "Errore nell'invio dell'appello, riprova più tardi.",
        "sent": (
            "Appello #{id} inviato agli amministratori. Riceverai qui la risposta. "
            "Puoi controllare lo stato con /stato."
        ),
        "cancelled": "Appello annullato. Puoi ricominciare con /start.",
        "use_buttons": "Usa i pulsanti per confermare o annullare.",
        "text_only": "Invia solo testo, per favore.",
        "no_appeals": "Nessun appello inviato. Usa /start per inviarne uno.",
        "last_appeal": "Ultimo appello #{id} del {date}: {status}.",
        "st_pending": "⏳ in attesa",
        "st_approved": "✅ approvato",
        "st_denied": "❌ negato",
        "can_send_new": "Puoi inviarne uno nuovo con /start.",
        "private_fallback": "Usa /start per inviare un appello di sban, /stato per vederne lo stato.",
        "n_approved": "✅ Il tuo appello #{id} è stato <b>accettato</b>.",
        "n_invite": "\nPuoi rientrare nel gruppo qui: {link}",
        "n_denied": "❌ Il tuo appello #{id} è stato <b>respinto</b>.",
        "n_denied_blocked": (
            "🚫 Il tuo appello #{id} è stato <b>respinto</b> e sei stato "
            "<b>bloccato</b>: non potrai inviare altri appelli."
        ),
        "n_reason": "\n\n📝 Motivo: {reason}",
        "n_blocked": "🚫 Sei stato <b>bloccato</b>: non potrai inviare ulteriori appelli.",
        "n_unblocked": "🔓 Puoi di nuovo inviare un appello con /start.",
        "a_header": "📨 <b>Appello #{id}</b> — {date}",
        "a_user": "👤 Utente",
        "a_bandate": "🕒 Data/ora ban",
        "a_cause": "⚠️ Causa",
        "a_comment": "💬 Commento",
        "a_previous": "📊 Appelli precedenti: {total} (negati: {denied})",
        "b_approve": "✅ Approva",
        "b_deny": "❌ Nega",
        "b_deny_block": "🚫 Nega e blocca appelli",
        "b_block": "🚫 Blocca appelli utente",
        "b_unblock": "🔓 Sblocca appelli utente",
        "b_no_reason": "➡️ Procedi senza motivo",
        "b_abort": "↩️ Annulla",
        "p_title": "Motivo del diniego — appello #{id}",
        "p_title_block": "Motivo del diniego e blocco — appello #{id}",
        "p_body": (
            "{admin}, <b>rispondi a questo messaggio</b> con il motivo "
            "(verrà inviato all'utente), oppure procedi senza motivo."
        ),
        "l_approved": "✅ <b>Approvato</b> da {by} il {date}",
        "l_denied": "❌ <b>Negato</b> da {by} il {date}",
        "l_denied_blocked": "🚫 <b>Negato e appelli bloccati</b> da {by} il {date}",
        "l_blocked": "🚫 <b>Appelli bloccati</b> da {by} il {date}",
        "l_unblocked": "🔓 <b>Appelli sbloccati</b> da {by} il {date}",
        "l_unreachable": " (⚠️ utente non raggiungibile)",
        "l_reason": "\n📝 Motivo: {reason}",
        "l_fallback": "Appello #{id}: {status}",
        "u_done": " — sban eseguito",
        "u_check_failed": " — ⚠️ verifica stato nel gruppo principale fallita: {err}",
        "u_not_banned": " — ⚠️ utente non bannato, nessuno sban",
        "u_failed": " — ⚠️ sban automatico fallito: {err}",
        "u_warn_admin": (
            "⚠️ Appello #{id}: {user} (ID <code>{uid}</code>) non è nella lista dei "
            "bannati del gruppo principale (stato: {status}). Nessuno sban eseguito."
        ),
        "u_public": "✅ Utente riammesso: {user}",
        "c_unauthorized": "Non autorizzato.",
        "c_admins_only": "Solo gli amministratori del gruppo possono decidere.",
        "c_invalid": "Dati non validi.",
        "c_not_found": "Appello non trovato.",
        "c_handled": "Appello già gestito.",
        "c_already_blocked": "Utente già bloccato.",
        "c_not_blocked": "L'utente non era bloccato.",
        "c_done": "Fatto.",
        "c_aborted": "Annullato.",
        "c_write_reason": "Scrivi il motivo o procedi senza.",
        "c_unknown": "Azione sconosciuta.",
        "reason_too_long": "Motivo troppo lungo (massimo {max} caratteri), riprova.",
        "unblock_usage": "Uso: /sblocca <user_id>",
        "unblock_ok": "Utente {uid} sbloccato.",
        "unblock_none": "L'utente {uid} non era bloccato.",
        "blocked_none": "Nessun utente bloccato.",
        "blocked_header": "<b>Utenti bloccati</b> (ultimi 50):",
        "blocked_item": "• <code>{uid}</code> — {date} da {by}",
        "chat_id": "Chat ID: <code>{id}</code>",
        "m_start": "Invia un appello di sban",
        "m_status": "Stato del tuo ultimo appello",
        "m_cancel": "Annulla l'appello in corso",
        "m_unblock": "Sblocca un utente: /sblocca <user_id>",
        "m_blocked": "Elenco utenti bloccati",
        "m_chatid": "Mostra l'ID di questa chat",
    },
}

if BOT_LANG not in STRINGS:
    log.warning("Unsupported BOT_LANG '%s', falling back to 'en'.", BOT_LANG)
    BOT_LANG = "en"
# Both translations must define the same keys
assert set(STRINGS["en"]) == set(STRINGS["it"]), "Translation keys mismatch"


def t(key: str, **kwargs) -> str:
    """Return the localized string for the active language."""
    text = STRINGS[BOT_LANG][key]
    return text.format(**kwargs) if kwargs else text


def cmd(key: str) -> str:
    """Command name in the active language (English is index 0, Italian index 1)."""
    return COMMANDS[key][0 if BOT_LANG == "en" else 1]


# Marker of the denial reason prompt: language independent, so replies keep
# working after a restart or a language change. "🚫" marks deny + block.
PROMPT_RE = re.compile(r"^✍️ (?P<blk>🚫 )?[^#\n]*#(?P<id>\d+)")


# --------------------------------------------------------------------------
# Utilities
# --------------------------------------------------------------------------
def now_ts() -> int:
    return int(time.time())


def fmt_ts(ts: int) -> str:
    return time.strftime(t("date_format"), time.localtime(ts))


def esc(s) -> str:
    return html.escape(str(s)) if s is not None else ""


def user_label(user_id: int, username, full_name) -> str:
    """HTML label: @username if set, otherwise a mention with the name."""
    if username:
        return f"@{esc(username)}"
    return f'<a href="tg://user?id={user_id}">{esc(full_name or user_id)}</a>'


def user_plain(username, full_name) -> str:
    """Plain label stored in the database (nickname column)."""
    return f"@{username}" if username else (full_name or "")


# --------------------------------------------------------------------------
# Database (SQLite, one connection per operation: simple and safe)
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
                reason        TEXT,  -- optional denial reason
                log           TEXT   -- decision history shown in the admin group
            );
            -- Only one 'pending' appeal per user, enforced by the database
            CREATE UNIQUE INDEX IF NOT EXISTS one_pending_per_user
                ON appeals(user_id) WHERE status = 'pending';
            CREATE TABLE IF NOT EXISTS blocked (
                user_id     INTEGER PRIMARY KEY,
                blocked_at  INTEGER NOT NULL,
                blocked_by  TEXT
            );
            """
        )
        # Migration from older versions: add missing columns
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
    """Run a write statement; returns (rowcount, lastrowid)."""
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
    """Insert the appeal. Raises sqlite3.IntegrityError if one is already pending."""
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
    """Close the appeal only if still pending (prevents double clicks). True if updated."""
    count, _ = db_exec(
        """UPDATE appeals SET status = ?, decided_at = ?, decided_by = ?, reason = ?
           WHERE id = ? AND status = 'pending'""",
        (status, now_ts(), by, reason, appeal_id),
    )
    return count > 0


def append_log(appeal_id: int, line: str) -> None:
    """Append a line to the appeal's decision history."""
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


def previous_stats(user_id: int, appeal_id: int) -> tuple[int, int]:
    """Appeals sent by the user before this one: (total, denied)."""
    row = db_one(
        """SELECT COUNT(*) AS total,
                  COALESCE(SUM(status = 'denied'), 0) AS denied
           FROM appeals WHERE user_id = ? AND id < ?""",
        (user_id, appeal_id),
    )
    return row["total"], row["denied"]


# --------------------------------------------------------------------------
# Texts and keyboards
# --------------------------------------------------------------------------
def build_admin_text(appeal) -> str:
    total, denied = previous_stats(appeal["user_id"], appeal["id"])
    uname = f" (@{esc(appeal['username'])})" if appeal["username"] else ""
    cause = esc(appeal["cause"]) if appeal["cause"] else t("not_specified")
    text = (
        f"{t('a_header', id=appeal['id'], date=fmt_ts(appeal['created_at']))}\n\n"
        f"{t('a_user')}: <a href=\"tg://user?id={appeal['user_id']}\">"
        f"{esc(appeal['full_name'])}</a>{uname}\n"
        f"🆔 ID: <code>{appeal['user_id']}</code>\n"
        f"{t('a_bandate')}: {esc(appeal['ban_datetime'])}\n"
        f"{t('a_cause')}: {cause}\n"
        f"{t('a_comment')}:\n{esc(appeal['comment'])}\n\n"
        f"{t('a_previous', total=total, denied=denied)}"
    )
    if appeal["log"]:
        text += f"\n\n{appeal['log']}"
    return text


def kb_pending(appeal_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(t("b_approve"), callback_data=f"a:ok:{appeal_id}"),
                InlineKeyboardButton(t("b_deny"), callback_data=f"a:no:{appeal_id}"),
            ],
            [InlineKeyboardButton(t("b_deny_block"), callback_data=f"a:blk:{appeal_id}")],
        ]
    )


def kb_block(appeal_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(t("b_block"), callback_data=f"a:blk:{appeal_id}")]]
    )


def kb_unblock(appeal_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(t("b_unblock"), callback_data=f"a:unb:{appeal_id}")]]
    )


def kb_reason(appeal_id: int, block: bool) -> InlineKeyboardMarkup:
    action = "dnb" if block else "dnn"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(t("b_no_reason"), callback_data=f"a:{action}:{appeal_id}")],
            [InlineKeyboardButton(t("b_abort"), callback_data=f"a:cnl:{appeal_id}")],
        ]
    )


def confirm_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[t("btn_send"), t("btn_cancel")]], resize_keyboard=True, one_time_keyboard=True
    )


async def notify_user(context: ContextTypes.DEFAULT_TYPE, user_id: int, text: str) -> bool:
    """Send a message to the user; False if unreachable (e.g. they blocked the bot)."""
    try:
        await context.bot.send_message(
            user_id, text, parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW
        )
        return True
    except TelegramError as e:
        log.warning("Cannot notify user %s: %s", user_id, e)
        return False


async def delete_quietly(message) -> None:
    try:
        await message.delete()
    except TelegramError:
        pass


# --------------------------------------------------------------------------
# User conversation (private chat)
# --------------------------------------------------------------------------
async def read_field(update: Update, field: str):
    """Validate a text field; None if invalid (the user stays on the same step)."""
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text(t("empty_field"))
        return None
    if len(text) > LIMITS[field]:
        await update.message.reply_text(t("too_long", max=LIMITS[field]))
        return None
    return text


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    if is_blocked(user.id):
        await update.message.reply_text(t("blocked"), reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    pending = get_pending(user.id)
    if pending:
        await update.message.reply_text(
            t("already_pending", id=pending["id"], date=fmt_ts(pending["created_at"])),
            reply_markup=ReplyKeyboardRemove(),
        )
        return ConversationHandler.END

    # A public username is required (easier identification for the admins)
    if not user.username:
        await update.message.reply_text(
            t("username_required"), parse_mode=ParseMode.HTML, reply_markup=ReplyKeyboardRemove()
        )
        return ConversationHandler.END

    context.user_data.clear()
    # The account comes from Telegram and cannot be changed by the user
    context.user_data["nickname"] = user_plain(user.username, user.full_name)
    await update.message.reply_text(
        t(
            "start",
            account=user_label(user.id, user.username, user.full_name),
            uid=user.id,
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardRemove(),
    )
    return BANDATE


async def got_bandate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = await read_field(update, "ban_datetime")
    if value is None:
        return BANDATE
    context.user_data["ban_datetime"] = value
    await update.message.reply_text(t("ask_cause"))
    return CAUSE


async def got_cause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = await read_field(update, "cause")
    if value is None:
        return CAUSE
    context.user_data["cause"] = value
    await update.message.reply_text(t("ask_comment"))
    return COMMENT


async def skip_cause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["cause"] = None
    await update.message.reply_text(t("ask_comment"))
    return COMMENT


async def got_comment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = await read_field(update, "comment")
    if value is None:
        return COMMENT
    ud = context.user_data
    ud["comment"] = value
    user = update.effective_user
    await update.message.reply_text(
        t(
            "summary",
            account=user_label(user.id, user.username, user.full_name),
            bandate=esc(ud["ban_datetime"]),
            cause=esc(ud["cause"]) if ud.get("cause") else t("not_specified"),
            comment=esc(ud["comment"]),
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=confirm_keyboard(),
        link_preview_options=NO_PREVIEW,
    )
    return CONFIRM


async def send_appeal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    ud = context.user_data

    # Re-check: the state may have changed while the user was typing
    if is_blocked(user.id):
        await update.message.reply_text(t("blocked"), reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END
    if not user.username:
        ud.clear()
        await update.message.reply_text(
            t("username_required"), parse_mode=ParseMode.HTML, reply_markup=ReplyKeyboardRemove()
        )
        return ConversationHandler.END
    if not all(k in ud for k in ("nickname", "ban_datetime", "comment")):
        await update.message.reply_text(
            t("session_expired"), reply_markup=ReplyKeyboardRemove()
        )
        return ConversationHandler.END

    try:
        appeal_id = create_appeal(user, ud)
    except sqlite3.IntegrityError:
        await update.message.reply_text(t("pending_exists"), reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    try:
        msg = await context.bot.send_message(
            ADMIN_CHAT_ID,
            build_admin_text(get_appeal(appeal_id)),
            parse_mode=ParseMode.HTML,
            reply_markup=kb_pending(appeal_id),
            link_preview_options=NO_PREVIEW,
        )
    except TelegramError as e:
        # If the admins cannot receive it, roll back so the user is not stuck
        log.error("Sending to the admin group failed: %s", e)
        delete_appeal(appeal_id)
        await update.message.reply_text(t("send_error"), reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    set_admin_msg(appeal_id, msg.message_id)
    ud.clear()
    await update.message.reply_text(t("sent", id=appeal_id), reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(t("cancelled"), reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


async def confirm_other(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(t("use_buttons"), reply_markup=confirm_keyboard())
    return CONFIRM


async def need_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(t("text_only"))
    # No return value: the conversation state is unchanged


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if is_blocked(uid):
        await update.message.reply_text(t("blocked"))
        return
    last = get_last_appeal(uid)
    if not last:
        await update.message.reply_text(t("no_appeals"))
        return
    status = t(f"st_{last['status']}") if last["status"] in ("pending", "approved", "denied") else last["status"]
    text = t("last_appeal", id=last["id"], date=fmt_ts(last["created_at"]), status=status)
    if last["status"] != "pending":
        text += "\n" + t("can_send_new")
    await update.message.reply_text(text)


async def private_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(t("private_fallback"), reply_markup=ReplyKeyboardRemove())


# --------------------------------------------------------------------------
# Unban in the main group
# --------------------------------------------------------------------------
async def do_unban(context: ContextTypes.DEFAULT_TYPE, appeal) -> str:
    """
    Check that the user is in the main group's ban list:
    - banned: unban + "User readmitted" announcement in the main group;
    - not banned: do nothing + warning in the admin group.
    Returns the note appended to the appeal's status line.
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
        return t("u_check_failed", err=esc(e))

    # Fresh user data (the username may have changed since the appeal)
    label = user_label(uid, member.user.username, member.user.full_name)

    if member.status != ChatMemberStatus.BANNED:
        try:
            await context.bot.send_message(
                ADMIN_CHAT_ID,
                t("u_warn_admin", id=appeal["id"], user=label, uid=uid, status=esc(member.status)),
                parse_mode=ParseMode.HTML,
                reply_parameters=reply,
            )
        except TelegramError as e:
            log.warning("Admin warning failed: %s", e)
        return t("u_not_banned")

    try:
        # only_if_banned avoids kicking a user who is already in the group
        await context.bot.unban_chat_member(MAIN_CHAT_ID, uid, only_if_banned=True)
    except TelegramError as e:
        return t("u_failed", err=esc(e))

    try:
        await context.bot.send_message(
            MAIN_CHAT_ID, t("u_public", user=label), parse_mode=ParseMode.HTML
        )
    except TelegramError as e:
        log.warning("Announcement in the main group failed: %s", e)
    return t("u_done")


# --------------------------------------------------------------------------
# Admin actions (buttons in the admin group)
# --------------------------------------------------------------------------
async def is_authorized(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    """With REQUIRE_CHAT_ADMIN only admins/owner of the admin group can decide."""
    if not REQUIRE_CHAT_ADMIN:
        return True
    try:
        member = await context.bot.get_chat_member(ADMIN_CHAT_ID, user_id)
    except TelegramError:
        return False
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)


def admin_stamp(admin) -> tuple[str, str, str]:
    """(who, for the database; who, as HTML; current date/time)."""
    return f"{admin.full_name} ({admin.id})", esc(admin.full_name), fmt_ts(now_ts())


async def refresh_admin_message(
    context: ContextTypes.DEFAULT_TYPE, appeal_id: int, markup, status_line: str
) -> None:
    """Redraw the appeal message in the admin group (data + history from the database)."""
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
        # E.g. message too long: update only the buttons and post the status as a reply
        log.warning("Editing the appeal message failed: %s", e)
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=ADMIN_CHAT_ID, message_id=appeal["admin_msg_id"], reply_markup=markup
            )
        except BadRequest:
            pass
        await context.bot.send_message(
            ADMIN_CHAT_ID,
            t("l_fallback", id=appeal_id, status=status_line),
            parse_mode=ParseMode.HTML,
            reply_parameters=ReplyParameters(
                message_id=appeal["admin_msg_id"], allow_sending_without_reply=True
            ),
        )


async def finalize_deny(
    context: ContextTypes.DEFAULT_TYPE, appeal_id: int, block: bool, admin, reason
):
    """Deny the appeal (and optionally block), notify the user. Returns an error or None."""
    appeal = get_appeal(appeal_id)
    if appeal is None:
        return t("c_not_found")
    by, by_html, stamp = admin_stamp(admin)
    if not decide(appeal_id, "denied", by, reason):
        return t("c_handled")

    uid = appeal["user_id"]
    if block:
        block_user(uid, by)
        text_user = t("n_denied_blocked", id=appeal_id)
        status_line = t("l_denied_blocked", by=by_html, date=stamp)
        markup = kb_unblock(appeal_id)
    else:
        text_user = t("n_denied", id=appeal_id)
        status_line = t("l_denied", by=by_html, date=stamp)
        markup = kb_unblock(appeal_id) if is_blocked(uid) else kb_block(appeal_id)

    if reason:
        text_user += t("n_reason", reason=esc(reason))
    if not await notify_user(context, uid, text_user):
        status_line += t("l_unreachable")
    if reason:
        status_line += t("l_reason", reason=esc(reason))

    append_log(appeal_id, status_line)
    await refresh_admin_message(context, appeal_id, markup, status_line)
    return None


async def on_admin_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.message is None or query.message.chat.id != ADMIN_CHAT_ID:
        await query.answer(t("c_unauthorized"), show_alert=True)
        return
    admin = query.from_user
    if not await is_authorized(context, admin.id):
        await query.answer(t("c_admins_only"), show_alert=True)
        return

    try:
        _, action, sid = query.data.split(":")
        appeal_id = int(sid)
    except ValueError:
        await query.answer(t("c_invalid"), show_alert=True)
        return

    appeal = get_appeal(appeal_id)
    if appeal is None:
        await query.answer(t("c_not_found"), show_alert=True)
        return
    uid = appeal["user_id"]

    # Deny / Deny and block on a pending appeal: ask for the (optional) reason
    if action in ("no", "blk") and appeal["status"] == "pending":
        block = action == "blk"
        title = t("p_title_block" if block else "p_title", id=appeal_id)
        marker = "✍️ 🚫" if block else "✍️"
        await context.bot.send_message(
            ADMIN_CHAT_ID,
            f"{marker} <b>{title}</b>\n{t('p_body', admin=esc(admin.full_name))}",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_reason(appeal_id, block),
            reply_parameters=ReplyParameters(
                message_id=query.message.message_id, allow_sending_without_reply=True
            ),
        )
        await query.answer(t("c_write_reason"))
        return

    # Buttons of the reason prompt
    if action in ("dnn", "dnb"):
        err = await finalize_deny(context, appeal_id, action == "dnb", admin, None)
        await delete_quietly(query.message)
        await query.answer(err or t("c_done"), show_alert=bool(err))
        return
    if action == "cnl":
        await delete_quietly(query.message)
        await query.answer(t("c_aborted"))
        return

    by, by_html, stamp = admin_stamp(admin)
    delivered = True

    if action == "ok":
        if not decide(appeal_id, "approved", by):
            await query.answer(t("c_handled"), show_alert=True)
            return
        note = ""
        if AUTO_UNBAN and MAIN_CHAT_ID:
            note = await do_unban(context, appeal)
        msg = t("n_approved", id=appeal_id)
        if INVITE_LINK:
            msg += t("n_invite", link=esc(INVITE_LINK))
        delivered = await notify_user(context, uid, msg)
        status_line = t("l_approved", by=by_html, date=stamp) + note
        markup = None

    elif action == "blk":
        # Block on an already closed appeal (e.g. after a denial, if the user insists)
        if is_blocked(uid):
            await query.answer(t("c_already_blocked"))
            await query.edit_message_reply_markup(kb_unblock(appeal_id))
            return
        block_user(uid, by)
        delivered = await notify_user(context, uid, t("n_blocked"))
        status_line = t("l_blocked", by=by_html, date=stamp)
        markup = kb_unblock(appeal_id)

    elif action == "unb":
        if not unblock_user(uid):
            await query.answer(t("c_not_blocked"))
        delivered = await notify_user(context, uid, t("n_unblocked"))
        status_line = t("l_unblocked", by=by_html, date=stamp)
        current = get_appeal(appeal_id)
        markup = kb_pending(appeal_id) if current["status"] == "pending" else kb_block(appeal_id)

    elif action == "no":
        await query.answer(t("c_handled"), show_alert=True)
        return

    else:
        await query.answer(t("c_unknown"), show_alert=True)
        return

    if not delivered:
        status_line += t("l_unreachable")
    append_log(appeal_id, status_line)
    await refresh_admin_message(context, appeal_id, markup, status_line)
    await query.answer(t("c_done"))


async def on_reason_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Text reply to the 'Denial reason' prompt: deny with the given reason."""
    msg = update.message
    prompt = msg.reply_to_message
    if prompt is None or prompt.from_user is None or prompt.from_user.id != context.bot.id:
        return
    m = PROMPT_RE.search(prompt.text or "")
    if not m:
        return
    if not await is_authorized(context, msg.from_user.id):
        await msg.reply_text(t("c_admins_only"))
        return
    reason = (msg.text or "").strip()
    if len(reason) > LIMITS["reason"]:
        await msg.reply_text(t("reason_too_long", max=LIMITS["reason"]))
        return
    err = await finalize_deny(context, int(m["id"]), bool(m["blk"]), msg.from_user, reason)
    await delete_quietly(prompt)
    if err:
        await msg.reply_text(err)


# --------------------------------------------------------------------------
# Admin commands (admin group only)
# --------------------------------------------------------------------------
async def cmd_unblock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or not re.fullmatch(r"-?\d+", context.args[0]):
        await update.message.reply_text(t("unblock_usage"))
        return
    uid = int(context.args[0])
    if unblock_user(uid):
        await notify_user(context, uid, t("n_unblocked"))
        await update.message.reply_text(t("unblock_ok", uid=uid))
    else:
        await update.message.reply_text(t("unblock_none", uid=uid))


async def cmd_blocked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = db_all("SELECT * FROM blocked ORDER BY blocked_at DESC LIMIT 50")
    if not rows:
        await update.message.reply_text(t("blocked_none"))
        return
    lines = [
        t("blocked_item", uid=r["user_id"], date=fmt_ts(r["blocked_at"]), by=esc(r["blocked_by"]))
        for r in rows
    ]
    await update.message.reply_text(
        t("blocked_header") + "\n" + "\n".join(lines), parse_mode=ParseMode.HTML
    )


async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Setup helper: show the current chat ID."""
    await update.message.reply_text(
        t("chat_id", id=update.effective_chat.id), parse_mode=ParseMode.HTML
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled error", exc_info=context.error)


async def post_init(app: Application) -> None:
    """Publish the command menu in the active language (best effort)."""
    try:
        await app.bot.set_my_commands(
            [
                BotCommand("start", t("m_start")),
                BotCommand(cmd("status"), t("m_status")),
                BotCommand(cmd("cancel"), t("m_cancel")),
            ],
            scope=BotCommandScopeAllPrivateChats(),
        )
        if ADMIN_CHAT_ID is not None:
            await app.bot.set_my_commands(
                [
                    BotCommand(cmd("unblock"), t("m_unblock")),
                    BotCommand(cmd("blocked"), t("m_blocked")),
                    BotCommand("chatid", t("m_chatid")),
                ],
                scope=BotCommandScopeChat(chat_id=ADMIN_CHAT_ID),
            )
    except TelegramError as e:
        log.warning("Setting the command menu failed: %s", e)


# --------------------------------------------------------------------------
# Startup
# --------------------------------------------------------------------------
def main() -> None:
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # Setup mode: without ADMIN_CHAT_ID only /chatid is available to find the IDs
    if ADMIN_CHAT_ID is None:
        app.add_handler(CommandHandler("chatid", cmd_chatid))
        app.add_error_handler(on_error)
        log.warning(
            "ADMIN_CHAT_ID not set: setup mode. Send /chatid@<bot_name> in the groups, "
            "fill in .env and restart."
        )
        app.run_polling(allowed_updates=Update.ALL_TYPES)
        return

    db_init()

    private = filters.ChatType.PRIVATE
    text_only = filters.TEXT & ~filters.COMMAND
    admin_chat = filters.Chat(chat_id=ADMIN_CHAT_ID)

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start, filters=private)],
        states={
            BANDATE: [MessageHandler(text_only, got_bandate)],
            CAUSE: [
                CommandHandler(COMMANDS["skip"], skip_cause),
                MessageHandler(text_only, got_cause),
            ],
            COMMENT: [MessageHandler(text_only, got_comment)],
            CONFIRM: [
                MessageHandler(filters.Regex(f"^{re.escape(t('btn_send'))}$"), send_appeal),
                MessageHandler(filters.Regex(f"^{re.escape(t('btn_cancel'))}$"), cmd_cancel),
                MessageHandler(text_only, confirm_other),
            ],
        },
        fallbacks=[
            CommandHandler(COMMANDS["cancel"], cmd_cancel),
            MessageHandler(~filters.TEXT, need_text),
        ],
        allow_reentry=True,
    )
    app.add_handler(conv)

    app.add_handler(CallbackQueryHandler(on_admin_action, pattern=r"^a:"))
    app.add_handler(
        MessageHandler(
            admin_chat & filters.REPLY & filters.TEXT & ~filters.COMMAND, on_reason_reply
        )
    )
    app.add_handler(CommandHandler(COMMANDS["unblock"], cmd_unblock, filters=admin_chat))
    app.add_handler(CommandHandler(COMMANDS["blocked"], cmd_blocked, filters=admin_chat))
    app.add_handler(CommandHandler(COMMANDS["status"], cmd_status, filters=private))
    app.add_handler(CommandHandler("chatid", cmd_chatid))
    app.add_handler(MessageHandler(private & ~filters.COMMAND, private_fallback))
    app.add_error_handler(on_error)

    log.info("Bot started (language: %s).", BOT_LANG)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
