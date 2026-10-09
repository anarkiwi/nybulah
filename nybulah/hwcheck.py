"""Batched hardware session: identify, RAM probe, transfer bench and alias check.

Every step is isolated: a failure is logged, the drive is recovered and the
session moves on. Records stream as JSON lines; a summary closes the log.
"""

import json
import os
import pathlib
import sys
import time

from . import bench, ramprobe, tool
from .monitor import recover, supported


class Session:
    """Runs steps against one adapter and streams their records."""

    def __init__(self, cbm, log, recover_timeout=3.0):
        self.cbm, self.log, self.recover_timeout = cbm, log, recover_timeout
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


def check_dev(session, dev, protos, size, reps):
    """All steps for one drive."""
    cbm = session.cbm
    session.step("identify", dev, lambda: list(cbm.identify(dev)))
    probe = session.step("ramprobe", dev, lambda: ramprobe.probe(cbm, dev))
    base = probe and ramprobe.expansion_base(probe, size)
    for proto in protos:
        if base is None:
            session.skip(f"bench_{proto}", dev, f"no unaliased RAM run of {size} bytes")
        elif not supported(cbm, proto):
            session.skip(f"bench_{proto}", dev, f"{proto} not supported here")
        else:
            pattern = os.urandom(size)
            session.step(
                f"bench_{proto}",
                dev,
                lambda p=proto, d=pattern: bench.run(cbm, dev, base, size, reps, p, d),
            )
            session.step(
                f"alias_{proto}",
                dev,
                lambda d=pattern: ramprobe.verify(cbm, dev, base, d),
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
    return out


def add_arguments(ap):
    """Command line options."""
    ap.add_argument("--devs", type=int, nargs="+", default=[8, 10])
    ap.add_argument("--s2", action="store_true", help="bench S2 instead of S1")
    ap.add_argument("--proto", action="append", default=[], help="extra protocol")
    ap.add_argument("--size", type=int, default=8192)
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--out", type=pathlib.Path, default=pathlib.Path("artifacts"))
    ap.add_argument("--recover-timeout", type=float, default=3.0)


def execute(args, cbm):
    """Run the session; return the summary."""
    protos = ["s2" if args.s2 else "s1"] + args.proto
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"hwcheck-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    with path.open("w") as log:
        session = Session(cbm, log, args.recover_timeout)
        for dev in args.devs:
            check_dev(session, dev, protos, args.size, args.reps)
        summary = summarize(session.records)
        session.emit({"summary": summary, "log": str(path)})
    return summary


def main(argv=None, cbm=None):
    """CLI entry point."""
    return tool.standalone(sys.modules[__name__], argv, cbm)


if __name__ == "__main__":
    main()
