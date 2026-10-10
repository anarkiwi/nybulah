"""ramcheck: RAM captures against a stream of the same track, and the comparison."""

import contextlib
import dataclasses
import functools
import json
import pathlib

import numpy as np
import pytest
from test_stream import RAM_PASS_US, rig

from nybulah import cli, ramcheck
from nybulah.formats import D64, d64_to_g64
from nybulah.nibbler import HOME_HALFTRACK, Capture, Nibbler, TrackError
from nybulah.simdisk import SENSOR_EDGES, Media, disk_drive
from nybulah.simhost import SimCBM, SimMonitor

HW = pathlib.Path(__file__).parent / "data" / "hw"
S4 = HW / "s4-1571"


def patch(monkeypatch, model, mon):
    monkeypatch.setattr(ramcheck, "identify_model", lambda cbm, dev: model)
    monkeypatch.setattr(
        ramcheck, "Monitor", lambda cbm, dev, proto: contextlib.nullcontext(mon)
    )
    monkeypatch.setattr(
        ramcheck,
        "Nibbler",
        functools.partial(Nibbler, stepms=1, spinup_s=0, sleep=lambda s: None),
    )


def test_1571_ram_captures_match_the_stream(monkeypatch, capsys, g64, tmp_path):
    cbm, mech, nib = rig(Media.from_g64(g64), halftrack=36, timeout_us=RAM_PASS_US)
    patch(monkeypatch, "1571", nib.mon)
    argv = ["ramcheck", "--dev", "9", "--halftracks", "36", "--repeats", "1"]
    out = cli.main(argv + ["--settle-ms", "1", "--save", str(tmp_path)], cbm)
    assert json.loads(capsys.readouterr().out) == json.loads(json.dumps(out))
    assert out["streaming"] and out["located"] == 2
    stream, ram = out["tracks"][36]
    assert stream["name"] == "stream-h36.npz" and stream["against"] == []
    assert ram["sectors_ok"] == stream["sectors_ok"] == 19
    (vs,) = ram["against"]
    assert vs["reference"] == "stream-h36.npz" and vs["sectors"] == 19
    assert vs["differ"] == vs["extra_syncs"] == vs["missing_syncs"] == 0
    assert Capture.load(tmp_path / "ram-h36-0.npz").tb is not None
    assert mech.halftrack == 36 and mech.bumps == mech.inner_stops == 0
    with pytest.raises(ValueError, match="34 outward steps"):
        cli.main(argv + ["--max-steps", "33"], cbm)


def test_1541_ram_captures_against_a_reference(monkeypatch):
    image = D64(np.zeros((683, 256), np.uint8))
    drive = disk_drive("1541", Media.from_g64(d64_to_g64(image, progress=False)), 10)
    drive.write(0x22, 18)
    patch(monkeypatch, "1541", SimMonitor(drive))
    ref = HW / "read-s0-t18-0.npz"
    argv = ["ramcheck", "--dev", "10", "--transport", "s1", "--halftracks", "36"]
    out = cli.main(argv + ["--repeats", "2", "--reference", str(ref)], SimCBM(drive))
    assert not out["streaming"] and out["located"] == 36
    for cap in out["tracks"][36]:
        assert cap["sectors_ok"] == 19 and cap["against"][0]["reference"] == str(ref)
        assert cap["against"][0]["sectors"] == 19


@dataclasses.dataclass
class Bytes:
    """The capture fields compare reads."""

    data: np.ndarray
    positions: np.ndarray
    sync_bits: np.ndarray
    valid_bytes: int
    start: str = "sync"


def test_compare_separates_byte_faults_from_extra_syncs():
    cap = Capture.load(HW / "read-s0-t18-0.npz")
    ref = Bytes(cap.data, cap.positions, cap.sync_bits, cap.valid_bytes)
    _, heads = ramcheck.headers(ref, 18)
    assert ramcheck.against(ref, 18, "self", ref)["rows"] == []
    a, b = heads[3], heads[7]
    data = ref.data.copy()
    data[a + 12] ^= 0x10
    at = np.searchsorted(ref.positions, b + 30)
    fault = Bytes(
        data,
        np.insert(ref.positions, at, b + 30),
        np.insert(ref.sync_bits, at, 10),
        ref.valid_bytes,
    )
    rows = {r["sector"]: r for r in ramcheck.compare(fault, ref, 18)}
    assert rows[3]["differ"] == 1 and rows[3]["first_diff"] == 12
    assert rows[3]["extra_syncs"] == [] and rows[3]["missing_syncs"] == []
    assert rows[7]["differ"] == 0 and rows[7]["extra_syncs"] == [[30, 10]]
    assert sum(r["differ"] + len(r["extra_syncs"]) for r in rows.values()) == 2
    swapped = ramcheck.against(ref, 18, "fault", fault)
    assert swapped["missing_syncs"] == 1 and swapped["extra_syncs"] == 0


@pytest.mark.parametrize("halftrack", [36, 50])
@pytest.mark.parametrize("repeat", range(3))
def test_1571_ram_captures_add_no_syncs_to_the_stream(halftrack, repeat):
    """Real 1571 RAM captures of a blank disk, with the stream of the same track:
    the bytes agree, so every sync the merge finds is one the stream has."""
    ram = Capture.load(S4 / f"ram-h{halftrack}-{repeat}.npz")
    stream = Capture.load(S4 / f"stream-h{halftrack}.npz")
    out = ramcheck.digest(ram, halftrack // 2, [("stream", stream)])
    (vs,) = out["against"]
    assert vs["sectors"] == out["sectors_ok"] == (19 if halftrack == 36 else 18)
    assert vs["extra_syncs"] == vs["missing_syncs"] == 0 and vs["differ"] <= 1


def test_1571_locate_places_the_head_from_headers_after_a_reset(make_rig, g64):
    drive, nib = make_rig("1571", Media.from_g64(g64), halftrack=40)
    drive.write(0x22, 0)
    with pytest.raises(ValueError, match="38 outward steps"):
        ramcheck.locate(nib, 37)
    assert ramcheck.locate(nib, 38) == HOME_HALFTRACK == drive.mech.halftrack
    assert drive.mech.bumps == drive.mech.inner_stops == 0


def test_1571_locate_searches_out_to_the_sensor(make_rig):
    drive, nib = make_rig(
        "1571", halftrack=SENSOR_EDGES[1] + 4, sensor_edge=SENSOR_EDGES[1]
    )
    drive.write(0x22, 0)
    with pytest.raises(TrackError, match="nothing places"):
        ramcheck.locate(nib, 40)
    assert ramcheck.locate(nib, 40, 6) == HOME_HALFTRACK == drive.mech.halftrack
    assert drive.mech.bumps == drive.mech.inner_stops == 0


def test_1571_search_steps_count_towards_max_steps(make_rig, g64):
    """A search found headers 4 steps out on halftrack 70: homing from there is 68
    more outward steps, refused over max_steps with the head left where it searched."""
    drive, nib = make_rig("1571", Media.from_g64(g64), halftrack=74)
    drive.write(0x22, 0)
    with pytest.raises(ValueError, match="search of 8 steps is over 7"):
        ramcheck.locate(nib, 7, 8)
    assert drive.mech.halftrack == 74
    with pytest.raises(ValueError, match="72 outward steps, over 71"):
        ramcheck.locate(nib, 71, 8)
    assert drive.mech.halftrack == 70 and nib.halftrack is None
    drive.write(0x22, 0)
    assert ramcheck.locate(nib, 68, 8) == HOME_HALFTRACK == drive.mech.halftrack
    assert drive.mech.bumps == drive.mech.inner_stops == 0
