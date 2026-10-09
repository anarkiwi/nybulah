"""D71 sector images: two 35-track 1571 sides, optional error info."""

from dataclasses import dataclass

import numpy as np

from ..analysis.sector import SectorError
from .d64 import BAM_ID, BAM_TRACK, D64, track_offsets, write_d64

SIDE_TRACKS = 35
_SIDE = track_offsets(SIDE_TRACKS)
SIDE_SECTORS = int(_SIDE[-1])
D71_TRACKS = 2 * SIDE_TRACKS
D71_SECTORS = 2 * SIDE_SECTORS
_OFFSETS = np.concatenate((_SIDE, _SIDE[1:] + SIDE_SECTORS))


@dataclass
class D71:
    """Sector payloads ``(1366, 256)`` and per-sector D64 error bytes ``(1366,)``."""

    data: np.ndarray
    errors: np.ndarray = None

    tracks = D71_TRACKS

    def __post_init__(self):
        self.data = np.asarray(self.data, dtype=np.uint8).reshape(-1, 256)
        if len(self.data) != D71_SECTORS:
            raise ValueError(f"{len(self.data)} sectors is not a D71 geometry")
        if self.errors is None:
            self.errors = np.full(D71_SECTORS, SectorError.OK, np.uint8)
        self.errors = np.asarray(self.errors, dtype=np.uint8)
        if self.errors.shape != (D71_SECTORS,):
            raise ValueError(f"{self.errors.shape} is not a D71 error table shape")

    @staticmethod
    def side(track):
        """``(side, physical track)`` for D71 ``track`` 1..70."""
        if not 1 <= track <= D71_TRACKS:
            raise ValueError(f"track {track} outside 1..{D71_TRACKS}")
        side, index = divmod(track - 1, SIDE_TRACKS)
        return side, index + 1

    def span(self, track):
        """Slice of the sector rows belonging to ``track`` (1-based)."""
        self.side(track)
        return slice(int(_OFFSETS[track - 1]), int(_OFFSETS[track]))

    @property
    def disk_id(self):
        """Disk ID bytes from the BAM, in BAM order."""
        return bytes(self.data[self.span(BAM_TRACK).start, BAM_ID])

    def sides(self):
        """The two sides as independent 35-track D64 images."""
        return tuple(
            D64(self.data[rows].copy(), self.errors[rows].copy())
            for rows in (slice(0, SIDE_SECTORS), slice(SIDE_SECTORS, None))
        )

    @classmethod
    def from_sides(cls, side0, side1):
        """Join two 35-track D64 images into one D71."""
        if side0.tracks != SIDE_TRACKS or side1.tracks != SIDE_TRACKS:
            raise ValueError("D71 sides must be 35-track D64 images")
        return cls(
            np.concatenate((side0.data, side1.data)),
            np.concatenate((side0.errors, side1.errors)),
        )


def read_d71(buf):
    """Parse a D71 image from bytes."""
    buf = np.frombuffer(bytes(buf), dtype=np.uint8)
    size = D71_SECTORS * 256
    if len(buf) not in (size, size + D71_SECTORS):
        raise ValueError(f"{len(buf)} bytes is not a D71 image size")
    errors = buf[size:].copy() if len(buf) > size else None
    return D71(buf[:size].reshape(D71_SECTORS, 256).copy(), errors)


def write_d71(image, errors=None):
    """Serialise a D71; ``errors`` None appends error info only if any sector failed."""
    return write_d64(image, errors)
