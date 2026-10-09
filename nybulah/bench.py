"""Measure host<->drive transfer rates for the available transports."""

import functools
import json
import os
import sys
import time

import numpy as np
from tqdm import tqdm

from . import tool
from .monitor import Monitor, protocols


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


def add_arguments(ap):
    """Command line options."""
    ap.add_argument("--dev", type=int, default=8)
    ap.add_argument("--addr", type=functools.partial(int, base=0), default=0x8000)
    ap.add_argument("--size", type=int, default=0x2000)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--protocol", choices=protocols(), default="s1")
    ap.add_argument("--fast", action="store_true", help="1571 at 2 MHz (s3 only)")


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
