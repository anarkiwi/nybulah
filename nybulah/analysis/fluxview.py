"""Flux view: every revolution of every track as transitions in time ``tau``
(nominal bit cells of the zone), and the per-angle channels drawn from them.

Timing sources (:data:`TIMING`) and inferences are in docs/analysis.md (flux
view); each transition is spread over one divider carry, the read circuit's
time resolution (:mod:`.flux`: four carries per cell).
"""

from dataclasses import dataclass

import numpy as np
from tqdm import tqdm

from ..formats.g64 import SIDE1
from .capture import segments
from .cycle import TrackKind, find_cycle, lag_window
from .diskmap import _anchor, _faults, align
from .flux import divider, interval_bits
from .gcr import bits_per_revolution, speed_zone
from .regions import ILLEGAL_ZEROS, chains, parse

TIMING = ("flux", "tb", "zones", "track")
CARRIES_PER_CELL = 4
KERNEL_CELLS = 1.0 / CARRIES_PER_CELL
CHANNELS = ("density", "delta", "noflux", "var", "fault")


@dataclass
class FluxRev:  # pylint: disable=too-many-instance-attributes
    """One revolution: bits, ``(bit, tau, tau error bound)`` knots, transitions.

    Drawn from angle ``shift`` (turns); ``scale``: its cell over the standard
    zone's. Per bit: ``noflux`` (inferred), ``fault`` (slip starts), ``var``.
    """

    key: int
    zone: int
    bits: np.ndarray
    knots: tuple
    trans: np.ndarray
    timing: str
    indexed: bool
    read: bool
    shift: float = 0.0
    scale: float = 1.0
    noflux: np.ndarray = None
    fault: np.ndarray = None
    var: np.ndarray = None

    @property
    def n(self):
        """Bits in the revolution."""
        return len(self.bits)

    @property
    def period(self):
        """Revolution time in nominal cells."""
        return float(self.knots[1][-1])

    def tau(self, bit):
        """Time of fractional bit positions within the revolution."""
        return np.interp(bit, *self.knots[:2])

    def bit_at(self, tau):
        """Fractional bit position at times in ``(-period, 2 period)``."""
        kb, kt = self._ext()[:2]
        return np.interp(tau, kt, kb)

    def _ext(self):
        """Knots over three turns."""
        kb, kt, ke = self.knots
        return (
            np.concatenate((kb - self.n, kb[1:], kb[1:] + self.n)),
            np.concatenate((kt - self.period, kt[1:], kt[1:] + self.period)),
            np.concatenate((ke, ke[1:], ke[1:])),
        )

    def angle_tau(self, turns):
        """Times at angles (turns) in ``[0, 1]``, within ``(-period, period]``."""
        return (np.asarray(turns, float) - self.shift % 1.0) * self.period

    def trans_kept(self):
        """Transition times outside the inferred no-flux spans."""
        if self.noflux is None or not self.noflux.any():
            return self.trans
        at = np.clip(self.bit_at(self.trans).astype(np.int64), 0, self.n - 1)
        return self.trans[self.noflux[at] == 0]

    def channels(self, edges):
        """Per angular bin between ``edges`` (turns), each of :data:`CHANNELS`.

        density: transitions per cell; delta: cell length over the standard
        zone's less one, shrunk to 0 by the timing error bound and without
        transitions; noflux, var, fault: means over the bits in the bin.
        """
        u = self.angle_tau(edges)
        kb, kt, ke = self._ext()
        b = np.interp(u, kt, kb)
        err = np.interp(u, kt, ke)
        du, db = np.diff(u), np.maximum(np.diff(b), 1e-12)
        keep = self.trans_kept()
        t = np.concatenate((keep - self.period, keep, keep + self.period))
        count = np.diff(box_integral(t, u))
        delta = self.scale * du / db - 1
        bound = self.scale * (err[1:] + err[:-1]) / db
        delta = np.where(
            count > 0, np.sign(delta) * np.maximum(np.abs(delta) - bound, 0), 0
        )
        out = {"density": count / du, "delta": delta}
        for name in CHANNELS[2:]:
            out[name] = np.diff(_cum_at(getattr(self, name), b)) / db
        return out


def box_integral(t, x):
    """Integral to each ``x`` of unit-mass boxes ``KERNEL_CELLS`` wide on sorted ``t``."""
    half = KERNEL_CELLS / 2
    done = np.searchsorted(t + half, x, "right")
    started = np.searchsorted(t - half, x, "right")
    s = np.concatenate(([0.0], np.cumsum(t - half)))
    return done + ((started - done) * x - (s[started] - s[done])) / KERNEL_CELLS


def _cum_at(per_bit, b):
    """Integral of a per-bit quantity to fractional bits ``b``, extended periodically."""
    per_bit = np.asarray(per_bit, float)
    n = len(per_bit)
    cum = np.concatenate(([0.0], np.cumsum(per_bit)))
    turns = np.floor(b / n)
    return turns * cum[-1] + np.interp(b - turns * n, np.arange(n + 1), cum)


def flux_knots(cap):
    """``(bits, tau, error)`` at every transition of a flux capture, as
    :func:`.flux.decode_flux` clocks it (bit ``first[k]`` starts at transition
    ``k``), each latched to within one 16 MHz clock."""
    cell = 4 * divider(cap.zone)
    times = np.floor(np.asarray(cap.flux, float))
    end = cap.flux_index[-1] if len(cap.flux_index) else None
    if end is not None and end > times[-1]:
        times = np.append(times, np.floor(end))
    first = np.concatenate(([0], np.cumsum(interval_bits(np.diff(times), cap.zone))))
    return first.astype(float), times / cell, np.full(len(times), 1.0 / cell)


def tb_knots(rec):
    """``(bits, tau, error)`` of the bytes a TB pass timed: each byte ends in its
    arrival window (:func:`nybulah.passes.tb_arrivals`), taken at the middle."""
    from .. import passes
    from ..nibbler import cell_cycles

    arr = passes.tb_arrivals(rec.tb)
    if rec.ts is not None:
        period = float(np.median(np.diff(arr.read)))
        found = passes.ts_wraps(arr, rec.ts_syncs(), period, rec.base >= 0, rec.tb)
        arr = passes.tb_arrivals(rec.tb, found[0])
    ok = passes.capable(rec.data)[0]
    pos = passes._tb_positions(  # pylint: disable=protected-access
        arr, max(rec.base, 0), ok, rec.base >= 0
    )[0]
    seg = segments(rec)
    timed = np.isfinite(arr.lo) & np.isfinite(arr.hi) & arr.valid
    timed &= (pos >= 0) & (pos < len(seg.data))
    pos = pos[timed]
    s = np.searchsorted(seg.first, pos, "right") - 1
    start = np.where(s >= 0, seg.begin[np.maximum(s, 0)], 0)
    first = np.where(s >= 0, seg.first[np.maximum(s, 0)], 0)
    bits = (start + 8 * (pos - first) + 8).astype(float)
    cell = cell_cycles(rec.density)
    tau = (arr.lo[timed] + arr.hi[timed]) / 2 / cell
    err = (arr.hi[timed] - arr.lo[timed]) / 2 / cell
    keep = np.diff(bits, prepend=-1.0) > 0
    return bits[keep], tau[keep], err[keep]


def zone_knots(cap):
    """``(bits, tau, error)`` of a G64 track with a per-byte speed map."""
    cells = divider(np.asarray(cap.speed, float).ravel()) / divider(cap.zone)
    cells = np.repeat(cells, 8)[: len(cap.bits)]
    tau = np.concatenate(([0.0], np.cumsum(cells)))
    return np.arange(len(tau), dtype=float), tau, np.zeros(len(tau))


def _reach(knots, n):
    """Knots extended to bits 0 and ``n`` at the mean measured rate, as uncertain
    as the nearest measured knot."""
    kb, kt, ke = knots
    rate = (kt[-1] - kt[0]) / max(kb[-1] - kb[0], 1.0)
    inside = (kb > 0) & (kb < n)
    kb, kt, ke = kb[inside], kt[inside], ke[inside]
    return (
        np.concatenate(([0.0], kb, [float(n)])),
        np.concatenate(([kt[0] - kb[0] * rate], kt, [kt[-1] + (n - kb[-1]) * rate])),
        np.concatenate((ke[:1], ke, ke[-1:])),
    )


def capture_timing(cap):
    """``(timing, knots or None)`` of a whole capture."""
    if cap.flux is not None:
        return "flux", flux_knots(cap)
    rec = cap.framed
    if getattr(rec, "tb", None) is not None and rec.version >= 2:
        knots = tb_knots(rec)
        if ((knots[0] > 0) & (knots[0] < len(cap.bits))).sum() > 1:
            return "tb", _reach(knots, len(cap.bits))
    if cap.speed is not None and np.ndim(cap.speed):
        return "zones", zone_knots(cap)
    return "track", None


def _passes(rec, segs):
    """Bit spans between successive passes of one measured sync, ``segs`` apart."""
    run = segments(rec).run
    first = np.flatnonzero(run >= 0)
    k = np.arange(first[0], len(run) - segs, segs) if len(first) else np.zeros(0, int)
    k = k[(run[k] >= 0) & (run[k + segs] >= 0)]
    return list(zip(run[k].tolist(), run[k + segs].tolist()))


def cuts(cap):
    """``(start, end)`` bit offsets of a capture's whole revolutions, and whether
    angle 0 is the index; byte-ready captures are cut inside syncs."""
    n = len(cap.bits)
    if cap.revolutions:
        return list(zip(cap.index[:-1].tolist(), cap.index[1:].tolist())), True
    indexed = cap.flux_index is not None
    if cap.circular or n <= lag_window(cap.zone)[0]:
        return [(0, n)], indexed
    cycle = find_cycle(cap.framed if cap.framed is not None else cap.bits, cap.zone)
    if cap.framed is not None and cycle.segments:
        found = _passes(cap.framed, cycle.segments)
        if found:
            return found, indexed
    start = cycle.start if cycle.kind == TrackKind.FORMATTED else 0
    length = max(cycle.length, 1)
    count = (n - start) // length
    found = [(start + k * length, start + (k + 1) * length) for k in range(count)]
    return found or [(0, min(n, length))], indexed


def _rev(key, cap, span, timing, indexed):
    """The :class:`FluxRev` of bits ``span`` of a capture."""
    a, b = span
    kind, knots = timing
    bits = np.asarray(cap.bits[a:b], np.uint8)
    n = len(bits)
    if knots is None:
        turn = bits_per_revolution(cap.zone)
        knots = (np.array([0.0, n]), np.array([0.0, turn]), np.zeros(2))
    else:
        kb, kt, ke = knots
        lo, hi = np.interp([a, b], kb, kt)
        inside = (kb > a) & (kb < b)
        knots = (
            np.concatenate(([0.0], kb[inside] - a, [float(n)])),
            np.concatenate(([0.0], kt[inside] - lo, [hi - lo])),
            np.concatenate(
                (np.interp([a], kb, ke), ke[inside], np.interp([b], kb, ke))
            ),
        )
    trans = knots[1][1:-1] if kind == "flux" else None
    rev = FluxRev(key, cap.zone, bits, knots, trans, kind, indexed, not cap.circular)
    rev.scale = divider(cap.zone) / divider(standard_zone(key))
    if rev.trans is None:
        rev.trans = rev.tau(np.flatnonzero(bits).astype(float))
    return rev


def standard_zone(key):
    """DOS density zone of a track key's track (half tracks: the track below)."""
    return speed_zone(max((key & ~SIDE1) // 2, 1))


def _mapping(ref, rev):
    """``(to_ref, from_ref)`` bit maps between parsed revolutions: anchored at
    shared syncs, else proportional."""
    if len(ref.sync_start) and len(rev.sync_start) and not (ref.killer or rev.killer):
        found = align(ref, rev)
        return found.to_ref, found.from_ref
    return (
        lambda p: (np.asarray(p) * ref.n // max(rev.n, 1)) % max(ref.n, 1),
        lambda p: (np.asarray(p) * rev.n // max(ref.n, 1)) % max(rev.n, 1),
    )


def spans_mask(n, start, end):
    """Per-bit 0/1 mask of circular spans ``[start, end)`` with ``0 <= start < n``."""
    start, end = np.asarray(start, np.int64), np.asarray(end, np.int64)
    diff = np.zeros(n + 1, np.int64)
    for s, e in ((start, np.minimum(end, n)), (np.zeros_like(start), end - n)):
        ok = e > s
        np.add.at(diff, s[ok], 1)
        np.add.at(diff, np.minimum(e[ok], n), -1)
    return (np.cumsum(diff)[:n] > 0).astype(np.uint8)


def _annotate(rev, parsed):
    """Set the inferred no-flux mask and the bits where decode slips start."""
    rev.noflux = np.zeros(rev.n, np.uint8)
    if rev.timing != "flux" and len(parsed.zero_start):
        order = np.argsort(parsed.zero_start)
        zs = parsed.zero_start[order]
        start, end, _ = chains(zs, zs + parsed.zero_len[order])
        rev.noflux = spans_mask(rev.n, start % rev.n, start % rev.n + end - start)
    rev.fault = np.zeros(rev.n, np.uint8)
    if rev.read and rev.n:
        rev.fault[_faults(parsed)["start_bit"].astype(np.int64) % rev.n] = 1


@dataclass
class FluxTrack:
    """Revolutions of one track (the first is the reference) and their bit maps."""

    key: int
    revs: list
    maps: list

    @property
    def ref(self):
        """Reference revolution."""
        return self.revs[0]

    def drift(self, rev, samples):
        """``(turns, cells)``: measured time less uniform cells, or without
        measured timing the bit offset from the reference (median removed)."""
        r = self.revs[rev]
        if r.timing != "track":
            bit = np.linspace(0, r.n, samples, endpoint=False)
            tau = r.tau(bit)
            return (tau / r.period + r.shift) % 1.0, tau - bit * r.period / r.n
        c = np.linspace(0, self.ref.n, samples, endpoint=False).astype(np.int64)
        off = (self.maps[rev][1](c) - c + r.n // 2) % r.n - r.n // 2
        turns = (self.ref.tau(c) / self.ref.period + self.ref.shift) % 1.0
        return turns, off - np.median(off)


def build_track(key, captures):
    """:class:`FluxTrack` of every whole revolution of a track's captures; the
    reference is the first of the best capture (:func:`best_revolution`)."""
    from ..formats.image import best_revolution

    best = best_revolution(captures, key)[0] if len(captures) > 1 else captures[0]
    revs = []
    for cap in [best] + [c for c in captures if c is not best]:
        timing = capture_timing(cap)
        spans, indexed = cuts(cap)
        revs += [_rev(key, cap, s, timing, indexed) for s in spans]
    revs = [r for r in revs if r.n > ILLEGAL_ZEROS]
    if not revs:
        return None
    parsed = [parse(r.bits) for r in revs]
    maps = [_mapping(parsed[0], p) for p in parsed]
    base = revs[0]
    if not base.indexed:
        base.shift = -base.tau(_anchor(parsed[0])) / base.period
    for rev, p, (to_ref, _) in zip(revs, parsed, maps):
        _annotate(rev, p)
        if rev is not base and not (rev.indexed and base.indexed):
            rev.shift = base.tau(float(to_ref(0))) / base.period + base.shift
    cells = np.arange(base.n)
    p = np.mean([r.bits[m[1](cells) % r.n] for r, m in zip(revs, maps)], axis=0)
    var = 4 * p * (1 - p)
    for rev, (to_ref, _) in zip(revs, maps):
        rev.var = var[to_ref(np.arange(rev.n)) % base.n]
    return FluxTrack(key, revs, maps)


@dataclass
class FluxDisk:
    """Flux tracks of an image by key (halftrack | side)."""

    tracks: dict
    name: str = ""

    @property
    def keys(self):
        """Sorted track keys."""
        return np.array(sorted(self.tracks), np.int64)

    def raster(self, bins, rev=None):
        """``{key: {channel: (revolutions, bins)}}`` over equal angular bins;
        ``rev`` keeps one revolution (a track's last when it has fewer)."""
        edges = np.linspace(0.0, 1.0, bins + 1)
        out = {}
        for key, track in self.tracks.items():
            revs = track.revs
            if rev is not None:
                revs = [revs[min(rev, len(revs) - 1)]]
            found = [r.channels(edges) for r in revs]
            out[key] = {c: np.stack([f[c] for f in found]) for c in CHANNELS}
        return out

    def sources(self):
        """Revolutions per timing source."""
        found = {}
        for track in self.tracks.values():
            for r in track.revs:
                found[r.timing] = found.get(r.timing, 0) + 1
        return found

    def intervals(self):
        """``{zone: (measured, inferred)}`` transition intervals in cells."""
        found = {}
        for track in self.tracks.values():
            for r in track.revs:
                slot = found.setdefault(r.zone, ([], []))
                slot[r.timing != "flux"].append(np.diff(r.trans_kept()))
        return {
            z: tuple(np.concatenate(v) if v else np.zeros(0) for v in pair)
            for z, pair in sorted(found.items())
        }

    def eye(self, key, angle_bins, edges):
        """Histogram ``(interval bin, angle bin)`` of a track's intervals in cells."""
        hist = np.zeros((len(edges) - 1, angle_bins))
        for r in self.tracks[key].revs:
            t = r.trans_kept()
            turns = (t[:-1] / r.period + r.shift) % 1.0
            hist += np.histogram2d(
                np.diff(t), turns, (edges, np.linspace(0, 1, angle_bins + 1))
            )[0]
        return hist


def interval_edges(zone, top=2 * (ILLEGAL_ZEROS + 1)):
    """Histogram edges one 16 MHz clock apart, in cells of ``zone``, to ``top`` cells."""
    tick = 1.0 / (4 * divider(zone))
    return np.arange(0.0, top + tick, tick) - tick / 2


def flux_disk(image, captures=(), keys=None, progress=False):
    """:class:`FluxDisk` of a DiskImage; ``captures`` are other images of the disk."""
    chosen = [k for k in sorted(image.tracks) if keys is None or k in keys]
    tracks = {}
    for key in tqdm(chosen, desc="flux", unit="trk", disable=not progress):
        caps = image.tracks[key] + [c for o in captures for c in o.tracks.get(key, [])]
        track = build_track(key, caps)
        if track is not None:
            tracks[key] = track
    return FluxDisk(tracks, image.kind)
