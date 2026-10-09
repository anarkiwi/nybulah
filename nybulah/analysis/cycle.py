"""Track revolution (cycle) detection on raw multi-revolution captures.

Continuous bit streams: the physically possible lag whose bit agreement is most
significant (FFT autocorrelation). Byte-ready captures: the segment shift whose
contents repeat most significantly without contradicting a sector header.
"""

from dataclasses import dataclass, replace
from enum import IntEnum
from statistics import NormalDist

import numpy as np

from .capture import segments, uniform_sigma
from .gcr import NOMINAL_RPM, bit_rate, decode_bits, runs_of_ones
from .sector import HEADER_GCR_BYTES, valid_headers

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

    ``match`` is the fraction of bits (bytes, for segmented captures) repeating
    one period later and ``z`` its significance above chance. Segmented captures
    also give the period in ``segments`` and the length's standard error ``sigma``.
    """

    kind: TrackKind
    start: int
    length: int
    match: float = 0.0
    z: float = 0.0
    sigma: float = 0.0
    segments: int = 0


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


def _zscore(agree, total, chance):
    return (agree - total * chance) / np.sqrt(
        np.maximum(total, 1) * chance * (1 - chance)
    )


def _threshold(alpha, tests):
    return NormalDist().inv_cdf(1 - alpha / max(tests, 1))


def _best_lag(bits, lo, hi, chance):
    """Lag in ``[lo, hi]`` whose bit agreement is most significant above chance.

    Returns ``(lag, agreement fraction, z score)``.
    """
    lags = np.arange(lo, hi + 1)
    overlap = len(bits) - lags
    agree = (overlap + _autocorrelation(2.0 * bits - 1.0, lags)) / 2
    z = _zscore(agree, overlap, chance)
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
    sector0 = np.flatnonzero(valid_headers(hdr, valid) & (hdr[:, 2] == 0))
    ref = sector0[0] if len(sector0) else np.argmax(ends - starts)
    return int(starts[ref] % period)


def _segment_headers(seg):
    """Per segment: valid header flag and its (sector, track, id) as one integer."""
    width = 8 * HEADER_GCR_BYTES
    bits = np.concatenate((seg.bits, np.zeros(width, np.uint8)))
    hdr, valid = decode_bits(bits[seg.begin[:, None] + np.arange(width)])
    key = hdr[:, 2:6].astype(np.int64) @ (1 << np.arange(0, 32, 8))
    return valid_headers(hdr, valid), key, hdr[:, 2]


def _pairs(seg, lo, hi):
    """Segment pairs ``(i, j)`` whose distance can be one revolution, and that distance."""
    i, j = np.triu_indices(len(seg), 1)
    dist = seg.begin[j] - seg.begin[i]
    slack = seg.error * (j - i)
    keep = (dist + slack >= lo) & (dist - slack <= hi)
    return i[keep], j[keep], dist[keep]


def _segment_anchor(seg, sector0, period):
    """Run start of the measured sync before a sector 0 header, else of the longest."""
    measured = seg.run >= 0
    if not measured.any():
        return 0
    idx = np.arange(len(seg))
    near = sector0 | np.isin(idx, np.flatnonzero(sector0) + period)
    near |= np.isin(idx, np.flatnonzero(sector0) - period)
    found = np.flatnonzero(near & measured)
    if len(found):
        return int(seg.run[found[0]])
    return int(seg.run[measured][np.argmax((seg.begin - seg.run)[measured])])


@dataclass(frozen=True)
class _Pairs:
    """Segment pairs that may be one revolution apart and how their bytes agree."""

    i: np.ndarray
    j: np.ndarray
    dist: np.ndarray
    agree: np.ndarray
    total: np.ndarray
    chance: float

    @classmethod
    def compare(cls, seg, lo, hi):
        """Pairs in the window ``[lo, hi]``; ``chance`` is the byte coincidence rate."""
        mat = seg.matrix()
        present = mat >= 0
        freq = np.bincount(mat[present], minlength=256) / max(int(present.sum()), 1)
        i, j, dist = _pairs(seg, lo, hi)
        both = present[i] & present[j]
        agree = ((mat[i] == mat[j]) & both).sum(axis=1)
        return cls(i, j, dist, agree, both.sum(axis=1), float((freq**2).sum()))

    def z(self):
        """Per-pair significance of the agreement."""
        return _zscore(self.agree, self.total, self.chance)


def _best_shift(pairs, valid, key):
    """Pairs of the most significant shift no valid header pair contradicts.

    Returns ``(mask, match, z, tests)``.
    """
    shifts, inv = np.unique(pairs.j - pairs.i, return_inverse=True)
    differ = valid[pairs.i] & valid[pairs.j] & (key[pairs.i] != key[pairs.j])
    agree, total = np.bincount(inv, pairs.agree), np.bincount(inv, pairs.total)
    z = _zscore(agree, total, pairs.chance)
    z[(np.bincount(inv, differ) > 0) | (total == 0)] = -np.inf
    best = int(np.argmax(z))
    return (
        inv == best,
        float(agree[best] / max(total[best], 1)),
        float(z[best]),
        len(shifts),
    )


def _revolution(seg, pairs, sel, alpha):
    """Length, its standard error and the segment period from the chosen pairs.

    Pairs whose own agreement is significant are preferred; distances beyond
    the sync-error bound of their median belong to misaligned pairs.
    """
    strong = sel & (pairs.z() >= _threshold(alpha, 1))
    sel = strong if strong.any() else sel
    shift = pairs.j - pairs.i
    middle = np.sort(pairs.dist[sel])[(sel.sum() - 1) // 2]
    sel &= np.abs(pairs.dist - middle) <= 2 * seg.error * shift
    cover = np.zeros(len(seg) + 1)
    np.add.at(cover, pairs.i[sel] + 1, 1)
    np.add.at(cover, pairs.j[sel] + 1, -1)
    weight = np.cumsum(cover) / sel.sum()
    sigma = uniform_sigma(seg.error) * float(np.sqrt((weight**2).sum()))
    return float(pairs.dist[sel].mean()), sigma, int(shift[sel][0])


def _segment_cycle(seg, lo, hi, alpha, nominal, index_aligned):
    """Cycle of a segmented capture (see :func:`find_cycle`)."""
    unformatted = Cycle(TrackKind.UNFORMATTED, 0, nominal)
    pairs = _Pairs.compare(seg, lo, hi)
    if pairs.chance >= 1 or not pairs.i.size:
        return unformatted
    valid, key, sector = _segment_headers(seg)
    sel, match, z, tests = _best_shift(pairs, valid, key)
    if not z >= _threshold(alpha, tests):
        return replace(unformatted, match=match, z=max(z, 0.0))
    length, sigma, period = _revolution(seg, pairs, sel, alpha)
    start = 0 if index_aligned else _segment_anchor(seg, valid & (sector == 0), period)
    return Cycle(
        TrackKind.FORMATTED, start, int(round(length)), match, z, sigma, period
    )


def header_period(capture, zone=None, period=None, tolerance=None):
    """Revolution length from the nearest repeat of a sector header, independent of content.

    Returns ``(bits, bound)`` for the shortest distance in the physical window
    between two identical valid headers, ``bound`` its worst-case error; else None.
    """
    seg = segments(capture)
    zone = getattr(capture, "density", None) if zone is None else zone
    if len(seg) < 2:
        return None
    i, j, dist = _pairs(seg, *lag_window(zone, period, tolerance))
    valid, key, _ = _segment_headers(seg)
    same = valid[i] & valid[j] & (key[i] == key[j])
    if not same.any():
        return None
    k = np.flatnonzero(same)[np.argmin(dist[same])]
    return int(dist[k]), int(seg.error * (j[k] - i[k]))


def _is_capture(x):
    return hasattr(x, "positions")


def _bit_cycle(bits, lo, hi, alpha, nominal, index_aligned):
    """Cycle of a continuous bit stream (see :func:`find_cycle`)."""
    chance = bits.mean() ** 2 + (1 - bits.mean()) ** 2
    if chance >= 1:
        return Cycle(TrackKind.UNFORMATTED, 0, nominal)
    lag, match, z = _best_lag(bits, lo, hi, chance)
    if z < _threshold(alpha, hi - lo + 1) or match <= (1 + chance) / 2:
        return Cycle(TrackKind.UNFORMATTED, 0, nominal, match, z)
    if index_aligned:
        return Cycle(TrackKind.FORMATTED, 0, lag, match, z)
    starts, lengths = runs_of_ones(bits)
    anchor = _anchor(bits, starts, starts + lengths, lag)
    return Cycle(TrackKind.FORMATTED, anchor or 0, lag, match, z)


def find_cycle(
    bits,
    zone=None,
    period=None,
    index_aligned=False,
    tolerance=None,
    alpha=DEFAULT_ALPHA,
):
    """Find one revolution in a capture of more than one revolution.

    Majority-sync captures are KILLER. Otherwise the capture is UNFORMATTED
    unless the best period is significant at Bonferroni level ``alpha`` (for
    continuous streams, most overlapping bits must also repeat).

    Args:
        bits: 0/1 array of a continuous capture, or a byte-ready capture
            (see :func:`capture.segments`), which is analysed by segment.
        zone: density zone the capture was read at (default: the capture's).
        period: rotation period in seconds (default: the capture's index
            period, if it has one).
        index_aligned: bit 0 of the capture is the index pulse.
        tolerance: see :func:`lag_window`.
        alpha: family-wise false-detection probability.
    """
    seg = None
    if _is_capture(bits):
        seg = segments(bits)
        zone = getattr(bits, "density", None) if zone is None else zone
        rpm = getattr(bits, "rpm", None)
        period = 60.0 / rpm if period is None and rpm else period
        bits = seg.bits
    bits = np.asarray(bits, dtype=np.uint8)
    lo, hi = lag_window(zone, period, tolerance)
    nominal = _nominal(zone, period)
    if 2 * runs_of_ones(bits)[1].sum() > len(bits):
        return Cycle(TrackKind.KILLER, 0, nominal)
    if seg is not None and (len(seg) > 1 or lo >= len(bits)):
        return _segment_cycle(seg, lo, hi, alpha, nominal, index_aligned)
    if lo >= len(bits):
        raise ValueError(f"capture of {len(bits)} bits is shorter than {lo} bits")
    return _bit_cycle(bits, lo, min(hi, len(bits) - 1), alpha, nominal, index_aligned)


def _segment_revolution(seg, cycle):
    """Bits between a sync and the same sync one period later, rotated to ``cycle.start``.

    None when no measured pair of syncs spans the period.
    """
    k = np.flatnonzero(seg.run[: -cycle.segments] >= 0)
    width = seg.run[k + cycle.segments] - seg.run[k]
    k = k[np.abs(width - cycle.length) <= seg.error * cycle.segments]
    if not k.size:
        return None
    inside = (seg.run[k] <= cycle.start) & (cycle.start < seg.run[k + cycle.segments])
    k = int(k[np.argmax(inside)])
    rev = seg.bits[seg.run[k] : seg.run[k + cycle.segments]]
    return np.roll(rev, -((cycle.start - seg.run[k]) % len(rev)))


def extract_revolution(bits, cycle):
    """The ``cycle.length`` bits of one revolution starting at ``cycle.start``.

    Bits past the end of the capture are taken from the previous revolution.
    Segmented captures give the exact bits between two passes of one sync,
    which may differ from ``cycle.length`` within the sync-length uncertainty.
    """
    if _is_capture(bits):
        seg = segments(bits)
        if cycle.segments and cycle.segments < len(seg):
            rev = _segment_revolution(seg, cycle)
            if rev is not None:
                return rev
        bits = seg.bits
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
