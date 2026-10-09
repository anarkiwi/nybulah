"""D81 sector images of the 1581 (80 tracks x 40 sectors, optional error bytes).

Header 40/0 and BAM 40/1-2 follow the DOS 318045-01 sources newdsk.src (header)
and mapit.src ``newmap`` (BAM: six bytes per track from offset ``bindx`` = 16).
"""

from dataclasses import dataclass

import numpy as np
from tqdm import tqdm

from ..analysis import mfm
from ..analysis.sector import SectorError

TRACKS = mfm.CYLINDERS
SECTORS = mfm.LOGICAL_SECTORS
D81_SECTORS = TRACKS * SECTORS
D81_BYTES = D81_SECTORS * mfm.LOGICAL_BYTES
DIR_TRACK = 40
BAM_INDEX = 16
BAM_ENTRY = 6
BAM_TRACKS = 40
_SEVERITY = (
    SectorError.NO_SYNC,
    SectorError.HEADER_NOT_FOUND,
    SectorError.HEADER_CHECKSUM,
    SectorError.DATA_NOT_FOUND,
    SectorError.DATA_CHECKSUM,
    SectorError.OK,
)


def _text(raw):
    return bytes(raw).rstrip(b"\xa0").decode("latin-1")


@dataclass
class D81:
    """Sector payloads ``(3200, 256)`` and per-sector D64-style error bytes."""

    data: np.ndarray
    errors: np.ndarray = None

    def __post_init__(self):
        self.data = np.asarray(self.data, np.uint8).reshape(-1, mfm.LOGICAL_BYTES)
        if len(self.data) != D81_SECTORS:
            raise ValueError(f"{len(self.data)} sectors is not a D81 geometry")
        if self.errors is None:
            self.errors = np.full(D81_SECTORS, SectorError.OK, np.uint8)
        self.errors = np.asarray(self.errors, np.uint8)
        if self.errors.shape != (D81_SECTORS,):
            raise ValueError(f"{self.errors.shape} is not a D81 error table shape")

    @staticmethod
    def index(track, sector):
        """Row of logical ``track`` 1..80, ``sector`` 0..39."""
        mfm.physical(track, sector)
        return (np.asarray(track) - 1) * SECTORS + np.asarray(sector)

    def sector(self, track, sector):
        """Payload of one logical sector."""
        return self.data[self.index(track, sector)]

    def header(self):
        """Disk name, ID, DOS version and format byte of the header block 40/0."""
        h = self.sector(DIR_TRACK, 0)
        return {
            "name": _text(h[4:20]),
            "id": _text(h[22:24]),
            "dos": chr(h[25]),
            "format": chr(h[2]),
            "directory": (int(h[0]), int(h[1])),
        }

    def bam(self):
        """``(free (80,), bitmap (80, 40) bool)`` from BAM blocks 40/1 and 40/2;
        a set bit marks a free sector."""
        rows = np.stack([self.sector(DIR_TRACK, s) for s in (1, 2)])
        entries = rows[:, BAM_INDEX:].reshape(2, -1, BAM_ENTRY)[:, :BAM_TRACKS]
        entries = entries.reshape(TRACKS, BAM_ENTRY)
        bits = np.unpackbits(entries[:, 1:], axis=1, bitorder="little")
        return entries[:, 0].astype(np.int64), bits[:, :SECTORS].astype(bool)

    def info(self):
        """Header fields, blocks free (directory track excluded) and BAM mismatches."""
        free, bitmap = self.bam()
        out = self.header()
        out["blocks_free"] = int(np.delete(free, DIR_TRACK - 1).sum())
        out["bam_mismatch"] = (np.flatnonzero(free != bitmap.sum(axis=1)) + 1).tolist()
        out["errors"] = int((self.errors != SectorError.OK).sum())
        return out


def read_d81(buf):
    """Parse a D81 image from bytes (819200, or 822400 with error bytes)."""
    buf = np.frombuffer(bytes(buf), np.uint8)
    if len(buf) not in (D81_BYTES, D81_BYTES + D81_SECTORS):
        raise ValueError(f"{len(buf)} bytes is not a D81 image size")
    errors = buf[D81_BYTES:].copy() if len(buf) > D81_BYTES else None
    return D81(buf[:D81_BYTES].reshape(D81_SECTORS, -1).copy(), errors)


def write_d81(image, errors=None):
    """Serialise a D81; ``errors`` None appends error bytes only if a sector failed."""
    if errors is None:
        errors = bool((image.errors != SectorError.OK).any())
    return image.data.tobytes() + (image.errors.tobytes() if errors else b"")


def side_rows(cylinder, side):
    """D81 rows of the logical sectors on one side of a cylinder, in R/half order."""
    r, half = np.divmod(np.arange(mfm.HALF_SECTORS), 2)
    return D81.index(*mfm.logical(cylinder, side, r + mfm.FIRST_SECTOR, half))


def pair_errors(errors):
    """One error per physical sector from its two halves: the more severe one."""
    unknown = np.setdiff1d(errors, _SEVERITY)
    if len(unknown):
        raise ValueError(f"error bytes {unknown.tolist()} have no MFM form")
    rank = np.full(256, len(_SEVERITY), np.int64)
    rank[list(_SEVERITY)] = np.arange(len(_SEVERITY))
    pairs = np.asarray(errors, np.uint8).reshape(-1, 2)
    return pairs[np.arange(len(pairs)), rank[pairs].argmin(axis=1)]


def to_tracks(image, progress=False):
    """Standard 1581 media ``{(cylinder, head): (data, mark)}`` of a D81; error
    bytes per physical sector as :func:`mfm.standard_layout` writes them."""
    out = {}
    for cyl in tqdm(range(TRACKS), desc="d81->mfm", unit="cyl", disable=not progress):
        for side in (0, 1):
            rows = side_rows(cyl, side)
            data = image.data[rows].reshape(mfm.SECTORS, -1)
            errors = pair_errors(image.errors[rows])
            specs = mfm.standard_layout(cyl, side, data, errors)
            out[(cyl, 1 - side)] = mfm.encode_track(specs)
    return out


def from_decodes(decodes, progress=False):
    """D81 of decoded tracks ``{(cylinder, head): [MfmTrack, ...]}``: per side the
    best read of each physical sector; absent tracks are header-not-found."""
    data = np.zeros((D81_SECTORS, mfm.LOGICAL_BYTES), np.uint8)
    errors = np.full(D81_SECTORS, SectorError.HEADER_NOT_FOUND, np.uint8)
    keys = sorted(decodes)
    for cyl, head in tqdm(keys, desc="mfm->d81", unit="trk", disable=not progress):
        if not 0 <= cyl < TRACKS:
            continue
        side = mfm.head_side(head)
        rows = side_rows(cyl, side)
        data[rows], errors[rows] = mfm.best_sectors(decodes[(cyl, head)], cyl, side)
    return D81(data, errors)
