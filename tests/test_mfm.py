import numpy as np
import pytest

from nybulah.analysis import mfm
from nybulah.analysis.crc import SYNC3, crc16
from nybulah.analysis.mfm import Flag, SectorSpec
from nybulah.analysis.sector import SectorError as E

CYL, SIDE = 7, 1
PITCH = 2 * (12 + 3) + 7 + 22 + 3 + 512 + 35


def _data(seed=0, sectors=10):
    return np.random.default_rng(seed).integers(0, 256, (sectors, 512), np.uint8)


def _specs(seed=0, **errors):
    return mfm.standard_layout(CYL, SIDE, _data(seed), errors.get("errors"))


def _decode(specs, circular=True, marks=True):
    data, mark = mfm.encode_track(specs)
    return mfm.decode_track(
        data, mark if marks else None, circular, cylinder=CYL, side=SIDE
    )


def _read_track(data, mark, rng):
    """Read Track output: missing clocks lost, the first sync byte misframed and
    the plain gap bytes replaced by garbage that holds no A1 run."""
    out = np.array(data)
    first = mark & ~np.roll(mark, 1)
    out[first] = rng.integers(0, 0xA0, first.sum())
    field = np.zeros(len(data), int)
    rec = mfm.decode_track(data, mark).sectors
    for start, end in (
        (rec["id_pos"], rec["id_pos"] + 7),
        (rec["data_pos"], rec["data_pos"] + 515),
    ):
        np.add.at(field, start, 1)
        np.add.at(field, end, -1)
    gap = (out == mfm.GAP_BYTE) & (np.cumsum(field) == 0)
    out[gap] = rng.choice([0x4E, 0x27, 0x13, 0x9C, 0xFF], gap.sum())
    return out


def test_sync_crc_is_datasheet_preset():
    assert SYNC3 == 0xCDB4
    assert crc16(np.array([0xA1] * 3 + [0xFE, 0, 0, 1, 2], np.uint8)) == crc16(
        np.array([0xFE, 0, 0, 1, 2], np.uint8), SYNC3
    )


def test_standard_layout_matches_fmtrk():
    data, mark = mfm.encode_track(_specs())
    assert len(data) == mfm.TRACK_BYTES == 6250 and mfm.BYTE_US == 32
    marks = np.flatnonzero(mark & ~np.roll(mark, -1)) + 1
    ids = marks[data[marks] == mfm.IDAM]
    assert ids[0] == 32 + 12 + 3
    assert (np.diff(ids) == PITCH).all() and len(ids) == 10
    assert (data[ids + 1] == CYL).all() and (data[ids + 2] == SIDE).all()
    assert (data[ids + 3] == np.arange(1, 11)).all() and (data[ids + 4] == 2).all()
    assert (data[ids[-1] + PITCH - 15 :] == 0x4E).all()


@pytest.mark.parametrize(
    "circular,marks", [(True, True), (False, True), (False, False)]
)
def test_roundtrip_clean(circular, marks):
    track = _decode(_specs(), circular, marks)
    rec = track.sectors
    assert len(rec) == 10 and (rec["error"] == E.OK).all() and (rec["flags"] == 0).all()
    assert (rec["split"] == 34).all() and (rec["gap"][1:] == 47).all()
    data, errors = mfm.best_sectors([track], CYL, SIDE)
    assert np.array_equal(data.reshape(10, 512), _data()) and (errors == E.OK).all()


def test_read_track_misframed_and_garbage_gaps():
    data, mark = mfm.encode_track(_specs())
    rng = np.random.default_rng(3)
    track = mfm.decode_track(_read_track(data, mark, rng), cylinder=CYL, side=SIDE)
    assert (track.sectors["error"] == E.OK).all() and len(track.sectors) == 10
    assert (track.sectors["id_pos"] == _decode(_specs()).sectors["id_pos"]).all()


def test_marks_inside_data_are_data():
    payload = _data()
    payload[2, 100:104] = [0xA1, 0xA1, 0xA1, 0xFE]
    payload[4, 7:10] = [0xA1, 0xA1, 0xFB]
    track = _decode(mfm.standard_layout(CYL, SIDE, payload), marks=False)
    assert len(track.sectors) == 10 and (track.sectors["error"] == E.OK).all()


def test_index_mark():
    data, mark = mfm.encode_track(_specs())
    data[10:14], mark[10:13] = [0xC2, 0xC2, 0xC2, 0xFC], True
    assert list(mfm.decode_track(data, mark).index_marks) == [13]
    assert list(mfm.decode_track(data).index_marks) == [13]


def test_error_classes():
    errors = [E.OK, E.HEADER_NOT_FOUND, E.HEADER_CHECKSUM, E.DATA_NOT_FOUND]
    errors += [E.DATA_CHECKSUM, E.NO_SYNC] + [E.OK] * 4
    track = _decode(mfm.standard_layout(CYL, SIDE, _data(), errors))
    rec = track.sectors
    orphan = rec[rec["id_pos"] < 0]
    assert len(orphan) == 1 and orphan["error"][0] == E.HEADER_NOT_FOUND
    by_r = {int(r["r"]): int(r["error"]) for r in rec[rec["id_pos"] >= 0]}
    assert by_r == {1: 1, 3: 9, 4: 4, 5: 5, 7: 1, 8: 1, 9: 1, 10: 1}
    _, logical = mfm.best_sectors([track], CYL, SIDE)
    expect = [1, 2, 9, 4, 5, 2, 1, 1, 1, 1]
    assert list(logical[::2]) == expect and list(logical[1::2]) == expect
    with pytest.raises(ValueError):
        mfm.standard_layout(CYL, SIDE, errors=[E.BAD_GCR] * 10)


def test_nonstandard_layouts():
    d = _data()
    specs = _specs()
    specs[1].deleted = True
    specs[2] = SectorSpec(CYL, SIDE, 3, 1, d[2, :256])
    specs[3] = SectorSpec(CYL, SIDE, 4, 3, np.zeros(1024, np.uint8), gap3=10)
    specs[5].r = 3
    specs[6].r = 11
    specs[7].gap2 = 26
    specs[8].c = CYL + 1
    specs = specs[:-1]
    track = _decode(specs)
    rec = track.sectors
    flags = {int(r["id_pos"]): int(r["flags"]) for r in rec}
    get = [flags[int(p)] for p in rec["id_pos"]]
    assert get[1] == Flag.DELETED and rec["error"][1] == E.OK
    assert get[2] & Flag.ODD_SIZE and rec["size"][2] == 256
    assert get[3] & Flag.ODD_SIZE and rec["size"][3] == 1024
    assert get[2] & Flag.DUPLICATE and get[5] & Flag.DUPLICATE
    assert get[6] == Flag.FOREIGN and get[8] == Flag.FOREIGN
    assert get[7] == Flag.GAP and rec["split"][7] == 26 + 12
    assert get[4] & Flag.GAP and rec["gap"][4] == 10 + 12
    assert np.array_equal(track.payload(2), d[2, :256])
    data, errors = mfm.best_sectors([track], CYL, SIDE)
    assert np.array_equal(data[4], d[2, :256]) and (data[5] == 0).all()
    assert (errors[4:8] == E.OK).all()
    with pytest.raises(ValueError):
        mfm.encode_track([SectorSpec(0, 0, 1, 2, np.zeros(10, np.uint8))])
    with pytest.raises(ValueError):
        mfm.encode_track(_specs() * 2)


def test_data_window():
    specs = _specs()
    specs[0].gap2 = 50
    rec = _decode(specs).sectors
    assert rec["error"][0] == E.DATA_NOT_FOUND and rec["error"][1] == E.HEADER_NOT_FOUND


def test_truncated_read_prefers_whole_revolution():
    data, _ = mfm.encode_track(_specs())
    cut = mfm.decode_track(data[:3000], cylinder=CYL, side=SIDE)
    last = cut.sectors[-1]
    assert last["flags"] & Flag.TRUNCATED and not last["data_ok"]
    other = _data(1)
    whole = _decode(mfm.standard_layout(CYL, SIDE, other))
    got, errors = mfm.best_sectors([cut, whole], CYL, SIDE)
    r = int(last["r"]) - 1
    assert np.array_equal(got.reshape(10, 512)[r], other[r])
    assert np.array_equal(got.reshape(10, 512)[0], _data()[0])
    assert (errors == E.OK).all()


def test_rle_tokens():
    dr = np.array([1, 2, 3, 3, 3, 3, 4, 4, 5] + [9] * 300, np.uint8)
    image = mfm.rle(dr)
    assert list(image) == [129, 1, 2, 4, 3, 131, 4, 4, 5, 9, 0]
    back = mfm.unrle(image)
    assert np.array_equal(back, dr[:10])
    long = np.arange(300).astype(np.uint8) % 7
    assert np.array_equal(
        mfm.unrle(mfm.rle(np.append(long, 0x4E))), np.append(long, 0x4E)
    )
    for bad in ([], [1, 0xF7]):
        with pytest.raises(ValueError):
            mfm.rle(bad)


def test_write_track_semantics():
    dr = np.array(
        [0x4E] * 4 + [0x00] * 2 + [0xF5] * 3 + [0xFE, 1, 0, 1, 2, 0xF7, 0xF6, 0x4E],
        np.uint8,
    )
    data, mark = mfm.wd_write_track(dr, 24)
    crc = crc16(np.array([0xFE, 1, 0, 1, 2], np.uint8), SYNC3)
    assert list(data[6:9]) == [0xA1] * 3 and mark[6:9].all()
    assert list(data[14:16]) == [crc >> 8, crc & 0xFF]
    assert data[16] == 0xC2 and mark[16] and mark.sum() == 4
    assert (data[17:] == 0x4E).all()


def test_plan_reproduces_layout():
    specs = _specs()
    specs[0].data = np.full(512, 0xE5, np.uint8)
    specs[1].data[:4] = [0xF5, 0xF6, 0xF7, 0xF5]
    specs[2].bad_data_crc = True
    specs[2].data &= 0x7F
    specs[3].deleted = True
    plan = mfm.plan_track(specs)
    assert [w[1] for w in plan.writes] == [2, 4, 5, 6, 7, 8, 9, 10]
    assert plan.writes[1][3] and len(plan.image) < 2 * 512
    data, mark = mfm.wd_write_track(mfm.unrle(plan.image))
    for c, r, payload, deleted in plan.writes:
        rec = mfm.decode_track(data, mark).sectors
        pos = int(rec["id_pos"][rec["r"] == r][0]) + mfm.ID_BYTES
        data, mark = mfm.wd_write_sector(data, mark, pos, payload, deleted)
        assert c == CYL
    assert np.array_equal(data, plan.data) and np.array_equal(mark, plan.mark)
    direct, _ = mfm.encode_track(specs)
    differ = np.flatnonzero(direct != plan.data)
    assert (plan.data[differ] == mfm.LOGIC_ONES).all() and len(differ) == 8
    track = mfm.decode_track(plan.data, plan.mark, True, CYL, SIDE)
    assert list(track.sectors["error"]) == [1, 1, 5] + [1] * 7
    assert track.sectors["flags"][3] == Flag.DELETED


def test_plan_rejects_unwritable():
    specs = _specs()
    specs[2].bad_data_crc = True
    specs[2].data[0] = 0xF7
    with pytest.raises(ValueError):
        mfm.plan_track(specs)
    specs = _specs()
    specs[0].r = 0xF5
    with pytest.raises(ValueError):
        mfm.plan_track(specs)
    specs = _specs()
    specs[4].bad_id_crc = True
    specs[4].data &= 0x7F
    plan = mfm.plan_track(specs)
    assert 5 not in [w[1] for w in plan.writes]
    crc_bytes = plan.data[
        mfm.decode_track(plan.data, plan.mark).sectors["id_pos"][4] + 5 :
    ][:2]
    assert not np.isin(crc_bytes, mfm.WRITE_CODES).any()


def test_bad_crc_avoids_write_codes():
    for crc in (0x0809, 0xF6F7, 0x0AF5, 0x1234):
        out = mfm._bad_crc(crc)  # pylint: disable=protected-access
        assert not np.isin(out, mfm.WRITE_CODES).any()
        assert (int(out[0]) << 8 | int(out[1])) != crc


def test_physical_logical_mapping():
    track, sector = np.meshgrid(np.arange(1, 81), np.arange(40), indexing="ij")
    cyl, side, r, half = mfm.physical(track, sector)
    assert (mfm.physical(1, 0)) == (0, 0, 1, 0)
    assert tuple(int(v) for v in mfm.physical(80, 39)) == (79, 1, 10, 1)
    assert tuple(int(v) for v in mfm.physical(40, 20)) == (39, 1, 1, 0)
    back = mfm.logical(cyl, side, r, half)
    assert np.array_equal(back[0], track) and np.array_equal(back[1], sector)
    assert mfm.head_side(0) == 1 and mfm.head_side(1) == 0
    for bad in ((0, 0), (81, 0), (1, 40), (1, -1)):
        with pytest.raises(ValueError):
            mfm.physical(*bad)


def test_decode_ids_and_reads():
    ids = np.array([[CYL, SIDE, r, 2, 0, 0] for r in (1, 2, 2, 12)], np.uint8)
    status = np.array([0, mfm.ST_CRC, 0, 0], np.uint8)
    us = 1000.0 + mfm.BYTE_US * np.array([50, 659, 1268, 1877])
    track = mfm.decode_ids(ids, status, us, [1000.0, 201000.0], CYL, SIDE)
    rec = track.sectors
    assert list(rec["id_pos"]) == [50, 659, 1268, 1877]
    assert list(rec["error"]) == [E.OK, E.HEADER_CHECKSUM, E.OK, E.OK]
    assert rec["flags"][3] == Flag.FOREIGN and rec["flags"][2] == 0
    payload = _data()[0]
    reads = [
        (CYL, 1, payload, 0),
        (CYL, 2, payload, mfm.ST_CRC),
        (CYL, 3, payload, mfm.ST_RNF),
        (CYL, 4, payload, mfm.ST_RNF | mfm.ST_CRC),
        (CYL, 5, payload, mfm.ST_DELETED),
        (CYL, 6, payload[:256], mfm.ST_LOST),
    ]
    track = mfm.decode_reads(reads, SIDE)
    assert list(track.sectors["error"]) == [1, 5, 2, 9, 1, 9]
    assert (
        track.sectors["flags"][4] == Flag.DELETED
        and track.sectors["flags"][5] == Flag.ODD_SIZE
    )
    data, errors = mfm.best_sectors([track], CYL, SIDE)
    assert np.array_equal(data[0], payload[:256]) and np.array_equal(
        data[3], payload[256:]
    )
    assert list(errors[::2]) == [1, 5, 2, 9, 1, 9, 2, 2, 2, 2]
    with pytest.raises(ValueError):
        mfm.size_code(300)


def test_compare_revolutions_weak():
    specs = _specs()
    revs = []
    for k in range(3):
        if k == 2:
            specs[4].data = specs[4].data.copy()
            specs[4].data[100:103] ^= 0xFF
        data, _ = mfm.encode_track(specs)
        if k == 1:
            data[47 + 3 * PITCH : 47 + 3 * PITCH + 2] = 0
        revs.append(mfm.decode_track(data, cylinder=CYL, side=SIDE))
    agree, weak = mfm.compare_revolutions(revs)
    stable = {int(a["r"]): bool(a["stable"]) for a in agree}
    assert stable == {r: r not in (4, 5) for r in range(1, 11)}
    five = agree[agree["r"] == 5][0]
    assert five["variants"] == 2 and five["ok"] == 3 and five["reads"] == 3
    assert agree[agree["r"] == 4][0]["reads"] == 2
    assert len(weak) == 1 and weak[0]["rev"] == 2
    assert (weak[0]["start"], weak[0]["end"]) == (100, 103)
    assert list(mfm.runs([0, 1, 1, 0, 1])[0]) == [1, 4]
