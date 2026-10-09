"""Track revolution (cycle) detection on raw multi-revolution captures.

The period is the physically possible lag whose bit agreement is most
significant (FFT autocorrelation); the start is anchored on a sync mark.
"""

from dataclasses import dataclass, replace
from enum import IntEnum
from statistics import NormalDist

import numpy as np

from .gcr import NOMINAL_RPM, bit_rate, decode_bits, runs_of_ones
from .sector import HEADER_ID

NOMINAL_PERIOD = 60.0 / NOMINAL_RPM
UNMEASURED_TOLERANCE = 0.05
MEASURED_TOLERANCE = 0.02
DEFAULT_ALPHA = 1e-3


class TrackKind(IntEnum):
    """Capture classification."""

    FORMATTED = 0
    KILLER = 1
    UNFORMATTED = 2


@dataclass(frozen=True)
class Cycle:
    """One revolution within a capture: ``bits[start:start + length]``.

    ``match`` is the fraction of bits repeating one period later and ``z``
    its significance (standard scores above chance agreement).
    """

    kind: TrackKind
    start: int
    length: int
    match: float = 0.0
    z: float = 0.0


def lag_window(zone, period=None, tolerance=None):
    """Inclusive ``(lo, hi)`` bits-per-revolution range for a density zone.

    Args:
        zone: density zone the track is read at (0-3).
        period: measured rotation period in seconds (default 0.2 s).
        tolerance: allowed relative speed difference between the writing
            drive and ``period``; defaults depend on whether it was measured.
    """
    if tolerance is None:
        tolerance = UNMEASURED_TOLERANCE if period is None else MEASURED_TOLERANCE
    bits = bit_rate(zone) * (NOMINAL_PERIOD if period is None else period)
    return int(np.floor(bits * (1 - tolerance))), int(np.ceil(bits * (1 + tolerance)))


def _nominal(zone, period):
    return int(round(bit_rate(zone) * (period or NOMINAL_PERIOD)))


def _autocorrelation(x, lags):
    nfft = 1 << (2 * len(x) - 1).bit_length()
    spec = np.fft.rfft(x, nfft)
    return np.fft.irfft(spec * np.conj(spec), nfft)[lags]


def _best_lag(bits, lo, hi, chance):
    """Lag in ``[lo, hi]`` whose bit agreement is most significant above chance.

    Returns ``(lag, agreement fraction, z score)``.
    """
    lags = np.arange(lo, hi + 1)
    overlap = len(bits) - lags
    agree = (overlap + _autocorrelation(2.0 * bits - 1.0, lags)) / 2
    z = (agree - overlap * chance) / np.sqrt(overlap * chance * (1 - chance))
    best = int(np.argmax(z))
    return int(lags[best]), float(agree[best] / overlap[best]), float(z[best])


def _anchor(bits, starts, ends, period):
    """Revolution start: the sync before a sector 0 header, else the longest sync.

    Syncs truncated by the start of the capture are ignored; None if no sync.
    """
    keep = (starts > 0) & (ends + 80 <= len(bits))
    starts, ends = starts[keep], ends[keep]
    if len(starts) == 0:
        return None
    hdr, valid = decode_bits(bits[ends[:, None] + np.arange(80)])
    sector0 = np.flatnonzero(
        (hdr[:, 0] == HEADER_ID)
        & (hdr[:, 2] == 0)
        & valid[:, :6].all(axis=1)
        & (np.bitwise_xor.reduce(hdr[:, 1:6], axis=1) == 0)
    )
    ref = sector0[0] if len(sector0) else np.argmax(ends - starts)
    return int(starts[ref] % period)


def find_cycle(
    bits,
    zone,
    period=None,
    index_aligned=False,
    tolerance=None,
    alpha=DEFAULT_ALPHA,
):
    """Find one revolution in a capture of more than one revolution.

    Majority-sync captures are KILLER. Otherwise the capture is UNFORMATTED
    unless the best period is significant at Bonferroni level ``alpha`` and
    most overlapping bits repeat (agreement past the chance-certainty midpoint).

    Args:
        bits: 0/1 array of the raw capture.
        zone: density zone the capture was read at.
        period: measured rotation period in seconds (index-pulse timing).
        index_aligned: bit 0 of the capture is the index pulse.
        tolerance: see :func:`lag_window`.
        alpha: family-wise false-detection probability.
    """
    bits = np.asarray(bits, dtype=np.uint8)
    lo, hi = lag_window(zone, period, tolerance)
    starts, lengths = runs_of_ones(bits)
    if 2 * lengths.sum() > len(bits):
        return Cycle(TrackKind.KILLER, 0, _nominal(zone, period))
    hi = min(hi, len(bits) - 1)
    if lo > hi:
        raise ValueError(f"capture of {len(bits)} bits is shorter than {lo} bits")
    chance = bits.mean() ** 2 + (1 - bits.mean()) ** 2
    if chance >= 1:
        return Cycle(TrackKind.UNFORMATTED, 0, _nominal(zone, period))
    lag, match, z = _best_lag(bits, lo, hi, chance)
    if z < NormalDist().inv_cdf(1 - alpha / (hi - lo + 1)) or match <= (1 + chance) / 2:
        return Cycle(TrackKind.UNFORMATTED, 0, _nominal(zone, period), match, z)
    if index_aligned:
        return Cycle(TrackKind.FORMATTED, 0, lag, match, z)
    anchor = _anchor(bits, starts, starts + lengths, lag)
    return Cycle(TrackKind.FORMATTED, anchor or 0, lag, match, z)


def extract_revolution(bits, cycle):
    """The ``cycle.length`` bits of one revolution starting at ``cycle.start``.

    Bits past the end of the capture are taken from the previous revolution.
    """
    bits = np.asarray(bits, dtype=np.uint8)
    idx = cycle.start + np.arange(cycle.length)
    idx[idx >= len(bits)] -= cycle.length
    return bits[idx]


def index_align(captures):
    """One revolution per track, each starting at its index pulse.

    ``captures`` maps a key (e.g. halftrack) to ``(bits, cycle)`` where bit 0
    of every capture was taken at the index pulse.
    """
    return {
        key: extract_revolution(bits, replace(cycle, start=0))
        for key, (bits, cycle) in captures.items()
    }
