"""1581 MFM tracks as the WD177x reads and writes them.

Sources: WD1772 datasheet (J.L. Guerin edit v1.3: sector length table, Type II/III
commands, status register, MFM format); 1581 DOS 318045-01 (mrout.src ``fmtrk``,
dskint.src ``psetdef``, burstc.src ``nsecks``, msub.src ``trans_ts``/``wdstatus``).
"""

from dataclasses import dataclass, field
from enum import IntFlag

import numpy as np
from numba import njit

from .crc import SYNC3, TABLE, crc16
from .sector import SectorError

A1, C2 = 0xA1, 0xC2
WRITE_A1, WRITE_C2, WRITE_CRC = 0xF5, 0xF6, 0xF7
WRITE_CODES = (WRITE_A1, WRITE_C2, WRITE_CRC)
IDAM, INDEX_AM = 0xFE, 0xFC
DAM, DELETED_DAM = 0xFB, 0xF8
GAP_BYTE = 0x4E
LOGIC_ONES = 0xFF
SYNC_ZEROS = 12
SYNC_MARKS = 3
READ_SYNC = SYNC_MARKS - 1
ID_BYTES = 7
DAM_WINDOW = 43
SPLICE_SLIP = 1

ST_BUSY, ST_DRQ, ST_LOST, ST_CRC, ST_RNF, ST_DELETED = 1, 2, 4, 8, 16, 32

LEAD = 32
GAP2 = 22
GAP3 = 35
SECTORS = 10
FIRST_SECTOR = 1
SIZE_CODE = 2
SECTOR_BYTES = 128 << SIZE_CODE
BIT_RATE = 250_000
RPM = 300
TRACK_BYTES = BIT_RATE * 60 // RPM // 8
BYTE_US = 8 * 1_000_000 // BIT_RATE
CYLINDERS = 80
LOGICAL_SECTORS = 40
HALF_SECTORS = LOGICAL_SECTORS // 2
LOGICAL_BYTES = 256

GAP_LEAD = LEAD + SYNC_ZEROS
GAP_AFTER_ID = GAP2 + SYNC_ZEROS
GAP_AFTER_DATA = GAP3 + SYNC_ZEROS

_ID, _DATA, _INDEX = 1, 2, 3


class Flag(IntFlag):
    """Sector record flags beside the error byte."""

    DELETED = 1
    ODD_SIZE = 2
    DUPLICATE = 4
    FOREIGN = 8
    TRUNCATED = 16
    GAP = 32


SECTOR_DTYPE = np.dtype(
    [
        ("id_pos", "<i8"),
        ("c", "u1"),
        ("h", "u1"),
        ("r", "u1"),
        ("n", "u1"),
        ("id_ok", "?"),
        ("data_pos", "<i8"),
        ("dam", "u1"),
        ("size", "<i4"),
        ("data_ok", "?"),
        ("gap", "<i4"),
        ("gap_std", "<i4"),
        ("split", "<i4"),
        ("error", "u1"),
        ("flags", "u1"),
        ("time_us", "<f8"),
    ]
)


def sector_size(n):
    """Data bytes for size code ``n``; only its low two bits count (datasheet table)."""
    return 128 << (np.asarray(n, np.int64) & 3)


def size_code(size):
    """Size code of a 128/256/512/1024-byte sector."""
    code = int(size).bit_length() - 8
    if code not in range(4) or 128 << code != size:
        raise ValueError(f"{size} is not a WD sector size")
    return code


@dataclass
class MfmTrack:
    """Sector records of one revolution and the buffer their positions index.

    A data field occupies ``buf[data_pos]`` (the mark) to ``data_pos + 3 + size``;
    ``n`` is the revolution length in bytes, ``index_marks`` the C2 mark positions.
    """

    sectors: np.ndarray
    buf: np.ndarray
    n: int
    index_marks: np.ndarray = field(default_factory=lambda: np.zeros(0, np.int64))

    def payload(self, i):
        """Data bytes of record ``i`` (empty without a data field)."""
        rec = self.sectors[i]
        if rec["data_pos"] < 0:
            return np.zeros(0, np.uint8)
        start = int(rec["data_pos"]) + 1
        return self.buf[start : start + int(rec["size"])]


def _run(sync, depth, circular):
    """Length, up to ``depth``, of the run of True ending just before each position."""
    run = np.zeros(len(sync), np.int64)
    alive = np.ones(len(sync), bool)
    for k in range(1, depth + 1):
        alive &= np.roll(sync, k)
        if not circular:
            alive[:k] = False
        run += alive
    return run


def find_marks(data, mark=None, circular=False):
    """``(pos, cls)`` of the address marks: the byte after a run of sync bytes.

    With ``mark`` (media missing-clock flags) runs are exact. Read Track output
    resynchronises on each A1*/C2*, so the first sync byte may be misframed and
    :data:`READ_SYNC` A1 (C2) bytes before an ID/data mark ($FC) suffice.
    """
    data = np.asarray(data, np.uint8)
    if mark is not None:
        sync = np.asarray(mark, bool)
        ok = (_run(sync, 1, circular) > 0) & ~sync
        prev = np.roll(data, 1)
        a1, c2 = ok & (prev == A1), ok & (prev == C2)
    else:
        a1 = (_run(data == A1, READ_SYNC, circular) == READ_SYNC) & (data != A1)
        c2 = (_run(data == C2, READ_SYNC, circular) == READ_SYNC) & (data != C2)
    cls = np.select(
        [
            a1 & (data >= 0xFC),
            a1 & (data >= 0xF8) & (data <= 0xFB),
            c2 & (data == INDEX_AM),
        ],
        [_ID, _DATA, _INDEX],
        0,
    )
    pos = np.flatnonzero(cls)
    return pos, cls[pos]


@njit(cache=True)
def _accept(buf, pos, cls, orphan_n):  # pragma: no cover
    """Greedy WD field acceptance: marks inside an accepted field are data bytes;
    a data mark within :data:`DAM_WINDOW` bytes of an ID's CRC belongs to it."""
    m = len(pos)
    ids = np.full(m, -1, np.int64)
    dats = np.full(m, -1, np.int64)
    count, busy, pending = 0, 0, -1
    for j in range(m):
        p = pos[j]
        if p < busy:
            continue
        if cls[j] == _ID:
            ids[count] = p
            pending = count
            count += 1
            busy = p + ID_BYTES
        elif cls[j] == _DATA:
            n = orphan_n
            if pending >= 0 and p - (ids[pending] + ID_BYTES) < DAM_WINDOW:
                rec = pending
                n = buf[ids[pending] + 4]
            else:
                rec = count
                count += 1
            dats[rec] = p
            busy = p + 3 + (128 << (n & 3))
            pending = -1
        else:
            busy = p + 1
    return ids[:count], dats[:count]


def _crc_ok(buf, start, length, valid):
    """Whether each field's stored CRC matches the CRC over its mark and body."""
    out = np.zeros(len(start), bool)
    for i in np.flatnonzero(valid):
        s, k = int(start[i]), int(length[i])
        stored = int(buf[s + k]) << 8 | int(buf[s + k + 1])
        out[i] = crc16(buf[s : s + k], SYNC3) == stored
    return out


def _gaps(rec, has_id, has_data, circular, n):
    """Set the gap before each record's first field, its 1581 value and the ID to
    data split; returns which differ by more than a write splice slip."""
    first = np.where(has_id, rec["id_pos"], rec["data_pos"])
    end = np.where(
        has_data, rec["data_pos"] + 3 + rec["size"], rec["id_pos"] + ID_BYTES
    )
    prev = np.roll(end, 1)
    std = np.where(np.roll(has_data, 1), GAP_AFTER_DATA, GAP_AFTER_ID)
    if len(rec):
        prev[0] = prev[0] - n if circular else 0
        std[0] = -1 if circular else GAP_LEAD
    rec["gap"] = first - SYNC_MARKS - prev
    rec["gap_std"] = std
    both = has_id & has_data
    split = rec["data_pos"] - SYNC_MARKS - rec["id_pos"] - ID_BYTES
    rec["split"] = np.where(both, split, -1)
    off = (std >= 0) & (np.abs(rec["gap"] - std) > SPLICE_SLIP)
    return off | (both & (np.abs(rec["split"] - GAP_AFTER_ID) > SPLICE_SLIP))


def _errors(rec, has_id, has_data):
    """D64-style error byte of each record (see :func:`decode_track`)."""
    return np.select(
        [~has_id, ~rec["id_ok"], ~has_data, ~rec["data_ok"]],
        [
            SectorError.HEADER_NOT_FOUND,
            SectorError.HEADER_CHECKSUM,
            SectorError.DATA_NOT_FOUND,
            SectorError.DATA_CHECKSUM,
        ],
        SectorError.OK,
    )


def _layout_flags(rec, has_id, cylinder, side):
    ok = has_id & rec["id_ok"]
    key = rec["c"].astype(np.int64) << 16 | rec["h"].astype(np.int64) << 8 | rec["r"]
    _, inverse, counts = np.unique(key[ok], return_inverse=True, return_counts=True)
    dup = np.zeros(len(rec), bool)
    dup[np.flatnonzero(ok)] = counts[inverse] > 1
    foreign = (rec["r"] < FIRST_SECTOR) | (rec["r"] >= FIRST_SECTOR + SECTORS)
    if cylinder is not None:
        foreign |= rec["c"] != cylinder
    if side is not None:
        foreign |= rec["h"] != side
    return np.where(dup, Flag.DUPLICATE, 0) | np.where(ok & foreign, Flag.FOREIGN, 0)


def _records(buf, ids, dats, n, orphan_n):
    """Field contents and CRC checks; ``buf`` is padded past ``n``."""
    rec = np.zeros(len(ids), SECTOR_DTYPE)
    rec["id_pos"], rec["data_pos"], rec["time_us"] = ids, dats, np.nan
    has_id, has_data = ids >= 0, dats >= 0
    hdr = buf[np.where(has_id, ids, 0)[:, None] + np.arange(1, 5)]
    for i, name in enumerate("chrn"):
        rec[name] = np.where(has_id, hdr[:, i], 0)
    rec["size"] = sector_size(np.where(has_id & has_data, rec["n"], orphan_n))
    rec["dam"] = np.where(has_data, buf[np.maximum(dats, 0)], 0)
    rec["id_ok"] = _crc_ok(buf, ids, np.full(len(ids), 5), has_id)
    rec["data_ok"] = _crc_ok(buf, dats, 1 + rec["size"], has_data)
    id_cut = has_id & (ids + ID_BYTES > n)
    cut = id_cut | (has_data & (dats + 3 + rec["size"] > n))
    rec["id_ok"] &= ~id_cut
    rec["data_ok"] &= ~cut
    return rec, cut


def decode_track(data, mark=None, circular=False, cylinder=None, side=None):
    """:class:`MfmTrack` of one revolution of Read Track output or media.

    Errors: no ID before the data 20, ID CRC 27, no data mark within
    :data:`DAM_WINDOW` bytes of a good ID 22, data CRC 23. ``cylinder`` and
    ``side`` (the ID's H) enable FOREIGN; circular media decode from an ID sync.
    """
    data = np.asarray(data, np.uint8)
    n = len(data)
    pos, cls = find_marks(data, mark, circular)
    shift = 0
    if circular and len(pos):
        start = pos[cls == _ID] if (cls == _ID).any() else pos[cls != _INDEX]
        shift = int(start[0]) - SYNC_MARKS if len(start) else 0
        data = np.roll(data, -shift)
        mark = None if mark is None else np.roll(np.asarray(mark, bool), -shift)
        pos, cls = find_marks(data, mark, False)
    pad = 3 + int(sector_size(3)) + ID_BYTES
    tail = np.resize(data, pad) if circular and n else np.zeros(pad, np.uint8)
    buf = np.concatenate((data, tail))
    ids, dats = _accept(buf, pos, cls, SIZE_CODE)
    rec, cut = _records(buf, ids, dats, n, SIZE_CODE)
    has_id, has_data = ids >= 0, dats >= 0
    off = _gaps(rec, has_id, has_data, circular, n)
    rec["error"] = _errors(rec, has_id, has_data)
    rec["flags"] = (
        np.where(has_data & (rec["dam"] <= 0xF9), Flag.DELETED, 0)
        | np.where(has_id & (rec["n"] != SIZE_CODE), Flag.ODD_SIZE, 0)
        | np.where(cut, Flag.TRUNCATED, 0)
        | np.where(off, Flag.GAP, 0)
        | _layout_flags(rec, has_id, cylinder, side)
    )
    if circular and n:
        for name in ("id_pos", "data_pos"):
            rec[name] = np.where(rec[name] >= 0, (rec[name] + shift) % n, -1)
        buf = np.roll(buf[:n], shift)
        buf = np.concatenate((buf, np.resize(buf, pad)))
    index = (pos[cls == _INDEX] + shift) % max(n, 1)
    return MfmTrack(rec, buf, n, index)


def decode_ids(ids, status, us, index_us=None, cylinder=None, side=None):
    """Read Address results (six ID bytes, WD status, time in us) as records.

    Positions count :data:`BYTE_US` bytes from the first index edge; with no
    data field read the error is 27 or OK, from the CRC status bit.
    """
    ids = np.asarray(ids, np.uint8).reshape(-1, 6)
    status = np.asarray(status, np.uint8)
    us = np.asarray(us, np.float64)
    t0 = index_us[0] if index_us is not None and len(index_us) else 0.0
    rec = np.zeros(len(ids), SECTOR_DTYPE)
    for i, name in enumerate("chrn"):
        rec[name] = ids[:, i]
    rec["id_ok"] = (status & ST_CRC) == 0
    rec["id_pos"] = np.round((us - t0) / BYTE_US)
    rec["time_us"] = us
    rec["data_pos"], rec["split"], rec["gap"], rec["gap_std"] = -1, -1, -1, -1
    rec["size"] = sector_size(rec["n"])
    rec["error"] = np.where(rec["id_ok"], SectorError.OK, SectorError.HEADER_CHECKSUM)
    odd = np.where(rec["n"] != SIZE_CODE, Flag.ODD_SIZE, 0)
    rec["flags"] = odd | _layout_flags(rec, np.ones(len(rec), bool), cylinder, side)
    return MfmTrack(rec, np.zeros(0, np.uint8), TRACK_BYTES)


def status_error(status):
    """Error byte of Read Sector status: RNF+CRC 27 (ID CRC), RNF 20, CRC 23
    (datasheet status summary); lost data 27 as the DOS ``wdstatus`` maps it."""
    status = np.asarray(status, np.uint8)
    rnf, crc = (status & ST_RNF) > 0, (status & ST_CRC) > 0
    return np.select(
        [rnf & crc, rnf, (status & ST_LOST) > 0, crc],
        [
            SectorError.HEADER_CHECKSUM,
            SectorError.HEADER_NOT_FOUND,
            SectorError.HEADER_CHECKSUM,
            SectorError.DATA_CHECKSUM,
        ],
        SectorError.OK,
    ).astype(np.uint8)


def decode_reads(reads, side=None):
    """Read Sector results ``(track_id, sector, data, status)`` as records; the
    buffer holds each as mark, data and two CRC placeholder bytes."""
    reads = list(reads)
    rec = np.zeros(len(reads), SECTOR_DTYPE)
    parts, at = [], 0
    for i, (track_id, sector, data, status) in enumerate(reads):
        data = np.asarray(data, np.uint8)
        dam = DELETED_DAM if status & ST_DELETED else DAM
        parts.append(np.concatenate(([dam], data, [0, 0])).astype(np.uint8))
        rec[i]["c"], rec[i]["r"], rec[i]["dam"] = track_id, sector, dam
        rec[i]["h"] = 0 if side is None else side
        rec[i]["n"] = size_code(len(data)) if len(data) else 0
        rec[i]["size"], rec[i]["data_pos"] = len(data), at
        at += len(parts[-1])
    status = np.array([r[3] for r in reads], np.uint8)
    rec["error"] = status_error(status)
    rec["id_ok"] = rec["error"] != SectorError.HEADER_CHECKSUM
    rec["data_ok"] = rec["error"] == SectorError.OK
    rec["id_pos"], rec["split"], rec["gap"], rec["gap_std"] = -1, -1, -1, -1
    rec["time_us"] = np.nan
    rec["flags"] = np.where(status & ST_DELETED, Flag.DELETED, 0) | np.where(
        rec["n"] != SIZE_CODE, Flag.ODD_SIZE, 0
    )
    buf = np.concatenate(parts) if parts else np.zeros(0, np.uint8)
    return MfmTrack(rec, buf, TRACK_BYTES)


_PREFERENCE = (
    SectorError.OK,
    SectorError.DATA_CHECKSUM,
    SectorError.DATA_NOT_FOUND,
    SectorError.HEADER_CHECKSUM,
)
_RANK = np.full(256, len(_PREFERENCE), np.int64)
_RANK[list(_PREFERENCE)] = np.arange(len(_PREFERENCE))


def rank(rec):
    """Read preference of records (lower is better): OK, data CRC, no data, ID
    CRC, the rest; a field cut by the end of the read after a whole one."""
    return 2 * _RANK[rec["error"]] + ((rec["flags"] & Flag.TRUNCATED) > 0)


def best_sectors(tracks, cylinder, side):
    """Logical sectors ``(data (20, 256), errors (20,))`` of one side of a cylinder.

    Per R the best read over ``tracks``: OK, data CRC, no data, ID CRC, a cut
    field after a whole one. OK and data CRC reads keep 512 bytes (cut or padded).
    """
    data = np.zeros((SECTORS, SECTOR_BYTES), np.uint8)
    errors = np.full(SECTORS, SectorError.HEADER_NOT_FOUND, np.uint8)
    best = np.full(SECTORS, np.iinfo(np.int64).max, np.int64)
    for track in tracks:
        rec = track.sectors
        r = rec["r"].astype(np.int64) - FIRST_SECTOR
        mine = (rec["c"] == cylinder) & (rec["h"] == side) & (r >= 0) & (r < SECTORS)
        mine &= rec["error"] != SectorError.HEADER_NOT_FOUND
        order = rank(rec)
        for i in np.flatnonzero(mine):
            if order[i] < best[r[i]]:
                best[r[i]], errors[r[i]] = order[i], rec["error"][i]
                data[r[i]] = 0
                if _RANK[rec["error"][i]] <= _RANK[SectorError.DATA_CHECKSUM]:
                    payload = track.payload(i)[:SECTOR_BYTES]
                    data[r[i], : len(payload)] = payload
    return data.reshape(HALF_SECTORS, LOGICAL_BYTES), np.repeat(errors, 2)


def physical(track, sector):
    """``(cylinder, side, R, half)`` of logical ``track`` 1..80, ``sector`` 0..39
    (``trans_ts``: side is the ID's H and PA0; the physical head is ``1 - side``)."""
    track, sector = np.asarray(track, np.int64), np.asarray(sector, np.int64)
    bad = (track < 1) | (track > CYLINDERS) | (sector < 0)
    if (bad | (sector >= LOGICAL_SECTORS)).any():
        raise ValueError("logical track 1..80 and sector 0..39")
    side = sector // HALF_SECTORS
    return track - 1, side, (sector % HALF_SECTORS) // 2 + FIRST_SECTOR, sector % 2


def logical(cylinder, side, r, half):
    """``(track, sector)`` of a physical sector half (inverse of :func:`physical`)."""
    cylinder, side = np.asarray(cylinder, np.int64), np.asarray(side, np.int64)
    r, half = np.asarray(r, np.int64), np.asarray(half, np.int64)
    return cylinder + 1, side * HALF_SECTORS + 2 * (r - FIRST_SECTOR) + half


def head_side(head):
    """ID side H (and CIA PA0) of physical ``head``: PA0 = 0 selects head 1."""
    return 1 - head


@dataclass
class SectorSpec:
    """One sector of a track layout; ``data`` None leaves out the data field."""

    c: int
    h: int
    r: int
    n: int = SIZE_CODE
    data: np.ndarray = None
    has_id: bool = True
    deleted: bool = False
    bad_id_crc: bool = False
    bad_data_crc: bool = False
    gap2: int = GAP2
    gap3: int = GAP3


def _bad_crc(crc):
    """CRC bytes differing from ``crc`` that Write Track can write literally:
    per byte, ``^ $FF`` or ``^ $01`` leaves $F5-$F7."""
    for mask in (0xFFFF, 0xFF01, 0x01FF, 0x0101):
        out = np.array([(crc ^ mask) >> 8, (crc ^ mask) & 0xFF], np.uint8)
        if not np.isin(out, WRITE_CODES).any():
            return out
    raise AssertionError("unreachable")


def _cat(parts, dtype):
    return np.concatenate(parts).astype(dtype)


class _Builder:
    """Media bytes, missing-clock flags and Write Track register bytes side by side."""

    def __init__(self):
        self.media, self.mark, self.dr, self.cmd = [], [], [], []

    def put(self, media, mark=False, dr=None, cmd=False):
        media = np.atleast_1d(np.asarray(media, np.uint8))
        dr = media if dr is None else np.atleast_1d(np.asarray(dr, np.uint8))
        self.media.append(media)
        self.mark.append(np.full(len(media), mark))
        self.dr.append(dr)
        self.cmd.append(np.full(len(dr), cmd))

    def fill(self, value, count):
        self.put(np.full(count, value, np.uint8))

    def field(self, body, good):
        """Sync, mark and body, then the CRC (F7 when good, else literal bytes)."""
        self.fill(0, SYNC_ZEROS)
        self.put([A1] * SYNC_MARKS, True, [WRITE_A1] * SYNC_MARKS, True)
        body = np.asarray(body, np.uint8)
        crc = crc16(body, SYNC3)
        self.put(body)
        if good:
            self.put([crc >> 8, crc & 0xFF], dr=[WRITE_CRC], cmd=True)
        else:
            self.put(_bad_crc(crc))

    def arrays(self):
        return (
            _cat(self.media, np.uint8),
            _cat(self.mark, bool),
            _cat(self.dr, np.uint8),
            _cat(self.cmd, bool),
        )


def _sector(out, spec, payload):
    if spec.has_id:
        out.field([IDAM, spec.c, spec.h, spec.r, spec.n], not spec.bad_id_crc)
    else:
        out.fill(GAP_BYTE, SYNC_ZEROS + SYNC_MARKS + ID_BYTES)
    out.fill(GAP_BYTE, spec.gap2)
    if payload is None:
        out.fill(GAP_BYTE, SYNC_ZEROS + SYNC_MARKS + 3 + int(sector_size(spec.n)))
    else:
        payload = np.asarray(payload, np.uint8)
        if len(payload) != sector_size(spec.n):
            raise ValueError(f"sector {spec.r}: {len(payload)} bytes for N={spec.n}")
        mark = DELETED_DAM if spec.deleted else DAM
        out.field(np.concatenate(([mark], payload)), not spec.bad_data_crc)
    out.fill(GAP_BYTE, spec.gap3)


def _build(specs, lead, payloads=None):
    out = _Builder()
    out.fill(GAP_BYTE, lead)
    for i, spec in enumerate(specs):
        _sector(out, spec, spec.data if payloads is None else payloads[i])
    return out.arrays()


def encode_track(specs, lead=LEAD, n=TRACK_BYTES):
    """Media ``(data, mark)`` of a layout, ``$4E`` to the index; any byte values."""
    media, mark, _, _ = _build(specs, lead)
    if len(media) > n:
        raise ValueError(f"layout of {len(media)} bytes exceeds {n}")
    pad = n - len(media)
    return (
        np.concatenate((media, np.full(pad, GAP_BYTE, np.uint8))),
        np.concatenate((mark, np.zeros(pad, bool))),
    )


_NO_ID = (SectorError.HEADER_NOT_FOUND, SectorError.NO_SYNC)
_NO_DATA = (SectorError.DATA_NOT_FOUND, SectorError.NO_SYNC)
_ENCODABLE = (
    (SectorError.OK, SectorError.HEADER_CHECKSUM, SectorError.DATA_CHECKSUM)
    + _NO_ID
    + _NO_DATA
)


def standard_layout(cylinder, side, data=None, errors=None):
    """The ``fmtrk`` layout of one side (R 1..10, N 2, gap3 35) holding ``data``
    ``(10, 512)``; per physical sector error bytes 20 drop the ID, 22 the data,
    21 both, 27 and 23 write a wrong ID or data CRC."""
    data = np.zeros((SECTORS, SECTOR_BYTES), np.uint8) if data is None else data
    data = np.asarray(data, np.uint8).reshape(SECTORS, SECTOR_BYTES)
    errors = np.full(SECTORS, SectorError.OK) if errors is None else errors
    specs = []
    for i, err in enumerate(np.asarray(errors, np.uint8)):
        if err not in _ENCODABLE:
            raise ValueError(f"error byte {err} has no MFM form")
        specs.append(
            SectorSpec(
                cylinder,
                side,
                FIRST_SECTOR + i,
                data=None if err in _NO_DATA else data[i],
                has_id=err not in _NO_ID,
                bad_id_crc=err == SectorError.HEADER_CHECKSUM,
                bad_data_crc=err == SectorError.DATA_CHECKSUM,
            )
        )
    return specs


@njit(cache=True)
def _rle(dr):  # pragma: no cover
    n = len(dr)
    out = np.empty(n + n // 128 + 2, np.uint8)
    o, i = 0, 0
    while i < n:
        j = i
        while j < n and dr[j] == dr[i] and j - i < 127:
            j += 1
        if j - i >= 3:
            out[o], out[o + 1] = j - i, dr[i]
            o, i = o + 2, j
            continue
        s = i
        while i < n and i - s < 128:
            k = i
            while k < n and dr[k] == dr[i] and k - i < 3:
                k += 1
            if k - i >= 3:
                break
            i += 1
        out[o] = 127 + i - s
        out[o + 1 : o + 1 + i - s] = dr[s:i]
        o += 1 + i - s
    out[o] = 0
    return out[: o + 1]


def rle(dr):
    """Drive write-track image of register bytes: token 0 end, 1..127 repeat the
    next byte, 128..255 copy ``t - 127`` literals; the trailing run of the last
    byte is cut to one, as the drive repeats it until the index."""
    dr = np.asarray(dr, np.uint8)
    if len(dr) == 0:
        raise ValueError("empty write-track image")
    if dr[-1] in WRITE_CODES:
        raise ValueError("a write-track image cannot end in $F5-$F7")
    differ = np.flatnonzero(dr != dr[-1])
    keep = int(differ[-1]) + 1 if len(differ) else 0
    return _rle(dr[: keep + 1])


@njit(cache=True)
def _unrle(image):  # pragma: no cover
    out = np.empty(128 * len(image), np.uint8)
    o, i = 0, 0
    while i < len(image) and image[i] != 0:
        t = image[i]
        if t < 128:
            out[o : o + t] = image[i + 1]
            o, i = o + t, i + 2
        else:
            k = t - 127
            out[o : o + k] = image[i + 1 : i + 1 + k]
            o, i = o + k, i + 1 + k
    return out[:o]


def unrle(image):
    """Register bytes of a write-track image (inverse of :func:`rle`)."""
    return _unrle(np.asarray(image, np.uint8))


@njit(cache=True)
def _write_track(dr, n, sync_crc):  # pragma: no cover
    data = np.empty(n, np.uint8)
    mark = np.zeros(n, np.bool_)
    crc, o, i = 0xFFFF, 0, 0
    while o < n:
        b = dr[min(i, len(dr) - 1)]
        i += 1
        if b == WRITE_A1:
            data[o], mark[o], crc = A1, True, sync_crc
            o += 1
        elif b == WRITE_C2:
            data[o], mark[o] = C2, True
            o += 1
        elif b == WRITE_CRC:
            data[o] = crc >> 8
            if o + 1 < n:
                data[o + 1] = crc & 0xFF
            o += 2
        else:
            data[o] = b
            crc = ((crc << 8) & 0xFFFF) ^ TABLE[((crc >> 8) ^ b) & 0xFF]
            o += 1
    return data, mark


def wd_write_track(dr, n=TRACK_BYTES):
    """Media ``(data, mark)`` Write Track of register bytes ``dr`` leaves on ``n``
    bytes: $F5 A1* (CRC preset to three A1), $F6 C2*, $F7 the two CRC bytes;
    the last byte repeats to the index and bytes past it are not written."""
    return _write_track(np.asarray(dr, np.uint8), n, SYNC3)


def wd_write_sector(data, mark, id_end, payload, deleted=False):
    """Media after Write Sector on the ID ending at ``id_end``: from 22 bytes on,
    12 zeros, three A1*, the data mark, data, CRC and one byte of logic ones."""
    data, mark = np.array(data, np.uint8), np.array(mark, bool)
    body = np.concatenate(([DELETED_DAM if deleted else DAM], payload)).astype(np.uint8)
    crc = crc16(body, SYNC3)
    new = np.concatenate(
        (
            np.zeros(SYNC_ZEROS, np.uint8),
            np.full(SYNC_MARKS, A1, np.uint8),
            body,
            np.array([crc >> 8, crc & 0xFF, LOGIC_ONES], np.uint8),
        )
    )
    at = (id_end + GAP2 + np.arange(len(new))) % len(data)
    data[at] = new
    mark[at] = False
    mark[at[SYNC_ZEROS : SYNC_ZEROS + SYNC_MARKS]] = True
    return data, mark


@dataclass
class TrackPlan:
    """A layout as the drive writes it: Write Track of ``image``, then Write Sector
    of ``writes`` ``(track_id, sector, data, deleted)``, leaving ``data``/``mark``."""

    image: np.ndarray
    writes: list
    data: np.ndarray
    mark: np.ndarray


def _by_write_sector(specs):
    """Which sectors Write Sector can reproduce: a findable unique ID (the WD
    compares C and R only), a good data CRC and the fixed 22-byte gap 2."""
    keys = [(s.c, s.r) for s in specs if s.has_id and not s.bad_id_crc]
    return [
        s.has_id
        and not s.bad_id_crc
        and not s.bad_data_crc
        and s.data is not None
        and s.gap2 == GAP2
        and s.gap3 > 0
        and keys.count((s.c, s.r)) == 1
        for s in specs
    ]


def plan_track(specs, lead=LEAD, n=TRACK_BYTES):
    """:class:`TrackPlan` of a layout: constant data goes in the image, other data
    Write Sector can write is formatted as zeros and written after; data with
    $F5-$F7 it cannot write is rejected."""
    payloads, writes = [], []
    for spec, writable in zip(specs, _by_write_sector(specs)):
        data = None if spec.data is None else np.asarray(spec.data, np.uint8)
        coded = data is not None and np.isin(data, WRITE_CODES).any()
        inline = data is None or ((data == data[0]).all() and not coded)
        if writable and not inline:
            payloads.append(np.zeros_like(data))
            writes.append((spec.c, spec.r, data, spec.deleted))
        elif coded:
            raise ValueError(f"sector {spec.r}: data with $F5-$F7 needs Write Sector")
        else:
            payloads.append(data)
    _, _, dr, cmd = _build(specs, lead, payloads)
    if np.isin(dr[~cmd], WRITE_CODES).any():
        raise ValueError(
            "ID, CRC or gap bytes $F5-$F7 cannot be written by Write Track"
        )
    data, mark = wd_write_track(dr, n)
    rec = decode_track(data, mark).sectors
    for c, r, payload, deleted in writes:
        pos = rec["id_pos"][(rec["c"] == c) & (rec["r"] == r) & rec["id_ok"]][0]
        data, mark = wd_write_sector(data, mark, int(pos) + ID_BYTES, payload, deleted)
    return TrackPlan(rle(dr), writes, data, mark)


def _mode_rows(rows):
    """Per column, the most common value among the rows (first on ties)."""
    counts = (rows[:, None, :] == rows[None, :, :]).sum(axis=1)
    return rows[np.argmax(counts, axis=0), np.arange(rows.shape[1])]


def runs(mask):
    """``(start, end)`` of the runs of True in a boolean array."""
    edges = np.diff(np.concatenate(([0], np.asarray(mask, np.int8), [0])))
    return np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)


AGREE_DTYPE = np.dtype(
    [
        ("c", "u1"),
        ("h", "u1"),
        ("r", "u1"),
        ("copy", "<i4"),
        ("reads", "<i4"),
        ("ok", "<i4"),
        ("variants", "<i4"),
        ("stable", "?"),
    ]
)
WEAK_DTYPE = np.dtype(
    [
        ("c", "u1"),
        ("h", "u1"),
        ("r", "u1"),
        ("copy", "<i4"),
        ("rev", "<i4"),
        ("record", "<i4"),
        ("start", "<i4"),
        ("end", "<i4"),
    ]
)


def _reads(tracks):
    """``{(c, h, r, copy): [(rev, record)]}`` of the good IDs of every revolution;
    ``copy`` numbers a revolution's IDs of one (C, H, R) in rotational order."""
    out = {}
    for rev, track in enumerate(tracks):
        rec = track.sectors
        good = np.flatnonzero(rec["id_ok"] & (rec["id_pos"] >= 0))
        seen = {}
        for i in good[np.argsort(rec["id_pos"][good], kind="stable")]:
            chr_ = (int(rec["c"][i]), int(rec["h"][i]), int(rec["r"][i]))
            seen[chr_] = seen.get(chr_, -1) + 1
            out.setdefault((*chr_, seen[chr_]), []).append((rev, int(i)))
    return out


def _weak(tracks, key, reads):
    """Distinct contents of whole same-size payloads and their minority byte runs."""
    whole = [
        (rev, i, tracks[rev].payload(i))
        for rev, i in reads
        if not tracks[rev].sectors["flags"][i] & Flag.TRUNCATED
    ]
    size = max((len(p) for _, _, p in whole), default=0)
    whole = [w for w in whole if size and len(w[2]) == size]
    variants = len({p.tobytes() for _, _, p in whole})
    if variants < 2:
        return variants, []
    rows = np.stack([p for _, _, p in whole])
    differ = rows != _mode_rows(rows)
    return variants, [
        (*key, rev, i, s, e)
        for (rev, i, _), row in zip(whole, differ)
        for s, e in zip(*runs(row))
    ]


def compare_revolutions(tracks):
    """Agreement of each good ID's sector over revolutions, and weak byte runs.

    ``agree`` per (C, H, R) and copy: revolutions read, error-free reads, distinct data and
    stability. ``weak``: payload runs differing from the per-byte majority.
    """
    reads = _reads(tracks)
    agree = np.zeros(len(reads), AGREE_DTYPE)
    weak = []
    for j, key in enumerate(sorted(reads)):
        errors = [int(tracks[rev].sectors["error"][i]) for rev, i in reads[key]]
        revs = len({rev for rev, _ in reads[key]})
        variants, found = _weak(tracks, key, reads[key])
        weak += found
        stable = revs == len(tracks) and len(set(errors)) == 1 and variants <= 1
        agree[j] = (*key, revs, errors.count(SectorError.OK), variants, stable)
    return agree, np.array(weak, WEAK_DTYPE)
