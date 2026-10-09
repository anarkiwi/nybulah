"""Host side of the drive-resident command loop (drive/monitor.s)."""

import re
import struct
import time
from importlib import resources

from .opencbm import IEC_ATN, IEC_CLOCK, IEC_DATA, OpenCBMError

BASE = 0x0500
CLOCK_HZ = 1_000_000
WATCHDOG_S = 1.0
WATCHDOG_IDLE_S = 10.0
IDLE_LINE = {"s1": IEC_DATA, "s2": IEC_CLOCK}


class BusError(IOError):
    """The IEC bus is not in the state the protocol requires."""


class HandshakeTimeout(BusError):
    """A bus line did not reach the expected state in time."""


class BusNotIdle(BusError):
    """Another device holds a line the monitor needs."""


class DriveUnresponsive(IOError):
    """The drive did not answer its status channel after reset."""


RECOVERABLE = (BusError, OpenCBMError)


def drivecode(name):
    """Return an assembled drive program from the package."""
    return resources.files("nybulah.drivecode").joinpath(f"{name}.bin").read_bytes()


def protocols():
    """Protocols with an assembled monitor in the package."""
    files = resources.files("nybulah.drivecode").iterdir()
    return tuple(
        sorted(m[1] for f in files if (m := re.match(r"monitor_(\w+)\.bin$", f.name)))
    )


def answers(status):
    """Whether a status channel string came from a live DOS."""
    m = re.match(r"\s*(\d+)\s*,", status or "")
    return bool(m) and int(m.group(1)) != 99


def recover(cbm, dev, resets=2, timeout=3.0, poll=0.1):
    """Release the host's lines, reset the bus and wait until dev answers.

    Returns the drive's status string; a second reset covers drives that
    do not come back from the first. Raises DriveUnresponsive otherwise.
    """
    cbm.iec_release(IEC_ATN | IEC_CLOCK | IEC_DATA)
    status = ""
    for _ in range(resets):
        cbm.reset()
        deadline = time.monotonic() + timeout
        while True:
            try:
                status = cbm.status(dev)
            except OpenCBMError as e:
                status = str(e)
            if answers(status):
                return status
            if time.monotonic() > deadline:
                break
            time.sleep(poll)
    raise DriveUnresponsive(f"device {dev} silent after {resets} resets: {status!r}")


class Monitor:
    """Upload, start and talk to the monitor on one drive.

    S1 uses only CLK/DATA and tolerates other DOS-idle drives on the bus; S2
    strobes ATN, so every other drive must be off the bus or parked. The drive
    returns to DOS after WATCHDOG_S stalled mid-command or WATCHDOG_IDLE_S
    between commands; a command after idle_s idle restarts the monitor first
    (a 1571 at 2 MHz halves the drive's windows, so halve idle_s there).
    """

    def __init__(
        self,
        cbm,
        dev,
        protocol="s1",
        code=None,
        timeout=2.0,
        idle_s=0.9 * WATCHDOG_IDLE_S,
        clock=time.monotonic,
    ):
        if protocol not in protocols() or not hasattr(cbm, f"{protocol}_read"):
            raise ValueError(f"protocol must be one of {protocols()}")
        self.cbm, self.dev, self.protocol = cbm, dev, protocol
        self.code = code if code is not None else drivecode(f"monitor_{protocol}")
        self._read = getattr(cbm, f"{protocol}_read")
        self._write = getattr(cbm, f"{protocol}_write")
        self.running = False
        self.timeout, self.idle_s, self.clock = timeout, idle_s, clock
        self._last = clock()

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
        try:
            lines = self._wait(IEC_CLOCK, 1, "drive ready")
        except HandshakeTimeout:
            lines = self.cbm.iec_poll()
            if not lines & (IEC_DATA | IEC_ATN):
                raise
        if lines & (IEC_DATA | IEC_ATN):
            raise BusNotIdle(f"bus not idle after M-E: bus=0x{lines:02x}")
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
        self._last = self.clock()

    def alive(self):
        """Whether the drive still holds its idle line (no watchdog exit)."""
        return bool(self.cbm.iec_poll() & IDLE_LINE[self.protocol])

    def restart(self):
        """Stop (or recover) and start afresh, opening a new idle window."""
        try:
            self.stop()
        except RECOVERABLE:
            self.recover()
        self.start()

    def _transact(self, payload, size=None):
        if self.clock() - self._last > self.idle_s:
            self.restart()
        elif not self.alive():
            self.running = False
            raise HandshakeTimeout("drive left the monitor")
        self._write(payload)
        data = b"" if size is None else self._response(size)
        self._last = self.clock()
        return data

    def stop(self):
        """Return the drive to DOS."""
        if not self.running:
            return
        self.running = False
        if not self.alive():
            self.cbm.iec_release(IEC_ATN | IEC_CLOCK | IEC_DATA)
            return
        self._write(b"Q")
        if self.protocol == "s1":
            self.cbm.iec_release(IEC_CLOCK)
            self._wait(IEC_DATA, 0, "drive exit")
        else:
            self.cbm.iec_release(IEC_ATN)
            self._wait(IEC_CLOCK, 0, "drive exit")

    def recover(self):
        """Abandon the session and bring the drive back to DOS."""
        self.running = False
        return recover(self.cbm, self.dev)

    def __enter__(self):
        try:
            self.start()
        except RECOVERABLE as e:
            setattr(e, "recovered", self.recover())
            raise
        return self

    def __exit__(self, exc_type, exc, tb):
        if isinstance(exc, RECOVERABLE):
            setattr(exc, "recovered", self.recover())
        else:
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
        return self._transact(b"R" + struct.pack("<HH", addr, size & 0xFFFF), size)

    def write(self, addr, data):
        """Write bytes to drive memory."""
        data = bytes(data)
        self._transact(b"W" + struct.pack("<HH", addr, len(data) & 0xFFFF) + data)

    def jsr(self, addr):
        """Call a drive subroutine; return its (A, X, Y)."""
        return tuple(self._transact(b"J" + struct.pack("<H", addr), 3))
