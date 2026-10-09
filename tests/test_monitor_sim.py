import os

import pytest

from nybulah.monitor import BASE, Monitor
from nybulah.sim import IdleDOSDrive
from nybulah.simx import adapter

ALL = ["s1", "s2", "s3"]


def make_cbm(idle_peer=False, proto="s1"):
    cbm = adapter(proto, device=10)
    if idle_peer:
        IdleDOSDrive(cbm.bus)
    return cbm


@pytest.mark.parametrize("proto", ALL)
def test_round_trip(proto):
    cbm = make_cbm(proto=proto)
    data = os.urandom(300)
    with Monitor(cbm, 10, proto) as mon:
        mon.write(0x8000, data)
        assert mon.read(0x8000, len(data)) == data
        assert mon.read(0x8100, 4) == data[0x100:0x104]
    cbm.settle()
    assert cbm.drive.halted
    assert cbm.bus.lines() == 0


@pytest.mark.parametrize("proto", ALL)
def test_jsr_returns_registers(proto):
    cbm = make_cbm(proto=proto)
    # lda #$12; ldx #$34; ldy #$56; rts
    with Monitor(cbm, 10, proto) as mon:
        mon.write(0x8100, bytes([0xA9, 0x12, 0xA2, 0x34, 0xA0, 0x56, 0x60]))
        assert mon.jsr(0x8100) == (0x12, 0x34, 0x56)
        assert mon.read(0x8100, 1) == b"\xa9"


@pytest.mark.parametrize("proto", ALL)
def test_zero_page_restored(proto):
    cbm = make_cbm(proto=proto)
    saved = bytes(range(0x11, 0x17))
    cbm.drive.load(0x30, saved)
    with Monitor(cbm, 10, proto) as mon:
        mon.read(BASE, 2)
    cbm.settle()
    assert cbm.drive.dump(0x30, len(saved)) == saved


@pytest.mark.parametrize("proto", ["s1", "s3"])
def test_tolerates_idle_drive_on_bus(proto):
    cbm = make_cbm(idle_peer=True, proto=proto)
    with Monitor(cbm, 10, proto) as mon:
        mon.write(0x8000, b"\x00\xff\x5a")
        assert mon.read(0x8000, 3) == b"\x00\xff\x5a"


def test_s2_blocked_by_idle_drive_on_bus():
    from nybulah.monitor import HandshakeTimeout

    cbm = make_cbm(idle_peer=True)
    with pytest.raises(HandshakeTimeout, match="drive tracks ATN"):
        Monitor(cbm, 10, "s2", timeout=0.05).start()


def test_rejects_unknown_protocol():
    with pytest.raises(ValueError):
        Monitor(make_cbm(), 10, "pp")


def test_bench_runs_on_sim():
    from nybulah import bench

    out = bench.run(make_cbm(), 10, 0x8000, 64, 2, "s1")
    assert out["errors"] == 0 and out["read"]["bytes"] == 128


def test_handshake_timeout_reports_step():
    from nybulah.monitor import HandshakeTimeout

    cbm = make_cbm()
    mon = Monitor(cbm, 10, "s1", code=bytes([0x60]), timeout=0.05)
    with pytest.raises(HandshakeTimeout, match="drive ready"):
        mon.start()
