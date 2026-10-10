"""Known test track: write it on a 1571, verify every capture path against it.

write homes a 1571 within --max-steps (never bumping) and writes the pattern; verify
locates, streams (1571) and takes RAM captures, each aligned to the regenerated
pattern; compare does the same for saved captures.
"""

import json
import pathlib

import numpy as np
from tqdm import tqdm

from .analysis import pattern as pt
from .analysis.synth import byte_capture
from .analysis.gcr import bits_per_revolution
from .disk import Archive, cells_at, revolution_cells, revolution_stream
from .monitor import Monitor
from .nibbler import Capture, Nibbler
from .ramcheck import digest, drive_options, locate
from .ramprobe import identify_model
from .speed import WINDOW, speed_trace

TRUTH = "truth.json"


def _drive_options(ap):
    drive_options(ap)
    ap.add_argument("--halftrack", type=int, required=True)
    ap.add_argument("--density", type=int, choices=range(4), help="default: zone")
    ap.add_argument("--seed", type=int, default=pt.SEED)


def add_arguments(ap):
    """Command line options: write, verify and compare actions."""
    sub = ap.add_subparsers(dest="action", required=True)
    _drive_options(sub.add_parser("write", help="write the pattern (1571)"))
    verify = sub.add_parser("verify", help="capture and compare with the pattern")
    _drive_options(verify)
    verify.add_argument("--repeats", type=int, default=3)
    verify.add_argument("--revolutions", type=int, default=2, help="per stream")
    verify.add_argument("--start", choices=("now", "sync"), default="now")
    verify.add_argument("--cells", type=int, help="cells per revolution written")
    verify.add_argument("--window", type=int, default=WINDOW, help="speed trace bytes")
    cmp = sub.add_parser("compare", help="saved captures against a truth record")
    cmp.add_argument("--truth", type=pathlib.Path, required=True)
    cmp.add_argument("--window", type=int, default=WINDOW, help="speed trace bytes")
    cmp.add_argument("captures", type=pathlib.Path, nargs="+")


def needs_adapter(args):
    """Only write and verify use a drive."""
    return args.action != "compare"


def _save_truth(truth, root):
    if root is not None:
        root.mkdir(parents=True, exist_ok=True)
        (root / TRUTH).write_text(json.dumps(truth.to_json()))


def layout(truth):
    """Regions without their bits."""
    return [
        {"name": r.name, "kind": r.kind, "offset": r.offset, "bits": r.length}
        | ({"run": r.run} if r.run else {})
        for r in truth.regions
    ]


def write(nib, truth, archive=None):
    """Measure the drive's revolution, then write the pattern last over it."""
    cells0 = revolution_cells(nib, truth.halftrack, archive)
    cells = cells_at(cells0, truth.density)
    truth.cells = int(round(cells))
    stream = revolution_stream(truth.data.tobytes(), cells)
    nib.write_track(truth.halftrack, stream, density=truth.density)
    return {"cells0": cells0, "cells": truth.cells, "stream_bytes": len(stream)}


def captures(nib, truth, args):
    """Named streams (when streaming) and RAM captures of the pattern's halftrack."""
    h, out = truth.halftrack, []
    kinds = (["stream"] if nib.streaming else []) + ["ram"]
    for kind in kinds:
        for i in tqdm(range(args.repeats), desc=f"{kind} h{h}", unit="cap"):
            if kind == "stream":
                cap = nib.stream(h, args.revolutions, density=truth.density)
            else:
                cap = nib.capture(h, truth.density, start=args.start)
            out.append((f"{kind}-h{h}-{i}.npz", cap))
    if args.save is not None:
        args.save.mkdir(parents=True, exist_ok=True)
        for name, cap in out:
            cap.save(args.save / name)
    return out


def _path(cap):
    return "stream" if cap.parsed is not None else "ram"


def _angles(pos, revolution, index):
    """Fractions of a revolution from the pattern start and from the index."""
    pos = np.asarray(pos, float)
    out = {"pattern": np.round(pos / revolution % 1, 4).tolist()}
    if index is not None:
        out["index"] = np.round((pos - index) / revolution % 1, 4).tolist()
    return out


def _check(truth, name, cap, refs):
    bits, byte_bit, begins = pt.capture_bits(cap)
    al = pt.align(bits, truth, period=truth.cells)
    rep = pt.region_report(truth, al, begins)
    entry = {
        "name": name,
        "path": _path(cap),
        "copies": len(al.placements),
        "found": al.found,
        "band": al.band,
        "sync_error": cap.sync_error,
        "revolution_bits": al.revolution(len(truth.bits)),
        "groups": rep["groups"],
        "syncs": rep["syncs"],
        "gap55_framing": rep["gap55_framing"],
        "digest": digest(cap, truth.track, refs),
    }
    return entry, al, byte_bit, rep["weak"]


def _summary(truth, entries, weak):
    """Per path: errors and slips in exact groups, revolutions, weak instability."""
    out = {}
    for path in sorted({e["path"] for e in entries}):
        mine = [e for e in entries if e["path"] == path]
        exact = [
            g for e in mine for g in e["groups"].values() if g["kind"] != "unstable"
        ]
        revs = [r for e in mine for r in e["revolution_bits"]]
        out[path] = {
            "captures": len(mine),
            "bit_errors": sum(g["errors"] for g in exact),
            "slips": sum(g["slips"] for g in exact),
            "revolution_bits": revs,
            "weak": pt.instability(weak[path], _weak_length(truth)),
        }
    out["weak_all"] = pt.instability(
        [r for reads in weak.values() for r in reads], _weak_length(truth)
    )
    return out


def _weak_length(truth):
    return sum(r.length for r in truth.regions if r.kind == pt.UNSTABLE)


def truth_capture(truth):
    """What byte ready latches from the pattern, syncs exact; it ends with the
    pattern, as the filler after it starts at the write's splice."""
    return byte_capture(truth.bits, len(truth.data), sync_error=0)


def compare(truth, named, window=WINDOW):
    """Every capture aligned to the truth, digested against it (and the streams,
    for RAM captures), with RAM speed traces placed on the disk's angle."""
    refs = [("truth", truth_capture(truth))]
    entries, weak, aligned = [], {}, []
    for name, cap in tqdm(named, desc="compare", unit="cap"):
        entry, al, byte_bit, reads = _check(truth, name, cap, refs)
        if entry["path"] == "stream":
            refs = refs + [(name, cap)]
            entry["index_bits"] = al.track_position(cap.index_bits()).tolist()
        entries.append(entry)
        weak.setdefault(entry["path"], []).extend(reads)
        aligned.append((cap, al, byte_bit))
    summary = _summary(truth, entries, weak)
    revs = [
        r
        for p in ("stream", "ram")
        for r in summary.get(p, {}).get("revolution_bits", [])
    ]
    revolution = float(
        np.median(revs) if revs else truth.cells or bits_per_revolution(truth.density)
    )
    idx = [i for e in entries for i in e.get("index_bits", [])]
    index = float(np.median(np.asarray(idx) % revolution)) if idx else None
    for entry, (cap, al, byte_bit) in zip(entries, aligned):
        entry["start_angle"] = _angles(al.track_position([0]), revolution, index)
        trace = speed_trace(cap, window) if cap.parsed is None else None
        if trace is not None:
            for x in trace["excursions"]:
                pos = al.track_position(byte_bit([x["start"]]))
                x["angle"] = _angles(pos, revolution, index)
            entry["speed"] = trace
    return {
        "truth": {
            "halftrack": truth.halftrack,
            "density": truth.density,
            "seed": truth.seed,
            "bits": len(truth.bits),
            "cells": truth.cells,
            "regions": layout(truth),
        },
        "revolution_bits": revolution,
        "index_bits": index,
        "summary": summary,
        "captures": entries,
    }


def execute(args, cbm):
    """Run the action and print its JSON report."""
    if args.action == "compare":
        truth = pt.Truth.from_json(json.loads(args.truth.read_text()))
        named = [(str(p), Capture.load(p)) for p in args.captures]
        report = compare(truth, named, args.window)
    else:
        cells = getattr(args, "cells", None)
        truth = pt.make_truth(args.halftrack, args.density, args.seed, cells)
        model = identify_model(cbm, args.dev)
        with (
            Monitor(cbm, args.dev, args.transport) as mon,
            Nibbler(mon, model, settle_ms=args.settle_ms) as nib,
        ):
            report = {"model": model, "located": locate(nib, args.max_steps)}
            if args.action == "write":
                report |= write(nib, truth, Archive(args.save))
                report["truth"] = {"bits": len(truth.bits), "regions": layout(truth)}
            else:
                report["streaming"] = nib.streaming
                named = captures(nib, truth, args)
                report |= compare(truth, named, args.window)
        _save_truth(truth, args.save)
    print(json.dumps(report))
    return report
