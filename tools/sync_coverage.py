"""SYNC coverage of drive/stream.s on the timed 1571: the logged syncs the stream reports.

Per logged sync of the pattern track (--pattern, --cells) or random sync tracks
(--runs, --phases): ones n, ones latched before it k, the SYNC pulse's start and end
in cycles after the last SDR write, reported or missed; then the reported share per n.
"""

import argparse
import bisect
import pathlib
import sys
from concurrent.futures import ProcessPoolExecutor

import numpy as np
from tqdm import tqdm

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

# pylint: disable=wrong-import-position,wrong-import-order,import-error
from stream_margin import labels  # noqa: E402
from test_stream import rig, sdr_writes  # noqa: E402

from nybulah.analysis import pattern as pt  # noqa: E402
from nybulah.analysis.capture import trailing_ones  # noqa: E402
from nybulah.analysis.gcr import bits_per_revolution, to_bits  # noqa: E402
from nybulah.pattern import _load_truth  # noqa: E402  pylint: disable=protected-access
from nybulah.simdisk import Media, log_bytes, sync_track  # noqa: E402

STREAM_MHZ = 2


def pattern_track(halftrack, cells, truth_path=None):
    """The pattern then $55 filler to ``cells``."""
    truth = _load_truth(truth_path) if truth_path else pt.make_truth(halftrack)
    return np.concatenate((truth.bits, pt.gap_bits(cells - len(truth.bits))))


def streamed(halftrack, track, rpm, revolutions):
    """(capture, mechanism log, SDR write times, SYNC reads (time, pc)) of one stream."""
    cbm, mech, nib = rig(Media({(0, halftrack): track}, rpm=rpm), halftrack=halftrack)
    writes = sdr_writes(cbm)
    reads, read = [], mech.read

    def on_read(fdc, reg, cycles, pc=-1):
        if not fdc and reg == 0:
            reads.append((cbm.drive.time(cycles), cbm.drive.mpu.pc))
        return read(fdc, reg, cycles, pc)

    mech.read = on_read
    nib.seek(halftrack)
    mech.log = []
    del writes[:], reads[:]
    cap = nib.stream(halftrack, revolutions=revolutions)
    assert cap.stream_status == {"adapter": "done", "drive": "done"}, cap.stream_status
    return cap, mech.log, np.array([cbm.drive.time(c) for c in writes]), reads


def coverage(halftrack, track, rpm, revolutions=2):
    """Rows (n, k, pulse start, pulse end, reported, read gap, read site) per
    logged sync of one stream; start, end and the gap in cycles after the last
    SDR write before the pulse."""
    cap, log, wtimes, reads = streamed(halftrack, track, rpm, revolutions)
    data, times = log_bytes(log)
    want = bytes(cap.data)
    first = data.find(want[:32])
    assert first >= 0 and data[first : first + len(want)] == want
    reported = set(cap.positions.tolist())
    bits = to_bits(np.frombuffer(data, np.uint8))
    rtimes = np.array([t for t, _ in reads])
    addrs, names = labels()
    rows = []
    for _, start, end, ones in (e for e in log if e[0] == "sync"):
        pos = int(np.searchsorted(times, end)) - first
        if not 0 < pos < len(want):
            continue
        w = wtimes[np.searchsorted(wtimes, start) - 1]
        i = np.searchsorted(rtimes, start) - 1
        rows.append(
            (
                ones,
                int(trailing_ones(bits, [8 * (pos + first)])[0]),
                STREAM_MHZ * (start - w),
                STREAM_MHZ * (end - w),
                pos in reported,
                STREAM_MHZ * (rtimes[i + 1] - rtimes[i]),
                names[max(bisect.bisect_right(addrs, reads[i][1] - 1) - 1, 0)],
            )
        )
    return rows


def random_job(args):
    zone, runs, phase, rpm, written = args
    cells = int(round(bits_per_revolution(zone, written)))
    track = sync_track(np.random.default_rng(phase).choice(runs, 40).tolist(), cells)
    return coverage(4, np.roll(track, phase * 977), rpm)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pattern", type=int, help="halftrack of the pattern track")
    ap.add_argument("--cells", type=int, help="cells per revolution")
    ap.add_argument("--truth", type=pathlib.Path)
    ap.add_argument("--runs", default="10,11,12,13,14,16,20")
    ap.add_argument("--phases", type=int, default=8)
    ap.add_argument("--zone", type=int, default=3)
    ap.add_argument("--rpm", type=float, default=300.0)
    ap.add_argument("--written-rpm", type=float, default=300.0)
    ap.add_argument("--rows", action="store_true", help="print every sync")
    args = ap.parse_args()
    if args.pattern is not None:
        track = pattern_track(args.pattern, args.cells, args.truth)
        rows = coverage(args.pattern, track, args.rpm)
    else:
        runs = [int(r) for r in args.runs.split(",")]
        jobs = [
            (args.zone, runs, p, args.rpm, args.written_rpm) for p in range(args.phases)
        ]
        with ProcessPoolExecutor() as ex:
            rows = sum(
                tqdm(ex.map(random_job, jobs), total=len(jobs), unit="track"), []
            )
    if args.rows or args.pattern is not None:
        print(
            f"{'n':>5} {'k':>2} {'start':>7} {'end':>7} {'gap':>6}  {'site':8} reported"
        )
        for n, k, start, end, hit, gap, site in rows:
            flag = "yes" if hit else "MISSED"
            print(f"{n:5} {k:2} {start:7.1f} {end:7.1f} {gap:6.1f}  {site:8} {flag}")
    print(f"{'n':>5} {'syncs':>6} {'reported':>9}")
    for n in sorted({r[0] for r in rows}):
        hits = [r[4] for r in rows if r[0] == n]
        print(f"{n:5} {len(hits):6} {np.mean(hits):9.1%}")
    print("missed, by the read site before the pulse:")
    for site in sorted({r[6] for r in rows if not r[4]}):
        gaps = [r[5] for r in rows if r[6] == site and not r[4]]
        print(f"  {site:8} {len(gaps):5}  gap {min(gaps):5.1f} - {max(gaps):5.1f}")


if __name__ == "__main__":
    main()
