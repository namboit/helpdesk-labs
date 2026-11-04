# helpdesk-labs

A production-ready IT helpdesk system built on **Telegram**. Staff submit tickets through a guided bot wizard or an embedded Telegram MiniApp — no external apps, accounts, or training required. IT teams manage, escalate, and close tickets directly inside Telegram groups with inline keyboard controls.

Built with Python, FastAPI, python-telegram-bot, and SQLite. Runs on a single server with zero external service dependencies.

---

## Features

**Bot interface** (`bot.py`)
- Multi-step guided wizard: routes staff through property → department → category → description → photo
- Inline keyboard controls for IT: mark in-progress, close, reopen, add notes, reassign
- `/list`, `/my`, `/export`, `/report`, `/delete`, `/restore` commands
- Photo attachment support for both ticket submission and resolution evidence
- Auto-detects close intent from natural language replies in group chats
- Soft-delete with restore capability; full audit event log per ticket

**Web MiniApp** (`ticket_miniapp.py` + `index.html`)
- Embedded Telegram WebApp — opens inside Telegram on mobile and desktop
- Staff tab: submit new tickets with form UI (no `/commands` needed)
- My tickets tab: view personal ticket history and status
- Admin tab: IT team full ticket management with inline status controls and photo gallery
- Analysis tab: monthly statistics, resolution time, category breakdown
- Bilingual UI (English + Khmer) with live language toggle
- Streaming Excel export via Pandas + XlsxWriter with formatted headers, zebra striping, and frozen panes

**Backend** (`ticket_miniapp.py`)
- FastAPI with Pydantic v2 validation
- SQLite with WAL mode and foreign keys — crash-safe, no server required
- Idempotent schema migrations via `ensure_col()` — safe to redeploy over live data
- Composite indexes on `status`, `open_ts_utc`, and `deleted` for fast filtered queries
- Pushes live Telegram message updates (edit-in-place) when ticket status changes from the web UI

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Telegram Client                          │
│                                                                 │
│   Private Chat (DM)          Group Chat (IT Log Channel)        │
│   ┌──────────────┐           ┌─────────────────────────────┐   │
│   │ Ticket wizard │           │  Posted ticket messages      │   │
│   │ /new command  │           │  with inline action buttons  │   │
│   │ MiniApp btn   │           │  (status, close, note)       │   │
│   └──────┬───────┘           └──────────────┬──────────────┘   │
└──────────┼────────────────────────────────── ┼─────────────────┘
           │  python-telegram-bot polling       │
           ▼                                   │
  ┌────────────────┐    mutate DB    ┌──────────┴───────┐
  │    bot.py      │◄───────────────►│  tickets.db      │
  │ (PTB handlers) │                 │  (SQLite WAL)    │
  └───────┬────────┘                 └──────────▲───────┘
          │ webhook / REST calls                 │
          ▼                                      │
  ┌────────────────┐   CRUD + export  ────────────┘
  │ticket_miniapp  │◄────────────────
  │  (FastAPI)     │
  └───────┬────────┘
          │  serves
          ▼
  ┌────────────────┐
  │  index.html    │
  │ (Telegram      │
  │  WebApp UI)    │
  └────────────────┘
```

---

## Stack

| Layer | Technology |
|---|---|
| Bot framework | [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) v21 |
| Web API | [FastAPI](https://fastapi.tiangolo.com/) + [Uvicorn](https://www.uvicorn.org/) |
| Validation | [Pydantic](https://docs.pydantic.dev/) v2 |
| Database | SQLite (WAL mode, foreign keys) |
| Data export | [Pandas](https://pandas.pydata.org/) + [XlsxWriter](https://xlsxwriter.readthedocs.io/) |
| Frontend | Vanilla JS + [HTMX](https://htmx.org/) + [Telegram WebApp SDK](https://core.telegram.org/bots/webapps) |
| Timezone | `zoneinfo` (Python 3.9+) |

---

## Quickstart

### Prerequisites
- Python 3.11+
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- A Telegram group chat where the bot has admin rights (for the IT log channel)

### 1. Clone & install

```bash
git clone https://github.com/namboit/helpdesk-labs.git
cd helpdesk-labs

python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env — set BOT_TOKEN, LOG_CHAT_ID, your IT admin IDs, and your org's branches/departments
```

Key variables in `.env`:

| Variable | Description |
|---|---|
| `BOT_TOKEN` | Telegram bot token from BotFather |
| `LOG_CHAT_ID` | Group chat ID where ticket posts appear |
| `BRANCHES` | Comma-separated list of your org's locations/properties |
| `DEPARTMENTS` | Comma-separated list of departments |
| `IT_USER_IDS` | Comma-separated Telegram user IDs with admin access |
| `IT_USERNAMES` | Comma-separated Telegram usernames with admin access |
| `TZ` | IANA timezone (e.g. `Asia/Bangkok`, `Asia/Singapore`) |

### 3. Run

**Bot only** (Telegram command interface):
```bash
BOT_TOKEN=your_token python bot.py
```

**Web MiniApp API + UI** (run alongside the bot):
```bash
BOT_TOKEN=your_token LOG_CHAT_ID=your_chat_id python ticket_miniapp.py
```

The FastAPI server starts at `http://localhost:8001`. Point your MiniApp URL in BotFather to `http://your-server:8001/`.

### 4. Set the MiniApp URL

In [@BotFather](https://t.me/BotFather):
1. `/mybots` → select your bot → Bot Settings → Menu Button
2. Set the URL to your server: `https://your-domain.com/`

Staff will see a **"Open Portal"** button in the bot chat that opens the MiniApp directly inside Telegram.

---

## Project Structure

```
helpdesk-labs/
├── bot.py               # Telegram bot — polling, wizard, inline keyboards
├── ticket_miniapp.py    # FastAPI web API + serves index.html
├── index.html           # Telegram MiniApp frontend (vanilla JS + HTMX)
├── requirements.txt     # Python dependencies
├── .env.example         # Environment variable template
└── .gitignore
```

---

## Configuration Reference

All org-specific options are environment-driven — no code changes needed to deploy this for a different organisation.

```bash
# .env
BOT_TOKEN=...
LOG_CHAT_ID=...
TZ=Asia/Bangkok

BRANCHES=Head Office,Warehouse,Remote Site,Other
DEPARTMENTS=IT,HR,Finance,Operations,Admin
CATEGORIES=Internet/WiFi,Computer,Printer,Software,CCTV,Access Control,Other

IT_USER_IDS=123456789,987654321
IT_USERNAMES=itadmin,helpdeskteam
```

---

## Ticket Lifecycle

```
Staff submits via bot wizard or MiniApp form
        │
        ▼
   OPEN  ──► IN-PROGRESS  ──► CLOSED
        ▲                        │
        └────────── REOPEN ───── ┘
```

Every status change creates an event log entry with author and timestamp. Closed tickets retain their resolution note and closing photo.

---

## License

MIT
