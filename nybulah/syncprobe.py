"""1571 sync sensor probe (s4, xum1541 firmware v12): a stream and a sensor pass of
one track, the sensed syncs placed on the streamed bytes; never bumps.

Homes through Nibbler.home within --max-steps outward steps (homeprobe's plan).
"""

import numpy as np

from . import tss
from .nibbler import STREAM_HZ, TSS_REVS
from .streamprobe import homed, printed, track_arguments
from .streamprobe import summary as stream_summary


def add_arguments(ap):
    """Command line options."""
    track_arguments(ap)
    ap.add_argument("--sense", type=int, default=TSS_REVS, help="revolutions sensed")
    ap.add_argument("--table", action="store_true", help="list every sensed sync")


def table(cap):
    """Per sensed sync: bytes since the previous record, its kind and the pulse
    width in cells with its bounds."""
    rec = cap.records
    cell = rec.cell(STREAM_HZ / 1e6 * cap.cell_cycles)
    count, est, lo, hi = rec.pulses(cell)
    rows = []
    for c, k, e, l, h in zip(count, rec.kind[rec.is_sync], est, lo, hi):
        rows.append(
            {
                "count": int(c),
                "kind": int(k),
                "cells": round(float(e / cell), 2),
                "cells_lo": round(float(l / cell), 2),
                "cells_hi": round(float(h / cell), 2),
            }
        )
    return rows


def summary(cap):
    """JSON-ready digest of a stream with its sensor pass."""
    rec = cap.records
    cell = rec.cell(STREAM_HZ / 1e6 * cap.cell_cycles)
    cells = rec.cells(cell)
    hist = np.bincount(np.round(cells).astype(int) + 9) if len(cells) else []
    return stream_summary(cap) | {
        "status": tss.status_names(cap.sensed_status),
        "sensed": int(rec.is_sync.sum()),
        "index_records": len(rec.index),
        "sensed_revolution_bytes": np.diff(rec.index).tolist(),
        "sensed_rpm": (
            None
            if rec.revolution_cycles() is None
            else round(60.0 * tss.CPU_HZ / rec.revolution_cycles(), 2)
        ),
        "stream_syncs": len(cap.parsed.syncs()[0]),
        "syncs": len(cap.positions),
        "unmatched": int(cap.lost),
        "runs": {str(n): int(c) for n, c in enumerate(hist) if c},
    }


def execute(args, cbm):
    """Home, stream, sense and print the report as JSON."""
    with homed(args, cbm, "the sync sensor") as (nib, report):
        cap = nib.stream(args.halftrack, args.revolutions, args.side)
        cap = nib.sense_syncs(args.halftrack, args.sense, args.side).attach(cap)
        report |= summary(cap)
        if args.table:
            report["table"] = table(cap)
    return printed(report, cap, args.save)
