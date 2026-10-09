"""NBZ: NIB bytes compressed with the LZ77 coder of Marcus Geelnard's BCL (zlib licence).

A stream is a marker byte, then literals; the marker introduces either 0 (a
literal marker) or a ``(length, offset)`` pair of big-endian base-128 varints
(bit 7 = continuation). References copy earlier bytes and never overlap.
"""

import numba
import numpy as np

from .nib import read_nib, write_nib

MAX_OFFSET = 100_000
MAX_CHAIN = 256
_NO_POS = -1


@numba.njit(cache=True)
def _varint(buf, pos):
    value = 0
    while pos < len(buf):
        byte = buf[pos]
        pos += 1
        value = (value << 7) | (byte & 0x7F)
        if byte < 0x80:
            return value, pos
    return -1, pos


@numba.njit(cache=True)
def _decode(buf, out):
    """Decode into ``out``, or only measure when ``out`` is empty; -1 if malformed."""
    marker = buf[0]
    pos, size, write = 1, 0, len(out) > 0
    while pos < len(buf):
        symbol = buf[pos]
        pos += 1
        if symbol != marker:
            if write:
                out[size] = symbol
            size += 1
            continue
        if pos >= len(buf):
            return -1
        if buf[pos] == 0:
            if write:
                out[size] = marker
            size += 1
            pos += 1
            continue
        length, pos = _varint(buf, pos)
        offset, pos = _varint(buf, pos)
        if length < 0 or offset <= 0 or offset > size:
            return -1
        if write:
            for i in range(length):
                out[size + i] = out[size + i - offset]
        size += length
    return size


def lz_decompress(buf):
    """Decode a BCL LZ77 stream into a uint8 array."""
    buf = np.frombuffer(bytes(buf), np.uint8).astype(np.int64)
    if len(buf) == 0:
        return np.zeros(0, np.uint8)
    size = _decode(buf, np.zeros(0, np.uint8))
    if size < 0:
        raise ValueError("malformed BCL LZ77 stream")
    out = np.empty(size, np.uint8)
    _decode(buf, out)
    return out


@numba.njit(cache=True)
def _put_varint(out, pos, value):
    groups = 1
    while value >> (7 * groups):
        groups += 1
    for i in range(groups - 1, -1, -1):
        out[pos] = ((value >> (7 * i)) & 0x7F) | (0x80 if i else 0)
        pos += 1
    return pos


@numba.njit(cache=True)
def _worthwhile(length, offset):
    """A reference is emitted only when it is shorter than the literals."""
    if length >= 8:
        return True
    limits = (0, 0, 0, 0, 0x7F, 0x3FFF, 0x1FFFFF, 0x0FFFFFFF)
    return length >= 4 and offset <= limits[length]


@numba.njit(cache=True)
def _match(src, chain, pos, max_chain):
    """Longest non-overlapping earlier match at ``pos``: ``(length, offset)``."""
    n = len(src)
    best_len, best_off = 3, 0
    cand, steps = chain[pos], 0
    while cand != _NO_POS and pos - cand < MAX_OFFSET and steps < max_chain:
        limit = min(n - pos, pos - cand)
        if limit > best_len and src[cand + best_len] == src[pos + best_len]:
            length = 2
            while length < limit and src[cand + length] == src[pos + length]:
                length += 1
            if length > best_len:
                best_len, best_off = length, pos - cand
                if length == n - pos:
                    break
        cand = chain[cand]
        steps += 1
    return best_len, best_off


@numba.njit(cache=True)
def _encode(src, marker, max_chain):
    n = len(src)
    out = np.empty(2 * n + 1, np.uint8)
    out[0] = marker
    last = np.full(65536, _NO_POS, np.int64)
    chain = np.full(n, _NO_POS, np.int64)
    for i in range(n - 1):
        pair = (np.int64(src[i]) << 8) | src[i + 1]
        chain[i] = last[pair]
        last[pair] = i
    pos, opos = 0, 1
    while pos < n:
        length, offset = _match(src, chain, pos, max_chain)
        if offset and _worthwhile(length, offset):
            out[opos] = marker
            opos = _put_varint(out, opos + 1, length)
            opos = _put_varint(out, opos, offset)
            pos += length
            continue
        out[opos] = src[pos]
        opos += 1
        if src[pos] == marker:
            out[opos] = 0
            opos += 1
        pos += 1
    return out[:opos]


def lz_compress(data, max_chain=MAX_CHAIN):
    """Encode bytes as a BCL LZ77 stream with a hash-chain match search.

    ``max_chain`` bounds the candidates tried per position, trading ratio for
    speed; every setting decodes identically.
    """
    src = np.frombuffer(bytes(data), np.uint8).astype(np.int64)
    if len(src) == 0:
        return b""
    marker = int(np.argmin(np.bincount(src, minlength=256)))
    return _encode(src, marker, max_chain).tobytes()


def read_nbz(buf, nb2=False):
    """Parse an NBZ (LZ77-compressed NIB/NB2) image."""
    return read_nib(lz_decompress(buf).tobytes(), nb2)


def write_nbz(image):
    """Serialise a NIB/NB2 image as NBZ."""
    return lz_compress(write_nib(image))
