"""Host side of the capture passes in drive/track.s: sample windows, anchors and merge.

BITS holds the latched bytes, TB the T2 low byte at each byte ready, TS each
SYNC's byte count and release wait. The ones a sync hides are the extra cells
in the byte interval across it. Cycle constants mirror drive/track.s.
"""

import dataclasses
from statistics import NormalDist

import numba
import numpy as np

from .analysis.capture import trailing_ones
from .eventalign import align
from .analysis.gcr import NOMINAL_RPM, SYNC_MIN_BITS, bit_rate, to_bits

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
TS_RESUME = 35
DRIFT = 0.02

MATCH_TO_TB = 41
VWAIT_GAP = 7
ANCHOR_MAX = 8

BITS_GAP, BITS_PAGE_BODY, BITS_READ = 7, 32, 8
BITS_MIN_PERIOD = (BITS_GAP + BITS_PAGE_BODY + BITS_READ) / 2

RPM_MAX, RPM_MIN = 310.0, 285.0
PRE_ONES = SYNC_MIN_BITS - 1
MIN_LATCHED = SYNC_MIN_BITS - 8
CELLS = 8
CPU_HZ = 1_000_000
FASTEST_BYTE = CELLS * CPU_HZ / bit_rate(3) * NOMINAL_RPM / RPM_MAX
TS_LATE_MERGE = int(np.ceil((TS_ITER + TS_WRAP_EXTRA + TS_RESUME) / FASTEST_BYTE)) - 1
PIN = 4
P_SPAN = 256
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


def tb_intervals(tb):
    """T2 cycles of each TB wait (mod 256), and the loop body before it."""
    tb = np.asarray(tb, np.int64)
    body = np.where(np.arange(len(tb)) % 256 == 255, TB_PAGE_BODY, TB_BODY)[:-1]
    return (tb[:-1] - tb[1:] - body - TB_BVS) % 256 + TB_BVS, body


def explained(e):
    """Whether a TB wait of e cycles ends on a T2 read of the drive loop."""
    e = np.asarray(e, np.int64)
    _, ready = tb_schedule(e.max(initial=TB_BVS))
    return np.isin(e, ready)


def tb_arrivals(tb, wraps=None):
    """Arrival windows of a TB pass; ``wraps`` adds 256-cycle timer wraps per byte."""
    e, body = tb_intervals(tb)
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


@numba.njit(cache=True)
def agreements(data, lo, hi):
    """Per lag in lo..hi: how many bytes equal the byte that many later."""
    out = np.zeros(hi - lo + 1, np.int64)
    for lag in range(lo, hi + 1):
        n = 0
        for i in range(len(data) - lag):
            n += data[i] == data[i + lag]
        out[lag - lo] = n
    return out


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
    agree = agreements(data, lo, hi)
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


def sync_weights(ok, ones):
    """Evidence of a sync before each byte: -log of the share of BITS boundaries
    latching that many ones (0 where no sync fits).

    A sync's latched ones depend on its bit phase, not on the data, so of two
    alignments landing as many syncs on capable bytes the likelier puts them
    where the data itself rarely latches such ones.
    """
    ok, ones = np.asarray(ok, bool), np.asarray(ones, np.int64)
    freq = np.bincount(ones[ok], minlength=PRE_ONES + 1) / len(ok)
    out = np.zeros(len(ok))
    out[ok] = -np.log(freq[ones[ok]])
    return out


def _fold(a, rev, op):
    """``a`` over one revolution of ``rev`` bytes, every repeat merged by ``op``."""
    out = a[:rev].copy()
    for k in range(rev, len(a), rev):
        seg = a[k : k + rev]
        out[: len(seg)] = op(out[: len(seg)], seg)
    return out


def nearest_turn(u, n, rev):
    """BITS indices for positions ``u`` of a revolution of ``rev`` bytes over
    ``n`` captured bytes: ``u`` inside them, else the nearest copy whole turns
    away inside them."""
    u = np.asarray(u, np.int64)
    return np.where(
        u < 0,
        u + rev * ((rev - 1 - u) // rev),
        np.where(u >= n, u - rev * ((u - n) // rev + 1), u),
    )


def _likeliest_shift(events, ok, weight):
    """Circular shift putting most ``events`` on ``ok``, then the most ``weight``."""
    size = len(ok)
    hist = np.conj(np.fft.rfft(np.bincount(events % size, minlength=size)))
    hits, score = (np.fft.irfft(np.fft.rfft(a) * hist, size) for a in (ok, weight))
    best = np.flatnonzero(hits.round() == hits.round().max())
    return int(best[np.argmax(score[best])])


def _events_offsets(  # pylint: disable=too-many-arguments,too-many-locals
    events, base, evidence, anchored, rev=None, rise=None, known=None, free=None
):
    """Per-event BITS offsets for pass events landing on capable positions.

    ``evidence`` is ``(capable, weight)`` of the BITS boundaries; ``known``
    is optionally ``(positions, fits)``, where another pass placed syncs and
    which of them each event's sync fits: among equally consistent
    alignments, events landing on syncs they fit outweigh any ``weight``.
    The passes read different revolutions, so an unstable stretch can latch
    different byte counts in each: the offset changes there by any amount
    that keeps positions increasing, rising by at most ``rise`` (the bytes
    the pass's own sync at the event could have held), or by up to ``free``
    bytes at no cost. Over a known revolution of ``rev`` bytes a position
    past either end of the BITS bytes stands for its nearest copy a whole
    number of turns away inside them (:func:`nearest_turn`); one inside them
    is itself, as the bytes need not recur where the turns latched different
    counts. Otherwise events past either end of the pass's own placement
    constrain nothing. A pass starts at its anchor, or unanchored at the
    shift putting most events on ``ok``, and its first event may move off
    that start by any amount.
    """
    events = base + np.asarray(events, np.int64)
    if events.size == 0:
        return np.zeros(0, np.int64), np.zeros(0, bool)
    ok, weight = evidence
    if rev:
        ok, weight = _fold(ok, rev, np.logical_or), _fold(weight, rev, np.maximum)
        shift = 0
        if not anchored:
            shift = (_likeliest_shift(events, ok, weight) + base) % rev - base
        offsets = shift + np.arange(-(rev // 2), rev - rev // 2)
        ok, weight = evidence
        pos = nearest_turn(events[:, None] + offsets, len(ok), rev)
        inside = np.ones(pos.shape, bool)
    else:
        shift = 0 if anchored else best_offset(events, ok)[0]
        lo = min(shift, -int(events.max()))
        offsets = np.arange(lo, max(shift, len(ok) - int(events.min())) + 1)
        pos = events[:, None] + offsets
        inside = (pos > 0) & (pos < len(ok))
        pos = np.clip(pos, 0, len(ok) - 1)
    hit = inside & ok[pos]
    weight = np.where(hit, weight[pos], 0.0)
    if known is not None:
        where, fits = known
        seen = np.zeros(hit.shape, bool)
        if rev:
            for j, w in enumerate(where):
                seen |= fits[:, j : j + 1] & (pos == w)
        else:
            col = where[None, :] - events[:, None] - offsets[0]
            i, j = np.nonzero(fits & (col >= 0) & (col < len(offsets)))
            seen[i, col[i, j]] = True
        weight += (seen & hit) * (1.0 + weight.max(axis=1, initial=0.0).sum())
    fall = np.diff(events, prepend=events[0]) - 1
    rise = np.full(len(events), len(offsets)) if rise is None else np.array(rise)
    fall[0] = rise[0] = len(offsets)
    start = int(np.searchsorted(offsets, shift))
    placed = np.arange(len(offsets)) == start
    cols, matched = align(
        hit | (~inside & placed),
        free=free,
        land=inside,
        weight=weight,
        start=start,
        rise=rise,
        fall=fall,
    )
    return shift + cols, matched


@dataclasses.dataclass
class Syncs:  # pylint: disable=too-many-instance-attributes
    """Syncs in a BITS pass: ``positions`` (bytes before each) and run lengths.

    A run (latched and hidden ones) lies in ``[lo, hi]`` (hi -1: unbounded);
    ``latched`` counts its ones in the bytes before it. Sync context is known
    for bytes ``first`` to ``valid``. ``intervals`` (from a TB pass) are the
    BITS index after each TB byte interval and its cycles, NaN unless the
    interval holds exactly one latched byte; ``timing`` the BITS index of each
    TB byte (-1 where a slip skipped it) and their arrivals with the timer
    wraps TS resolved.
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
    intervals: tuple | None = None
    timing: tuple | None = None

    @property
    def hidden(self):
        """Ones of each run that byte ready never latched."""
        return np.maximum(self.runs - self.latched, 0)

    @property
    def whole(self):
        """Per sync, whether the bytes hold all of it: its run starts after a
        latched zero, it ends inside the known context, its length is bounded."""
        pos = np.asarray(self.positions, np.int64)
        inside = (pos >= self.first) & (pos < self.valid)
        return inside & (self.latched < CELLS * pos) & (self.hi >= 0)


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
    per cell. Hidden ones are 0 or reach 10 ones with the latched ones; an
    excess fitting neither takes the nearer, and one below zero is no sync.
    """
    floor = max(1, SYNC_MIN_BITS - int(latched))
    (lo_e, hi_e), (c_lo, c_hi) = excess, cells
    h_lo = np.ceil(lo_e / (c_hi if lo_e > 0 else c_lo)) if np.isfinite(lo_e) else 0
    h_hi = (
        np.floor(hi_e / (c_lo if hi_e > 0 else c_hi)) if np.isfinite(hi_e) else np.inf
    )
    zero = h_lo <= 0 <= h_hi
    first = max(h_lo, floor)
    known = np.isfinite(lo_e) and np.isfinite(hi_e)
    mid = (lo_e + hi_e) / (c_lo + c_hi) if known else (0 if zero else first)
    if h_hi < first:
        if zero or not known or abs(mid) <= abs(first - mid):
            return 0, int(latched), int(latched)
        h_hi = first
    best = int(min(max(round(mid), first), h_hi))
    if zero and abs(mid) <= abs(best - mid):
        best = 0
    hi = UNBOUNDED if np.isinf(h_hi) else int(latched + h_hi)
    return (
        (int(latched) + best if best else 0),
        int(latched + (0 if zero else first)),
        hi,
    )


def wrap_fits(waits, fine, xlo, xhi):
    """``(w, fits)``: candidate wrap counts per TB wait and which fit.

    ``waits`` and ``fine`` are a TB byte's wait and extra cycles; a count fits
    when the extra cycles land in ``[xlo, xhi]`` on a T2 read of the loop.
    """
    lo = np.maximum(np.ceil((xlo - fine) / 256), 0).astype(np.int64)
    hi = np.floor((xhi - fine) / 256).astype(np.int64)
    w = lo[..., None] + np.arange(max(int((hi - lo).max(initial=0)) + 1, 1))
    return w, (w <= hi[..., None]) & explained(waits[..., None] + 256 * w)


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


def pinned(arr, period, error, breaks, step, hidden):
    """Arrival windows; a waiting byte's comes from its nearest timed neighbour.

    ``step`` -1 looks before, 1 after; the neighbour counts only with no
    possible sync (``breaks[j]``: before byte j) between them, and the
    ``hidden`` cells of the known syncs crossed shift it on.
    """
    n = len(arr.read)
    lo, hi = arr.lo.copy(), arr.hi.copy()
    todo = ~np.isfinite(lo) & np.isfinite(hi)
    alive = np.ones(n, bool)
    cells = np.zeros(n)
    idx = np.arange(n)
    for i in range(1, PIN + 1):
        j = idx + step * i
        jj = np.clip(j, 0, n - 1)
        edge = np.maximum(jj, np.clip(jj - step, 0, n - 1))
        alive &= (j >= 0) & (j < n) & ~breaks[edge] & arr.valid[jj]
        cells += hidden[edge]
        hit = todo & alive & np.isfinite(arr.lo[jj])
        shift = step * (i * period + cells * period / CELLS)
        slack = (i + cells / CELLS) * error
        lo = np.where(hit, arr.lo[jj] - shift - slack, lo)
        hi = np.where(hit, np.minimum(hi, arr.hi[jj] - shift + slack), hi)
        todo &= ~hit
    return lo, hi


def tb_excess(arr, breaks, period, error, hidden):
    """``(lo, hi)`` extra cycles in the byte interval before each TB byte."""
    pre_lo, pre_hi = pinned(arr, period, error, breaks, -1, hidden)
    post_lo, post_hi = pinned(arr, period, error, breaks, 1, hidden)
    lo = post_lo[1:] - pre_hi[:-1] - period[1:] - error[1:]
    hi = post_hi[1:] - pre_lo[:-1] - period[1:] + error[1:]
    return np.concatenate(([-np.inf], lo)), np.concatenate(([np.inf], hi))


def _definite(arr):
    """``(syncs, excess)``: TB bytes with a sync surely before them (more than a
    cell beyond a byte period), and the least extra cycles before each."""
    period = float(np.median(np.diff(arr.read))) if len(arr.read) > 1 else 0.0
    excess = arr.lo[1:] - arr.hi[:-1] - period
    syncs = np.flatnonzero(excess > period / CELLS) + 1
    return syncs, excess[syncs - 1]


def _tb_positions(  # pylint: disable=too-many-arguments
    arr, base, evidence, anchored, rev, known=None
):
    """BITS index of each TB byte, following slips at the definite syncs; the
    definite syncs and which landed on a BITS boundary that explains them.

    ``evidence`` is ``(capable, sync weights)`` of the BITS bytes; ``known``
    is optionally ``(positions, fits, need, trusted)``: where TS placed
    syncs, which of them each definite sync fits, which definite syncs TS
    must have seen, and the BITS index up to which it saw every one: only
    those explain such a sync. A slip rises by at most the bytes the sync's
    wait could hold as latched (timer wraps TS confirmed were the same sync
    in both passes).
    """
    n = len(arr.read)
    period = float(np.median(np.diff(arr.read))) if n > 1 else 0.0
    definite, _ = _definite(arr)
    room = arr.hi[definite] - arr.lo[definite - 1] - period
    room /= period * (1 - DRIFT)
    rise = np.where(np.isfinite(room), np.floor(room), n).astype(np.int64)
    offs, matched = _events_offsets(
        definite, base, evidence, anchored, rev, rise, known and known[:2]
    )
    which = np.clip(np.searchsorted(definite, np.arange(n), "right") - 1, 0, None)
    pos = base + np.arange(n) + (offs[which] if len(definite) else 0)
    if known is not None:
        where, fits, need, trusted = known
        need = need & (pos[definite] <= trusted)
        q = pos[definite]
        q = np.where(q < len(evidence[0]), q, q - (rev or 0))
        landed = (fits & (where[None, :] == q[:, None])).any(axis=1)
        landed |= (q <= 0) | (q >= len(evidence[0]))
        matched &= ~need | landed
    return pos, definite, matched


def _suspect(n, definite, matched):
    """TB bytes between the matched definite syncs around each unmatched one,
    where the TB pass may have slipped against the BITS bytes unseen."""
    out = np.zeros(n + 1, np.int64)
    hit = definite[matched]
    for k in definite[~matched]:
        i = np.searchsorted(hit, k)
        out[hit[i - 1] + 1 if i else 0] += 1
        out[hit[i] if i < len(hit) else n] -= 1
    return np.cumsum(out[:n]) > 0


def _monotone(pos):
    """``(keep, after)``: TB bytes whose BITS index precedes every later one's (a
    slip back drops the bytes it skips), and the least index from each on."""
    after = np.minimum.accumulate(pos[::-1])[::-1]
    return pos < np.append(after[1:], np.iinfo(np.int64).max), after


def _reindex(arr, pos, keep):
    """Arrivals over consecutive BITS indices from ``pos[keep][0]``: the kept TB
    bytes, and untimed bytes the TB pass never latched."""
    at = pos[keep] - pos[keep][0]
    lo, hi = np.full(at[-1] + 1, -np.inf), np.full(at[-1] + 1, np.inf)
    read, valid = np.full(at[-1] + 1, np.nan), np.zeros(at[-1] + 1, bool)
    lo[at], hi[at], read[at], valid[at] = (
        arr.lo[keep],
        arr.hi[keep],
        arr.read[keep],
        arr.valid[keep],
    )
    return Arrivals(read, lo, hi, valid)


def recurs(data, period, span=REPEAT_SPAN):
    """Per boundary q: the ``span`` bytes before or after it recur a ``period`` on,
    so a sync there is one a revolution later."""
    after = _repeats(np.asarray(data, np.uint8), period, span)
    return after | np.concatenate((np.zeros(span, bool), after[:-span]))


def _classify(arrs, capable_k, latched_k, ts_end):
    """Run ranges at every capable TB boundary, and the local byte period.

    ``arrs`` are the arrivals for each fitting wrap count; a boundary's
    excess spans them all. Boundaries that surely hold no sync, or a sync
    of one possible length, join the period estimate on the next round.
    """
    arr = arrs[0]
    n = len(arr.read)
    breaks = capable_k.copy()
    hidden = np.zeros(n)
    runs = {}
    for _ in range(PERIOD_ROUNDS):
        period, error = _local_period(arr, breaks, hidden)
        ex = [tb_excess(a, breaks, period, error, hidden) for a in arrs]
        ex_lo, ex_hi = np.min([x[0] for x in ex], axis=0), np.max(
            [x[1] for x in ex], axis=0
        )
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


def _plain(arr, pos, keep, definite, synced):
    """TB byte intervals and the BITS index after each; NaN unless one latched
    byte lies in it: the loop explains the wait, both ends keep consecutive
    BITS indices, and neither TB nor the merge put a sync there."""
    keep, after = keep
    plain = keep[1:] & keep[:-1] & (np.diff(pos) == 1) & arr.valid[1:]
    plain &= ~np.isin(np.arange(1, len(pos)), definite) & ~synced[1:]
    return after[1:], np.where(plain, np.diff(arr.read), np.nan)


def _wrapped(tb, steady, place, ts):
    """Arrivals for each fitting wrap count of the TB bytes TS syncs were placed
    on (``steady``: BITS boundary per TB byte, -1 where none), and the TB
    bytes whose wait fits no wrap count of their TS sync."""
    if place is None:
        return [tb_arrivals(tb)], np.zeros(0, np.int64)
    wraps, spread, unfit = _ts_wraps_at(tb, steady, place, ts[1])
    arrs = [
        tb_arrivals(tb, wraps + np.minimum(spread, j)) for j in range(spread.max() + 1)
    ]
    return arrs, unfit


def _demote(runs, suspect, latched, cell, ts_seen):
    """Run ranges where the TB pass may have slipped unseen: unbounded above, and
    no sync where TS (when ``ts_seen``) would have seen it and placed none."""
    for v in np.flatnonzero(suspect):
        if v in runs:
            run, lo, _ = runs[v]
            seen = ts_seen and (lo - PRE_ONES) * cell > TS_PB7_GAP
            runs[v] = (0, int(latched[v]), UNBOUNDED) if seen else (run, lo, UNBOUNDED)
    return runs


def merge_tb(  # pylint: disable=too-many-arguments,too-many-locals
    data, base, tb, ts=None, anchored=True, revolution=None, ts_end=None
):
    """Syncs of a BITS pass from TB (and TS for long syncs), TB[0] at ``data[base]``.

    ``ts`` is ``(count, iterations)`` from :func:`ts_syncs`, covering TB
    bytes before ``ts_end``; past it a sync's length is unbounded above. With
    the ``revolution`` in bytes, bytes before TB starts are timed a turn on
    where the BITS bytes recur. TS measures the boundaries TB cannot, and a
    TS sync TB's wait there cannot hold marks the TB pass as slipped around it.
    """
    data = np.asarray(data, np.uint8)
    tb = np.asarray(tb, np.int64)
    ok, latched = capable(data)
    place, trusted, (pos, events, matched) = _place(
        data,
        base,
        tb,
        ts,
        anchored,
        revolution,
        ts_end,
        (ok, sync_weights(ok, latched)),
    )
    missed = int((~matched).sum())
    keep, after = _monotone(pos)
    tb_steady = np.where(pos < len(data), pos, pos - (revolution or len(data)))
    arrs, unfit = _wrapped(tb, np.where(keep, tb_steady, -1), place, ts)
    unfit = np.setdiff1d(unfit[pos[unfit] <= trusted], events)
    order = np.argsort(np.concatenate((events, unfit)), kind="stable")
    events = np.concatenate((events, unfit))[order]
    matched = np.concatenate((matched, np.zeros(len(unfit), bool)))[order]
    p = pos[keep][0] + np.arange(pos[keep][-1] - pos[keep][0] + 1)
    suspect = np.zeros(len(p), bool)
    suspect[pos[keep] - p[0]] = _suspect(len(tb), events, matched)[keep]
    again = recurs(data, revolution) if revolution else np.zeros(len(data) + 1, bool)
    steady = np.where(p < len(data), p, p - (revolution or len(data)))
    at = np.clip(steady, 0, len(data) - 1)
    inside = (steady > 0) & (steady < len(data)) & ((p < len(data)) | again[at])
    end = None if ts_end is None else after[min(ts_end, len(tb) - 1)] - p[0]
    runs, byte_cycles = _classify(
        [_reindex(a, pos, keep) for a in arrs], inside & ok[at], latched[at], end
    )
    cell = byte_cycles / CELLS * (1 - DRIFT)
    runs = _demote(runs, suspect, latched[at], cell, place is not None)
    rows = [
        (int(q), *run)
        for v, run in runs.items()
        for q in (p[v], p[v] - revolution if revolution else -1)
        if 0 < q < len(data) and (q == p[v] or again[q])
    ]
    timed = {r[0] for r in rows if r[3] >= 0}
    if place is not None:
        more = _ts_rows(*place, ts[1], byte_cycles / CELLS)
        rows += [r for r in more if r[0] not in timed]
    first = 0 if anchored else int(np.clip(p[min(1, len(p) - 1)], 0, len(data)))
    valid = len(data) if anchored else int(np.clip(p[-1] + 1, first, len(data)))
    out = _syncs(rows, latched, (first, valid), byte_cycles, missed + len(unfit))
    synced = np.isin(tb_steady, out.positions)
    out.intervals = _plain(arrs[0], pos, (keep, after), events, synced)
    out.timing = (np.where(keep, pos, -1), arrs[0])
    return out


def _low_rows(positions, est, plo, phi, cell, n):
    """``(position, run, lo, hi)`` rows from SYNC low cycles: estimate and bounds
    (phi < 0: unbounded), for positions inside n bytes."""
    rows = []
    for p, e, lo_c, hi_c in zip(positions, est, plo, phi):
        if not 0 < p < n:
            continue
        lo = max(PRE_ONES + int(np.ceil(lo_c / cell)), SYNC_MIN_BITS)
        hi = PRE_ONES + int(np.floor(hi_c / cell)) if hi_c >= 0 else UNBOUNDED
        run = max(PRE_ONES + int(round(e / cell)), lo)
        rows.append((int(p), run if hi < 0 else min(run, hi), lo, hi))
    return rows


def _ts_place(  # pylint: disable=too-many-arguments,too-many-locals
    data, base, ts, byte, anchored, revolution, evidence, known=None
):
    """``(positions, syncs, unmatched, trusted)``: BITS boundaries of the TS syncs
    (``byte`` cycles per byte) and which sync each is, landing where they can
    on the syncs of another pass they fit (``known``, as
    :func:`_events_offsets`). With the ``revolution`` in bytes a sync is also
    placed a turn away where the BITS bytes recur. TS places every sync it
    could see up to BITS index ``trusted``, where it first slips against the
    BITS bytes.
    """
    count, iters = ts
    _, phi = ts_pulse(iters)
    rise = np.floor(phi / (byte * (1 - DRIFT))).astype(np.int64)
    late = (iters >= 256) & (iters % 256 == 0)
    free = np.concatenate(([0], np.where(late[:-1], TS_LATE_MERGE, 0)))
    offs, matched = _events_offsets(
        count, base, evidence, anchored, revolution, rise, known, free
    )
    step = np.diff(offs, prepend=0 if anchored else offs[:1])
    slip = np.flatnonzero((step < 0) | (step > free))
    trusted = np.inf
    if len(slip):
        trusted = base + count[slip[0] - 1] + offs[slip[0] - 1] if slip[0] else -np.inf
    sel = np.flatnonzero(matched)
    pos = base + count[sel] + offs[sel]
    if revolution:
        again = recurs(data, revolution)
        turn = pos % revolution
        both = np.stack((turn, turn + revolution))
        use = (both == pos) | again[np.minimum(turn, len(data))]
        pos, sel = both[use], np.tile(sel, (2, 1))[use]
    inside = (pos > 0) & (pos < len(data))
    return pos[inside], sel[inside], int((~matched).sum()), trusted


def _ts_rows(pos, sel, iters, cell):
    """``(position, run, lo, hi)`` rows of placed TS syncs from SYNC low time."""
    plo, phi = ts_pulse(iters[sel])
    return _low_rows(pos, (plo + phi) / 2, plo, phi, cell, np.inf)


def _ts_window(iters, period):
    """Extra cycles a TB byte's interval spans around each TS sync's SYNC low
    time, with the poll and sample slack and ``DRIFT`` between the passes."""
    plo, phi = ts_pulse(iters)
    slack = TB_LOOP + TB_OUT[-1][0]
    return (
        plo * (1 - DRIFT) - slack,
        phi * (1 + DRIFT) + PRE_ONES * period / CELLS + slack,
    )


def _fits(tb, iters):
    """``(definite, fits, need)``: the TB definite syncs, which TS syncs each fits
    (its wait, up to timer wraps, inside the SYNC low time) and which TS,
    polling PB7 at most ``TS_PB7_GAP`` apart, must have seen."""
    arr = tb_arrivals(tb)
    period = float(np.median(np.diff(arr.read)))
    definite, excess = _definite(arr)
    xlo, xhi = _ts_window(iters, period)
    fine = np.diff(arr.read, prepend=0.0)[definite] - period
    _, fits = wrap_fits(
        tb_intervals(tb)[0][definite - 1][:, None],
        fine[:, None],
        xlo[None, :],
        xhi[None, :],
    )
    low = excess - (PRE_ONES - MIN_LATCHED) * period / CELLS
    return definite, fits.any(axis=2), low * (1 - DRIFT) > TS_PB7_GAP


def _place(  # pylint: disable=too-many-arguments,too-many-locals
    data, base, tb, ts, anchored, revolution, ts_end, evidence
):
    """TB and TS placed on the BITS bytes, each landing where it can on the
    other's syncs it fits: TB alone, TS on it, then TB on TS. Returns the TS
    placement, the BITS index up to which TS saw every sync it could, and
    :func:`_tb_positions`."""
    arr = tb_arrivals(tb)
    rev = anchored and revolution
    pos, definite, matched = _tb_positions(arr, base, evidence, anchored, rev)
    if ts is None or np.size(ts[0]) == 0:
        return None, -np.inf, (pos, definite, matched)
    _, fits, need = _fits(tb, ts[1])
    q = pos[definite]
    q = np.where(q < len(data), q, q - (rev or 0))
    byte = float(np.median(np.diff(arr.read)))
    *place, _, trusted = _ts_place(
        data,
        base,
        ts,
        byte,
        anchored,
        revolution,
        evidence,
        (q[matched], fits[matched].T),
    )
    need &= definite < (ts_end or len(tb) + 1)
    known = (place[0], fits[:, place[1]], need, trusted)
    return place, trusted, _tb_positions(arr, base, evidence, anchored, rev, known)


def _ts_wraps_at(tb, steady, place, iters):
    """TB timer wraps at the TB bytes whose BITS boundary (``steady``, -1 where
    none) a TS sync was placed on: the fewest wrap counts leaving a wait the
    drive loop can end on inside the TS sync's SYNC low time, the spread of
    further fitting counts, and the TB bytes whose wait fits none; a TS sync
    on a boundary TB did not time constrains nothing."""
    where, sel = place
    arr = tb_arrivals(tb)
    period = float(np.median(np.diff(arr.read)))
    order = np.argsort(steady, kind="stable")
    at = np.searchsorted(steady[order], where)
    k = order[np.minimum(at, len(order) - 1)]
    found = (steady[k] == where) & (k > 0)
    k, sel = k[found], sel[found]
    xlo, xhi = _ts_window(iters[sel], period)
    w, fits = wrap_fits(
        tb_intervals(tb)[0][k - 1],
        np.diff(arr.read, prepend=0.0)[k] - period,
        xlo,
        xhi,
    )
    hit = fits.any(axis=1)
    first = np.argmax(fits, axis=1)
    last = fits.shape[1] - 1 - np.argmax(fits[:, ::-1], axis=1)
    fewest, spread = np.zeros(len(tb), np.int64), np.zeros(len(tb), np.int64)
    fewest[k[hit]] = w[hit, first[hit]]
    spread[k[hit]] = (last - first)[hit]
    return fewest, spread, k[~hit]


def merge_ts(  # pylint: disable=too-many-arguments
    data, base, ts, cell, anchored=True, revolution=None
):
    """Syncs of a BITS pass from TS alone: exact positions, lengths from SYNC low time.

    With the ``revolution`` in bytes each sync is also placed a turn away
    where the BITS bytes recur.
    """
    data = np.asarray(data, np.uint8)
    ok, latched = capable(data)
    evidence = (ok, sync_weights(ok, latched))
    pos, sel, unmatched, _ = _ts_place(
        data, base, ts, CELLS * cell, anchored, revolution, evidence
    )
    rows = _ts_rows(pos, sel, ts[1], cell)
    return _syncs(rows, latched, (0, len(data)), None, unmatched)


def stream_syncs(data, syncs, cell):
    """Syncs of a stream: exact positions, ``syncs`` = (positions, estimate, lo, hi)
    in SYNC low cycles (:meth:`nybulah.stream.Stream.syncs`)."""
    data = np.asarray(data, np.uint8)
    _, latched = capable(data)
    rows = _low_rows(*syncs, cell, len(data))
    return _syncs(rows, latched, (0, len(data)), None, 0)
