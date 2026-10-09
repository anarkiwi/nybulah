"""Classify GCR decode failures over archived captures and test where they fall.

Usage: python tools/fault_report.py CAPTURE.npz...
"""

import sys
from collections import Counter

import numpy as np
from tqdm import tqdm

from nybulah.analysis.capture import segments
from nybulah.analysis.cycle import TrackKind, find_cycle
from nybulah.analysis.faults import FaultKind, capture_faults
from nybulah.nibbler import Capture

PAGE = 256


def ks_uniform(u):
    """Kolmogorov-Smirnov statistic and asymptotic p-value of samples against U(0, 1)."""
    u = np.sort(np.asarray(u, float))
    n = len(u)
    if not n:
        return 0.0, 1.0
    i = np.arange(1, n + 1)
    d = max((i / n - u).max(), (u - (i - 1) / n).max())
    lam = (np.sqrt(n) + 0.12 + 0.11 / np.sqrt(n)) * d
    k = np.arange(1, 101)
    p = 2 * np.sum((-1.0) ** (k - 1) * np.exp(-2 * (k * lam) ** 2))
    return float(d), float(np.clip(p, 0, 1))


def rayleigh(phase):
    """Rayleigh test of phases in [0, 1) for clustering: (resultant length, p-value)."""
    n = len(phase)
    if not n:
        return 0.0, 1.0
    r = np.abs(np.exp(2j * np.pi * np.asarray(phase)).mean())
    z = n * r**2
    p = np.exp(-z) * (1 + (2 * z - z**2) / (4 * n))
    return float(r), float(np.clip(p, 0, 1))


def collect(paths):
    """All faults with their segment-relative position, over the captures."""
    rows = []
    for path in tqdm(paths, desc="faults", unit="cap"):
        cap = Capture.load(path)
        cycle = find_cycle(cap)
        seg = segments(cap)
        faults = capture_faults(
            cap, cycle if cycle.kind == TrackKind.FORMATTED else None
        )
        content = seg.content[faults["segment"]]
        for f, c in zip(faults, content):
            rows.append((path, f, f["bit"] / max(c, 1), seg.run[f["segment"]] < 0))
    return rows


def main(paths):
    rows = collect(paths)
    lead = [r for r in rows if r[3] and r[1]["bit"] < 16]
    rest = [r for r in rows if not (r[3] and r[1]["bit"] < 16)]
    print(f"faults {len(rows)}; at the start of a capture's first segment {len(lead)}")
    kinds = Counter((FaultKind(r[1]["kind"]).name, bool(r[1]["exact"])) for r in rest)
    for (kind, exact), n in sorted(kinds.items()):
        print(f"  {kind:9s} {'exact' if exact else 'mod 5':5s} {n}")
    shifts = Counter(int(r[1]["shift"]) for r in rest)
    print("  shift (bits lost):", dict(sorted(shifts.items())))
    byte = np.array([r[1]["byte"] for r in rest])
    r, p = rayleigh((byte % PAGE) / PAGE)
    near = np.minimum(byte % PAGE, PAGE - byte % PAGE)
    print(
        f"page phase: resultant {r:.3f}, Rayleigh p {p:.3g}; within 4 bytes of a page edge "
        f"{int((near <= 4).sum())} (expected {len(byte) * 9 / PAGE:.1f})"
    )
    d, p = ks_uniform([r[2] for r in rest])
    print(f"position within segment vs uniform: KS D {d:.3f}, p {p:.3g}")
    first = np.array([r[1]["bit"] for r in rest])
    print(
        "bits after sync end, quartiles:", np.percentile(first, [25, 50, 75]).tolist()
    )


if __name__ == "__main__":
    main(sys.argv[1:])
