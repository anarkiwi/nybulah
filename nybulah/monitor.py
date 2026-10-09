"""Host side of the drive-resident command loop (drive/monitor.s)."""

import os
import pathlib
import re
import struct
import time
import warnings
from importlib import resources

from .bus import DriveUnresponsive, answers, recover
from .fastx import SrqLink, XBLink, XLink
from .link import (
    BASE,
    CLOCK_HZ,
    WATCHDOG_IDLE_S,
    WATCHDOG_S,
    BusError,
    HandshakeTimeout,
    S1Link,
    S2Link,
)
from .opencbm import IEC_ATN, IEC_CLOCK, IEC_DATA, OpenCBMError

__all__ = [
    "BASE",
    "CLOCK_HZ",
    "WATCHDOG_IDLE_S",
    "WATCHDOG_S",
    "BusError",
    "BusNotIdle",
    "DriveUnresponsive",
    "HandshakeTimeout",
    "Monitor",
    "answers",
    "drivecode",
    "protocols",
    "recover",
    "resolve",
    "supported",
]


class BusNotIdle(BusError):
    """Another device holds a line the monitor needs."""


RECOVERABLE = (BusError, OpenCBMError)


def _drivecode_dirs():
    """The package's drivecode, then $NYBULAH_DRIVECODE (bins built in an image)."""
    yield resources.files("nybulah.drivecode")
    if os.environ.get("NYBULAH_DRIVECODE"):
        yield pathlib.Path(os.environ["NYBULAH_DRIVECODE"])


def drivecode(name):
    """Return an assembled drive program."""
    for d in _drivecode_dirs():
        f = d.joinpath(f"{name}.bin")
        if f.is_file():
            return f.read_bytes()
    raise FileNotFoundError(f"{name}.bin: build drive/ or set NYBULAH_DRIVECODE")


def _drivecode_names():
    return {f.name for d in _drivecode_dirs() if d.is_dir() for f in d.iterdir()}


def protocols():
    """Protocols with an assembled monitor available."""
    return tuple(
        sorted(
            {
                m[1]
                for n in _drivecode_names()
                if (m := re.match(r"monitor_(s\d+)\.bin$", n))
            }
        )
    )


def supported(cbm, protocol):
    """Whether a monitor binary exists for protocol and cbm can speak it."""
    if protocol not in protocols():
        return False
    probe = getattr(cbm, "supports", None)
    return probe(protocol) if probe else hasattr(cbm, f"{protocol}_read")


LINKS = {"s1": S1Link, "s2": S2Link, "s3": XLink, "s4": SrqLink}


def link_class(cbm, protocol):
    """The link for protocol: s3 uses burst X when adapter and drive code allow."""
    if protocol == "s3" and f"{XBLink.code_name}.bin" in _drivecode_names():
        probe = getattr(cbm, "supports", None)
        if probe("xb") if probe else hasattr(cbm, "xb_read"):
            return XBLink
    return LINKS[protocol]


def resolve(cbm, protocol):
    """protocol, or s3 when s4 is asked of an adapter without it (pre-v11 firmware)."""
    if protocol == "s4" and not supported(cbm, "s4") and supported(cbm, "s3"):
        warnings.warn("adapter lacks SRQ fast serial (firmware v11); using s3")
        return "s3"
    return protocol


class Monitor:
    """Upload, start and talk to the monitor on one drive.

    S1 and S3 (X, xum1541 firmware v9+) use only CLK/DATA and tolerate other
    DOS-idle drives on the bus; S2 strobes ATN, so every other drive must be
    off the bus or parked. A per-protocol link does handshakes and blocks. The drive
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
        protocol = resolve(cbm, protocol)
        if not supported(cbm, protocol):
            raise ValueError(f"protocol must be one of {protocols()}")
        self.cbm, self.dev, self.protocol = cbm, dev, protocol
        cls = link_class(cbm, protocol)
        name = getattr(cls, "code_name", f"monitor_{protocol}")
        self.code = code if code is not None else drivecode(name)
        self.link = cls(self)
        self.running = False
        self.timeout, self.idle_s, self.clock = timeout, idle_s, clock
        self._last = clock()

    def wait(self, line, state, step):
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
            lines = self.wait(IEC_CLOCK, 1, "drive ready")
        except HandshakeTimeout:
            lines = self.cbm.iec_poll()
            if not lines & (IEC_DATA | IEC_ATN):
                raise
        if lines & (IEC_DATA | IEC_ATN):
            raise BusNotIdle(f"bus not idle after M-E: bus=0x{lines:02x}")
        self.link.open()
        self.running = True
        self._last = self.clock()

    def alive(self):
        """Whether the drive still looks alive (no watchdog exit)."""
        return self.link.alive()

    def restart(self):
        """Stop (or recover) and start afresh, opening a new idle window."""
        try:
            self.stop()
        except RECOVERABLE:
            self.recover()
        self.start()

    def transact(self, payload, size=None):
        """Send one command and return its size-byte response (for links)."""
        if self.clock() - self._last > self.idle_s:
            self.restart()
        elif not self.alive():
            self.running = False
            raise HandshakeTimeout("drive left the monitor")
        self.link.send(payload)
        data = b"" if size is None else self.link.response(size)
        self._last = self.clock()
        return data

    def touch(self):
        """Restart the idle window after a command sent outside transact."""
        self._last = self.clock()

    def set_fast(self, fast):
        """Run a 1571 at 2 MHz (True) or 1 MHz; only the s3 and s4 links support it."""
        if not hasattr(self.link, "set_fast"):
            raise ValueError(f"{self.protocol} cannot change the drive clock")
        self.link.set_fast(fast)

    def stop(self):
        """Return the drive to DOS."""
        if not self.running:
            return
        if getattr(self.link, "fast", False):
            self.set_fast(False)
        self.running = False
        if not self.alive():
            self.cbm.iec_release(IEC_ATN | IEC_CLOCK | IEC_DATA)
            return
        try:
            self.link.send(b"Q")
        except HandshakeTimeout:
            self.cbm.iec_release(IEC_ATN | IEC_CLOCK | IEC_DATA)
            return
        self.link.close()

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

    def read(self, addr, size):
        """Read size (1..65536) bytes of drive memory."""
        return self.link.read(addr, size)

    def write(self, addr, data):
        """Write bytes to drive memory."""
        self.link.write(addr, bytes(data))

    def jsr(self, addr):
        """Call a drive subroutine; return its (A, X, Y)."""
        return tuple(self.transact(b"J" + struct.pack("<H", addr), 3))
