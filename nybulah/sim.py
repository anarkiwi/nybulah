"""Cycle-stepped 1541 + IEC bus + xum1541 model for running drive code without hardware.

py65 6502 with mirrored 2K RAM, expansion RAM and VIA1 port B on an open-collector
bus including the ATN auto-acknowledge; SimCBM mirrors the OpenCBM API subset in use.
"""

from py65.devices.mpu6502 import MPU

from .opencbm import IEC_ATN, IEC_CLOCK, IEC_DATA

PB_DATA_IN, PB_DATA_OUT, PB_CLK_IN, PB_CLK_OUT, PB_ATNA, PB_ATN_IN = (
    0x01,
    0x02,
    0x04,
    0x08,
    0x10,
    0x80,
)
RETURN_TRAP = 0xFFF0


class SimTimeout(RuntimeError):
    """The host waited longer than the cycle budget for a bus condition."""


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


class Drive1541:
    """A 1541 CPU with the memory map needed by drive-resident code."""

    def __init__(self, device=8, expansion=((0x8000, 0xA000),), bus=None):
        self.bus = bus or Bus()
        self.bus.devices.append(self)
        self.ram = bytearray(0x800)
        self.ranges = list(expansion)
        self.expansion = {lo: bytearray(hi - lo) for lo, hi in expansion}
        self.pb_out = 0
        self.device_bits = ((device - 8) & 3) << 5
        self.mpu = MPU(memory=_Memory(self))
        self.halted = True
        self.cycles = 0

    def _exp(self, addr):
        for lo, hi in self.ranges:
            if lo <= addr < hi:
                return self.expansion[lo], addr - lo
        return None, 0

    @staticmethod
    def _is_via1(addr):
        return addr < 0x8000 and addr & 0x1C00 == 0x1800

    def read(self, addr):
        """CPU read."""
        if addr == RETURN_TRAP:
            return 0xEA
        buf, off = self._exp(addr)
        if buf is not None:
            return buf[off]
        if self._is_via1(addr):
            return self.port_b() if addr & 0x0F == 0 else 0
        if addr < 0x8000 and addr & 0x1800 == 0:
            return self.ram[addr & 0x7FF]
        return 0

    def write(self, addr, value):
        """CPU write."""
        buf, off = self._exp(addr)
        if buf is not None:
            buf[off] = value & 0xFF
        elif self._is_via1(addr):
            if addr & 0x0F == 0:
                self.pb_out = value & (PB_DATA_OUT | PB_CLK_OUT | PB_ATNA)
        elif addr < 0x8000 and addr & 0x1800 == 0:
            self.ram[addr & 0x7FF] = value & 0xFF

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
        v = self.pb_out | self.device_bits
        v |= PB_DATA_IN if bus & IEC_DATA else 0
        v |= PB_CLK_IN if bus & IEC_CLOCK else 0
        v |= PB_ATN_IN if bus & IEC_ATN else 0
        return v

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
        self.halted = False

    def step(self):
        """Execute one instruction unless halted."""
        if not self.halted:
            before = self.mpu.processorCycles
            self.mpu.step()
            self.cycles += self.mpu.processorCycles - before
            if self.mpu.pc == RETURN_TRAP:
                self.halted = True


class SimCBM:
    """OpenCBM stand-in driving a single simulated drive."""

    def __init__(self, drive=None, dev=8, budget=2_000_000):
        self.drive = drive or Drive1541(device=dev)
        self.bus = self.drive.bus
        self.dev = dev
        self.budget = budget

    def close(self):
        """No-op for API compatibility."""

    def _run_until(self, cond):
        start = self.drive.cycles
        while not cond():
            if self.drive.halted:
                raise SimTimeout("drive halted while host was waiting")
            self.drive.step()
            if self.drive.cycles - start > self.budget:
                raise SimTimeout("cycle budget exceeded")

    def settle(self):
        """Run the drive until its program returns."""
        start = self.drive.cycles
        while not self.drive.halted:
            self.drive.step()
            if self.drive.cycles - start > self.budget:
                raise SimTimeout("cycle budget exceeded")

    def _asserted(self, line):
        return bool(self.bus.lines() & line)

    def upload(self, dev, addr, data):
        """M-W."""
        assert dev == self.dev
        self.drive.load(addr, data)

    def download(self, dev, addr, size):
        """M-R."""
        assert dev == self.dev
        return self.drive.dump(addr, size)

    def command(self, dev, cmd):
        """Only M-E is modelled; like the xum1541, the host keeps CLK afterwards."""
        assert dev == self.dev and cmd[:3] == b"M-E"
        self.drive.call(cmd[3] | cmd[4] << 8)
        self.bus.host_lines |= IEC_CLOCK

    def iec_poll(self):
        """Current bus state, after letting the drive run briefly."""
        for _ in range(16):
            self.drive.step()
        return self.bus.lines()

    def iec_set(self, lines):
        """Assert host lines."""
        self.bus.host_lines |= lines

    def iec_release(self, lines):
        """Release host lines."""
        self.bus.host_lines &= ~lines

    def iec_wait(self, line, state):
        """Run the drive until line is asserted (state=1) or released (state=0)."""
        self._run_until(lambda: self._asserted(line) == bool(state))
        return self.iec_poll()

    def _wait_clk(self, state):
        self._run_until(lambda: self._asserted(IEC_CLOCK) == state)

    def _wait(self, line, state):
        self._run_until(lambda: self._asserted(line) == state)

    def s1_read(self, size):
        """xum1541 s1_read_byte, size times."""
        out = bytearray()
        for _ in range(size):
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
            for _ in range(8):
                bit = c & 0x80
                (self.iec_set if bit else self.iec_release)(IEC_DATA)
                self.iec_release(IEC_CLOCK)
                self._wait(IEC_CLOCK, True)
                (self.iec_release if bit else self.iec_set)(IEC_DATA)
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
            c = 0
            for _ in range(4):
                self._wait_clk(True)
                c = c >> 1 | self._sample()
                self.iec_release(IEC_ATN)
                self._wait_clk(False)
                c = c >> 1 | self._sample()
                self.iec_set(IEC_ATN)
            out.append(c)
        return bytes(out)

    def s2_write(self, data):
        """xum1541 s2_write_byte for each byte."""
        for c in bytes(data):
            for _ in range(4):
                (self.iec_set if c & 1 else self.iec_release)(IEC_DATA)
                c >>= 1
                self.iec_release(IEC_ATN)
                self._wait_clk(False)
                (self.iec_set if c & 1 else self.iec_release)(IEC_DATA)
                c >>= 1
                self.iec_set(IEC_ATN)
                self._wait_clk(True)
            self.iec_release(IEC_DATA)
