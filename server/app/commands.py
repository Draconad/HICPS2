"""Relays commands (camera pan/tilt, Controls tab) from the app/dashboard to the PC agent and the result back.

The agent long-polls GET /api/agent/commands; a viewer's POST /api/commands wakes it immediately and waits a few
seconds for the agent's result. Commands expire after COMMAND_TTL seconds, so nothing stale (a machine change
from minutes ago) ever runs when the PC reconnects.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid

log = logging.getLogger("hanwha-server.commands")

COMMAND_TTL = 10
ALLOWED = {"ptz", "ptz_preset", "set_required", "set_work_counter"}


class Commands:
    def __init__(self):
        self.cv = threading.Condition()
        self.pending: list[dict] = []
        self.results: dict[str, dict] = {}
        self.agent_seen = 0.0

    @property
    def agent_listening(self) -> bool:
        return time.time() - self.agent_seen < 40

    def submit(self, cmd: dict) -> str:
        cid = uuid.uuid4().hex[:12]
        cmd = dict(cmd, id=cid, at=time.time())
        with self.cv:
            self.pending.append(cmd)
            self.cv.notify_all()
        if cmd["type"] != "ptz":
            log.info("Command %s queued: %s", cmd["type"], {k: v for k, v in cmd.items() if k not in ("id", "at")})
        return cid

    def take(self, wait: float) -> list[dict]:
        self.agent_seen = time.time()
        with self.cv:
            self.cv.wait_for(lambda: bool(self.pending), timeout=wait)
            now = time.time()
            out = [c for c in self.pending if now - c["at"] < COMMAND_TTL]
            self.pending.clear()
        self.agent_seen = time.time()
        return out

    def put_result(self, cid: str, ok: bool, message: str):
        with self.cv:
            self.results[cid] = {"ok": bool(ok), "message": str(message)[:300], "at": time.time()}
            for k in [k for k, v in self.results.items() if time.time() - v["at"] > 60]:
                self.results.pop(k, None)
            self.cv.notify_all()

    def wait_result(self, cid: str, timeout: float) -> dict | None:
        with self.cv:
            self.cv.wait_for(lambda: cid in self.results, timeout=timeout)
            return self.results.pop(cid, None)
