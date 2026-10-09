"""Synthetic flux images of a DOS disk with known physical features at known angles."""

import numpy as np

from .flux import divider
from .gcr import bits_per_revolution, speed_zone, to_bits
from .synth import _data_span, _dos, _long_sync

JITTER = 0.03
WOBBLE = 0.02
WEAK_JITTER = 0.5
NOISY_JITTER = 0.08
FAST_ZONE = 3
CROSSTALK = 0.5


def track_flux(bits, zone, revolutions, rng, **features):
    """``(times, index)`` in 16 MHz clocks of a track written in cells of ``zone``.

    Args:
        cells: per-bit cell lengths in cells of ``zone`` (default 1).
        wobble: speed amplitude once per turn: cell length ``1 + wobble sin(2 pi angle)``.
        drop: ``(start, end)`` bits written with no flux.
        weak: ``(start, end, sigma)`` bits read with extra jitter.
        slip: ``(rev, bit)``: one cell inserted after ``bit`` in revolution ``rev``.
        jitter: transition time standard deviation in cells.
    """
    bits = np.asarray(bits, np.uint8)
    cells = np.asarray(features.get("cells", np.ones(len(bits))), float)
    turn = bits_per_revolution(zone)
    pos = np.cumsum(cells) - cells
    ones = np.flatnonzero(bits)
    if "drop" in features:
        lo, hi = features["drop"]
        ones = ones[(ones < lo) | (ones >= hi)]
    sigma = np.full(len(ones), features.get("jitter", JITTER))
    if "weak" in features:
        lo, hi, extra = features["weak"]
        sigma[(ones >= lo) & (ones < hi)] = extra
    wobble = features.get("wobble", 0.0)
    revs = []
    for r in range(revolutions):
        p = pos[ones] + sigma * rng.standard_normal(len(ones))
        if features.get("slip", (-1,))[0] == r:
            p = p + (ones > features["slip"][1])
        p = np.clip(p, 0, turn - 1)
        p = p + wobble * turn / (2 * np.pi) * (1 - np.cos(2 * np.pi * p / turn))
        revs.append(r * turn + np.sort(p))
    clocks = 4 * divider(zone)
    return np.concatenate(revs) * clocks, np.arange(revolutions + 1) * turn * clocks


def _mixed(track, rng):
    """A DOS track whose second half is written in ``FAST_ZONE`` cells."""
    bits = to_bits(_dos(track, rng))
    cells = np.ones(len(bits))
    half = len(bits) // 2
    cells[half:] = divider(FAST_ZONE) / divider(speed_zone(track))
    return bits, cells, half


def _crosstalk(inner, outer, rng):
    """A head between two tracks: each transition of either read with ``CROSSTALK`` odds."""
    times = np.concatenate((inner[0], outer[0]))
    keep = rng.random(len(times)) < CROSSTALK
    return np.sort(times[keep]), inner[1]


def synthetic_flux_disk(revolutions=4, rng=0):
    """Index-aligned flux reads of a DOS disk carrying one of each physical feature.

    Returns ``(DiskImage, truth)``; ``truth`` lists ``(key, feature, start,
    end, revolution or None, value)`` with spans in turns from the index.
    """
    from ..formats.image import DiskImage, flux_capture

    rng = np.random.default_rng(rng)

    def turns(key, span):
        return tuple(
            np.asarray(span, float) / bits_per_revolution(speed_zone(key // 2))
        )

    tracks = {t: to_bits(_dos(t, rng)) for t in range(1, 36)}
    gcr, span = _long_sync(3, 5, rng)
    tracks[3] = to_bits(gcr)
    truth = [(6, "long_sync", *turns(6, span), None, 1.0)]
    tracks[31] = np.ones(len(tracks[31]), np.uint8)
    truth.append((62, "killer", 0.0, 1.0, None, 1.0))
    features = {2: {"wobble": WOBBLE}, 30: {"jitter": NOISY_JITTER}}
    truth.append((2, "wobble", 0.0, 1.0, None, WOBBLE))
    truth.append((30, "jitter", 0.0, 1.0, None, NOISY_JITTER))
    drop = _data_span(5, 3, 400, 1600)
    features[10] = {"drop": drop}
    truth.append((10, "no_flux", *turns(10, drop), None, 0.0))
    weak = _data_span(7, 6, 200, 1200)
    features[14] = {"weak": (*weak, WEAK_JITTER)}
    truth.append((14, "weak", *turns(14, weak), None, WEAK_JITTER))
    slip = _data_span(12, 6, 800, 1)
    features[24] = {"slip": (revolutions // 2, slip[0])}
    truth.append((24, "slip", *turns(24, slip), revolutions // 2, 1.0))
    tracks[26], cells, half = _mixed(26, rng)
    features[52] = {"cells": cells}
    truth.append((52, "density", *turns(52, (half, cells.sum())), None, cells[-1] - 1))
    flux = {
        2
        * t: track_flux(
            bits, speed_zone(t), revolutions, rng, **features.get(2 * t, {})
        )
        for t, bits in tracks.items()
    }
    flux[41] = _crosstalk(flux[40], flux[42], rng)
    truth.append((41, "crosstalk", 0.0, 1.0, None, CROSSTALK))
    image = DiskImage("synthflux")
    for key, (times, index) in sorted(flux.items()):
        image.tracks[key] = [flux_capture(times, index)]
    return image, truth
