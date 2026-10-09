"""Render the README disk map of a synthetic disk with one of each injected anomaly.

Usage: python tools/diskmap_example.py [OUT_DIR] (default docs/img)
"""

import json
import pathlib
import sys

from nybulah import viz
from nybulah.analysis.diskmap import disk_map
from nybulah.analysis.synth import synthetic_disk


def main(out="docs/img"):
    out = pathlib.Path(out)
    out.mkdir(parents=True, exist_ok=True)
    image, truth = synthetic_disk()
    dmap = disk_map(image, progress=True)
    viz.save_png(dmap, out / "diskmap.png")
    frames = viz.save_apng(dmap, out / "diskmap.apng")
    sizes = {p.name: p.stat().st_size for p in sorted(out.glob("diskmap.*"))}
    print(json.dumps({"frames": frames, "bytes": sizes, "injected": len(truth)}))


if __name__ == "__main__":
    main(*sys.argv[1:])
