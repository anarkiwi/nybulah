"""D64 sector images (35, 40 or 42 tracks, optional error info)."""

from dataclasses import dataclass

import numpy as np

from ..analysis.gcr import sectors_per_track
from ..analysis.sector import SectorError

D64_TRACKS = (35, 40, 42)
BAM_TRACK = 18
BAM_ID = slice(0xA2, 0xA4)


def track_offsets(tracks):
    """First sector index of each track 1..tracks, plus the total at the end."""
    counts = [sectors_per_track(t) for t in range(1, tracks + 1)]
    return np.concatenate(([0], np.cumsum(counts)))


@dataclass
class D64:
    """Sector payloads ``(n, 256)`` and per-sector D64 error bytes ``(n,)``."""

    data: np.ndarray
    errors: np.ndarray = None

    def __post_init__(self):
        self.data = np.asarray(self.data, dtype=np.uint8).reshape(-1, 256)
        tracks = {int(track_offsets(t)[-1]): t for t in D64_TRACKS}
        if len(self.data) not in tracks:
            raise ValueError(f"{len(self.data)} sectors is not a D64 geometry")
        self.tracks = tracks[len(self.data)]
        self._offsets = track_offsets(self.tracks)
        if self.errors is None:
            self.errors = np.full(len(self.data), SectorError.OK, np.uint8)
        self.errors = np.asarray(self.errors, dtype=np.uint8)

    def span(self, track):
        """Slice of the sector rows belonging to ``track`` (1-based)."""
        return slice(self._offsets[track - 1], self._offsets[track])

    @property
    def disk_id(self):
        """Disk ID bytes from the BAM, in BAM order."""
        return bytes(self.data[self.span(BAM_TRACK).start, BAM_ID])


def read_d64(buf):
    """Parse a D64 image from bytes."""
    buf = np.frombuffer(bytes(buf), dtype=np.uint8)
    for tracks in D64_TRACKS:
        n = int(track_offsets(tracks)[-1])
        if len(buf) == n * 256:
            return D64(buf.reshape(n, 256).copy())
        if len(buf) == n * 257:
            return D64(buf[: n * 256].reshape(n, 256).copy(), buf[n * 256 :].copy())
    raise ValueError(f"{len(buf)} bytes is not a D64 image size")


def write_d64(image, errors=None):
    """Serialise a D64; ``errors`` None appends error info only if any sector failed."""
    if errors is None:
        errors = bool((image.errors != SectorError.OK).any())
    parts = [image.data.tobytes()]
    if errors:
        parts.append(image.errors.tobytes())
    return b"".join(parts)
