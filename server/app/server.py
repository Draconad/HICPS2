"""Hanwha Monitor server (standard library; httpx[http2] + cryptography only for Apple push).

Receives snapshots from the Windows agent, keeps the alarm history in SQLite, and serves
status to the iPhone app plus a small web dashboard.

  POST /api/ingest     agent -> server snapshot
  GET  /api/status     current machine status + active alarms
  GET  /api/alarms     alarm history  (?limit=100&before=<epoch>&active=1)
  DELETE /api/alarms   clear cleared history  (?demo=1: delete demo-mode alarms only)
  GET  /api/states     state change log (?hours=24)
  PUT  /api/agent/update?version=X  (exe body, X-Signature header)   GET /api/agent/update   GET /api/agent/update/download
  GET  /api/barchanges   bar change history
  GET  /api/messages   operator message history (?limit=100) - e.g. "work count end in 1 hour", not alarms
  GET  /api/health
  POST /api/push/register   {kind: alert|la|la_start, token, activity_id?, env?, prefs?}
  POST /api/push/unregister {token? , activity_id?}
  GET  /api/push/status     POST /api/push/test
  POST /api/camera/frame    (agent, image/jpeg)   GET /api/camera/frame.jpg   GET /api/camera/stream (MJPEG)
  POST /api/commands {type: ptz|set_required|set_work_counter, ...} -> result from the PC (waits up to ~8 s)
  GET  /api/agent/commands?wait=25 (agent long-poll)   POST /api/agent/results {id, ok, message}
  GET  /api/camera/status   GET /api/camera/live -> token URL of the live HLS stream
  PUT  /api/camera/hls/push/<session>/<file>  (agent's ffmpeg)   GET /api/camera/hls/v/<token>/<session>/<file>
  GET  /               web dashboard (needs a web login)      GET /login  login page
  GET  /auth/info   POST /auth/login {username,password,api_key?}   POST /auth/change   POST /auth/logout

/api/* accepts either the API key (X-API-Key header, or ?key=) or a logged-in browser session.
Without API_KEY set, /api/* is open (the agent and app need no key) - only the dashboard page needs a login.
"""
from __future__ import annotations

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

VERSION = "1.9.0"
DB_PATH = os.environ.get("DB_PATH", "/data/monitor.db")
API_KEY = os.environ.get("API_KEY", "").strip()
AGENT_TIMEOUT = float(os.environ.get("AGENT_TIMEOUT", "30"))
PORT = int(os.environ.get("PORT", "8420"))
# Running -> standby only shows once standby has lasted this long (hides the gap between part cycles).
STANDBY_DELAY = float(os.environ.get("STANDBY_DELAY", "4"))
# a bar change lasting longer than this sends a "taking long" notification (usually a failed bar load)
BAR_CHANGE_ALERT = float(os.environ.get("BAR_CHANGE_ALERT", "180"))
STATIC = Path(__file__).parent / "static"

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
    """)
    cols = {r["name"] for r in c.execute("PRAGMA table_info(alarms)")}
    if "demo" not in cols:   # 1 = from the agent's demo mode, 0 = real machine, NULL = recorded before 1.2.0
        c.execute("ALTER TABLE alarms ADD COLUMN demo INTEGER")
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
def alarm_row(r: sqlite3.Row, now: float) -> dict:
    d = dict(r)
    d.pop("key", None)
    end = d["cleared_at"] or now
    d["duration_s"] = round(max(0.0, end - d["started_at"]), 1)
    d["active"] = d["cleared_at"] is None
    d["demo"] = bool(d.get("demo"))
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


def track_state(now: float):
    state, detail, _ = effective_state(now)
    if state != meta.get("state"):
        log.info("State %s -> %s (%s)", meta.get("state"), state, detail)
        if meta.get("state") == "running":
            meta["running_ended_at"] = now      # for "only notify while running" (+ a short grace period)
        meta["state"] = state
        meta["state_since"] = now
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
                   (a["id"], a["key"], a.get("path"), a.get("path_name"), a.get("code"), a.get("type"),
                    a.get("type_name"), a.get("number"), a.get("axis"), a.get("message"), started, cleared,
                    1 if demo else 0))
        if cleared is None:
            log.info("ALARM %s [%s] %s", a.get("code"), a.get("path_name"), a.get("message"))

    for a in alarms:
        if not a.get("id") or not a.get("key") or a.get("started_at") is None:
            continue
        started = float(a["started_at"]) + offset
        cleared = float(a["cleared_at"]) + offset if a.get("cleared_at") else None
        row = lookup(a["id"])
        if cleared is None:
            if not machine_connected:
                continue
            active_keys.add(a["key"])
            if row is not None:
                if row["cleared_at"] is None and a.get("message") and a["message"] != row["message"]:
                    db.execute("UPDATE alarms SET message=? WHERE id=?", (a["message"], row["id"]))
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
    r = db.execute("SELECT COUNT(*) n, AVG(duration_s) avg FROM bar_changes WHERE started_at>=?", (day_start,)).fetchone()
    last = db.execute("SELECT * FROM bar_changes ORDER BY started_at DESC LIMIT 1").fetchone()
    week = db.execute("SELECT AVG(duration_s) avg FROM bar_changes WHERE started_at>=?", (now - 7 * 86400,)).fetchone()
    return {"today": r["n"] or 0, "avg_today_s": round(r["avg"], 1) if r["avg"] else None,
            "avg_week_s": round(week["avg"], 1) if week["avg"] else None,
            "last_s": last["duration_s"] if last else None, "last_at": last["ended_at"] if last else None,
            "alert_after_s": BAR_CHANGE_ALERT}


def message_rows(active_only: bool, limit: int = 100) -> list[dict]:
    sql = "SELECT * FROM op_messages" + (" WHERE cleared_at IS NULL" if active_only else "") + \
          " ORDER BY started_at DESC LIMIT ?"
    return [{"id": r["id"], "number": r["number"], "text": r["text"], "started_at": r["started_at"],
             "cleared_at": r["cleared_at"], "active": r["cleared_at"] is None, "demo": bool(r["demo"])}
            for r in db.execute(sql, (limit,)).fetchall()]


def ingest(snap: dict) -> dict:
    now = time.time()
    with _lock:
        sent_at = float(snap.get("sent_at") or now)
        offset = now - sent_at if abs(now - sent_at) > 3 else 0.0   # correct the PC's clock drift
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
        if snap.get("last_cycle_s") is None:
            snap["last_cycle_s"] = latest.get("last_cycle_s")   # keep last known across agent restarts
        snap.pop("alarms", None)
        snap["received_at"] = now
        timer = snap.get("cycle_timer_s")
        snap["cycle_started_at"] = (now - timer) if (timer and snap.get("state") == "running") else None
        if snap.get("state") != latest.get("state"):
            meta["raw_since"] = now
        snap["bar_change"] = bool(snap.get("bar_change")) and connected and snap.get("state") == "running"
        if snap["bar_change"] != bool(latest.get("bar_change")):
            if snap["bar_change"]:
                meta["bar_change_since"] = now
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
        # when the count first sat at exactly the required count (for "over producing")
        if req and parts is not None and parts == req:
            if meta.get("at_req") != [parts, req]:
                meta["at_req"], meta["at_req_since"] = [parts, req], now
        else:
            meta.pop("at_req", None)
        if snap.get("state") == "running":
            meta["last_running"] = {"detail": snap.get("state_detail"), "cycle_started_at": snap.get("cycle_started_at"),
                                    "cycle_timer_s": snap.get("cycle_timer_s")}
        latest.clear()
        latest.update(snap)
        meta["last_seen"] = now
        if connected:
            meta["last_connected_at"] = now
        track_state(now)
        kv_set("latest", latest)
        kv_set("meta", meta)
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
        today = db.execute("SELECT COUNT(*) AS n FROM alarms WHERE started_at>=?", (day_start,)).fetchone()["n"]
        parts, req, cyc = latest.get("parts"), latest.get("parts_required"), latest.get("last_cycle_s")
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
            "bar_changes": bar_change_stats(now),
            # still running after reaching the required count (the work counter isn't stopping it)
            "over_producing": bool(running and not bar_change and req and parts is not None
                                   and (parts > req or (parts == req and latest.get("state") == "running"
                                                        and now - (meta.get("at_req_since") or now) > 8))),
            "agent_version": latest.get("agent_version"),
            "agent_update": update_info().get("version"),
            "program": latest.get("program") or {},
            "paths": latest.get("paths") or [],
            "active_alarms": active if state != "off" else [],
            "messages": message_rows(True, 10) if state != "off" else [],
            "alarms_today": today,
            # A bar change is reported as state "running" plus this flag (keeps older app builds working).
            "bar_change": bar_change,
            "bar_change_since": meta.get("bar_change_since") if bar_change else None,
            # work counter "stop at required count" switch: True/False, None = not set up on the monitor PC
            "work_counter": latest.get("work_counter") if state != "off" else None,
            "camera": dict(camera.info(), **(latest.get("camera_features") or {})),
            "controls": dict(latest.get("controls") or {}, connected=commands.agent_listening),
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


def clear_demo() -> dict:
    """Delete every alarm that came from the agent's demo mode (active ones too)."""
    with _lock:
        where, args = _demo_where()
        n = db.execute(f"DELETE FROM alarms WHERE {where}", args).rowcount
        db.execute("DELETE FROM alarm_alias WHERE alarm_id NOT IN (SELECT id FROM alarms)")
    log.info("Cleared %d demo alarm(s)", n)
    return {"ok": True, "deleted": n}


def clear_history() -> dict:
    with _lock:
        db.execute("DELETE FROM alarms WHERE cleared_at IS NOT NULL")
        db.execute("DELETE FROM alarm_alias WHERE alarm_id NOT IN (SELECT id FROM alarms)")
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
                out += self.rfile.read(size)
                self.rfile.readline(8)
                if len(out) > limit:
                    raise ValueError("body too large")
        length = int(self.headers.get("Content-Length") or 0)
        if length > limit:
            raise ValueError("body too large")
        return self.rfile.read(length) if length > 0 else b""

    def _body(self) -> dict:
        self._drained = True
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length < 100_000:
            return {}
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
            if method == "GET" and path in ("/", "/index.html"):
                s = self._session()
                if not s or s["must_change"]:
                    return self._redirect("login")
                return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
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
            if method == "GET" and path == "/api/messages":
                limit = max(1, min(500, int((qs.get("limit") or ["100"])[0])))
                with _lock:
                    return self._json({"messages": message_rows(False, limit)})
            if method == "GET" and path == "/api/states":
                hours = max(0.1, min(24 * 31, float((qs.get("hours") or ["24"])[0])))
                return self._json(state_history(hours))
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

    def _send_file(self, path: Path, ctype: str):
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            with open(path, "rb") as f:
                while chunk := f.read(1 << 20):
                    self.wfile.write(chunk)
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
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    httpd.daemon_threads = True
    log.info("Hanwha Monitor server %s listening on :%d (db %s, api key %s, push %s)", VERSION, PORT, DB_PATH,
             "ON" if API_KEY else "off", "ON" if push.apns.enabled else push.apns.error)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
