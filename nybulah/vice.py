"""VICE as a drive test bench: x64sc or x128 headless with true drive emulation.

The emulator runs as a separate program and is driven over its binary monitor (VICE
manual, "Binary Monitor"); :class:`DriveMonitor` stands in for the host monitor.
"""

import contextlib
import os
import pathlib
import shutil
import socket
import struct
import subprocess
import tempfile
import time

import numpy as np

STX, API = 0x02, 0x02
HEADER = struct.Struct("<BBIBBI")  # STX, API, length, type, error, request id
REQUEST = struct.Struct("<BBII")  # STX, API, length, request id
EVENT = 0xFFFFFFFF
MEM_GET, MEM_SET = 0x01, 0x02
CHECKPOINT_SET, CHECKPOINT_DELETE = 0x12, 0x13
REGISTERS_GET, REGISTERS_SET = 0x31, 0x32
KEYBOARD_FEED = 0x72
PING, BANKS, REGISTERS_AVAILABLE, INFO, HISTORY = 0x81, 0x82, 0x83, 0x85, 0x86
EXIT, QUIT = 0xAA, 0xBB
R_CHECKPOINT, R_JAM, R_STOPPED, R_RESUMED = 0x11, 0x61, 0x62, 0x63
OP_LOAD, OP_STORE, OP_EXEC = 0x01, 0x02, 0x04
MAIN = 0
HISTORY_MAX = 0xFFFF
MEM_CHUNK = 0x8000
DRIVE_HZ = {"1541": 1_000_000, "1571": 1_000_000, "1581": 2_000_000}
DRIVE_TYPE = {"1541": 1541, "1571": 1571, "1581": 1581}
JMP, INX = 0x4C, 0xE8
# delay: ldx #0; dex; bne *-1; dey; bne delay; rts: 2 + 256 * 5 - 1 + 2 + 3 per Y
DELAY = bytes([0xA2, 0x00, 0xCA, 0xD0, 0xFD, 0x88, 0xD0, 0xF8, 0x60])
DELAY_LOOP = 2 + 256 * 5 - 1 + 2 + 3
FLAG_I = 0x04
STACK = 0x0100
IRQ_VECTOR = 0xFFFE
TCP_LISTEN = "0A"
PARK = 0x0500  # the monitor's own load address: free while it is not loaded
CIA1581 = 0x4000
# drive/monitor.s ciasave: timer A latch 1 continuous, timer B counting its underflows
CIA1581_SETUP = (
    (0x0E, 0x10),
    (0x0F, 0x10),
    (0x04, 0x01),
    (0x05, 0x00),
    (0x06, 0xFF),
    (0x07, 0xFF),
    (0x0E, 0x11),
    (0x0F, 0x51),
    (0x0D, 0x82),
)
HISTORY_DTYPE = np.dtype(
    [
        ("clock", np.uint64),
        ("pc", np.uint16),
        ("a", np.uint8),
        ("x", np.uint8),
        ("y", np.uint8),
        ("sp", np.uint8),
        ("fl", np.uint8),
        ("op", np.uint8, 3),
    ]
)
LOADS = {"a": (0xAD, 0xBD, 0xB9), "x": (0xAE, 0xBE), "y": (0xAC, 0xBC)}
STORES = {"a": (0x8D, 0x9D, 0x99), "x": (0x8E,), "y": (0x8C,)}
RX_BASE, RX_DATA, RX_LINES = 0x1300, 0x2000, 0x7000  # drive/vicerx.s


class ViceError(RuntimeError):
    """The emulator refused a command, exited or did not answer."""


def memspace(unit):
    """Binary monitor memspace of a drive unit (8..11), or MAIN for None."""
    if unit is None:
        return MAIN
    if not 8 <= unit <= 11:
        raise ValueError(f"unit {unit} is not 8..11")
    return unit - 7


def request(command, body, request_id):
    """One framed command."""
    return REQUEST.pack(STX, API, len(body), request_id) + bytes([command]) + body


def checkpoint_body(start, end, op=OP_EXEC, stop=True, temporary=False, space=MAIN):
    """MON_CMD_CHECKPOINT_SET body for [start, end] in a memspace."""
    return struct.pack("<HHBBBBB", start, end, stop, True, op, temporary, space)


def _items(body, at=2):
    """The (offset, size) of each item of a counted array of sized items."""
    (count,) = struct.unpack_from("<H", body)
    for _ in range(count):
        yield at + 1, body[at]
        at += body[at] + 1


def parse_registers(body):
    """MON_RESPONSE_REGISTER_INFO body: {register id: value}."""
    return dict(struct.unpack_from("<BH", body, at) for at, _ in _items(body))


def parse_names(body):
    """MON_RESPONSE_REGISTERS_AVAILABLE body: {name: register id}."""
    return {
        body[at + 3 : at + 3 + body[at + 2]].decode(): body[at]
        for at, _ in _items(body)
    }


def parse_banks(body):
    """MON_RESPONSE_BANKS_AVAILABLE body: {name: bank id}."""
    out = {}
    for at, _ in _items(body):
        bank, n = struct.unpack_from("<HB", body, at)
        out[body[at + 3 : at + 3 + n].decode()] = bank
    return out


def parse_history(body, names):
    """MON_RESPONSE_CPUHISTORY_GET body as HISTORY_DTYPE rows, oldest first; the
    registers are those at each instruction's start (a load shows in the next row)."""
    (count,) = struct.unpack_from("<I", body)
    out = np.zeros(count, HISTORY_DTYPE)
    if not count:
        return out
    size = body[4] + 1
    rows = np.frombuffer(body, np.uint8, count * size, 4).reshape(count, size)
    ids = {v: k.lower() for k, v in names.items()}
    at = 3
    for _ in range(int(struct.unpack_from("<H", body, 5)[0])):
        rs, field = int(rows[0, at]), ids.get(int(rows[0, at + 1]))
        if field in ("pc", "a", "x", "y", "sp", "fl"):
            value = rows[:, at + 2].astype(np.uint16)
            out[field] = value | rows[:, at + 3].astype(np.uint16) << 8
        at += rs + 1
    out["clock"] = rows[:, at : at + 8].copy().view("<u8").ravel()
    n = min(int(rows[0, at + 8]), 3)
    out["op"][:, :n] = rows[:, at + 9 : at + 9 + n]
    return out


def _absolute_opcodes():
    """Opcodes with a two-byte address operand (absolute, absolute X or Y)."""
    table = np.zeros(256, bool)
    low = np.arange(256) & 0x1F
    table[np.isin(low, (0x0C, 0x0D, 0x0E, 0x19, 0x1D, 0x1E))] = True
    table[[0x20, 0xBC, 0xBE]] = True
    table[[0x1C, 0x3C, 0x5C, 0x7C, 0xDC, 0xFC, 0x9C, 0x9E]] = False
    table[[0x0C]] = False
    return table


ABSOLUTE = _absolute_opcodes()


def operands(history):
    """The address operand of each row, -1 where the opcode has none."""
    op = history["op"].astype(np.int64)
    return np.where(ABSOLUTE[op[:, 0]], op[:, 1] | op[:, 2] << 8, -1)


def accesses(history, lo, hi):
    """Rows touching [lo, hi]: (row, address, value) with the value loaded (the next
    row's register) or stored, -1 for any other access (bit, inc, jmp ...)."""
    addr = operands(history)
    at = np.flatnonzero((addr >= lo) & (addr <= hi))
    at = at[at + 1 < len(history)]
    op = history["op"][at, 0]
    value = np.full(len(at), -1, np.int64)
    for reg, codes in LOADS.items():
        hit = np.isin(op, codes)
        value[hit] = history[reg][at + 1][hit]
    for reg, codes in STORES.items():
        hit = np.isin(op, codes)
        value[hit] = history[reg][at][hit]
    return at, addr[at], value


class BinaryMonitor:
    """A binary monitor client; stop, resume and checkpoint events are tracked as
    responses arrive."""

    def __init__(self, sock, timeout=30.0):
        self.sock, self.timeout = sock, timeout
        self.sock.settimeout(timeout)
        self._next = 1
        self.stopped = self.pc = None
        self.hits = []
        self.jammed = False
        self._buf = b""

    def _recv(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(1 << 16)
            if not chunk:
                raise ViceError("binary monitor connection closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def read_response(self):
        """(type, error, request id, body) of the next response."""
        stx, _, length, kind, error, rid = HEADER.unpack(self._recv(HEADER.size))
        if stx != STX:
            raise ViceError(f"bad response start ${stx:02X}")
        body = self._recv(length)
        if kind in (R_STOPPED, R_JAM):
            self.stopped, self.jammed = True, kind == R_JAM
            self.pc = struct.unpack_from("<H", body)[0] if len(body) >= 2 else self.pc
        elif kind == R_RESUMED:
            self.stopped = False
        elif kind == R_CHECKPOINT and rid == EVENT:
            self.hits.append(struct.unpack_from("<I", body)[0])
        return kind, error, rid, body

    def command(self, command, body=b""):
        """Send a command, return its response body; ViceError on an error code."""
        rid = self._next
        self._next = self._next % 0x7FFFFFFF + 1
        self.sock.sendall(request(command, bytes(body), rid))
        while True:
            _, error, got, payload = self.read_response()
            if got != rid:
                continue
            if error:
                raise ViceError(f"command ${command:02X}: error ${error:02X}")
            return payload

    def wait_stopped(self, timeout=None):
        """Read events until the emulator stops; False when timeout passes first."""
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        try:
            while not self.stopped:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self.sock.settimeout(left)
                try:
                    self.read_response()
                except socket.timeout:
                    return False
        finally:
            self.sock.settimeout(self.timeout)
        return True

    def resume(self):
        """Leave the monitor; the emulator runs to a checkpoint or the next command."""
        self.command(EXIT)
        self.stopped = False

    def stop(self):
        """Stop the emulator (any command does)."""
        self.command(PING)
        self.stopped = True

    def mem_get(self, start, n, space=MAIN, bank=0, effects=False):
        """n bytes from start in a memspace and bank."""
        out = bytearray()
        for at in range(start, start + n, MEM_CHUNK):
            end = min(at + MEM_CHUNK, start + n) - 1
            body = struct.pack("<BHHBH", effects, at, end, space, bank)
            out += self.command(MEM_GET, body)[2:]
        return bytes(out)

    def mem_set(self, start, data, space=MAIN, bank=0, effects=False):
        """Write data at start in a memspace and bank."""
        data = bytes(data)
        for at in range(0, len(data), MEM_CHUNK):
            part = data[at : at + MEM_CHUNK]
            end = start + at + len(part) - 1
            body = struct.pack("<BHHBH", effects, start + at, end, space, bank)
            self.command(MEM_SET, body + part)

    def checkpoint(self, start, end=None, **kw):
        """Set a checkpoint (checkpoint_body keywords); returns its number."""
        body = checkpoint_body(start, start if end is None else end, **kw)
        return struct.unpack_from("<I", self.command(CHECKPOINT_SET, body))[0]

    def delete(self, number):
        """Delete a checkpoint."""
        self.command(CHECKPOINT_DELETE, struct.pack("<I", number))

    def registers_available(self, space=MAIN):
        """{name: register id}."""
        return parse_names(self.command(REGISTERS_AVAILABLE, bytes([space])))

    def banks(self):
        """{name: bank id}."""
        return parse_banks(self.command(BANKS))

    def registers(self, space=MAIN):
        """{register id: value}."""
        return parse_registers(self.command(REGISTERS_GET, bytes([space])))

    def set_registers(self, values, space=MAIN):
        """Set {register id: value}."""
        items = b"".join(struct.pack("<BBH", 3, k, v) for k, v in values.items())
        self.command(REGISTERS_SET, struct.pack("<BH", space, len(values)) + items)

    def history(self, count, names, space=MAIN):
        """The last count (at most HISTORY_MAX) instructions of a memspace's CPU."""
        body = self.command(HISTORY, struct.pack("<BI", space, min(count, HISTORY_MAX)))
        return parse_history(body, names)

    def feed(self, text):
        """Type PETSCII text into the keyboard buffer."""
        raw = text.encode("latin-1")
        self.command(KEYBOARD_FEED, bytes([len(raw)]) + raw)

    def info(self):
        """The emulator version as a tuple."""
        body = self.command(INFO)
        return tuple(body[1 : 1 + body[0]])

    def quit(self):
        """Quit the emulator."""
        with contextlib.suppress(OSError, ViceError):
            self.command(QUIT)


def available(machine="x64sc"):
    """Whether the emulator binary is on PATH."""
    return shutil.which(machine) is not None


def listening_port(pid):
    """The TCP/IPv4 port a process listens on (Linux /proc), None while it has none."""
    inodes = set()
    for fd in pathlib.Path(f"/proc/{pid}/fd").iterdir():
        with contextlib.suppress(OSError):
            link = os.readlink(fd)
            if link.startswith("socket:["):
                inodes.add(link[8:-1])
    for line in (
        pathlib.Path(f"/proc/{pid}/net/tcp").read_text("ascii").splitlines()[1:]
    ):
        field = line.split()
        if field[3] == TCP_LISTEN and field[9] in inodes:
            return int(field[1].rsplit(":", 1)[1], 16)
    return None


def command_line(machine, port, drives, history_lines, warp=True):
    """Emulator arguments for {unit: (model, image)} with true drive emulation, every
    other unit off, no sound, a fixed random seed, the binary monitor on port (0: one
    the system picks)."""
    args = [shutil.which(machine) or machine, "-default", "+logcolorize", "+sound"]
    args += ["-seed", "1", "-binarymonitor"]
    args += ["-binarymonitoraddress", f"ip4://127.0.0.1:{port}"]
    args += ["-monchislines", str(history_lines)] + (["-warp"] if warp else [])
    for unit in range(8, 12):
        model, image = drives.get(unit, (None, None))
        args += [f"-drive{unit}type", str(DRIVE_TYPE[model] if model else 0)]
        if model:
            args += [f"-drive{unit}truedrive", f"-drive{unit}idle", "0"]
            args += [f"-{unit}", str(image)] if image is not None else []
    return args


class Vice:
    """A running emulator and its binary monitor client (``mon``), stopped.

    ``drives`` maps units to ``(model, image path)``; ``history_lines`` sizes VICE's
    CPU history for :meth:`history`."""

    def __init__(
        self, drives, machine="x64sc", history_lines=200_000, timeout=60.0, warp=True
    ):
        self.machine, self.drives = machine, dict(drives)
        self.port = None
        self._tmp = pathlib.Path(tempfile.mkdtemp(prefix="vice-"))
        self.log = self._tmp / "vice.log"
        args = command_line(machine, 0, self.drives, history_lines, warp)
        env = os.environ | {"HOME": str(self._tmp)}
        with open(self.log, "wb") as log:
            self.proc = subprocess.Popen(  # pylint: disable=consider-using-with
                args, stdout=log, stderr=subprocess.STDOUT, env=env
            )
        self._names = {}
        try:
            self.mon = BinaryMonitor(self._connect(timeout), timeout)
            self.mon.stop()
        except BaseException:
            self.proc.kill()
            self.proc.wait()
            shutil.rmtree(self._tmp, ignore_errors=True)
            raise

    def _connect(self, timeout):
        deadline = time.monotonic() + timeout
        while True:
            if self.proc.poll() is not None:
                raise ViceError(
                    f"{self.machine} exited: {self.log.read_text()[-2000:]}"
                )
            self.port = listening_port(self.proc.pid)
            if self.port is not None:
                return socket.create_connection(("127.0.0.1", self.port), timeout=1)
            if time.monotonic() > deadline:
                raise ViceError(f"{self.machine} opened no binary monitor")
            time.sleep(0.1)

    def names(self, space):
        """{register name: id} of a memspace."""
        if space not in self._names:
            self._names[space] = self.mon.registers_available(space)
        return self._names[space]

    def registers(self, space):
        """{register name: value} of a memspace's CPU."""
        ids = self.mon.registers(space)
        return {k: ids[v] for k, v in self.names(space).items() if v in ids}

    def set_registers(self, space, **values):
        """Set registers by name."""
        names = self.names(space)
        self.mon.set_registers({names[k.upper()]: v for k, v in values.items()}, space)

    def history(self, space, count=HISTORY_MAX):
        """The last count instructions of a memspace's CPU (HISTORY_DTYPE)."""
        return self.mon.history(count, self.names(space), space)

    def clock(self, space):
        """The CPU clock at the start of the memspace's last instruction."""
        h = self.history(space, 1)
        return int(h["clock"][-1]) if len(h) else 0

    def run_until(self, space, addr, timeout):
        """Run until the memspace's CPU executes addr; False, stopped, on timeout or
        any other stop. A drive stopped there runs on to the main CPU's clock at the
        next command, so only a loop at addr holds it there."""
        cp = self.mon.checkpoint(addr, space=space)
        try:
            self.mon.resume()
            if not self.mon.wait_stopped(timeout):
                self.mon.stop()
                return False
            return cp in self.mon.hits
        finally:
            self.mon.delete(cp)

    def close(self, timeout=30.0):
        """Quit (attached images are written back) and wait for the emulator."""
        if self.proc.poll() is None:
            self.mon.quit()
            try:
                self.proc.wait(timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.mon.sock.close()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class DriveMonitor:
    """:class:`nybulah.monitor.Monitor`'s read, write and jsr on an emulated drive: a
    call returns into a parked ``jmp *`` with interrupts masked, and on a 1581 the
    CIA timers run as drive/monitor.s sets them for J."""

    protocol = "vice"
    cbm = None

    def __init__(self, vice, unit, model="1581", park=PARK, call_timeout=120.0):
        self.vice, self.dev, self.model = vice, unit, model
        self.space = memspace(unit)
        self.park, self.call_timeout = park, call_timeout
        self.hz = DRIVE_HZ[model]
        self.running = False

    def start(self):
        """Once the DOS has finished its reset (its interrupt handler has run), park
        the drive CPU, interrupts masked, and set up its timers."""
        irq = struct.unpack("<H", self.read(IRQ_VECTOR, 2))[0]
        if not self.vice.run_until(self.space, irq, self.call_timeout):
            raise ViceError("the drive DOS did not finish its reset")
        park = bytes([JMP, self.park & 0xFF, self.park >> 8])
        self.write(self.park, park + DELAY)
        regs = self.vice.registers(self.space)
        self.vice.set_registers(self.space, pc=self.park, fl=regs["FL"] | FLAG_I)
        if not self.vice.run_until(self.space, self.park, self.call_timeout):
            raise ViceError("the drive did not park")
        if self.model == "1581":
            for reg, value in CIA1581_SETUP:
                self.poke(CIA1581 + reg, value)
        self.running = True
        return self

    def stop(self):
        """The drive stays parked."""
        self.running = False

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    def read(self, addr, size):
        """Drive memory, I/O registers peeked without side effects."""
        return self.vice.mon.mem_get(addr, size, self.space)

    def write(self, addr, data):
        """Write drive RAM."""
        self.vice.mon.mem_set(addr, data, self.space)

    def poke(self, addr, value):
        """Write one byte through the drive's bus (I/O side effects)."""
        self.vice.mon.mem_set(addr, bytes([value]), self.space, effects=True)

    def call(self, addr, a=0, x=0, y=0):
        """Set the drive up to run the routine at addr (see :meth:`finish`)."""
        regs = self.vice.registers(self.space)
        sp, ret = regs["SP"], self.park - 1
        self.write(STACK + sp, bytes([ret >> 8]))
        self.write(STACK + ((sp - 1) & 0xFF), bytes([ret & 0xFF]))
        fl = regs["FL"] | FLAG_I
        self.vice.set_registers(
            self.space, pc=addr, a=a, x=x, y=y, sp=(sp - 2) & 0xFF, fl=fl
        )

    def finish(self, timeout=None):
        """Run until the routine returns: (A, X, Y); ViceError, stopped, on timeout."""
        timeout = self.call_timeout if timeout is None else timeout
        if not self.vice.run_until(self.space, self.park, timeout):
            pc = self.vice.registers(self.space)["PC"]
            raise ViceError(f"drive call did not return in {timeout} s (PC ${pc:04X})")
        regs = self.vice.registers(self.space)
        return regs["A"], regs["X"], regs["Y"]

    def jsr(self, addr, a=0, x=0, y=0):
        """Call a drive subroutine; return its (A, X, Y)."""
        self.call(addr, a, x, y)
        return self.finish()

    def run_cycles(self, cycles):
        """Run the drive's delay loop for at least cycles (whole loops of DELAY_LOOP)."""
        loops = -(-int(cycles) // DELAY_LOOP)
        while loops > 0:
            self.jsr(self.park + 3, y=min(loops, 256) & 0xFF)
            loops -= 256

    def sleep(self, seconds):
        """Let seconds of drive time pass (:meth:`run_cycles`)."""
        self.run_cycles(seconds * self.hz)

    def history(self, count=HISTORY_MAX):
        """The drive CPU's last count instructions."""
        return self.vice.history(self.space, count)


def adapter_raw(data, lines, clk_bit=0x40):
    """xum1541 v12 adapter output (:mod:`nybulah.stream` framing) of received bytes
    and the CIA2 port A read before each (CLK asserted reads 0: metadata)."""
    data = np.asarray(data, np.uint8)
    esc = ((np.asarray(lines, np.uint8) & clk_bit) == 0) | (data == 0)
    at = np.arange(len(data)) + np.cumsum(esc)
    out = np.zeros(len(data) + int(esc.sum()) + 2, np.uint8)
    out[at] = data
    out[-1] = 0x80
    return out.tobytes()


class C128Receiver:
    """The emulated C128 as the stream adapter (drive/vicerx.s): host go, then every
    fast serial byte with the CLK line beside it."""

    def __init__(self, vice, code, boot_timeout=120.0):
        if vice.machine != "x128":
            raise ValueError("the fast serial receiver needs x128")
        self.vice, self.code = vice, bytes(code)
        self.boot_timeout = boot_timeout
        self.ram = vice.mon.banks().get("ram", 0)
        self.page_at = struct.unpack_from("<H", self.code, 3)[0]

    def start(self):
        """Once BASIC runs SYS into it, load the receiver; go is asserted when the
        emulator next resumes."""
        self.vice.mon.feed(f"SYS{RX_BASE}\r")
        if not self.vice.run_until(MAIN, RX_BASE, self.boot_timeout):
            raise ViceError("the C128 did not reach the receiver")
        self.vice.mon.mem_set(RX_BASE, self.code, MAIN, self.ram)

    def received(self):
        """(data, lines) received so far, the emulator stopped; X comes from the CPU
        history, since VICE refreshes the 8502's registers only when it stopped it."""
        mon = self.vice.mon
        page = mon.mem_get(self.page_at, 1, MAIN, self.ram)[0]
        last = self.vice.history(MAIN, 1)
        x = (int(last["x"][-1]) + (last["op"][-1, 0] == INX)) & 0xFF if len(last) else 0
        n = (page - (RX_DATA >> 8)) * 256 + x
        if n <= 0:
            return np.zeros(0, np.uint8), np.zeros(0, np.uint8)
        data = mon.mem_get(RX_DATA, n, MAIN, self.ram)
        lines = mon.mem_get(RX_LINES, n, MAIN, self.ram)
        return np.frombuffer(data, np.uint8), np.frombuffer(lines, np.uint8)
