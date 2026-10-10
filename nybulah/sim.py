"""Cycle-stepped 1541/1571, IEC bus and xum1541 model: drive code without hardware.

py65 6502 with the model's address decoding, VIA1 port B on an open-collector bus with
ATN auto-acknowledge, VIA1 timers 1 and 2 and the 1571's 6526 (SRQ, DATA through the
drivers VIA1 PA1 turns) clocked by the CPU cycle counter; VIA2 and the WD1770 come from
nybulah.simdisk, simfast runs drives compiled unless NYBULAH_SIM=py65, simhost hosts.
"""

import os

import numpy as np
from py65.devices.mpu6502 import MPU

from .opencbm import IEC_ATN, IEC_CLOCK, IEC_DATA, IEC_SRQ, OpenCBMError
from .simwd import Wd

PB_DATA_IN, PB_DATA_OUT, PB_CLK_IN, PB_CLK_OUT, PB_ATNA, PB_ATN_IN = (
    0x01,
    0x02,
    0x04,
    0x08,
    0x10,
    0x80,
)
RETURN_TRAP = 0xFFF0
RAM, ROM, VIA1, OPEN, IO, VIA2, FDC, CIA = range(8)
ACR_T1_FREERUN, IRQ_T1, IRQ_T2 = 0x40, 0x40, 0x20
IO_ACCESS_CYCLE = 3
CIA_BASE, PA_FSDIR = 0x4000, 0x02
CRA_START, CRA_LOAD, CRA_SPOUT = 0x01, 0x10, 0x40
ICR_TA, ICR_SP, ICR_IR = 0x01, 0x08, 0x80
ICR_TB, ICR_FLAG = 0x02, 0x10
CRB_START, CRB_ONESHOT, CRB_LOAD, CRB_INMODE = 0x01, 0x08, 0x10, 0x60
INMODE_PHI2, INMODE_TA = 0x00, 0x40
PB_FSDIR, PB_WPRT = 0x20, 0x40
# DOS's listen and talk addresses (LSNADR, TLKADR): set from the device number at
# initialisation, compared with each ATN command byte (1541/1571 $E8A9, 1581 $AC2C)
LSNADR, TLKADR, LISTEN, TALK = 0x77, 0x78, 0x20, 0x40


WAIT, SET, REL, PUT, SAMPLE, SHL, SHR = range(7)

D, C, T = IEC_DATA, IEC_CLOCK, IEC_ATN
HOST = {
    "s1_read": [(WAIT, D, 0), (REL, C, 0), (SAMPLE, C, 0), (SET, D, 0), (WAIT, C, 2)]
    + [(REL, D, 0), (WAIT, D, 1), (SET, C, 0)],
    "s1_write": [(PUT, D, 0), (REL, C, 0), (WAIT, C, 1), (PUT, D, 1), (WAIT, C, 0)]
    + [(SET, C, 0), (REL, D, 0), (WAIT, D, 1), (SHL, 0, 0)],
    "s2_read": [(WAIT, C, 1), (SAMPLE, D, 0), (REL, T, 0), (WAIT, C, 0), (SAMPLE, D, 0)]
    + [(SET, T, 0)],
    "s2_write": [(PUT, D, 2), (SHR, 0, 0), (REL, T, 0), (WAIT, C, 0), (PUT, D, 2)]
    + [(SHR, 0, 0), (SET, T, 0), (WAIT, C, 1)],
}
HOST_BITS = {"s1_read": 8, "s1_write": 8, "s2_read": 4, "s2_write": 4}
HOST_TAIL = {"s2_write": [(REL, D, 0)]}


def host_programs():
    """Per-byte xum1541 programs (protocol, step, op/line/arg); name -> (index, length)."""
    progs = {n: ops * HOST_BITS[n] + HOST_TAIL.get(n, []) for n, ops in HOST.items()}
    out = np.zeros((len(progs), max(map(len, progs.values())), 3), np.int64)
    for i, ops in enumerate(progs.values()):
        out[i, : len(ops)] = ops
    return out, {n: (i, len(ops)) for i, (n, ops) in enumerate(progs.items())}


HOST_PROGRAMS, HOST_LENGTHS = host_programs()


class SimTimeout(OpenCBMError):
    """The host waited longer than the cycle budget for a bus condition."""


class HostGone(OpenCBMError):
    """The simulated adapter vanished mid-transfer."""


class SimIOWrite(RuntimeError):
    """Drive code wrote to an I/O address other than a modelled base register."""


def memory_map(model, expansion):
    """Per-address (kind, physical index) tables, ROM offset and store size.

    expansion entries are (lo, hi) or (lo, hi, size) for RAM mirrored every size bytes.
    """
    a = np.arange(0x10000)
    low = a < 0x8000
    kind = np.full(0x10000, IO, np.uint8)
    phys = np.zeros(0x10000, np.int64)
    if model == "1581":
        return _map_1581(a, kind, phys)
    if model == "1541":
        ram, rom_size = low & (a & 0x1800 == 0), 0x4000
        io = {VIA1: low & (a & 0x1C00 == 0x1800), VIA2: low & (a & 0x1C00 == 0x1C00)}
    elif model == "1571":
        ram, rom_size = a < 0x0800, 0x8000
        io = {
            VIA1: (a >= 0x1800) & (a < 0x1C00),
            VIA2: (a >= 0x1C00) & (a < 0x2000),
            FDC: (a >= 0x2000) & (a < 0x4000),
        }
        io[CIA] = (a >= CIA_BASE) & (a < CIA_BASE + 16)
        kind[(a >= 0x6000) & low] = OPEN
    else:
        raise ValueError(f"unknown model {model}")
    kind[ram], phys[ram] = RAM, a[ram] & 0x7FF
    for k, sel in io.items():
        kind[sel], phys[sel] = k, a[sel] & (0x1FFF if k == FDC else 0x3FF)
    rom_at = 0x800 + sum(e[2] if len(e) > 2 else e[1] - e[0] for e in expansion)
    kind[~low], phys[~low] = ROM, rom_at + (a[~low] & (rom_size - 1))
    _place_expansion(kind, phys, expansion)
    return kind.tolist(), phys.tolist(), rom_at, rom_at + rom_size


def _map_1581(a, kind, phys):
    """1581 decoding (sm sheet 1, 74LS139 U6): 8K RAM, open $2000-$3FFF, 8520 every
    16 bytes from $4000, WD177x every 4 from $6000, 32K ROM."""
    ram, cia, fdc = a < 0x2000, (a >> 13) == 2, (a >> 13) == 3
    kind[ram], phys[ram] = RAM, a[ram]
    kind[(a >> 13) == 1] = OPEN
    kind[cia], phys[cia] = CIA, a[cia] & 0xF
    kind[fdc], phys[fdc] = FDC, a[fdc] & 3
    rom = a >= 0x8000
    kind[rom], phys[rom] = ROM, 0x2000 + (a[rom] & 0x7FFF)
    return kind.tolist(), phys.tolist(), 0x2000, 0xA000


def _place_expansion(kind, phys, expansion):
    top = 0x800
    for lo, hi, *size in expansion:
        n = size[0] if size else hi - lo
        kind[lo:hi], phys[lo:hi] = RAM, top + np.arange(hi - lo) % n
        top += n


class Bus:
    """Open-collector IEC bus: host lines plus every attached device."""

    def __init__(self):
        self.host_lines = 0
        self.devices = []

    def lines(self):
        """Wired-OR state (1 = asserted)."""
        v = self.host_lines
        for d in self.devices:
            v |= d.drive_lines()
        return v


class IdleDOSDrive:
    """A drive sitting in DOS: acknowledges ATN by holding DATA, else silent."""

    halted = True

    def __init__(self, bus):
        self.bus = bus
        bus.devices.append(self)

    def drive_lines(self):
        """DATA while ATN is asserted."""
        return IEC_DATA if self.bus.host_lines & IEC_ATN else 0


class Via:
    """6522 registers used by drive code: port B, timers 1 and 2, ACR, IFR, IER."""

    def __init__(self, drive):
        self.drive = drive
        self.reset()

    def reset(self):
        """Power-on state."""
        self.acr = self.ier = self.latch = self.t1_start = self.t1_ack = 0
        self.t2_latch = self.t2_start = 0
        self.t2_ack = True
        self.regs = bytearray(16)

    def _t1_events(self):
        e = self.drive.cycles - self.t1_start - self.latch - 1
        if e < 0:
            return 0
        return e // (self.latch + 2) + 1 if self.acr & ACR_T1_FREERUN else 1

    def t2(self):
        """Timer 2 counter: one-shot from its latch, counting on past zero."""
        return (self.t2_latch - (self.drive.cycles - self.t2_start)) & 0xFFFF

    def ifr(self):
        """Interrupt flags; timers 1 and 2 are the sources."""
        v = IRQ_T1 if self._t1_events() > self.t1_ack else 0
        if not self.t2_ack and self.drive.cycles - self.t2_start > self.t2_latch:
            v |= IRQ_T2
        return v | 0x80 if v & self.ier else v

    def read(self, reg):
        """Register read with 6522 side effects."""
        if reg == 0:
            return self.drive.port_b()
        if reg in (1, 15):
            return self.drive.port_a(self.regs[1])
        if reg in (4, 5):
            count = (self.latch - (self.drive.cycles - self.t1_start)) & 0xFFFF
            if reg == 4:
                self.t1_ack = self._t1_events()
            return count >> 8 * (reg - 4) & 0xFF
        if reg in (6, 7):
            return self.latch >> 8 * (reg - 6) & 0xFF
        if reg in (8, 9):
            self.t2_ack |= reg == 8
            return self.t2() >> 8 * (reg - 8) & 0xFF
        return {0xB: self.acr, 0xD: self.ifr(), 0xE: self.ier | 0x80}.get(
            reg, self.regs[reg]
        )

    def write(self, reg, value):
        """Register write with 6522 side effects."""
        if reg == 0:
            self.drive.pb_out = value & (PB_DATA_OUT | PB_CLK_OUT | PB_ATNA)
        elif reg in (4, 6):
            self.latch = self.latch & 0xFF00 | value
        elif reg in (5, 7):
            self.latch = self.latch & 0xFF | value << 8
            if reg == 5:
                self.t1_start, self.t1_ack = self.drive.cycles, 0
        elif reg == 8:
            self.t2_latch = self.t2_latch & 0xFF00 | value
        elif reg == 9:
            self.t2_latch = self.t2_latch & 0xFF | value << 8
            self.t2_start, self.t2_ack = self.drive.cycles, False
        elif reg == 0xB:
            self.acr = value
        elif reg == 0xD:
            if value & IRQ_T1:
                self.t1_ack = self._t1_events()
        elif reg == 0xE:
            self.ier = self.ier | value & 0x7F if value & 0x80 else self.ier & ~value
        elif reg == 0xF:
            self.regs[1] = value
        else:
            self.regs[reg] = value

    def state(self):
        """(ACR, IER, T1 latch) for restoration checks."""
        return self.acr, self.ier, self.latch


class Cia:  # pylint: disable=too-many-instance-attributes
    """6526 timer A, serial port and ICR as the datasheet describes them, in CPU cycles.

    Timer A counts down from c0 at cycle t0 while CRA bit 0 runs it; underflows come every
    latch + 1 cycles and reload the latch (continuous mode). Output mode (CRA bit 6): an
    SDR byte starts at the first underflow after the write (u1; ``delay`` adds cycles of
    unspecified pipeline before that underflow), CNT falls at u1 + 2kP with bit 7-k on SP
    and rises P later; the 8th rise sets ICR bit 3 and starts a byte written meanwhile.
    Input mode: edge() shifts SP in on each rising CNT and fills SDR after eight. The
    other registers only hold what is written.
    """

    FIELDS = ("cra", "latch", "t0", "c0", "mask", "flags", "ticr", "sdr", "sr", "bits")
    FIELDS += ("u1", "out", "pend", "sp", "rise", "delay")

    def __init__(self):
        self.delay = 0
        self.reset()

    def reset(self):
        """RES: timers stopped at $FFFF, serial input, no flags."""
        self.cra = self.mask = self.flags = self.ticr = self.t0 = self.rise = 0
        self.sdr = self.sr = self.bits = self.out = 0
        self.latch = self.c0 = 0xFFFF
        self.u1 = self.pend = -1
        self.sp = 1
        self.regs = bytearray(16)

    def next_uf(self, c):
        """First timer A underflow after cycle c (timer running)."""
        first, p = self.t0 + self.c0 + 1, self.latch + 1
        return first if c < first else first + ((c - first) // p + 1) * p

    def counter(self, c):
        """Timer A count at cycle c."""
        first = self.t0 + self.c0 + 1
        if not self.cra & CRA_START:
            return self.c0
        if c < first:
            return self.c0 - (c - self.t0)
        return self.latch - (c - first) % (self.latch + 1)

    def advance(self, c):
        """(u1, out, pend, sp, rise, bytes done) once the shifter has run to cycle c."""
        u1, out, pend, sp, rise, done = (
            self.u1,
            self.out,
            self.pend,
            self.sp,
            self.rise,
            0,
        )
        p = self.latch + 1
        while 0 <= u1 <= c - 15 * p:
            rise, sp, done = u1 + 15 * p, out & 1, done + 1
            u1, out, pend = (u1 + 16 * p, pend, -1) if pend >= 0 else (-1, out, -1)
        return u1, out, pend, sp, rise, done

    def settle(self, c):
        """Run the shifter and timer flags up to cycle c."""
        self.u1, self.out, self.pend, self.sp, self.rise, done = self.advance(c)
        if done:
            self.flags |= ICR_SP
        if self.cra & CRA_START and self.next_uf(self.ticr) <= c:
            self.flags |= ICR_TA
        self.ticr = c

    def levels(self, c):
        """(CNT, SP) output levels at cycle c."""
        u1, out, _, sp, _, _ = self.advance(c)
        if u1 < 0 or c < u1:
            return 1, sp
        k = (c - u1) // (self.latch + 1)
        return k & 1, out >> 7 - (k >> 1) & 1

    def lines(self, c, fsdir):
        """IEC lines driven at cycle c: output mode with the bus drivers out."""
        if not (fsdir and self.cra & CRA_SPOUT):
            return 0
        cnt, sp = self.levels(c)
        return (0 if cnt else IEC_SRQ) | (0 if sp else IEC_DATA)

    def edge(self, sp):
        """A rising CNT in input mode, SP at sp."""
        if self.cra & CRA_SPOUT:
            return
        self.sr, self.bits = (self.sr << 1 | sp) & 0xFF, self.bits + 1
        if self.bits == 8:
            self.bits, self.sdr = 0, self.sr
            self.flags |= ICR_SP

    def read(self, reg, c):
        """Register read at cycle c."""
        if reg in (4, 5):
            return self.counter(c) >> 8 * (reg - 4) & 0xFF
        if reg in (12, 13):
            self.settle(c)
            if reg == 12:
                return self.sdr
            v = self.flags | (ICR_IR if self.flags & self.mask else 0)
            self.flags = 0
            return v
        return self.cra if reg == 14 else self.regs[reg]

    def write(self, reg, value, c):
        """Register write at cycle c."""
        if reg == 4:
            self.latch = self.latch & 0xFF00 | value
        elif reg == 5:
            self.latch = self.latch & 0xFF | value << 8
            if not self.cra & CRA_START:
                self.t0, self.c0 = c, self.latch
        elif reg == 12:
            self.settle(c)
            self.sdr = value
            if self.cra & CRA_SPOUT:
                self.load(value, c)
        elif reg == 13:
            m = value & 0x7F
            self.mask = self.mask | m if value & 0x80 else self.mask & ~m
        elif reg == 14:
            self.control(value, c)
        else:
            self.regs[reg] = value

    def load(self, value, c):
        """An SDR byte for the output shifter."""
        if self.u1 > c:
            self.out = value
        elif self.u1 >= 0 or not self.cra & CRA_START:
            self.pend = value
        else:
            self.u1, self.out = self.next_uf(c + self.delay), value

    def control(self, value, c):
        """CRA write: start/stop, force load, serial direction."""
        self.settle(c)
        count = self.latch if value & CRA_LOAD else self.counter(c)
        if value & CRA_LOAD or (value ^ self.cra) & CRA_START or not value & CRA_START:
            self.t0, self.c0 = c, count
        if (value ^ self.cra) & CRA_SPOUT or not value & CRA_START:
            self.u1 = self.pend = -1
        self.cra = value & ~CRA_LOAD
        if self.cra & CRA_SPOUT and self.u1 < 0 <= self.pend:
            self.u1, self.out = self.next_uf(c + self.delay), self.pend
            self.pend = -1


class Cia8520(Cia):
    """The 1581's 8520: Cia plus timer B, CRB and the FLAG input (MOS 6526 register map).

    Timer B counts phi2 cycles or timer A underflows (CRB INMODE), continuous or one-shot,
    with force load; underflows set ICR bit 1. Counting CNT edges is not modelled.
    """

    FIELDS81 = ("crb", "tb_latch", "tb_t0", "tb_c0", "atn")

    def reset(self):
        """RES: timer B stopped at $FFFF too."""
        super().reset()
        self.crb = self.tb_t0 = self.atn = 0
        self.tb_latch = self.tb_c0 = 0xFFFF

    def ta_underflows(self, a, c):
        """Timer A underflows in (a, c]."""
        if not self.cra & CRA_START:
            return 0
        first, p = self.t0 + self.c0 + 1, self.latch + 1
        a, c = (0 if x < first else (x - first) // p + 1 for x in (a, c))
        return c - a

    def tb_settle(self, c):
        """Run timer B to cycle c."""
        if self.crb & CRB_START:
            mode = self.crb & CRB_INMODE
            m = c - self.tb_t0 if mode == INMODE_PHI2 else 0
            m = self.ta_underflows(self.tb_t0, c) if mode == INMODE_TA else m
            if m > self.tb_c0:
                self.flags |= ICR_TB
                if self.crb & CRB_ONESHOT:
                    self.crb &= ~CRB_START
                    self.tb_c0 = self.tb_latch
                else:
                    self.tb_c0 = self.tb_latch - (m - self.tb_c0 - 1) % (
                        self.tb_latch + 1
                    )
            else:
                self.tb_c0 -= m
        self.tb_t0 = c

    def settle(self, c):
        """Run both timers and the shifter to cycle c."""
        self.tb_settle(c)
        super().settle(c)

    def atn_edge(self, atn):
        """FLAG = bus ATN level (sm sheet 3): ICR bit 4 when ATN is asserted."""
        if atn and not self.atn:
            self.flags |= ICR_FLAG
        self.atn = atn

    def read(self, reg, c):
        """Register read at cycle c."""
        if reg in (6, 7, 15):
            self.tb_settle(c)
            return self.crb if reg == 15 else self.tb_c0 >> 8 * (reg - 6) & 0xFF
        return super().read(reg, c)

    def write(self, reg, value, c):
        """Register write at cycle c."""
        if reg in (4, 5, 6, 7, 15):
            self.tb_settle(c)
        if reg == 6:
            self.tb_latch = self.tb_latch & 0xFF00 | value
        elif reg == 7:
            self.tb_latch = self.tb_latch & 0xFF | value << 8
            if not self.crb & CRB_START:
                self.tb_c0 = self.tb_latch
        elif reg == 15:
            if value & CRB_LOAD:
                self.tb_c0 = self.tb_latch
            self.crb = value & ~CRB_LOAD
        else:
            super().write(reg, value, c)


class Drive1541:  # pylint: disable=too-many-instance-attributes
    """A drive CPU with its model's memory map, VIA1 and optional expansion RAM."""

    MODEL = "1541"
    EXPANSION = ((0x8000, 0xA000),)
    TIMED = False

    def __init__(self, device=8, expansion=None, bus=None, resets_to_boot=1, seed=0):
        self.bus = bus or Bus()
        self.bus.devices.append(self)
        self.device = device
        self._kind, self._phys, rom_at, size = memory_map(
            self.MODEL, self.EXPANSION if expansion is None else expansion
        )
        self.store = bytearray(size)
        self.store[rom_at:] = np.random.default_rng(seed).bytes(size - rom_at)
        self.via1 = Via(self)
        self.cia = Cia() if self.MODEL == "1571" else None
        self.cycles = 0
        self.pb_out = 0
        self.mpu = MPU(memory=_Memory(self))
        self.halted = True
        self.resets_to_boot, self._pending = resets_to_boot, 0
        self.mech = None
        self._pc = -1
        self.fast = os.environ.get("NYBULAH_SIM") != "py65"
        self.dos_addresses()

    def dos_addresses(self):
        """Set LSNADR and TLKADR from the device number, as DOS initialisation does."""
        self.store[self._phys[LSNADR]] = LISTEN | self.device
        self.store[self._phys[TLKADR]] = TALK | self.device

    @property
    def responsive(self):
        """Whether DOS would answer on the bus: running, and still matching its
        device number's LISTEN and TALK commands."""
        return (
            self.halted
            and not self._pending
            and self.read(LSNADR) == LISTEN | self.device
            and self.read(TLKADR) == TALK | self.device
        )

    def read(self, addr):
        """CPU read."""
        k = self._kind[addr]
        if k <= ROM:
            return self.store[self._phys[addr]]
        if k == VIA1:
            return self.via1.read(self._phys[addr] & 0xF)
        if k == CIA:
            return self.cia.read(self._phys[addr], self.cycles + IO_ACCESS_CYCLE)
        if k == FDC and self.mech is not None:
            return self.mech.read(True, self._phys[addr] & 3, self.cycles, self._pc)
        if k == VIA2 and self.mech is not None and self._phys[addr] < 16:
            return self.mech.read(False, self._phys[addr], self.cycles)
        return addr >> 8 if k == OPEN else 0

    def write(self, addr, value):
        """CPU write."""
        k = self._kind[addr]
        if k == RAM:
            self.store[self._phys[addr]] = value & 0xFF
        elif k == VIA1 and self._phys[addr] < 16:
            self.via1.write(self._phys[addr], value & 0xFF)
        elif k == CIA:
            self.cia.write(
                self._phys[addr], value & 0xFF, self.cycles + IO_ACCESS_CYCLE
            )
        elif k == VIA2 and self.mech is not None and self._phys[addr] < 16:
            self.mech.write(False, self._phys[addr], value & 0xFF, self.cycles)
        elif k == FDC and self.mech is not None:
            reg = self._phys[addr] & 3
            self.mech.write(True, reg, value & 0xFF, self.cycles, self._pc)
        elif k not in (ROM, OPEN):
            raise SimIOWrite(f"write ${value & 0xFF:02x} to I/O ${addr:04x}")

    def via_lines(self):
        """IEC lines VIA1 port B asserts (with ATN auto-acknowledge)."""
        lines = 0
        atn = bool(self.bus.host_lines & IEC_ATN)
        if self.pb_out & PB_DATA_OUT or atn != bool(self.pb_out & PB_ATNA):
            lines |= IEC_DATA
        if self.pb_out & PB_CLK_OUT:
            lines |= IEC_CLOCK
        return lines

    def fsdir(self):
        """Fast serial direction: CIA CNT/SP drive SRQ/DATA (1571 VIA1 PA1)."""
        return self.via1.regs[1] & PA_FSDIR

    def drive_lines(self):
        """IEC lines this drive is asserting."""
        lines = self.via_lines()
        if self.cia is not None:
            lines |= self.cia.lines(self.cycles, self.fsdir())
        return lines

    def port_b(self):
        """Value read from VIA1 port B."""
        bus = self.bus.lines()
        v = self.pb_out | ((self.device - 8) & 3) << 5
        v |= PB_DATA_IN if bus & IEC_DATA else 0
        v |= PB_CLK_IN if bus & IEC_CLOCK else 0
        v |= PB_ATN_IN if bus & IEC_ATN else 0
        return v

    def port_a(self, latch):
        """Value read from VIA1 port A: the output latch."""
        return latch

    def load(self, addr, data):
        """Write bytes into drive memory without running the CPU."""
        for i, b in enumerate(data):
            self.write(addr + i, b)

    def dump(self, addr, size):
        """Read bytes from drive memory without running the CPU."""
        return bytes(self.read(addr + i) for i in range(size))

    def call(self, addr):
        """Start executing at addr; an RTS from there halts the CPU."""
        mpu = self.mpu
        mpu.sp = 0xFF
        mpu.stPushWord(RETURN_TRAP - 1)
        mpu.pc = addr
        self.halted, self._pending = False, self.resets_to_boot

    def reset(self):
        """RESET pulse; DOS answers after resets_to_boot pulses once code has run."""
        self.halted, self.pb_out = True, 0
        self.via1.reset()
        if self.cia is not None:
            self.cia.reset()
        self._pending = max(self._pending - 1, 0)
        self.dos_addresses()

    def step(self):
        """Execute one instruction unless halted; return cycles used."""
        if self.halted:
            return 0
        if self.mech is not None and self.cycles >= self.mech.due:
            self.mech.update(self.cycles)
        before = self.mpu.processorCycles
        self._pc = self.mpu.pc
        try:
            self.mpu.step()
        finally:
            self._pc = -1
        n = self.mpu.processorCycles - before
        self.cycles += n
        if self.mpu.pc == RETURN_TRAP:
            self.halted, self._pending = True, 0
        return n


class Drive1571(Drive1541):
    """1571 decoding: 2K RAM, VIAs, WD1770 and CIA below $6000, 32K ROM."""

    MODEL = "1571"
    EXPANSION = ((0x6000, 0x8000),)
    PA_TRK0 = 0x01

    def port_a(self, latch):
        """PA0 is the track 00 sensor input, low while the sensor covers the head."""
        if self.mech is None:
            return latch
        return latch & ~self.PA_TRK0 | (0 if self.mech.track0 else self.PA_TRK0)


class Drive1581(Drive1541):  # pylint: disable=too-many-instance-attributes
    """1581: 8K RAM, 8520 CIA (ports, both timers, serial port, FLAG), WD177x and 32K ROM
    at 2 MHz; built in the state DOS leaves the CIA in, holding ``media`` (simwd)."""

    MODEL = "1581"
    EXPANSION = ()
    # iodef.src init_prt_pa/init_dd_pa/init_prt_pb/init_dd_pb, dskint.src TA/CRA/ICR,
    # mrout.src reset_ctl + msub.src resetim TB latch $4E20 and CRB $11
    DOS_CIA = {0: 0xFE, 1: 0xD5, 2: 0x65, 3: 0x3A, 4: 6, 5: 0, 13: 0x9A, 14: CRA_START}
    DOS_CIA |= {6: 0x20, 7: 0x4E, 15: CRB_START | CRB_LOAD}

    def __init__(self, device=8, bus=None, media=None, seed=0, **wd):
        self._pc = -1
        self.wd = Wd(media, **wd)
        super().__init__(device=device, expansion=(), bus=bus, seed=seed)
        self.cia = Cia8520()
        self.dos_cia()

    def dos_cia(self):
        """Program the CIA as DOS initialises it."""
        for reg, value in self.DOS_CIA.items():
            self.write(CIA_BASE + reg, value)

    def fsdir(self):
        """PB5 FSDIR (iodef.src, sm sheet 3: 74LS241 U13 direction)."""
        return self.port_pins(1) & PB_FSDIR

    def port_pins(self, port):
        """Output pin levels of port A (0) or B (1); input pins read high (pull-ups)."""
        regs = self.cia.regs
        return regs[port] & regs[port + 2] | ~regs[port + 2] & 0xFF

    def via_lines(self):
        """DATA from PB1, or from the ATN acknowledge gate (PB4 and ATN, sm sheet 3
        74LS00 U7); CLK from PB3."""
        atn = self.bus.host_lines & IEC_ATN
        lines = IEC_CLOCK if self.pb_out & PB_CLK_OUT else 0
        if self.pb_out & PB_DATA_OUT or self.pb_out & PB_ATNA and atn:
            lines |= IEC_DATA
        return lines

    def read(self, addr):
        """CPU read: port A pins PA3/PA4 device switches (dskint.src), /RDY and /DISK
        CHNG; port B pins DATA, CLK, ATN in (1 = asserted) and /WPRT; the WD."""
        k, reg, c = self._kind[addr], self._phys[addr], self.cycles + IO_ACCESS_CYCLE
        if k == CIA and reg == 0:
            pins = self.wd.inputs(c, 0xE7 | ((self.device - 8) & 3) << 3)
        elif k == CIA and reg == 1:
            pins = self.port_b() & (PB_DATA_IN | PB_CLK_IN | PB_ATN_IN) | 0x3A
            pins |= 0 if self.wd.write_protect else PB_WPRT
        elif k == FDC:
            return self.wd.read(reg, c, self._pc)
        else:
            return super().read(addr)
        regs = self.cia.regs
        return regs[reg] & regs[reg + 2] | pins & ~regs[reg + 2] & 0xFF

    def write(self, addr, value):
        """CPU write: port A drives the mechanism, port B the bus."""
        k, reg = self._kind[addr], self._phys[addr]
        if k == FDC:
            self.wd.write(reg, value & 0xFF, self.cycles + IO_ACCESS_CYCLE, self._pc)
            return
        super().write(addr, value)
        if k == CIA and reg in (0, 2):
            self.wd.control(self.cycles + IO_ACCESS_CYCLE, self.port_pins(0))
        elif k == CIA and reg in (1, 3):
            self.pb_out = self.port_pins(1) & (PB_DATA_OUT | PB_CLK_OUT | PB_ATNA)

    def step(self):
        """One instruction; the CIA's FLAG sees ATN first."""
        if self.halted:
            return 0
        self.cia.atn_edge(bool(self.bus.host_lines & IEC_ATN))
        self._pc = self.mpu.pc
        n = super().step()
        self._pc = -1
        return n

    def reset(self):
        """RESET: the WD's master reset ends any command; DOS reinitialises the CIA."""
        super().reset()
        self.wd.write(0, 0xD0, self.cycles)
        self.dos_cia()

    def sync(self):
        """Run the WD to the current cycle."""
        self.wd.sync(self.cycles)


class IdleFastDrive(IdleDOSDrive):
    """A 1571/1581 idle in DOS: ATN acknowledge and the fast serial "fast host" flag its
    SP interrupt latches after 8 SRQ rises (irq.src), cleared only by UNLISTEN or UNTALK
    under ATN (sieee.src); SRQ rises are those the host makes."""

    UNLISTEN, UNTALK = 0x3F, 0x5F

    def __init__(self, bus):
        super().__init__(bus)
        self.fast_host, self.bits, self._srq = False, 0, False
        if hasattr(bus, "listeners"):
            bus.listeners.append(self._host)

    def _host(self, lines, _t):
        srq = bool(lines & IEC_SRQ)
        if self._srq and not srq:
            self.srq_rise()
        self._srq = srq

    def srq_rise(self):
        """SRQ released: one bit into the CIA's serial port."""
        self.bits += 1
        if self.bits == 8:
            self.bits, self.fast_host = 0, True

    def atn_command(self, byte):
        """A command byte under ATN."""
        if byte in (self.UNLISTEN, self.UNTALK):
            self.fast_host = False


class _Memory(list):
    def __init__(self, drive):
        super().__init__([0] * 0x10000)
        self.drive = drive

    def __getitem__(self, addr):
        if isinstance(addr, slice):
            return [self[a] for a in range(*addr.indices(0x10000))]
        return self.drive.read(addr)

    def __setitem__(self, addr, value):
        self.drive.write(addr, value)
