"""Compiled 6526 (sim.Cia) and 8520 (sim.Cia8520) kernels over simfast's state vector."""

# pylint: disable=too-many-return-statements,too-many-branches

import numpy as np
from numba import njit

from .opencbm import IEC_CLOCK, IEC_DATA, IEC_SRQ
from .sim import CRA_LOAD, CRA_SPOUT, CRA_START, ICR_IR, ICR_SP, ICR_TA, ICR_TB
from .sim import CRB_INMODE, CRB_LOAD, CRB_ONESHOT, CRB_START, INMODE_PHI2, INMODE_TA
from .sim import ICR_FLAG, PB_ATN_IN, PB_ATNA, PB_CLK_IN, PB_CLK_OUT, PB_DATA_IN
from .sim import PB_DATA_OUT, PB_FSDIR, PB_WPRT, Cia, Cia8520
from .simwd import WWP, wd_control, wd_inputs

CCRA, CLATCH, CT0, CC0, CMASK, CFLAGS, CTICR, CSDR = range(90, 98)
CSR, CBITS, CU1, COUT, CPEND, CSP, CRISE, CDELAY = range(98, 106)
CREGS, HASCIA = 106, 122
CCRB, CTBL, CTBT0, CTBC0, CATN, C8520, END = range(123, 130)
FIELDS, FIELDS81 = np.arange(CCRA, CREGS), np.arange(CCRB, C8520)
# Kernels allocate nothing: without NRT their array arguments are not refcounted.
kernel = njit(cache=True, _nrt=False)


def pack_cia(cia, s):
    """A drive's Cia (or None) into the state vector."""
    if cia is not None:
        s[FIELDS] = [getattr(cia, n) for n in Cia.FIELDS]
        s[CREGS : CREGS + 16] = memoryview(cia.regs)
        s[HASCIA] = 1
    if isinstance(cia, Cia8520):
        s[FIELDS81] = [getattr(cia, n) for n in Cia8520.FIELDS81]
        s[C8520] = 1


def unpack_cia(cia, s):
    """The state vector's CIA back into the drive's Cia."""
    if cia is not None:
        vars(cia).update(zip(Cia.FIELDS, s[FIELDS].tolist()))
        cia.regs[:] = s[CREGS : CREGS + 16].astype(np.uint8).tobytes()
    if isinstance(cia, Cia8520):
        vars(cia).update(zip(Cia8520.FIELDS81, s[FIELDS81].tolist()))


@kernel
def cia_port(s, port, pins):
    """Port A (0) or B (1) as read: output latch bits, input pins elsewhere."""
    ddr = s[CREGS + port + 2]
    return s[CREGS + port] & ddr | pins & ~ddr & 0xFF


@kernel
def cia_pins(s, port):
    """Drive1581.port_pins: output pins, input pins high (pull-ups)."""
    return cia_port(s, port, 0xFF)


@kernel
def ta_underflows(s, a, c):
    """Cia8520.ta_underflows."""
    if not s[CCRA] & CRA_START:
        return 0
    first, p = s[CT0] + s[CC0] + 1, s[CLATCH] + 1
    upto_c = 0 if c < first else (c - first) // p + 1
    return upto_c - (0 if a < first else (a - first) // p + 1)


@kernel
def tb_settle(s, c):
    """Cia8520.tb_settle."""
    if s[CCRB] & CRB_START:
        mode = s[CCRB] & CRB_INMODE
        m = c - s[CTBT0] if mode == INMODE_PHI2 else 0
        if mode == INMODE_TA:
            m = ta_underflows(s, s[CTBT0], c)
        if m > s[CTBC0]:
            s[CFLAGS] |= ICR_TB
            if s[CCRB] & CRB_ONESHOT:
                s[CCRB] &= ~CRB_START
                s[CTBC0] = s[CTBL]
            else:
                s[CTBC0] = s[CTBL] - (m - s[CTBC0] - 1) % (s[CTBL] + 1)
        else:
            s[CTBC0] -= m
    s[CTBT0] = c


@kernel
def tb_write(s, reg, value, c):
    """Cia8520.write of timer B and CRB."""
    tb_settle(s, c)
    if reg == 6:
        s[CTBL] = s[CTBL] & 0xFF00 | value
    elif reg == 7:
        s[CTBL] = s[CTBL] & 0xFF | value << 8
        if not s[CCRB] & CRB_START:
            s[CTBC0] = s[CTBL]
    else:
        if value & CRB_LOAD:
            s[CTBC0] = s[CTBL]
        s[CCRB] = value & ~CRB_LOAD


@kernel
def cia_next_uf(s, c):
    """Cia.next_uf."""
    first, p = s[CT0] + s[CC0] + 1, s[CLATCH] + 1
    return first if c < first else first + ((c - first) // p + 1) * p


@kernel
def cia_counter(s, c):
    """Cia.counter."""
    first = s[CT0] + s[CC0] + 1
    if not s[CCRA] & CRA_START:
        return s[CC0]
    if c < first:
        return s[CC0] - (c - s[CT0])
    return s[CLATCH] - (c - first) % (s[CLATCH] + 1)


@kernel
def cia_advance(s, c):
    """Cia.advance."""
    u1, out, pend, sp, rise, done = s[CU1], s[COUT], s[CPEND], s[CSP], s[CRISE], 0
    p = s[CLATCH] + 1
    while 0 <= u1 <= c - 15 * p:
        rise, sp, done = u1 + 15 * p, out & 1, done + 1
        if pend >= 0:
            u1, out, pend = u1 + 16 * p, pend, -1
        else:
            u1, pend = -1, -1
    return u1, out, pend, sp, rise, done


@kernel
def cia_settle(s, c):
    """Cia.settle (and Cia8520's timer B)."""
    if s[C8520]:
        tb_settle(s, c)
    s[CU1], s[COUT], s[CPEND], s[CSP], s[CRISE], done = cia_advance(s, c)
    if done:
        s[CFLAGS] |= ICR_SP
    if s[CCRA] & CRA_START and cia_next_uf(s, s[CTICR]) <= c:
        s[CFLAGS] |= ICR_TA
    s[CTICR] = c


@kernel
def cia_lines(s, c, fsdir):
    """Cia.lines."""
    if not (s[HASCIA] and fsdir and s[CCRA] & CRA_SPOUT):
        return 0
    u1, out, _, sp, _, _ = cia_advance(s, c)
    cnt = 1
    if 0 <= u1 <= c:
        k = (c - u1) // (s[CLATCH] + 1)
        cnt, sp = k & 1, out >> 7 - (k >> 1) & 1
    return (0 if cnt else IEC_SRQ) | (0 if sp else IEC_DATA)


@kernel
def cia_read(s, reg, c):
    """Cia.read and Cia8520.read."""
    if s[C8520] and reg in (6, 7, 15):
        tb_settle(s, c)
        return s[CCRB] if reg == 15 else s[CTBC0] >> 8 * (reg - 6) & 0xFF
    if reg in (4, 5):
        return cia_counter(s, c) >> 8 * (reg - 4) & 0xFF
    if reg in (12, 13):
        cia_settle(s, c)
        if reg == 12:
            return s[CSDR]
        v = s[CFLAGS] | (ICR_IR if s[CFLAGS] & s[CMASK] else 0)
        s[CFLAGS] = 0
        return v
    return s[CCRA] if reg == 14 else s[CREGS + reg]


@kernel
def cia_load(s, value, c):
    """Cia.load."""
    if s[CU1] > c:
        s[COUT] = value
    elif s[CU1] >= 0 or not s[CCRA] & CRA_START:
        s[CPEND] = value
    else:
        s[CU1], s[COUT] = cia_next_uf(s, c + s[CDELAY]), value


@kernel
def cia_write(s, reg, value, c):
    """Cia.write, Cia.control and Cia8520.write."""
    if s[C8520] and reg in (6, 7, 15):
        tb_write(s, reg, value, c)
        return
    if s[C8520] and reg in (4, 5):
        tb_settle(s, c)
    if reg == 4:
        s[CLATCH] = s[CLATCH] & 0xFF00 | value
    elif reg == 5:
        s[CLATCH] = s[CLATCH] & 0xFF | value << 8
        if not s[CCRA] & CRA_START:
            s[CT0], s[CC0] = c, s[CLATCH]
    elif reg == 12:
        cia_settle(s, c)
        s[CSDR] = value
        if s[CCRA] & CRA_SPOUT:
            cia_load(s, value, c)
    elif reg == 13:
        m = value & 0x7F
        s[CMASK] = s[CMASK] | m if value & 0x80 else s[CMASK] & ~m
    elif reg == 14:
        cia_settle(s, c)
        count = s[CLATCH] if value & CRA_LOAD else cia_counter(s, c)
        if value & CRA_LOAD or (value ^ s[CCRA]) & CRA_START or not value & CRA_START:
            s[CT0], s[CC0] = c, count
        if (value ^ s[CCRA]) & CRA_SPOUT or not value & CRA_START:
            s[CU1], s[CPEND] = -1, -1
        s[CCRA] = value & ~CRA_LOAD
        if s[CCRA] & CRA_SPOUT and s[CU1] < 0 <= s[CPEND]:
            s[CU1], s[COUT] = cia_next_uf(s, c + s[CDELAY]), s[CPEND]
            s[CPEND] = -1
    else:
        s[CREGS + reg] = value


@kernel
def cia_atn(s, atn):
    """Cia8520.atn_edge."""
    if atn and not s[CATN]:
        s[CFLAGS] |= ICR_FLAG
    s[CATN] = 1 if atn else 0


@kernel
def cia81_lines(s, pb, atn, c):
    """Drive1581.drive_lines from port B outputs pb, bus ATN and the CIA at c."""
    lines = IEC_CLOCK if pb & PB_CLK_OUT else 0
    if pb & PB_DATA_OUT or pb & PB_ATNA and atn:
        lines |= IEC_DATA
    return lines | cia_lines(s, c, cia_pins(s, 1) & PB_FSDIR)


@kernel
def cia81_read(s, m, reg, c, device, pb):
    """Drive1581.read of the CIA; pb: Drive1541.port_b for port B."""
    if reg > 1:
        return cia_read(s, reg, c)
    if reg == 0:
        return cia_port(s, 0, wd_inputs(s, m, c, 0xE7 | ((device - 8) & 3) << 3))
    v = pb & (PB_DATA_IN | PB_CLK_IN | PB_ATN_IN) | 0x3A
    return cia_port(s, 1, v | (0 if s[WWP] else PB_WPRT))


@kernel
def cia81_write(s, m, reg, value, c):
    """Drive1581.write of the CIA: port A to the mechanism; port B bus outputs."""
    cia_write(s, reg, value, c)
    if reg in (0, 2):
        wd_control(s, m, c, cia_pins(s, 0))
    return cia_pins(s, 1) & (PB_DATA_OUT | PB_CLK_OUT | PB_ATNA)
