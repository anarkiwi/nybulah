"""convert, info, map and flux subcommands: image files only, no drive needed."""

import argparse
import json
import pathlib

import numpy as np

from . import fluxcmd, viz
from .analysis import mfm
from .analysis.diskmap import Cls, disk_map, load_thresholds
from .analysis.mfmmap import mfm_disk_map
from .analysis.sector import SectorError
from .formats import d81, imd, mfmcap
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


MFM_WRITERS = {
    ".d81": lambda disk, path: path.write_bytes(
        d81.write_d81(d81.from_decodes(disk.tracks, True))
    ),
    ".imd": lambda disk, path: path.write_bytes(
        imd.write_imd(imd.from_decodes(disk.tracks))
    ),
    ".npz": lambda disk, path: mfmcap.save_captures(path, disk.captures),
}


def _load(args):
    disk = mfmcap.load_disk(args.source, progress=True)
    if disk is not None:
        return disk
    options = {} if args.layout is None else {"layout": args.layout}
    return images.load(args.source, **options)


def _mfm_convert(disk, target):
    writer = MFM_WRITERS.get(target.suffix.lower())
    if writer is None:
        raise ValueError(f"{target}: MFM output must be one of {sorted(MFM_WRITERS)}")
    writer(disk, target)
    return {"source": disk.kind, "target": str(target), "tracks": len(disk.tracks)}


def mfm_info(disk):
    """Per (cylinder, head): revolutions, ID lists, sectors and flags of the best
    revolution, and the logical sector errors over all revolutions."""
    rows = []
    for (cyl, head), tracks in disk.decodes().items():
        side = mfm.head_side(head)
        reads = disk.tracks.get((cyl, head), [])
        _, errors = mfm.best_sectors(reads, cyl, side)
        best = max(tracks, key=lambda t: int(t.sectors["id_ok"].sum()))
        flags = int(np.bitwise_or.reduce(best.sectors["flags"], initial=0))
        rows.append(
            {
                "cylinder": cyl,
                "head": head,
                "side": side,
                "revolutions": len(reads),
                "id_lists": len(tracks) - len(reads),
                "ids": int(best.sectors["id_ok"].sum()),
                "errors": int((errors != SectorError.OK).sum()) if reads else None,
                "flags": [f.name.lower() for f in mfm.Flag if flags & f],
            }
        )
    out = {"kind": disk.kind, "tracks": rows}
    if isinstance(disk.source, d81.D81):
        out["d81"] = disk.source.info()
    return out


def _layout_option(ap):
    ap.add_argument(
        "--layout",
        choices=sorted(images.LAYOUTS),
        help="flux image track numbering (default: inferred from sector headers)",
    )


def _captures_option(ap):
    ap.add_argument(
        "--captures",
        type=pathlib.Path,
        nargs="*",
        default=[],
        help="other images of the same disk, compared as more revolutions",
    )


def _merged(disk, others):
    """Read Track decodes of ``disk`` with those of other MFM captures appended."""
    out = {k: list(v) for k, v in disk.tracks.items()}
    for other in others:
        if not isinstance(other, mfmcap.MfmDisk):
            raise ValueError("--captures of an MFM disk must be MFM captures")
        for key, tracks in other.tracks.items():
            out.setdefault(key, []).extend(tracks)
    return out


class Convert:
    """Convert a NIB/NBZ/NB2/G64/G71/P64/SCP/KryoFlux/D64 image to G64, G71, D64 or P64,
    or a D81/IMD/1581 capture to D81, IMD or capture npz.

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
        image = _load(args)
        if isinstance(image, mfmcap.MfmDisk):
            out = _mfm_convert(image, args.target)
            print(json.dumps(out))
            return out
        writer = WRITERS.get(args.target.suffix.lower())
        if writer is None:
            raise ValueError(f"{args.target}: output must be one of {sorted(WRITERS)}")
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
        mfm_disk = isinstance(image, mfmcap.MfmDisk)
        if args.map:
            dmap = (
                mfm_disk_map(image.tracks, progress=True)
                if mfm_disk
                else disk_map(image, progress=True)
            )
            lines = viz.terminal_strip(dmap, args.width)
            print("\n".join(lines + [viz.legend_line()]))
            return {"kind": image.kind, "map": lines}
        if mfm_disk:
            out = mfm_info(image)
        else:
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
        _captures_option(ap)
        ap.add_argument(
            "--thresholds",
            type=pathlib.Path,
            help="survey summary.json whose thresholds replace the shipped ones",
        )
        ap.add_argument("--bins", type=int, default=2048, help="angular bins per track")
        ap.add_argument(
            "--analog", action="store_true", help="flux view instead (see flux)"
        )
        fluxcmd.add_view_arguments(ap)
        _layout_option(ap)

    @staticmethod
    def execute(args, _cbm=None):
        if args.analog:
            return Flux.execute(args)
        image = _load(args)
        thresholds = (
            None if args.thresholds is None else load_thresholds(args.thresholds)
        )
        captures = [
            _load(argparse.Namespace(source=p, layout=None)) for p in args.captures
        ]
        if isinstance(image, mfmcap.MfmDisk):
            dmap = mfm_disk_map(_merged(image, captures), args.bins, progress=True)
        else:
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


class Flux:
    """Analog flux view of an image: transitions per cell as lightness, cell length
    as hue, revolution-to-revolution variance as lost chroma; polar disk with
    panels (.png), one frame per revolution (.apng) or a zoomable viewer (.html)."""

    NEEDS_ADAPTER = False

    @staticmethod
    def add_arguments(ap):
        ap.add_argument("source", type=pathlib.Path)
        ap.add_argument("-o", "--output", type=pathlib.Path, required=True)
        _captures_option(ap)
        fluxcmd.add_view_arguments(ap)
        _layout_option(ap)

    @staticmethod
    def execute(args, _cbm=None):
        image = _load(args)
        out = fluxcmd.render(
            image, [images.load(p) for p in args.captures], args, args.source.name
        )
        print(json.dumps(out))
        return out
