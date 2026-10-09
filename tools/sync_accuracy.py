"""Simulated sync-length accuracy and byte loss of Nibbler captures.

Usage: python tools/sync_accuracy.py [--seeds N] [--models 1541 1571] [--jobs N]
Each case captures syncs of 10-1000 bits and compares the merged capture
with the simulator's log: bytes lost, syncs missed or invented, errors.
"""

import argparse
import collections
import concurrent.futures
import itertools
import json
import os

import numpy as np
from tqdm import tqdm

from nybulah.analysis.gcr import bits_per_revolution
from nybulah.nibbler import Nibbler
from nybulah.simdisk import Media, disk_drive, log_bytes, sync_track
from nybulah.simhost import SimMonitor
from nybulah.simdisk import true_syncs

RUNS = list(range(10, 21)) + [24, 28, 32, 40, 48, 64, 80, 100, 128, 200, 255, 256]
RUNS += [300, 400, 500, 640, 800, 1000]
HALFTRACK = {3: 10, 2: 40, 1: 52, 0: 64}


def capture_case(model, zone, rpm, wander, start, seed):
    """One capture, whether it lost a byte, and the true syncs in it."""
    cells = int(round(bits_per_revolution(zone)))
    track = sync_track(RUNS * 2, cells, seed=seed, gaps=(1, 24))
    media = Media({(0, HALFTRACK[zone]): track}, rpm=rpm, wander=(wander, 2.0))
    drive = disk_drive(model, media)
    nib = Nibbler(
        SimMonitor(drive), model, stepms=1, settle_ms=1, spinup_s=0, sleep=lambda s: 0
    ).open()
    nib.halftrack = 36
    drive.mech.log = []
    cap = nib.capture(HALFTRACK[zone], density=zone, start=start)
    stream, _ = log_bytes(drive.mech.log)
    lost = stream.find(cap.data.tobytes()) < 0
    pos, runs = true_syncs(drive.mech.log, cap.data) if not lost else ([], [])
    return cap, lost, np.asarray(pos), np.asarray(runs)


def case_tally(args):
    """Counts, run-length errors and bound widths of one capture case."""
    out, errors, widths = (collections.Counter() for _ in range(3))
    cap, lost, pos, runs = capture_case(*args)
    out["captures"] += 1
    out["lost"] += int(lost)
    if lost:
        return out, errors, widths
    found = np.isin(pos, cap.positions)
    out["missed"] += int((~found).sum())
    out["invented"] += int((~np.isin(cap.positions, pos)).sum())
    out["syncs"] += int(found.sum())
    idx = np.searchsorted(cap.positions, pos[found])
    true = runs[found]
    lo, hi = cap.sync_bounds[0][idx], cap.sync_bounds[1][idx]
    out["outside"] += int(((true < lo) | ((hi >= 0) & (true > hi))).sum())
    errors.update((cap.sync_bits[idx] - true).tolist())
    widths.update(np.where(hi >= 0, hi - lo, -1).tolist())
    return out, errors, widths


def tally(cases, jobs=1):
    """Counts, run-length error histogram and bound widths over the cases."""
    totals = [collections.Counter() for _ in range(3)]
    with concurrent.futures.ProcessPoolExecutor(jobs) as pool:
        results = pool.map(case_tally, cases, chunksize=4)
        for parts in tqdm(results, total=len(cases), desc="captures", unit="cap"):
            for total, part in zip(totals, parts):
                total.update(part)
    out, errors, widths = totals
    return dict(out), dict(sorted(errors.items())), dict(sorted(widths.items()))


def main(argv=None):
    """Run the grid and print the tallies as JSON."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seeds", type=int, default=1)
    ap.add_argument("--models", nargs="+", default=["1541", "1571"])
    ap.add_argument("--rpm", type=float, nargs="+", default=[297.0, 300.0, 303.0])
    ap.add_argument("--wander", type=float, nargs="+", default=[0.0, 3.0])
    ap.add_argument("--jobs", type=int, default=os.cpu_count())
    args = ap.parse_args(argv)
    cases = [
        (m, z, r, w, s, seed)
        for m, z, r, w, seed in itertools.product(
            args.models, range(4), args.rpm, args.wander, range(args.seeds)
        )
        for s in (("now", "sync", "index") if m == "1571" else ("now", "sync"))
    ]
    counts, errors, widths = tally(cases, args.jobs)
    print(json.dumps({"counts": counts, "errors": errors, "widths": widths}))


if __name__ == "__main__":
    main()
