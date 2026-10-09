"""Disk map: the classified intervals of every revolution of every track.

Intervals (:mod:`.regions`) are classified against the clean-DOS outlier bounds
of :func:`nybulah.scenarios.thresholds`; other revolutions are aligned to the
surveyed one and compared, and framing shifts (:mod:`.faults`) blamed on the read.
"""

import json
import pathlib
from dataclasses import dataclass, field
from collections.abc import Callable
from enum import IntEnum

import numpy as np
from tqdm import tqdm

from .. import scenarios, survey
from .capture import segments
from .cycle import TrackKind, extract_revolution, find_cycle, lag_window
from .faults import stream_faults
from .gcr import sectors_per_track
from .regions import (
    DATA_BITS,
    HEADER_BITS,
    SKIP_BITS,
    Block,
    Blocks,
    agreement,
    block_kinds,
    canonical_keep,
    chains,
    headers_ok,
    pair_blocks,
    parse,
    ranges,
)

THRESHOLDS = pathlib.Path(__file__).resolve().parent.parent / "thresholds.json"


class Cls(IntEnum):
    """Colour classes; a higher class is drawn over a lower one."""

    NONE = 0
    STANDARD = 1
    DENSITY = 2
    GAP = 3
    SYNC = 4
    HEADER = 5
    DATA = 6
    WEAK = 7
    FAULT = 8


class Kind(IntEnum):
    """Fine region kinds; :data:`KIND_CLASS` gives each one's class."""

    SYNC = 0
    HEADER = 1
    DATA = 2
    GAP = 3
    ZERO_SPAN = 4
    SYNC_LONG = 5
    SYNC_SHORT = 6
    SYNC_IN_BLOCK = 7
    NO_SYNC = 8
    KILLER = 9
    HDR_GCR = 10
    HDR_CHECKSUM = 11
    HDR_TRACK = 12
    HDR_SECTOR = 13
    HDR_DUPLICATE = 14
    HDR_ID = 15
    HDR_MARK = 16
    DATA_GCR = 17
    DATA_CHECKSUM = 18
    DATA_SHORT = 19
    DATA_ORPHAN = 20
    DATA_MARK = 21
    BLOCK_OTHER = 22
    GAP_LONG = 23
    GAP_SHORT = 24
    GAP_FILL = 25
    GAP_IRREGULAR = 26
    GAP_NOFLUX = 27
    NOFLUX_SPAN = 28
    DISAGREE = 29
    UNFORMATTED = 30
    ZONE = 31
    ZONE_MIXED = 32
    ZONE_LABEL = 33
    LONG_TRACK = 34
    SHORT_TRACK = 35
    HALF_TRACK = 36
    FAT_TRACK = 37
    EMPTY = 38
    CAPTURE_FAULT = 39
    ID_NO_DATA = 40
    DATA_SIZE = 41
    DATA_DELETED = 42


KIND_CLASS = np.array(
    [Cls.STANDARD] * 5
    + [Cls.SYNC] * 5
    + [Cls.HEADER] * 7
    + [Cls.DATA] * 6
    + [Cls.GAP] * 4
    + [Cls.WEAK] * 4
    + [Cls.DENSITY] * 7
    + [Cls.NONE, Cls.FAULT]
    + [Cls.HEADER, Cls.DATA, Cls.DATA],
    np.uint8,
)
TRACK_SCENARIOS = {
    "killer": Kind.KILLER,
    "no_sync": Kind.NO_SYNC,
    "nonstandard_density": Kind.ZONE,
    "mixed_density": Kind.ZONE_MIXED,
    "density_label_mismatch": Kind.ZONE_LABEL,
    "long_track": Kind.LONG_TRACK,
    "short_track": Kind.SHORT_TRACK,
    "half_track_data": Kind.HALF_TRACK,
    "fat_track": Kind.FAT_TRACK,
}
WIDE = np.isin(
    np.arange(len(Kind)), [*TRACK_SCENARIOS.values(), Kind.UNFORMATTED, Kind.EMPTY]
)


class Stability(IntEnum):
    """How a region behaves across revolutions."""

    INTRINSIC = 0
    UNSTABLE = 1
    TRANSIENT = 2
    UNCONFIRMED = 3


UNSTABLE_MARK, FAULT_MARK = 1, 2
REGION_DTYPE = np.dtype(
    [
        ("track", "<u2"),
        ("rev", "<u2"),
        ("start_bit", "<i4"),
        ("end_bit", "<i4"),
        ("kind", "u1"),
        ("cls", "u1"),
        ("detail", "<i4"),
        ("stability", "u1"),
    ]
)


def load_thresholds(path=None):
    """Outlier bounds per capture family from a survey ``summary.json`` or a
    bare thresholds file (default: the bounds shipped with nybulah)."""
    data = json.loads(pathlib.Path(path or THRESHOLDS).read_text(encoding="utf-8"))
    return data.get("thresholds", data)


def _rows(start, end, kind, detail):
    out = np.zeros(len(start), REGION_DTYPE)
    out["start_bit"], out["end_bit"] = start, end
    out["kind"], out["detail"] = kind, detail
    out["cls"] = KIND_CLASS[out["kind"]]
    return out[out["end_bit"] > out["start_bit"]]


def _syncs(rev, thr):
    start, length = rev.sync_start, rev.sync_len
    prev = np.roll(np.arange(len(start)), 1)
    inside = rev.seg_len[prev] < SKIP_BITS[rev.block_kind[prev]]
    kind = np.select(
        [inside, length > thr["sync_long"], length < thr["sync_short"]],
        [Kind.SYNC_IN_BLOCK, Kind.SYNC_LONG, Kind.SYNC_SHORT],
        Kind.SYNC,
    )
    return _rows(
        start, start + length, kind, np.where(inside, rev.seg_len[prev], length)
    )


def _headers(rev, key, disk_id):
    idx = np.flatnonzero(rev.block_kind == Block.HEADER)
    hdr, valid = rev.hdr[idx], rev.valid[idx]
    ok = headers_ok(hdr, valid)
    place = hdr[:, 3].astype(np.int64) << 8 | hdr[:, 2]
    dupe = ok & (np.bincount(place[ok], minlength=1 << 16)[place] > 1)
    ids = hdr[:, 5].astype(np.int64) << 8 | hdr[:, 4]
    kind = np.select(
        [
            ~valid[:, :6].all(axis=1),
            ~ok,
            hdr[:, 3] != survey.physical_track(key),
            hdr[:, 2] >= sectors_per_track(survey.zone_track(key)),
            dupe,
            (disk_id >= 0) & (ids != disk_id),
        ],
        [
            Kind.HDR_GCR,
            Kind.HDR_CHECKSUM,
            Kind.HDR_TRACK,
            Kind.HDR_SECTOR,
            Kind.HDR_DUPLICATE,
            Kind.HDR_ID,
        ],
        Kind.HEADER,
    )
    start = rev.block_start[idx]
    end = start + np.minimum(rev.seg_len[idx], HEADER_BITS)
    return _rows(start, end, kind, hdr[:, 2])


def _blocks(rev):
    """Data blocks, and blocks with another mark: a non-standard data mark after a
    header, a damaged header mark before a data block."""
    ok = headers_ok(rev.hdr, rev.valid)
    prev = (np.arange(len(ok)) - 1) % max(len(ok), 1)
    data, other = rev.data.index, np.flatnonzero(rev.block_kind == Block.OTHER)
    kind = np.select(
        [
            rev.seg_len[data] < DATA_BITS,
            ~rev.valid[data, 0] | ~rev.data.payload,
            rev.data.checksum != 0,
            ~ok[prev[data]],
        ],
        [Kind.DATA_SHORT, Kind.DATA_GCR, Kind.DATA_CHECKSUM, Kind.DATA_ORPHAN],
        Kind.DATA,
    )
    sector = np.where(ok[prev[data]], rev.hdr[prev[data], 2].astype(np.int64), -1)
    start = rev.block_start
    mark = ok[prev[other]] & rev.valid[other, 0]
    lost = rev.block_kind[(other + 1) % max(len(ok), 1)] == Block.DATA
    return np.concatenate(
        (
            _rows(
                start[data],
                start[data] + np.minimum(rev.seg_len[data], DATA_BITS),
                kind,
                np.select(
                    [kind == Kind.DATA_CHECKSUM, kind == Kind.DATA_ORPHAN],
                    [
                        rev.data.checksum,
                        (start[data] - start[prev[data]]) % max(rev.n, 1),
                    ],
                    sector,
                ),
            ),
            _rows(
                start[other],
                start[other] + rev.seg_len[other],
                np.select(
                    [mark, lost], [Kind.DATA_MARK, Kind.HDR_MARK], Kind.BLOCK_OTHER
                ),
                rev.hdr[other, 0],
            ),
        )
    )


def _gap_bounds(thr):
    """``(short, long)`` gap length bounds indexed by the kind of block before the gap."""
    out = np.array([[-np.inf, np.inf]] * len(Block))
    for name, block in (("header", Block.HEADER), ("data", Block.DATA)):
        out[block] = thr.get(f"{name}_gap_short", -np.inf), thr.get(
            f"{name}_gap_long", np.inf
        )
    return out


def _gaps(rev, thr):
    after, length = rev.gap_after, rev.gaps.length
    top, hits = rev.gap_classes()
    count = length // 8
    share = hits / np.maximum(count, 1)
    has = count > 0
    fill = thr.get("fill_classes")
    foreign = (
        has & ~np.isin(top, fill) if fill is not None else np.zeros(len(top), bool)
    )
    bounds = _gap_bounds(thr)[after]
    kind = np.select(
        [
            has & scenarios.ILLEGAL_FILL[top] & (share > scenarios.MAJORITY),
            foreign,
            has & (share < thr.get("gap_share", -np.inf)),
            length > bounds[:, 1],
            length < bounds[:, 0],
        ],
        [
            Kind.GAP_NOFLUX,
            Kind.GAP_FILL,
            Kind.GAP_IRREGULAR,
            Kind.GAP_LONG,
            Kind.GAP_SHORT,
        ],
        Kind.GAP,
    )
    detail = np.where(np.isin(kind, [Kind.GAP_NOFLUX, Kind.GAP_FILL]), top, length)
    keep = after != Block.OTHER
    start = rev.gaps.start[keep]
    return _rows(start, start + length[keep], kind[keep], detail[keep])


def _zero_spans(rev, thr):
    start, end, _ = chains(rev.zero_start, rev.zero_start + rev.zero_len)
    kind = np.where(end - start > thr["bad_span"], Kind.NOFLUX_SPAN, Kind.ZERO_SPAN)
    return _rows(start, end, kind, end - start)


def _segments(rev):
    if len(rev.sync_len):
        return rev.block_start, rev.seg_len
    return np.zeros(1, np.int64), np.array([rev.n])


def _faults(rev):
    """Decode failures after which framing resumes shifted (a slipped bit or byte);
    ``detail`` is the shift."""
    starts, lengths = _segments(rev)
    streams = [rev.bits[np.arange(s, s + n) % rev.n] for s, n in zip(starts, lengths)]
    faults = stream_faults(streams)
    faults = faults[faults["resynced"] & (faults["shift"] != 0)]
    start = starts[faults["segment"]] + faults["bit"]
    return _rows(start, start + faults["width"], Kind.CAPTURE_FAULT, faults["shift"])


def classify(rev, key, disk_id, thr, read=True):
    """Regions of a parsed revolution (``track``, ``rev`` and ``stability`` unset);
    decode faults are located only in reads, not in images of one revolution."""
    found = (
        _syncs(rev, thr),
        _headers(rev, key, disk_id),
        _blocks(rev),
        _gaps(rev, thr),
        _zero_spans(rev, thr),
    )
    return np.concatenate(found + ((_faults(rev),) if read else ()))


def _piecewise(src, dst, n_src, n_dst):
    """Map positions by the offset of the last anchor at or before them."""
    order = np.argsort(src)
    src, dst = src[order], dst[order]

    def apply(p):
        k = np.searchsorted(src, p % n_src, side="right") - 1
        return (dst[k] + (p - src[k]) % n_src) % n_dst

    return apply


@dataclass
class Alignment:
    """Maps between a revolution and the reference, and where their bits differ."""

    to_ref: Callable
    from_ref: Callable
    miss: np.ndarray


def _lag(ref, bits):
    """Position in ``ref`` of bit 0 of ``bits`` by a global canonical alignment."""
    keep_ref, keep = canonical_keep(ref.bits), canonical_keep(bits)
    ref_idx = np.flatnonzero(keep_ref)
    return ref_idx[agreement(ref.bits[keep_ref], bits[keep])[2] % len(ref_idx)]


def _synced(*revs):
    """Every revolution has syncs."""
    return all(len(r.sync_start) for r in revs)


def align(ref, rev):
    """:class:`Alignment` of parsed ``rev`` to parsed ``ref``, anchored at shared syncs.

    Blocks pair as :func:`pair_blocks` finds; each pair is compared bit by bit
    from both block starts over the narrower nominal width, so gaps, splices and
    the bits framed before a sync are not compared. Revolutions without syncs
    are compared whole after a global canonical alignment.
    """
    i, j = pair_blocks(rev.blocks, ref.blocks)[:2] if _synced(ref, rev) else ((),) * 2
    src, dst = rev.block_start[i], ref.block_start[j]
    width = np.minimum(ref.blocks.width[j], rev.blocks.width[i])
    if len(src) == 0:
        src, dst = np.zeros(1, np.int64), np.array([_lag(ref, rev.bits)])
        width = np.array([min(ref.n, rev.n)])
    to_ref = _piecewise(src, dst, rev.n, ref.n)
    from_ref = _piecewise(dst, src, ref.n, rev.n)
    pos = ranges(dst, width)
    other = rev.bits[ranges(src, width) % rev.n]
    return Alignment(
        to_ref, from_ref, np.unique(pos[ref.bits[pos % ref.n] != other] % ref.n)
    )


def _passes(ref, cap):
    """Whole revolutions of a byte-ready capture: the bits between successive
    passes of one sync, its segments paired with the reference blocks."""
    seg = segments(cap.framed)
    if ref.sync_len.size == 0 or len(seg) < 2:
        return []
    ends = np.minimum(seg.begin, len(seg.bits) - 1)
    blocks = Blocks(seg.bits, ends, seg.content, block_kinds(seg.bits, ends)[0])
    k, near, turn = pair_blocks(blocks, ref.blocks)
    keep = seg.run[k] >= 0
    order = np.lexsort((turn[keep], near[keep]))
    k, near, turn = k[keep][order], near[keep][order], turn[keep][order]
    nxt = np.flatnonzero((near[1:] == near[:-1]) & (turn[1:] == turn[:-1] + 1))
    if not nxt.size:
        return []
    best = np.argmax(np.bincount(near[nxt]))
    return [seg.bits[seg.run[k[a]] : seg.run[k[a + 1]]] for a in nxt[near[nxt] == best]]


def _overlap(a, b, n):
    """Pairwise overlap of circular intervals ``a`` (rows) and ``b`` (columns)."""
    d = (b["start_bit"][None].astype(np.int64) - a["start_bit"][:, None]) % n
    la = (a["end_bit"] - a["start_bit"])[:, None]
    return (d < la) | ((n - d) % n < (b["end_bit"] - b["start_bit"])[None])


def _reach(regs):
    """Regions extended back to what explains them: an orphan data block to the block before."""
    out = regs.copy()
    orphan = out["kind"] == Kind.DATA_ORPHAN
    out["start_bit"] = np.where(
        orphan, out["start_bit"] - out["detail"], out["start_bit"]
    )
    return out


def stability(regs, nrev, n):
    """Set ``stability`` of one track's regions; keep only the faults that explain one.

    Intrinsic: every revolution has one of its kind overlapping it and no
    unexplained disagreement does. Transient: lone, over a slip of its own read.
    """
    fault = regs["kind"] == Kind.CAPTURE_FAULT
    dis = regs["kind"] == Kind.DISAGREE
    rev = regs["rev"]
    over = _overlap(regs, regs, n)
    same = over & (regs["kind"][:, None] == regs["kind"][None])
    revs = (same @ (rev[:, None] == np.arange(nrev)) > 0).sum(axis=1)
    everywhere = (revs == nrev) & (nrev > 1)
    explains = (rev[:, None] == rev[None]) | (dis[:, None] & (rev[None] == 0))
    slips = _overlap(_reach(regs), regs, n) & explains & (fault & ~everywhere)[None]
    odd = (regs["cls"] > Cls.STANDARD) & ~fault & ((regs["cls"] != Cls.WEAK) | dis)
    odd &= ~np.isin(regs["kind"], [Kind.BLOCK_OTHER, Kind.DATA_MARK]) & ~everywhere
    by_read = odd & slips.any(axis=1)
    shaky = (over & (dis & ~by_read)[None]).any(axis=1) | ((revs < nrev) & ~dis)
    st = np.full(len(regs), Stability.UNCONFIRMED if nrev == 1 else Stability.INTRINSIC)
    st[(shaky & (nrev > 1)) | dis] = Stability.UNSTABLE
    st[by_read | fault] = Stability.TRANSIENT
    regs["stability"] = st
    return regs[~fault | slips[by_read].any(axis=0)]


def _windows(cap, best, ref):
    """Whole revolutions of one capture: index to index, sync to sync for other
    byte-ready captures (see :func:`_passes`), else cycle by cycle; for the
    surveyed capture, those after its surveyed revolution."""
    if cap.revolutions:
        cuts = np.stack((cap.index[:-1], cap.index[1:]), axis=1)
        return [cap.bits[a:b] for a, b in cuts[int(best) :]]
    if cap.circular or len(cap.bits) < lag_window(cap.zone)[0]:
        return [] if best else [cap.bits]
    if cap.framed is not None and not best:
        return _passes(ref, cap)
    source = cap.framed if cap.framed is not None else cap.bits
    cycle = find_cycle(source, cap.zone)
    if cycle.kind != TrackKind.FORMATTED:
        return []
    starts = cycle.start + cycle.length * np.arange(1, len(cap.bits) // cycle.length)
    starts = starts[starts + cycle.length <= len(cap.bits)]
    first = [] if best else [extract_revolution(source, cycle)]
    return first + [cap.bits[a : a + cycle.length] for a in starts]


def _anchor(rev):
    """Start of the sync before the first sector 0 header, else of the longest sync."""
    if rev.sync_len.size == 0:
        return 0
    zero = headers_ok(rev.hdr, rev.valid) & (rev.hdr[:, 2] == 0)
    k = np.flatnonzero(zero)[0] if zero.any() else int(np.argmax(rev.sync_len))
    return int(rev.sync_start[k])


@dataclass
class _Track:
    """Inputs of one track's map: its surveyed revolution, captures and whole-track kinds."""

    key: int
    best: tuple
    ref: object
    caps: list
    wide: list
    disk_id: int


def _local(track, thr):
    """Regions of every revolution of a formatted track, in the reference frame."""
    cap = track.best[0]
    revs = [(track.ref, not cap.circular)] + [
        (parse(w), not c.circular)
        for c in track.caps
        for w in _windows(c, c is cap, track.ref)
    ]
    out = [classify(track.ref, track.key, track.disk_id, thr, revs[0][1])]
    for r, (rev, read) in enumerate(revs[1:], 1):
        found = align(track.ref, rev)
        regs = classify(rev, track.key, track.disk_id, thr, read)
        width = np.minimum(regs["end_bit"] - regs["start_bit"], track.ref.n)
        regs["start_bit"] = found.to_ref(regs["start_bit"].astype(np.int64))
        regs["end_bit"] = regs["start_bit"] + width
        start, end, members = chains(found.miss, found.miss + 1)
        regs = np.concatenate((regs, _rows(start, end, Kind.DISAGREE, members)))
        regs["rev"] = r
        out.append(regs)
    return np.concatenate(out), len(revs)


def _track_map(track, thr, aligned):
    """Regions of one track, rotated so its anchor is bit 0 unless index-aligned."""
    n = max(track.ref.n, 1)
    count = len(track.wide)
    wide = _rows(np.zeros(count), np.full(count, n), track.wide, 0)
    if track.best[2].kind != TrackKind.FORMATTED or Kind.EMPTY in track.wide:
        regs, nrev = wide, sum(max(c.revolutions, 1) for c in track.caps)
    else:
        local, nrev = _local(track, thr)
        regs = np.concatenate((wide, stability(local, nrev, n)))
    regs["stability"][: len(wide)] = (
        Stability.UNCONFIRMED if nrev == 1 else Stability.INTRINSIC
    )
    if not aligned:
        width = regs["end_bit"] - regs["start_bit"]
        regs["start_bit"] = (regs["start_bit"] - _anchor(track.ref)) % n
        regs["end_bit"] = regs["start_bit"] + width
    regs["track"] = track.key
    return regs, nrev


def _wide_kinds(rows, linear, thr):
    """Whole-track kinds per row from the survey scenarios; tracks without a
    revolution are unformatted (whole DOS tracks) or empty."""
    d = scenarios.derive(rows, np.full(len(rows), "nib" if linear else "g64"))
    masks = scenarios.scenarios(rows, d, thr)
    kinds = [
        [k for name, k in TRACK_SCENARIOS.items() if masks[name][i]]
        for i in range(len(rows))
    ]
    unformatted = rows["kind"] == TrackKind.UNFORMATTED
    lost = unformatted & d["whole"] & (rows["halftrack"] // 2 <= scenarios.DOS_TRACKS)
    for i in np.flatnonzero(unformatted | masks["half_track_crosstalk"]):
        kinds[i] = [Kind.UNFORMATTED if lost[i] else Kind.EMPTY]
    return kinds


def _cover(row, start, end, layer, shape, bins, n):
    """Per layer, row and bin: whether an interval covers it (intervals wrap at ``n``)."""
    wrap = end > n
    row, layer = np.append(row, row[wrap]), np.append(layer, layer[wrap])
    start = np.append(start, np.zeros(wrap.sum(), np.int64))
    end, n = np.append(np.minimum(end, n), end[wrap] - n[wrap]), np.append(n, n[wrap])
    b0 = start * bins // n
    b1 = np.maximum(-(-end * bins // n), b0 + 1)
    diff = np.zeros((*shape, bins + 1), np.int32)
    np.add.at(diff, (layer, row, b0), 1)
    np.add.at(diff, (layer, row, b1), -1)
    return np.cumsum(diff, axis=-1)[..., :bins] > 0


@dataclass
class DiskMap:
    """Regions of an image with its survey rows, and their raster.

    ``grid[row, bin]`` is the class drawn over each angular bin of each track
    (rows follow ``keys``); ``marks`` flags unstable and capture-fault bins.
    """

    keys: np.ndarray
    tracks: np.ndarray
    revs: np.ndarray
    aligned: np.ndarray
    regions: np.ndarray
    bins: int = 2048
    thresholds: dict = field(default_factory=dict)
    grid: np.ndarray = field(init=False)
    marks: np.ndarray = field(init=False)

    def __post_init__(self):
        self.grid, self.marks = self.raster()

    @property
    def length(self):
        """Bits in each track's reference revolution."""
        return self.tracks["rev_len"].astype(np.int64)

    def select(self, rev=None):
        """Regions shown for revolution ``rev`` (a track's last one if it has fewer)."""
        r = self.regions
        if rev is None:
            return r
        row = np.searchsorted(self.keys, r["track"])
        return r[WIDE[r["kind"]] | (r["rev"] == np.minimum(rev, self.revs[row] - 1))]

    def raster(self, rev=None, bins=None):
        """``(grid, marks)`` of every revolution together, or of one (see :meth:`select`)."""
        bins = bins or self.bins
        r = self.select(rev)
        row = np.searchsorted(self.keys, r["track"])
        n = np.maximum(self.length[row], 1)
        start, end = r["start_bit"].astype(np.int64), r["end_bit"].astype(np.int64)
        transient = r["stability"] == Stability.TRANSIENT
        cls = np.where(transient & (r["cls"] != Cls.FAULT), Cls.STANDARD, r["cls"])
        paint = cls != Cls.FAULT
        layer = cls + len(Cls) * (~WIDE[r["kind"]] & (cls > Cls.STANDARD))
        shape = (2 * len(Cls), len(self.keys))
        cover = _cover(
            row[paint], start[paint], end[paint], layer[paint], shape, bins, n[paint]
        )
        top = len(cover) - 1 - np.argmax(cover[::-1], axis=0)
        grid = np.where(cover.any(axis=0), top % len(Cls), Cls.NONE).astype(np.uint8)
        unstable = (r["stability"] == Stability.UNSTABLE) & (cls > Cls.STANDARD)
        sel = (unstable & paint) | ~paint
        flag = (~paint[sel]).astype(np.int64)
        shape = (2, len(self.keys))
        marks = _cover(row[sel], start[sel], end[sel], flag, shape, bins, n[sel])
        return grid, (marks[0] * UNSTABLE_MARK | marks[1] * FAULT_MARK).astype(np.uint8)


def disk_map(image, captures=None, bins=2048, thresholds=None, progress=False):
    """:class:`DiskMap` of a DiskImage; ``captures`` are other images of the same disk."""
    thr = load_thresholds() if thresholds is None else thresholds
    measured = survey.measure_image(image)
    keys = np.array(sorted(image.tracks), np.int64)
    caps = [
        image.tracks[k] + [c for o in captures or () for c in o.tracks.get(k, [])]
        for k in keys
    ]
    linear = image.kind in scenarios.LINEAR_KINDS or any(
        c.framed is not None for t in caps for c in t
    )
    fam = thr["linear" if linear else "circular"]
    wide = _wide_kinds(measured.rows, linear, thr)
    regions, revs = [], np.ones(len(keys), np.int64)
    aligned = np.zeros(len(keys), bool)
    for i, key in enumerate(tqdm(keys, desc="map", unit="trk", disable=not progress)):
        best = measured.best[key]
        aligned[i] = best[0].revolutions > 0 or image.kind == "p64"
        track = _Track(
            int(key), best, measured.parsed[key], caps[i], wide[i], measured.ids[0]
        )
        regs, revs[i] = _track_map(track, fam, aligned[i])
        regions.append(regs)
    regions = np.concatenate(regions) if regions else np.zeros(0, REGION_DTYPE)
    return DiskMap(keys, measured.rows, revs, aligned, regions, bins, thr)
