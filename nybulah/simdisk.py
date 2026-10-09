"""Disk mechanism for the drive simulator: rotating media, stepper, VIA2 and WD1770 index.

Cell j of an L-cell track passes the head at angle j / L (index at angle 0); reading
follows the media's cell rate, writing the selected density's. SYNC holds the bit
counter after ten ones; each eighth cell otherwise latches a byte and raises SO.
"""

import math

import numpy as np

from .analysis.gcr import NOMINAL_RPM, bit_rate, bits_per_revolution, speed_zone
from .analysis.gcr import to_bits
from .sim import IO_ACCESS_CYCLE, Drive1541, Drive1571

CPU_HZ = 1_000_000
HT_STOP, HT_MAX = 2, 84
SYNC_ONES = 10
INDEX_FRACTION = 0.02
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
    """

    def __init__(self, tracks=None, rpm=NOMINAL_RPM, seed=0):
        self.tracks = {k: np.asarray(v, np.uint8) for k, v in (tracks or {}).items()}
        self.rpm = rpm
        self.rng = np.random.default_rng(seed)

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
    alter written cells.
    """

    def __init__(self, drive, media, write_protect=False, halftrack=36):
        drive.mech = self
        self.drive, self.media, self.write_protect = drive, media, write_protect
        self.halftrack = halftrack
        self.pb, self.pcr, self.ddrb = halftrack & PB_PHASE, DOS_PCR, DOS_DDRB
        self.ora = self.ddra = 0
        self.regs = bytearray(16)
        self.wd_command = None
        self.overruns = self.underruns = 0
        self.log = self.corrupt = None
        self.latch = 0
        self.due = 0
        self._key = self._cells = self._k = self._sync_start = None
        self._cpc = 1.0
        self._ones = self._count = self._shreg = self._pending = 0
        self._ca1 = self._written = self._armed = False

    @property
    def side(self):
        """Selected head: VIA1 PA2 on a 1571."""
        return (self.drive.via1.regs[1] >> 2) & 1 if self.drive.MODEL == "1571" else 0

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
        return now * self.media.rpm / (60.0 * CPU_HZ)

    def _retrack(self, now):
        self._key = (self.side, self.halftrack)
        self._cells = self.media.cells(*self._key)
        self._cpc = 60.0 * CPU_HZ / (self.media.rpm * len(self._cells))
        self._k = int(now // self._cpc)

    def _resample(self, now):
        """Rescale the current track to the write density's cell count."""
        n = int(round(60.0 * bit_rate(self.zone) / self.media.rpm))
        cells = self._cells
        if n != len(cells):
            cells = cells[np.arange(n) * len(cells) // n]
        self.media.tracks[self._key] = cells.copy()
        self._retrack(now)

    def update(self, now):
        """Process every cell that has passed the head by cycle now."""
        if not self.pb & PB_MOTOR:
            self._k, self.due = None, math.inf
            return
        if self._k is None or self._key != (self.side, self.halftrack):
            self._retrack(now)
        end = int(now // self._cpc)
        cell = self._write_cell if self.writing else self._read_cell
        for j in range(self._k, end):
            cell(j)
        self._k = max(end, self._k)
        self.due = math.ceil((self._k + 8 - self._count) * self._cpc)

    def _event(self, j):
        self._ca1 = True
        if self.pcr & PCR_SOE == PCR_SOE:
            self.drive.mpu.p |= V_FLAG
        if self.log is not None:
            self.log.append(("byte", (j + 1) * self._cpc, self.latch))

    def _read_cell(self, j):
        b = int(self._cells[j % len(self._cells)])
        self._shreg = (self._shreg << 1 | b) & 0xFF
        if b:
            self._ones += 1
            if self._ones >= SYNC_ONES:
                self._count = 0
                if self._sync_start is None:
                    self._sync_start = (j + 1) * self._cpc
                return
        else:
            if self._sync_start is not None and self.log is not None:
                end = (j + 1) * self._cpc
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
            hole = self.angle(now) % 1.0 < INDEX_FRACTION
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
                self._resample(now)
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
            phase = value & PB_PHASE
            if ht < HT_STOP:
                ht = HT_STOP + ((phase - HT_STOP) & PB_PHASE)
            elif ht > HT_MAX:
                ht = HT_MAX - ((HT_MAX - phase) & PB_PHASE)
            self.halftrack = ht


def disk_drive(model, media, device=8, **kw):
    """A simulated 1541 or 1571 with expansion RAM and a mechanism holding media."""
    drive = {"1541": Drive1541, "1571": Drive1571}[model](device=device)
    Mechanism(drive, media, **kw)
    return drive


class SimMonitor:
    """Monitor stand-in that runs drive code directly, without a bus transport."""

    def __init__(self, drive, budget=50_000_000):
        self.drive, self.budget = drive, budget

    def read(self, addr, size):
        """Read drive memory."""
        return self.drive.dump(addr, size)

    def write(self, addr, data):
        """Write drive memory."""
        self.drive.load(addr, bytes(data))

    def jsr(self, addr):
        """Run a subroutine to its rts; return (A, X, Y)."""
        d = self.drive
        d.call(addr)
        start = d.cycles
        while not d.halted:
            d.step()
            if d.cycles - start > self.budget:
                raise TimeoutError(f"jsr ${addr:04x} ran past {self.budget} cycles")
        return d.mpu.a, d.mpu.x, d.mpu.y
