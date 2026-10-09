"""Measure host<->drive transfer rates for the available transports."""

import functools
import json
import os
import sys
import time

import numpy as np
from tqdm import tqdm

from . import tool
from .monitor import Monitor, code_suffix, drivecode, protocols

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
