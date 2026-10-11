"""Syncs of saved stream captures at the pattern's sync regions.

Per capture, region and revolution: whether the stream reported the sync, the ones
latched before it (k), the reported run and the T2 span from SYNC_START to SYNC_END.
usage: python tools/sync_pulse.py TRUTH.json CAPTURE.npz [...] [--regions sync10.sync0]
"""

import argparse
import pathlib
import sys

import numpy as np
from tqdm import tqdm

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

# pylint: disable=wrong-import-position
from nybulah import stream as fmt  # noqa: E402
from nybulah.analysis.capture import trailing_ones  # noqa: E402
from nybulah.analysis.gcr import to_bits  # noqa: E402
from nybulah.nibbler import Capture  # noqa: E402
from nybulah.pattern import _load_truth  # noqa: E402  pylint: disable=protected-access

MATCH_BITS = 96


def region_ends(bits, truth, region):
    """Capture bit offsets where the exact bits before ``region`` end."""
    k = next(i for i, r in enumerate(truth.regions) if r.name == region)
    before = truth.regions[k - 1]
    end = before.offset + before.length
    tail = truth.bits[end - MATCH_BITS : end]
    view = np.lib.stride_tricks.sliding_window_view(bits, MATCH_BITS)
    return np.flatnonzero((view == tail).all(axis=1)) + MATCH_BITS


def spans(cap):
    """{position: (run, T2 span cycles, hi)} of the stream's reported syncs."""
    pos, est, _, hi = fmt.sync_bounds(cap.parsed.pos, cap.parsed.val)
    raw = est - fmt.START_T2[0] + sum(fmt.END_T2) / 2
    runs = dict(zip(cap.positions.tolist(), cap.sync_bits.tolist()))
    return {
        int(p): (runs.get(int(p)), float(s), float(h)) for p, s, h in zip(pos, raw, hi)
    }


def report(truth, path, regions):
    """Rows (capture, region, revolution, k, reported run, span, hi) for one capture."""
    cap = Capture.load(path)
    if cap.parsed is None:
        return [(path.name, "not a stream capture", 0, None, None, None, None)]
    bits = to_bits(cap.data)
    found = spans(cap)
    rows = []
    for region in regions:
        for rev, end in enumerate(region_ends(bits, truth, region)):
            first = (end + 1 + 7) // 8
            hit = next((p for p in range(first, first + 2) if p in found), None)
            k = int(trailing_ones(bits, [8 * hit])[0]) if hit else None
            run, span, hi = found[hit] if hit else (None, None, None)
            rows.append((path.name, region, rev, k, run, span, hi))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("truth", type=pathlib.Path)
    ap.add_argument("captures", nargs="+", type=pathlib.Path)
    ap.add_argument("--regions", default="sync10.sync0")
    args = ap.parse_args()
    truth = _load_truth(args.truth)
    regions = args.regions.split(",")
    print(f"{'capture':20} {'region':14} rev  k   run  span  hi")
    for path in tqdm(args.captures, unit="cap"):
        for name, region, rev, k, run, span, hi in report(truth, path, regions):
            if run is None:
                print(f"{name:20} {region:14} {rev:3}  missed")
            else:
                print(
                    f"{name:20} {region:14} {rev:3} {k:2} {run:5} {span:5.0f} {hi:4.0f}"
                )


if __name__ == "__main__":
    main()
