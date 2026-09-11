"""Thin, safe ctypes wrapper around FANUC FOCAS (Fwlib32.dll) for the Hanwha XE35 (FANUC 0i-F).

Fixes vs. the original script:
  * Fwlib32 uses the WINAPI (stdcall) convention -> loaded with WinDLL, not cdll.
  * Every function returns a C `short`; restype is set so negative error codes
    come back as e.g. -16 instead of garbage like 65520 / 74383344.
  * The handle is freed and re-opened after any communication error
    (the old script never reconnected once the machine dropped off).
"""
from __future__ import annotations

import ctypes
import logging
import math
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("hanwha.focas")

# ---------------------------------------------------------------------------
# Error codes (fwlib32.h)
# ---------------------------------------------------------------------------
FOCAS_ERRORS = {
    -17: "EW_PROTOCOL (protocol error)",
    -16: "EW_SOCKET (socket / network error)",
    -15: "EW_NODLL (DLL missing for this CNC series)",
    -14: "EW_INIERR (library init error)",
    -11: "EW_BUS (bus error)",
    -10: "EW_SYSTEM2 (system error)",
    -9: "EW_HSSB (HSSB communication error)",
    -8: "EW_HANDLE (invalid handle)",
    -7: "EW_VERSION (CNC/PMC version mismatch)",
    -6: "EW_UNEXP (unexpected error)",
    -5: "EW_SYSTEM (system error)",
    -4: "EW_PARITY (shared RAM parity error)",
    -2: "EW_RESET (reset or stop occurred)",
    -1: "EW_BUSY (busy)",
    0: "EW_OK",
    1: "EW_FUNC (function not available)",
    2: "EW_LENGTH (data block length error)",
    3: "EW_NUMBER (data number error)",
    4: "EW_ATTRIB (data attribute error)",
    5: "EW_DATA (data error)",
    6: "EW_NOOPT (option not fitted)",
    7: "EW_PROT (write protected)",
    8: "EW_OVRFLOW (memory overflow)",
    9: "EW_PARAM (CNC parameter error)",
    10: "EW_BUFFER (buffer error)",
    11: "EW_PATH (path error)",
    12: "EW_MODE (CNC mode error)",
    13: "EW_REJECT (execution rejected)",
    14: "EW_DTSRVR (data server error)",
}

# Error codes that mean "the connection is dead, reconnect"
CONNECTION_ERRORS = {-16, -17, -15, -14, -11, -10, -9, -8, -6, -5, -4}


class FocasError(Exception):
    def __init__(self, func: str, code: int):
        self.func = func
        self.code = code
        super().__init__(f"{func} returned {code}: {FOCAS_ERRORS.get(code, 'unknown error')}")

    @property
    def is_connection_error(self) -> bool:
        return self.code in CONNECTION_ERRORS


# ---------------------------------------------------------------------------
# Structures (fwlib32.h, #pragma pack(4))
# ---------------------------------------------------------------------------
class ODBST(ctypes.Structure):  # cnc_statinfo, 0i/16i/18i/30i layout
    _pack_ = 4
    _fields_ = [
        ("hdck", ctypes.c_short),
        ("tmmode", ctypes.c_short),
        ("aut", ctypes.c_short),
        ("run", ctypes.c_short),
        ("motion", ctypes.c_short),
        ("mstb", ctypes.c_short),
        ("emergency", ctypes.c_short),
        ("alarm", ctypes.c_short),
        ("edit", ctypes.c_short),
    ]


class ODBALMMSG2(ctypes.Structure):  # cnc_rdalmmsg2
    _pack_ = 4
    _fields_ = [
        ("alm_no", ctypes.c_long),
        ("type", ctypes.c_short),
        ("axis", ctypes.c_short),
        ("dummy", ctypes.c_short),
        ("msg_len", ctypes.c_short),
        ("alm_msg", ctypes.c_char * 64),
    ]


class ODBM(ctypes.Structure):  # cnc_rdmacro
    _pack_ = 4
    _fields_ = [
        ("datano", ctypes.c_short),
        ("dummy", ctypes.c_short),
        ("mcr_val", ctypes.c_long),
        ("dec_val", ctypes.c_short),
    ]


class IODBTIME(ctypes.Structure):  # cnc_rdtimer
    _pack_ = 4
    _fields_ = [("minute", ctypes.c_long), ("msec", ctypes.c_long)]


class ODBEXEPRG(ctypes.Structure):  # cnc_exeprgname
    _pack_ = 4
    _fields_ = [("name", ctypes.c_char * 36), ("o_num", ctypes.c_long)]


class ODBPRO(ctypes.Structure):  # cnc_rdprgnum (short variant)
    _pack_ = 4
    _fields_ = [("dummy", ctypes.c_short * 2), ("data", ctypes.c_short), ("mdata", ctypes.c_short)]


class ODBCMD(ctypes.Structure):  # cnc_rdcommand (one commanded address, e.g. M92)
    _pack_ = 4
    _fields_ = [("adrs", ctypes.c_char), ("num", ctypes.c_char), ("flag", ctypes.c_short),
                ("cmd_val", ctypes.c_long), ("dec_val", ctypes.c_long)]


PMC_AREAS = {"G": 0, "F": 1, "Y": 2, "X": 3, "A": 4, "R": 5, "T": 6, "K": 7, "C": 8, "D": 9, "E": 12}
PMC_CHUNK = 200   # bytes per pmc_rdpmcrng call


class IODBPMC(ctypes.Structure):  # pmc_rdpmcrng, byte data
    _pack_ = 4
    _fields_ = [("type_a", ctypes.c_short), ("type_d", ctypes.c_short),
                ("datano_s", ctypes.c_ushort), ("datano_e", ctypes.c_ushort),
                ("cdata", ctypes.c_ubyte * PMC_CHUNK)]


_SIGNAL_RE = re.compile(r"^\s*(!?)\s*(?:([GFYXARTKCDE])\s*(\d+)(?:\.([0-7]))?|#\s*(\d+))\s*$", re.I)


def parse_signal(text: str):
    """'K5.3', '!R120.1', 'D200' (byte, non-zero = on) or '#512' (macro variable, non-zero = on).
    Returns (invert, area or '#', number, bit or None), or None if blank/invalid."""
    m = _SIGNAL_RE.match(text or "")
    if not m:
        return None
    inv = bool(m.group(1))
    if m.group(5):
        return inv, "#", int(m.group(5)), None
    return inv, m.group(2).upper(), int(m.group(3)), (int(m.group(4)) if m.group(4) is not None else None)


class _PrgDate(ctypes.Structure):
    _pack_ = 4
    _fields_ = [(n, ctypes.c_short) for n in ("year", "month", "day", "hour", "minute", "dummy")]


class PRGDIR3(ctypes.Structure):  # cnc_rdprogdir3
    _pack_ = 4
    _fields_ = [
        ("number", ctypes.c_long),
        ("length", ctypes.c_long),
        ("page", ctypes.c_long),
        ("comment", ctypes.c_char * 52),
        ("mdate", _PrgDate),
        ("cdate", _PrgDate),
    ]


class IODBPSD(ctypes.Structure):  # cnc_rdparam (non-axis long parameter)
    _pack_ = 4
    _fields_ = [("datano", ctypes.c_short), ("type", ctypes.c_short), ("ldata", ctypes.c_long),
                ("_pad", ctypes.c_long * 32)]


# ---------------------------------------------------------------------------
# Human readable tables
# ---------------------------------------------------------------------------
# cnc_rdalmmsg2 alarm types (FS0i-F) -> (short code, description)
ALARM_TYPES = {
    0: ("SW", "Parameter switch on"),
    1: ("PW", "Power off parameter set"),
    2: ("IO", "I/O error"),
    3: ("PS", "Program / operation"),
    4: ("OT", "Overtravel / external data"),
    5: ("OH", "Overheat"),
    6: ("SV", "Servo"),
    7: ("SR", "Data I/O"),
    8: ("MC", "Macro"),
    9: ("SP", "Spindle"),
    10: ("DS", "Other (DS)"),
    11: ("IE", "Malfunction prevention"),
    12: ("BG", "Background P/S"),
    13: ("SN", "Synchronised error"),
    15: ("EX", "External (machine) alarm"),
    19: ("PC", "PMC error"),
}

AUTO_MODES = {0: "MDI", 1: "MEM", 2: "----", 3: "EDIT", 4: "HANDLE", 5: "JOG",
              6: "TJOG", 7: "THND", 8: "INC", 9: "REF", 10: "RMT"}
RUN_STATES = {0: "RESET", 1: "STOP", 2: "HOLD", 3: "START", 4: "MSTR"}


@dataclass
class PathStatus:
    mode: str
    run: str
    run_code: int
    emergency: bool
    alarm: bool


@dataclass
class RawAlarm:
    number: int
    type: int
    axis: int
    message: str

    @property
    def code(self) -> str:
        prefix = ALARM_TYPES.get(self.type, ("AL", ""))[0]
        return f"{prefix}{self.number:04d}"

    @property
    def type_name(self) -> str:
        return ALARM_TYPES.get(self.type, ("", f"Type {self.type}"))[1]


# ---------------------------------------------------------------------------
# DLL loading
# ---------------------------------------------------------------------------
def _candidate_dll_paths(configured: str | None) -> list[Path]:
    cands: list[Path] = []
    if configured:
        p = Path(configured)
        cands.append(p if p.suffix.lower() == ".dll" else p / "Fwlib32.dll")
    exe_dir = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]
    cands += [exe_dir / "Fwlib32.dll", Path.cwd() / "Fwlib32.dll", Path(r"C:\Users\Hanwha\focas\Fwlib32.dll")]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        cands.append(Path(meipass) / "Fwlib32.dll")
    return cands


def find_dll(configured: str | None = None) -> Path | None:
    for p in _candidate_dll_paths(configured):
        if p.is_file():
            return p
    return None


_lib = None


def load_library(configured: str | None = None):
    global _lib
    if _lib is not None:
        return _lib
    if os.name != "nt":
        raise RuntimeError("FOCAS (Fwlib32.dll) only works on Windows. Enable Demo mode to test elsewhere.")
    if ctypes.sizeof(ctypes.c_void_p) != 4:
        raise RuntimeError("Fwlib32.dll is 32-bit: this program must be built/run with 32-bit Python.")
    dll = find_dll(configured)
    if dll is None:
        raise RuntimeError("Fwlib32.dll not found. Put Fwlib32.dll and fwlibe1.dll next to HanwhaMonitor.exe "
                           "or set the DLL folder in Settings.")
    dll_dir = str(dll.parent)
    # Fwlib32 loads fwlibe1.dll at runtime; make sure Windows can find it.
    os.environ["PATH"] = dll_dir + os.pathsep + os.environ.get("PATH", "")
    try:
        os.add_dll_directory(dll_dir)
    except (AttributeError, OSError):
        pass
    os.chdir(dll_dir)
    lib = ctypes.WinDLL(str(dll))  # WINAPI == stdcall on x86

    S, US, L = ctypes.c_short, ctypes.c_ushort, ctypes.c_long
    P = ctypes.POINTER
    sigs = {
        "cnc_allclibhndl3": [ctypes.c_char_p, US, L, P(US)],
        "cnc_freelibhndl": [US],
        "cnc_setpath": [US, S],
        "cnc_statinfo": [US, P(ODBST)],
        "cnc_alarm2": [US, P(L)],
        "cnc_rdalmmsg2": [US, S, P(S), P(ODBALMMSG2)],
        "cnc_rdmacro": [US, S, S, P(ODBM)],
        "cnc_rdtimer": [US, S, P(IODBTIME)],
        "cnc_exeprgname": [US, P(ODBEXEPRG)],
        "cnc_rdprgnum": [US, P(ODBPRO)],
        "cnc_rdprogdir3": [US, S, P(L), P(S), P(PRGDIR3)],
        "cnc_rdparam": [US, S, S, S, P(IODBPSD)],
        "cnc_rdcommand": [US, S, S, P(S), P(ODBCMD)],
        "cnc_rdexecprog": [US, P(US), P(S), P(ctypes.c_char)],
        "pmc_rdpmcrng": [US, S, S, US, US, US, P(IODBPMC)],
    }
    for name, args in sigs.items():
        fn = getattr(lib, name)
        fn.argtypes = args
        fn.restype = ctypes.c_short
    log.info("Loaded FOCAS library from %s", dll)
    _lib = lib
    return lib


def _decode(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("latin-1", errors="replace").strip()


# ---------------------------------------------------------------------------
# Real machine
# ---------------------------------------------------------------------------
class FocasMachine:
    def __init__(self, ip: str, port: int = 8193, timeout: int = 5, dll_path: str | None = None):
        self.ip, self.port, self.timeout = ip, int(port), int(timeout)
        self.dll_path = dll_path
        self.handle = ctypes.c_ushort(0)
        self.connected = False
        self._current_path: int | None = None

    # -- connection -----------------------------------------------------
    def _check(self, func: str, ret: int):
        if ret != 0:
            raise FocasError(func, ret)

    def connect(self):
        lib = load_library(self.dll_path)
        self.close()
        ret = lib.cnc_allclibhndl3(self.ip.encode(), self.port, self.timeout, ctypes.byref(self.handle))
        self._check("cnc_allclibhndl3", ret)
        self.connected = True
        self._current_path = None
        log.info("Connected to %s:%s (handle %s)", self.ip, self.port, self.handle.value)

    def close(self):
        if self.connected and _lib is not None:
            try:
                _lib.cnc_freelibhndl(self.handle)
            except Exception:  # pragma: no cover
                pass
        self.connected = False
        self._current_path = None

    # -- reads ----------------------------------------------------------
    def set_path(self, path: int):
        if self._current_path == path:
            return
        self._check("cnc_setpath", _lib.cnc_setpath(self.handle, path))
        self._current_path = path

    def status(self, path: int) -> PathStatus:
        self.set_path(path)
        st = ODBST()
        self._check("cnc_statinfo", _lib.cnc_statinfo(self.handle, ctypes.byref(st)))
        return PathStatus(mode=AUTO_MODES.get(st.aut, str(st.aut)), run=RUN_STATES.get(st.run, str(st.run)),
                          run_code=st.run, emergency=st.emergency == 1, alarm=st.alarm == 1)

    def alarms(self, path: int) -> list[RawAlarm]:
        self.set_path(path)
        bits = ctypes.c_long(0)
        self._check("cnc_alarm2", _lib.cnc_alarm2(self.handle, ctypes.byref(bits)))
        out: list[RawAlarm] = []
        for idx in range(32):
            if not bits.value & (1 << idx):
                continue
            buf = (ODBALMMSG2 * 10)()
            num = ctypes.c_short(10)
            self._check("cnc_rdalmmsg2", _lib.cnc_rdalmmsg2(self.handle, idx, ctypes.byref(num), buf))
            for a in buf[: max(0, min(num.value, 10))]:
                msg = a.alm_msg[: max(0, min(a.msg_len, 64))].decode("latin-1", errors="replace").strip()
                out.append(RawAlarm(number=a.alm_no, type=a.type, axis=a.axis, message=msg))
        return out

    def macro(self, path: int, number: int) -> float | None:
        self.set_path(path)
        m = ODBM()
        ret = _lib.cnc_rdmacro(self.handle, number, 10, ctypes.byref(m))
        if ret != 0:
            raise FocasError("cnc_rdmacro", ret)
        if m.mcr_val == 0 and m.dec_val == -1:  # <vacant>
            return None
        return m.mcr_val / (10 ** m.dec_val) if m.dec_val >= 0 else float(m.mcr_val)

    def param_long(self, path: int, number: int) -> int:
        self.set_path(path)
        p = IODBPSD()
        self._check("cnc_rdparam", _lib.cnc_rdparam(self.handle, number, 0, 8, ctypes.byref(p)))
        return p.ldata

    def cycle_timer(self, path: int) -> float:
        self.set_path(path)
        t = IODBTIME()
        self._check("cnc_rdtimer", _lib.cnc_rdtimer(self.handle, 3, ctypes.byref(t)))  # 3 = cycle time
        return t.minute * 60 + t.msec / 1000.0

    def program(self, path: int) -> tuple[int | None, str]:
        """Returns (O-number, name) of the executing program."""
        self.set_path(path)
        e = ODBEXEPRG()
        ret = _lib.cnc_exeprgname(self.handle, ctypes.byref(e))
        if ret == 0:
            name = _decode(e.name)
            return (e.o_num or None), (name or (f"O{e.o_num:04d}" if e.o_num else ""))
        p = ODBPRO()
        self._check("cnc_rdprgnum", _lib.cnc_rdprgnum(self.handle, ctypes.byref(p)))
        return (p.data or None), (f"O{p.data:04d}" if p.data else "")

    def active_mcodes(self, path: int) -> list[tuple[int, int]]:
        """M codes commanded in the block being executed, as (value, flag) pairs (cnc_rdcommand)."""
        self.set_path(path)
        buf = (ODBCMD * 30)()
        n = ctypes.c_short(30)
        # type -2 = all commanded data, block 1 = the active block
        self._check("cnc_rdcommand", _lib.cnc_rdcommand(self.handle, -2, 1, ctypes.byref(n), buf))
        out = []
        for c in buf[: max(0, min(n.value, 30))]:
            if c.adrs.upper() != b"M":
                continue
            val = c.cmd_val / (10 ** c.dec_val) if 0 < c.dec_val < 9 else c.cmd_val
            out.append((int(round(val)), c.flag & 0xFFFF))
        return out

    def exec_block(self, path: int) -> str:
        """Text of the NC block being executed (first block cnc_rdexecprog returns)."""
        self.set_path(path)
        buf = ctypes.create_string_buffer(512)
        length = ctypes.c_ushort(500)
        blocks = ctypes.c_short(0)
        self._check("cnc_rdexecprog", _lib.cnc_rdexecprog(self.handle, ctypes.byref(length), ctypes.byref(blocks), buf))
        text = buf.raw[: min(length.value, 500)].split(b"\x00", 1)[0].decode("latin-1", errors="replace")
        lines = [ln.strip() for ln in re.split(r"[\r\n;]", text)]
        lines = [ln for ln in lines if ln and ln != "%"]
        return lines[0] if lines else ""

    def pmc_bytes(self, area: str, start: int, count: int) -> bytes:
        """Read `count` (<= PMC_CHUNK) bytes of a PMC area, e.g. ('K', 0, 100)."""
        count = max(1, min(count, PMC_CHUNK))
        buf = IODBPMC()
        ret = _lib.pmc_rdpmcrng(self.handle, PMC_AREAS[area], 0, start, start + count - 1, 8 + count,
                                ctypes.byref(buf))
        self._check("pmc_rdpmcrng", ret)
        return bytes(buf.cdata[:count])

    def read_signal(self, sig) -> bool:
        """Current state of a parsed signal (see parse_signal)."""
        inv, area, num, bit = sig
        if area == "#":
            v = self.macro(1, num)
            on = bool(v)
        else:
            b = self.pmc_bytes(area, num, 1)[0]
            on = bool(b >> bit & 1) if bit is not None else b != 0
        return on != inv

    def program_comment(self, path: int, number: int) -> str:
        self.set_path(path)
        top = ctypes.c_long(number)
        num = ctypes.c_short(1)
        d = PRGDIR3()
        self._check("cnc_rdprogdir3", _lib.cnc_rdprogdir3(self.handle, 1, ctypes.byref(top), ctypes.byref(num), ctypes.byref(d)))
        if num.value < 1 or d.number != number:
            return ""
        return _decode(d.comment).strip("()").strip()


# ---------------------------------------------------------------------------
# Demo machine - lets you test the whole chain (server + iPhone) without the lathe
# ---------------------------------------------------------------------------
class MockMachine:
    DEMO_ALARMS = [
        (1051, 15, 0, "BARFEEDER EMERGENCY STOP"),
        (1010, 15, 0, "MAIN CHUCK SENSOR ERR. ALARM"),
        (1049, 15, 0, "BARFEEDER AUTO OFF"),
        (401, 6, 2, "(Z1)SERVO V-READY OFF"),
    ]

    def __init__(self, *_, **__):
        self.connected = False
        self.parts = 118
        self.required = 500
        self.cycle_len = 22.0
        self.cycle_start = time.time()
        self.phase = "running"
        self.phase_until = time.time() + 120
        self.alarm: RawAlarm | None = None

    def connect(self):
        self.connected = True

    def close(self):
        self.connected = False

    def _tick(self):
        now = time.time()
        if self.phase == "running" and now - self.cycle_start >= self.cycle_len:
            self.parts += 1
            self.cycle_start = now
            self.cycle_len = 22.0 + random.uniform(-0.6, 0.6)
        if now >= self.phase_until:
            if self.phase == "running":
                self.phase = random.choice(["standby", "alarm", "barchange"])
                if self.phase == "alarm":
                    self.alarm = RawAlarm(*random.choice(self.DEMO_ALARMS))
                self.phase_until = now + (30 if self.phase == "barchange" else 45)
            elif self.phase == "barchange":   # new bar loaded - carry on with the job
                self.phase = "running"
                self.cycle_start = now
                self.phase_until = now + 150
            else:
                self.phase, self.alarm = "running", None
                self.cycle_start = now
                self.phase_until = now + 150

    def status(self, path: int) -> PathStatus:
        self._tick()
        run = {"running": 3, "barchange": 3, "standby": 0, "alarm": 1}[self.phase]
        return PathStatus(mode="MEM", run=RUN_STATES[run], run_code=run, emergency=False,
                          alarm=self.phase == "alarm" and path == 1)

    def alarms(self, path: int) -> list[RawAlarm]:
        return [self.alarm] if (self.alarm and path == 1) else []

    def macro(self, path: int, number: int):
        return {3901: self.parts, 3902: self.required}.get(number)

    def param_long(self, path: int, number: int) -> int:
        return {6711: self.parts, 6713: self.required, 6712: 40000 + self.parts}.get(number, 0)

    def cycle_timer(self, path: int) -> float:
        return (time.time() - self.cycle_start) if self.phase in ("running", "barchange") else 0.0

    def active_mcodes(self, path: int) -> list[tuple[int, int]]:
        return [(92, 0x8000)] if (self.phase == "barchange" and path == 1) else []

    def pmc_bytes(self, area: str, start: int, count: int) -> bytes:
        if area == "K" and start + count > 99:
            raise FocasError("pmc_rdpmcrng", 3)
        if area not in ("K", "R", "X"):
            raise FocasError("pmc_rdpmcrng", 3)
        data = bytearray(count)
        if area == "K" and start <= 5 < start + count:
            data[5 - start] = 0x08          # K5.3 = demo "work counter stop" on
        if area == "X" and start <= 8 < start + count:
            data[8 - start] = 0x20 if self.phase == "running" else 0
        return bytes(data)

    def read_signal(self, sig) -> bool:
        inv, area, num, bit = sig
        if area == "#":
            return bool(self.macro(1, num)) != inv
        b = self.pmc_bytes(area, num, 1)[0]
        return (bool(b >> bit & 1) if bit is not None else b != 0) != inv

    def exec_block(self, path: int) -> str:
        if self.phase == "barchange" and path == 1:
            return "N9000 M92"
        return "G01 X4.2 Z-12.5 F0.03" if self.phase == "running" else ""

    def program(self, path: int):
        return 1234, "O1234"

    def program_comment(self, path: int, number: int) -> str:
        return "DEMO PART"
