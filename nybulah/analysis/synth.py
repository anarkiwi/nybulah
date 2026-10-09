"""Synthetic drive captures of known tracks, for testing and calibration."""

import numpy as np

from .gcr import CLOCK_HZ, NOMINAL_RPM


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
