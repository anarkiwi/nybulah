"""Least-cost offsets of a sequence of pass events against another pass's bytes.

Each event takes a column (an offset); a path pays one per inconsistent event
and one per change of column, then prefers weight. Changes are bounded per
event, and small rises may be free.
"""

import numba
import numpy as np


@numba.njit(cache=True)
def window_min(a, b, below, above):
    """Per column c: the first least ``(a, b)`` over columns ``c - below .. c + above``."""
    n = len(a)
    arg = np.empty(n, np.int64)
    queue = np.empty(n, np.int64)
    head = tail = nxt = 0
    for c in range(n):
        while nxt <= min(c + above, n - 1):
            while tail > head and (
                a[queue[tail - 1]] > a[nxt]
                or (a[queue[tail - 1]] == a[nxt] and b[queue[tail - 1]] > b[nxt])
            ):
                tail -= 1
            queue[tail] = nxt
            tail += 1
            nxt += 1
        while queue[head] < c - below:
            head += 1
        arg[c] = queue[head]
    return arg


@numba.njit(cache=True)
def _follow(
    miss, land, weight, start, free, rise, fall
):  # pylint: disable=too-many-arguments
    """Least-cost column path of :func:`align`; ties go to fewer changes, then to
    staying; ``weight`` is scaled to sum below one."""
    n, width = miss.shape
    fwd = np.full(width, np.inf)
    fwd[start] = 0.0
    chg = np.zeros(width, np.int64)
    back = np.zeros((n, width), np.int64)
    for i in range(n):
        arg = window_min(fwd, chg, min(rise[i], width), min(fall[i], width))
        new, now = np.empty(width), np.empty(width, np.int64)
        for c in range(width):
            best, steps, at = fwd[c], chg[c], c
            for f in range(1, min(free[i], c) + 1):
                if (fwd[c - f], chg[c - f]) < (best, steps):
                    best, steps, at = fwd[c - f], chg[c - f], c - f
            j = arg[c]
            if land[i, c] and (fwd[j] + 1.0, chg[j] + 1) < (best, steps):
                best, steps, at = fwd[j] + 1.0, chg[j] + 1, j
            back[i, c] = at
            new[c], now[c] = best + miss[i, c] - weight[i, c], steps
        fwd, chg = new, now
    path = np.zeros(n, np.int64)
    if n:
        least = np.flatnonzero(fwd == fwd.min())
        path[-1] = least[np.argmin(chg[least])]
        for i in range(n - 1, 0, -1):
            path[i - 1] = back[i, path[i]]
    return path


def align(  # pylint: disable=too-many-arguments
    consistent, free=None, land=None, weight=None, start=None, rise=None, fall=None
):
    """Offsets (columns less ``start``, default the middle) per event, by dynamic
    programming.

    Minimises inconsistent events plus offset changes, then maximises the
    ``weight`` of consistent events, from column ``start``. A change lands
    on ``land`` columns only, rising by at most ``rise[i]`` or falling by at
    most ``fall[i]`` before event i; a rise of up to ``free[i]`` costs
    nothing. Returns ``(offsets, matched)``.
    """
    consistent = np.asarray(consistent, bool)
    n, width = consistent.shape
    unbounded = np.full(n, width, np.int64)

    def bound(a):
        return unbounded if a is None else np.clip(np.asarray(a), 0, width)

    weight = np.zeros((n, width)) if weight is None else np.asarray(weight, float)
    weight = weight / (1.0 + weight.max(axis=1, initial=0.0).sum())

    path = _follow(
        (~consistent).astype(np.float64),
        np.ones((n, width), bool) if land is None else np.asarray(land, bool),
        weight,
        width // 2 if start is None else int(start),
        np.zeros(n, np.int64) if free is None else np.asarray(free, np.int64),
        bound(rise).astype(np.int64),
        bound(fall).astype(np.int64),
    )
    start = width // 2 if start is None else int(start)
    return path - start, consistent[np.arange(n), path]
