"""G64 (GCR-1541 version 0) raw GCR images."""

from dataclasses import dataclass, field

import numpy as np

G64_SIGNATURE = b"GCR-1541"
G64_HALFTRACKS = 84
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
    """Tracks keyed by halftrack number (2 = track 1, 3 = track 1.5)."""

    tracks: dict = field(default_factory=dict)
    max_track_size: int = G64_MAX_TRACK
    halftracks: int = G64_HALFTRACKS


def _speed_block(zones, size):
    pad = np.zeros(4 * ((size + 3) // 4), np.uint8)
    pad[: len(zones)] = zones
    return (pad.reshape(-1, 4) << _SHIFT2).sum(axis=1, dtype=np.uint8)


def read_g64(buf):
    """Parse a G64 image from bytes."""
    buf = bytes(buf)
    if buf[:8] != G64_SIGNATURE:
        raise ValueError("not a G64 image")
    count = buf[9]
    max_size = int.from_bytes(buf[10:12], "little")
    offsets = np.frombuffer(buf, "<u4", count, _HEADER)
    speeds = np.frombuffer(buf, "<u4", count, _HEADER + 4 * count)
    raw = np.frombuffer(buf, np.uint8)
    image = G64({}, max_size, count)
    for entry in np.flatnonzero(offsets):
        pos = int(offsets[entry])
        length = int.from_bytes(buf[pos : pos + 2], "little")
        speed = int(speeds[entry])
        if speed > 3:
            packed = raw[speed : speed + (length + 3) // 4]
            speed = ((packed[:, None] >> _SHIFT2) & 3).ravel()[:length]
        image.tracks[int(entry) + FIRST_HALFTRACK] = G64Track(
            raw[pos + 2 : pos + 2 + length].copy(), speed
        )
    return image


def write_g64(image):
    """Serialise a G64 image; tracks are padded to the maximum track size."""
    size = max([image.max_track_size] + [len(t.data) for t in image.tracks.values()])
    count = image.halftracks
    offsets = np.zeros(count, "<u4")
    speeds = np.zeros(count, "<u4")
    pos = _HEADER + 8 * count
    blocks, maps = [], []
    for halftrack in sorted(image.tracks):
        track = image.tracks[halftrack]
        entry = halftrack - FIRST_HALFTRACK
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
    header = G64_SIGNATURE + bytes([0, count]) + size.to_bytes(2, "little")
    return b"".join(
        [header, offsets.tobytes(), speeds.tobytes()] + [b.tobytes() for b in blocks]
    )
