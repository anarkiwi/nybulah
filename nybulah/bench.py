"""Measure host<->drive transfer rates for the available transports."""

import argparse
import json
import os
import time

import numpy as np

from tqdm import tqdm

from .monitor import Monitor, protocols
from .opencbm import OpenCBM


def _rate(fn, size, reps, desc):
    t0 = time.perf_counter()
    for _ in tqdm(range(reps), desc=desc, unit="xfer"):
        fn()
    dt = time.perf_counter() - t0
    return {"bytes": size * reps, "seconds": dt, "bytes_per_s": size * reps / dt}


def run(cbm, dev, addr, size, reps, protocol="s1", pattern=None):
    """Benchmark M-R and the monitor's read/write; verify the round trip."""
    out = {"dev": dev, "addr": addr, "size": size, "protocol": protocol}
    mr = min(size, 1024)
    out["mr"] = _rate(lambda: cbm.download(dev, addr, mr), mr, 1, "M-R")
    pattern = os.urandom(size) if pattern is None else bytes(pattern[:size])
    with Monitor(cbm, dev, protocol) as mon:
        out["write"] = _rate(lambda: mon.write(addr, pattern), size, reps, "write")
        got = []
        out["read"] = _rate(
            lambda: got.append(mon.read(addr, size)), size, reps, "read"
        )
        want = np.frombuffer(pattern, np.uint8)
        out["errors"] = sum(
            int(np.count_nonzero(np.frombuffer(g, np.uint8) != want)) for g in got
        )
    return out


def main(argv=None):
    """CLI entry point."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dev", type=int, default=8)
    ap.add_argument("--addr", type=lambda s: int(s, 0), default=0x8000)
    ap.add_argument("--size", type=int, default=0x2000)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--protocol", choices=protocols(), default="s1")
    args = ap.parse_args(argv)
    with OpenCBM() as cbm:
        print(
            json.dumps(
                run(cbm, args.dev, args.addr, args.size, args.reps, args.protocol),
                indent=1,
            )
        )


if __name__ == "__main__":
    main()
