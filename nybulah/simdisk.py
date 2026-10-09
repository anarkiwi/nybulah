"""Disk mechanism for the drive simulator: rotating media, stepper, VIA2 and WD1770 index.

Cell j of an L-cell track passes the head at angle j / L (index at angle 0); reading
follows the media's cell rate, writing the selected density's. SYNC holds the bit
counter after ten ones; each eighth cell otherwise latches a byte and raises SO.
"""

import math

import numpy as np

from .analysis.gcr import NOMINAL_RPM, bit_rate, bits_per_revolution, speed_zone
from .analysis.gcr import encode_bits, to_bits
from .sim import IO_ACCESS_CYCLE, Drive1541, Drive1571, Drive1581
from .simwd import INDEX_FRACTION

CPU_HZ = 1_000_000
HT_STOP, HT_MAX, HT_TRACK1 = 0, 84, 2
PHASE_OFFSET = 2
PHASES = 4
SENSOR_EDGES = range(HT_TRACK1, HT_TRACK1 + PHASES)
SENSOR_STUCK_ON, SENSOR_STUCK_OFF = HT_MAX, HT_STOP - 1
DOS_TRACK = 0x22
SYNC_ONES = 10
WANDER_NEWTON = 4
CELL_EPS = 1e-6
V_FLAG = 0x40
PB_PHASE, PB_MOTOR, PB_WE, PB_SYNC = 0x03, 0x04, 0x10, 0x80
PB_INPUTS = PB_WE | PB_SYNC
CA1_FLAG = 0x02
PCR_SOE, PCR_MODE, PCR_WRITE = 0x0E, 0xE0, 0xC0
WD_INDEX = 0x02
DOS_PCR, DOS_DDRB = 0xEE, 0x6F


class Media:
    """Disk surface: circular 0/1 cell arrays keyed by (side, halftrack).

    Halftracks never written read as fixed random noise, one revolution long.
    The spindle turns at ``rpm`` plus ``wander`` = (amplitude rpm, period s)
    of sinusoidal speed variation.
    """

    def __init__(self, tracks=None, rpm=NOMINAL_RPM, seed=0, wander=(0.0, 1.0)):
        self.tracks = {k: np.asarray(v, np.uint8) for k, v in (tracks or {}).items()}
        self.rpm = rpm
        self.wander = wander
        self.rng = np.random.default_rng(seed)

    def turns(self, now):
        """Revolutions since cycle 0."""
        amp, period = self.wander
        w = 2 * math.pi / (period * CPU_HZ)
        return (self.rpm * now + amp * (1 - math.cos(w * now)) / w) / (60.0 * CPU_HZ)

    def time_at(self, turns):
        """Cycle at which the spindle has made ``turns`` revolutions."""
        amp, period = self.wander
        w = 2 * math.pi / (period * CPU_HZ)
        t = turns * 60.0 * CPU_HZ / self.rpm
        for _ in range(WANDER_NEWTON if amp else 0):
            rate = (self.rpm + amp * math.sin(w * t)) / (60.0 * CPU_HZ)
            t -= (self.turns(t) - turns) / rate
        return t

    @classmethod
    def from_g64(cls, image, side=0, **kw):
        """Media holding every G64 halftrack on side."""
        media = cls(**kw)
        media.add_g64(image, side)
        return media

    def add_g64(self, image, side=0):
        """Place a G64's halftracks on side."""
        for ht, track in image.tracks.items():
            self.tracks[(side, ht)] = to_bits(track.data)

    def cells(self, side, halftrack):
        """The cell array under the head, created as noise if never written."""
        key = (side, halftrack)
        if key not in self.tracks:
            zone = speed_zone(max(halftrack // 2, 1))
            n = int(round(bits_per_revolution(zone, self.rpm)))
            self.tracks[key] = self.rng.integers(0, 2, n, dtype=np.uint8)
        return self.tracks[key]


class Mechanism:  # pylint: disable=too-many-instance-attributes
    """VIA2, stepper, read/write electronics and (1571) WD1770 index for one drive.

    ``log``, when a list, receives ("byte", t, value) per byte ready and
    ("sync", t_start, t_end, ones) per sync; ``corrupt(key, cell, bit)`` may
    alter written cells. ``bumps`` and ``inner_stops`` count steps driven
    against the outer (track 0) and inner end stops. The 1571 track 00 sensor
    covers every halftrack up to ``sensor_edge``: one of ``SENSOR_EDGES`` (the
    range DOS's track 00 rule allows), or ``SENSOR_STUCK_ON`` or
    ``SENSOR_STUCK_OFF`` for a failed sensor.
    """

    def __init__(  # pylint: disable=too-many-arguments
        self,
        drive,
        media,
        write_protect=False,
        halftrack=36,
        sensor_edge=SENSOR_EDGES[-1],
    ):
        drive.mech = self
        self.drive, self.media, self.write_protect = drive, media, write_protect
        self.halftrack, self.sensor_edge = halftrack, sensor_edge
        self.pb = (halftrack + PHASE_OFFSET) & PB_PHASE
        self.pcr, self.ddrb = DOS_PCR, DOS_DDRB
        self.bumps = self.inner_stops = 0
        self.ora = self.ddra = 0
        self.regs = bytearray(16)
        self.wd_command = None
        self.overruns = self.underruns = 0
        self.log = self.corrupt = None
        self.latch = 0
        self.due = 0
        self._key = self._cells = self._k = self._sync_start = None
        self._n = 1
        self._ones = self._count = self._shreg = self._pending = 0
        self._ca1 = self._written = self._armed = False

    @property
    def side(self):
        """Selected head: VIA1 PA2 on a 1571."""
        return (self.drive.via1.regs[1] >> 2) & 1 if self.drive.MODEL == "1571" else 0

    @property
    def track0(self):
        """1571 track 00 sensor: the head is at or outside ``sensor_edge``."""
        return self.halftrack <= self.sensor_edge

    @property
    def zone(self):
        """Density select bits PB5-6."""
        return (self.pb >> 5) & 3

    @property
    def writing(self):
        """CB2 low: write mode."""
        return self.pcr & PCR_MODE == PCR_WRITE

    @property
    def sync(self):
        """SYNC as the hardware asserts it (read mode only)."""
        return not self.writing and self._ones >= SYNC_ONES

    def angle(self, now):
        """Revolutions since cycle 0."""
        return self.media.turns(now)

    def _cell(self, now):
        return math.floor(self.media.turns(now) * self._n + CELL_EPS)

    def _time(self, cell):
        return self.media.time_at(cell / self._n)

    def _retrack(self, now):
        self._key = (self.side, self.halftrack)
        self._cells = self.media.cells(*self._key)
        self._n = len(self._cells)
        self._k = self._cell(now)

    def _resample(self, now):
        """Rescale the current track to the write density's cell count."""
        n = int(round(60.0 * bit_rate(self.zone) / self.media.rpm))
        cells = self._cells
        if n != len(cells):
            cells = cells[np.arange(n) * len(cells) // n]
        self.media.tracks[self._key] = cells.copy()
        self._retrack(now)

    def media_time(self, cycles):
        """Media time (us at CPU_HZ) of a drive cycle count, across a timed drive's
        clock changes."""
        return self.drive.time(cycles) if self.drive.TIMED else cycles

    def update(self, cycles):
        """Process every cell that has passed the head by drive cycle cycles."""
        now = self.media_time(cycles)
        if not self.pb & PB_MOTOR:
            self._k, self.due = None, math.inf
            return
        if self._k is None or self._key != (self.side, self.halftrack):
            self._retrack(now)
        end = self._cell(now)
        cell = self._write_cell if self.writing else self._read_cell
        for j in range(self._k, end):
            cell(j)
        self._k = max(end, self._k)
        due = self._time(self._k + 8 - self._count)
        if self.drive.TIMED and math.isfinite(due):
            due = self.drive.cycle_at(due)
        self.due = math.ceil(due) if math.isfinite(due) else math.inf

    def _event(self, j):
        self._ca1 = True
        if self.pcr & PCR_SOE == PCR_SOE:
            self.drive.mpu.p |= V_FLAG
        if self.log is not None:
            self.log.append(("byte", self._time(j + 1), self.latch))

    def _read_cell(self, j):
        b = int(self._cells[j % len(self._cells)])
        self._shreg = (self._shreg << 1 | b) & 0xFF
        if b:
            self._ones += 1
            if self._ones >= SYNC_ONES:
                self._count = 0
                if self._sync_start is None:
                    self._sync_start = self._time(j + 1)
                return
        else:
            if self._sync_start is not None and self.log is not None:
                end = self._time(j + 1)
                self.log.append(("sync", self._sync_start, end, self._ones))
            self._sync_start = None
            self._ones = 0
        self._count += 1
        if self._count == 8:
            self._count = 0
            self.latch = self._shreg
            self._pending += 1
            self._event(j)

    def _write_cell(self, j):
        b = self._shreg >> 7 & 1
        if self.corrupt is not None:
            b = self.corrupt(self._key, j % len(self._cells), b)
        self._cells[j % len(self._cells)] = b
        self._shreg = self._shreg << 1 & 0xFF
        self._count += 1
        if self._count == 8:
            self._count = 0
            if self._armed and not self._written:
                self.underruns += 1
            self._written = False
            self._shreg = self.ora if self.ddra == 0xFF else 0xFF
            self.latch = self._shreg
            self._event(j)

    def _port_b(self):
        v = self.pb & ~PB_INPUTS & 0xFF
        v |= 0 if self.write_protect else PB_WE
        return v | (0 if self.sync else PB_SYNC)

    def read(self, fdc, reg, cycles):
        """Register read by the instruction starting at cycles."""
        now = cycles + IO_ACCESS_CYCLE
        self.update(now)
        if fdc:
            hole = self.angle(self.media_time(now)) % 1.0 < INDEX_FRACTION
            return WD_INDEX if reg == 0 and hole and self.pb & PB_MOTOR else 0
        if reg == 0:
            return self._port_b()
        if reg in (1, 15):
            self.overruns += max(self._pending - 1, 0)
            self._pending = 0
            self._ca1 &= reg != 1
            return self.ora if self.writing else self.latch
        if reg == 13:
            return CA1_FLAG if self._ca1 else 0
        return {2: self.ddrb, 3: self.ddra, 12: self.pcr}.get(reg, self.regs[reg])

    def write(self, fdc, reg, value, cycles):
        """Register write by the instruction starting at cycles."""
        now = cycles + IO_ACCESS_CYCLE
        self.update(now)
        if fdc:
            self.wd_command = value
        elif reg == 0:
            self._port_b_write(value)
        elif reg in (1, 15):
            self.ora, self._written = value, True
            self._armed = self.writing
        elif reg == 2:
            self.ddrb = value
        elif reg == 3:
            self.ddra = value
        elif reg == 12:
            entering = not self.writing and value & PCR_MODE == PCR_WRITE
            self.pcr, self._armed = value, False
            if entering and self._k is not None:
                self._resample(self.media_time(now))
        else:
            self.regs[reg] = value
        self.update(now)

    def _port_b_write(self, value):
        step = (value - self.pb) & PB_PHASE
        if value & PB_MOTOR and not self.pb & PB_MOTOR:
            self._k = None
            self._ones = self._count = 0
        self.pb = value
        if step in (1, 3):
            ht = self.halftrack + (1 if step == 1 else -1)
            phase = (value - PHASE_OFFSET) & PB_PHASE
            if ht < HT_STOP:
                self.bumps += 1
                ht = HT_STOP + ((phase - HT_STOP) & PB_PHASE)
            elif ht > HT_MAX:
                self.inner_stops += 1
                ht = HT_MAX - ((HT_MAX - phase) & PB_PHASE)
            self.halftrack = ht


def log_bytes(log):
    """``(bytes, times)`` of the byte-ready events in a mechanism log."""
    events = [e for e in log if e[0] == "byte"]
    return bytes(e[2] for e in events), np.array([e[1] for e in events])


def true_syncs(log, data):
    """``(positions, runs)`` of the logged syncs inside a capture of ``data``.

    The capture must be one contiguous run of the logged bytes.
    """
    stream, times = log_bytes(log)
    first = stream.find(bytes(data))
    if first < 0:
        raise ValueError("capture is not a contiguous run of latched bytes")
    syncs = [e for e in log if e[0] == "sync"]
    pos = np.searchsorted(times, [e[2] for e in syncs]) - first
    runs = np.array([e[3] for e in syncs], np.int64)
    keep = (pos > 0) & (pos < len(data))
    return pos[keep], runs[keep]


def sync_track(runs, cells, seed=0, gaps=(1, 40)):
    """A track of GCR data with syncs of the given run lengths, ``cells`` long.

    Each sync sits between zero bits; ``gaps`` bounds the data bytes after one.
    """
    rng = np.random.default_rng(seed)
    parts = []
    for run in runs:
        data = rng.integers(0, 256, int(rng.integers(*gaps)), dtype=np.uint8)
        parts += [np.zeros(1, np.uint8), np.ones(run, np.uint8), np.zeros(1, np.uint8)]
        parts.append(encode_bits(data))
    bits = np.concatenate(parts)
    if len(bits) > cells:
        raise ValueError(f"{len(bits)} bits do not fit {cells} cells")
    fill = encode_bits(
        rng.integers(0, 256, (cells - len(bits)) // 10 + 1, dtype=np.uint8)
    )
    return np.concatenate((bits, fill))[:cells]


def disk_drive(model, media, device=8, **kw):
    """A simulated 1541 or 1571 with expansion RAM and a mechanism holding media, or a
    1581 holding simwd.MfmMedia (kw for simwd.Wd).

    DOS's current track for drive 0 is set as if DOS had left the head there.
    """
    if model == "1581":
        return Drive1581(device=device, media=media, **kw)
    drive = {"1541": Drive1541, "1571": Drive1571}[model](device=device)
    mech = Mechanism(drive, media, **kw)
    drive.write(DOS_TRACK, mech.halftrack // 2)
    return drive
