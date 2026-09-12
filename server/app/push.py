"""Pushes machine status to iPhones through APNs:

* Live Activity updates   - lock screen / Dynamic Island stay current with the app closed
* Live Activity push-to-start (iOS 17.2+) - starts a new Live Activity by itself, e.g. after iOS's 8-hour limit
* Alert notifications     - new alarms, machine stopped, machine off (per-device preferences)
"""
from __future__ import annotations

import json
import os
import logging
import threading
import time

log = logging.getLogger("hanwha-server.push")

HEARTBEAT = 10 * 60            # refresh an unchanged Live Activity this often (keeps it from going stale)
STALE_AFTER = 15 * 60          # stale-date sent with every update
ROLLOVER_AGE = 7.6 * 3600      # iOS ends Live Activities at 8 h - replace ours a bit before
MIN_GAP_LOW_PRIORITY = 5       # seconds between routine updates to the same Live Activity
START_THROTTLE = 10 * 60       # don't push-to-start more than this often
# devices with "only while running" on still get everything for this long after the machine stops,
# so the alarm that stopped it (or one right after) still comes through
RUNNING_GRACE = float(os.environ.get("PUSH_RUNNING_GRACE", "15"))
# the machine has been idle this long: end the Live Activity rather than leave a stale card on the lock screen
# (the app starts a new one when it's opened, and push-to-start brings one back when the machine runs again)
LA_IDLE_END = float(os.environ.get("LA_IDLE_END", "600"))


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
        title = prog.get("label") or prog.get("name") or (f"O{prog['number']:04d}" if prog.get("number") else "")
        c = {
            "state": s["state"],
            "detail": s.get("state_detail") or "",
            "program": title,
            "alarms": [f"{a.get('code') or 'ALARM'} {(a.get('message') or '').strip()}".strip()
                       for a in (s.get("active_alarms") or [])[:3]],
            "reachable": True,
            "updatedEpoch": round(time.time(), 1),
        }
        msgs = s.get("messages") or []
        if msgs:
            c["message"] = msgs[0].get("text") or ""
        if s.get("finish_at"):
            c["finishEpoch"] = round(s["finish_at"] / 60) * 60   # to the minute: no push for every second it shifts
        for key, src in (("parts", "parts"), ("required", "parts_required"), ("lastCycle", "last_cycle_s"),
                         ("cycleStartEpoch", "cycle_started_at")):
            if s.get(src) is not None:
                c[key] = s[src]
        if s.get("work_counter") is not None:
            c["counterStop"] = bool(s["work_counter"])
        if s.get("over_producing"):
            c["overProducing"] = True
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
        state_since = s.get("state_since") or now
        # "machine off" only after it has stayed off a little while: the monitor PC restarting (e.g. for an update)
        # briefly looks like off, and that isn't worth a notification
        off_for = (now - state_since) if s["state"] == "off" else 0
        notify_off = off_for > 25 and self.kv_get("push_off_alert_for") != round(state_since)
        ended = s.get("running_ended_at")
        in_window = s["state"] == "running" or bool(ended and now - ended <= RUNNING_GRACE)

        def muted(row) -> bool:
            """This device only wants notifications / Live Activity updates while the machine is running."""
            try:
                return bool(json.loads(row["prefs"] or "{}").get("running_only")) and not in_window
            except (ValueError, TypeError):
                return False

        # ---- alarm + state alerts -------------------------------------------------
        notified = set(self.kv_get("push_notified_alarms", []) or [])
        active_ids = [a["id"] for a in s.get("active_alarms") or []]
        new_alarms = [a for a in (s.get("active_alarms") or []) if a["id"] not in notified
                      and now - a["started_at"] < 15 * 60]
        with self.lock:
            alert_rows = self.db.execute("SELECT * FROM push_tokens WHERE kind='alert'").fetchall()
        alert_rows = [r for r in alert_rows if not muted(r)]
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
            if notify_off and prefs.get("off"):
                self._send(row, {"aps": {"alert": {"title": f"{machine} is off", "body": s.get("state_detail") or ""},
                                         "sound": "default", "thread-id": "state"}}, "alert", 10)
        if notify_off:
            self.kv_set("push_off_alert_for", round(state_since))
        # ---- operator messages (e.g. "work count end in 1 hour") - not alarms, but worth a notification
        msg_notified = set(self.kv_get("push_notified_messages", []) or [])
        active_msgs = s.get("messages") or []
        new_msgs = [m for m in active_msgs if m["id"] not in msg_notified and now - m["started_at"] < 15 * 60]
        for row in alert_rows:
            prefs = json.loads(row["prefs"] or "{}")
            if prefs.get("messages", True):
                for m in new_msgs:
                    self._send(row, {"aps": {"alert": {"title": f"💬 {machine}", "body": m.get("text") or f"Message {m.get('number')}"},
                                             "sound": "default", "thread-id": "messages"}}, "alert", 10,
                               collapse=f"msg{m['id']}")
        if new_msgs or {m["id"] for m in active_msgs} != msg_notified:
            self.kv_set("push_notified_messages", sorted({m["id"] for m in active_msgs} | {m["id"] for m in new_msgs}))

        # ---- job complete (count reached the required count) --------------------------
        jc = s.get("job_complete") or {}
        if jc.get("at") and jc["at"] != self.kv_get("push_job_complete_at") and now - jc["at"] < 600:
            for row in alert_rows:
                if json.loads(row["prefs"] or "{}").get("complete", True):
                    self._send(row, {"aps": {"alert": {"title": f"✅ {machine}: job complete",
                                                       "body": f"{jc.get('parts')}/{jc.get('required')} parts"},
                                             "sound": "default", "thread-id": "job"}}, "alert", 10,
                               collapse=f"job{int(jc['at'])}")
            self.kv_set("push_job_complete_at", jc["at"])

        # ---- bar change taking unusually long (usually a failed bar load) ----------------
        since = s.get("bar_change_since")
        limit = (s.get("bar_changes") or {}).get("alert_after_s") or 180
        if s.get("bar_change") and since and now - since > limit and self.kv_get("push_bar_alert_for") != since:
            dur = int(now - since)
            for row in alert_rows:
                if json.loads(row["prefs"] or "{}").get("barchange", True):
                    self._send(row, {"aps": {"alert": {"title": f"⏳ {machine}: bar change taking long",
                                                       "body": f"{dur // 60} min {dur % 60} s so far"},
                                             "sound": "default", "interruption-level": "time-sensitive",
                                             "thread-id": "barchange"}}, "alert", 10, collapse=f"bar{int(since)}")
            self.kv_set("push_bar_alert_for", since)

        if new_alarms or set(active_ids) != notified:
            self.kv_set("push_notified_alarms", sorted(set(active_ids) | {a["id"] for a in new_alarms}))
        self.kv_set("push_last_state", s["state"])

        # ---- Live Activity updates -----------------------------------------------
        since = s.get("state_since") or now
        # only when the app isn't open - while it is, it starts and ends its own Live Activity
        app_away = now - float(s.get("app_seen") or 0) > 120
        idle = s["state"] in ("standby", "off") and LA_IDLE_END > 0 and now - since > LA_IDLE_END and app_away
        with self.lock:
            la_rows = self.db.execute("SELECT * FROM push_tokens WHERE kind='la'").fetchall()
        # rollover still applies to muted ones (iOS would end them at 8 h anyway); updates don't
        alert = None
        if new_alarms:
            a = new_alarms[0]
            alert = {"title": f"{machine} alarm", "body": f"{a.get('code') or ''} {(a.get('message') or '').strip()}".strip(),
                     "sound": "default"}
        for row in la_rows:
            if idle:
                # nothing is happening and the app isn't there to end it itself: take the card away
                log.info("Ending Live Activity: %s for %d min", s["state"], (now - since) / 60)
                self._send(row, {"aps": {"timestamp": int(now), "event": "end", "content-state": content,
                                         "dismissal-date": int(now)}}, "liveactivity", 10)
                self.unregister(token=row["token"])
                continue
            age = now - (row["created"] or now)
            if age > ROLLOVER_AGE:
                # end the old one; push-to-start below brings up a fresh one
                self._send(row, {"aps": {"timestamp": int(now), "event": "end", "content-state": content,
                                         "dismissal-date": int(now)}}, "liveactivity", 10)
                self.unregister(token=row["token"])
                self.kv_set("push_rollover_pending", True)
                continue
            if muted(row):
                continue
            last_key = row["last_content"]
            changed = last_key != key
            due = now - (row["last_push"] or 0) > HEARTBEAT
            if not (changed or due):
                continue
            prev_c = json.loads(last_key) if last_key else {}
            important = state_changed or bool(new_alarms) or (last_key and (
                prev_c.get("state") != content["state"] or bool(prev_c.get("barChange")) != bool(content.get("barChange"))
                or prev_c.get("message") != content.get("message")
                or bool(prev_c.get("overProducing")) != bool(content.get("overProducing"))))
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
        start_rows = [r for r in start_rows if not muted(r)]
        rollover = bool(self.kv_get("push_rollover_pending", False))
        last_start = float(self.kv_get("push_last_start", 0) or 0)
        if not has_la and start_rows and not idle and (rollover or state_changed or new_alarms) \
                and now - last_start > START_THROTTLE:
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
