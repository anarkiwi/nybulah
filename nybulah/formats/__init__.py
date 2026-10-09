"""Disk image formats: D64, D71, D81, G64, IMD, NIB and NB2, 1581 capture records,
and conversions between them."""

from .convert import d64_to_g64, g64_to_d64, nib_to_g64, revolution_bytes
from .d64 import D64, read_d64, write_d64
from .d71 import D71, read_d71, write_d71
from .g64 import G64, G64Track, read_g64, write_g64
from .nib import Nib, NibEntry, read_nib, write_nib
from .image import Capture, DiskImage, info, load, loads, to_d64, to_g64, to_p64
from .d81 import D81, read_d81, write_d81
from .imd import Imd, ImdTrack, read_imd, write_imd
from .mfmcap import MfmCapture, MfmDisk, load_captures, load_disk, save_captures

__all__ = [
    "D64",
    "D71",
    "G64",
    "G64Track",
    "Nib",
    "NibEntry",
    "d64_to_g64",
    "g64_to_d64",
    "nib_to_g64",
    "read_d64",
    "read_d71",
    "read_g64",
    "read_nib",
    "revolution_bytes",
    "write_d64",
    "write_d71",
    "write_g64",
    "write_nib",
]

__all__ += [
    "Capture",
    "DiskImage",
    "info",
    "load",
    "loads",
    "to_d64",
    "to_g64",
    "to_p64",
]

__all__ += [
    "D81",
    "Imd",
    "ImdTrack",
    "MfmCapture",
    "MfmDisk",
    "load_captures",
    "load_disk",
    "read_d81",
    "read_imd",
    "save_captures",
    "write_d81",
    "write_imd",
]
