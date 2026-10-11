"""1571 sync sensor pass (drive/tss.s): records, their timing and the sync pulses.

The drive reads SYNC every 6 to 13 cycles at 2 MHz and records each pulse with
VIA1 T2 stamps and the byte-ready count since the previous record; a rising index
edge is a record too. Cycle counts here are drive cycles at 2 MHz.
"""

import dataclasses

import numpy as np

from .passes import CELLS, DRIFT, _events_offsets, _low_rows, _placed, _syncs
from .passes import capable, sync_weights
from .unstable import absorbable

RECLEN = 8
K_SHORT, K_LONG, K_INDEX = 4, 5, 6
ST_KILLER, ST_TIMEOUT, ST_FULL, ST_SLOW = 0x04, 0x10, 0x40, 0x80
STATUS = {ST_KILLER: "killer", ST_TIMEOUT: "timeout", ST_FULL: "full", ST_SLOW: "slow"}
REC_PAGE, REC_END_PAGE = 0x60, 0x80
ZP, ZP_SIZE = 0x60, 13
PARAMS = {"revs": 0, "tmo": 1, "rend": 2, "status": 3, "ri": 4, "rpg": 5, "wdst": 6}
CPU_HZ = 2_000_000

SEEN_GAP = 13  # SYNC reads at most this far apart outside a seen sequence
EXIT = np.array([0, 7, 17, 26, 36])  # short kind k: SYNC high at R + EXIT[k]
PREV = np.array([0, 0, 7, 17, 26])  # and still low at R + PREV[k]
STAMP_LO = np.array([0, 14, 13, 13, 13, 13, 0])  # R -> T2 lo read, per kind
STAMP_HI = np.array([0, 12, 19, 28, 19, 19, 7])  # T2 lo read -> hi read, per kind
RELEASE_FIRST = 50  # R -> the release loop's first read
RELEASE_GAP = 11  # release loop reads at most this far apart
END_LO, END_HI = 7, 4  # release read -> end lo read -> hi read
INDEX_POLL_BYTES = 8  # the index is polled every this many bytes
TAIL_BLIND = 140  # cycles after a release read before SYNC is read again
FALL = 8  # bytes a placement may fall between syncs (the counts are exact)


def t2_value(lo, hi, delay):
    """T2 at the lo read, from a lo/hi pair read ``delay`` cycles apart."""
    lo, hi = np.asarray(lo, np.int64), np.asarray(hi, np.int64)
    return ((hi + (lo < delay)) & 0xFF) << 8 | lo


@dataclasses.dataclass
class Records:
    """A sensor pass: per record its kind, T2 at the read that saw the event
    (``seen``: R for a sync; for an index record the WD1770 read, up to
    INDEX_POLL_BYTES bytes after the edge), T2 at the read that saw SYNC high
    again (``end``, long syncs) and the bytes counted since the previous record."""

    kind: np.ndarray
    seen: np.ndarray
    end: np.ndarray
    count: np.ndarray

    @classmethod
    def parse(cls, raw):
        """Records from the drive's bytes."""
        rec = np.frombuffer(bytes(raw), np.uint8).reshape(-1, RECLEN).astype(np.int64)
        kind = rec[:, 0]
        seen = (
            t2_value(rec[:, 1], rec[:, 2], STAMP_HI[kind]) + STAMP_LO[kind]
        ) & 0xFFFF
        end = (t2_value(rec[:, 3], rec[:, 4], END_HI) + END_LO) & 0xFFFF
        return cls(kind, seen, end, rec[:, 5] | rec[:, 6] << 8)

    @property
    def cumulative(self):
        """Bytes counted before each record since the pass started."""
        return np.cumsum(self.count)

    @property
    def is_sync(self):
        """Which records are syncs."""
        return self.kind <= K_LONG

    @property
    def index(self):
        """Bytes counted before each index record."""
        return self.cumulative[self.kind == K_INDEX]

    def pulses(self, cell=None):
        """``(count, estimate, lo, hi)`` per sync: bytes before it and its SYNC
        low cycles, the bounds inclusive; with ``cell`` (cycles per cell) a sync
        that may have started inside the previous record's blind tail gets the
        tail added to its upper bound; a sync already low at the pass's first
        read is unbounded."""
        sel = self.is_sync
        kind = self.kind[sel]
        count = self.cumulative[sel]
        low = np.where(kind == K_LONG, (self.seen[sel] - self.end[sel]) & 0xFFFF, 0)
        gap = np.where(low == RELEASE_FIRST, RELEASE_FIRST - EXIT[K_SHORT], RELEASE_GAP)
        lo = np.where(kind == K_LONG, low - gap, PREV[np.minimum(kind, K_SHORT)])
        hi = np.where(kind == K_LONG, low, EXIT[np.minimum(kind, K_SHORT)]) + SEEN_GAP
        hi = hi.astype(float)
        if cell is not None:
            hi += np.maximum(TAIL_BLIND - 8 * cell * self.count[sel], 0)
        if len(hi) and self.count[sel][0] <= 1 and self.kind[0] <= K_LONG:
            hi[0] = np.inf
        lo = np.maximum(lo, 0).astype(float)
        est = np.where(np.isinf(hi), lo, (lo + hi) / 2)
        return count, est, lo, hi

    def cells(self, cell):
        """Sync pulse width in cells (SYNC low cycles over ``cell`` cycles) per sync."""
        _, est, _, _ = self.pulses(cell)
        return est / cell


@dataclasses.dataclass
class Sensed:
    """One sensor pass as the drive left it: the record bytes, its status and the
    density it ran at."""

    raw: np.ndarray
    status: int
    density: int

    @property
    def records(self):
        """The parsed records."""
        return Records.parse(self.raw)

    def attach(self, cap):
        """The stream capture ``cap`` with this pass attached: its syncs are placed
        on the bytes by their byte counts from the index (:func:`place`)."""
        return dataclasses.replace(cap, sensed=self.raw, sensed_status=self.status)


def place(  # pylint: disable=too-many-arguments
    data, base, count, pulse, cell, anchored=True, revolution=None, rows=()
):
    """Syncs of a capture from a sensor pass: ``count`` bytes before each sensed
    sync from the pass start at BITS index ``base``, ``pulse`` = (estimate, lo,
    hi) SYNC low cycles, ``cell`` cycles per cell, placed like TS syncs
    (:func:`nybulah.passes.merge_ts`) alongside any other ``rows``."""
    data = np.asarray(data, np.uint8)
    ok, latched = capable(data)
    evidence = (ok, sync_weights(ok, latched), absorbable(data, latched))
    count = np.asarray(count, np.int64)
    est, plo, phi = (np.asarray(v, float) for v in pulse)
    keep = ~at_edge(base + count, len(data), revolution) if anchored else slice(None)
    count, est, plo, phi = count[keep], est[keep], plo[keep], phi[keep]
    rise = np.floor(phi / (CELLS * cell * (1 - DRIFT))).astype(np.int64)
    offs, matched = _events_offsets(
        count, base, evidence, anchored, revolution, rise, fall_max=FALL
    )
    pos, sel = _placed(data, base, count, offs, matched, revolution)
    rows = list(rows) + _low_rows(pos, est[sel], plo[sel], phi[sel], cell, np.inf)
    lost = int((~matched).sum()) + int(np.size(keep) - np.count_nonzero(keep))
    return _syncs(rows, latched, (0, len(data)), None, lost)


def at_edge(nominal, n, revolution=None):
    """Which anchored events, placed up to INDEX_POLL_BYTES after their nominal
    position, would straddle an end of ``n`` bytes (or a copy a ``revolution``
    away): the sync there has no byte after it inside the data."""
    nominal = np.asarray(nominal, np.int64)
    hit = np.zeros(len(nominal), bool)
    for end in (n, 0):
        d = end - nominal
        d = d % revolution if revolution else d
        hit |= (d >= 0) & (d <= INDEX_POLL_BYTES)
    return hit


def status_names(status):
    """Names of the status bits set."""
    return [name for bit, name in STATUS.items() if status & bit]
