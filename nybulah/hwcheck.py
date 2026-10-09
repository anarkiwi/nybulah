"""Batched hardware session: identify, RAM probe, transfer bench and alias check.

Every step is isolated: a failure is logged, the drive is recovered and the
session moves on. Records stream as JSON lines; a summary closes the log.
"""

import json
import os
import pathlib
import sys
import time

from . import bench, disk, disk1581, r1581, ramprobe, tool
from .bus import recover
from .monitor import Monitor, supported
from .nibbler import Nibbler


class Session:
    """Runs steps against one adapter and streams their records."""

    def __init__(self, cbm, log, recover_timeout=None, archive=None):
        self.cbm, self.log, self.recover_timeout = cbm, log, recover_timeout
        self.archive = archive
        self.records = []

    def emit(self, rec):
        """Append a record to the log and echo it."""
        self.records.append(rec)
        line = json.dumps(rec)
        self.log.write(line + "\n")
        self.log.flush()
        print(line, flush=True)

    def step(self, name, dev, fn):
        """Run fn; on failure recover dev. Returns the result or None."""
        rec = {"step": name, "dev": dev, "time": time.time()}
        t0 = time.perf_counter()
        try:
            rec["result"], rec["ok"] = fn(), True
        except Exception as e:  # pylint: disable=broad-exception-caught
            rec.update(ok=False, error=f"{type(e).__name__}: {e}")
            rec["recovered"] = getattr(e, "recovered", None)
            if rec["recovered"] is None:
                try:
                    rec["recovered"] = recover(
                        self.cbm, dev, timeout=self.recover_timeout
                    )
                except Exception as r:  # pylint: disable=broad-exception-caught
                    rec["recover_error"] = f"{type(r).__name__}: {r}"
        rec["seconds"] = time.perf_counter() - t0
        self.emit(rec)
        return rec.get("result")

    def skip(self, name, dev, reason):
        """Record a step that cannot run."""
        self.emit({"step": name, "dev": dev, "ok": None, "skipped": reason})


def disk_step(cbm, dev, proto, model, allow_bump=False, archive=None):
    """Read one track per density zone without writing anything; ``archive``
    saves the captures."""
    with (
        Monitor(cbm, dev, proto) as mon,
        Nibbler(mon, model, allow_bump=allow_bump) as nib,
    ):
        return disk.survey(nib, archive=archive)


def disk_1581(cbm, dev, proto):
    """The 1581 dry probe with headers: status, index period, an ID; no stepping."""
    with r1581.session(cbm, dev, proto) as drive:
        return disk1581.dry(drive, headers=True)


FAST = ("s3", "s4")  # protocols that can run a 1571 at 2 MHz


def bench_skip(cbm, proto, model, base, size):
    """Why proto cannot be benched here, else None."""
    if base is None:
        return f"no unaliased RAM run of {size} bytes"
    if proto == "s4" and model not in ("1571", "1581"):
        return "s4 needs a 1571 or 1581"
    if not supported(cbm, proto):
        return f"{proto} not supported here"
    return None


def check_dev(  # pylint: disable=too-many-arguments
    session, dev, protos, size, reps, disk_check=False, allow_bump=False, fast=False
):
    """All steps for one drive; fast adds 2 MHz benches of s3/s4 on a 1571."""
    cbm = session.cbm
    session.step("identify", dev, lambda: list(cbm.identify(dev)))
    probe = session.step("ramprobe", dev, lambda: ramprobe.probe(cbm, dev))
    model = probe and probe["model"]
    if model == "1581":
        size = min(size, ramprobe.CACHE_1581[1] - ramprobe.CACHE_1581[0])
    base = probe and ramprobe.expansion_base(probe, size)
    for proto in protos:
        reason = bench_skip(cbm, proto, model, base, size)
        if reason:
            session.skip(f"bench_{proto}", dev, reason)
            continue
        clocks = (
            (False, True) if fast and model == "1571" and proto in FAST else (False,)
        )
        for clock in clocks:
            name, pattern = proto + ("_2mhz" if clock else ""), os.urandom(size)
            session.step(
                f"bench_{name}",
                dev,
                lambda p=proto, d=pattern, c=clock: bench.run(
                    cbm, dev, base, size, reps, p, d, fast=c
                ),
            )
            session.step(
                f"alias_{name}",
                dev,
                lambda d=pattern: ramprobe.verify(cbm, dev, base, d),
            )
    if model == "1581":
        session.step("dos_cache", dev, lambda: r1581.invalidate(cbm, dev))
        if disk_check:
            session.step("disk", dev, lambda: disk_1581(cbm, dev, protos[0]))
        return
    if not disk_check:
        return
    if base is None:
        session.skip("disk", dev, "no expansion RAM")
    elif not supported(cbm, protos[0]):
        session.skip("disk", dev, f"{protos[0]} not supported here")
    else:
        model = probe["model"]
        session.step(
            "disk",
            dev,
            lambda: disk_step(
                cbm,
                dev,
                protos[0],
                model,
                allow_bump,
                session.archive and session.archive / f"dev{dev}",
            ),
        )


def summarize(records):
    """Per device: model, RAM runs, rates, errors and failed steps."""
    out = {}
    for r in records:
        s = out.setdefault(str(r["dev"]), {"failed": [], "skipped": []})
        res, step = r.get("result"), r["step"]
        if r["ok"] is False:
            s["failed"].append(step)
        elif r["ok"] is None:
            s["skipped"].append(step)
        elif step == "identify":
            s["identify"] = res
        elif step == "ramprobe":
            s.update(model=res["model"], ram=res["ram"])
        elif step.startswith("bench_"):
            s[step] = {k: res[k]["bytes_per_s"] for k in ("mr", "write", "read")} | {
                "errors": res["errors"],
                "addr": res["addr"],
            }
        elif step.startswith("alias_"):
            s[step] = res["mismatched"]
        elif step == "disk":
            s[step] = res
    return out


def add_arguments(ap):
    """Command line options."""
    ap.add_argument("--devs", type=int, nargs="+", default=[8, 10])
    ap.add_argument("--s2", action="store_true", help="bench S2 instead of S1")
    ap.add_argument("--proto", action="append", default=[], help="extra protocol")
    ap.add_argument(
        "--fast", action="store_true", help="also bench s3/s4 on a 1571 at 2 MHz"
    )
    ap.add_argument("--size", type=int, default=8192)
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--out", type=pathlib.Path, default=pathlib.Path("artifacts"))
    ap.add_argument(
        "--recover-timeout",
        type=float,
        help="outer limit after RESET (default: the longest derived boot)",
    )
    ap.add_argument(
        "--disk", action="store_true", help="read one track per zone (no writes)"
    )
    ap.add_argument(
        "--allow-bump",
        action="store_true",
        help="1541: bump the head against the stop if nothing else locates it",
    )


def execute(args, cbm):
    """Run the session; return the summary."""
    protos = ["s2" if args.s2 else "s1"] + args.proto
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"hwcheck-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    with path.open("w") as log:
        session = Session(cbm, log, args.recover_timeout, path.with_suffix(""))
        for dev in args.devs:
            check_dev(
                session,
                dev,
                protos,
                args.size,
                args.reps,
                args.disk,
                args.allow_bump,
                args.fast,
            )
        summary = summarize(session.records)
        session.emit({"summary": summary, "log": str(path)})
    return summary


def main(argv=None, cbm=None):
    """CLI entry point."""
    return tool.standalone(sys.modules[__name__], argv, cbm)


if __name__ == "__main__":
    main()
