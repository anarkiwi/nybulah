"""Byte-ready margins of drive/stream.s on the timed 1571 simulator.

Per read site (stream.s label): reads, longest arrival-to-read wait, shortest
read-to-next-arrival margin (--context: the reads and writes before it); then each
lost byte. Usage: python tools/stream_margin.py [--zones 3] [--kind index|syncs]
"""

import argparse
import bisect
import os
import pathlib
import subprocess
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor

import numpy as np
from tqdm import tqdm

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

# pylint: disable=wrong-import-position,wrong-import-order,import-error
# pylint: disable=protected-access
from test_stream import rig  # noqa: E402

from nybulah.analysis.gcr import bits_per_revolution  # noqa: E402
from nybulah.simdisk import Media, sync_track  # noqa: E402

STREAM_MHZ = 2  # stream.s cycles per microsecond
MONITOR = 0x0500  # monitor.s origin: SDR writes below it are stream.s's
SYNC_RUNS = [10, 11, 12, 14, 20, 40, 80, 300, 1500]


def labels():
    """(addresses, names) of stream.s's labels as linked at $0300, sorted."""
    with tempfile.TemporaryDirectory() as tmp:
        obj, lbl = f"{tmp}/s.o", f"{tmp}/s.lbl"
        drive = ROOT / "drive"
        subprocess.run(
            ["ca65", "-t", "none", "-g", "-o", obj, "stream.s"], check=True, cwd=drive
        )
        subprocess.run(
            ["ld65", "-C", "stream.cfg", "-S", "$0300", "-Ln", lbl, "-o", os.devnull]
            + [obj],
            check=True,
            cwd=drive,
        )
        with open(lbl, encoding="ascii") as f:
            rows = sorted(
                (int(a, 16), n.lstrip("."))
                for _, a, n in (line.split() for line in f)
                if not n.startswith(".__")
            )
    return [a for a, _ in rows], [n for _, n in rows]


def track(zone, written, kind, phase):
    """Zone's cells at ``written`` rpm: one sync then GCR (index) or random syncs."""
    cells = int(round(bits_per_revolution(zone, written)))
    rng = np.random.default_rng(phase)
    if kind == "syncs":
        bits = sync_track(rng.choice(SYNC_RUNS, 30).tolist(), cells, phase, (1, 30))
    else:
        bits = sync_track([40], cells, seed=phase, gaps=(4, 5))
    return np.roll(bits, phase * cells // 7 + phase)


def record(cbm, mech):
    """Hook the drive: byte arrivals, VIA2 port A reads and stream.s SDR writes,
    in drive cycles at 2 MHz (arrivals while the drive runs at 2 MHz)."""
    drive, log = cbm.drive, {"arrivals": [], "reads": [], "writes": []}
    event, read, sdr = mech._event, mech.read, drive.cia.write

    def on_event(j):
        event(j)
        if drive.cyc < 1:
            log["arrivals"].append(mech._time(j + 1) * STREAM_MHZ)

    def on_read(fdc, reg, cycles, pc=-1):
        if not fdc and reg in (1, 15):
            log["reads"].append((drive.time(cycles) * STREAM_MHZ, drive.mpu.pc))
        return read(fdc, reg, cycles, pc)

    def on_sdr(reg, value, c):
        if reg == 12 and drive.mpu.pc < MONITOR:
            log["writes"].append((drive.time(c) * STREAM_MHZ, drive.mpu.pc))
        return sdr(reg, value, c)

    mech._event, mech.read, drive.cia.write = on_event, on_read, on_sdr
    return log


def match(arrivals, reads, events, edges):
    """Each read's byte: [(read pc, lag, margin, context)], [(lost, read pc)]."""
    sites, lost = [], []
    done = bisect.bisect_right(arrivals, reads[0][0]) - 2
    for c, pc in reads:
        k = bisect.bisect_right(arrivals, c) - 1
        if k <= done:
            continue
        for a in arrivals[done + 1 : k]:
            lost.append((a - edges[bisect.bisect_right(edges, a) - 1], pc))
        nxt = arrivals[k + 1] - c if k + 1 < len(arrivals) else np.inf
        e = bisect.bisect_left(events, (c,))
        context = [(t - c, kind, p) for t, kind, p in events[max(e - 6, 0) : e]]
        sites.append((pc, c - arrivals[k], nxt, context))
        done = k
    return sites, lost


def run(args):
    """(byte period, read sites, lost bytes) of one stream: see :func:`match`."""
    zone, rpm, written, kind, phase = args
    media = Media({(0, 2): track(zone, written, kind, phase)}, rpm=rpm)
    cbm, mech, nib = rig(media, halftrack=4)
    log = record(cbm, mech)
    nib.seek(2)
    for v in log.values():
        v.clear()
    nib.stream(2, revolutions=3)
    first, last = log["writes"][0][0], log["writes"][-1][0]
    arrivals = [a for a in log["arrivals"] if first <= a <= last]
    reads = [r for r in log["reads"] if first <= r[0] <= last]
    events = sorted(
        [(c, "read", pc) for c, pc in reads]
        + [(c, "write", pc) for c, pc in log["writes"]]
    )
    turn = int(media.turns(first / STREAM_MHZ))
    edges = [media.time_at(k) * STREAM_MHZ for k in range(turn, turn + 5)]
    period = float(np.median(np.diff(arrivals))) if len(arrivals) > 1 else 0.0
    return (period, *match(arrivals, reads, events, edges))


def main():
    """Print per-site lags and margins, and every lost byte."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--zones", default="3")
    ap.add_argument("--rpm", type=float, default=310.0)
    ap.add_argument("--written-rpm", type=float, default=300.0)
    ap.add_argument("--kind", choices=("index", "syncs"), default="index")
    ap.add_argument("--phases", type=int, default=8)
    ap.add_argument("--jobs", type=int, default=os.cpu_count())
    ap.add_argument(
        "--context", action="store_true", help="events before each worst read"
    )
    args = ap.parse_args()
    addrs, names = labels()

    def site(pc):
        return names[max(bisect.bisect_right(addrs, pc - 1) - 1, 0)]

    jobs = [
        (int(z), args.rpm, args.written_rpm, args.kind, p)
        for z in args.zones.split(",")
        for p in range(args.phases)
    ]
    stats = {}
    with ProcessPoolExecutor(args.jobs) as ex:
        results = list(tqdm(ex.map(run, jobs), total=len(jobs), unit="stream"))
    for (zone, *_), (period, sites, lost) in zip(jobs, results):
        z = stats.setdefault(zone, {"period": period, "sites": {}, "lost": []})
        for pc, lag, nxt, context in sites:
            s = z["sites"].setdefault(site(pc), [0, 0.0, np.inf, []])
            if nxt < s[2]:
                s[2], s[3] = nxt, context
            s[0], s[1] = s[0] + 1, max(s[1], lag)
        z["lost"] += [(a, site(pc)) for a, pc in lost]
    for zone, z in sorted(stats.items()):
        print(f"zone {zone}: byte period {z['period']:.1f} cycles")
        print(f"  {'site':8} {'reads':>8} {'max lag':>8} {'margin':>8}")
        for name, (n, lag, nxt, context) in sorted(
            z["sites"].items(), key=lambda kv: kv[1][2]
        ):
            print(f"  {name:8} {n:8} {lag:8.1f} {nxt:8.1f}")
            if args.context:
                for t, kind, p in context:
                    print(f"           {t:7.1f} {kind:5} at {site(p)}")
        print(f"  lost {len(z['lost'])}")
        for a, name in z["lost"][:20]:
            print(f"    arrival {a:.1f} after an index edge, read past at {name}")


if __name__ == "__main__":
    main()
