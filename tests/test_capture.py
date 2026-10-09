import re

import numpy as np
import pytest

from nybulah import passes
from nybulah.analysis.gcr import bits_per_revolution, to_bits
from nybulah.nibbler import READ, Capture, cell_cycles
from nybulah.simdisk import Media, log_bytes, sync_track, true_syncs

RUNS = list(range(10, 21)) + [24, 28, 32, 40, 48, 64, 80, 100, 128, 200, 255, 256]
RUNS += [300, 400, 500, 640, 800, 1000]
ZONE_HALFTRACK = {3: 10, 2: 40, 1: 52, 0: 64}
CASES = [
    ("1541", 3, 297.0, 3.0, "now"),
    ("1541", 3, 303.0, 3.0, "sync"),
    ("1541", 2, 300.0, 0.0, "now"),
    ("1541", 1, 303.0, 0.0, "sync"),
    ("1541", 0, 297.0, 3.0, "now"),
    ("1571", 3, 300.0, 3.0, "index"),
    ("1571", 2, 297.0, 0.0, "sync"),
    ("1571", 1, 300.0, 3.0, "now"),
    ("1571", 0, 303.0, 3.0, "index"),
    ("1571", 2, 297.0, 3.0, "index"),
    ("1571", 0, 297.0, 3.0, "index"),
    ("1541", 1, 297.0, 0.0, "now", 25),
]


def sync_media(zone, rpm, wander, seed=0):
    cells = int(round(bits_per_revolution(zone)))
    track = sync_track(RUNS * 2, cells, seed=seed + zone, gaps=(1, 24))
    return Media({(0, ZONE_HALFTRACK[zone]): track}, rpm=rpm, wander=(wander, 2.0))


@pytest.fixture(name="sync_capture", scope="module", params=CASES, ids=str)
def sync_capture_fixture(request):
    from conftest import rig

    model, zone, rpm, wander, start, *seed = request.param
    drive, nib = rig(model, sync_media(zone, rpm, wander, *seed))
    nib.halftrack = 36
    drive.mech.log = []
    cap = nib.capture(ZONE_HALFTRACK[zone], density=zone, start=start)
    reads = [c for c in nib.mon.calls if c[0] == READ]
    return cap, drive.mech.log, drive, reads


def test_bits_pass_loses_no_byte(sync_capture):
    cap, log, _, _ = sync_capture
    stream, _ = log_bytes(log)
    assert len(cap.data) == 31 * 256 and stream.find(cap.data.tobytes()) >= 0


def test_sync_lengths_within_proved_bound(sync_capture):
    cap, log, _, _ = sync_capture
    pos, runs = true_syncs(log, cap.data)
    assert cap.status == 0 and cap.base >= 0 and cap.lost == 0
    assert (cap.positions == pos).all()
    lo, hi = cap.sync_bounds
    assert (lo <= runs).all() and ((hi < 0) | (runs <= hi)).all()
    assert (np.abs(cap.sync_bits - runs) <= 1).all()
    assert np.mean(cap.sync_bits == runs) > 0.5
    assert set(runs) >= {10, 1000} and cap.sync_error >= 1


def test_tb_windows_hold_every_arrival(sync_capture):
    cap, log, _, reads = sync_capture
    stream, times = log_bytes(log)
    begin = int(np.searchsorted(times, reads[-2][1]))
    first = stream.find(cap.data[cap.base : cap.base + 64].tobytes(), begin)
    gap = np.diff(times[first:][: len(cap.tb)])[1:]
    arr = passes.tb_arrivals(cap.tb, _wraps(cap))
    lo, hi = arr.lo[2:] - arr.hi[1:-1], arr.hi[2:] - arr.lo[1:-1]
    assert arr.valid.all() and ((gap > lo) & (gap <= hi + 1e-6)).all()


def _wraps(cap):
    arr = passes.tb_arrivals(cap.tb)
    period = float(np.median(np.diff(arr.read)))
    return passes.ts_wraps(arr, cap.ts_syncs(), period, True, cap.tb)[0]


def squash(bits):
    """Bits as text with every run of 10 or more ones shown as one 's'."""
    return re.sub("1{10,}", "s", "".join(map(str, bits)))


def test_bits_restore_the_track(sync_capture):
    cap, _, drive, _ = sync_capture
    cells = drive.mech.media.tracks[(0, ZONE_HALFTRACK[cap.density])]
    got = squash(cap.bits())
    ring = squash(np.concatenate((cells, cells, cells)))
    start = got.index("s") + 1
    assert ring.find(got[start:]) >= 0


def test_revolution_from_tb_on_a_1541(sync_capture):
    cap, _, drive, _ = sync_capture
    media = drive.mech.media
    if cap.start == "index" or media.wander[0]:
        return
    assert cap.rpm == pytest.approx(media.rpm, rel=2e-3)


def test_record_round_trip(sync_capture, tmp_path):
    cap, _, _, _ = sync_capture
    cap.save(tmp_path / "c.npz")
    back = Capture.load(tmp_path / "c.npz")
    assert (back.bits() == cap.bits()).all() and back.base == cap.base


def test_version_1_records_still_load():
    from pathlib import Path

    here = Path(__file__).parent / "fixtures"
    cap = Capture.load(here / "capture_v1.npz")
    with np.load(here / "capture_v1_expect.npz") as f:
        want = {k: f[k] for k in f.files}
    expect = np.unpackbits(want["bits"])[: int(want["nbits"])]
    assert cap.version == 1 and np.array_equal(cap.positions, want["positions"])
    assert np.array_equal(cap.sync_bits, want["sync_bits"])
    assert np.array_equal(cap.bits(), expect) and cap.rpm == pytest.approx(want["rpm"])
    assert cap.byte_cycles is None or cap.byte_cycles > 0
    assert (cap.sync_bounds[1] - cap.sync_bounds[0] == 6).all() and cap.sync_error == 3


def test_syncs_timing_locates_with_coarse_lengths(make_rig):
    drive, nib = make_rig("1541", sync_media(2, 300.0, 0.0))
    nib.halftrack = 36
    log = []
    drive.mech.log = log
    cap = nib.capture(40, density=2, start="sync", timing="syncs")
    pos, runs = true_syncs(log, cap.data)
    lo, hi = cap.sync_bounds
    found = np.isin(pos, cap.positions)
    assert (runs[~found] <= 12).all() and cap.tb is None
    keep = np.isin(cap.positions, pos)
    assert keep.all() and (lo <= runs[found]).all() and (runs[found] <= hi).all()


def test_tb_schedule_is_ordered():
    sample, ready = passes.tb_schedule(10_000)
    assert (np.diff(sample) > 0).all() and (np.diff(ready) > 0).all()
    gaps = np.diff(sample)
    assert gaps[: passes.TB_CHAIN - 1].max() == 2 and gaps.max() <= 7


def test_ts_groups_wrapped_release_waits():
    tc = np.array([5, 5, 5, 9])
    th = np.array([1, 1, 1, 0])
    td = np.array([0, 0, 0xFE, 0x10])
    count, iters = passes.ts_syncs(tc, th, td)
    assert count.tolist() == [261, 9] and iters.tolist() == [514, 240]
    lo, hi = passes.ts_pulse([0, 256])
    assert lo[0] == 0 and hi[1] - lo[1] == passes.TS_ITER + passes.TS_WRAP_EXTRA + 46


def test_run_range_feasibility():
    cells = (3.25, 3.25)
    assert passes.run_range((-2.0, 2.0), cells, 5) == (0, 5, 5)
    assert passes.run_range((30.0, 33.0), cells, 3)[0] == 3 + 10
    run, lo, hi = passes.run_range((-1.0, 5.0), cells, 9)
    assert lo == 9 and hi == 10 and run in (0, 10)
    assert passes.run_range((5.0, np.inf), cells, 2) == (10, 10, -1)
    assert passes.run_range((-np.inf, np.inf), cells, 2) == (0, 2, -1)
    assert passes.run_range((20.0, 21.0), cells, 2)[0] == 10


def test_align_follows_slips():
    events = np.arange(6)
    consistent = np.zeros((6, 2 * passes.BAND + 1), bool)
    consistent[:3, passes.BAND] = True
    consistent[3:, passes.BAND + 1] = True
    consistent[4, passes.BAND + 1] = False
    offs, matched = passes.align(consistent)
    assert offs.tolist() == [0, 0, 0, 1, 1, 1] and matched.tolist() == [
        1,
        1,
        1,
        1,
        0,
        1,
    ]
    assert passes.align(np.zeros((0, 3), bool))[0].size == 0
    del events


def test_best_offset_and_byte_period():
    mask = np.zeros(100, bool)
    mask[[30, 45, 70]] = True
    assert passes.best_offset([10, 25, 50], mask) == (20, 3)
    assert passes.best_offset([], mask) == (0, 0)
    rng = np.random.default_rng(1)
    rev = rng.integers(0, 256, 6000, dtype=np.uint8)
    data = np.concatenate((rev, rev[:1936]))
    assert passes.byte_period(data, 6000 * 8) == 6000
    assert passes.byte_period(np.zeros(7936, np.uint8), 6000 * 8) is None
    assert (
        passes.byte_period(rng.integers(0, 256, 7936, dtype=np.uint8), 6000 * 8) is None
    )


def test_anchor_choice():
    rng = np.random.default_rng(2)
    rev = rng.integers(0, 0x80, 6000, dtype=np.uint8)
    data = np.concatenate((rev, rev[:1936]))
    cell = cell_cycles(2, passes.RPM_MAX)
    base, anchor = passes.choose_anchor(data, cell, 6000)
    assert (data[base - len(anchor) : base] == anchor).all() and base <= 4
    assert passes.choose_anchor(data, cell, None, steady=False) is None
    flat = np.full(7936, 0x55, np.uint8)
    assert passes.choose_anchor(flat, cell, None) is None
    assert passes.unmeasured_after_anchor(cell_cycles(0)) >= 1
    assert not passes.capable(to_bits(np.zeros(1, np.uint8))[:1])[0].any()


def test_agreements_count_equal_bytes_per_lag():
    data = np.array([1, 2, 1, 2, 1, 3], np.uint8)
    want = [
        sum(data[i] == data[i + lag] for i in range(len(data) - lag))
        for lag in (1, 2, 3)
    ]
    for fn in (passes.agreements, passes.agreements.py_func):
        assert fn(data, 1, 3).tolist() == want == [0, 3, 0]


def test_late_ts_release_counts_are_followed():
    """Releases TS sees on its wrap path merge byte readies; the merge follows."""
    from conftest import rig

    track = sync_track([818, 1000] * 3, int(round(bits_per_revolution(2))), 1, (2, 3))
    drive, nib = rig("1541", Media({(0, 40): track}))
    nib.halftrack = 36
    drive.mech.log = []
    cap = nib.capture(40, density=2, start="sync")
    count, iters = cap.ts_syncs()
    late = (iters >= 256) & (iters % 256 == 0)
    pos, runs = true_syncs(drive.mech.log, cap.data)
    assert late.sum() >= 3 and np.diff(count).min() < np.diff(pos).min()
    lo, hi = cap.sync_bounds
    assert np.array_equal(cap.positions, pos)
    assert (lo <= runs).all() and ((hi < 0) | (runs <= hi)).all()
    assert (np.abs(cap.sync_bits - runs) <= 1).all()
