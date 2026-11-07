# Telegram Appeal Bot

[🇬🇧 English](README.md) | 🇮🇹 Italiano

Bot Telegram per gestire gli **appelli di sban** di un gruppo.

Gli utenti bannati scrivono al bot in privato e compilano una richiesta guidata. La richiesta arriva in un gruppo riservato agli amministratori, che approvano, negano o bloccano con un pulsante. All'approvazione il bot può anche rimuovere il ban in automatico e inviare all'utente il link per rientrare.

Basta appelli dispersi su altre piattaforme che nessuno legge.

## Funzionalità

- **Modulo guidato** in chat privata, con riepilogo e conferma prima dell'invio:
  - account dell'utente (username e ID) rilevato automaticamente e mostrato a schermo, così nessuno può fare appello per conto di altri;
  - data e ora indicativa del ban;
  - causa del ban (facoltativa, `/salta`);
  - commento libero.
- **Un solo appello aperto per utente**, garantito anche a livello di database. Dopo una decisione se ne può inviare uno nuovo.
- **Gruppo admin** con pulsanti sul messaggio dell'appello:
  - ✅ **Approva**;
  - ❌ **Nega**, con motivo facoltativo inviato all'utente;
  - 🚫 **Nega e blocca**, con motivo facoltativo; l'utente viene avvisato che non potrà inviare altri appelli;
  - 🚫 **Blocca appelli utente** anche dopo un diniego, per chi insiste;
  - 🔓 **Sblocca**.
- Il messaggio dell'appello mantiene uno **storico delle decisioni** (chi, quando, motivo) e il numero di appelli precedenti dell'utente.
- **Sban automatico** (facoltativo) all'approvazione:
  - il bot verifica prima che l'utente sia davvero nella lista dei bannati del gruppo principale;
  - se lo è, lo sbanna e scrive nel gruppo `✅ Utente riammesso: @username`;
  - se non lo è, non fa nulla e avvisa nel gruppo admin.
- **Link d'invito** (facoltativo) inviato all'utente approvato.
- Interfaccia in **inglese o italiano** (`BOT_LANG`). I comandi funzionano con entrambi i nomi, per esempio `/annulla` e `/cancel`.
- Nessuno stato in memoria per le decisioni: tutto è salvato su SQLite e resiste ai riavvii.

## Come funziona

```
Utente ──/start──▶ Bot (privato) ──appello──▶ Gruppo admin
                                                  │
                         ┌────────────────────────┼─────────────────────────┐
                      Approva                    Nega                Nega e blocca
                         │                  (motivo facolt.)       (motivo facolt.)
              sban + "Utente riammesso"           │                         │
              nel gruppo principale                ▼                         ▼
                         │                  notifica utente     notifica utente + blocco
                         ▼
          notifica utente + link d'invito
```

## Requisiti

- Linux con systemd (gli esempi sotto sono per Debian/Ubuntu)
- Python 3.10 o superiore, con `venv`
- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) 21.x o 22.x e python-dotenv, entrambi installati da `requirements.txt`

## Installazione

Il bot gira con un **utente dedicato senza privilegi** (`appealbot`), mai come root:

- codice e virtualenv appartengono a root e sono in sola lettura per il bot;
- la configurazione è leggibile solo da root e dal bot;
- il database sta in `/var/lib/telegram-appeal-bot`, cartella creata da systemd.

Esegui da root:

```bash
# 1. Dipendenze
apt install python3 python3-venv git

# 2. Utente di sistema dedicato: senza home e senza shell di login
useradd --system --user-group --no-create-home --shell /usr/sbin/nologin appealbot

# 3. Codice e virtualenv (di proprietà di root)
git clone https://github.com/Leproide/telegram-appeal-bot.git /opt/telegram-appeal-bot
python3 -m venv /opt/telegram-appeal-bot/venv
/opt/telegram-appeal-bot/venv/bin/pip install -r /opt/telegram-appeal-bot/requirements.txt

# 4. Configurazione, leggibile solo da root e dal bot
cp /opt/telegram-appeal-bot/.env.example /opt/telegram-appeal-bot/.env
chown root:appealbot /opt/telegram-appeal-bot/.env
chmod 640 /opt/telegram-appeal-bot/.env
nano /opt/telegram-appeal-bot/.env    # imposta almeno BOT_TOKEN e BOT_LANG=it

# 5. Servizio
cp /opt/telegram-appeal-bot/telegram-appeal-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now telegram-appeal-bot
```

Se cambi percorsi o nome utente, aggiorna di conseguenza `telegram-appeal-bot.service`. Nello stesso file cambia anche `TZ` se non sei in `Europe/Rome`.

## Configurazione

1. **Crea il bot** con [@BotFather](https://t.me/BotFather) e inserisci il token in `BOT_TOKEN`.
2. **Modalità setup**: con `ADMIN_CHAT_ID` vuoto il bot risponde solo a `/chatid`. È lo stato in cui si trova dopo il passo 5 dell'installazione. Verificalo con `journalctl -u telegram-appeal-bot -f`.
3. **Gruppo principale** (da cui si sbanna):
   - aggiungi il bot come **amministratore** con il permesso *Ban utenti*; serve solo con `AUTO_UNBAN=true`;
   - deve poter anche scrivere messaggi, per l'annuncio di riammissione;
   - poi scrivi `/chatid@NomeDelBot` e copia l'ID in `MAIN_CHAT_ID`.
4. **Gruppo admin** (dove arrivano gli appelli): aggiungi il bot, scrivi `/chatid@NomeDelBot` e copia l'ID in `ADMIN_CHAT_ID`.
5. Imposta le opzioni facoltative e riavvia il bot con `systemctl restart telegram-appeal-bot`.

| Variabile | Obbligatoria | Descrizione |
|---|---|---|
| `BOT_TOKEN` | sì | Token di @BotFather |
| `BOT_LANG` | no | `en` (default) oppure `it` |
| `ADMIN_CHAT_ID` | sì* | ID del gruppo admin. Se vuoto: modalità setup |
| `MAIN_CHAT_ID` | con `AUTO_UNBAN` | ID del gruppo principale |
| `AUTO_UNBAN` | no | `true` per sbannare all'approvazione (default `false`) |
| `INVITE_LINK` | no | Link inviato all'utente approvato |
| `REQUIRE_CHAT_ADMIN` | no | `true`: solo admin/owner del gruppo admin possono decidere (default `false`) |
| `DB_PATH` | no | Percorso del database SQLite (default `appeals.db`, cioè `/var/lib/telegram-appeal-bot/appeals.db` con la unit fornita) |

Note:
- Se il gruppo principale è un gruppo "base", nominare il bot amministratore può convertirlo in supergruppo e **cambiarne l'ID**: scrivi `/chatid` dopo la promozione.
- Usa la forma `/chatid@NomeDelBot`: con la privacy mode attiva un `/chatid` senza il nome del bot potrebbe non arrivargli.
- Per il motivo del diniego bisogna **rispondere al messaggio del bot**: con la privacy mode attiva il bot riceve solo le risposte ai propri messaggi.
- Lo sban automatico agisce sull'**account Telegram che ha inviato l'appello** (il suo ID).

## Comandi

| Dove | Comando | Funzione |
|---|---|---|
| Privato | `/start` | Inizia un appello |
| Privato | `/stato` · `/status` | Stato dell'ultimo appello |
| Privato | `/annulla` · `/cancel` | Annulla la compilazione |
| Privato | `/salta` · `/skip` | Salta la causa del ban (facoltativa) |
| Gruppo admin | `/sblocca <user_id>` · `/unblock` | Sblocca un utente |
| Gruppo admin | `/bloccati` · `/blocked` | Elenco degli utenti bloccati |
| Ovunque | `/chatid` | Mostra l'ID della chat (per la configurazione) |

Il bot pubblica il menu dei comandi nella lingua configurata.

## Aggiornamento e backup

Il database `/var/lib/telegram-appeal-bot/appeals.db` contiene lo storico degli appelli e la lista dei bloccati: includilo nei backup. All'avvio il bot aggiorna da solo lo schema del database, aggiungendo le colonne mancanti senza toccare i dati. Fai comunque un backup prima di aggiornare:

```bash
systemctl stop telegram-appeal-bot
cp /var/lib/telegram-appeal-bot/appeals.db /root/appeals.db.bak
cd /opt/telegram-appeal-bot && git pull
/opt/telegram-appeal-bot/venv/bin/pip install -r requirements.txt
systemctl start telegram-appeal-bot
```

### Migrazione da un'installazione esistente

Se il bot girava già altrove, per esempio come root con il database accanto a `bot.py`, sposta il database nella nuova posizione prima di avviare il servizio:

```bash
systemctl stop <vecchio-servizio>     # oppure ferma il bot avviato a mano
install -d -o appealbot -g appealbot -m 750 /var/lib/telegram-appeal-bot
install -o appealbot -g appealbot -m 600 /percorso/vecchio/appeals.db /var/lib/telegram-appeal-bot/appeals.db
```

Conserva la vecchia copia finché non hai verificato che tutto funzioni.

## Sicurezza

- Il bot gira con l'utente senza privilegi `appealbot`, con le opzioni di hardening di systemd. Può scrivere solo nella propria cartella di stato.
- Non pubblicare mai il file `.env`: contiene il token, ed è già escluso in `.gitignore`.
- Se il token viene esposto, rigeneralo da @BotFather con `/revoke` e aggiorna `.env`.
- I pulsanti di decisione funzionano solo dal gruppo admin e i comandi admin sono accettati solo lì. Gli appelli si compilano solo in chat privata.

## Annuncio per il gruppo

Messaggi pronti da pubblicare nel gruppo: [`docs/ANNUNCIO.md`](docs/ANNUNCIO.md) (italiano) e [`docs/ANNOUNCEMENT.md`](docs/ANNOUNCEMENT.md) (inglese).

## License

Distribuito sotto licenza **GNU General Public License v3.0 (GPL-3.0)**. Vedi il file [`LICENSE`](LICENSE).

## Author

https://github.com/Leproide
