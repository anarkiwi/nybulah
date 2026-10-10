"""Alignment-free check of pattern captures: runs of ``W``-bit capture windows
absent from the written track (pattern then $55 filler to the cells, circular),
each with the region the alignment puts there.

usage: python tools/pattern_windows.py TRUTH.json CAPTURE.npz [...] [--width W]
"""

import argparse
import json

import numpy as np
from tqdm import tqdm

from nybulah.analysis import pattern as pt
from nybulah.nibbler import Capture


def windows(bits, width):
    """Integer of each ``width``-bit window of ``bits``."""
    weights = np.left_shift(np.uint64(1), np.arange(width, dtype=np.uint64)[::-1])
    view = np.lib.stride_tricks.sliding_window_view(bits.astype(np.uint64), width)
    return view @ weights


def track(truth):
    """The written revolution: the pattern then $55 filler to the cells."""
    cells = truth.cells or len(truth.bits)
    return np.concatenate((truth.bits, pt.gap_bits(cells - len(truth.bits))))


def absent_runs(c, truth, width):
    """``(start, end)`` capture bit spans covered by windows absent from the track."""
    t = track(truth)
    ref = np.unique(windows(np.concatenate((t, t[: width - 1])), width))
    bad = ~np.isin(windows(c, width), ref)
    edges = np.diff(np.concatenate(([0], bad.view(np.int8), [0])))
    starts, ends = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
    return list(zip(starts.tolist(), (ends + width - 1).tolist()))


def region_at(truth, al, j):
    """Pattern region name and position the alignment gives capture bit ``j``."""
    p = int(al.track_position(np.array([j]))[0])
    if 0 <= p < len(truth.bits):
        return truth.regions[truth.kinds()[p]].name, p
    return "filler", p


def main():
    """Print absent-window runs per capture."""
    ap = argparse.ArgumentParser()
    ap.add_argument("truth")
    ap.add_argument("captures", nargs="+")
    ap.add_argument("--width", type=int, default=32)
    args = ap.parse_args()
    with open(args.truth, encoding="utf-8") as f:
        truth = pt.Truth.from_json(json.load(f))
    for path in tqdm(args.captures, unit="cap"):
        cap = Capture.load(path)
        c, _, _ = pt.capture_bits(cap)
        al = pt.align(c, truth, period=truth.cells, sync_error=cap.sync_error)
        runs = absent_runs(c, truth, args.width)
        bad = sum(b - a for a, b in runs)
        print(f"{path}: {len(c)} bits, {len(runs)} absent runs, {bad} bits")
        for a, b in runs:
            name, p = region_at(truth, al, a)
            print(f"  c[{a}:{b}] ({b - a} bits) ~ {name} @ {p}")


if __name__ == "__main__":
    main()
