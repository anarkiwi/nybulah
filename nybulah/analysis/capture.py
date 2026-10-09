"""Byte-ready captures: sync-delimited segments and the restored bit stream.

Bytes are framed exactly from the end of each sync, but sync lengths are only
measured to within a capture's ``sync_error`` (``SYNC_ERROR_BITS`` unless it
says otherwise), so positions are uncertain across syncs.
"""

from dataclasses import dataclass

import numpy as np

from .gcr import SYNC_MIN_BITS, runs_of_ones, to_bits

SYNC_ERROR_BITS = 3
FRAMED_SYNC_ERROR_BITS = 8


def uniform_sigma(bound):
    """Standard deviation of an integer error uniform on ``[-bound, bound]``."""
    return float(np.sqrt(((2 * bound + 1) ** 2 - 1) / 12))


def trailing_ones(bits, ends):
    """Count of consecutive one bits ending just before each index in ends."""
    zeros = np.concatenate(([-1], np.flatnonzero(np.asarray(bits) == 0)))
    ends = np.asarray(ends, np.int64)
    return ends - 1 - zeros[np.searchsorted(zeros, ends) - 1]


def _hidden(bits, positions, runs):
    ends = 8 * np.asarray(positions, np.int64)
    runs = np.maximum(np.asarray(runs, np.int64), SYNC_MIN_BITS)
    latched = trailing_ones(bits, ends)
    return ends, latched, np.maximum(runs - latched, 0)


def capture_bits(data, positions, runs, lead=0):
    """Bit stream of a capture with each sync run restored to its measured length.

    ``positions`` count the bytes captured before each sync, ``runs`` give sync
    lengths in bits (at least a hardware sync), including ones already latched.
    ``lead`` ones go first, for a capture started just after an unmeasured sync.
    """
    bits = to_bits(np.asarray(data, np.uint8))
    ends, _, extra = _hidden(bits, positions, runs)
    bits = np.insert(bits, np.repeat(ends, extra), 1)
    return np.concatenate((np.ones(lead, np.uint8), bits))


@dataclass(frozen=True)
class Segments:
    """A byte-ready capture as sync-delimited segments.

    Segment ``k`` is ``data[first[k]:first[k + 1]]``, starting at bit ``begin[k]``
    of the restored stream ``bits``; its sync run starts at ``run[k]`` (-1: unmeasured).
    Each measured sync length is within ``error`` bits.
    """

    data: np.ndarray
    bits: np.ndarray
    first: np.ndarray
    begin: np.ndarray
    run: np.ndarray
    error: int = SYNC_ERROR_BITS

    def __len__(self):
        return len(self.first)

    @property
    def lengths(self):
        """Bytes per segment."""
        return np.diff(np.append(self.first, len(self.data)))

    @property
    def content(self):
        """Bits from each segment's start to the next sync run (the last: to the end)."""
        return np.append(self.run[1:], len(self.bits)) - self.begin

    def matrix(self):
        """Segments as rows of an ``int16`` matrix padded with -1."""
        lengths = self.lengths
        out = np.full((len(self), max(int(lengths.max(initial=0)), 1)), -1, np.int16)
        out[np.arange(out.shape[1]) < lengths[:, None]] = self.data[
            self.first[0] if len(self) else len(self.data) :
        ]
        return out


@dataclass(frozen=True)
class ByteCapture:
    """Latched bytes, the bytes before each sync and each sync's measured length."""

    data: np.ndarray
    positions: np.ndarray
    sync_bits: np.ndarray
    start: str = "sync"
    sync_error: int = SYNC_ERROR_BITS


def framed_capture(data, sync_error=FRAMED_SYNC_ERROR_BITS):
    """ByteCapture of a raw track whose syncs are stored as one bits (NIB).

    Syncs ending on a byte boundary restart framing; their whole 0xFF bytes are
    dropped and the run kept as the sync length. A run reaching the end is filler.
    """
    data = np.asarray(data, np.uint8)
    starts, lengths = runs_of_ones(to_bits(data))
    ends = starts + lengths
    if len(ends) and ends[-1] == 8 * len(data) and starts[-1]:
        data, starts, ends = data[: starts[-1] // 8], starts[:-1], ends[:-1]
    keep = (ends % 8 == 0) & (ends < 8 * len(data))
    starts, ends = starts[keep], ends[keep]
    inner = -(-starts // 8), ends // 8
    drop = np.concatenate([np.arange(a, b) for a, b in zip(*inner)] + [[]])
    gone = np.cumsum(inner[1] - inner[0])
    head = int(len(starts) > 0 and starts[0] == 0)
    return ByteCapture(
        np.delete(data, drop.astype(np.int64)),
        (inner[1] - gone)[head:],
        (ends - starts)[head:],
        "sync" if head else "now",
        sync_error,
    )


def segments(capture):
    """Segments of a byte-ready capture.

    ``capture`` needs ``data``, ``positions``, ``sync_bits`` and ``start`` and
    optionally ``valid_bytes``, as ``nibbler.Capture`` and :class:`ByteCapture`
    have. Bytes before the first sync are unframed unless ``start == "sync"``.
    """
    data = np.asarray(capture.data, np.uint8)
    data = data[: getattr(capture, "valid_bytes", len(data))]
    positions = np.asarray(capture.positions, np.int64)
    lead = SYNC_MIN_BITS if capture.start == "sync" else 0
    bits = to_bits(data)
    ends, latched, extra = _hidden(bits, positions, capture.sync_bits)
    begin = ends + lead + np.cumsum(extra)
    run = begin - latched - extra
    stream = np.concatenate(
        (np.ones(lead, np.uint8), np.insert(bits, np.repeat(ends, extra), 1))
    )
    if lead:
        positions = np.concatenate(([0], positions))
        begin = np.concatenate(([lead], begin))
        run = np.concatenate(([-1], run))
    error = getattr(capture, "sync_error", SYNC_ERROR_BITS)
    return Segments(data, stream, positions, begin, run, error)
