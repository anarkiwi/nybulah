"""Run `nybulah pattern compare` over saved capture sets and tabulate per-capture
exact-group errors and slips, optionally against an earlier run.

usage: python tools/pattern_sets.py OUT DIR[=TRUTH] [...] [--before OLD] [-j N]

Each DIR's captures (*.npz other than probes) are compared with DIR/truth.json
or TRUTH; reports land in OUT/<DIR name>.json.
"""

import argparse
import json
import os
import pathlib
from concurrent.futures import ProcessPoolExecutor, as_completed

from tqdm import tqdm


def _run(spec, out):
    from nybulah import pattern  # pylint: disable=import-outside-toplevel
    from nybulah.nibbler import Capture  # pylint: disable=import-outside-toplevel

    path, _, truth = spec.partition("=")
    path = pathlib.Path(path)
    truth = pattern._load_truth(  # pylint: disable=protected-access
        pathlib.Path(truth) if truth else path / pattern.TRUTH
    )
    caps = sorted(p for p in path.glob("*.npz") if not p.name.startswith("probe"))
    named = [(p.name, Capture.load(p)) for p in caps]
    report = pattern.compare(truth, named)
    dest = pathlib.Path(out) / f"{path.parent.name}-{path.name}.json"
    dest.write_text(json.dumps(report), encoding="utf-8")
    return dest


def scores(report):
    """Per capture name: (errors, slips, revolution bits) over exact groups."""
    out = {}
    for c in report["captures"]:
        exact = [g for g in c["groups"].values() if g["kind"] != "unstable"]
        out[c["name"]] = (
            sum(g["errors"] for g in exact),
            sum(g["slips"] for g in exact),
            c["revolution_bits"],
        )
    return out


def table(out, before=None):
    """Lines of ``set capture errors slips [before errors slips]``."""
    lines = []
    for dest in sorted(pathlib.Path(out).glob("*.json")):
        now = scores(json.loads(dest.read_text(encoding="utf-8")))
        old = {}
        if before and (pathlib.Path(before) / dest.name).exists():
            old = scores(json.loads((pathlib.Path(before) / dest.name).read_text()))
        for name, (err, slips, rev) in now.items():
            prev = old.get(name)
            was = f"{prev[0]:>6} {prev[1]:>4} " if prev else ""
            lines.append(f"{dest.stem:28s} {name:20s} {was}{err:>6} {slips:>4} {rev}")
    return lines


def main():
    """Run the sets in parallel, then print the table."""
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("sets", nargs="*")
    ap.add_argument("--before")
    ap.add_argument("-j", type=int, default=os.cpu_count() // 2)
    args = ap.parse_args()
    pathlib.Path(args.out).mkdir(parents=True, exist_ok=True)
    with ProcessPoolExecutor(args.j) as pool:
        jobs = [pool.submit(_run, s, args.out) for s in args.sets]
        for job in tqdm(as_completed(jobs), total=len(jobs), unit="set"):
            job.result()
    print("\n".join(table(args.out, args.before)))


if __name__ == "__main__":
    main()
