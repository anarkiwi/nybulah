"""Build a G64 from a ``read --archive`` folder with every seam cut inside a sync.

Each revolution runs from inside a sync to the same point of that sync's copy
one index period later, so the seam falls in a sync and the length is exact.
Usage: ``python tools/captures_g64.py OUT.g64 CAPS/read-*.npz [MORE/GLOB ...]``.
"""

import argparse
import json
import pathlib

import numpy as np
from tqdm import tqdm

from nybulah.analysis.gcr import bits_per_revolution, runs_of_ones
from nybulah.analysis.sector import SectorError, decode_track
from nybulah.formats.g64 import G64, G64Track, write_g64
from nybulah.formats.image import best_revolution, load, revolution_bytes
from nybulah.survey import load_captures

TOLERANCE = 64


def sync_cuts(bits, period, tolerance=TOLERANCE):
    """``(start, stop)`` revolutions from mid-sync to the same point of the sync
    whose end lies nearest one ``period`` later, best-matched first."""
    starts, lengths = runs_of_ones(bits)
    if len(starts) < 2:
        return np.zeros((0, 2), np.int64)
    ends = starts + lengths
    j = np.clip(np.searchsorted(ends, ends + period), 1, len(ends) - 1)
    j = np.where(
        np.abs(ends[j - 1] - ends - period) < np.abs(ends[j] - ends - period), j - 1, j
    )
    drift = np.abs(ends[j] - ends - period)
    back = np.minimum(lengths, lengths[j]) // 2
    keep = (drift <= tolerance) & (j > np.arange(len(ends)))
    order = np.argsort(drift[keep], kind="stable")
    cut = (ends - back)[keep][order]
    return np.stack([cut, (ends[j] - back)[keep][order]], axis=1)


def errors(bits, track):
    """Sector errors of one circular revolution."""
    return int((decode_track(bits, track).errors != SectorError.OK).sum())


def best_cut(capture, track, limit=16):
    """``(errors, bits)`` of the best sync-cut revolution, or None."""
    if capture.index is None or len(capture.index) < 2:
        return None
    period = int(round(np.diff(np.asarray(capture.index)).mean()))
    bits = np.asarray(capture.bits, np.uint8)
    cuts = sync_cuts(bits, period)[:limit]
    scored = ((errors(bits[a:b], track), bits[a:b]) for a, b in cuts)
    return min(scored, key=lambda s: s[0], default=None)


def window_lags(bits, period, width=256, reach=64, tie=0.005):
    """Per ``width``-bit window, the lag from ``period`` at which the next
    revolution agrees best (ties to the lag nearest 0) and that agreement."""
    lags = np.arange(-reach, reach + 1)
    starts = np.arange(0, len(bits) - period - width - reach, width)
    if len(starts) == 0:
        return starts, lags[:0], np.zeros(0)
    here = bits[starts[:, None] + np.arange(width)]
    there = bits[(starts + period)[:, None, None] + lags[:, None] + np.arange(width)]
    agree = (here[:, None, :] == there).mean(axis=2)
    near = np.where(
        agree >= agree.max(axis=1, keepdims=True) - tie, -np.abs(lags), -reach - 1
    )
    pick = near.argmax(axis=1)
    return starts, lags[pick], agree[np.arange(len(starts)), pick]


def self_period(bits, nominal, spread=0.2, tail=2048):
    """Shift in ``nominal`` +/- ``spread`` at which ``bits`` best agrees with
    itself (FFT autocorrelation normalised by overlap), or None."""
    lo, hi = int(nominal * (1 - spread)), min(
        int(nominal * (1 + spread)), len(bits) - tail
    )
    if hi <= lo:
        return None
    x = bits.astype(np.float64) * 2 - 1
    size = 1 << int(np.ceil(np.log2(2 * len(x))))
    spec = np.fft.rfft(x, size)
    corr = np.fft.irfft(spec * np.conj(spec), size)[: len(x)]
    corr = corr / (len(x) - np.arange(len(x)))
    return lo + int(np.argmax(corr[lo:hi]))


def repeat_cut(capture, floor=0.99):
    """``(agreement, bits)`` of one revolution of a sync-less capture: the mode
    of the window lags fixes its length, the seam sits mid-run of that lag."""
    bits = np.asarray(capture.bits, np.uint8)
    if capture.index is not None and len(capture.index) >= 3:
        period = int(round(np.diff(np.asarray(capture.index)).mean()))
    else:
        period = self_period(bits, bits_per_revolution(capture.zone))
    if period is None:
        return None
    starts, lags, agree = window_lags(bits, period)
    good = agree >= floor
    if not good.any():
        return None
    lag = int(np.bincount(lags[good] - lags.min()).argmax() + lags.min())
    ok = np.concatenate(([0], (good & (lags == lag)).astype(np.int8), [0]))
    edges = np.flatnonzero(np.diff(ok))
    lo, hi = max(zip(edges[::2], edges[1::2]), key=lambda r: r[1] - r[0])
    seam = int(starts[(lo + hi) // 2])
    return float(agree[good].mean()), bits[seam : seam + period + lag]


def whole_bytes(rev):
    """G64 bytes of a revolution trimmed at its seam to a whole number of bytes."""
    return np.packbits(rev[: len(rev) - len(rev) % 8])


def build(captures):
    """``(G64, report)`` from ``{key: [Capture]}``."""
    out, report = G64(), {}
    for key in tqdm(sorted(captures), desc="g64", unit="trk"):
        track = (key & 0x7F) // 2
        cuts = [c for c in (best_cut(cap, track) for cap in captures[key]) if c]
        cap, bits, cycle = best_revolution(captures[key], key)
        err, data, how = errors(bits, track), revolution_bytes(bits, cycle), "index"
        if cuts:
            best = min(cuts, key=lambda c: c[0])
            if best[0] <= err:
                err, data, how = best[0], whole_bytes(best[1]), "sync-cut"
        elif len(runs_of_ones(bits)[0]) == 0:
            reps = [r for r in (repeat_cut(c) for c in captures[key]) if r]
            if reps:
                agreement, rev = max(reps, key=lambda r: r[0])
                data, how = whole_bytes(rev), f"repeat-cut {agreement:.4f}"
        speed = cap.speed if cap.speed is not None else cap.zone
        out.tracks[key] = G64Track(data, speed)
        report[key] = {
            "track": key / 2,
            "errors": err,
            "cut": how,
            "kind": cycle.kind.name,
            "match": round(float(cycle.match), 4),
            "bytes": len(data),
        }
    return out, report


def merged(sources, max_track):
    """Captures of every ``DIR/GLOB`` source, a later source replacing a track."""
    captures = {}
    for source in sources:
        folder, pattern = source.rsplit("/", 1) if "/" in source else (".", source)
        captures |= load_captures(folder, pattern)
    return {k: v for k, v in captures.items() if (k & 0x7F) // 2 <= max_track}


def take(g64, report, image_path, tracks):
    """Copy ``tracks`` from another image's best revolutions into ``g64``."""
    image = load(image_path)
    for track in tracks:
        key = 2 * track
        cap, bits, cycle = best_revolution(image.tracks[key], key)
        speed = cap.speed if cap.speed is not None else cap.zone
        g64.tracks[key] = G64Track(revolution_bytes(bits, cycle), speed)
        report[key] = {
            "track": float(track),
            "errors": errors(bits, track),
            "cut": f"from {pathlib.Path(image_path).name}",
            "bytes": len(g64.tracks[key].data),
        }


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("out")
    ap.add_argument("sources", nargs="+", help="DIR/GLOB capture sets, later wins")
    ap.add_argument("--tracks", type=int, default=42, help="highest track kept")
    ap.add_argument("--take", help="image to copy --take-tracks from")
    ap.add_argument("--take-tracks", type=int, nargs="*", default=[])
    args = ap.parse_args()
    g64, report = build(merged(args.sources, args.tracks))
    if args.take:
        take(g64, report, args.take, args.take_tracks)
    with open(args.out, "wb") as f:
        f.write(write_g64(g64))
    for _, row in sorted(report.items()):
        print(json.dumps(row))


if __name__ == "__main__":
    main()
