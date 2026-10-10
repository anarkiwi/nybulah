"""Host side of the drive-resident command loop (drive/monitor.s)."""

import contextlib
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
from .ramprobe import identify_model

__all__ = [
    "BASE",
    "CLOCK_HZ",
    "WATCHDOG_IDLE_S",
    "WATCHDOG_S",
    "BusError",
    "BusNotIdle",
    "DriveLost",
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


class DriveLost(BusError):
    """The drive left the monitor (stuck in a routine, or idled out) while a session
    held DOS's zero page; DOS cannot answer its device number then (its listen and
    talk addresses are overwritten), so only a bus reset brings it back."""


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


def model_of(cbm, dev):
    """The drive model, or None when the adapter cannot identify it."""
    try:
        return identify_model(cbm, dev)
    except (AttributeError, ValueError, OpenCBMError):
        return None


def code_suffix(model):
    """Drive code built for model (monitor.s -D M1581)."""
    return "_1581" if model == "1581" else ""


def link_class(cbm, protocol, model=None):
    """The link for protocol: s3 uses burst X when adapter and drive code allow; a
    1581 has burst X only (its per-byte X monitor does not fit beside its CIA code)."""
    suffix = code_suffix(model)
    if protocol == "s3" and f"{XBLink.code_name}{suffix}.bin" in _drivecode_names():
        probe = getattr(cbm, "supports", None)
        if probe("xb") if probe else hasattr(cbm, "xb_read"):
            return XBLink
    if protocol == "s3" and suffix:
        raise ValueError("s3 on a 1581 needs burst X (xum1541 firmware v10)")
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
    (a 1571 at 2 MHz halves the drive's windows, so halve idle_s there). The
    identified model picks the drive code; a 1581's runs at 2 MHz throughout.
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
        self.model = model_of(cbm, dev)
        cls = link_class(cbm, protocol, self.model)
        name = getattr(cls, "code_name", f"monitor_{protocol}")
        name += code_suffix(self.model)
        self.code = code if code is not None else drivecode(name)
        self.link = cls(self)
        self.running = False
        self.holding = None
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
        """Send one command and return its size-byte response (for links).

        While ``holding`` (a session's state is in DOS's zero page) a drive that
        idled out or does not complete the command is lost (:meth:`guard`).
        """
        idle = self.clock() - self._last
        if idle > self.idle_s:
            if self.holding is not None:
                self.lost(
                    f"no command for {idle:.1f} s, past the drive's idle window",
                    None,
                )
            self.restart()
        with self.guard(self.describe(payload)):
            if not self.alive():
                self.running = False
                raise HandshakeTimeout("drive left the monitor")
            self.link.send(payload)
            data = b"" if size is None else self.link.response(size)
        self._last = self.clock()
        return data

    def describe(self, payload):
        """A monitor command for reports, a 'J' with the holder's name for the
        routine it calls: 'J $0303 (read)', 'W $0060+17'."""
        op = payload[:1].decode("latin-1")
        if len(payload) < 3:
            return op
        addr = struct.unpack_from("<H", payload, 1)[0]
        text = f"{op} ${addr:04X}"
        if op in "RW" and len(payload) >= 5:
            text += f"+{struct.unpack_from('<H', payload, 3)[0]}"
        routine = getattr(self.holding, "routines", {}).get(addr) if op == "J" else None
        return f"{text} ({routine})" if routine else text

    @contextlib.contextmanager
    def guard(self, what):
        """Run the transfers of ``what``; while ``holding``, a bus or adapter error
        means the drive is lost (:meth:`lost`)."""
        try:
            yield
        except DriveLost:
            raise
        except RECOVERABLE as e:
            if self.holding is None:
                raise
            self.lost(f"{what} failed ({e})", e)

    def lost(self, why, cause):
        """Bring back a drive that left the monitor while ``holding`` by a bus
        reset (DOS no longer matches its device number), end the session and
        raise DriveLost saying why."""
        holder, self.holding = self.holding, None
        getattr(self.link, "drive_reset", lambda: None)()
        try:
            status = self.recover()
        except DriveUnresponsive as e:
            raise DriveLost(
                f"device {self.dev}: {why} while {holder} held DOS's zero page; {e}"
            ) from e
        error = DriveLost(
            f"device {self.dev}: {why} while {holder} held DOS's zero page; bus"
            f" reset, drive status {status}"
        )
        setattr(error, "recovered", status)
        raise error from cause

    def touch(self):
        """Restart the idle window after a command sent outside transact."""
        self._last = self.clock()

    def set_fast(self, fast):
        """Run a 1571 at 2 MHz (True) or 1 MHz; only the s3 and s4 links support it.
        A 1581 always runs at 2 MHz: True is accepted, False refused."""
        if self.model == "1581":
            if not fast:
                raise ValueError("a 1581 runs at 2 MHz only")
            return
        if not hasattr(self.link, "set_fast"):
            raise ValueError(f"{self.protocol} cannot change the drive clock")
        self.link.set_fast(fast)

    def stop(self):
        """Return the drive to DOS; after s4, UNLISTEN clears the fast host flag SRQ
        traffic leaves in idle 1571s and 1581s (docs/protocol.md)."""
        if not self.running:
            return
        if getattr(self.link, "fast", False) and self.model != "1581":
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
        if self.protocol == "s4" and hasattr(self.cbm, "unlisten"):
            self.cbm.unlisten()

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
        if isinstance(exc, DriveLost):
            return
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
