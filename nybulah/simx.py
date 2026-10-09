"""Timed co-simulation of the X transport (drive/proto_x.inc) against an xum1541 model.

Times are in microseconds; port accesses happen mid final cycle, assertions are instant
and releases read asserted for `rise`. SimX mirrors the firmware's per-byte go, SYNC
detection and pair timing; `python -m nybulah.simx` prints the derived timing table.
"""

import argparse
import dataclasses
import math
from functools import cached_property

import numpy as np

from .fastx import PA_2MHZ
from .opencbm import IEC_ATN, IEC_CLOCK, IEC_DATA, OpenCBMError
from .sim import (
    PB_ATN_IN,
    PB_CLK_IN,
    PB_DATA_IN,
    VIA1,
    Bus,
    Drive1541,
    Drive1571,
    HostGone,
    IdleDOSDrive,
    SimCBM,
)

SEND_SCHEDULE = (0, 13, 25, 35, 47, 57)
RECV_SCHEDULE = (0, 6, 12, 21, 34, 43)
SEND_PAIRS = ((1, 3), (5, 7), (0, 2), (4, 6))
RECV_PAIRS = ((0, 2), (1, 3), (4, 6), (5, 7))
GRACE = 32
AVR_HZ = 16_000_000


class XError(OpenCBMError):
    """The adapter gave up on an X transfer; partial holds the bytes moved."""

    def __init__(self, msg, partial=b""):
        super().__init__(msg)
        self.partial = bytes(partial)


class XTimeout(XError):
    """The drive never produced SYNC within the adapter's I/O timeout."""


def encode(byte, pairs):
    """Bus line masks (IEC_DATA, IEC_CLOCK) carrying byte in pair order."""
    return tuple(
        (IEC_DATA if byte >> d & 1 else 0) | (IEC_CLOCK if byte >> c & 1 else 0)
        for d, c in pairs
    )


def decode(samples, pairs):
    """Inverse of encode."""
    return sum(
        (bool(s & IEC_DATA) << d) | (bool(s & IEC_CLOCK) << c)
        for s, (d, c) in zip(samples, pairs)
    )


@dataclasses.dataclass(frozen=True)
class Timing:
    """Adapter offsets from the SYNC detection, centred in the drive's windows.

    cyc: drive cycle in us (0.5 for a 1571 at 2 MHz); rise: release budget; poll,
    sync: adapter edge loop and synchroniser; via: drive sampling error (cyc/2);
    turn: adapter time from the last sample to the next go.
    """

    cyc: float = 1.0
    rise: float = 1.0
    poll: float = 6 / 16
    sync: float = 1 / 16
    via: float | None = None
    turn: float = 3.0

    @property
    def v(self):
        """Drive port sampling uncertainty."""
        return 0.5 * self.cyc if self.via is None else self.via

    @cached_property
    def sample(self):
        """Drive -> host: sampling offsets for P0..P3."""
        w = np.array(SEND_SCHEDULE) * self.cyc
        return tuple((w[1:5] + self.rise + w[2:6] - self.poll) / 2)

    @cached_property
    def send_margin(self):
        """Slack on each side of every drive -> host sample."""
        w = np.array(SEND_SCHEDULE) * self.cyc
        return tuple((w[2:6] - w[1:5] - self.rise - self.poll) / 2)

    @cached_property
    def drive(self):
        """Host -> drive: output change offsets for P0..P3 and the final release."""
        r = np.array(RECV_SCHEDULE) * self.cyc
        c = (r[1:5] + r[2:6] - self.poll - self.rise) / 2 - self.sync
        c[0] = 4 / 16
        return tuple(c) + (r[5] + 4 * self.cyc,)

    @cached_property
    def recv_margin(self):
        """Slack on each side of every drive read."""
        r = np.array(RECV_SCHEDULE) * self.cyc
        m = (r[2:6] - r[1:5] - 2 * self.v - self.poll - self.rise) / 2
        m[0] = r[2] - r[1] - self.v - self.rise
        return tuple(m)

    def avr_cycles(self):
        """Offsets in adapter clocks."""
        f = AVR_HZ / 1e6
        return {
            "sample": [round(x * f) for x in self.sample],
            "drive": [round(x * f) for x in self.drive],
        }


class TimedBus(Bus):
    """Bus whose lines carry per-source edge history in microseconds."""

    def __init__(self, rise=0.5):
        self.rise = rise
        self.edges = {}
        self.clock = lambda: 0.0
        self.version = 0
        self._host = 0
        super().__init__()

    @property
    def host_lines(self):
        """Lines the adapter asserts."""
        return self._host

    @host_lines.setter
    def host_lines(self, value):
        self.drive_host(value, self.clock())

    def drive_host(self, value, t):
        """Set the adapter's asserted lines at time t."""
        self._host = value
        self.record("host", value, t)

    def record(self, src, value, t):
        """Note source src asserting the lines in value from time t."""
        for line in (IEC_DATA, IEC_CLOCK):
            hist = self.edges.setdefault((src, line), [(-math.inf, False)])
            state = bool(value & line)
            if hist[-1][1] != state:
                hist.append((max(t, hist[-1][0]), state))
                del hist[:-16]
                self.version += 1

    def _holds(self, hist, t):
        for ti, si in reversed(hist):
            if ti <= t:
                return si or t < ti + self.rise
        return False

    def level(self, t):
        """Wired-OR line state at time t."""
        v = self._host & IEC_ATN
        for (_, line), hist in self.edges.items():
            if not v & line and self._holds(hist, t):
                v |= line
        for d in self.devices:
            if not isinstance(d, TimedDrive1541):
                v |= d.drive_lines()
        return v

    def settles(self, t):
        """Earliest time after t at which a rising line finishes, else inf."""
        return min(
            (
                ti + self.rise
                for hist in self.edges.values()
                for ti, si in hist[-2:]
                if not si and t < ti + self.rise
            ),
            default=math.inf,
        )

    def lines(self):
        return self.level(self.clock())


class TimedDrive1541(Drive1541):
    """1541 on a TimedBus: timestamps port writes and reads the bus at access time."""

    def __init__(self, *args, cyc=1.0, read_jitter=0.0, seed=0, **kw):
        self.cyc, self.read_jitter = cyc, read_jitter
        self.rng = np.random.default_rng(seed)
        self.t_access = 0.0
        self._pb = 0
        self._t0, self._c0 = 0.0, 0
        super().__init__(*args, **kw)
        self.bus.clock = lambda: self.time(self.cycles)

    def time(self, cycles):
        """Microseconds at a cycle count, across clock-speed changes."""
        return self._t0 + (cycles - self._c0) * self.cyc

    def set_cyc(self, cyc):
        """Change the CPU clock period from the current cycle onwards."""
        self._t0, self._c0, self.cyc = self.time(self.cycles), self.cycles, cyc

    @property
    def pb_out(self):
        """VIA1 port B outputs."""
        return self._pb

    @pb_out.setter
    def pb_out(self, value):
        self._pb = value
        if isinstance(self.bus, TimedBus):
            self.bus.record(self, self.drive_lines(), self.t_access)

    def next_access(self):
        """Time of the next instruction's port access."""
        op = self.read(self.mpu.pc)
        return self.time(self.cycles + self.mpu.cycletime[op] - 0.5)

    def step(self):
        """Execute one instruction with its access time set."""
        if not self.halted:
            self.t_access = self.next_access()
        return super().step()

    def port_b(self):
        """Port B with the bus sampled at the access time (plus jitter)."""
        dt = self.rng.uniform(-1, 1) * self.read_jitter if self.read_jitter else 0
        bus = self.bus.level(self.t_access + dt)
        v = self.pb_out | ((self.device - 8) & 3) << 5
        v |= PB_DATA_IN if bus & IEC_DATA else 0
        v |= PB_CLK_IN if bus & IEC_CLOCK else 0
        return v | (PB_ATN_IN if bus & IEC_ATN else 0)


class TimedDrive1571(TimedDrive1541):
    """1571 on a TimedBus; VIA1 PA5 (or cyc=0.5) selects 2 MHz."""

    MODEL, EXPANSION = Drive1571.MODEL, Drive1571.EXPANSION

    def write(self, addr, value):
        """CPU write; a VIA1 port A write applies PA5 to the clock."""
        super().write(addr, value)
        if self._kind[addr] == VIA1 and self._phys[addr] & 0xF in (1, 15):
            self.set_cyc(0.5 if value & PA_2MHZ else 1.0)


class SimX(SimCBM):
    """SimCBM plus the xum1541 X protocol model (x_read/x_write, x2_* at 2 MHz).

    skew shifts adapter offsets; faults: byte ordinal -> (pair, line) flipped on the
    wire; pause: byte ordinal -> stall (us) before go; vanish_at: byte ordinal.
    """

    def __init__(self, drive, dev=8, timing=None, seed=0, **kw):
        super().__init__(drive, dev, kw.pop("budget", 4_000_000))
        self.timing = timing or Timing(cyc=drive.cyc)
        self.rng = np.random.default_rng(seed)
        self.slice_us = kw.pop("slice_us", 20_000.0)
        self.timeout_us = kw.pop("timeout_us", 200_000.0)
        if kw:
            raise TypeError(f"unexpected {sorted(kw)}")
        self.skew, self.faults, self.pause, self.vanish_at = 0.0, {}, {}, None
        self.now, self.ordinal, self._go = 0.0, 0, False
        self.retracts = 0

    def _advance(self, t):
        d = self.drive
        while not d.halted and d.next_access() < t:
            d.step()

    def _host(self, value, t):
        self.bus.drive_host(value, t)

    def _poll_until(self, line, state, t0, deadline):
        """First poll instant in [t0, deadline] seeing line in state, else None."""
        p, s, bus = self.timing.poll, self.timing.sync, self.bus
        t = t0 + self.rng.uniform(0, p)
        while t <= deadline:
            self._advance(t - s)
            if bool(bus.level(t - s) & line) == state:
                return t
            seen = bus.version
            nxt = min(
                bus.settles(t - s),
                math.inf if self.drive.halted else self.drive.next_access(),
            )
            if nxt == math.inf:
                return None
            if nxt > t - s + p and bus.version == seen:
                t += math.ceil((nxt - (t - s)) / p) * p
            else:
                t += p
        return None

    def unplug(self, after_bytes, edges=3):
        """Make the adapter vanish before X byte after_bytes from now."""
        del edges
        self.vanish_at = self.ordinal + after_bytes

    def _sync(self):
        """Assert go and return the SYNC detection time, retracting on slices."""
        if self.gap:
            self.idle(self.gap)
        if self.vanish_at == self.ordinal:
            self.vanish_at = None
            raise HostGone("adapter vanished")
        self.now = max(self.now, self.drive.time(self.drive.cycles))
        self.now += self.pause.pop(self.ordinal, 0.0)
        start = self.now
        grace = GRACE * self.timing.cyc
        while True:
            if not self._go:
                self._host(IEC_DATA, self.now)
                self._go = True
            end = self.now + self.slice_us
            t = self._poll_until(IEC_CLOCK, False, self.now, end)
            if t is not None:
                t = self._poll_until(IEC_CLOCK, True, t, end)
            if t is None:
                self._host(0, end)
                self._go = False
                self.retracts += 1
                t = self._poll_until(IEC_CLOCK, True, end, end + grace)
            if t is not None:
                self.now = t
                self.ordinal += 1
                return t
            self.now = end + grace
            if self.now - start > self.timeout_us:
                raise XTimeout("no SYNC from drive")

    def _flip(self, k):
        f = self.faults.get(self.ordinal - 1)
        return f[1] if f and f[0] == k else 0

    def _fail(self, e, partial):
        self._host(0, self.now)
        self._go = False
        e.partial = bytes(partial)
        return e

    def x_read(self, size, timing=None):
        """Drive -> host transfer of size bytes."""
        tm = timing or self.timing
        out = bytearray()
        for _ in range(size):
            try:
                t = self._sync()
            except (XTimeout, HostGone) as e:
                raise self._fail(e, out)
            self._host(0, t + 2 / 16)
            self._go = False
            samples = []
            for k, off in enumerate(tm.sample):
                at = t + off + self.skew - tm.sync
                self._advance(at)
                samples.append(self.bus.level(at) ^ self._flip(k))
            out.append(decode(samples, SEND_PAIRS))
            self.now = t + tm.sample[3] + tm.turn
        return bytes(out)

    def x_write(self, data, timing=None):
        """Host -> drive transfer."""
        tm = timing or self.timing
        data = bytes(data)
        for i, b in enumerate(data):
            try:
                t = self._sync()
            except (XTimeout, HostGone) as e:
                raise self._fail(e, data[:i])
            for k, v in enumerate(encode(b, RECV_PAIRS)):
                self._host(v ^ self._flip(k), t + tm.drive[k] + self.skew)
            self.now = t + tm.drive[4] + self.skew
            self._go = False
            self._host(0, self.now)
            self.now += tm.turn
        self._advance(self.now)

    def x2_read(self, size):
        """2 MHz schedule read (1571 in fast mode)."""
        return self.x_read(size, dataclasses.replace(self.timing, cyc=0.5))

    def x2_write(self, data):
        """2 MHz schedule write (1571 in fast mode)."""
        self.x_write(data, dataclasses.replace(self.timing, cyc=0.5))

    s3_read, s3_write = x_read, x_write


TIMED = {Drive1541: TimedDrive1541, Drive1571: TimedDrive1571}


def adapter(protocol, cls=Drive1541, device=8, bus=None, **drive_kw):
    """SimX on a TimedBus for s3, else SimCBM, around a new cls drive."""
    if protocol != "s3":
        return SimCBM(cls(device=device, bus=bus or Bus(), **drive_kw), dev=device)
    drive = TIMED[cls](device=device, bus=bus or TimedBus(), **drive_kw)
    return SimX(drive, device)


def make(model="1541", cyc=1.0, rise=0.5, peers=0, dev=8, **kw):
    """SimX with a timed drive (and idle DOS peers) on a fresh bus."""
    bus = TimedBus(rise)
    cls = TimedDrive1571 if model == "1571" else TimedDrive1541
    drive = cls(device=dev, bus=bus, cyc=cyc, read_jitter=kw.pop("read_jitter", 0.0))
    for _ in range(peers):
        IdleDOSDrive(bus)
    return SimX(drive, dev, **kw)


def report(cyc):
    """Derived offsets and margins for one drive clock."""
    t = Timing(cyc=cyc)
    r = {"cyc_us": cyc}
    for k in ("sample", "send_margin", "drive", "recv_margin"):
        r[k + "_us"] = [round(float(x), 3) for x in getattr(t, k)]
    r["avr_cycles"] = t.avr_cycles()
    return r


def main(argv=None):
    """Print the derived timing table."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cyc", type=float, nargs="*", default=[1.0, 0.5])
    for cyc in ap.parse_args(argv).cyc:
        print(report(cyc))


if __name__ == "__main__":
    main()
