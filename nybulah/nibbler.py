"""Raw track capture and write through any monitor transport (drive/track.s host side)."""

import dataclasses
import json
import time

import numpy as np

from .analysis.capture import capture_bits, trailing_ones
from .analysis.gcr import SYNC_MIN_BITS, bit_rate, speed_zone, to_bits
from .analysis.sector import header_tracks
from .monitor import drivecode

CPU_HZ = 1_000_000
CODE_BASE = 0x0300
PREP, READ, WRITE = (CODE_BASE + 3 * i for i in range(3))
ZP, ZP_PARAMS, ZP_SIZE = 0x60, 9, 27
BUFPG = {"1541": 0x80, "1571": 0x60}
NPAGES = 31
TAB_N, TAB_STRIDE = 50, 51
TSL, PL, PH, TEL, CNT = (TAB_STRIDE * i for i in range(5))
ST_NOSYNC, ST_NOINDEX, ST_KILLER, ST_WPROT = 0x01, 0x02, 0x04, 0x08
START = {"now": 0, "sync": 1, "index": 2}

VIA1PA, T2CL, ACR1, VIA1PA_NH = 0x1801, 0x1808, 0x180B, 0x180F
DOS_TRACK = 0x22
PA_TRK0 = 0x01
VIA2PB, PCR2 = 0x1C00, 0x1C0C
PA_SIDE, PA_2MHZ, ACR_T2_PULSE = 0x04, 0x20, 0x20
PCR_SOE_MASK, PCR_SOE_OFF = 0xF1, 0x0C
PB_MOTOR_LED, PB_PHASE = 0x0C, 0x03

T2_HI_DELAY = 4
SYNC_SEEN_TO_T2 = 11
RELEASE_SEEN_TO_T2 = 7
SYNC_POLL_GAPS = (9,)
BYTE_TO_POLL = 24
RELEASE_POLL_GAPS = (6, 11)
SYNC_LOOP = 17
SYNC_WAIT_BASE = 65
READ_PAIR_CYCLES = 46

STEP_MS, SETTLE_MS, SPINUP_S = 5, 20, 0.5
HOME_HALFTRACK, HOME_STEPS = 2, 88
PHASE_OFFSET = 2
PHASE_NUDGE = (0, 1, 0, -1)
MAX_HALFTRACK = 84
MAX_DOS_TRACK = MAX_HALFTRACK // 2


class TrackError(IOError):
    """The drive refused or could not complete a track operation."""


def mean_latency(gaps):
    """Mean delay from an edge to the poll that sees it, for cyclic poll gaps."""
    g = np.asarray(gaps, float)
    return float((g**2).sum() / (2 * g.sum()))


def sync_latency(lead):
    """Mean delay from SYNC to the poll that sees it, SYNC lead cycles after a byte.

    The byte's service ends with a poll read BYTE_TO_POLL cycles after it is seen,
    seen within one poll period; later reads follow every poll period.
    """
    (period,) = SYNC_POLL_GAPS
    first = np.arange(period)[:, None] + BYTE_TO_POLL - np.asarray(lead, float)
    return np.where(first >= 0, first, first % period).mean(axis=0)


def t2_value(lo, hi):
    """Timer 2 at the low byte read, from a low/high pair read T2_HI_DELAY apart."""
    lo, hi = np.asarray(lo, np.int64), np.asarray(hi, np.int64)
    return ((hi + (lo < T2_HI_DELAY)) & 0xFF) << 8 | lo


def unwrap(earlier, later, expected):
    """Cycles from T2 reading earlier to later, the 16-bit wrap chosen nearest expected."""
    d = (int(earlier) - int(later)) & 0xFFFF
    return d + 0x10000 * max(round((expected - d) / 0x10000), 0)


@dataclasses.dataclass
class Capture:  # pylint: disable=too-many-instance-attributes
    """One read: the raw bytes, the drive's sync table and result block, and context.

    Everything else is derived, so save/load round trips losslessly.
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

    def __post_init__(self):
        self.data = np.frombuffer(bytes(self.data), np.uint8)
        self.table = np.frombuffer(bytes(self.table), np.uint8)
        self.result = np.frombuffer(bytes(self.result), np.uint8)

    def _r(self, off):
        return int(self.result[off])

    @property
    def status(self):
        """Drive status bits (ST_*)."""
        return self._r(9)

    @property
    def lost(self):
        """Syncs the full table could not record."""
        return self._r(11)

    @property
    def cell_cycles(self):
        """Nominal CPU cycles per bit cell at the capture density."""
        return CPU_HZ / bit_rate(self.density)

    def _entries(self):
        """Sync table entries usable for reconstruction (the last is dropped on overflow)."""
        n = min(self._r(10), TAB_N)
        return n - 1 if self.lost else n

    @property
    def positions(self):
        """Bytes captured before each recorded sync."""
        n = self._entries()
        ph = self.table[PH : PH + n].astype(np.int64)
        return (self.pages - ph) * 256 + self.table[PL : PL + n]

    @property
    def latched(self):
        """Ones of each sync run already latched in the byte before it."""
        return trailing_ones(to_bits(self.data), 8 * self.positions)

    @property
    def sync_cycles(self):
        """SYNC durations in CPU cycles, corrected for the expected poll latencies."""
        n = self._entries()
        t = self.table.astype(np.int64)
        fine = (t[TSL : TSL + n] - t[TEL : TEL + n]) & 0xFF
        loops = (t[CNT : CNT + n] - t[PL : PL + n]) & 0xFF
        coarse = loops * SYNC_LOOP + SYNC_WAIT_BASE
        delta = fine + 256 * np.round((coarse - fine) / 256)
        lead = SYNC_MIN_BITS - np.minimum(self.latched, SYNC_MIN_BITS - 1)
        seen = sync_latency(lead * self.cell_cycles)
        offset = SYNC_SEEN_TO_T2 - RELEASE_SEEN_TO_T2 - mean_latency(RELEASE_POLL_GAPS)
        return delta + seen + offset

    @property
    def sync_bits(self):
        """Sync run lengths in bits: SYNC spans all but the first nine ones of a run."""
        cell = self.byte_cycles / 8 if self.byte_cycles else self.cell_cycles
        cells = np.round(self.sync_cycles / cell).astype(np.int64)
        return np.maximum(cells, 1) + SYNC_MIN_BITS - 1

    @property
    def hidden(self):
        """Sync ones never latched into a byte, per recorded sync."""
        return np.maximum(self.sync_bits - self.latched, 0)

    @property
    def valid_bytes(self):
        """Bytes whose sync context is fully known (all, unless the table overflowed)."""
        if not self.lost:
            return len(self.data)
        n = min(self._r(10), TAB_N) - 1
        return (self.pages - int(self.table[PH + n])) * 256 + int(self.table[PL + n])

    def bits(self):
        """The captured bit stream with sync runs restored."""
        lead = SYNC_MIN_BITS if self.start == "sync" else 0
        data = self.data[: self.valid_bytes]
        return capture_bits(data, self.positions, self.sync_bits, lead)

    @property
    def revolution_cycles(self):
        """Index-to-index time in CPU cycles (start="index" on a 1571), else None."""
        if self.start != "index" or self.status & ST_NOINDEX:
            return None
        idx = t2_value(self.result[[18, 20]], self.result[[19, 21]])
        return unwrap(idx[0], idx[1], 60.0 * CPU_HZ / 300.0)

    @property
    def rpm(self):
        """Motor speed from the index period, else None."""
        rev = self.revolution_cycles
        return None if rev is None else 60.0 * CPU_HZ / rev

    @property
    def byte_cycles(self):
        """Mean CPU cycles per latched byte over the capture (start now or index)."""
        if self.start == "sync" or not self.data.size:
            return None
        first = 18 + 2 if self.start == "index" else 14
        t0 = t2_value(self.result[first], self.result[first + 1])
        t1 = t2_value(self.result[16], self.result[17])
        syncs = float(np.sum(self.sync_cycles))
        expected = len(self.data) * 8 * self.cell_cycles + syncs
        return (unwrap(t0, t1, expected) - syncs) / len(self.data)

    @property
    def overrun_risk(self):
        """Bytes arrived faster than the read loop's two-byte worst case allows."""
        cycles = self.byte_cycles
        return cycles is not None and 2 * cycles <= READ_PAIR_CYCLES

    def save(self, path):
        """Write the capture as a compressed .npz record."""
        meta = {f.name: getattr(self, f.name) for f in dataclasses.fields(self)}
        arrays = {k: meta.pop(k) for k in ("data", "table", "result")}
        np.savez_compressed(path, meta=json.dumps(meta), **arrays)

    @classmethod
    def load(cls, path):
        """Read a capture written by save."""
        with np.load(path) as f:
            return cls(f["data"], f["table"], f["result"], **json.loads(str(f["meta"])))


class Nibbler:
    """Track-level access to one drive through a started monitor.

    The head position is tracked on the host and found by ``locate()`` the
    first time it is needed. Bumping the head against the stop happens only
    when ``allow_bump`` is set and nothing else can place it. ``sleep`` waits
    for the spindle after the motor starts.
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
        self.halftrack = None
        self.motor = False
        self._saved = None

    @property
    def buffer(self):
        """Address of the capture/write buffer."""
        return BUFPG[self.model] << 8

    @property
    def table_addr(self):
        """Address of the sync table page."""
        return (BUFPG[self.model] + NPAGES) << 8

    def open(self):
        """Load the routines and set up VIA state; saves everything close restores."""
        mon = self.mon
        mon.write(CODE_BASE, drivecode(f"track_{self.model}"))
        self._saved = [(ZP, mon.read(ZP, ZP_SIZE)), (PCR2, mon.read(PCR2, 1))]
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
        """Stop the motor and restore what open saved."""
        if self._saved is None:
            return
        self._prep(0, 0, 0)
        self.motor = False
        for addr, value in self._saved:
            self.mon.write(addr, value)
        self._saved = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    def _params(self, **kw):
        names = ("pbset", "side", "steps", "stepms", "settle", "mode", "npages")
        names += ("marker", "mmask")
        values = {"stepms": self.stepms, "settle": self.settle_ms} | kw
        block = bytes(values.get(n, 0) & 0xFF for n in names)
        assert len(block) == ZP_PARAMS
        self.mon.write(ZP, block)

    def _call(self, entry):
        return self.mon.jsr(entry)[0]

    def _prep(self, pbset, side, steps):
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
        if start not in START:
            raise ValueError(f"start must be one of {tuple(START)}")

    def bump(self):
        """Step outwards past the stop, ending on the phase of track 1."""
        phase = self.mon.read(VIA2PB, 1)[0] & PB_PHASE
        steps = HOME_STEPS + ((phase - HOME_HALFTRACK - PHASE_OFFSET) & PB_PHASE)
        self._prep(self._pbset(), 0, -steps)
        self.halftrack = HOME_HALFTRACK

    def _pbset(self, density=0):
        return (PB_MOTOR_LED | (density & 3) << 5) if self.motor else 0

    def _phase_halftrack(self, track):
        """2 * track, moved to an adjacent halftrack when the stepper phase says so.

        A phase two halftracks away is ambiguous and leaves 2 * track.
        """
        phase = self.mon.read(VIA2PB, 1)[0] & PB_PHASE
        return 2 * track + PHASE_NUDGE[(phase - 2 * track - PHASE_OFFSET) & PB_PHASE]

    def _track0(self):
        """1571 track 0 sensor (VIA1 PA0 low), read without clearing the ATN flag."""
        return not self.mon.read(VIA1PA_NH, 1)[0] & PA_TRK0

    def _to_sensor(self):
        """1571: step out one halftrack at a time until the track 0 sensor trips."""
        for _ in range(MAX_HALFTRACK - HOME_HALFTRACK + 1):
            if self._track0():
                return True
            self._prep(self._pbset(), 0, -1)
        return False

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
            found = header_tracks(self._read(density, "sync", 0, None).bits())
            if len(found):
                return int(np.bincount(found).argmax())
        return None

    def locate(self):
        """Find the head position: 1571 track 0 sensor, else headers and DOS's track.

        Raises TrackError when nothing places the head, unless ``allow_bump``.
        """
        if self.model == "1571" and self._to_sensor():
            self.halftrack = HOME_HALFTRACK
            return self.halftrack
        dos = self._dos_track()
        track = self._headers_here(dos) or dos
        if track is not None:
            self.halftrack = self._phase_halftrack(track)
        elif self.allow_bump:
            self.bump()
        else:
            raise TrackError(
                "head position unknown (no track 0 sensor, DOS track or headers);"
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

    def capture(self, halftrack, density=None, start="now", side=0, marker=None):
        """Capture NPAGES pages of raw bytes and the sync table.

        ``start`` is "now", "sync" (after a sync, optionally followed by the
        byte ``marker`` given as (value, mask)) or "index" (1571).
        """
        self._check(side, start)
        density = self.seek(halftrack, density, side)
        return self._read(density, start, side, marker)

    def _read(self, density, start, side, marker):
        value, mask = marker or (0, 0)
        self._params(mode=START[start], npages=NPAGES, marker=value, mmask=mask)
        self._call(READ)
        result = np.frombuffer(self.mon.read(ZP, ZP_SIZE), np.uint8)
        size = (NPAGES - int(result[12])) * 256 + int(result[13])
        return Capture(
            self.mon.read(self.buffer, size),
            self.mon.read(self.table_addr, 256),
            result,
            self.model,
            self.halftrack,
            side,
            density,
            start,
            NPAGES,
        )

    def write_track(self, halftrack, data, density=None, side=0, start="now", pad=None):
        """Write raw bytes to a track; ``pad`` left-fills to a whole number of pages."""
        self._check(side, start)
        data = bytes(data)
        if pad is not None:
            data = bytes([pad]) * (-len(data) % 256) + data
        if len(data) % 256 or not 0 < len(data) <= NPAGES * 256:
            raise ValueError(f"{len(data)} bytes is not 1..{NPAGES} whole pages")
        self.mon.write(self.buffer, data)
        density = self.seek(halftrack, density, side)
        self._params(mode=START[start], npages=len(data) // 256)
        status = self._call(WRITE)
        if status & ST_WPROT:
            raise TrackError("disk is write protected")
        if status & ST_NOINDEX:
            raise TrackError("no index pulse")
        return density
