"""G64 (GCR-1541) and G71 (GCR-1571) version 0 raw GCR images, with the SPS EXT block."""

from dataclasses import dataclass, field

import numpy as np

G64_SIGNATURE = b"GCR-1541"
G71_SIGNATURE = b"GCR-1571"
G64_HALFTRACKS = 84
SIDE1 = 0x80
EXT_TAG = b"EXT\x01"
EXT_DTYPE = np.dtype(
    [
        ("splice", "<u4"),
        ("write_area", "<u4"),
        ("bitcell_ns", "<u4"),
        ("fill", "u1"),
        ("reserved", "u1"),
        ("format", "u1"),
        ("extension", "u1"),
    ]
)
G64_MAX_TRACK = 7928
FIRST_HALFTRACK = 2
_HEADER = 12
_SHIFT2 = np.array([6, 4, 2, 0], np.uint8)


@dataclass
class G64Track:
    """GCR bytes of one track; ``speed`` is a zone 0-3 or a per-byte zone array."""

    data: np.ndarray
    speed: object = 3


@dataclass
class G64:
    """Tracks keyed by halftrack number (2 = track 1, 3 = track 1.5).

    Second-side (G71) halftracks carry the ``SIDE1`` flag. ``ext`` holds the
    per-entry SPS mastering records (``EXT_DTYPE``) when present.
    """

    tracks: dict = field(default_factory=dict)
    max_track_size: int = G64_MAX_TRACK
    halftracks: int = G64_HALFTRACKS
    ext: np.ndarray = None


def halftrack_key(entry):
    """Track key of a G64/G71 table entry."""
    side, index = divmod(int(entry), G64_HALFTRACKS)
    return (index + FIRST_HALFTRACK) | (SIDE1 if side else 0)


def table_entry(key):
    """G64/G71 table entry of a track key."""
    return (key & ~SIDE1) - FIRST_HALFTRACK + (G64_HALFTRACKS if key & SIDE1 else 0)


def _speed_block(zones, size):
    pad = np.zeros(4 * ((size + 3) // 4), np.uint8)
    pad[: len(zones)] = zones
    return (pad.reshape(-1, 4) << _SHIFT2).sum(axis=1, dtype=np.uint8)


def read_g64(buf):
    """Parse a G64 image from bytes."""
    buf = bytes(buf)
    if buf[:8] not in (G64_SIGNATURE, G71_SIGNATURE):
        raise ValueError("not a G64/G71 image")
    count = buf[9]
    max_size = int.from_bytes(buf[10:12], "little")
    table = _HEADER + 8 * count
    if len(buf) < table:
        raise ValueError("truncated G64 header")
    offsets = np.frombuffer(buf, "<u4", count, _HEADER)
    speeds = np.frombuffer(buf, "<u4", count, _HEADER + 4 * count)
    raw = np.frombuffer(buf, np.uint8)
    image = G64({}, max_size, count)
    if buf[table : table + 4] == EXT_TAG and len(buf) >= table + 4 + 16 * count:
        image.ext = np.frombuffer(buf, EXT_DTYPE, count, table + 4).copy()
    for entry in np.flatnonzero(offsets):
        pos = int(offsets[entry])
        length = int.from_bytes(buf[pos : pos + 2], "little")
        speed = int(speeds[entry])
        if pos + 2 + length > len(buf) or speed + (length + 3) // 4 > len(buf):
            raise ValueError(f"G64 entry {entry} lies outside the image")
        if speed > 3:
            packed = raw[speed : speed + (length + 3) // 4]
            speed = ((packed[:, None] >> _SHIFT2) & 3).ravel()[:length]
        image.tracks[halftrack_key(entry)] = G64Track(
            raw[pos + 2 : pos + 2 + length].copy(), speed
        )
    return image


def _ext_block(ext, count):
    out = np.zeros(count, EXT_DTYPE)
    out[: min(count, len(ext))] = ext[:count]
    return np.frombuffer(EXT_TAG + out.tobytes(), np.uint8)


def write_g64(image):
    """Serialise a G64, or a G71 when there are second-side entries.

    Tracks are padded to the maximum track size.
    """
    size = max([image.max_track_size] + [len(t.data) for t in image.tracks.values()])
    double = any(key & SIDE1 for key in image.tracks)
    count = max(image.halftracks, 2 * G64_HALFTRACKS if double else 0)
    offsets = np.zeros(count, "<u4")
    speeds = np.zeros(count, "<u4")
    blocks = [] if image.ext is None else [_ext_block(image.ext, count)]
    pos = _HEADER + 8 * count + sum(len(b) for b in blocks)
    maps = []
    for halftrack in sorted(image.tracks, key=table_entry):
        track = image.tracks[halftrack]
        entry = table_entry(halftrack)
        block = np.zeros(size + 2, np.uint8)
        block[:2] = np.frombuffer(len(track.data).to_bytes(2, "little"), np.uint8)
        block[2 : 2 + len(track.data)] = track.data
        offsets[entry] = pos
        pos += len(block)
        blocks.append(block)
        if np.ndim(track.speed):
            maps.append((entry, _speed_block(track.speed, size)))
        else:
            speeds[entry] = track.speed
    for entry, block in maps:
        speeds[entry] = pos
        pos += len(block)
        blocks.append(block)
    signature = G71_SIGNATURE if count > G64_HALFTRACKS else G64_SIGNATURE
    header = signature + bytes([0, count]) + size.to_bytes(2, "little")
    return b"".join(
        [header, offsets.tobytes(), speeds.tobytes()] + [b.tobytes() for b in blocks]
    )
