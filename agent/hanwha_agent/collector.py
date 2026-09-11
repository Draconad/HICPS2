"""Polls the lathe and turns raw FOCAS reads into a JSON snapshot for the server."""
from __future__ import annotations

import logging
import queue
import re
import threading
import time
import uuid
from dataclasses import dataclass, field

from .config import VERSION, Config
from .focas import FocasError, FocasMachine, MockMachine, RawAlarm, parse_signal

log = logging.getLogger("hanwha.collector")

FAILS_BEFORE_OFF = 3


@dataclass
class Episode:
    """One occurrence of an alarm, from first seen to cleared."""
    id: str
    key: str
    path: int
    path_name: str
    code: str
    type: int
    type_name: str
    number: int
    axis: int
    message: str
    started_at: float
    cleared_at: float | None = None

    def to_json(self) -> dict:
        return dict(self.__dict__)


@dataclass
class CollectorState:
    machine_connected: bool = False
    last_poll_ok: float | None = None
    last_error: str = ""
    snapshot: dict = field(default_factory=dict)


class Collector:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.state = CollectorState()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.machine = (MockMachine() if cfg.demo_mode else
                        FocasMachine(cfg.machine_ip, cfg.machine_port, cfg.connect_timeout, cfg.dll_path or None))
        self._fails = 0
        self._active: dict[str, Episode] = {}
        self._pending_cleared: list[Episode] = []
        # part / cycle tracking
        self._last_parts: int | None = None
        self._last_part_time: float | None = None
        self._uninterrupted = False
        self._last_cycle_s: float | None = None
        self._prev_timer: float | None = None
        self._comment_cache: dict[int, tuple[str, float]] = {}
        self._last_state: str | None = None
        # bar change detection
        self._bar_in_cycle = False          # a bar change happened since the last part was counted
        self._bar_since: float | None = None
        self._bar_methods_failed: set[str] = set()
        self._work_counter: bool | None = None
        self._jobs: queue.Queue = queue.Queue()
        self.camera_presets: list[dict] = []          # the camera's saved positions (filled in by CommandClient)   # machine writes from the app, run on this thread between polls
        self._wc_error = ""
        self.listeners: list = []   # callables(snapshot)

    # ------------------------------------------------------------------
    def start(self):
        self._thread = threading.Thread(target=self._run, name="collector", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.cfg.connect_timeout + 3)
        try:
            self.machine.close()
        except Exception:
            pass

    def ack(self, ids: set[str]):
        """Server confirmed these cleared alarm episodes - stop resending them."""
        with self._lock:
            self._pending_cleared = [e for e in self._pending_cleared if e.id not in ids]

    # ------------------------------------------------------------------
    def _run(self):
        while not self._stop.is_set():
            started = time.time()
            snap = self.poll_once()
            for fn in list(self.listeners):
                try:
                    fn(snap)
                except Exception:
                    log.exception("listener failed")
            interval = self.cfg.poll_interval if self.state.machine_connected else max(self.cfg.poll_interval, 5)
            # wait for the next poll, running any machine writes that come in meanwhile
            while not self._stop.is_set():
                left = interval - (time.time() - started)
                if left <= 0:
                    break
                try:
                    fn, done, box = self._jobs.get(timeout=min(left, 0.5))
                except queue.Empty:
                    continue
                try:
                    if not self.state.machine_connected:
                        raise RuntimeError("The machine isn't connected.")
                    box["result"] = fn(self.machine)
                except Exception as e:  # noqa: BLE001 - reported back to the app
                    box["error"] = e
                finally:
                    done.set()
                break   # poll again straight away so the app sees the change

    def run_on_machine(self, fn, timeout: float = 10):
        """Run fn(machine) on the polling thread (FOCAS handles aren't thread-safe) and return its result."""
        done, box = threading.Event(), {}
        self._jobs.put((fn, done, box))
        if not done.wait(timeout):
            raise TimeoutError("The machine didn't respond in time.")
        if "error" in box:
            raise box["error"]
        return box.get("result")

    def poll_once(self) -> dict:
        now = time.time()
        try:
            if not self.machine.connected:
                self.machine.connect()
            data = self._read(now)
            self._fails = 0
            if not self.state.machine_connected:
                log.info("Machine connection OK")
            self.state.machine_connected = True
            self.state.last_poll_ok = now
            self.state.last_error = ""
        except (FocasError, OSError, RuntimeError) as e:
            self._fails += 1
            self.state.last_error = str(e)
            if self._fails == 1 or self._fails % 30 == 0:
                log.warning("Machine read failed (%d in a row): %s", self._fails, e)
            try:
                self.machine.close()   # always reconnect on the next poll
            except Exception:
                pass
            if self._fails >= FAILS_BEFORE_OFF:
                if self.state.machine_connected:
                    log.warning("Machine unreachable - reporting OFF")
                self.state.machine_connected = False
                if self._active:   # alarms can't outlive the machine being switched off
                    self._update_alarms(self.state.last_poll_ok or now, [])
            data = None
        except Exception as e:  # pragma: no cover - never let the loop die
            log.exception("Unexpected error while polling")
            self.state.last_error = f"Unexpected: {e}"
            data = None

        snap = self._build_snapshot(now, data)
        with self._lock:
            self.state.snapshot = snap
        if snap["state"] != self._last_state:
            log.info("Machine state: %s -> %s (%s)", self._last_state, snap["state"], snap.get("state_detail", ""))
            self._last_state = snap["state"]
        return snap

    # ------------------------------------------------------------------
    def _read(self, now: float) -> dict:
        m, cfg = self.machine, self.cfg
        paths = []
        alarms: list[tuple[int, str, RawAlarm]] = []
        for num, name in cfg.paths:
            st = m.status(num)
            paths.append({"path": num, "name": name, "mode": st.mode, "run": st.run, "run_code": st.run_code,
                          "emergency": st.emergency, "alarm": st.alarm})
            for a in m.alarms(num):
                alarms.append((num, name, a))

        cp = cfg.count_path
        parts = required = total = None
        try:
            v = m.macro(cp, 3901)
            parts = int(v) if v is not None else None
            v = m.macro(cp, 3902)
            required = int(v) if v is not None else None
        except FocasError as e:
            if e.is_connection_error:
                raise
        if parts is None:
            try:
                parts = m.param_long(cp, 6711)
                required = m.param_long(cp, 6713)
            except FocasError as e:
                if e.is_connection_error:
                    raise
        try:
            total = m.param_long(cp, 6712)
        except FocasError as e:
            if e.is_connection_error:
                raise

        timer = None
        try:
            timer = m.cycle_timer(cp)
        except FocasError as e:
            if e.is_connection_error:
                raise

        prog_num, prog_name, comment = None, "", ""
        try:
            prog_num, prog_name = m.program(cp)
            if prog_num:
                comment = self._comment(prog_num)
        except FocasError as e:
            if e.is_connection_error:
                raise

        wc = None
        sig = parse_signal(cfg.work_counter_signal)
        if sig:
            try:
                wc = m.read_signal(sig)
                self._wc_error = ""
            except FocasError as e:
                if e.is_connection_error:
                    raise
                if str(e) != self._wc_error:
                    log.warning("Can't read work counter signal %s: %s", cfg.work_counter_signal, e)
                self._wc_error = str(e)
        elif cfg.work_counter_signal.strip() and not self._wc_error:
            self._wc_error = "invalid"
            log.warning("Work counter signal '%s' isn't a valid address (examples: K5.3, R120.1, #512)",
                        cfg.work_counter_signal)
        if wc is not None and wc != self._work_counter:
            log.info("Work counter stop %s", "ENABLED" if wc else "disabled")
        self._work_counter = wc

        bar, how = False, ""
        if (cfg.bar_change_program or cfg.bar_change_mcode) and any(p["run_code"] in (3, 4) for p in paths):
            bar, how = self._detect_bar_change(paths)

        return {"paths": paths, "alarms": alarms, "parts": parts, "required": required, "total": total,
                "timer": timer, "program": {"number": prog_num, "name": prog_name, "comment": comment},
                "bar_change": bar, "bar_how": how, "work_counter": wc}

    def _detect_bar_change(self, paths: list[dict]) -> tuple[bool, str]:
        """True while the bar-change subprogram (O9002) is executing on a running path - optionally also while a
        bar-change M code is the active block (checked two ways: the active M codes and the block's text)."""
        prog = int(self.cfg.bar_change_program or 0)
        code = int(self.cfg.bar_change_mcode or 0)
        pattern = re.compile(rf"(?<![A-Z#])M0*{code}(?!\d)", re.I)
        hows = []
        for p in paths:
            if p["run_code"] not in (3, 4):
                continue
            num, name = p["path"], p["name"]
            if prog and "program" not in self._bar_methods_failed:
                try:
                    running, _main = self.machine.program_numbers(num)
                    if running == prog:
                        hows.append(f"{name}: O{prog:04d} running")
                except FocasError as e:
                    if e.is_connection_error:
                        raise
                    self._bar_methods_failed.add("program")
                    log.info("Bar change: can't read the running program number (%s)", e)
            if not code:
                continue
            if "mcode" not in self._bar_methods_failed:
                try:
                    ms = self.machine.active_mcodes(num)
                    # respect the "commanded in this block" flag if the control sets it on anything
                    uses_flag = any(f & 0x8000 for _, f in ms)
                    if any(v == code and (f & 0x8000 or not uses_flag) for v, f in ms):
                        hows.append(f"{name}: M{code} active")
                except FocasError as e:
                    if e.is_connection_error:
                        raise
                    self._bar_methods_failed.add("mcode")
                    log.info("Bar change: can't read active M codes (%s) - using the program text only", e)
            if "block" not in self._bar_methods_failed:
                try:
                    line = self.machine.exec_block(num)
                    if line and pattern.search(re.sub(r"\(.*?\)", "", line)):
                        hows.append(f"{name}: '{line[:40]}'")
                except FocasError as e:
                    if e.is_connection_error:
                        raise
                    self._bar_methods_failed.add("block")
                    log.info("Bar change: can't read the executing block (%s) - using active M codes only", e)
        return bool(hows), "; ".join(hows)

    def _comment(self, number: int) -> str:
        cached = self._comment_cache.get(number)
        if cached and (cached[0] or time.time() - cached[1] < 300):
            return cached[0]
        try:
            c = self.machine.program_comment(self.cfg.count_path, number)
        except FocasError as e:
            if e.is_connection_error:
                raise
            c = ""
        self._comment_cache[number] = (c, time.time())
        return c

    # ------------------------------------------------------------------
    def _update_alarms(self, now: float, alarms: list[tuple[int, str, RawAlarm]]):
        seen: set[str] = set()
        for path, pname, a in alarms:
            key = f"{path}:{a.type}:{a.number}:{a.axis}"
            if key in seen:
                continue
            seen.add(key)
            ep = self._active.get(key)
            if ep is None:
                ep = Episode(id=uuid.uuid4().hex, key=key, path=path, path_name=pname, code=a.code, type=a.type,
                             type_name=a.type_name, number=a.number, axis=a.axis, message=a.message, started_at=now)
                self._active[key] = ep
                log.warning("ALARM %s [%s] %s", ep.code, pname, ep.message)
            else:
                ep.message = a.message or ep.message
        for key in list(self._active):
            if key not in seen:
                ep = self._active.pop(key)
                ep.cleared_at = now
                log.info("Alarm cleared %s [%s] %s", ep.code, ep.path_name, ep.message)
                with self._lock:
                    self._pending_cleared.append(ep)
                    self._pending_cleared = self._pending_cleared[-200:]

    def _update_cycle(self, now: float, running: bool, parts: int | None, timer: float | None, bar: bool = False):
        if not running:
            self._uninterrupted = False
        if bar:
            self._bar_in_cycle = True
        if parts is not None:
            if self._last_parts is not None and parts > self._last_parts:
                k = parts - self._last_parts
                if self._bar_in_cycle:
                    # this part's time includes a bar change - don't let it skew the cycle time
                    self._bar_in_cycle = bar
                    log.info("Part counted after a bar change - keeping the previous cycle time")
                elif self._last_part_time is not None and self._uninterrupted and k <= 5:
                    self._last_cycle_s = round((now - self._last_part_time) / k, 1)
                elif self._prev_timer and self._prev_timer > 1:
                    # fall back to the CNC cycle timer value just before the part was counted
                    self._last_cycle_s = round(self._prev_timer, 1)
                self._last_part_time = now
                self._uninterrupted = running
            elif self._last_parts is not None and parts < self._last_parts:
                self._last_part_time = None  # counter reset by operator
            self._last_parts = parts
        # CNC cycle timer resets at cycle start: until we have a part-to-part time, use the finished timer value
        if timer is not None:
            if self._prev_timer is not None and timer + 0.5 < self._prev_timer and self._prev_timer > 1 \
                    and self._last_part_time is None:
                self._last_cycle_s = round(self._prev_timer, 1)
            self._prev_timer = timer

    def _build_snapshot(self, now: float, data: dict | None) -> dict:
        cfg = self.cfg
        base = {
            "agent_version": VERSION,
            "machine_name": cfg.machine_name,
            "sent_at": now,
            "demo": cfg.demo_mode,
            "machine_connected": self.state.machine_connected,
            # what the app may offer (the PC decides - nothing is written unless "Allow remote changes" is ticked)
            "controls": {"remote": bool(cfg.remote_control),
                         "work_counter_signal": bool(parse_signal(cfg.work_counter_signal))},
            "camera_features": {"ptz": bool(cfg.camera_enabled and cfg.camera_ptz),
                                "audio": bool(cfg.camera_enabled and cfg.camera_audio),
                                "presets": self.camera_presets if cfg.camera_enabled and cfg.camera_ptz else []},
        }
        if data is None:
            # Keep reporting the last known values but flag the machine as off / unreachable
            prev = self.state.snapshot or {}
            state = prev.get("state", "off") if self.state.machine_connected else "off"
            base.update({k: prev.get(k) for k in ("parts", "parts_required", "parts_total", "last_cycle_s", "program",
                                                  "paths", "work_counter")})
            base.update({
                "state": state,
                "state_detail": "Machine off / unreachable" if state == "off" else prev.get("state_detail", ""),
                "cycle_timer_s": None,
                "error": self.state.last_error,
                "alarms": [e.to_json() for e in self._active.values()] + [e.to_json() for e in self._pending_cleared],
            })
            return base

        paths = data["paths"]
        running = any(p["run_code"] in (3, 4) for p in paths)
        emergency = any(p["emergency"] for p in paths)
        self._update_alarms(now, data["alarms"])
        bar = bool(data.get("bar_change")) and running
        self._update_cycle(now, running, data["parts"], data["timer"], bar)
        if bar and self._bar_since is None:
            self._bar_since = now
            log.info("Bar change started (%s)", data.get("bar_how"))
        elif not bar and self._bar_since is not None:
            log.info("Bar change finished after %.0f s", now - self._bar_since)
            self._bar_since = None

        if self._active or emergency or any(p["alarm"] for p in paths):
            state = "alarm"
            if self._active:
                first = next(iter(self._active.values()))
                detail = f"{first.code} {first.message}".strip()
            else:
                detail = "Emergency stop" if emergency else "Alarm"
        elif running:
            state = "running"
            detail = ("Bar change" if bar else
                      " / ".join(f"{p['name']} {p['mode']} {p['run']}" for p in paths))
        else:
            state = "standby"
            runs = {p["run"] for p in paths}
            detail = "Feed hold" if "HOLD" in runs else ("Stopped" if "STOP" in runs else "Reset / idle")

        with self._lock:
            alarm_payload = [e.to_json() for e in self._active.values()] + [e.to_json() for e in self._pending_cleared]
        base.update({
            "state": state,
            "state_detail": detail,
            "parts": data["parts"],
            "parts_required": data["required"] or None,
            "parts_total": data["total"],
            "cycle_timer_s": round(data["timer"], 1) if data["timer"] is not None else None,
            "last_cycle_s": self._last_cycle_s,
            "program": data["program"],
            "paths": [{k: v for k, v in p.items() if k != "run_code"} for p in paths],
            "alarms": alarm_payload,
            "error": "",
            "bar_change": bar and state == "running",
            "bar_change_how": data.get("bar_how") if bar else "",
            "work_counter": data.get("work_counter"),   # None = not configured / unreadable
        })
        return base

    @property
    def active_alarms(self) -> list[Episode]:
        return list(self._active.values())
