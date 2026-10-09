import pytest

from nybulah.monitor import (
    CLOCK_HZ,
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
from nybulah.sim import (
    Bus,
    Drive1541,
    Drive1571,
    HostGone,
    SimCBM,
    SimIOWrite,
    SimTimeout,
)

TIMEOUT = CLOCK_HZ * WATCHDOG_S
ZP = bytes(range(0x11, 0x17))
VIA = (0x01, 0x42, 0x1234)


def armed(proto, cls=Drive1541):
    cbm = SimCBM(cls(device=10), dev=10)
    d = cbm.drive
    d.load(0x30, ZP)
    d.via1.acr, d.via1.ier, d.via1.latch = VIA
    mon = Monitor(cbm, 10, proto)
    mon.start()
    assert not d.via1.ier & 0x40 and d.via1.acr & 0xC0 == 0x40
    return cbm, mon


def assert_returned(d):
    assert d.halted and d.pb_out == 0
    assert d.dump(0x30, len(ZP)) == ZP
    assert d.via1.state() == VIA
    assert not d.via1.ifr() & 0x40


@pytest.mark.parametrize("proto", ["s1", "s2"])
@pytest.mark.parametrize(
    "op,after",
    [(lambda m: m.write(0x8000, b"abcd"), 2), (lambda m: m.read(0x8000, 4), 6)],
)
def test_host_vanishes_mid_byte(proto, op, after):
    cbm, mon = armed(proto)
    d = cbm.drive
    cbm.unplug(after)
    with pytest.raises(HostGone):
        op(mon)
    t0 = d.cycles
    cbm.settle()
    assert 0.9 * TIMEOUT <= d.cycles - t0 <= TIMEOUT + 200
    assert_returned(d)


@pytest.mark.parametrize("proto", ["s1", "s2"])
def test_progress_slower_than_timeout_survives(proto):
    cbm, mon = armed(proto)
    cbm.gap = int(0.7 * TIMEOUT)
    mon.write(0x8000, b"\x5a\xa5")
    cbm.gap = 0
    assert cbm.drive.cycles > 4 * TIMEOUT
    assert mon.read(0x8000, 2) == b"\x5a\xa5"
    mon.stop()
    cbm.settle()
    assert_returned(cbm.drive)


def test_idle_monitor_returns_and_context_recovers():
    cbm = SimCBM(Drive1571(device=10), dev=10)
    with pytest.raises(HandshakeTimeout, match="left the monitor") as e:
        with Monitor(cbm, 10, "s2") as mon:
            cbm.idle(int(1.1 * TIMEOUT))
            assert cbm.drive.halted
            mon.read(0x6000, 1)
    assert answers(e.value.recovered) and not mon.running
    assert cbm.bus.lines() == 0


@pytest.mark.parametrize("proto", ["s1", "s2"])
def test_stop_after_watchdog_exit_releases_host(proto):
    cbm, mon = armed(proto)
    cbm.idle(int(1.1 * TIMEOUT))
    assert_returned(cbm.drive)
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
    assert {"s1", "s2"} <= set(protocols())
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
