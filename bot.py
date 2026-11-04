import os
import re
import sqlite3
import datetime
from contextlib import closing
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo
import warnings

import pandas as pd
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode, ChatType
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from telegram.warnings import PTBUserWarning

warnings.filterwarnings("ignore", category=PTBUserWarning)

def dbg(msg: str):
    """Quick and dirty debug logger."""
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[DEBUG {now}] {msg}")


# ======================
# CONFIG
# ======================

BOT_TOKEN = os.getenv("BOT_TOKEN")
LOCAL_TZ = ZoneInfo(os.getenv("TZ", "Asia/Phnom_Penh"))
DB_PATH = Path("tickets.db")

# Ticket log group chat ID
LOG_CHAT_ID = int(os.getenv("LOG_CHAT_ID", "0") or "0")

# Wizard options
BRANCHES = [
    v.strip() for v in os.getenv("BRANCHES", "Head Office,Branch A,Branch B,Other").split(",")
    if v.strip()
]
DEPARTMENTS = [
    v.strip() for v in os.getenv("DEPARTMENTS", "IT,HR,Finance,Sales,Marketing,Admin").split(",")
    if v.strip()
]
CATEGORIES = [
    v.strip() for v in os.getenv("CATEGORIES", "Internet/WiFi,Computer,Printer,Software,CCTV,Other").split(",")
    if v.strip()
]

# IT/admin IDs
IT_IDS = {int(x.strip()) for x in os.getenv("IT_USER_IDS", "").split(",") if x.strip().isdigit()}
IT_USERNAMES = {x.strip().lower() for x in os.getenv("IT_USERNAMES", "").split(",") if x.strip()}  # lowercase, no "@"

# Statuses
ALL_STATUSES = ["OPEN", "IN-PROGRESS", "CLOSED"]
DEFAULT_STATUS = "OPEN"

# Close markers in text replies
CLOSE_MARKERS = [
    r"✅",
    r"\[DONE\]",
    r":white_check_mark:",
    r"\b(resolved|resolve|complete|completed|fixed)\b",
]

# Conversation states
BRANCH, DEPT, CAT, DESC, CONFIRM = range(5)

USER_TICKET_CONFIRM = "✅ Ticket #{id} created.\nOur IT team has been notified."

# In-memory state: IT user_id -> ticket_id
CLOSING_TICKET_BY_USER: dict[int, int] = {}
NOTE_TICKET_BY_USER: dict[int, int] = {}

# Telegram limits
MAX_TEXT_LEN = 4096
SAFE_TEXT_LEN = 3500  # safety margin
MAX_CAPTION_LEN = 1024
SAFE_CAPTION_LEN = 900  # safety margin


# ======================
# UTIL
# ======================

def utcnow_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def db() -> sqlite3.Connection:
    """
    Create a new DB connection.
    We keep it per-operation (no sharing across threads).
    """
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db() -> None:
    """
    Run once at startup: ensure schema & indexes.
    Designed to be idempotent.
    """
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")

        # Base tables (new schema, but CREATE IF NOT EXISTS won't modify existing)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                open_msg_id INTEGER NOT NULL,
                bot_msg_id INTEGER,
                open_user TEXT,
                open_user_id INTEGER,
                open_text TEXT,
                open_ts_utc TEXT NOT NULL,
                tags TEXT,
                assignees TEXT,
                status TEXT DEFAULT 'OPEN',
                branch TEXT,
                department TEXT,
                category TEXT,
                close_note TEXT,
                photo_file_id TEXT,
                is_photo INTEGER DEFAULT 0,
                closed_ts_utc TEXT,
                close_photo_file_id TEXT,
                deleted INTEGER DEFAULT 0
            );
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER,
                chat_id INTEGER,
                msg_id INTEGER,
                kind TEXT,
                author TEXT,
                text TEXT,
                ts_utc TEXT NOT NULL
            );
            """
        )

        # Auto-migrate: add missing columns for older DBs
        def ensure_col(table: str, col_name: str, col_type: str) -> None:
            cols = {
                r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if col_name not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type};")

        ensure_col("tickets", "open_user_id", "INTEGER")
        ensure_col("tickets", "deleted", "INTEGER DEFAULT 0")
        ensure_col("tickets", "bot_msg_id", "INTEGER")
        ensure_col("tickets", "branch", "TEXT")
        ensure_col("tickets", "department", "TEXT")
        ensure_col("tickets", "category", "TEXT")
        ensure_col("tickets", "status", "TEXT")
        ensure_col("tickets", "close_note", "TEXT")
        ensure_col("tickets", "photo_file_id", "TEXT")
        ensure_col("tickets", "is_photo", "INTEGER")
        ensure_col("tickets", "closed_ts_utc", "TEXT")
        ensure_col("tickets", "close_photo_file_id", "TEXT")

        # Indexes
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tickets_open_ts ON tickets(open_ts_utc);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tickets_deleted ON tickets(deleted);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_ticket_ts ON events(ticket_id, ts_utc);"
        )

        conn.commit()


def is_it_user(u) -> bool:
    if not u:
        return False
    if u.id in IT_IDS:
        return True
    uname = (u.username or "").lower()
    return uname in IT_USERNAMES


def is_close_text(text: str) -> bool:
    if not text:
        return False
    if text.strip().lower().startswith("/done"):
        return True
    for pat in CLOSE_MARKERS:
        if re.search(pat, text, flags=re.IGNORECASE):
            return True
    return False


def dm_ticket_url(bot_username: str) -> str:
    """Deep link for /ticket wizard."""
    return f"https://t.me/{bot_username}?start=ticket"


def truncate_for_text(s: str, max_len: int) -> str:
    """Ensure text doesn't exceed Telegram limits."""
    if not s:
        return s
    if len(s) <= max_len:
        return s
    return s[: max_len - 20] + " … [truncated]"


# ======================
# DB HELPERS
# ======================

def row_to_dict(conn: sqlite3.Connection, table: str, row) -> dict:
    cols = [c[1] for c in conn.execute(f"PRAGMA table_info({table})")]
    return dict(zip(cols, row))


def get_ticket(conn: sqlite3.Connection, ticket_id: int) -> dict | None:
    cur = conn.cursor()
    cur.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,))
    row = cur.fetchone()
    if not row:
        return None
    return row_to_dict(conn, "tickets", row)


# ======================
# KEYBOARDS & RENDERING
# ======================

def kb_properties() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(name, callback_data=f"PROP:{name}")]
        for name in BRANCHES
    ]
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="CANCEL")])
    return InlineKeyboardMarkup(buttons)


def kb_departments() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(name, callback_data=f"DEPT:{name}")]
        for name in DEPARTMENTS
    ]
    buttons.append(
        [
            InlineKeyboardButton("⬅️ Back", callback_data="BACK_TO_PROP"),
            InlineKeyboardButton("❌ Cancel", callback_data="CANCEL"),
        ]
    )
    return InlineKeyboardMarkup(buttons)


def kb_categories() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(name, callback_data=f"CAT:{name}")]
        for name in CATEGORIES
    ]
    buttons.append(
        [
            InlineKeyboardButton("⬅️ Back", callback_data="BACK_TO_DEPT"),
            InlineKeyboardButton("❌ Cancel", callback_data="CANCEL"),
        ]
    )
    return InlineKeyboardMarkup(buttons)


def kb_controls(ticket_id: int, status: str) -> InlineKeyboardMarkup | None:
    """
    Inline controls under the ticket in the group:
    - OPEN: [▶ In-Progress] [✅ Close]
    - IN-PROGRESS: [⏮ Back to OPEN] [✅ Close]
    - CLOSED: none
    """
    if status == "CLOSED":
        return None

    if status == "IN-PROGRESS":
        row = [
            InlineKeyboardButton(
                "⏮ Back to OPEN", callback_data=f"STATUS:{ticket_id}:OPEN"
            ),
            InlineKeyboardButton(
                "✅ Close Ticket", callback_data=f"CLOSE:{ticket_id}"
            ),
        ]
    else:
        row = [
            InlineKeyboardButton(
                "▶ In-PROGRESS", callback_data=f"STATUS:{ticket_id}:IN-PROGRESS"
            ),
            InlineKeyboardButton(
                "✅ Close Ticket", callback_data=f"CLOSE:{ticket_id}"
            ),
        ]
    return InlineKeyboardMarkup([row])


def render_ticket_text(t: dict) -> str:
    status = t.get("status") or DEFAULT_STATUS

    if status == "CLOSED":
        header = f"🛑 CLOSED – Ticket #{t['id']}"
    elif status == "IN-PROGRESS":
        header = f"🟡 IN-PROGRESS – Ticket #{t['id']}"
    else:
        header = f"🎫 Ticket #{t['id']}"

    parts: list[str] = [header]

    # Opened by
    if t.get("open_user"):
        parts.append(f"Opened by: {t['open_user']}")

    # Open date local
    open_ts = t.get("open_ts_utc")
    if open_ts:
        try:
            dt = datetime.datetime.fromisoformat(open_ts)
            local = dt.astimezone(LOCAL_TZ)
            parts.append("Opened: " + local.strftime("%Y-%m-%d %H:%M"))
        except Exception:
            pass

    # Closed date local
    closed_ts = t.get("closed_ts_utc")
    if closed_ts:
        try:
            dtc = datetime.datetime.fromisoformat(closed_ts)
            localc = dtc.astimezone(LOCAL_TZ)
            parts.append("Closed: " + localc.strftime("%Y-%m-%d %H:%M"))
        except Exception:
            pass

    parts.append(f"Status: {status}")

    if t.get("branch"):
        parts.append(f"Property: {t['branch']}")
    if t.get("department"):
        parts.append(f"Dept: {t['department']}")
    if t.get("category"):
        parts.append(f"Type: {t['category']}")

    desc = (t.get("open_text") or "").strip()
    desc = truncate_for_text(desc, SAFE_TEXT_LEN)
    parts.append(f"Desc: {desc or '(no description)'}")

    if t.get("photo_file_id"):
        parts.append("User Photo: attached")
    if t.get("close_photo_file_id"):
        parts.append("IT Photo: attached")
    if t.get("close_note"):
        note = truncate_for_text(t["close_note"], SAFE_TEXT_LEN)
        parts.append(f"IT Note: {note}")

    if t.get("deleted"):
        parts.append("⚠️ [DELETED] (soft)")

    text = "\n".join(parts)
    return truncate_for_text(text, SAFE_TEXT_LEN)


async def update_ticket_message(
    bot, tdict: dict, remove_keyboard: bool = False
) -> None:
    """
    Edit the main ticket message (photo caption or text) after changes.
    """
    txt = render_ticket_text(tdict)
    chat_id = tdict["chat_id"]
    msg_id = tdict.get("bot_msg_id")
    if not msg_id:
        return

    rm = None
    if not remove_keyboard and not tdict.get("deleted"):
        rm = kb_controls(tdict["id"], tdict.get("status", DEFAULT_STATUS))

    try:
        if tdict.get("is_photo"):
            caption = truncate_for_text(txt, SAFE_CAPTION_LEN)
            await bot.edit_message_caption(
                chat_id=chat_id,
                message_id=msg_id,
                caption=caption,
                reply_markup=rm,
            )
        else:
            text = truncate_for_text(txt, SAFE_TEXT_LEN)
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=text,
                reply_markup=rm,
            )
    except Exception as e:
        # Common: "Message is not modified" or message too old
        print("Error updating ticket message:", repr(e))


async def notify_ticket_user(bot, tdict: dict, message: str) -> None:
    """
    DM the original ticket opener if we have their Telegram ID.
    """
    uid = tdict.get("open_user_id")
    if not uid:
        return
    try:
        await bot.send_message(chat_id=uid, text=message)
    except Exception as e:
        print("Error DMing ticket user:", repr(e))


# ======================
# WIZARD: /ticket /start
# ======================

async def cmd_ticket(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Start ticket wizard:
    - In group: send deep-link button to DM
    - In DM: open property selection
    """
    chat = update.effective_chat
    msg = update.effective_message

    dbg(
        f"cmd_ticket fired: chat_id={chat.id}, chat_type={chat.type}, "
        f"text={getattr(msg, 'text', None)!r}"
    )

    ctx.user_data.clear()

    if chat.type != ChatType.PRIVATE:
        url = dm_ticket_url(ctx.bot.username)
        kb_panel = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🎫 Open Ticket", url=url)]]
        )
        await msg.reply_text(
            "Please open tickets in a private chat with me:",
            reply_markup=kb_panel,
        )
        return ConversationHandler.END

    wizard_msg = await msg.reply_text(
        "Choose property:", reply_markup=kb_properties()
    )
    ctx.user_data["wizard_msg_id"] = wizard_msg.message_id
    return BRANCH



async def pick_branch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""

    if data == "CANCEL":
        await q.edit_message_text("Cancelled.")
        return ConversationHandler.END

    if not data.startswith("PROP:"):
        return BRANCH

    prop = data.split(":", 1)[1]
    ctx.user_data["branch"] = prop
    await q.edit_message_text(
        f"Property: *{prop}*\n\nChoose department:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb_departments(),
    )
    return DEPT


async def pick_dept(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""

    if data == "CANCEL":
        await q.edit_message_text("Cancelled.")
        return ConversationHandler.END

    if data == "BACK_TO_PROP":
        await q.edit_message_text(
            "Choose property:", reply_markup=kb_properties()
        )
        return BRANCH

    if not data.startswith("DEPT:"):
        return DEPT

    dept = data.split(":", 1)[1]
    ctx.user_data["department"] = dept
    await q.edit_message_text(
        f"Property: *{ctx.user_data['branch']}*\n"
        f"Department: *{dept}*\n\nChoose problem type:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb_categories(),
    )
    return CAT


async def pick_cat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""

    if data == "CANCEL":
        await q.edit_message_text("Cancelled.")
        return ConversationHandler.END

    if data == "BACK_TO_DEPT":
        await q.edit_message_text(
            f"Property: *{ctx.user_data['branch']}*\n\nChoose department:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_departments(),
        )
        return DEPT

    if not data.startswith("CAT:"):
        return CAT

    category = data.split(":", 1)[1]
    ctx.user_data["category"] = category
    await q.edit_message_text(
        "Great. Now send a short description.\n"
        "You can also attach *one photo* (screenshot or phone photo) with a caption.\n\n"
        "_(You can also send /cancel to stop.)_",
        parse_mode=ParseMode.MARKDOWN,
    )
    return DESC


async def get_desc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message

    photo_file_id = None
    if msg.photo:
        photo_file_id = msg.photo[-1].file_id
        desc = (msg.caption or "").strip()
    else:
        desc = (msg.text or "").strip()

    if not desc:
        desc = "(no description)"

    ctx.user_data["desc"] = desc
    ctx.user_data["photo_file_id"] = photo_file_id

    b = ctx.user_data["branch"]
    d = ctx.user_data["department"]
    c = ctx.user_data["category"]
    t = ctx.user_data["desc"]
    photo_flag = "Yes" if photo_file_id else "No"

    summary = (
        "Please confirm ticket:\n"
        f"• Property: {b}\n"
        f"• Department: {d}\n"
        f"• Category: {c}\n"
        f"• Description: {t}\n"
        f"• Photo: {photo_flag}"
    )

    await msg.reply_text(
        summary,
        # no parse_mode here on purpose (user text inside)
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✅ Confirm", callback_data="CONFIRM")],
                [
                    InlineKeyboardButton("⬅️ Back", callback_data="BACK_TO_CAT"),
                    InlineKeyboardButton("❌ Cancel", callback_data="CANCEL"),
                ],
            ]
        ),
    )
    return CONFIRM


async def confirm_ticket(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""

    if data == "CANCEL":
        await q.edit_message_text("Cancelled.")
        return ConversationHandler.END

    if data == "BACK_TO_CAT":
        await q.edit_message_text(
            f"Property: *{ctx.user_data['branch']}*\n"
            f"Department: *{ctx.user_data['department']}*\n\nChoose problem type:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_categories(),
        )
        return CAT

    if data != "CONFIRM":
        return CONFIRM

    user = q.from_user
    author = f"@{user.username}" if user.username else user.full_name

    b = ctx.user_data.get("branch")
    d = ctx.user_data.get("department")
    c = ctx.user_data.get("category")
    t = ctx.user_data.get("desc", "")
    photo_file_id = ctx.user_data.get("photo_file_id")
    is_photo = 1 if photo_file_id else 0

    open_text = t
    ticket_chat_id = LOG_CHAT_ID

    with closing(db()) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO tickets
            (chat_id, open_msg_id, bot_msg_id, open_user, open_user_id,
             open_text, open_ts_utc, tags, assignees, status,
             branch, department, category, close_note,
             photo_file_id, is_photo, closed_ts_utc, close_photo_file_id, deleted)
            VALUES (?,?,?,?,?,
                    ?,?,?,?,?,
                    ?,?,?,?,
                    ?,?,NULL,NULL,0)
            """,
            (
                ticket_chat_id,
                0,
                None,
                author,
                user.id,
                open_text,
                utcnow_iso(),
                "",
                "",
                "OPEN",
                b,
                d,
                c,
                None,
                photo_file_id,
                is_photo,
            ),
        )
        ticket_id = cur.lastrowid

        tdict = get_ticket(conn, ticket_id)
        text = render_ticket_text(tdict)
        conn.commit()

    # Post to group
    try:
        if photo_file_id:
            caption = truncate_for_text(text, SAFE_CAPTION_LEN)
            posted = await ctx.bot.send_photo(
                chat_id=ticket_chat_id,
                photo=photo_file_id,
                caption=caption,
                reply_markup=kb_controls(ticket_id, "OPEN"),
            )
        else:
            msg_text = truncate_for_text(text, SAFE_TEXT_LEN)
            posted = await ctx.bot.send_message(
                chat_id=ticket_chat_id,
                text=msg_text,
                reply_markup=kb_controls(ticket_id, "OPEN"),
            )
    except Exception as e:
        print("Error sending ticket to group:", repr(e))
        await q.edit_message_text("❌ Could not post ticket to group. Tell IT.")
        return ConversationHandler.END

    # Update DB with message IDs + OPEN event
    with closing(db()) as conn:
        conn.execute(
            "UPDATE tickets SET open_msg_id=?, bot_msg_id=? WHERE id=?",
            (posted.message_id, posted.message_id, ticket_id),
        )
        conn.execute(
            """
            INSERT INTO events (ticket_id, chat_id, msg_id, kind, author, text, ts_utc)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                ticket_id,
                ticket_chat_id,
                posted.message_id,
                "OPEN",
                author,
                open_text,
                utcnow_iso(),
            ),
        )
        conn.commit()

    await q.edit_message_text(USER_TICKET_CONFIRM.format(id=ticket_id))
    return ConversationHandler.END


# ======================
# ADMIN / UTILITY COMMANDS
# ======================

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    is_it = is_it_user(u)

    lines = [
        "🛠 *TicketBot Help*",
        "",
        "*For staff:*",
        "- /ticket – Open a new IT ticket (step-by-step).",
        "- /start – Same as /ticket in private chat.",
        "",
        "Tickets are always created in a private chat and then posted to the IT group.",
    ]

    if is_it:
        lines += [
            "",
            "*For IT (admins):*",
            "- /open – List active (non-closed) tickets.",
            "- /closed – List latest closed tickets.",
            "- /stats – Count tickets by status.",
            "- /export YYYY-MM – Excel export (monthly).",
            "- /report YYYY-MM-DD YYYY-MM-DD – Flexible date-range report.",
            "- /delete <id> – Soft-delete a ticket.",
            "- /restore <id> – Restore a soft-deleted ticket.",
            "- /edit <id> <field> <value> – Edit branch/department/category/desc.",
            "- /reopen <id> – Reopen a CLOSED ticket.",
            "- /note <id> [text] – Attach an IT note (or photo via DM).",
            "- /ping – Health check.",
            "- /say <text> – Announcement to ticket group.",
            "- /panel – Post 'Open Ticket' button panel in the group.",
            "- /whoami – Show your Telegram ID & IT status.",
            "- /chatid – Show current chat id (use in group to bind LOG_CHAT_ID).",
            "- /done – Reply to a ticket in group to close it.",
        ]

    await update.message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.MARKDOWN
    )


async def cmd_whoami(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await update.message.reply_text(
        f"username: @{u.username or '-'}\n"
        f"user_id: {u.id}\n"
        f"is_it_user: {is_it_user(u)}"
    )


async def cmd_chatid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_it_user(update.effective_user):
        await update.message.reply_text("Only IT can use this command.")
        return
    await update.message.reply_text(
        f"chat_id: {update.effective_chat.id}"
    )


async def cmd_panel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_it_user(update.effective_user):
        await update.message.reply_text("Only IT can use this command.")
        return
    if update.effective_chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    ):
        await update.message.reply_text(
            "Use /panel *in the ticket group*.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    url = dm_ticket_url(ctx.bot.username)
    kb_panel = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🎫 Open Ticket", url=url)]]
    )
    await update.message.reply_text(
        "Tap the button below to open a new IT ticket in DM:",
        reply_markup=kb_panel,
    )


async def cmd_ping(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_it_user(update.effective_user):
        await update.message.reply_text("Only IT can use this command.")
        return
    now = datetime.datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")
    await update.message.reply_text(
        f"🏓 TicketBot is alive. Local time: {now}"
    )


async def cmd_say(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not is_it_user(u):
        await update.message.reply_text("This command is for IT only.")
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /say Your announcement text")
        return
    text = " ".join(ctx.args).strip()
    loud = f"📢📢📢 *IT ANNOUNCEMENT*\n\n{text}"
    try:
        await ctx.bot.send_message(
            chat_id=LOG_CHAT_ID,
            text=truncate_for_text(loud, SAFE_TEXT_LEN),
            parse_mode=ParseMode.MARKDOWN,
        )
        if update.effective_chat.id != LOG_CHAT_ID:
            await update.message.reply_text(
                "Announcement sent to the IT ticket group."
            )
    except Exception as e:
        print("Error sending announcement:", repr(e))
        await update.message.reply_text(
            "Failed to send announcement. Check LOG_CHAT_ID."
        )


# ======================
# GROUP MESSAGE HANDLER
# ======================

async def on_group_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    - Reply-based closing with markers (✅, [DONE], /done, etc.) – IT only.
    - Logs comments in events as 'COMMENT'.
    """
    m = update.effective_message
    if not m or not m.text:
        return


    dbg(
        f"on_group_message: chat_id={m.chat_id}, chat_type={update.effective_chat.type}, "
        f"text={m.text!r}, reply_to={getattr(m.reply_to_message, 'message_id', None)}, "
        f"user_id={m.from_user.id}, is_it={is_it_user(m.from_user)}"
    )

    text = m.text.strip()
    chat_id = m.chat_id

    if m.from_user.username:
        author = f"@{m.from_user.username}"
    else:
        author = m.from_user.full_name

    if not m.reply_to_message:
        return

    replied_id = m.reply_to_message.message_id

    with closing(db()) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT * FROM tickets
            WHERE chat_id=? AND (open_msg_id=? OR bot_msg_id=?)
            """,
            (chat_id, replied_id, replied_id),
        )
        row = cur.fetchone()
        if not row:
            return

        tdict = row_to_dict(conn, "tickets", row)
        ticket_id = tdict["id"]
        status = tdict.get("status", DEFAULT_STATUS)

        dbg(
            f"on_group_message: matched ticket_id={ticket_id}, status={status}, "
            f"text={text!r}, close_text={is_close_text(text)}"
        )

        # IT + close markers => close
        if is_it_user(m.from_user) and is_close_text(text):
            if status != "CLOSED":
                now_iso = utcnow_iso()
                conn.execute(
                    """
                    UPDATE tickets
                    SET status='CLOSED', close_note=?, closed_ts_utc=?
                    WHERE id=?
                    """,
                    (text, now_iso, ticket_id),
                )
                conn.execute(
                    """
                    INSERT INTO events (ticket_id, chat_id, msg_id, kind, author, text, ts_utc)
                    VALUES (?,?,?,?,?,?,?)
                    """,
                    (
                        ticket_id,
                        chat_id,
                        m.message_id,
                        "CLOSE",
                        author,
                        text,
                        now_iso,
                    ),
                )
                row_full = get_ticket(conn, ticket_id)
                conn.commit()
                await update_ticket_message(
                    ctx.bot, row_full, remove_keyboard=True
                )
                await m.reply_text(f"✅ Ticket #{ticket_id} closed.")
                await notify_ticket_user(
                    ctx.bot,
                    row_full,
                    f"✅ Your ticket #{ticket_id} has been CLOSED by IT.",
                )
            return

        # Otherwise: log as COMMENT
        conn.execute(
            """
            INSERT INTO events (ticket_id, chat_id, msg_id, kind, author, text, ts_utc)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                ticket_id,
                chat_id,
                m.message_id,
                "COMMENT",
                author,
                text,
                utcnow_iso(),
            ),
        )
        conn.commit()


# ======================
# PRIVATE MESSAGE HANDLERS (IT close/note flows)
# ======================

async def on_private_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    - If IT is in 'closing' flow: treat message as closing note (text).
    - If IT is in '/note' flow: treat message as note text.
    - Otherwise: hint about /ticket and /help.
    """
    m = update.effective_message
    if not m or not m.text:
        return

    text = m.text.strip()
    user = update.effective_user
    user_id = user.id

    # 1) Closing flow
    closing_tid = CLOSING_TICKET_BY_USER.get(user_id)
    if closing_tid and is_it_user(user):
        CLOSING_TICKET_BY_USER.pop(user_id, None)
        NOTE_TICKET_BY_USER.pop(user_id, None)

        with closing(db()) as conn:
            tdict = get_ticket(conn, closing_tid)
            if not tdict:
                await m.reply_text(f"Ticket #{closing_tid} not found.")
                return
            if tdict.get("status") == "CLOSED":
                await m.reply_text(f"Ticket #{closing_tid} is already CLOSED.")
                return

            now_iso = utcnow_iso()
            conn.execute(
                """
                UPDATE tickets
                SET status='CLOSED', close_note=?, closed_ts_utc=?
                WHERE id=?
                """,
                (text, now_iso, closing_tid),
            )
            conn.execute(
                """
                INSERT INTO events (ticket_id, chat_id, msg_id, kind, author, text, ts_utc)
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    closing_tid,
                    tdict["chat_id"],
                    None,
                    "CLOSE",
                    user.username or user.full_name,
                    text,
                    now_iso,
                ),
            )
            row_full = get_ticket(conn, closing_tid)
            conn.commit()

        await update_ticket_message(
            ctx.bot, row_full, remove_keyboard=True
        )
        await m.reply_text(f"✅ Ticket #{closing_tid} closed with note.")
        await notify_ticket_user(
            ctx.bot,
            row_full,
            f"✅ Your ticket #{closing_tid} has been CLOSED by IT.",
        )
        return

    # 2) /note flow
    note_tid = NOTE_TICKET_BY_USER.get(user_id)
    if note_tid and is_it_user(user):
        NOTE_TICKET_BY_USER.pop(user_id, None)
        CLOSING_TICKET_BY_USER.pop(user_id, None)

        with closing(db()) as conn:
            tdict = get_ticket(conn, note_tid)
            if not tdict:
                await m.reply_text(f"Ticket #{note_tid} not found.")
                return

            existing = tdict.get("close_note") or ""
            new_note = text if not existing else (existing + "\n---\n" + text)

            conn.execute(
                "UPDATE tickets SET close_note=? WHERE id=?",
                (new_note, note_tid),
            )
            conn.execute(
                """
                INSERT INTO events (ticket_id, chat_id, msg_id, kind, author, text, ts_utc)
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    note_tid,
                    tdict["chat_id"],
                    None,
                    "NOTE",
                    user.username or user.full_name,
                    text,
                    utcnow_iso(),
                ),
            )
            row_full = get_ticket(conn, note_tid)
            conn.commit()

        await update_ticket_message(ctx.bot, row_full)
        await m.reply_text(f"📝 Note saved for Ticket #{note_tid}.")
        await notify_ticket_user(
            ctx.bot,
            row_full,
            f"📝 IT added a note to your ticket #{note_tid}.",
        )
        return

    # 3) Default
    await m.reply_text(
        "Hi! Use /ticket to open a new IT ticket, or /help to see commands."
    )


async def on_private_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    - If IT is in 'closing' flow: photo close (with caption).
    - If IT is in '/note' flow: photo note (with caption).
    """
    m = update.effective_message
    if not m or not m.photo:
        return

    user = update.effective_user
    user_id = user.id

    photo = m.photo[-1]
    file_id = photo.file_id
    caption = (m.caption or "").strip()

    # 1) Closing flow
    closing_tid = CLOSING_TICKET_BY_USER.get(user_id)
    if closing_tid and is_it_user(user):
        CLOSING_TICKET_BY_USER.pop(user_id, None)
        NOTE_TICKET_BY_USER.pop(user_id, None)

        with closing(db()) as conn:
            tdict = get_ticket(conn, closing_tid)
            if not tdict:
                await m.reply_text(f"Ticket #{closing_tid} not found.")
                return
            if tdict.get("status") == "CLOSED":
                await m.reply_text(f"Ticket #{closing_tid} is already CLOSED.")
                return

            now_iso = utcnow_iso()
            conn.execute(
                """
                UPDATE tickets
                SET status='CLOSED',
                    close_note=?,
                    close_photo_file_id=?,
                    closed_ts_utc=?
                WHERE id=?
                """,
                (caption, file_id, now_iso, closing_tid),
            )
            conn.execute(
                """
                INSERT INTO events (ticket_id, chat_id, msg_id, kind, author, text, ts_utc)
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    closing_tid,
                    tdict["chat_id"],
                    None,
                    "CLOSE",
                    user.username or user.full_name,
                    caption or "[photo close]",
                    now_iso,
                ),
            )
            row_full = get_ticket(conn, closing_tid)
            conn.commit()

        await update_ticket_message(
            ctx.bot, row_full, remove_keyboard=True
        )
        await m.reply_text(
            f"✅ Ticket #{closing_tid} closed with photo note."
        )
        await notify_ticket_user(
            ctx.bot,
            row_full,
            f"✅ Your ticket #{closing_tid} has been CLOSED by IT (with photo).",
        )
        return

    # 2) /note flow with photo
    note_tid = NOTE_TICKET_BY_USER.get(user_id)
    if note_tid and is_it_user(user):
        NOTE_TICKET_BY_USER.pop(user_id, None)
        CLOSING_TICKET_BY_USER.pop(user_id, None)

        with closing(db()) as conn:
            tdict = get_ticket(conn, note_tid)
            if not tdict:
                await m.reply_text(f"Ticket #{note_tid} not found.")
                return

            existing = tdict.get("close_note") or ""
            new_piece = caption or "[photo]"
            new_note = new_piece if not existing else (
                existing + "\n---\n" + new_piece
            )

            conn.execute(
                """
                UPDATE tickets
                SET close_note=?, close_photo_file_id=?
                WHERE id=?
                """,
                (new_note, file_id, note_tid),
            )
            conn.execute(
                """
                INSERT INTO events (ticket_id, chat_id, msg_id, kind, author, text, ts_utc)
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    note_tid,
                    tdict["chat_id"],
                    None,
                    "NOTE",
                    user.username or user.full_name,
                    caption or "[photo note]",
                    utcnow_iso(),
                ),
            )
            row_full = get_ticket(conn, note_tid)
            conn.commit()

        await update_ticket_message(ctx.bot, row_full)
        await m.reply_text(
            f"📝 Photo note saved for Ticket #{note_tid}."
        )
        await notify_ticket_user(
            ctx.bot,
            row_full,
            f"📝 IT added a photo note to your ticket #{note_tid}.",
        )
        return

    # 3) No active context
    await m.reply_text(
        "There's no active ticket close or /note flow. Use /note <id> or the Close button first."
    )


# ======================
# IT COMMANDS: LISTING / STATS
# ======================

async def cmd_open(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_it_user(update.effective_user):
        await update.message.reply_text("Only IT can use this command.")
        return

    with closing(db()) as conn:
        df = pd.read_sql_query(
            """
            SELECT id, open_ts_utc, open_user, branch, department, category, status
            FROM tickets
            WHERE status!='CLOSED' AND (deleted IS NULL OR deleted=0)
            ORDER BY id DESC
            """,
            conn,
        )

    if df.empty:
        await update.message.reply_text("No OPEN/ACTIVE tickets.")
        return

    lines = ["Active tickets:"]
    for _, r in df.iterrows():
        when = (
            pd.to_datetime(r["open_ts_utc"], utc=True)
            .tz_convert(LOCAL_TZ)
            .strftime("%Y-%m-%d %H:%M")
        )
        extra = " | ".join(
            [
                x
                for x in [r.get("branch"), r.get("department"), r.get("category")]
                if x
            ]
        )
        lines.append(
            f"• #{r['id']} — {when} — {r['status']} — {extra or '-'} — by {r['open_user']}"
        )

    text = truncate_for_text("\n".join(lines), SAFE_TEXT_LEN)
    await update.message.reply_text(text)


async def cmd_closed(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_it_user(update.effective_user):
        await update.message.reply_text("Only IT can use this command.")
        return

    with closing(db()) as conn:
        df = pd.read_sql_query(
            """
            SELECT id, open_ts_utc, open_user, branch, department, category
            FROM tickets
            WHERE status='CLOSED' AND (deleted IS NULL OR deleted=0)
            ORDER BY id DESC
            LIMIT 20
            """,
            conn,
        )

    if df.empty:
        await update.message.reply_text("No CLOSED tickets.")
        return

    lines = ["Latest CLOSED:"]
    for _, r in df.iterrows():
        when = (
            pd.to_datetime(r["open_ts_utc"], utc=True)
            .tz_convert(LOCAL_TZ)
            .strftime("%Y-%m-%d %H:%M")
        )
        extra = " | ".join(
            [
                x
                for x in [r.get("branch"), r.get("department"), r.get("category")]
                if x
            ]
        )
        lines.append(
            f"• #{r['id']} — {when} — {extra or '-'} — by {r['open_user']}"
        )

    text = truncate_for_text("\n".join(lines), SAFE_TEXT_LEN)
    await update.message.reply_text(text)


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_it_user(update.effective_user):
        await update.message.reply_text("Only IT can use this command.")
        return

    with closing(db()) as conn:
        df = pd.read_sql_query(
            "SELECT status FROM tickets WHERE (deleted IS NULL OR deleted=0)",
            conn,
        )

    if df.empty:
        await update.message.reply_text("No tickets yet.")
        return

    counts = df["status"].value_counts().to_dict()
    order = [s for s in ALL_STATUSES if s in counts] + [
        k for k in counts if k not in ALL_STATUSES
    ]

    lines = ["Ticket Status Summary:"]
    for s in order:
        lines.append(f"{s}: {counts.get(s, 0)}")

    text = truncate_for_text("\n".join(lines), SAFE_TEXT_LEN)
    await update.message.reply_text(text)


# ======================
# EXPORT & REPORT
# ======================

async def cmd_export(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Monthly Excel export: /export YYYY-MM
    """
    if not is_it_user(update.effective_user):
        await update.message.reply_text("Only IT can use this command.")
        return

    if not ctx.args:
        await update.message.reply_text(
            "Usage: /export YYYY-MM (e.g. /export 2025-11)"
        )
        return

    ym = ctx.args[0]
    try:
        y, m = map(int, ym.split("-"))
        _ = datetime.datetime(y, m, 1)
    except Exception:
        await update.message.reply_text(
            "Bad format. Use /export YYYY-MM (e.g. /export 2025-11)"
        )
        return

    try:
        with closing(db()) as conn:
            df_t = pd.read_sql_query(
                "SELECT * FROM tickets WHERE (deleted IS NULL OR deleted=0)",
                conn,
            )
            df_e = pd.read_sql_query("SELECT * FROM events", conn)
    except Exception as e:
        print("EXPORT DB ERROR:", repr(e))
        await update.message.reply_text(
            f"Export failed while reading DB: {e}"
        )
        return

    if df_t.empty:
        await update.message.reply_text("No tickets.")
        return

    df_t["open_ts_utc"] = pd.to_datetime(df_t["open_ts_utc"], utc=True)

    if not df_e.empty:
        df_e["ts_utc"] = pd.to_datetime(
            df_e["ts_utc"], utc=True, errors="coerce"
        )
        close_map = (
            df_e[df_e["kind"] == "CLOSE"]
            .sort_values("ts_utc")
            .groupby("ticket_id")["ts_utc"]
            .first()
        )
    else:
        close_map = {}

    df = df_t.copy()

    df["closed_ts_utc"] = pd.to_datetime(
        df.get("closed_ts_utc"), errors="coerce", utc=True
    )
    if len(close_map) > 0:
        tmp = df["id"].map(close_map)
        df["closed_ts_utc"] = df["closed_ts_utc"].fillna(tmp)

    df["open_local"] = df["open_ts_utc"].dt.tz_convert(LOCAL_TZ).dt.tz_localize(
        None
    )
    df["closed_local"] = (
        df["closed_ts_utc"]
        .dt.tz_convert(LOCAL_TZ)
        .dt.tz_localize(None)
        if "closed_ts_utc" in df.columns
        else None
    )

    df["hours_to_close"] = (
        (df["closed_ts_utc"] - df["open_ts_utc"]).dt.total_seconds() / 3600.0
    )

    df["ym"] = df["open_local"].dt.strftime("%Y-%m")
    dfm = df[df["ym"] == ym].sort_values("open_ts_utc")
    if dfm.empty:
        await update.message.reply_text(f"No tickets in {ym}.")
        return

    closed_df = dfm[dfm["status"] == "CLOSED"]
    open_df = dfm[dfm["status"] != "CLOSED"]

    median_hours = avg_hours = None
    if not closed_df.empty:
        hours = closed_df["hours_to_close"].dropna()
        if not hours.empty:
            median_hours = round(hours.median(), 2)
            avg_hours = round(hours.mean(), 2)

    summary = pd.DataFrame(
        [
            {
                "period": ym,
                "tickets_opened": int(len(dfm)),
                "tickets_closed": int(len(closed_df)),
                "open_unresolved": int(len(open_df)),
                "median_hours_to_close": median_hours,
                "avg_hours_to_close": avg_hours,
            }
        ]
    )

    def build_stats(df_src, key_col_name: str, display_name: str):
        temp = df_src.copy()
        temp[key_col_name] = temp[key_col_name].fillna("Unspecified")
        temp["status"] = temp["status"].fillna("UNKNOWN")
        table = (
            temp.groupby([key_col_name, "status"])
            .size()
            .unstack(fill_value=0)
        )
        table["Total"] = table.sum(axis=1)
        table = table.reset_index()
        status_cols = [
            c for c in table.columns if c not in (key_col_name, "Total")
        ]
        table = table[[key_col_name, "Total"] + status_cols]
        table = table.rename(columns={key_col_name: display_name})
        return table

    category_stats = build_stats(dfm, "category", "Category")
    property_stats = build_stats(dfm, "branch", "Property")
    department_stats = build_stats(dfm, "department", "Department")

    status_overall = (
        dfm["status"].fillna("UNKNOWN").value_counts().reset_index()
    )
    status_overall.columns = ["Status", "Count"]

    tickets_cols = [
        "id",
        "status",
        "branch",
        "department",
        "category",
        "open_user",
        "open_local",
        "closed_local",
        "hours_to_close",
        "open_text",
        "close_note",
    ]
    tickets_cols = [c for c in tickets_cols if c in dfm.columns]
    tickets_df = dfm[tickets_cols].copy()

    rename_map = {
        "id": "Ticket ID",
        "status": "Status",
        "branch": "Property",
        "department": "Department",
        "category": "Category",
        "open_user": "Opened By",
        "open_local": "Opened (Local Time)",
        "closed_local": "Closed (Local Time)",
        "hours_to_close": "Hours to Close",
        "open_text": "User Description",
        "close_note": "IT Closing Note",
    }
    tickets_df = tickets_df.rename(columns=rename_map)

    buffer = BytesIO()
    try:
        with pd.ExcelWriter(
            buffer,
            engine="xlsxwriter",
            datetime_format="yyyy-mm-dd hh:mm",
        ) as writer:
            summary.to_excel(writer, sheet_name="Summary", index=False)
            category_stats.to_excel(
                writer, sheet_name="Category_Stats", index=False
            )
            property_stats.to_excel(
                writer, sheet_name="Property_Stats", index=False
            )
            department_stats.to_excel(
                writer, sheet_name="Department_Stats", index=False
            )
            status_overall.to_excel(
                writer, sheet_name="Status_Overall", index=False
            )
            tickets_df.to_excel(
                writer, sheet_name="Tickets", index=False
            )
    except Exception as e:
        print("EXPORT WRITE ERROR:", repr(e))
        await update.message.reply_text(
            f"Export failed while writing Excel: {e}"
        )
        return

    buffer.seek(0)
    filename = f"tickets_report_{ym}.xlsx"
    try:
        await update.message.reply_document(
            document=buffer,
            filename=filename,
            caption=f"Ticket report {ym}",
        )
    except Exception as e:
        print("EXPORT SEND ERROR:", repr(e))
        await update.message.reply_text(
            f"Export created but could not be sent: {e}"
        )


async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /report YYYY-MM-DD YYYY-MM-DD
    """
    if not is_it_user(update.effective_user):
        await update.message.reply_text("Only IT can use this command.")
        return

    if len(ctx.args) < 2:
        await update.message.reply_text(
            "Usage: /report YYYY-MM-DD YYYY-MM-DD"
        )
        return

    try:
        start_date = datetime.date.fromisoformat(ctx.args[0])
        end_date = datetime.date.fromisoformat(ctx.args[1])
    except ValueError:
        await update.message.reply_text(
            "Bad date format. Use YYYY-MM-DD YYYY-MM-DD"
        )
        return
    if end_date < start_date:
        await update.message.reply_text(
            "End date must be on or after start date."
        )
        return

    with closing(db()) as conn:
        df = pd.read_sql_query(
            "SELECT * FROM tickets WHERE (deleted IS NULL OR deleted=0)",
            conn,
        )

    if df.empty:
        await update.message.reply_text("No tickets yet.")
        return

    df["open_ts_utc"] = pd.to_datetime(
        df["open_ts_utc"], utc=True, errors="coerce"
    )
    df["open_local"] = df["open_ts_utc"].dt.tz_convert(LOCAL_TZ)
    df["open_date"] = df["open_local"].dt.date

    mask = (df["open_date"] >= start_date) & (df["open_date"] <= end_date)
    df_range = df[mask].copy()
    if df_range.empty:
        await update.message.reply_text("No tickets in that date range.")
        return

    total = len(df_range)

    by_cat = (
        df_range["category"].fillna("Unspecified").value_counts().to_dict()
    )
    by_branch = (
        df_range["branch"].fillna("Unspecified").value_counts().to_dict()
    )
    by_status = (
        df_range["status"].fillna("UNKNOWN").value_counts().to_dict()
    )

    df_range["closed_ts_utc"] = pd.to_datetime(
        df_range.get("closed_ts_utc"), errors="coerce", utc=True
    )
    closed_mask = (df_range["status"] == "CLOSED") & df_range[
        "closed_ts_utc"
    ].notna()
    df_closed = df_range[closed_mask]
    median_hours = avg_hours = None
    if not df_closed.empty:
        hours = (
            df_closed["closed_ts_utc"] - df_closed["open_ts_utc"]
        ).dt.total_seconds() / 3600.0
        median_hours = round(hours.median(), 2)
        avg_hours = round(hours.mean(), 2)

    lines = [
        "Ticket Report",
        f"Range: {start_date.isoformat()} → {end_date.isoformat()}",
        f"Total tickets opened: {total}",
        "",
        "By Category (problem type):",
    ]
    for cat, cnt in by_cat.items():
        lines.append(f"- {cat}: {cnt}")

    lines.append("")
    lines.append("By Property:")
    for br, cnt in by_branch.items():
        lines.append(f"- {br}: {cnt}")

    lines.append("")
    lines.append("By Status:")
    for st, cnt in by_status.items():
        lines.append(f"- {st}: {cnt}")

    if median_hours is not None:
        lines.append("")
        lines.append("Resolution time (for closed tickets in range):")
        lines.append(f"- Median hours to close: {median_hours}")
        lines.append(f"- Average hours to close: {avg_hours}")

    text = truncate_for_text("\n".join(lines), SAFE_TEXT_LEN)
    await update.message.reply_text(text)


# ======================
# /DELETE /RESTORE /EDIT /REOPEN /NOTE
# ======================

async def cmd_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_it_user(update.effective_user):
        await update.message.reply_text("Only IT can use this command.")
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /delete <ticket_id>")
        return
    try:
        tid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("Ticket id must be a number.")
        return

    with closing(db()) as conn:
        tdict = get_ticket(conn, tid)
        if not tdict:
            await update.message.reply_text(f"Ticket #{tid} not found.")
            return
        if tdict.get("deleted"):
            await update.message.reply_text(
                f"Ticket #{tid} is already deleted."
            )
            return

        conn.execute("UPDATE tickets SET deleted=1 WHERE id=?", (tid,))
        conn.execute(
            """
            INSERT INTO events (ticket_id, chat_id, msg_id, kind, author, text, ts_utc)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                tid,
                tdict["chat_id"],
                None,
                "DELETE",
                update.effective_user.username
                or update.effective_user.full_name,
                "Ticket soft-deleted",
                utcnow_iso(),
            ),
        )
        row_full = get_ticket(conn, tid)
        conn.commit()

    await update_ticket_message(
        ctx.bot, row_full, remove_keyboard=True
    )
    await update.message.reply_text(f"🗑 Ticket #{tid} marked as deleted.")
    await notify_ticket_user(
        ctx.bot,
        row_full,
        f"ℹ️ Your ticket #{tid} was removed by IT (likely a test ticket or duplicate).",
    )


async def cmd_restore(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_it_user(update.effective_user):
        await update.message.reply_text("Only IT can use this command.")
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /restore <ticket_id>")
        return
    try:
        tid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("Ticket id must be a number.")
        return

    with closing(db()) as conn:
        tdict = get_ticket(conn, tid)
        if not tdict:
            await update.message.reply_text(f"Ticket #{tid} not found.")
            return
        if not tdict.get("deleted"):
            await update.message.reply_text(
                f"Ticket #{tid} is not deleted."
            )
            return

        conn.execute("UPDATE tickets SET deleted=0 WHERE id=?", (tid,))
        conn.execute(
            """
            INSERT INTO events (ticket_id, chat_id, msg_id, kind, author, text, ts_utc)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                tid,
                tdict["chat_id"],
                None,
                "RESTORE",
                update.effective_user.username
                or update.effective_user.full_name,
                "Ticket restored",
                utcnow_iso(),
            ),
        )
        row_full = get_ticket(conn, tid)
        conn.commit()

    await update_ticket_message(ctx.bot, row_full, remove_keyboard=False)
    await update.message.reply_text(f"✅ Ticket #{tid} restored.")
    await notify_ticket_user(
        ctx.bot, row_full, f"ℹ️ Your ticket #{tid} was restored by IT."
    )


async def cmd_edit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /edit <id> <field> <value...>
    field ∈ branch|department|category|desc
    """
    if not is_it_user(update.effective_user):
        await update.message.reply_text("Only IT can use this command.")
        return
    if len(ctx.args) < 3:
        await update.message.reply_text(
            "Usage: /edit <id> <field> <value...>\n"
            "Fields: branch, department, category, desc"
        )
        return
    try:
        tid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("Ticket id must be a number.")
        return

    field = ctx.args[1].lower()
    value = " ".join(ctx.args[2:]).strip()

    field_map = {
        "branch": "branch",
        "department": "department",
        "dept": "department",
        "category": "category",
        "cat": "category",
        "desc": "open_text",
        "description": "open_text",
    }
    if field not in field_map:
        await update.message.reply_text(
            "Field must be one of: branch, department, category, desc"
        )
        return
    col = field_map[field]

    with closing(db()) as conn:
        tdict = get_ticket(conn, tid)
        if not tdict:
            await update.message.reply_text(f"Ticket #{tid} not found.")
            return

        conn.execute(
            f"UPDATE tickets SET {col}=? WHERE id=?",
            (value, tid),
        )
        conn.execute(
            """
            INSERT INTO events (ticket_id, chat_id, msg_id, kind, author, text, ts_utc)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                tid,
                tdict["chat_id"],
                None,
                "EDIT",
                update.effective_user.username
                or update.effective_user.full_name,
                f"{col} => {value}",
                utcnow_iso(),
            ),
        )
        row_full = get_ticket(conn, tid)
        conn.commit()

    await update_ticket_message(ctx.bot, row_full)
    await update.message.reply_text(
        f"✏️ Ticket #{tid} updated ({col})."
    )
    await notify_ticket_user(
        ctx.bot,
        row_full,
        f"ℹ️ IT updated your ticket #{tid} ({col}).",
    )


async def cmd_reopen(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_it_user(update.effective_user):
        await update.message.reply_text("Only IT can use this command.")
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /reopen <ticket_id>")
        return
    try:
        tid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("Ticket id must be a number.")
        return

    with closing(db()) as conn:
        tdict = get_ticket(conn, tid)
        if not tdict:
            await update.message.reply_text(f"Ticket #{tid} not found.")
            return
        if tdict.get("status") != "CLOSED":
            await update.message.reply_text(
                f"Ticket #{tid} is not CLOSED."
            )
            return

        conn.execute(
            """
            UPDATE tickets
            SET status='OPEN', closed_ts_utc=NULL
            WHERE id=?
            """,
            (tid,),
        )
        conn.execute(
            """
            INSERT INTO events (ticket_id, chat_id, msg_id, kind, author, text, ts_utc)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                tid,
                tdict["chat_id"],
                None,
                "REOPEN",
                update.effective_user.username
                or update.effective_user.full_name,
                "Ticket reopened",
                utcnow_iso(),
            ),
        )
        row_full = get_ticket(conn, tid)
        conn.commit()

    await update_ticket_message(ctx.bot, row_full)
    await update.message.reply_text(f"🔁 Ticket #{tid} reopened.")
    await notify_ticket_user(
        ctx.bot,
        row_full,
        f"ℹ️ Your ticket #{tid} was reopened by IT.",
    )


async def cmd_note(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /note <id> [text...]
    - If text provided: immediate note
    - If no text: start a note flow and wait for next message (text/photo)
    """
    u = update.effective_user
    if not is_it_user(u):
        await update.message.reply_text("Only IT can use this command.")
        return

    if not ctx.args:
        await update.message.reply_text(
            "Usage: /note <ticket_id> [text]\n"
            "If you omit text, I'll ask you to send it (or a photo) next."
        )
        return

    try:
        tid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("Ticket id must be a number.")
        return

    text = " ".join(ctx.args[1:]).strip()

    with closing(db()) as conn:
        tdict = get_ticket(conn, tid)
        if not tdict:
            await update.message.reply_text(f"Ticket #{tid} not found.")
            return

    # If text provided -> immediate note
    if text:
        with closing(db()) as conn:
            existing = tdict.get("close_note") or ""
            new_note = text if not existing else (existing + "\n---\n" + text)

            conn.execute(
                "UPDATE tickets SET close_note=? WHERE id=?",
                (new_note, tid),
            )
            conn.execute(
                """
                INSERT INTO events (ticket_id, chat_id, msg_id, kind, author, text, ts_utc)
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    tid,
                    tdict["chat_id"],
                    None,
                    "NOTE",
                    u.username or u.full_name,
                    text,
                    utcnow_iso(),
                ),
            )
            row_full = get_ticket(conn, tid)
            conn.commit()

        await update_ticket_message(ctx.bot, row_full)
        await update.message.reply_text(
            f"📝 Note saved for Ticket #{tid}."
        )
        await notify_ticket_user(
            ctx.bot,
            row_full,
            f"📝 IT added a note to your ticket #{tid}.",
        )
    else:
        # start note flow
        NOTE_TICKET_BY_USER[u.id] = tid
        CLOSING_TICKET_BY_USER.pop(u.id, None)
        await update.message.reply_text(
            f"📝 Send the note for Ticket #{tid} as a text message, "
            f"or send a photo with a caption.\n\n"
            f"I'll attach it as an IT note."
        )


# ======================
# INLINE CALLBACKS (STATUS / CLOSE)
# ======================

async def cb_inline(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    u = q.from_user
    data = q.data or ""

    if not is_it_user(u):
        await q.answer("Only IT can use this.", show_alert=True)
        return

    # CLOSE:tid
    if data.startswith("CLOSE:"):
        try:
            tid = int(data.split(":", 1)[1])
        except ValueError:
            return

        CLOSING_TICKET_BY_USER[u.id] = tid
        NOTE_TICKET_BY_USER.pop(u.id, None)

        try:
            await ctx.bot.send_message(
                chat_id=u.id,
                text=(
                    f"Please send a *closing note* for Ticket #{tid}.\n\n"
                    f"You can:\n"
                    f"- Send a text message (closing note), OR\n"
                    f"- Send a photo with an optional caption.\n\n"
                    f"I'll attach it to the ticket and close it."
                ),
                parse_mode=ParseMode.MARKDOWN,
            )
            await q.answer(
                "Check your private chat with me to enter the closing note.",
                show_alert=True,
            )
        except Exception as e:
            print("Error DMing IT on close:", repr(e))
            await q.message.reply_text(
                "I couldn't DM you. Please /start me in a private chat, then press 'Close Ticket' again."
            )
        return

    # STATUS:tid:new_status
    if data.startswith("STATUS:"):
        try:
            _, tid_str, new_status = data.split(":", 2)
            tid = int(tid_str)
        except Exception:
            return

        if new_status not in ALL_STATUSES:
            await q.answer("Invalid status.", show_alert=True)
            return

        with closing(db()) as conn:
            tdict = get_ticket(conn, tid)
            if not tdict:
                await q.message.reply_text(f"Ticket #{tid} not found.")
                return

            old_status = tdict.get("status", DEFAULT_STATUS)
            if old_status == new_status:
                await q.answer("Status unchanged.")
                return

            conn.execute(
                "UPDATE tickets SET status=? WHERE id=?",
                (new_status, tid),
            )
            conn.execute(
                """
                INSERT INTO events (ticket_id, chat_id, msg_id, kind, author, text, ts_utc)
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    tid,
                    tdict["chat_id"],
                    q.message.message_id,
                    "STATUS",
                    u.username or u.full_name,
                    f"{old_status} -> {new_status}",
                    utcnow_iso(),
                ),
            )
            row_full = get_ticket(conn, tid)
            conn.commit()

        await update_ticket_message(ctx.bot, row_full)
        await q.answer(f"Status set to {new_status}.")
        await notify_ticket_user(
            ctx.bot,
            row_full,
            f"ℹ️ Your ticket #{tid} status changed: {old_status} → {new_status}.",
        )


# ======================
# ERROR / UNKNOWN
# ======================

async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    print("Error:", repr(ctx.error))
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                "⚠️ Something unexpected happened. Please try again or contact IT."
            )
    except Exception:
        pass


async def cmd_unknown(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = update.message
    dbg(
        f"cmd_unknown fired: chat_id={m.chat_id}, chat_type={update.effective_chat.type}, "
        f"text={m.text!r}"
    )
    await m.reply_text(
        "I don't recognize that command. Use /help to see what I can do."
    )



# ======================
# EXTRA: /done + /cancel
# ======================

async def cmd_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /done used in group as a reply to a ticket message.
    Reuses the same logic as on_group_message.
    """
    m = update.effective_message
    dbg(
        f"cmd_done called: chat_id={m.chat_id}, chat_type={update.effective_chat.type}, "
        f"text={m.text!r}, reply_to={getattr(m.reply_to_message, 'message_id', None)}"
    )
    # Just delegate to the same handler – the logic will see text "/done ..."
    await on_group_message(update, ctx)



async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Cancel the current /ticket wizard.
    """
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


# ======================
# MAIN
# ======================

def main():
    if not BOT_TOKEN:
        print("Set BOT_TOKEN env var first.")
        return

    # Run DB migrations once
    init_db()

    app: Application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )

    # Conversation for /start and /ticket
    conv = ConversationHandler(
    entry_points=[
        CommandHandler("start", cmd_ticket),
        CommandHandler("ticket", cmd_ticket),
    ],
    states={
        BRANCH: [CallbackQueryHandler(pick_branch)],
        DEPT: [CallbackQueryHandler(pick_dept)],
        CAT: [CallbackQueryHandler(pick_cat)],
        DESC: [
            MessageHandler(
                (filters.TEXT | filters.PHOTO) & ~filters.COMMAND, get_desc
            )
        ],
        CONFIRM: [CallbackQueryHandler(confirm_ticket)],
    },
    fallbacks=[
        CommandHandler("cancel", cmd_cancel)
    ],
    # Keep it simple – default is per_chat=True, per_user=False, per_message=False
    )


    app.add_handler(conv)

    # Inline callbacks (close / status)
    app.add_handler(
        CallbackQueryHandler(
            cb_inline, pattern=r"^(CLOSE:\d+|STATUS:\d+:.+)$"
        )
    )

    # IT / admin commands
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("chatid", cmd_chatid))
    app.add_handler(CommandHandler("panel", cmd_panel))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("say", cmd_say))

    app.add_handler(CommandHandler("open", cmd_open))
    app.add_handler(CommandHandler("closed", cmd_closed))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CommandHandler("report", cmd_report))

    app.add_handler(CommandHandler("delete", cmd_delete))
    app.add_handler(CommandHandler("restore", cmd_restore))
    app.add_handler(CommandHandler("edit", cmd_edit))
    app.add_handler(CommandHandler("reopen", cmd_reopen))
    app.add_handler(CommandHandler("note", cmd_note))

    # /done: catch as command AND as raw text in groups (belt + suspenders)
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(
        MessageHandler(
            filters.Regex(r"^/done(\s|$)") & filters.ChatType.GROUPS,
            cmd_done,
        )
    )

    # Group vs private message handlers
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS,
            on_group_message,
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
            on_private_message,
        )
    )
    app.add_handler(
        MessageHandler(
            filters.PHOTO & ~filters.COMMAND & filters.ChatType.PRIVATE,
            on_private_photo,
        )
    )

    # Unknown commands
    app.add_handler(MessageHandler(filters.COMMAND, cmd_unknown))

    app.add_error_handler(on_error)

    print("✅ TicketBot v2.1 running… Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()
