"""read and write subcommands: D64/D71 and raw (G64/G71, and any raw image on write)
images through the raw track routines, D81 through the 1581's WD177x (Read Sector,
Write Track, Write Sector)."""

import json
import pathlib

from . import disk, disk1581, r1581, rawdisk
from .formats import read_d64, read_d71, write_d64, write_d71
from .formats import image as images
from .formats.d81 import read_d81, write_d81
from .formats.g64 import write_g64
from .monitor import Monitor, protocols, resolve, supported
from .nibbler import Nibbler, TrackError
from .ramprobe import identify_model

FORMATS = {".d64": (read_d64, write_d64), ".d71": (read_d71, write_d71)}
FORMATS[".d81"] = (read_d81, write_d81)
RAW_SIDES = {".g64": (0,), ".g71": (0, 1)}
NEEDS = {".d71": "1571", ".d81": "1581", ".g71": "1571"}


def _common(ap):
    ap.add_argument("image", type=pathlib.Path)
    ap.add_argument("--dev", type=int, default=8)
    ap.add_argument("--transport", choices=protocols() or ("s1",), default="s1")
    ap.add_argument("--retries", type=int, default=2)
    ap.add_argument("--archive", type=pathlib.Path, help="save every capture here")
    ap.add_argument(
        "--allow-bump",
        action="store_true",
        help="1541: bump the head against the stop if nothing else locates it",
    )
    ap.add_argument(
        "--max-steps",
        type=int,
        default=r1581.MAX_CYL,
        help="1581: refuse homing that needs more Restore step pulses",
    )


def _session(args, cbm, write):
    """Check the request against the drive; returns the image kind and its model.

    Reads take a sector image or .g64/.g71; writes take a sector image or any raw
    image (kind "raw")."""
    kind = args.image.suffix.lower()
    if kind not in FORMATS:
        if not write and kind not in RAW_SIDES:
            raise ValueError(f"{args.image}: expected .d64, .d71, .d81, .g64 or .g71")
        kind = kind if kind in RAW_SIDES else "raw"
    raw_only = getattr(args, "align", "none") != "none" or getattr(
        args, "halftracks", False
    )
    if kind in FORMATS and raw_only:
        raise ValueError("--align and --halftracks need a raw image")
    if not supported(cbm, resolve(cbm, args.transport)):
        raise ValueError(f"transport {args.transport} is not available here")
    model = identify_model(cbm, args.dev)
    if kind in NEEDS and model != NEEDS[kind]:
        raise ValueError(
            f"device {args.dev} is a {model}: {kind} needs a {NEEDS[kind]}"
        )
    if model == "1581" and kind != ".d81":
        raise ValueError(f"device {args.dev} is a 1581: use a .d81 image")
    return kind, model


def _raw(args, nib, write, kw):
    """Write any raw image, or read a G64/G71; returns the summary."""
    if write:
        g64 = images.to_g64(images.load(args.image))
        out = rawdisk.write_g64(nib, g64, args.align, **kw)
        out["skipped"] = [images.track_name(k) for k in out["skipped"]]
        out["failed"] = disk.failed(out)
        return out
    unformatted = []
    sides = RAW_SIDES[args.image.suffix.lower()]
    g64 = rawdisk.read_g64(
        nib, args.tracks, sides, args.halftracks, unformatted=unformatted, **kw
    )
    args.image.write_bytes(write_g64(g64))
    return {
        "tracks": len(g64.tracks),
        "unformatted": [images.track_name(k) for k in unformatted],
    }


def _d81(args, cbm, write):
    """Home within --max-steps, then read or write the whole disk."""
    with r1581.session(cbm, args.dev, args.transport, writes=write) as drive:
        drive.motor(True)
        disk1581.home(drive, disk1581.dry(drive), args.max_steps)
        if write:
            out = disk1581.write_disk(drive, read_d81(args.image.read_bytes()))
            out["failed"] = out["mismatched"]
        else:
            image = disk1581.read_disk(drive, args.retries, args.archive)
            args.image.write_bytes(write_d81(image))
            out = {"errors": int((image.errors != 1).sum())}
    return out


class Command:
    """A read or write subcommand in the tool-module shape (add_arguments, execute)."""

    def __init__(self, write):
        self.write = write
        self.__doc__ = (
            "Write a D64/D71/D81 or raw (G64/G71/NIB/NBZ/P64...) image to disk,"
            " verifying every track."
            if write
            else "Read a disk into a D64/D71/D81 image with error bytes, or a raw"
            " G64/G71 image."
        )

    def add_arguments(self, ap):
        """Command line options."""
        _common(ap)
        if self.write:
            ap.add_argument(
                "--align",
                choices=rawdisk.ALIGNS,
                default="none",
                help="raw image: write each track's byte 0 at the index (1571)",
            )
        else:
            ap.add_argument("--tracks", type=int, choices=(35, 40), default=35)
            ap.add_argument(
                "--halftracks",
                action="store_true",
                help="raw read: capture the halftracks between tracks too",
            )

    def execute(self, args, cbm):
        """Run against cbm; returns a summary (and writes the image when reading)."""
        kind, model = _session(args, cbm, self.write)
        if kind == ".d81":
            out = _d81(args, cbm, self.write) | {"image": str(args.image)}
            print(json.dumps(out | {"model": model}))
            if out.get("failed"):
                raise r1581.TrackError(f"verify failed on {len(out['failed'])} sides")
            return out
        kw = {"retries": args.retries, "archive": args.archive}
        with (
            Monitor(cbm, args.dev, args.transport) as mon,
            Nibbler(mon, model, allow_bump=args.allow_bump) as nib,
        ):
            if kind not in FORMATS:
                out = _raw(args, nib, self.write, kw)
            elif self.write:
                image = FORMATS[kind][0](args.image.read_bytes())
                op = disk.write_d71 if kind == ".d71" else disk.write_d64
                out = op(nib, image, **kw)
                out["failed"] = disk.failed(out)
            else:
                if kind == ".d71":
                    image = disk.read_d71(nib, **kw)
                else:
                    image = disk.read_d64(nib, args.tracks, **kw)
                args.image.write_bytes(FORMATS[kind][1](image))
                out = {"errors": int((image.errors != 1).sum())}
        out |= {"image": str(args.image), "model": model}
        print(json.dumps(out))
        if out.get("failed"):
            raise TrackError(f"verify failed on {len(out['failed'])} tracks")
        return out


READ, WRITE = Command(False), Command(True)
