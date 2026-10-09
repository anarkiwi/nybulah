"""NIB and NB2 (MNIB-1541-RAW) raw drive captures.

A 0x100 byte header holds the signature, version and halftrack flag, then
(halftrack, density) pairs from 0x10; NIB stores one 0x2000 byte capture per
entry, NB2 sixteen (four passes at each of the four densities).
"""

from dataclasses import dataclass, field

import numpy as np

NIB_SIGNATURE = b"MNIB-1541-RAW"
NIB_HEADER = 0x100
NIB_TABLE = 0x10
NIB_TRACK = 0x2000
NIB_VERSION = 3
NB2_VERSION = 2
NB2_PASSES = 4
DENSITIES = 4
DENSITY_MASK = 0x03
BM_MATCH = 0x10
BM_NO_CYCLE = 0x20
BM_NO_SYNC = 0x40
BM_FF_TRACK = 0x80


@dataclass
class NibEntry:
    """A captured halftrack: header density byte (with flags) and capture data.

    ``data`` is ``(0x2000,)`` for NIB or ``(densities, passes, 0x2000)`` for NB2.
    """

    halftrack: int
    density: int
    data: np.ndarray

    @property
    def zone(self):
        """Density zone (0-3) the halftrack was read at."""
        return self.density & DENSITY_MASK


@dataclass
class Nib:
    """A NIB (``passes`` None) or NB2 capture set."""

    entries: list = field(default_factory=list)
    version: int = NIB_VERSION
    halftracks: bool = False
    passes: int = None

    @property
    def shape(self):
        """Capture array shape of one entry."""
        if self.passes is None:
            return (NIB_TRACK,)
        return (DENSITIES, self.passes, NIB_TRACK)


def read_nib(buf, nb2=False):
    """Parse a NIB (or with ``nb2`` an NB2) image from bytes."""
    buf = bytes(buf)
    if buf[: len(NIB_SIGNATURE)] != NIB_SIGNATURE:
        raise ValueError("not an MNIB-1541-RAW image")
    image = Nib([], buf[13], bool(buf[15]), NB2_PASSES if nb2 else None)
    table = np.frombuffer(buf, np.uint8, NIB_HEADER - NIB_TABLE, NIB_TABLE)
    table = table.reshape(-1, 2)
    table = table[: np.argmin(np.append(table[:, 0], 0) != 0)]
    size = int(np.prod(image.shape))
    if len(buf) < NIB_HEADER + len(table) * size:
        raise ValueError("truncated MNIB-1541-RAW image")
    data = np.frombuffer(buf, np.uint8, len(table) * size, NIB_HEADER)
    data = data.reshape(len(table), *image.shape)
    image.entries = [
        NibEntry(int(h), int(d), data[i].copy()) for i, (h, d) in enumerate(table)
    ]
    return image


def write_nib(image):
    """Serialise a NIB/NB2 image."""
    header = np.zeros(NIB_HEADER, np.uint8)
    header[: len(NIB_SIGNATURE)] = np.frombuffer(NIB_SIGNATURE, np.uint8)
    header[13] = image.version
    header[15] = image.halftracks
    table = [(e.halftrack, e.density) for e in image.entries]
    header[NIB_TABLE : NIB_TABLE + 2 * len(table)] = np.ravel(table)
    return b"".join(
        [header.tobytes()]
        + [
            np.asarray(e.data, np.uint8).reshape(image.shape).tobytes()
            for e in image.entries
        ]
    )
