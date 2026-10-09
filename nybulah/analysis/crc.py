"""CRC-16-CCITT as the WD177x computes it: G(x) = x^16 + x^12 + x^5 + 1, MSB first,
preset to ones, over everything from the first A1 of an address mark to the CRC."""

import numpy as np
from numba import njit

POLY = 0x1021
PRESET = 0xFFFF


def _table():
    c = np.arange(256, dtype=np.uint32) << 8
    for _ in range(8):
        c = np.where(c & 0x8000, (c << 1) ^ POLY, c << 1) & 0xFFFF
    return c.astype(np.uint16)


TABLE = _table()


@njit(cache=True)
def crc16(data, crc=PRESET):
    """CRC of a uint8 array continuing from crc."""
    for b in data:
        crc = ((crc << 8) & 0xFFFF) ^ TABLE[((crc >> 8) ^ b) & 0xFF]
    return crc


SYNC3 = crc16(np.full(3, 0xA1, np.uint8))
