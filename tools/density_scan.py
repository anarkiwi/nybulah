"""Capture halftracks at every density, saving each capture for analysis.

A 1571 streams (homed on its sensor); a 1541 takes --captures RAM passes per
density after locating its head as ``read`` does.
Usage: ``python tools/density_scan.py --dev 8 --halftracks 2 4 --out DIR``.
"""

import contextlib
import json
import pathlib

from tqdm import tqdm

from nybulah import disk, streamprobe
from nybulah.monitor import Monitor
from nybulah.nibbler import Nibbler
from nybulah.ramprobe import identify_model
from nybulah.tool import standalone


@contextlib.contextmanager
def located(args, cbm):
    """``(nibbler, report)`` of a 1541 whose head is located from DOS or headers."""
    with (
        Monitor(cbm, args.dev, args.transport) as mon,
        Nibbler(mon, "1541") as nib,
    ):
        disk.calibrate(nib, disk.Archive(None))
        yield nib, {"located": nib.halftrack}


class DensityScan:
    """Tool module: every density of the given halftracks, one homing."""

    @staticmethod
    def add_arguments(ap):
        """Command line options."""
        streamprobe.track_arguments(ap)
        ap.add_argument("--halftracks", type=int, nargs="+", required=True)
        ap.add_argument("--densities", type=int, nargs="+", default=[0, 1, 2, 3])
        ap.add_argument("--captures", type=int, default=3, help="1541 passes each")
        ap.add_argument("--transport", default="s3", help="1541 transport")
        ap.add_argument("--out", type=pathlib.Path, required=True)

    @staticmethod
    def execute(args, cbm):
        """Locate once, then capture each (halftrack, density)."""
        args.out.mkdir(parents=True, exist_ok=True)
        ram = identify_model(cbm, args.dev) == "1541"
        reps = args.captures if ram else 1
        jobs = [
            (h, d, n)
            for h in args.halftracks
            for d in args.densities
            for n in range(reps)
        ]
        session = located if ram else streamprobe.homed
        with session(args, cbm) as (nib, report):
            print(json.dumps(report))
            for halftrack, density, n in tqdm(jobs, desc="scan", unit="cap"):
                if ram:
                    cap = nib.capture(halftrack, density, "now", args.side, "none")
                    row = {"halftrack": halftrack, "bytes": len(cap.data)}
                else:
                    cap = nib.stream(halftrack, args.revolutions, args.side, density)
                    row = streamprobe.summary(cap)
                path = args.out / f"h{halftrack:02d}-d{density}-{n}.npz"
                cap.save(path)
                row |= {"density": density, "saved": str(path)}
                print(json.dumps(row), flush=True)


if __name__ == "__main__":
    standalone(DensityScan)
