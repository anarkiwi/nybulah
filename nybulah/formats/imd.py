"""ImageDisk (.IMD) images, from the format chapter of Dave Dunfield's IMD.TXT
(ImageDisk 1.18, chapter 6 "Image file format"), including the suggested 0xFF
per-sector size table extension."""

import datetime
from dataclasses import dataclass

import numpy as np

from ..analysis import mfm
from ..analysis.sector import SectorError

MAGIC = b"IMD "
EOF = 0x1A
MODE_MFM_250 = 5
CYLINDER_MAP, HEAD_MAP = 0x80, 0x40
SIZE_TABLE = 0xFF
UNAVAILABLE = 0
MIN_GAP = 2


@dataclass
class ImdTrack:
    """One track: per sector ID (``r``, ``c``, ``h``), size, data (None when
    unavailable), deleted-data mark and data error."""

    mode: int
    cylinder: int
    head: int
    r: np.ndarray
    c: np.ndarray
    h: np.ndarray
    sizes: np.ndarray
    data: list
    deleted: np.ndarray
    error: np.ndarray

    @property
    def size_code(self):
        """Header size value: the common code, or 0xFF for the size table."""
        sizes = set(self.sizes.tolist())
        return int(self.sizes[0]).bit_length() - 8 if len(sizes) == 1 else SIZE_TABLE


@dataclass
class Imd:
    """ASCII header and comment (everything before the 0x1A) and the tracks."""

    header: bytes
    tracks: list


def default_header(comment="", when=None):
    """``IMD 1.18: dd/mm/yyyy hh:mm:ss`` line and comment."""
    when = when or datetime.datetime.now()
    return f"IMD 1.18: {when:%d/%m/%Y %H:%M:%S}\r\n{comment}".encode("ascii")


def _take(buf, pos, count):
    if pos + count > len(buf):
        raise ValueError("IMD image truncated")
    return np.frombuffer(buf, np.uint8, count, pos).copy(), pos + count


def _record(buf, pos, size):
    """``(data, deleted, error, pos)`` of one sector data record."""
    (kind,), pos = _take(buf, pos, 1)
    if kind == UNAVAILABLE:
        return None, False, False, pos
    if kind > 8:
        raise ValueError(f"IMD sector record type {kind}")
    code = int(kind) - 1
    if code & 1:
        (fill,), pos = _take(buf, pos, 1)
        data = np.full(size, fill, np.uint8)
    else:
        data, pos = _take(buf, pos, size)
    return data, bool(code & 2), bool(code & 4), pos


def _track(buf, pos):
    fields, pos = _take(buf, pos, 5)
    mode, cyl, head, count, size = (int(v) for v in fields)
    r, pos = _take(buf, pos, count)
    c, h = np.full(count, cyl, np.uint8), np.full(count, head & 1, np.uint8)
    if head & CYLINDER_MAP:
        c, pos = _take(buf, pos, count)
    if head & HEAD_MAP:
        h, pos = _take(buf, pos, count)
    if size == SIZE_TABLE:
        table, pos = _take(buf, pos, 2 * count)
        sizes = table.view("<u2").astype(np.int64)
    else:
        sizes = np.full(count, 128 << size, np.int64)
    records = []
    for s in sizes:
        *rec, pos = _record(buf, pos, int(s))
        records.append(rec)
    data, deleted, error = (list(x) for x in zip(*records)) if records else ([],) * 3
    track = ImdTrack(
        mode,
        cyl,
        head & 1,
        r,
        c,
        h,
        sizes,
        data,
        np.array(deleted, bool),
        np.array(error, bool),
    )
    return track, pos


def read_imd(buf):
    """Parse an IMD image from bytes."""
    buf = bytes(buf)
    if not buf.startswith(MAGIC) or EOF not in buf:
        raise ValueError("not an IMD image")
    pos = buf.index(EOF) + 1
    tracks = []
    while pos < len(buf):
        track, pos = _track(buf, pos)
        tracks.append(track)
    return Imd(buf[: buf.index(EOF)], tracks)


def _data_record(track, i):
    data = track.data[i]
    if data is None:
        return bytes([UNAVAILABLE])
    data = np.asarray(data, np.uint8)
    compressed = bool(len(data)) and bool((data == data[0]).all())
    kind = 1 + compressed + 2 * bool(track.deleted[i]) + 4 * bool(track.error[i])
    return bytes([kind]) + (data[:1] if compressed else data).tobytes()


def write_imd(image):
    """Serialise an IMD; sectors of one repeated byte are written compressed."""
    out = [image.header, bytes([EOF])]
    for t in image.tracks:
        cmap = (t.c != t.cylinder).any()
        hmap = (t.h != t.head).any()
        flags = t.head | (CYLINDER_MAP if cmap else 0) | (HEAD_MAP if hmap else 0)
        out.append(bytes([t.mode, t.cylinder, flags, len(t.r), t.size_code]))
        out.append(t.r.tobytes())
        out += [t.c.tobytes()] * bool(cmap) + [t.h.tobytes()] * bool(hmap)
        if t.size_code == SIZE_TABLE:
            out.append(t.sizes.astype("<u2").tobytes())
        out += [_data_record(t, i) for i in range(len(t.r))]
    return b"".join(out)


def _best(tracks):
    """Best record per good ID over revolutions, in rotational order."""
    best = {}
    for k, track in enumerate(tracks):
        rec = track.sectors
        rank = mfm.rank(rec)
        for i in np.flatnonzero(rec["id_ok"] & (rec["id_pos"] >= 0)):
            key = (int(rec["c"][i]), int(rec["h"][i]), int(rec["r"][i]))
            if key not in best or rank[i] < best[key][0]:
                best[key] = (rank[i], int(rec["id_pos"][i]), k, int(i))
    return sorted(best.values(), key=lambda b: b[1])


def track_from_decodes(key, tracks):
    """:class:`ImdTrack` (250 kbps MFM) of the decodes of one ``(cylinder, head)``;
    None without a readable ID. ID CRC errors are left out, as ImageDisk cannot read them.
    """
    chosen = _best(tracks)
    if not chosen:
        return None
    recs = [tracks[k].sectors[i] for _, _, k, i in chosen]
    readable = [r["error"] in (SectorError.OK, SectorError.DATA_CHECKSUM) for r in recs]
    return ImdTrack(
        MODE_MFM_250,
        key[0],
        key[1],
        np.array([r["r"] for r in recs], np.uint8),
        np.array([r["c"] for r in recs], np.uint8),
        np.array([r["h"] for r in recs], np.uint8),
        np.array([mfm.sector_size(r["n"]) for r in recs], np.int64),
        [
            tracks[k].payload(i).copy() if ok else None
            for (_, _, k, i), ok in zip(chosen, readable)
        ],
        np.array([bool(r["flags"] & mfm.Flag.DELETED) for r in recs], bool),
        np.array([r["error"] == SectorError.DATA_CHECKSUM for r in recs], bool),
    )


def from_decodes(decodes, header=None):
    """IMD of decoded tracks ``{(cylinder, head): [MfmTrack, ...]}``."""
    tracks = [track_from_decodes(k, decodes[k]) for k in sorted(decodes)]
    return Imd(header or default_header(), [t for t in tracks if t is not None])


def specs(track):
    """:class:`mfm.SectorSpec` layout of an IMD track: data errors as bad CRCs,
    unavailable data as a missing data field, gap3 shrunk (to at least
    :data:`MIN_GAP`, the WD1772 minimum) when the sectors overflow the track."""
    out = [
        mfm.SectorSpec(
            int(track.c[i]),
            int(track.h[i]),
            int(track.r[i]),
            mfm.size_code(track.sizes[i]),
            track.data[i],
            deleted=bool(track.deleted[i]),
            bad_data_crc=bool(track.error[i]),
        )
        for i in range(len(track.r))
    ]
    used = mfm.LEAD + sum(
        2 * (mfm.SYNC_ZEROS + mfm.SYNC_MARKS) + mfm.ID_BYTES + mfm.GAP2 + 3 + s
        for s in track.sizes
    )
    room = (mfm.TRACK_BYTES - used) // max(len(out), 1)
    gap3 = min(mfm.GAP3, room)
    if gap3 < MIN_GAP:
        raise ValueError(f"cylinder {track.cylinder}: sectors exceed the track")
    for spec in out:
        spec.gap3 = gap3
    return out


def to_tracks(image):
    """Media ``{(cylinder, head): (data, mark)}`` of an IMD's MFM tracks."""
    out = {}
    for track in image.tracks:
        if track.mode != MODE_MFM_250:
            raise ValueError(f"IMD mode {track.mode} is not 250 kbps MFM")
        out[(track.cylinder, track.head)] = mfm.encode_track(specs(track))
    return out
