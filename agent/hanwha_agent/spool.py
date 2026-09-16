"""Store and forward: keeps what happened while the server couldn't be reached, and sends it when it's back.

Every snapshot that changes something worth keeping (a part counted, a bar change, a program or state change, a
machine message) is written to a small SQLite file next to the config. Rows are marked as sent once the server has
them, so a network outage, an Unraid reboot - or this app restarting or updating itself - doesn't lose the part
times, bars or alarms from that period.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path

log = logging.getLogger("hanwha.spool")

HEARTBEAT = 300          # also keep one snapshot every few minutes, so long quiet periods are still on record
KEEP_SENT = 6 * 3600     # tidy sent rows away after this long
KEEP_ANY = 14 * 86400    # never keep anything longer than this
MAX_ROWS = 50_000


def _notable(a: dict, b: dict) -> bool:
    """Is snapshot b different from a in a way worth storing?"""
    if not a:
        return True
    keys = ("state", "parts", "parts_total", "parts_required", "last_cycle_s", "bar_change", "work_counter",
            "machine_connected")
    if any(a.get(k) != b.get(k) for k in keys):
        return True
    if (a.get("program") or {}).get("number") != (b.get("program") or {}).get("number"):
        return True
    if [m.get("text") for m in (a.get("messages") or [])] != [m.get("text") for m in (b.get("messages") or [])]:
        return True
    ids = lambda s: sorted((x.get("id"), bool(x.get("cleared_at"))) for x in (s.get("alarms") or []))  # noqa: E731
    return ids(a) != ids(b)


class Spool:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._last: dict = {}
        self._last_at = 0.0
        self.db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL,"
                        " sent INTEGER NOT NULL DEFAULT 0, payload TEXT NOT NULL)")
        self.db.execute("CREATE INDEX IF NOT EXISTS events_unsent ON events(sent, id)")

    def add(self, snap: dict) -> int | None:
        """Store the snapshot if it's worth keeping. Returns its row id (or None if nothing was stored)."""
        now = float(snap.get("sent_at") or time.time())
        with self._lock:
            if not _notable(self._last, snap) and now - self._last_at < HEARTBEAT:
                return None
            self._last, self._last_at = snap, now
            try:
                cur = self.db.execute("INSERT INTO events(at,payload) VALUES(?,?)", (now, json.dumps(snap)))
                return cur.lastrowid
            except sqlite3.Error as e:
                log.warning("Couldn't save to the local log: %s", e)
                return None

    def pending(self) -> int:
        with self._lock:
            try:
                return self.db.execute("SELECT COUNT(*) n FROM events WHERE sent=0").fetchone()["n"]
            except sqlite3.Error:
                return 0

    def unsent(self, limit: int = 100) -> list[tuple[int, dict]]:
        with self._lock:
            try:
                rows = self.db.execute("SELECT id, payload FROM events WHERE sent=0 ORDER BY id LIMIT ?",
                                       (limit,)).fetchall()
            except sqlite3.Error:
                return []
        out = []
        for r in rows:
            try:
                out.append((r["id"], json.loads(r["payload"])))
            except ValueError:
                self.mark_sent(r["id"])      # unreadable row: drop it rather than block the queue
        return out

    def mark_sent(self, upto_id: int):
        with self._lock:
            try:
                self.db.execute("UPDATE events SET sent=1 WHERE id<=? AND sent=0", (upto_id,))
            except sqlite3.Error as e:
                log.debug("mark_sent: %s", e)

    def prune(self):
        now = time.time()
        with self._lock:
            try:
                self.db.execute("DELETE FROM events WHERE sent=1 AND at < ?", (now - KEEP_SENT,))
                self.db.execute("DELETE FROM events WHERE at < ?", (now - KEEP_ANY,))
                n = self.db.execute("SELECT COUNT(*) n FROM events").fetchone()["n"]
                if n > MAX_ROWS:             # something is badly wrong (server gone for weeks): keep the newest
                    self.db.execute("DELETE FROM events WHERE id NOT IN "
                                    "(SELECT id FROM events ORDER BY id DESC LIMIT ?)", (MAX_ROWS,))
            except sqlite3.Error as e:
                log.debug("prune: %s", e)

    def close(self):
        with self._lock:
            try:
                self.db.close()
            except sqlite3.Error:
                pass
