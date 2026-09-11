"""Pushes machine status to iPhones through APNs:

* Live Activity updates   - lock screen / Dynamic Island stay current with the app closed
* Live Activity push-to-start (iOS 17.2+) - starts a new Live Activity by itself, e.g. after iOS's 8-hour limit
* Alert notifications     - new alarms, machine stopped, machine off (per-device preferences)
"""
from __future__ import annotations

import json
import logging
import threading
import time

log = logging.getLogger("hanwha-server.push")

HEARTBEAT = 10 * 60            # refresh an unchanged Live Activity this often (keeps it from going stale)
STALE_AFTER = 15 * 60          # stale-date sent with every update
ROLLOVER_AGE = 7.6 * 3600      # iOS ends Live Activities at 8 h - replace ours a bit before
MIN_GAP_LOW_PRIORITY = 5       # seconds between routine updates to the same Live Activity
START_THROTTLE = 10 * 60       # don't push-to-start more than this often


class PushService:
    def __init__(self, db, lock, apns, status_fn, kv_get, kv_set):
        self.db, self.lock, self.apns = db, lock, apns
        self.status_fn, self.kv_get, self.kv_set = status_fn, kv_get, kv_set
        with lock:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS push_tokens (
                    token TEXT PRIMARY KEY, kind TEXT NOT NULL, activity_id TEXT, env TEXT DEFAULT 'production',
                    prefs TEXT, created REAL, updated REAL, last_content TEXT, last_push REAL);
            """)
            cols = {r[1] for r in db.execute("PRAGMA table_info(push_tokens)")}
            if "topic" not in cols:
                db.execute("ALTER TABLE push_tokens ADD COLUMN topic TEXT")
        self._wake = threading.Event()
        self._tick_lock = threading.Lock()   # the background loop and manual calls must not overlap
        self.last_error = ""
        self.sent = 0
        threading.Thread(target=self._loop, name="push", daemon=True).start()

    # ------------------------------------------------------------------ registry
    def register(self, kind: str, token: str, activity_id: str | None = None, env: str | None = None,
                 prefs: dict | None = None, topic: str | None = None) -> dict:
        if kind not in ("alert", "la", "la_start") or not token or len(token) > 400:
            raise ValueError("bad kind or token")
        now = time.time()
        with self.lock:
            row = self.db.execute("SELECT token FROM push_tokens WHERE token=?", (token,)).fetchone()
            if row:
                self.db.execute("UPDATE push_tokens SET kind=?, activity_id=COALESCE(?, activity_id), env=COALESCE(?, env),"
                                " prefs=COALESCE(?, prefs), topic=COALESCE(?, topic), updated=? WHERE token=?",
                                (kind, activity_id, env, json.dumps(prefs) if prefs is not None else None, topic, now, token))
            else:
                self.db.execute("INSERT INTO push_tokens(token,kind,activity_id,env,prefs,topic,created,updated)"
                                " VALUES(?,?,?,?,?,?,?,?)",
                                (token, kind, activity_id, env or "production", json.dumps(prefs or {}), topic, now, now))
            if kind == "la" and activity_id:
                # a new token for an activity replaces its old one
                self.db.execute("DELETE FROM push_tokens WHERE kind='la' AND activity_id=? AND token<>?", (activity_id, token))
        log.info("Registered %s token %s…", kind, token[:10])
        self._wake.set()
        return {"ok": True, "push_enabled": self.apns.enabled}

    def unregister(self, token: str = "", activity_id: str = ""):
        with self.lock:
            if token:
                self.db.execute("DELETE FROM push_tokens WHERE token=?", (token,))
            if activity_id:
                self.db.execute("DELETE FROM push_tokens WHERE kind='la' AND activity_id=?", (activity_id,))

    def info(self) -> dict:
        with self.lock:
            counts = {r["kind"]: r["n"] for r in self.db.execute("SELECT kind, COUNT(*) n FROM push_tokens GROUP BY kind")}
        return {"enabled": self.apns.enabled, "error": self.apns.error if not self.apns.enabled else self.last_error,
                "topic": self.apns.topic, "tokens": counts, "sent": self.sent}

    def poke(self):
        self._wake.set()

    def test_alert(self) -> dict:
        with self.lock:
            rows = self.db.execute("SELECT * FROM push_tokens WHERE kind='alert'").fetchall()
        ok = sum(self._send(r, {"aps": {"alert": {"title": "HiCPS-2 test", "body": "Push notifications are working."},
                                        "sound": "default"}}, "alert", 10) for r in rows)
        return {"ok": ok > 0, "sent": ok, "devices": len(rows), "error": self.last_error if ok == 0 else ""}

    # ------------------------------------------------------------------ content
    @staticmethod
    def content_from(s: dict) -> dict:
        prog = s.get("program") or {}
        title = prog.get("name") or (f"O{prog['number']:04d}" if prog.get("number") else "")
        c = {
            "state": s["state"],
            "detail": s.get("state_detail") or "",
            "program": title,
            "alarms": [f"{a.get('code') or 'ALARM'} {(a.get('message') or '').strip()}".strip()
                       for a in (s.get("active_alarms") or [])[:3]],
            "reachable": True,
            "updatedEpoch": round(time.time(), 1),
        }
        for key, src in (("parts", "parts"), ("required", "parts_required"), ("lastCycle", "last_cycle_s"),
                         ("cycleStartEpoch", "cycle_started_at")):
            if s.get(src) is not None:
                c[key] = s[src]
        if s.get("work_counter") is not None:
            c["counterStop"] = bool(s["work_counter"])
        if s.get("bar_change"):   # state stays "running" so older app builds still decode it
            c["barChange"] = True
            if s.get("bar_change_since"):
                c["barChangeStartEpoch"] = s["bar_change_since"]
        return c

    @staticmethod
    def _compare_key(c: dict) -> str:
        k = {x: y for x, y in c.items() if x != "updatedEpoch"}
        if k.get("cycleStartEpoch"):
            k["cycleStartEpoch"] = round(k["cycleStartEpoch"] / 5)
        return json.dumps(k, sort_keys=True)

    # ------------------------------------------------------------------ sending
    def _send(self, row, payload: dict, push_type: str, priority: int, collapse: str | None = None) -> bool:
        env = row["env"] or "production"
        topic = row["topic"] if "topic" in row.keys() else None
        status, reason = self.apns.send(row["token"], payload, push_type, env=env, priority=priority,
                                        collapse_id=collapse, topic=topic)
        if status == 400 and reason == "BadDeviceToken":
            other = "sandbox" if env == "production" else "production"
            status, reason = self.apns.send(row["token"], payload, push_type, env=other, priority=priority,
                                            collapse_id=collapse, topic=topic)
            if status == 200:
                with self.lock:
                    self.db.execute("UPDATE push_tokens SET env=? WHERE token=?", (other, row["token"]))
        if status == 200:
            self.sent += 1
            return True
        self.last_error = f"{push_type} → HTTP {status} {reason}"
        log.warning("APNs %s push failed for %s…: %s %s", push_type, row["token"][:10], status, reason)
        if status in (400, 410) and reason in ("BadDeviceToken", "Unregistered", "ExpiredToken", "DeviceTokenNotForTopic"):
            self.unregister(token=row["token"])
        return False

    def _loop(self):
        while True:
            self._wake.wait(timeout=15)
            self._wake.clear()
            if not self.apns.enabled:
                continue
            try:
                self.tick()
            except Exception:
                log.exception("push tick failed")

    def tick(self):
        with self._tick_lock:
            self._tick()

    def _tick(self):
        s = self.status_fn()
        now = time.time()
        content = self.content_from(s)
        key = self._compare_key(content)
        machine = s.get("machine_name") or "Hanwha XE35"
        prev_state = self.kv_get("push_last_state")
        state_changed = prev_state is not None and prev_state != s["state"]

        # ---- alarm + state alerts -------------------------------------------------
        notified = set(self.kv_get("push_notified_alarms", []) or [])
        active_ids = [a["id"] for a in s.get("active_alarms") or []]
        new_alarms = [a for a in (s.get("active_alarms") or []) if a["id"] not in notified
                      and now - a["started_at"] < 15 * 60]
        with self.lock:
            alert_rows = self.db.execute("SELECT * FROM push_tokens WHERE kind='alert'").fetchall()
        for row in alert_rows:
            prefs = json.loads(row["prefs"] or "{}")
            if prefs.get("alarms", True):
                for a in new_alarms:
                    body = (a.get("message") or a.get("type_name") or "Alarm").strip()
                    if a.get("path_name"):
                        body += f" · {a['path_name']} path"
                    self._send(row, {"aps": {"alert": {"title": f"⚠️ {machine} — {a.get('code') or 'ALARM'}", "body": body},
                                             "sound": "default", "interruption-level": "time-sensitive",
                                             "thread-id": "alarms"}}, "alert", 10, collapse=a["id"])
            if state_changed and prefs.get("stopped") and prev_state == "running" and s["state"] == "standby":
                self._send(row, {"aps": {"alert": {"title": f"{machine} stopped", "body": s.get("state_detail") or "Standby"},
                                         "sound": "default", "thread-id": "state"}}, "alert", 10)
            if state_changed and prefs.get("off") and s["state"] == "off":
                self._send(row, {"aps": {"alert": {"title": f"{machine} is off", "body": s.get("state_detail") or ""},
                                         "sound": "default", "thread-id": "state"}}, "alert", 10)
        if new_alarms or set(active_ids) != notified:
            self.kv_set("push_notified_alarms", sorted(set(active_ids) | {a["id"] for a in new_alarms}))
        self.kv_set("push_last_state", s["state"])

        # ---- Live Activity updates -----------------------------------------------
        with self.lock:
            la_rows = self.db.execute("SELECT * FROM push_tokens WHERE kind='la'").fetchall()
        alert = None
        if new_alarms:
            a = new_alarms[0]
            alert = {"title": f"{machine} alarm", "body": f"{a.get('code') or ''} {(a.get('message') or '').strip()}".strip(),
                     "sound": "default"}
        for row in la_rows:
            age = now - (row["created"] or now)
            if age > ROLLOVER_AGE:
                # end the old one; push-to-start below brings up a fresh one
                self._send(row, {"aps": {"timestamp": int(now), "event": "end", "content-state": content,
                                         "dismissal-date": int(now)}}, "liveactivity", 10)
                self.unregister(token=row["token"])
                self.kv_set("push_rollover_pending", True)
                continue
            last_key = row["last_content"]
            changed = last_key != key
            due = now - (row["last_push"] or 0) > HEARTBEAT
            if not (changed or due):
                continue
            prev_c = json.loads(last_key) if last_key else {}
            important = state_changed or bool(new_alarms) or (last_key and (
                prev_c.get("state") != content["state"] or bool(prev_c.get("barChange")) != bool(content.get("barChange"))))
            if not important and now - (row["last_push"] or 0) < MIN_GAP_LOW_PRIORITY:
                continue
            aps = {"timestamp": int(now), "event": "update", "content-state": content,
                   "stale-date": int(now + STALE_AFTER)}
            if alert and important:
                aps["alert"] = alert
            if self._send(row, {"aps": aps}, "liveactivity", 10 if important else 5):
                with self.lock:
                    self.db.execute("UPDATE push_tokens SET last_content=?, last_push=? WHERE token=?", (key, now, row["token"]))

        # ---- push-to-start when no Live Activity is running ----------------------
        with self.lock:
            has_la = self.db.execute("SELECT COUNT(*) n FROM push_tokens WHERE kind='la'").fetchone()["n"] > 0
            start_rows = self.db.execute("SELECT * FROM push_tokens WHERE kind='la_start'").fetchall()
        rollover = bool(self.kv_get("push_rollover_pending", False))
        last_start = float(self.kv_get("push_last_start", 0) or 0)
        if not has_la and start_rows and (rollover or state_changed or new_alarms) and now - last_start > START_THROTTLE:
            payload = {"aps": {
                "timestamp": int(now), "event": "start", "content-state": content,
                "attributes-type": "MachineActivityAttributes", "attributes": {"machineName": machine},
                "stale-date": int(now + STALE_AFTER),
                "input-push-token": 1,   # iOS 18+: ask for an update token for the new activity
                "alert": {"title": machine, "body": f"{s['state'].capitalize()} · {content.get('parts', '—')}"
                          + (f"/{content['required']}" if content.get("required") else "")},
            }}
            for row in start_rows:
                self._send(row, payload, "liveactivity", 10)
            self.kv_set("push_last_start", now)
            self.kv_set("push_rollover_pending", False)
