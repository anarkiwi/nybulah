import struct
from itertools import groupby
import types

import numpy as np
import pytest

from nybulah import simx
from nybulah.fastx import CHUNK, ChecksumError, XLink, xio
from nybulah.monitor import CLOCK_HZ, WATCHDOG_S
from nybulah.opencbm import IEC_CLOCK, IEC_DATA, OpenCBMError
from nybulah.sim import HostGone

MODELS = [("1541", 1.0, 0x8000), ("1571", 0.5, 0x6000)]
ZP = bytes(range(0x11, 0x17))


def rand(n, seed=0):
    return bytes(np.random.default_rng(seed).integers(0, 256, n, dtype=np.uint8))


def session(model="1541", cyc=1.0, peers=0, seed=0, rise=1.0, retries=3, **kw):
    cbm = simx.make(model, cyc, rise=rise, peers=peers, dev=9, seed=seed, **kw)
    cbm.drive.load(0x30, ZP)
    link = XLink(cbm, 9, fast=cyc == 0.5, retries=retries)
    link.start()
    return cbm, link


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
    assert simx.xsum(data) == reference_sum(data)


@pytest.mark.parametrize("model,cyc,base", MODELS)
@pytest.mark.parametrize("peers", [0, 2])
def test_block_round_trip(model, cyc, base, peers):
    cbm, link = session(model, cyc, peers)
    data = rand(0x1A5, peers)
    link.write(base + 0xF3, data)
    assert cbm.drive.dump(base + 0xF3, len(data)) == data
    assert link.read(base + 0xF3, len(data)) == data
    assert link.read(base, 0) == b""
    assert link.rejects == link.restarts == cbm.retracts == 0
    link.stop()
    cbm.settle()
    assert cbm.drive.halted and cbm.drive.pb_out == 0
    assert cbm.drive.dump(0x30, len(ZP)) == ZP
    assert not cbm.bus.lines() & (IEC_DATA | IEC_CLOCK)


def test_reads_span_chunks():
    cbm, link = session()
    data = rand(CHUNK + 17, 5)
    cbm.drive.load(0x8000, data)
    assert link.read(0x8000, len(data)) == data


@pytest.mark.parametrize("model,cyc,base", MODELS)
def test_monitor_byte_commands(model, cyc, base):
    _, link = session(model, cyc)
    link.tx(b"W" + struct.pack("<HH", base, 3) + b"\x00\xff\x5a")
    link.tx(b"R" + struct.pack("<HH", base, 3))
    assert link.rx(3) == b"\x00\xff\x5a"
    link.write(0x0700, bytes([0xA9, 0x12, 0xA2, 0x34, 0xA0, 0x56, 0x60]))
    assert link.jsr(0x0700) == (0x12, 0x34, 0x56)


def trace(cbm, run):
    """Per SYNC (CLK-only write after a read): offsets of the writes, then reads, after it."""
    via, log = cbm.drive.via1, []
    read, write = via.read, via.write

    def r(reg):
        log.append((cbm.drive.t_access, "r", None) if reg == 0 else None)
        return read(reg)

    def w(reg, v):
        log.append((cbm.drive.t_access, "w", v) if reg == 0 else None)
        write(reg, v)

    via.read, via.write = r, w
    run()
    via.read, via.write = read, write
    runs = [list(g) for _, g in groupby(filter(None, log), lambda e: e[1])]
    return [
        tuple([e[0] - ws[0][0] for e in x] for x in (ws[1:], rs))
        for ws, rs in zip(runs, runs[1:])
        if ws[0][1:] == ("w", 0x08) and ws is not runs[0]
    ]


@pytest.mark.parametrize("model,cyc,base", MODELS)
def test_drive_schedule_matches_timing_table(model, cyc, base):
    cbm, link = session(model, cyc)
    data = rand(8)
    cbm.drive.load(base, data)
    writes = [f[0] for f in trace(cbm, lambda: link.read(base, 8)) if f[0][3:]]
    assert len(writes) >= 8
    for w in writes:
        assert [round(t / cyc) for t in w[:4]] == list(simx.SEND_SCHEDULE[1:5])
        assert round(w[4] / cyc) >= simx.SEND_SCHEDULE[5]
    reads = [f for f in trace(cbm, lambda: link.write(base, data)) if not f[0][1:]]
    assert len(reads) >= len(data) + 12
    for ws, rs in reads:
        assert [round(t / cyc) for t in ws] == [simx.RECV_SCHEDULE[1]]
        assert [round(t / cyc) for t in rs[:4]] == list(simx.RECV_SCHEDULE[2:])


def steady_cycles(cbm, op, n):
    start = cbm.drive.cycles
    op(n)
    a = cbm.drive.cycles - start
    start = cbm.drive.cycles
    op(2 * n)
    return (cbm.drive.cycles - start - a) / n


def test_block_throughput():
    cbm, link = session()
    rd = steady_cycles(cbm, lambda n: link.read(0x8000, n), 256)
    wr = steady_cycles(cbm, lambda n: link.write(0x8000, bytes(n)), 256)
    assert rd == pytest.approx(100, abs=0.3)
    assert wr == pytest.approx(112, abs=0.3)


def skewed(cbm, link, op, base, skew):
    data = rand(64, 7)
    cbm.drive.load(base, bytes(64))
    if op == "read":
        cbm.drive.load(base, data)
        link.params(base, 64, link.xread)
        cbm.skew = skew
        got = link.rx(67)
        cbm.skew = 0.0
        return got[:64] == data
    link.params(base, 64, link.xwrite)
    cbm.skew = skew
    link.tx(data)
    cbm.skew = 0.0
    link.rx(3)
    return cbm.drive.dump(base, 64) == data


@pytest.mark.parametrize("model,cyc,base", MODELS)
@pytest.mark.parametrize("op", ["read", "write"])
@pytest.mark.parametrize("seed", range(3))
def test_margins_hold_with_worst_rise_and_jitter(model, cyc, base, op, seed):
    t = simx.Timing(cyc=cyc)
    cbm, link = session(model, cyc, seed=seed, read_jitter=t.v)
    margin = min(t.send_margin if op == "read" else t.recv_margin)
    for skew in (-0.95 * margin, 0.0, 0.95 * margin):
        assert skewed(cbm, link, op, base, skew)


@pytest.mark.parametrize("model,cyc,base", MODELS)
@pytest.mark.parametrize("op", ["read", "write"])
def test_violating_margins_corrupts(model, cyc, base, op):
    t = simx.Timing(cyc=cyc)
    cbm, link = session(model, cyc)
    margin = max(t.send_margin if op == "read" else t.recv_margin)
    beyond = margin + t.poll + 2 * t.v + t.rise
    assert skewed(cbm, link, op, base, 0.0)
    assert not skewed(cbm, link, op, base, beyond)
    assert not skewed(cbm, link, op, base, -beyond)


@pytest.mark.parametrize("op", ["read", "write"])
def test_corrupt_pair_is_retried(op):
    cbm, link = session()
    data = rand(100, 3)
    cbm.drive.load(0x8000, data)
    cbm.faults[cbm.ordinal + 20] = (2, IEC_DATA)
    if op == "read":
        assert link.read(0x8000, 100) == data
    else:
        link.write(0x8100, data)
        assert cbm.drive.dump(0x8100, 100) == data
    assert link.rejects == 1


def test_persistent_corruption_raises():
    _, link = session(retries=1)
    read = link.rx

    def flipped(n):
        got = read(n)
        return bytes([got[0] ^ (n > 3)]) + got[1:]

    link.rx = flipped
    with pytest.raises(ChecksumError):
        link.read(0x8000, 16)
    assert link.rejects == 2


def test_backpressure_and_slow_drive():
    cbm, link = session(slice_us=300.0)
    data = rand(40, 9)
    cbm.drive.load(0x8000, data)
    cbm.pause[cbm.ordinal + 15] = 5000.0
    assert link.read(0x8000, 40) == data
    # dex/bne nested delay of ~5 ms, then rts
    loop = bytes([0xA0, 0x04, 0xA2, 0x00, 0xCA, 0xD0, 0xFD, 0x88, 0xD0, 0xF8, 0x60])
    link.write(0x0700, loop)
    assert link.jsr(0x0700)[1:] == (0, 0)
    assert cbm.retracts > 0


def test_adapter_timeout_when_drive_gone():
    cbm, link = session(timeout_us=2_000.0, slice_us=500.0)
    link.stop()
    cbm.settle()
    with pytest.raises(simx.XTimeout) as e:
        cbm.x_read(4)
    assert e.value.partial == b""
    with pytest.raises(simx.XTimeout):
        cbm.x_write(b"Q")
    assert not cbm.bus.host_lines


@pytest.mark.parametrize("op", ["read", "write"])
def test_host_vanishes_drive_times_out(op):
    cbm, link = session()
    cbm.vanish_at = cbm.ordinal + 30
    with pytest.raises(HostGone):
        if op == "read":
            link.params(0x8000, 200, link.xread)
            link.rx(203)
        else:
            link.params(0x8000, 200, link.xwrite)
            link.tx(bytes(200))
    t0 = cbm.drive.cycles
    cbm.settle()
    assert cbm.drive.cycles - t0 <= CLOCK_HZ * WATCHDOG_S + 500
    assert cbm.drive.halted and cbm.drive.pb_out == 0
    assert cbm.drive.dump(0x30, len(ZP)) == ZP


def test_transport_error_restarts_session():
    cbm, link = session()
    data = rand(50, 4)
    cbm.drive.load(0x8000, data)
    cbm.vanish_at = cbm.ordinal + 20
    assert link.read(0x8000, 50) == data
    assert link.restarts == 1


def test_context_manager_recovers_on_error():
    cbm, link = session()
    link.stop()
    cbm.settle()
    with pytest.raises(OpenCBMError):
        with XLink(cbm, 9) as x:
            cbm.vanish_at = cbm.ordinal
            x.jsr(0x0700)
    assert cbm.drive.halted


def test_xio_uses_plugin_entry_points():
    calls = []

    fake = types.SimpleNamespace(
        _read_n=lambda proto, n: calls.append((proto, n)) or bytes(n),
        _write_n=lambda proto, data: calls.append((proto, bytes(data))),
    )
    rd, wr = xio(fake, fast=True)
    assert rd(2) == b"\0\0"
    wr(b"x")
    assert calls == [("x2", 2), ("x2", b"x")]


def test_rejects_code_without_tag():
    with pytest.raises(ValueError):
        XLink(simx.make(), 8, code=b"\x60")


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
