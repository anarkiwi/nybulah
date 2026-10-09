"""1541 sector header/data blocks, standard track formatting and track decoding."""

from dataclasses import dataclass
from enum import IntEnum

import numpy as np

from .gcr import (
    decode_bits,
    encode,
    runs_of_ones,
    sectors_per_track,
    speed_zone,
    track_capacity,
)

HEADER_ID = 0x08
DATA_ID = 0x07
SYNC_BYTES = 5
HEADER_GAP_BYTES = 9
GAP_BYTE = 0x55
HEADER_GCR_BYTES = 10
DATA_GCR_BYTES = 325
SECTOR_BYTES = 2 * SYNC_BYTES + HEADER_GCR_BYTES + HEADER_GAP_BYTES + DATA_GCR_BYTES


class SectorError(IntEnum):
    """D64 error-info byte values."""

    OK = 0x01
    HEADER_NOT_FOUND = 0x02
    NO_SYNC = 0x03
    DATA_NOT_FOUND = 0x04
    DATA_CHECKSUM = 0x05
    BAD_GCR = 0x06
    WRITE_VERIFY = 0x07
    WRITE_PROTECT = 0x08
    HEADER_CHECKSUM = 0x09
    LONG_DATA = 0x0A
    ID_MISMATCH = 0x0B
    NOT_READY = 0x0F

    @property
    def dos_code(self):
        """The CBM DOS error number reported for this condition."""
        return _DOS_CODES[self]


_DOS_CODES = {
    SectorError.OK: 0,
    SectorError.HEADER_NOT_FOUND: 20,
    SectorError.NO_SYNC: 21,
    SectorError.DATA_NOT_FOUND: 22,
    SectorError.DATA_CHECKSUM: 23,
    SectorError.BAD_GCR: 24,
    SectorError.WRITE_VERIFY: 25,
    SectorError.WRITE_PROTECT: 26,
    SectorError.HEADER_CHECKSUM: 27,
    SectorError.LONG_DATA: 28,
    SectorError.ID_MISMATCH: 29,
    SectorError.NOT_READY: 74,
}

# When several headers claim one sector, the read that progressed furthest wins.
_PREFERENCE = (
    SectorError.OK,
    SectorError.DATA_CHECKSUM,
    SectorError.BAD_GCR,
    SectorError.DATA_NOT_FOUND,
    SectorError.ID_MISMATCH,
    SectorError.HEADER_CHECKSUM,
)
_RANK = np.full(256, len(_PREFERENCE), np.int64)
_RANK[list(_PREFERENCE)] = np.arange(len(_PREFERENCE))
_NONE = np.iinfo(np.int64).max


def header_blocks(track, sectors, disk_id):
    """Raw 8-byte header blocks ``(n, 8)``; ``disk_id`` is in BAM order."""
    sectors = np.asarray(sectors, dtype=np.uint8)
    out = np.empty((len(sectors), 8), np.uint8)
    out[:, 0] = HEADER_ID
    out[:, 2] = sectors
    out[:, 3] = track
    out[:, 4] = disk_id[1]
    out[:, 5] = disk_id[0]
    out[:, 6:] = 0x0F
    out[:, 1] = np.bitwise_xor.reduce(out[:, 2:6], axis=1)
    return out


def data_blocks(data):
    """Raw 260-byte data blocks ``(n, 260)`` for sector payloads ``(n, 256)``."""
    data = np.asarray(data, dtype=np.uint8).reshape(-1, 256)
    out = np.zeros((len(data), 260), np.uint8)
    out[:, 0] = DATA_ID
    out[:, 1:257] = data
    out[:, 257] = np.bitwise_xor.reduce(data, axis=1)
    return out


def format_track(track, data, disk_id, errors=None, capacity=None):
    """GCR bytes of a DOS-formatted track holding ``data`` ``(n, 256)``.

    ``errors`` holds optional D64 error-info bytes reproduced on the track
    (NO_SYNC strips every sync of the track). ``capacity`` defaults to one
    revolution at 300 rpm; the inter-sector gap shares out the free space.
    """
    data = np.asarray(data, dtype=np.uint8).reshape(-1, 256)
    n = len(data)
    if capacity is None:
        capacity = track_capacity(speed_zone(track))
    gap = (capacity - n * SECTOR_BYTES) // n
    if gap < 0:
        raise ValueError(f"{n} sectors do not fit in {capacity} bytes")
    errors = np.full(n, SectorError.OK, np.uint8) if errors is None else errors
    errors = np.asarray(errors, dtype=np.uint8)
    hdr = header_blocks(track, np.arange(n), disk_id)
    blk = data_blocks(data)
    bad_id = errors == SectorError.ID_MISMATCH
    hdr[bad_id, 4:6] ^= 0xFF
    hdr[errors == SectorError.HEADER_NOT_FOUND, 0] ^= 0xFF
    hdr[errors == SectorError.HEADER_CHECKSUM, 1] ^= 0xFF
    blk[errors == SectorError.DATA_NOT_FOUND, 0] ^= 0xFF
    blk[errors == SectorError.DATA_CHECKSUM, 257] ^= 0xFF
    hdr_gcr = encode(hdr).reshape(n, HEADER_GCR_BYTES)
    blk_gcr = encode(blk).reshape(n, DATA_GCR_BYTES)
    blk_gcr[errors == SectorError.BAD_GCR, 5:10] = 0
    sync = GAP_BYTE if (errors == SectorError.NO_SYNC).any() else 0xFF

    def fill(width, value):
        return np.full((n, width), value, np.uint8)

    body = np.hstack(
        (
            fill(SYNC_BYTES, sync),
            hdr_gcr,
            fill(HEADER_GAP_BYTES, GAP_BYTE),
            fill(SYNC_BYTES, sync),
            blk_gcr,
            fill(gap, GAP_BYTE),
        )
    ).ravel()
    return np.concatenate((body, np.full(capacity - len(body), GAP_BYTE, np.uint8)))


@dataclass
class TrackDecode:
    """Sectors recovered from one track.

    ``data`` is ``(n, 256)``, ``errors`` holds D64 error-info bytes, ``ids``
    the header disk ID per sector (BAM order) and ``offsets`` the bit position
    of each sector's header (sync end), -1 when absent.
    """

    data: np.ndarray
    errors: np.ndarray
    ids: np.ndarray
    offsets: np.ndarray


def _windows(bits, starts, width):
    return bits[(starts[:, None] + np.arange(width)) % len(bits)]


def _read_errors(hdr, hvalid, blk, bvalid, blk_is_hdr, disk_id):
    """Error reading each header's sector, with ``blk`` the block after it."""
    err = np.full(len(hdr), SectorError.OK, np.uint8)
    err[blk[:, 257] != np.bitwise_xor.reduce(blk[:, 1:257], axis=1)] = (
        SectorError.DATA_CHECKSUM
    )
    err[~bvalid.all(axis=1)] = SectorError.BAD_GCR
    missing = blk_is_hdr | (blk[:, 0] != DATA_ID) | ~bvalid[:, 0]
    err[missing] = SectorError.DATA_NOT_FOUND
    if disk_id is not None:
        wrong = (hdr[:, 5] != disk_id[0]) | (hdr[:, 4] != disk_id[1])
        err[wrong] = SectorError.ID_MISMATCH
    err[np.bitwise_xor.reduce(hdr[:, 1:6], axis=1) != 0] = SectorError.HEADER_CHECKSUM
    err[~hvalid[:, :6].all(axis=1)] = SectorError.BAD_GCR
    return err


def _read_blocks(bits, ends, track, sectors, disk_id):
    """Decode the header and following block at every sync end.

    Returns ``(headers, blocks, errors)``; syncs that do not start a header
    of a sector of this track get error 0.
    """
    hdr, hvalid = decode_bits(_windows(bits, ends, 8 * HEADER_GCR_BYTES))
    blk, bvalid = decode_bits(_windows(bits, ends, 8 * DATA_GCR_BYTES))
    is_hdr = (hdr[:, 0] == HEADER_ID) & hvalid[:, 0]
    nxt = np.roll(np.arange(len(ends)), -1)
    err = _read_errors(hdr, hvalid, blk[nxt], bvalid[nxt], is_hdr[nxt], disk_id)
    ours = is_hdr & hvalid[:, 2] & hvalid[:, 3] & (hdr[:, 3] == track)
    err[~(ours & (hdr[:, 2] < sectors))] = 0
    return hdr, blk[nxt], err


def _best_per_sector(sector, err, n):
    """Sectors with a header and, for each, the index of its best header."""
    cand = np.flatnonzero(err)
    key = np.full(n, _NONE, np.int64)
    np.minimum.at(key, sector[cand], _RANK[err[cand]] * len(err) + cand)
    found = np.flatnonzero(key != _NONE)
    return found, key[found] % len(err)


def decode_track(bits, track, disk_id=None, sectors=None):
    """Decode the sectors of a circular track bit stream.

    ``disk_id`` (BAM order) enables ID-mismatch (29) detection.
    """
    bits = np.asarray(bits, dtype=np.uint8)
    n = sectors_per_track(track) if sectors is None else sectors
    out = TrackDecode(
        np.zeros((n, 256), np.uint8),
        np.full(n, SectorError.HEADER_NOT_FOUND, np.uint8),
        np.zeros((n, 2), np.uint8),
        np.full(n, -1, np.int64),
    )
    starts, lengths = runs_of_ones(bits, circular=True)
    if len(starts) == 0 or lengths[0] >= len(bits):
        out.errors[:] = SectorError.NO_SYNC
        return out
    ends = (starts + lengths) % len(bits)
    hdr, blk, err = _read_blocks(bits, ends, track, n, disk_id)
    found, best = _best_per_sector(hdr[:, 2], err, n)
    out.errors[found] = err[best]
    out.ids[found] = hdr[best][:, [5, 4]]
    out.offsets[found] = ends[best]
    readable = _RANK[out.errors[found]] < _RANK[SectorError.DATA_NOT_FOUND]
    out.data[found[readable]] = blk[best[readable], 1:257]
    return out


def merge_decodes(a, b):
    """Per sector, the better of two decodes of the same track (a wins ties)."""
    take = _RANK[b.errors] < _RANK[a.errors]
    return TrackDecode(
        *(
            np.where(take.reshape(-1, *[1] * (x.ndim - 1)), y, x)
            for x, y in (
                (a.data, b.data),
                (a.errors, b.errors),
                (a.ids, b.ids),
                (a.offsets, b.offsets),
            )
        )
    )


def header_tracks(bits):
    """Track numbers in the checksum-valid sector headers of a circular bit stream."""
    bits = np.asarray(bits, dtype=np.uint8)
    starts, lengths = runs_of_ones(bits, circular=True)
    if len(starts) == 0 or lengths[0] >= len(bits):
        return np.zeros(0, np.uint8)
    hdr, valid = decode_bits(_windows(bits, (starts + lengths) % len(bits), 80))
    ok = (hdr[:, 0] == HEADER_ID) & valid[:, :6].all(axis=1)
    ok &= np.bitwise_xor.reduce(hdr[:, 1:6], axis=1) == 0
    return hdr[ok, 3]
