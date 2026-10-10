"""Lone bytes through the CIA shift register after programmed idles, under the
firmware v12 stream receive (drive/sdrgap.s on a 1571 or 1581 under s4).

Per gap one stream: START, the bytes, END. Prints a line per gap and a JSON report
of what the adapter framed against what the drive's ICR says it shifted.
"""

import argparse
import json

from tqdm import tqdm

from nybulah import bench
from nybulah.monitor import Monitor
from nybulah.opencbm import OpenCBM


def main():
    """CLI."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dev", type=int, default=9)
    ap.add_argument(
        "--gaps",
        type=float,
        nargs="+",
        default=[0.0, 0.1, 1.0, 3.0, 6.5, 15.0],
        help="idle before each byte, ms",
    )
    ap.add_argument("--reps", type=int, default=10, help="bytes per gap")
    ap.add_argument(
        "--kinds",
        nargs="+",
        default=["meta", "plain"],
        choices=["meta", "plain"],
        help="byte kinds, cycled",
    )
    ap.add_argument(
        "--modes",
        nargs="+",
        default=["noicr", "icr"],
        choices=["noicr", "icr"],
        help="never read ICR (as the stream), or before and after every write",
    )
    ap.add_argument(
        "--rearm",
        action="store_true",
        help="write CRA (output mode) before every write",
    )
    ap.add_argument("--save", help="JSON report path")
    args = ap.parse_args()
    runs = []
    with OpenCBM() as cbm:
        plan = [(gap, mode) for gap in args.gaps for mode in args.modes]
        for gap, mode in tqdm(plan, desc="runs", unit="run"):
            with Monitor(cbm, args.dev, "s4") as mon:
                if mon.model != "1581":
                    mon.set_fast(True)
                out = bench.sdr_gap(
                    mon,
                    gap,
                    reps=args.reps,
                    kinds=tuple(args.kinds),
                    icr_clear=mode == "icr",
                    rearm=args.rearm,
                )
            runs.append(out)
            print(bench.sdr_gap_line(out), flush=True)
    if args.save:
        with open(args.save, "w", encoding="utf-8") as f:
            json.dump(runs, f, indent=1)


if __name__ == "__main__":
    main()
