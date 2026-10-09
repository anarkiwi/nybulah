"""Cycle-stepped 1541/1571 + IEC bus + xum1541 model for running drive code without hardware.

py65 6502 with the model's address decoding, VIA1 port B on an open-collector bus with
ATN auto-acknowledge, and VIA1 timers 1 and 2 clocked by the CPU cycle counter. VIA2 and
the 1571's WD1770 are served by an attached disk mechanism (nybulah.simdisk) when there is
one. Writes to other I/O raise SimIOWrite; SimCBM mirrors the OpenCBM API subset.
"""

import numpy as np
from py65.devices.mpu6502 import MPU

from .opencbm import IEC_ATN, IEC_CLOCK, IEC_DATA, OpenCBMError

PB_DATA_IN, PB_DATA_OUT, PB_CLK_IN, PB_CLK_OUT, PB_ATNA, PB_ATN_IN = (
    0x01,
    0x02,
    0x04,
    0x08,
    0x10,
    0x80,
)
RETURN_TRAP = 0xFFF0
RAM, ROM, VIA1, OPEN, IO, VIA2, FDC = range(7)
ACR_T1_FREERUN, IRQ_T1, IRQ_T2 = 0x40, 0x40, 0x20
IO_ACCESS_CYCLE = 3
IDENTITY = {
    "1541": (0, "1541", "CBM DOS V2.6 1541"),
    "1571": (2, "1571", "CBM DOS V3.0 1571"),
}


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


class Drive1541:  # pylint: disable=too-many-instance-attributes
    """A drive CPU with its model's memory map, VIA1 and optional expansion RAM."""

    MODEL = "1541"
    EXPANSION = ((0x8000, 0xA000),)

    def __init__(self, device=8, expansion=None, bus=None, resets_to_boot=1, seed=0):
        self.bus = bus or Bus()
        self.bus.devices.append(self)
        self.device = device
        self._kind, self._phys, rom_at, size = memory_map(
            self.MODEL, self.EXPANSION if expansion is None else expansion
        )
        self.store = bytearray(size)
        self.store[rom_at:] = np.random.default_rng(seed).bytes(size - rom_at)
        self.pb_out = 0
        self.via1 = Via(self)
        self.mpu = MPU(memory=_Memory(self))
        self.halted = True
        self.cycles = 0
        self.resets_to_boot, self._pending = resets_to_boot, 0
        self.mech = None

    @property
    def responsive(self):
        """Whether DOS would answer on the bus."""
        return self.halted and not self._pending

    def read(self, addr):
        """CPU read."""
        k = self._kind[addr]
        if k <= ROM:
            return self.store[self._phys[addr]]
        if k == VIA1:
            return self.via1.read(self._phys[addr] & 0xF)
        if k in (VIA2, FDC) and self.mech is not None and self._phys[addr] < 16:
            return self.mech.read(k == FDC, self._phys[addr], self.cycles)
        return addr >> 8 if k == OPEN else 0

    def write(self, addr, value):
        """CPU write."""
        k = self._kind[addr]
        if k == RAM:
            self.store[self._phys[addr]] = value & 0xFF
        elif k == VIA1 and self._phys[addr] < 16:
            self.via1.write(self._phys[addr], value & 0xFF)
        elif k == VIA2 and self.mech is not None and self._phys[addr] < 16:
            self.mech.write(False, self._phys[addr], value & 0xFF, self.cycles)
        elif k == FDC and self.mech is not None and self._phys[addr] == 0:
            self.mech.write(True, 0, value & 0xFF, self.cycles)
        elif k not in (ROM, OPEN):
            raise SimIOWrite(f"write ${value & 0xFF:02x} to I/O ${addr:04x}")

    def drive_lines(self):
        """IEC lines this drive is asserting."""
        lines = 0
        atn = bool(self.bus.host_lines & IEC_ATN)
        if self.pb_out & PB_DATA_OUT or atn != bool(self.pb_out & PB_ATNA):
            lines |= IEC_DATA
        if self.pb_out & PB_CLK_OUT:
            lines |= IEC_CLOCK
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
        self._pending = max(self._pending - 1, 0)

    def step(self):
        """Execute one instruction unless halted; return cycles used."""
        if self.halted:
            return 0
        if self.mech is not None and self.cycles >= self.mech.due:
            self.mech.update(self.cycles)
        before = self.mpu.processorCycles
        self.mpu.step()
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
        """PA0 is the track 0 sensor input, low on track 1."""
        if self.mech is None:
            return latch
        return latch & ~self.PA_TRK0 | (0 if self.mech.track0 else self.PA_TRK0)


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


class SimCBM:
    """OpenCBM stand-in driving simulated drives sharing one bus.

    gap idles the drives that many cycles before each protocol byte;
    unplug() makes the host vanish part way into a chosen byte.
    """

    def __init__(self, drive=None, dev=8, budget=2_000_000):
        self.drive = drive or Drive1541(device=dev)
        self.bus = self.drive.bus
        self.drives = {dev: self.drive}
        self.dev = dev
        self.budget = budget
        self.gap = 0
        self._cut = None

    def attach(self, dev, drive):
        """Add another drive on the same bus."""
        assert drive.bus is self.bus
        self.drives[dev] = drive

    def close(self):
        """No-op for API compatibility."""

    def _answering(self, dev, grace=10_000):
        """Drive dev if DOS answers within grace cycles (a bus command's duration)."""
        d = self.drives.get(dev)
        t = 0
        while d is not None and t < grace and not d.halted:
            t += d.step()
        return d if d is not None and d.responsive else None

    def _dos(self, dev):
        d = self._answering(dev)
        if d is None:
            raise OpenCBMError(f"device {dev} not responding")
        return d

    def _step(self):
        n = max(d.step() for d in self.drives.values())
        if not n:
            raise SimTimeout("drive halted while host was waiting")
        return n

    def _run_until(self, cond):
        t = 0
        while not cond():
            t += self._step()
            if t > self.budget:
                raise SimTimeout("cycle budget exceeded")

    def settle(self):
        """Run the drives until their programs return."""
        self._run_until(lambda: all(d.halted for d in self.drives.values()))

    def idle(self, cycles):
        """Let running drives execute for about cycles without host activity."""
        t = 0
        while t < cycles and not all(d.halted for d in self.drives.values()):
            t += self._step()

    def unplug(self, after_bytes, edges=3):
        """Freeze the host after edges line changes into protocol byte after_bytes."""
        self._cut = [after_bytes, edges, False]

    def _byte(self):
        if self.gap:
            self.idle(self.gap)
        if self._cut and not self._cut[2]:
            self._cut[2] = self._cut[0] == 0
            self._cut[0] -= 1

    def _edge(self):
        if self._cut and self._cut[2]:
            if self._cut[1] == 0:
                self._cut = None
                raise HostGone("adapter vanished")
            self._cut[1] -= 1

    def _asserted(self, line):
        return bool(self.bus.lines() & line)

    def identify(self, dev):
        """cbm_identify: (device type code, description)."""
        code, desc, _ = IDENTITY[self._dos(dev).MODEL]
        return code, desc

    def status(self, dev):
        """Error channel; OpenCBM reports a silent drive as 99, DRIVER ERROR."""
        d = self._answering(dev)
        if d is None:
            return "99, DRIVER ERROR,00,00"
        return f"73,{IDENTITY[d.MODEL][2]},00,00"

    def reset(self):
        """Pulse RESET; the adapter releases its lines."""
        self.bus.host_lines = 0
        for d in self.drives.values():
            d.reset()

    def upload(self, dev, addr, data):
        """M-W."""
        self._dos(dev).load(addr, data)

    def download(self, dev, addr, size):
        """M-R."""
        return self._dos(dev).dump(addr, size)

    def command(self, dev, cmd):
        """Only M-E is modelled; like the xum1541, the host keeps CLK afterwards."""
        assert cmd[:3] == b"M-E"
        self._dos(dev).call(cmd[3] | cmd[4] << 8)
        self.bus.host_lines |= IEC_CLOCK

    def iec_poll(self):
        """Current bus state, after letting the drives run briefly."""
        for _ in range(16):
            for d in self.drives.values():
                d.step()
        return self.bus.lines()

    def iec_set(self, lines):
        """Assert host lines."""
        self._edge()
        self.bus.host_lines |= lines

    def iec_release(self, lines):
        """Release host lines."""
        self._edge()
        self.bus.host_lines &= ~lines

    def iec_wait(self, line, state):
        """Run the drive until line is asserted (state=1) or released (state=0)."""
        self._run_until(lambda: self._asserted(line) == bool(state))
        return self.iec_poll()

    def _wait(self, line, state):
        self._run_until(lambda: self._asserted(line) == state)

    def _put(self, line, state):
        (self.iec_set if state else self.iec_release)(line)

    def s1_read(self, size):
        """xum1541 s1_read_byte, size times."""
        out = bytearray()
        for _ in range(size):
            self._byte()
            c = 0
            for _ in range(8):
                self._wait(IEC_DATA, False)
                self.iec_release(IEC_CLOCK)
                b = self._asserted(IEC_CLOCK)
                c = c >> 1 | (0x80 if b else 0)
                self.iec_set(IEC_DATA)
                self._wait(IEC_CLOCK, not b)
                self.iec_release(IEC_DATA)
                self._wait(IEC_DATA, True)
                self.iec_set(IEC_CLOCK)
            out.append(c)
        return bytes(out)

    def s1_write(self, data):
        """xum1541 s1_write_byte for each byte."""
        for c in bytes(data):
            self._byte()
            for _ in range(8):
                bit = c & 0x80
                self._put(IEC_DATA, bit)
                self.iec_release(IEC_CLOCK)
                self._wait(IEC_CLOCK, True)
                self._put(IEC_DATA, not bit)
                self._wait(IEC_CLOCK, False)
                self.iec_set(IEC_CLOCK)
                self.iec_release(IEC_DATA)
                self._wait(IEC_DATA, True)
                c = c << 1 & 0xFF

    def _sample(self):
        return 0x80 if self._asserted(IEC_DATA) else 0

    def s2_read(self, size):
        """xum1541 s2_read_byte, size times."""
        out = bytearray()
        for _ in range(size):
            self._byte()
            c = 0
            for _ in range(4):
                self._wait(IEC_CLOCK, True)
                c = c >> 1 | self._sample()
                self.iec_release(IEC_ATN)
                self._wait(IEC_CLOCK, False)
                c = c >> 1 | self._sample()
                self.iec_set(IEC_ATN)
            out.append(c)
        return bytes(out)

    def s2_write(self, data):
        """xum1541 s2_write_byte for each byte."""
        for c in bytes(data):
            self._byte()
            for _ in range(4):
                self._put(IEC_DATA, c & 1)
                c >>= 1
                self.iec_release(IEC_ATN)
                self._wait(IEC_CLOCK, False)
                self._put(IEC_DATA, c & 1)
                c >>= 1
                self.iec_set(IEC_ATN)
                self._wait(IEC_CLOCK, True)
            self.iec_release(IEC_DATA)
