"""Hanwha Monitor server (standard library; httpx[http2] + cryptography only for Apple push).

Receives snapshots from the Windows agent, keeps the alarm history in SQLite, and serves
status to the iPhone app plus a small web dashboard.

  POST /api/ingest     agent -> server snapshot
  POST /api/replay     agent -> snapshots it kept while the server was unreachable {snapshots: [...]} (oldest first)
  GET  /api/status     current machine status + active alarms
  GET  /api/alarms     alarm history  (?limit=100&before=<epoch>&active=1)
  DELETE /api/alarms   clear cleared history  (?demo=1: delete demo-mode alarms only)
  GET  /api/states     state change log (?hours=24)
  PUT  /api/agent/update?version=X  (exe body, X-Signature header)   GET /api/agent/update   GET /api/agent/update/download
  GET  /api/barchanges   bar change history
  GET  /api/programs   program info list    GET /api/programs/detail?program=O3110   (+ its bars)
  POST /api/programs {program, name?, ppb_manual?, notes?, stick_out?}   only the fields sent change
  GET/PUT/DELETE /api/programs/doc?program=O3110   the job PDF (dimensions) for a program; PUT body = the PDF
  GET  /api/report?period=day|month|year&from=&to=   parts, bars and running time per day/month/year
  GET  /api/maintenance   backup battery etc: when it was last done, when it's next due
  POST /api/maintenance {item, changed|last_at, every_months?, warn_days?, notes?}
  DELETE /api/bars?id=17 | ?program=O3110   forget one bar / all of a program's bars
  GET  /api/bars/day?date=2026-09-12   the day's bar changes (time, parts, program(s))
  GET  /api/bars   parts per bar per program + recent bars      DELETE /api/bars?program=O1234   forget a program's bars
  GET  /api/messages   operator message history (?limit=100) - e.g. "work count end in 1 hour", not alarms
  GET  /api/health
  POST /api/push/register   {kind: alert|la|la_start, token, activity_id?, env?, prefs?}
  POST /api/push/unregister {token? , activity_id?}
  GET  /api/push/status     POST /api/push/test
  POST /api/camera/frame    (agent, image/jpeg)   GET /api/camera/frame.jpg   GET /api/camera/stream (MJPEG)
  PUT  /api/camera/clip?alarm=<id>&at=&seconds=&delay=  (agent, video/mp4)   GET /api/camera/clip?alarm=<id>
  GET  /api/camera/clips    the saved alarm clips
  POST /api/commands {type: ptz|set_required|set_work_counter, ...} -> result from the PC (waits up to ~8 s)
  GET  /api/agent/commands?wait=25 (agent long-poll)   POST /api/agent/results {id, ok, message}
  GET  /api/camera/status   GET /api/camera/live -> token URL of the live HLS stream
  PUT  /api/camera/hls/push/<session>/<file>  (agent's ffmpeg)   GET /api/camera/hls/v/<token>/<session>/<file>
  GET  /               web dashboard (needs a web login)      GET /login  login page
  GET  /programs  /report  /wall  /settings   the other dashboard pages
  GET  /auth/info   POST /auth/login {username,password,api_key?}   POST /auth/change   POST /auth/logout

/api/* accepts either the API key (X-API-Key header, or ?key=) or a logged-in browser session.
Without API_KEY set, /api/* is open (the agent and app need no key) - only the dashboard page needs a login.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlparse

try:  # works both as "python app/server.py" and as a package import
    from apns import APNs
    from auth import COOKIE, DEVICE_COOKIE, DEVICE_DAYS, SESSION_DAYS, Auth
    from camera import MAX_FRAME, Camera, LiveVideo
    from commands import ALLOWED, Commands
    from push import PushService
except ImportError:  # pragma: no cover
    from .apns import APNs
    from .auth import COOKIE, DEVICE_COOKIE, DEVICE_DAYS, SESSION_DAYS, Auth
    from .camera import MAX_FRAME, Camera, LiveVideo
    from .commands import ALLOWED, Commands
    from .push import PushService

VERSION = "1.11.1"
DB_PATH = os.environ.get("DB_PATH", "/data/monitor.db")
API_KEY = os.environ.get("API_KEY", "").strip()
AGENT_TIMEOUT = float(os.environ.get("AGENT_TIMEOUT", "30"))
PORT = int(os.environ.get("PORT", "8420"))
# Running -> standby only shows once standby has lasted this long (hides the gap between part cycles).
STANDBY_DELAY = float(os.environ.get("STANDBY_DELAY", "10"))
# a bar change lasting longer than this sends a "taking long" notification (usually a failed bar load)
BAR_CHANGE_ALERT = float(os.environ.get("BAR_CHANGE_ALERT", "180"))
STATIC = Path(__file__).parent / "static"
# job PDFs (dimensions etc.) kept next to the database, one per program
DOCS = Path(os.environ.get("DOCS_DIR", str(Path(DB_PATH).parent / "docs")))
MAX_DOC = int(float(os.environ.get("MAX_DOC_MB", "25")) * 1024 * 1024)
# camera clips saved around an alarm, sent up by the PC app
CLIPS = Path(os.environ.get("CLIPS_DIR", str(Path(DB_PATH).parent / "clips")))
MAX_CLIP = int(float(os.environ.get("MAX_CLIP_MB", "60")) * 1024 * 1024)
KEEP_CLIPS = int(os.environ.get("KEEP_CLIPS", "300"))          # newest this many are kept
KEEP_CLIP_DAYS = float(os.environ.get("KEEP_CLIP_DAYS", "60"))

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")
log = logging.getLogger("hanwha-server")
_lock = threading.RLock()


# --------------------------------------------------------------------------- database
def _connect() -> sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript("""
        CREATE TABLE IF NOT EXISTS alarms (
            id TEXT PRIMARY KEY, key TEXT NOT NULL, path INTEGER, path_name TEXT, code TEXT,
            type INTEGER, type_name TEXT, number INTEGER, axis INTEGER, message TEXT,
            started_at REAL NOT NULL, cleared_at REAL);
        CREATE INDEX IF NOT EXISTS alarms_started ON alarms(started_at DESC);
        CREATE INDEX IF NOT EXISTS alarms_open ON alarms(key) WHERE cleared_at IS NULL;
        CREATE TABLE IF NOT EXISTS alarm_alias (agent_id TEXT PRIMARY KEY, alarm_id TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS state_log (ts REAL NOT NULL, state TEXT NOT NULL, detail TEXT);
        CREATE INDEX IF NOT EXISTS state_log_ts ON state_log(ts DESC);
        CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS op_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, number INTEGER, text TEXT NOT NULL,
            started_at REAL NOT NULL, cleared_at REAL, demo INTEGER);
        CREATE INDEX IF NOT EXISTS op_messages_started ON op_messages(started_at DESC);
        CREATE TABLE IF NOT EXISTS bar_changes (started_at REAL NOT NULL, ended_at REAL NOT NULL, duration_s REAL NOT NULL,
            parts INTEGER, demo INTEGER);
        CREATE INDEX IF NOT EXISTS bar_changes_started ON bar_changes(started_at DESC);
        -- one row per whole bar: how many parts it made, against the main program that was running
        CREATE TABLE IF NOT EXISTS bars (id INTEGER PRIMARY KEY AUTOINCREMENT, program TEXT NOT NULL, parts INTEGER NOT NULL,
            started_at REAL NOT NULL, ended_at REAL NOT NULL, demo INTEGER);
        CREATE INDEX IF NOT EXISTS bars_program ON bars(program, ended_at DESC);
        -- your own names for programs ("EMS301"), shown after the number: O3110 - EMS301
        CREATE TABLE IF NOT EXISTS programs (program TEXT PRIMARY KEY, name TEXT NOT NULL, updated REAL);
        -- part-to-part cycle times per program (the most recent CYCLE_KEEP of each are kept)
        CREATE TABLE IF NOT EXISTS cycles (id INTEGER PRIMARY KEY AUTOINCREMENT, program TEXT NOT NULL, cycle_s REAL NOT NULL,
            at REAL NOT NULL, demo INTEGER);
        CREATE INDEX IF NOT EXISTS cycles_program ON cycles(program, at DESC);
        -- day-by-day production figures the operations report adds up (program "" = none loaded)
        CREATE TABLE IF NOT EXISTS daily (day TEXT NOT NULL, program TEXT NOT NULL, demo INTEGER NOT NULL DEFAULT 0,
            parts INTEGER NOT NULL DEFAULT 0, bars INTEGER NOT NULL DEFAULT 0, cycles INTEGER NOT NULL DEFAULT 0,
            cycle_s REAL NOT NULL DEFAULT 0, run_s REAL NOT NULL DEFAULT 0, standby_s REAL NOT NULL DEFAULT 0,
            alarm_s REAL NOT NULL DEFAULT 0, off_s REAL NOT NULL DEFAULT 0, first_at REAL, last_at REAL,
            PRIMARY KEY (day, program, demo));
        CREATE INDEX IF NOT EXISTS daily_day ON daily(day DESC);
        -- camera clips saved around an alarm (the video file itself sits in clips/ next to the database)
        CREATE TABLE IF NOT EXISTS clips (alarm_id TEXT PRIMARY KEY, at REAL NOT NULL, seconds REAL, size INTEGER,
            code TEXT, message TEXT, uploaded_at REAL NOT NULL, delay_s REAL);
        CREATE INDEX IF NOT EXISTS clips_at ON clips(at DESC);
        -- things that have to be done every so often - the machine's backup batteries to start with
        CREATE TABLE IF NOT EXISTS maintenance (item TEXT PRIMARY KEY, name TEXT NOT NULL, last_at REAL,
            every_months REAL NOT NULL DEFAULT 12, warn_days REAL NOT NULL DEFAULT 30, notes TEXT,
            history TEXT, updated REAL);
    """)
    cols = {r["name"] for r in c.execute("PRAGMA table_info(alarms)")}
    if "demo" not in cols:   # 1 = from the agent's demo mode, 0 = real machine, NULL = recorded before 1.2.0
        c.execute("ALTER TABLE alarms ADD COLUMN demo INTEGER")
    pcols = {r["name"] for r in c.execute("PRAGMA table_info(programs)")}
    if "ppb_manual" not in pcols:   # parts per bar typed in by you (overrides the learnt figure)
        c.execute("ALTER TABLE programs ADD COLUMN ppb_manual REAL")
    if "notes" not in pcols:
        c.execute("ALTER TABLE programs ADD COLUMN notes TEXT")
    for col, decl in (("stick_out", "REAL"),      # sub spindle stick out for this job, in mm
                      ("doc_name", "TEXT"),       # the job PDF's original file name (the file itself is in docs/)
                      ("doc_at", "REAL"), ("doc_size", "INTEGER")):
        if col not in pcols:
            c.execute(f"ALTER TABLE programs ADD COLUMN {col} {decl}")
    bcols = {r["name"] for r in c.execute("PRAGMA table_info(bars)")}
    if "programs" not in bcols:      # [{"program": "O1234", "parts": 30}, ...] when the program changed mid-bar
        c.execute("ALTER TABLE bars ADD COLUMN programs TEXT")
    if "partial" not in bcols:       # 1 = more than one program ran on this bar: left out of the averages
        c.execute("ALTER TABLE bars ADD COLUMN partial INTEGER")
    return c


db = _connect()


def kv_get(k: str, default=None):
    row = db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return json.loads(row["v"]) if row else default


def kv_set(k: str, v):
    db.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))


# in-memory copies, persisted so a container restart keeps the last known values
latest: dict[str, Any] = kv_get("latest", {}) or {}
meta: dict[str, Any] = kv_get("meta", {}) or {}


# --------------------------------------------------------------------------- logic
def _asdict(v) -> dict:
    return v if isinstance(v, dict) else {}


def alarm_row(r: sqlite3.Row, now: float) -> dict:
    d = dict(r)
    d.pop("key", None)
    end = d["cleared_at"] or now
    d["duration_s"] = round(max(0.0, end - d["started_at"]), 1)
    d["active"] = d["cleared_at"] is None
    d["demo"] = bool(d.get("demo"))
    d["has_clip"] = d.get("id") in clip_ids      # a camera clip of what happened was saved
    return d


def effective_state(now: float) -> tuple[str, str, bool]:
    last_seen = meta.get("last_seen")
    agent_online = bool(last_seen and now - last_seen <= AGENT_TIMEOUT)
    if not latest:
        return "off", "Waiting for the monitor PC", agent_online
    if not agent_online:
        return "off", "Monitor PC not reporting", False
    if not latest.get("machine_connected"):
        return "off", latest.get("state_detail") or "Machine off / unreachable", True
    state = latest.get("state", "off")
    if (state == "standby" and meta.get("state") == "running"
            and now - (meta.get("raw_since") or now) < STANDBY_DELAY):
        # the short pause between part cycles - keep showing running
        return "running", (meta.get("last_running") or {}).get("detail") or "Running", True
    return state, latest.get("state_detail", ""), True


# A blip - the monitor PC restarting (e.g. to install an update), a network hiccup, a missed FOCAS poll - briefly
# shows as Off. If the machine is back in the same state within this long, keep the original "since" time instead of
# restarting the clock.
STATE_GLITCH = float(os.environ.get("STATE_GLITCH", "120"))


def track_state(now: float):
    state, detail, _ = effective_state(now)
    if state != meta.get("state"):
        log.info("State %s -> %s (%s)", meta.get("state"), state, detail)
        if meta.get("state") == "running":
            meta["running_ended_at"] = now      # for "only notify while running" (+ a short grace period)
        was, was_since = meta.get("state"), meta.get("state_since") or now
        blip = now - was_since               # how long the state it is leaving lasted
        # a dropout (off) that comes straight back, or any flicker of a few seconds, shouldn't restart the clock
        if state == meta.get("prev_state") and blip < (STATE_GLITCH if was == "off" else 6):
            meta["state_since"] = meta.get("prev_state_since") or now
            log.info("  (a %.0f s blip - %s since %s kept)", blip, state,
                     time.strftime("%H:%M:%S", time.localtime(meta["state_since"])))
        else:
            meta["state_since"] = now
        meta["prev_state"], meta["prev_state_since"] = was, was_since
        meta["state"] = state
        db.execute("INSERT INTO state_log(ts,state,detail) VALUES(?,?,?)", (now, state, detail))
        kv_set("meta", meta)


# The alarms the agent's demo machine makes up (focas.MockMachine) - used to spot demo alarms recorded
# before the server started tagging them.
DEMO_ALARMS = {(1051, "BARFEEDER EMERGENCY STOP"), (1010, "MAIN CHUCK SENSOR ERR. ALARM"),
               (1049, "BARFEEDER AUTO OFF"), (401, "(Z1)SERVO V-READY OFF")}


def _demo_where() -> tuple[str, list]:
    """SQL matching demo alarms: tagged ones, plus untagged (pre-1.2.0) ones that match the demo list
    and happened before the server first heard from the real machine."""
    first_real = meta.get("first_real_at")
    legacy = " OR ".join("(number=? AND message=?)" for _ in DEMO_ALARMS)
    args: list = [x for pair in sorted(DEMO_ALARMS) for x in pair]
    sql = f"(demo=1 OR (demo IS NULL AND ({legacy})"
    if first_real:
        sql += " AND started_at < ?"
        args.append(first_real)
    return sql + "))", args


def _scalar(v):
    """SQLite only takes numbers, text and None - anything else from a malformed snapshot becomes None."""
    return v if isinstance(v, (int, float, str, bytes, type(None))) else None


def upsert_alarms(alarms: list[dict], offset: float, now: float, machine_connected: bool,
                  demo: bool = False) -> list[str]:
    """Merge the agent's alarm episodes into history. Returns ids of cleared episodes to acknowledge."""
    ack: list[str] = []
    active_keys: set[str] = set()

    def lookup(agent_id: str):
        r = db.execute("SELECT * FROM alarms WHERE id=?", (agent_id,)).fetchone()
        if r:
            return r
        a = db.execute("SELECT alarm_id FROM alarm_alias WHERE agent_id=?", (agent_id,)).fetchone()
        return db.execute("SELECT * FROM alarms WHERE id=?", (a["alarm_id"],)).fetchone() if a else None

    def open_by_key(key: str):
        return db.execute("SELECT * FROM alarms WHERE key=? AND cleared_at IS NULL ORDER BY started_at DESC LIMIT 1",
                          (key,)).fetchone()

    def insert(a: dict, started: float, cleared: float | None):
        db.execute("""INSERT OR IGNORE INTO alarms(id,key,path,path_name,code,type,type_name,number,axis,message,
                      started_at,cleared_at,demo) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (str(a["id"]), str(a["key"]), _scalar(a.get("path")), _scalar(a.get("path_name")),
                    _scalar(a.get("code")), _scalar(a.get("type")), _scalar(a.get("type_name")),
                    _scalar(a.get("number")), _scalar(a.get("axis")), _scalar(a.get("message")), started, cleared,
                    1 if demo else 0))
        if cleared is None:
            log.info("ALARM %s [%s] %s", a.get("code"), a.get("path_name"), a.get("message"))

    for a in alarms:
        if not isinstance(a, dict) or not a.get("id") or not a.get("key") or a.get("started_at") is None:
            continue
        started = float(a["started_at"]) + offset
        cleared = float(a["cleared_at"]) + offset if a.get("cleared_at") else None
        row = lookup(a["id"])
        if cleared is None:
            if not machine_connected:
                continue
            active_keys.add(a["key"])
            if row is not None:
                # a blip (one missed poll, the machine off for a moment) closes every open alarm below; when the
                # machine comes back still reporting this one, open it again rather than lose it for good
                if row["cleared_at"] is not None:
                    db.execute("UPDATE alarms SET cleared_at=NULL WHERE id=?", (row["id"],))
                    log.info("ALARM %s is still on the machine - reopened", a.get("code"))
                msg = _scalar(a.get("message"))
                if msg and msg != row["message"]:
                    db.execute("UPDATE alarms SET message=? WHERE id=?", (msg, row["id"]))
                continue
            existing = open_by_key(a["key"])
            if existing is not None:   # agent restarted while the alarm was active - keep the original
                db.execute("INSERT OR IGNORE INTO alarm_alias(agent_id,alarm_id) VALUES(?,?)", (a["id"], existing["id"]))
            else:
                insert(a, started, None)
        else:
            if row is not None:
                if row["cleared_at"] is None:
                    db.execute("UPDATE alarms SET cleared_at=? WHERE id=?", (max(cleared, row["started_at"]), row["id"]))
            else:
                existing = open_by_key(a["key"])
                if existing is not None:
                    db.execute("UPDATE alarms SET cleared_at=? WHERE id=?", (max(cleared, existing["started_at"]), existing["id"]))
                    db.execute("INSERT OR IGNORE INTO alarm_alias(agent_id,alarm_id) VALUES(?,?)", (a["id"], existing["id"]))
                else:   # happened while the server was down
                    insert(a, started, cleared)
            ack.append(a["id"])

    if machine_connected:
        # anything still open that the machine no longer reports has cleared
        for r in db.execute("SELECT id,key FROM alarms WHERE cleared_at IS NULL").fetchall():
            if r["key"] not in active_keys:
                db.execute("UPDATE alarms SET cleared_at=? WHERE id=?", (now, r["id"]))
    else:
        # machine switched off / unreachable: open alarms end when we last saw the machine
        end = meta.get("last_connected_at") or now
        db.execute("UPDATE alarms SET cleared_at=MAX(started_at, ?) WHERE cleared_at IS NULL", (end,))
    return ack


def upsert_messages(messages: list[dict], now: float, demo: bool):
    """Operator messages (not alarms): open a row when one appears, close it when it's gone."""
    current = {(int(m.get("number") or 0), str(m.get("text") or "").strip()) for m in messages if isinstance(m, dict)}
    open_rows = db.execute("SELECT id, number, text FROM op_messages WHERE cleared_at IS NULL").fetchall()
    have = {(r["number"], r["text"]): r["id"] for r in open_rows}
    for key, rid in have.items():
        if key not in current:
            db.execute("UPDATE op_messages SET cleared_at=? WHERE id=?", (now, rid))
    for number, text in current - set(have):
        db.execute("INSERT INTO op_messages(number,text,started_at,demo) VALUES(?,?,?,?)", (number, text, now, int(demo)))
        log.info("MESSAGE %s %s", number, text)


# ---- PC app updates: uploaded by push-to-github's download script, fetched by the PC app ----
UPDATE_DIR = Path(DB_PATH).parent / "agent-update"


def update_info() -> dict:
    try:
        return json.loads((UPDATE_DIR / "update.json").read_text())
    except (OSError, ValueError):
        return {}


def bar_change_stats(now: float) -> dict:
    lt = time.localtime(now)
    day_start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    r = db.execute("SELECT COUNT(*) n, AVG(duration_s) avg FROM bar_changes WHERE started_at>=? "
                   "AND COALESCE(demo,0)=0", (day_start,)).fetchone()
    last = db.execute("SELECT * FROM bar_changes WHERE COALESCE(demo,0)=0 ORDER BY started_at DESC LIMIT 1").fetchone()
    week = db.execute("SELECT AVG(duration_s) avg FROM bar_changes WHERE started_at>=? AND COALESCE(demo,0)=0",
                      (now - 7 * 86400,)).fetchone()
    return {"today": r["n"] or 0, "avg_today_s": round(r["avg"], 1) if r["avg"] else None,
            "avg_week_s": round(week["avg"], 1) if week["avg"] else None,
            "last_s": last["duration_s"] if last else None, "last_at": last["ended_at"] if last else None,
            "alert_after_s": BAR_CHANGE_ALERT}


# ---- parts per bar, remembered per program -------------------------------------------------------------
# A bar runs from the start of one bar change to the start of the next. The parts made in between are stored
# against the main program, so when that program is loaded again the parts per bar (and how many bars the job
# still needs) are known straight away.
BAR_HISTORY = 10          # average over this many recent bars of the program


def program_key(prog: dict | None) -> str | None:
    prog = prog if isinstance(prog, dict) else {}
    if prog.get("number"):
        return f"O{int(prog['number']):04d}"
    return (prog.get("name") or "").strip() or None


def program_name(key: str | None) -> str | None:
    if not key:
        return None
    r = db.execute("SELECT name FROM programs WHERE program=?", (key,)).fetchone()
    return (r["name"] or None) if r else None


def program_label(key: str | None) -> str | None:
    """"O3110 - EMS301" (or just "O3110" without a name)."""
    name = program_name(key)
    return f"{key} - {name}" if key and name else key


app_seen = [0.0]      # when the iPhone app last polled (see LA_IDLE_END in push.py)


# --------------------------------------------------------------------------- maintenance reminders
# Jobs that come round every so often. The machine's memory backup batteries are the one that matters:
# nothing warns you until the machine has already lost its parameters, so the date of the last change is
# kept here and the dashboard, the app and a notification say when the next one is due.
MAINT_ITEMS = {"battery": "Backup battery"}
MAINT_KEEP = 20          # past changes kept per item


# a date has to be somewhere between 1990 and 2100 to be a date at all
def sane_date(v) -> float | None:
    try:
        t = float(v)
    except (TypeError, ValueError):
        return None
    return t if 631152000 < t < 4102444800 else None


def add_months(when: float, months: float) -> float | None:
    """Same day of the month, `months` later (the 31st becomes the 30th etc. in a shorter month)."""
    if sane_date(when) is None:
        return None
    lt = time.localtime(when)
    total = lt.tm_mon - 1 + int(round(months))
    year, month = lt.tm_year + total // 12, total % 12 + 1
    last = [31, 29 if (year % 4 == 0 and year % 100 != 0) or year % 400 == 0 else 28,
            31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
    return time.mktime((year, month, min(lt.tm_mday, last), lt.tm_hour, lt.tm_min, lt.tm_sec, 0, 0, -1))


def maint_row(item: str, now: float | None = None) -> dict:
    """One maintenance job with when it's next due and whether that's passed."""
    now = now or time.time()
    r = db.execute("SELECT * FROM maintenance WHERE item=?", (item,)).fetchone()
    name = (r["name"] if r else None) or MAINT_ITEMS.get(item, item)
    every = float(r["every_months"]) if r and r["every_months"] else 12.0
    warn = float(r["warn_days"]) if r and r["warn_days"] is not None else 30.0
    last = sane_date(r["last_at"]) if r else None
    try:
        history = json.loads(r["history"]) if r and r["history"] else []
    except ValueError:
        history = []
    due = add_months(last, every) if last else None
    left = (due - now) / 86400 if due else None
    state = "unset" if not last else ("overdue" if left <= 0 else ("soon" if left <= warn else "ok"))
    return {"item": item, "name": name, "last_at": last, "every_months": every, "warn_days": warn,
            "notes": (r["notes"] or None) if r else None, "history": history[:MAINT_KEEP],
            "due_at": due, "days_left": round(left, 2) if left is not None else None, "state": state,
            "changes": len(history)}


def maint_all(now: float | None = None) -> list[dict]:
    now = now or time.time()
    keys = list(MAINT_ITEMS)
    keys += [r["item"] for r in db.execute("SELECT item FROM maintenance") if r["item"] not in keys]
    return [maint_row(k, now) for k in keys]


def maint_save(item: str, changes: dict, now: float | None = None) -> dict:
    """Update one job. changes may hold last_at, every_months, warn_days, notes, or changed=True (done now)."""
    now = now or time.time()
    cur = maint_row(item, now)
    last, history = cur["last_at"], list(cur["history"])
    if changes.get("changed"):
        last = sane_date(changes.get("last_at")) or now
        history = [last] + [h for h in history if abs(h - last) > 60]
    elif "last_at" in changes:
        # correcting the date replaces the most recent entry rather than adding another one
        last = sane_date(changes["last_at"]) if changes["last_at"] not in (None, "") else None
        if changes["last_at"] not in (None, "") and last is None:
            raise ValueError("that date doesn't look like a date")
        if last:
            history = [last] + [h for h in history[1:] if abs(h - last) > 60]
    every = cur["every_months"]
    if "every_months" in changes:
        every = max(1.0, min(120.0, float(changes["every_months"])))
    warn = cur["warn_days"]
    if "warn_days" in changes:
        warn = max(0.0, min(365.0, float(changes["warn_days"])))
    notes = cur["notes"] or ""
    if "notes" in changes:
        notes = str(changes.get("notes") or "").strip()[:500]
    db.execute("INSERT INTO maintenance(item,name,last_at,every_months,warn_days,notes,history,updated) "
               "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(item) DO UPDATE SET last_at=excluded.last_at, "
               "every_months=excluded.every_months, warn_days=excluded.warn_days, notes=excluded.notes, "
               "history=excluded.history, updated=excluded.updated",
               (item, cur["name"], last, every, warn, notes, json.dumps(history[:MAINT_KEEP]), now))
    return maint_row(item, now)


# ---- camera clips around an alarm -----------------------------------------------------------------
clip_ids: set[str] = set()


def clip_path(alarm_id: str) -> Path:
    """One file per alarm, named from a hash of its id (alarm ids aren't safe as file names)."""
    return CLIPS / (hashlib.sha1(alarm_id.encode()).hexdigest()[:20] + ".mp4")


def canonical_alarm(aid: str) -> str:
    """The agent's id for an alarm is normally the server's too - unless the PC app restarted while the alarm
    was on, in which case the server kept the original id and noted the agent's as an alias."""
    if db.execute("SELECT 1 FROM alarms WHERE id=?", (aid,)).fetchone():
        return aid
    r = db.execute("SELECT alarm_id FROM alarm_alias WHERE agent_id=?", (aid,)).fetchone()
    return r["alarm_id"] if r else aid


def load_clip_ids():
    try:
        clip_ids.clear()
        clip_ids.update(r["alarm_id"] for r in db.execute("SELECT alarm_id FROM clips"))
    except sqlite3.Error:
        pass


def prune_clips():
    """Keep the newest KEEP_CLIPS and nothing older than KEEP_CLIP_DAYS."""
    old = [r["alarm_id"] for r in db.execute(
        "SELECT alarm_id FROM clips WHERE at < ? OR alarm_id NOT IN "
        "(SELECT alarm_id FROM clips ORDER BY at DESC LIMIT ?)",
        (time.time() - KEEP_CLIP_DAYS * 86400, KEEP_CLIPS))]
    for aid in old:
        db.execute("DELETE FROM clips WHERE alarm_id=?", (aid,))
        clip_ids.discard(aid)
    if old:
        log.info("Tidied away %d old camera clip(s)", len(old))
    return old          # the caller unlinks the files, outside the lock


def clip_list(limit: int) -> list[dict]:
    return [dict(r, has_file=clip_path(r["alarm_id"]).exists())
            for r in db.execute("SELECT * FROM clips ORDER BY at DESC LIMIT ?", (limit,))]


def doc_path(key: str) -> Path:
    """Where this program's job PDF is kept (the key is checked by valid_program_key, so it's safe as a name)."""
    return DOCS / f"{key}.pdf"


def setup_info(key: str | None) -> dict:
    """The setup sheet bits: sub stick out and whether there's a job PDF to look at."""
    if not key:
        return {}
    r = db.execute("SELECT stick_out, doc_name, doc_at, doc_size FROM programs WHERE program=?", (key,)).fetchone()
    if not r:
        return {"stick_out": None, "has_doc": False}
    has = bool(r["doc_name"]) and doc_path(key).exists()
    return {"stick_out": r["stick_out"], "has_doc": has,
            "doc_name": r["doc_name"] if has else None,
            "doc_at": r["doc_at"] if has else None, "doc_size": r["doc_size"] if has else None}


def program_info() -> dict:
    """The running program plus its key, your name for it, the combined label and its average cycle time."""
    prog = dict(latest.get("program") or {})
    key = program_key(prog)
    if key:
        avg, n = learnt_cycle(key, bool(latest.get("demo")))
        prog.update(key=key, custom_name=program_name(key), label=program_label(key), avg_cycle_s=avg, avg_cycle_parts=n,
                    **setup_info(key))
    return prog


# ---- average cycle time per program -------------------------------------------------------------------
CYCLE_AVG_OVER = 50       # parts
CYCLE_KEEP = 500          # stored per program


def record_cycle(snap: dict, now: float, demo: bool):
    """A part was just made: store its part-to-part time against the program (only a clean count-up by 1-5)."""
    key = program_key(snap.get("program"))
    cyc = snap.get("last_cycle_s")
    kind, n = _bar_count(snap)
    pkind, pn = _bar_count(latest)
    if (not key or not cyc or not 0.5 <= cyc <= 7200 or snap.get("bar_change") or kind is None or kind != pkind
            or pn is None or not 1 <= n - pn <= 5 or program_key(latest.get("program")) != key):
        return
    db.execute("INSERT INTO cycles(program,cycle_s,at,demo) VALUES(?,?,?,?)", (key, float(cyc), now, int(demo)))
    db.execute("DELETE FROM cycles WHERE program=? AND id NOT IN (SELECT id FROM cycles WHERE program=? "
               "ORDER BY at DESC LIMIT ?)", (key, key, CYCLE_KEEP))


def learnt_cycle(key: str | None, demo: bool) -> tuple[float | None, int]:
    """Average part-to-part time of the program's recent parts, leaving out odd ones (a stop, a slow first part)."""
    if not key:
        return None, 0
    vals = [r["cycle_s"] for r in db.execute(
        "SELECT cycle_s FROM cycles WHERE program=? AND COALESCE(demo,0)=? ORDER BY at DESC LIMIT ?",
        (key, int(demo), CYCLE_AVG_OVER))]
    if not vals:
        return None, 0
    med = sorted(vals)[len(vals) // 2]
    good = [v for v in vals if 0.75 * med <= v <= 1.25 * med] or vals
    return round(sum(good) / len(good), 1), len(good)


def _bar_count(snap: dict):
    """The counter a bar is measured with: the machine's total parts counter if there is one (operators reset the
    part counter, rarely the total), otherwise the part counter."""
    total = snap.get("parts_total")
    if isinstance(total, (int, float)) and not isinstance(total, bool) and total > 0:
        return "total", int(total)
    parts = snap.get("parts")
    return ("parts", int(parts)) if isinstance(parts, (int, float)) and not isinstance(parts, bool) else (None, None)


def track_bar_program(snap: dict):
    """Follows which program is running through the current bar, so a bar that spans a program change keeps
    the part count of each."""
    key = program_key(snap.get("program"))
    kind, n = _bar_count(snap)
    if not key or n is None:
        return
    segs = meta.get("bar_segs") or []
    if not segs or segs[-1].get("program") != key or segs[-1].get("kind") != kind:
        # the segments tile the whole bar: the new one starts where the last one ended, so any parts counted
        # in the same poll as the program change go to the new program
        start = segs[-1]["n1"] if segs and segs[-1].get("kind") == kind and segs[-1]["n1"] <= n else n
        segs.append({"program": key, "kind": kind, "n0": start, "n1": n})
        meta["bar_segs"] = segs[-8:]
    elif n >= segs[-1]["n1"]:
        segs[-1]["n1"] = n
    else:                                        # counter reset part way through: start this segment again
        segs[-1]["n0"] = segs[-1]["n1"] = n


def record_bar(snap: dict, now: float, demo: bool):
    """Called when a bar change starts: closes the bar that just finished and starts counting the next one."""
    key = program_key(snap.get("program"))
    kind, n = _bar_count(snap)
    prev = meta.get("bar_start")
    segs = meta.get("bar_segs") or []
    meta["bar_start"] = {"program": key, "kind": kind, "n": n, "at": now, "demo": bool(demo)} if key and kind else None
    meta["bar_segs"] = [{"program": key, "kind": kind, "n0": n, "n1": n}] if key and n is not None else []
    if not prev or not key or kind is None:
        return
    if (prev.get("kind"), bool(prev.get("demo"))) != (kind, bool(demo)):
        return                                   # the counter changed part way through the bar
    made = n - prev["n"]
    if not 1 <= made <= 20000 or now - prev["at"] > 7 * 86400:
        return                                   # counter reset, or a gap too long to trust
    by_program = [{"program": s["program"], "parts": s["n1"] - s["n0"]} for s in segs
                  if s.get("kind") == kind and s["n1"] > s["n0"]]
    if not by_program:
        by_program = [{"program": prev.get("program") or key, "parts": made}]
    partial = len(by_program) > 1                # more than one program on this bar: not a clean parts-per-bar
    main = max(by_program, key=lambda x: x["parts"])["program"]
    db.execute("INSERT INTO bars(program,parts,started_at,ended_at,demo,programs,partial) VALUES(?,?,?,?,?,?,?)",
               (main, made, prev["at"], now, int(demo), json.dumps(by_program), int(partial)))
    roll(day_key(now), main, demo, bars=1)      # for the operations report
    log.info("Bar finished: %d parts (%s)%s", made,
             ", ".join(f"{p['program']} {p['parts']}" for p in by_program), " - partial, left out of the average" if partial else "")


def learnt_parts_per_bar(key: str, demo: bool) -> tuple[float | None, int]:
    vals = [r["parts"] for r in db.execute(
        "SELECT parts FROM bars WHERE program=? AND COALESCE(demo,0)=? AND COALESCE(partial,0)=0 "
        "ORDER BY ended_at DESC LIMIT ?", (key, int(demo), BAR_HISTORY * 2)).fetchall()]
    if not vals:
        return None, 0
    med = sorted(vals)[len(vals) // 2]
    # leave out odd ones (a missed bar change counts double, a short remnant bar counts low)
    good = [v for v in vals if 0.6 * med <= v <= 1.4 * med][:BAR_HISTORY] or vals[:BAR_HISTORY]
    return round(sum(good) / len(good), 1), len(good)


def parts_per_bar(key: str | None, demo: bool) -> dict | None:
    """Parts per bar for a program: the figure typed in under Program info if there is one, else the learnt one."""
    if not key:
        return None
    learnt, n = learnt_parts_per_bar(key, demo)
    r = db.execute("SELECT ppb_manual FROM programs WHERE program=?", (key,)).fetchone()
    manual = r["ppb_manual"] if r and r["ppb_manual"] else None
    if manual is None and learnt is None:
        return None
    return {"program": key, "label": program_label(key), "avg": manual or learnt, "bars": n,
            "source": "manual" if manual else "learnt", "learnt_avg": learnt}


def program_row(key: str, current: str | None = None) -> dict:
    """Everything stored about one program, for the Program info screens."""
    r = db.execute("SELECT * FROM programs WHERE program=?", (key,)).fetchone()
    learnt, n = learnt_parts_per_bar(key, False)
    agg = db.execute("SELECT COUNT(*) n, MAX(ended_at) last, MIN(started_at) first FROM bars "
                     "WHERE program=? AND COALESCE(demo,0)=0", (key,)).fetchone()
    cyc, cyc_n = learnt_cycle(key, False)
    return {"program": key, "name": (r["name"] or None) if r else None, "label": program_label(key),
            "avg_cycle_s": cyc, "avg_cycle_parts": cyc_n,
            "cycles_recorded": db.execute("SELECT COUNT(*) n FROM cycles WHERE program=? AND COALESCE(demo,0)=0",
                                          (key,)).fetchone()["n"],
            "notes": (r["notes"] or None) if r else None, "ppb_manual": r["ppb_manual"] if r else None,
            "ppb_learnt": learnt, "ppb_learnt_bars": n, "bars_recorded": agg["n"] or 0,
            "first_bar_at": agg["first"], "last_bar_at": agg["last"], "loaded": key == current,
            **setup_info(key)}


def valid_program_key(raw) -> str | None:
    key = str(raw or "").strip().upper()
    if not key.strip("."):        # "." / ".." etc: never a program, and no use as a file name
        return None
    if re.fullmatch(r"\d{1,5}", key):
        key = f"O{int(key):04d}"
    return key if re.fullmatch(r"O\d{4,5}|[A-Z0-9_.\-]{1,32}", key) else None


def bar_forecast(now: float) -> dict | None:
    """Parts per bar for the loaded program, and how many more bars the job needs."""
    key = program_key(latest.get("program"))
    pb = parts_per_bar(key, bool(latest.get("demo")))
    if not pb:
        return {"program": key, "label": program_label(key), "avg": None} if key else None
    avg = pb["avg"]
    start = meta.get("bar_start") or {}
    kind, n = _bar_count(latest)
    into = None
    if latest.get("bar_change"):
        into = 0
    elif start.get("program") == key and start.get("kind") == kind and n is not None and n >= start.get("n", n + 1):
        into = n - start["n"]
    pb["into_bar"] = into
    parts, req = latest.get("parts"), latest.get("parts_required")
    left = (req - parts) if (req and parts is not None and req > parts) else None
    pb["parts_left"] = left
    on_bar = max(0, round(avg - into)) if (into is not None and avg > 0) else None
    if on_bar is not None:
        pb["left_on_bar"] = on_bar
    if left and avg > 0:
        import math
        if on_bar is not None:
            pb["more_bars"] = 0 if left <= on_bar else math.ceil((left - on_bar) / avg)
        else:
            pb["bars_total"] = math.ceil(left / avg)   # don't know how far into the current bar it is
    # when this bar runs out, at the cycle time this program is running at
    cyc = latest.get("last_cycle_s") or learnt_cycle(key, bool(latest.get("demo")))[0]
    if on_bar is not None and cyc:
        wait = on_bar * cyc
        if left:                                       # the job finishes first: no more bar changes needed
            wait = None if left <= on_bar else wait
        if wait is not None and wait < 30 * 86400:
            pb["next_bar_in_s"] = round(wait)
            pb["next_bar_at"] = round(now + wait)
    return pb


# --------------------------------------------------------------------------- operations report
# A day-by-day rollup, kept up to date as snapshots come in: how many parts each program made, how many bars
# it ran, and how the machine's time was spent. The report screens add these up by day, month or year.
MAX_GAP = 120         # a longer gap than this (server down, PC off) isn't counted as machine time
BUCKETS = {"running": "run_s", "alarm": "alarm_s", "off": "off_s"}


def day_key(t: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(t))


def _split_by_day(start: float, end: float) -> list[tuple[str, float]]:
    """[(day, seconds)] for a span, split at local midnight so nothing lands on the wrong day."""
    out = []
    while start < end:
        lt = time.localtime(start)
        midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)) + 86400
        stop = min(end, midnight)
        out.append((day_key(start), stop - start))
        start = stop
    return out


def roll(day: str, program: str, demo: bool, **inc):
    """Add to one day's figures for a program ("" when no program was loaded)."""
    if not inc:
        return
    db.execute("INSERT INTO daily(day,program,demo) VALUES(?,?,?) ON CONFLICT(day,program,demo) DO NOTHING",
               (day, program or "", int(demo)))
    sets = ",".join(f"{k}={k}+?" for k in inc)
    db.execute(f"UPDATE daily SET {sets} WHERE day=? AND program=? AND demo=?",
               (*inc.values(), day, program or "", int(demo)))


def track_production(snap: dict, now: float, demo: bool):
    """Called on every snapshot, before it becomes the current one."""
    prev_at, prev = meta.get("prod_at"), dict(latest)
    if prev_at and now < prev_at:
        return                                   # an out-of-order replay: its parts are already on record
    meta["prod_at"] = now
    if prev and prev_at and 0 < now - prev_at <= MAX_GAP:
        # the time since the last snapshot goes to whatever the machine was doing then
        bucket = "off_s" if not prev.get("machine_connected") else BUCKETS.get(prev.get("state") or "", "standby_s")
        key = "" if bucket == "off_s" else (program_key(prev.get("program")) or "")
        for day, secs in _split_by_day(prev_at, now):      # a gap over midnight counts on both days
            roll(day, key, bool(prev.get("demo")), **{bucket: round(secs, 2)})
    if not snap.get("machine_connected"):
        return
    kind, n = _bar_count(snap)
    pkind, pn = _bar_count(prev)
    if not kind or kind != pkind or n is None or pn is None or not 1 <= n - pn <= 20:
        return                                   # counter reset, first snapshot, or too big a jump to trust
    made = n - pn
    inc: dict[str, Any] = {"parts": made}
    cyc = snap.get("last_cycle_s")
    if made == 1 and cyc and 0.5 <= cyc <= 7200 and not snap.get("bar_change"):
        inc["cycles"], inc["cycle_s"] = 1, float(cyc)
    key = program_key(snap.get("program")) or ""
    roll(day_key(now), key, demo, **inc)
    db.execute("UPDATE daily SET first_at=COALESCE(MIN(first_at,?),?), last_at=MAX(COALESCE(last_at,?),?) "
               "WHERE day=? AND program=? AND demo=?", (now, now, now, now, day_key(now), key, int(demo)))


def backfill_daily():
    """Fills the rollup in from the bars and cycles already on record, so the report isn't empty on day one.
    Only days before today, so it can't double-count what's being recorded now."""
    if kv_get("daily_backfilled"):
        return
    if db.execute("SELECT 1 FROM daily LIMIT 1").fetchone():
        kv_set("daily_backfilled", time.time())    # already recording for itself: don't count the same parts twice
        return
    today = day_key(time.time())
    n = 0
    for r in db.execute("SELECT program, parts, ended_at, demo FROM bars ORDER BY ended_at"):
        day = day_key(r["ended_at"])
        if day >= today:
            continue
        roll(day, r["program"] or "", bool(r["demo"]), parts=int(r["parts"] or 0), bars=1)
        n += 1
    for r in db.execute("SELECT program, cycle_s, at, demo FROM cycles ORDER BY at"):
        day = day_key(r["at"])
        if day >= today:
            continue
        roll(day, r["program"] or "", bool(r["demo"]), cycles=1, cycle_s=float(r["cycle_s"] or 0),
             run_s=float(r["cycle_s"] or 0))
    kv_set("daily_backfilled", time.time())
    if n:
        log.info("Report: filled in %d bars of history", n)


def report_date(raw: str, end: bool) -> str | None:
    """"2026", "2026-03" or "2026-03-14" -> the first (or last) day it covers."""
    raw = raw.strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
        return raw
    if re.match(r"^\d{4}-\d{2}$", raw):
        return f"{raw}-31" if end else f"{raw}-01"
    if re.match(r"^\d{4}$", raw):
        return f"{raw}-12-31" if end else f"{raw}-01-01"
    return None


def _bucket_key(day: str, period: str) -> str:
    return {"day": day, "month": day[:7], "year": day[:4]}[period]


def _bucket_label(key: str, period: str) -> str:
    try:
        if period == "day":
            return time.strftime("%a %d %b %Y", time.strptime(key, "%Y-%m-%d"))
        if period == "month":
            return time.strftime("%B %Y", time.strptime(key, "%Y-%m"))
    except ValueError:
        pass
    return key


def report(period: str, limit: int, since: str | None = None, until: str | None = None) -> dict:
    """Parts, bars and machine time by day, month or year - with the breakdown by program."""
    if period not in ("day", "month", "year"):
        return {"error": "period must be day, month or year"}
    sql = "SELECT * FROM daily WHERE COALESCE(demo,0)=0"
    args: list = []
    if since:
        sql, args = sql + " AND day>=?", args + [since]
    if until:
        sql, args = sql + " AND day<=?", args + [until]
    buckets: dict[str, dict] = {}
    for r in db.execute(sql + " ORDER BY day", args):
        b = buckets.setdefault(_bucket_key(r["day"], period),
                               {"key": _bucket_key(r["day"], period), "parts": 0, "bars": 0, "run_s": 0.0,
                                "standby_s": 0.0, "alarm_s": 0.0, "off_s": 0.0, "cycles": 0, "cycle_s": 0.0,
                                "days": set(), "first_at": None, "last_at": None, "_p": {}})
        p = b["_p"].setdefault(r["program"] or "",
                               {"program": r["program"] or None, "parts": 0, "bars": 0, "run_s": 0.0,
                                "cycles": 0, "cycle_s": 0.0})
        for k in ("parts", "bars", "run_s", "standby_s", "alarm_s", "off_s", "cycles", "cycle_s"):
            b[k] += r[k] or 0
        for k in ("parts", "bars", "run_s", "cycles", "cycle_s"):
            p[k] += r[k] or 0
        if (r["parts"] or 0) or (r["run_s"] or 0) > 60:
            b["days"].add(r["day"])
        for k, pick in (("first_at", min), ("last_at", max)):
            if r[k]:
                b[k] = r[k] if b[k] is None else pick(b[k], r[k])
    every = sorted(buckets.values(), key=lambda x: x["key"], reverse=True)
    grand = {k: round(sum(b[k] for b in every), 1)
             for k in ("parts", "bars", "run_s", "standby_s", "alarm_s", "off_s")}
    out = []
    for b in every[:limit]:
        progs = sorted(b.pop("_p").values(), key=lambda p: -p["parts"])
        for p in progs:
            p["label"] = program_label(p["program"]) or "No program"
            p["avg_cycle_s"] = round(p["cycle_s"] / p["cycles"], 2) if p["cycles"] else None
            p.pop("cycle_s", None)
        b["programs"] = [p for p in progs if p["parts"] or p["bars"] or p["run_s"] > 60]
        b["days"] = len(b["days"])
        b["label"] = _bucket_label(b["key"], period)
        b["avg_cycle_s"] = round(b["cycle_s"] / b["cycles"], 2) if b["cycles"] else None
        b["parts_per_hour"] = round(b["parts"] / (b["run_s"] / 3600), 1) if b["run_s"] > 600 else None
        for k in ("run_s", "standby_s", "alarm_s", "off_s", "cycle_s"):
            b[k] = round(b[k], 1)
        out.append(b)
    totals = dict(grand)
    totals["parts"], totals["bars"] = int(totals["parts"]), int(totals["bars"])
    by_program: dict[str, dict] = {}
    for b in out:
        for p in b["programs"]:
            t = by_program.setdefault(p["label"], {"program": p["program"], "label": p["label"], "parts": 0,
                                                   "bars": 0, "run_s": 0.0})
            for k in ("parts", "bars", "run_s"):
                t[k] += p[k]
    totals["programs"] = sorted(by_program.values(), key=lambda p: -p["parts"])
    first = db.execute("SELECT MIN(day) d FROM daily WHERE COALESCE(demo,0)=0").fetchone()
    return {"period": period, "buckets": out, "totals": totals, "from": first["d"] if first else None}


def bar_day(date_str: str | None, now: float) -> dict:
    """Every bar change of one day: when the bar was changed, how long it took, how many parts the bar that
    finished made, and which program(s) made them."""
    if date_str:
        try:
            t = time.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            return {"error": "date must look like 2026-09-12"}
        start = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))
    else:
        lt = time.localtime(now)
        start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    end = start + 86400
    rows = db.execute(
        "SELECT bc.started_at, bc.ended_at, bc.duration_s, bc.demo, b.id bar_id, b.parts, b.programs, b.partial, "
        "b.started_at bar_from FROM bar_changes bc LEFT JOIN bars b ON b.ended_at = bc.started_at "
        "WHERE bc.started_at >= ? AND bc.started_at < ? ORDER BY bc.started_at DESC", (start, end)).fetchall()
    out = []
    for r in rows:
        try:
            progs = json.loads(r["programs"]) if r["programs"] else []
        except ValueError:
            progs = []
        if not progs and r["parts"] is not None:
            progs = [{"program": None, "parts": r["parts"]}]
        out.append({"at": r["started_at"], "ended_at": r["ended_at"], "duration_s": r["duration_s"],
                    "demo": bool(r["demo"]), "bar_id": r["bar_id"], "parts": r["parts"], "bar_from": r["bar_from"],
                    "partial": bool(r["partial"]),
                    "programs": [{**p, "label": program_label(p.get("program"))} for p in progs]})
    made = sum(b["parts"] or 0 for b in out)
    return {"date": time.strftime("%Y-%m-%d", time.localtime(start)), "day_start": start,
            "changes": len(out), "parts": made, "bars": out}


def message_rows(active_only: bool, limit: int = 100) -> list[dict]:
    sql = "SELECT * FROM op_messages" + (" WHERE cleared_at IS NULL" if active_only else "") + \
          " ORDER BY started_at DESC LIMIT ?"
    return [{"id": r["id"], "number": r["number"], "text": r["text"], "started_at": r["started_at"],
             "cleared_at": r["cleared_at"], "active": r["cleared_at"] is None, "demo": bool(r["demo"])}
            for r in db.execute(sql, (limit,)).fetchall()]


def ingest(snap: dict, replay: bool = False) -> dict:
    """replay=True: a snapshot the PC kept while this server was unreachable. It is processed with its own
    timestamp (so cycle times, bars and the state log land where they belong) and sends no notifications."""
    now = float(snap.get("sent_at") or time.time()) if replay else time.time()
    with _lock:
        sent_at = float(snap.get("sent_at") or now)
        offset = 0.0 if replay else (now - sent_at if abs(now - sent_at) > 3 else 0.0)   # PC clock drift
        connected = bool(snap.get("machine_connected"))
        demo = bool(snap.get("demo"))
        if connected and not demo and not meta.get("first_real_at"):
            meta["first_real_at"] = now
        db.execute("BEGIN")
        try:
            ack = upsert_alarms(snap.get("alarms") or [], offset, now, connected, demo)
            if connected and "messages" in snap:
                upsert_messages(snap.get("messages") or [], now, demo)
            elif not connected:
                db.execute("UPDATE op_messages SET cleared_at=? WHERE cleared_at IS NULL", (now,))
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
        if snap.get("last_cycle_s") is None and program_key(snap.get("program")) == program_key(latest.get("program")):
            snap["last_cycle_s"] = latest.get("last_cycle_s")   # keep last known across agent restarts
            # (a different program starts without one: the finish time uses that program's stored average)
        snap.pop("alarms", None)
        snap["received_at"] = now
        timer = snap.get("cycle_timer_s")
        snap["cycle_started_at"] = (now - timer) if (timer and snap.get("state") == "running") else None
        if snap.get("state") != latest.get("state"):
            meta["raw_since"] = now
        if connected:
            track_bar_program(snap)     # keep the per-program part counts up to date before any bar change
        snap["bar_change"] = bool(snap.get("bar_change")) and connected and snap.get("state") == "running"
        if snap["bar_change"] != bool(latest.get("bar_change")):
            if snap["bar_change"]:
                meta["bar_change_since"] = now
                record_bar(snap, now, demo)
                log.info("Bar change started (%s)", snap.get("bar_change_how") or "")
            else:
                since = meta.get("bar_change_since")
                log.info("Bar change finished%s", f" after {now - since:.0f} s" if since else "")
                if since and 1 <= now - since < 3600:
                    db.execute("INSERT INTO bar_changes(started_at,ended_at,duration_s,parts,demo) VALUES(?,?,?,?,?)",
                               (since, now, round(now - since, 1), snap.get("parts"), int(demo)))
        # job complete: the count reaches the required count (counting up - not a reset or a new target)
        req, parts, prev_parts = snap.get("parts_required"), snap.get("parts"), latest.get("parts")
        if (req and parts is not None and prev_parts is not None and parts >= req > prev_parts
                and parts - prev_parts <= 5 and latest.get("parts_required") == req):
            meta["job_complete"] = {"at": now, "parts": parts, "required": req}
            log.info("Job complete: %s/%s", parts, req)
        if connected:
            record_cycle(snap, now, demo)
        track_production(snap, now, demo)
        # when the count first sat at exactly the required count (for "overrun")
        if req and parts is not None and parts == req:
            if meta.get("at_req") != [parts, req]:
                meta["at_req"], meta["at_req_since"] = [parts, req], now
        else:
            meta.pop("at_req", None)
        if snap.get("state") == "running":
            meta["last_running"] = {"detail": snap.get("state_detail"), "cycle_started_at": snap.get("cycle_started_at"),
                                    "cycle_timer_s": snap.get("cycle_timer_s")}
        # a replayed snapshot is history: it fills in the bars, cycles and alarms of that period, but it must
        # not become "the machine right now" if something newer has already been through
        stale = replay and now < float(meta.get("last_seen") or 0)
        if not stale:
            latest.clear()
            latest.update(snap)
            meta["last_seen"] = now
            if connected:
                meta["last_connected_at"] = now
            track_state(now)
        kv_set("latest", latest)
        kv_set("meta", meta)
    if not replay:
        push.poke()
    return {"ok": True, "ack": ack, "camera_live": camera.watched}


def status() -> dict:
    now = time.time()
    with _lock:
        track_state(now)
        state, detail, agent_online = effective_state(now)
        active = [alarm_row(r, now) for r in db.execute(
            "SELECT * FROM alarms WHERE cleared_at IS NULL ORDER BY started_at DESC").fetchall()]
        lt = time.localtime(now)
        day_start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        today = db.execute("SELECT COUNT(*) AS n FROM alarms WHERE started_at>=? AND COALESCE(demo,0)=0",
                           (day_start,)).fetchone()["n"]
        parts, req, cyc = latest.get("parts"), latest.get("parts_required"), latest.get("last_cycle_s")
        prog_info = program_info()
        cyc = cyc or prog_info.get("avg_cycle_s")   # just loaded: the program's average from last time
        eta = (req - parts) * cyc if (parts is not None and req and cyc and req > parts) else None
        running = state == "running"
        bar_change = running and bool(latest.get("bar_change")) and latest.get("state") == "running"
        # during the short between-cycles pause the agent reports standby: keep the last cycle clock
        cyc_src = latest if latest.get("state") == "running" else (meta.get("last_running") or {})
        return {
            "server_time": now,
            "machine_name": latest.get("machine_name") or "Hanwha XE35",
            "state": state,
            "state_detail": detail,
            "state_since": meta.get("state_since"),
            "running_ended_at": meta.get("running_ended_at"),
            "app_seen": app_seen[0],
            "agent_online": agent_online,
            "agent_last_seen": meta.get("last_seen"),
            "machine_connected": bool(latest.get("machine_connected")) and agent_online,
            "demo": bool(latest.get("demo")),
            "parts": parts,
            "parts_required": req,
            "parts_total": latest.get("parts_total"),
            "last_cycle_s": cyc,
            "cycle_timer_s": cyc_src.get("cycle_timer_s") if running else None,
            "cycle_started_at": cyc_src.get("cycle_started_at") if running else None,
            "eta_s": round(eta) if eta else None,
            # when the job should finish (machine running, target set) - "done at 17:40"
            "finish_at": round(now + eta) if (eta and state in ("running",)) else None,
            "job_complete": meta.get("job_complete"),
            "bar_changes": {**bar_change_stats(now), "per_bar": bar_forecast(now)},
            # still running after reaching the required count (the work counter isn't stopping it)
            "over_producing": bool(running and not bar_change and req and parts is not None
                                   and (parts > req or (parts == req and latest.get("state") == "running"
                                                        and now - (meta.get("at_req_since") or now) > 8))),
            "agent_version": latest.get("agent_version"),
            "agent_update": update_info().get("version"),
            "program": prog_info,
            "paths": latest.get("paths") or [],
            "active_alarms": active if state != "off" else [],
            "messages": message_rows(True, 10) if state != "off" else [],
            "alarms_today": today,
            # A bar change is reported as state "running" plus this flag (keeps older app builds working).
            "bar_change": bar_change,
            "bar_change_since": meta.get("bar_change_since") if bar_change else None,
            # work counter "stop at required count" switch: True/False, None = not set up on the monitor PC
            "work_counter": latest.get("work_counter") if state != "off" else None,
            # backup battery and anything else on a schedule: {"item","name","state","due_at","days_left",…}
            "maintenance": maint_all(now),
            "camera": dict(camera.info(), **_asdict(latest.get("camera_features"))),
            "controls": dict(_asdict(latest.get("controls")), connected=commands.agent_listening),
        }


def alarm_history(limit: int, before: float | None, active_only: bool) -> dict:
    now = time.time()
    sql, args = "SELECT * FROM alarms WHERE 1=1", []
    if before:
        sql += " AND started_at < ?"
        args.append(before)
    if active_only:
        sql += " AND cleared_at IS NULL"
    sql += " ORDER BY started_at DESC LIMIT ?"
    args.append(limit)
    with _lock:
        rows = [alarm_row(r, now) for r in db.execute(sql, args).fetchall()]
        where, wargs = _demo_where()
        demo_ids = {r["id"] for r in db.execute(f"SELECT id FROM alarms WHERE {where}", wargs).fetchall()}
    for r in rows:
        r["demo"] = r["id"] in demo_ids
    return {"alarms": rows, "next_before": rows[-1]["started_at"] if len(rows) == limit else None,
            "demo_count": len(demo_ids)}


def state_history(hours: float) -> dict:
    with _lock:
        rows = db.execute("SELECT ts,state,detail FROM state_log WHERE ts>=? ORDER BY ts",
                          (time.time() - hours * 3600,)).fetchall()
    return {"states": [dict(r) for r in rows]}


def _kv_get_locked(k, default=None):
    with _lock:
        return kv_get(k, default)


def _kv_set_locked(k, v):
    with _lock:
        kv_set(k, v)


push = PushService(db, _lock, APNs(), lambda: status(), _kv_get_locked, _kv_set_locked)
camera = Camera(Path(DB_PATH).parent)
video = LiveVideo()
commands = Commands()
auth = Auth(db, _lock, reset=os.environ.get("RESET_LOGIN", "").strip().lower() in ("1", "true", "yes"))


def drop_orphan_clips():
    """Clips of alarms that have just been deleted: forget the rows and the video files."""
    gone = [r["alarm_id"] for r in db.execute("SELECT alarm_id FROM clips WHERE alarm_id NOT IN "
                                              "(SELECT id FROM alarms)")]
    for aid in gone:
        db.execute("DELETE FROM clips WHERE alarm_id=?", (aid,))
        clip_ids.discard(aid)
    return gone


def clear_demo() -> dict:
    """Delete every alarm that came from the agent's demo mode (active ones too)."""
    with _lock:
        where, args = _demo_where()
        n = db.execute(f"DELETE FROM alarms WHERE {where}", args).rowcount
        db.execute("DELETE FROM alarm_alias WHERE alarm_id NOT IN (SELECT id FROM alarms)")
        gone = drop_orphan_clips()
    for aid in gone:
        clip_path(aid).unlink(missing_ok=True)
    log.info("Cleared %d demo alarm(s)", n)
    return {"ok": True, "deleted": n}


def clear_history() -> dict:
    with _lock:
        db.execute("DELETE FROM alarms WHERE cleared_at IS NOT NULL")
        db.execute("DELETE FROM alarm_alias WHERE alarm_id NOT IN (SELECT id FROM alarms)")
        gone = drop_orphan_clips()
    for aid in gone:
        clip_path(aid).unlink(missing_ok=True)
    return {"ok": True}


# --------------------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = f"HanwhaMonitor/{VERSION}"
    protocol_version = "HTTP/1.1"

    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError, TimeoutError):
            pass   # e.g. the agent's kept-open video upload connection closing when ffmpeg stops

    def log_message(self, fmt, *args):   # quiet: the agent posts every couple of seconds
        pass

    def _send(self, code: int, body: bytes, ctype: str, headers: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Frame-Options", "DENY")
        for k, v in (headers or {}).items():
            for item in (v if isinstance(v, list) else [v]):   # several Set-Cookie headers
                self.send_header(k, item)
        try:
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True   # e.g. ffmpeg hangs up after a PUT without reading the reply

    def _json(self, obj, code: int = 200, headers: dict | None = None):
        self._send(code, json.dumps(obj).encode(), "application/json", headers)

    def _redirect(self, where: str):
        self._send(302, b"", "text/plain", {"Location": where})

    def _cookie(self, name: str) -> str | None:
        try:
            c = SimpleCookie(self.headers.get("Cookie") or "")
            return c[name].value if name in c else None
        except Exception:
            return None

    def _token(self) -> str | None:
        return self._cookie(COOKIE)

    def _device_trusted(self) -> bool:
        return bool(API_KEY) and auth.device_ok(self._cookie(DEVICE_COOKIE), auth.key_fingerprint(API_KEY))

    def _session(self) -> dict | None:
        return auth.session(self._token())

    def _client_ip(self) -> str:
        return self.client_address[0] if self.client_address else "?"

    def _raw_body(self, limit: int) -> bytes:
        """Request body, including chunked transfer encoding (ffmpeg's HTTP PUT uses it)."""
        self._drained = True
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            out = bytearray()
            while True:
                size = int(self.rfile.readline(1024).split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline(1024) not in (b"\r\n", b"\n", b""):
                        pass
                    return bytes(out)
                if size > limit - len(out):
                    raise ValueError("body too large")
                out += self.rfile.read(size)
                self.rfile.readline(8)
        length = int(self.headers.get("Content-Length") or 0)
        if length > limit:
            raise ValueError("body too large")
        return self.rfile.read(length) if length > 0 else b""

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length < 100_000:
            return {}          # left unread on purpose: _drain closes the connection rather than desync it
        self._drained = True
        body = json.loads(self.rfile.read(length))
        return body if isinstance(body, dict) else {}

    @staticmethod
    def _key_ok(given: str) -> bool:
        return bool(API_KEY) and hmac.compare_digest((given or "").encode(), API_KEY.encode())

    def _authorised(self, qs) -> bool:
        s = self._session()
        if s and not s["must_change"]:
            return True
        if not API_KEY:
            return True
        return self._key_ok(self.headers.get("X-API-Key") or (qs.get("key") or [""])[0])

    def _auth_route(self, method: str, path: str) -> bool:
        """Login page + /auth/* endpoints. Returns True if handled."""
        if method == "GET" and path == "/login":
            self._send(200, (STATIC / "login.html").read_bytes(), "text/html; charset=utf-8")
            return True
        if method == "GET" and path == "/logo.png":
            self._send(200, (STATIC / "logo.png").read_bytes(), "image/png", {"Cache-Control": "max-age=86400"})
            return True
        if not path.startswith("/auth/"):
            return False
        s = self._session()
        if method == "GET" and path == "/auth/info":
            trusted = self._device_trusted()
            self._json({"api_key_set": bool(API_KEY),                        # the data API is protected
                        "api_key_needed": bool(API_KEY) and not trusted,     # this browser must type it at login
                        "device_trusted": trusted, "logged_in": bool(s),
                        "must_change": bool(s and s["must_change"]), "username": s["username"] if s else None})
        elif method == "POST" and path == "/auth/login":
            b = self._body()
            typed_key = str(b.get("api_key") or "")
            trusted = self._device_trusted()
            key_ok = (trusted or self._key_ok(typed_key)) if API_KEY else True
            token, err = auth.login(str(b.get("username") or ""), str(b.get("password") or ""), self._client_ip(), key_ok)
            if not token:
                self._json({"ok": False, "error": err}, 401)
            else:
                cookies = [f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_DAYS * 86400}"]
                if API_KEY and not trusted and b.get("remember_key", True) and self._key_ok(typed_key):
                    dev = auth.trust_device(auth.key_fingerprint(API_KEY), self._client_ip())
                    cookies.append(f"{DEVICE_COOKIE}={dev}; Path=/; HttpOnly; SameSite=Lax; Max-Age={DEVICE_DAYS * 86400}")
                self._json({"ok": True, "must_change": auth.session(token)["must_change"]}, 200, {"Set-Cookie": cookies})
        elif method == "POST" and path == "/auth/change":
            if not s:
                self._json({"ok": False, "error": "Please log in again."}, 401)
            else:
                b = self._body()
                err = auth.change(self._token(), str(b.get("current_password") or ""), str(b.get("username") or ""),
                                  str(b.get("password") or ""))
                self._json({"ok": not err, "error": err}, 200 if not err else 400)
        elif method == "POST" and path == "/auth/logout":
            auth.logout(self._token())
            cookies = [f"{COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"]
            if self._body().get("forget_device"):
                auth.forget_device(self._cookie(DEVICE_COOKIE))
                cookies.append(f"{DEVICE_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")
            self._json({"ok": True}, 200, {"Set-Cookie": cookies})
        else:
            self._json({"detail": "not found"}, 404)
        return True

    def _route(self, method: str):
        self._drained = False   # handler objects are reused for every request on a kept-open connection
        url = urlparse(self.path)
        qs = parse_qs(url.query)
        path = url.path.rstrip("/") or "/"
        try:
            if method == "GET" and path in ("/", "/index.html", "/programs", "/report", "/wall", "/settings"):
                s = self._session()
                if not s or s["must_change"]:
                    return self._redirect("login")
                page = {"/programs": "programs.html", "/report": "report.html", "/wall": "wall.html",
                        "/settings": "settings.html"}.get(path, "index.html")
                f = STATIC / page
                if not f.is_file():
                    return self._json({"detail": f"{page} is missing from this build"}, 404)
                return self._send(200, f.read_bytes(), "text/html; charset=utf-8")
            if self._auth_route(method, path):
                return
            if method == "GET" and path == "/hls.min.js":   # video player library (bundled into the image)
                f = STATIC / "hls.min.js"
                if not f.is_file():
                    return self._json({"detail": "not bundled"}, 404)
                return self._send(200, f.read_bytes(), "text/javascript", {"Cache-Control": "max-age=86400"})
            if method == "GET" and path.startswith("/api/camera/hls/v/"):
                return self._hls_view(path)
            if not path.startswith("/api/"):
                return self._json({"detail": "not found"}, 404)
            if not self._authorised(qs):
                return self._json({"detail": "Invalid or missing API key"}, 401)

            if method == "GET" and path == "/api/health":
                return self._json({"ok": True, "version": VERSION, "server_time": time.time()})
            if method == "GET" and path == "/api/status":
                if (self.headers.get("X-Client") or "").startswith("ios-app"):
                    app_seen[0] = time.time()      # the phone app is open: it looks after its own Live Activity
                return self._json(status())
            if method == "GET" and path == "/api/alarms":
                limit = max(1, min(500, int((qs.get("limit") or ["100"])[0])))
                before = float(qs["before"][0]) if qs.get("before") else None
                active = (qs.get("active") or ["0"])[0].lower() in ("1", "true", "yes")
                return self._json(alarm_history(limit, before, active))
            if path == "/api/agent/update" and method == "PUT":
                return self._receive_update(qs)
            if path == "/api/agent/update" and method == "GET":
                info = update_info()
                return self._json({k: info.get(k) for k in ("version", "sha256", "size", "signature", "uploaded_at")})
            if path == "/api/agent/update/download" and method == "GET":
                f = UPDATE_DIR / "HanwhaMonitor.exe"
                if not f.is_file():
                    return self._json({"detail": "no update uploaded"}, 404)
                return self._send_file(f, "application/octet-stream")
            if method == "GET" and path == "/api/barchanges":
                limit = max(1, min(1000, int((qs.get("limit") or ["200"])[0])))
                with _lock:
                    rows = db.execute("SELECT * FROM bar_changes ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
                return self._json({"bar_changes": [dict(r) for r in rows]})
            if method == "GET" and path == "/api/programs":
                # every program with a name, notes or bars on record (plus the loaded one)
                with _lock:
                    current = program_key(latest.get("program"))
                    keys = {r["program"] for r in db.execute("SELECT program FROM programs")}
                    keys |= {r["program"] for r in db.execute("SELECT DISTINCT program FROM bars WHERE COALESCE(demo,0)=0")}
                    keys |= {r["program"] for r in db.execute("SELECT DISTINCT program FROM cycles WHERE COALESCE(demo,0)=0")}
                    if current:
                        keys.add(current)
                    rows = [program_row(k, current) for k in keys]
                rows.sort(key=lambda r: (not r["loaded"], -(r["last_bar_at"] or 0), r["program"]))
                return self._json({"programs": rows, "loaded": current})
            if method == "GET" and path == "/api/programs/detail":
                key = valid_program_key((qs.get("program") or [""])[0])
                if not key:
                    return self._json({"ok": False, "error": "program= is required"}, 400)
                with _lock:
                    info = program_row(key, program_key(latest.get("program")))
                    info["bars"] = [{**dict(r), "partial": bool(r["partial"]),
                                     "programs": json.loads(r["programs"]) if r["programs"] else []}
                                    for r in db.execute(
                        "SELECT id, parts, started_at, ended_at, partial, programs FROM bars WHERE program=? "
                        "AND COALESCE(demo,0)=0 ORDER BY ended_at DESC LIMIT 200", (key,))]
                return self._json(info)
            if method in ("POST", "PUT") and path == "/api/programs":
                # {"program": "O3110", "name": "EMS301", "ppb_manual": 43, "notes": "..."} - only the fields sent change;
                # an empty name / notes or a null ppb_manual clears that field
                b = self._body()
                key = valid_program_key(b.get("program"))
                if not key:
                    return self._json({"ok": False, "error": "program must look like O3110"}, 400)
                with _lock:
                    r = db.execute("SELECT * FROM programs WHERE program=?", (key,)).fetchone()
                    name = (r["name"] or "") if r else ""
                    ppb = r["ppb_manual"] if r else None
                    notes = (r["notes"] or "") if r else ""
                    stick = r["stick_out"] if r else None
                    if "stick_out" in b:
                        try:
                            stick = float(b["stick_out"]) if b["stick_out"] not in (None, "") else None
                        except (TypeError, ValueError):
                            return self._json({"ok": False, "error": "stick out must be a number of mm"}, 400)
                        if stick is not None and not 0 < stick <= 2000:
                            return self._json({"ok": False, "error": "stick out must be between 0 and 2000 mm"}, 400)
                    if "name" in b:
                        name = re.sub(r"\s+", " ", str(b.get("name") or "")).strip()[:40]
                    if "notes" in b:
                        notes = str(b.get("notes") or "").strip()[:500]
                    if "ppb_manual" in b:
                        try:
                            ppb = float(b["ppb_manual"]) if b["ppb_manual"] not in (None, "") else None
                        except (TypeError, ValueError):
                            return self._json({"ok": False, "error": "parts per bar must be a number"}, 400)
                        if ppb is not None and not 1 <= ppb <= 20000:
                            return self._json({"ok": False, "error": "parts per bar must be between 1 and 20000"}, 400)
                    has_doc = bool(r["doc_name"]) if r else False
                    if name or ppb or notes or stick or has_doc:
                        db.execute("INSERT INTO programs(program,name,ppb_manual,notes,stick_out,updated) "
                                   "VALUES(?,?,?,?,?,?) ON CONFLICT(program) DO UPDATE SET name=excluded.name, "
                                   "ppb_manual=excluded.ppb_manual, notes=excluded.notes, stick_out=excluded.stick_out, "
                                   "updated=excluded.updated", (key, name, ppb, notes, stick, time.time()))
                    else:
                        db.execute("DELETE FROM programs WHERE program=?", (key,))
                    info = program_row(key, program_key(latest.get("program")))
                log.info("Program %s: name=%r parts/bar=%s stick out=%s", key, name, ppb, stick)
                push.poke()
                return self._json({"ok": True, **info})
            if path == "/api/programs/doc":
                # the job PDF for a program: the dimensions drawing you look at when setting up
                key = valid_program_key((qs.get("program") or [""])[0])
                if not key:
                    return self._json({"ok": False, "error": "program= is required"}, 400)
                if method == "GET":
                    with _lock:
                        r = db.execute("SELECT doc_name FROM programs WHERE program=?", (key,)).fetchone()
                    f = doc_path(key)
                    if not r or not r["doc_name"] or not f.exists():
                        return self._json({"ok": False, "error": "No job PDF for this program"}, 404)
                    safe = re.sub(r'[^A-Za-z0-9._ -]', "_", r["doc_name"])[:80] or f"{key}.pdf"
                    return self._send_file(f, "application/pdf",
                                           {"Content-Disposition": f'inline; filename="{safe}"'})
                if method in ("PUT", "POST"):
                    try:
                        blob = self._raw_body(MAX_DOC + 1)
                    except ValueError:
                        return self._json({"ok": False, "error": f"The PDF is bigger than {MAX_DOC // (1024*1024)} MB"}, 413)
                    if len(blob) > MAX_DOC:
                        return self._json({"ok": False, "error": f"The PDF is bigger than {MAX_DOC // (1024*1024)} MB"}, 413)
                    if not blob.startswith(b"%PDF-"):
                        return self._json({"ok": False, "error": "That doesn't look like a PDF"}, 400)
                    given = (self.headers.get("X-Filename") or "").strip()
                    fname = (re.sub(r"[\r\n\"]", "", given)[:120] or f"{key}.pdf")
                    if not fname.lower().endswith(".pdf"):
                        fname += ".pdf"
                    DOCS.mkdir(parents=True, exist_ok=True)
                    tmp = doc_path(key).with_suffix(".part")
                    tmp.write_bytes(blob)
                    tmp.replace(doc_path(key))
                    with _lock:
                        db.execute("INSERT INTO programs(program,name,doc_name,doc_at,doc_size,updated) "
                                   "VALUES(?,'',?,?,?,?) ON CONFLICT(program) DO UPDATE SET doc_name=excluded.doc_name, "
                                   "doc_at=excluded.doc_at, doc_size=excluded.doc_size, updated=excluded.updated",
                                   (key, fname, time.time(), len(blob), time.time()))
                        info = program_row(key, program_key(latest.get("program")))
                    log.info("Job PDF for %s: %s (%d KB)", key, fname, len(blob) // 1024)
                    push.poke()
                    return self._json({"ok": True, **info})
                if method == "DELETE":
                    with _lock:
                        db.execute("UPDATE programs SET doc_name=NULL, doc_at=NULL, doc_size=NULL, updated=? "
                                   "WHERE program=?", (time.time(), key))
                        row = db.execute("SELECT name, ppb_manual, notes, stick_out FROM programs "
                                         "WHERE program=?", (key,)).fetchone()
                        if row and not (row["name"] or row["ppb_manual"] or row["notes"] or row["stick_out"]):
                            db.execute("DELETE FROM programs WHERE program=?", (key,))
                    doc_path(key).unlink(missing_ok=True)
                    return self._json({"ok": True})
            if path == "/api/maintenance":
                # the backup battery (and anything else on a schedule): when it was last done and how often
                if method == "GET":
                    with _lock:
                        items = maint_all()
                    return self._json({"items": items})
                if method in ("POST", "PUT"):
                    # {"item":"battery", "changed":true}  or  {"item":"battery","last_at":…,"every_months":12,
                    #  "warn_days":30,"notes":"…"}
                    b = self._body()
                    item = re.sub(r"[^a-z0-9_-]", "", str(b.get("item") or "battery").lower())[:32] or "battery"
                    try:
                        with _lock:
                            info = maint_save(item, b)
                    except (TypeError, ValueError):
                        return self._json({"ok": False, "error": "Check the date and the number of months"}, 400)
                    log.info("Maintenance %s: last done %s, every %g months", item,
                             time.strftime("%Y-%m-%d", time.localtime(info["last_at"])) if info["last_at"] else "-",
                             info["every_months"])
                    push.poke()
                    return self._json({"ok": True, **info})
            if method == "GET" and path == "/api/report":
                # ?period=day|month|year&limit=30&from=2026-01-01&to=2026-12-31
                period = (qs.get("period") or ["day"])[0].strip().lower()
                try:
                    limit = max(1, min(400, int((qs.get("limit") or ["30"])[0])))
                except ValueError:
                    limit = 30
                since = report_date((qs.get("from") or [""])[0].strip(), False)
                until = report_date((qs.get("to") or [""])[0].strip(), True)
                with _lock:
                    d = report(period, limit, since, until)
                return self._json(d, 400 if d.get("error") else 200)
            if method == "GET" and path == "/api/bars/day":
                with _lock:
                    d = bar_day((qs.get("date") or [""])[0].strip() or None, time.time())
                return self._json(d, 400 if d.get("error") else 200)
            if method == "GET" and path == "/api/bars":
                # parts per bar, per program (what the forecast is based on), plus the recent bars
                with _lock:
                    progs = db.execute("SELECT program, COUNT(*) n, MAX(ended_at) last FROM bars WHERE COALESCE(demo,0)=0 "
                                       "GROUP BY program ORDER BY last DESC").fetchall()
                    out = [{**(parts_per_bar(r["program"], False) or {}), "program": r["program"],
                            "label": program_label(r["program"]), "recorded": r["n"], "last_at": r["last"]}
                           for r in progs]
                    recent = [dict(r) for r in db.execute("SELECT * FROM bars ORDER BY ended_at DESC LIMIT 100")]
                return self._json({"programs": out, "bars": recent})
            if method == "DELETE" and path == "/api/bars":
                # forget one bar (?id=17 - e.g. a bad one) or all of a program's bars (?program=O1234 - e.g. after
                # changing the bar length or the part)
                bar_id = (qs.get("id") or [""])[0].strip()
                prog = valid_program_key((qs.get("program") or [""])[0])
                if not bar_id.isdigit() and not prog:
                    return self._json({"ok": False, "error": "id= or program= is required"}, 400)
                with _lock:
                    if bar_id.isdigit():
                        n = db.execute("DELETE FROM bars WHERE id=?", (int(bar_id),)).rowcount
                    else:
                        n = db.execute("DELETE FROM bars WHERE program=?", (prog,)).rowcount
                push.poke()
                return self._json({"ok": True, "deleted": n})
            if method == "GET" and path == "/api/messages":
                limit = max(1, min(500, int((qs.get("limit") or ["100"])[0])))
                with _lock:
                    msgs = message_rows(False, limit)
                return self._json({"messages": msgs})
            if method == "GET" and path == "/api/states":
                hours = max(0.1, min(24 * 31, float((qs.get("hours") or ["24"])[0])))
                return self._json(state_history(hours))
            if method == "POST" and path == "/api/replay":
                # the PC catching the server up after an outage: a list of snapshots, oldest first
                body = self._body()
                snaps = body.get("snapshots") or []
                if not isinstance(snaps, list) or len(snaps) > 500:
                    return self._json({"ok": False, "error": "send up to 500 snapshots"}, 400)
                ack: list[str] = []
                first = last = None
                for snap in snaps:
                    if not isinstance(snap, dict):
                        continue
                    at = float(snap.get("sent_at") or 0)
                    first, last = first or at, at
                    try:
                        ack += ingest(snap, replay=True).get("ack") or []
                    except Exception:
                        log.exception("Replayed snapshot failed")
                if snaps:
                    log.info("Caught up on %d update(s) the PC saved while this server was away (%s - %s)", len(snaps),
                             time.strftime("%H:%M:%S", time.localtime(first or 0)),
                             time.strftime("%H:%M:%S", time.localtime(last or 0)))
                return self._json({"ok": True, "accepted": len(snaps), "ack": ack})
            if method == "POST" and path == "/api/ingest":
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0 or length > 1_000_000:
                    return self._json({"detail": "bad body"}, 400)
                self._drained = True
                snap = json.loads(self.rfile.read(length))
                if not isinstance(snap, dict):
                    return self._json({"detail": "expected object"}, 400)
                return self._json(ingest(snap))
            if path.startswith("/api/push/"):
                body = {}
                if method == "POST":
                    length = int(self.headers.get("Content-Length") or 0)
                    self._drained = True
                    body = json.loads(self.rfile.read(length)) if 0 < length < 100_000 else {}
                if method == "GET" and path == "/api/push/status":
                    return self._json(push.info())
                if method == "POST" and path == "/api/push/register":
                    return self._json(push.register(body.get("kind", ""), body.get("token", ""), body.get("activity_id"),
                                                    body.get("env"), body.get("prefs"), body.get("bundle_id")))
                if method == "POST" and path == "/api/push/unregister":
                    push.unregister(body.get("token", ""), body.get("activity_id", ""))
                    return self._json({"ok": True})
                if method == "POST" and path == "/api/push/test":
                    return self._json(push.test_alert())
            if path.startswith("/api/camera/"):
                return self._camera_route(method, path)
            if method == "POST" and path == "/api/commands":
                b = self._body()
                kind = b.get("type")
                if kind not in ALLOWED:
                    return self._json({"ok": False, "error": "Unknown command"}, 400)
                if not commands.agent_listening:
                    return self._json({"ok": False, "error": "The PC app on the machine isn't connected."}, 503)
                cmd = {"type": kind, "from": self._client_ip()}
                for k in ("x", "y", "value", "on", "token"):
                    if k in b:
                        cmd[k] = b[k]
                cid = commands.submit(cmd)
                res = commands.wait_result(cid, timeout=8)
                if res is None:
                    return self._json({"ok": False, "error": "No answer from the PC app in time."}, 504)
                return self._json({"ok": res["ok"], "message": res["message"],
                                   "error": "" if res["ok"] else res["message"]})
            if method == "GET" and path == "/api/agent/commands":
                wait = max(0.0, min(30.0, float((qs.get("wait") or ["25"])[0])))
                return self._json({"commands": commands.take(wait)})
            if method == "POST" and path == "/api/agent/results":
                b = self._body()
                commands.put_result(str(b.get("id") or ""), bool(b.get("ok")), str(b.get("message") or ""))
                return self._json({"ok": True})
            if method == "DELETE" and path == "/api/alarms":
                if (qs.get("demo") or ["0"])[0].lower() in ("1", "true", "yes"):
                    return self._json(clear_demo())
                return self._json(clear_history())
            return self._json({"detail": "not found"}, 404)
        except (ValueError, KeyError, TypeError) as e:
            return self._json({"detail": f"bad request: {e}"}, 400)
        except Exception as e:  # pragma: no cover
            log.exception("request failed")
            return self._json({"detail": f"server error: {e}"}, 500)

    # ------------------------------------------------------------------ camera
    def _camera_route(self, method: str, path: str):
        if method == "POST" and path == "/api/camera/frame":
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= MAX_FRAME:
                return self._json({"detail": "bad frame"}, 400)
            self._drained = True
            jpg = self.rfile.read(length)
            if not jpg.startswith(b"\xff\xd8"):
                return self._json({"detail": "not a JPEG"}, 400)
            try:
                ft = float(self.headers.get("X-Frame-Time") or 0) or None
            except ValueError:
                ft = None
            camera.put(jpg, ft)
            return self._json({"ok": True, "live": camera.watched})
        if path == "/api/camera/clip":
            # a few seconds of video from around an alarm, sent up by the PC app once the camera has caught up
            url = urlparse(self.path)
            qs = parse_qs(url.query)
            aid = (qs.get("alarm") or [""])[0].strip()
            if not aid:
                return self._json({"ok": False, "error": "alarm= is required"}, 400)
            with _lock:
                aid = canonical_alarm(aid)
            if method in ("PUT", "POST"):
                try:
                    blob = self._raw_body(MAX_CLIP + 1)
                except ValueError:
                    return self._json({"ok": False, "error": "clip too big"}, 413)
                if len(blob) > MAX_CLIP or len(blob) < 1000:
                    return self._json({"ok": False, "error": "clip too big or empty"}, 413)
                CLIPS.mkdir(parents=True, exist_ok=True)
                f = clip_path(aid)
                tmp = f.with_suffix(".part")
                tmp.write_bytes(blob)
                tmp.replace(f)
                def num(name, default=None):
                    try:
                        return float((qs.get(name) or [""])[0])
                    except ValueError:
                        return default
                with _lock:
                    row = db.execute("SELECT code, message, started_at FROM alarms WHERE id=?", (aid,)).fetchone()
                    db.execute("INSERT INTO clips(alarm_id,at,seconds,size,code,message,uploaded_at,delay_s) "
                               "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(alarm_id) DO UPDATE SET at=excluded.at, "
                               "seconds=excluded.seconds, size=excluded.size, uploaded_at=excluded.uploaded_at, "
                               "delay_s=excluded.delay_s",
                               (aid, num("at") or (row["started_at"] if row else time.time()), num("seconds"),
                                len(blob), row["code"] if row else None, row["message"] if row else None,
                                time.time(), num("delay")))
                    clip_ids.add(aid)
                    old = prune_clips()
                for gone in old:
                    clip_path(gone).unlink(missing_ok=True)
                log.info("Camera clip for alarm %s: %.1f s, %d KB", aid, num("seconds") or 0, len(blob) // 1024)
                return self._json({"ok": True})
            if method == "GET":
                f = clip_path(aid)
                if aid not in clip_ids or not f.exists():
                    return self._json({"ok": False, "error": "No clip for that alarm"}, 404)
                return self._send_video(f)
            if method == "DELETE":
                with _lock:
                    db.execute("DELETE FROM clips WHERE alarm_id=?", (aid,))
                    clip_ids.discard(aid)
                clip_path(aid).unlink(missing_ok=True)
                return self._json({"ok": True})
        if method == "GET" and path == "/api/camera/clips":
            with _lock:
                clips = clip_list(100)
            return self._json({"clips": clips})
        if method == "GET" and path == "/api/camera/status":
            return self._json(camera.info())
        if method == "GET" and path == "/api/camera/frame.jpg":
            camera.touch()
            seq, jpg, ft = camera.seq, camera.jpg, camera.frame_time
            if jpg is None:
                return self._json({"detail": "no camera image yet"}, 404)
            etag = f'"{seq}"'
            hdrs = {"ETag": etag, "X-Frame-Time": f"{ft:.3f}"}
            if self.headers.get("If-None-Match") == etag:
                return self._send(304, b"", "image/jpeg", hdrs)
            return self._send(200, jpg, "image/jpeg", hdrs)
        if method == "GET" and path == "/api/camera/stream":
            return self._mjpeg()
        if method == "GET" and path == "/api/camera/live":
            camera.touch()
            info = video.info()
            if info["ready"]:
                info["url"] = f"api/camera/hls/v/{video.token()}/{info['session']}/live.m3u8"
            info["still"] = camera.info()
            return self._json(info)
        if path.startswith("/api/camera/hls/push/"):
            parts = path.split("/")          # ['', 'api', 'camera', 'hls', 'push', session, file]
            if len(parts) != 7 or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", parts[5]) \
                    or not re.fullmatch(r"[A-Za-z0-9_.-]{1,60}", parts[6]):
                return self._json({"detail": "bad path"}, 400)
            if method in ("PUT", "POST"):
                video.put(parts[5], parts[6], self._raw_body(8_000_000))
                return self._json({"ok": True, "live": camera.watched})
            if method == "DELETE":
                # ffmpeg sends its DELETEs with an (empty) chunked body: it must be read, or the leftover bytes
                # corrupt the next upload on this kept-open connection and the live video stalls
                self._raw_body(1_000_000)
                return self._json({"ok": True})
        return self._json({"detail": "not found"}, 404)

    def _receive_update(self, qs):
        import hashlib
        version = (qs.get("version") or [""])[0].strip()
        sig = (self.headers.get("X-Signature") or "").strip().lower()
        if not re.fullmatch(r"\d+(\.\d+){1,3}", version):
            return self._json({"ok": False, "error": "version must look like 1.8.0"}, 400)
        if not re.fullmatch(r"[0-9a-f]{128}", sig):
            return self._json({"ok": False, "error": "missing or malformed X-Signature (the build's .sig file)"}, 400)
        length = int(self.headers.get("Content-Length") or 0)
        if not 100_000 < length < 300_000_000:
            return self._json({"ok": False, "error": "expected the HanwhaMonitor.exe file"}, 400)
        self._drained = True
        UPDATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = UPDATE_DIR / "upload.tmp"
        h = hashlib.sha256()
        left = length
        with open(tmp, "wb") as out:
            while left > 0:
                chunk = self.rfile.read(min(1 << 20, left))
                if not chunk:
                    return self._json({"ok": False, "error": "upload cut short"}, 400)
                out.write(chunk)
                h.update(chunk)
                left -= len(chunk)
        os.replace(tmp, UPDATE_DIR / "HanwhaMonitor.exe")
        info = {"version": version, "sha256": h.hexdigest(), "size": length, "signature": sig, "uploaded_at": time.time()}
        (UPDATE_DIR / "update.json").write_text(json.dumps(info))
        log.info("PC app update %s uploaded (%d KB)", version, length // 1024)
        return self._json({"ok": True, **{k: info[k] for k in ("version", "sha256", "size")}})

    def _send_file(self, path: Path, ctype: str, headers: dict | None = None):
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            with open(path, "rb") as f:
                while chunk := f.read(1 << 20):
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _send_video(self, path: Path):
        """An mp4 with byte-range support - AVPlayer on the phone asks for ranges before it will play."""
        size = path.stat().st_size
        if size <= 0:
            return self._json({"ok": False, "error": "that clip is empty"}, 404)
        start, end = 0, size - 1
        rng = (self.headers.get("Range") or "").strip()
        partial = False
        m = re.match(r"bytes=(\d*)-(\d*)$", rng)
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                if start >= size:                  # asking past the end of the file
                    return self._send(416, b"", "text/plain", {"Content-Range": f"bytes */{size}"})
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
            else:                                  # "bytes=-500": the last 500 bytes
                start = max(0, size - int(m.group(2)))
            if end < start:
                start, end = 0, size - 1
            else:
                partial = True
        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "private, max-age=600")
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = length
                while left > 0 and (chunk := f.read(min(1 << 20, left))):
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _hls_view(self, path: str):
        parts = path.split("/")          # ['', 'api', 'camera', 'hls', 'v', token, session, file]
        if len(parts) != 8 or not video.token_ok(parts[5]):
            return self._json({"detail": "link expired"}, 403)
        camera.touch()
        data, ctype = video.get(parts[6], parts[7])
        if data is None:
            return self._json({"detail": "gone"}, 404)
        return self._send(200, data, ctype)

    def _mjpeg(self):
        """multipart/x-mixed-replace stream for <img src>. Re-sends the last frame every few seconds when nothing
        new arrives, so a closed browser tab is noticed (the write fails) and the agent can stop streaming."""
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        seq, started, last_write = -1, time.time(), 0.0
        try:
            while time.time() - started < 30 * 60:   # the page reconnects after this
                camera.touch()
                new_seq, jpg, ft = camera.wait_new(seq, timeout=1.0)
                if jpg is None or (new_seq == seq and time.time() - last_write < 5):
                    continue
                seq = new_seq
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(jpg))
                self.wfile.write(jpg)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
                last_write = time.time()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def do_GET(self):
        self._route("GET")

    def do_HEAD(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")
        self._drain()

    def do_PUT(self):
        self._route("PUT")
        self._drain()

    def do_DELETE(self):
        self._route("DELETE")
        self._drain()

    def _drain(self):
        """Discard any request body a handler didn't read, so a kept-open connection stays in sync."""
        if getattr(self, "_drained", False):
            return
        try:
            if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower() or \
                    int(self.headers.get("Content-Length") or 0) > 0:
                self.close_connection = True   # can't tell safely how much is left - start a fresh connection
        except ValueError:
            self.close_connection = True

    def do_OPTIONS(self):
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-API-Key")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()


def main():
    load_clip_ids()
    try:
        backfill_daily()
    except sqlite3.Error as e:      # the report can fill itself in from here on; not worth failing to start over
        log.warning("Couldn't fill in the report history: %s", e)
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    httpd.daemon_threads = True
    log.info("Hanwha Monitor server %s listening on :%d (db %s, api key %s, push %s)", VERSION, PORT, DB_PATH,
             "ON" if API_KEY else "off", "ON" if push.apns.enabled else push.apns.error)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
