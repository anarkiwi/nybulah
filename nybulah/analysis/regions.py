"""Intervals of one circular revolution: syncs, the blocks and gaps after them,
and runs of illegal GCR, with the bytes each one decodes to.

No thresholds are applied here; :mod:`nybulah.survey` measures these intervals
and :mod:`nybulah.analysis.diskmap` classifies them.
"""

from dataclasses import dataclass
from enum import IntEnum
from typing import NamedTuple

import numpy as np

from .gcr import SYNC_MIN_BITS, decode_bits, runs_of_ones
from .sector import (
    DATA_CHECKED_BYTES,
    DATA_GCR_BYTES,
    DATA_ID,
    HEADER_CHECKED_BYTES,
    HEADER_GCR_BYTES,
    HEADER_ID,
    SECTOR_DATA,
)

GROUP_BITS = 40
HEADER_BITS = 8 * HEADER_GCR_BYTES
DATA_BITS = 8 * DATA_GCR_BYTES
ILLEGAL_ZEROS = 3
_BYTE = np.arange(256)
ROTATION_CLASS = np.min(
    [(_BYTE << r | _BYTE >> (8 - r)) & 0xFF for r in range(8)], axis=0
)


class Block(IntEnum):
    """What follows a sync, by its first GCR byte (NONE: a track without syncs)."""

    NONE = 0
    HEADER = 1
    DATA = 2
    OTHER = 3


SKIP_BITS = np.array([0, HEADER_BITS, DATA_BITS, 0])


def ranges(starts, counts, step=1):
    """Concatenated ``starts[i] + step * arange(counts[i])``."""
    counts = np.asarray(counts, np.int64)
    offsets = np.arange(counts.sum()) - np.repeat(np.cumsum(counts) - counts, counts)
    return np.repeat(np.asarray(starts, np.int64), counts) + step * offsets


def chains(starts, ends, link=GROUP_BITS):
    """Sorted intervals joined when at most ``link`` bits apart: ``(starts, ends, members)``."""
    starts, ends = np.asarray(starts, np.int64), np.asarray(ends, np.int64)
    if len(starts) == 0:
        return starts, ends, np.zeros(0, np.int64)
    brk = np.flatnonzero(starts[1:] - ends[:-1] > link) + 1
    first = np.concatenate(([0], brk))
    last = np.concatenate((brk - 1, [len(starts) - 1]))
    return starts[first], ends[last], last - first + 1


def longest_chain(starts, ends, link=GROUP_BITS):
    """Longest span of sorted intervals joined when at most ``link`` bits apart."""
    first, last, _ = chains(starts, ends, link)
    return int((last - first).max()) if len(first) else 0


def canonical_keep(bits):
    """Mask of the bits kept when every sync run is cut to ``SYNC_MIN_BITS`` ones."""
    bits = np.asarray(bits, np.uint8)
    starts, lengths = runs_of_ones(bits, circular=True)
    keep = np.ones(len(bits), bool)
    if len(starts) and lengths[0] >= len(bits):
        keep[SYNC_MIN_BITS:] = False
        return keep
    cut = ranges(starts + SYNC_MIN_BITS, lengths - SYNC_MIN_BITS)
    keep[cut % max(len(bits), 1)] = False
    return keep


def canonical(bits):
    """A circular bit stream with every sync run cut to ``SYNC_MIN_BITS`` ones.

    Captures frame and time syncs differently; this makes them comparable.
    """
    bits = np.asarray(bits, np.uint8)
    return bits[canonical_keep(bits)]


def agreement(ref, bits):
    """Best alignment of ``bits`` against the circular ``ref``.

    Returns ``(fraction of agreeing bits, z above chance, lag, mismatch mask)``
    over the first ``min(len(ref), len(bits))`` bits of ``bits``; bit ``i``
    is compared with ``ref[(i + lag) % len(ref)]``.
    """
    n = min(len(ref), len(bits))
    if n == 0:
        return np.nan, np.nan, 0, np.zeros(0, bool)
    ref = np.asarray(ref, np.uint8)
    probe = np.asarray(bits[:n], np.uint8)
    tiled = np.concatenate((ref, ref[:n]))
    size = 1 << (len(tiled) + n).bit_length()
    spec = np.fft.rfft(2.0 * tiled - 1, size) * np.conj(
        np.fft.rfft(2.0 * probe - 1, size)
    )
    corr = np.fft.irfft(spec, size)[: len(ref)]
    lag = int(np.argmax(corr))
    agree = (n + corr[lag]) / (2 * n)
    p, q = ref.mean(), probe.mean()
    chance = p * q + (1 - p) * (1 - q)
    z = (agree - chance) * np.sqrt(n / max(chance * (1 - chance), 1e-12))
    return float(agree), float(z), lag, probe != tiled[lag : lag + n]


def windows(bits, starts, width):
    """``bits[start:start + width]`` for each start, wrapping around the revolution."""
    return bits[(np.asarray(starts, np.int64)[:, None] + np.arange(width)) % len(bits)]


def headers_ok(hdr, valid):
    """Header blocks with legal GCR up to the ID and a zero checksum."""
    return (
        (hdr[:, 0] == HEADER_ID)
        & valid[:, :HEADER_CHECKED_BYTES].all(axis=1)
        & (np.bitwise_xor.reduce(hdr[:, 1:HEADER_CHECKED_BYTES], axis=1) == 0)
    )


class DataBlocks(NamedTuple):
    """Data blocks: block index, checksum delta, legal GCR in the checked bytes and in all."""

    index: np.ndarray
    checksum: np.ndarray
    payload: np.ndarray
    whole: np.ndarray


class Gaps(NamedTuple):
    """Gaps after each block's nominal width: block (-1: no syncs), start, bits, whole bytes."""

    block: np.ndarray
    start: np.ndarray
    length: np.ndarray
    bytes: np.ndarray


@dataclass(frozen=True)
class Revolution:
    """Intervals of one revolution ``bits``; positions wrap modulo its length.

    Block ``k`` runs ``seg_len[k]`` bits from the end of sync ``k`` and starts
    with the bytes ``hdr[k]`` (``valid``: legal GCR).
    """

    bits: np.ndarray
    sync_start: np.ndarray
    sync_len: np.ndarray
    block_kind: np.ndarray
    seg_len: np.ndarray
    hdr: np.ndarray
    valid: np.ndarray
    data: DataBlocks
    gaps: Gaps
    zero_start: np.ndarray
    zero_len: np.ndarray

    @property
    def n(self):
        """Bits in the revolution."""
        return len(self.bits)

    @property
    def killer(self):
        """One sync covers the whole revolution."""
        return bool(len(self.sync_len) and self.sync_len[0] >= self.n)

    @property
    def block_start(self):
        """Sync ends."""
        return (self.sync_start + self.sync_len) % max(self.n, 1)

    @property
    def gap_after(self):
        """Kind of the block each gap follows."""
        return np.append(self.block_kind, np.uint8(Block.NONE))[self.gaps.block]

    def gap_classes(self):
        """Per gap: dominant byte rotation class (-1 if empty) and how many bytes share it."""
        counts = self.gaps.length // 8
        owner = np.repeat(np.arange(len(counts)), counts)
        hist = np.bincount(
            owner * 256 + ROTATION_CLASS[self.gaps.bytes], minlength=256 * len(counts)
        ).reshape(len(counts), 256)
        return np.where(counts > 0, hist.argmax(axis=1), -1), hist.max(axis=1)


def _data_blocks(bits, index, starts):
    if len(starts) == 0:
        return DataBlocks(
            index, np.zeros(0, np.uint8), np.zeros(0, bool), np.zeros(0, bool)
        )
    blk, bvalid = decode_bits(windows(bits, starts, DATA_BITS))
    total = np.bitwise_xor.reduce(blk[:, 1 : 1 + SECTOR_DATA], axis=1)
    return DataBlocks(
        index,
        blk[:, 1 + SECTOR_DATA] ^ total,
        bvalid[:, :DATA_CHECKED_BYTES].all(axis=1),
        bvalid.all(axis=1),
    )


def _gaps(bits, block, start, length):
    starts = ranges(start, length // 8, 8)
    data = np.packbits(windows(bits, starts, 8), axis=1).ravel()
    return Gaps(block, start % max(len(bits), 1), length, data)


def _blocks(bits, starts, lengths):
    n = len(bits)
    ends = (starts + lengths) % n
    hdr, valid = decode_bits(windows(bits, ends, HEADER_BITS))
    is_hdr = (hdr[:, 0] == HEADER_ID) & valid[:, 0]
    kind = np.where(
        is_hdr, Block.HEADER, np.where(hdr[:, 0] == DATA_ID, Block.DATA, Block.OTHER)
    ).astype(np.uint8)
    seg = (np.roll(starts, -1) - ends) % n
    idx = np.flatnonzero(kind == Block.DATA)
    skip = SKIP_BITS[kind]
    gaps = _gaps(bits, np.arange(len(ends)), ends + skip, np.maximum(seg - skip, 0))
    return kind, seg, hdr, valid, _data_blocks(bits, idx, ends[idx]), gaps


def parse(bits):
    """:class:`Revolution` of a circular bit stream."""
    bits = np.asarray(bits, np.uint8)
    n = len(bits)
    starts, lengths = runs_of_ones(bits, circular=True)
    zeros = runs_of_ones(1 - bits, min_len=ILLEGAL_ZEROS, circular=True)
    if len(starts) and lengths[0] < n:
        blocks = _blocks(bits, starts, lengths)
        return Revolution(bits, starts, lengths, *blocks, *zeros)
    none = np.zeros(0, np.int64)
    gaps = Gaps(none, none, none, none.astype(np.uint8))
    if len(starts) == 0:
        gaps = _gaps(bits, np.array([-1]), np.array([0]), np.array([n]))
    header = (np.zeros((0, 8), np.uint8), np.zeros((0, 8), bool))
    data = _data_blocks(bits, none, none)
    blocks = (none.astype(np.uint8), none, *header, data, gaps)
    return Revolution(bits, starts, lengths, *blocks, *zeros)
