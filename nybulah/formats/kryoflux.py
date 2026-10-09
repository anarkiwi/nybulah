"""KryoFlux stream files (``trackNN.S.raw``), one per cylinder and side."""

import re

import numba
import numpy as np

from .scp import FluxTrack

MCK_HZ = 18_432_000 * 73 / 14 / 2
SCK_HZ = MCK_HZ / 2
STREAM_NAME = re.compile(r"(\d{2})\.([01])\.raw$", re.IGNORECASE)
OP_NOP1, OP_NOP2, OP_NOP3, OP_OVL16, OP_FLUX3, OP_OOB = 8, 9, 10, 11, 12, 13
OOB_STREAM_INFO, OOB_INDEX, OOB_STREAM_END, OOB_KFINFO, OOB_EOF = 1, 2, 3, 4, 13
_SCK = re.compile(rb"sck=([0-9.eE+]+)")


@numba.njit
def _oob_blocks(buf):
    """``(type, payload offset, size)`` of every out-of-band block."""
    out = np.empty((len(buf) // 4 + 1, 3), np.int64)
    n, pos = 0, 0
    while pos < len(buf):
        op = buf[pos]
        if op == OP_OOB:
            if pos + 4 > len(buf):
                break
            kind = buf[pos + 1]
            size = np.int64(buf[pos + 2]) | (np.int64(buf[pos + 3]) << 8)
            out[n, 0], out[n, 1], out[n, 2] = kind, pos + 4, size
            n += 1
            if kind == OOB_EOF:
                break
            pos += 4 + size
        elif op in (OP_FLUX3, OP_NOP3):
            pos += 3
        elif op <= 7 or op == OP_NOP2:
            pos += 2
        else:
            pos += 1
    return out[:n]


@numba.njit
def _flux(buf, index_pos, index_counter):
    """Intervals (sample clocks) and the sample time of each index pulse."""
    flux = np.empty(len(buf), np.int64)
    index = np.zeros(len(index_pos), np.int64)
    n, pos, stream, value, elapsed, k = 0, 0, 0, 0, 0, 0
    while pos < len(buf):
        while k < len(index_pos) and stream >= index_pos[k]:
            index[k] = elapsed + value + index_counter[k]
            k += 1
        op = buf[pos]
        step, emit = 1, False
        if op <= 7:
            value += (np.int64(op) << 8) + buf[pos + 1]
            step, emit = 2, True
        elif op in (OP_NOP1, OP_NOP2, OP_NOP3):
            step = op - 7
        elif op == OP_OVL16:
            value += 0x10000
        elif op == OP_FLUX3:
            value += (np.int64(buf[pos + 1]) << 8) + buf[pos + 2]
            step, emit = 3, True
        elif op == OP_OOB:
            if buf[pos + 1] == OOB_EOF:
                break
            pos += 4 + (np.int64(buf[pos + 2]) | (np.int64(buf[pos + 3]) << 8))
            continue
        else:
            value += op
            emit = True
        if emit:
            flux[n] = value
            elapsed += value
            n += 1
            value = 0
        pos += step
        stream += step
    while k < len(index_pos) and stream >= index_pos[k]:
        index[k] = elapsed + value + index_counter[k]
        k += 1
    return flux[:n], index[:k]


def read_stream(buf):
    """Parse one KryoFlux stream file into a :class:`FluxTrack`."""
    raw = np.frombuffer(bytes(buf), np.uint8)
    wide = raw.astype(np.int64)
    blocks = _oob_blocks(wide)
    sample_hz = SCK_HZ
    for _, offset, size in blocks[blocks[:, 0] == OOB_KFINFO]:
        found = _SCK.search(raw[offset : offset + size].tobytes())
        sample_hz = float(found.group(1)) if found else sample_hz
    offsets = blocks[blocks[:, 0] == OOB_INDEX, 1]
    fields = np.zeros((len(offsets), 2), np.int64)
    for row, offset in enumerate(offsets):
        fields[row] = np.frombuffer(raw, "<u4", 2, int(offset))
    intervals, times = _flux(wide, fields[:, 0], fields[:, 1])
    return FluxTrack(intervals, times.astype(np.float64), sample_hz)


def _oob(kind, payload):
    return bytes([OP_OOB, kind]) + len(payload).to_bytes(2, "little") + payload


def write_stream(track):
    """Serialise a :class:`FluxTrack` as a KryoFlux stream (sample clock kept)."""
    out, stream = bytearray(), 0
    index_times = np.asarray(track.index, np.int64)
    k, elapsed = 0, 0
    for value in np.asarray(track.intervals, np.int64).tolist():
        while k < len(index_times) and index_times[k] < elapsed + value:
            counter = int(index_times[k] - elapsed)
            fields = np.array([stream, counter, 0], "<u4").tobytes()
            out += _oob(OOB_INDEX, fields)
            k += 1
        elapsed += value
        code = bytes([OP_OVL16]) * (value >> 16)
        value &= 0xFFFF
        if 0x0E <= value <= 0xFF:
            code += bytes([value])
        elif value < 0x800:
            code += bytes([value >> 8, value & 0xFF])
        else:
            code += bytes([OP_FLUX3, value >> 8, value & 0xFF])
        out += code
        stream += len(code)
    for when in index_times[k:]:
        out += _oob(OOB_INDEX, np.array([stream, when - elapsed, 0], "<u4").tobytes())
    info = f"sck={track.sample_hz}, ick={track.sample_hz / 8}".encode() + b"\0"
    end = _oob(OOB_STREAM_END, np.array([stream, 0], "<u4").tobytes())
    return (
        _oob(OOB_KFINFO, info) + bytes(out) + end + bytes([OP_OOB, OOB_EOF, 0x0D, 0x0D])
    )
