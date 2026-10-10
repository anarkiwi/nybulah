"""1581 drive code on VICE's emulated drive: write and stream scenarios with traces.

``python -m nybulah.vicebench writes|stream [--drivecode DIR]`` runs this tree's drive
code, or another build's, and prints a JSON report (docs/vice.md).
"""

import argparse
import json
import pathlib
import shutil
import sys
import tempfile

import numpy as np

from . import r1581, vice
from . import mfmstream as ms
from .analysis import mfm
from .formats import d81
from .monitor import drivecode

UNIT = 9
SPACE = vice.memspace(UNIT)
IO_1581 = {
    **{0x4000 + i: n for i, n in enumerate("PRA PRB DDRA DDRB".split())},
    **{0x4004 + i: n for i, n in enumerate("TALO TAHI TBLO TBHI".split())},
    **{0x400C + i: n for i, n in enumerate("SDR ICR CRA CRB".split())},
    **{0x6000 + i: n for i, n in enumerate("WDCMD WDTRK WDSEC WDDAT".split())},
}
STREAM_ENTRY = r1581.CODE_BASE
STREAM_TAG = b"NYMS"
TAG_TMO = 4 * ms.LIST_ENTRIES
PATTERN = [0x11, 0x22, 0x22, 0x22, 0x33, 0x44, 0x44, 0x44]
PATTERN_REPEATS = 400
IO_SPAN = 0x10


def loader(directory=None):
    """Drive code by name from directory, else this package's build."""
    if directory is None:
        return drivecode
    root = pathlib.Path(directory)
    return lambda name: (root / f"{name}.bin").read_bytes()


def random_d81(seed=1):
    """A D81 of random sectors."""
    rng = np.random.default_rng(seed)
    return d81.D81(rng.integers(0, 256, (d81.D81_SECTORS, 256), dtype=np.uint8))


def io_trace(history, regs=None):
    """CIA and WD register accesses in a drive history: rows of (clock, pc, register,
    value), value -1 where the instruction neither loads nor stores it plainly."""
    regs = IO_1581 if regs is None else regs
    out = []
    for lo in sorted({a & ~0xFFF for a in regs}):
        at, addr, value = vice.accesses(history, lo, lo + IO_SPAN - 1)
        out += zip(at.tolist(), addr.tolist(), value.tolist())
    out.sort()
    return [
        {
            "clock": int(history["clock"][i]),
            "pc": int(history["pc"][i]),
            "reg": regs.get(a, f"${a:04X}"),
            "value": v,
        }
        for i, a, v in out
    ]


WD_STATUS, WD_DATA, IO_SDR = 0x6000, 0x6003, 0x400C


def wd_commands(history):
    """Per WD command written in a drive history: its code, cycles from the write to
    the first status read showing DRQ and to the first data register access, the
    data accesses, the longest gap between them after the second (and the PCs run in
    it), the last status."""
    at, addr, value = vice.accesses(history, WD_STATUS, WD_DATA)
    store = np.isin(history["op"][at, 0], sum(vice.STORES.values(), ()))
    clock = history["clock"][at].astype(np.int64)
    cmds = np.flatnonzero(store & (addr == WD_STATUS))
    bounds = np.append(cmds[1:], len(at))
    out = []
    for c, end in zip(cmds, bounds):
        span = np.arange(c + 1, end)
        status = span[~store[span] & (addr[span] == WD_STATUS) & (value[span] >= 0)]
        data = span[addr[span] == WD_DATA]
        drq = status[(value[status] & mfm.ST_DRQ) != 0]
        gaps = np.diff(clock[data])[1:]
        worst = int(np.argmax(gaps)) + 1 if len(gaps) else None
        pcs = (
            history["pc"][at[data[worst]] : at[data[worst + 1]] + 1].tolist()
            if worst is not None
            else []
        )
        out.append(
            {
                "command": _status(int(value[c])),
                "first_drq": int(clock[drq[0]] - clock[c]) if len(drq) else None,
                "first_data": int(clock[data[0]] - clock[c]) if len(data) else None,
                "data": len(data),
                "max_gap": int(gaps.max()) if len(gaps) else None,
                "max_gap_pcs": " ".join(f"{p:04X}" for p in pcs),
                "status": _status(int(value[status[-1]])) if len(status) else None,
            }
        )
    return out


def summarise(trace, limit=40):
    """The first and last limit accesses of an io_trace, clocks from the first."""
    if not trace:
        return []
    t0 = trace[0]["clock"]
    keep = trace if len(trace) <= 2 * limit else trace[:limit] + trace[-limit:]
    return [
        f"{r['clock'] - t0:>9} ${r['pc']:04X} {r['reg']:<5} "
        + (f"${r['value']:02X}" if r["value"] >= 0 else "--")
        for r in keep
    ]


class Bench:
    """VICE with a 1581 on UNIT holding a D81, the drive parked under a DriveMonitor
    and an opened :class:`r1581.Mfm1581` on it; ``receiver`` on x128."""

    def __init__(self, image, code=None, machine="x64sc", receiver=False):
        self._tmp = pathlib.Path(tempfile.mkdtemp(prefix="vicebench-"))
        self.path = self._tmp / "disk.d81"
        self.path.write_bytes(d81.write_d81(image))
        self.load = loader(code)
        self.vice = vice.Vice({UNIT: ("1581", self.path)}, machine)
        self.receiver = None
        if receiver:
            self.receiver = vice.C128Receiver(self.vice, self.load("vicerx_c128"))
            self.receiver.start()
        self.mon = vice.DriveMonitor(self.vice, UNIT).start()
        self.drive = r1581.Mfm1581(self.mon, sleep=self.mon.sleep, loader=self.load)
        self.drive.open()

    def place(self, cylinder, side):
        """Motor on, bounded Restore from the ID's cylinder, seek, side."""
        self.drive.motor(True)
        self.drive.estimate()
        self.drive.home(self.drive.entry)
        self.drive.seek(cylinder)
        self.drive.side(side)

    def image(self):
        """Quit the emulator (VICE writes the disk back) and read the D81."""
        self.vice.close()
        return d81.read_d81(self.path.read_bytes())

    def close(self):
        """Quit the emulator and drop the disk."""
        self.vice.close()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _status(st):
    return f"${st:02X}"


def write_track_case(bench, image):
    """Write Track of an RLE image: its status."""
    status = bench.drive.write_track(image)
    return {"status": _status(status), "ok": not status & (r1581.ST_WP | mfm.ST_LOST)}


def write_sectors_case(bench, cylinder, rows, deleted):
    """Write Sector of rows from sector 1, then Read Sector of each: statuses, data
    and deleted marks as written."""
    c0 = bench.vice.clock(SPACE)
    status, written = bench.drive.write_sectors(cylinder, 1, rows, deleted)
    history = bench.mon.history()
    commands = wd_commands(history[history["clock"] > c0])
    reads = [bench.drive.read_sector(cylinder, r + 1) for r in range(len(rows))]
    marks = [bool(st & mfm.ST_DELETED) for _, st in reads]
    return {
        "status": _status(status),
        "written": written,
        "lost_data": bool(status & mfm.ST_LOST),
        "read_status": [_status(st) for _, st in reads],
        "data_ok": all(np.array_equal(d, w) for (d, _), w in zip(reads, rows)),
        "marks_ok": marks == [deleted] * len(rows),
        "last_commands": commands,
    }


def writes(code=None, cylinder=39, side=0, seed=1):
    """Write Track (a pattern, then the standard layout), Write Sector (data marks,
    then deleted marks) on one side; the D81 VICE writes back is checked against
    the last sectors written and the rest of the disk."""
    image = random_d81(seed)
    rng = np.random.default_rng(seed + 1)
    rows = rng.integers(0, 0xF5, (mfm.SECTORS, mfm.SECTOR_BYTES), dtype=np.uint8)
    report = {"cylinder": cylinder, "side": side}
    bench = Bench(image, code)
    try:
        bench.place(cylinder, side)
        report["pattern"] = write_track_case(
            bench, mfm.rle(PATTERN * PATTERN_REPEATS + [mfm.GAP_BYTE])
        )
        plan = mfm.plan_track(mfm.standard_layout(cylinder, side))
        report["layout"] = write_track_case(bench, plan.image)
        for deleted in (True, False):
            report[f"sectors_deleted_{int(deleted)}"] = write_sectors_case(
                bench, cylinder, rows, deleted
            )
        back = bench.image()
    finally:
        bench.close()
    at = d81.side_rows(cylinder, side)
    rest = np.setdiff1d(np.arange(d81.D81_SECTORS), at)
    report["d81_side_ok"] = bool(
        np.array_equal(back.data[at].reshape(rows.shape), rows)
    )
    report["d81_rest_ok"] = bool(np.array_equal(back.data[rest], image.data[rest]))
    return report


def stream_code(code, list_block, tmo, code2):
    """(segments, list address) of a stream build: its two parts at CODE_BASE and
    code2, the list and TMO written after its tag."""
    at = code.index(STREAM_TAG) + len(STREAM_TAG)
    body = bytearray(code)
    body[at : at + len(list_block)] = list_block
    body[at + TAG_TMO] = tmo
    split = r1581.SPLIT
    return [(r1581.CODE_BASE, bytes(body[:split])), (code2, bytes(body[split:]))]


def stream(  # pylint: disable=too-many-locals
    code=None,
    cylinder=39,
    side=0,
    revolutions=2,
    code2=r1581.CODE2,
    under=None,
    head_writes=0,
    seed=1,
):
    """Read Track streamed by mfmstream_1581 (from code) to the emulated C128, the
    head placed by the mfm_1581 of under: the parsed stream, the drive's return,
    sectors decoded from it against the D81, the I/O trace (from the call to its
    head_writes-th SDR write, and at the end)."""
    image = random_d81(seed)
    with Bench(image, under, machine="x128", receiver=True) as bench:
        bench.place(cylinder, side)
        entry = ms.entry(ms.OP_READ_TRACK, cylinder, rep=revolutions)
        tmo = bench.drive._tmo(r1581.RNF_REVS + 1)  # pylint: disable=protected-access
        segments = stream_code(
            loader(code)(r1581.STREAM_CODE), ms.command_list([entry]), tmo, code2
        )
        for addr, part in segments:
            bench.mon.write(addr, part)
        c0 = bench.vice.clock(SPACE)
        bench.mon.call(STREAM_ENTRY)
        head = _head(bench, head_writes)
        try:
            a, x, y = bench.mon.finish()
            returned = {"a": _status(a), "x": x, "y": _status(y)}
        except vice.ViceError as e:
            returned = {"error": str(e)}
        history = bench.mon.history()
        data, lines = bench.receiver.received()
    got = ms.MfmStream.parse(vice.adapter_raw(data, lines))
    report = {
        "returned": returned,
        "bytes": len(data),
        "adapter": got.adapter,
        "drive_end": got.drive_end,
        "keepalives": got.keepalives,
        "commands": [
            {"bytes": len(c.data), "status": _status(c.status), "timeout": c.timeout}
            for c in got.commands
        ],
        "metadata_head": [f"${v:02X}" for v in data[(lines & 0x40) == 0][:8]],
        "drive_cycles": int(history["clock"][-1]) - c0 if len(history) else 0,
        "head": [
            f"{r['clock'] - c0:>8} ${r['pc']:04X} {r['reg']:<5} "
            + (f"${r['value']:02X}" if r["value"] >= 0 else "--")
            for r in io_trace(head[head["clock"] >= c0])
        ],
        "trace": summarise(io_trace(history[history["clock"] >= c0])),
    }
    rows = d81.side_rows(cylinder, side)
    want = image.data[rows].reshape(mfm.SECTORS, -1)
    report["sectors_ok"] = [
        _sectors_match(c.data, want, cylinder, side) for c in got.commands
    ]
    return report


def _head(bench, count):
    """The drive history from the call to its count-th SDR store: a store checkpoint
    stops the emulator at each (the receiver's port reads keep the drive in step)."""
    if not count:
        return np.zeros(0, vice.HISTORY_DTYPE)
    mon = bench.vice.mon
    cp = mon.checkpoint(IO_SDR, op=vice.OP_STORE, space=SPACE)
    parts = []
    try:
        for _ in range(count):
            mon.resume()
            if not mon.wait_stopped(bench.mon.call_timeout):
                mon.stop()
                break
            parts.append(bench.mon.history())
    finally:
        mon.delete(cp)
    rows = np.concatenate(parts) if parts else np.zeros(0, vice.HISTORY_DTYPE)
    _, first = np.unique(rows["clock"], return_index=True)
    return rows[first]


def _sectors_match(data, want, cylinder, side):
    """Sectors 1..10 decoded from a Read Track with good CRCs and the D81's data."""
    if not np.size(data):
        return 0
    track = mfm.decode_track(np.asarray(data, np.uint8), cylinder=cylinder, side=side)
    ok = 0
    for i, rec in enumerate(track.sectors):
        r = int(rec["r"])
        if rec["data_ok"] and 1 <= r <= mfm.SECTORS:
            ok += np.array_equal(track.payload(i), want[r - 1])
    return int(ok)


def main(argv=None):
    """CLI: the writes or stream scenario as JSON."""
    parser = argparse.ArgumentParser(prog="python -m nybulah.vicebench")
    parser.add_argument("scenario", choices=("writes", "stream"))
    parser.add_argument("--drivecode", help="directory of another build's .bin files")
    parser.add_argument("--under", help="stream: the build that places the head")
    parser.add_argument("--head", type=int, default=0, help="stream: SDR writes traced")
    parser.add_argument("--cylinder", type=int, default=39)
    parser.add_argument("--side", type=int, default=0)
    parser.add_argument("--revolutions", type=int, default=2)
    parser.add_argument(
        "--code2", type=lambda s: int(s, 0), default=r1581.CODE2, help="stream part 2"
    )
    args = parser.parse_args(argv)
    if args.scenario == "writes":
        report = writes(args.drivecode, args.cylinder, args.side)
    else:
        report = stream(
            args.drivecode,
            args.cylinder,
            args.side,
            args.revolutions,
            args.code2,
            under=args.under,
            head_writes=args.head,
        )
    json.dump(report, sys.stdout, indent=1)
    sys.stdout.write("\n")
    return report


if __name__ == "__main__":
    main()
