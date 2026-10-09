"""Analog flux renderings: polar disk with panels (.png), one frame per revolution
(.apng) and a zoomable viewer (.html, :mod:`.fluxhtml`); docs/analysis.md
(flux view) explains the channels.
"""

import pathlib

import numpy as np

from .analysis.flux import divider
from .analysis.fluxview import interval_edges, standard_zone
from .analysis.regions import ILLEGAL_ZEROS
from .formats.g64 import SIDE1
from .formats.image import track_name

SHORT_CELL, LONG_CELL = "#2a78d6", "#eb6834"
FAULT, STIPPLE, SURFACE = "#1baf7a", "#a3a29c", "#0b0b0b"
INK, INK_SECONDARY = "#f2f1ec", "#a3a29c"
SERIES = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300")
L_RANGE = (0.3, 0.98)
LUT_DENSITY, LUT_CHROMA = 65, 129
HOLE = 0.26
STIPPLE_PITCH = 3
STIPPLE_MARK, FAULT_MARK = 1, 2

_M1 = np.array(
    [
        [0.4122214708, 0.5363325363, 0.0514459929],
        [0.2119034982, 0.6806995451, 0.1073969566],
        [0.0883024619, 0.2817188376, 0.6299787005],
    ]
)
_M2 = np.array(
    [
        [0.2104542553, 0.7936177850, -0.0040720468],
        [1.9779984951, -2.4285922050, 0.4505937099],
        [0.0259040371, 0.7827717662, -0.8086757660],
    ]
)


def hex_rgb(code):
    """``#rrggbb`` as floats in [0, 1]."""
    return np.array([int(code[i : i + 2], 16) for i in (1, 3, 5)]) / 255.0


def rgb8(code):
    return np.round(hex_rgb(code) * 255).astype(np.uint8)


def _linear(rgb):
    rgb = np.asarray(rgb, float)
    return np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)


def _gamma(lin):
    lin = np.clip(lin, 0.0, 1.0)
    return np.where(lin <= 0.0031308, 12.92 * lin, 1.055 * lin ** (1 / 2.4) - 0.055)


def to_oklab(rgb):
    """OKLab of sRGB values in [0, 1] (last axis)."""
    return np.cbrt(_linear(rgb) @ _M1.T) @ _M2.T


def oklab_linear(lab):
    """Linear sRGB of OKLab values (last axis); outside [0, 1] when out of gamut."""
    return (lab @ np.linalg.inv(_M2).T) ** 3 @ np.linalg.inv(_M1).T


def max_chroma(lightness, hue, steps=32):
    """Largest in-gamut OKLCh chroma at each lightness and hue (radians)."""
    lo, hi = np.zeros_like(lightness), np.full_like(lightness, 0.5)
    for _ in range(steps):
        mid = (lo + hi) / 2
        lab = np.stack((lightness, mid * np.cos(hue), mid * np.sin(hue)), axis=-1)
        lin = oklab_linear(lab)
        ok = ((lin >= 0) & (lin <= 1)).all(axis=-1)
        lo, hi = np.where(ok, mid, lo), np.where(ok, hi, mid)
    return lo


def colour_lut():
    """``(LUT_DENSITY, LUT_CHROMA, 3)`` uint8: lightness by density, signed
    chroma (short cells negative) at the poles' hues, equal on both arms."""
    poles = [to_oklab(hex_rgb(c)) for c in (SHORT_CELL, LONG_CELL)]
    hues = [np.arctan2(p[2], p[1]) for p in poles]
    light = L_RANGE[0] + (L_RANGE[1] - L_RANGE[0]) * np.linspace(0, 1, LUT_DENSITY)
    cap = np.minimum(*(max_chroma(light, np.full_like(light, h)) for h in hues))
    signed = np.linspace(-1, 1, LUT_CHROMA)
    hue = np.where(signed < 0, hues[0], hues[1])[None, :]
    chroma = cap[:, None] * np.abs(signed)[None, :]
    lab = np.stack(
        (
            np.broadcast_to(light[:, None], chroma.shape),
            chroma * np.cos(hue),
            chroma * np.sin(hue),
        ),
        axis=-1,
    )
    return np.round(_gamma(oklab_linear(lab)) * 255).astype(np.uint8)


def signed_chroma(ch, key):
    """Cell-length deviation in zone steps, faded by revolution variance, in [-1, 1]."""
    signed = np.clip(np.nan_to_num(ch["delta"]) * divider(standard_zone(key)), -1, 1)
    return signed * (1 - np.clip(ch["var"], 0, 1))


def compose(ch, key, lut):
    """``(rgb, marks)`` of a track's channels ``(revolutions, bins)``; marks
    holds ``STIPPLE_MARK`` (mostly inferred no-flux) and ``FAULT_MARK`` bits."""
    density = np.clip(np.nan_to_num(ch["density"]), 0, 1)
    i = np.rint(density * (LUT_DENSITY - 1)).astype(np.int64)
    j = np.rint((signed_chroma(ch, key) + 1) / 2 * (LUT_CHROMA - 1)).astype(np.int64)
    marks = (ch["noflux"] >= 0.5) * STIPPLE_MARK | (ch["fault"] > 0) * FAULT_MARK
    return lut[i, j], marks.astype(np.uint8)


def sides(keys):
    """Sides present among track keys."""
    return [s for s in (0, SIDE1) if ((np.asarray(keys) & SIDE1) == s).any()]


def strip(disk, bins, rows, side=0, raster=None):
    """``(rgb, marks, owner)`` rows of one side, outermost halftrack first;
    ``rows`` per halftrack split among its revolutions, faults marked across
    them all; owner is (key, rev) or -1."""
    raster = disk.raster(bins) if raster is None else raster
    keys = [k for k in disk.keys if (k & SIDE1) == side]
    halves = np.array([k & ~SIDE1 for k in keys])
    lo = int(halves.min())
    height = (int(halves.max()) - lo + 1) * rows
    rgb = np.empty((height, bins, 3), np.uint8)
    rgb[:] = rgb8(SURFACE)
    marks = np.zeros((height, bins), np.uint8)
    owner = np.full((height, 2), -1, np.int64)
    lut = colour_lut()
    for key, half in zip(keys, halves):
        colour, mark = compose(raster[key], key, lut)
        which = np.arange(rows) * len(colour) // rows
        top = (half - lo) * rows
        rgb[top : top + rows] = colour[which]
        marks[top : top + rows] = mark[which] | (
            np.bitwise_or.reduce(mark, axis=0) & FAULT_MARK
        )
        owner[top : top + rows] = np.stack((np.full(rows, key), which), axis=1)
    return rgb, marks, owner


def polar(rgb, marks, size, hole=HOLE):
    """Disk of a strip: outermost row at the rim, angle clockwise from the top;
    a fault anywhere in a pixel's arc marks it."""
    height, bins = marks.shape
    y, x = (np.indices((size, size)) + 0.5 - size / 2) / (size / 2)
    radius = np.hypot(x, y)
    angle = np.arctan2(x, -y) % (2 * np.pi)
    row = np.floor((1 - radius) / (1 - hole) * height).astype(np.int64)
    inside = (row >= 0) & (row < height)
    col = np.minimum((angle / (2 * np.pi) * bins).astype(np.int64), bins - 1)
    row = np.clip(row, 0, height - 1)
    out = np.where(inside[..., None], rgb[row, col], rgb8(SURFACE))
    py, px = np.indices((size, size))
    lattice = (px % STIPPLE_PITCH == 0) & (py % STIPPLE_PITCH == 0)
    out[inside & (marks[row, col] & STIPPLE_MARK > 0) & lattice] = rgb8(STIPPLE)
    faults = np.concatenate(
        (np.zeros((height, 1)), np.cumsum(marks & FAULT_MARK > 0, axis=1)), axis=1
    )
    reach = bins / (2 * np.pi * np.maximum(radius * size / 2, 1))
    lo = np.clip(np.floor(col - reach).astype(np.int64), 0, bins)
    hi = np.clip(np.ceil(col + reach).astype(np.int64) + 1, 0, bins)
    out[inside & (faults[row, hi] > faults[row, lo])] = rgb8(FAULT)
    return out.astype(np.uint8)


def disk_bins(size):
    """Angular bins covering the rim of a ``size`` pixel disk at one per pixel."""
    return 1 << int(np.ceil(np.log2(np.pi * size)))


def disks(disk, size, rev=None):
    """Polar image of each side, ``size`` pixels across."""
    bins = disk_bins(size)
    raster = disk.raster(bins, rev)
    out = []
    for side in sides(disk.keys):
        halves = [k & ~SIDE1 for k in disk.keys if (k & SIDE1) == side]
        span = max(halves) - min(halves) + 1
        rows = max(int(size / 2 * (1 - HOLE) / span), 1)
        rgb, marks, _ = strip(disk, bins, rows, side, raster)
        out.append(polar(rgb, marks, size))
    return out


def eye_key(disk, bins=512):
    """Track whose measured cell length varies most, else the least stable one."""
    edges = np.linspace(0, 1, bins + 1)
    score = {}
    for key, track in disk.tracks.items():
        ch = track.ref.channels(edges)
        measured = track.ref.timing in ("flux", "tb")
        score[int(key)] = (
            measured,
            float(np.std(ch["delta"]) if measured else np.mean(ch["var"])),
        )
    return max(score, key=score.get)


def _style(ax, title):
    ax.set_facecolor(SURFACE)
    ax.set_title(title, color=INK, fontsize=8, loc="left")
    ax.tick_params(colors=INK_SECONDARY, labelsize=7)
    for spine in ax.spines.values():
        spine.set_color(INK_SECONDARY)
        spine.set_linewidth(0.5)


def _cell_lines(ax, horizontal=False):
    line = ax.axhline if horizontal else ax.axvline
    span = ax.axhspan if horizontal else ax.axvspan
    for n in range(1, ILLEGAL_ZEROS + 1):
        line(n, color=INK_SECONDARY, linewidth=0.4, linestyle=":")
    top = 2 * (ILLEGAL_ZEROS + 1)
    span(ILLEGAL_ZEROS + 0.5, top, color=INK_SECONDARY, alpha=0.15, linewidth=0)


def histogram_panel(ax, disk):
    """Interval histograms per zone: measured flux solid, decoded bits dashed."""
    _style(ax, "intervals per zone (cells); shaded ≥4T: no legal GCR")
    for z, pair in disk.intervals().items():
        edges = interval_edges(z)
        for values, dash, kind in zip(pair, ("-", "--"), ("measured", "inferred")):
            if len(values):
                counts = np.maximum(np.histogram(values, edges)[0], 0.5)
                ax.step(
                    edges[:-1],
                    counts,
                    where="post",
                    linestyle=dash,
                    color=SERIES[z],
                    linewidth=1,
                    label=f"zone {z} {kind}",
                )
    ax.set_yscale("log")
    _cell_lines(ax)
    ax.set_xlim(0, 2 * (ILLEGAL_ZEROS + 1))
    ax.legend(fontsize=6, frameon=False, labelcolor=INK)


def eye_panel(ax, disk, key, angle_bins=256):
    """Interval against angle for one track, all revolutions."""
    edges = interval_edges(disk.tracks[key].ref.zone)
    hist = disk.eye(key, angle_bins, edges)
    _style(ax, f"timing eye, track {track_name(key)}: interval (cells) by angle")
    ax.imshow(
        np.log1p(hist),
        origin="lower",
        aspect="auto",
        cmap="magma",
        extent=(0, 360, edges[0], edges[-1]),
        interpolation="antialiased",
    )
    _cell_lines(ax, horizontal=True)


def drift_panel(ax, disk, key, samples=1024):
    """Each revolution's timing drift (cells) against angle."""
    track = disk.tracks[key]
    _style(
        ax,
        f"drift, track {track_name(key)} (cells): measured time less uniform, else bit offset",
    )
    for r in range(min(len(track.revs), len(SERIES))):
        turns, cells = track.drift(r, samples)
        order = np.argsort(turns)
        ax.plot(
            turns[order] * 360,
            cells[order],
            color=SERIES[r],
            linewidth=0.8,
            label=f"rev {r} ({track.revs[r].timing})",
        )
    ax.set_xlim(0, 360)
    ax.set_xlabel("angle from index or sector 0 (degrees)", color=INK, fontsize=7)
    ax.legend(fontsize=6, frameon=False, labelcolor=INK, ncol=3)


def key_panel(ax):
    """Colour key: density by cell-length deviation, and the marks."""
    _style(ax, "lightness: transitions per cell; hue: cell length")
    ax.imshow(colour_lut(), origin="lower", aspect="auto", extent=(-1, 1, 0, 1))
    ax.set_xlabel("shorter ← cell (zone steps) → longer", color=INK, fontsize=7)
    notes = (
        ("dots: no flux inferred from bits", STIPPLE),
        ("green: decode slip", FAULT),
        ("greyer: varies by revolution", INK_SECONDARY),
    )
    for i, (text, colour) in enumerate(notes):
        ax.text(
            1.05, 0.9 - 0.3 * i, text, color=colour, fontsize=7, transform=ax.transAxes
        )


SOURCE_TEXT = {
    "flux": "measured per transition (flux)",
    "tb": "measured per latched byte (TB)",
    "zones": "per byte from the image's speed map",
    "track": "per revolution only, at 300 rpm; transitions at decoded cells",
}


def figure(disk, size=1200, key=None, dpi=100):
    """Matplotlib figure: polar disk per side and the companion panels."""
    from matplotlib.figure import Figure

    images = disks(disk, size)
    panel = 5.5
    width = len(images) * size / dpi + panel
    height = max(size / dpi, 8.0)
    fig = Figure(figsize=(width, height), dpi=dpi, facecolor=SURFACE)
    disk_w = size / dpi / width
    for i, img in enumerate(images):
        ax = fig.add_axes([i * disk_w, 0.03, disk_w, 0.94])
        ax.imshow(img, interpolation="nearest")
        ax.set_axis_off()
        side = sides(disk.keys)[i]
        outer = track_name(min(k for k in disk.keys if (k & SIDE1) == side))
        label = f"side {int(bool(side))}, " if len(images) > 1 else ""
        ax.set_title(
            f"{disk.name}: {label}track {outer} outermost", color=INK, fontsize=9
        )
    x0, w = len(images) * disk_w + 0.5 / width, (panel - 1.0) / width
    key = eye_key(disk) if key is None else key
    panels = (
        (key_panel, 0.55),
        (lambda ax: histogram_panel(ax, disk), 1),
        (lambda ax: eye_panel(ax, disk, key), 1),
        (lambda ax: drift_panel(ax, disk, key), 1),
    )
    for slot, (draw, scale) in enumerate(panels):
        draw(fig.add_axes([x0, 0.79 - slot * 0.235, w * scale, 0.16]))
    lines = [f"timing: {SOURCE_TEXT[k]}: {v} revs" for k, v in disk.sources().items()]
    fig.text(x0, 0.01, "\n".join(lines), color=INK_SECONDARY, fontsize=6)
    return fig


def _pil(array):
    from PIL import Image

    return Image.fromarray(np.ascontiguousarray(array, np.uint8))


def save_png(disk, path, size=1200, key=None):
    """Write the polar view with its panels; returns the file size."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg

    canvas = FigureCanvasAgg(figure(disk, size, key))
    canvas.draw()
    _pil(np.asarray(canvas.buffer_rgba())[..., :3]).save(path, optimize=True)
    return pathlib.Path(path).stat().st_size


def save_apng(disk, path, size=800, duration=600):
    """Write one frame per revolution; returns the frame count."""
    from PIL import ImageDraw

    count = max(len(t.revs) for t in disk.tracks.values())
    frames = []
    for rev in range(count):
        img = _pil(np.concatenate(disks(disk, size, rev), axis=1))
        ImageDraw.Draw(img).text((8, 8), f"revolution {rev}", fill=INK)
        frames.append(img)
    frames[0].save(
        path, save_all=True, append_images=frames[1:], duration=duration, loop=0
    )
    return count
