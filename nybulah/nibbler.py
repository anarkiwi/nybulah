"""Raw track capture and write through any monitor transport (drive/track.s host side),
and streaming capture on a 1571 with xum1541 firmware v12 (drive/stream.s)."""

import dataclasses
import json
import math
import struct
import time

import numpy as np
from tqdm import tqdm

from . import passes
from . import stream as fmt
from .analysis.capture import _hidden, capture_bits, trailing_ones
from .analysis.gcr import SYNC_MIN_BITS, bit_rate, bits_per_revolution, speed_zone
from .analysis.gcr import to_bits
from .analysis.sector import header_tracks
from .monitor import drivecode

CPU_HZ = 1_000_000
CODE_BASE, CODE_SIZE = 0x0300, 0x0200
PREP, READ = CODE_BASE, CODE_BASE + 3
ZP, ZP_PARAMS, ZP_SIZE = 0x60, 17, 34
RESULT = {"status": 17, "endpg": 18, "endy": 19, "tfirst": 20, "tlast": 22}
RESULT |= {"idx1": 24, "idx2": 26, "count": 28, "wdst": 33}
RESULT_V1 = {"status": 9, "endpg": 12, "endy": 13, "tfirst": 14, "tlast": 16}
RESULT_V1 |= {"idx1": 18, "idx2": 20}
BUFPG = {"1541": 0x80, "1571": 0x60}
NPAGES = 31
ST_NOSYNC, ST_NOINDEX, ST_KILLER, ST_WPROT = 0x01, 0x02, 0x04, 0x08
ST_TIMEOUT, ST_NOANCHOR, ST_FULL = 0x10, 0x20, 0x40
ST_STOPPED = ST_NOSYNC | ST_KILLER | ST_TIMEOUT
ST_STOPPED_TS = ST_STOPPED | ST_FULL
START = {"now": 0, "sync": 1, "index": 2, "anchor": 3}
USER_STARTS = ("now", "sync", "index")
BITS, TB, TS = 0, 1, 2
TIMING = {"full": (TB, TS), "syncs": (TS,), "none": ()}
CAPTURE_VERSION = 2
STREAM_VERSION = 4
WD_INDEX = 0x02

VIA1PA, T2CL, ACR1 = 0x1801, 0x1808, 0x180B
DOS_TRACK = 0x22
SENSE_CODE, SN_TRK00 = "sense_1571", 0x04
SEEK_CODE, STREAM_CODE = "seek_1571", "stream_1571"
STREAM_ZP, ZP_STREAM_SIZE = 0x84, 0x100 - ZP  # stream.s ZPCODE; ZP saved through $FF
SENSE_STREAM = CODE_BASE + 0x100  # inside the seek code's padding (no expansion RAM)
STREAM_HZ = 2_000_000
T1_PERIOD = 62_500  # monitor.s WD_PERIOD: VIA1 T1 cycles per tick
RPM_MIN = passes.RPM_MIN
SR_PERIOD = 40  # drive/stream.s: cycles between shift register writes
VIA2PB, PCR2 = 0x1C00, 0x1C0C
PA_SIDE, PA_2MHZ, ACR_T2_PULSE = 0x04, 0x20, 0x20
PCR_SOE_MASK, PCR_SOE_OFF = 0xF1, 0x0C
PB_MOTOR_LED, PB_PHASE = 0x0C, 0x03

T2_HI_DELAY = 4

STEP_MS, SETTLE_MS, SPINUP_S = 5, 20, 0.5
HOME_HALFTRACK, HOME_STEPS = 2, 88
PHASE_OFFSET = 2
PHASE_NUDGE = (0, 1, -2, -1)
SENSOR_EDGE = HOME_HALFTRACK + PB_PHASE  # DOS takes the first phase 0 detent sensed
SENSOR_CLEAR = HOME_HALFTRACK + 1
HT_OUTER = HOME_HALFTRACK - PB_PHASE  # DOS takes the stop at phase 0 as track 1
SENSOR_WALK = SENSOR_EDGE + 1 - HT_OUTER
MAX_HALFTRACK = 84
MAX_DOS_TRACK = MAX_HALFTRACK // 2

V1_TAB_N, V1_STRIDE = 50, 51
V1_SEEN_TO_T2, V1_RELEASE_TO_T2 = 11, 7
V1_POLL, V1_BYTE_TO_POLL = 9, 24
V1_RELEASE_GAPS = (6, 11)
V1_LOOP, V1_WAIT_BASE = 17, 65
V1_ACCURACY = 3


class TrackError(IOError):
    """The drive refused or could not complete a track operation."""


def t2_value(lo, hi):
    """Timer 2 at the low byte read, from a low/high pair read T2_HI_DELAY apart."""
    lo, hi = np.asarray(lo, np.int64), np.asarray(hi, np.int64)
    return ((hi + (lo < T2_HI_DELAY)) & 0xFF) << 8 | lo


def unwrap(earlier, later, expected):
    """Cycles from T2 reading earlier to later, the 16-bit wrap chosen nearest expected."""
    d = (int(earlier) - int(later)) & 0xFFFF
    return d + 0x10000 * max(round((expected - d) / 0x10000), 0)


def cell_cycles(density, rpm=300.0):
    """CPU cycles per bit cell at a density, for a disk written at 300 rpm."""
    return CPU_HZ / bit_rate(density) * 300.0 / rpm


def stored(result):
    """Bytes a BITS or TB pass stored, from its result block."""
    return (NPAGES - int(result[RESULT["endpg"]])) * 256 + int(result[RESULT["endy"]])


def _u8(value):
    if value is None:
        return None
    raw = value if isinstance(value, bytes) else np.asarray(value, np.uint8)
    return np.frombuffer(bytes(raw), np.uint8)


@dataclasses.dataclass
class Capture:  # pylint: disable=too-many-instance-attributes
    """One capture: BITS bytes, the TB and TS passes, their result blocks, context.

    Records hold raw drive output, so save/load is lossless and everything else is
    derived. ``base``: BITS index of TB/TS byte 0 (-1: by search); ``table``: a version
    1 sync table; ``stream``: a version 3 record's adapter output (``data`` decoded).
    """

    data: np.ndarray
    table: np.ndarray
    result: np.ndarray
    model: str
    halftrack: int
    side: int
    density: int
    start: str
    pages: int
    tb: np.ndarray | None = None
    tb_result: np.ndarray | None = None
    ts: np.ndarray | None = None
    ts_result: np.ndarray | None = None
    base: int = -1
    version: int = CAPTURE_VERSION
    stream: np.ndarray | None = None

    ARRAYS = ("data", "table", "result", "tb", "tb_result", "ts", "ts_result")
    ARRAYS += ("stream",)

    def __post_init__(self):
        for name in self.ARRAYS:
            setattr(self, name, _u8(getattr(self, name)))
        self._syncs = None
        self.parsed = None if self.stream is None else fmt.Stream.parse(self.stream)

    @property
    def index(self):
        """Stream: data positions of the rising index edges, else None."""
        return None if self.parsed is None else self.parsed.index

    def index_bits(self):
        """Stream: bit offsets in :meth:`bits` of the index edges, else None."""
        if self.parsed is None:
            return None
        keep = self.positions < self.valid_bytes
        pos = self.positions[keep]
        _, _, extra = _hidden(to_bits(self.data), pos, self.sync_bits[keep])
        before = np.searchsorted(pos, self.index, side="right")
        cum = np.concatenate(([0], np.cumsum(extra)))
        return 8 * self.index + cum[before]

    @property
    def stream_status(self):
        """Stream: how the adapter and the drive ended it ("done" both when whole)."""
        p = self.parsed
        return None if p is None else {"adapter": p.adapter, "drive": p.drive_end}

    def _r(self, key, result=None):
        offsets = RESULT if self.version >= 2 else RESULT_V1
        return int((self.result if result is None else result)[offsets[key]])

    @property
    def status(self):
        """Drive status bits (ST_*) of every pass; a stream maps its end onto them."""
        if self.parsed is not None:
            if self.parsed.complete:
                return 0
            return ST_NOINDEX if self.parsed.drive_end == "noindex" else ST_TIMEOUT
        blocks = [
            r for r in (self.result, self.tb_result, self.ts_result) if r is not None
        ]
        return int(np.bitwise_or.reduce([self._r("status", r) for r in blocks]))

    @property
    def cell_cycles(self):
        """Nominal CPU cycles per bit cell at the capture density."""
        return cell_cycles(self.density)

    @property
    def syncs(self):
        """Syncs located in the BITS bytes (:class:`nybulah.passes.Syncs`)."""
        if self._syncs is None:
            self._syncs = self._derive()
        return self._syncs

    def ts_syncs(self):
        """TS pass as ``(count, release-wait iterations)`` per sync."""
        tc, th, td = self.ts.reshape(3, 256)[:, : self._r("endy", self.ts_result)]
        return passes.ts_syncs(tc, th, td)

    def _ts_count(self):
        off = RESULT["count"]
        return int(self.ts_result[off]) | int(self.ts_result[off + 1]) << 8

    def _derive(self):
        if self.parsed is not None:
            cell = STREAM_HZ / CPU_HZ * self.cell_cycles
            return passes.stream_syncs(self.data, self.parsed.syncs(cell), cell)
        if self.version < 2:
            return v1_syncs(self)
        anchored, base = self.base >= 0, max(self.base, 0)
        ts = self.ts_syncs() if self.ts is not None else None
        rev = self.revolution_bytes() if ts is not None or self.tb is not None else None
        if self.tb is not None:
            end = None
            if ts is None or self._r("status", self.ts_result) & ST_STOPPED_TS:
                end = self._ts_count() if ts is not None else 0
            return passes.merge_tb(self.data, base, self.tb, ts, anchored, rev, end)
        if ts is not None:
            return passes.merge_ts(self.data, base, ts, self.cell_cycles, anchored, rev)
        none = np.zeros(0, np.int64)
        return passes.Syncs(none, none, none, none, none, len(self.data))

    def revolution_bytes(self):
        """Bytes per revolution: a stream's index spacing, else the BITS bytes' own
        repetition, or None."""
        if self.parsed is not None:
            idx = self.parsed.index
            return int(np.median(np.diff(idx))) if len(idx) > 1 else None
        return passes.byte_period(self.data, bits_per_revolution(self.density))

    @property
    def positions(self):
        """Bytes captured before each sync."""
        return self.syncs.positions

    @property
    def sync_bits(self):
        """Sync run lengths in bits (latched and hidden ones)."""
        return self.syncs.runs

    @property
    def sync_bounds(self):
        """``(lo, hi)`` run lengths each sync allows; a negative ``hi`` is unbounded."""
        return self.syncs.lo, self.syncs.hi

    @property
    def sync_error(self):
        """Bits a run may be off by, the widest of its bounds (unbounded runs aside)."""
        if self.version < 2:
            return V1_ACCURACY
        syncs = self.syncs
        high = np.where(syncs.hi >= 0, syncs.hi - syncs.runs, 0)
        return int(np.maximum(syncs.runs - syncs.lo, high).max(initial=0))

    @property
    def latched(self):
        """Ones of each sync run latched in the bytes before it."""
        return self.syncs.latched

    @property
    def hidden(self):
        """Sync ones never latched into a byte, per sync."""
        return self.syncs.hidden

    @property
    def lost(self):
        """TB or TS syncs that matched no boundary of the BITS bytes."""
        return self.syncs.unmatched

    @property
    def valid_bytes(self):
        """Bytes whose sync context is known."""
        return self.syncs.valid

    def bits(self):
        """The captured bit stream with sync runs restored."""
        lead = SYNC_MIN_BITS if self.start == "sync" else 0
        keep = self.positions < self.valid_bytes
        data = self.data[: self.valid_bytes]
        return capture_bits(data, self.positions[keep], self.sync_bits[keep], lead)

    @property
    def revolution_cycles(self):
        """Revolution time in CPU cycles: 1571 index period, else TB across the repeat."""
        if self.parsed is not None:
            return None
        if self.start == "index" and not self.status & ST_NOINDEX:
            return unwrap(self.t2("idx1"), self.t2("idx2"), 60.0 * CPU_HZ / 300.0)
        rev = self.revolution_bytes() if self.tb is not None else None
        if rev is None or rev >= len(self.tb):
            return None
        reads = self._tb_reads()
        return float(np.median(reads[rev:] - reads[:-rev]))

    def _tb_reads(self):
        """TB T2 read times, with the timer wraps TS resolves."""
        timing = self.syncs.timing
        return passes.tb_arrivals(self.tb).read if timing is None else timing[1].read

    @property
    def rpm(self):
        """Motor speed from the revolution time, else None."""
        rev = self.revolution_cycles
        return None if rev is None else 60.0 * CPU_HZ / rev

    @property
    def byte_cycles(self):
        """Mean CPU cycles per latched byte: TB's local periods, else BITS end times."""
        if self.parsed is not None:
            return None
        if self.version >= 2 and self.syncs.byte_cycles:
            return self.syncs.byte_cycles
        if self.start == "sync" or not self.data.size:
            return None
        t0 = self.t2("idx2" if self.start == "index" else "tfirst")
        hidden = float(np.sum(self.hidden)) * self.cell_cycles
        expected = len(self.data) * CELLS_PER_BYTE * self.cell_cycles + hidden
        return (unwrap(t0, self.t2("tlast"), expected) - hidden) / len(self.data)

    def t2(self, key):
        """Timer 2 value at a result block's lo/hi pair (``tfirst``, ``tlast``, ``idx1``...)."""
        off = (RESULT if self.version >= 2 else RESULT_V1)[key]
        return t2_value(self.result[off], self.result[off + 1])

    @property
    def overrun_risk(self):
        """Bytes arrived faster than the BITS loop is proven to keep up with."""
        cycles = self.byte_cycles
        return cycles is not None and cycles <= passes.BITS_MIN_PERIOD

    def save(self, path):
        """Write the capture as a compressed .npz record."""
        meta = {f.name: getattr(self, f.name) for f in dataclasses.fields(self)}
        arrays = {k: meta.pop(k) for k in self.ARRAYS}
        arrays = {k: v for k, v in arrays.items() if v is not None}
        np.savez_compressed(path, meta=json.dumps(meta), **arrays)

    @classmethod
    def load(cls, path):
        """Read a capture written by save, of any version."""
        with np.load(path) as f:
            meta = json.loads(str(f["meta"]))
            meta.setdefault("version", 1)
            arrays = {k: f[k] for k in cls.ARRAYS if k in f}
            return cls(**arrays, **meta)


CELLS_PER_BYTE = 8


def index_sense(cap):
    """1571 ``(wd_status, index_level)``: the WD1770 status a capture's index sensing
    began from (a stream's first read, or prep's after taking type I status) and a
    version 4 stream's index level (status bit 1) at its end; None where unknown."""
    if cap.model != "1571":
        return None, None
    if cap.parsed is not None:
        if cap.version < 4:
            return None, None
        return int(cap.result[1]), int(cap.result[2] & WD_INDEX) >> 1
    at = RESULT["wdst"]
    return (int(cap.result[at]) if len(cap.result) > at else None), None


def v1_syncs(cap):
    """Syncs of a version 1 record: one combined pass with a polled sync table."""
    t = cap.table.astype(np.int64)
    tsl, pl, ph, tel, cnt = (V1_STRIDE * i for i in range(5))
    lost = int(cap.result[11])
    n = min(int(cap.result[10]), V1_TAB_N) - (1 if lost else 0)
    positions = (cap.pages - t[ph : ph + n]) * 256 + t[pl : pl + n]
    latched = trailing_ones(to_bits(cap.data), 8 * positions)
    fine = (t[tsl : tsl + n] - t[tel : tel + n]) & 0xFF
    loops = (t[cnt : cnt + n] - t[pl : pl + n]) & 0xFF
    delta = fine + 256 * np.round((loops * V1_LOOP + V1_WAIT_BASE - fine) / 256)
    lead = SYNC_MIN_BITS - np.minimum(latched, SYNC_MIN_BITS - 1)
    poll = np.arange(V1_POLL)[:, None] + V1_BYTE_TO_POLL - lead * cap.cell_cycles
    seen = np.where(poll >= 0, poll, poll % V1_POLL).mean(axis=0)
    gaps = np.asarray(V1_RELEASE_GAPS, float)
    cycles = (
        delta
        + seen
        + V1_SEEN_TO_T2
        - V1_RELEASE_TO_T2
        - (gaps**2).sum() / (2 * gaps.sum())
    )
    byte = _v1_byte_cycles(cap, cycles)
    cell = byte / CELLS_PER_BYTE if byte else cap.cell_cycles
    runs = np.maximum(np.round(cycles / cell).astype(np.int64), 1) + SYNC_MIN_BITS - 1
    valid = len(cap.data)
    if lost:
        valid = (cap.pages - int(t[ph + n])) * 256 + int(t[pl + n])
    return passes.Syncs(
        positions,
        runs,
        runs - V1_ACCURACY,
        runs + V1_ACCURACY,
        latched,
        valid,
        byte,
        lost,
    )


def _v1_byte_cycles(cap, sync_cycles):
    if cap.start == "sync" or not cap.data.size:
        return None
    t0 = cap.t2("idx2" if cap.start == "index" else "tfirst")
    t1 = cap.t2("tlast")
    syncs = float(np.sum(sync_cycles))
    expected = len(cap.data) * CELLS_PER_BYTE * cap.cell_cycles + syncs
    return (unwrap(t0, t1, expected) - syncs) / len(cap.data)


STREAM_REVS = 1


def stream_size(revolutions):
    """Adapter output bound for a stream: every byte escaped, writes SR_PERIOD apart,
    the slowest motor, the revolutions plus the partial ones at either end."""
    per_rev = 60 / RPM_MIN * STREAM_HZ / SR_PERIOD
    return 64 * math.ceil(2 * per_rev * (revolutions + 2) / 64)


class Nibbler:  # pylint: disable=too-many-instance-attributes
    """Track-level access to one drive through a started monitor.

    The head position is tracked on the host and found by ``locate()`` the first time
    it is needed. Only a 1541 is ever bumped against the stop, when ``allow_bump`` is
    set and nothing else places it. ``stream`` (default: when the drive, link and
    adapter allow it) captures by streaming instead of the expansion RAM passes.
    """

    def __init__(
        self,
        mon,
        model,
        stepms=STEP_MS,
        settle_ms=SETTLE_MS,
        spinup_s=SPINUP_S,
        sleep=time.sleep,
        allow_bump=False,
        stream=None,
    ):
        if model not in BUFPG:
            raise ValueError(f"unsupported model {model}")
        self.mon, self.model = mon, model
        self.stepms, self.settle_ms, self.spinup_s, self.sleep = (
            stepms,
            settle_ms,
            spinup_s,
            sleep,
        )
        self.allow_bump = allow_bump
        streaming = self._can_stream() if stream is None else bool(stream)
        if streaming and not self._can_stream():
            raise ValueError("streaming needs a 1571, s4 and xum1541 firmware v12")
        self._streaming, self._overlay = streaming, None
        self.halftrack = None
        self.motor = self._sensing = False
        self._saved = None

    @property
    def buffer(self):
        """Address of the capture/write buffer."""
        return BUFPG[self.model] << 8

    @property
    def streaming(self):
        """Captures stream (drive/stream.s) instead of using expansion RAM."""
        return self._streaming

    @property
    def _track_code(self):
        return f"track_{self.model}"

    def _can_stream(self):
        supports = getattr(getattr(self.mon, "cbm", None), "supports", None)
        return (
            self.model == "1571"
            and getattr(self.mon, "protocol", None) == "s4"
            and bool(supports and supports("stream"))
        )

    def _load(self, overlay):
        """Put the track, seek (prep) or stream code at CODE_BASE.

        The track code's PASS page goes after the expansion RAM buffer and
        must read back; the stream code's tail goes to zero page.
        """
        if self._overlay == overlay:
            return
        code = drivecode(overlay)
        self._overlay = None
        self.mon.write(CODE_BASE, code[:CODE_SIZE])
        tail = code[CODE_SIZE:]
        if overlay == self._track_code:
            at = self.buffer + NPAGES * 256
            self.mon.write(at, tail)
            if bytes(self.mon.read(at, len(tail))) != tail:
                raise TrackError(f"no expansion RAM at ${at:04X} for the track code")
        elif tail:
            self.mon.write(STREAM_ZP, tail)
        self._overlay, self._sensing = overlay, False

    def open(self):
        """Load the routines and set up VIA state; saves everything close restores."""
        mon = self.mon
        self._overlay = None
        if self.streaming:
            self._load(SEEK_CODE)
            self._saved = [(ZP, mon.read(ZP, ZP_STREAM_SIZE))]
        else:
            self._load(self._track_code)
            self._saved = [(ZP, mon.read(ZP, ZP_SIZE))]
        self._saved.append((PCR2, mon.read(PCR2, 1)))
        if self.model == "1571":
            pa = mon.read(VIA1PA, 1)
            self._saved.append((VIA1PA, pa))
            mon.write(VIA1PA, bytes([pa[0] & ~PA_2MHZ & 0xFF]))
        acr = mon.read(ACR1, 1)[0]
        mon.write(ACR1, bytes([acr & ~ACR_T2_PULSE & 0xFF]))
        mon.write(T2CL, b"\xff\xff")
        pcr = mon.read(PCR2, 1)[0]
        mon.write(PCR2, bytes([pcr & PCR_SOE_MASK | PCR_SOE_OFF]))
        return self

    def close(self):
        """Stop the motor, leave DOS's track ($22) on the head's, restore the rest."""
        if self._saved is None:
            return
        self.motor = False
        self._prep(0, 0, (self.halftrack or 0) & 1)
        if self.halftrack is not None:
            self.halftrack += self.halftrack & 1
            self.mon.write(DOS_TRACK, bytes([self.halftrack // 2]))
        for addr, value in self._saved:
            self.mon.write(addr, value)
        self._saved = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    def _params(self, anchor=b"", **kw):
        names = ("pbset", "side", "steps", "stepms", "settle", "mode", "npages")
        names += ("kind", "alen")
        values = {"stepms": self.stepms, "settle": self.settle_ms} | kw
        block = bytes(values.get(n, 0) & 0xFF for n in names)
        block += bytes(anchor).ljust(passes.ANCHOR_MAX, b"\0")
        assert len(block) == ZP_PARAMS
        self.mon.write(ZP, block)

    def _call(self, entry):
        return self.mon.jsr(entry)[0]

    def _prep(self, pbset, side, steps):
        if self._overlay == STREAM_CODE:
            self._load(SEEK_CODE)
        while True:
            chunk = max(-127, min(127, steps))
            self._params(pbset=pbset, side=side, steps=chunk)
            self._call(PREP)
            steps -= chunk
            if not steps:
                return

    def _check(self, side, start):
        if self.model == "1541" and (side or start == "index"):
            raise ValueError("side 1 and index starts need a 1571")
        if start not in USER_STARTS:
            raise ValueError(f"start must be one of {USER_STARTS}")

    def bump(self):
        """1541 only: step outwards past the stop, ending on the phase of track 1."""
        if self.model != "1541":
            raise TrackError(f"a {self.model} head is never bumped")
        phase = self.mon.read(VIA2PB, 1)[0] & PB_PHASE
        steps = HOME_STEPS + ((phase - HOME_HALFTRACK - PHASE_OFFSET) & PB_PHASE)
        self._prep(self._pbset(), 0, -steps)
        self.halftrack = HOME_HALFTRACK

    def _pbset(self, density=0):
        return (PB_MOTOR_LED | (density & 3) << 5) if self.motor else 0

    def _step(self, steps):
        if steps:
            self._prep(self._pbset(), 0, steps)

    def _phase_halftrack(self, track):
        """2 * track, moved to the nearest halftrack of the stepper's phase.

        A phase two halftracks away takes the outer of the two.
        """
        phase = self.mon.read(VIA2PB, 1)[0] & PB_PHASE
        return 2 * track + PHASE_NUDGE[(phase - 2 * track - PHASE_OFFSET) & PB_PHASE]

    def sense(self):
        """1571 ``(track 00 sensed, stepper phase)`` by DOS's debounced test, no step."""
        if self.model != "1571":
            raise ValueError("the track 00 sensor needs a 1571")
        at = SENSE_STREAM if self.streaming else self.buffer
        if self.streaming:
            self._load(SEEK_CODE)
        if not self._sensing:
            self.mon.write(at, drivecode(SENSE_CODE))
            self._sensing = True
        status = self._call(at)
        return bool(status & SN_TRK00), status & PB_PHASE

    def _sense(self, halftrack, trace):
        """sense(), refused unless consistent with the head on halftrack (None: unknown)."""
        on, phase = self.sense()
        trace.append((halftrack, on, phase))
        if halftrack is None:
            return on, phase
        if phase != (halftrack + PHASE_OFFSET) & PB_PHASE:
            raise TrackError(f"stepper phase {phase} is not halftrack {halftrack}'s")
        if (on and halftrack > SENSOR_EDGE) or (not on and halftrack <= HOME_HALFTRACK):
            state = "on" if on else "clear"
            raise TrackError(f"track 00 sensor {state} at halftrack {halftrack}")
        return on, phase

    def home(self, halftrack=None, trace=None):
        """1571: head to HOME_HALFTRACK by DOS's rule, track 00 sensed at phase 0.

        Steps inwards while the sensor is on (where it clears, the phase places an
        unknown ``halftrack``), then outwards one checked step at a time; any
        contradiction raises TrackError. ``trace`` gets each (halftrack, on, phase).
        """
        self.halftrack = None
        trace = [] if trace is None else trace
        on, phase = self._sense(halftrack, trace)
        walked = 0
        while on:
            if walked == SENSOR_WALK:
                raise TrackError(f"track 00 sensor still on {walked} steps inwards")
            self._step(1)
            walked += 1
            halftrack = None if halftrack is None else halftrack + 1
            on, phase = self._sense(halftrack, trace)
        if halftrack is None:
            if not walked:
                raise TrackError("track 00 sensor clear and nothing places the head")
            halftrack = SENSOR_CLEAR + (
                (phase - SENSOR_CLEAR - PHASE_OFFSET) & PB_PHASE
            )
            trace[-walked - 1 :] = [
                (halftrack - walked + i, *read[1:])
                for i, read in enumerate(trace[-walked - 1 :])
            ]
        for x in range(halftrack - 1, HOME_HALFTRACK - 1, -1):
            self._step(-1)
            self._sense(x, trace)
        self.halftrack = HOME_HALFTRACK
        return self.halftrack

    def _dos_track(self):
        """Drive 0's current track as DOS last left it, if plausible."""
        track = self.mon.read(DOS_TRACK, 1)[0]
        return track if 1 <= track <= MAX_DOS_TRACK else None

    def _headers_here(self, track):
        """Track number from the sector headers under the head, trying every density."""
        if not self.motor:
            self.motor = True
            self.sleep(self.spinup_s)
        zones = [speed_zone(track)] if track else []
        for density in zones + [z for z in range(4) if z not in zones]:
            self._prep(self._pbset(density), 0, 0)
            cap = (
                self._stream(density, 0, 1)
                if self.streaming
                else self._read(density, "sync", 0, "syncs")
            )
            found = header_tracks(cap.bits())
            if len(found):
                return int(np.bincount(found).argmax())
        return None

    def estimate(self, headers=True):
        """Halftrack under the head from its headers, else DOS's track, and the phase."""
        dos = self._dos_track()
        track = (self._headers_here(dos) if headers else None) or dos
        return None if track is None else self._phase_halftrack(track)

    def search(self, steps):
        """Step outwards one halftrack at a time, at most ``steps``, until headers
        place the head or a 1571's track 00 sensor turns on: ``(halftrack, taken)``,
        halftrack None for the sensor; TrackError when neither does."""
        for taken in tqdm(
            range(1, steps + 1), desc="search outwards", unit="step", leave=False
        ):
            self._step(-1)
            if self.model == "1571" and self.sense()[0]:
                return None, taken
            track = self._headers_here(None)
            if track:
                return self._phase_halftrack(track), taken
        raise TrackError(f"nothing placed the head within {steps} outward steps")

    def locate(self, search=0):
        """Find the head position without touching the stop.

        With no estimate (and on a 1571 the sensor clear), the head first
        searches up to ``search`` halftracks outwards (``search``): the caller's
        promise that it is at least ``search + 2`` halftracks from the stop. A
        1571 is then homed (``home``). A 1541 with no position raises TrackError,
        unless ``allow_bump``.
        """
        estimate = self.estimate()
        if estimate is None and search:
            if self.model != "1571" or not self.sense()[0]:
                estimate = self.search(search)[0]
        if self.model == "1571":
            return self.home(estimate)
        if estimate is not None:
            self.halftrack = estimate
        elif self.allow_bump:
            self.bump()
        else:
            raise TrackError(
                "head position unknown (no DOS track or headers);"
                " allow_bump permits a bump against the stop"
            )
        return self.halftrack

    def seek(self, halftrack, density=None, side=0):
        """Motor on, density and side selected, head on halftrack."""
        if not 2 <= halftrack <= MAX_HALFTRACK:
            raise ValueError(f"halftrack {halftrack} out of range")
        if density is None:
            density = speed_zone(halftrack // 2)
        spinning = self.motor
        if self.halftrack is None:
            self.locate()
        self.motor = True
        self._prep(
            self._pbset(density), PA_SIDE if side else 0, halftrack - self.halftrack
        )
        self.halftrack = halftrack
        if not spinning:
            self.sleep(self.spinup_s)
        return density

    def capture(self, halftrack, density=None, start="now", side=0, timing="full"):
        """Capture NPAGES pages of raw bytes and locate their syncs.

        ``start`` is "now", "sync" (just after a sync) or "index" (1571).
        ``timing`` "full" times every sync (TB and TS passes), "syncs" only
        locates them (TS), "none" keeps the bytes alone.
        """
        self._check(side, start)
        if timing not in TIMING:
            raise ValueError(f"timing must be one of {tuple(TIMING)}")
        density = self.seek(halftrack, density, side)
        return self._read(density, start, side, timing)

    def scan(self, halftrack, side=0, density=None):
        """A capture that decodes the track: a stream of STREAM_REVS revolutions, or
        BITS and TS passes from just after a sync."""
        if self.streaming:
            return self.stream(halftrack, STREAM_REVS, side, density)
        return self.capture(halftrack, density, "sync", side, "syncs")

    def stream(self, halftrack, revolutions=STREAM_REVS, side=0, density=None):
        """Stream the track from now until ``revolutions`` whole index-to-index
        revolutions have passed (drive/stream.s, 1571 at 2 MHz, firmware v12).

        Returns a version 3 :class:`Capture`; ``stream_status`` reports how the
        adapter (overrun, framing, timeout) and the drive ended it.
        """
        if not self.streaming:
            raise TrackError("streaming needs a 1571, s4 and xum1541 firmware v12")
        density = self.seek(halftrack, density, side)
        return self._stream(density, side, revolutions)

    def _stream(self, density, side, revolutions):
        self._load(STREAM_CODE)
        ticks = math.ceil(2 * 60 / RPM_MIN * STREAM_HZ / T1_PERIOD) + 1
        self._params(mode=ticks, npages=revolutions + 1)
        size = stream_size(revolutions)
        mon = self.mon
        mon.set_fast(True)
        try:
            mon.transact(b"J" + struct.pack("<H", CODE_BASE))
            raw = mon.cbm.srq2_stream(size)
            reply = mon.link.response(3)
            mon.touch()
        finally:
            mon.set_fast(False)
        parsed = fmt.Stream.parse(raw)
        return Capture(
            parsed.data,
            b"",
            reply,
            self.model,
            self.halftrack,
            side,
            density,
            "now",
            0,
            version=STREAM_VERSION,
            stream=raw,
        )

    def _pass(self, kind, mode, anchor=b""):
        self._load(self._track_code)
        self._sensing = False
        self._params(
            mode=mode, npages=NPAGES, kind=kind, alen=len(anchor), anchor=anchor
        )
        self._call(READ)
        return np.frombuffer(self.mon.read(ZP, ZP_SIZE), np.uint8)

    def _read(self, density, start, side, timing="full"):
        """BITS pass, then the timing passes; a "now" capture whose bytes show no
        anchor is retaken from a sync, since bytes before the first sync after a
        step are framed unlike every later revolution.
        """
        kinds = TIMING[timing]
        for attempt in (start, "sync") if start == "now" and kinds else (start,):
            mode = START[attempt]
            result = self._pass(BITS, mode)
            data = self.mon.read(self.buffer, stored(result))
            extra = {}
            if not kinds or not data or result[RESULT["status"]] & ST_STOPPED:
                break
            array = np.frombuffer(data, np.uint8)
            extra = self._timing(array, density, mode, kinds, mode != START["now"])
            if extra is not None:
                break
        return Capture(
            data,
            b"",
            result,
            self.model,
            self.halftrack,
            side,
            density,
            start,
            NPAGES,
            **extra,
        )

    def _timing(  # pylint: disable=too-many-arguments
        self, data, density, mode, kinds, fallback, anchored=True
    ):
        """TB and TS passes after an anchor chosen from the BITS bytes.

        Without an anchor they start like BITS did (if ``fallback``, else
        None is returned) and are aligned by search.
        """
        cell = cell_cycles(density, passes.RPM_MAX)
        rev = passes.byte_period(data, bits_per_revolution(density))
        steady = mode != START["now"]
        anchor = passes.choose_anchor(data, cell, rev, steady) if anchored else None
        if anchor is None and not fallback:
            return None
        out = {"base": -1}
        args = (mode,)
        if anchor is not None:
            out["base"], args = anchor[0], (START["anchor"], anchor[1].tobytes())
        for kind in kinds:
            result = self._pass(kind, *args)
            if result[RESULT["status"]] & ST_NOANCHOR:
                return self._timing(data, density, mode, kinds, True, False)
            name = "tb" if kind == TB else "ts"
            size = stored(result) if kind == TB else 3 * 256
            out[name], out[name + "_result"] = self.mon.read(self.buffer, size), result
        return out

    def write_track(self, halftrack, data, density=None, side=0, start="now", pad=None):
        """Write raw bytes to a track; ``pad`` left-fills to a whole number of pages."""
        self._check(side, start)
        data = bytes(data)
        if pad is not None:
            data = bytes([pad]) * (-len(data) % 256) + data
        if len(data) % 256 or not 0 < len(data) <= NPAGES * 256:
            raise ValueError(f"{len(data)} bytes is not 1..{NPAGES} whole pages")
        density = self.seek(halftrack, density, side)
        self._load(self._track_code)
        self._sensing = False
        self.mon.write(self.buffer, data)
        self._params(mode=START[start], npages=len(data) // 256)
        status = self._call(self.buffer + NPAGES * 256)
        if status & ST_WPROT:
            raise TrackError("disk is write protected")
        if status & ST_NOINDEX:
            raise TrackError("no index pulse")
        if status & ST_TIMEOUT:
            raise TrackError("byte ready stopped while writing")
        return density
