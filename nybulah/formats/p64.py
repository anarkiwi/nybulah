"""P64 (P64-1541 version 0) flux pulse images, from the VICE manual specification.

Each halftrack holds pulse positions within one revolution (16 MHz clocks at
300 rpm) and strengths (trigger probability out of 2**32), delta-coded with
an adaptive binary range coder (FPAQ0-style, 12-bit probabilities).
"""

import zlib
from dataclasses import dataclass, field

import numba
import numpy as np

from ..analysis.flux import ROTATION_TICKS, bits_to_times
from .g64 import FIRST_HALFTRACK, SIDE1

P64_SIGNATURE = b"P64-1541"
LAST_HALFTRACK = 85
STRONG = 0xFFFFFFFF
FLAG_WRITE_PROTECT = 1
FLAG_DOUBLE_SIDED = 2
_HEADER = 24
_MASK = 0xFFFFFFFF
_TOP = 0xFF000000
_POSITION, _STRENGTH = 0, 4
_POSITION_FLAG, _STRENGTH_FLAG = 8 * 65536, 8 * 65536 + 4
_PROBS = 8 * 65536 + 8


@dataclass
class P64Track:
    """Pulse positions (ascending, < ``ROTATION_TICKS``) and strengths."""

    positions: np.ndarray
    strengths: np.ndarray = None

    def __post_init__(self):
        self.positions = np.asarray(self.positions, np.uint32)
        if self.strengths is None:
            self.strengths = np.full(len(self.positions), STRONG, np.uint32)
        self.strengths = np.asarray(self.strengths, np.uint32)


@dataclass
class P64:
    """Tracks keyed by halftrack (2 = track 1), side 1 with the ``SIDE1`` flag."""

    tracks: dict = field(default_factory=dict)
    write_protect: bool = False
    sides: int = 1


@numba.njit
def _decode_bit(state, buf, probs, slot):
    low, high, code, pos = state[0], state[1], state[2], state[3]
    prob = probs[slot]
    mid = low + ((high - low) >> 12) * prob
    if code <= mid:
        bit = 1
        probs[slot] = prob + ((0xFFF - prob) >> 4)
        high = mid
    else:
        bit = 0
        probs[slot] = prob - (prob >> 4)
        low = mid + 1
    while ((low ^ high) & _TOP) == 0:
        low = (low << 8) & _MASK
        high = ((high << 8) & _MASK) | 0xFF
        code = ((code << 8) & _MASK) | (buf[pos] if pos < len(buf) else 0)
        pos += 1
    state[0], state[1], state[2], state[3] = low, high, code, pos
    return bit


@numba.njit
def _decode_dword(state, buf, probs, ctx, model):
    value = 0
    for byte in range(4):
        context = 1
        for _ in range(8):
            slot = (model + byte) * 65536 + (
                ((ctx[model + byte] << 8) | context) & 0xFFFF
            )
            context = (context << 1) | _decode_bit(state, buf, probs, slot)
        ctx[model + byte] = context & 0xFF
        value |= (context & 0xFF) << (8 * byte)
    return value


@numba.njit
def _decode_flag(state, buf, probs, ctx, slot, model):
    ctx[model] = _decode_bit(state, buf, probs, slot + ctx[model])
    return ctx[model]


@numba.njit
def _decode_stream(buf, count):
    probs = np.full(_PROBS, 2048, np.int64)
    ctx = np.zeros(10, np.int64)
    state = np.array([0, _MASK, 0, 0], np.int64)
    for _ in range(4):
        state[2] = (state[2] << 8) | (buf[state[3]] if state[3] < len(buf) else 0)
        state[3] += 1
    positions = np.empty(count, np.int64)
    strengths = np.empty(count, np.int64)
    position, delta, strength = 0, 0, 0
    for i in range(count):
        if _decode_flag(state, buf, probs, ctx, _POSITION_FLAG, 8):
            delta = _decode_dword(state, buf, probs, ctx, _POSITION)
            if delta == 0:
                return positions[:i], strengths[:i]
        position = (position + delta) & _MASK
        if _decode_flag(state, buf, probs, ctx, _STRENGTH_FLAG, 9):
            strength = (
                strength + _decode_dword(state, buf, probs, ctx, _STRENGTH)
            ) & _MASK
        positions[i] = position
        strengths[i] = strength
    return positions, strengths


@numba.njit
def _encode_bit(state, out, probs, slot, bit):
    low, high, pos = state[0], state[1], state[3]
    prob = probs[slot]
    mid = low + ((high - low) >> 12) * prob
    if bit:
        probs[slot] = prob + ((0xFFF - prob) >> 4)
        high = mid
    else:
        probs[slot] = prob - (prob >> 4)
        low = mid + 1
    while ((low ^ high) & _TOP) == 0:
        out[pos] = high >> 24
        pos += 1
        low = (low << 8) & _MASK
        high = ((high << 8) & _MASK) | 0xFF
    state[0], state[1], state[3] = low, high, pos


@numba.njit
def _encode_dword(state, out, probs, ctx, model, value):
    for byte in range(4):
        byte_value = (value >> (8 * byte)) & 0xFF
        context = 1
        for shift in range(7, -1, -1):
            bit = (byte_value >> shift) & 1
            slot = (model + byte) * 65536 + (
                ((ctx[model + byte] << 8) | context) & 0xFFFF
            )
            _encode_bit(state, out, probs, slot, bit)
            context = (context << 1) | bit
        ctx[model + byte] = byte_value


@numba.njit
def _encode_flag(state, out, probs, ctx, slot, model, bit):
    _encode_bit(state, out, probs, slot + ctx[model], bit)
    ctx[model] = bit


@numba.njit
def _encode_stream(positions, strengths):
    probs = np.full(_PROBS, 2048, np.int64)
    ctx = np.zeros(10, np.int64)
    state = np.array([0, _MASK, 0, 0], np.int64)
    out = np.empty(16 * len(positions) + 64, np.uint8)
    last, previous, strength = 0, 0, 0
    for i, position in enumerate(positions):
        delta = (position - last) & _MASK
        if delta != previous:
            previous = delta
            _encode_flag(state, out, probs, ctx, _POSITION_FLAG, 8, 1)
            _encode_dword(state, out, probs, ctx, _POSITION, delta)
        else:
            _encode_flag(state, out, probs, ctx, _POSITION_FLAG, 8, 0)
        last = position
        if strengths[i] != strength:
            _encode_flag(state, out, probs, ctx, _STRENGTH_FLAG, 9, 1)
            _encode_dword(
                state, out, probs, ctx, _STRENGTH, (strengths[i] - strength) & _MASK
            )
        else:
            _encode_flag(state, out, probs, ctx, _STRENGTH_FLAG, 9, 0)
        strength = strengths[i]
    _encode_flag(state, out, probs, ctx, _POSITION_FLAG, 8, 1)
    _encode_dword(state, out, probs, ctx, _POSITION, 0)
    pos, high = state[3], state[1]
    for _ in range(4):
        out[pos] = high >> 24
        high = (high << 8) & _MASK
        pos += 1
    return out[:pos]


def _chunk(signature, data):
    head = (
        signature
        + np.array([len(data), zlib.crc32(data) if data else 0], "<u4").tobytes()
    )
    return head + data


def _chunks(body):
    pos = 0
    while pos + 12 <= len(body):
        signature = body[pos : pos + 4]
        size, crc = np.frombuffer(body, "<u4", 2, pos + 4)
        data = body[pos + 12 : pos + 12 + int(size)]
        if len(data) != size or (size and zlib.crc32(data) != crc):
            raise ValueError(f"P64 chunk {signature!r} is corrupt")
        yield signature, data
        pos += 12 + int(size)


def read_p64(buf):
    """Parse a P64 image, checking every CRC."""
    buf = bytes(buf)
    if buf[:8] != P64_SIGNATURE or len(buf) < _HEADER:
        raise ValueError("not a P64 image")
    version, flags, size, crc = np.frombuffer(buf, "<u4", 4, 8)
    body = buf[_HEADER : _HEADER + int(size)]
    if version != 0 or len(body) != size or zlib.crc32(body) != crc:
        raise ValueError("unsupported or corrupt P64 image")
    image = P64(
        {}, bool(flags & FLAG_WRITE_PROTECT), 2 if flags & FLAG_DOUBLE_SIDED else 1
    )
    for signature, data in _chunks(body):
        if signature[:3] != b"HTP" or len(data) < 8:
            continue
        count, coded = np.frombuffer(data, "<u4", 2)
        payload = np.frombuffer(data, np.uint8, int(coded), 8).astype(np.int64)
        positions, strengths = _decode_stream(payload, int(count))
        if len(positions) != count:
            raise ValueError(f"P64 chunk {signature!r} ends early")
        if count:
            image.tracks[signature[3]] = P64Track(positions, strengths)
    return image


def write_p64(image):
    """Serialise a P64 image with a chunk for every halftrack of every side."""
    sides = 2 if image.sides == 2 or any(k & SIDE1 for k in image.tracks) else 1
    empty = P64Track(np.zeros(0, np.uint32))
    chunks = []
    for side in range(sides):
        for halftrack in range(FIRST_HALFTRACK, LAST_HALFTRACK + 1):
            key = halftrack | (SIDE1 if side else 0)
            track = image.tracks.get(key, empty)
            coded = _encode_stream(
                track.positions.astype(np.int64), track.strengths.astype(np.int64)
            )
            head = np.array([len(track.positions), len(coded)], "<u4").tobytes()
            chunks.append(_chunk(b"HTP" + bytes([key]), head + coded.tobytes()))
    body = b"".join(chunks) + _chunk(b"DONE", b"")
    flags = FLAG_WRITE_PROTECT * image.write_protect + FLAG_DOUBLE_SIDED * (sides == 2)
    header = np.array([0, flags, len(body), zlib.crc32(body)], "<u4").tobytes()
    return P64_SIGNATURE + header + body


def track_from_bits(bits, weak=(), strength=1 << 31, zones=None):
    """One revolution of bits as pulses, cells sized by per-bit ``zones`` if given.

    ``weak`` ``(start, length)`` bit spans become a pulse per cell at
    ``strength`` (trigger probability out of 2**32).
    """
    bits = np.asarray(bits, np.uint8).copy()
    strong = np.ones(len(bits), bool)
    for start, length in weak:
        span = np.arange(start, start + length) % len(bits)
        bits[span], strong[span] = 1, False
    positions = bits_to_times(bits, zones=zones)
    ones = np.flatnonzero(bits)
    strengths = np.where(strong[ones], STRONG, strength).astype(np.uint32)
    return P64Track(positions, strengths)


def track_times(track, revolutions=1, rng=None):
    """Absolute pulse times over ``revolutions`` reads, weak pulses drawn at random.

    Returns ``(times, index)``: the transitions that fired (16 MHz clocks)
    and the index instants, one per revolution plus the end.
    """
    rng = np.random.default_rng(rng)
    n = len(track.positions)
    draws = rng.integers(0, 1 << 32, (revolutions, n), dtype=np.uint64)
    fired = (track.strengths == STRONG) | (draws < track.strengths)
    times = (
        track.positions[None].astype(np.int64)
        + ROTATION_TICKS * np.arange(revolutions)[:, None]
    )
    times = times[fired]
    first = track.positions[fired[0]][:1].astype(np.int64)
    times = np.concatenate((times, first + revolutions * ROTATION_TICKS))
    return times, ROTATION_TICKS * np.arange(revolutions + 1)
