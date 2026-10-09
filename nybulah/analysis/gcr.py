"""Commodore 4-to-5 GCR codec, bit-stream helpers, sync detection and zones."""

import numpy as np

GCR_ENCODE = np.array(
    [
        0x0A,
        0x0B,
        0x12,
        0x13,
        0x0E,
        0x0F,
        0x16,
        0x17,
        0x09,
        0x19,
        0x1A,
        0x1B,
        0x0D,
        0x1D,
        0x1E,
        0x15,
    ],
    dtype=np.uint8,
)
GCR_DECODE = np.full(32, -1, dtype=np.int16)
GCR_DECODE[GCR_ENCODE] = np.arange(16)

SYNC_MIN_BITS = 10
CLOCK_HZ = 16_000_000
NOMINAL_RPM = 300.0
SECTORS_PER_ZONE = (17, 18, 19, 21)
ZONE_BOUNDARIES = (18, 25, 31)

_SHIFT5 = np.arange(4, -1, -1, dtype=np.uint8)
_WEIGHT5 = (1 << _SHIFT5).astype(np.int16)


def to_bits(data):
    """Unpack bytes (MSB first) into a uint8 array of 0/1 bits."""
    return np.unpackbits(np.asarray(data, dtype=np.uint8))


def to_bytes(bits):
    """Pack 0/1 bits into bytes, zero-padding the final byte."""
    return np.packbits(np.asarray(bits, dtype=np.uint8))


def rotate(bits, offset):
    """Rotate a circular bit stream so that ``bits[offset]`` comes first."""
    return np.roll(bits, -int(offset))


def encode_bits(data):
    """GCR-encode bytes into a bit array of 10 bits per byte."""
    data = np.asarray(data, dtype=np.uint8)
    nibbles = np.stack((data >> 4, data & 0x0F), axis=-1).reshape(-1)
    return ((GCR_ENCODE[nibbles][:, None] >> _SHIFT5) & 1).astype(np.uint8).ravel()


def encode(data):
    """GCR-encode bytes; every 4 input bytes produce 5 output bytes."""
    return to_bytes(encode_bits(data))


def decode_bits(bits):
    """Decode GCR bits shaped ``(..., 10 * n)`` into ``(bytes, valid)``.

    ``valid`` flags bytes whose two 5-bit codes are both legal GCR.
    """
    bits = np.asarray(bits, dtype=np.uint8)
    codes = bits.reshape(*bits.shape[:-1], -1, 5).astype(np.int16) @ _WEIGHT5
    nib = GCR_DECODE[codes].reshape(*codes.shape[:-1], -1, 2)
    valid = (nib >= 0).all(axis=-1)
    nib = nib & 0x0F
    return ((nib[..., 0] << 4) | nib[..., 1]).astype(np.uint8), valid


def decode(gcr):
    """Decode GCR bytes; trailing bits that do not form a full byte are dropped."""
    bits = to_bits(gcr)
    return decode_bits(bits[: len(bits) // 10 * 10])


def runs_of_ones(bits, min_len=SYNC_MIN_BITS, circular=False):
    """Return ``(starts, lengths)`` of runs of at least ``min_len`` one bits.

    In circular mode a run may wrap past the end; an all-ones stream is a
    single run starting at 0 spanning the whole stream.
    """
    bits = np.asarray(bits, dtype=np.uint8)
    n = len(bits)
    shift = 0
    if circular:
        zeros = np.flatnonzero(bits == 0)
        if len(zeros) == 0:
            full = n >= min_len
            return np.zeros(int(full), np.int64), np.full(int(full), n, np.int64)
        shift = int(zeros[0])
        bits = np.roll(bits, -shift)
    edges = np.diff(np.concatenate(([0], bits, [0])).astype(np.int8))
    starts = np.flatnonzero(edges == 1)
    lengths = np.flatnonzero(edges == -1) - starts
    keep = lengths >= min_len
    starts = (starts[keep] + shift) % max(n, 1)
    order = np.argsort(starts, kind="stable")
    return starts[order].astype(np.int64), lengths[keep][order].astype(np.int64)


def sync_mask(bits, circular=False):
    """Boolean mask of bits that belong to a sync mark."""
    n = len(bits)
    starts, lengths = runs_of_ones(bits, circular=circular)
    delta = np.zeros(n + 1, np.int32)
    ends = starts + lengths
    np.add.at(delta, starts, 1)
    np.add.at(delta, np.minimum(ends, n), -1)
    wrap = ends > n
    np.add.at(delta, np.zeros(int(wrap.sum()), np.int64), 1)
    np.add.at(delta, ends[wrap] - n, -1)
    return np.cumsum(delta[:n]) > 0


def speed_zone(track):
    """Standard 1541 density zone (3 = fastest) for a whole track number."""
    return 3 - int(np.searchsorted(ZONE_BOUNDARIES, track, side="right"))


def sectors_per_track(track):
    """Number of sectors DOS formats on ``track``."""
    return SECTORS_PER_ZONE[speed_zone(track)]


def bit_rate(zone):
    """Bit cell rate in bits/s: the 16 MHz clock divided by 4 * (16 - zone)."""
    return CLOCK_HZ / (4 * (16 - zone))


def bits_per_revolution(zone, rpm=NOMINAL_RPM):
    """Bit cells per revolution at ``rpm``."""
    return bit_rate(zone) * 60.0 / rpm


def track_capacity(zone, rpm=NOMINAL_RPM):
    """Whole GCR bytes that fit in one revolution at ``rpm``."""
    return int(bits_per_revolution(zone, rpm) // 8)
