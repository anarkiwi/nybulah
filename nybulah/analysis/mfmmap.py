"""Disk map of 1581 MFM tracks on the :mod:`.diskmap` regions, stability and raster.

Positions are data bits (8 per byte) from the index: Read Track starts at the
index pulse (WD1772 datasheet), so revolutions are compared without alignment.
"""

import numpy as np
from tqdm import tqdm

from ..formats.g64 import FIRST_HALFTRACK, SIDE1
from . import mfm
from .diskmap import REGION_DTYPE, DiskMap, Kind, _rows, stability

MFM_KINDS = {
    "sync": Kind.SYNC,
    "id": Kind.HEADER,
    "data": Kind.DATA,
    "gap": Kind.GAP,
    "id_crc": Kind.HDR_CHECKSUM,
    "id_track": Kind.HDR_TRACK,
    "id_side": Kind.HDR_ID,
    "id_sector": Kind.HDR_SECTOR,
    "duplicate": Kind.HDR_DUPLICATE,
    "no_data": Kind.ID_NO_DATA,
    "no_id": Kind.DATA_ORPHAN,
    "data_crc": Kind.DATA_CHECKSUM,
    "deleted": Kind.DATA_DELETED,
    "odd_size": Kind.DATA_SIZE,
    "gap_long": Kind.GAP_LONG,
    "gap_short": Kind.GAP_SHORT,
    "weak": Kind.DISAGREE,
    "unformatted": Kind.UNFORMATTED,
}
K = MFM_KINDS


def map_key(cylinder, side):
    """Disk map key: the halftrack of logical track ``cylinder + 1``, ``SIDE1`` for H = 1."""
    return (2 * cylinder + FIRST_HALFTRACK) | (SIDE1 if side else 0)


def _ids(rec, cylinder, side):
    has = rec["id_pos"] >= 0
    rec, flags = rec[has], rec["flags"][has]
    r = rec["r"].astype(np.int64)
    kind = np.select(
        [
            ~rec["id_ok"],
            rec["c"] != cylinder,
            rec["h"] != side,
            (r < mfm.FIRST_SECTOR) | (r >= mfm.FIRST_SECTOR + mfm.SECTORS),
            (flags & mfm.Flag.DUPLICATE) > 0,
            rec["data_pos"] < 0,
        ],
        [K["id_crc"], K["id_track"], K["id_side"], K["id_sector"]]
        + [K["duplicate"], K["no_data"]],
        K["id"],
    )
    start = rec["id_pos"]
    return _rows(start, start + mfm.ID_BYTES, kind, r), start


def _data(rec):
    has = rec["data_pos"] >= 0
    rec = rec[has]
    flags = rec["flags"]
    kind = np.select(
        [
            rec["id_pos"] < 0,
            ~rec["data_ok"],
            (flags & mfm.Flag.DELETED) > 0,
            (flags & mfm.Flag.ODD_SIZE) > 0,
        ],
        [K["no_id"], K["data_crc"], K["deleted"], K["odd_size"]],
        K["data"],
    )
    detail = np.where(rec["id_pos"] < 0, -1, rec["r"].astype(np.int64))
    start = rec["data_pos"]
    return _rows(start, start + 3 + rec["size"], kind, detail), start


def _gap_kind(length, std):
    return np.select(
        [(std >= 0) & (length > std + mfm.SPLICE_SLIP), length < std - mfm.SPLICE_SLIP],
        [K["gap_long"], K["gap_short"]],
        K["gap"],
    )


def _gaps(rec, n):
    """Gap before each record, the ID-to-data split and, from Read Track output,
    the run-out to the index."""
    first = np.where(rec["id_pos"] >= 0, rec["id_pos"], rec["data_pos"])
    split = rec["split"] >= 0
    last = rec[-1:] if rec["gap_std"][0] >= 0 else rec[:0]
    tail = np.where(
        last["data_pos"] >= 0,
        last["data_pos"] + 3 + last["size"],
        last["id_pos"] + mfm.ID_BYTES,
    )
    starts = np.concatenate(
        (first - mfm.SYNC_MARKS - rec["gap"], rec["id_pos"][split] + mfm.ID_BYTES, tail)
    )
    lengths = np.concatenate((rec["gap"], rec["split"][split], n - tail))
    stds = np.concatenate(
        (rec["gap_std"], np.full(split.sum(), mfm.GAP_AFTER_ID), np.full(len(tail), -1))
    )
    return _rows(starts % n, starts % n + lengths, _gap_kind(lengths, stds), lengths)


def track_regions(track, cylinder, side):
    """Regions (byte units) of one decoded revolution; no field is UNFORMATTED."""
    rec = track.sectors
    if len(rec) == 0:
        return _rows(np.zeros(1), np.full(1, track.n), K["unformatted"], 0)
    first = np.where(rec["id_pos"] >= 0, rec["id_pos"], rec["data_pos"])
    rec = rec[np.argsort(first, kind="stable")]
    ids, id_start = _ids(rec, cylinder, side)
    data, data_start = _data(rec)
    marks = np.concatenate((id_start, data_start))
    syncs = _rows(marks - mfm.SYNC_MARKS, marks, K["sync"], mfm.SYNC_MARKS)
    regs = np.concatenate((syncs, ids, data, _gaps(rec, track.n)))
    width = regs["end_bit"] - regs["start_bit"]
    regs["start_bit"] %= track.n
    regs["end_bit"] = regs["start_bit"] + width
    return regs


def _weak(tracks):
    """DISAGREE regions of every revolution's minority payload bytes."""
    _, weak = mfm.compare_revolutions(tracks)
    out = []
    for rev in np.unique(weak["rev"]):
        w = weak[weak["rev"] == rev]
        start = tracks[rev].sectors["data_pos"][w["record"]] + 1 + w["start"]
        rows = _rows(
            start, start + w["end"] - w["start"], K["weak"], w["end"] - w["start"]
        )
        rows["rev"] = rev
        out.append(rows)
    return out


def mfm_track_map(tracks, cylinder, side):
    """Regions of every revolution of one track (bits), with stability set."""
    n = max(max((t.n for t in tracks), default=1), 1)
    found = []
    for rev, track in enumerate(tracks):
        regs = track_regions(track, cylinder, side)
        regs["rev"] = rev
        found.append(regs)
    regs = np.concatenate(found + _weak(tracks))
    regs["start_bit"] *= 8
    regs["end_bit"] *= 8
    return stability(regs, len(tracks), 8 * n), n


def mfm_disk_map(decodes, bins=2048, progress=False):
    """:class:`DiskMap` of Read Track/media decodes ``{(cylinder, head): [MfmTrack]}``;
    rows are keyed by :func:`map_key` of the ID side H of the head."""
    keys, lengths, revs, regions = [], [], [], []
    for cyl, head in tqdm(
        sorted(decodes), desc="map", unit="trk", disable=not progress
    ):
        tracks = decodes[(cyl, head)]
        if not tracks:
            continue
        side = mfm.head_side(head)
        regs, n = mfm_track_map(tracks, cyl, side)
        regs["track"] = map_key(cyl, side)
        keys.append(map_key(cyl, side))
        lengths.append(8 * n)
        revs.append(len(tracks))
        regions.append(regs)
    order = np.argsort(keys)
    table = np.zeros(len(keys), [("rev_len", "<i8")])
    table["rev_len"] = np.array(lengths, np.int64)[order]
    regions = np.concatenate(regions) if regions else np.zeros(0, REGION_DTYPE)
    return DiskMap(
        np.array(keys, np.int64)[order],
        table,
        np.array(revs, np.int64)[order],
        np.ones(len(keys), bool),
        regions,
        bins,
    )
