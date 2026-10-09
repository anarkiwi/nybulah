"""Synthetic drive captures of known tracks, for testing and calibration."""

import numpy as np


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
