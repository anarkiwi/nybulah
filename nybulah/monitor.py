"""Host side of the drive-resident command loop (drive/monitor.s)."""

import struct
import time
from importlib import resources

from .opencbm import IEC_ATN, IEC_CLOCK, IEC_DATA

BASE = 0x0500
PROTOCOLS = ("s1", "s2")


class HandshakeTimeout(IOError):
    """A bus line did not reach the expected state in time."""


def drivecode(name):
    """Return an assembled drive program from the package."""
    return resources.files("nybulah.drivecode").joinpath(f"{name}.bin").read_bytes()


class Monitor:
    """Upload, start and talk to the monitor on one drive.

    S1 uses only CLK/DATA and tolerates other DOS-idle drives on the bus; S2
    strobes ATN, so every other drive must be off the bus or parked.
    """

    def __init__(self, cbm, dev, protocol="s1", code=None, timeout=2.0):
        if protocol not in PROTOCOLS:
            raise ValueError(f"protocol must be one of {PROTOCOLS}")
        self.cbm, self.dev, self.protocol = cbm, dev, protocol
        self.code = code if code is not None else drivecode(f"monitor_{protocol}")
        self._read = getattr(cbm, f"{protocol}_read")
        self._write = getattr(cbm, f"{protocol}_write")
        self.running = False
        self.timeout = timeout

    def _wait(self, line, state, step):
        """Poll until line is asserted (state=1) or released (state=0)."""
        deadline = time.monotonic() + self.timeout
        while True:
            lines = self.cbm.iec_poll()
            if bool(lines & line) == bool(state):
                return lines
            if time.monotonic() > deadline:
                raise HandshakeTimeout(f"{step}: bus=0x{lines:02x}")

    def start(self):
        """Upload the monitor, execute it and complete the startup handshake."""
        self.cbm.upload(self.dev, BASE, self.code)
        self.cbm.command(self.dev, b"M-E" + struct.pack("<H", BASE))
        self.cbm.iec_release(IEC_CLOCK | IEC_DATA | IEC_ATN)
        self._wait(IEC_CLOCK, 1, "drive ready")
        if self.protocol == "s1":
            self.cbm.iec_set(IEC_DATA)
            self._wait(IEC_CLOCK, 0, "drive saw host DATA")
            self.cbm.iec_set(IEC_CLOCK)
            self.cbm.iec_release(IEC_DATA)
            self._wait(IEC_DATA, 1, "drive idle")
        else:
            self.cbm.iec_set(IEC_ATN)
            self._wait(IEC_DATA, 0, "drive tracks ATN")
        self.running = True

    def stop(self):
        """Return the drive to DOS."""
        if not self.running:
            return
        self._write(b"Q")
        if self.protocol == "s1":
            self.cbm.iec_release(IEC_CLOCK)
            self._wait(IEC_DATA, 0, "drive exit")
        else:
            self.cbm.iec_release(IEC_ATN)
            self._wait(IEC_CLOCK, 0, "drive exit")
        self.running = False

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()

    def _response(self, size):
        if self.protocol == "s1":
            return self._read(size)
        self._wait(IEC_CLOCK, 0, "drive send mode")
        data = self._read(size)
        self._wait(IEC_CLOCK, 1, "drive receive mode")
        return data

    def read(self, addr, size):
        """Read size (1..65536) bytes of drive memory."""
        self._write(b"R" + struct.pack("<HH", addr, size & 0xFFFF))
        return self._response(size)

    def write(self, addr, data):
        """Write bytes to drive memory."""
        data = bytes(data)
        self._write(b"W" + struct.pack("<HH", addr, len(data) & 0xFFFF) + data)

    def jsr(self, addr):
        """Call a drive subroutine; return its (A, X, Y)."""
        self._write(b"J" + struct.pack("<H", addr))
        return tuple(self._response(3))
