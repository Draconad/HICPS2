"""Signal finder: snapshot the machine's PMC memory and macro variables, flip a switch on the machine,
snapshot again and list what changed. Used to find the address of things like the work counter
"stop at required count" switch, which is machine-builder specific (not a standard FANUC setting)."""
from __future__ import annotations

import logging

from .focas import PMC_CHUNK, FocasError

log = logging.getLogger("hanwha.signals")

# (area, first byte, last byte to try) - reading stops at the first address the PMC doesn't have.
# Most likely homes for an operator on/off setting first: keep relays, internal relays, extra relays, data table.
AREAS = [("K", 0, 999), ("R", 0, 15999), ("E", 0, 9999), ("D", 0, 19999), ("C", 0, 399), ("T", 0, 499),
         ("X", 0, 999), ("Y", 0, 999), ("G", 0, 2999), ("F", 0, 2999)]
MACROS = list(range(100, 200)) + list(range(500, 1000))
NOISY = {"F", "G", "T", "C"}   # change on their own while the machine runs


def snapshot(machine, progress=None, cancelled=lambda: False) -> dict:
    total = sum((e - s) // PMC_CHUNK + 1 for _, s, e in AREAS) + len(MACROS) // 20
    done = 0
    pmc: dict[str, bytes] = {}
    for area, start, end in AREAS:
        data = bytearray()
        addr = start
        while addr <= end and not cancelled():
            n = min(PMC_CHUNK, end - addr + 1)
            try:
                data += machine.pmc_bytes(area, addr, n)
            except FocasError as e:
                if e.is_connection_error:
                    raise
                if n > 1:   # the area may end part-way through this chunk: read the rest byte by byte
                    for a in range(addr, addr + n):
                        try:
                            data += machine.pmc_bytes(area, a, 1)
                        except FocasError as e2:
                            if e2.is_connection_error:
                                raise
                            break
                break
            addr += n
            done += 1
            if progress:
                progress(done / total, f"Reading {area} {addr}")
        done += max(0, (end - addr) // PMC_CHUNK)
        if data:
            pmc[area] = bytes(data)
    macros: dict[int, float | None] = {}
    for i, n in enumerate(MACROS):
        if cancelled():
            break
        try:
            macros[n] = machine.macro(1, n)
        except FocasError as e:
            if e.is_connection_error:
                raise
        if progress and i % 20 == 0:
            done += 1
            progress(min(1.0, done / total), f"Reading #{n}")
    log.info("Signal snapshot: %s + %d macro variables",
             ", ".join(f"{a} {len(b)} bytes" for a, b in pmc.items()) or "no PMC data", len(macros))
    return {"pmc": pmc, "macro": macros}


def compare(a: dict, b: dict, limit: int = 400) -> list[str]:
    """Human-readable list of differences, most likely candidates first."""
    quiet, noisy = [], []
    for area, _, _ in AREAS:
        old, new = a["pmc"].get(area, b""), b["pmc"].get(area, b"")
        for addr in range(min(len(old), len(new))):
            x, y = old[addr], new[addr]
            if x == y:
                continue
            bits = [bit for bit in range(8) if (x ^ y) >> bit & 1]
            line = (f"{area}{addr}.{bits[0]}   {x >> bits[0] & 1} -> {y >> bits[0] & 1}" if len(bits) == 1 else
                    f"{area}{addr}   {x} -> {y}   (bits {', '.join(str(bit) for bit in bits)})")
            (noisy if area in NOISY else quiet).append(line)
    for n in MACROS:
        x, y = a["macro"].get(n), b["macro"].get(n)
        if x != y:
            quiet.append(f"#{n}   {'<vacant>' if x is None else x} -> {'<vacant>' if y is None else y}")
    out = quiet[:limit]
    if noisy:
        out += ["", "Also changed (these move on their own while the machine works, so less likely):"]
        out += noisy[: max(0, limit - len(out))]
    return out
