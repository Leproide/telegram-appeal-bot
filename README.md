# Telegram Appeal Bot

🇬🇧 English | [🇮🇹 Italiano](README-ITALIAN.md)

A Telegram bot to handle **unban appeals** for a group.

Banned users write to the bot in private and fill in a guided request. The request is posted to a group reserved to admins, who approve, deny or block with one tap. On approval the bot can also lift the ban automatically and send the user the link to rejoin.

No more appeals scattered across other platforms where nobody reads them.

## Features

- **Guided form** in private chat, with a summary and confirmation before sending:
  - the user's account (username and ID) is detected automatically and shown on screen, so nobody can appeal on behalf of someone else;
  - a public **@username is required**: users without one are asked to set it and start again;
  - approximate date and time of the ban;
  - reason for the ban (optional, `/skip`);
  - free comment.
- **One open appeal per user**, also enforced by the database. After a decision a new one can be sent.
- **Admin group** with buttons on the appeal message:
  - ✅ **Approve**;
  - ❌ **Deny**, with an optional reason sent to the user;
  - 🚫 **Deny and block**, with an optional reason; the user is told they cannot send further appeals;
  - 🚫 **Block user's appeals** even after a denial, for users who insist;
  - 🔓 **Unblock**.
- The appeal message keeps a **decision history** (who, when, reason) and the number of the user's previous appeals.
- **Automatic unban** (optional) on approval:
  - the bot first checks that the user is actually in the main group's ban list;
  - if so, it unbans them and posts `✅ User readmitted: @username` in the group;
  - if not, it does nothing and warns the admin group.
- **Invite link** (optional) sent to approved users.
- **English or Italian** interface (`BOT_LANG`). Commands work under both names, e.g. `/cancel` and `/annulla`.
- No in-memory state for decisions: everything is stored in SQLite and survives restarts.

## How it works

```
User ──/start──▶ Bot (private) ──appeal──▶ Admin group
                                                │
                       ┌────────────────────────┼─────────────────────────┐
                    Approve                    Deny                 Deny and block
                       │                 (optional reason)        (optional reason)
            unban + "User readmitted"           │                         │
            in the main group                    ▼                         ▼
                       │                   notify user           notify user + block
                       ▼
        notify user + invite link
```

## Requirements

- Linux with systemd (Debian/Ubuntu examples below)
- Python 3.10 or newer, with `venv`
- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) 21.x or 22.x and python-dotenv, both installed from `requirements.txt`

## Installation

The bot runs as a **dedicated unprivileged user** (`appealbot`), never as root:

- the code and virtualenv are owned by root and are read-only for the bot;
- the configuration is readable only by root and the bot;
- the database lives in `/var/lib/telegram-appeal-bot`, created by systemd.

Run as root:

```bash
# 1. Dependencies
apt install python3 python3-venv git

# 2. Dedicated system user: no home, no login shell
useradd --system --user-group --no-create-home --shell /usr/sbin/nologin appealbot

# 3. Code and virtualenv (owned by root)
git clone https://github.com/Leproide/telegram-appeal-bot.git /opt/telegram-appeal-bot
python3 -m venv /opt/telegram-appeal-bot/venv
/opt/telegram-appeal-bot/venv/bin/pip install -r /opt/telegram-appeal-bot/requirements.txt

# 4. Configuration, readable only by root and the bot
cp /opt/telegram-appeal-bot/.env.example /opt/telegram-appeal-bot/.env
chown root:appealbot /opt/telegram-appeal-bot/.env
chmod 640 /opt/telegram-appeal-bot/.env
nano /opt/telegram-appeal-bot/.env    # set at least BOT_TOKEN and BOT_LANG

# 5. Service
cp /opt/telegram-appeal-bot/telegram-appeal-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now telegram-appeal-bot
```

If you change paths or the user name, update `telegram-appeal-bot.service` to match. Set `TZ` there too if you are not in `Europe/Rome`.

## Configuration

1. **Create the bot** with [@BotFather](https://t.me/BotFather) and put the token in `BOT_TOKEN`.
2. **Setup mode**: with `ADMIN_CHAT_ID` empty, the bot only answers `/chatid`. This is what you get after step 5 of the installation. Check it with `journalctl -u telegram-appeal-bot -f`.
3. **Main group** (where users are unbanned from):
   - add the bot and make it an **admin** with the *Ban users* permission; this is only needed with `AUTO_UNBAN=true`;
   - it must also be able to send messages, for the readmission announcement;
   - then send `/chatid@YourBotName` and copy the ID to `MAIN_CHAT_ID`.
4. **Admin group** (where appeals are posted): add the bot, send `/chatid@YourBotName` and copy the ID to `ADMIN_CHAT_ID`.
5. Set the optional variables and restart the bot with `systemctl restart telegram-appeal-bot`.

| Variable | Required | Description |
|---|---|---|
| `BOT_TOKEN` | yes | Token from @BotFather |
| `BOT_LANG` | no | `en` (default) or `it` |
| `ADMIN_CHAT_ID` | yes* | Admin group ID. If empty: setup mode |
| `MAIN_CHAT_ID` | with `AUTO_UNBAN` | Main group ID |
| `AUTO_UNBAN` | no | `true` to unban on approval (default `false`) |
| `INVITE_LINK` | no | Link sent to approved users |
| `REQUIRE_CHAT_ADMIN` | no | `true`: only admins/owner of the admin group can decide (default `false`) |
| `DB_PATH` | no | SQLite database path (default `appeals.db`, i.e. `/var/lib/telegram-appeal-bot/appeals.db` with the provided unit) |

Notes:
- If the main group is a basic group, promoting the bot to admin may convert it to a supergroup and **change its ID**: send `/chatid` after the promotion.
- Use the form `/chatid@YourBotName`: with privacy mode on, a plain `/chatid` may not reach the bot.
- To give a denial reason, **reply to the bot's prompt message**: with privacy mode on, the bot only receives replies to its own messages.
- The automatic unban acts on the **Telegram account that sent the appeal** (its ID).

## Commands

| Where | Command | Function |
|---|---|---|
| Private | `/start` | Start an appeal |
| Private | `/status` · `/stato` | Status of the last appeal |
| Private | `/cancel` · `/annulla` | Cancel the form |
| Private | `/skip` · `/salta` | Skip the (optional) ban reason |
| Admin group | `/unblock <user_id>` · `/sblocca` | Unblock a user |
| Admin group | `/blocked` · `/bloccati` | List blocked users |
| Anywhere | `/chatid` | Show the chat ID (for setup) |

The bot publishes the command menu in the configured language.

## Updates and backups

The database `/var/lib/telegram-appeal-bot/appeals.db` holds the appeal history and the block list: include it in your backups. On startup the bot updates the database schema by itself, adding missing columns without touching existing data. Back up before updating anyway:

```bash
systemctl stop telegram-appeal-bot
cp /var/lib/telegram-appeal-bot/appeals.db /root/appeals.db.bak
cd /opt/telegram-appeal-bot && git pull
/opt/telegram-appeal-bot/venv/bin/pip install -r requirements.txt
systemctl start telegram-appeal-bot
```

### Migrating from an existing installation

If the bot used to run elsewhere, for example as root with the database next to `bot.py`, move the database to the new location before starting the service:

```bash
systemctl stop <old-service>     # or stop the manually started bot
install -d -o appealbot -g appealbot -m 750 /var/lib/telegram-appeal-bot
install -o appealbot -g appealbot -m 600 /path/to/old/appeals.db /var/lib/telegram-appeal-bot/appeals.db
```

Keep the old copy until you have verified that everything works.

## Security

- The bot runs as the unprivileged `appealbot` user, with systemd hardening. It can write only to its own state directory.
- Never publish the `.env` file: it contains the token, and it is already excluded in `.gitignore`.
- If the token is exposed, regenerate it from @BotFather with `/revoke` and update `.env`.
- Decision buttons work only from the admin group, and admin commands are accepted only there. Appeals can be filled in only in private chat.

## Group announcement

Ready-made messages to post in your group: [`docs/ANNOUNCEMENT.md`](docs/ANNOUNCEMENT.md) (English) and [`docs/ANNUNCIO.md`](docs/ANNUNCIO.md) (Italian).

## License

Released under the **GNU General Public License v3.0 (GPL-3.0)**. See [`LICENSE`](LICENSE).

## Author

https://github.com/Leproide
