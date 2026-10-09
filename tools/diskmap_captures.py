"""Map saved capture folders of one disk together and count its flagged regions.

Each folder's captures of a track are compared as further revolutions; prints
JSON of revolutions per track and, per ``KIND/STABILITY``, the tracks with
non-standard or unstable regions. Usage: FOLDER [FOLDER ...] [-o MAP]
"""

import argparse
import collections
import json
import pathlib

from nybulah import survey, viz
from nybulah.analysis.diskmap import Cls, Kind, Stability, disk_map


def merged(folders):
    """``{key: [Capture]}`` of every folder's captures."""
    caps = {}
    for folder in folders:
        for key, found in survey.load_captures(folder).items():
            caps.setdefault(key, []).extend(found)
    return caps


def flagged(dmap):
    """``{KIND/STABILITY: {track key: count}}`` of non-standard or unstable regions."""
    r = dmap.regions
    r = r[(r["cls"] > Cls.STANDARD) | (r["stability"] == Stability.UNSTABLE)]
    out = collections.defaultdict(collections.Counter)
    for key, kind, st in zip(r["track"], r["kind"], r["stability"]):
        out[f"{Kind(kind).name}/{Stability(st).name}"][int(key)] += 1
    return {name: dict(sorted(c.items())) for name, c in sorted(out.items())}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("folders", type=pathlib.Path, nargs="+")
    ap.add_argument("-o", "--output", type=pathlib.Path)
    args = ap.parse_args()
    dmap = disk_map(survey.DiskImage("capture", merged(args.folders)), progress=True)
    if args.output:
        viz.save(dmap, args.output, title=args.folders[0].name)
    revs = dict(zip(dmap.keys.tolist(), dmap.revs.tolist()))
    print(json.dumps({"revolutions": revs, "flagged": flagged(dmap)}, indent=1))


if __name__ == "__main__":
    main()
