import json

import numpy as np
import pytest

from nybulah import cli, viz
from nybulah.analysis import mfm
from nybulah.analysis.diskmap import Cls, Kind, Stability
from nybulah.analysis.mfm import SectorSpec
from nybulah.analysis.mfmmap import MFM_KINDS, map_key, mfm_disk_map, track_regions
from nybulah.formats import D81, MfmCapture, MfmDisk, load_captures, save_captures
from nybulah.formats import d81, imd, mfmcap

CYL, HEAD = 3, 0
SIDE = mfm.head_side(HEAD)


def _layout():
    specs = mfm.standard_layout(CYL, SIDE, np.arange(5120).reshape(10, 512) % 251)
    specs[0].bad_id_crc = True
    specs[1].bad_data_crc = True
    specs[2].deleted = True
    specs[3] = SectorSpec(CYL, SIDE, 4, 1, np.zeros(256, np.uint8))
    specs[4].has_id = False
    specs[5].data = None
    specs[6].r = 2
    specs[7].r = 12
    specs[8].gap3 = 60
    specs[9].c = CYL + 2
    return specs


def _kinds(regs, start):
    return {int(r["kind"]) for r in regs if r["start_bit"] == start}


def test_region_kinds():
    data, _ = mfm.encode_track(_layout())
    track = mfm.decode_track(data, cylinder=CYL, side=SIDE)
    regs = track_regions(track, CYL, SIDE)
    rec = track.sectors
    assert len(rec) == 10
    ids, dat = rec["id_pos"], rec["data_pos"]
    assert _kinds(regs, ids[0]) == {Kind.HDR_CHECKSUM}
    assert _kinds(regs, dat[1]) == {Kind.DATA_CHECKSUM}
    assert _kinds(regs, dat[2]) == {Kind.DATA_DELETED}
    assert _kinds(regs, dat[3]) == {Kind.DATA_SIZE}
    assert ids[4] < 0 and _kinds(regs, dat[4]) == {Kind.DATA_ORPHAN}
    assert _kinds(regs, ids[5]) == {Kind.ID_NO_DATA}
    assert _kinds(regs, ids[1]) == _kinds(regs, ids[6]) == {Kind.HDR_DUPLICATE}
    assert _kinds(regs, ids[7]) == {Kind.HDR_SECTOR}
    assert _kinds(regs, ids[9]) == {Kind.HDR_TRACK}
    assert _kinds(regs, dat[8] + 515) == {Kind.GAP_LONG}
    assert (regs["kind"] == Kind.GAP).sum() > 0 and (
        regs["kind"] == Kind.SYNC
    ).sum() == 18
    assert set(MFM_KINDS.values()) <= set(Kind)


def test_unformatted_track():
    track = mfm.decode_track(np.full(6250, 0x4E, np.uint8))
    regs = track_regions(track, 0, 0)
    assert len(regs) == 1 and regs["kind"][0] == Kind.UNFORMATTED


def _revs(count=3, weak_rev=2):
    specs = mfm.standard_layout(CYL, SIDE, np.ones((10, 512), np.uint8))
    out = []
    for k in range(count):
        if k == weak_rev:
            specs[6].data = specs[6].data.copy()
            specs[6].data[10:20] = 0x77
        out.append(mfm.encode_track(specs)[0])
    return out


def test_weak_sector_is_unstable():
    revs = [mfm.decode_track(r, cylinder=CYL, side=SIDE) for r in _revs()]
    dmap = mfm_disk_map({(CYL, HEAD): revs}, bins=256)
    assert list(dmap.keys) == [map_key(CYL, SIDE)] and dmap.revs[0] == 3
    assert dmap.length[0] == 8 * 6250 and dmap.aligned.all()
    weak = dmap.regions[dmap.regions["kind"] == Kind.DISAGREE]
    pos = int(revs[2].sectors["data_pos"][6]) + 1 + 10
    assert len(weak) == 1 and weak["start_bit"][0] == 8 * pos and weak["rev"][0] == 2
    assert weak["stability"][0] == Stability.UNSTABLE
    assert (dmap.grid == Cls.WEAK).any() and dmap.marks.any()
    standard = dmap.regions[dmap.regions["kind"] == Kind.HEADER]
    assert (standard["stability"] == Stability.INTRINSIC).all()


def test_flags_and_viz(tmp_path):
    data, _ = mfm.encode_track(_layout())
    dmap = mfm_disk_map(
        {(CYL, HEAD): [mfm.decode_track(data, cylinder=CYL, side=SIDE)]}
    )
    lines = viz.terminal_strip(dmap, 64)
    assert lines[0].startswith("   4' |") and "w" not in lines[0].lower()
    viz.save(dmap, tmp_path / "m.svg")
    assert "hdr_checksum" in (tmp_path / "m.svg").read_text()
    assert len(mfm_disk_map({}).keys) == 0


def test_capture_save_load(tmp_path):
    revs = _revs()
    cap = MfmCapture(
        "track",
        CYL,
        HEAD,
        side_select=SIDE,
        data=np.concatenate(revs),
        rev_offsets=np.arange(4) * 6250,
        rev_start_us=[0, 200000, 400000],
        rev_end_us=[200000, 400000, 600000],
        rev_status=[0, 0, 0],
        meta={"rpm": 300.0},
    )
    ids = MfmCapture(
        "ids",
        CYL,
        HEAD,
        ids=[[CYL, SIDE, r, 2, 0, 0] for r in range(1, 11)],
        id_status=np.zeros(10),
        id_us=np.arange(10) * 19488.0 + 1600,
        index_us=[0.0, 200000.0],
    )
    save_captures(tmp_path / "a.npz", [cap])
    save_captures(tmp_path / "b.npz", [ids])
    np.savez(tmp_path / "other.npz", x=np.zeros(3))
    back = load_captures(tmp_path)
    assert len(back) == 2 and back[0].meta == {"rpm": 300.0}
    assert np.array_equal(back[0].data, cap.data) and back[0].side_select == SIDE
    assert back[1].ids.shape == (10, 6) and back[1].kind == "ids"
    assert load_captures(tmp_path / "other.npz") is None
    assert load_captures(tmp_path / "a.txt") is None
    disk = mfmcap.load_disk(tmp_path / "a.npz")
    assert len(disk.tracks[(CYL, HEAD)]) == 3 and not disk.ids
    disk = MfmDisk.from_captures(back)
    assert list(disk.ids[(CYL, HEAD)][0].sectors["id_pos"][:2]) == [50, 659]
    assert len(disk.decodes()[(CYL, HEAD)]) == 4


@pytest.mark.parametrize(
    "bad",
    [
        {"kind": "flux"},
        {"side_select": SIDE ^ 1},
        {"bogus": [1]},
    ],
)
def test_capture_validation(bad):
    args = {"kind": "track", "cylinder": 0, "head": 0} | bad
    with pytest.raises((ValueError, TypeError)):
        MfmCapture(**args)
    with pytest.raises(AttributeError):
        _ = MfmCapture("track", 0, 0).nothing


def test_future_version_rejected(tmp_path):
    np.savez(tmp_path / "v.npz", nybulah_mfm=np.array([mfmcap.VERSION + 1, 0]))
    with pytest.raises(ValueError):
        load_captures(tmp_path / "v.npz")


@pytest.fixture(name="d81_path")
def d81_path_fixture(tmp_path):
    image = D81(np.random.default_rng(2).integers(0, 256, (3200, 256), np.uint8))
    image.errors[40:42] = 5
    path = tmp_path / "disk.d81"
    path.write_bytes(d81.write_d81(image))
    return path


def test_cli_d81(d81_path, tmp_path, capsys):
    out = cli.main(["info", str(d81_path)])
    assert out["kind"] == "d81" and len(out["tracks"]) == 160
    assert sum(t["errors"] for t in out["tracks"]) == 2 and "d81" in out
    out = cli.main(["info", str(d81_path), "--map", "--width", "16"])
    assert len(out["map"]) == 160
    target = tmp_path / "disk.imd"
    out = cli.main(["convert", str(d81_path), str(target)])
    assert out["target"] == str(target) and target.read_bytes().startswith(b"IMD ")
    again = tmp_path / "again.d81"
    cli.main(["convert", str(target), str(again)])
    assert again.read_bytes() == d81_path.read_bytes()
    caps = tmp_path / "caps.npz"
    cli.main(["convert", str(d81_path), str(caps)])
    out = cli.main(
        [
            "map",
            str(caps),
            "-o",
            str(tmp_path / "m.html"),
            "--bins",
            "64",
            "--captures",
            str(caps),
        ]
    )
    assert out["tracks"] == 160 and out["anomalies"] >= 1
    with pytest.raises(ValueError):
        cli.main(["convert", str(d81_path), str(tmp_path / "x.g64")])
    capsys.readouterr()


def test_imd_loads(tmp_path):
    raw = imd.write_imd(imd.Imd(b"IMD 1.18: x", []))
    (tmp_path / "e.imd").write_bytes(raw)
    disk = mfmcap.load_disk(tmp_path / "e.imd")
    assert disk.kind == "imd" and not disk.tracks
    (tmp_path / "junk.bin").write_bytes(b"x" * 10)
    assert mfmcap.load_disk(tmp_path / "junk.bin") is None
    assert mfmcap.load_disk(tmp_path) is None
    assert json.loads(json.dumps(MfmDisk("capture", []).decodes())) == {}
