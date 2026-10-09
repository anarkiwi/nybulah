"""1571 single-track stream probe (s4, xum1541 firmware v12); never bumps.

Homes through Nibbler.home within --max-steps outward steps (homeprobe's plan),
streams one track and prints how it ended, its bytes, index edges and syncs.
"""

import json
import pathlib

import numpy as np

from . import homeprobe
from .monitor import Monitor
from .nibbler import MAX_HALFTRACK, STREAM_REVS, Nibbler
from .ramprobe import identify_model


def add_arguments(ap):
    """Command line options."""
    ap.add_argument("--dev", type=int, default=8)
    ap.add_argument("--halftrack", type=int, default=36)
    ap.add_argument("--revolutions", type=int, default=STREAM_REVS)
    ap.add_argument("--side", type=int, choices=(0, 1), default=0)
    ap.add_argument("--headers", action="store_true", help="estimate from headers")
    ap.add_argument(
        "--max-steps",
        type=int,
        default=MAX_HALFTRACK,
        help="refuse when homing needs more outward steps",
    )
    ap.add_argument("--save", type=pathlib.Path, help="write the capture (.npz)")


def summary(cap):
    """JSON-ready digest of a stream capture."""
    pos, est, _, hi = cap.parsed.syncs(2 * cap.cell_cycles)
    done = hi >= 0
    return cap.stream_status | {
        "halftrack": cap.halftrack,
        "side": cap.side,
        "bytes": len(cap.data),
        "stream_bytes": len(cap.stream),
        "index": cap.index.tolist(),
        "revolution_bytes": cap.revolution_bytes(),
        "syncs": len(pos),
        "sync_cycles_median": float(np.median(est[done])) if done.any() else None,
    }


def execute(args, cbm):
    """Home, stream and print the report as JSON."""
    model = identify_model(cbm, args.dev)
    if model != "1571":
        raise ValueError(f"device {args.dev} is a {model}: streaming needs a 1571")
    with (
        Monitor(cbm, args.dev, "s4") as mon,
        Nibbler(mon, model, stream=True) as nib,
    ):
        report = {"home": homeprobe.dry(nib, args.headers)}
        outward = report["home"]["outward_steps"]
        if outward > args.max_steps:
            raise ValueError(
                f"homing needs {outward} outward steps, over {args.max_steps}"
            )
        nib.home(report["home"]["estimate"])
        cap = nib.stream(args.halftrack, args.revolutions, args.side)
        report |= summary(cap)
        if args.save:
            cap.save(args.save)
            report["saved"] = str(args.save)
    print(json.dumps(report))
    return report

