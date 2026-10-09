"""Disk map renderings: polar disk, animated disk, SVG/HTML strip and terminal strip.

Colours follow :class:`~nybulah.analysis.diskmap.Cls`; standard content is a
recessive grey so only anomalies stand out. Matplotlib and Pillow (the ``viz``
extra) are needed for the PNG and APNG renderings only.
"""

import html
import pathlib

import numpy as np

from .analysis.diskmap import (
    FAULT_MARK,
    UNSTABLE_MARK,
    WIDE,
    Cls,
    Kind,
    Stability,
)
from .formats.g64 import SIDE1
from .formats.image import track_name

PALETTE = (
    "#fcfcfb",
    "#dcdbd4",
    "#2a78d6",
    "#1baf7a",
    "#eda100",
    "#008300",
    "#4a3aa7",
    "#e34948",
    "#0b0b0b",
)
LABELS = (
    "not captured",
    "standard",
    "density",
    "gap / fill",
    "sync",
    "header",
    "data",
    "no flux / weak",
    "capture fault",
)
INK, INK_SECONDARY, SURFACE = "#0b0b0b", "#52514e", PALETTE[Cls.NONE]
CHARS = " .zgshdw!"
HATCH = "////"


def painted(regions):
    """Colour class each region is painted with: transient anomalies read as standard."""
    transient = regions["stability"] == Stability.TRANSIENT
    return np.where(
        transient & (regions["cls"] != Cls.FAULT), Cls.STANDARD, regions["cls"]
    )


def terminal_strip(dmap, width=64):
    """One fixed-width line per track: a character per angular bin group.

    Lowercase marks a class, uppercase an unstable bin, ``!`` a capture fault
    over standard content, ``.`` standard and space nothing captured.
    """
    grid, marks = dmap.raster(bins=width)
    chars = np.array(list(CHARS))[grid]
    chars = np.where(marks & UNSTABLE_MARK, np.char.upper(chars), chars)
    fault = (marks & FAULT_MARK).astype(bool) & (grid <= Cls.STANDARD)
    chars = np.where(fault, CHARS[Cls.FAULT], chars)
    names = [track_name(int(k)) + ("'" if k & SIDE1 else "") for k in dmap.keys]
    return [f"{n:>5} |{''.join(row)}|" for n, row in zip(names, chars)]


def legend_line():
    """Key to :func:`terminal_strip` characters."""
    return " ".join(f"{c!r}={label}" for c, label in zip(CHARS, LABELS))


def _sides(dmap):
    return [s for s in (0, SIDE1) if ((dmap.keys & SIDE1) == s).any()]


def _rings(dmap, grid, side):
    """Grid rows of one side by halftrack, innermost first; an absent halftrack
    shows the track just outside it, so whole tracks fill their pitch."""
    sel = (dmap.keys & SIDE1) == side
    half = dmap.keys[sel] & ~SIDE1
    lo, hi = int(half.min()), int(half.max())
    present = np.zeros(hi - lo + 1, bool)
    present[half - lo] = True
    source = np.maximum.accumulate(np.where(present, np.arange(len(present)), 0))
    rows = np.cumsum(present) - 1
    return grid[sel][rows[source]][::-1], len(present)


def _disk_axes(fig, rect, title):
    ax = fig.add_axes(rect, projection="polar")
    ax.set_theta_zero_location("N")
    ax.set_theta_direction(-1)
    ax.set_axis_off()
    ax.text(
        0.5,
        -0.02,
        title,
        transform=ax.transAxes,
        ha="center",
        va="top",
        color=INK_SECONDARY,
        fontsize=8,
    )
    return ax


def _draw_disk(ax, dmap, raster, side):
    from matplotlib.colors import ListedColormap

    grid, marks = raster
    cls, rows = _rings(dmap, grid, side)
    flags, _ = _rings(dmap, marks, side)
    hole = rows * 0.35
    theta = np.linspace(0, 2 * np.pi, grid.shape[1] + 1)
    radius = hole + np.arange(rows + 1)
    cmap = ListedColormap(PALETTE)
    ax.pcolormesh(theta, radius, cls, cmap=cmap, vmin=-0.5, vmax=len(Cls) - 0.5)
    mid_t, mid_r = (theta[:-1] + theta[1:]) / 2, (radius[:-1] + radius[1:]) / 2
    unstable = (flags & UNSTABLE_MARK).astype(float)
    if unstable.any():
        ax.contourf(
            mid_t, mid_r, unstable, levels=[0.5, 1.5], colors="none", hatches=[HATCH]
        )
    fault = (flags & FAULT_MARK).astype(float)
    if fault.any():
        ax.contour(mid_t, mid_r, fault, levels=[0.5], colors=INK, linewidths=0.6)
    ax.set_ylim(0, radius[-1])


def _legend(fig):
    from matplotlib.patches import Patch

    handles = [
        Patch(facecolor=PALETTE[c], edgecolor=INK_SECONDARY, linewidth=0.3)
        for c in range(Cls.STANDARD, Cls.FAULT)
    ]
    handles += [
        Patch(facecolor="none", edgecolor=INK_SECONDARY, hatch=HATCH, linewidth=0.3),
        Patch(facecolor="none", edgecolor=INK, linewidth=0.8),
    ]
    labels = list(LABELS[Cls.STANDARD : Cls.FAULT]) + ["unstable", LABELS[Cls.FAULT]]
    fig.legend(
        handles,
        labels,
        loc="center right",
        frameon=False,
        fontsize=8,
        labelcolor=INK,
        handlelength=1.2,
    )


def disk_figure(dmap, rev=None, size=(6.4, 4.4), dpi=100):
    """Matplotlib figure of the polar disk(s) of one revolution or all of them."""
    from matplotlib.figure import Figure

    fig = Figure(figsize=size, dpi=dpi, facecolor=SURFACE)
    raster = dmap.raster(rev)
    sides = _sides(dmap)
    width = 0.72 / len(sides)
    for i, side in enumerate(sides):
        name = (
            "side 1" if side else ("side 0" if len(sides) > 1 else "track 1 outermost")
        )
        ax = _disk_axes(fig, [0.02 + i * width, 0.08, width, 0.9], name)
        _draw_disk(ax, dmap, raster, side)
    _legend(fig)
    return fig


def _render(fig):
    from matplotlib.backends.backend_agg import FigureCanvasAgg

    canvas = FigureCanvasAgg(fig)
    canvas.draw()
    height = canvas.get_width_height()[1]
    boxes = [
        tuple(int(round(v)) for v in (b.x0, height - b.y1, b.x1, height - b.y0))
        for b in (ax.get_window_extent() for ax in fig.axes)
    ]
    return np.asarray(canvas.buffer_rgba())[..., :3].copy(), boxes


def save_png(dmap, path, rev=None):
    """Write the polar disk view as a PNG."""
    image, _ = _render(disk_figure(dmap, rev))
    _pil(image).save(path, optimize=True)


def _pil(array):
    from PIL import Image

    return Image.fromarray(np.ascontiguousarray(array, np.uint8))


def head_regions(dmap, fraction):
    """``(track, kind)`` of the local anomalies under a head at ``fraction`` of a revolution."""
    r = dmap.regions
    row = np.searchsorted(dmap.keys, r["track"])
    n = np.maximum(dmap.length[row], 1)
    pos = np.floor(fraction * n).astype(np.int64)
    under = (pos - r["start_bit"]) % n < (r["end_bit"] - r["start_bit"])
    shown = under & (painted(r) > Cls.STANDARD) & (r["cls"] != Cls.FAULT)
    shown &= ~WIDE[r["kind"]]
    pairs = np.unique(np.stack((r["track"][shown], r["kind"][shown]), axis=1), axis=0)
    return [(int(t), Kind(int(k))) for t, k in pairs]


def _readout(dmap, fraction):
    hits = head_regions(dmap, fraction)
    text = ", ".join(f"T{track_name(t)} {k.name.lower()}" for t, k in hits[:3])
    more = f" +{len(hits) - 3}" if len(hits) > 3 else ""
    return f"{360 * fraction:5.1f} deg  " + (text + more if hits else "standard")


def _annotate(frame, boxes, text):
    from PIL import ImageDraw

    draw = ImageDraw.Draw(frame)
    for x0, y0, x1, _ in boxes[: len(boxes)]:
        mid = (x0 + x1) // 2
        draw.polygon([(mid - 5, y0 - 2), (mid + 5, y0 - 2), (mid, y0 + 8)], fill=INK)
    draw.text((8, frame.height - 16), text, fill=INK)
    return frame


def _rotate(image, boxes, degrees):
    """Rotate each disk box of a rendered figure; the rest stays fixed."""
    out = _pil(image)
    for box in boxes:
        disk = out.crop(box).rotate(degrees, fillcolor=SURFACE)
        out.paste(disk, box[:2])
    return out


def _frames(dmap, mode, count):
    """Frames and per-frame text of one animation."""
    if mode == "revs":
        revs = int(dmap.revs.max())
        rendered = [_render(disk_figure(dmap, rev=r)) for r in range(revs)]
        disks = [(_pil(img), boxes) for img, boxes in rendered]
        return [
            (img, boxes, f"revolution {r + 1} of {revs}")
            for r, (img, boxes) in enumerate(disks)
        ]
    image, boxes = _render(disk_figure(dmap))
    disks = [boxes[i] for i in range(len(_sides(dmap)))]
    fractions = np.arange(count) / count
    return [
        (_rotate(image, disks, 360 * f), disks, _readout(dmap, f)) for f in fractions
    ]


def save_apng(dmap, path, mode=None, frames=36, colors=256):
    """Write an animated PNG: revolutions in turn (``mode="revs"``, the default when
    a track has several) or the disk rotating under a fixed head (``"rotate"``)."""
    mode = mode or ("revs" if dmap.revs.max() > 1 else "rotate")
    shots = [
        _annotate(img, boxes, text) for img, boxes, text in _frames(dmap, mode, frames)
    ]
    from PIL import Image

    first = shots[0].quantize(colors, dither=Image.Dither.NONE)
    rest = [s.quantize(palette=first, dither=Image.Dither.NONE) for s in shots[1:]]
    duration = 700 if mode == "revs" else 120
    first.save(
        path,
        format="PNG",
        save_all=True,
        append_images=rest,
        duration=duration,
        loop=0,
        optimize=True,
    )
    return len(shots)


def _hatch_defs():
    return (
        '<defs><pattern id="u" width="4" height="4" patternUnits="userSpaceOnUse" '
        f'patternTransform="rotate(45)"><line x1="0" y1="0" x2="0" y2="4" '
        f'stroke="{INK_SECONDARY}" stroke-width="1"/></pattern></defs>'
    )


def _rects(x0, x1, y, height, **attrs):
    extra = "".join(f' {k.replace("_", "-")}="{v}"' for k, v in attrs.items())
    return "".join(
        f'<rect x="{a:.1f}" y="{y:.1f}" width="{max(b - a, 0.5):.1f}" '
        f'height="{height:.1f}"{extra}/>'
        for a, b in zip(x0, x1)
    )


def _tooltip(region):
    return html.escape(
        f"track {track_name(int(region['track']))} rev {region['rev']}: "
        f"{Kind(int(region['kind'])).name.lower()} bits {region['start_bit']}-"
        f"{region['end_bit']} detail {region['detail']} "
        f"{Stability(int(region['stability'])).name.lower()}"
    )


def _region_svg(region, cls, scale, y, height, n):
    """One region as a group of rectangles (split where it wraps) with its tooltip."""
    start, end = int(region["start_bit"]), int(region["end_bit"])
    pieces = [(start, min(end, n))] + ([(0, end - n)] if end > n else [])
    x0 = np.array([a for a, _ in pieces]) * scale
    x1 = np.array([b for _, b in pieces]) * scale
    if cls == Cls.FAULT:
        body = _rects(x0, x1, y, height, fill="none", stroke=INK, stroke_width=0.6)
    else:
        body = _rects(x0, x1, y, height, fill=PALETTE[cls])
        if region["stability"] == Stability.UNSTABLE and cls > Cls.STANDARD:
            body += _rects(x0, x1, y, height, fill="url(#u)")
    return f"<g><title>{_tooltip(region)}</title>{body}</g>"


def _sector_labels(regions, scale, y):
    hdr = regions[regions["cls"] == Cls.HEADER]
    hdr = np.concatenate((regions[regions["kind"] == Kind.HEADER], hdr))
    return "".join(
        f'<text x="{h["start_bit"] * scale:.1f}" y="{y:.1f}">{h["detail"]}</text>'
        for h in hdr[hdr["rev"] == 0]
    )


def strip_svg(dmap, width=1200, row=12, margin=48):
    """Strip view as SVG: a row per track (a band per revolution), bit position
    across, and a sector-number row under each track; one tooltip per region."""
    r = dmap.regions
    cls = painted(r)
    band = row + row * 0.8
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width + margin}" '
        f'height="{band * len(dmap.keys) + row}" font-family="system-ui, sans-serif" '
        f'font-size="{row * 0.7:.1f}" fill="{INK_SECONDARY}">',
        _hatch_defs(),
    ]
    for i, key in enumerate(dmap.keys):
        top, n = i * band, max(int(dmap.length[i]), 1)
        mine = np.flatnonzero(r["track"] == key)
        sub = row / dmap.revs[i]
        out.append(
            f'<text x="0" y="{top + row * 0.8:.1f}" fill="{INK}">'
            f"{track_name(int(key))}</text>"
            f'<g transform="translate({margin},0)">'
        )
        for j in mine:
            whole = WIDE[r["kind"][j]]
            y, height = (top, row) if whole else (top + r["rev"][j] * sub, sub)
            out.append(_region_svg(r[j], cls[j], width / n, y, height, n))
        out.append(_sector_labels(r[mine], width / n, top + row + row * 0.7) + "</g>")
    return "".join(out) + "</svg>"


def _table(dmap):
    r = dmap.regions
    rows = r[(painted(r) > Cls.STANDARD) | (r["cls"] == Cls.FAULT)]
    cells = "".join(
        "<tr>"
        + "".join(
            f"<td>{v}</td>"
            for v in (
                track_name(int(x["track"])),
                x["rev"],
                Kind(int(x["kind"])).name.lower(),
                LABELS[x["cls"]],
                x["start_bit"],
                x["end_bit"],
                x["detail"],
                Stability(int(x["stability"])).name.lower(),
            )
        )
        + "</tr>"
        for x in rows
    )
    head = "".join(
        f"<th>{h}</th>"
        for h in (
            "track",
            "rev",
            "kind",
            "class",
            "start",
            "end",
            "detail",
            "stability",
        )
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{cells}</tbody></table>"


def _swatches():
    items = [
        (f"background:{PALETTE[c]}", LABELS[c]) for c in range(Cls.STANDARD, Cls.FAULT)
    ]
    items += [
        (f"background:{HATCH_CSS}", "unstable"),
        (f"border:1px solid {INK}", "capture fault"),
    ]
    return "".join(
        f'<span class="sw"><i style="{css}"></i>{label}</span>' for css, label in items
    )


HATCH_CSS = (
    f"repeating-linear-gradient(45deg,{INK_SECONDARY} 0 1px,transparent 1px 4px)"
)
STYLE = (
    f"body{{background:{SURFACE};color:{INK};font:14px system-ui,sans-serif;margin:0;"
    "padding:16px}.svg{overflow-x:auto}table{border-collapse:collapse;"
    "font-variant-numeric:tabular-nums}td,th{padding:2px 8px;text-align:right;"
    "border-bottom:1px solid #e1e0d9}.sw{margin-right:12px;white-space:nowrap}"
    ".sw i{display:inline-block;width:12px;height:12px;margin-right:4px;"
    "vertical-align:middle}"
)


def strip_html(dmap, title="disk map"):
    """HTML page: legend, the strip SVG and a table of the non-standard regions."""
    return (
        f"<title>{html.escape(title)}</title><style>{STYLE}</style>"
        f"<h1>{html.escape(title)}</h1><p>{_swatches()}</p>"
        f'<div class="svg">{strip_svg(dmap)}</div>'
        f"<h2>Non-standard regions</h2>{_table(dmap)}"
    )


def save(dmap, path, animate=False, mode=None, title=None):
    """Write ``dmap`` in the format named by ``path``'s extension (.png, .apng, .svg, .html)."""
    path = pathlib.Path(path)
    suffix = path.suffix.lower()
    if suffix == ".apng" or (suffix == ".png" and animate):
        return save_apng(dmap, path, mode)
    if suffix == ".png":
        return save_png(dmap, path)
    if suffix == ".svg":
        return path.write_text(strip_svg(dmap), encoding="utf-8")
    if suffix == ".html":
        return path.write_text(strip_html(dmap, title or path.stem), encoding="utf-8")
    raise ValueError(f"{path}: output must be .png, .apng, .svg or .html")
