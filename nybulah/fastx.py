"""Host side of the X transport: monitor_s3 with checked block reads and writes."""

import struct
import time

from .monitor import BASE, HandshakeTimeout, drivecode, recover
from .opencbm import IEC_ATN, IEC_CLOCK, IEC_DATA, OpenCBMError
from .simx import xsum

TAG = b"NYBX"
CHUNK = 0x1000


class ChecksumError(IOError):
    """A block failed its check after every retry."""


def xio(cbm, fast=False):
    """(read, write) callables for the X transport on cbm (x2_* in 2 MHz mode)."""
    name = "x2" if fast else "x"
    if hasattr(cbm, f"{name}_read"):
        return getattr(cbm, f"{name}_read"), getattr(cbm, f"{name}_write")
    # pylint: disable-next=protected-access
    return (lambda n: cbm._read_n(name, n)), (lambda d: cbm._write_n(name, d))


class XLink:
    """Upload monitor_s3, then move checked blocks with xread/xwrite via 'J'.

    A failed check is retried; a transport error abandons the session (the drive
    returns to DOS through its watchdog or a reset) and restarts it once per retry.
    """

    def __init__(self, cbm, dev, fast=False, code=None, timeout=2.0, retries=3):
        self.cbm, self.dev, self.timeout, self.retries = cbm, dev, timeout, retries
        self.code = code if code is not None else drivecode("monitor_s3")
        at = self.code.find(TAG)
        if at < 0:
            raise ValueError("monitor_s3 lacks the NYBX tag")
        self.xparm = BASE + at + len(TAG)
        self.rx, self.tx = xio(cbm, fast)
        self.running = False
        self.restarts = self.rejects = 0

    def _wait(self, line, state, step):
        deadline = time.monotonic() + self.timeout
        while bool((lines := self.cbm.iec_poll()) & line) != bool(state):
            if time.monotonic() > deadline:
                raise HandshakeTimeout(f"{step}: bus=0x{lines:02x}")
        return lines

    def _handshake(self, step):
        self._wait(IEC_CLOCK, 1, step)
        self.cbm.iec_set(IEC_DATA)
        self._wait(IEC_CLOCK, 0, f"{step} ack")
        self.cbm.iec_release(IEC_DATA)
        self._wait(IEC_DATA, 0, f"{step} idle")

    def start(self):
        """Run monitor_s3 on the drive; the bus idles with both lines released."""
        self.cbm.upload(self.dev, BASE, self.code)
        self.cbm.command(self.dev, b"M-E" + struct.pack("<H", BASE))
        self.cbm.iec_release(IEC_CLOCK | IEC_DATA | IEC_ATN)
        self._handshake("drive ready")
        self.running = True

    def stop(self):
        """Return the drive to DOS."""
        if self.running:
            self.running = False
            self.tx(b"Q")
            self._handshake("drive exit")

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        if isinstance(exc, OpenCBMError):
            self.running = False
            recover(self.cbm, self.dev)
        else:
            self.stop()

    def _restart(self):
        self.running = False
        self.restarts += 1
        recover(self.cbm, self.dev)
        self.start()

    def _checked(self, op):
        for _ in range(self.retries + 1):
            try:
                ok, value = op()
            except OpenCBMError:
                self._restart()
                continue
            if ok:
                return value
            self.rejects += 1
        raise ChecksumError(f"block failed after {self.retries} retries")

    @property
    def xread(self):
        """Address of xread (after xparm)."""
        return self.xparm + 4

    @property
    def xwrite(self):
        """Address of xwrite."""
        return self.xparm + 10

    def params(self, addr, size, entry):
        """Store addr/size in xparm and call entry (xread or xwrite) via 'J'."""
        self.tx(
            b"W"
            + struct.pack("<HHHH", self.xparm, 4, addr, size)
            + b"J"
            + struct.pack("<H", entry)
        )

    def read(self, addr, size):
        """Read size bytes of drive memory, CHUNK at a time, each checked."""
        out = bytearray()
        for a in range(addr, addr + size, CHUNK):
            n = min(CHUNK, addr + size - a)

            def op(a=a, n=n):
                self.params(a, n, self.xread)
                got = self.rx(n + 3)
                return xsum(got[:n]) == tuple(got[n : n + 2]), got[:n]

            out += self._checked(op)
        return bytes(out)

    def write(self, addr, data):
        """Write bytes to drive memory, CHUNK at a time, each checked."""
        data = bytes(data)
        for i in range(0, len(data), CHUNK):
            block = data[i : i + CHUNK]

            def op(a=addr + i, block=block):
                self.params(a, len(block), self.xwrite)
                self.tx(block)
                return xsum(block) == tuple(self.rx(3)[:2]), None

            self._checked(op)

    def jsr(self, addr):
        """Call a drive subroutine; return its (A, X, Y)."""
        self.tx(b"J" + struct.pack("<H", addr))
        return tuple(self.rx(3))
