"""Timed co-simulation of SRQ fast serial (drive/proto_srq.inc) with xum1541 firmware v11.

The adapter times each drive -> host byte from its first SRQ fall and samples DATA
mid-window; towards the drive it clocks SRQ at a fixed bit and byte period. SrqTiming
derives the firmware's clocks (x.c SRQ_*); `python -m nybulah.simsrq` prints them.
"""

import argparse
import dataclasses
from functools import cached_property

from . import stream as fmt
from .opencbm import IEC_ATN, IEC_CLOCK, IEC_DATA, IEC_SRQ
from .sim import IdleDOSDrive, IdleFastDrive
from .simx import AVR_FOUND, AVR_HZ, AVR_POLL, AVR_RISE, AVR_SYNC
from .simx import MODELS, SimX, TimedBus, XError

SRQ_U = 2  # cycles per CNT phase: timer A latch 1
SRQ_LAST = 15 * SRQ_U  # first fall of a byte to its last rise
SRQ_GMIN, SRQ_GMAX = 7, 14  # drive: last rise to the next byte's first fall
CIA_FLAG_MAX = 39  # latest ICR flag after an SDR write on a 1571: 45 cycles/byte at v11
SRQ_SEND = CIA_FLAG_MAX + 1  # drive cycles per sent byte (proto_srq.inc SR_PERIOD)
SRQ_RLOOP = 39  # drive receive loop: cycles between ICR polls once behind
SRQ_POLL = 5  # adapter clocks per in-burst SRQ poll
SRQ_ENCODE = 3  # adapter clocks to build a bit's port value
FIRMWARE = 11
STREAM_FIRMWARE = 12
SR_PERIOD = SRQ_SEND  # drive/stream.s: cycles between SDR writes
USB_BANK = 32
# A 32-byte full-speed bulk IN transaction: token, data (SYNC PID data CRC16 EOP)
# and handshake packets plus two turnarounds of 7.5 bit times, at 12 Mbit/s.
USB_PACKET_US = (35 + (8 + 8 + 8 * USB_BANK + 16 + 3) + 19 + 15) / 12
STREAM_TIMEOUT_US = 20_000.0  # no SRQ fall: drive gone (metadata every <256 cycles)
# ATN hold (x.c stream_stop): the drive's longest wait between ATN checks, 256
# bytes of 8 cells at zone 0 and 285 rpm, then until SRQ stays released this long.
STREAM_ATN_MIN_US = 256 * 8 * 4.0 * 300 / 285
STREAM_QUIET_US = 1_000.0
STREAM_ATN_US = 50_000.0  # longest ATN hold


@dataclasses.dataclass(frozen=True)
class SrqTiming:
    """Firmware v11 SRQ clocks (16 per us) at f clocks per drive cycle.

    Reads centre each sample and the next byte's first poll in their windows; writes
    give the DATA set-up and hold sides of every bit the tightest 2 MHz read slack
    (sigma), with the byte period no shorter than the drive's receive loop.
    """

    f: int = 16
    rise: int = AVR_RISE
    poll: int = AVR_POLL
    sync: int = AVR_SYNC

    def _sample(self, a, b):
        return ((a + b) * self.f + self.rise - self.poll) // 2

    @cached_property
    def sample(self):
        """Read: sample clocks of bits 7..0 from the detecting poll."""
        return tuple(self._sample(2 * j * SRQ_U, (2 * j + 2) * SRQ_U) for j in range(8))

    @property
    def frame(self):
        """Stream (x_timing.h SRQ_FRAME, SRQ_WAIT): earliest clock of the poll that must
        see SRQ released, latest of the first poll for the next fall."""
        return SRQ_LAST * self.f + self.rise, (SR_PERIOD - 1) * self.f - self.poll

    @property
    def start(self):
        """Read: first poll for the next byte's fall."""
        return self._sample(SRQ_LAST, SRQ_LAST + SRQ_GMIN)

    @property
    def polls(self):
        """Read: polls before giving up on the next byte (one byte beyond the latest)."""
        return (
            (SRQ_LAST + SRQ_GMAX + 16 * SRQ_U) * self.f - self.start
        ) // SRQ_POLL + 2

    @property
    def sigma(self):
        """Slack each side of the 2 MHz read windows, the write design slack."""
        return (2 * SRQ_U * 8 - self.rise - self.poll) // 2

    @property
    def bit(self):
        """Write: bit period."""
        floor = -(-((SRQ_RLOOP + 1) * self.f + self.rise + self.sigma) // 8)
        return max(2 * (self.rise + self.sigma) + self.f, floor)

    @property
    def low(self):
        """Write: SRQ low per bit."""
        return (self.bit - self.f) // 2

    @property
    def period(self):
        """Write: byte period."""
        return 8 * self.bit

    @property
    def first(self):
        """Write: first SRQ assertion after the detecting poll."""
        return AVR_FOUND + SRQ_ENCODE

    def changes(self, burst, flip=lambda k, i: 0):
        """(clock, lines) of a host -> drive burst; flip(bit, byte) xors lines."""
        out = []
        for i, b in enumerate(burst):
            for k in range(8):
                data = (0 if b >> 7 - k & 1 else IEC_DATA) ^ flip(k, i)
                at = self.first + i * self.period + k * self.bit
                out += [(at, IEC_SRQ | data), (at + self.low, data)]
        return out + [(out[-1][0] + self.bit - self.low, 0)] if out else out

    def read_slack(self, ppm=0.0):
        """Per sample, then the next byte's first poll: (left, right) slack in us."""
        e, c = abs(ppm) * 1e-6, self.f
        windows = [(4 * j, 4 * j + 4, s) for j, s in enumerate(self.sample)]
        windows.append((SRQ_LAST, SRQ_LAST + SRQ_GMIN, self.start))
        return tuple(
            ((s - a * c - self.rise - s * e) / 16, (b * c - s - self.poll - s * e) / 16)
            for a, b, s in windows
        )

    def write_slack(self):
        """(set-up, hold) slack of every bit and the drive loop's slack, in us."""
        hold = self.bit - self.low - self.rise - self.f
        loop = self.period - (SRQ_RLOOP + 1) * self.f - self.rise
        return (self.low - self.rise) / 16, hold / 16, loop / 16

    def margin(self, ppm=0.0):
        """Smallest (read, write) slack in us."""
        return min(min(p) for p in self.read_slack(ppm)), min(self.write_slack())

    def rate(self):
        """(read, write) bytes/s within a burst."""
        return AVR_HZ / (SRQ_SEND * self.f), AVR_HZ / self.period


class SimSRQ(SimX):
    """SimX plus the firmware v11 SRQ transfers (srq_*, srq2_* at 2 MHz); data_skew
    (us) delays the adapter's DATA changes against its SRQ changes."""

    def __init__(self, drive, dev=8, timing=None, seed=0, **kw):
        kw.setdefault("firmware", FIRMWARE)
        super().__init__(drive, dev, timing, seed, **kw)
        self.data_skew = 0.0

    def _split(self, t, changes):
        """(time, lines) host writes for changes from detection t, DATA skewed."""
        events = sorted(
            (
                self._clock(t, clock) + (self.data_skew if line == IEC_DATA else 0),
                line,
                v,
            )
            for clock, v in changes
            for line in (IEC_SRQ, IEC_DATA)
        )
        lines, out = self.bus.host_lines, []
        for at, line, v in events:
            lines = lines & ~line | v & line
            out.append((at, lines))
        return out

    def supports(self, protocol):
        """Whether this adapter speaks protocol ("srq"/"s4" need firmware 11,
        "stream" 12)."""
        if protocol == "stream":
            return self.firmware >= STREAM_FIRMWARE
        if protocol in ("srq", "s4"):
            return self.firmware >= FIRMWARE
        return super().supports(protocol)

    def _srq_timing(self, f):
        return SrqTiming(f or round(16 * self.timing.cyc))

    def _next_fall(self, t, st):
        """Detection of the next byte's first fall after the byte detected at t."""
        p = SRQ_POLL / 16
        start = self._clock(t, st.start)
        deadline = start + 4 / 16 + (st.polls - 2) * p
        high = self._poll_until(IEC_SRQ, False, start, deadline, p, 0.0)
        if high is None:
            return None
        return self._poll_until(IEC_SRQ, True, high + 4 / 16, deadline, p, 0.0)

    def srq_read(self, size, f=None):
        """Drive -> host SRQ transfer of size bytes."""
        st = self._srq_timing(f)

        def burst(t, n, first, out):
            for i in range(n):
                if i and (t := self._next_fall(t, st)) is None:
                    raise self._fail(XError("SRQ byte missing"), out)
                b = 0
                for j, off in enumerate(st.sample):
                    at = self._clock(t, off) - st.sync / 16
                    self._advance(at)
                    level = self.bus.level(at) ^ self._flip(j, first + i)
                    b |= (0 if level & IEC_DATA else 1) << 7 - j
                out.append(b)
            return at  # pylint: disable=undefined-loop-variable

        return self._read_bursts(size, burst, IEC_CLOCK, IEC_SRQ)

    def srq_write(self, data, f=None):
        """Host -> drive SRQ transfer."""
        st = self._srq_timing(f)
        self._write_bursts(
            data,
            lambda t, burst, first: self._split(
                t, st.changes(burst, lambda k, i: self._flip(k, first + i))
            ),
        )

    def srq2_stream(self, size, packet_us=USB_PACKET_US, frame=None):
        """Firmware v12 streaming receive (2 MHz): adapter output, at most size bytes.

        The host drains one 32-byte packet per packet_us; frame is the clock of the
        poll after each byte that must see SRQ high (default: the latest allowed).
        """
        if self.firmware < STREAM_FIRMWARE:
            raise XError("firmware lacks streaming")
        st = self._srq_timing(8)
        frame = st.frame[1] if frame is None else frame
        usb = UsbIn(size, packet_us)
        try:
            t = self._sync(1, IEC_CLOCK, IEC_SRQ)
        except XError as e:
            raise self._fail(e, b"")
        self._host(0, t + (AVR_FOUND + 2) / 16)
        self._go = False
        self.count["bursts"] += 1
        while True:
            b, meta = 0, False
            for j, off in enumerate(st.sample):
                at = self._clock(t, off) - st.sync / 16
                self._advance(at)
                level = self.bus.level(at)
                b |= (0 if level & IEC_DATA else 1) << 7 - j
                meta |= j == 0 and bool(level & IEC_CLOCK)
            code = usb.put(t, b, meta)
            if code is None and meta and b in fmt.DRIVE_END:
                code = fmt.A_DONE
            at = self._clock(t, frame)
            self._advance(at)
            if code is None and self.bus.level(at) & IEC_SRQ:
                code = fmt.A_FRAMING
            if code is None:
                t = self._poll_until(
                    IEC_SRQ, True, at, at + STREAM_TIMEOUT_US, st.poll / 16, 0.0
                )
                if t is None:
                    code, at = fmt.A_TIMEOUT, at + STREAM_TIMEOUT_US
            if code is not None:
                break
        self.now = at
        if code != fmt.A_DONE:
            self._stop_drive()
        return usb.close(code)

    def _stop_drive(self):
        """Hold ATN STREAM_ATN_MIN_US, then until SRQ stays released STREAM_QUIET_US
        (at most STREAM_ATN_US), then release it."""
        t0 = self.now
        self._host(IEC_ATN, t0)
        last = t0 + STREAM_ATN_MIN_US - STREAM_QUIET_US
        self.now = max(self.now, last)
        while self.now - last < STREAM_QUIET_US and self.now - t0 < STREAM_ATN_US:
            nxt = self._poll_until(
                IEC_SRQ, True, self.now, last + STREAM_QUIET_US, SRQ_POLL / 16, 0.0
            )
            if nxt is None:
                self.now = last + STREAM_QUIET_US
                break
            last = self.now = nxt + 1.0
        self._host(0, self.now)
        self.count["atn_stops"] += 1

    def srq2_read(self, size):
        """SRQ read timed for a 1571 at 2 MHz."""
        return self.srq_read(size, 8)

    def srq2_write(self, data):
        """SRQ write timed for a 1571 at 2 MHz."""
        self.srq_write(data, 8)

    s4_read, s4_write = srq_read, srq_write


class UsbIn:
    """The adapter's double-banked IN endpoint with a host draining one packet per
    packet_us after hand-off; a bank still waiting when needed is an overrun."""

    def __init__(self, size, packet_us):
        self.size, self.packet_us = size, packet_us
        self.out = bytearray()
        self.flight = []
        self.stopped = None

    def _byte(self, t, b):
        if len(self.out) % USB_BANK == 0:
            self.flight = [d for d in self.flight if d > t]
            if len(self.flight) >= 2:
                return fmt.A_OVERRUN
        self.out.append(b)
        if len(self.out) % USB_BANK == 0:
            last = self.flight[-1] if self.flight else t
            self.flight.append(max(t, last) + self.packet_us)
        return None

    def put(self, t, b, meta):
        """Store a received byte (escaped) at time t; an adapter code stops it."""
        pair = meta or b == fmt.ESC
        if len(self.out) + 2 + pair > self.size - 2:
            return fmt.A_TRUNCATED
        for x in ((fmt.ESC, b) if pair else (b,)):
            code = self._byte(t, x)
            if code is not None:
                return code
        return None

    def close(self, code):
        """Output with the trailer; a dangling ESC takes the code as its second byte."""
        if len(self.out) % USB_BANK and self.out[-1] == fmt.ESC and self._open_esc():
            self.out.append(code)
        else:
            self.out += bytes((fmt.ESC, code))
        return bytes(self.out)

    def _open_esc(self):
        i = len(self.out)
        while i and self.out[i - 1] == fmt.ESC:
            i -= 1
        return (len(self.out) - i) % 2 == 1


def make(cyc=1.0, rise=0.5, peers=0, dev=8, delay=0, **kw):
    """SimSRQ around a timed 1571 (model="1581": a 1581 at 2 MHz; CIA start delay in
    cycles) and idle DOS peers (fast_peers: IdleFastDrive)."""
    bus = TimedBus(rise)
    model = kw.pop("model", "1571")
    cyc = 0.5 if model == "1581" else cyc
    drive = MODELS[model](
        device=dev, bus=bus, cyc=cyc, read_jitter=kw.pop("read_jitter", 0.0)
    )
    for _ in range(kw.pop("fast_peers", 0)):
        IdleFastDrive(bus)
    drive.cia.delay = delay
    for _ in range(peers):
        IdleDOSDrive(bus)
    return SimSRQ(drive, dev, **kw)


def report(f):
    """Derived clocks, slack and rates at f clocks per drive cycle."""
    t = SrqTiming(f)
    return {
        "f": f,
        "sample": list(t.sample),
        "start": t.start,
        "polls": t.polls,
        "bit": [t.bit, t.low, t.bit - t.low],
        "period": t.period,
        "margin_us": [round(x, 3) for x in t.margin()],
        "margin_200ppm_us": [round(x, 3) for x in t.margin(200)],
        "rate_bps": [round(x) for x in t.rate()],
    }


def main(argv=None):
    """Print the derived SRQ timing."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--f", type=int, nargs="*", default=[16, 8])
    for f in ap.parse_args(argv).f:
        print(report(f))


if __name__ == "__main__":
    main()
