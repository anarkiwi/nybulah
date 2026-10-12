"""G64/G71 imaging through a Nibbler: raw track captures, and raw track writes fitted
to the drive's revolution and verified by re-capture."""

import numpy as np
from tqdm import tqdm

from . import passes
from . import stream as fmt
from .analysis.cycle import MEASURED_TOLERANCE, TrackKind
from .analysis.cycle import extract_revolution, find_cycle
from .analysis.gcr import NOMINAL_RPM, SYNC_MIN_BITS, bit_rate, runs_of_ones
from .analysis.gcr import to_bits, to_bytes
from .analysis.regions import agreement, canonical_keep, ranges
from .analysis.sector import GAP_BYTE
from .disk import SIDE1_TRACK_BASE, Archive, TrackJob, calibrate, cells_at
from .disk import revolution_cells, revolution_stream
from .formats.convert import _score
from .formats.g64 import SIDE1, table_entry
from .formats.image import DiskImage, from_nibbler, revolution, to_g64, track_zone
from .nibbler import CELLS_PER_BYTE, MAX_HALFTRACK, TrackError

SYNC_LOW_LAG = 8  # an n-one run holds SYNC low for about n - 8 cells (protocol.md)
MIN_GAP_BYTES = 1
# Byte 0 enters the shift register at the second byte ready after the write sees
# the index, a capture from the index latches its first byte within a byte of
# seeing it, and either index poll may see the edge up to a byte late.
ALIGN_BITS = 4 * CELLS_PER_BYTE
ALIGNS = ("none", "index")


def raw_jobs(tracks, sides=(0,), halftracks=False):
    """Every track (and with ``halftracks`` the halftrack inside it) of each side."""
    return [
        TrackJob(side, ht // 2, ht // 2 + (SIDE1_TRACK_BASE if side else 0), ht % 2)
        for side in sides
        for ht in range(2, 2 * tracks + 1 + halftracks, 1 if halftracks else 2)
    ]


def _clean(cap, key):
    """A capture whose revolution repeats and decodes without sector errors."""
    return _score(*revolution(cap), key)[:2] == (TrackKind.FORMATTED, 0)


def read_raw(nib, jobs, retries=2, archive=None, progress=True):
    """Raw captures of every job, located and calibrated as for a sector read.

    Each track is captured until a capture is clean, at most ``retries + 1`` times.
    """
    archive = archive if isinstance(archive, Archive) else Archive(archive)
    calibrate(nib, archive)
    image = DiskImage("capture")
    for job in tqdm(jobs, desc="read", unit="trk", disable=not progress):
        caps = image.tracks[job.key] = []
        for _ in range(retries + 1):
            cap = archive("read", job, nib.scan(job.halftrack, side=job.side))
            caps.append(from_nibbler(cap)[1])
            if _clean(caps[-1], job.key):
                break
    return image


def read_g64(  # pylint: disable=too-many-arguments
    nib, tracks=35, sides=(0,), halftracks=False, unformatted=None, **kw
):
    """G64 (G71 with ``sides`` (0, 1)) of the best revolution of every track;
    ``unformatted`` gets the keys of tracks with no repeating revolution."""
    if 1 in sides and nib.model != "1571":
        raise ValueError("G71 needs a 1571")
    image = read_raw(nib, raw_jobs(tracks, sides, halftracks), **kw)
    return to_g64(image, unformatted=unformatted)


def sync_floor(density, streaming=False):
    """Shortest sync the verifying capture is sure to report at ``density``: SYNC
    low for longer than a RAM TS pass's widest gap between PB7 polls (at most
    DRIFT slower than measured), or a stream's longest wait for a SYNC read, at
    the fastest motor."""
    wait = (
        fmt.START_LATE / fmt.CPU_HZ
        if streaming
        else passes.TS_PB7_GAP / passes.CPU_HZ / (1 - passes.DRIFT)
    )
    cells = wait * bit_rate(density) * passes.RPM_MAX / NOMINAL_RPM
    return SYNC_LOW_LAG + int(np.ceil(cells))


def shave(lengths, floor, amount):
    """Units to take from each run, ``amount`` in all, longest runs first and none
    left shorter than ``floor``."""
    lengths = np.asarray(lengths, np.int64)
    cut = np.zeros(len(lengths), np.int64)
    if amount <= 0:
        return cut
    lo, hi = floor, int(lengths.max())
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if np.maximum(lengths - mid, 0).sum() >= amount:
            lo = mid
        else:
            hi = mid - 1
    cut = np.maximum(lengths - lo, 0)
    cut[np.flatnonzero(cut)[: cut.sum() - amount]] -= 1
    return cut


def _gaps(bits, starts, ends):
    """``(bytes, fill)`` of the gap before each sync: the bits before its run that
    repeat every byte, after the previous sync, and the fill byte as framed there."""
    n = len(bits)
    same = np.tile(bits == np.roll(bits, -CELLS_PER_BYTE), 2)
    last = np.maximum.accumulate(np.where(same, -1, np.arange(2 * n)))
    run = (starts - CELLS_PER_BYTE - 1) % n + n
    space = (starts - np.roll(ends, 1)) % n
    width = np.minimum(CELLS_PER_BYTE + np.minimum(run - last[run], n), space)
    first = starts - width + (np.roll(ends, 1) - starts + width) % CELLS_PER_BYTE
    first = np.where(first + CELLS_PER_BYTE <= starts, first, starts - CELLS_PER_BYTE)
    fill = to_bytes(bits[(first[:, None] + np.arange(CELLS_PER_BYTE)) % n].ravel())
    return width // CELLS_PER_BYTE, fill


def gap_fill(data):
    """Fill byte of a track's longest gap; the DOS gap byte without one."""
    bits = to_bits(data)
    starts, lengths = runs_of_ones(bits, circular=True)
    if starts.size == 0:
        return GAP_BYTE
    if lengths[0] >= len(bits):
        return 0xFF
    width, fill = _gaps(bits, starts, starts + lengths)
    return int(fill[np.argmax(width)])


def fit_track(data, capacity, min_sync=SYNC_MIN_BITS):
    """A track's bytes shortened to at most ``capacity`` bytes, never touching data.

    Sync runs are trimmed first, longest first and to no fewer than ``min_sync``
    ones, then the gaps before syncs, whole fill bytes at a time and longest
    first, to no fewer than MIN_GAP_BYTES; TrackError if that is not enough.
    """
    data = np.asarray(data, np.uint8)
    excess = CELLS_PER_BYTE * (len(data) - capacity)
    if excess <= 0:
        return data
    bits = to_bits(data)
    n = len(bits)
    starts, lengths = runs_of_ones(bits, circular=True)
    lengths = np.minimum(lengths, n)
    if len(starts) and lengths[0] < n:
        width, _ = _gaps(bits, starts, starts + lengths)
    else:
        width = np.zeros(len(starts), np.int64)
    sync_room = int(np.maximum(lengths - min_sync, 0).sum())
    gap_room = int(np.maximum(width - MIN_GAP_BYTES, 0).sum())
    gap_bytes = -(-max(excess - sync_room, 0) // CELLS_PER_BYTE)
    if gap_bytes > gap_room:
        raise TrackError(
            f"{len(data)} bytes do not fit a {capacity} byte revolution: syncs and"
            f" gaps give {(sync_room // CELLS_PER_BYTE) + gap_room} bytes"
        )
    sync_cut = shave(lengths, min_sync, excess - CELLS_PER_BYTE * gap_bytes)
    gap_cut = CELLS_PER_BYTE * shave(width, MIN_GAP_BYTES, gap_bytes)
    drop = np.concatenate((ranges(starts, sync_cut), ranges(starts - gap_cut, gap_cut)))
    return to_bytes(np.delete(bits, drop % n))


def _index_revolution(cap):
    """``(revolution, cycle, bit of the revolution at the capture's index)``; the index
    of a byte-ready capture is its bit 0, valid when it began at the index."""
    if cap.framed is None:
        rev, cycle = revolution(cap)
        return rev, cycle, 0 if cap.revolutions else None
    cycle = find_cycle(cap.framed, cap.zone)
    if cycle.kind != TrackKind.FORMATTED:
        return (*revolution(cap), None)
    rev = extract_revolution(cap.framed, cycle)
    return rev, cycle, -cycle.start % len(rev)


def placed(capture, payload, fill):
    """Bit offset of ``payload`` after the index in a nibbler capture's revolution
    (0 for a killer track), or None unless the payload is there whole.

    Revolution and payload are compared with every sync cut to SYNC_MIN_BITS,
    ``fill`` standing for the filler around the payload; only payload bits count.
    """
    rev, cycle, index = _index_revolution(from_nibbler(capture)[1])
    probe = to_bits(np.append(payload, np.uint8(fill)))
    if probe.all() or cycle.kind == TrackKind.KILLER:
        return 0 if probe.all() and cycle.kind == TrackKind.KILLER else None
    keep, keep_ref = canonical_keep(probe), canonical_keep(rev)
    if keep.sum() > keep_ref.sum():
        return None
    _, _, lag, miss = agreement(rev[keep_ref], probe[keep])
    if miss[: int(keep[: CELLS_PER_BYTE * len(payload)].sum())].any():
        return None
    if index is None:
        return 0
    start = np.flatnonzero(keep_ref)[lag] - np.flatnonzero(keep)[0]
    offset = (start - index) % len(rev)
    return offset - len(rev) if 2 * offset > len(rev) else offset


def write_jobs(g64):
    """``(jobs, skipped keys)``: the whole tracks of an image a drive can write."""
    jobs, skipped = [], []
    for key in sorted(g64.tracks, key=table_entry):
        half, side = key & ~SIDE1, int(bool(key & SIDE1))
        if half % 2 or not 2 <= half <= MAX_HALFTRACK:
            skipped.append(key)
            continue
        jobs.append(TrackJob(side, half // 2, half // 2 + side * SIDE1_TRACK_BASE))
    return jobs, skipped


def plan(track, cells0, aligned, streaming=False):
    """``(density, payload, fill, cells)`` of a G64 track for a drive writing
    ``cells0`` cells per revolution at density 0, its syncs kept long enough for
    the verifying capture (``streaming``: a stream) to report."""
    density = track_zone(track.speed)
    cells = cells_at(cells0, density)
    capacity = int(cells * (1 - MEASURED_TOLERANCE) // CELLS_PER_BYTE)
    if aligned:
        capacity -= capacity % 256
    payload = fit_track(track.data, capacity, sync_floor(density, streaming))
    return density, payload, gap_fill(payload), cells


def _verify_capture(
    nib, job, density, synced, aligned
):  # pylint: disable=too-many-arguments
    """A capture of a written track: from the index when aligned (RAM passes, as
    the write needs the expansion RAM anyway), else a stream, else from a sync
    when the track has one, else from now with the bytes alone."""
    if nib.streaming and not aligned:
        return nib.stream(job.halftrack, side=job.side, density=density)
    start = "index" if aligned else "sync" if synced else "now"
    timing = "syncs" if synced else "none"
    return nib.capture(job.halftrack, density, start, job.side, timing)


def _write_job(
    nib, job, track, retries, archive, aligned
):  # pylint: disable=too-many-arguments
    """Attempts until verify passed, or None."""
    density, payload, fill, cells = track
    synced = runs_of_ones(to_bits(payload), circular=True)[0].size > 0
    if aligned:
        erase = revolution_stream(b"", cells, fill=fill)
        stream = bytes(payload) + bytes([fill]) * (-len(payload) % 256)
    else:
        stream = revolution_stream(payload, cells, fill=fill)
    for attempt in range(1, retries + 2):
        if aligned:
            nib.write_track(job.halftrack, erase, density, job.side)
            nib.write_track(job.halftrack, stream, density, job.side, "index")
        else:
            nib.write_track(job.halftrack, stream, density, job.side)
        cap = _verify_capture(nib, job, density, synced, aligned)
        offset = placed(archive("verify", job, cap), payload, fill)
        if offset is not None and (not aligned or 0 <= offset <= ALIGN_BITS):
            return attempt
    return None


def write_g64(  # pylint: disable=too-many-arguments
    nib, g64, align="none", retries=2, archive=None, progress=True
):
    """Write every whole track of a G64/G71 at its own density and verify each by
    re-capture; ``align`` "index" (1571) writes each track's byte 0 at the index.

    Returns per-track attempt records, the measured rpm and the skipped keys.
    """
    if align not in ALIGNS:
        raise ValueError(f"align must be one of {ALIGNS}")
    aligned = align == "index"
    if aligned and nib.model != "1571":
        raise ValueError(
            f"index alignment needs a 1571: a {nib.model} has no index sensor"
        )
    jobs, skipped = write_jobs(g64)
    if not jobs:
        raise ValueError("the image has no whole tracks to write")
    if any(j.side for j in jobs) and nib.model != "1571":
        raise ValueError("second side tracks need a 1571")
    archive = archive if isinstance(archive, Archive) else Archive(archive)
    first = jobs[0]
    cells0 = revolution_cells(nib, first.halftrack, archive, side=first.side)
    plans = [plan(g64.tracks[j.key], cells0, aligned, nib.streaming) for j in jobs]
    report = []
    for job, track in tqdm(
        list(zip(jobs, plans)), desc="write", unit="trk", disable=not progress
    ):
        attempts = _write_job(nib, job, track, retries, archive, aligned)
        trimmed = len(g64.tracks[job.key].data) - len(track[1])
        report.append(
            {
                "side": job.side,
                "track": job.track,
                "attempts": attempts,
                "trimmed": max(trimmed, 0),
            }
        )
    return {
        "rpm": 60.0 * bit_rate(0) / cells0,
        "tracks": report,
        "skipped": skipped,
    }
