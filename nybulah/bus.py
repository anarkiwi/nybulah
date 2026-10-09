"""Run IEC bus steps in one adapter session, waiting on drive readiness.

Each step prints one JSON line; a summary with every drive's final status
closes the run.
"""

import argparse
import contextlib
import json
import math
import pathlib
import re
import shlex
import signal
import sys
import threading
import time

from tqdm import tqdm

from . import ramprobe, tool
from .link import BusError
from .opencbm import (
    IEC_ATN,
    IEC_CLOCK,
    IEC_DATA,
    IEC_RESET,
    IEC_SRQ,
    IO_TIMEOUT_MS,
    OpenCBMError,
)

STEPS = """steps (one per argument or per --script line, # comments):
  reset                  pulse RESET; wait until no drive holds CLK or DATA
  wait DEV...            until each drive's DOS answers its error channel
  status DEV...          read the error channel
  command DEV "CMD"      DOS command; done when its status reads back
  dir DEV                list the directory
  identify DEV...        drive model
  detect                 model of every drive answering on 8-30

Commands that can make DOS step a 1571 head to the stop are refused unless
--allow-dos-bump."""

T_AT = 1e-3
RESET_HOLD_S = 0.1
DETECT = range(8, 31)
JOB_QUEUE = range(0x00, 0x0B)
OPS = ("reset", "detect", "wait", "status", "command", "dir", "identify")
LINES = {"ATN": IEC_ATN, "CLK": IEC_CLOCK, "DATA": IEC_DATA, "RESET": IEC_RESET}
LINES["SRQ"] = IEC_SRQ


def diagnostic_cycles(rom_pages, ram_pages):
    """CPU cycles of the DOS power-on diagnostic: zero page count test, ROM
    checksum and RAM pattern test, by instruction cycles of each loop."""
    zero_page = 256 * 11 + 256 * (8 + 256 * 11 - 1 + 21)
    rom = rom_pages * (5 + 256 * 10 - 1 + 5)
    ram = ram_pages * ((256 * 18 - 1 + 10) + (5 + 256 * 42 - 1 + 5))
    return zero_page + rom + ram


DIAGNOSTIC = {"1541": (64, 7, 1e6), "1571": (128, 7, 1e6), "1581": (128, 31, 2e6)}
BOOT_S = max(diagnostic_cycles(r, m) / hz for r, m, hz in DIAGNOSTIC.values())


def boot_file_search_s(rev=0.2, step=0.012, settle=0.018, tick=20000 / 2e6):
    """Bound on a stock 1581's boot file search after its diagnostic: reset_ctl,
    restore, spin-up, seek to the directory cylinder, then every disk job with
    the ROM's tries (derivation in docs/hardware.md)."""
    rnf = 5 * rev
    run = rnf + rev + rnf
    mechanics = 2 * 0.255 + 79 * step + 0x50 * tick + (2 + 39) * step + 3 * settle
    init = 3 * rnf + 3 * rnf + 3 * run
    lookup = 5 * run + 2 * 39 * step + 2 * settle
    return mechanics + init + lookup


BOOT_FILE_S = {"1581": boot_file_search_s()}


class DeviceHung(BusError):
    """A drive did not answer its error channel by the deadline."""


class BusHeld(BusError):
    """A device still holds CLK or DATA at the deadline."""


class DriveUnresponsive(IOError):
    """The drive did not answer its status channel after reset."""


class Interrupted(Exception):
    """A signal asked the session to stop."""


def answers(status):
    """Whether a status channel string came from a live DOS."""
    m = re.match(r"\s*(\d+)\s*,", status or "")
    return bool(m) and int(m.group(1)) != 99


def dos_ok(status):
    """No status, or a DOS status below 20 or the power-on message (73)."""
    if status is None:
        return True
    code = int(re.match(r"\s*(\d*)", status).group(1) or 99)
    return code < 20 or code == 73


def bump_risk(cmd):
    """Why a DOS command can step a 1571 head to the stop (docs/disk.md), else None."""
    if cmd[:1] == b"N" and b"," in cmd:
        return "N with an ID formats the disk"
    if cmd[:2] == b"U0" and cmd[2:3] != b">":
        return "burst (U0) MFM command"
    if cmd[:3] == b"M-W" and len(cmd) > 5:
        lo = cmd[3] | cmd[4] << 8
        if lo < JOB_QUEUE.stop and lo + cmd[5] > JOB_QUEUE.start:
            return "M-W into the job queue"
    if cmd[:3] in (b"M-E", b"B-E") or re.match(rb"&|U[3-8C-H]", cmd):
        return "runs drive code"
    return None


def model_of(cbm, dev):
    """ramprobe.identify_model, or 1581 (cbm_identify type 3)."""
    try:
        return ramprobe.identify_model(cbm, dev)
    except ValueError:
        code, desc = cbm.identify(dev)
        if code == 3 or "1581" in desc:
            return "1581"
        raise


def restarts(cmd):
    """Whether a DOS command restarts the drive (UJ, U:)."""
    return cmd[:2] in (b"UJ", b"U:")


def listing(data):
    """Lines of a directory in BASIC program form."""
    out, i = [], 2
    while i + 4 <= len(data) and data[i : i + 2] != b"\0\0":
        end = data.find(0, i + 4)
        end = len(data) if end < 0 else end
        blocks = data[i + 2] | data[i + 3] << 8
        out.append(f"{blocks} {data[i + 4 : end].decode('latin-1')}")
        i = end + 1
    return out


class Bus:  # pylint: disable=too-many-instance-attributes
    """One adapter session: readiness waits and DOS transactions.

    Waits release the host's lines, then poll until no drive holds CLK or DATA
    and the drive's error channel answers, backing off from T_AT to a deadline;
    each transaction arms the adapter's I/O timeout with the time left.
    """

    def __init__(self, cbm, boot_s=None, io_s=IO_TIMEOUT_MS / 1000, models=None):
        self.cbm, self.boot_s, self.io_s = cbm, boot_s, io_s
        self.models = dict(models or {})
        self.stop = threading.Event()
        self.clock = getattr(cbm, "clock", time.monotonic)
        self.sleep = getattr(cbm, "sleep", self.stop.wait)
        self.set_timeout = getattr(cbm, "set_timeout", None)
        self.known, self.low = set(), {}
        self.started, self.reset_at, self.first_sample = self.clock(), None, None
        self.atn_at = self.addressed = self.stopped = None

    def boot_span(self, dev=None):
        """The override, else the diagnostic plus a 1581's boot file search."""
        if self.boot_s is not None:
            return self.boot_s
        return BOOT_S + BOOT_FILE_S.get(self.models.get(dev), 0.0)

    def boot_deadline(self, dev=None):
        """boot_span after the last reset, or after the session start."""
        base = self.started if self.reset_at is None else self.reset_at + RESET_HOLD_S
        return base + self.boot_span(dev)

    def sample(self):
        """Poll the lines, noting when each was first seen low."""
        lines, now = self.cbm.iec_poll(), self.clock()
        if self.first_sample is None:
            self.first_sample = now
        for name, bit in LINES.items():
            if lines & bit:
                self.low.setdefault(name, now)
            else:
                self.low.pop(name, None)
        return lines

    def snapshot(self):
        """Lines low and for how long, time since reset and ATN, who is addressed."""
        try:
            self.sample()
        except OpenCBMError as e:
            return {"error": f"iec_poll: {e}"}
        now = self.clock()
        reset = self.reset_at

        def ago(t):
            return None if t is None else round(now - t, 3)

        held = {
            n: ago(reset if reset is not None and t == self.first_sample else t)
            for n, t in self.low.items()
        }
        text = [
            f"{n} low for {h:.2f} s"
            + (" since reset" if reset is not None and h == ago(reset) else "")
            for n, h in held.items()
        ] or ["no line low"]
        if "ATN" not in held:
            text.append("ATN not asserted")
        text.append("no reset" if reset is None else f"reset {ago(reset):.2f} s ago")
        text.append(
            "no ATN yet"
            if self.atn_at is None
            else f"last ATN {ago(self.atn_at):.2f} s ago"
        )
        text.append(self.addressed or "no drive addressed")
        return {
            "low": list(held),
            "held_s": held,
            "since_reset_s": ago(reset),
            "since_atn_s": ago(self.atn_at),
            "addressed": self.addressed,
            "text": "; ".join(text),
        }

    def interrupt(self, signum, _frame=None):
        """Signal handler: stop after the transaction in progress."""
        self.stopped = signal.Signals(signum).name
        self.stop.set()

    def idle(self):
        """Release every host line."""
        self.cbm.iec_release(IEC_ATN | IEC_CLOCK | IEC_DATA)

    def arm(self, deadline):
        """Bound the next transaction to deadline (at least one adapter tick)."""
        self.atn_at = self.clock()
        if self.set_timeout:
            self.set_timeout(max(1, math.ceil((deadline - self.atn_at) * 1000)))

    def restore(self):
        """Give the adapter back its default I/O timeout."""
        if self.set_timeout:
            self.set_timeout(IO_TIMEOUT_MS)

    def attempts(self, deadline):
        """Yield for each try: now, then after T_AT, doubling, the last at deadline."""
        delay = T_AT
        while True:
            yield
            left = deadline - self.clock()
            if left <= 0:
                return
            if self.stopped:
                raise Interrupted(self.stopped)
            self.sleep(min(delay, left))
            delay *= 2

    def settle(self, deadline):
        """Release the host's lines and wait until no device holds CLK or DATA."""
        self.idle()
        held = 0
        for _ in self.attempts(deadline):
            held = self.sample() & (IEC_CLOCK | IEC_DATA)
            if not held:
                return
        raise BusHeld(f"bus lines 0x{held:02x} held at the deadline")

    def ready(self, dev, deadline):
        """dev's error channel once the bus is free and its DOS answers."""
        self.settle(deadline)
        status = ""
        for _ in self.attempts(deadline):
            self.sample()
            self.arm(deadline)
            status = self.cbm.status(dev)
            if answers(status):
                self.known.add(dev)
                return status
        raise DeviceHung(f"device {dev}: no DOS status by the deadline: {status!r}")

    def deadline(self, dev):
        """A drive not yet heard from may still be booting."""
        return (
            self.boot_deadline(dev)
            if dev not in self.known
            else self.clock() + self.io_s
        )

    def ensure(self, dev):
        """Wait for dev's DOS unless it has answered since the last reset."""
        if dev not in self.known:
            self.ready(dev, self.boot_deadline(dev))

    def reset(self, _dev=None, _arg=None):
        """Pulse RESET; every drive reruns its diagnostic."""
        self.idle()
        self.cbm.reset()
        self.known.clear()
        self.low, self.reset_at, self.first_sample = {}, self.clock(), None
        self.settle(self.boot_deadline())
        return {}

    def status(self, dev, _arg=None):
        """The error channel, waiting for the drive first."""
        return {"status": self.ready(dev, self.deadline(dev))}

    wait = status

    def command(self, dev, cmd):
        """Send cmd to the command channel; done when the status reads back."""
        self.ensure(dev)
        self.arm(self.clock() + self.io_s)
        self.cbm.command(dev, cmd)
        span = self.boot_span(dev) if restarts(cmd) else self.io_s
        return {"status": self.ready(dev, self.clock() + span)}

    def dir(self, dev, _arg=None):
        """Directory listing over OPEN "$", TALK, UNTALK and CLOSE."""
        self.ensure(dev)
        data, eoi = b"", False
        self.arm(self.clock() + self.io_s)
        self.cbm.open_file(dev, 0, b"$")
        try:
            self.cbm.talk(dev, 0)
            self.addressed = f"device {dev} talking"
            try:
                while not eoi:
                    chunk = self.cbm.raw_read(256)
                    data += chunk
                    eoi = len(chunk) < 256 or self.cbm.get_eoi()
            finally:
                self.arm(self.clock())
                self.cbm.untalk()
                self.addressed = None
        finally:
            self.arm(self.clock())
            self.cbm.close_file(dev, 0)
        return {"files": listing(data), "status": self.ready(dev, self.deadline(dev))}

    def identify(self, dev, _arg=None):
        """The drive model from cbm_identify (ramprobe.identify_model)."""
        self.ensure(dev)
        self.arm(self.clock() + self.io_s)
        self.models[dev] = model_of(self.cbm, dev)
        return {"model": self.models[dev]}

    def detect(self, _dev=None, _arg=None):
        """Model of every drive on 8-30 answering cbm_identify."""
        self.settle(self.boot_deadline())
        found = {}
        for dev in DETECT:
            self.arm(self.clock() + self.io_s)
            try:
                found[dev] = self.models[dev] = model_of(self.cbm, dev)
            except OpenCBMError:
                continue
            except ValueError as e:
                found[dev] = str(e)
            self.known.add(dev)
        return {"devices": found}


def recover(cbm, dev, resets=2, timeout=None):
    """Release the host's lines, reset the bus and wait until dev answers.

    Returns the drive's status string; a second reset covers drives that do
    not come back from the first. Raises DriveUnresponsive otherwise.
    """
    bus = Bus(cbm, timeout)
    error = None
    try:
        for _ in range(resets):
            try:
                bus.reset()
                return bus.ready(dev, bus.boot_deadline(dev))
            except (BusError, OpenCBMError) as e:
                error = e
    finally:
        bus.restore()
    raise DriveUnresponsive(f"device {dev} silent after {resets} resets: {error}")


def parse(line):
    """Actions (op, dev, arg) of one step line."""
    words = shlex.split(line, comments=True)
    if not words:
        return []
    op, args, arg = words[0], words[1:], None
    if op not in OPS:
        raise ValueError(f"unknown step {op!r}")
    if op in ("reset", "detect"):
        if args:
            raise ValueError(f"{op} takes no arguments")
        return [(op, None, None)]
    if op == "command":
        if len(args) != 2:
            raise ValueError('command takes DEV "CMD"')
        raw = args.pop().encode("latin-1").decode("unicode_escape")
        arg = raw.encode("latin-1")
    devs = [int(a) for a in args]
    if not devs or any(d not in DETECT for d in devs) or op == "dir" and len(devs) > 1:
        raise ValueError(f"{line!r}: devices are 8-30, one for dir")
    return [(op, d, arg) for d in devs]


@contextlib.contextmanager
def _signals(handler):
    """SIGINT and SIGTERM call handler while the block runs (main thread only)."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    old = {s: signal.signal(s, handler) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for s, h in old.items():
            signal.signal(s, h)


def _print(rec):
    print(json.dumps(rec), flush=True)


class Script:
    """Runs actions in order, one JSON record each, stopping at the first
    failure unless keep_going (then only the failed drive's steps are skipped)."""

    def __init__(self, bus, keep_going=False, emit=_print):
        self.bus, self.keep_going, self.emit = bus, keep_going, emit
        self.failed, self.touched, self.final, self.lines = set(), {}, {}, {}

    def do(self, op, dev, arg):
        """Run one action and emit its record; True when it succeeded."""
        bus, t0 = self.bus, self.bus.clock()
        rec = {"step": op, "dev": dev}
        if arg is not None:
            rec["arg"] = arg.decode("latin-1")
        try:
            rec |= getattr(bus, op)(dev, arg)
            rec["result"] = "ok" if dos_ok(rec.get("status")) else "dos-error"
        except Interrupted:
            rec["result"] = "interrupted"
        except (BusError, OpenCBMError, ValueError) as e:
            rec |= {"result": "error", "error": f"{type(e).__name__}: {e}"}
        if rec["result"] != "ok":
            rec["bus"] = self.lines[dev] = bus.snapshot()
            bus.idle()
        rec["seconds"] = round(bus.clock() - t0, 6)
        if dev is not None:
            self.touched[dev] = None
            self.final[dev] = rec.get("status", self.final.get(dev))
        self.emit(rec)
        return rec["result"] == "ok"

    def run(self, actions, end_check=True):
        """Execute actions, then the end check; return the summary record."""
        t0 = self.bus.clock()
        with tqdm(actions, desc="bus", unit="step", file=sys.stderr) as progress:
            for op, dev, arg in progress:
                if self.bus.stopped:
                    break
                if dev in self.failed:
                    self.emit({"step": op, "dev": dev, "result": "skipped"})
                elif not self.do(op, dev, arg):
                    self.failed.add(dev)
                    if dev is None or not self.keep_going:
                        break
        failing = self.failed and (None in self.failed or not self.keep_going)
        if end_check and not self.bus.stopped and not failing:
            for dev in [d for d in self.touched if d not in self.failed]:
                if not self.do("status", dev, None):
                    self.failed.add(dev)
        lines = self.bus.snapshot()
        self.bus.idle()
        devs = sorted(set(self.touched) | set(self.bus.models))
        out = {
            "summary": {
                str(d): {
                    "status": self.final.get(d),
                    "model": self.bus.models.get(d),
                    "failed": d in self.failed,
                }
                | ({"bus": self.lines[d]} if d in self.lines else {})
                for d in devs
            },
            "ok": not self.failed and not self.bus.stopped,
            "interrupted": self.bus.stopped,
            "bus": lines,
            "seconds": round(self.bus.clock() - t0, 6),
        }
        self.emit(out)
        return out


def steps(args):
    """Actions from --script then the command line, and the refused commands."""
    lines = args.script.read_text().splitlines() if args.script else []
    actions = [a for line in lines + args.steps for a in parse(line)]
    if args.reset_first:
        actions.insert(0, ("reset", None, None))
    refused = [
        {"step": op, "dev": dev, "arg": arg.decode("latin-1"), "error": why}
        for op, dev, arg in actions
        if op == "command" and (why := bump_risk(arg)) and not args.allow_dos_bump
    ]
    return actions, refused


def model_arg(text):
    """DEV=MODEL as (dev, model)."""
    dev, _, model = text.partition("=")
    if model not in DIAGNOSTIC:
        raise argparse.ArgumentTypeError(f"model is one of {sorted(DIAGNOSTIC)}")
    return int(dev), model


def add_arguments(ap):
    """Command line options."""
    ap.formatter_class = argparse.RawDescriptionHelpFormatter
    ap.epilog = STEPS
    ap.add_argument("steps", nargs="*", help='steps, e.g. "status 8 9"')
    ap.add_argument("--script", type=pathlib.Path, help="file of steps, one a line")
    ap.add_argument("--keep-going", action="store_true", help="skip only failed drives")
    ap.add_argument(
        "--end-check",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="read every touched drive's status at the end",
    )
    ap.add_argument("--reset-first", action="store_true", help="start with reset")
    ap.add_argument(
        "--allow-dos-bump",
        action="store_true",
        help="allow DOS commands that can step a 1571 head to the stop",
    )
    ap.add_argument(
        "--boot-seconds",
        type=float,
        help="readiness deadline after RESET (default: the DOS diagnostic, plus "
        "the boot file search for a 1581)",
    )
    ap.add_argument(
        "--model",
        action="append",
        default=[],
        type=model_arg,
        metavar="DEV=MODEL",
        help="declare a drive's model before it answers, e.g. 9=1581",
    )
    ap.add_argument(
        "--command-seconds",
        type=float,
        default=IO_TIMEOUT_MS / 1000,
        help="deadline for a DOS command (default: the adapter I/O timeout)",
    )


def execute(args, cbm):
    """Run the steps; return the summary."""
    actions, refused = steps(args)
    if refused:
        for rec in refused:
            _print(rec | {"result": "refused"})
        return {"summary": {}, "ok": False, "interrupted": None, "refused": refused}
    bus = Bus(cbm, args.boot_seconds, args.command_seconds, dict(args.model))
    with _signals(bus.interrupt):
        try:
            return Script(bus, args.keep_going).run(actions, args.end_check)
        finally:
            bus.idle()
            bus.restore()


def main(argv=None, cbm=None):
    """CLI entry point."""
    return tool.standalone(sys.modules[__name__], argv, cbm)


if __name__ == "__main__":
    main()
