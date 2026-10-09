"""Host side of the capture passes in drive/track.s: sample windows, anchors and merge.

BITS holds the latched bytes, TB the T2 low byte at each byte ready, TS each
SYNC's byte count and release wait. The ones a sync hides are the extra cells
in the byte interval across it. Cycle constants mirror drive/track.s.
"""

import dataclasses
from statistics import NormalDist

import numpy as np

from .analysis.capture import trailing_ones
from .analysis.gcr import SYNC_MIN_BITS, to_bits

TB_CHAIN = 12
TB_BODY, TB_PAGE_BODY = 14, 27
TB_BVS, TB_BVC = 5, 4
TB_LOOP_START = 2 * TB_CHAIN + 2
TB_LOOP, TB_OUT_EXTRA = 11, 10
TB_NORMAL = ((0, TB_BVS), (4, TB_BVS), (8, TB_BVC))
TB_OUT = ((0, TB_BVS), (4, TB_BVS), (9, TB_BVS), (16, TB_BVS))
TB_SEEN_TO_WAIT = TB_BVS + TB_BODY

TS_FIRST_READ, TS_ITER, TS_WRAP_EXTRA = 11, 11, 34
TS_PB7_GAP = 46

MATCH_TO_TB = 41
VWAIT_GAP = 7
ANCHOR_MAX = 8

BITS_GAP, BITS_PAGE_BODY, BITS_READ = 7, 32, 8
BITS_MIN_PERIOD = (BITS_GAP + BITS_PAGE_BODY + BITS_READ) / 2

RPM_MAX, RPM_MIN = 310.0, 285.0
PRE_ONES = SYNC_MIN_BITS - 1
MIN_LATCHED = SYNC_MIN_BITS - 8
CELLS = 8
PIN = 4
P_SPAN = 256
BAND = 8
UNBOUNDED = -1
REPEAT_SPAN = 16
PERIOD_ROUNDS = 4


def tb_schedule(limit):
    """``(sample, ready)`` cycles after a TB wait starts; ready is the T2 read.

    Sorted, covering every sample whose ready time is at most ``limit``.
    """
    chain = 2 * np.arange(TB_CHAIN)
    m = np.arange(max(0, (int(limit) - TB_LOOP_START) // TB_LOOP + 2))
    base = TB_LOOP_START + TB_LOOP * m + TB_OUT_EXTRA * (m // 256)
    out = m % 256 == 255
    parts = [(chain, chain + TB_BVS)]
    for rows, sel in ((TB_NORMAL, ~out), (TB_OUT, out)):
        parts += [(base[sel] + off, base[sel] + off + d) for off, d in rows]
    sample = np.concatenate([p[0] for p in parts])
    ready = np.concatenate([p[1] for p in parts])
    order = np.argsort(sample, kind="stable")
    return sample[order], ready[order]


@dataclasses.dataclass
class Arrivals:
    """TB byte k latched in ``(lo[k], hi[k]]`` cycles after TB[0]'s T2 read.

    ``lo`` is -inf where the byte was waiting before its wait began; ``valid``
    is False where no sample of the drive loop explains the wait.
    """

    read: np.ndarray
    lo: np.ndarray
    hi: np.ndarray
    valid: np.ndarray


def tb_arrivals(tb, wraps=None):
    """Arrival windows of a TB pass; ``wraps`` adds 256-cycle timer wraps per byte."""
    tb = np.asarray(tb, np.int64)
    body = np.where(np.arange(len(tb)) % 256 == 255, TB_PAGE_BODY, TB_BODY)[:-1]
    e = (tb[:-1] - tb[1:] - body - TB_BVS) % 256 + TB_BVS
    if wraps is not None:
        e = e + 256 * np.asarray(wraps, np.int64)[1:]
    sample, ready = tb_schedule(e.max(initial=TB_BVS))
    idx = np.minimum(np.searchsorted(ready, e), len(ready) - 1)
    lo_rel = np.where(idx > 0, sample[np.maximum(idx - 1, 0)], -np.inf)
    read = np.concatenate(([0], np.cumsum(body + e))).astype(float)
    start = read[:-1] + body
    return Arrivals(
        read,
        np.concatenate(([-np.inf], start + lo_rel)),
        np.concatenate(([np.inf], start + sample[idx])),
        np.concatenate(([True], ready[idx] == e)),
    )


def ts_syncs(tc, th, td):
    """TS table to ``(count, release-wait iterations)`` per sync."""
    count = np.asarray(tc, np.int64) + 256 * np.asarray(th, np.int64)
    first = np.flatnonzero(np.diff(count, prepend=-1) != 0)
    size = np.diff(np.append(first, len(count)))
    iters = 256 * (size - 1) + (-np.asarray(td, np.int64)[first + size - 1] & 0xFF)
    return count[first], iters


def ts_pulse(iters):
    """``(lo, hi)`` cycles bounding each TS sync's SYNC low time."""
    i = np.asarray(iters, np.int64)

    def read(j):
        return np.where(
            j < 0, 0, TS_FIRST_READ + TS_ITER * j + TS_WRAP_EXTRA * (j // 256)
        )

    return read(i - 1).astype(float), read(i) + float(TS_PB7_GAP)


def capable(data):
    """Per byte p: could a sync end just before it, and the ones latched before p.

    A sync leaves 2-9 latched ones and the byte after it starts with the 0
    that ended it. Byte 0 starts the capture and is never capable.
    """
    data = np.asarray(data, np.uint8)
    p = np.arange(len(data))
    ones = trailing_ones(to_bits(data), 8 * p)
    ok = (ones >= MIN_LATCHED) & (ones <= PRE_ONES) & (data < 0x80) & (p > 0)
    return ok, ones


def byte_period(data, cells, alpha=1e-3):
    """Bytes per revolution of a byte stream, or None without significant repetition.

    ``cells`` is a revolution's cells at 300 rpm; lags run from half that to
    the slowest supported motor. Of lags agreeing significantly above chance
    (Bonferroni at ``alpha``), the fewest mismatches per overlap byte wins.
    """
    data = np.asarray(data, np.uint8)
    lo = int(cells / CELLS / 2)
    hi = min(int(np.ceil(cells / CELLS * 300.0 / RPM_MIN)), len(data) - 2)
    if lo > hi:
        return None
    lags = np.arange(lo, hi + 1)
    onehot = np.zeros((256, len(data)))
    onehot[data, np.arange(len(data))] = 1.0
    nfft = 1 << (2 * len(data) - 1).bit_length()
    spec = np.fft.rfft(onehot, nfft)
    agree = np.fft.irfft((spec * np.conj(spec)).sum(axis=0), nfft)[lags].round()
    freq = np.bincount(data, minlength=256) / len(data)
    chance = float((freq**2).sum())
    overlap = len(data) - lags
    if chance >= 1:
        return None
    z = (agree - overlap * chance) / np.sqrt(overlap * chance * (1 - chance))
    sig = z >= NormalDist().inv_cdf(1 - alpha / len(lags))
    if not sig.any():
        return None
    miss = np.where(sig, (overlap - agree) / overlap, np.inf)
    best = np.flatnonzero(miss == miss.min())
    return int(lags[best[np.argmax(overlap[best])]])


def unmeasured_after_anchor(cell):
    """Boundaries after an anchor that TB cannot time, at the fastest motor.

    The first TB byte has no timed predecessor and bytes stay waiting until
    the TB loop has caught up with the matcher-to-TB path.
    """
    period = CELLS * cell
    backlog = MATCH_TO_TB + VWAIT_GAP - period
    late = int(np.ceil(backlog / (period - TB_SEEN_TO_WAIT))) if backlog > 0 else 0
    return late + 1


def _matcher_idle(data, s, k):
    """The drive's no-restart matcher holds no partial match when byte s arrives."""
    q = data[s : s + k]
    return all(not np.array_equal(data[s - x : s], q[:x]) for x in range(1, k))


def _repeats(data, period, span=REPEAT_SPAN):
    """Bytes whose next ``span`` bytes recur a period later, framed as every revolution.

    Bytes read before the first sync after a step may be framed otherwise.
    """
    out = np.zeros(len(data) + 1, bool)
    if period is None or period >= len(data) - span:
        return out
    same = np.concatenate(([0], np.cumsum(data[period:] == data[:-period])))
    n = len(same) - 1 - span
    out[: n + 1] = same[span : span + n + 1] - same[: n + 1] == span
    return out


def _one_angle(keys, period):
    """Per window: no equal window lies other than whole periods away."""
    _, inv, counts = np.unique(keys, return_inverse=True, return_counts=True)
    if period is None:
        return counts[inv] == 1
    pairs = np.unique(np.stack((inv, np.arange(len(keys)) % period)), axis=1)
    return np.bincount(pairs[0], minlength=len(counts))[inv] == 1


def choose_anchor(data, cell, period=None, steady=True):
    """``(base, anchor bytes)`` with ``data[base - k:base]`` as anchor, or None.

    The window occurs at one angle, framed as on every revolution, and the
    matcher finds it. Without a ``period`` the bytes must be ``steady`` and
    no sync may fit before ``base`` or where TB is blind after it.
    """
    data = np.asarray(data, np.uint8)
    if period is None and not steady:
        return None
    ok, _ = capable(data)
    blind = unmeasured_after_anchor(cell)
    limit = len(data) - blind
    if period is None:
        limit = min(limit, int(np.argmax(ok)) if ok.any() else limit)
        hits = np.convolve(ok, np.ones(blind, int))[blind - 1 :]
        framed = np.append(hits[: len(data)] == 0, False)
        shift = 1
    else:
        framed, shift = _repeats(data, period), 0
    best, best_base = None, len(data) + 1
    for k in range(1, ANCHOR_MAX + 1):
        windows = np.lib.stride_tricks.sliding_window_view(data, k)
        keys = np.zeros(len(windows), np.uint64)
        for j in range(k):
            keys = keys << np.uint64(8) | windows[:, j].astype(np.uint64)
        starts = np.arange(k - 1, max(limit - k + 1, k - 1))
        starts = starts[_one_angle(keys, period)[starts] & framed[starts + k * shift]]
        for s in starts[starts + k < best_base]:
            if _matcher_idle(data, s, k):
                best, best_base = data[s : s + k].copy(), int(s + k)
                break
    return None if best is None else (best_base, best)


def best_offset(events, mask):
    """``(offset, hits)``: the shift putting most ``events`` on True ``mask`` entries."""
    events = np.asarray(events, np.int64)
    if events.size == 0:
        return 0, 0
    a = np.zeros(events.max() + 1)
    a[events] = 1.0
    b = np.asarray(mask, float)
    nfft = 1 << (len(a) + len(b)).bit_length()
    corr = np.fft.irfft(np.fft.rfft(b, nfft) * np.conj(np.fft.rfft(a, nfft)), nfft)
    offsets = np.concatenate((np.arange(len(b)), np.arange(1 - len(a), 0)))
    vals = np.concatenate((corr[: len(b)], corr[nfft - len(a) + 1 :])).round()
    i = int(np.argmax(vals))
    return int(offsets[i]), int(vals[i])


def align(consistent):
    """Offsets (columns ``-band..band``) per event, by dynamic programming.

    Minimises inconsistent events plus the total change of offset between
    consecutive events, from offset 0. Returns ``(offsets, matched)``.
    """
    consistent = np.asarray(consistent, bool)
    n, width = consistent.shape
    if not n:
        return np.zeros(0, np.int64), np.zeros(0, bool)
    o = np.arange(width)
    step = np.abs(o[:, None] - o[None, :])
    cost = np.abs(o - width // 2) + ~consistent[0]
    back = np.zeros((n, width), np.int64)
    for i in range(1, n):
        total = cost[None, :] + step
        back[i] = np.argmin(total, axis=1)
        cost = total[o, back[i]] + ~consistent[i]
    path = np.zeros(n, np.int64)
    path[-1] = int(np.argmin(cost))
    for i in range(n - 1, 0, -1):
        path[i - 1] = back[i, path[i]]
    return path - width // 2, consistent[np.arange(n), path]


def _events_offsets(events, base, mask, anchored):
    """Per-event BITS offsets for pass events landing on ``mask`` positions.

    Events past either end of the BITS bytes constrain nothing.
    """
    events = np.asarray(events, np.int64)
    shift = 0 if anchored else best_offset(events + base, mask)[0]
    pos = base + shift + events[:, None] + np.arange(-BAND, BAND + 1)[None, :]
    inside = (pos > 0) & (pos < len(mask))
    offs, matched = align(~inside | mask[np.clip(pos, 0, len(mask) - 1)])
    return offs + shift, matched


@dataclasses.dataclass
class Syncs:  # pylint: disable=too-many-instance-attributes
    """Syncs in a BITS pass: ``positions`` (bytes before each) and run lengths.

    A run (latched and hidden ones) lies in ``[lo, hi]`` (hi -1: unbounded);
    ``latched`` counts its ones in the bytes before it. Sync context is known
    for bytes ``first`` to ``valid``.
    """

    positions: np.ndarray
    runs: np.ndarray
    lo: np.ndarray
    hi: np.ndarray
    latched: np.ndarray
    valid: int
    byte_cycles: float | None = None
    unmatched: int = 0
    first: int = 0

    @property
    def hidden(self):
        """Ones of each run that byte ready never latched."""
        return np.maximum(self.runs - self.latched, 0)


def _syncs(rows, latched, span, byte_cycles, unmatched):
    """Syncs from ``(position, run, lo, hi)`` rows (run 0: no sync).

    Of several measurements of one boundary the narrowest decides.
    """
    best = {}
    for row in rows:
        width = row[3] - row[2] if row[3] >= 0 else 1 << 30
        if row[0] not in best or width < best[row[0]][0]:
            best[row[0]] = (width, row)
    rows = [best[q][1] for q in sorted(best) if best[q][1][1]]
    rows = np.array(rows, np.int64).reshape(-1, 4)
    p, run, lo, hi = rows.T
    return Syncs(p, run, lo, hi, latched[p], span[1], byte_cycles, unmatched, span[0])


def run_range(excess, cells, latched):
    """``(run, lo, hi)`` bits at a capable boundary (run 0: no sync; hi -1: unbounded).

    ``excess`` bounds the extra cycles before the byte, ``cells`` the cycles
    per cell. Hidden ones are 0 or reach 10 ones with the latched ones.
    """
    floor = max(1, SYNC_MIN_BITS - int(latched))
    (lo_e, hi_e), (c_lo, c_hi) = excess, cells
    h_lo = np.ceil(lo_e / (c_hi if lo_e > 0 else c_lo)) if np.isfinite(lo_e) else 0
    h_hi = (
        np.floor(hi_e / (c_lo if hi_e > 0 else c_hi)) if np.isfinite(hi_e) else np.inf
    )
    zero = h_lo <= 0 <= h_hi
    first = max(h_lo, floor)
    if h_hi < first:
        if zero:
            return 0, int(latched), int(latched)
        h_hi = first
    known = np.isfinite(lo_e) and np.isfinite(hi_e)
    mid = (lo_e + hi_e) / (c_lo + c_hi) if known else (0 if zero else first)
    best = int(min(max(round(mid), first), h_hi))
    if zero and abs(mid) <= abs(best - mid):
        best = 0
    hi = UNBOUNDED if np.isinf(h_hi) else int(latched + h_hi)
    return (
        (int(latched) + best if best else 0),
        int(latched + (0 if zero else first)),
        hi,
    )


def ts_wraps(arr, ts, period, anchored):
    """Extra TB timer wraps at the bytes after TS syncs, and unmatched TS syncs."""
    count, iters = ts
    n = len(arr.read)
    fine = np.diff(arr.read, prepend=0.0) - period
    if not anchored:
        count = count + best_offset(count, fine > period / CELLS)[0]
    plo, phi = ts_pulse(iters)
    slack = TB_LOOP + TB_OUT[-1][0]
    xlo, xhi = plo - slack, phi + PRE_ONES * period / CELLS + slack
    k = count[:, None] + np.arange(-BAND, BAND + 1)[None, :]
    kk = np.clip(k, 1, n - 1)
    need = np.maximum(np.ceil((xlo[:, None] - fine[kk]) / 256), 0)
    inside = (k >= 1) & (k < n)
    offs, matched = align(~inside | (fine[kk] + 256 * need <= xhi[:, None]))
    wraps = np.zeros(n, np.int64)
    hit = count + offs
    sel = matched & (hit >= 1) & (hit < n)
    wraps[hit[sel]] = need[np.arange(len(hit)), offs + BAND][sel].astype(np.int64)
    return wraps, int((~matched).sum())


def _runs(arr, breaks, hidden):
    """Runs of timed bytes with no possible sync inside (``breaks[j]``: before byte j).

    Returns each run's cells (8 per byte plus ``hidden`` ones of known syncs),
    window-middle step, mean end window width and centre index.
    """
    timed = np.isfinite(arr.lo) & np.isfinite(arr.hi) & arr.valid
    lo, hi = np.where(timed, arr.lo, 0.0), np.where(timed, arr.hi, 0.0)
    use = timed & np.roll(timed, 1) & ~breaks
    use[0] = False
    edges = np.flatnonzero(np.diff(np.concatenate(([0], use.astype(np.int8), [0]))))
    start, stop = edges[::2] - 1, edges[1::2] - 1
    cum = np.concatenate(([0.0], np.cumsum(np.where(use, hidden, 0.0))))
    cells = CELLS * (stop - start) + cum[stop + 1] - cum[start + 1]
    step = (lo[stop] + hi[stop] - lo[start] - hi[start]) / 2
    ends = (hi[start] - lo[start] + hi[stop] - lo[stop]) / 2
    return cells, step, ends, (start + stop) / 2


def _local_period(arr, breaks, hidden, span=P_SPAN):
    """``(period, error)`` per byte from the runs centred on it within +-span bytes.

    A run's cell estimate is its step over its c cells, off by at most w / c
    (w its end windows); runs combine weighted by c, bounding the error by
    sum(c w) / sum(c^2). Centred runs cancel a steady drift of motor speed.
    """
    n = len(arr.read)
    cells, step, ends, centre = _runs(arr, breaks, hidden)
    if centre.size == 0:
        return np.full(n, np.nan), np.full(n, np.inf)
    sums = [
        np.concatenate(([0.0], np.cumsum(v)))
        for v in (cells * step, cells**2, cells * ends)
    ]
    k = np.arange(n)
    d = np.clip(np.minimum(k - centre[0], centre[-1] - k), 1, span)
    near = np.clip(np.searchsorted(centre, k), 0, len(centre) - 1)
    first = np.minimum(np.searchsorted(centre, k - d), near)
    last = np.maximum(np.searchsorted(centre, k + d, "right"), near + 1)
    sums = [c[last] - c[first] for c in sums]
    return CELLS * sums[0] / sums[1], CELLS * sums[2] / sums[1]


def pinned(arr, period, error, breaks, step):
    """Arrival windows; a waiting byte's comes from its nearest timed neighbour.

    ``step`` -1 looks before, 1 after; the neighbour counts only with no
    possible sync (``breaks[j]``: before byte j) between them.
    """
    n = len(arr.read)
    lo, hi = arr.lo.copy(), arr.hi.copy()
    todo = ~np.isfinite(lo) & np.isfinite(hi)
    alive = np.ones(n, bool)
    idx = np.arange(n)
    for i in range(1, PIN + 1):
        j = idx + step * i
        jj = np.clip(j, 0, n - 1)
        edge = np.maximum(jj, np.clip(jj - step, 0, n - 1))
        alive &= (j >= 0) & (j < n) & ~breaks[edge] & arr.valid[jj]
        hit = todo & alive & np.isfinite(arr.lo[jj])
        slack = i * error
        lo = np.where(hit, arr.lo[jj] - step * i * period - slack, lo)
        hi = np.where(hit, np.minimum(hi, arr.hi[jj] - step * i * period + slack), hi)
        todo &= ~hit
    return lo, hi


def tb_excess(arr, breaks, period, error):
    """``(lo, hi)`` extra cycles in the byte interval before each TB byte."""
    pre_lo, pre_hi = pinned(arr, period, error, breaks, -1)
    post_lo, post_hi = pinned(arr, period, error, breaks, 1)
    lo = post_lo[1:] - pre_hi[:-1] - period[1:] - error[1:]
    hi = post_hi[1:] - pre_lo[:-1] - period[1:] + error[1:]
    return np.concatenate(([-np.inf], lo)), np.concatenate(([np.inf], hi))


def _tb_positions(arr, base, ok, anchored):
    """BITS index of each TB byte, following slips at the definite syncs."""
    n = len(arr.read)
    period = float(np.median(np.diff(arr.read))) if n > 1 else 0.0
    definite = np.flatnonzero(arr.lo[1:] - arr.hi[:-1] - period > period / CELLS) + 1
    offs, matched = _events_offsets(definite, base, ok, anchored)
    which = np.clip(np.searchsorted(definite, np.arange(n), "right") - 1, 0, None)
    pos = base + np.arange(n) + (offs[which] if len(definite) else 0)
    return pos, int((~matched).sum())


def _classify(arr, capable_k, latched_k, ts_end):
    """Run ranges at every capable TB boundary, and the local byte period.

    Boundaries that surely hold no sync, or a sync of one possible length,
    join the period estimate on the next round, refining it.
    """
    n = len(arr.read)
    breaks = capable_k.copy()
    hidden = np.zeros(n)
    runs = {}
    for _ in range(PERIOD_ROUNDS):
        period, error = _local_period(arr, breaks, hidden)
        ex_lo, ex_hi = tb_excess(arr, breaks, period, error)
        for k in np.flatnonzero(capable_k[1:]) + 1:
            cells = ((period[k] - error[k]) / CELLS, (period[k] + error[k]) / CELLS)
            run = run_range((ex_lo[k], ex_hi[k]), cells, latched_k[k])
            if run[0] and ts_end is not None and k > ts_end:
                run = (run[0], run[1], UNBOUNDED)
            runs[k] = run
        sure = [k for k, r in runs.items() if 0 <= r[2] == r[1]]
        breaks = capable_k.copy()
        breaks[sure] = False
        hidden[sure] = [max(runs[k][1] - latched_k[k], 0) for k in sure]
    return runs, float(np.median(period))


def merge_tb(  # pylint: disable=too-many-arguments
    data, base, tb, ts=None, anchored=True, revolution=None, ts_end=None
):
    """Syncs of a BITS pass from TB (and TS for long syncs), TB[0] at ``data[base]``.

    ``ts`` is ``(count, iterations)`` from :func:`ts_syncs`, covering TB
    bytes before ``ts_end``; past it a sync's length is unbounded above. With
    the ``revolution`` in bytes, bytes before TB starts are timed a turn on.
    """
    data = np.asarray(data, np.uint8)
    tb = np.asarray(tb, np.int64)
    ok, latched = capable(data)
    arr = tb_arrivals(tb)
    unmatched = 0
    if ts is not None and len(ts[0]):
        wraps, unmatched = ts_wraps(
            arr, ts, float(np.median(np.diff(arr.read))), anchored
        )
        arr = tb_arrivals(tb, wraps)
    pos, missed = _tb_positions(arr, base, ok, anchored)
    steady = np.where(pos < len(data), pos, pos - (revolution or len(data)))
    inside = (steady > 0) & (steady < len(data))
    at = np.clip(steady, 0, len(data) - 1)
    runs, byte_cycles = _classify(arr, inside & ok[at], latched[at], ts_end)
    rows = [
        (int(q), *run)
        for k, run in runs.items()
        for q in (pos[k], pos[k] - revolution if revolution else -1)
        if 0 < q < len(data)
    ]
    first = 0 if anchored else int(np.clip(pos[min(1, len(tb) - 1)], 0, len(data)))
    valid = len(data) if anchored else int(np.clip(pos[-1] + 1, first, len(data)))
    return _syncs(rows, latched, (first, valid), byte_cycles, unmatched + missed)


def merge_ts(data, base, ts, cell, anchored=True):
    """Syncs of a BITS pass from TS alone: exact positions, lengths from SYNC low time."""
    data = np.asarray(data, np.uint8)
    count, iters = ts
    ok, latched = capable(data)
    offs, matched = _events_offsets(count, base, ok, anchored)
    plo, phi = ts_pulse(iters)
    rows = []
    for j in np.flatnonzero(matched):
        p = base + int(count[j] + offs[j])
        if not 0 < p < len(data):
            continue
        lo = max(PRE_ONES + int(np.ceil(plo[j] / cell)), SYNC_MIN_BITS)
        hi = PRE_ONES + int(np.floor(phi[j] / cell))
        mid = PRE_ONES + int(round((plo[j] + phi[j]) / 2 / cell))
        rows.append((p, min(max(mid, lo), hi), lo, hi))
    return _syncs(rows, latched, (0, len(data)), None, int((~matched).sum()))
