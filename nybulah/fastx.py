"""S3/S4 links for Monitor: the X and SRQ transports with checked block transfers.

XLink speaks firmware v9 X (per-byte go/SYNC, drive/proto_x.inc); XBLink the v10
burst form (drive/proto_xb.inc), chosen whenever the adapter supports it; SrqLink the
v11 1571 SRQ fast serial (drive/proto_srq.inc) with the same commands and bursts.
"""

import struct

import numpy as np

from .link import BASE, WATCHDOG_IDLE_S, BusError, HandshakeTimeout, S1Link
from .opencbm import IEC_CLOCK, IEC_DATA, OpenCBMError
from .ramprobe import identify_model

TAG = b"NYBX"
XB_TAG = b"NYXB"
SRQ_TAG = b"NYSR"
CHUNK = 0x1000
XB_CHUNK = 0x2000
XB_BURST = 64
VIA1PA = 0x1801
PA_2MHZ = 0x20
CLOCK_CODE = {
    True: bytes([0xA9, PA_2MHZ, 0x0D]) + struct.pack("<H", VIA1PA),
    False: bytes([0xA9, 0xFF ^ PA_2MHZ, 0x2D]) + struct.pack("<H", VIA1PA),
}
CLOCK_TAIL = bytes([0x8D]) + struct.pack("<H", VIA1PA) + b"\x60"


def xsum(data):
    """Block check of sendblk/recvblk: (s1, s2) over bytes or per-byte addends."""
    if isinstance(data, np.ndarray):
        b = data.astype(np.int64)
    else:
        b = np.frombuffer(bytes(data), np.uint8)
    s = np.cumsum(b, dtype=np.int64)
    if not s.size:
        return 0, 0
    return int(s[-1] & 0xFF), int(((s & 0xFF).sum() + (s[-1] >> 8)) & 0xFF)


def xbsum(received, sent=b""):
    """Burst check: received bytes add b, sent bytes add b plus bit 3 of b (the
    carry the drive's send loop leaves)."""
    r = np.frombuffer(bytes(received), np.uint8).astype(np.int64)
    t = np.frombuffer(bytes(sent), np.uint8).astype(np.int64)
    return xsum(np.concatenate((r, t + (t >> 3 & 1))))


class ChecksumError(BusError):
    """A block failed its check after every retry."""


class XLink(S1Link):
    """Idle bus with both lines released; blocks go through xread/xwrite via 'J'.

    Each block is checked against the drive's (s1, s2) and repeated up to
    retries times on a mismatch.
    """

    idle_line = 0
    retries = 3
    code_name = "monitor_s3"
    speeds = {False: "s3", True: "x2"}

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
        self.send(b"J" + struct.pack("<H", addr))
        self._use(fast)
        self.response(3)
        self.mon.touch()

    def _use(self, fast):
        """Transfer at the drive's clock; its watchdog windows scale with it."""
        if fast != self.fast:
            self.mon.idle_s *= 0.5 if fast else 2.0
        self.fast = fast
        speed = self.speeds[fast]
        self.rx = getattr(self.cbm, f"{speed}_read")
        self.tx = getattr(self.cbm, f"{speed}_write")

    def drive_reset(self):
        """After a drive reset: a 1571 runs at 1 MHz again."""
        if getattr(self.mon, "model", None) != "1581":
            self._use(False)

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


class XBLink(XLink):
    """Firmware v10 burst X: 5-byte command bursts (addr, len, op), checked blocks.

    The adapter cuts a transfer into XB_BURST-byte bursts from its start, the drive at
    XB_BURST-aligned addresses, so an unaligned block moves its head separately.
    """

    code_name = "monitor_xb"
    speeds = {False: "xb", True: "xb2"}
    chunk = XB_CHUNK
    tag = XB_TAG

    def __init__(self, mon):
        if self.tag not in mon.code:
            raise ValueError(f"monitor code lacks the {self.tag.decode()} tag")
        super(XLink, self).__init__(mon)  # pylint: disable=bad-super-call
        self.fast = getattr(mon, "model", None) == "1581"
        speed = self.speeds[self.fast]
        self.rx = getattr(self.cbm, f"{speed}_read")
        self.tx = getattr(self.cbm, f"{speed}_write")
        self.rejects = 0

    @staticmethod
    def check(cmd, received=b"", sent=b""):
        """Expected (s1, s2) over a command and its data."""
        return xbsum(cmd + received, sent)

    @staticmethod
    def packet(payload):
        """5-byte command burst for a v9-style payload (op, then addr and len)."""
        return payload[1:5].ljust(4, b"\0") + payload[:1]

    def send(self, payload):
        super().send(self.packet(payload))

    def _split(self, addr, n):
        head = min(n, XB_BURST - addr % XB_BURST)
        return [m for m in (head, n - head) if m]

    def _block(self, op, addr, n, data=None):
        payload = op + struct.pack("<HH", addr, n)
        cmd = self.packet(payload)
        self.mon.transact(payload)
        with self.mon.guard(self.mon.describe(payload)):
            if data is None:
                got = b"".join(self.rx(m) for m in self._split(addr, n))
                check = self.check(cmd, sent=got)
            else:
                got = None
                at = 0
                for m in self._split(addr, n):
                    self.tx(data[at : at + m])
                    at += m
                check = self.check(cmd, data)
            reply = self.rx(3)
        self.mon.touch()
        return check == tuple(reply[:2]), got

    def read(self, addr, size):
        """Read size bytes, chunk at a time, each checked."""
        out = bytearray()
        for a in range(addr, addr + size, self.chunk):
            n = min(self.chunk, addr + size - a)
            out += self._checked(lambda a=a, n=n: self._block(b"R", a, n))
        return bytes(out)

    def write(self, addr, data):
        """Write bytes, chunk at a time, each checked."""
        for i in range(0, len(data), self.chunk):
            block = bytes(data[i : i + self.chunk])
            self._checked(lambda a=addr + i, b=block: self._block(b"W", a, len(b), b))


class SrqLink(XBLink):
    """Firmware v11 SRQ fast serial on a 1571 or 1581: XBLink's commands, bursts and
    chunks over the CIA shift register; checks are xsum in both directions."""

    code_name = "monitor_s4"
    speeds = {False: "srq", True: "srq2"}
    tag = SRQ_TAG

    def __init__(self, mon):
        model = getattr(mon, "model", None) or identify_model(mon.cbm, mon.dev)
        if model not in ("1571", "1581"):
            raise ValueError(
                f"device {mon.dev}: s4 (SRQ fast serial) needs a 1571 or 1581"
            )
        super().__init__(mon)

    @staticmethod
    def check(cmd, received=b"", sent=b""):
        return xsum(cmd + received + sent)
