"""read and write subcommands: D64/D71 images through the raw track routines."""

import json
import pathlib

from . import disk
from .formats import read_d64, read_d71, write_d64, write_d71
from .monitor import Monitor, protocols, resolve, supported
from .nibbler import Nibbler, TrackError
from .ramprobe import identify_model

FORMATS = {".d64": (read_d64, write_d64), ".d71": (read_d71, write_d71)}


def _common(ap):
    ap.add_argument("image", type=pathlib.Path)
    ap.add_argument("--dev", type=int, default=8)
    ap.add_argument("--transport", choices=protocols() or ("s1",), default="s1")
    ap.add_argument("--retries", type=int, default=2)
    ap.add_argument("--archive", type=pathlib.Path, help="save every capture here")
    ap.add_argument(
        "--allow-bump",
        action="store_true",
        help="bump the head against the stop if nothing else locates it",
    )


def _session(args, cbm):
    """Check the request against the drive; returns its model."""
    kind = args.image.suffix.lower()
    if kind not in FORMATS:
        raise ValueError(f"{args.image}: expected .d64 or .d71")
    if not supported(cbm, resolve(cbm, args.transport)):
        raise ValueError(f"transport {args.transport} is not available here")
    model = identify_model(cbm, args.dev)
    if kind == ".d71" and model != "1571":
        raise ValueError(f"device {args.dev} is a {model}: D71 needs a 1571")
    return kind, model


class Command:
    """A read or write subcommand in the tool-module shape (add_arguments, execute)."""

    def __init__(self, write):
        self.write = write
        self.__doc__ = (
            "Write a D64/D71 image to disk, verifying every track."
            if write
            else "Read a disk into a D64/D71 image with error bytes."
        )

    def add_arguments(self, ap):
        """Command line options."""
        _common(ap)
        if not self.write:
            ap.add_argument("--tracks", type=int, choices=(35, 40), default=35)

    def execute(self, args, cbm):
        """Run against cbm; returns a summary (and writes the image when reading)."""
        kind, model = _session(args, cbm)
        parse, serialise = FORMATS[kind]
        kw = {"retries": args.retries, "archive": args.archive}
        with (
            Monitor(cbm, args.dev, args.transport) as mon,
            Nibbler(mon, model, allow_bump=args.allow_bump) as nib,
        ):
            if self.write:
                image = parse(args.image.read_bytes())
                op = disk.write_d71 if kind == ".d71" else disk.write_d64
                out = op(nib, image, **kw)
                out["failed"] = disk.failed(out)
            else:
                if kind == ".d71":
                    image = disk.read_d71(nib, **kw)
                else:
                    image = disk.read_d64(nib, args.tracks, **kw)
                args.image.write_bytes(serialise(image))
                out = {"errors": int((image.errors != 1).sum())}
        out |= {"image": str(args.image), "model": model}
        print(json.dumps(out))
        if out.get("failed"):
            raise TrackError(f"verify failed on {len(out['failed'])} tracks")
        return out


READ, WRITE = Command(False), Command(True)
