"""Synthetic drive captures of known tracks, for testing and calibration."""

import numpy as np

from .capture import SYNC_ERROR_BITS, ByteCapture
from .gcr import (
    CLOCK_HZ,
    NOMINAL_RPM,
    SYNC_MIN_BITS,
    encode,
    runs_of_ones,
    sectors_per_track,
    speed_zone,
    to_bits,
    track_capacity,
)
from .sector import (
    DATA_GCR_BYTES,
    HEADER_GAP_BYTES,
    HEADER_GCR_BYTES,
    SECTOR_BYTES,
    SYNC_BYTES,
    SectorError,
    format_track,
    header_blocks,
)


def simulate_capture(track_bits, length, start=0, noise=0.0, weak=None, rng=None):
    """Read ``length`` bits of a circular track beginning at bit ``start``.

    ``noise`` is the independent bit-flip probability and ``weak`` an optional
    ``(offset, size)`` track region that reads as fresh random bits every pass.
    """
    rng = np.random.default_rng(rng)
    track_bits = np.asarray(track_bits, dtype=np.uint8)
    pos = (start + np.arange(length)) % len(track_bits)
    out = track_bits[pos]
    if weak is not None:
        offset, size = weak
        region = (pos - offset) % len(track_bits) < size
        out[region] = rng.integers(0, 2, int(region.sum()), dtype=np.uint8)
    return out ^ (rng.random(length) < noise).astype(np.uint8)


def simulate_flux(
    track_bits, zone, revolutions, jitter=0.0, rpm=NOMINAL_RPM, phase=0.25, rng=None
):
    """Transition and index times (seconds) of a track written at ``zone``.

    Written at 300 rpm, read at ``rpm``; transitions sit ``phase`` cells into
    their bit cell plus Gaussian ``jitter`` (standard deviation in cells).
    """
    rng = np.random.default_rng(rng)
    track_bits = np.asarray(track_bits, dtype=np.uint8)
    cell = 4 * (16 - zone) / CLOCK_HZ * NOMINAL_RPM / rpm
    ones = np.flatnonzero(track_bits)
    starts = len(track_bits) * np.arange(revolutions)
    cells = (starts[:, None] + ones[None]).ravel().astype(np.float64)
    times = (cells + phase + jitter * rng.standard_normal(len(cells))) * cell
    return np.sort(times), len(track_bits) * cell * np.arange(revolutions + 1)


def _syncs(stream, start):
    """Complete sync runs ``(starts, lengths)`` after the capture starts, and that start."""
    starts, lengths = runs_of_ones(stream)
    whole = starts + lengths < len(stream)
    starts, lengths = starts[whole], lengths[whole]
    if start != "sync":
        return starts, lengths, 0
    frame = int(starts[starts > 0][0] + lengths[starts > 0][0])
    keep = starts >= frame
    return starts[keep], lengths[keep], frame


def byte_capture(stream, nbytes, start="sync", sync_error=SYNC_ERROR_BITS, rng=None):
    """What byte ready latches from a bit stream, with syncs measured to ``±sync_error``.

    Each sync restarts byte framing; bytes complete until SYNC asserts on the
    tenth one. ``start="sync"`` begins after the first complete sync.
    """
    rng = np.random.default_rng(rng)
    stream = np.asarray(stream, np.uint8)
    starts, lengths, frame = _syncs(stream, start)
    frames = np.concatenate(([frame], starts + lengths))
    counts = np.append(starts + SYNC_MIN_BITS - 1, len(stream)) - frames
    counts = np.maximum(counts, 0) // 8
    idx = np.concatenate([f + np.arange(8 * c) for f, c in zip(frames, counts)])
    data = np.packbits(stream[idx.astype(np.int64)])
    positions = np.cumsum(counts)[:-1]
    keep = positions < min(nbytes, len(data))
    error = rng.integers(-sync_error, sync_error + 1, int(keep.sum()))
    return ByteCapture(
        data[:nbytes],
        positions[keep],
        np.maximum(lengths[keep] + error, SYNC_MIN_BITS),
        start,
        sync_error,
    )


SYNTH_ID = b"NY"
DATA_OFFSET = 2 * SYNC_BYTES + HEADER_GCR_BYTES + HEADER_GAP_BYTES
LONG_SYNC_BYTES = 100
WEAK_BITS = 2400
NO_FLUX_ONES = 1 / 8


def _sector_offset(track, sector, capacity):
    """Byte offset of a sector's header sync in :func:`format_track` output."""
    n = sectors_per_track(track)
    return sector * (SECTOR_BYTES + (capacity - n * SECTOR_BYTES) // n)


def _dos(track, rng, zone=None, errors=None, capacity=None):
    zone = speed_zone(track) if zone is None else zone
    data = rng.integers(0, 256, (sectors_per_track(track), 256), np.uint8)
    return format_track(track, data, SYNTH_ID, errors, capacity or track_capacity(zone))


def _long_sync(track, sector, rng):
    """A sync before a header lengthened by ``LONG_SYNC_BYTES`` taken from the gaps."""
    capacity = track_capacity(speed_zone(track)) - LONG_SYNC_BYTES
    out = _dos(track, rng, capacity=capacity)
    at = _sector_offset(track, sector, capacity)
    out = np.insert(out, at, np.full(LONG_SYNC_BYTES, 0xFF, np.uint8))
    return out, (8 * at, 8 * (at + LONG_SYNC_BYTES + SYNC_BYTES))


def _renumbered(track, sector, rng):
    """A header carrying the next track's number (checksum kept valid)."""
    out = _dos(track, rng)
    at = _sector_offset(track, sector, len(out)) + SYNC_BYTES
    hdr = header_blocks(track + 1, [sector], SYNTH_ID)
    out[at : at + HEADER_GCR_BYTES] = encode(hdr).ravel()
    return out, (8 * at, 8 * (at + HEADER_GCR_BYTES))


def _data_span(track, sector, offset, size):
    """Bit span ``offset`` bits into a sector's data block of a standard track."""
    start = _sector_offset(track, sector, track_capacity(speed_zone(track)))
    at = 8 * (start + DATA_OFFSET) + offset
    return at, at + size


def synthetic_disk(revolutions=4, rng=0):
    """Index-aligned reads of a DOS disk carrying one of each injected anomaly;
    the no-flux span reads as sparse random ones, different every revolution.

    Returns ``(DiskImage, truth)``; ``truth`` lists ``(key, kind name, start,
    end, revolution or None)`` with spans in bits from the index.
    """
    from ..formats.image import Capture, DiskImage

    rng = np.random.default_rng(rng)
    tracks = {t: _dos(t, rng) for t in range(1, 36)}
    truth = []
    tracks[3], span = _long_sync(3, 5, rng)
    truth.append((6, "SYNC_LONG", *span, None))
    errors = np.full(sectors_per_track(5), SectorError.OK, np.uint8)
    errors[2] = SectorError.DATA_CHECKSUM
    tracks[5] = _dos(5, rng, errors=errors)
    truth.append((10, "DATA_CHECKSUM", *_data_span(5, 2, 0, 8 * DATA_GCR_BYTES), None))
    tracks[7], span = _renumbered(7, 3, rng)
    truth.append((14, "HDR_TRACK", *span, None))
    weak = _data_span(10, 4, 100, WEAK_BITS)
    truth.append((20, "NOFLUX_SPAN", *weak, None))
    slip = _data_span(12, 6, 800, 1)
    slipped = revolutions // 2
    truth.append((24, "DATA_GCR", *_data_span(12, 6, 0, 8 * DATA_GCR_BYTES), slipped))
    tracks[20] = _dos(20, rng, zone=3)
    truth.append((40, "ZONE", 0, 8 * track_capacity(3), None))
    half = _dos(20, rng)
    truth.append((41, "HALF_TRACK", 0, 8 * len(half), None))
    tracks[31] = np.full(track_capacity(speed_zone(31)), 0xFF, np.uint8)
    truth.append((62, "KILLER", 0, 8 * len(tracks[31]), None))
    image = DiskImage("synthetic")
    for key, gcr in [(2 * t, g) for t, g in tracks.items()] + [(41, half)]:
        bits = to_bits(gcr)
        revs = np.tile(bits, (revolutions, 1))
        if key == 20:
            revs[:, weak[0] : weak[1]] = (
                rng.random((revolutions, WEAK_BITS)) < NO_FLUX_ONES
            )
        revs = list(revs)
        if key == 24:
            revs[slipped] = np.delete(revs[slipped], slip[0])
        zone = 3 if key == 40 else speed_zone(key // 2)
        index = np.cumsum([0] + [len(r) for r in revs])
        image.tracks[key] = [Capture(np.concatenate(revs), zone, index=index)]
    return image, truth
