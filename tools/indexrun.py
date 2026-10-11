"""Index stamp delivery on a 1581: streams of back-to-back index waits.

A stamp sent but dropped by the parse (an INDEX marker with its chunks cut short)
counts as a marker without a stamp and a period near two revolutions.
"""

import argparse
import json

import numpy as np
from tqdm import tqdm

from nybulah import mfmstream as ms
from nybulah import r1581


def run_one(drive, waits):
    """One stream of waits index waits: its digest."""
    got = drive.stream([ms.entry(ms.OP_INDEX, rep=waits)])
    periods = np.diff(got.index_us)
    rev = np.median(periods) if len(periods) else 0
    return {
        "adapter": got.adapter,
        "drive_end": got.drive_end,
        "markers": got.codes.get("index", 0),
        "stamps": len(got.index_us),
        "long_periods": int((periods > 1.5 * rev).sum()) if rev else 0,
        "period_us": [int(periods.min()), int(periods.max())] if rev else [],
    }


def run(drive, waits, streams):
    """Motor on, streams streams in turn, motor off."""
    drive.motor(True)
    out = []
    try:
        for _ in tqdm(range(streams), desc="streams", unit="stream"):
            out.append(run_one(drive, waits))
            tqdm.write(json.dumps(out[-1]))
    finally:
        drive.motor(False)
    lost = sum(s["markers"] - s["stamps"] for s in out)
    return {"waits": waits, "lost_stamps": lost, "streams": out}


def main():
    """CLI."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dev", type=int, default=9)
    ap.add_argument("--transport", default="s4")
    ap.add_argument("--sim", action="store_true", help="the simulated 1581")
    ap.add_argument("--waits", type=int, default=ms.REP_MAX)
    ap.add_argument("--streams", type=int, default=4)
    ap.add_argument("--save", help="JSON report path")
    args = ap.parse_args()
    if args.sim:
        from ipprobe import sim_drive  # pylint: disable=import-outside-toplevel

        report = run(sim_drive(), args.waits, args.streams)
    else:
        from nybulah.opencbm import OpenCBM  # pylint: disable=import-outside-toplevel

        with OpenCBM() as cbm, r1581.session(cbm, args.dev, args.transport) as drive:
            report = run(drive, args.waits, args.streams)
    print(f"lost stamps: {report['lost_stamps']}")
    if args.save:
        with open(args.save, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=1)


if __name__ == "__main__":
    main()
