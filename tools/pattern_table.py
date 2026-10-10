"""Tabulate `nybulah pattern compare` reports: per capture, errors/slips of exact
groups, the measured sync runs and the speed excursions.

usage: python tools/pattern_table.py REPORT.json [REPORT.json ...]
"""

import json
import pathlib
import sys


def rows(report):
    """One line per capture."""
    for c in report["captures"]:
        exact = {k: g for k, g in c["groups"].items() if g["kind"] != "unstable"}
        bad = {
            k: f"{g['errors']}e{g['ins']}i{g['del']}d"
            for k, g in exact.items()
            if g["errors"] or g["slips"]
        }
        syncs = {
            s["region"].split(".")[0]: s["found"]
            for s in c["syncs"]
            if not s["region"].startswith("dos")
        }
        exc = [
            (x["angle"]["pattern"][0], round(x["peak_pct"], 2))
            for x in c.get("speed", {}).get("excursions", [])
        ]
        name = pathlib.Path(c["name"]).stem
        yield (
            f"{name:14s} start={c['start_angle']['pattern'][0]:<7} "
            f"errors={sum(g['errors'] for g in exact.values()):<4} "
            f"slips={sum(g['slips'] for g in exact.values()):<4} "
            f"bad={bad} syncs={syncs} speed={exc}"
        )


def main(paths):
    for path in paths:
        report = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        print(f"== {path}")
        for p, s in report["summary"].items():
            if "bit_errors" in s:
                print(f"  {p}: bit_errors={s['bit_errors']} slips={s['slips']}")
        for line in rows(report):
            print("  " + line)


if __name__ == "__main__":
    main(sys.argv[1:])
