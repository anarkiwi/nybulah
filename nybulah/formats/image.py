"""Format-independent disk captures: sniffing loader, analysis and conversion.

Every format loads into a :class:`DiskImage`: per track key (halftrack, with
``SIDE1`` for the second side) a list of :class:`Capture` bit streams.
"""

import pathlib
from dataclasses import dataclass, field, replace

import numpy as np
from tqdm import tqdm

from ..analysis.capture import framed_capture, segments
from ..analysis.cycle import (
    NOMINAL_PERIOD,
    Cycle,
    TrackKind,
    extract_revolution,
    find_cycle,
    lag_window,
)
from ..analysis.flux import ROTATION_TICKS, decode_flux, estimate_zone
from ..analysis.gcr import decode_bits, runs_of_ones, sectors_per_track, to_bits
from ..analysis.sector import HEADER_ID, SectorError, decode_track
from .convert import _score, d64_to_g64, g64_to_d64, revolution_bytes
from .d64 import read_d64
from .g64 import FIRST_HALFTRACK, G64, SIDE1, G64Track, read_g64
from .kryoflux import STREAM_NAME, read_stream
from .nbz import lz_decompress
from .nib import NB2_PASSES, NIB_HEADER, NIB_SIGNATURE, NIB_TRACK, read_nib
from .p64 import P64, STRONG, P64Track, read_p64, track_from_bits, track_times
from .scp import read_scp

P64_WEAK_REVOLUTIONS = 5
SIDE1_TRACK_BASE = 35
LAYOUTS = {
    "cylinders": lambda tnr: (2 * (tnr >> 1) + FIRST_HALFTRACK) | (SIDE1 * (tnr & 1)),
    "half-cylinders": lambda tnr: ((tnr >> 1) + FIRST_HALFTRACK) | (SIDE1 * (tnr & 1)),
    "halftracks": lambda tnr: tnr + FIRST_HALFTRACK,
}


@dataclass
class Capture:
    """One read of a track as a 0/1 bit stream at density ``zone``.

    ``circular``: the bits are exactly one revolution. ``index``: bit offsets
    of index pulses. Flux sources keep transition times (16 MHz clocks, one
    revolution normalised to 300 rpm) in ``flux`` and ``flux_index``. Byte-ready
    sources keep their sync-framed form in ``framed`` (``bits`` is its stream).
    """

    bits: np.ndarray
    zone: int
    circular: bool = False
    index: np.ndarray = None
    speed: object = None
    flux: np.ndarray = None
    flux_index: np.ndarray = None
    strengths: np.ndarray = None
    framed: object = None

    @property
    def revolutions(self):
        """Whole index-to-index revolutions in the capture."""
        return 0 if self.index is None else max(len(self.index) - 1, 0)


@dataclass
class DiskImage:
    """Captures per track key, the source format and its parsed form."""

    kind: str
    tracks: dict = field(default_factory=dict)
    source: object = None
    meta: dict = field(default_factory=dict)


def _one_revolution(bits):
    _, lengths = runs_of_ones(bits, circular=True)
    kind = TrackKind.KILLER if 2 * lengths.sum() > len(bits) else TrackKind.FORMATTED
    return bits, Cycle(kind, 0, len(bits), 1.0, 0.0)


def revolution(capture):
    """``(bits, cycle)`` of one revolution of a capture (cycle start 0)."""
    bits = capture.bits
    if capture.framed is not None:
        cycle = find_cycle(capture.framed, capture.zone)
        if cycle.kind != TrackKind.FORMATTED:
            return bits[: cycle.length], cycle
        rev = extract_revolution(capture.framed, cycle)
        return rev, replace(cycle, start=0, length=len(rev))
    if capture.revolutions == 1:
        return _one_revolution(bits[capture.index[0] : capture.index[1]])
    if capture.circular or len(bits) <= lag_window(capture.zone)[0]:
        return _one_revolution(bits)
    if capture.revolutions:
        bits = bits[capture.index[0] : capture.index[-1]]
        cycle = find_cycle(bits, capture.zone, NOMINAL_PERIOD, index_aligned=True)
    else:
        cycle = find_cycle(bits, capture.zone)
    if cycle.kind == TrackKind.KILLER:
        return bits[: cycle.length], cycle
    rev = extract_revolution(bits, cycle)
    return rev, Cycle(cycle.kind, 0, cycle.length, cycle.match, cycle.z)


def best_revolution(captures, key):
    """``(capture, bits, cycle)``: fewest sector errors, then highest repetition."""
    reads = [(cap, *revolution(cap)) for cap in captures]
    return min(reads, key=lambda r: _score(r[1], r[2], key))


def header_track(bits):
    """Most common track number in the valid sector headers of ``bits``, or None."""
    starts, lengths = runs_of_ones(bits)
    ends = (starts + lengths)[starts + lengths + 80 <= len(bits)]
    if len(ends) == 0:
        return None
    hdr, valid = decode_bits(bits[ends[:, None] + np.arange(80)])
    ok = (
        (hdr[:, 0] == HEADER_ID)
        & valid[:, :6].all(axis=1)
        & (np.bitwise_xor.reduce(hdr[:, 1:6], axis=1) == 0)
    )
    return int(np.bincount(hdr[ok, 3]).argmax()) if ok.any() else None


def _expected_tracks(key):
    half, base = key & ~SIDE1, SIDE1_TRACK_BASE if key & SIDE1 else 0
    return {half // 2 + base, (half + 1) // 2 + base}


def choose_layout(headers, candidates, fallback):
    """Layout mapping raw track numbers to keys that best explains sector headers.

    ``headers`` maps raw track number to its header track (or None); ties
    keep ``fallback`` when it is among the best.
    """
    votes = {
        name: sum(
            h in _expected_tracks(LAYOUTS[name](tnr))
            for tnr, h in headers.items()
            if h is not None
        )
        for name in candidates
    }
    top = max(votes.values())
    return fallback if votes[fallback] == top else max(votes, key=votes.get)


def flux_capture(times, index, zone=None):
    """Decode transition times (16 MHz clocks) into a :class:`Capture`."""
    if zone is None:
        zone = estimate_zone(np.diff(np.floor(times)))
    zone = 3 if zone is None else zone
    end = None if index is None or len(index) == 0 else index[-1]
    bits, index_bits = decode_flux(times, zone, index, end)
    return Capture(bits, zone, index=index_bits, flux=times, flux_index=index)


def _from_flux(kind, tracks, candidates, fallback, layout, progress, **meta):
    raw = {
        tnr: flux_capture(*track.ticks())
        for tnr, track in tqdm(
            sorted(tracks.items()),
            desc=f"{kind} flux",
            unit="trk",
            disable=not progress,
        )
    }
    if layout is None:
        headers = {tnr: header_track(cap.bits) for tnr, cap in raw.items()}
        layout = choose_layout(headers, candidates, fallback)
    image = DiskImage(kind, meta=dict(meta, layout=layout))
    for tnr, cap in raw.items():
        image.tracks.setdefault(LAYOUTS[layout](tnr), []).append(cap)
    return image


def from_scp(scp, layout=None, progress=False):
    """Captures of an SCP image; ``layout`` defaults to the header vote."""
    fallback = "half-cylinders" if scp.tpi96 else "cylinders"
    image = _from_flux(
        "scp",
        scp.tracks,
        tuple(LAYOUTS),
        fallback,
        layout,
        progress,
        splices={t: f.splice for t, f in scp.tracks.items() if f.splice is not None},
    )
    image.source = scp
    return image


def from_kryoflux(streams, layout=None, progress=False):
    """Captures of KryoFlux streams keyed by ``(cylinder, side)``."""
    tracks = {2 * cyl + side: track for (cyl, side), track in streams.items()}
    deep = max((cyl for cyl, _ in streams), default=0) > 42
    fallback = "half-cylinders" if deep else "cylinders"
    candidates = ("cylinders", "half-cylinders")
    return _from_flux("kryoflux", tracks, candidates, fallback, layout, progress)


def from_g64(g64):
    """One circular capture per G64/G71 track."""
    image = DiskImage("g71" if g64.halftracks > 84 else "g64", source=g64)
    if g64.ext is not None:
        image.meta["ext"] = g64.ext
    for key, track in g64.tracks.items():
        speed = np.asarray(track.speed)
        zone = (
            int(np.bincount(speed.ravel(), minlength=4).argmax())
            if speed.ndim
            else int(speed)
        )
        image.tracks[key] = [
            Capture(to_bits(track.data), zone, True, speed=track.speed)
        ]
    return image


def framed(data, zone):
    """Capture of a byte-ready raw track whose syncs are stored as one bits."""
    cap = framed_capture(data)
    return Capture(segments(cap).bits, zone, framed=cap)


def from_nib(nib, kind="nib"):
    """NIB entries, or the header-density passes of NB2 entries."""
    image = DiskImage(kind, source=nib)
    for entry in nib.entries:
        passes = entry.data[None] if nib.passes is None else entry.data[entry.zone]
        image.tracks.setdefault(entry.halftrack, []).extend(
            framed(p, entry.zone) for p in passes
        )
    return image


def from_p64(p64, revolutions=P64_WEAK_REVOLUTIONS, rng=0):
    """Decode P64 pulses; tracks with weak pulses read ``revolutions`` times."""
    image = DiskImage("p64", source=p64)
    for key, track in p64.tracks.items():
        revs = 1 if (track.strengths == STRONG).all() else revolutions
        times, index = track_times(track, revs, rng)
        cap = flux_capture(times, index)
        cap.strengths, cap.circular = track.strengths, revs == 1
        if revs == 1:
            cap.index = None
        image.tracks[key] = [cap]
    return image


def from_d64(d64):
    """Standard formatting of a D64, one circular capture per track."""
    image = from_g64(d64_to_g64(d64, progress=False))
    image.kind, image.source = "d64", d64
    return image


def _is_nb2(buf):
    table = np.frombuffer(buf, np.uint8, NIB_HEADER - 0x10, 0x10).reshape(-1, 2)
    entries = int(np.argmin(np.append(table[:, 0], 0) != 0))
    return entries > 0 and len(buf) >= NIB_HEADER + entries * 4 * NB2_PASSES * NIB_TRACK


def _nib(buf, kind=None):
    nb2 = _is_nb2(buf)
    image = from_nib(read_nib(buf, nb2), kind or ("nb2" if nb2 else "nib"))
    image.meta["nb2"] = nb2
    return image


def _headerless(buf):
    """D64 by size, else an NBZ stream that unpacks to a NIB; None otherwise."""
    try:
        return from_d64(read_d64(buf))
    except ValueError:
        pass
    try:
        raw = lz_decompress(buf).tobytes()
    except ValueError:
        return None
    return _nib(raw, "nbz") if raw.startswith(NIB_SIGNATURE) else None


MAGIC = (
    (b"GCR-15", lambda buf, _: from_g64(read_g64(buf))),
    (NIB_SIGNATURE, lambda buf, _: _nib(buf)),
    (b"P64-1541", lambda buf, _: from_p64(read_p64(buf))),
    (b"SCP", lambda buf, options: from_scp(read_scp(buf), **options)),
)


def loads(buf, name="", **options):
    """Sniff and parse an image from bytes; ``name`` identifies KryoFlux streams."""
    buf = bytes(buf)
    for magic, loader in MAGIC:
        if buf.startswith(magic):
            return loader(buf, options)
    found = STREAM_NAME.search(name)
    if found:
        cyl_side = (int(found.group(1)), int(found.group(2)))
        return from_kryoflux({cyl_side: read_stream(buf)}, **options)
    image = _headerless(buf)
    if image is None:
        raise ValueError(f"{name or 'image'}: unrecognised format")
    return image


def load(path, **options):
    """Load any supported image; a KryoFlux stream loads its whole set."""
    path = pathlib.Path(path)
    if not (path.is_dir() or STREAM_NAME.search(path.name)):
        return loads(path.read_bytes(), path.name, **options)
    folder, prefix = (path, None) if path.is_dir() else (path.parent, _stem(path.name))
    streams = {
        (int(m.group(1)), int(m.group(2))): read_stream(item.read_bytes())
        for item in sorted(folder.iterdir())
        if (m := STREAM_NAME.search(item.name)) and prefix in (None, _stem(item.name))
    }
    return from_kryoflux(streams, **options)


def _stem(name):
    return name[: STREAM_NAME.search(name).start()]


def to_g64(image, progress=False):
    """Best revolution of every formatted or killer track, as a G64/G71."""
    if isinstance(image.source, G64):
        return image.source
    out = G64()
    for key in tqdm(sorted(image.tracks), desc="g64", unit="trk", disable=not progress):
        cap, bits, cycle = best_revolution(image.tracks[key], key)
        if cycle.kind != TrackKind.UNFORMATTED:
            speed = cap.speed if cap.speed is not None else cap.zone
            out.tracks[key] = G64Track(revolution_bytes(bits, cycle), speed)
    return out


def to_d64(image, progress=False):
    """Decoded sectors (first side) with error bytes."""
    return g64_to_d64(to_g64(image, progress), progress=progress)


def _flux_revolution(cap):
    lo, hi = cap.flux_index[0], cap.flux_index[1]
    pick = (cap.flux >= lo) & (cap.flux < hi)
    positions = np.floor((cap.flux[pick] - lo) * ROTATION_TICKS / (hi - lo))
    return P64Track(np.minimum(positions, ROTATION_TICKS - 1))


def to_p64(image, progress=False):
    """Flux of the first indexed revolution where kept, else the best bit revolution."""
    if isinstance(image.source, P64):
        return image.source
    out = P64()
    for key in tqdm(sorted(image.tracks), desc="p64", unit="trk", disable=not progress):
        caps = image.tracks[key]
        if caps[0].flux_index is not None and len(caps[0].flux_index) > 1:
            out.tracks[key] = _flux_revolution(caps[0])
            continue
        cap, bits, cycle = best_revolution(caps, key)
        if cycle.kind != TrackKind.UNFORMATTED:
            zones = None
            if cap.circular and np.ndim(cap.speed):
                zones = np.repeat(cap.speed, 8)[: len(bits)]
            out.tracks[key] = track_from_bits(bits, zones=zones)
    return out


def _track_name(key):
    half = key & ~SIDE1
    return f"{half // 2}" + (".5" if half % 2 else "")


def info(image, progress=False):
    """Per-track summary of the best capture: kind, cycle, density and sector errors."""
    rows = []
    for key in tqdm(
        sorted(image.tracks), desc="info", unit="trk", disable=not progress
    ):
        cap, bits, cycle = best_revolution(image.tracks[key], key)
        half, side = key & ~SIDE1, int(bool(key & SIDE1))
        errors = None
        if half % 2 == 0 and cycle.kind == TrackKind.FORMATTED and half <= 84:
            track = half // 2 + (SIDE1_TRACK_BASE if side else 0)
            decoded = decode_track(bits, track, sectors=sectors_per_track(half // 2))
            errors = int((decoded.errors != SectorError.OK).sum())
        rows.append(
            {
                "track": _track_name(key),
                "side": side,
                "captures": len(image.tracks[key]),
                "revolutions": cap.revolutions,
                "kind": cycle.kind.name,
                "length": cycle.length,
                "z": round(cycle.z, 1),
                "zone": cap.zone,
                "errors": errors,
            }
        )
    return rows
