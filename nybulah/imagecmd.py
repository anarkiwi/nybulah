"""convert and info subcommands: image files only, no drive needed."""

import json
import pathlib

from .formats import image as images
from .formats.d64 import write_d64
from .formats.g64 import write_g64
from .formats.p64 import write_p64

WRITERS = {
    ".g64": lambda img: write_g64(images.to_g64(img, progress=True)),
    ".g71": lambda img: write_g64(images.to_g64(img, progress=True)),
    ".d64": lambda img: write_d64(images.to_d64(img, progress=True)),
    ".p64": lambda img: write_p64(images.to_p64(img, progress=True)),
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
    """Convert a NIB/NBZ/NB2/G64/G71/P64/SCP/KryoFlux/D64 image to G64, G71, D64 or P64."""

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
        args.target.write_bytes(writer(image))
        out = {
            "source": image.kind,
            "target": str(args.target),
            "tracks": len(image.tracks),
        }
        print(json.dumps(out))
        return out


class Info:
    """Per-track summary of an image: kind, cycle length, z, density, sector errors."""

    NEEDS_ADAPTER = False

    @staticmethod
    def add_arguments(ap):
        ap.add_argument("source", type=pathlib.Path)
        _layout_option(ap)

    @staticmethod
    def execute(args, _cbm=None):
        image = _load(args)
        out = {"kind": image.kind, "tracks": images.info(image, progress=True)}
        print(json.dumps(out, indent=1))
        return out
