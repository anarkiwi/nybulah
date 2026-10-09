import numpy as np
import pytest

from nybulah.analysis import gcr
from nybulah.analysis.capture import ByteCapture, segments
from nybulah.analysis.cycle import find_cycle
from nybulah.analysis.faults import FaultKind, capture_faults, code_valid, gcr_faults
from nybulah.analysis.sector import format_track
from nybulah.analysis.synth import byte_capture, simulate_capture


def _block(seed=0):
    rng = np.random.default_rng(seed)
    return gcr.encode_bits(rng.integers(0, 256, 260, dtype=np.uint8))


def test_code_valid():
    bits = gcr.encode_bits([0x00])
    assert code_valid(bits)[[0, 5]].all() and len(code_valid(bits[:4])) == 0
    assert not code_valid(np.ones(5, np.uint8))[0]


@pytest.mark.parametrize(
    "lost,kind",
    [
        (1, FaultKind.SLIP),
        (-1, FaultKind.SLIP),
        (2, FaultKind.AMBIGUOUS),
        (8, FaultKind.AMBIGUOUS),
    ],
)
def test_slip_located_and_shift_mod5(lost, kind):
    rng = np.random.default_rng(lost + 10)
    bits = _block()
    cut = 1000
    if lost > 0:
        bits = np.delete(bits, np.arange(cut, cut + lost))
    else:
        bits = np.insert(bits, cut, rng.integers(0, 2, -lost))
    (fault,) = gcr_faults(bits)
    assert (fault["shift"] - lost) % 5 == 0 and -2 <= fault["shift"] <= 2
    assert fault["kind"] == kind and fault["resynced"] and not fault["exact"]
    assert cut - 40 <= fault["bit"] <= cut + 5


def test_corrupt_and_clean():
    assert len(gcr_faults(_block())) == 0
    bits = _block()
    bits[1500:1505] = 1
    (fault,) = gcr_faults(bits)
    assert fault["kind"] == FaultKind.CORRUPT and 1490 <= fault["bit"] <= 1505
    bits[-30:] = 1
    tail = gcr_faults(bits)[-1]
    assert not tail["resynced"] and tail["kind"] == FaultKind.AMBIGUOUS


def _overlap_capture(slip=None):
    """Zone 0 capture of 1.27 revolutions, a bit deleted at stream bit ``slip``."""
    rng = np.random.default_rng(4)
    bits = gcr.to_bits(
        format_track(31, rng.integers(0, 256, (17, 256), np.uint8), b"ID")
    )
    stream = simulate_capture(bits, 80000, 0)
    cap = byte_capture(stream, 31 * 256, rng=rng)
    cycle = find_cycle(cap, 0)
    if slip is not None:
        cap = byte_capture(np.delete(stream, slip), 31 * 256, sync_error=0)
    return cycle, cap


def test_byte_drop_measured_against_copy():
    cycle, cap = _overlap_capture()
    seg = segments(cap)
    k = 1 + int(np.argmax(seg.lengths[1:] > 300))
    at = seg.first[k] + 100
    dropped = ByteCapture(
        np.delete(cap.data, at), cap.positions - (cap.positions > at), cap.sync_bits
    )
    faults = capture_faults(dropped, cycle)
    (hit,) = faults
    assert hit["segment"] == k and hit["exact"] and hit["shift"] == 8
    assert hit["kind"] == FaultKind.BYTE and abs(hit["byte"] - at) <= 6
    plain = capture_faults(dropped)
    assert not plain["exact"].any() and plain["kind"][0] == FaultKind.AMBIGUOUS


def test_bit_slip_measured_against_copy():
    cycle, cap = _overlap_capture(slip=1500)
    (hit,) = capture_faults(cap, cycle)
    assert hit["exact"] and hit["shift"] == 1 and hit["kind"] == FaultKind.SLIP
    seg = segments(cap)
    rng = np.random.default_rng(4)
    bits = gcr.to_bits(
        format_track(31, rng.integers(0, 256, (17, 256), np.uint8), b"ID")
    )
    starts, lengths = gcr.runs_of_ones(bits)
    frame = starts[starts > 0][0] + lengths[starts > 0][0]
    where = seg.begin[hit["segment"]] + hit["bit"] + frame - gcr.SYNC_MIN_BITS
    assert 1500 - 40 <= where <= 1500 + 5
