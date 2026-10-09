import struct

import pytest
from xsim import FIRMWARE, SPARE, assert_returned, rand, session, steady_cycles, trace

from nybulah import simx
from nybulah.fastx import CHUNK, ChecksumError, XBLink, xsum
from nybulah.monitor import CLOCK_HZ, WATCHDOG_S, Monitor
from nybulah.opencbm import IEC_CLOCK, IEC_DATA, OpenCBMError
from nybulah.sim import HostGone

MODELS = [("1541", 1.0, 0x8000), ("1571", 0.5, 0x6000)]
DEVICES = [(9, 9), (10, 8), (10, 9), (10, 10), (10, 11)]


def reference_sum(data):
    s1 = s2 = 0
    for b in data:
        t = s1 + b
        s1, c = t & 0xFF, t >> 8
        s2 = (s2 + s1 + c) & 0xFF
    return s1, s2


@pytest.mark.parametrize("n", [0, 1, 255, 256, 1000])
def test_xsum_matches_6502_semantics(n):
    data = rand(n, n)
    assert xsum(data) == reference_sum(data)


@pytest.mark.parametrize("model,cyc,base", MODELS)
@pytest.mark.parametrize("peers", [0, 2])
@pytest.mark.parametrize("fw,dev", DEVICES)
def test_block_round_trip(model, cyc, base, peers, fw, dev):
    cbm, mon = session(model, cyc, peers, fw=fw, dev=dev)
    assert isinstance(mon.link, XBLink) == (fw >= 10)
    data = rand(0x1A5, peers + dev)
    mon.write(base + 0xF3, data)
    assert cbm.drive.dump(base + 0xF3, len(data)) == data
    assert mon.read(base + 0xF3, len(data)) == data
    assert mon.read(base, 0) == b""
    mon.write(base, b"")
    assert mon.link.rejects == cbm.retracts == 0
    mon.write(SPARE, bytes([0xA9, 0x12, 0xA2, 0x34, 0xA0, 0x56, 0x60]))
    assert mon.jsr(SPARE) == (0x12, 0x34, 0x56)
    mon.stop()
    cbm.settle()
    assert_returned(cbm)
    assert not cbm.bus.lines() & (IEC_DATA | IEC_CLOCK)


def test_reads_span_chunks():
    cbm, mon = session()
    data = rand(CHUNK + 17, 5)
    cbm.drive.load(0x8000, data)
    assert mon.read(0x8000, len(data)) == data


@pytest.mark.parametrize("model,cyc,base", MODELS)
def test_monitor_byte_commands(model, cyc, base):
    _, mon = session(model, cyc)
    mon.link.tx(b"W" + struct.pack("<HH", base, 3) + b"\x00\xff\x5a")
    mon.link.tx(b"R" + struct.pack("<HH", base, 3))
    assert mon.link.rx(3) == b"\x00\xff\x5a"


@pytest.mark.parametrize("model,cyc,base", MODELS)
def test_drive_schedule_matches_timing_table(model, cyc, base):
    cbm, mon = session(model, cyc)
    data = rand(8)
    cbm.drive.load(base, data)
    writes = [f[0] for f in trace(cbm, lambda: mon.read(base, 8)) if f[0][3:]]
    assert len(writes) >= 8
    for w in writes:
        assert [round(t / cyc) for t in w[:4]] == list(simx.SEND_SCHEDULE[1:5])
        assert round(w[4] / cyc) >= simx.SEND_SCHEDULE[5]
    reads = [f for f in trace(cbm, lambda: mon.write(base, data)) if not f[0][1:]]
    assert len(reads) >= len(data) + 12
    for ws, rs in reads:
        assert [round(t / cyc) for t in ws] == [simx.RECV_SCHEDULE[1]]
        assert [round(t / cyc) for t in rs[:4]] == list(simx.RECV_SCHEDULE[2:])


def test_block_throughput():
    cbm, mon = session()
    rd = steady_cycles(cbm, lambda n: mon.read(0x8000, n), 256)
    wr = steady_cycles(cbm, lambda n: mon.write(0x8000, bytes(n)), 256)
    assert 107 <= rd <= 109.5
    assert 120 <= wr <= 122.5


def skewed(cbm, mon, op, base, skew):
    data = rand(64, 7)
    cbm.drive.load(base, bytes(64))
    if op == "read":
        cbm.drive.load(base, data)
        mon.link.tx(mon.link.params(base, 64, mon.link.xread))
        cbm.skew = skew
        got = mon.link.rx(67)
        cbm.skew = 0.0
        return got[:64] == data
    mon.link.tx(mon.link.params(base, 64, mon.link.xwrite))
    cbm.skew = skew
    mon.link.tx(data)
    cbm.skew = 0.0
    mon.link.rx(3)
    return cbm.drive.dump(base, 64) == data


@pytest.mark.parametrize("model,cyc,base", MODELS)
@pytest.mark.parametrize("op", ["read", "write"])
@pytest.mark.parametrize("seed", range(3))
def test_margins_hold_with_worst_rise_and_jitter(model, cyc, base, op, seed):
    t = simx.Timing(cyc=cyc)
    cbm, mon = session(model, cyc, seed=seed, read_jitter=t.v)
    margin = min(t.send_margin if op == "read" else t.recv_margin)
    for skew in (-0.95 * margin, 0.0, 0.95 * margin):
        assert skewed(cbm, mon, op, base, skew)


@pytest.mark.parametrize("model,cyc,base", MODELS)
@pytest.mark.parametrize("op", ["read", "write"])
def test_violating_margins_corrupts(model, cyc, base, op):
    t = simx.Timing(cyc=cyc)
    cbm, mon = session(model, cyc)
    margin = max(t.send_margin if op == "read" else t.recv_margin)
    beyond = margin + t.poll + 2 * t.v + t.rise
    assert skewed(cbm, mon, op, base, 0.0)
    assert not skewed(cbm, mon, op, base, beyond)
    assert not skewed(cbm, mon, op, base, -beyond)


@pytest.mark.parametrize("op", ["read", "write"])
@pytest.mark.parametrize("fw", FIRMWARE)
def test_corrupt_pair_is_retried(op, fw):
    cbm, mon = session(fw=fw)
    data = rand(100, 3)
    cbm.drive.load(0x8000, data)
    cbm.faults[cbm.ordinal + 20] = (2, IEC_DATA)
    if op == "read":
        assert mon.read(0x8000, 100) == data
    else:
        mon.write(0x8100, data)
        assert cbm.drive.dump(0x8100, 100) == data
    assert mon.link.rejects == 1


@pytest.mark.parametrize("fw", FIRMWARE)
def test_persistent_corruption_raises(fw):
    _, mon = session(retries=1, fw=fw)
    read = mon.link.rx

    def flipped(n):
        got = read(n)
        return bytes([got[0] ^ (n > 3)]) + got[1:]

    mon.link.rx = flipped
    with pytest.raises(ChecksumError):
        mon.read(0x8000, 16)
    assert mon.link.rejects == 2


@pytest.mark.parametrize("fw", FIRMWARE)
def test_backpressure_and_slow_drive(fw):
    cbm, mon = session(slice_us=300.0, fw=fw)
    data = rand(200, 9)
    cbm.drive.load(0x8000, data)
    cbm.pause[cbm.ordinal + 70] = 5000.0
    assert mon.read(0x8000, 200) == data
    # dex/bne nested delay of ~5 ms, then rts
    loop = bytes([0xA0, 0x04, 0xA2, 0x00, 0xCA, 0xD0, 0xFD, 0x88, 0xD0, 0xF8, 0x60])
    mon.write(SPARE, loop)
    assert mon.jsr(SPARE)[1:] == (0, 0)
    assert cbm.retracts > 0


@pytest.mark.parametrize("fw", FIRMWARE)
def test_adapter_timeout_when_drive_gone(fw):
    cbm, mon = session(timeout_us=2_000.0, slice_us=500.0, fw=fw)
    speed = mon.link.speeds[False]
    mon.stop()
    cbm.settle()
    with pytest.raises(simx.XTimeout) as e:
        getattr(cbm, f"{speed}_read")(4)
    assert e.value.partial == b""
    with pytest.raises(simx.XTimeout):
        getattr(cbm, f"{speed}_write")(b"Q")
    assert not cbm.bus.host_lines


@pytest.mark.parametrize("op", ["read", "write"])
def test_host_vanishes_drive_times_out(op):
    cbm, mon = session()
    cbm.vanish_at = cbm.ordinal + 30
    with pytest.raises(HostGone):
        if op == "read":
            mon.link.tx(mon.link.params(0x8000, 200, mon.link.xread))
            mon.link.rx(203)
        else:
            mon.link.tx(mon.link.params(0x8000, 200, mon.link.xwrite))
            mon.link.tx(bytes(200))
    t0 = cbm.drive.cycles
    cbm.settle()
    assert cbm.drive.cycles - t0 <= CLOCK_HZ * WATCHDOG_S + 500
    assert_returned(cbm)


@pytest.mark.parametrize("fw", FIRMWARE)
def test_transport_error_is_recoverable(fw):
    cbm, mon = session(fw=fw)
    data = rand(150, 4)
    cbm.drive.load(0x8000, data)
    cbm.vanish_at = cbm.ordinal + 70
    with pytest.raises(HostGone):
        mon.read(0x8000, 150)
    mon.recover()
    mon.start()
    assert mon.read(0x8000, 150) == data


@pytest.mark.parametrize("fw", FIRMWARE)
def test_context_manager_recovers_on_error(fw):
    cbm, mon = session(fw=fw)
    mon.stop()
    cbm.drive.load(SPARE, b"\x60")
    with pytest.raises(OpenCBMError) as e:
        with Monitor(cbm, 9, "s3") as m:
            cbm.vanish_at = cbm.ordinal + (4 if fw < 10 else 6)  # first reply byte
            m.jsr(SPARE)
    assert e.value.recovered.startswith("73,")
    assert cbm.drive.halted and not m.running


@pytest.mark.parametrize("fw", FIRMWARE)
def test_rejects_code_without_tag(fw):
    with pytest.raises(ValueError, match="tag"):
        Monitor(simx.make(firmware=fw), 8, "s3", code=b"\x60")


def test_make_rejects_unknown_option():
    with pytest.raises(TypeError):
        simx.make(bogus=1)


def test_timing_report(capsys):
    simx.main(["--cyc", "1.0"])
    out = capsys.readouterr().out
    r = simx.report(1.0)
    assert str(r["sample_us"]) in out
    assert min(r["send_margin_us"] + r["recv_margin_us"]) > 3
    assert min(simx.report(0.5)["recv_margin_us"]) > 1
    assert simx.Timing().avr_cycles() == {
        "sample": [309, 485, 661, 837],
        "drive": [4, 252, 428, 604, 752],
    }
    assert simx.Timing(cyc=0.5).avr_cycles() == {
        "sample": [157, 245, 333, 421],
        "drive": [4, 120, 208, 296, 376],
    }
    assert (
        simx.encode(0xA5, simx.SEND_PAIRS)
        and simx.decode(simx.encode(0xA5, simx.RECV_PAIRS), simx.RECV_PAIRS) == 0xA5
    )
    assert simx.TimedBus().settles(0.0) == float("inf")
    assert IEC_CLOCK


@pytest.mark.parametrize("fw", FIRMWARE)
def test_1571_switches_to_2mhz_and_back(fw):
    cbm, mon = session("1571", 1.0, fw=fw)
    idle = mon.idle_s
    mon.set_fast(True)
    assert cbm.drive.cyc == 0.5 and mon.idle_s == idle / 2
    data = rand(0x300, 7)
    mon.write(0x6000, data)
    assert mon.read(0x6000, len(data)) == data
    assert mon.link.rejects == 0
    mon.set_fast(True)
    mon.stop()
    cbm.settle()
    assert cbm.drive.cyc == 1.0
    assert_returned(cbm)


@pytest.mark.parametrize("fw", FIRMWARE)
def test_2mhz_needs_the_2mhz_adapter_timing(fw):
    cbm, mon = session("1571", 1.0, fw=fw)
    mon.set_fast(True)
    speed = mon.link.speeds[False]
    mon.link.rx = getattr(cbm, f"{speed}_read")
    mon.link.tx = getattr(cbm, f"{speed}_write")
    mon.link.retries = 0
    with pytest.raises((ChecksumError, OpenCBMError)):
        mon.write(0x6000, rand(64))
        mon.read(0x6000, 64)


def test_clock_switch_is_s3_only():
    cbm = simx.make("1571", 1.0, dev=9)
    mon = Monitor(cbm, 9, "s1")
    with pytest.raises(ValueError, match="cannot change the drive clock"):
        mon.set_fast(True)
