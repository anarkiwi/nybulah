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
                         (a wedged adapter is reset with the bus, else
                         USB-reset, once each)
  adapterreset           reset the adapter and the bus (firmware v13)
  usbreset               USB-reset the adapter, then reset
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
RECOVERY = {"adapterreset": "adapter_reset", "usbreset": "usb_reset"}
OPS = ("reset", *RECOVERY, "detect", "wait", "status", "command", "dir", "identify")
LINES = {"ATN": IEC_ATN, "CLK": IEC_CLOCK, "DATA": IEC_DATA, "RESET": IEC_RESET}
LINES["SRQ"] = IEC_SRQ
RESET_ORIGINS = ("reset", "adapter reset", "usb reset")


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
OUTER_S = RESET_HOLD_S + BOOT_S + max(BOOT_FILE_S.values())


class DeviceHung(BusError):
    """A drive did not answer its error channel by the limit."""


class BusHeld(BusError):
    """A device still holds CLK or DATA at the outer limit; ``recovery`` maps
    each adapter recovery step tried to its outcome."""

    recovery = None


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
    """One adapter session: settle detection, readiness and DOS transactions.

    Waits assert nothing: they watch CLK and DATA until no drive holds them,
    then a drive is ready once its error channel answers. Limits only stop new
    transactions; a running one may take the longest its kind can legitimately need.
    """

    def __init__(self, cbm, outer_s=OUTER_S, io_s=IO_TIMEOUT_MS / 1000):
        self.cbm, self.outer_s, self.io_s = cbm, outer_s, io_s
        self.stop = threading.Event()
        self.clock = getattr(cbm, "clock", time.monotonic)
        self.sleep = getattr(cbm, "sleep", self.stop.wait)
        self.set_timeout = getattr(cbm, "set_timeout", None)
        self.known, self.low, self.models, self.timelines = set(), {}, {}, []
        self.started, self.reset_at, self.first_sample = self.clock(), None, None
        self.atn_at = self.addressed = self.stopped = None
        self.hands_off, self.last, self.quiet, self.free_since = False, None, 0.0, None
        self._timeline("run start")

    def _timeline(self, origin):
        self.timeline = {"from": origin, "transitions": [], "released": {}}
        self.timeline |= {"answered": {}, "t0": self.clock()}
        self.timelines.append(self.timeline)
        self.last, self.quiet, self.free_since = None, 0.0, None

    def rel(self):
        """Seconds since the current timeline began."""
        return round(self.clock() - self.timeline["t0"], 4)

    def base(self):
        """When the drives last started: the reset, else the session start."""
        return self.started if self.reset_at is None else self.reset_at + RESET_HOLD_S

    def sample(self):
        """Poll the lines; note when each was first seen low and every change."""
        lines, now = self.cbm.iec_poll(), self.clock()
        if self.first_sample is None:
            self.first_sample = now
        both = IEC_CLOCK | IEC_DATA
        if lines & both and self.free_since is not None and self.last is not None:
            self.quiet = max(self.quiet, now - self.free_since)
        self.free_since = None if lines & both else self.free_since or now
        if lines != self.last:
            t, tl = self.rel(), self.timeline
            tl["transitions"].append([t, [n for n, b in LINES.items() if lines & b]])
            for n, b in LINES.items():
                if self.last is not None and self.last & b and not lines & b:
                    tl["released"][n] = t
            self.last = lines
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
        now, reset = self.clock(), self.reset_at

        def ago(t):
            return None if t is None else round(now - t, 3)

        since = {
            n: "seen" if t != self.first_sample else "reset" if reset else "unknown"
            for n, t in self.low.items()
        }
        held = {
            n: ago(reset if since[n] == "reset" else t) for n, t in self.low.items()
        }
        words = {"seen": "", "reset": " since reset"}
        words["unknown"] = " at least (low when this run started)"
        text = [f"{n} low for {held[n]:.2f} s{words[since[n]]}" for n in held]
        text = text or ["no line low"]
        if "ATN" not in held:
            text.append("ATN not asserted")
        text.append("no reset" if reset is None else f"reset {ago(reset):.2f} s ago")
        atn = self.atn_at
        text.append("no ATN yet" if atn is None else f"last ATN {ago(atn):.2f} s ago")
        text.append(self.addressed or "no drive addressed")
        return {
            "low": list(held),
            "held_s": held,
            "held_since": since,
            "since_reset_s": ago(reset),
            "since_atn_s": ago(atn),
            "addressed": self.addressed,
            "text": "; ".join(text),
        }

    def interrupt(self, signum, _frame=None):
        """Signal handler: stop after the transaction in progress."""
        self.stopped = signal.Signals(signum).name
        self.stop.set()

    def idle(self):
        """Release every host line, unless the bus is being left alone."""
        if not self.hands_off:
            self.cbm.iec_release(IEC_ATN | IEC_CLOCK | IEC_DATA)

    def timeout(self, seconds):
        """The adapter's I/O timeout for the next call."""
        if self.set_timeout:
            self.set_timeout(max(1, math.ceil(seconds * 1000)))

    def arm(self, span):
        """An ATN transaction starts that may legitimately take span seconds."""
        self.atn_at = self.clock()
        self.timeout(span)

    def restore(self):
        """Give the adapter back its default I/O timeout."""
        if self.set_timeout:
            self.set_timeout(IO_TIMEOUT_MS)

    def attempts(self, limit):
        """Yield for each try: now, then after T_AT, doubling, the last at limit."""
        delay = T_AT
        while True:
            yield
            left = limit - self.clock()
            if left <= 0:
                return
            if self.stopped:
                raise Interrupted(self.stopped)
            self.sleep(min(delay, left))
            delay *= 2

    def settle(self, limit):
        """Watch, asserting nothing, until CLK and DATA have stayed released for
        the quiet window (the longest release a drive was seen to take back).
        At limit held lines are left exactly as they are and BusHeld reports."""
        self.idle()
        delay = T_AT
        while True:
            held = self.sample() & (IEC_CLOCK | IEC_DATA)
            free_for = 0.0 if held else self.clock() - self.free_since
            left = limit - self.clock()
            if not held and (free_for >= self.quiet or left <= 0):
                self.timeline.setdefault("settled", self.rel())
                return
            if left <= 0:
                self.hands_off = True
                raise BusHeld(self.held_report(held))
            if self.stopped:
                raise Interrupted(self.stopped)
            wait = left if held else min(left, self.quiet - free_for)
            line = IEC_DATA if held & IEC_DATA or not held else IEC_CLOCK
            t0 = self.clock()
            self.timeout(wait)
            with contextlib.suppress(OpenCBMError):
                self.cbm.iec_wait(line, 0 if held else 1)
            if self.clock() - t0 < min(delay, wait):
                self.sleep(min(delay, wait))
                delay *= 2

    def adapter_held(self):
        """Whether the adapter, not a drive, is the likely holder of CLK or DATA.

        A RESET pulse restarts every drive, which releases its bus lines while
        held in reset and through its boot diagnostic. Every sample since the
        reset showing one unchanged state with CLK or DATA low, never released,
        means the reset never reached the drives: the adapter performs RESET
        only once its command loop is free, so it is wedged and is the one
        asserting the lines.
        """
        tl = self.timeline
        return (
            tl["from"] in RESET_ORIGINS
            and len(tl["transitions"]) == 1
            and bool({"CLK", "DATA"} & set(tl["transitions"][0][1]))
        )

    def held_report(self, held):
        """Plain words for lines still held at the outer limit."""
        names = " and ".join(n for n, b in LINES.items() if held & b)
        start = "the reset" if self.reset_at is not None else "this run started"
        tl = self.timeline
        released = tl["released"] or "never"
        seen = f"{len(tl['transitions'])} line states seen, released at {released}"
        who = "a drive is holding the bus and needs a power cycle"
        if self.adapter_held():
            who = "the adapter is holding the bus; " + {
                "usb reset": "a USB reset did not clear it and it needs a power cycle",
                "adapter reset": "an adapter reset did not clear it; "
                "a USB reset of the adapter (bus step usbreset) clears it",
            }.get(
                tl["from"],
                "an adapter reset (bus step adapterreset) or a USB reset of the "
                "adapter (bus step usbreset) clears it",
            )
        return (
            f"{names} still held {self.clock() - self.base():.2f} s after {start}, past "
            f"every derived boot bound ({self.outer_s:.2f} s); {seen}; {who}; "
            "nothing was sent"
        )

    def ready(self, dev, limit, span):
        """dev's error channel once the bus has settled; a probe may take span."""
        status = ""
        for _ in self.attempts(limit):
            self.settle(limit)
            self.arm(span)
            status = self.cbm.status(dev)
            if answers(status):
                self.known.add(dev)
                self.timeline["answered"].setdefault(str(dev), self.rel())
                return status
        raise DeviceHung(f"device {dev}: no DOS status by the limit: {status!r}")

    def boot_limit(self):
        """No new transaction for an unheard drive after this: the outer limit."""
        return self.base() + self.outer_s

    def ensure(self, dev):
        """Wait for dev's DOS unless it has answered since the last reset."""
        if dev not in self.known:
            self.ready(dev, self.boot_limit(), self.outer_s)

    def _reset(self, origin, pulse=True):
        """Pulse RESET (unless the caller already reset the bus) and settle
        within the outer limit from it."""
        self.idle()
        if pulse:
            self.cbm.reset()
        self.known.clear()
        self.low, self.reset_at, self.first_sample = {}, self.clock(), None
        self._timeline(origin)
        self.settle(self.boot_limit())
        return {"settled_s": self.rel(), "timeline": self.timeline}

    def reset(self, _dev=None, _arg=None):
        """Pulse RESET; watch the lines until every drive has released them. A
        wedged adapter holding them (adapter_held) gets each RECOVERY step the
        adapter offers, in order, until one frees the bus; ``recovery`` maps
        each step tried to "ok" or its error."""
        try:
            return self._reset("reset")
        except BusHeld as e:
            held = e
        recovery = {}
        for step, method in RECOVERY.items():
            if not self.adapter_held():
                break
            if not hasattr(self.cbm, method):
                continue
            try:
                rec = getattr(self, step)()
            except (BusHeld, OpenCBMError) as e:
                held = e if isinstance(e, BusHeld) else held
                recovery[step] = f"{type(e).__name__}: {e}"
                continue
            return rec | {"recovery": recovery | {step: "ok"}}
        held.recovery = recovery
        raise held

    def adapterreset(self, _dev=None, _arg=None):
        """Reset the adapter and the bus from its control endpoint, then settle."""
        if not hasattr(self.cbm, "adapter_reset"):
            raise ValueError("adapter cannot be reset by command")
        self.cbm.adapter_reset(reset_bus=True)
        self.hands_off = False
        return self._reset("adapter reset", pulse=False) | {"adapter_reset": True}

    def usbreset(self, _dev=None, _arg=None):
        """USB-reset the adapter, then pulse RESET and settle."""
        if not hasattr(self.cbm, "usb_reset"):
            raise ValueError("adapter cannot be USB-reset")
        self.cbm.usb_reset()
        self.hands_off = False
        return self._reset("usb reset") | {"usb_reset": True}

    def status(self, dev, _arg=None):
        """The error channel, waiting for the drive first."""
        if dev in self.known:
            return {"status": self.ready(dev, self.clock() + self.io_s, self.io_s)}
        status = self.ready(dev, self.boot_limit(), self.outer_s)
        return {"status": status, "answered_s": self.timeline["answered"][str(dev)]}

    wait = status

    def command(self, dev, cmd):
        """Send cmd to the command channel; done when the status reads back."""
        self.ensure(dev)
        self.arm(self.io_s)
        self.cbm.command(dev, cmd)
        if restarts(cmd):
            self._timeline(f"{cmd.decode('latin-1')} {dev}")
        span = self.outer_s if restarts(cmd) else self.io_s
        return {"status": self.ready(dev, self.clock() + span, span)}

    def dir(self, dev, _arg=None):
        """Directory listing over OPEN "$", TALK, UNTALK and CLOSE."""
        self.ensure(dev)
        data, eoi = b"", False
        self.arm(self.io_s)
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
                self.arm(self.io_s)
                self.cbm.untalk()
                self.addressed = None
        finally:
            self.arm(self.io_s)
            self.cbm.close_file(dev, 0)
        status = self.ready(dev, self.clock() + self.io_s, self.io_s)
        return {"files": listing(data), "status": status}

    def identify(self, dev, _arg=None):
        """The drive model from cbm_identify (ramprobe.identify_model)."""
        self.ensure(dev)
        self.arm(self.io_s)
        self.models[dev] = model_of(self.cbm, dev)
        return {"model": self.models[dev]}

    def detect(self, _dev=None, _arg=None):
        """Model of every drive on 8-30 answering cbm_identify."""
        self.settle(self.boot_limit())
        found = {}
        for dev in DETECT:
            self.arm(self.io_s)
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

    Returns the drive's status string. A second reset covers a drive that let
    the lines go but did not answer; a wedged adapter is reset with the bus,
    else USB-reset, by Bus.reset; lines still held end it untouched (BusHeld).
    """
    bus = Bus(cbm, OUTER_S if timeout is None else timeout)
    error = None
    try:
        for _ in range(resets):
            try:
                bus.reset()
                return bus.ready(dev, bus.boot_limit(), bus.outer_s)
            except BusHeld as e:
                raise DriveUnresponsive(f"device {dev}: {e}") from e
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
    if op in ("reset", *RECOVERY, "detect"):
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
        self.held = None

    def do(self, op, dev, arg):
        """Run one action and emit its record; True when it succeeded, else
        what failed: dev, or None for the bus (held lines name no drive)."""
        bus, t0, culprit = self.bus, self.bus.clock(), dev
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
            if isinstance(e, BusHeld):
                self.held, culprit = str(e), None
                if e.recovery:
                    rec["recovery"] = e.recovery
        if rec["result"] != "ok":
            rec["bus"] = self.lines[culprit] = bus.snapshot()
            bus.idle()
        rec["seconds"] = round(bus.clock() - t0, 6)
        if dev is not None:
            self.touched[dev] = None
            self.final[dev] = rec.get("status", self.final.get(dev))
        self.emit(rec)
        return True if rec["result"] == "ok" else culprit

    def run(self, actions, end_check=True):
        """Execute actions, then the end check; return the summary record."""
        t0 = self.bus.clock()
        with tqdm(actions, desc="bus", unit="step", file=sys.stderr) as progress:
            for op, dev, arg in progress:
                if self.bus.stopped:
                    break
                if dev in self.failed:
                    self.emit({"step": op, "dev": dev, "result": "skipped"})
                elif (culprit := self.do(op, dev, arg)) is not True:
                    self.failed.add(culprit)
                    if culprit is None or not self.keep_going:
                        break
        failing = self.failed and (None in self.failed or not self.keep_going)
        if end_check and not self.bus.stopped and not failing:
            for dev in [d for d in self.touched if d not in self.failed]:
                if (culprit := self.do("status", dev, None)) is not True:
                    self.failed.add(culprit)
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
            "held": self.held,
            "bus": lines,
            "timelines": self.bus.timelines,
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
        default=OUTER_S,
        help="outer limit after RESET, past which held lines are only reported "
        "(default: the longest derived boot, a stock 1581's boot file search)",
    )
    ap.add_argument(
        "--command-seconds",
        type=float,
        default=IO_TIMEOUT_MS / 1000,
        help="time a DOS command may take (default: the adapter I/O timeout)",
    )


def execute(args, cbm):
    """Run the steps; return the summary."""
    actions, refused = steps(args)
    if refused:
        for rec in refused:
            _print(rec | {"result": "refused"})
        return {"summary": {}, "ok": False, "interrupted": None, "refused": refused}
    bus = Bus(cbm, args.boot_seconds, args.command_seconds)
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
