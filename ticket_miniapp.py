import os
import sqlite3
import datetime
import time
import shutil
import json
from contextlib import closing
from pathlib import Path
from typing import List, Optional

from io import BytesIO

import pandas as pd
import requests
from fastapi import (
    FastAPI,
    HTTPException,
    UploadFile,
    File,
    Form,
    Query,
)
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from zoneinfo import ZoneInfo
from pydantic import BaseModel

# ======================
# CONFIG
# ======================

DB_PATH = Path("tickets.db")
UPLOAD_DIR = Path("uploads")
INDEX_PATH = Path("index.html")

LOCAL_TZ = ZoneInfo(os.getenv("TZ", "Asia/Phnom_Penh"))
DEFAULT_STATUS = "OPEN"
ALL_STATUSES = ["OPEN", "IN-PROGRESS", "HOLDING", "CLOSED"]
DEFAULT_PRIORITY = "NORMAL"
ALL_PRIORITIES = ["LOW", "NORMAL", "HIGH", "URGENT"]

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
LOG_CHAT_ID = int(os.getenv("LOG_CHAT_ID", "0") or "0")

# ======================
# DB HELPERS
# ======================

def db() -> sqlite3.Connection:
    """Create a DB connection with sane defaults."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db() -> None:
    """Ensure tickets/events tables exist and have required columns."""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")

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

        # Auto-migrate: add missing columns (for older DBs)
        def ensure_col(table: str, col_name: str, col_type: str) -> None:
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
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
        ensure_col("tickets", "priority", "TEXT")

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


def utcnow_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


def to_local_str(ts_utc: Optional[str]) -> Optional[str]:
    if not ts_utc:
        return None
    try:
        dt = datetime.datetime.fromisoformat(ts_utc)
        local = dt.astimezone(LOCAL_TZ)
        return local.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ts_utc


def parse_photo_paths(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    parts = [p.strip() for p in str(raw).split("|") if p.strip()]
    return parts


def make_photo_urls(raw: Optional[str]) -> List[str]:
    paths = parse_photo_paths(raw)
    urls: List[str] = []
    for p in paths:
        # Normalize relative to UPLOAD_DIR
        try:
            rel = os.path.relpath(p, UPLOAD_DIR)
        except ValueError:
            # If not under UPLOAD_DIR, just take basename
            rel = os.path.basename(p)
        rel = rel.replace("\\", "/")
        urls.append(f"/uploads/{rel}")
    return urls


# ======================
# Telegram helpers (stubbed but functional)
# ======================

def build_telegram_text(d: dict) -> str:
    status = d.get("status") or DEFAULT_STATUS
    priority = d.get("priority") or DEFAULT_PRIORITY
    open_local = to_local_str(d.get("open_ts_utc")) or "-"
    closed_local = to_local_str(d.get("closed_ts_utc")) or "Not closed"

    lines = [
        f"🎫 *Ticket #{d.get('id')}*",
        f"*Status:* {status}",
        f"*Priority:* {priority}",
        "",
        f"*Property:* {d.get('branch') or '-'}",
        f"*Department:* {d.get('department') or '-'}",
        f"*Type:* {d.get('category') or '-'}",
        "",
        f"*Opened by:* {d.get('open_user') or '-'}",
        f"*Opened (local):* {open_local}",
        f"*Closed (local):* {closed_local}",
        "",
        "*Description:*",
        d.get("open_text") or "-",
    ]
    note = d.get("close_note")
    if note:
        lines.extend(["", "*IT Note:*", note])

    return "\n".join(lines)


def build_telegram_keyboard(ticket_id: int, status: str) -> dict:
    # Keep it simple: just in-progress + close like the bot
    kb = {
        "inline_keyboard": [
            [
                {
                    "text": "▶ IN-PROGRESS",
                    "callback_data": f"STATUS:{ticket_id}:IN-PROGRESS",
                },
                {
                    "text": "✅ Close Ticket",
                    "callback_data": f"CLOSE:{ticket_id}",
                },
            ]
        ]
    }
    return kb


def send_telegram_new_ticket(d: dict, first_photo_path: Optional[str]) -> tuple[Optional[int], Optional[int]]:
    """Send a new ticket message to the LOG_CHAT via Bot API. Returns (chat_id, message_id) if ok."""
    if not BOT_TOKEN or not LOG_CHAT_ID:
        # Not configured, silently skip
        return None, None

    base_url = f"https://api.telegram.org/bot{BOT_TOKEN}"
    text = build_telegram_text(d)
    kb = build_telegram_keyboard(d["id"], d.get("status") or DEFAULT_STATUS)

    try:
        if first_photo_path and os.path.isfile(first_photo_path):
            url = f"{base_url}/sendPhoto"
            with open(first_photo_path, "rb") as f:
                files = {"photo": f}
                data = {
                    "chat_id": LOG_CHAT_ID,
                    "caption": text,
                    "parse_mode": "Markdown",
                    "reply_markup": json.dumps(kb),
                }
                resp = requests.post(url, data=data, files=files, timeout=10)
        else:
            url = f"{base_url}/sendMessage"
            payload = {
                "chat_id": LOG_CHAT_ID,
                "text": text,
                "parse_mode": "Markdown",
                "reply_markup": kb,
            }
            resp = requests.post(url, json=payload, timeout=10)

        if not resp.ok:
            print("Telegram send error:", resp.text)
            return None, None

        data = resp.json()
        if not data.get("ok"):
            print("Telegram send API not ok:", data)
            return None, None

        msg = data["result"]
        return msg["chat"]["id"], msg["message_id"]

    except Exception as e:
        print("Telegram send exception:", e)
        return None, None


def edit_telegram_ticket(d: dict) -> None:
    """Edit the existing Telegram message for this ticket."""
    if not BOT_TOKEN or not LOG_CHAT_ID:
        return

    chat_id = d.get("chat_id")
    msg_id = d.get("bot_msg_id") or d.get("open_msg_id")
    if not chat_id or not msg_id:
        return

    base_url = f"https://api.telegram.org/bot{BOT_TOKEN}"
    text = build_telegram_text(d)
    kb = build_telegram_keyboard(d["id"], d.get("status") or DEFAULT_STATUS)
    url = f"{base_url}/editMessageText"

    try:
        payload = {
            "chat_id": chat_id,
            "message_id": msg_id,
            "text": text,
            "parse_mode": "Markdown",
            "reply_markup": kb,
        }
        resp = requests.post(url, json=payload, timeout=10)
        if not resp.ok:
            print("Telegram edit error:", resp.text)
    except Exception as e:
        print("Telegram edit exception:", e)


# ======================
# Pydantic MODELS
# ======================

class TicketOut(BaseModel):
    id: int
    status: str
    priority: Optional[str]
    branch: Optional[str]
    department: Optional[str]
    category: Optional[str]
    open_user: Optional[str]
    open_ts_local: str
    description: Optional[str]
    it_note: Optional[str]
    has_photo: bool


class TicketDetailOut(BaseModel):
    id: int
    status: str
    priority: Optional[str]
    branch: Optional[str]
    department: Optional[str]
    category: Optional[str]
    open_user: Optional[str]
    open_user_id: Optional[int]
    open_ts_local: str
    open_text: Optional[str]
    close_note: Optional[str]
    has_photo: bool
    photo_urls: List[str]
    closed_ts_local: Optional[str]


# ======================
# FASTAPI APP
# ======================

UPLOAD_DIR.mkdir(exist_ok=True)

app = FastAPI(title="Ticket MiniApp")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve uploaded files
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")


@app.on_event("startup")
def on_startup():
    init_db()
    print("✅ Ticket MiniApp started – DB ready")


# ======================
# UI ROUTE
# ======================

@app.get("/", response_class=HTMLResponse)
async def root():
    """
    Single UI entrypoint.
    Telegram / browser always hit '/' and get index.html.
    """
    if INDEX_PATH.is_file():
        return HTMLResponse(INDEX_PATH.read_text(encoding="utf-8"))
    return HTMLResponse(
        "<h1>Ticket MiniApp</h1>"
        "<p>index.html not found yet. Place your UI at ./index.html</p>",
        status_code=200,
    )


@app.get("/health")
async def health():
    return {"status": "ok", "time_utc": utcnow_iso()}


# ======================
# API: LIST / DETAIL
# ======================

@app.get("/api/tickets", response_model=List[TicketOut])
async def list_tickets(
    status: Optional[str] = Query(None),
    branch: Optional[str] = Query(None),
    department: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=300),
    offset: int = Query(0, ge=0),
):
    where_clauses = ["(deleted IS NULL OR deleted = 0)"]
    params: list = []

    if status:
        where_clauses.append("status = ?")
        params.append(status)
    if branch:
        where_clauses.append("branch = ?")
        params.append(branch)
    if department:
        where_clauses.append("department = ?")
        params.append(department)
    if category:
        where_clauses.append("category = ?")
        params.append(category)
    if search:
        where_clauses.append("(open_text LIKE ? OR open_user LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like])

    where_sql = " AND ".join(where_clauses)
    sql = f"""
        SELECT
            id,
            status,
            priority,
            branch,
            department,
            category,
            open_user,
            open_text,
            close_note,
            open_ts_utc,
            photo_file_id,
            is_photo
        FROM tickets
        WHERE {where_sql}
        ORDER BY id DESC
        LIMIT ? OFFSET ?
    """
    params.extend([limit, offset])

    with closing(db()) as conn:
        cur = conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()

    results: List[TicketOut] = []
    for r in rows:
        d = row_to_dict(r)
        photo_paths = parse_photo_paths(d.get("photo_file_id"))
        results.append(
            TicketOut(
                id=d["id"],
                status=d.get("status") or DEFAULT_STATUS,
                priority=d.get("priority"),
                branch=d.get("branch"),
                department=d.get("department"),
                category=d.get("category"),
                open_user=d.get("open_user"),
                open_ts_local=to_local_str(d.get("open_ts_utc")) or "",
                description=d.get("open_text"),
                it_note=d.get("close_note"),
                has_photo=bool(photo_paths),
            )
        )
    return results


@app.get("/api/tickets/{ticket_id}", response_model=TicketDetailOut)
async def get_ticket_detail(ticket_id: int):
    with closing(db()) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM tickets WHERE id = ? AND (deleted IS NULL OR deleted = 0)",
            (ticket_id,),
        )
        row = cur.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Ticket not found")

    d = row_to_dict(row)
    photo_urls = make_photo_urls(d.get("photo_file_id"))
    return TicketDetailOut(
        id=d["id"],
        status=d.get("status") or DEFAULT_STATUS,
        priority=d.get("priority"),
        branch=d.get("branch"),
        department=d.get("department"),
        category=d.get("category"),
        open_user=d.get("open_user"),
        open_user_id=d.get("open_user_id"),
        open_ts_local=to_local_str(d.get("open_ts_utc")) or "",
        open_text=d.get("open_text"),
        close_note=d.get("close_note"),
        has_photo=bool(photo_urls),
        photo_urls=photo_urls,
        closed_ts_local=to_local_str(d.get("closed_ts_utc")),
    )


# ======================
# API: CREATE TICKET
# ======================

@app.post("/api/tickets", response_model=TicketDetailOut)
async def create_ticket(
    branch: str = Form(...),
    department: str = Form(...),
    category: str = Form(...),
    description: str = Form(""),
    open_user: str = Form(...),
    open_user_id: Optional[int] = Form(None),
    photos: List[UploadFile] = File([]),
):
    if not description.strip():
        description = "(no description)"

    open_ts = utcnow_iso()

    saved_paths: List[str] = []
    # Save up to 3 photos
    for up in photos[:3]:
        if not up or not up.filename:
            continue
        raw_name = up.filename
        filename = f"{int(time.time())}_{raw_name}"
        safe_name = "".join(c for c in filename if c not in r'\/:*?"<>|')
        dest = UPLOAD_DIR / safe_name
        with dest.open("wb") as f:
            shutil.copyfileobj(up.file, f)
        saved_paths.append(str(dest))

    photo_paths_str: Optional[str] = "|".join(saved_paths) if saved_paths else None
    is_photo = 1 if saved_paths else 0

    with closing(db()) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO tickets
            (chat_id, open_msg_id, bot_msg_id,
             open_user, open_user_id,
             open_text, open_ts_utc,
             tags, assignees, status,
             branch, department, category,
             close_note, photo_file_id, is_photo,
             closed_ts_utc, close_photo_file_id, deleted)
            VALUES (?,?,?,?,?,
                    ?,?,
                    ?,?,?, 
                    ?,?,?, 
                    ?,?,?, 
                    NULL,NULL,0)
            """,
            (
                LOG_CHAT_ID or 0,   # chat_id
                0,                  # open_msg_id
                None,               # bot_msg_id
                open_user,
                open_user_id,
                description,
                open_ts,
                "",
                "",
                DEFAULT_STATUS,
                branch,
                department,
                category,
                None,               # close_note
                photo_paths_str,
                is_photo,
            ),
        )
        ticket_id = cur.lastrowid

        cur.execute(
            """
            INSERT INTO events (ticket_id, chat_id, msg_id, kind, author, text, ts_utc)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                ticket_id,
                0,
                None,
                "OPEN",
                open_user,
                description,
                open_ts,
            ),
        )

        cur.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,))
        row = cur.fetchone()
        conn.commit()

    d = row_to_dict(row)

    # Telegram notification
    first_photo_path = parse_photo_paths(d.get("photo_file_id"))[0] if is_photo else None
    chat_id, msg_id = send_telegram_new_ticket(d | {"id": ticket_id}, first_photo_path)
    if chat_id and msg_id:
        with closing(db()) as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE tickets SET chat_id=?, open_msg_id=?, bot_msg_id=? WHERE id=?",
                (chat_id, msg_id, msg_id, ticket_id),
            )
            conn.commit()
        d["chat_id"] = chat_id
        d["open_msg_id"] = msg_id
        d["bot_msg_id"] = msg_id

    photo_urls = make_photo_urls(d.get("photo_file_id"))
    return TicketDetailOut(
        id=ticket_id,
        status=d.get("status") or DEFAULT_STATUS,
        priority=d.get("priority"),
        branch=d.get("branch"),
        department=d.get("department"),
        category=d.get("category"),
        open_user=d.get("open_user"),
        open_user_id=d.get("open_user_id"),
        open_ts_local=to_local_str(d.get("open_ts_utc")) or "",
        open_text=d.get("open_text"),
        close_note=d.get("close_note"),
        has_photo=bool(photo_urls),
        photo_urls=photo_urls,
        closed_ts_local=to_local_str(d.get("closed_ts_utc")),
    )


# ======================
# API: STATUS / PRIORITY (ADMIN)
# ======================

@app.post("/api/tickets/{ticket_id}/status_priority")
async def update_status_priority(
    ticket_id: int,
    status: Optional[str] = Form(None),
    priority: Optional[str] = Form(None),
):
    updates = []
    params: List[object] = []

    if status:
        if status not in ALL_STATUSES:
            raise HTTPException(400, "Invalid status")
        updates.append("status = ?")
        params.append(status)

    if priority:
        if priority not in ALL_PRIORITIES:
            raise HTTPException(400, "Invalid priority")
        updates.append("priority = ?")
        params.append(priority)

    if not updates:
        return {"status": "ok", "message": "No changes"}

    with closing(db()) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM tickets WHERE id = ? AND (deleted IS NULL OR deleted = 0)",
            (ticket_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Ticket not found")

        sql = f"UPDATE tickets SET {', '.join(updates)} WHERE id = ?"
        params.append(ticket_id)
        cur.execute(sql, params)

        # Event
        now = utcnow_iso()
        cur.execute(
            """
            INSERT INTO events (ticket_id, chat_id, msg_id, kind, author, text, ts_utc)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                ticket_id,
                row["chat_id"],
                row["bot_msg_id"],
                "STATUS",
                "MiniApp Admin",
                f"Status/priority changed to {status or row['status']}/{priority or row['priority']}",
                now,
            ),
        )

        cur.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,))
        updated = cur.fetchone()
        conn.commit()

    d = row_to_dict(updated)
    # Sync Telegram message
    edit_telegram_ticket(d | {"id": ticket_id})
    return {"status": "ok"}


# ======================
# API: IT NOTE (ADMIN)
# ======================

@app.post("/api/tickets/{ticket_id}/note")
async def update_it_note(
    ticket_id: int,
    note: str = Form(...),
):
    with closing(db()) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM tickets WHERE id = ? AND (deleted IS NULL OR deleted = 0)",
            (ticket_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Ticket not found")

        cur.execute(
            "UPDATE tickets SET close_note = ? WHERE id = ?",
            (note, ticket_id),
        )

        now = utcnow_iso()
        cur.execute(
            """
            INSERT INTO events (ticket_id, chat_id, msg_id, kind, author, text, ts_utc)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                ticket_id,
                row["chat_id"],
                row["bot_msg_id"],
                "NOTE",
                "MiniApp Admin",
                note,
                now,
            ),
        )

        cur.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,))
        updated = cur.fetchone()
        conn.commit()

    d = row_to_dict(updated)
    edit_telegram_ticket(d | {"id": ticket_id})
    return {"status": "ok"}


# ======================
# API: ADMIN ADD PHOTOS
# ======================

@app.post("/api/tickets/{ticket_id}/admin_photos")
async def admin_add_photos(
    ticket_id: int,
    photos: List[UploadFile] = File([]),
):
    if not photos:
        raise HTTPException(400, "No photos uploaded")

    with closing(db()) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM tickets WHERE id = ? AND (deleted IS NULL OR deleted = 0)",
            (ticket_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Ticket not found")

        existing_paths = parse_photo_paths(row["photo_file_id"])
        for up in photos:
            if not up or not up.filename:
                continue
            raw_name = up.filename
            filename = f"{int(time.time())}_{raw_name}"
            safe_name = "".join(c for c in filename if c not in r'\/:*?"<>|')
            dest = UPLOAD_DIR / safe_name
            with dest.open("wb") as f:
                shutil.copyfileobj(up.file, f)
            existing_paths.append(str(dest))

        new_paths_str = "|".join(existing_paths)
        cur.execute(
            "UPDATE tickets SET photo_file_id = ?, is_photo = ? WHERE id = ?",
            (new_paths_str, 1 if existing_paths else 0, ticket_id),
        )

        now = utcnow_iso()
        cur.execute(
            """
            INSERT INTO events (ticket_id, chat_id, msg_id, kind, author, text, ts_utc)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                ticket_id,
                row["chat_id"],
                row["bot_msg_id"],
                "PHOTO",
                "MiniApp Admin",
                f"Added {len(photos)} photo(s) via MiniApp",
                now,
            ),
        )

        cur.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,))
        updated = cur.fetchone()
        conn.commit()

    d = row_to_dict(updated)
    edit_telegram_ticket(d | {"id": ticket_id})
    return {"status": "ok"}


# ======================
# API: SOFT DELETE (ADMIN)
# ======================

@app.post("/api/tickets/{ticket_id}/delete")
async def soft_delete_ticket(ticket_id: int):
    with closing(db()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Ticket not found")

        d = row_to_dict(row)
        if d.get("deleted"):
            return {"status": "ok", "message": "Already deleted"}

        cur.execute(
            "UPDATE tickets SET deleted = 1 WHERE id = ?",
            (ticket_id,),
        )

        cur.execute(
            """
            INSERT INTO events (ticket_id, chat_id, msg_id, kind, author, text, ts_utc)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                ticket_id,
                d.get("chat_id") or 0,
                d.get("bot_msg_id"),
                "DELETE",
                "MiniApp Admin",
                "Ticket soft-deleted via MiniApp",
                utcnow_iso(),
            ),
        )

        conn.commit()

    return {"status": "ok"}


# ======================
# API: MY TICKETS (STAFF)
# ======================

@app.get("/api/my_tickets", response_model=List[TicketOut])
async def my_tickets(
    open_user_id: int = Query(...),
):
    with closing(db()) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, status, priority, branch, department, category,
                   open_user, open_text, close_note, open_ts_utc, photo_file_id, is_photo
            FROM tickets
            WHERE open_user_id = ?
              AND (deleted IS NULL OR deleted = 0)
            ORDER BY id DESC
            """,
            (open_user_id,),
        )
        rows = cur.fetchall()

    results: List[TicketOut] = []
    for r in rows:
        d = row_to_dict(r)
        photo_paths = parse_photo_paths(d.get("photo_file_id"))
        results.append(
            TicketOut(
                id=d["id"],
                status=d.get("status") or DEFAULT_STATUS,
                priority=d.get("priority"),
                branch=d.get("branch"),
                department=d.get("department"),
                category=d.get("category"),
                open_user=d.get("open_user"),
                open_ts_local=to_local_str(d.get("open_ts_utc")) or "",
                description=d.get("open_text"),
                it_note=d.get("close_note"),
                has_photo=bool(photo_paths),
            )
        )
    return results

@app.post("/api/my_tickets/{ticket_id}/edit")
async def edit_my_ticket(
    ticket_id: int,
    open_user_id: int = Form(...),
    description: str = Form(""),
):
    """
    Let the ticket owner update their own description from the MiniApp.
    """
    if not description.strip():
        description = "(no description)"

    with closing(db()) as conn:
        cur = conn.cursor()
        # Get ticket and ensure not deleted
        cur.execute(
            "SELECT * FROM tickets WHERE id = ? AND (deleted IS NULL OR deleted = 0)",
            (ticket_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Ticket not found")

        # Only the original opener can edit
        if row["open_user_id"] != open_user_id:
            raise HTTPException(status_code=403, detail="Not allowed to edit this ticket")

        # Update the description (column is `open_text` in your DB)
        cur.execute(
            "UPDATE tickets SET open_text = ? WHERE id = ?",
            (description, ticket_id),
        )

        # Optional: log an event
        now = utcnow_iso()
        cur.execute(
            """
            INSERT INTO events (ticket_id, chat_id, msg_id, kind, author, text, ts_utc)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                ticket_id,
                row["chat_id"],
                row["bot_msg_id"],
                "EDIT_BY_USER",
                row["open_user"] or "MiniApp User",
                f"User updated description via MiniApp",
                now,
            ),
        )

        # Re-read updated ticket for Telegram sync (if you want)
        cur.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,))
        updated = cur.fetchone()
        conn.commit()

    # Optionally sync Telegram message with new description
    d = row_to_dict(updated)
    edit_telegram_ticket(d | {"id": ticket_id})

    return {"status": "ok"}

@app.post("/api/my_tickets/{ticket_id}/delete")
async def delete_my_ticket(
    ticket_id: int,
    open_user_id: int = Form(...),
):
    with closing(db()) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM tickets WHERE id = ? AND (deleted IS NULL OR deleted = 0)",
            (ticket_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Ticket not found")

        if row["open_user_id"] != open_user_id:
            raise HTTPException(403, "You can only delete your own tickets")

        cur.execute(
            "UPDATE tickets SET deleted = 1 WHERE id = ?",
            (ticket_id,),
        )

        now = utcnow_iso()
        cur.execute(
            """
            INSERT INTO events (ticket_id, chat_id, msg_id, kind, author, text, ts_utc)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                ticket_id,
                row["chat_id"],
                row["bot_msg_id"],
                "DELETE_BY_USER",
                row["open_user"],
                "Ticket deleted by user via MiniApp",
                now,
            ),
        )

        conn.commit()

    # We leave Telegram message as-is; DB mark hides from stats/UI
    return {"status": "ok"}


# ======================
# API: MONTHLY EXPORT (ADMIN)
# ======================

@app.get("/api/export/month")
async def export_month(
    ym: str = Query(..., description="Year-month in format YYYY-MM"),
):
    # Parse ym
    try:
        year_s, month_s = ym.split("-")
        year = int(year_s)
        month = int(month_s)
        if month < 1 or month > 12:
            raise ValueError
    except Exception:
        raise HTTPException(400, "ym must be in format YYYY-MM")

    start = datetime.datetime(year, month, 1, tzinfo=datetime.timezone.utc)
    if month == 12:
        end = datetime.datetime(year + 1, 1, 1, tzinfo=datetime.timezone.utc)
    else:
        end = datetime.datetime(year, month + 1, 1, tzinfo=datetime.timezone.utc)

    start_iso = start.isoformat()
    end_iso = end.isoformat()

    # Fetch data
    with closing(db()) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, status, priority, branch, department, category,
                   open_user, open_ts_utc, closed_ts_utc, open_text, close_note
            FROM tickets
            WHERE (deleted IS NULL OR deleted = 0)
              AND open_ts_utc >= ? AND open_ts_utc < ?
            ORDER BY open_ts_utc ASC
            """,
            (start_iso, end_iso),
        )
        rows = [dict(r) for r in cur.fetchall()]

    if not rows:
        raise HTTPException(404, "No tickets in that month")

    # Local time strings
    for r in rows:
        r["open_local"] = to_local_str(r["open_ts_utc"])
        r["closed_local"] = to_local_str(r.get("closed_ts_utc"))

    # Build DataFrame
    df = pd.DataFrame(rows)
    cols = [
        "id",
        "status",
        "priority",
        "branch",
        "department",
        "category",
        "open_user",
        "open_local",
        "closed_local",
        "open_text",
        "close_note",
    ]
    df = df[cols]
    df.columns = [
        "ID",
        "Status",
        "Priority",
        "Property",
        "Department",
        "Type",
        "Opened by",
        "Opened (local)",
        "Closed (local)",
        "User description",
        "IT note",
    ]

    # ===== Excel styling (corporate) =====
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="xlsxwriter", datetime_format="yyyy-mm-dd hh:mm") as writer:
        df.to_excel(writer, index=False, sheet_name="Tickets")
        wb = writer.book
        ws = writer.sheets["Tickets"]

        # Header style
        header_format = wb.add_format({
            "bold": True,
            "font_color": "white",
            "bg_color": "#3B91FF",  
            "align": "center",
            "valign": "vcenter",
            "border": 1
        })

        # Apply header format
        for col_num, value in enumerate(df.columns.values):
            ws.write(0, col_num, value, header_format)

        # Body styles
        body_format = wb.add_format({
            "font_size": 10,
            "align": "left",
            "valign": "top",
            "border": 1,
            "text_wrap": True,
        })

        alt_body_format = wb.add_format({
            "font_size": 10,
            "align": "left",
            "valign": "top",
            "border": 1,
            "text_wrap": True,
            "bg_color": "#F3F4F6",  # light gray for zebra striping
        })

        num_rows = len(df)
        for row in range(1, num_rows + 1):
            fmt = body_format if row % 2 == 1 else alt_body_format
            ws.set_row(row, 18, fmt)

        # Column widths
        ws.set_column("A:A", 6)    # ID
        ws.set_column("B:C", 10)   # Status, Priority
        ws.set_column("D:F", 18)   # Property, Department, Type
        ws.set_column("G:G", 18)   # Opened by
        ws.set_column("H:I", 20)   # Opened (local), Closed (local)
        ws.set_column("J:K", 55)   # User description, IT note

        # Freeze header and add filter
        ws.freeze_panes(1, 0)
        ws.autofilter(0, 0, num_rows, len(df.columns) - 1)

    buffer.seek(0)
    filename = f"tickets_{ym}.xlsx"
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"'
    }
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=headers,
    )


# ======================
# MAIN (manual run)
# ======================

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "ticket_miniapp:app",
        host="0.0.0.0",
        port=8001,
        reload=True,
    )
