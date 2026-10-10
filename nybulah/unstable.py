"""Bits read from no flux or illegal GCR: a hidden Markov model of two sources.

Written GCR never holds more than ``ZEROS`` zeros in a row: its source emits each
bit given the ``ZEROS`` before it. Unstable bits come from structureless noise.
Both sources and the switch rates are fitted to the bits by expectation maximisation.
"""

import numba
import numpy as np

from .analysis.gcr import to_bits

ZEROS = 2
TOLERANCE = 1e-9
ROUNDS = 200


def contexts(bits, order=ZEROS):
    """The ``order`` bits before each bit as an integer, ones before the first."""
    bits = np.asarray(bits, np.int64)
    pad = np.concatenate((np.ones(order, np.int64), bits))
    out = np.zeros(len(bits), np.int64)
    for k in range(order):
        out = out * 2 + pad[k : k + len(bits)]
    return out


@numba.njit(cache=True)
def _smooth(bits, ctx, gcr, noise, switch):
    """``(posterior of noise, expected switches, log likelihood)`` by forward-backward."""
    n = len(bits)
    emit = np.empty((n, 2))
    for t in range(n):
        p = gcr[ctx[t]]
        emit[t, 0] = p if bits[t] else 1.0 - p
        emit[t, 1] = noise if bits[t] else 1.0 - noise
    alpha = np.empty((n, 2))
    scale = np.empty(n)
    prev = np.array([1.0 - switch[0], switch[0]])
    for t in range(n):
        if t:
            prev = np.array(
                [
                    alpha[t - 1, 0] * (1.0 - switch[0]) + alpha[t - 1, 1] * switch[1],
                    alpha[t - 1, 0] * switch[0] + alpha[t - 1, 1] * (1.0 - switch[1]),
                ]
            )
        alpha[t] = prev * emit[t]
        scale[t] = alpha[t].sum()
        alpha[t] /= scale[t]
    post = np.empty(n)
    moves = np.zeros(2)
    stays = np.zeros(2)
    beta = np.ones(2)
    for t in range(n - 1, -1, -1):
        g = alpha[t] * beta
        post[t] = g[1] / g.sum()
        if t == 0:
            break
        e = emit[t] * beta / scale[t]
        moves[0] += alpha[t - 1, 0] * switch[0] * e[1]
        moves[1] += alpha[t - 1, 1] * switch[1] * e[0]
        stays[0] += alpha[t - 1, 0] * (1.0 - switch[0]) * e[0]
        stays[1] += alpha[t - 1, 1] * (1.0 - switch[1]) * e[1]
        beta = np.array(
            [
                (1.0 - switch[0]) * e[0] + switch[0] * e[1],
                switch[1] * e[0] + (1.0 - switch[1]) * e[1],
            ]
        )
    return post, moves / (moves + stays), np.log(scale).sum()


def noise_posterior(bits):
    """Per bit: posterior probability that the noise source read it."""
    bits = np.asarray(bits, np.int64)
    n = len(bits)
    ctx = contexts(bits)
    gcr = np.full(1 << ZEROS, 0.5)
    gcr[0] = 1.0
    noise, switch, last = 0.5, np.full(2, 1.0 / n), -np.inf
    for _ in range(ROUNDS):
        post, rate, ll = _smooth(bits, ctx, gcr, noise, switch)
        held = 1.0 - post
        ones = np.bincount(ctx, held * bits, 1 << ZEROS)
        seen = np.bincount(ctx, held, 1 << ZEROS)
        gcr[1:] = np.where(seen[1:] > 0, ones[1:] / np.maximum(seen[1:], 1e-300), 0.5)
        noise = float(post @ bits / max(post.sum(), 1e-300))
        switch = np.clip(rate, 1.0 / n, 0.5)
        if ll - last <= TOLERANCE * abs(ll):
            break
        last = ll
    return post


def noise_runs(bits):
    """``(first, end)`` bit spans the noise source read (posterior above even),
    each holding a zero run GCR cannot write."""
    bits = np.asarray(bits, np.int64)
    noisy = noise_posterior(bits) > 0.5 if bits.size else np.zeros(0, bool)
    edges = np.flatnonzero(np.diff(np.concatenate(([0], noisy.view(np.int8), [0]))))
    first, end = edges[::2], edges[1::2]
    illegal = np.concatenate(([0], np.cumsum((bits == 0) & (contexts(bits) == 0))))
    keep = illegal[end] > illegal[first]
    return first[keep], end[keep]


def absorbable(data, latched):
    """``(absorb, noisy)`` per byte p: unstable bytes a sync ending before p can
    have hidden (a :func:`noise_runs` run reaching the byte before its ``latched``
    ones, with the byte in progress when it began), and whether byte p holds noise.
    """
    data = np.asarray(data, np.uint8)
    first, end = noise_runs(to_bits(data))
    byte = np.zeros(len(data) + 1, np.int64)
    np.add.at(byte, first // 8, 1)
    np.add.at(byte, (end - 1) // 8 + 1, -1)
    noisy = np.cumsum(byte)[:-1] > 0
    out = np.zeros(len(data), np.int64)
    if not first.size:
        return out, noisy
    j = 8 * np.arange(len(data)) - np.asarray(latched, np.int64)
    last = (j - 1) // 8
    r = np.maximum(np.searchsorted(first, j, side="left") - 1, 0)
    hit = (j > first[r]) & (end[r] - 1 >= 8 * last)
    out[hit] = last[hit] - first[r][hit] // 8 + 2
    return out, noisy
