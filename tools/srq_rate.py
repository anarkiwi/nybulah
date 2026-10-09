"""Drive-bound s4 read rate in co-simulation, per drive clock and 6526 start delay.

Times two block reads on the timed 1571 and differences them, so per-block costs
cancel; prints drive cycles per byte (send period plus burst overhead) and bytes/s.
A delay whose flag reaches the next write fails the block check and prints "fails".
"""

import argparse
import json

from tqdm import tqdm

from nybulah import simsrq
from nybulah.fastx import ChecksumError
from nybulah.monitor import Monitor
from nybulah.opencbm import OpenCBMError

BASE = 0x6000


def per_byte(fast, delay, small=512, large=2048):
    """Drive cycles per byte read, or None if the block check fails."""
    cbm = simsrq.make(1.0, rise=1.0, dev=9, delay=delay)
    mon = Monitor(cbm, 9, "s4")
    try:
        mon.start()
        if fast:
            mon.set_fast(True)
        cbm.drive.load(BASE, bytes(range(256)) * (large // 256))
        cycles = []
        for n in (small, large):
            start = cbm.drive.cycles
            mon.read(BASE, n)
            cycles.append(cbm.drive.cycles - start)
    except (ChecksumError, OpenCBMError):
        return None
    return (cycles[1] - cycles[0]) / (large - small)


def main(argv=None):
    """CLI: one JSON line per (clock, delay)."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--delays", type=int, nargs="*", default=list(range(11)))
    args = ap.parse_args(argv)
    jobs = [(fast, d) for fast in (False, True) for d in args.delays]
    for fast, delay in tqdm(jobs, desc="srq rate", unit="run"):
        cyc = per_byte(fast, delay)
        hz = 2e6 if fast else 1e6
        row = {"mhz": hz / 1e6, "delay": delay}
        row |= (
            {"cycles_per_byte": round(cyc, 2), "bytes_per_s": round(hz / cyc)}
            if cyc
            else {"fails": True}
        )
        print(json.dumps(row))


if __name__ == "__main__":
    main()
