"""RAM capture check: repeated captures of single tracks against a stream; never bumps.

Homes a 1571 (within --max-steps) or locates a 1541, then per halftrack streams
(s4, firmware v12) and takes --repeats BITS/TB/TS captures; each is compared per
sector with the stream and --reference captures: differing bytes, extra syncs.
"""

import json
import pathlib

import numpy as np
from tqdm import tqdm

from . import homeprobe
from .analysis.capture import segments
from .analysis.sector import SectorError, decode_track
from .monitor import Monitor, protocols
from .nibbler import MAX_HALFTRACK, SETTLE_MS, Capture, Nibbler
from .ramprobe import identify_model


def add_arguments(ap):
    """Command line options."""
    ap.add_argument("--dev", type=int, default=8)
    ap.add_argument("--transport", choices=protocols() or ("s1",), default="s4")
    ap.add_argument("--halftracks", type=int, nargs="+", default=[36, 50])
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--start", choices=("now", "sync"), default="now")
    ap.add_argument("--settle-ms", type=int, default=SETTLE_MS)
    ap.add_argument(
        "--reference", type=pathlib.Path, nargs="*", default=[], help="saved captures"
    )
    ap.add_argument("--save", type=pathlib.Path, help="directory for every capture")
    ap.add_argument(
        "--max-steps",
        type=int,
        default=MAX_HALFTRACK,
        help="1571: refuse when homing needs more outward steps",
    )


def headers(cap, track):
    """``(decode, {sector: byte index of its header's first byte})`` of a capture."""
    seg = segments(cap)
    dec = decode_track(seg.bits, track)
    found = np.flatnonzero(dec.offsets >= 0)
    k = np.minimum(np.searchsorted(seg.begin, dec.offsets[found]), len(seg.begin) - 1)
    exact = seg.begin[k] == dec.offsets[found]
    return dec, dict(zip(found[exact].tolist(), seg.first[k[exact]].tolist()))


def compare(cap, ref, track):
    """Per sector headed in both, over its span in ``cap`` (to the next header):
    bytes compared and differing, and syncs (bytes after the header) one lacks."""
    dec, mine = headers(cap, track)
    ref_dec, theirs = headers(ref, track)
    starts = np.array(sorted(mine.values()) + [cap.valid_bytes])
    rows = []
    for s in sorted(set(mine) & set(theirs)):
        a, b = mine[s], theirs[s]
        n = int(
            min(starts[np.searchsorted(starts, a, "right")] - a, ref.valid_bytes - b)
        )
        diff = np.flatnonzero(cap.data[a : a + n] != ref.data[b : b + n])
        here = cap.positions[(cap.positions > a) & (cap.positions < a + n)] - a
        there = ref.positions[(ref.positions > b) & (ref.positions < b + n)] - b
        extra = np.setdiff1d(here, there)
        runs = cap.sync_bits[np.searchsorted(cap.positions, extra + a)]
        rows.append(
            {
                "sector": s,
                "error": int(dec.errors[s]),
                "ref_error": int(ref_dec.errors[s]),
                "bytes": n,
                "differ": len(diff),
                "first_diff": int(diff[0]) if len(diff) else None,
                "extra_syncs": [[int(p), int(r)] for p, r in zip(extra, runs)],
                "missing_syncs": np.setdiff1d(there, here).tolist(),
            }
        )
    return rows


def against(cap, track, name, ref):
    """Totals of :func:`compare`, with the sectors that differ or gained syncs."""
    rows = compare(cap, ref, track)
    return {
        "reference": name,
        "sectors": len(rows),
        "differ": sum(r["differ"] for r in rows),
        "extra_syncs": sum(len(r["extra_syncs"]) for r in rows),
        "missing_syncs": sum(len(r["missing_syncs"]) for r in rows),
        "rows": [r for r in rows if r["differ"] or r["extra_syncs"]],
    }


def digest(cap, track, refs=()):
    """Sync and sector summary of a capture, compared with each ``(name, ref)``."""
    runs = cap.sync_bits
    dec = decode_track(cap.bits(), track)
    return {
        "status": cap.status,
        "bytes": len(cap.data),
        "syncs": len(runs),
        "sync_bits": [int(f(runs)) for f in (np.min, np.median)] if len(runs) else None,
        "lost": cap.lost,
        "sectors_ok": int((dec.errors == SectorError.OK).sum()),
        "against": [against(cap, track, n, r) for n, r in refs],
    }


def _locate(nib, max_steps):
    if nib.model != "1571":
        return nib.locate()
    plan = homeprobe.dry(nib)
    if plan["outward_steps"] > max_steps:
        raise ValueError(
            f"homing needs {plan['outward_steps']} outward steps, over {max_steps}"
        )
    return nib.home(plan["estimate"])


def check_track(nib, halftrack, args, refs):
    """Stream (when streaming) and RAM captures of one halftrack, saved and digested."""
    track = halftrack // 2
    refs = [(n, c) for n, c in refs if c.halftrack == halftrack]
    out = []
    if nib.streaming:
        name, cap = f"stream-h{halftrack}.npz", nib.stream(halftrack)
        out.append((name, cap, digest(cap, track, refs)))
        refs = refs + [(name, cap)]
    for i in tqdm(range(args.repeats), desc=f"halftrack {halftrack}", unit="cap"):
        cap = nib.capture(halftrack, start=args.start)
        out.append((f"ram-h{halftrack}-{i}.npz", cap, digest(cap, track, refs)))
    if args.save is not None:
        args.save.mkdir(parents=True, exist_ok=True)
        for name, cap, _ in out:
            cap.save(args.save / name)
    return [{"name": name} | d for name, _, d in out]


def execute(args, cbm):
    """Locate, capture, compare and print the report as JSON."""
    refs = [(str(p), Capture.load(p)) for p in args.reference]
    model = identify_model(cbm, args.dev)
    with (
        Monitor(cbm, args.dev, args.transport) as mon,
        Nibbler(mon, model, settle_ms=args.settle_ms) as nib,
    ):
        report = {
            "model": model,
            "streaming": nib.streaming,
            "located": _locate(nib, args.max_steps),
        }
        report["tracks"] = {h: check_track(nib, h, args, refs) for h in args.halftracks}
    print(json.dumps(report))
    return report
