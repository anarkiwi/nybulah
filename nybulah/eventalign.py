"""Least-cost offsets of a sequence of pass events against another pass's bytes.

An offset holds between events unless the bytes between them read differently:
a change unstable bytes explain costs least, others only within a measured bound,
ranked below events that land on nothing (docs/hardware.md, Test pattern).
"""

import numba
import numpy as np


@numba.njit(cache=True)
def _less(a, b, i, j):
    return a[i] < a[j] or (a[i] == a[j] and b[i] < b[j])


@numba.njit(cache=True)
def window_min(a, b, below, above):
    """Per column c: the first least ``(a, b)`` over columns ``c - below .. c + above``."""
    n = len(a)
    arg = np.empty(n, np.int64)
    queue = np.empty(n, np.int64)
    head = tail = nxt = 0
    for c in range(n):
        while nxt <= min(c + above, n - 1):
            while tail > head and _less(a, b, nxt, queue[tail - 1]):
                tail -= 1
            queue[tail] = nxt
            tail += 1
            nxt += 1
        while queue[head] < c - below:
            head += 1
        arg[c] = queue[head]
    return arg


@numba.njit(cache=True)
def sparse_min(a, b):
    """Sparse table: row k holds the first least ``(a, b)`` over ``c .. c + 2**k - 1``."""
    n = len(a)
    levels = 1
    while (1 << levels) <= n:
        levels += 1
    table = np.empty((levels, n), np.int64)
    table[0] = np.arange(n)
    for k in range(1, levels):
        half = 1 << (k - 1)
        for c in range(n):
            x = table[k - 1, c]
            if c + half < n and _less(a, b, table[k - 1, c + half], x):
                x = table[k - 1, c + half]
            table[k, c] = x
    return table


@numba.njit(cache=True)
def range_min(table, a, b, lo, hi):
    """The first least ``(a, b)`` over columns ``lo .. hi`` from :func:`sparse_min`."""
    k = 0
    while (2 << k) <= hi - lo + 1:
        k += 1
    x, y = table[k, lo], table[k, hi - (1 << k) + 1]
    return y if _less(a, b, y, x) else x


@numba.njit(cache=True)
def _follow(  # pylint: disable=too-many-arguments,too-many-locals
    miss, land, weight, start, absorb, free, rise, fall
):
    """Least path of :func:`align`: ``miss`` per event, ``(n + 1) ** 2`` per
    unexplained change and one per change, then most weight; ties stay."""
    n, width = miss.shape
    step = float(n + 1) ** 2
    key = np.full(width, np.inf)
    key[start] = 0.0
    neg = np.zeros(width)
    back = np.zeros((n, width), np.int64)
    for i in range(n):
        costly = window_min(key, neg, min(rise[i], width), min(fall[i], width))
        down = window_min(key, neg, 0, min(fall[i], width))
        table = sparse_min(key, neg)
        new, now = np.empty(width), np.empty(width)
        for c in range(width):
            best, tie, at = key[c], neg[c], c
            for f in range(1, min(free[i], c) + 1):
                if (key[c - f], neg[c - f]) < (best, tie):
                    best, tie, at = key[c - f], neg[c - f], c - f
            up = min(absorb[i, c], c)
            moves = (
                (range_min(table, key, neg, c - up, c - 1) if up else c, 1.0, up > 0),
                (down[c], 1.0, up > 0 and land[i, c]),
                (costly[c], step + 1.0, land[i, c]),
            )
            for j, cost, ok in moves:
                if ok and (key[j] + cost, neg[j]) < (best, tie):
                    best, tie, at = key[j] + cost, neg[j], j
            back[i, c] = at
            new[c], now[c] = best + miss[i, c], tie - weight[i, c]
        key, neg = new, now
    path = np.zeros(n, np.int64)
    if n:
        least = np.flatnonzero(key == key.min())
        path[-1] = least[np.argmin(neg[least])]
        for i in range(n - 1, 0, -1):
            path[i - 1] = back[i, path[i]]
    return path


def align(  # pylint: disable=too-many-arguments
    consistent,
    absorb=None,
    *,
    land=None,
    weight=None,
    start=None,
    rise=None,
    fall=None,
    free=None,
    soft=None,
):  # pylint: disable=too-many-locals
    """``(offsets, matched)`` per event: columns less ``start`` (default the middle).

    Event i reaching column c rises up to ``absorb[i, c]`` (or falls, if that is
    positive, onto ``land``) explained, else ``rise[i]`` or ``fall[i]`` unexplained;
    ``free[i]`` costs nothing. See :func:`_follow`; ``soft`` misses cost ``n + 1``.
    """
    consistent = np.asarray(consistent, bool)
    n, width = consistent.shape

    def per_event(a, default):
        return np.full(n, default, np.int64) if a is None else np.asarray(a, np.int64)

    scale = float(n + 1)
    soft = np.zeros((n, width), bool) if soft is None else np.asarray(soft, bool)
    absorb = np.zeros((n, width), np.int64) if absorb is None else np.asarray(absorb)
    start = width // 2 if start is None else int(start)
    path = _follow(
        np.where(consistent, 0.0, np.where(soft, scale, scale**3)),
        np.ones((n, width), bool) if land is None else np.asarray(land, bool),
        np.zeros((n, width)) if weight is None else np.asarray(weight, float),
        start,
        np.clip(absorb, 0, width).astype(np.int64),
        per_event(free, 0),
        np.clip(per_event(rise, width), 0, width),
        np.clip(per_event(fall, width), 0, width),
    )
    return path - start, consistent[np.arange(n), path]
