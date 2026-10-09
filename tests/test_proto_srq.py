"""SRQ fast serial (s4, firmware v11) on a timed 1571 with the firmware's adapter model."""

import struct
import warnings

import pytest
from xsim import vanish_mid_block, ZP, rand

from nybulah import simsrq, simx
from nybulah.fastx import SrqLink, XBLink, xsum
from nybulah.monitor import CLOCK_HZ, WATCHDOG_S, Monitor
from nybulah.opencbm import IEC_CLOCK, IEC_DATA, IEC_SRQ
from nybulah.sim import CRA_START, PA_FSDIR
from nybulah.simsrq import SRQ_GMAX, SRQ_GMIN, SRQ_LAST, SRQ_RLOOP, SRQ_SEND, SrqTiming

BASE = 0x6000
DOS_CIA = {"latch": 6, "cra": CRA_START, "mask": 0x08}
CLOCKS = [False, True]


def session(fast=False, peers=0, seed=0, rise=1.0, **kw):
    """Started s4 Monitor on a timed 1571 (DOS CIA state) at device 9."""
    retries = kw.pop("retries", 3)
    cbm = simsrq.make(1.0, rise=rise, peers=peers, dev=9, seed=seed, **kw)
    d = cbm.drive
    d.load(0x30, ZP)
    d.cia.write(4, DOS_CIA["latch"], 0)
    d.cia.write(5, 0, 0)
    d.cia.write(13, 0x80 | DOS_CIA["mask"], 0)
    d.cia.write(14, DOS_CIA["cra"], 0)
    mon = Monitor(cbm, 9, "s4")
    mon.link.retries = retries
    mon.start()
    if fast:
        mon.set_fast(True)
    return cbm, mon


def assert_returned(cbm):
    """Back in DOS: lines released, zero page, CIA and port A as DOS left them."""
    d = cbm.drive
    assert d.halted and d.pb_out == 0
    assert d.dump(0x30, len(ZP)) == ZP
    assert (d.cia.latch, d.cia.cra, d.cia.mask) == tuple(DOS_CIA.values())
    assert not d.cia.flags and not d.via1.regs[1] & (PA_FSDIR | 0x20)
    assert not cbm.bus.lines() & (IEC_SRQ | IEC_DATA | IEC_CLOCK)


def f_of(fast):
    return 8 if fast else 16


@pytest.mark.parametrize("fast", CLOCKS)
@pytest.mark.parametrize("peers", [0, 2])
@pytest.mark.parametrize("delay", [0, 3])
def test_round_trip_with_idle_peers(fast, peers, delay):
    cbm, mon = session(fast, peers, delay=delay)
    assert isinstance(mon.link, SrqLink)
    data = rand(0x1A5, peers + delay)
    mon.write(BASE + 0xF3, data)
    assert cbm.drive.dump(BASE + 0xF3, len(data)) == data
    assert mon.read(BASE + 0xF3, len(data)) == data
    assert mon.read(BASE, 0) == b"" and mon.link.rejects == cbm.retracts == 0
    mon.write(0x0300, bytes([0xA9, 0x12, 0xA2, 0x34, 0xA0, 0x56, 0x60]))
    assert mon.jsr(0x0300) == (0x12, 0x34, 0x56)
    mon.stop()
    cbm.settle()
    assert_returned(cbm)


@pytest.mark.parametrize("n", [1, 31, 32, 33, 63, 64, 65, 200])
def test_bank_boundary_sizes(n):
    cbm, mon = session(True)
    data, before = rand(n, n), cbm.bursts
    mon.write(BASE + 64, data)
    assert mon.read(BASE + 64, n) == data
    assert cbm.bursts - before == 2 * (2 + -(-n // 64))


def cia_log(cbm, run):
    """[(op, reg, cycle)] of the CIA accesses during run."""
    cia, log = cbm.drive.cia, []
    read, write = cia.read, cia.write
    cia.read = lambda reg, c: (log.append(("r", reg, c)), read(reg, c))[1]
    cia.write = lambda reg, v, c: (log.append(("w", reg, c)), write(reg, v, c))[1]
    run()
    cia.read, cia.write = read, write
    return log


@pytest.mark.parametrize("fast", CLOCKS)
def test_drive_loops_match_timing_constants(fast):
    cbm, mon = session(fast)
    cia = cbm.drive.cia
    starts = []
    write = cia.write

    def tracked(reg, v, c):
        write(reg, v, c)
        if reg == 12:
            starts.append(cia.u1)

    cbm.drive.load(BASE, rand(64))
    cia.write = tracked
    mon.read(BASE, 64)
    cia.write = write
    gaps = {b - a - SRQ_LAST for a, b in zip(starts[5:], starts[6:-3])}
    assert SRQ_GMIN <= min(gaps) and max(gaps) <= SRQ_GMAX
    sdr = [c for op, reg, c in cia_log(cbm, lambda: mon.read(BASE, 64)) if reg == 12]
    assert {b - a for a, b in zip(sdr[5:], sdr[6:-3])} == {SRQ_SEND}
    log = cia_log(cbm, lambda: mon.write(BASE, rand(64)))
    hits = [i for i, (op, reg, _) in enumerate(log) if (op, reg) == ("r", 12)]
    assert {log[i][2] - log[i - 1][2] for i in hits} == {7}
    nxt = {log[i + 1][2] - log[i - 1][2] for i in hits[:-1]}
    assert {d for d in nxt if d < 2 * SRQ_RLOOP} == {SRQ_RLOOP}  # within bursts


@pytest.mark.parametrize("fast", CLOCKS)
def test_block_throughput(fast):
    cbm, mon = session(fast)
    t = SrqTiming(f_of(fast))
    cyc = 0.5 if fast else 1.0

    def per_byte(op):
        start = cbm.drive.cycles
        op(512)
        a = cbm.drive.cycles - start
        start = cbm.drive.cycles
        op(1024)
        return (cbm.drive.cycles - start - a) / 512 * cyc

    rd = per_byte(lambda n: mon.read(BASE, n))
    wr = per_byte(lambda n: mon.write(BASE, bytes(n)))
    assert SRQ_SEND * cyc <= rd <= SRQ_SEND * cyc * 1.1
    assert t.period / 16 <= wr <= t.period / 16 * 1.1


def skewed(cbm, mon, op, skew=0.0, ppm=0.0, data_skew=0.0):
    """Whether a 64-byte block survives the adapter offsets (no retry)."""
    data, link = rand(64, 7), mon.link
    cbm.drive.load(BASE, bytes(64))
    cmd = (b"R" if op == "read" else b"W") + struct.pack("<HH", BASE, 64)
    if op == "read":
        cbm.drive.load(BASE, data)
    link.send(cmd)
    cbm.skew, cbm.ppm, cbm.data_skew = skew, ppm, data_skew
    try:
        if op == "read":
            got = link.rx(64)
        else:
            link.tx(data)
    except simx.XError:
        return False
    finally:
        cbm.skew = cbm.ppm = cbm.data_skew = 0.0
    link.rx(3)
    return (got if op == "read" else cbm.drive.dump(BASE, 64)) == data


def write_skews(t):
    """DATA-against-SRQ slack (set-up, hold) in us."""
    return t.write_slack()[:2]


@pytest.mark.parametrize("fast", CLOCKS)
@pytest.mark.parametrize("seed", range(3))
def test_read_margins_hold_with_worst_rise_and_jitter(fast, seed):
    t = SrqTiming(f_of(fast))
    low = t.margin()[0]
    cbm, mon = session(fast, seed=seed, read_jitter=t.f / 32)
    for skew in (-0.95 * low, 0.0, 0.95 * low):
        assert skewed(cbm, mon, "read", skew)


@pytest.mark.parametrize("fast", CLOCKS)
@pytest.mark.parametrize("seed", range(3))
def test_write_margins_hold_with_worst_rise_and_jitter(fast, seed):
    setup, hold = write_skews(SrqTiming(f_of(fast)))
    cbm, mon = session(fast, seed=seed, read_jitter=f_of(fast) / 32)
    for skew in (0.95 * setup, 0.0, -0.95 * hold):
        assert skewed(cbm, mon, "write", data_skew=skew)


@pytest.mark.parametrize("fast", CLOCKS)
@pytest.mark.parametrize(
    "op,side", [("read", 1), ("read", -1), ("write", 1), ("write", -1)]
)
def test_violating_margins_corrupts(fast, op, side):
    t = SrqTiming(f_of(fast))
    cbm, mon = session(fast)
    assert skewed(cbm, mon, op)
    rise, cyc = t.rise / 16, t.f / 16
    if op == "read":
        beyond = max(max(p) for p in t.read_slack()) + (t.poll + t.rise) / 16 + cyc
        assert not skewed(cbm, mon, op, side * beyond)
    else:
        slack = write_skews(t)[side < 0]
        assert not skewed(cbm, mon, op, data_skew=side * (slack + 2 * rise + cyc))


@pytest.mark.parametrize("fast", CLOCKS)
def test_crystal_drift(fast):
    t = SrqTiming(f_of(fast))
    assert min(t.margin(200)) > 0
    cbm, mon = session(fast, read_jitter=t.f / 32)
    for ppm in (-200.0, 200.0):
        assert skewed(cbm, mon, "read", ppm=ppm)
        assert skewed(cbm, mon, "write", ppm=ppm)
    beyond = max(max(p) for p in t.read_slack()) + (t.poll + t.rise + t.f) / 16
    assert not skewed(cbm, mon, "read", ppm=beyond / (t.sample[-1] / 16) * 1e6)


@pytest.mark.parametrize("op", ["read", "write"])
def test_corrupt_bit_is_retried(op):
    cbm, mon = session(True)
    data = rand(100, 3)
    cbm.drive.load(BASE, data)
    cbm.faults[cbm.ordinal + 5 + 20] = (2, IEC_DATA)
    if op == "read":
        assert mon.read(BASE, 100) == data
    else:
        mon.write(BASE + 0x100, data)
        assert cbm.drive.dump(BASE + 0x100, 100) == data
    assert mon.link.rejects == 1


def test_check_is_xsum_over_command_and_data():
    cmd, data = b"R\0\x60\x04\0", rand(4)
    assert SrqLink.check(cmd, sent=data) == xsum(cmd + data) == SrqLink.check(cmd, data)
    assert XBLink.check(cmd, sent=data) != xsum(cmd + data)


@pytest.mark.parametrize("op", [b"R", b"W"])
def test_host_vanishes_drive_times_out(op):
    cbm, mon = session(True)
    partial, cycles = vanish_mid_block(cbm, mon, op, BASE)
    assert len(partial) == 2 * simx.XB_BURST
    assert cycles <= CLOCK_HZ * WATCHDOG_S + 500
    assert cbm.drive.cyc == 1.0
    assert_returned(cbm)


@pytest.mark.parametrize("fast", CLOCKS)
def test_host_stops_mid_burst_drive_leaves(fast, monkeypatch):
    cbm, mon = session(fast)
    full = SrqTiming.changes
    monkeypatch.setattr(
        SrqTiming, "changes", lambda self, b, flip: full(self, b, flip)[:99]
    )
    mon.link.send(b"W" + struct.pack("<HH", BASE, 64))
    t0 = cbm.drive.cycles
    mon.link.tx(bytes(64))
    cbm.bus.drive_host(0, cbm.now)  # adapter loses power mid-byte
    cbm.settle()
    assert cbm.drive.cycles - t0 < 300 * SRQ_RLOOP
    assert_returned(cbm)


@pytest.mark.parametrize("op", ["read", "write"])
def test_drive_vanishes(op):
    cbm, mon = session(True, timeout_us=2_000.0, slice_us=500.0)
    cbm.drive.load(BASE, rand(200))
    step, budget = cbm.drive.step, [1500]

    def dying():
        budget[0] -= 1
        if not budget[0]:
            cbm.drive.reset()
        return step()

    cbm.drive.step = dying
    with pytest.raises(simx.XError) as e:
        if op == "read":
            mon.link.send(b"R" + struct.pack("<HH", BASE, 200))
            mon.link.rx(200)
        else:
            mon.link.send(b"W" + struct.pack("<HH", BASE, 200))
            mon.link.tx(bytes(200))
            mon.link.rx(3)
    assert 0 < len(e.value.partial) < 200 or op == "write"
    assert not cbm.bus.host_lines
    settled = max(cbm.now, cbm.drive.time(cbm.drive.cycles)) + cbm.bus.rise
    assert not cbm.bus.level(settled) & (IEC_SRQ | IEC_DATA)


def test_refused_on_a_1541():
    cbm = simx.make("1541", dev=9, firmware=11)
    cbm.supports = lambda p: True
    with pytest.raises(ValueError, match="needs a 1571"):
        Monitor(cbm, 9, "s4")


def test_older_firmware_falls_back_to_burst_x():
    cbm = simsrq.make(dev=9, firmware=10)
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        mon = Monitor(cbm, 9, "s4")
    assert mon.protocol == "s3" and isinstance(mon.link, XBLink)
    assert "firmware v11" in str(w[0].message)


def test_timing_report(capsys):
    simsrq.main(["--f", "8"])
    r = simsrq.report(8)
    assert str(r["sample"]) in capsys.readouterr().out
    assert SrqTiming(8).sample == (21, 53, 85, 117, 149, 181, 213, 245)
    assert (SrqTiming(8).start, SrqTiming(8).bit, SrqTiming(8).low) == (273, 50, 21)
    assert SrqTiming(16).sample[0] == 37 and SrqTiming(16).start == 541
    assert (SrqTiming(16).bit, SrqTiming(16).low) == (83, 33)
    assert min(r["margin_200ppm_us"]) > 0.3
    assert not simsrq.SrqTiming().changes(b"")
