import numpy as np
import pytest

from nybulah.analysis import gcr
from nybulah.analysis.cycle import (
    Cycle,
    TrackKind,
    extract_revolution,
    find_cycle,
    index_align,
    lag_window,
)
from nybulah.analysis.sector import SectorError, decode_track, format_track
from nybulah.analysis.synth import simulate_capture

NIB_BITS = 0x2000 * 8


def _track(track, rpm=300.0, extra=0, seed=0):
    rng = np.random.default_rng(seed)
    n = gcr.sectors_per_track(track)
    payload = rng.integers(0, 256, (n, 256), dtype=np.uint8)
    capacity = gcr.track_capacity(gcr.speed_zone(track), rpm)
    bits = gcr.to_bits(format_track(track, payload, b"ID", capacity=capacity))
    return np.concatenate((bits, rng.integers(0, 2, extra, dtype=np.uint8)))


def _sector0_sync(bits):
    starts, lengths = gcr.runs_of_ones(bits, circular=True)
    covers = (0 - starts) % len(bits) < lengths
    return int(starts[covers][0])


@pytest.mark.parametrize("track", [1, 18, 25, 31])
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_cycle_exact_with_weak_region(track, seed):
    rng = np.random.default_rng(seed)
    bits = _track(track, extra=int(rng.integers(0, 64)), seed=seed)
    length = len(bits)
    start = int(rng.integers(0, length))
    weak = (length // 3, length // 10)
    cap = simulate_capture(bits, 5 * length // 4, start, weak=weak, rng=seed)
    cycle = find_cycle(cap, gcr.speed_zone(track))
    assert cycle.kind == TrackKind.FORMATTED
    assert cycle.length == length
    assert cycle.start == (_sector0_sync(bits) - start) % length
    rev = extract_revolution(cap, cycle)
    phys = (start + cycle.start + np.arange(length)) % length
    stable = (phys - weak[0]) % length >= weak[1]
    assert np.array_equal(rev[stable], bits[phys][stable])
    out = decode_track(rev, track, b"ID")
    assert out.offsets[0] == np.argmin(rev) >= gcr.SYNC_MIN_BITS
    assert (out.errors == SectorError.OK).sum() >= gcr.sectors_per_track(track) - 3


@pytest.mark.parametrize("seed", range(4))
def test_cycle_length_with_bit_noise(seed):
    rng = np.random.default_rng(seed)
    bits = _track(5, extra=int(rng.integers(1, 8)), seed=seed)
    cap = simulate_capture(
        bits, int(1.2 * len(bits)), int(rng.integers(0, len(bits))), 1e-3, rng=seed
    )
    cycle = find_cycle(cap, 3)
    assert cycle.kind == TrackKind.FORMATTED
    assert cycle.length == len(bits)
    assert 0.99 < cycle.match < 1.0


def test_byte_framed_capture_revolution_matches_track():
    bits = _track(20)
    raw = gcr.to_bytes(simulate_capture(bits, NIB_BITS, 12345))
    cycle = find_cycle(gcr.to_bits(raw), 2)
    rev = extract_revolution(gcr.to_bits(raw), cycle)
    assert np.array_equal(rev, gcr.rotate(bits, (12345 + cycle.start) % len(bits)))


def test_measured_period_extends_search():
    bits = _track(10, rpm=280.0)
    cap = simulate_capture(bits, 3 * len(bits) // 2, 999)
    assert find_cycle(cap, 3).kind == TrackKind.UNFORMATTED
    cycle = find_cycle(cap, 3, period=60 / 280)
    assert cycle.kind == TrackKind.FORMATTED and cycle.length == len(bits)
    measured, nominal = lag_window(3, 60 / 280), lag_window(3)
    assert measured[1] - measured[0] < nominal[1] - nominal[0]


def test_index_aligned():
    bits = _track(30)
    cap = simulate_capture(bits, NIB_BITS, 4321)
    cycle = find_cycle(cap, 1, index_aligned=True)
    assert cycle.start == 0 and cycle.length == len(bits)
    other = simulate_capture(_track(31), NIB_BITS, 17)
    revs = index_align({60: (cap, cycle), 62: (other, find_cycle(other, 0))})
    assert np.array_equal(revs[60], cap[: len(bits)])
    assert np.array_equal(revs[62], other[: len(_track(31))])


def test_lag_window_physics():
    lo, hi = lag_window(3)
    assert lo == int(61538.46 * 0.95) and hi == int(np.ceil(61538.46 * 1.05))
    lo, hi = lag_window(0, period=0.2, tolerance=0.0)
    assert lo == hi == 50000


def test_killer_and_unformatted():
    rng = np.random.default_rng(0)
    killer = np.ones(NIB_BITS, np.uint8)
    killer[rng.integers(0, NIB_BITS, 20)] = 0
    assert find_cycle(killer, 3) == Cycle(TrackKind.KILLER, 0, 61538)
    for seed in range(5):
        noise = np.random.default_rng(seed).integers(0, 2, NIB_BITS, dtype=np.uint8)
        assert find_cycle(noise, 3).kind == TrackKind.UNFORMATTED
    assert find_cycle(np.zeros(NIB_BITS, np.uint8), 2).kind == TrackKind.UNFORMATTED
    with pytest.raises(ValueError):
        find_cycle(noise[:40000], 3)


def test_mostly_weak_track_is_unformatted():
    bits = _track(1)
    weak = (0, int(0.6 * len(bits)))
    cap = simulate_capture(bits, 2 * len(bits), 0, weak=weak, rng=1)
    assert find_cycle(cap, 3).kind == TrackKind.UNFORMATTED
    weak = (0, int(0.4 * len(bits)))
    cap = simulate_capture(bits, 2 * len(bits), 0, weak=weak, rng=1)
    cycle = find_cycle(cap, 3)
    assert cycle.kind == TrackKind.FORMATTED and cycle.length == len(bits)


def test_unsynced_periodic_track():
    rng = np.random.default_rng(5)
    bits = gcr.encode_bits(rng.integers(0, 256, 6000, dtype=np.uint8))
    cycle = find_cycle(simulate_capture(bits, NIB_BITS, 77), 2)
    assert cycle.kind == TrackKind.FORMATTED
    assert cycle.length == len(bits) and cycle.start == 0
