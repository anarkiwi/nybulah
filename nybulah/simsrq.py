"""Timed co-simulation of SRQ fast serial (drive/proto_srq.inc) with xum1541 firmware v11.

The adapter times each drive -> host byte from its first SRQ fall and samples DATA
mid-window; towards the drive it clocks SRQ at a fixed bit and byte period. SrqTiming
derives the firmware's clocks (x.c SRQ_*); `python -m nybulah.simsrq` prints them.
"""

import argparse
import dataclasses
from functools import cached_property

from .opencbm import IEC_CLOCK, IEC_DATA, IEC_SRQ
from .sim import IdleDOSDrive
from .simx import AVR_FOUND, AVR_HZ, AVR_POLL, AVR_RISE, AVR_SYNC
from .simx import SimX, TimedBus, TimedDrive1571, XError

SRQ_U = 2  # cycles per CNT phase: timer A latch 1
SRQ_LAST = 15 * SRQ_U  # first fall of a byte to its last rise
SRQ_GMIN, SRQ_GMAX = 7, 14  # drive: last rise to the next byte's first fall
SRQ_SEND = 38  # drive cycles per sent byte
SRQ_RLOOP = 39  # drive receive loop: cycles between ICR polls once behind
SRQ_POLL = 5  # adapter clocks per in-burst SRQ poll
SRQ_ENCODE = 3  # adapter clocks to build a bit's port value
FIRMWARE = 11


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
        """Whether this adapter speaks protocol ("srq"/"s4" need firmware 11)."""
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

    def srq2_read(self, size):
        """SRQ read timed for a 1571 at 2 MHz."""
        return self.srq_read(size, 8)

    def srq2_write(self, data):
        """SRQ write timed for a 1571 at 2 MHz."""
        self.srq_write(data, 8)

    s4_read, s4_write = srq_read, srq_write


def make(cyc=1.0, rise=0.5, peers=0, dev=8, delay=0, **kw):
    """SimSRQ around a timed 1571 (CIA start delay in cycles) and idle DOS peers."""
    bus = TimedBus(rise)
    drive = TimedDrive1571(
        device=dev, bus=bus, cyc=cyc, read_jitter=kw.pop("read_jitter", 0.0)
    )
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
