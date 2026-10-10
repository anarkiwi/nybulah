"""Streaming capture (drive/stream.s through xum1541 firmware v12): framing and syncs.

The adapter sends a drive byte as itself, data $00 as ESC $00, metadata m as ESC m,
and ends with ESC and an ``A_*`` code; cycle counts are drive cycles at 2 MHz.
"""

import dataclasses

import numpy as np
from numba import njit

from .analysis.capture import trailing_ones
from .analysis.gcr import SYNC_MIN_BITS as SYNC_ONES
from .analysis.gcr import to_bits

ESC = 0x00
KIND = 0x03
SSTART, SCONT, SEND, CONTROL = 1, 2, 3, 0
M_START, M_INDEX = 0x04, 0x08
M_END, M_END_NOINDEX, M_END_ATN = 0x40, 0x44, 0x48
A_DONE, A_OVERRUN, A_FRAMING, A_TIMEOUT, A_TRUNCATED = 0x80, 0x84, 0x88, 0x8C, 0x90
ADAPTER = {
    A_DONE: "done",
    A_OVERRUN: "overrun",
    A_FRAMING: "framing",
    A_TIMEOUT: "timeout",
    A_TRUNCATED: "truncated",
}
DRIVE_END = {M_END: "done", M_END_NOINDEX: "noindex", M_END_ATN: "atn"}

START_T2, END_T2 = (12, 12), (7, 8)  # SYNC read that saw the change -> T2 read
START_LATE, END_LATE = 125, 70  # longest wait for a SYNC read (data paths, sync loop)
NW_POLL = 15  # SYNC read period in nw
NW_SYNC = 33  # write of a byte from nw -> nw's first SYNC read after it
NW_WRITE = (12, 21)  # byte ready -> its write from nw
QUANT = 3  # T2 bits dropped by the timestamps
CPU_HZ = 2_000_000


@njit(cache=True)
def _parse(raw, data, pos, val):
    """Split adapter output into data bytes and (position, value) metadata."""
    d = e = 0
    i = 0
    code = -1
    while i < len(raw):
        b = raw[i]
        if b != ESC:
            data[d] = b
            d += 1
            i += 1
            continue
        if i + 1 == len(raw):
            break
        m = raw[i + 1]
        i += 2
        if m == ESC:
            data[d] = 0
            d += 1
        elif m & 0x83 == 0x80:
            code = m
            break
        else:
            pos[e] = d
            val[e] = m
            e += 1
    return d, e, code


@dataclasses.dataclass
class Stream:
    """A parsed stream: data bytes, metadata at data positions, how it ended."""

    data: np.ndarray
    pos: np.ndarray
    val: np.ndarray
    adapter: str

    @classmethod
    def parse(cls, raw):
        """Parse adapter output (bytes or uint8 array)."""
        raw = np.frombuffer(bytes(raw), np.uint8)
        data = np.zeros(len(raw), np.uint8)
        pos = np.zeros(len(raw) // 2 + 1, np.int64)
        val = np.zeros(len(raw) // 2 + 1, np.uint8)
        d, e, code = _parse(raw, data, pos, val)
        return cls(data[:d], pos[:e], val[:e], ADAPTER.get(code, "cut"))

    @property
    def drive_end(self):
        """How the drive ended the stream ("done", "noindex", "atn"), else None."""
        ends = [DRIVE_END[v] for v in self.val.tolist() if v in DRIVE_END]
        return ends[-1] if ends else None

    @property
    def index(self):
        """Data positions of the rising index edges."""
        return self.pos[self.val == M_INDEX]

    @property
    def index_syncs(self):
        """Per index edge, the syncs that started before it (SYNC_STARTs ahead of
        its INDEX in the stream, also at the same data position)."""
        starts = np.cumsum((self.val & KIND) == SSTART)
        return starts[self.val == M_INDEX]

    @property
    def complete(self):
        """The drive ended the stream itself and the adapter lost nothing."""
        return self.adapter == "done" and self.drive_end == "done"

    def syncs(self, cell=None):
        """``(positions, estimate, lo, hi)``: data position and SYNC low cycles;
        with ``cell`` (cycles per bit) the start lag follows the latched ones."""
        if cell is None:
            return sync_bounds(self.pos, self.val)
        ones = trailing_ones(to_bits(self.data), 8 * np.arange(len(self.data) + 1))
        return sync_bounds(self.pos, self.val, lambda p: start_lag(ones[p], cell))


def _elapsed(prev, cur):
    """T2 cycles from timestamp prev to cur (T2 counts down, under 256 apart)."""
    return (int(prev) - int(cur)) & 0xFF


def start_lag(ones, cell):
    """Expected cycles from SYNC low to the read that sees it, after a byte with
    ``ones`` trailing ones: nw's first read after the byte's write, or half a poll."""
    first = np.mean(NW_WRITE) + NW_SYNC - (SYNC_ONES - np.asarray(ones)) * cell
    return np.maximum(first, NW_POLL / 2)


def sync_bounds(pos, val, lag=None):
    """``(positions, estimate, lo, hi)`` of the syncs in a metadata sequence.

    SYNC_CONT extends the newest open sync, SYNC_END closes the oldest; times are
    SYNC low cycles (hi -1: still low at the end); lag(position) is the expected
    start lag (default half a poll).
    """
    lag = lag or (lambda p: NW_POLL / 2)
    open_ = []
    out = []
    for p, v in zip(pos.tolist(), val.tolist()):
        kind, t = v & KIND, v & 0xFC
        if kind == SSTART:
            open_.append([p, 0, t])
        elif kind == SCONT and open_:
            sync = open_[-1]
            sync[1] += _elapsed(sync[2], t)
            sync[2] = t
        elif kind == SEND and open_:
            start, acc, last = open_.pop(0)
            span = acc + _elapsed(last, t)
            lo = span - QUANT - END_T2[1] + START_T2[0] - END_LATE
            hi = span + QUANT - END_T2[0] + START_T2[1] + START_LATE
            est = span - sum(END_T2) / 2 + START_T2[0] + lag(start) - NW_POLL / 2
            out.append((start, min(max(est, lo), hi), max(lo, 0), hi))
    out += [(start, acc, acc, -1) for start, acc, _ in open_]
    rows = np.array(sorted(out), float).reshape(-1, 4)
    return rows[:, 0].astype(np.int64), rows[:, 1], rows[:, 2], rows[:, 3]
