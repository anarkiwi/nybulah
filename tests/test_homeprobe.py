import contextlib
import functools
import json

import pytest

from nybulah import cli, homeprobe
from nybulah.nibbler import HOME_HALFTRACK, SENSOR_WALK, Nibbler, TrackError
from nybulah.simdisk import SENSOR_EDGES, SENSOR_STUCK_OFF, Media, disk_drive
from nybulah.simhost import SimCBM, SimMonitor


def probe_cli(monkeypatch, model="1571", dev=8, **kw):
    drive = disk_drive(model, Media(), dev, **kw)
    monkeypatch.setattr(
        homeprobe,
        "Monitor",
        lambda cbm, dev, proto: contextlib.nullcontext(SimMonitor(cbm.drive)),
    )
    monkeypatch.setattr(
        homeprobe,
        "Nibbler",
        functools.partial(
            Nibbler, stepms=1, settle_ms=1, spinup_s=0, sleep=lambda s: None
        ),
    )
    return drive, SimCBM(drive, dev=dev)


def test_dry_run_never_steps(monkeypatch, capsys):
    drive, cbm = probe_cli(monkeypatch, halftrack=36)
    out = cli.main(["homeprobe"], cbm)
    assert json.loads(capsys.readouterr().out) == out
    assert out["pa0"] == [1] * homeprobe.RAW_READS and not out["sensed"]
    assert out["phase"] == 2 and out["dos_track"] == 18 and out["estimate"] == 36
    assert (out["outward_steps"], out["inward_steps"]) == (34, 0)
    assert drive.mech.halftrack == 36 and "trace" not in out


@pytest.mark.parametrize("edge", SENSOR_EDGES)
@pytest.mark.parametrize("start", (1, 3, 9))
def test_step_finds_the_edge_and_returns(monkeypatch, start, edge):
    drive, cbm = probe_cli(monkeypatch, halftrack=start, sensor_edge=edge)
    out = cli.main(["homeprobe", "--step", "--max-steps", "7"], cbm)
    assert out["sensor_edge"] == edge and out["trace"][-1] == [HOME_HALFTRACK, 1, 0]
    assert drive.mech.bumps == 0 == drive.mech.inner_stops
    back = out["estimate"] or HOME_HALFTRACK
    assert drive.mech.halftrack == back + (back & 1) == 2 * drive.read(0x22)
    assert len(out["trace"]) <= out["outward_steps"] + out["inward_steps"] + 1
    assert out["inward_steps"] in (0, SENSOR_WALK)


def test_step_refusals(monkeypatch):
    drive, cbm = probe_cli(monkeypatch, halftrack=36)
    with pytest.raises(ValueError, match="34 outward steps"):
        cli.main(["homeprobe", "--step", "--max-steps", "33"], cbm)
    drive, cbm = probe_cli(monkeypatch, halftrack=36, sensor_edge=SENSOR_STUCK_OFF)
    with pytest.raises(TrackError, match="clear at halftrack 2"):
        cli.main(["homeprobe", "--step"], cbm)
    assert drive.mech.bumps == 0
    _, cbm = probe_cli(monkeypatch, "1541")
    with pytest.raises(ValueError, match="needs a 1571"):
        cli.main(["homeprobe"], cbm)
