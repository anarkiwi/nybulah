"""1571 WD1770 registers for the drive simulator: status type, busy and the index.

Datasheet: status bit 1 is the index only in type I status; Force Interrupt keeps a
running command's status type and shows type I only when idle. Type I ends at once,
type II/III find no MFM and stay busy; WD accesses from addresses ending %00 count.
"""

import numpy as np

from .simwd import kernel

W71_TYPE1, W71_BUSY, W71_CMD, W71_TRK, W71_SEC, W71_DAT, W71_VIOL = range(7)
W71_SIZE = 7
BUSY, INDEX = 0x01, 0x02
FORCE, FORCE_MASK, TYPE2 = 0xD0, 0xF0, 0x80
SEEK, SEEK_MASK = 0x10, 0xF0


def wd71_state():
    """Power-on state: idle, type I status, no command written."""
    w = np.zeros(W71_SIZE, np.int64)
    w[W71_TYPE1], w[W71_CMD] = 1, -1
    return w


@kernel
def wd71_access(w, o, pc):
    """Count an access by an instruction at an address ending in %00 (wd1770.src)."""
    if pc >= 0 and pc & 3 == 0:
        w[o + W71_VIOL] += 1


@kernel
def wd71_read(w, o, reg, hole, pc):
    """Register ``reg`` (0-3) of the state at ``o``; ``hole``: the sensor sees it."""
    wd71_access(w, o, pc)
    if reg == 0:
        return w[o + W71_BUSY] | (INDEX if w[o + W71_TYPE1] and hole else 0)
    return w[o + W71_TRK - 1 + reg]


@kernel
def wd71_write(w, o, reg, value, pc):
    """Write ``value`` to register ``reg``; a Seek loads the track register from data
    (the 1571 steps through VIA2); busy, only Force Interrupt and data are accepted."""
    wd71_access(w, o, pc)
    busy = w[o + W71_BUSY]
    if reg == 0:
        w[o + W71_CMD] = value
        if value & FORCE_MASK == FORCE:
            if busy:
                w[o + W71_BUSY] = 0
            else:
                w[o + W71_TYPE1] = 1
        elif not busy:
            w[o + W71_TYPE1] = value < TYPE2
            w[o + W71_BUSY] = value >= TYPE2
            if value & SEEK_MASK == SEEK:
                w[o + W71_TRK] = w[o + W71_DAT]
    elif reg == 3 or not busy:
        w[o + W71_TRK - 1 + reg] = value
