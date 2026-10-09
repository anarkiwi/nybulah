"""convert, info and map subcommands: image files only, no drive needed."""

import json
import pathlib

from . import viz
from .analysis.diskmap import Cls, disk_map, load_thresholds
from .formats import image as images
from .formats.d64 import write_d64
from .formats.g64 import write_g64
from .formats.p64 import write_p64

WRITERS = {
    ".g64": lambda img, unf: write_g64(images.to_g64(img, True, unf)),
    ".g71": lambda img, unf: write_g64(images.to_g64(img, True, unf)),
    ".d64": lambda img, unf: write_d64(images.to_d64(img, True, unf)),
    ".p64": lambda img, unf: write_p64(images.to_p64(img, True, unf)),
}


def _load(args):
    options = {} if args.layout is None else {"layout": args.layout}
    return images.load(args.source, **options)


def _layout_option(ap):
    ap.add_argument(
        "--layout",
        choices=sorted(images.LAYOUTS),
        help="flux image track numbering (default: inferred from sector headers)",
    )


class Convert:
    """Convert a NIB/NBZ/NB2/G64/G71/P64/SCP/KryoFlux/D64 image to G64, G71, D64 or P64.

    Tracks with no repeating revolution are kept as their capture cut to the
    nominal length and listed under ``unformatted``.
    """

    NEEDS_ADAPTER = False

    @staticmethod
    def add_arguments(ap):
        ap.add_argument("source", type=pathlib.Path)
        ap.add_argument("target", type=pathlib.Path)
        _layout_option(ap)

    @staticmethod
    def execute(args, _cbm=None):
        writer = WRITERS.get(args.target.suffix.lower())
        if writer is None:
            raise ValueError(f"{args.target}: output must be one of {sorted(WRITERS)}")
        image = _load(args)
        unformatted = []
        args.target.write_bytes(writer(image, unformatted))
        out = {
            "source": image.kind,
            "target": str(args.target),
            "tracks": len(image.tracks),
            "unformatted": [images.track_name(k) for k in unformatted],
        }
        print(json.dumps(out))
        return out


class Info:
    """Per-track summary of an image: kind, cycle length, z, density, sector errors.

    ``--map`` prints a one-line-per-track character map of the disk instead.
    """

    NEEDS_ADAPTER = False

    @staticmethod
    def add_arguments(ap):
        ap.add_argument("source", type=pathlib.Path)
        ap.add_argument("--map", action="store_true", help="print a disk map strip")
        ap.add_argument(
            "--width", type=int, default=64, help="map characters per track"
        )
        _layout_option(ap)

    @staticmethod
    def execute(args, _cbm=None):
        image = _load(args)
        if args.map:
            lines = viz.terminal_strip(disk_map(image, progress=True), args.width)
            print("\n".join(lines + [viz.legend_line()]))
            return {"kind": image.kind, "map": lines}
        out = {"kind": image.kind, "tracks": images.info(image, progress=True)}
        print(json.dumps(out, indent=1))
        return out


class Map:
    """Disk map of an image: polar disk (.png), animation (.apng, or .png with
    --animate), strip view (.svg) or strip with region table (.html)."""

    NEEDS_ADAPTER = False

    @staticmethod
    def add_arguments(ap):
        ap.add_argument("source", type=pathlib.Path)
        ap.add_argument("-o", "--output", type=pathlib.Path, required=True)
        ap.add_argument("--animate", action="store_true", help="animate a .png")
        ap.add_argument(
            "--mode",
            choices=("rotate", "revs"),
            help="animation: rotating disk, or revolution by revolution "
            "(default when a track has several)",
        )
        ap.add_argument(
            "--captures",
            type=pathlib.Path,
            nargs="*",
            default=[],
            help="other images of the same disk, compared as more revolutions",
        )
        ap.add_argument(
            "--thresholds",
            type=pathlib.Path,
            help="survey summary.json whose thresholds replace the shipped ones",
        )
        ap.add_argument("--bins", type=int, default=2048, help="angular bins per track")
        _layout_option(ap)

    @staticmethod
    def execute(args, _cbm=None):
        image = _load(args)
        thresholds = (
            None if args.thresholds is None else load_thresholds(args.thresholds)
        )
        captures = [images.load(p) for p in args.captures]
        dmap = disk_map(image, captures, args.bins, thresholds, progress=True)
        viz.save(dmap, args.output, args.animate, args.mode, args.source.name)
        anomalies = viz.painted(dmap.regions) > Cls.STANDARD
        out = {
            "source": image.kind,
            "output": str(args.output),
            "tracks": len(dmap.keys),
            "regions": len(dmap.regions),
            "anomalies": int(anomalies.sum()),
        }
        print(json.dumps(out))
        return out
