"""1581 capture records (Read Track revolutions or Read Address ID lists), their
versioned npz form, and :class:`MfmDisk`, the container ``info``/``map``/``convert`` use.
"""

import json
import pathlib
from dataclasses import dataclass, field

import numpy as np

from ..analysis import mfm

VERSION = 1
MAGIC = "nybulah_mfm"


_ARRAYS = {
    "data": (np.uint8, (0,)),
    "rev_offsets": (np.int64, (1,)),
    "rev_start_us": (np.float64, (0,)),
    "rev_end_us": (np.float64, (0,)),
    "rev_status": (np.uint8, (0,)),
    "ids": (np.uint8, (0, 6)),
    "id_status": (np.uint8, (0,)),
    "id_us": (np.float64, (0,)),
    "index_us": (np.float64, (0,)),
}


class MfmCapture:
    """One capture of one track; ``kind`` "track" or "ids", ``head`` physical (1 - PA0).

    Read Track bytes of every revolution are concatenated in ``data`` and cut by
    ``rev_offsets``; Read Address results are ``ids`` ``(n, 6)``, ``id_status``, ``id_us``.
    """

    def __init__(self, kind, cylinder, head, side_select=None, meta=None, **arrays):
        if kind not in ("track", "ids"):
            raise ValueError(f"capture kind {kind!r}")
        if side_select is not None and side_select != mfm.head_side(head):
            raise ValueError(f"side_select {side_select} on head {head}")
        unknown = set(arrays) - set(_ARRAYS)
        if unknown:
            raise TypeError(f"unknown capture fields {sorted(unknown)}")
        self.kind, self.cylinder, self.head = kind, int(cylinder), int(head)
        self.meta = dict(meta or {})
        self.arrays = {
            name: np.asarray(arrays.get(name, np.zeros(shape)), dtype)
            for name, (dtype, shape) in _ARRAYS.items()
        }
        self.arrays["ids"] = self.arrays["ids"].reshape(-1, 6)

    def __getattr__(self, name):
        arrays = self.__dict__.get("arrays", {})
        if name in arrays:
            return arrays[name]
        raise AttributeError(name)

    @property
    def side_select(self):
        """CIA PA0 while capturing: the ID side H of a 1581 track on this head."""
        return mfm.head_side(self.head)

    @property
    def key(self):
        """``(cylinder, head)``."""
        return (self.cylinder, self.head)

    def revolutions(self):
        """Read Track bytes of each revolution."""
        cuts = self.rev_offsets
        return [self.data[a:b] for a, b in zip(cuts[:-1], cuts[1:])]

    def decode(self):
        """:class:`mfm.MfmTrack` per revolution, or one of the ID list."""
        args = {"cylinder": self.cylinder, "side": self.side_select}
        if self.kind == "ids":
            return [
                mfm.decode_ids(
                    self.ids, self.id_status, self.id_us, self.index_us, **args
                )
            ]
        return [mfm.decode_track(rev, **args) for rev in self.revolutions()]

    @classmethod
    def from_media(cls, key, data, **meta):
        """One revolution of media bytes as a Read Track capture."""
        data = np.asarray(data, np.uint8)
        return cls("track", *key, meta=meta, data=data, rev_offsets=[0, len(data)])


def _record(prefix, cap):
    out = {f"{prefix}{name}": array for name, array in cap.arrays.items()}
    out[f"{prefix}head"] = np.array([cap.kind, json.dumps(cap.meta)])
    out[f"{prefix}geometry"] = np.array([cap.cylinder, cap.head], np.int64)
    return out


def save_captures(path, captures):
    """Write capture records into one npz."""
    arrays = {MAGIC: np.array([VERSION, len(captures)], np.int64)}
    for i, cap in enumerate(captures):
        arrays.update(_record(f"r{i}_", cap))
    with open(path, "wb") as fh:
        np.savez_compressed(fh, **arrays)


def _load_npz(path):
    with np.load(path, allow_pickle=False) as z:
        if MAGIC not in z.files:
            return None
        version, count = (int(v) for v in np.asarray(z[MAGIC]))
        if version > VERSION:
            raise ValueError(f"{path}: capture version {version} > {VERSION}")
        out = []
        for i in range(count):
            p = f"r{i}_"
            kind, meta = (str(v) for v in np.asarray(z[f"{p}head"]))
            cyl, head = (int(v) for v in np.asarray(z[f"{p}geometry"]))
            arrays = {name: z[p + name] for name in _ARRAYS}
            out.append(MfmCapture(kind, cyl, head, meta=json.loads(meta), **arrays))
        return out


def load_captures(path):
    """Capture records of an npz, or of every capture npz in a directory; None if
    ``path`` holds none."""
    path = pathlib.Path(path)
    if path.is_dir():
        found = [_load_npz(p) for p in sorted(path.glob("*.npz"))]
        found = [c for c in found if c]
        return [c for caps in found for c in caps] or None
    if path.suffix.lower() != ".npz":
        return None
    return _load_npz(path)


@dataclass
class MfmDisk:
    """Captures of a 1581 disk by ``(cylinder, head)``, and their decodes.

    ``tracks`` holds Read Track (or media) revolutions, ``ids`` Read Address
    lists; ``circular`` media decode as whole revolutions with missing clocks.
    """

    kind: str
    captures: list
    tracks: dict = field(default_factory=dict)
    ids: dict = field(default_factory=dict)
    source: object = None

    @classmethod
    def from_captures(cls, captures, kind="capture", source=None):
        disk = cls(kind, list(captures), source=source)
        for cap in disk.captures:
            target = disk.ids if cap.kind == "ids" else disk.tracks
            target.setdefault(cap.key, []).extend(cap.decode())
        return disk

    @classmethod
    def from_media(cls, kind, media, source=None):
        """Disk of media ``{(cylinder, head): (data, mark)}``, one revolution each."""
        disk = cls(kind, [], source=source)
        for key, (data, mark) in sorted(media.items()):
            disk.captures.append(MfmCapture.from_media(key, data, source=kind))
            side = mfm.head_side(key[1])
            disk.tracks[key] = [
                mfm.decode_track(data, mark, True, cylinder=key[0], side=side)
            ]
        return disk

    def decodes(self):
        """Every decode by key, Read Track and Read Address together."""
        keys = sorted(set(self.tracks) | set(self.ids))
        return {k: self.tracks.get(k, []) + self.ids.get(k, []) for k in keys}


def load_disk(path, progress=False):
    """:class:`MfmDisk` of a D81, IMD or capture npz/directory; None otherwise."""
    from . import d81, imd

    path = pathlib.Path(path)
    captures = load_captures(path)
    if captures:
        return MfmDisk.from_captures(captures, source=path)
    if path.is_dir():
        return None
    head = path.read_bytes()
    if head.startswith(imd.MAGIC):
        image = imd.read_imd(head)
        return MfmDisk.from_media("imd", imd.to_tracks(image), image)
    if len(head) in (d81.D81_BYTES, d81.D81_BYTES + d81.D81_SECTORS):
        image = d81.read_d81(head)
        return MfmDisk.from_media("d81", d81.to_tracks(image, progress), image)
    return None
