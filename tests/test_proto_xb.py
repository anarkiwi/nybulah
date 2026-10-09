"""Burst X (firmware v10) specifics; shared scenarios run in test_proto_x for both."""

import struct

import pytest
from xsim import assert_returned, rand, session, steady_cycles, trace

from nybulah import simx
from nybulah.fastx import XB_CHUNK, xbsum
from nybulah.monitor import CLOCK_HZ, WATCHDOG_S
from nybulah.sim import HostGone

MODELS = [("1541", 1.0, 0x8000), ("1571", 0.5, 0x6000)]


def reference_sum(received, sent):
    """6502 semantics of the drive loops: the send loop enters adc with C = bit 3."""
    s1 = s2 = 0
    for b, c in [(b, 0) for b in received] + [(b, b >> 3 & 1) for b in sent]:
        t = s1 + b + c
        s1, c = t & 0xFF, t >> 8
        s2 = (s2 + s1 + c) & 0xFF
    return s1, s2


@pytest.mark.parametrize("n", [0, 1, 255, 256, 1000])
def test_xbsum_matches_6502_semantics(n):
    cmd, data = rand(5, n + 1), rand(n, n)
    assert xbsum(cmd, data) == reference_sum(cmd, data)
    assert xbsum(cmd + data) == reference_sum(cmd + data, b"")


def test_bursts_align_to_addresses_and_chunks():
    cbm, mon = session(fw=10)
    mon.link.chunk = 0x200
    data = rand(0x500, 5)
    cbm.drive.load(0x8000, data)
    before = cbm.bursts
    assert mon.read(0x8003, 0x400) == data[3:0x403]
    # per chunk: command, 61-byte head, 7 full bursts, 3-byte tail, reply
    assert cbm.bursts - before == 2 * (1 + 1 + 7 + 1 + 1)
    mon.write(0x8000 + 63, data[:130])
    assert cbm.drive.dump(0x8000 + 63, 130) == data[:130]
    assert XB_CHUNK == 0x2000


@pytest.mark.parametrize("model,cyc,base", MODELS)
def test_drive_schedule_matches_timing_table(model, cyc, base):
    cbm, mon = session(model, cyc, fw=10)
    n = 5
    cbm.drive.load(base, rand(n))
    sends = [f[0] for f in trace(cbm, lambda: mon.read(base, n)) if len(f[0]) > 4]
    assert len(sends) == 1
    want = [w + i * simx.XB_SEND_PERIOD for i in range(n) for w in simx.XB_SEND[:4]] + [
        simx.XB_SEND[4] + (n - 1) * simx.XB_SEND_PERIOD
    ]
    assert [round(t / cyc) for t in sends[0]] == want
    recvs = [f for f in trace(cbm, lambda: mon.write(base, rand(n))) if f[1]]
    assert len(recvs) == 2  # command and data, both 5 bytes, then go polls
    for ws, rs in recvs:
        assert [round(t / cyc) for t in ws] == [simx.XB_RECV[0]]
        assert [round(t / cyc) for t in rs[: 4 * n]] == [
            r + i * simx.XB_RECV_PERIOD for i in range(n) for r in simx.XB_RECV[1:]
        ]


@pytest.mark.parametrize("model,cyc", [("1541", 1.0), ("1571", 0.5)])
def test_block_throughput(model, cyc):
    cbm, mon = session(model, 1.0, fw=10)
    if cyc < 1:
        mon.set_fast(True)
    base = 0x8000 if model == "1541" else 0x6000
    rd = steady_cycles(cbm, lambda n: mon.read(base, n), 512)
    wr = steady_cycles(cbm, lambda n: mon.write(base, bytes(n)), 512)
    assert simx.XB_SEND_PERIOD <= rd <= simx.XB_SEND_PERIOD + 3
    assert simx.XB_RECV_PERIOD <= wr <= simx.XB_RECV_PERIOD + 3


def skewed(cbm, mon, op, base, skew, ppm=0.0):
    data = rand(simx.XB_BURST, 7)
    link = mon.link
    cbm.drive.load(base, bytes(len(data)))
    if op == "read":
        cbm.drive.load(base, data)
        link.send(b"R" + struct.pack("<HH", base, len(data)))
        cbm.skew, cbm.ppm = skew, ppm
        got = link.rx(len(data))
        cbm.skew = cbm.ppm = 0.0
        link.rx(3)
        return got == data
    link.send(b"W" + struct.pack("<HH", base, len(data)))
    cbm.skew, cbm.ppm = skew, ppm
    link.tx(data)
    cbm.skew = cbm.ppm = 0.0
    link.rx(3)
    return cbm.drive.dump(base, len(data)) == data


def margins(op, cyc):
    """Burst timing and the smallest and largest finite slack (us) for op."""
    t = simx.BurstTiming(round(16 * cyc))
    slack = [x for p in (t.send_slack() if op == "read" else t.recv_slack()) for x in p]
    return t, min(slack), max(x for x in slack if x < float("inf"))


@pytest.mark.parametrize("model,cyc,base", MODELS)
@pytest.mark.parametrize("op", ["read", "write"])
@pytest.mark.parametrize("seed", range(3))
def test_margins_hold_with_worst_rise_and_jitter(model, cyc, base, op, seed):
    t, low, _ = margins(op, cyc)
    cbm, mon = session(model, cyc, seed=seed, read_jitter=t.v, fw=10)
    for skew in (-0.95 * low, 0.0, 0.95 * low):
        assert skewed(cbm, mon, op, base, skew)


@pytest.mark.parametrize("model,cyc,base", MODELS)
@pytest.mark.parametrize("op", ["read", "write"])
def test_violating_margins_corrupts(model, cyc, base, op):
    t, _, high = margins(op, cyc)
    cbm, mon = session(model, cyc, fw=10)
    beyond = high + (t.poll + t.rise) / 16 + 2 * t.v
    assert skewed(cbm, mon, op, base, 0.0)
    assert not skewed(cbm, mon, op, base, beyond)
    assert not skewed(cbm, mon, op, base, -beyond)


@pytest.mark.parametrize("model,cyc,base", MODELS)
@pytest.mark.parametrize("op", ["read", "write"])
def test_crystal_drift(model, cyc, base, op):
    t, _, high = margins(op, cyc)
    assert min(t.margin(200)) > 0
    cbm, mon = session(model, cyc, read_jitter=t.v, fw=10)
    for ppm in (-200.0, 200.0):
        assert skewed(cbm, mon, op, base, 0.0, ppm)
    n = simx.XB_BURST - 1
    last = t.sample[3] + n * t.send_period if op == "read" else t.change[5]
    last += 0 if op == "read" else n * t.recv_period
    beyond = high + (t.poll + t.rise) / 16 + 2 * t.v
    assert not skewed(cbm, mon, op, base, 0.0, beyond / (last / 16) * 1e6)


@pytest.mark.parametrize("op", ["read", "write"])
def test_host_vanishes_drive_times_out(op):
    cbm, mon = session(fw=10)
    cbm.vanish_at = cbm.ordinal + 5 + 130
    with pytest.raises(HostGone) as e:
        if op == "read":
            mon.link.send(b"R" + struct.pack("<HH", 0x8000, 200))
            mon.link.rx(200)
        else:
            mon.link.send(b"W" + struct.pack("<HH", 0x8000, 200))
            mon.link.tx(bytes(200))
    assert len(e.value.partial) == 2 * simx.XB_BURST
    t0 = cbm.drive.cycles
    cbm.settle()
    assert cbm.drive.cycles - t0 <= CLOCK_HZ * WATCHDOG_S + 500
    assert_returned(cbm)


def test_timing_report(capsys):
    simx.main(["--cyc", "0.5"])
    r = simx.report(0.5)
    assert str(r["burst"]["sample"]) in capsys.readouterr().out
    assert r["burst"]["period"] == [536, 416]
    assert min(r["burst"]["margin_200ppm_us"]) > 0.5
    assert simx.BurstTiming().sample == (325, 613, 901, 1093)
    assert simx.BurstTiming().change == (4, 244, 388, 532, 804, 672)
    assert simx.BurstTiming(8).change == (4, 116, 188, 260, 396, 336)
