"""Self-contained zoomable flux viewer: the browser recomputes the channels of
:meth:`nybulah.analysis.fluxview.FluxRev.channels` for every pixel it shows,
from the revolutions' bits, knots and transitions embedded zlib-compressed.
"""

import base64
import html
import json
import pathlib
import zlib

import numpy as np

from . import fluxviz as fv
from .analysis.flux import divider
from .analysis.fluxview import KERNEL_CELLS, interval_edges, standard_zone
from .formats.g64 import SIDE1
from .formats.image import track_name

DRIFT_SAMPLES = 512


def blob(array):
    """Base64 of zlib-compressed little-endian bytes."""
    data = np.ascontiguousarray(array).astype(np.asarray(array).dtype.newbyteorder("<"))
    return base64.b64encode(zlib.compress(data.tobytes())).decode("ascii")


def _residual(kb, kt, q):
    """Knot times as int32 clocks (``q`` per cell) less ``q`` per decoded cell,
    delta-coded so steady cells code as small numbers."""
    ticks = np.diff(np.rint(np.asarray(kt) * q).astype(np.int64), prepend=0)
    return blob((ticks - q * np.diff(kb, prepend=0)).astype(np.int32))


def rev_record(track, i):
    """JSON-able record of revolution ``i`` of a :class:`FluxTrack`."""
    r = track.revs[i]
    q = 4 * divider(r.zone)
    kb, kt, ke = np.rint(r.knots[0]).astype(np.int64), r.knots[1], r.knots[2]
    turns, cells = track.drift(i, DRIFT_SAMPLES)
    return {
        "n": r.n,
        "q": q,
        "period": r.period,
        "shift": r.shift % 1.0,
        "scale": r.scale,
        "timing": r.timing,
        "bits": blob(np.packbits(r.bits)),
        "kb": blob(np.diff(kb, prepend=0).astype(np.int32)),
        "kt": _residual(kb, kt, q),
        "ke": blob(np.rint(ke * q).astype(np.uint16)),
        "noflux": blob(np.packbits(r.noflux)),
        "fault": blob(np.flatnonzero(r.fault).astype(np.int32)),
        "var": blob(np.rint(np.clip(r.var, 0, 1) * 255).astype(np.uint8)),
        "drift": blob(np.stack((turns, cells)).astype(np.float32)),
    }


def payload(disk, zoom):
    """Everything the viewer needs, as a JSON-able dict."""
    tracks = []
    for key in disk.keys:
        track = disk.tracks[key]
        tracks.append(
            {
                "key": int(key),
                "half": int(key & ~SIDE1),
                "side": int(bool(key & SIDE1)),
                "name": track_name(int(key)) + ("'" if key & SIDE1 else ""),
                "zone": track.ref.zone,
                "std": divider(standard_zone(int(key))),
                "revs": [rev_record(track, i) for i in range(len(track.revs))],
            }
        )
    hist = []
    for z, pair in disk.intervals().items():
        edges = interval_edges(z)
        counts = [np.histogram(v, edges)[0].astype(np.int32) for v in pair]
        hist.append(
            {
                "zone": z,
                "lo": edges[0],
                "step": edges[1] - edges[0],
                "measured": blob(counts[0]),
                "inferred": blob(counts[1]),
            }
        )
    return {
        "name": disk.name,
        "tracks": tracks,
        "hist": hist,
        "lut": blob(fv.colour_lut()),
        "lutShape": [fv.LUT_DENSITY, fv.LUT_CHROMA],
        "colours": {
            k: getattr(fv, k)
            for k in ("SURFACE", "FAULT", "STIPPLE", "INK", "INK_SECONDARY")
        },
        "series": list(fv.SERIES),
        "pitch": fv.STIPPLE_PITCH,
        "maxCellPx": zoom,
        "kernel": KERNEL_CELLS,
        "sources": fv.SOURCE_TEXT,
    }


TEMPLATE = pathlib.Path(__file__).with_name("fluxview.html")


def render(disk, zoom=16):
    """The viewer page as a string; ``zoom`` is the most pixels per cell."""
    page = TEMPLATE.read_text(encoding="utf-8")
    data = json.dumps(payload(disk, zoom), separators=(",", ":"))
    return page.replace("__TITLE__", html.escape(disk.name or "flux view")).replace(
        "__DATA__", data.replace("</", "<\\/")
    )


def save_html(disk, path, zoom=16):
    """Write the viewer; returns its size in bytes."""
    text = render(disk, zoom)
    pathlib.Path(path).write_text(text, encoding="utf-8")
    return len(text)
