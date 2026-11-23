"""G64 images assembled from capture archives and patched from other images.

Every seam falls inside a sync: a revolution runs mid-sync to the same point
one index period later, a sector is replaced from mid header sync to mid of
the next header's sync, so each joint reads as one sync.
"""

import functools
import pathlib

import numpy as np
from tqdm import tqdm

from ..analysis.gcr import bits_per_revolution, runs_of_ones
from ..analysis.sector import SectorError, decode_track, sync_headers
from .g64 import G64, G64Track
from .image import (
    CAPTURE_PATTERN,
    best_revolution,
    load,
    load_captures,
    revolution_bytes,
    track_zone,
)

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
    """``(errors, bits)`` of the best sync-cut revolution, or None. A cut holding
    a sector header twice spans more than a revolution and is never chosen."""
    bits = np.asarray(capture.bits, np.uint8)
    period = capture_period(capture, track)
    if period is None:
        return None
    scored = []
    for a, b in sync_cuts(bits, period)[:limit]:
        dec = decode_track(bits[a:b], track)
        if dec.copies is None or not (dec.copies > 1).any():
            scored.append((int((dec.errors != SectorError.OK).sum()), bits[a:b]))
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


def header_period(bits, track):
    """Median distance between successive headers of the same sector of ``track``
    (checksum-valid, sync-framed), or None without a repeat."""
    ends, rows = sync_headers(bits)
    mine = rows[:, 3] == track
    sector, pos = rows[mine, 2], ends[mine]
    order = np.lexsort((pos, sector))
    sector, pos = sector[order], pos[order]
    gaps = np.diff(pos)[sector[1:] == sector[:-1]]
    return int(np.median(gaps)) if len(gaps) else None


def capture_period(capture, track=None):
    """Bits per revolution: the mean index period with two or more index marks,
    else the recurrence of ``track``'s sector headers, else the self-agreement
    shift near the zone's nominal length, or None."""
    if capture.index is not None and len(capture.index) >= 2:
        return int(round(np.diff(np.asarray(capture.index)).mean()))
    bits = np.asarray(capture.bits, np.uint8)
    period = header_period(bits, track) if track is not None else None
    return period or self_period(bits, bits_per_revolution(capture.zone))


def repeat_cut(capture, floor=0.99):
    """``(agreement, bits)`` of one revolution of a sync-less capture: the mode
    of the window lags fixes its length, the seam sits mid-run of that lag."""
    bits = np.asarray(capture.bits, np.uint8)
    period = capture_period(capture)
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


def sync_bytes(rev):
    """G64 bytes of a revolution whose seam is inside a sync: padded there with
    ones, so the sync only lengthens (trimming could take it below 10 bits)."""
    return np.packbits(np.concatenate([rev, np.ones(-len(rev) % 8, np.uint8)]))


def sector_span(bits, header_end, circular):
    """``(start, stop)`` from mid of the sync ending at ``header_end`` to mid of
    the second sync after it (data block sync, then the next header's); a
    circular stream's ``stop`` is unwrapped past its end. None if not found."""
    n = len(bits)
    starts, lengths = runs_of_ones(bits, circular=circular)
    ends, mids = starts + lengths, starts + lengths // 2
    hit = np.flatnonzero((ends % n if circular else ends) == header_end)
    if hit.size == 0 or len(mids) < 3:
        return None
    i = int(hit[0])
    if not circular:
        return (int(mids[i]), int(mids[i + 2])) if i + 2 < len(mids) else None
    start = int(mids[i]) % n
    return start, start + (int(mids[(i + 2) % len(mids)]) - start) % n


def replace_span(rev, span, piece):
    """Circular ``rev`` with ``span`` replaced by ``piece`` and ones padded after
    it (mid-sync) to keep the length modulo 8. Bit 0 stays where it was unless
    it lay in ``span``; then the revolution starts with ``piece``."""
    n, (start, stop) = len(rev), span
    rest = np.take(rev, np.arange(stop, start + n), mode="wrap")
    pad = np.ones((stop - start - len(piece)) % 8, np.uint8)
    shift = n - stop if stop <= n else len(rest)
    return np.roll(np.concatenate([rest, piece, pad]), -shift)


def splice(rev, track, sources, sectors=None):
    """``rev`` with ``sectors`` (default: those that do not decode) replaced,
    header sync to next header sync, from the first of ``sources``
    (``(bits, circular)``) where the sector decodes before and after splicing.

    Returns ``(rev, spliced sectors, [(sector, reason)] not spliced)``.
    """
    here = decode_track(rev, track)
    todo = np.flatnonzero(here.errors != SectorError.OK) if sectors is None else sectors
    donors = [(bits, circ, decode_track(bits, track)) for bits, circ in sources]
    done, failed = [], []
    for sector in map(int, todo):
        if not 0 <= sector < len(here.errors):
            failed.append((sector, "no such sector"))
            continue
        span = sector_span(rev, int(here.offsets[sector]), True)
        reason = "not decodable in donor"
        for bits, circ, good in donors:
            if good.errors[sector] != SectorError.OK:
                continue
            there = sector_span(bits, int(good.offsets[sector]), circ)
            if span is None or there is None:
                reason = f"sync not found in {'target' if span is None else 'donor'}"
                continue
            new = replace_span(rev, span, np.take(bits, np.arange(*there), mode="wrap"))
            after = decode_track(new, track)
            if after.errors[sector] != SectorError.OK:
                reason = "not decodable after splice"
                continue
            rev, here, reason = new, after, None
            done.append(sector)
            break
        if reason:
            failed.append((sector, reason))
    return rev, done, failed


def _speed(cap):
    return cap.speed if cap.speed is not None else cap.zone


def assemble(captures):
    """``(G64, report)`` from ``{key: [Capture]}``."""
    out, report = G64(), {}
    for key in tqdm(sorted(captures), desc="g64", unit="trk"):
        track = (key & 0x7F) // 2
        caps = captures[key]
        cuts = [c for c in (best_cut(cap, track) for cap in caps) if c]
        cap, bits, cycle = best_revolution(caps, key)
        err, data, row = errors(bits, track), revolution_bytes(bits, cycle), {}
        row["cut"] = "index"
        if cuts:
            best_err, rev = min(cuts, key=lambda c: c[0])
            if best_err:
                sources = [(np.asarray(c.bits, np.uint8), c.circular) for c in caps]
                rev, row["spliced"], failed = splice(rev, track, sources)
                if failed:
                    row["unspliced"] = [{"sector": s, "reason": r} for s, r in failed]
                best_err = errors(rev, track)
            if best_err <= err:
                err, data, row["cut"] = best_err, sync_bytes(rev), "sync-cut"
        elif len(runs_of_ones(bits)[0]) == 0:
            reps = [r for r in (repeat_cut(c) for c in caps) if r]
            if reps:
                agreement, rev = max(reps, key=lambda r: r[0])
                data, row["cut"] = whole_bytes(rev), "repeat-cut"
                row["agreement"] = round(agreement, 4)
        out.tracks[key] = G64Track(data, _speed(cap))
        report[key] = {"track": key / 2, "errors": err, **row, "bytes": len(data)}
    return out, report


def merged(sources, max_track):
    """Captures of every source (a directory of ``read-*.npz`` records or a
    ``DIR/GLOB``), a later source replacing a whole track."""
    captures = {}
    for source in map(str, sources):
        if pathlib.Path(source).is_dir():
            folder, pattern = source, CAPTURE_PATTERN
        else:
            folder, pattern = source.rsplit("/", 1) if "/" in source else (".", source)
        captures |= load_captures(folder, pattern)
    return {k: v for k, v in captures.items() if (k & 0x7F) // 2 <= max_track}


def _row(report, key, g64):
    row = report.setdefault(key, {"track": key / 2})
    if key in g64.tracks:
        bits = np.unpackbits(np.asarray(g64.tracks[key].data, np.uint8))
        row.update(errors=errors(bits, key // 2), bytes=len(g64.tracks[key].data))
    return row


def take(g64, report, image, name, tracks):
    """Replace ``tracks`` of ``g64`` with the best revolutions of ``image``'s."""
    for track in tracks:
        key = 2 * track
        caps = image.tracks.get(key)
        if not caps:
            failed = {"from": name, "reason": "no track in donor"}
            _row(report, key, g64).setdefault("failed", []).append(failed)
            continue
        cap, bits, cycle = best_revolution(caps, key)
        g64.tracks[key] = G64Track(revolution_bytes(bits, cycle), _speed(cap))
        report.pop(key, None)
        _row(report, key, g64).update(cut="take", source=name)


def patch_sectors(g64, report, image, name, picks):
    """Splice the ``{track: [sector]}`` ``picks`` into ``g64`` from ``image``."""
    for track, sectors in picks.items():
        key = 2 * track
        caps, target = image.tracks.get(key), g64.tracks.get(key)
        row = _row(report, key, g64)
        failed = row.setdefault("failed", [])
        if target is None or not caps:
            why = "no track in " + ("target" if target is None else "donor")
            failed.extend({"sector": s, "from": name, "reason": why} for s in sectors)
            continue
        _, rev, _ = best_revolution(caps, key)
        sources = [(np.asarray(rev, np.uint8), True)]
        sources += [
            (np.asarray(c.bits, np.uint8), False) for c in caps if not c.circular
        ]
        bits = np.unpackbits(np.asarray(target.data, np.uint8))
        bits, done, missed = splice(bits, track, sources, sectors)
        data, speed = np.packbits(bits), target.speed
        if np.ndim(speed) and len(speed) != len(data):
            speed = track_zone(speed)
        g64.tracks[key] = G64Track(data, speed)
        _row(report, key, g64).setdefault("patched", []).extend(
            {"sector": s, "from": name} for s in done
        )
        failed.extend({"sector": s, "from": name, "reason": r} for s, r in missed)


def patch(g64, report, tracks_from=(), sectors_from=()):
    """Apply ``(image path, [track])`` whole-track replacements, then
    ``(image path, {track: [sector]})`` sector splices, each in order; a donor
    image is loaded once. Returns ``report``."""
    donor = functools.lru_cache(maxsize=None)(load)
    for path, tracks in tqdm(tracks_from, desc="tracks", unit="img"):
        take(g64, report, donor(path), pathlib.Path(path).name, tracks)
    for path, picks in tqdm(sectors_from, desc="sectors", unit="img"):
        patch_sectors(g64, report, donor(path), pathlib.Path(path).name, picks)
    for row in report.values():
        if not row.get("failed", True):
            del row["failed"]
    return report
