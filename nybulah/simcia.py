"""Compiled 6526 (sim.Cia) kernels over simfast's state vector, slots CCRA..HASCIA."""

# pylint: disable=too-many-return-statements,too-many-branches

import numpy as np
from numba import njit

from .opencbm import IEC_DATA, IEC_SRQ
from .sim import CRA_LOAD, CRA_SPOUT, CRA_START, ICR_IR, ICR_SP, ICR_TA, Cia

CCRA, CLATCH, CT0, CC0, CMASK, CFLAGS, CTICR, CSDR = range(90, 98)
CSR, CBITS, CU1, COUT, CPEND, CSP, CRISE, CDELAY = range(98, 106)
CREGS, HASCIA, END = 106, 122, 123
FIELDS = np.arange(CCRA, CREGS)
# Kernels allocate nothing: without NRT their array arguments are not refcounted.
kernel = njit(cache=True, _nrt=False)


def pack_cia(cia, s):
    """A drive's Cia (or None) into the state vector."""
    if cia is not None:
        s[FIELDS] = [getattr(cia, n) for n in Cia.FIELDS]
        s[CREGS : CREGS + 16] = memoryview(cia.regs)
        s[HASCIA] = 1


def unpack_cia(cia, s):
    """The state vector's CIA back into the drive's Cia."""
    if cia is not None:
        vars(cia).update(zip(Cia.FIELDS, s[FIELDS].tolist()))
        cia.regs[:] = s[CREGS : CREGS + 16].astype(np.uint8).tobytes()


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
    """Cia.settle."""
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
    """Cia.read."""
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
    """Cia.write and Cia.control."""
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
