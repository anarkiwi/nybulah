"""Captures of a freshly formatted disk on a 1571 (tests/data/hw)."""

import pathlib

import numpy as np
import pytest

from nybulah.analysis.capture import segments
from nybulah.analysis.cycle import TrackKind, extract_revolution, find_cycle, lag_window
from nybulah.analysis.faults import capture_faults
from nybulah.analysis.gcr import sectors_per_track, speed_zone
from nybulah.analysis.sector import SectorError, decode_track, header_tracks
from nybulah.nibbler import Capture

DATA = pathlib.Path(__file__).parent / "data" / "hw"
PATHS = sorted(DATA.glob("read-*.npz"))


@pytest.fixture(name="cycles", scope="module")
def cycles_fixture():
    caps = [Capture.load(p) for p in PATHS]
    return [(cap, find_cycle(cap)) for cap in caps]


def test_every_track_has_a_physical_period(cycles):
    assert len(cycles) == 36
    for cap, cycle in cycles:
        lo, hi = lag_window(cap.density)
        assert cycle.kind == TrackKind.FORMATTED and lo <= cycle.length <= hi
        assert 0 < cycle.segments <= 2 * sectors_per_track(cap.halftrack // 2)


def test_periods_agree_within_a_zone(cycles):
    for zone in range(4):
        zoned = [c for cap, c in cycles if cap.density == zone]
        lengths = np.array([c.length for c in zoned])
        bound = 2 * 3 * max(c.segments for c in zoned)
        assert lengths.max() - lengths.min() <= bound


def test_headers_and_sectors(cycles):
    repeats = 0
    for cap, cycle in cycles:
        track = cap.halftrack // 2
        assert cap.density == speed_zone(track)
        assert set(header_tracks(segments(cap).bits).tolist()) == {track}
        out = decode_track(cap, track)
        assert (out.copies[out.errors != SectorError.HEADER_NOT_FOUND] >= 1).all()
        repeats += int((out.copies > 1).sum())
        rev = extract_revolution(cap, cycle)
        assert abs(len(rev) - cycle.length) <= 3 * cycle.segments
        single = decode_track(rev, track)
        assert (out.errors == SectorError.OK).sum() >= (
            single.errors == SectorError.OK
        ).sum()
    assert repeats > len(cycles)


def test_faults_cover_bad_gcr_sectors(cycles):
    cap, cycle = next(c for c in cycles if c[0].halftrack == 26)
    faults = capture_faults(cap, cycle)
    out = decode_track(cap, 13)
    bad = int((out.errors == SectorError.BAD_GCR).sum())
    assert bad and len(np.unique(faults["segment"])) >= bad
    assert (faults["byte"] < len(cap.data)).all()
