"""Where a RAM capture's TB and TS events land on its BITS bytes, against the
written pattern: per event its pass index, measured length, BITS landing and
the pattern region the BITS bytes there were read from.

usage: python tools/event_landings.py TRUTH.json CAPTURE.npz [...]
"""

import argparse
import json

import numpy as np
from tqdm import tqdm

from nybulah import passes
from nybulah.analysis import pattern as pt
from nybulah.analysis.gcr import to_bits
from nybulah.nibbler import Capture
from nybulah.unstable import absorbable


def byte_regions(truth, data):
    """Pattern region name of each BITS byte boundary, by aligning the raw bytes."""
    al = pt.align(to_bits(data), truth, period=truth.cells)
    pos = al.track_position(8 * np.arange(len(data) + 1))
    names = [r.name for r in truth.regions] + ["filler"]
    kinds = truth.kinds()
    at = np.where(
        (pos >= 0) & (pos < len(kinds)), kinds[np.clip(pos, 0, len(kinds) - 1)], -1
    )
    return [names[k] for k in at], pos


def landings(cap):
    """``(tb, ts, revolution)``: rows ``(pass index, wait or SYNC low cycles,
    BITS index, matched)`` per TB definite sync and placed TS sync."""
    data = np.asarray(cap.data, np.uint8)
    base = max(cap.base, 0)
    ts = cap.ts_syncs() if cap.ts is not None else None
    rev = cap.revolution_bytes()
    ok, latched = passes.capable(data)
    place, _, (pos, definite, matched) = (
        passes._place(  # pylint: disable=protected-access
            data,
            base,
            cap.tb,
            ts,
            cap.base >= 0,
            rev,
            None,
            (ok, passes.sync_weights(ok, latched), absorbable(data, latched)),
        )
    )
    wait = passes.tb_intervals(cap.tb)[0]
    tb = [
        (int(k), int(wait[k - 1]), int(pos[k]), bool(m))
        for k, m in zip(definite, matched)
    ]
    rows = []
    if place is not None:
        _, phi = passes.ts_pulse(ts[1])
        for p, s in zip(*place[:2]):
            rows.append((int(ts[0][s]), int(phi[s]), int(p), True))
    return tb, rows, rev


def main():
    """Print the landings of each capture."""
    ap = argparse.ArgumentParser()
    ap.add_argument("truth")
    ap.add_argument("captures", nargs="+")
    args = ap.parse_args()
    with open(args.truth, encoding="utf-8") as f:
        truth = pt.Truth.from_json(json.load(f))
    for path in tqdm(args.captures, unit="cap"):
        cap = Capture.load(path)
        names, tpos = byte_regions(truth, np.asarray(cap.data, np.uint8))
        tb, ts, rev = landings(cap)
        print(f"== {path} base={cap.base} rev={rev}")
        for label, rows in (("tb", tb), ("ts", ts)):
            for k, measure, p, m in rows:
                q = min(max(p, 0), len(names) - 1)
                miss = "" if m else "unmatched "
                print(
                    f"  {label} {k:5d} {measure:6d} -> {p:5d} {miss}{names[q]} @{tpos[q]}"
                )


if __name__ == "__main__":
    main()
