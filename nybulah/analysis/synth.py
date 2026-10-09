"""Synthetic drive captures of known tracks, for testing and calibration."""

import numpy as np

from .capture import SYNC_ERROR_BITS, ByteCapture
from .gcr import CLOCK_HZ, NOMINAL_RPM, SYNC_MIN_BITS, runs_of_ones


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
