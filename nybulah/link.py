"""Monitor constants, bus errors and the S1/S2 links (handshakes and block commands)."""

import struct

from .opencbm import IEC_ATN, IEC_CLOCK, IEC_DATA

BASE = 0x0500
CLOCK_HZ = 1_000_000
WATCHDOG_S = 1.0
WATCHDOG_IDLE_S = 10.0


class BusError(IOError):
    """The IEC bus is not in the state the protocol requires."""


class HandshakeTimeout(BusError):
    """A bus line did not reach the expected state in time."""


class S1Link:
    """S1 handshakes: when idle the host holds CLK and the drive DATA."""

    idle_line = IEC_DATA

    def __init__(self, mon):
        self.mon, self.cbm = mon, mon.cbm
        self.rx = getattr(self.cbm, f"{mon.protocol}_read")
        self.tx = getattr(self.cbm, f"{mon.protocol}_write")

    def open(self):
        """Complete the startup handshake after the drive signalled ready."""
        self.cbm.iec_set(IEC_DATA)
        self.mon.wait(IEC_CLOCK, 0, "drive saw host DATA")
        self.cbm.iec_set(IEC_CLOCK)
        self.cbm.iec_release(IEC_DATA)
        self.mon.wait(IEC_DATA, 1, "drive idle")

    def close(self):
        """Exit handshake after 'Q'."""
        self.cbm.iec_release(IEC_CLOCK)
        self.mon.wait(IEC_DATA, 0, "drive exit")

    def alive(self):
        """Whether the drive still holds its idle line."""
        return bool(self.cbm.iec_poll() & self.idle_line)

    def send(self, payload):
        """Send command bytes."""
        self.tx(payload)

    def response(self, size):
        """Receive a command's response."""
        return self.rx(size)

    def read(self, addr, size):
        """Read drive memory with 'R'."""
        return self.mon.transact(b"R" + struct.pack("<HH", addr, size & 0xFFFF), size)

    def write(self, addr, data):
        """Write drive memory with 'W'."""
        self.mon.transact(b"W" + struct.pack("<HH", addr, len(data) & 0xFFFF) + data)


class S2Link(S1Link):
    """S2 handshakes: the host holds ATN when idle, the drive answers on CLK."""

    idle_line = IEC_CLOCK

    def open(self):
        self.cbm.iec_set(IEC_ATN)
        self.mon.wait(IEC_DATA, 0, "drive tracks ATN")

    def close(self):
        self.cbm.iec_release(IEC_ATN)
        self.mon.wait(IEC_CLOCK, 0, "drive exit")

    def response(self, size):
        self.mon.wait(IEC_CLOCK, 0, "drive send mode")
        data = self.rx(size)
        self.mon.wait(IEC_CLOCK, 1, "drive receive mode")
        return data
