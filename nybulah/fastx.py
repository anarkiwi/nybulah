"""S3 link for Monitor: the X transport (xum1541 v9+) with checked block transfers."""

import struct

import numpy as np

from .link import BASE, WATCHDOG_IDLE_S, BusError, HandshakeTimeout, S1Link
from .opencbm import IEC_CLOCK, IEC_DATA, OpenCBMError

TAG = b"NYBX"
CHUNK = 0x1000
VIA1PA = 0x1801
PA_2MHZ = 0x20
CLOCK_CODE = {
    True: bytes([0xA9, PA_2MHZ, 0x0D]) + struct.pack("<H", VIA1PA),
    False: bytes([0xA9, 0xFF ^ PA_2MHZ, 0x2D]) + struct.pack("<H", VIA1PA),
}
CLOCK_TAIL = bytes([0x8D]) + struct.pack("<H", VIA1PA) + b"\x60"


def xsum(data):
    """Block check of sendblk/recvblk: (s1, s2)."""
    s = np.cumsum(np.frombuffer(bytes(data), np.uint8), dtype=np.int64)
    if not s.size:
        return 0, 0
    return int(s[-1] & 0xFF), int(((s & 0xFF).sum() + (s[-1] >> 8)) & 0xFF)


class ChecksumError(BusError):
    """A block failed its check after every retry."""


class XLink(S1Link):
    """Idle bus with both lines released; blocks go through xread/xwrite via 'J'.

    Each block is checked against the drive's (s1, s2) and repeated up to
    retries times on a mismatch.
    """

    idle_line = 0
    retries = 3

    def __init__(self, mon):
        super().__init__(mon)
        at = mon.code.find(TAG)
        if at < 0:
            raise ValueError("monitor code lacks the NYBX tag")
        self.xparm = BASE + at + len(TAG)
        self.rejects = 0
        self.fast = False

    def set_fast(self, fast):
        """Switch a 1571 between 1 and 2 MHz; the reply already uses the new timing.

        The drive's watchdog windows scale with its clock, so idle_s follows.
        """
        if fast == self.fast:
            return
        code = CLOCK_CODE[fast] + CLOCK_TAIL
        addr = BASE + len(self.mon.code)
        if addr + len(code) > 0x0800:
            raise ValueError("no room for the clock switch after the monitor")
        self.write(addr, code)
        self.tx(b"J" + struct.pack("<H", addr))
        self.fast = fast
        speed = "x2" if fast else "s3"
        self.rx = getattr(self.cbm, f"{speed}_read")
        self.tx = getattr(self.cbm, f"{speed}_write")
        self.mon.idle_s *= 0.5 if fast else 2.0
        self.response(3)
        self.mon.touch()

    @property
    def xread(self):
        """Address of xread (after xparm)."""
        return self.xparm + 4

    @property
    def xwrite(self):
        """Address of xwrite."""
        return self.xparm + 10

    def open(self):
        set_timeout = getattr(self.cbm, "set_timeout", None)
        if set_timeout:
            set_timeout(int(WATCHDOG_IDLE_S * 1000))
        self.cbm.iec_set(IEC_DATA)
        self.mon.wait(IEC_CLOCK, 0, "drive saw host DATA")
        self.cbm.iec_release(IEC_DATA)
        self.mon.wait(IEC_DATA, 0, "drive idle")

    def close(self):
        self.mon.wait(IEC_CLOCK, 1, "drive exit")
        self.cbm.iec_set(IEC_DATA)
        self.mon.wait(IEC_CLOCK, 0, "drive exit ack")
        self.cbm.iec_release(IEC_DATA)

    def alive(self):
        """The X idle bus shows nothing; a gone drive surfaces in send()."""
        return True

    def send(self, payload):
        """Send bytes; a drive that takes none of them has left the monitor."""
        try:
            self.tx(payload)
        except OpenCBMError as e:
            if getattr(e, "partial", b""):
                raise
            self.mon.running = False
            raise HandshakeTimeout("drive left the monitor") from e

    def params(self, addr, size, entry):
        """'W' addr/size into xparm, then 'J' entry (xread or xwrite)."""
        return (
            b"W"
            + struct.pack("<HHHH", self.xparm, 4, addr, size)
            + b"J"
            + (struct.pack("<H", entry))
        )

    def _checked(self, op):
        for _ in range(self.retries + 1):
            ok, value = op()
            if ok:
                return value
            self.rejects += 1
        raise ChecksumError(f"block failed after {self.retries} retries")

    def read(self, addr, size):
        """Read size bytes, CHUNK at a time, each checked."""
        out = bytearray()
        for a in range(addr, addr + size, CHUNK):
            n = min(CHUNK, addr + size - a)

            def op(a=a, n=n):
                got = self.mon.transact(self.params(a, n, self.xread), n + 3)
                return xsum(got[:n]) == tuple(got[n : n + 2]), got[:n]

            out += self._checked(op)
        return bytes(out)

    def write(self, addr, data):
        """Write bytes, CHUNK at a time, each checked."""
        for i in range(0, len(data), CHUNK):
            block = data[i : i + CHUNK]

            def op(a=addr + i, block=block):
                cmd = self.params(a, len(block), self.xwrite) + block
                return xsum(block) == tuple(self.mon.transact(cmd, 3)[:2]), None

            self._checked(op)
