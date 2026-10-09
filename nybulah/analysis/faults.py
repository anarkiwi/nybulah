"""Localise and classify GCR decode failures in sync-framed bit streams.

A hidden Markov model tracks the 5-bit code phase: clean slots at a phase emit
legal codes, bursts emit anything and may resume at another phase. Parameters
are fitted by Viterbi training; a phase change is a bit slip mod 5.
"""

from enum import IntEnum

import numba
import numpy as np

from .capture import segments
from .gcr import GCR_DECODE

PHASES = 5
MAX_ROUNDS = 20


class FaultKind(IntEnum):
    """How framing differs after a decode failure."""

    CORRUPT = 0
    SLIP = 1
    BYTE = 2
    AMBIGUOUS = 3


FAULT_DTYPE = np.dtype(
    [
        ("segment", np.int64),
        ("bit", np.int64),
        ("width", np.int64),
        ("shift", np.int64),
        ("resynced", np.bool_),
        ("exact", np.bool_),
        ("kind", np.int8),
        ("byte", np.int64),
    ]
)


def code_valid(bits):
    """Whether the 5-bit code starting at each bit offset is legal GCR."""
    bits = np.asarray(bits, np.uint8)
    if len(bits) < PHASES:
        return np.zeros(0, np.bool_)
    windows = np.lib.stride_tricks.sliding_window_view(bits, PHASES)
    return GCR_DECODE[windows.astype(np.int16) @ (1 << np.arange(4, -1, -1))] >= 0


@numba.njit(cache=True)
def _step(cur, valid, params, back):  # pragma: no cover
    """One Viterbi slot: scores after it, filling ``back`` with the best predecessors.

    ``params`` are the logs of rho, tau, 1 - tau, beta and (1 - beta) / 5.
    """
    nxt = np.full(2 * PHASES, -np.inf)
    arg = PHASES + np.argmax(cur[PHASES:])
    resume = cur[arg] + params[4]
    for p in range(PHASES):
        stay = cur[p] + params[2]
        if valid[p]:
            nxt[p], back[p] = (stay, p) if stay >= resume else (resume, arg)
            nxt[p] -= params[0]
        enter, cont = cur[p] + params[1], cur[PHASES + p] + params[3]
        nxt[PHASES + p], back[PHASES + p] = (
            (enter, p) if enter > cont else (cont, PHASES + p)
        )
    return nxt


@numba.njit(cache=True)
def _viterbi(valid, offsets, slots, params):  # pragma: no cover
    """Most likely state per slot; states < 5 are clean phases, 5 + p a burst from p."""
    out = np.empty(slots.sum(), np.int8)
    base = 0
    for s, n in enumerate(slots):
        if n == 0:
            continue
        back = np.zeros((n, 2 * PHASES), np.int8)
        cur = np.full(2 * PHASES, -np.inf)
        cur[0] = -params[0] if valid[offsets[s]] else -np.inf
        cur[PHASES] = params[1]
        for t in range(1, n):
            at = offsets[s] + PHASES * t
            cur = _step(cur, valid[at : at + PHASES], params, back[t])
        state = np.argmax(cur)
        for t in range(n - 1, -1, -1):
            out[base + t] = state
            state = back[t, state]
        base += n
    return out


def _estimate(states, grid, first):
    """Parameters ``log(rho, tau, 1 - tau, beta, (1 - beta) / 5)`` from a state path.

    ``rho`` is how often a misaligned code is legal, ``tau`` the burst rate and
    ``beta`` burst persistence; each count carries a uniform prior.
    """
    burst = states >= PHASES
    enter = burst & (first | ~np.roll(burst, 1))
    clean = ~burst
    off = grid[clean].sum() - grid[clean, states[clean]].sum()
    rho = (off + 1) / ((PHASES - 1) * clean.sum() + 2)
    tau = (enter.sum() + 1) / (clean.sum() + 2)
    beta = (burst.sum() - enter.sum() + 1) / (burst.sum() + 2)
    return np.log([rho, tau, 1 - tau, beta, (1 - beta) / PHASES])


def _fit(valid, offsets, slots):
    """Viterbi training: state per slot, with each slot's stream and position."""
    owner = np.repeat(np.arange(len(slots)), slots)
    t = np.arange(slots.sum()) - np.repeat(np.cumsum(slots) - slots, slots)
    grid = valid[(offsets[owner] + PHASES * t)[:, None] + np.arange(PHASES)]
    first = np.zeros(len(owner), bool)
    first[(np.cumsum(slots) - slots)[slots > 0]] = True
    params = np.log([0.5, 0.01, 0.99, 0.5, 0.1])
    states = None
    for _ in range(MAX_ROUNDS):
        new = _viterbi(valid, offsets, slots, params)
        if states is not None and np.array_equal(new, states):
            break
        states = new
        params = _estimate(states, grid, first)
    return states, owner, t


def _pack(streams):
    """Concatenated code validity, each stream's offset into it and its slot count."""
    valids = [code_valid(s) for s in streams]
    slots = np.array([len(v) // PHASES for v in valids], np.int64)
    offsets = np.cumsum([0] + [len(v) for v in valids])[:-1].astype(np.int64)
    return np.concatenate(valids + [np.zeros(PHASES, bool)]), offsets, slots


def _streams_faults(streams):
    """Unclassified faults of each stream (segment index = position in ``streams``)."""
    states, owner, t = _fit(*_pack(streams))
    key = np.where(states >= PHASES, owner, -1)
    edges = np.flatnonzero(np.diff(np.concatenate(([-2], key, [-2]))))
    lo, hi = edges[:-1], edges[1:]
    keep = key[lo] >= 0
    lo, hi = lo[keep], hi[keep]
    nxt = np.minimum(hi, len(states) - 1)
    entry = states[lo] - PHASES
    faults = np.zeros(len(lo), FAULT_DTYPE)
    faults["segment"] = owner[lo]
    faults["bit"] = PHASES * t[lo] + entry
    faults["width"] = PHASES * (hi - lo)
    faults["resynced"] = (hi < len(states)) & (owner[nxt] == owner[lo])
    faults["shift"] = np.where(
        faults["resynced"], 2 - (states[nxt] - entry + 2) % PHASES, 0
    )
    return faults


def _classify(faults):
    """Set ``kind`` from the shift (bits lost) and whether it is exact."""
    shift, exact, resynced = faults["shift"], faults["exact"], faults["resynced"]
    kind = np.full(len(faults), FaultKind.AMBIGUOUS, np.int8)
    kind[resynced & (np.abs(shift) == 1)] = FaultKind.SLIP
    kind[resynced & (shift == 0)] = FaultKind.CORRUPT
    kind[exact & (shift != 0)] = FaultKind.SLIP
    kind[exact & (np.abs(shift) == 8)] = FaultKind.BYTE
    kind[exact & (shift == 0)] = FaultKind.CORRUPT
    faults["kind"] = kind
    return faults


def gcr_faults(bits):
    """Decode failures of one sync-framed GCR stream, as a ``FAULT_DTYPE`` array.

    ``shift`` counts bits lost (negative: gained) modulo 5, in ``[-2, 2]``.
    ``kind`` is CORRUPT (no shift), SLIP (one bit) or AMBIGUOUS (two bits, or
    ∓8: a whole byte; also failures that never resynchronise).
    """
    return _classify(_streams_faults([np.asarray(bits, np.uint8)]))


def _resolve(faults, content, period):
    """Exact shifts for lone faults whose segment has a fault-free copy a period away."""
    n = len(content)
    seg = faults["segment"]
    counts = np.bincount(seg, minlength=n)
    clean = (np.arange(n) < n - 1) & (counts == 0)
    lone = faults["resynced"] & (counts[seg] == 1) & (seg < n - 1)
    for ref in (seg - period, seg + period):
        inside = (ref >= 0) & (ref < n)
        ref = np.clip(ref, 0, n - 1)
        lost = content[ref] - content[seg]
        ok = lone & inside & clean[ref] & ~faults["exact"]
        ok &= (lost - faults["shift"]) % PHASES == 0
        faults["shift"] = np.where(ok, lost, faults["shift"])
        faults["exact"] |= ok


def capture_faults(capture, cycle=None):
    """Decode failures of every segment of a byte-ready capture (see :func:`gcr_faults`).

    ``byte`` is the capture buffer offset of each fault. Given a segmented
    ``cycle``, a lone fault is measured exactly against a clean copy one period away.
    """
    seg = segments(capture)
    content = seg.content
    faults = _streams_faults([seg.bits[b : b + c] for b, c in zip(seg.begin, content)])
    faults["byte"] = seg.first[faults["segment"]] + faults["bit"] // 8
    if cycle is not None and cycle.segments:
        _resolve(faults, content, cycle.segments)
    return _classify(faults)
