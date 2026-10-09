import numpy as np
import pytest

from nybulah.analysis import gcr
from nybulah.analysis.sector import (
    SectorError,
    data_blocks,
    decode_track,
    format_track,
    header_blocks,
)

DISK_ID = b"AB"


def _rng(seed=0):
    return np.random.default_rng(seed)


def test_gcr_known_values():
    assert gcr.encode([0, 0, 0, 0]).tolist() == [0x52, 0x94, 0xA5, 0x29, 0x4A]
    assert gcr.encode([0x08, 0, 0, 0])[0] == 0x52
    assert gcr.encode([0x07, 0, 0, 0])[0] == 0x55
    assert gcr.encode([0xFF] * 4).tolist() == [0xAD, 0x6B, 0x5A, 0xD6, 0xB5]


def test_gcr_code_constraints():
    codes = np.array([[int(c) for c in f"{v:05b}"] for v in gcr.GCR_ENCODE])
    assert len(set(gcr.GCR_ENCODE.tolist())) == 16
    pairs = np.concatenate(
        (np.repeat(codes, 16, axis=0), np.tile(codes, (16, 1))), axis=1
    )
    zero_runs = np.lib.stride_tricks.sliding_window_view(pairs, 3, axis=1).sum(-1)
    one_runs = np.lib.stride_tricks.sliding_window_view(pairs, 9, axis=1).sum(-1)
    assert zero_runs.min() > 0
    assert one_runs.max() < 9


def test_gcr_roundtrip_and_invalid():
    data = _rng().integers(0, 256, 1000, dtype=np.uint8)
    out, valid = gcr.decode(gcr.encode(data))
    assert np.array_equal(out, data) and valid.all()
    bits = gcr.encode_bits(data[:8]).reshape(2, 40)
    bits[1, 10:15] = 0
    out, valid = gcr.decode_bits(bits)
    assert out.shape == (2, 4)
    assert valid.tolist() == [[True] * 4, [True, False, True, True]]


def test_bits_helpers():
    bits = gcr.to_bits([0x80, 0x01])
    assert bits.tolist() == [1] + [0] * 14 + [1]
    assert gcr.to_bytes(gcr.rotate(bits, 15)).tolist() == [0xC0, 0x00]


def test_runs_of_ones():
    bits = np.array([1] * 12 + [0] + [1] * 9 + [0] + [1] * 3, np.uint8)
    starts, lengths = gcr.runs_of_ones(bits)
    assert starts.tolist() == [0] and lengths.tolist() == [12]
    starts, lengths = gcr.runs_of_ones(bits, circular=True)
    assert starts.tolist() == [23] and lengths.tolist() == [15]
    assert gcr.sync_mask(bits, circular=True).sum() == 15
    assert gcr.sync_mask(bits).sum() == 12
    starts, lengths = gcr.runs_of_ones(np.ones(20, np.uint8), circular=True)
    assert starts.tolist() == [0] and lengths.tolist() == [20]
    assert len(gcr.runs_of_ones(np.ones(5, np.uint8), circular=True)[0]) == 0


def test_zones():
    assert [gcr.speed_zone(t) for t in (1, 17, 18, 24, 25, 30, 31, 40)] == [
        3,
        3,
        2,
        2,
        1,
        1,
        0,
        0,
    ]
    assert sum(gcr.sectors_per_track(t) for t in range(1, 36)) == 683
    assert sum(gcr.sectors_per_track(t) for t in range(1, 41)) == 768
    assert [gcr.track_capacity(z) for z in range(4)] == [6250, 6666, 7142, 7692]
    assert gcr.bit_rate(0) == 250000
    assert gcr.bits_per_revolution(3) == pytest.approx(61538.46, abs=0.01)


def test_header_and_data_checksums():
    hdr = header_blocks(18, np.arange(19), DISK_ID)
    assert hdr[0].tolist() == [8, 18 ^ 0x42 ^ 0x41, 0, 18, 0x42, 0x41, 15, 15]
    assert not np.bitwise_xor.reduce(hdr[:, 1:6], axis=1).any()
    payload = _rng().integers(0, 256, (3, 256), dtype=np.uint8)
    blk = data_blocks(payload)
    assert blk.shape == (3, 260) and (blk[:, 0] == 7).all()
    assert not np.bitwise_xor.reduce(blk[:, 1:258], axis=1).any()


@pytest.mark.parametrize("track", [1, 17, 18, 24, 25, 30, 31, 35, 40])
def test_format_decode_roundtrip(track):
    n = gcr.sectors_per_track(track)
    payload = _rng(track).integers(0, 256, (n, 256), dtype=np.uint8)
    raw = format_track(track, payload, DISK_ID)
    assert len(raw) == gcr.track_capacity(gcr.speed_zone(track))
    out = decode_track(gcr.to_bits(raw), track, DISK_ID)
    assert (out.errors == SectorError.OK).all()
    assert np.array_equal(out.data, payload)
    assert [bytes(i) for i in out.ids] == [DISK_ID] * n
    assert (np.diff(out.offsets) > 0).all()


@pytest.mark.parametrize(
    "error,code",
    [
        (SectorError.HEADER_NOT_FOUND, 20),
        (SectorError.DATA_NOT_FOUND, 22),
        (SectorError.DATA_CHECKSUM, 23),
        (SectorError.BAD_GCR, 24),
        (SectorError.HEADER_CHECKSUM, 27),
        (SectorError.ID_MISMATCH, 29),
    ],
)
def test_error_injection(error, code):
    payload = _rng().integers(0, 256, (21, 256), dtype=np.uint8)
    errors = np.full(21, SectorError.OK, np.uint8)
    errors[7] = error
    bits = gcr.to_bits(format_track(3, payload, DISK_ID, errors))
    out = decode_track(bits, 3, DISK_ID)
    assert out.errors.tolist() == errors.tolist()
    assert SectorError(out.errors[7]).dos_code == code
    if error == SectorError.DATA_CHECKSUM:
        assert np.array_equal(out.data[7], payload[7])


def test_bad_gcr_in_header():
    payload = np.zeros((21, 256), np.uint8)
    raw = format_track(1, payload, DISK_ID)
    sector_bytes = len(raw) // 21
    raw[sector_bytes * 4 + 5 + 6 : sector_bytes * 4 + 5 + 8] = 0
    out = decode_track(gcr.to_bits(raw), 1, DISK_ID)
    assert out.errors[4] == SectorError.BAD_GCR
    assert (np.delete(out.errors, 4) == SectorError.OK).all()


def test_no_sync_and_noise():
    payload = np.zeros((21, 256), np.uint8)
    errors = np.full(21, SectorError.OK, np.uint8)
    errors[0] = SectorError.NO_SYNC
    out = decode_track(gcr.to_bits(format_track(1, payload, DISK_ID, errors)), 1)
    assert (out.errors == SectorError.NO_SYNC).all()
    assert (decode_track(np.ones(1000, np.uint8), 1).errors == 3).all()
    noise = _rng().integers(0, 2, 61538, dtype=np.uint8)
    assert (decode_track(noise, 1).errors == SectorError.HEADER_NOT_FOUND).all()


def test_wrong_track_header_not_found():
    payload = np.zeros((21, 256), np.uint8)
    out = decode_track(gcr.to_bits(format_track(2, payload, DISK_ID)), 1)
    assert (out.errors == SectorError.HEADER_NOT_FOUND).all()


def test_duplicate_headers_prefer_good_copy():
    payload = _rng().integers(0, 256, (21, 256), dtype=np.uint8)
    bad = np.full(21, SectorError.DATA_CHECKSUM, np.uint8)
    raw = np.concatenate(
        (format_track(1, payload, DISK_ID, bad), format_track(1, payload, DISK_ID))
    )
    out = decode_track(gcr.to_bits(raw), 1)
    assert (out.errors == SectorError.OK).all()
    assert (out.offsets >= 8 * len(raw) // 2).all()


def test_format_overflow():
    with pytest.raises(ValueError):
        format_track(1, np.zeros((21, 256), np.uint8), DISK_ID, capacity=7000)
