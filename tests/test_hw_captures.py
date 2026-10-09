"""Captures of a freshly formatted disk on a 1571 (tests/data/hw)."""

import pathlib

import numpy as np
import pytest

from nybulah.analysis.capture import ByteCapture, segments
from nybulah.analysis.cycle import (
    TrackKind,
    extract_revolution,
    find_cycle,
    lag_window,
    revolution_spans,
)
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


BLANK = sorted((DATA / "dev10").glob("*/read-*.npz"))


@pytest.mark.parametrize("path", BLANK, ids=lambda p: f"{p.parent.name}-{p.stem}")
def test_blank_rereads_survive_a_missed_sync(path):
    """Every data block of a blank format is identical. Without its first
    recorded sync, a capture's first segment holds a data block and a header,
    so a revolution's pairs, and those of a two-sector alias, fall under two
    segment shifts: the period must still be the whole revolution."""
    cap = Capture.load(path)
    whole = find_cycle(cap)
    track = cap.halftrack // 2
    assert whole.segments == 2 * sectors_per_track(track)
    rev = extract_revolution(cap, whole)
    assert (decode_track(rev, track).errors == SectorError.OK).all()
    bound = cap.sync_error * whole.segments
    keep = slice(1, None)
    missed = ByteCapture(
        cap.data[: cap.valid_bytes],
        cap.positions[keep],
        cap.sync_bits[keep],
        cap.start,
        cap.sync_error,
    )
    cycle = find_cycle(missed, cap.density)
    assert 0 <= whole.segments - cycle.segments <= 1
    assert abs(cycle.length - whole.length) <= bound
    assert abs(len(extract_revolution(missed, cycle)) - whole.length) <= bound
    spans = np.array(revolution_spans(missed, cycle)).reshape(-1, 2)
    assert (np.abs(np.diff(spans) - whole.length) <= bound).all()


def test_rereads_agree_on_the_revolution():
    """Two reads of one track, the first anchored from TS: one period, and a
    revolution that holds every sector."""
    first, second = (
        Capture.load(DATA / "dev10-t03" / f"read-s0-t03-{n}.npz") for n in (0, 1)
    )
    cycle, reread = find_cycle(first), find_cycle(second)
    assert cycle.segments == reread.segments == 2 * sectors_per_track(3)
    assert abs(cycle.length - reread.length) <= first.sync_error * cycle.segments
    rev = extract_revolution(first, cycle)
    assert (decode_track(rev, 3).errors == SectorError.OK).all()
