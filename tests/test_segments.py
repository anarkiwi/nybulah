import dataclasses

import numpy as np
import pytest

from nybulah.analysis import gcr
from nybulah.analysis.capture import (
    ByteCapture,
    capture_bits,
    framed_capture,
    segments,
)
from nybulah.analysis.cycle import (
    TrackKind,
    _clashes,
    _overlaps,
    _Pairs,
    extract_revolution,
    find_cycle,
    header_period,
    lag_window,
    revolution_spans,
)
from nybulah.analysis.sector import (
    SYNC_BYTES,
    SectorError,
    decode_track,
    format_track,
)
from nybulah.analysis.synth import byte_capture, simulate_capture

CAPTURE_BYTES = 31 * 256


def _track(track, payload=None, seed=0):
    n = gcr.sectors_per_track(track)
    if payload is None:
        payload = np.random.default_rng(seed).integers(0, 256, (n, 256), np.uint8)
    return gcr.to_bits(format_track(track, payload, b"ID"))


def _capture(bits, seed, start="sync", weak=None, nbytes=CAPTURE_BYTES):
    rng = np.random.default_rng(seed)
    stream = simulate_capture(
        bits, 9 * nbytes, int(rng.integers(len(bits))), weak=weak, rng=rng
    )
    return byte_capture(stream, nbytes, start, rng=rng)


def _assert_period(cycle, bits, track, error=3):
    assert cycle.kind == TrackKind.FORMATTED
    assert cycle.segments == 2 * gcr.sectors_per_track(track)
    assert abs(cycle.length - len(bits)) <= error * cycle.segments
    assert cycle.sigma > 0


@pytest.mark.parametrize("track", [1, 18, 25, 31])
@pytest.mark.parametrize("seed", range(3))
def test_uniform_format_period_is_not_a_sector_alias(track, seed):
    blank = np.ones((gcr.sectors_per_track(track), 256), np.uint8)
    bits = _track(track, blank)
    cap = _capture(bits, seed)
    cycle = find_cycle(cap, gcr.speed_zone(track))
    _assert_period(cycle, bits, track)
    rev = extract_revolution(cap, cycle)
    assert abs(len(rev) - len(bits)) <= 3 * cycle.segments
    out = decode_track(rev, track, b"ID")
    assert (out.errors == SectorError.OK).all()
    assert out.offsets[0] == np.argmin(rev) >= gcr.SYNC_MIN_BITS


@pytest.mark.parametrize("seed", range(3))
def test_unsynced_start_and_weak_region(seed):
    bits = _track(25, seed=seed)
    weak = (len(bits) // 3, len(bits) // 10)
    cap = _capture(bits, seed, start="now", weak=weak)
    cycle = find_cycle(cap, 1)
    assert cycle.kind == TrackKind.FORMATTED
    assert abs(cycle.length - len(bits)) <= 3 * cycle.segments


def test_index_period_hint_and_alignment():
    bits = _track(5)
    cap = _capture(bits, 7)
    hinted = find_cycle(cap, 3, period=60.0 / 300.0)
    assert hinted.length == find_cycle(cap, 3).length
    assert find_cycle(cap, 3, index_aligned=True).start == 0
    narrow = lag_window(3, 60.0 / 300.0)
    assert narrow[1] - narrow[0] < np.subtract(*lag_window(3)[::-1])


def test_segment_layout_restores_stream():
    bits = _track(18)
    stream = simulate_capture(bits, 70000, 1234)
    cap = byte_capture(stream, CAPTURE_BYTES, sync_error=0)
    seg = segments(cap)
    starts, lengths = gcr.runs_of_ones(stream)
    frame = starts[0] + lengths[0]
    lead = gcr.SYNC_MIN_BITS
    assert np.array_equal(seg.bits[lead:], stream[frame : frame + len(seg.bits) - lead])
    assert np.array_equal(
        seg.bits, capture_bits(cap.data, cap.positions, cap.sync_bits, lead)
    )
    assert seg.run[0] == -1 and (seg.bits[seg.run[1] : seg.begin[1]] == 1).all()


def test_framed_capture_matches_byte_capture():
    bits = _track(31)
    cap = _capture(bits, 3)
    seg = segments(cap)
    rows = [np.full(5, 0xFF, np.uint8)]
    for k in range(len(seg)):
        rows += [seg.data[seg.first[k] : seg.first[k] + seg.lengths[k]]]
        rows += [np.full(5, 0xFF, np.uint8)] if k < len(seg) - 1 else []
    nib = np.concatenate(rows + [np.full(300, 0xFF, np.uint8)])
    framed = framed_capture(nib)
    assert framed.start == "sync" and len(framed.positions) == len(cap.positions)
    assert np.array_equal(segments(framed).lengths[1:-1] < 30, seg.lengths[1:-1] < 30)
    _assert_period(find_cycle(framed, 0), bits, 31, framed.sync_error)


def test_segmented_unformatted_killer_and_short():
    rng = np.random.default_rng(0)
    noise = byte_capture(
        rng.integers(0, 2, 80000, dtype=np.uint8), CAPTURE_BYTES, "now"
    )
    assert len(segments(noise)) > 1
    assert find_cycle(noise, 3).kind == TrackKind.UNFORMATTED
    killer = ByteCapture(np.full(10, 0xFF, np.uint8), [2, 5], [5000, 5000], "now")
    assert find_cycle(killer, 3).kind == TrackKind.KILLER
    short = _capture(_track(1), 1, nbytes=3000)
    assert find_cycle(short, 3).kind == TrackKind.UNFORMATTED
    single = ByteCapture(np.full(10, 0x55, np.uint8), [], [], "now")
    assert find_cycle(single, 3).kind == TrackKind.UNFORMATTED


def test_decode_reports_copy_used():
    bits = _track(31)
    stream = simulate_capture(bits, 9 * CAPTURE_BYTES, 0)
    cap = byte_capture(stream, CAPTURE_BYTES, rng=0)
    out = decode_track(cap, 31, b"ID")
    twice = np.flatnonzero(out.copies == 2)
    assert len(twice) and (out.errors == SectorError.OK).all()
    assert (out.copy[twice] == 0).all()
    sector = int(twice[0])
    first = out.offsets[sector]
    damaged = segments(cap).bits.copy()
    damaged[first + 900 : first + 940] = 0
    out = decode_track(damaged, 31, b"ID")
    assert out.copy[sector] == 1 and out.errors[sector] == SectorError.OK


def test_header_period_matches_cycle():
    bits = _track(31)
    cap = _capture(bits, 5)
    length, bound = header_period(cap, 0)
    assert abs(length - len(bits)) <= bound and bound == 3 * 34
    assert abs(find_cycle(cap, 0).length - length) <= bound
    blank = ByteCapture(np.full(9000, 0x55, np.uint8), [10, 4000], [40, 40], "now")
    assert header_period(blank, 3) is None
    assert header_period(ByteCapture(np.zeros(9, np.uint8), [], []), 3) is None


@pytest.mark.parametrize("seed", range(3))
def test_density_label_does_not_bound_the_period(seed):
    """Zone 0 content labelled zone 2: headers repeat, so all zones' windows are searched."""
    bits = _track(35, seed=seed)
    cap = _capture(bits, seed)
    assert not lag_window(2)[0] <= len(bits) <= lag_window(2)[1]
    _assert_period(find_cycle(cap, 2), bits, 35)
    noise = gcr.encode_bits(np.random.default_rng(seed).integers(0, 256, 5000))
    assert find_cycle(_capture(noise, seed, "now"), 2).kind == TrackKind.UNFORMATTED


@pytest.mark.parametrize("seed", range(3))
def test_identical_duplicate_header_keeps_true_period(seed):
    """A sector written twice, identically, within one revolution."""
    track, n = 1, 19
    payload = np.random.default_rng(seed).integers(0, 256, (n, 256), np.uint8)
    raw = format_track(track, payload, b"ID", capacity=gcr.track_capacity(3))
    sector = len(raw) // n
    raw[(n - 1) * sector : n * sector] = raw[3 * sector : 4 * sector]
    bits = gcr.to_bits(raw)
    cycle = find_cycle(_capture(bits, seed), 3)
    assert cycle.kind == TrackKind.FORMATTED and cycle.segments == 2 * n
    assert abs(cycle.length - len(bits)) <= 3 * cycle.segments


def test_no_sync_pair_spans_a_revolution():
    """A one-sector track whose syncs all lie within one revolution: the gap fill
    repeats at any lag, so only a header seen twice fixes the period."""
    raw = format_track(1, np.arange(256, dtype=np.uint8)[None], b"ID", capacity=7692)
    bits = np.roll(gcr.to_bits(raw), 20000)
    cap = framed_capture(gcr.to_bytes(simulate_capture(bits, 8 * 8192)))
    assert segments(cap).begin.max() - segments(cap).begin.min() < lag_window(3)[0]
    assert find_cycle(cap, 3).kind == TrackKind.UNFORMATTED
    after_sync = np.roll(bits, -20000 - 8 * SYNC_BYTES)
    stream = gcr.to_bytes(simulate_capture(after_sync, 65536))
    unmeasured = ByteCapture(
        stream, np.zeros(0, np.int64), np.zeros(0, np.int64), "now"
    )
    cycle = find_cycle(unmeasured, 3)
    assert cycle.kind == TrackKind.FORMATTED and cycle.length == len(bits)


@pytest.mark.parametrize("seed", range(4))
def test_overlapping_distance_bounds(seed):
    rng = np.random.default_rng(seed)
    n = 80
    i = rng.integers(0, 30, n)
    j = i + rng.integers(1, 6, n)
    zeros = np.zeros(n, np.int64)
    pairs = _Pairs(i, j, rng.integers(1000, 1300, n), zeros, zeros, 0.3)
    mask, other = rng.random(n) < 0.5, rng.random(n) < 0.2
    lo, hi = pairs.dist - 3 * (j - i), pairs.dist + 3 * (j - i)
    brute = ((lo[:, None] <= hi[other]) & (hi[:, None] >= lo[other])).any(axis=1)
    assert (_overlaps(pairs, 3, mask, other) == (mask & brute)).all()
    assert not _overlaps(pairs, 3, mask, np.zeros(n, bool)).any()


def test_header_clash_needs_other_segments():
    """A differing header pair at a revolution's distance rules it out unless
    it reuses a segment the revolution already pairs (a misaligned neighbour)."""
    i, j = np.array([0, 1, 2]), np.array([40, 41, 41])
    zeros = np.zeros(3, np.int64)
    pairs = _Pairs(i, j, np.array([58000, 58100, 58050]), zeros, zeros, 0.3)
    lead = np.array([True, False, False])
    assert _clashes(pairs, 3, lead, np.array([False, True, False]))
    pairs = _Pairs(i, np.array([40, 40, 41]), pairs.dist, zeros, zeros, 0.3)
    assert not _clashes(pairs, 3, lead, np.array([False, True, False]))
    assert not _clashes(pairs, 3, lead, np.zeros(3, bool))


def test_revolution_spans_chain_whole_revolutions():
    bits = _track(18, seed=4)
    cap = _capture(bits, 4, nbytes=3 * len(bits) // 8)
    cycle = find_cycle(cap, gcr.speed_zone(18))
    spans = np.array(revolution_spans(cap, cycle))
    assert len(spans) >= 2 and (spans[1:, 0] == spans[:-1, 1]).all()
    assert (np.abs(np.diff(spans) - len(bits)) <= 3 * cycle.segments).all()
    assert not revolution_spans(cap, dataclasses.replace(cycle, length=10))
