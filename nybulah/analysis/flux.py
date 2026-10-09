"""Flux transitions to bits, as the 1541 read circuit clocks them out.

A transition reloads the 16 MHz divider (16 - zone) and clears the bit-cell
counter it drives. Every fourth divider carry, from the second, clocks one bit
into the shift register: 1 if no further carry since the transition, else 0.
"""

import numpy as np

from .gcr import CLOCK_HZ
from .cycle import NOMINAL_PERIOD

ROTATION_TICKS = int(CLOCK_HZ * NOMINAL_PERIOD)
ZONES = np.arange(4)


def divider(zone):
    """16 MHz clocks per divider carry: a quarter bit cell."""
    return 16 - zone


def interval_bits(ticks, zone):
    """Bits clocked out over transition intervals of ``ticks`` 16 MHz clocks.

    Bit ``k`` after a transition is clocked at ``(2 + 4k)`` divider carries,
    so an interval of ``n`` cells decodes as ``n`` bits within half a cell.
    """
    quarter = divider(zone)
    ticks = np.asarray(ticks, np.int64)
    return np.maximum(0, (ticks - 2 * quarter - 1) // (4 * quarter) + 1)


def _bits_before(times, first, counts, zone, at):
    """Bits clocked out before 16 MHz instants ``at``."""
    quarter = divider(zone)
    at = np.asarray(at, np.float64)
    prev = np.clip(np.searchsorted(times, at, side="right") - 1, 0, len(counts) - 1)
    since = np.ceil((at - times[prev] - 2 * quarter) / (4 * quarter))
    done = first[prev] + np.clip(since, 0, counts[prev]).astype(np.int64)
    return np.where(at < times[0], 0, done)


def decode_flux(times, zone, index=None, end=None):
    """Decode absolute transition times (16 MHz clocks) read at ``zone``.

    Times are latched on the clock edge; bits are clocked from the first
    transition until ``end`` (default: the last transition). Returns ``(bits,
    index_bits)`` with the bit offset of each instant in ``index`` (or None).
    """
    times = np.floor(np.asarray(times, np.float64)).astype(np.int64)
    if len(times) and end is not None and end > times[-1]:
        times = np.append(times, np.floor(end))
    if len(times) < 2:
        empty = None if index is None else np.zeros(len(index), np.int64)
        return np.zeros(0, np.uint8), empty
    counts = interval_bits(np.diff(times), zone)
    first = np.concatenate(([0], np.cumsum(counts)))
    pos = np.arange(first[-1]) - np.repeat(first[:-1], counts)
    bits = (pos % 4 == 0).astype(np.uint8)
    if index is None:
        return bits, None
    return bits, _bits_before(times, first, counts, zone, np.floor(index))


def estimate_zone(intervals):
    """Density zone whose bit cell best fits whole-cell transition intervals.

    Least squares over the four zones of the distance of each interval
    (16 MHz clocks) to its nearest whole number (at least one) of cells.
    """
    intervals = np.asarray(intervals, np.float64)
    if len(intervals) == 0:
        return None
    cells = intervals[:, None] / (4 * divider(ZONES))
    error = cells - np.maximum(np.rint(cells), 1)
    return int(np.argmin((error**2).mean(axis=0)))


def bits_to_times(bits, ticks=ROTATION_TICKS, zones=None):
    """Transition times spreading one revolution of ``bits`` over ``ticks`` clocks.

    ``zones`` (per bit) sizes each cell by its density; cells are equal without.
    """
    bits = np.asarray(bits, np.uint8)
    cells = (
        np.ones(len(bits)) if zones is None else divider(np.asarray(zones, np.float64))
    )
    starts = np.cumsum(cells) - cells
    return np.floor(starts[bits == 1] * ticks / max(cells.sum(), 1)).astype(np.int64)
