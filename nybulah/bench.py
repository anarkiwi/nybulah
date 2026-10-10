"""Measure host<->drive transfer rates for the available transports."""

import functools
import json
import os
import struct
import sys
import time

import numpy as np
from tqdm import tqdm

from . import tool
from .link import WATCHDOG_IDLE_S, WATCHDOG_S
from .monitor import Monitor, code_suffix, drivecode, protocols
from .stream import Stream

CIAPROBE = 0x0300
CIA_K0, CIA_NK = 26, 18
SWEEP_SIZES = tuple(64 << i for i in range(8))


def _rate(fn, size, reps, desc):
    t0 = time.perf_counter()
    for _ in tqdm(range(reps), desc=desc, unit="xfer"):
        fn()
    dt = time.perf_counter() - t0
    return {"bytes": size * reps, "seconds": dt, "bytes_per_s": size * reps / dt}


def run(cbm, dev, addr, size, reps, protocol="s1", pattern=None, fast=False):
    """Benchmark M-R and the monitor's read/write; verify the round trip."""
    out = {"dev": dev, "addr": addr, "size": size, "protocol": protocol, "fast": fast}
    mr = min(size, 1024)
    out["mr"] = _rate(lambda: cbm.download(dev, addr, mr), mr, 1, "M-R")
    pattern = os.urandom(size) if pattern is None else bytes(pattern[:size])
    with Monitor(cbm, dev, protocol) as mon:
        if fast:
            mon.set_fast(True)
        out["write"] = _rate(lambda: mon.write(addr, pattern), size, reps, "write")
        got = []
        out["read"] = _rate(
            lambda: got.append(mon.read(addr, size)), size, reps, "read"
        )
        want = np.frombuffer(pattern, np.uint8)
        out["rejects"] = getattr(mon.link, "rejects", 0)
        out["errors"] = sum(
            int(np.count_nonzero(np.frombuffer(g, np.uint8) != want)) for g in got
        )
    return out


def cia_flag(mon):
    """Drive cycles from an SDR write to the first ICR read showing SP, per timer phase.

    Runs drive/ciaprobe.s on a 1571 or 1581 under a monitor that leaves SRQ alone.
    ``table`` holds ICR & SP per write phase (rows) and read offset from CIA_K0.
    """
    code = drivecode("ciaprobe" + code_suffix(getattr(mon, "model", None)))
    mon.write(CIAPROBE, code)
    first = mon.jsr(CIAPROBE)[:2]
    res = mon.read(CIAPROBE + len(code) - 2 * CIA_NK, 2 * CIA_NK)
    table = np.frombuffer(res, np.uint8).reshape(CIA_NK, 2).T != 0
    return {
        "first": [None if k == 0xFF else int(k) for k in first],
        "k0": CIA_K0,
        "table": table.astype(int).tolist(),
    }


def sweep(mon, addr, sizes=SWEEP_SIZES, reps=10, clock=time.perf_counter):
    """Host-clock read times per block size and their least-squares line.

    ``per_byte_us`` is the slope (drive loop, bursts and USB), ``per_block_us`` the
    intercept (command, reply and USB round trips of one checked block).
    """
    rows = []
    for n in tqdm(sizes, desc="sweep", unit="size"):
        mon.read(addr, n)
        t0 = clock()
        for _ in range(reps):
            mon.read(addr, n)
        rows.append((n, (clock() - t0) / reps))
    n, t = np.array(rows).T
    slope, icept = np.polyfit(n, t, 1)
    return {
        "sizes": [int(x) for x in n],
        "seconds": t.tolist(),
        "per_byte_us": slope * 1e6,
        "per_block_us": icept * 1e6,
        "residual_us": (np.abs(t - (slope * n + icept)).max() * 1e6),
    }


def add_arguments(ap):
    """Command line options."""
    ap.add_argument("--dev", type=int, default=8)
    ap.add_argument("--addr", type=functools.partial(int, base=0), default=0x8000)
    ap.add_argument("--size", type=int, default=0x2000)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--protocol", choices=protocols(), default="s1")
    ap.add_argument("--fast", action="store_true", help="1571 at 2 MHz (s3, s4)")


def execute(args, cbm):
    """Benchmark and print the rates as JSON."""
    out = run(
        cbm, args.dev, args.addr, args.size, args.reps, args.protocol, fast=args.fast
    )
    print(json.dumps(out, indent=1))
    return out


def main(argv=None, cbm=None):
    """CLI entry point."""
    return tool.standalone(sys.modules[__name__], argv, cbm)


if __name__ == "__main__":
    main()


SDRGAP = 0x0300
SDRGAP_TAG = b"NYSG"
SDRGAP_LOG = {"1581": 0x0C00, "1571": 0x6000}  # drive/sdrgap.s LOG
SDRGAP_LOG_LEN = 16
SDRGAP_ENTRIES = 32
SDRGAP_UNIT_US = 50  # GAP_UNIT cycles at 2 MHz
SDRGAP_FIELDS = (
    "port",
    "cra",
    "talo",
    "icr_before",
    "icr_after",
    "tbhi1",
    "tblo",
    "tbhi2",
    "value",
    "flags",
    "port_after",
)
F_META, F_ICR, F_CRA = 0x01, 0x02, 0x04
M_START, M_END, M_END_ATN, M_KEEP, ST_NOGO = 0x04, 0x40, 0x48, 0x14, 0xFF
ICR_SP = 0x08
PLAIN_VALUES = (0x55, 0xAA)
SDRGAP_REPLIES = {M_END: "done", M_END_ATN: "atn", ST_NOGO: "nogo"}


def sdr_gap(
    mon, gap_ms, reps=10, kinds=("meta", "plain"), icr_clear=False, rearm=False
):
    """Lone bytes gap_ms apart through the shift register, the host in the firmware v12
    stream receive (drive/sdrgap.s): per byte, whether the adapter framed it and whether
    the drive's ICR says the shifter sent it.

    ``kinds`` cycles over the bytes: metadata (CLK asserted, value KEEP) or plain
    (PLAIN_VALUES). The adapter stops at the first byte it does not see within its gap
    timeout (``first_lost``), and its ATN then ends the probe in step.
    """
    model = getattr(mon, "model", None) or "1571"
    code = drivecode("sdrgap" + code_suffix(model))
    log_at = SDRGAP_LOG[model]
    units = round(gap_ms * 1000 / SDRGAP_UNIT_US)
    flags = (F_ICR if icr_clear else 0) | (F_CRA if rearm else 0)
    entries = sdr_entries(units, reps, kinds, flags)
    mon.write(SDRGAP, code)
    mon.write(SDRGAP + code.index(SDRGAP_TAG) + len(SDRGAP_TAG), sdr_table(entries))
    mon.transact(b"J" + struct.pack("<H", SDRGAP))
    t0 = time.perf_counter()
    raw = mon.cbm.srq2_stream(64 * -(-(4 * len(entries) + 8) // 64))
    elapsed = time.perf_counter() - t0
    reply = tuple(int(v) for v in mon.link.response(3))
    mon.touch()
    in_step = reply[0] in SDRGAP_REPLIES
    rows = sdr_log(_sdr_fetch(mon, in_step, log_at, reply[1], len(entries) + 2), model)
    stream = Stream.parse(raw)
    received = sdr_sequence(stream)
    expected = [(True, M_START)] + [(bool(f & F_META), v) for _, v, f in entries]
    first_lost = sdr_mark(received, expected + [(True, M_END)], rows)
    return {
        "model": model,
        "gap_ms": gap_ms,
        "units": units,
        "flags": flags,
        "adapter": stream.adapter,
        "reply": list(reply),
        "drive": SDRGAP_REPLIES.get(reply[0], "lost"),
        "in_step": in_step,
        "elapsed_s": round(elapsed, 6),
        "raw_bytes": len(raw),
        "sent": len(expected) + 1,
        "received": len(received),
        "first_lost": first_lost,
        "log": rows,
    }


def sdr_entries(units, reps, kinds, flags):
    """Table entries (gap units, value, flags): metadata KEEPs, plain $55/$AA."""
    entries = []
    for i in range(min(reps, SDRGAP_ENTRIES)):
        if kinds[i % len(kinds)] == "meta":
            entries.append((units, M_KEEP, flags | F_META))
        else:
            entries.append((units, PLAIN_VALUES[i % len(PLAIN_VALUES)], flags))
    return entries


def sdr_table(entries):
    """The drive's table: four bytes an entry, flags $FF after the last."""
    return b"".join(struct.pack("<HBB", *e) for e in entries) + b"\0\0\0\xff"


def _sdr_fetch(mon, in_step, log_at, logged, sent):
    """The probe's log: over the monitor when its reply was read in step, else over
    DOS M-R once the lost drive is back in DOS."""
    if in_step:
        return mon.read(log_at, logged * SDRGAP_LOG_LEN)
    mon.running = False
    return sdr_dos_read(mon, log_at, sent * SDRGAP_LOG_LEN, WATCHDOG_S, WATCHDOG_IDLE_S)


def sdr_mark(received, expected, rows):
    """Mark each log row ``seen`` up to the first expected byte the adapter did not
    frame in order; that byte's index, or None."""
    first_lost = next(
        (i for i, e in enumerate(expected) if i >= len(received) or received[i] != e),
        None,
    )
    for i, row in enumerate(rows):
        row["seen"] = first_lost is None or i < first_lost
    return first_lost


def sdr_dos_read(mon, addr, size, period_s, idle_s):
    """Drive memory over DOS M-R once the drive is back in DOS (its monitor's command
    wait and idle window have run out), tried every period_s; the bytes or b""."""
    download = getattr(mon.cbm, "download", None)
    tries = int(-(-(period_s + idle_s) // period_s)) + 1
    for _ in tqdm(range(tries), desc="drive log", unit="try", leave=False):
        try:
            return bytes(download(mon.dev, addr, size)) if download else b""
        except (IOError, ValueError):
            time.sleep(period_s)
    return b""


def sdr_sequence(stream):
    """The bytes the adapter framed, in order, as (metadata, value)."""
    out, at = [], 0
    for pos, val in zip(stream.pos.tolist(), stream.val.tolist()):
        out.extend((False, int(b)) for b in stream.data[at:pos])
        out.append((True, val))
        at = pos
    out.extend((False, int(b)) for b in stream.data[at:])
    return out


def sdr_log(raw, model):
    """The probe's log records as dicts; ``shifted`` is the ICR flag after the write
    (None when the entry read no ICR), ``t_us`` the timer B stamp on a 1581 (counting
    down at 1 MHz under its monitor)."""
    rows = []
    for rec in np.frombuffer(raw, np.uint8)[
        : len(raw) // SDRGAP_LOG_LEN * SDRGAP_LOG_LEN
    ].reshape(-1, SDRGAP_LOG_LEN):
        row = dict(zip(SDRGAP_FIELDS, rec[: len(SDRGAP_FIELDS)].tolist()))
        row["kind"] = "meta" if row["flags"] & F_META else "plain"
        row["shifted"] = (
            bool(row["icr_after"] & ICR_SP) if row["flags"] & F_ICR else None
        )
        hi = row["tbhi2"] if row["tblo"] & 0x80 else row["tbhi1"]
        row["t_us"] = (0xFFFF - (hi << 8 | row["tblo"])) if model == "1581" else None
        rows.append(row)
    return rows


def sdr_gap_line(out):
    """One line: gap, how the adapter and drive ended, bytes seen and shifted."""
    rows = out["log"]
    seen = sum(r["seen"] for r in rows)
    asked = [r for r in rows if r["shifted"] is not None]
    shifted = (
        f", {sum(r['shifted'] for r in asked)}/{len(asked)} shifted" if asked else ""
    )
    lost = out["first_lost"]
    where = (
        ""
        if lost is None
        else f" first lost #{lost} ({rows[lost]['kind'] if lost < len(rows) else '?'})"
    )
    mode = "icr" if out["flags"] & F_ICR else "no icr"
    return (
        f"gap {out['gap_ms']:g} ms ({mode}): adapter {out['adapter']}, drive"
        f" {out['drive']}, {seen}/{out['sent']} seen{shifted}{where}"
    )
