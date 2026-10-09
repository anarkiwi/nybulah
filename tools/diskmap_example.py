"""Render the README disk map and flux view of synthetic disks with one of each
injected anomaly (bits) and physical feature (flux).

Usage: python tools/diskmap_example.py [OUT_DIR] (default docs/img)
"""

import json
import pathlib
import sys

from nybulah import fluxviz, viz
from nybulah.analysis.diskmap import disk_map
from nybulah.analysis.fluxsynth import synthetic_flux_disk
from nybulah.analysis.fluxview import flux_disk
from nybulah.analysis.synth import synthetic_disk

FLUX_SIZE = 900


def main(out="docs/img"):
    out = pathlib.Path(out)
    out.mkdir(parents=True, exist_ok=True)
    image, truth = synthetic_disk()
    dmap = disk_map(image, progress=True)
    viz.save_png(dmap, out / "diskmap.png")
    frames = viz.save_apng(dmap, out / "diskmap.apng")
    flux, features = synthetic_flux_disk()
    disk = flux_disk(flux, progress=True)
    disk.name = "synthetic flux"
    fluxviz.save_png(disk, out / "fluxview.png", FLUX_SIZE, key=2)
    sizes = {p.name: p.stat().st_size for p in sorted(out.glob("*.*png"))}
    injected = {"bits": len(truth), "flux": len(features)}
    print(json.dumps({"frames": frames, "bytes": sizes, "injected": injected}))


if __name__ == "__main__":
    main(*sys.argv[1:])
