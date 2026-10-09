"""Byte-ready captures to bit streams: put back the sync cells the hardware does not latch."""

import numpy as np

from .gcr import SYNC_MIN_BITS, to_bits


def trailing_ones(bits, ends):
    """Count of consecutive one bits ending just before each index in ends."""
    zeros = np.concatenate(([-1], np.flatnonzero(np.asarray(bits) == 0)))
    ends = np.asarray(ends, np.int64)
    return ends - 1 - zeros[np.searchsorted(zeros, ends) - 1]


def capture_bits(data, positions, runs, lead=0):
    """Bit stream of a capture with each sync run restored to its measured length.

    ``positions`` count the bytes captured before each sync, ``runs`` give sync
    lengths in bits (at least a hardware sync), including ones already latched.
    ``lead`` ones go first, for a capture started just after an unmeasured sync.
    """
    bits = to_bits(np.asarray(data, np.uint8))
    ends = 8 * np.asarray(positions, np.int64)
    runs = np.maximum(np.asarray(runs, np.int64), SYNC_MIN_BITS)
    extra = np.maximum(runs - trailing_ones(bits, ends), 0)
    bits = np.insert(bits, np.repeat(ends, extra), 1)
    return np.concatenate((np.ones(lead, np.uint8), bits))
