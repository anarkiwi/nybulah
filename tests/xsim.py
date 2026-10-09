"""Shared helpers for the X transport co-simulation tests."""

import struct
from itertools import groupby

import numpy as np
import pytest

from nybulah import simx
from nybulah.monitor import Monitor
from nybulah.sim import HostGone

FIRMWARE = (9, 10)
ZP = bytes(range(0x11, 0x18))
SPARE = 0x0300


def rand(n, seed=0):
    return bytes(np.random.default_rng(seed).integers(0, 256, n, dtype=np.uint8))


def session(model="1541", cyc=1.0, peers=0, seed=0, rise=1.0, **kw):
    """Started s3 Monitor on a timed drive at device 9 (firmware fw, default 9)."""
    retries, dev = kw.pop("retries", 3), kw.pop("dev", 9)
    fw = kw.pop("fw", 9)
    cbm = simx.make(
        model, cyc, rise=rise, peers=peers, dev=dev, seed=seed, firmware=fw, **kw
    )
    cbm.drive.load(0x30, ZP)
    mon = Monitor(cbm, dev, "s3")
    mon.link.retries = retries
    mon.start()
    return cbm, mon


def assert_returned(cbm):
    """The drive is back in DOS with its lines released and zero page restored."""
    assert cbm.drive.halted and cbm.drive.pb_out == 0
    assert cbm.drive.dump(0x30, len(ZP)) == ZP


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


def steady_cycles(cbm, op, n):
    """Drive cycles per byte of op beyond its fixed cost (from n and 2n bytes)."""
    start = cbm.drive.cycles
    op(n)
    a = cbm.drive.cycles - start
    start = cbm.drive.cycles
    op(2 * n)
    return (cbm.drive.cycles - start - a) / n


def vanish_mid_block(cbm, mon, op, base, at=135):
    """Adapter gone `at` transfer bytes after a 200-byte block's command; returns the
    partial bytes and the drive cycles until it is back in DOS."""
    cbm.vanish_at = cbm.ordinal + at
    with pytest.raises(HostGone) as e:
        mon.link.send(op + struct.pack("<HH", base, 200))
        if op == b"R":
            mon.link.rx(200)
        else:
            mon.link.tx(bytes(200))
    t0 = cbm.drive.cycles
    cbm.settle()
    return e.value.partial, cbm.drive.cycles - t0
