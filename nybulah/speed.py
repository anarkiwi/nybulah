"""Motor speed from a TB pass: rolling byte period and its excursions.

Byte intervals come from the TB waits the merge placed on single latched bytes
(:attr:`nybulah.passes.Syncs.intervals`); each window's mean is the trace.
Excursions leave a bound set by the trace's own MAD, or the TB poll loop's
quantisation of a window's two end reads if larger, at a Bonferroni level over
its independent windows.
"""

import numpy as np

from .analysis.cycle import DEFAULT_ALPHA, _threshold
from .passes import CPU_HZ, TB_LOOP

MAD_SIGMA = 1.4826
WINDOW = 64


def trace(cycles, window=WINDOW):
    """Mean of each ``window`` of byte intervals, NaN ignored: consecutive TB reads
    telescope, so only each unbroken stretch's end reads add quantisation."""
    if len(cycles) < window:
        return np.zeros(0)
    ok = np.isfinite(cycles)
    cum = [np.concatenate(([0.0], np.cumsum(v))) for v in (np.where(ok, cycles, 0), ok)]
    total, n = (c[window:] - c[:-window] for c in cum)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(n > 0, total / n, np.nan)


def _lobes(dev, bound):
    """``(start, stop, peak index)`` of each stretch between sign changes of ``dev``
    whose peak passes ``bound``."""
    sign = np.sign(dev).astype(np.int8)
    edges = np.flatnonzero(np.diff(sign)) + 1
    lobes = []
    for a, b in zip(np.append(0, edges), np.append(edges, len(dev))):
        peak = int(a + np.argmax(np.abs(dev[a:b])))
        if abs(dev[peak]) > bound:
            lobes.append((int(a), int(b), peak))
    return lobes


def _groups(lobes):
    """Lobes chained while each gap is shorter than the lobe before it, merging
    same-sign neighbours."""
    groups = []
    for lobe in lobes:
        if groups:
            a, b, _ = groups[-1][-1]
            if lobe[0] - b < b - a:
                groups[-1].append(lobe)
                continue
        groups.append([lobe])
    return groups


def _merge_signs(group, dev):
    """Same-sign consecutive lobes joined, keeping the larger peak."""
    out = [group[0]]
    for a, b, p in group[1:]:
        q = out[-1]
        if np.sign(dev[p]) == np.sign(dev[q[2]]):
            peak = p if abs(dev[p]) > abs(dev[q[2]]) else q[2]
            out[-1] = (q[0], b, peak)
        else:
            out.append((a, b, p))
    return out


def excursions(period, byte_us, window=WINDOW, alpha=DEFAULT_ALPHA):
    """Departures of a byte-period trace from its median.

    Per excursion: trace start index, peak percent, oscillation period and decay
    time constant in ms (None from fewer than two lobes), lobe count.
    """
    ok = np.isfinite(period)
    if not ok.any():
        return [], None
    base = float(np.median(period[ok]))
    dev = np.where(ok, period / base - 1, 0.0)
    mad = MAD_SIGMA * float(np.median(np.abs(dev[ok] - np.median(dev[ok]))))
    sigma = max(mad, TB_LOOP / np.sqrt(6) / window / base)
    bound = sigma * _threshold(alpha, max(int(ok.sum()) // window, 1))
    found = []
    for group in _groups(_lobes(dev, bound)):
        lobes = _merge_signs(group, dev)
        peaks = np.array([p for _, _, p in lobes])
        top = peaks[np.argmax(np.abs(dev[peaks]))]
        osc = decay = None
        if len(peaks) > 1:
            osc = 2 * float(np.median([b - a for a, b, _ in lobes])) * byte_us / 1000
            t = peaks * byte_us / 1000
            slope = np.polyfit(t, np.log(np.abs(dev[peaks])), 1)[0]
            decay = float(-1 / slope) if slope < 0 else None
        found.append(
            {
                "start": int(group[0][0]),
                "end": int(group[-1][1]),
                "peak_pct": 100 * float(dev[top]),
                "period_ms": osc,
                "decay_ms": decay,
                "lobes": len(lobes),
            }
        )
    return found, {"median_cycles": base, "sigma_pct": 100 * sigma}


def speed_trace(cap, window=WINDOW, alpha=DEFAULT_ALPHA):
    """A RAM capture's speed trace and excursions, or None without a TB pass.

    ``bytes``/``cycles`` sample the trace every ``window`` bytes; excursion
    starts and ends are BITS byte indices.
    """
    if cap.tb is None or len(cap.tb) < window + 1 or cap.syncs.intervals is None:
        return None
    after, cycles = cap.syncs.intervals
    period = trace(cycles, window)
    if period.size == 0:
        return None
    centre = after[window // 2 : window // 2 + len(period)]
    byte_us = float(np.nanmedian(period)) * 1e6 / CPU_HZ
    found, stats = excursions(period, byte_us, window, alpha)
    for x in found:
        x["start"], x["end"] = int(centre[x["start"]]), int(centre[x["end"] - 1])
    step = slice(None, None, window)
    return {
        "window": window,
        "bytes": centre[step].tolist(),
        "cycles": [None if np.isnan(v) else round(float(v), 3) for v in period[step]],
        "excursions": found,
    } | (stats or {})
