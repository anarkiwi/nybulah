"""Test track pattern: ground truth, bit alignment of captures, per-region errors.

One revolution of known bits (every GCR byte, tagged syncs, a weak region, seeded
GCR, $55, DOS sectors) plus $55 filler of the writer's length. Captures are placed
per revolution by FFT cross-correlation, then aligned by banded edit distance.
"""

import dataclasses

import numba
import numpy as np

from .capture import _hidden
from .cycle import DEFAULT_ALPHA, UNMEASURED_TOLERANCE, _threshold, _zscore
from .cycle import lag_window
from .gcr import SYNC_MIN_BITS, bit_rate, bits_per_revolution, encode_bits
from .gcr import runs_of_ones, speed_zone, to_bits, track_capacity
from .sector import GAP_BYTE, HEADER_GAP_BYTES, SECTOR_BYTES, SYNC_BYTES
from .sector import format_track
from ..passes import CPU_HZ, ts_pulse

SEED = 1571
DISK_ID = b"NY"
DOS_SECTORS = 4
WEAK_BYTES = 32
GAP_BYTES = 64
T2_SPAN = 256
TS_SPAN = 256
EXACT, SYNC, UNSTABLE, FILLER = "exact", "sync", "unstable", "filler"
DIAG, DEL, INS, START = 0, 1, 2, 3
UNCOVERED, DELETED = -2, -1
COSTS = {EXACT: (2, 2), SYNC: (2, 1), UNSTABLE: (0, 0), FILLER: (0, 0)}


@dataclasses.dataclass(frozen=True)
class Region:
    """Bits ``[offset, offset + length)`` of the pattern; ``group`` names the part it
    belongs to, ``run`` the ones of a sync region."""

    name: str
    group: str
    kind: str
    offset: int
    length: int
    run: int = 0


def gap_bits(n, phase=0):
    """``n`` bits of $55 filler starting ``phase`` bits into a byte."""
    return to_bits([GAP_BYTE])[(phase + np.arange(n)) % 8]


def sync_runs(density):
    """Sync lengths written: the hardware minimum, DOS's, and runs just past the
    TB pass's T2 low byte span and the TS pass's release-wait counter span."""
    cell = CPU_HZ / bit_rate(density)
    ts = float(ts_pulse(np.array([TS_SPAN]))[1][0])
    longs = [int(np.ceil(span / cell)) + 1 for span in (T2_SPAN, ts)]
    return [SYNC_MIN_BITS, 8 * SYNC_BYTES, *longs]


@dataclasses.dataclass
class Truth:
    """A written pattern: its bits and regions, and the drive's cells per revolution
    when the write measured them."""

    halftrack: int
    density: int
    seed: int
    bits: np.ndarray
    regions: list
    cells: int | None = None

    @property
    def track(self):
        """Track number in the DOS headers."""
        return self.halftrack // 2

    @property
    def data(self):
        """The pattern as the bytes written."""
        return np.packbits(self.bits)

    def kinds(self):
        """Per pattern bit: index of its region."""
        out = np.empty(len(self.bits), np.int64)
        for i, r in enumerate(self.regions):
            out[r.offset : r.offset + r.length] = i
        return out

    def to_json(self):
        """JSON-ready record: every region's offset, length and expected bits."""
        regions = []
        for r in self.regions:
            expect = np.packbits(self.bits[r.offset : r.offset + r.length])
            regions.append(dataclasses.asdict(r) | {"expect": expect.tobytes().hex()})
        return {
            "halftrack": self.halftrack,
            "track": self.track,
            "density": self.density,
            "seed": self.seed,
            "cells": self.cells,
            "bits": len(self.bits),
            "regions": regions,
        }

    @classmethod
    def from_json(cls, rec):
        """Truth of a record written by :meth:`to_json`."""
        bits = np.zeros(rec["bits"], np.uint8)
        regions = []
        for r in rec["regions"]:
            fields = {k: r[k] for k in ("name", "group", "kind", "offset", "length")}
            region = Region(**fields, run=r.get("run", 0))
            expect = np.unpackbits(np.frombuffer(bytes.fromhex(r["expect"]), np.uint8))
            bits[region.offset : region.offset + region.length] = expect[
                : region.length
            ]
            regions.append(region)
        return cls(
            rec["halftrack"], rec["density"], rec["seed"], bits, regions, rec["cells"]
        )


def _split_syncs(parts):
    """Regions of the concatenated parts; each run of sync ones is its own region,
    in the part where it ends."""
    bits = np.concatenate([p[2] for p in parts])
    part = np.repeat(np.arange(len(parts)), [len(p[2]) for p in parts])
    starts, lengths = runs_of_ones(bits)
    run = np.full(len(bits), -1, np.int64)
    for k, (s, n) in enumerate(zip(starts, lengths)):
        run[s : s + n] = k
        part[s : s + n] = part[s + n - 1]
    key = np.where(run >= 0, len(parts) + run, part)
    cuts = np.flatnonzero(np.diff(key)) + 1
    regions, count = [], {}
    for a, b in zip(np.append(0, cuts), np.append(cuts, len(bits))):
        group, kind, _ = parts[part[a]]
        sync = bool(run[a] >= 0)
        label = f"{group}.sync" if sync else f"{group}."
        name = f"{label}{count.setdefault(label, 0)}"
        count[label] += 1
        a, b = int(a), int(b)
        regions.append(
            Region(
                name, group, SYNC if sync else kind, a, b - a, (b - a) if sync else 0
            )
        )
    return bits, regions


def make_truth(halftrack, density=None, seed=SEED, cells=None):
    """The pattern for a halftrack: as long as the shortest revolution a drive
    within the unmeasured speed tolerance turns at ``density``."""
    track = halftrack // 2
    density = speed_zone(max(track, 1)) if density is None else density
    rng = np.random.default_rng(seed)
    sectors = rng.integers(0, 256, (DOS_SECTORS, 256), dtype=np.uint8)
    capacity = DOS_SECTORS * (SECTOR_BYTES + HEADER_GAP_BYTES)
    dos = format_track(track, sectors, DISK_ID, capacity=capacity)
    gap = gap_bits(8 * HEADER_GAP_BYTES)
    parts = [("gcr_all", EXACT, encode_bits(np.arange(256, dtype=np.uint8)))]
    for i, run in enumerate(sync_runs(density)):
        tag = np.concatenate(([0], np.ones(run, np.uint8), encode_bits([i]), gap))
        parts.append((f"sync{run}", EXACT, tag.astype(np.uint8)))
    parts.append(("weak", UNSTABLE, np.zeros(8 * WEAK_BYTES, np.uint8)))
    resync = np.concatenate(([0], np.ones(8 * SYNC_BYTES), encode_bits([0]), gap))
    parts.append(("resync", EXACT, resync.astype(np.uint8)))
    total = 8 * int(track_capacity(density) * (1 - UNMEASURED_TOLERANCE))
    used = sum(len(p[2]) for p in parts) + 8 * GAP_BYTES + 8 * len(dos)
    count = (total - used) // 10
    if count <= 0:
        raise ValueError(f"density {density} leaves no room for the random region")
    parts.append(("random", EXACT, encode_bits(rng.integers(0, 256, count))))
    parts.append(("gap55", EXACT, gap_bits(total - used - 10 * count + 8 * GAP_BYTES)))
    parts.append(("dos", EXACT, to_bits(dos)))
    bits, regions = _split_syncs(parts)
    return Truth(halftrack, density, seed, bits, regions, cells)


@numba.njit(cache=True)
def _fill(c, e, sub, indel, band):
    """Banded edit-distance table: traceback steps and the best end cell.

    A mismatch at ``e[i]`` costs ``sub[i]``, an indel ``indel[i]`` (an insertion
    the cheaper of its neighbours'); both sequences' ends are free.
    """
    n, m = len(e), len(c)
    width = 2 * band + 1
    big = np.int64(1) << 40
    prev = np.full(width, big, np.int64)
    cur = np.full(width, big, np.int64)
    back = np.full((n + 1, width), START, np.uint8)
    best, end = (big, band), (0, band)
    for i in range(n + 1):
        for t in range(width):
            j = i + t - band
            cur[t] = big
            if j < 0 or j > m:
                continue
            cur[t] = 0
            if i and j:
                cur[t] = prev[t] + sub[i - 1] * (e[i - 1] != c[j - 1])
                back[i, t] = DIAG
                if t + 1 < width and prev[t + 1] + indel[i - 1] < cur[t]:
                    cur[t], back[i, t] = prev[t + 1] + indel[i - 1], DEL
                gap = min(indel[i - 1], indel[min(i, n - 1)])
                if t > 0 and cur[t - 1] + gap < cur[t]:
                    cur[t], back[i, t] = cur[t - 1] + gap, INS
            if (i == n or j == m) and (cur[t], abs(t - band)) < best:
                best, end = (cur[t], abs(t - band)), (i, t)
        prev, cur = cur, prev
    return back, end


@numba.njit(cache=True)
def _banded(c, e, sub, indel, band):
    """Edit-distance alignment of ``c`` against ``e`` within ``band`` of the diagonal
    (see :func:`_fill`): per ``e`` bit its ``c`` index (or DELETED, UNCOVERED),
    insertions before it, and whether the path touched the band's edge."""
    back, (i, t) = _fill(c, e, sub, indel, band)
    match = np.full(len(e), UNCOVERED, np.int64)
    ins = np.zeros(len(e) + 1, np.int64)
    edge = False
    while back[i, t] != START:
        edge = edge or min(t, 2 * band - t) == 0
        way = back[i, t]
        if way == INS:
            ins[i] += 1
            t -= 1
            continue
        match[i - 1] = i + t - band - 1 if way == DIAG else DELETED
        i -= 1
        t += way == DEL
    return match, ins[: len(e)], edge


def _correlate(c, p, w):
    """Per offset ``k`` of ``p`` against ``c`` (``-len(p) < k < len(c)``):
    weighted agreement and overlap."""
    nfft = 1 << (len(c) + len(p)).bit_length()
    span = np.arange(len(c) + len(p) - 1)

    def xcorr(a, b):
        return np.fft.irfft(np.fft.rfft(a, nfft) * np.fft.rfft(b[::-1], nfft), nfft)

    score = xcorr(2.0 * c - 1.0, w * (2.0 * p - 1.0))[span]
    overlap = np.rint(xcorr(np.ones(len(c)), np.abs(w)))[span]
    return (overlap + score) / 2, overlap


@dataclasses.dataclass
class Alignment:  # pylint: disable=too-many-instance-attributes
    """A capture's bits ``c`` aligned to repeated pattern copies.

    Per expected bit: track position (filler bits after the pattern's), region
    (``len(regions)``: filler), copy, matched ``c`` index and insertions before it;
    ``placements`` are each copy's ``c`` offset, ``found`` the significant ones.
    """

    c: np.ndarray
    pos: np.ndarray
    region: np.ndarray
    copy: np.ndarray
    match: np.ndarray
    ins: np.ndarray
    placements: np.ndarray
    found: int
    band: int

    def track_position(self, cbits):
        """Track positions of ``c`` bit indices, by the nearest matched bit."""
        ok = self.match >= 0
        m, p = self.match[ok], self.pos[ok]
        if m.size == 0:
            return np.full(np.shape(cbits), -1, np.int64)
        k = np.clip(np.searchsorted(m, cbits), 0, len(m) - 1)
        return p[k] + np.asarray(cbits) - m[k]

    def revolution(self, length):
        """Per consecutive copy pair: the median ``c`` distance of the pattern bits
        (of ``length``) matched in both."""
        out = []
        sel = (self.match >= 0) & (self.pos < length)
        for a in np.unique(self.copy[sel])[:-1]:
            at = np.full((2, length), -1, np.int64)
            for row, k in enumerate((a, a + 1)):
                s = sel & (self.copy == k)
                at[row, self.pos[s]] = self.match[s]
            both = (at >= 0).all(axis=0)
            if both.any():
                out.append(int(np.median(at[1, both] - at[0, both])))
        return out


def placements(c, truth, period=None, alpha=DEFAULT_ALPHA):
    """``(offsets, significant)``: ``c`` offsets of each pattern copy covering ``c``.

    Copies are significant cross-correlation peaks a revolution apart (``period``
    within the unmeasured tolerance, else the density's lag window); the rest are
    extrapolated by their median spacing, else ``period`` or the nominal one.
    """
    p = truth.bits
    w = np.array([r.kind != UNSTABLE for r in truth.regions], float)[truth.kinds()]
    agree, overlap = _correlate(c.astype(float), p.astype(float), w)
    pc, pp = c.mean(), p[w > 0].mean()
    z = _zscore(agree, overlap, pc * pp + (1 - pc) * (1 - pp))
    thr = _threshold(alpha, len(z))
    if period is None:
        lo, hi = lag_window(truth.density)
    else:
        tol = UNMEASURED_TOLERANCE
        lo, hi = int(period * (1 - tol)), int(np.ceil(period * (1 + tol)))
    best = int(np.argmax(z))
    if z[best] < thr:
        return np.zeros(0, np.int64), 0
    found = np.sort(_peaks(z, thr, best, lo, hi)) + 1 - len(p)
    nominal = period or bits_per_revolution(truth.density)
    spacing = int(round(np.median(np.diff(found)) if len(found) > 1 else nominal))
    before = int(np.ceil((found[0] + len(p)) / spacing))
    after = int(np.ceil((len(c) - found[-1]) / spacing))
    head = found[0] - spacing * np.arange(before, 0, -1)
    tail = found[-1] + spacing * np.arange(1, after + 1)
    out = np.concatenate((head, found, tail)).astype(np.int64)
    return out[(out + len(p) > 0) & (out < len(c))], len(found)


def _peaks(z, thr, best, lo, hi):
    """From ``best``, the strongest lag ``lo..hi`` on either way while it passes ``thr``."""
    found = [best]
    for step in (1, -1):
        k = best
        while True:
            a = max(k + (lo if step > 0 else -hi), 0)
            b = min(k + (hi if step > 0 else -lo), len(z) - 1)
            if a > b:
                break
            k = a + int(np.argmax(z[a : b + 1]))
            if z[k] < thr:
                break
            found.append(k)
    return np.array(found)


def _expected(truth, offsets, n):
    """Expected bits over ``c[0:n]``: copies at ``offsets``, filler between them.

    Returns bits, track position, region and copy per bit.
    """
    p, length = truth.bits, len(truth.bits)
    kinds = truth.kinds()
    bits = gap_bits(n)
    pos = np.full(n, -1, np.int64)
    region = np.full(n, len(truth.regions), np.int64)
    copy = np.full(n, -1, np.int64)
    ends = np.append(offsets[1:], n)
    for i, (k, end) in enumerate(zip(offsets, ends)):
        a, b = max(k, 0), min(k + length, n)
        bits[a:b] = p[a - k : b - k]
        pos[a:b] = np.arange(a - k, b - k)
        region[a:b] = kinds[a - k : b - k]
        copy[a:b] = i
        fa, fb = max(k + length, 0), min(end, n)
        if fb > fa:
            bits[fa:fb] = gap_bits(fb - fa, (fa - k - length) % 8)
            pos[fa:fb] = length + np.arange(fa - k - length, fb - k - length)
            copy[fa:fb] = i
    lead = np.flatnonzero(pos < 0)
    spacing = np.median(np.diff(offsets)) if len(offsets) > 1 else length
    pos[lead] = np.maximum(length, spacing - offsets[0] + lead).astype(np.int64)
    return bits, pos, region, copy


def align(c, truth, period=None, band=None, alpha=DEFAULT_ALPHA):
    """Align capture bits to the pattern (see :class:`Alignment`).

    Substitutions and indels cost alike, a sync's indels half (runs are measured),
    weak and filler bits nothing. ``band``, the starting half width (default: a bit
    per pattern sync), doubles while the best path touches its edge.
    """
    c = np.asarray(c, np.uint8)
    offsets, found = placements(c, truth, period, alpha)
    if offsets.size == 0:
        none = np.zeros(0, np.int64)
        return Alignment(c, none, none, none, none, none, offsets, 0, 0)
    e, pos, region, copy = _expected(truth, offsets, len(c))
    kinds = [r.kind for r in truth.regions] + [FILLER]
    sub, indel = np.array([COSTS[k] for k in kinds], np.uint8)[region].T.copy()
    band = band or sum(r.kind == SYNC for r in truth.regions)
    while True:
        match, ins, edge = _banded(c, e, sub, indel, band)
        if not edge or band >= len(c):
            break
        band *= 2
    return Alignment(c, pos, region, copy, match, ins, offsets, found, band)


def capture_bits(cap):
    """``(bits, byte_bit, begins)``: a capture's restored stream, a map from byte
    indices to bit indices in it, and the bit where each sync's segment begins."""
    data = np.asarray(cap.data, np.uint8)[: cap.valid_bytes]
    keep = cap.positions < cap.valid_bytes
    pos = cap.positions[keep]
    ends, _, extra = _hidden(to_bits(data), pos, cap.sync_bits[keep])
    lead = SYNC_MIN_BITS if cap.start == "sync" else 0
    cum = np.concatenate(([0], np.cumsum(extra)))

    def byte_bit(b):
        b = np.asarray(b, np.int64)
        return 8 * b + lead + cum[np.searchsorted(pos, b, side="right")]

    return cap.bits(), byte_bit, ends + lead + cum[1:]


def _copies(al, region):
    """Per copy of a region: indices of its matched expected bits."""
    sel = (al.region == region) & (al.match >= 0)
    return [np.flatnonzero(sel & (al.copy == k)) for k in np.unique(al.copy[sel])]


def _sync_found(al, runs, region):
    """Per copy: the run of ones in ``c`` holding the middle of a sync region."""
    starts, lengths = runs
    out = []
    for hit in _copies(al, region):
        j = al.match[hit[len(hit) // 2]]
        r = np.searchsorted(starts, j, side="right") - 1
        inside = r >= 0 and j < starts[r] + lengths[r]
        out.append(int(lengths[r]) if inside else 0)
    return out


def _framing(truth, al, begins, region):
    """Bit phase in the drive's bytes of each copy's region start, and the phase
    the written syncs give it."""
    r = truth.regions[region]
    syncs = [s for s in truth.regions[:region] if s.kind == SYNC]
    want = (r.offset - syncs[-1].offset - syncs[-1].length) % 8 if syncs else None
    seen = []
    for hit in _copies(al, region):
        j = al.match[hit[0]] - (al.pos[hit[0]] - r.offset)
        s = np.searchsorted(begins, j, side="right") - 1
        if s >= 0:
            seen.append(int((j - begins[s]) % 8))
    return {"expected": want, "seen": seen}


def truth_bits(truth, al):
    """Expected bit per aligned position (filler as $55)."""
    length = len(truth.bits)
    fill = al.pos >= length
    out = gap_bits(len(al.pos))
    out[~fill] = truth.bits[al.pos[~fill]]
    out[fill] = gap_bits(int(al.pos.max(initial=length)) + 1 - length)[
        al.pos[fill] - length
    ]
    return out


def _weak_reads(al, i, r):
    """Per copy covering the whole weak region: the bit read at each written bit
    (-1: deleted) and the ``c`` span it took."""
    out = []
    for k in np.unique(al.copy[al.region == i]):
        sel = (al.region == i) & (al.copy == k)
        m = al.match[sel]
        if (m == UNCOVERED).any() or len(m) != r.length:
            continue
        hit = m[m >= 0]
        span = int(hit[-1] - hit[0] + 1) if len(hit) else 0
        bits = np.where(m >= 0, al.c[np.maximum(m, 0)].astype(np.int64), -1)
        out.append({"bits": bits, "span": span + int(al.ins[sel][1:].sum())})
    return out


def region_report(truth, al, begins=()):
    """Per group: bits covered, bit errors, insertions and deletions outside syncs,
    start drift; per sync region the run written and found; weak reads; framing."""
    ok = al.match >= 0
    covered = al.match != UNCOVERED
    wrong = ok & (al.c[np.maximum(al.match, 0)] != truth_bits(truth, al))
    runs = runs_of_ones(al.c, 1)
    groups, syncs, weak = {}, [], []
    for i, r in enumerate(truth.regions):
        here = al.region == i
        g = groups.setdefault(
            r.group,
            {"kind": EXACT, "bits": 0, "errors": 0, "ins": 0, "del": 0, "drift": []},
        )
        if r.kind == SYNC:
            found = _sync_found(al, runs, i)
            syncs.append({"region": r.name, "written": r.run, "found": found})
        else:
            g["kind"] = r.kind
        g["bits"] += int((here & covered).sum())
        if r.kind != SYNC:
            g["del"] += int((here & (al.match == DELETED)).sum())
            g["ins"] += int(al.ins[here & covered].sum())
        g["errors"] += int((here & wrong).sum()) if r.kind != UNSTABLE else 0
        first = np.flatnonzero(here & ok & (al.pos == r.offset))
        g["drift"] += (al.match[first] - first).tolist()
        weak += _weak_reads(al, i, r) if r.kind == UNSTABLE else []
    for g in groups.values():
        g["slips"] = g["ins"] + g["del"]
    gap = next(i for i, r in enumerate(truth.regions) if r.group == "gap55")
    framing = _framing(truth, al, np.asarray(begins, np.int64), gap)
    return {"groups": groups, "syncs": syncs, "weak": weak, "gap55_framing": framing}


def instability(reads, length):
    """Across weak-region reads: positions read both 0 and 1, the mean pairwise
    disagreement, and per read its span and ones."""
    if not reads:
        return {"copies": 0}
    bits = np.stack([r["bits"] for r in reads])
    seen = bits >= 0
    ones = ((bits == 1) & seen).any(axis=0)
    zeros = ((bits == 0) & seen).any(axis=0)
    both = seen[:, None] & seen[None]
    differ = ((bits[:, None] != bits[None]) & both).sum(axis=-1)
    a, b = np.triu_indices(len(bits), 1)
    count = both.sum(axis=-1)[a, b]
    pairs = differ[a, b][count > 0] / count[count > 0]
    return {
        "copies": len(reads),
        "written_bits": length,
        "unstable_bits": int((ones & zeros).sum()),
        "disagreement": float(np.mean(pairs)) if pairs.size else None,
        "spans": [r["span"] for r in reads],
        "ones": [int((b == 1).sum()) for b in bits],
    }
