import json
import pathlib

import numpy as np
import pytest
from test_survey import ERROR_TRACK, KILLER_HT, NOSYNC_HT, _nib_disk

from nybulah import survey
from nybulah.analysis import regions
from nybulah.analysis.diskmap import (
    FAULT_MARK,
    KIND_CLASS,
    REGION_DTYPE,
    UNSTABLE_MARK,
    Cls,
    Kind,
    WIDE,
    Stability,
    align,
    disk_map,
    load_thresholds,
)
from nybulah.analysis.regions import ILLEGAL_ZEROS, parse
from nybulah.analysis.synth import synthetic_disk
from nybulah.formats import loads, to_g64, write_g64, write_nib

DATA = pathlib.Path(__file__).parent / "data"
REVS = 4
DECODE_ERRORS = [
    Kind.HDR_CHECKSUM,
    Kind.HDR_GCR,
    Kind.HDR_MARK,
    Kind.DATA_GCR,
    Kind.DATA_ORPHAN,
    Kind.BLOCK_OTHER,
]


@pytest.fixture(name="synthetic", scope="module")
def synthetic_fixture():
    image, truth = synthetic_disk(REVS)
    return image, truth, disk_map(image, bins=256)


def _found(dmap, key, kind):
    r = dmap.regions
    return r[(r["track"] == key) & (r["kind"] == kind)]


def test_injected_anomalies_recovered(synthetic):
    _, truth, dmap = synthetic
    assert dmap.aligned.all()
    for key, name, start, end, rev in truth:
        found = _found(dmap, key, Kind[name])
        once = rev is not None or WIDE[Kind[name]]
        assert len(found) == (1 if once else REVS), name
        assert (found["cls"] == KIND_CLASS[Kind[name]]).all()
        if rev is not None:
            assert found["rev"].tolist() == [rev]
        if name == "NOFLUX_SPAN":
            slack = ILLEGAL_ZEROS - 1
            assert (found["start_bit"] >= start - slack).all()
            assert (found["end_bit"] <= end + slack).all()
        else:
            assert (found["start_bit"] <= start).all() and (
                found["end_bit"] >= end
            ).all()
            assert (found["end_bit"] - found["start_bit"] <= end - start + 1).all()


def test_nothing_else_is_anomalous(synthetic):
    _, truth, dmap = synthetic
    r = dmap.regions
    odd = r[(r["cls"] > Cls.STANDARD)]
    assert set(odd["track"].tolist()) == {t[0] for t in truth}


def test_stability(synthetic):
    _, truth, dmap = synthetic
    for key, name, _, _, rev in truth:
        st = _found(dmap, key, Kind[name])["stability"]
        if name == "NOFLUX_SPAN":
            assert (st == Stability.UNSTABLE).all()
        elif rev is not None:
            assert (st == Stability.TRANSIENT).all()
        else:
            assert (st == Stability.INTRINSIC).all()
    weak = next(t for t in truth if t[1] == "NOFLUX_SPAN")
    dis = _found(dmap, weak[0], Kind.DISAGREE)
    assert sorted(dis["rev"].tolist()) == list(range(1, REVS))
    assert (dis["stability"] == Stability.UNSTABLE).all()
    assert (dis["start_bit"] >= weak[2]).all() and (dis["end_bit"] <= weak[3]).all()
    slip = next(t for t in truth if t[4] is not None)
    for kind in (Kind.CAPTURE_FAULT, Kind.DISAGREE):
        found = _found(dmap, slip[0], kind)
        assert found["rev"].tolist() == [slip[4]]
        assert (found["stability"] == Stability.TRANSIENT).all()
        assert slip[2] <= found["start_bit"][0] < slip[3]


def test_single_revolution_is_unconfirmed():
    image, truth = synthetic_disk(1)
    dmap = disk_map(image, bins=64)
    r = dmap.regions
    assert (dmap.revs == 1).all() and not (r["kind"] == Kind.DISAGREE).any()
    local = r[~WIDE[r["kind"]]]
    read = local["stability"] == Stability.TRANSIENT
    assert set(local["track"][read].tolist()) == {
        t[0] for t in truth if t[4] is not None
    }
    assert (local["stability"][~read] == Stability.UNCONFIRMED).all()
    assert {t[1] for t in truth if t[4] is None} <= {Kind(k).name for k in r["kind"]}


def test_raster(synthetic):
    _, truth, dmap = synthetic
    assert dmap.grid.shape == dmap.marks.shape == (len(dmap.keys), 256)
    row = {int(k): i for i, k in enumerate(dmap.keys)}
    assert (dmap.grid[row[62]] == Cls.SYNC).all()
    assert (dmap.grid[row[40]] == Cls.DENSITY).all()
    weak = next(t for t in truth if t[1] == "NOFLUX_SPAN")
    n = dmap.length[row[weak[0]]]
    inside = slice(weak[2] * 256 // n + 1, weak[3] * 256 // n)
    assert (dmap.grid[row[weak[0]], inside] == Cls.WEAK).all()
    assert (dmap.marks[row[weak[0]], inside] & UNSTABLE_MARK).all()
    slip = next(t for t in truth if t[4] is not None)
    assert (dmap.marks[row[slip[0]]] & FAULT_MARK).any()
    assert (dmap.grid[row[slip[0]]] == Cls.STANDARD).all()
    first, _ = dmap.raster(rev=0)
    assert not (first[row[weak[0]]] == Cls.WEAK).all()
    assert dmap.raster(bins=32)[0].shape == (len(dmap.keys), 32)
    assert len(dmap.select(REVS + 5)) == len(dmap.select(REVS - 1))


def test_hw_blank_disk_shows_only_read_errors():
    image = survey.DiskImage("capture", survey.load_captures(DATA / "hw"))
    dmap = disk_map(image, bins=128)
    r = dmap.regions
    odd = r[(r["cls"] > Cls.STANDARD) & (r["cls"] != Cls.FAULT)]
    assert not (odd["stability"] == Stability.INTRINSIC).any()
    slips = r[r["kind"] == Kind.CAPTURE_FAULT]
    assert (slips["stability"] == Stability.TRANSIENT).all() and (
        slips["detail"] != 0
    ).all()
    once = odd[odd["stability"] == Stability.UNCONFIRMED]
    assert np.isin(once["kind"], DECODE_ERRORS).all()
    twice = dmap.keys[dmap.revs > 1]
    shaky = odd[odd["stability"] == Stability.UNSTABLE]
    assert np.isin(shaky["track"], twice).all()
    assert np.isin(shaky["kind"], DECODE_ERRORS + [Kind.DISAGREE]).all()
    for reg in odd[odd["stability"] == Stability.TRANSIENT]:
        mine = slips[(slips["track"] == reg["track"]) & (slips["rev"] == reg["rev"])]
        reach = reg["start_bit"] - (
            reg["detail"] if reg["kind"] == Kind.DATA_ORPHAN else 0
        )
        assert ((mine["start_bit"] < reg["end_bit"]) & (mine["end_bit"] > reach)).any()
    assert set(np.unique(dmap.grid)) <= {Cls.STANDARD, Cls.HEADER, Cls.DATA, Cls.WEAK}


def test_nib_and_g64_images():
    nib = loads(write_nib(_nib_disk()))
    for image in (nib, loads(write_g64(to_g64(nib)))):
        dmap = disk_map(image, bins=64)
        assert not dmap.aligned.any()
        kinds = {
            int(k): {Kind(x) for x in dmap.regions["kind"][dmap.regions["track"] == k]}
            for k in dmap.keys
        }
        assert Kind.KILLER in kinds[KILLER_HT] and Kind.NO_SYNC in kinds[NOSYNC_HT]
        assert Kind.DATA_CHECKSUM in kinds[2 * ERROR_TRACK]
        assert {Kind.FAT_TRACK} <= kinds[68] and Kind.HDR_TRACK in kinds[70]
        assert kinds[69] == {Kind.EMPTY}
    assert kinds[2] == {Kind.SYNC, Kind.HEADER, Kind.DATA, Kind.GAP}
    assert not (dmap.regions["kind"] == Kind.CAPTURE_FAULT).any()
    first = dmap.regions[
        (dmap.regions["track"] == 2) & (dmap.regions["kind"] == Kind.SYNC)
    ]
    assert first["start_bit"].min() == 0


def test_extra_captures_add_revolutions():
    image, _ = synthetic_disk(1)
    again, _ = synthetic_disk(2, rng=5)
    dmap = disk_map(image, captures=[again], bins=32)
    assert (dmap.revs[dmap.keys != 62] == 3).all()


def test_thresholds_load(tmp_path):
    thr = load_thresholds()
    assert set(thr) == {"linear", "circular"}
    assert thr["circular"]["fill_classes"] and np.isfinite(thr["linear"]["sync_long"])
    (tmp_path / "summary.json").write_text(json.dumps({"thresholds": thr}))
    assert load_thresholds(tmp_path / "summary.json") == thr


def test_align_maps_rotation_and_slip():
    rng = np.random.default_rng(3)
    image, _ = synthetic_disk(1, rng=rng)
    bits = image.tracks[18][0].bits
    ref = parse(bits)
    other = np.delete(np.roll(bits, -1000), 5000)
    found = align(ref, parse(other))
    assert found.to_ref(np.array([0, 3000])).tolist() == [1000, 4000]
    assert found.to_ref(np.array([6000]))[0] == 7001 % len(bits)
    assert len(found.miss) and found.miss.min() >= 5000 + 1000 - regions.GROUP_BITS


def test_parse_edge_tracks():
    killer = parse(np.ones(800, np.uint8))
    assert killer.killer and len(killer.gaps.length) == 0
    blank = parse(np.tile(np.array([0, 1, 0, 1], np.uint8), 200))
    assert not blank.killer and blank.gaps.length.tolist() == [800]
    assert blank.gap_after.tolist() == [regions.Block.NONE]
    assert len(blank.gaps.bytes) == 100 and blank.gap_classes()[1].tolist() == [100]
    starts, ends, members = regions.chains(
        np.array([0, 10, 100]), np.array([5, 20, 110]), 8
    )
    assert starts.tolist() == [0, 100] and ends.tolist() == [20, 110]
    assert members.tolist() == [2, 1]
    assert np.zeros(0, REGION_DTYPE).dtype == REGION_DTYPE
