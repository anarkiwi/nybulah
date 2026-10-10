import pytest

from nybulah.nibbler import (
    HOME_HALFTRACK,
    MAX_DOS_TRACK,
    MAX_HALFTRACK,
    SENSOR_WALK,
    Nibbler,
    TrackError,
)
from nybulah.simdisk import (
    HT_MAX,
    SENSOR_EDGES,
    SENSOR_STUCK_OFF,
    SENSOR_STUCK_ON,
    Media,
    disk_drive,
)
from nybulah.simhost import SimMonitor

STARTS = range(HT_MAX + 1)
STUCK = (SENSOR_STUCK_ON, SENSOR_STUCK_OFF)


def homed(make_rig, start, edge, dos=None):
    """(drive, nib, error, trace) after homing from start, DOS's track default start's."""
    drive, nib = make_rig("1571", halftrack=start, sensor_edge=edge)
    drive.write(0x22, start // 2 if dos is None else dos)
    trace = []
    try:
        nib.home(nib.estimate(headers=False), trace)
    except TrackError as e:
        return drive, nib, e, trace
    return drive, nib, None, trace


def untouched(drive):
    return drive.mech.bumps == 0 == drive.mech.inner_stops


@pytest.mark.parametrize("edge", SENSOR_EDGES)
@pytest.mark.parametrize("start", STARTS)
def test_homes_exactly_from_any_halftrack(make_rig, start, edge):
    drive, nib, error, trace = homed(make_rig, start, edge)
    assert error is None and untouched(drive)
    assert drive.mech.halftrack == HOME_HALFTRACK == nib.halftrack
    assert trace[-1] == (HOME_HALFTRACK, True, 0)
    assert max(x for x, on, _ in trace if on) <= edge
    assert min(x for x, on, _ in trace if not on) == edge + 1


@pytest.mark.parametrize("edge", STUCK)
@pytest.mark.parametrize("start", STARTS)
def test_stuck_sensor_errors_without_a_bump(make_rig, start, edge):
    drive, nib, error, _ = homed(make_rig, start, edge)
    assert error is not None and nib.halftrack is None and untouched(drive)


@pytest.mark.parametrize("edge", (*SENSOR_EDGES, SENSOR_STUCK_ON))
@pytest.mark.parametrize("start", range(0, HT_MAX + 1, 3))
@pytest.mark.parametrize("off", (-3, -2, -1, 1, 2, 3, 20))
def test_wrong_dos_track_never_bumps(make_rig, start, edge, off):
    dos = min(max(start // 2 + off, 1), MAX_DOS_TRACK)
    drive, nib, error, _ = homed(make_rig, start, edge, dos)
    assert untouched(drive)
    if error is None:
        assert drive.mech.halftrack == HOME_HALFTRACK == nib.halftrack


@pytest.mark.parametrize("edge", (*SENSOR_EDGES, *STUCK))
@pytest.mark.parametrize("start", STARTS)
def test_sensor_alone(make_rig, start, edge):
    drive, nib, error, trace = homed(make_rig, start, edge, 0)
    assert drive.mech.bumps == 0
    if edge == SENSOR_STUCK_ON:
        assert "still on" in str(error) and len(trace) == SENSOR_WALK + 1
        assert drive.mech.inner_stops == 0 or start > HT_MAX - SENSOR_WALK
    elif start <= edge:
        assert error is None and untouched(drive)
        assert drive.mech.halftrack == HOME_HALFTRACK == nib.halftrack
    else:
        assert "nothing places" in str(error) and drive.mech.halftrack == start


@pytest.mark.parametrize("start", range(HOME_HALFTRACK + 11, HT_MAX + 1, 5))
@pytest.mark.parametrize("off", (-8, -4, 0))
def test_dead_sensor_stops_at_the_estimate(make_rig, start, off):
    drive, nib = make_rig("1571", halftrack=start, sensor_edge=SENSOR_STUCK_OFF)
    estimate = start + off
    trace = []
    with pytest.raises(TrackError, match="clear at halftrack"):
        nib.home(estimate, trace)
    assert drive.mech.halftrack == start - (estimate - HOME_HALFTRACK)
    assert untouched(drive)
    assert len(trace) == estimate - HOME_HALFTRACK + 1


def test_close_leaves_dos_on_the_track(make_rig):
    drive, nib = make_rig("1571", halftrack=9, sensor_edge=SENSOR_EDGES[1])
    nib.locate()
    nib.seek(31)
    nib.close()
    assert drive.mech.halftrack == 32 and drive.read(0x22) == 16
    nib.open()
    assert nib.estimate(headers=False) == 32 and nib.locate() == HOME_HALFTRACK


def test_inconsistent_phase_is_refused(make_rig):
    drive, nib = make_rig("1571", halftrack=40)
    with pytest.raises(TrackError, match="phase"):
        nib.home(41)
    assert drive.mech.halftrack == 40 and nib.halftrack is None


def test_locate_homes_then_seeks(make_rig):
    drive, nib = make_rig("1571", halftrack=40, sensor_edge=SENSOR_EDGES[0])
    assert nib.locate() == HOME_HALFTRACK == drive.mech.halftrack
    nib.capture(MAX_HALFTRACK, start="now", timing="none")
    assert drive.mech.halftrack == MAX_HALFTRACK and untouched(drive)
    assert nib.sense() == (False, (MAX_HALFTRACK + 2) & 3)


def test_1571_is_never_bumped():
    drive = disk_drive("1571", Media(), halftrack=40, sensor_edge=SENSOR_STUCK_OFF)
    drive.write(0x22, 0)
    nib = Nibbler(SimMonitor(drive), "1571", 1, 1, 0, lambda s: None, allow_bump=True)
    nib.open()
    for call in (nib.bump, nib.locate):
        with pytest.raises(TrackError):
            call()
    assert untouched(drive) and drive.mech.halftrack == 40


def test_sense_needs_a_1571(make_rig):
    _, nib = make_rig("1541")
    with pytest.raises(ValueError):
        nib.sense()


def lost(make_rig, model, start, media=None, **kw):
    """(drive, nib) with the head on start and DOS's track unknown."""
    drive, nib = make_rig(model, media, halftrack=start, **kw)
    drive.write(0x22, 0)
    return drive, nib


def test_1541_search_finds_headers_outwards(make_rig, g64):
    drive, nib = lost(make_rig, "1541", 74, Media.from_g64(g64))
    with pytest.raises(TrackError, match="allow_bump"):
        nib.locate()
    assert drive.mech.halftrack == 74
    assert nib.locate(search=8) == 70 == drive.mech.halftrack == nib.halftrack
    assert untouched(drive)


@pytest.mark.parametrize("model", ["1541", "1571"])
def test_search_gives_up_after_its_bound(make_rig, model):
    drive, nib = lost(make_rig, model, 40)
    with pytest.raises(TrackError, match="within 5 outward"):
        nib.locate(search=5)
    assert drive.mech.halftrack == 35 and nib.halftrack is None
    assert untouched(drive)


@pytest.mark.parametrize("edge", SENSOR_EDGES)
def test_1571_search_finds_the_sensor_edge(make_rig, edge):
    drive, nib = lost(make_rig, "1571", edge + 5, sensor_edge=edge)
    with pytest.raises(TrackError, match="nothing places"):
        nib.locate()
    assert nib.locate(search=7) == HOME_HALFTRACK == drive.mech.halftrack
    assert untouched(drive)


def test_1571_search_finds_headers_before_the_sensor(make_rig, g64):
    drive, nib = lost(
        make_rig, "1571", 74, Media.from_g64(g64), sensor_edge=SENSOR_EDGES[0]
    )
    trace = []
    nib.home = lambda estimate, _home=nib.home: trace.append(estimate) or _home(
        estimate
    )
    assert nib.locate(search=8) == HOME_HALFTRACK == drive.mech.halftrack
    assert trace == [70] and untouched(drive)
