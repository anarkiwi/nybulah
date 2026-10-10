import pathlib
import re

import numpy as np
import pytest

from nybulah import eventalign, passes
from nybulah.analysis.gcr import bits_per_revolution, to_bits
from nybulah.formats import D64, d64_to_g64
from nybulah.nibbler import READ, Capture, cell_cycles
from nybulah.simdisk import Media, log_bytes, sync_track, true_syncs

HW = pathlib.Path(__file__).parent / "data" / "hw" / "dev10"
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
    arr = cap.syncs.timing[1]
    lo, hi = arr.lo[2:] - arr.hi[1:-1], arr.hi[2:] - arr.lo[1:-1]
    assert arr.valid.all() and ((gap > lo) & (gap <= hi + 1e-6)).all()


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


def test_run_range_excess_that_fits_neither_takes_the_nearer():
    cells = (3.4, 3.5)
    assert passes.run_range((-4.85, -0.06), cells, 2) == (0, 2, 2)
    assert passes.run_range((-np.inf, -0.06), cells, 2) == (0, 2, 2)
    assert passes.run_range((10.0, 12.0), cells, 2) == (0, 2, 2)
    assert passes.run_range((17.0, 19.0), cells, 2) == (10, 10, 10)


def test_align_follows_slips():
    events = np.arange(6)
    consistent = np.zeros((6, 17), bool)
    consistent[:3, 8] = True
    consistent[3:, 9] = True
    consistent[4, 9] = False
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


def test_window_min_matches_brute_force():
    """The first lexicographic least of (a, b) in every window."""
    rng = np.random.default_rng(0)
    a, b = rng.integers(0, 4, 40).astype(float), rng.integers(0, 3, 40)
    key = a * 3 + b
    for below, above in ((0, 0), (3, 2), (50, 0), (0, 50), (5, 5)):
        for fn in (eventalign.window_min, eventalign.window_min.py_func):
            arg = fn(a, b, below, above)
            want = [
                max(0, c - below)
                + int(np.argmin(key[max(0, c - below) : c + above + 1]))
                for c in range(len(a))
            ]
            assert arg.tolist() == want


def test_align_slips_bounded_free_and_weighed():
    """Any slip costs one, bounded per event by rise and fall; a free rise costs
    nothing; changes land only on land columns; weight breaks ties."""
    consistent = np.zeros((4, 41), bool)
    consistent[:2, 20] = consistent[2:, 30] = True
    assert passes.align(consistent)[0].tolist() == [0, 0, 10, 10]
    offs, matched = passes.align(consistent, rise=[0, 0, 5, 5])
    assert offs.tolist() == [0, 0, 0, 0] and matched.tolist() == [1, 1, 0, 0]
    longer = np.zeros((5, 41), bool)
    longer[:2, 20] = longer[2:, 30] = True
    land = np.ones(longer.shape, bool)
    land[1:3, 30] = False
    assert passes.align(longer, land=land)[0].tolist() == [0, 0, 0, 10, 10]
    consistent[2:, 18] = True
    assert passes.align(consistent, fall=[0, 0, 1, 1])[0].tolist() == [0, 0, 10, 10]
    assert passes.align(consistent, free=[0, 0, 10, 0])[0].tolist() == [0, 0, 10, 10]
    weight = np.zeros(consistent.shape)
    weight[2:, 18] = 1.0
    assert passes.align(consistent, weight=weight)[0].tolist() == [0, 0, -2, -2]
    last = np.zeros((3, 21), bool)
    last[:2, 10] = last[2, 4] = True
    assert passes.align(last)[0].tolist() == [0, 0, 0]
    weight = np.zeros(last.shape)
    weight[2, 4] = 1.0
    assert passes.align(last, weight=weight)[0].tolist() == [0, 0, -6]


def test_recurs_on_either_side_of_a_boundary():
    rng = np.random.default_rng(1)
    turn = rng.integers(0, 256, 100, dtype=np.uint8)
    data = np.concatenate((turn, turn[:40]))
    data[[116, 124]] ^= 1
    again = passes.recurs(data, 100, span=8)
    assert again[12] and again[28] and not again[20]
    assert again[5] and not again[60]


def test_monotone_reindex_and_suspect():
    # pylint: disable=protected-access
    pos = np.array([10, 11, 12, 13, 11, 12, 20, 21])
    keep, after = passes._monotone(pos)
    assert keep.tolist() == [1, 0, 0, 0, 1, 1, 1, 1] and after[1] == 11
    arr = passes.Arrivals(
        np.arange(8.0), np.arange(8.0) - 1, np.arange(8.0), np.ones(8, bool)
    )
    out = passes._reindex(arr, pos, keep)
    assert len(out.read) == 12 and out.read[[0, 1, 2]].tolist() == [0, 4, 5]
    assert np.isinf(out.lo[3:10]).all() and not out.valid[3:10].any()
    definite = np.array([2, 5, 9])
    sus = passes._suspect(12, definite, np.array([True, False, True]))
    assert np.flatnonzero(sus).tolist() == [3, 4, 5, 6, 7, 8]
    sus = passes._suspect(12, definite, np.array([True, True, False]))
    assert np.flatnonzero(sus).tolist() == list(range(6, 12))


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


def _plant_before_syncs(cells, anchor):
    """Write ``anchor`` into every gap, framed as read, ending a byte before a sync."""
    edges = np.flatnonzero(np.diff(np.concatenate(([0], cells, [0])).astype(np.int8)))
    starts, ends = edges[::2], edges[1::2]
    sync = ends - starts >= 10
    starts, ends = starts[sync], ends[sync]
    nxt = np.append(starts[1:], starts[0] + len(cells))
    bits = to_bits(np.frombuffer(anchor, np.uint8))
    for at in ends + 8 * ((nxt - ends) // 8 - len(anchor) - 1):
        cells[np.arange(at, at + len(bits)) % len(cells)] = bits


def test_ts_anchor_drawn_off_its_angle_on_a_periodic_track(make_rig, monkeypatch):
    """Gaps that read as the anchor only after BITS draw the TS pass off its angle;
    the merge places the syncs where the BITS bytes latched them."""
    blank = d64_to_g64(D64(np.zeros((683, 256), np.uint8)), progress=False)
    media = Media.from_g64(blank)
    drive, nib = make_rig("1541", media)
    nib.halftrack = 36
    drive.mech.log = []
    run = nib._pass  # pylint: disable=protected-access

    def drawn(kind, mode, anchor=b""):
        if anchor:
            _plant_before_syncs(media.tracks[(0, 6)], anchor)
        return run(kind, mode, anchor)

    monkeypatch.setattr(nib, "_pass", drawn)
    cap = nib.capture(6, start="sync", timing="syncs")
    pos, runs = true_syncs(drive.mech.log, cap.data)
    placed = cap.base + cap.ts_syncs()[0]
    assert cap.base > 0 and not np.isin(placed[placed < len(cap.data)], pos).all()
    lo, hi = cap.sync_bounds
    assert np.array_equal(cap.positions, pos) and cap.syncs.unmatched == 0
    assert (lo <= runs).all() and ((hi < 0) | (runs <= hi)).all()


def test_sync_weights_favour_rare_latched_ones():
    ok = np.array([False, True, True, True, True])
    ones = np.array([0, 2, 2, 2, 9])
    w = passes.sync_weights(ok, ones)
    assert w[0] == 0 and w[4] > w[1] == w[2] == w[3] > 0
    fold = passes._fold  # pylint: disable=protected-access
    assert fold(np.arange(7), 5, np.maximum).tolist() == [5, 6, 2, 3, 4]
    assert fold(np.arange(12), 5, np.maximum).tolist() == [10, 11, 7, 8, 9]
    assert fold(np.arange(9) == 7, 3, np.logical_or).tolist() == [False, True, False]


def test_hw_ts_anchor_drawn_off_its_angle():
    """A 1541-II read of a blank track 3 whose TS pass anchored on a gap byte at
    the format's write splice; its syncs are where the BITS bytes latched $FF."""
    for side in "ab":
        cap = Capture.load(HW / side / "read-s0-t03-0.npz")
        data = cap.data
        latched = np.flatnonzero((data[:-1] == 0xFF) & (data[1:] != 0xFF)) + 1
        assert np.array_equal(cap.positions, latched) and cap.syncs.unmatched == 0
        placed = cap.base + cap.ts_syncs()[0]
        drawn = not np.isin(placed[placed < len(data)], latched).all()
        assert drawn == (side == "b")


def test_follow_compiled_and_python_agree():
    """The compiled path search is the Python one."""
    rng = np.random.default_rng(2)
    n, width = 12, 30
    args = (
        (rng.random((n, width)) < 0.7).astype(float),
        rng.random((n, width)) < 0.8,
        rng.random((n, width)) / (n + 1),
        width // 2,
        rng.integers(0, 3, n),
        rng.integers(0, width, n),
        rng.integers(0, width, n),
    )
    fn = eventalign._follow  # pylint: disable=protected-access
    assert fn(*args).tolist() == fn.py_func(*args).tolist()
