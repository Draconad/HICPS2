"""Hanwha Monitor server (standard library; httpx[http2] + cryptography only for Apple push).

Receives snapshots from the Windows agent, keeps the alarm history in SQLite, and serves
status to the iPhone app plus a small web dashboard.

  POST /api/ingest     agent -> server snapshot
  GET  /api/status     current machine status + active alarms
  GET  /api/alarms     alarm history  (?limit=100&before=<epoch>&active=1)
  GET  /api/states     state change log (?hours=24)
  GET  /api/health
  POST /api/push/register   {kind: alert|la|la_start, token, activity_id?, env?, prefs?}
  POST /api/push/unregister {token? , activity_id?}
  GET  /api/push/status     POST /api/push/test
  GET  /               web dashboard
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

try:  # works both as "python app/server.py" and as a package import
    from apns import APNs
    from push import PushService
except ImportError:  # pragma: no cover
    from .apns import APNs
    from .push import PushService

VERSION = "1.1.0"
DB_PATH = os.environ.get("DB_PATH", "/data/monitor.db")
API_KEY = os.environ.get("API_KEY", "").strip()
AGENT_TIMEOUT = float(os.environ.get("AGENT_TIMEOUT", "30"))
PORT = int(os.environ.get("PORT", "8420"))
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
    """)
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
    return latest.get("state", "off"), latest.get("state_detail", ""), True


def track_state(now: float):
    state, detail, _ = effective_state(now)
    if state != meta.get("state"):
        log.info("State %s -> %s (%s)", meta.get("state"), state, detail)
        meta["state"] = state
        meta["state_since"] = now
        db.execute("INSERT INTO state_log(ts,state,detail) VALUES(?,?,?)", (now, state, detail))
        kv_set("meta", meta)


def upsert_alarms(alarms: list[dict], offset: float, now: float, machine_connected: bool) -> list[str]:
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
                      started_at,cleared_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (a["id"], a["key"], a.get("path"), a.get("path_name"), a.get("code"), a.get("type"),
                    a.get("type_name"), a.get("number"), a.get("axis"), a.get("message"), started, cleared))
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


def ingest(snap: dict) -> dict:
    now = time.time()
    with _lock:
        sent_at = float(snap.get("sent_at") or now)
        offset = now - sent_at if abs(now - sent_at) > 3 else 0.0   # correct the PC's clock drift
        connected = bool(snap.get("machine_connected"))
        db.execute("BEGIN")
        try:
            ack = upsert_alarms(snap.get("alarms") or [], offset, now, connected)
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
        latest.clear()
        latest.update(snap)
        meta["last_seen"] = now
        if connected:
            meta["last_connected_at"] = now
        track_state(now)
        kv_set("latest", latest)
        kv_set("meta", meta)
    push.poke()
    return {"ok": True, "ack": ack}


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
        return {
            "server_time": now,
            "machine_name": latest.get("machine_name") or "Hanwha XE35",
            "state": state,
            "state_detail": detail,
            "state_since": meta.get("state_since"),
            "agent_online": agent_online,
            "agent_last_seen": meta.get("last_seen"),
            "machine_connected": bool(latest.get("machine_connected")) and agent_online,
            "demo": bool(latest.get("demo")),
            "parts": parts,
            "parts_required": req,
            "parts_total": latest.get("parts_total"),
            "last_cycle_s": cyc,
            "cycle_timer_s": latest.get("cycle_timer_s") if running else None,
            "cycle_started_at": latest.get("cycle_started_at") if running else None,
            "eta_s": round(eta) if eta else None,
            "program": latest.get("program") or {},
            "paths": latest.get("paths") or [],
            "active_alarms": active if state != "off" else [],
            "alarms_today": today,
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
    return {"alarms": rows, "next_before": rows[-1]["started_at"] if len(rows) == limit else None}


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


def clear_history() -> dict:
    with _lock:
        db.execute("DELETE FROM alarms WHERE cleared_at IS NOT NULL")
        db.execute("DELETE FROM alarm_alias WHERE alarm_id NOT IN (SELECT id FROM alarms)")
    return {"ok": True}


# --------------------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = f"HanwhaMonitor/{VERSION}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):   # quiet: the agent posts every couple of seconds
        pass

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _authorised(self, qs) -> bool:
        if not API_KEY:
            return True
        given = self.headers.get("X-API-Key") or (qs.get("key") or [""])[0]
        return given == API_KEY

    def _route(self, method: str):
        url = urlparse(self.path)
        qs = parse_qs(url.query)
        path = url.path.rstrip("/") or "/"
        try:
            if method == "GET" and path in ("/", "/index.html"):
                return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
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
            if method == "GET" and path == "/api/states":
                hours = max(0.1, min(24 * 31, float((qs.get("hours") or ["24"])[0])))
                return self._json(state_history(hours))
            if method == "POST" and path == "/api/ingest":
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0 or length > 1_000_000:
                    return self._json({"detail": "bad body"}, 400)
                snap = json.loads(self.rfile.read(length))
                if not isinstance(snap, dict):
                    return self._json({"detail": "expected object"}, 400)
                return self._json(ingest(snap))
            if path.startswith("/api/push/"):
                body = {}
                if method == "POST":
                    length = int(self.headers.get("Content-Length") or 0)
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
            if method == "DELETE" and path == "/api/alarms":
                return self._json(clear_history())
            return self._json({"detail": "not found"}, 404)
        except (ValueError, KeyError, TypeError) as e:
            return self._json({"detail": f"bad request: {e}"}, 400)
        except Exception as e:  # pragma: no cover
            log.exception("request failed")
            return self._json({"detail": f"server error: {e}"}, 500)

    def do_GET(self):
        self._route("GET")

    def do_HEAD(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_DELETE(self):
        self._route("DELETE")

    def do_OPTIONS(self):
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-API-Key")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
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
