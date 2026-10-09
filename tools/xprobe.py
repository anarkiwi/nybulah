"""Hardware probes of a monitor transport.

Default: reads, writes and a jsr of growing size, stopping at the first failure.
--rate: read rate of repeated blocks. --sweep: host-clock read time per block size
and its per-byte/per-block fit. --cia: 1571 6526 or 1581 8520 SDR-to-ICR flag latency (under s3).
"""

import argparse
import json
import os
import time
import traceback

from nybulah import bench
from nybulah.monitor import Monitor
from nybulah.opencbm import OpenCBM

SIZES = (1, 5, 31, 32, 33, 48, 63, 64, 65, 128, 256, 4096, 8192)


def probe(cbm, dev, base, protocol, fast):
    """Yield (step, ok, detail) for reads then writes of growing sizes."""
    with Monitor(cbm, dev, protocol) as mon:
        if fast:
            mon.set_fast(True)
        steps = []
        for n in SIZES:
            steps.append((f"read {n}", lambda n=n: mon.read(base, n)))
        for n in SIZES:
            data = os.urandom(n)
            steps.append(
                (
                    f"write {n}",
                    lambda n=n, d=data: mon.write(base, d) or mon.read(base, n) == d,
                )
            )
        steps.append(("jsr", lambda: mon.write(base, b"\x60") or mon.jsr(base)))
        for name, fn in steps:
            try:
                yield name, True, fn()
            except Exception:  # pylint: disable=broad-except
                yield name, False, traceback.format_exc(limit=1).strip().splitlines()[
                    -1
                ]
                return


def read_rate(cbm, dev, base, protocol, fast, size=8192, reps=10):
    """Bytes/s of repeated monitor reads, and whether all reads agree."""
    with Monitor(cbm, dev, protocol) as mon:
        if fast:
            mon.set_fast(True)
        first = mon.read(base, size)
        t0 = time.perf_counter()
        same = all(mon.read(base, size) == first for _ in range(reps))
        return size * reps / (time.perf_counter() - t0), same


def main():
    """CLI."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dev", type=int, default=10)
    ap.add_argument("--base", type=lambda s: int(s, 0), default=0x8000)
    ap.add_argument("--protocol", default="s3")
    ap.add_argument("--fast", action="store_true")
    ap.add_argument("--rate", action="store_true", help="only measure read rate")
    ap.add_argument("--sweep", action="store_true", help="read time per block size")
    ap.add_argument(
        "--cia", action="store_true", help="1571/1581 CIA flag latency (s3)"
    )
    ap.add_argument("--reps", type=int, default=10)
    args = ap.parse_args()
    with OpenCBM() as cbm:
        if args.sweep or args.cia:
            with Monitor(cbm, args.dev, "s3" if args.cia else args.protocol) as mon:
                if args.fast:
                    mon.set_fast(True)
                out = (
                    bench.cia_flag(mon)
                    if args.cia
                    else bench.sweep(mon, args.base, reps=args.reps)
                )
            print(json.dumps(out, indent=1))
            return
        if args.rate:
            rate, same = read_rate(cbm, args.dev, args.base, args.protocol, args.fast)
            print(f"read {rate:.0f} B/s consistent={same}")
            return
        for name, ok, detail in probe(
            cbm, args.dev, args.base, args.protocol, args.fast
        ):
            shown = detail if not isinstance(detail, bytes) else f"{len(detail)} bytes"
            print(f"{'ok ' if ok else 'ERR'} {name}: {shown}", flush=True)


if __name__ == "__main__":
    main()
