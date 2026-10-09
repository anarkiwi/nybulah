import pytest

from nybulah.monitor import (
    BASE,
    CLOCK_HZ,
    WATCHDOG_IDLE_S,
    WATCHDOG_S,
    BusNotIdle,
    DriveUnresponsive,
    HandshakeTimeout,
    Monitor,
    answers,
    protocols,
    recover,
)
from nybulah.opencbm import IEC_DATA
from nybulah.simx import adapter
from nybulah.sim import Bus, Drive1541, Drive1571, HostGone, SimIOWrite, SimTimeout
from nybulah.simhost import SimCBM

TIMEOUT = CLOCK_HZ * WATCHDOG_S
IDLE = CLOCK_HZ * WATCHDOG_IDLE_S
ZP = bytes(range(0x11, 0x17))
VIA = (0x01, 0x42, 0x1234)


def armed(proto, cls=Drive1541, **kw):
    cbm = adapter(proto, cls, device=10)
    d = cbm.drive
    d.load(0x30, ZP)
    d.via1.acr, d.via1.ier, d.via1.latch = VIA
    mon = Monitor(cbm, 10, proto, **kw)
    mon.start()
    assert not d.via1.ier & 0x40 and d.via1.acr & 0xC0 == 0x40
    return cbm, mon


def assert_returned(d):
    assert d.halted and d.pb_out == 0
    assert d.dump(0x30, len(ZP)) == ZP
    assert d.via1.state() == VIA
    assert not d.via1.ifr() & 0x40


@pytest.mark.parametrize("proto", ["s1", "s2", "s3"])
@pytest.mark.parametrize(
    "op,after",
    [(lambda m: m.write(0x8000, b"abcd"), 2), (lambda m: m.read(0x8000, 4), 6)],
)
def test_host_vanishes_mid_byte(proto, op, after):
    cbm, mon = armed(proto)
    d = cbm.drive
    # burst X: a 5-byte command burst is atomic, so vanish in the data phase
    cbm.unplug(max(after, 6) if proto == "s3" else after)
    with pytest.raises(HostGone):
        op(mon)
    t0 = d.cycles
    cbm.settle()
    assert 0.9 * TIMEOUT <= d.cycles - t0 <= TIMEOUT + 200
    assert_returned(d)


@pytest.mark.parametrize("proto", ["s1", "s2", "s3"])
def test_progress_slower_than_timeout_survives(proto):
    cbm, mon = armed(proto)
    cbm.gap = int(0.7 * TIMEOUT)
    mon.write(0x8000, b"\x5a\xa5")
    cbm.gap = 0
    assert cbm.drive.cycles > 2 * TIMEOUT
    assert mon.read(0x8000, 2) == b"\x5a\xa5"
    mon.stop()
    cbm.settle()
    assert_returned(cbm.drive)


class Clock:
    """Host clock advanced by hand."""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


@pytest.mark.parametrize("proto", ["s1", "s2", "s3"])
def test_idle_window_between_commands(proto):
    clock = Clock()
    cbm, mon = armed(proto, clock=clock)
    d = cbm.drive
    mon.write(0x8000, b"\x42")
    t0 = d.cycles
    cbm.idle(int(0.98 * IDLE))
    assert not d.halted and mon.alive()
    cbm.settle()
    assert 0.99 * IDLE <= d.cycles - t0 <= IDLE + 200
    assert_returned(d)
    clock.t += 1.1 * WATCHDOG_IDLE_S
    assert mon.read(0x8000, 1) == b"\x42" and not d.halted
    mon.stop()
    cbm.settle()
    assert_returned(d)


def test_long_pause_restarts_live_monitor():
    clock = Clock()
    cbm, mon = armed("s1", clock=clock)
    uploads, upload = [], cbm.upload
    cbm.upload = lambda dev, addr, data: (uploads.append(addr), upload(dev, addr, data))
    mon.write(0x8000, b"\x17")
    clock.t += mon.idle_s + 0.01
    assert mon.read(0x8000, 1) == b"\x17" and uploads == [BASE]
    assert mon.running and not cbm.drive.halted


@pytest.mark.parametrize("proto", ["s2", "s3"])
def test_drive_left_inside_window_recovers(proto):
    cbm = adapter(proto, Drive1571, device=10)
    with pytest.raises(HandshakeTimeout, match="left the monitor") as e:
        with Monitor(cbm, 10, proto) as mon:
            cbm.drive.reset()
            mon.read(0x6000, 1)
    assert answers(e.value.recovered) and not mon.running
    assert cbm.bus.lines() == 0


@pytest.mark.parametrize("proto", ["s1", "s2", "s3"])
def test_stop_after_drive_exit_releases_host(proto):
    cbm, mon = armed(proto)
    cbm.drive.reset()
    mon.stop()
    assert cbm.bus.lines() == 0 and not mon.running


class StuckData:
    """A device holding DATA forever."""

    def __init__(self, bus):
        bus.devices.append(self)

    @staticmethod
    def drive_lines():
        return IEC_DATA


def test_start_rejects_busy_bus_and_recovers():
    bus = Bus()
    cbm = SimCBM(Drive1541(device=10, bus=bus), dev=10)
    StuckData(bus)
    with pytest.raises(BusNotIdle, match="not idle") as e:
        with Monitor(cbm, 10, "s1", timeout=0.05):
            pass
    assert answers(e.value.recovered) and cbm.drive.halted


def test_recover_retries_reset():
    cbm = SimCBM(Drive1541(device=10, resets_to_boot=2), dev=10)
    Monitor(cbm, 10, "s1").start()
    assert cbm.status(10).startswith("99")
    assert recover(cbm, 10, timeout=0.01, poll=0).startswith("73,CBM DOS V2.6")
    wedged = SimCBM(Drive1541(device=9, resets_to_boot=3), dev=9)
    Monitor(wedged, 9, "s1").start()
    with pytest.raises(DriveUnresponsive):
        recover(wedged, 9, timeout=0.01, poll=0)


def test_answers():
    assert answers("00, OK,00,00") and not answers("99, DRIVER ERROR,00,00")
    assert not answers("") and not answers(None)


def test_protocols_and_unsupported():
    assert {"s1", "s2", "s3"} <= set(protocols())
    with pytest.raises(ValueError):
        Monitor(SimCBM(), 8, "s3")


def test_sim_rejects_io_and_mirror_writes():
    d41, d71 = Drive1541(), Drive1571(expansion=())
    for d, addr in ((d41, 0x1810), (d41, 0x1C00), (d41, 0x0800), (d71, 0x2000)):
        with pytest.raises(SimIOWrite):
            d.write(addr, 0)
    d41.write(0xC000, 1)
    d71.write(0x7000, 1)
    assert d71.read(0x7000) == 0x70
    with pytest.raises(SimTimeout):
        SimCBM(d41).iec_wait(IEC_DATA, 1)


@pytest.mark.parametrize("freerun", [True, False])
def test_via_timer1_modes(freerun):
    d = Drive1541()
    via = d.via1
    via.write(0xB, 0x40 if freerun else 0)
    via.write(4, 98)
    via.write(5, 0)
    d.cycles = 98
    assert not via.ifr() & 0x40
    d.cycles = 300
    assert via.read(0xD) & 0x40
    via.write(0xE, 0xC0)
    assert via.read(0xD) & 0x80 and via.read(0xE) == 0xC0
    via.read(4)
    assert not via.ifr() & 0x40
    d.cycles = 400
    assert bool(via.ifr() & 0x40) == freerun
    via.write(0xD, 0x40)
    assert not via.ifr() & 0x40
    via.write(7, 0x12)
    assert via.read(7) == 0x12 and via.read(6) == 98
    via.write(0xE, 0x40)
    via.write(3, 0xFF)
    assert via.ier == 0 and via.read(3) == 0xFF
