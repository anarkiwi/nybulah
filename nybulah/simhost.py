"""Host stand-ins for the simulated drives: SimCBM (the OpenCBM API subset over the
simulated IEC bus, xum1541 transfers), SimMonitor (drive calls without a bus) and
DOSBus (DOS-level drives with boot and command times, for bus scripts).
"""

import numpy as np

from .opencbm import IEC_ATN, IEC_CLOCK, IEC_DATA, IEC_SRQ, OpenCBMError
from .sim import PA_FSDIR, Bus, Drive1541, HostGone, SimTimeout
from .simfast import NEVER, eligible, run_drive, run_transfer

IDENTITY = {
    "1541": (0, "1541", "CBM DOS V2.6 1541"),
    "1571": (2, "1571", "CBM DOS V3.0 1571"),
}


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
        if d is not None:
            run_drive(d, d.cycles + grace)
        return d if d is not None and d.responsive else None

    def _dos(self, dev):
        d = self._answering(dev)
        if d is None:
            raise OpenCBMError(f"device {dev} not responding")
        return d

    def _solo(self):
        """The one running drive on a plain bus, else None."""
        running = [d for d in self.drives.values() if not d.halted]
        return (
            running[0]
            if len(running) == 1 and type(self.bus).lines is Bus.lines
            else None
        )

    def _step(self, budget=0):
        """Cycles to the next possible bus change (a lone drive runs at most budget)."""
        d = self._solo()
        if d is None:
            n = max(d.step() for d in self.drives.values())
        else:
            c0 = d.cycles
            run_drive(d, c0 + budget + 1, lines=True)
            n = d.cycles - c0
        if not n:
            raise SimTimeout("drive halted while host was waiting")
        return n

    def _run_until(self, cond):
        t = 0
        while not cond():
            t += self._step(self.budget - t)
            if t > self.budget:
                raise SimTimeout("cycle budget exceeded")

    def settle(self):
        """Run the drives until their programs return."""
        self._run_until(lambda: all(d.halted for d in self.drives.values()))

    def idle(self, cycles):
        """Let running drives execute for about cycles without host activity."""
        t = 0
        while t < cycles and not all(d.halted for d in self.drives.values()):
            t += self._step(cycles - t - 1)

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

    def _compiled(self, protocol, data):
        """data moved by the compiled xum1541 model, None where only Python models it."""
        d = self._solo()
        if d is None or self.gap or self._cut or not eligible(d):
            return None
        buf = np.frombuffer(bytes(data), np.uint8).copy()
        run_transfer(d, protocol, buf, self.budget)
        return buf.tobytes()

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
        solo = self._solo()
        if solo is not None:
            run_drive(solo, NEVER, steps=16)
        for _ in range(0 if solo else 16):
            for d in self.drives.values():
                d.step()
        return self.bus.lines()

    def iec_set(self, lines):
        """Assert host lines."""
        self._edge()
        self.bus.host_lines |= lines

    def iec_release(self, lines):
        """Release host lines; a released SRQ clocks DATA into listening CIAs."""
        self._edge()
        rising = self.bus.lines() & IEC_SRQ & lines
        self.bus.host_lines &= ~lines
        if rising and not self.bus.lines() & IEC_SRQ:
            sp = 0 if self.bus.lines() & IEC_DATA else 1
            for d in self.drives.values():
                if d.cia is not None and not (d.TIMED or d.via1.regs[1] & PA_FSDIR):
                    d.cia.edge(sp)

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
        out = self._compiled("s1_read", bytes(size))
        if out is not None:
            return out
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
        if self._compiled("s1_write", data) is not None:
            return
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
        out = self._compiled("s2_read", bytes(size))
        if out is not None:
            return out
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
        if self._compiled("s2_write", data) is not None:
            return
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


class SimMonitor:
    """Monitor stand-in that runs drive code directly, without a bus transport.

    ``calls`` records ``(address, first cycle, last cycle)`` of every jsr.
    """

    def __init__(self, drive, budget=50_000_000):
        self.drive, self.budget = drive, budget
        self.calls = []

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
        run_drive(d, start + self.budget + 1)
        if not d.halted:
            raise TimeoutError(f"jsr ${addr:04x} ran past {self.budget} cycles")
        self.calls.append((addr, start, d.cycles))
        return d.mpu.a, d.mpu.x, d.mpu.y


class DOSDrive:
    """A drive at DOS level for bus scripts, on DOSBus's clock.

    After RESET DOS is busy for boot_s, holding CLK and DATA for the first
    held_s (the diagnostic); command_s(cmd) is how long a command keeps it busy
    (inf: never done) and reply(cmd) the status it leaves.
    """

    def __init__(  # pylint: disable=too-many-arguments
        self, dev, model="1541", boot_s=0.0, command_s=None, reply=None, files=()
    ):
        self.dev, self.model, self.boot_s = dev, model, boot_s
        self.held_s = boot_s
        self.command_s = command_s or (lambda cmd: 0.0)
        self.reply = reply or (lambda cmd: "00, OK,00,00")
        self.files = files
        self.booting = self.busy = 0.0
        self.status = f"73,{IDENTITY[model][2]},00,00"

    def restart(self, now):
        """RESET or UJ: rerun the diagnostic, then the power-on message."""
        self.booting, self.busy = now + self.held_s, now + self.boot_s
        self.status = f"73,{IDENTITY[self.model][2]},00,00"

    def directory(self):
        """The directory as DOS sends it: a BASIC program."""
        lines = [(0, b'\x12"SIM" 00 2A')]
        lines += [(n, f'"{f}" PRG'.encode()) for f, n in self.files]
        lines.append((664, b"BLOCKS FREE."))
        body = b"".join(b"\1\1" + n.to_bytes(2, "little") + t + b"\0" for n, t in lines)
        return b"\1\4" + body + b"\0\0"


class DOSBus:  # pylint: disable=too-many-instance-attributes
    """OpenCBM stand-in over DOSDrives on a virtual clock (clock(), sleep()).

    An ATN sequence waits until every drive's DOS is idle (a busy DOS holds the
    hardware ATN acknowledge), bounded by the I/O timeout; ``log`` records each
    one as (time, kind, dev, busy devices) and ``violations`` the misuse.
    """

    NO_ACK_S = 0.002

    def __init__(self, drives):
        self.drives = {d.dev: d for d in drives}
        self.now, self.timeout = 0.0, float("inf")
        self.host_lines = 0
        self.addressed = None
        self.log, self.violations = [], []
        self.hook, self.closed = None, False
        self._stream, self._eoi = b"", False

    def clock(self):
        """Virtual time."""
        return self.now

    def sleep(self, seconds):
        """Advance virtual time."""
        self.now += seconds

    def close(self):
        """Release the adapter."""
        self.closed = True

    def set_timeout(self, ms):
        """xum1541 I/O idle timeout, in 100 ms ticks."""
        self.timeout = -(-ms // 100) / 10 if ms else float("inf")
        return True

    def reset(self):
        """RESET: the adapter releases its lines and every drive restarts."""
        self.host_lines, self.addressed = 0, None
        self.now += 0.1
        for d in self.drives.values():
            d.restart(self.now)

    def iec_poll(self):
        """Host lines plus CLK and DATA from any drive in its diagnostic."""
        booting = any(d.booting > self.now for d in self.drives.values())
        return self.host_lines | (IEC_CLOCK | IEC_DATA if booting else 0)

    def iec_release(self, lines):
        """Release host lines."""
        self.host_lines &= ~lines

    def _atn(self, kind, dev):
        """One ATN sequence; returns dev's drive or None when absent."""
        if self.hook:
            self.hook(kind, dev)
        busy = sorted(d.dev for d in self.drives.values() if d.busy > self.now)
        self.log.append((self.now, kind, dev, busy))
        if self.addressed or (busy and kind == "listen") or self.iec_poll():
            self.violations.append((self.now, kind, dev, busy, self.addressed))
        if not self.drives:
            self.now += self.NO_ACK_S
            raise OpenCBMError("no device acknowledged ATN")
        ready = max(d.busy for d in self.drives.values())
        if ready - self.now > self.timeout:
            self.now += self.timeout
            self.host_lines = 0
            raise OpenCBMError("adapter I/O timeout")
        self.now = max(self.now, ready)
        return self.drives.get(dev)

    def _listener(self, dev):
        d = self._atn("listen", dev)
        if d is None:
            self.now += self.NO_ACK_S
            raise OpenCBMError(f"device {dev} not present")
        return d

    def _talker(self, dev):
        d = self._atn("talk", dev)
        if d is None:
            self.now += self.timeout
            raise OpenCBMError(f"device {dev} never took CLK")
        return d

    def status(self, dev):
        """TALK 15, read, UNTALK; reading resets the channel to 00, OK."""
        try:
            d = self._talker(dev)
        except OpenCBMError:
            return "99 DRIVER ERROR,01,00"
        status, d.status = d.status, "00, OK,00,00"
        return status

    def command(self, dev, cmd):
        """LISTEN 15, the command, UNLISTEN; DOS then runs it."""
        d = self._listener(dev)
        if cmd[:2] in (b"UJ", b"U:"):
            d.restart(self.now)
        else:
            d.busy, d.status = self.now + d.command_s(cmd), d.reply(cmd)

    def identify(self, dev):
        """cbm_identify: M-R of the ROM footprint, which leaves 00, OK."""
        d = self._listener(dev)
        d.status = "00, OK,00,00"
        code, desc, _ = IDENTITY[d.model]
        return code, desc

    def open_file(self, dev, sa, name):
        """OPEN: LISTEN, name, UNLISTEN; "$" builds the directory."""
        d = self._listener(dev)
        assert sa == 0 and name == b"$"
        d.busy = self.now + d.command_s(name)
        self._stream = d.directory()

    def close_file(self, dev, sa):
        """CLOSE: LISTEN, CLOSE sa, UNLISTEN."""
        assert sa == 0
        self._listener(dev)

    def talk(self, dev, sa):
        """Address dev as talker."""
        assert sa == 0
        self._talker(dev)
        self.addressed = ("talk", dev)

    def untalk(self):
        """UNTALK."""
        self.addressed = None
        self._atn("untalk", None)

    def raw_read(self, size):
        """Up to size bytes of the talker's stream."""
        assert self.addressed
        out, self._stream = self._stream[:size], self._stream[size:]
        self._eoi = not self._stream
        return out

    def get_eoi(self):
        """EOI on the last byte read."""
        return self._eoi
