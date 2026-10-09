"""Analog flux view rendering for the flux and map --analog subcommands."""

import pathlib

from . import fluxviz
from .analysis.fluxview import flux_disk
from .fluxhtml import save_html
from .formats.g64 import SIDE1


def track_keys(spec):
    """Track keys (both sides) of an inclusive track range ``"LO-HI"`` (``.5`` allowed)."""
    lo, _, hi = spec.partition("-")
    first, last = (int(round(2 * float(v))) for v in (lo, hi or lo))
    halves = range(first, last + 1)
    return {h | side for h in halves for side in (0, SIDE1)}


def add_view_arguments(ap):
    """Options shared with ``map --analog``."""
    ap.add_argument("--size", type=int, default=1200, help="disk diameter in pixels")
    ap.add_argument(
        "--zoom", type=int, default=16, help="viewer: most pixels per bit cell"
    )
    ap.add_argument("--tracks", help="track range, e.g. 1-35 or 18-18.5")
    ap.add_argument("--track", type=float, help="track for the eye and drift panels")


def save(disk, path, size=1200, key=None, zoom=16):
    """Write by suffix: .png, .apng or .html (``zoom``: most pixels per cell)."""
    path = pathlib.Path(path)
    suffix = path.suffix.lower()
    if suffix == ".apng":
        return fluxviz.save_apng(disk, path, size)
    if suffix in (".html", ".htm"):
        return save_html(disk, path, zoom)
    if suffix != ".png":
        raise ValueError(f"{path}: output must be .png, .apng or .html")
    return fluxviz.save_png(disk, path, size, key)


def render(image, captures, args, name):
    """Build the flux view of an image and write ``args.output``."""
    keys = None if args.tracks is None else track_keys(args.tracks)
    disk = flux_disk(image, captures, keys, progress=True)
    disk.name = name
    key = None if args.track is None else int(round(2 * args.track))
    result = save(disk, args.output, args.size, key, args.zoom)
    return {
        "source": image.kind,
        "output": str(args.output),
        "tracks": len(disk.tracks),
        "timing": disk.sources(),
        "frames" if args.output.suffix.lower() == ".apng" else "bytes": result,
    }
