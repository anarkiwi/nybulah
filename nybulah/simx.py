"""Timed co-simulation of X (drive/proto_x.inc) and burst X (proto_xb.inc) with xum1541.

Times in us; port accesses happen mid final cycle, assertions are instant, releases read
asserted for `rise`. SimX mirrors the firmware's go, SYNC detection and pair timing (the
burst model in its integer clocks); `python -m nybulah.simx` prints the timing tables.
"""

import argparse
import dataclasses
import math
from collections import Counter
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
XB_BURST = 64
XB_SEND = (14, 26, 50, 62, 74)
XB_SEND_PERIOD = 67
XB_RECV = (6, 12, 20, 30, 38)
XB_RECV_PERIOD = 52
XB_SEND_PAIRS = ((1, 3), (5, 7), (0, 2), (4, 6))
XB_RECV_PAIRS = ((5, 7), (4, 6), (1, 3), (0, 2))
AVR_RISE, AVR_POLL, AVR_SYNC, AVR_FOUND = 16, 6, 1, 4


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

    cyc: drive cycle in us; rise: release budget; poll, sync: adapter edge loop and
    synchroniser; via: drive sampling error (cyc/2); turn: adapter time from the last
    sample to the next go; slice, timeout: go retraction slice and I/O timeout.
    """

    cyc: float = 1.0
    rise: float = 1.0
    poll: float = 6 / 16
    sync: float = 1 / 16
    via: float | None = None
    turn: float = 3.0
    slice: float = 20_000.0
    timeout: float = 200_000.0

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


@dataclasses.dataclass(frozen=True)
class BurstTiming:
    """Firmware v10 burst offsets in adapter clocks (x.c X_SAMPLE/X_CHANGE).

    f: clocks per drive cycle (16 at 1 MHz, 8 at 2 MHz); offsets count from the SYNC
    poll, byte i adds i periods, against XB_SEND/XB_RECV (drive cycles after SYNC).
    rise, poll, sync: adapter budgets in clocks; via: drive sampling error in us.
    """

    f: int = 16
    rise: int = AVR_RISE
    poll: int = AVR_POLL
    sync: int = AVR_SYNC
    via: float | None = None

    @property
    def cyc(self):
        """Drive cycle in us."""
        return self.f / 16

    @property
    def v(self):
        """Drive port sampling uncertainty (us)."""
        return 0.5 * self.cyc if self.via is None else self.via

    @property
    def send_period(self):
        """Clocks per drive -> host byte."""
        return XB_SEND_PERIOD * self.f

    @property
    def recv_period(self):
        """Clocks per host -> drive byte."""
        return XB_RECV_PERIOD * self.f

    @cached_property
    def sample(self):
        """Drive -> host: sample clocks of P0..P3, centred in [write + rise, next)."""
        w = XB_SEND
        return tuple(
            ((w[k] + w[k + 1]) * self.f + self.rise - self.poll) // 2 for k in range(4)
        )

    def _mid(self, a, b):
        return ((a + b) * self.f - self.poll - self.rise) // 2 - self.sync

    @cached_property
    def change(self):
        """Host -> drive: P0 at detection, P1..P3, the next byte's P0, release."""
        r, n = XB_RECV, XB_RECV_PERIOD
        mids = tuple(self._mid(r[k], r[k + 1]) for k in (1, 2, 3))
        return (AVR_FOUND,) + mids + (self._mid(r[4], r[1] + n), (r[4] + 4) * self.f)

    def send_slack(self, ppm=0.0, n=XB_BURST):
        """Per pair (left, right) slack in us of the worst byte of an n-byte burst."""
        w, c, e = XB_SEND, self.cyc, abs(ppm) * 1e-6
        d = (self.sample[3] + (n - 1) * self.send_period) / 16 * e
        return tuple(
            (
                self.sample[k] / 16 - (w[k] * c + self.rise / 16) - d,
                w[k + 1] * c - (self.sample[k] + self.poll) / 16 - d,
            )
            for k in range(4)
        )

    def recv_slack(self, ppm=0.0, n=XB_BURST):
        """Per change (left, right) slack in us: P0 of byte 0, P1..P3, P0 of the next
        byte, and the left slack of the final release."""
        r, ch = np.array(XB_RECV) * self.cyc, np.array(self.change) / 16
        d = (self.change[5] + (n - 1) * self.recv_period) / 16 * abs(ppm) * 1e-6
        early, late = self.sync / 16, (self.sync + self.poll + self.rise) / 16
        prev = r[1:] + self.v
        nxt = np.append(r[2:], r[1] + XB_RECV_PERIOD * self.cyc) - self.v
        left, right = early + ch[1:5] - prev - d, nxt - late - ch[1:5] - d
        first = (
            r[1] - self.v - max(r[0], late - self.rise / 16 + ch[0]) - self.rise / 16
        )
        return (
            ((math.inf, first),)
            + tuple(zip(left, right))
            + ((early + ch[5] - prev[3] - d, math.inf),)
        )

    def margin(self, ppm=0.0, n=XB_BURST):
        """Smallest (send, recv) slack in us."""
        return (
            min(min(p) for p in self.send_slack(ppm, n)),
            min(min(p) for p in self.recv_slack(ppm, n)),
        )


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
    """SimCBM plus the xum1541 X model (x_*, x2_* at 2 MHz) and burst X (xb_*, xb2_*).

    skew shifts adapter offsets; ppm scales burst offsets (adapter clock error); faults:
    byte ordinal -> (pair, line) flipped; pause: byte ordinal -> stall (us) before go;
    vanish_at: byte ordinal. firmware < 10 lacks burst X.
    """

    def __init__(self, drive, dev=8, timing=None, seed=0, **kw):
        super().__init__(drive, dev, kw.pop("budget", 4_000_000))
        slice_us = kw.pop("slice_us", Timing.slice)
        timeout_us = kw.pop("timeout_us", Timing.timeout)
        self.timing = timing or Timing(drive.cyc, slice=slice_us, timeout=timeout_us)
        self.rng = np.random.default_rng(seed)
        self.firmware = kw.pop("firmware", 10)
        if kw:
            raise TypeError(f"unexpected {sorted(kw)}")
        self.skew, self.ppm, self.faults, self.pause = 0.0, 0.0, {}, {}
        self.vanish_at = None
        self.now, self.ordinal, self._go = 0.0, 0, False
        self.count = Counter()

    @property
    def retracts(self):
        """Go withdrawals after a slice without SYNC."""
        return self.count["retracts"]

    @property
    def bursts(self):
        """Burst transfers started."""
        return self.count["bursts"]

    def supports(self, protocol):
        """Whether this adapter speaks protocol ("xb" needs firmware 10)."""
        if protocol == "xb":
            return self.firmware >= 10
        return hasattr(self, f"{protocol}_read")

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

    def _sync(self, n=1):
        """Assert go for the next n bytes and return the SYNC detection time,
        retracting on slices."""
        if self.gap:
            self.idle(self.gap)
        span = range(self.ordinal, self.ordinal + n)
        if self.vanish_at in span:
            self.vanish_at = None
            raise HostGone("adapter vanished")
        self.now = max(self.now, self.drive.time(self.drive.cycles))
        self.now += sum(self.pause.pop(i, 0.0) for i in span)
        start = self.now
        grace = GRACE * self.timing.cyc
        while True:
            if not self._go:
                self._host(IEC_DATA, self.now)
                self._go = True
            end = self.now + self.timing.slice
            t = self._poll_until(IEC_CLOCK, False, self.now, end)
            if t is not None:
                t = self._poll_until(IEC_CLOCK, True, t, end)
            if t is None:
                self._host(0, end)
                self._go = False
                self.count["retracts"] += 1
                t = self._poll_until(IEC_CLOCK, True, end, end + grace)
            if t is not None:
                self.now = t
                self.ordinal += n
                return t
            self.now = end + grace
            if self.now - start > self.timing.timeout:
                raise XTimeout("no SYNC from drive")

    def _flip(self, k, ordinal=None):
        f = self.faults.get(self.ordinal - 1 if ordinal is None else ordinal)
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

    def _clock(self, t, clocks):
        """Time of an adapter offset from the detection at t."""
        return t + clocks * (1 + self.ppm * 1e-6) / 16 + self.skew

    def _burst_timing(self, f):
        return BurstTiming(f or round(16 * self.timing.cyc), via=self.timing.via)

    def xb_read(self, size, f=None):
        """Drive -> host burst transfer of size bytes (f defaults to the timing's
        drive clock, as x_read does)."""
        bt, out = self._burst_timing(f), bytearray()
        while len(out) < size:
            n = min(XB_BURST, size - len(out))
            first = self.ordinal
            try:
                t = self._sync(n)
            except (XTimeout, HostGone) as e:
                raise self._fail(e, out)
            self._host(0, t + (AVR_FOUND + 2) / 16)
            self._go = False
            self.count["bursts"] += 1
            for i in range(n):
                samples = []
                for k, off in enumerate(bt.sample):
                    at = self._clock(t, off + i * bt.send_period) - bt.sync / 16
                    self._advance(at)
                    samples.append(self.bus.level(at) ^ self._flip(k, first + i))
                out.append(decode(samples, XB_SEND_PAIRS))
            self.now = at + self.timing.turn
        return bytes(out)

    def _xb_changes(self, bt, burst, first):
        """(clock, lines) of a host -> drive burst: P0 at detection, P1..P3, the next
        byte's P0 and, after the last byte, the release."""
        pairs = [
            [
                v ^ self._flip(k, first + i)
                for k, v in enumerate(encode(b, XB_RECV_PAIRS))
            ]
            for i, b in enumerate(burst)
        ]
        out = [(bt.change[0], pairs[0][0])]
        for i, p in enumerate(pairs):
            base = i * bt.recv_period
            out += [(base + bt.change[k], p[k]) for k in (1, 2, 3)]
            more = i + 1 < len(pairs)
            out.append(
                (base + bt.change[4 if more else 5], pairs[i + 1][0] if more else 0)
            )
        return out

    def xb_write(self, data, f=None):
        """Host -> drive burst transfer."""
        bt, data = self._burst_timing(f), bytes(data)
        for j in range(0, len(data), XB_BURST):
            burst, first = data[j : j + XB_BURST], self.ordinal
            try:
                t = self._sync(len(burst))
            except (XTimeout, HostGone) as e:
                raise self._fail(e, data[:j])
            self.count["bursts"] += 1
            for clock, lines in self._xb_changes(bt, burst, first):
                at = self._clock(t, clock)
                self._advance(at)
                self._host(lines, at)
            self._go = False
            self.now = at + self.timing.turn
        self._advance(self.now)

    def xb2_read(self, size):
        """Burst read timed for a 1571 at 2 MHz."""
        return self.xb_read(size, 8)

    def xb2_write(self, data):
        """Burst write timed for a 1571 at 2 MHz."""
        self.xb_write(data, 8)


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
    """Derived offsets and margins for one drive clock (v9 and burst)."""
    t = Timing(cyc=cyc)
    r = {"cyc_us": cyc}
    for k in ("sample", "send_margin", "drive", "recv_margin"):
        r[k + "_us"] = [round(float(x), 3) for x in getattr(t, k)]
    r["avr_cycles"] = t.avr_cycles()
    b = BurstTiming(round(16 * cyc))
    r["burst"] = {
        "sample": list(b.sample),
        "change": list(b.change),
        "period": [b.send_period, b.recv_period],
        "margin_us": [round(float(x), 3) for x in b.margin()],
        "margin_200ppm_us": [round(float(x), 3) for x in b.margin(200)],
    }
    return r


def main(argv=None):
    """Print the derived timing table."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cyc", type=float, nargs="*", default=[1.0, 0.5])
    for cyc in ap.parse_args(argv).cyc:
        print(report(cyc))


if __name__ == "__main__":
    main()
