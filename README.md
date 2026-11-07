# Telegram Appeal Bot

Bot Telegram per gestire gli **appelli di sban** di un gruppo.

Gli utenti bannati scrivono al bot in privato e compilano una richiesta guidata. La richiesta arriva in un gruppo riservato agli amministratori, che approvano, negano o bloccano con un pulsante. All'approvazione il bot può anche rimuovere il ban in automatico e inviare all'utente il link per rientrare.

Basta appelli dispersi su altre piattaforme che nessuno legge.

## Funzionalità

- **Modulo guidato** in chat privata, con riepilogo e conferma prima dell'invio:
  - account dell'utente (username e ID) rilevato automaticamente e mostrato a schermo: non si può fare appello per conto di altri;
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
  - verifica prima che l'utente sia davvero tra i bannati del gruppo principale;
  - se lo è, lo sbanna e scrive nel gruppo `✅ Utente riammesso: @username`;
  - se non lo è, non fa nulla e avvisa nel gruppo admin.
- **Link d'invito** (facoltativo) inviato all'utente approvato.
- Nessuno stato in memoria per le decisioni: tutto è salvato su SQLite e funziona anche dopo un riavvio.

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

- Python 3.10 o superiore
- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) 21.x o 22.x
- python-dotenv

## Installazione

```bash
git clone https://github.com/Leproide/telegram-appeal-bot.git /opt/telegram-appeal-bot
cd /opt/telegram-appeal-bot
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
```

In alternativa al virtualenv: `pip install -r requirements.txt` e avvio con `python3 bot.py`.

## Configurazione

1. **Crea il bot** con [@BotFather](https://t.me/BotFather) e inserisci il token in `BOT_TOKEN` nel file `.env`.
2. **Avvia in modalità setup**: con `ADMIN_CHAT_ID` vuoto il bot risponde solo a `/chatid`.
   ```bash
   venv/bin/python bot.py
   ```
3. **Gruppo principale** (da cui si sbanna):
   - aggiungi il bot come **amministratore** con il permesso *Ban utenti*; serve solo con `AUTO_UNBAN=true`;
   - deve poter anche scrivere messaggi, per l'annuncio di riammissione;
   - poi scrivi `/chatid@NomeDelBot` e copia l'ID in `MAIN_CHAT_ID`.
4. **Gruppo admin** (dove arrivano gli appelli): aggiungi il bot, scrivi `/chatid@NomeDelBot` e copia l'ID in `ADMIN_CHAT_ID`.
5. Imposta le opzioni facoltative e **riavvia** il bot.

| Variabile | Obbligatoria | Descrizione |
|---|---|---|
| `BOT_TOKEN` | sì | Token di @BotFather |
| `ADMIN_CHAT_ID` | sì* | ID del gruppo admin. Se vuoto: modalità setup |
| `MAIN_CHAT_ID` | con `AUTO_UNBAN` | ID del gruppo principale |
| `AUTO_UNBAN` | no | `true` per sbannare all'approvazione (default `false`) |
| `INVITE_LINK` | no | Link inviato all'utente approvato |
| `REQUIRE_CHAT_ADMIN` | no | `true`: solo admin/owner del gruppo admin possono decidere (default `false`) |
| `DB_PATH` | no | Percorso del database SQLite (default `appeals.db`) |

Note:
- Se il gruppo principale è un gruppo "base", nominare il bot amministratore può convertirlo in supergruppo e **cambiarne l'ID**: scrivi `/chatid` dopo la promozione.
- Usa la forma `/chatid@NomeDelBot`: con la privacy mode attiva un `/chatid` senza il nome del bot potrebbe non arrivargli.
- Per il motivo del diniego bisogna **rispondere al messaggio del bot**: con la privacy mode attiva il bot riceve solo le risposte ai propri messaggi.
- Lo sban automatico agisce sull'**account Telegram che ha inviato l'appello** (ID).

## Comandi

| Dove | Comando | Funzione |
|---|---|---|
| Privato | `/start` | Inizia un appello |
| Privato | `/stato` | Stato dell'ultimo appello |
| Privato | `/annulla` | Annulla la compilazione |
| Privato | `/salta` | Salta la causa (facoltativa) |
| Gruppo admin | `/sblocca <user_id>` | Sblocca un utente |
| Gruppo admin | `/bloccati` | Elenco degli utenti bloccati |
| Ovunque | `/chatid` | Mostra l'ID della chat (per la configurazione) |

## Avvio come servizio (systemd)

```bash
cp telegram-appeal-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now telegram-appeal-bot
journalctl -u telegram-appeal-bot -f
```

Adatta `WorkingDirectory`, `ExecStart`, `ReadWritePaths` e `TZ` se usi percorsi o fusi orari diversi.

## Aggiornamento e backup

Il database `appeals.db` contiene lo storico degli appelli e la lista dei bloccati: includilo nei backup. All'avvio il bot aggiorna da solo lo schema del database, aggiungendo le colonne mancanti senza toccare i dati. Fai comunque un backup prima di aggiornare:

```bash
systemctl stop telegram-appeal-bot
cp appeals.db appeals.db.bak
git pull
systemctl start telegram-appeal-bot
```

## Sicurezza

- Non pubblicare mai il file `.env`: contiene il token, ed è già escluso in `.gitignore`.
- Se il token viene esposto, rigeneralo da @BotFather con `/revoke` e aggiorna `.env`.
- I pulsanti di decisione funzionano solo dal gruppo admin; i comandi admin sono accettati solo lì; la compilazione degli appelli avviene solo in chat privata.

## Annuncio per il gruppo

Un modello di messaggio da pubblicare nel gruppo è in [`docs/ANNUNCIO.md`](docs/ANNUNCIO.md).

## License

Distribuito sotto licenza **GNU General Public License v3.0 (GPL-3.0)**. Vedi il file [`LICENSE`](LICENSE).

## Author

https://github.com/Leproide
