"""1581 drive access through a started monitor_*_1581 (drive/mfm.s, drive/mfmstream.s).

The head moves only by the WD177x: a bounded Restore (TR00 or an error, never more step
pulses than the estimate) and Seek from the homed track register, at most MAX_CYL.
"""

import contextlib
import math
import struct
import time

import numpy as np
from tqdm import tqdm

from . import mfmstream as ms
from .analysis import mfm
from .formats.mfmcap import MfmCapture
from .link import WATCHDOG_IDLE_S, WATCHDOG_S, HandshakeTimeout
from .monitor import Monitor, drivecode

CODE_BASE, CODE2, SPLIT = 0x0300, 0x0782, 0x0200
CIA_PA = 0x4000
MFM_CODE, STREAM_CODE = "mfm_1581", "mfmstream_1581"
TAGS = {MFM_CODE: b"NYMF", STREAM_CODE: b"NYMS"}
ENTRY = dict(
    zip(
        "sense motor side restore seek index readaddr readsec writesec writetrk"
        " settrk".split(),
        range(CODE_BASE, CODE_BASE + 33, 3),
    )
)
FIELDS = {
    "arg": 0,
    "sec": 1,
    "cnt": 2,
    "cyl": 3,
    "flags": 4,
    "tmo": 5,
    "buf": 6,
    "len": 8,
}
PB_AT, T0_AT, T1_AT, ID_AT, RS_AT = 10, 11, 14, 17, 23
LIST_TMO = 4 * ms.LIST_ENTRIES  # drive/mfmstream.s: TMO after the list
STATE_LEN = 20  # drive/mfmstream.s state: issued, op, rep, lp, trksave, sc, stamps...
STATE_AT = CODE_BASE + SPLIT - STATE_LEN  # ... status, flags, count: the block's end
FORCE_WRAPS = 2  # drive/mfmstream.s: busy wait after the timeout's force interrupt
BUFFER, BUFFER_END = 0x0C00, 0x2000  # the DOS track cache (equate.src buffcache)
MAX_CYL = 80  # drive/mfm.inc: DOS pmaxtrk 79, cylinder 80 used by Wheels
STEP_US = 12_000  # WD r1r0 = 01 on WD1770 and WD1772
SETTLE_S = 0.018  # DOS setval (mrout.src reset_ctl)
SPINUP_S = 0x50 * 20_000 / 2_000_000  # DOS motoracc ticks x the controller timer
TB_WRAP_US = 1 << 16
NOMINAL_US = 200_000  # 300 rpm
BYTE_US = 32  # 250 kbit/s MFM
TRACK_BYTES = mfm.TRACK_BYTES
RNF_REVS = 5  # datasheet: Read Sector / Read Address give up after 5 revolutions
ST_BUSY, ST_T0, ST_IP, ST_WP, ST_MO = 0x01, 0x04, 0x02, 0x40, 0x80
ST_FAIL = 0xFF
PA_SIDE, PA_RDY, PA_MOTOR, PA_LED, PA_CHANGE = 0x01, 0x02, 0x04, 0x40, 0x80
PB_WPRT = 0x40
DOS_JOBS, JOB_RESET, JOB_DONE = 0x0002, 0x82, 0x80
DOS_FLUSH_S = 32 * 0.010  # sieeeset controller ticks before DOS writes its cache back
JOB_WAIT_S = 3 * 0.255 + 1.0  # reset_ctl's two 255 ms delays and its register test
STATUS = {"t0": ST_T0, "index": ST_IP, "wprot": ST_WP, "motor": ST_MO}


class TrackError(IOError):
    """The drive refused or could not complete a 1581 operation; ``trace`` holds what
    the drive saw, when it reported it."""

    def __init__(self, message, trace=None):
        super().__init__(message)
        self.trace = trace


class StreamLost(HandshakeTimeout):
    """The reply after a stream was not the drive's J return: the drive is still
    streaming or left its monitor, so nothing more is sent to it (the monitor is
    recovered); ``meta`` holds the stream's report."""

    def __init__(self, message, meta):
        super().__init__(message)
        self.meta = meta


def restore_trace(steps, result, elapsed_us, rs):
    """The drive's account of a bounded Restore (drive/mfm.s P_RS): statuses, whether
    BUSY was up at the first valid read, what ended the wait and the step pulses
    issued (from the track register when the deadline stopped the WD, else the
    elapsed step periods)."""
    trace = {"steps": steps, "result": result}
    if not steps:
        return trace
    first, last, forced, track = rs
    deadline = bool(last & ST_BUSY)
    return trace | {
        "first_status": first,
        "busy_seen": bool(first & ST_BUSY),
        "end": "deadline" if deadline else "busy_fell",
        "last_status": last,
        "forced_status": forced,
        "track_register": track,
        "elapsed_us": elapsed_us,
        "pulses": 0xFF - track if deadline else round(elapsed_us / STEP_US),
    }


def _bits(value, names):
    return {k: bool(value & v) for k, v in names.items()}


def pa_state(pa, pb):
    """Decoded CIA port A/B inputs and outputs (iodef.src, schematic sheet 3)."""
    return {
        "side_select": pa & PA_SIDE,
        "ready": not pa & PA_RDY,
        "motor": not pa & PA_MOTOR,
        "disk_changed": not pa & PA_CHANGE,
        "write_protected": not pb & PB_WPRT,
        "device_switches": pa >> 3 & 3,
    }


class Mfm1581:  # pylint: disable=too-many-instance-attributes
    """WD177x access to one 1581 through a started monitor.

    ``cylinder`` is known after ``home``; ``cache_used`` says the DOS track cache RAM
    was overwritten, so the session must invalidate DOS's cache (``invalidate``).
    """

    def __init__(
        self, mon, sleep=None, settle_s=SETTLE_S, spinup_s=SPINUP_S, loader=drivecode
    ):
        if mon.model != "1581":
            raise ValueError(f"device {mon.dev} is a {mon.model}, not a 1581")
        self.mon, self.sleep, self.loader = mon, sleep or time.sleep, loader
        self.settle_s, self.spinup_s = settle_s, spinup_s
        self.cylinder = self.entry = self.home_trace = None
        self.period_us = NOMINAL_US
        self.cache_used = False
        self.select = 0
        self._block = bytearray(FIELDS["len"] + 2)
        self._overlay = self._p = self._saved = None

    @property
    def streaming(self):
        """Reads stream (s4 and xum1541 firmware v12)."""
        supports = getattr(self.mon.cbm, "supports", None)
        return self.mon.protocol == "s4" and bool(supports and supports("stream"))

    def _load(self, name):
        if self._overlay != name:
            code = self.loader(name)
            self.mon.write(CODE_BASE, code[:SPLIT])
            self.mon.write(CODE2, code[SPLIT:])
            self._p = CODE_BASE + code.index(TAGS[name]) + len(TAGS[name])
            self._overlay = name

    def _set(self, **kw):
        self._load(MFM_CODE)
        for k, v in kw.items():
            width = 2 if k in ("buf", "len") else 1
            self._block[FIELDS[k] : FIELDS[k] + width] = (v & 0xFFFF).to_bytes(
                2, "little"
            )[:width]
        self.mon.write(self._p, bytes(self._block))

    def _call(self, name, **kw):
        self._set(**kw)
        return self.mon.jsr(ENTRY[name])

    def _result(self, at, n):
        return bytes(self.mon.read(self._p + at, n))

    def _stamp(self, at):
        lo, hi, wraps = self._result(at, 3)
        return wraps << 16 | hi << 8 | lo

    def _tmo(self, revolutions):
        return min(255, math.ceil(revolutions * self.period_us / TB_WRAP_US) + 1)

    def open(self):
        """Load the code; remember CIA port A and the WD track register."""
        self._load(MFM_CODE)
        self._saved = self.mon.read(CIA_PA, 1)[0], self._call("sense")[1]
        self.select = self._saved[0] & PA_SIDE
        return self

    def close(self):
        """Motor off, the head back on the cylinder ``estimate`` found it on, the WD
        track register, side select, motor and LED outputs as found."""
        if self._saved is None or not self.mon.running:
            return
        self.motor(False)
        if self.cylinder is not None and self.entry is not None:
            self.seek(self.entry)
        self._call("settrk", arg=self._saved[1])
        pa = self.mon.read(CIA_PA, 1)[0]
        keep = PA_SIDE | PA_MOTOR | PA_LED
        self.mon.write(CIA_PA, bytes([pa & ~keep | self._saved[0] & keep]))
        self._saved = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    def sense(self):
        """Type I status, track register and port state; nothing moves."""
        status, track, pa = self._call("sense")
        pb = self._result(PB_AT, 1)[0]
        return (
            {"status": status, "track_register": track}
            | _bits(status, STATUS)
            | pa_state(pa, pb)
        )

    def motor(self, on):
        """Spindle motor and activity LED; on waits the DOS's spin-up time and measures
        the index period."""
        self._call("motor", arg=int(on))
        if on:
            self.sleep(self.spinup_s)
            self.period_us = self.index_period() or NOMINAL_US

    def side(self, select):
        """CIA PA0 = select (the ID side H; physical head 1 - H)."""
        self._call("side", arg=select)
        self.select = select & PA_SIDE

    def index_period(self):
        """Microseconds between two rising index edges, or None (no index)."""
        a, _, _ = self._call("index", tmo=self._tmo(3))
        if a == ST_FAIL:
            return None
        return (self._stamp(T1_AT) - self._stamp(T0_AT)) % (TB_WRAP_US << 8)

    def read_id(self):
        """The next ID: ``{c, h, r, n, crc_ok, status, time_us}``, or None."""
        status = self._call("readaddr", tmo=self._tmo(RNF_REVS + 1))[0]
        if status == ST_FAIL or status & mfm.ST_RNF:
            return None
        c, h, r, n = self._result(ID_AT, 4)
        crc_ok = not status & mfm.ST_CRC
        return {"c": c, "h": h, "r": r, "n": n, "crc_ok": crc_ok, "status": status}

    def estimate(self):
        """``(cylinder, source)``: a good ID's C on this head, else the WD track register
        (the DOS seeks through it), else 0 when TR00 is sensed."""
        state = self.sense()
        if state["t0"]:
            found = 0, "tr00"
        else:
            ident = self.read_id() if state["motor"] else None
            if ident and ident["crc_ok"] and ident["c"] <= MAX_CYL:
                found = ident["c"], "id"
            else:
                found = state["track_register"], "track_register"
        self.entry = min(found[0], MAX_CYL)
        return found

    def home(self, steps):
        """Restore with at most ``steps`` step pulses (the estimate, where ``close``
        returns the head unless ``estimate`` ran); TR00 must be sensed at the end and
        again after the settling time, or TrackError (with ``home_trace``) is raised
        with the head where the pulses left it."""
        if not 0 <= steps <= MAX_CYL:
            raise TrackError(f"homing needs 0..{MAX_CYL} steps, not {steps}")
        if self.entry is None:
            self.entry = steps
        self.cylinder = None
        a, _, _ = self._call("restore", arg=steps)
        trace = self.home_trace = restore_trace(
            steps, a, self._stamp(T1_AT), self._result(RS_AT, 4)
        )
        if a:
            raise TrackError(
                f"TR00 not sensed within {steps} steps (status ${a:02X})", trace
            )
        self.sleep(self.settle_s)
        settled = self.sense()
        trace["settled_status"], trace["settled_t0"] = settled["status"], settled["t0"]
        if not trace["settled_t0"]:
            raise TrackError("TR00 sensed by the Restore but not after settling", trace)
        self.cylinder = 0

    def seek(self, cylinder):
        """Seek from the homed track register; settles."""
        if self.cylinder is None:
            raise TrackError("seek before homing")
        if not 0 <= cylinder <= MAX_CYL:
            raise TrackError(f"cylinder {cylinder} outside 0..{MAX_CYL}")
        if cylinder != self.cylinder:
            self._call("seek", arg=cylinder)
            self.cylinder = cylinder
            self.sleep(self.settle_s)

    def _buffer(self, data):
        if BUFFER + len(data) > BUFFER_END:
            raise TrackError(f"{len(data)} bytes exceed the track cache buffer")
        self.cache_used = True
        self.mon.write(BUFFER, bytes(data))

    def read_sector(self, track_id, sector, size=mfm.SECTOR_BYTES):
        """Read Sector into the buffer: ``(data, status)``."""
        self.cache_used = True
        status, lo, hi = self._call(
            "readsec",
            arg=track_id,
            sec=sector,
            buf=BUFFER,
            len=size,
            tmo=self._tmo(RNF_REVS + 1),
        )
        n = min(lo | hi << 8, size)
        return np.frombuffer(self.mon.read(BUFFER, n), np.uint8), status

    def write_sectors(self, track_id, first, data, deleted=False):
        """Write Sector of equal-size ``data`` rows to sectors first, first + 1, ...
        back to back; returns ``(status, written)``."""
        data = np.asarray(data, np.uint8).reshape(len(data), -1)
        self._buffer(data.tobytes())
        status, written, _ = self._call(
            "writesec",
            arg=track_id,
            sec=first,
            cnt=len(data),
            cyl=self.cylinder,
            flags=int(deleted),
            buf=BUFFER,
            len=data.shape[1],
            tmo=self._tmo(RNF_REVS + 1),
        )
        return status, written

    def write_track(self, image):
        """Write Track from an RLE image (:func:`mfm.rle`); returns the status."""
        self._buffer(np.asarray(image, np.uint8).tobytes())
        return self._call("writetrk", cyl=self.cylinder, buf=BUFFER, tmo=self._tmo(3))[
            0
        ]

    def stream(self, entries):
        """Run a command list (:func:`mfmstream.entry`) streaming: MfmStream."""
        if not self.streaming:
            raise TrackError("streaming needs s4 and xum1541 firmware v12")
        revs = 2 + sum(_revs(e) * (e[3] & ms.REP_MAX) for e in entries)
        self._load(STREAM_CODE)
        self.mon.write(self._p, ms.command_list(entries))
        size = 64 * math.ceil(2 * revs * (TRACK_BYTES + 64) / 64)
        mon = self.mon
        tmo = self._tmo(RNF_REVS + 1)
        self.mon.write(self._p + LIST_TMO, bytes([tmo]))
        mon.transact(b"J" + struct.pack("<H", CODE_BASE))
        t0 = mon.clock()
        raw = mon.cbm.srq2_stream(size)
        elapsed = mon.clock() - t0
        reply = mon.link.response(3)
        mon.touch()
        got = ms.MfmStream.parse(raw, reply)
        got.elapsed_s = round(elapsed, 6)
        if not got.in_step:
            mon.running = False
            reps = sum(e[3] & ms.REP_MAX for e in entries)
            got.state = self._dos_state((reps * tmo + FORCE_WRAPS) * TB_WRAP_US)
            raise StreamLost(
                f"stream ended {got.adapter}, drive out of step", _meta(got)
            )
        if not got.complete:
            got.state = stream_state(mon.read(STATE_AT, STATE_LEN))
        return got

    def _dos_state(self, stream_us):
        """The stream state over DOS M-R, or why it could not be read. The drive is in
        DOS at the latest once its stream has ended (stream_us), its J reply has waited
        WATCHDOG_S for the host and its monitor WATCHDOG_IDLE_S for a command (the
        reply may have been taken); M-R is tried every WATCHDOG_S until then. An
        adapter on a virtual clock advances it instead of sleeping."""
        wait = getattr(self.mon.cbm, "host_wait", None) or self.sleep
        limit = stream_us / 1e6 + WATCHDOG_S + WATCHDOG_IDLE_S
        tries = math.ceil(limit / WATCHDOG_S) + 1
        error = None
        for i in tqdm(range(tries), desc="drive state", unit="try", leave=False):
            try:
                raw = self.mon.cbm.download(self.mon.dev, STATE_AT, STATE_LEN)
            except (IOError, ValueError) as e:
                error = f"{type(e).__name__}: {e}"
                wait(WATCHDOG_S)
                continue
            return stream_state(raw) | {"answered_s": i * WATCHDOG_S}
        return {"error": error, "waited_s": (tries - 1) * WATCHDOG_S}

    def read_track(self, revolutions=1):
        """Read Track ``revolutions`` times: a "track" MfmCapture."""
        got = self.stream([ms.entry(ms.OP_READ_TRACK, self.cylinder, rep=revolutions)])
        cmds = got.commands
        return MfmCapture(
            "track",
            self.cylinder,
            self._head(),
            data=np.concatenate([c.data for c in cmds]) if cmds else [],
            rev_offsets=np.cumsum([0] + [len(c.data) for c in cmds]),
            rev_start_us=[c.t_first for c in cmds],
            rev_end_us=[c.t_end for c in cmds],
            rev_status=[c.status for c in cmds],
            meta=_meta(got),
        )

    def read_ids(self, count):
        """Index, ``count`` Read Address commands, two indexes: an "ids" MfmCapture."""
        got = self.stream(
            [ms.entry(ms.OP_INDEX), ms.entry(ms.OP_READ_ADDRESS, rep=count)]
            + [ms.entry(ms.OP_INDEX, rep=2)]
        )
        cmds = [c for c in got.commands if len(c.data) == 6 or c.status & mfm.ST_RNF]
        return MfmCapture(
            "ids",
            self.cylinder,
            self._head(),
            ids=np.array([c.data for c in cmds if len(c.data) == 6]).reshape(-1, 6),
            id_status=[c.status for c in cmds if len(c.data) == 6],
            id_us=[c.t_first for c in cmds if len(c.data) == 6],
            index_us=got.index_us,
            meta=_meta(got),
        )

    def read_sectors(self, track_id, first, count):
        """Read Sector of sectors first.. in turn: ``[(track_id, r, data, status)]``."""
        if self.streaming:
            got = self.stream(
                [ms.entry(ms.OP_READ_SECTOR, track_id, first, count, True)]
            )
            if not got.complete:
                raise TrackError(f"stream ended {got.adapter}/{got.drive_end}")
            return [
                (track_id, first + i, c.data, c.status)
                for i, c in enumerate(got.commands)
            ]
        return [
            (track_id, r, *self.read_sector(track_id, r))
            for r in range(first, first + count)
        ]

    def _head(self):
        return mfm.head_side(self.select)


def _revs(e):
    """Revolutions an entry may take: Read Track waits for an index and reads one."""
    return {ms.OP_READ_TRACK: 2, ms.OP_INDEX: 1}.get(e[0], 1)


def stream_state(raw):
    """drive/mfmstream.s's state block, cleared when the stream starts: entries
    started, the list position, whether a command's first data byte was written
    (first_set), the stamps in microseconds, the WD status (read once valid after the
    last command write, until a record replaced it), flags and the data bytes (set by a
    record or an ATN abort)."""
    b = bytes(raw)
    return {
        "first_set": any(b[6:11]),
        "entries_started": b[0],
        "op": b[1],
        "rep_left": b[2],
        "list_at": b[3],
        "t_first_us": ms.stamp_us(b[6:11]),
        "t_end_us": ms.stamp_us(b[11:16]),
        "wd_status": b[16],
        "flags": b[17],
        "count": b[18] | b[19] << 8,
    }


def _meta(got):
    meta = {"adapter": got.adapter, "drive_end": got.drive_end, "reply": got.reply}
    if not (got.complete and got.in_step):
        meta["diagnosis"] = got.diagnosis()
    return meta


def invalidate(cbm, dev, sleep=None, clock=time.monotonic):
    """Run DOS job $82 (controller reset: cache invalidated, no head motion) through
    the documented job queue and wait for it; True when it finished."""
    sleep = sleep or time.sleep
    cbm.upload(dev, DOS_JOBS, bytes([JOB_RESET]))
    deadline = clock() + JOB_WAIT_S
    while clock() < deadline:
        if cbm.download(dev, DOS_JOBS, 1)[0] < JOB_DONE:
            return True
        sleep(0.05)
    return False


@contextlib.contextmanager
def session(cbm, dev, protocol="s4", sleep=None, writes=False):
    """Monitor and Mfm1581; DOS gets DOS_FLUSH_S first when the cache RAM will be
    written, and its cache is invalidated afterwards whenever it was."""
    sleep = sleep or time.sleep
    if writes:
        sleep(DOS_FLUSH_S)
    with Monitor(cbm, dev, protocol) as mon:
        drive = Mfm1581(mon, sleep=sleep)
        with drive:
            yield drive
    if drive.cache_used and not invalidate(cbm, dev, sleep):
        raise TrackError("DOS job $82 did not finish: power-cycle before using DOS")
