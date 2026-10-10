"""Known test track: write it on a 1571, verify every capture path against it.

write homes a 1571 within --max-steps (never bumping) and writes the pattern; verify
locates, streams (1571) and takes RAM captures, each aligned to the regenerated
pattern; compare does the same for saved captures. cells measures the cells per
revolution at each density on a scratch halftrack (writing it); halftracks reads
other halftracks against a written truth (cross-talk).
"""

import json
import pathlib

import numpy as np
from tqdm import tqdm

from .analysis import pattern as pt
from .analysis.synth import byte_capture
from .analysis.gcr import NOMINAL_RPM, bit_rate, bits_per_revolution
from .disk import Archive, TrackJob, revolution_cells, revolution_stream
from .monitor import Monitor
from .nibbler import CPU_HZ, HOME_HALFTRACK, MAX_HALFTRACK, ST_NOINDEX, Capture
from .nibbler import Nibbler
from .ramcheck import digest, drive_options, locate
from .ramprobe import identify_model
from .speed import WINDOW, speed_trace

TRUTH = "truth.json"


def _pattern_options(ap):
    drive_options(ap)
    ap.add_argument("--halftrack", type=int, required=True)
    ap.add_argument("--density", type=int, choices=range(4), help="default: zone")
    ap.add_argument("--seed", type=int, default=pt.SEED)
    ap.add_argument("--region", choices=pt.VARIANTS, default=pt.VARIANTS[0])


def _capture_options(ap):
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--revolutions", type=int, default=2, help="per stream")
    ap.add_argument("--start", choices=("now", "sync"), default="now")
    ap.add_argument("--window", type=int, default=WINDOW, help="speed trace bytes")


def _lead(text):
    value = int(text)
    if value < 0:
        raise ValueError(text)
    return value


def add_arguments(ap):
    """Command line options: write, verify, compare, cells and halftracks actions."""
    sub = ap.add_subparsers(dest="action", required=True)
    write_ = sub.add_parser("write", help="write the pattern (1571)")
    _pattern_options(write_)
    write_.add_argument("--lead", type=_lead, help="$55 bytes before the pattern")
    verify = sub.add_parser("verify", help="capture and compare with the pattern")
    _pattern_options(verify)
    _capture_options(verify)
    verify.add_argument("--cells", type=int, help="cells per revolution written")
    cmp = sub.add_parser("compare", help="saved captures against a truth record")
    cmp.add_argument("--truth", type=pathlib.Path, required=True)
    cmp.add_argument("--window", type=int, default=WINDOW, help="speed trace bytes")
    cmp.add_argument("captures", type=pathlib.Path, nargs="+")
    probe = sub.add_parser(
        "cells", help="cells per revolution per density (writes --halftrack)"
    )
    drive_options(probe)
    probe.add_argument("--halftrack", type=int, required=True)
    probe.add_argument(
        "--densities", type=int, nargs="+", choices=range(4), default=list(range(4))
    )
    near = sub.add_parser("halftracks", help="read halftracks against a truth")
    drive_options(near)
    near.add_argument("--truth", type=pathlib.Path, required=True)
    near.add_argument("--halftracks", type=int, nargs="+", required=True)
    _capture_options(near)
    near.set_defaults(repeats=1)


def needs_adapter(args):
    """Every action but compare uses a drive."""
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


def write(nib, truth, archive=None, lead=None):
    """Measure the drive's revolution at the pattern's density, then write the
    pattern over it after ``lead`` $55 bytes (default: last in the write)."""
    truth.cells = revolution_cells(nib, truth.halftrack, archive, truth.density)
    payload = truth.data.tobytes()
    stream = revolution_stream(payload, truth.cells, lead)
    truth.lead = len(stream) - len(payload) if lead is None else lead
    nib.write_track(truth.halftrack, stream, density=truth.density)
    return {
        "density": truth.density,
        "cells": truth.cells,
        "lead": truth.lead,
        "stream_bytes": len(stream),
    }


def captures(nib, halftrack, density, args, index=False):
    """Named streams (when streaming) and RAM captures of a halftrack; with
    ``index`` on a 1571, one more RAM capture started at the index."""
    h, out = halftrack, []
    plan = [("stream", "now")] * nib.streaming + [("ram", args.start)]
    for kind, start in plan:
        for i in tqdm(range(args.repeats), desc=f"{kind} h{h}", unit="cap"):
            if kind == "stream":
                cap = nib.stream(h, args.revolutions, density=density)
            else:
                cap = nib.capture(h, density, start=start)
            out.append((f"{kind}-h{h}-{i}.npz", cap))
    if index and nib.model == "1571":
        out.append((f"ram-index-h{h}.npz", nib.capture(h, density, start="index")))
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


def _unstable(truth, reads):
    """Per unstable group: instability across ``reads`` (group -> reads)."""
    length = {}
    for r in truth.regions:
        if r.kind == pt.UNSTABLE:
            length[r.group] = length.get(r.group, 0) + r.length
    return {g: pt.instability(reads.get(g, []), n) for g, n in length.items()}


def _summary(truth, entries, weak):
    """Per path: errors and slips in exact groups, revolutions, per unstable
    group its instability across every read of it."""
    out = {}
    for path in sorted({e["path"] for e in entries}):
        mine = [e for e in entries if e["path"] == path]
        exact = [
            g for e in mine for g in e["groups"].values() if g["kind"] != pt.UNSTABLE
        ]
        revs = [r for e in mine for r in e["revolution_bits"]]
        out[path] = {
            "captures": len(mine),
            "bit_errors": sum(g["errors"] for g in exact),
            "slips": sum(g["slips"] for g in exact),
            "revolution_bits": revs,
            "unstable": _unstable(truth, weak[path]),
        }
    merged = {}
    for reads in weak.values():
        for g, r in reads.items():
            merged.setdefault(g, []).extend(r)
    out["unstable_all"] = _unstable(truth, merged)
    return out


def truth_capture(truth):
    """What byte ready latches from the pattern, syncs exact; it ends with the
    pattern, as the filler after it starts at the write's splice."""
    return byte_capture(truth.bits, len(truth.data), sync_error=0)


def _indexed(cap):
    """A RAM capture whose BITS pass began at an index edge."""
    return cap.parsed is None and cap.start == "index" and not cap.status & ST_NOINDEX


def circular_mean(pos, period):
    """Mean of positions on a circle of ``period``, in ``[0, period)``; None if none."""
    pos = np.asarray(pos, float)
    if not pos.size:
        return None
    mean = np.angle(np.exp(2j * np.pi * pos / period).mean()) / (2 * np.pi)
    return float(mean % 1 * period)


def _offsets(pos, index, revolution):
    """Signed distances of track positions after ``index``, within half a turn."""
    half = revolution / 2
    off = (np.asarray(pos, float) - index + half) % revolution - half
    return np.round(off).astype(int).tolist()


def _index(entries, aligned, revolution):
    """The index's track position: the circular mean of the starts of RAM
    captures begun at an index edge. Stream INDEX metadata can trail its edge
    (drive/stream.s checks the index only with no metadata due), so stream
    edges are reported against it, not used for it."""
    starts = [
        al.track_position([0])[0]
        for cap, al, _ in aligned
        if _indexed(cap) and al.found
    ]
    index = circular_mean(starts, revolution)
    streams = [e["index"] for e in entries if "index" in e]
    edges = [b for s in streams for b in s["bits"]]
    report = {
        "captures": len(starts),
        "stream_edges": len(edges),
        "drive_end": sorted({str(s["drive"]) for s in streams}),
        "pattern_angle": None,
    }
    if index is not None:
        report["pattern_angle"] = round(-index / revolution % 1, 4)
        report["start_offsets"] = _offsets(starts, index, revolution)
        report["stream_edge_offsets"] = _offsets(edges, index, revolution)
    return index, report


def _stream_edges(cap, al):
    """A stream's INDEX metadata as track positions, and how the stream ended."""
    edges = cap.index_bits()
    bits = al.track_position(edges).tolist() if al.found else []
    return {"edges": len(edges), "bits": bits} | cap.stream_status


def compare(truth, named, window=WINDOW):
    """Every capture aligned to the truth, digested against it (and the streams,
    for RAM captures); the index placed on the pattern (:func:`_index`), and
    through it every capture start and speed excursion on the disk's angle."""
    refs = [("truth", truth_capture(truth))]
    entries, weak, aligned = [], {}, []
    for name, cap in tqdm(named, desc="compare", unit="cap"):
        entry, al, byte_bit, reads = _check(truth, name, cap, refs)
        if entry["path"] == "stream":
            refs = refs + [(name, cap)]
            entry["index"] = _stream_edges(cap, al)
        entries.append(entry)
        mine = weak.setdefault(entry["path"], {})
        for g, r in reads.items():
            mine.setdefault(g, []).extend(r)
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
    index, index_report = _index(entries, aligned, revolution)
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
            "variant": truth.variant,
            "bits": len(truth.bits),
            "cells": truth.cells,
            "lead": truth.lead,
            "regions": layout(truth),
        },
        "revolution_bits": revolution,
        "index_bits": index,
        "index": index_report,
        "summary": summary,
        "captures": entries,
    }


def crosstalk(truth, name, cap):
    """A capture of any halftrack aligned to the truth: copies found, bit errors,
    slips and the fraction of covered exact bits read as written."""
    bits, _, begins = pt.capture_bits(cap)
    al = pt.align(bits, truth, period=truth.cells)
    out = {"name": name, "path": _path(cap), "found": al.found, "bits": len(bits)}
    if not al.found:
        return out | {"bit_errors": None, "slips": None, "match": None}
    groups = pt.region_report(truth, al, begins)["groups"].values()
    exact = [g for g in groups if g["kind"] != pt.UNSTABLE]
    covered = sum(g["bits"] for g in exact)
    errors = sum(g["errors"] for g in exact)
    return out | {
        "placements": al.placements.tolist(),
        "covered_bits": covered,
        "bit_errors": errors,
        "slips": sum(g["slips"] for g in exact),
        "match": round(1 - errors / covered, 6) if covered else None,
    }


def halftracks(nib, truth, args):
    """Captures of each requested halftrack the head reaches without bumping,
    each aligned to the truth (:func:`crosstalk`)."""
    reach = range(HOME_HALFTRACK, MAX_HALFTRACK + 1)
    wanted = sorted(set(args.halftracks))
    out = {
        "max_halftrack": reach[-1],
        "skipped": [h for h in wanted if h not in reach],
        "halftracks": [],
    }
    for h in tqdm([h for h in wanted if h in reach], desc="halftracks", unit="ht"):
        named = captures(nib, h, truth.density, args)
        rows = [crosstalk(truth, name, cap) for name, cap in named]
        out["halftracks"].append({"halftrack": h, "captures": rows})
    return out


def cells(nib, args):
    """Cells per revolution at each density on a scratch halftrack (each probe
    write destroys it); on a 1571 also the index period of a capture started
    at the index, and the cells it implies at that density."""
    archive = Archive(args.save)
    rows = []
    for density in tqdm(args.densities, desc=f"cells h{args.halftrack}", unit="d"):
        n = revolution_cells(nib, args.halftrack, archive, density)
        nominal = bits_per_revolution(density)
        row = {"density": density, "cells": n, "nominal": nominal}
        row["rpm"] = NOMINAL_RPM * nominal / n
        if nib.model == "1571":
            cap = nib.capture(args.halftrack, density, start="index", timing="none")
            job = TrackJob(0, args.halftrack // 2, args.halftrack // 2)
            archive("index", job, cap)
            period = cap.revolution_cycles
            row["index"] = {"status": cap.status, "period_us": period}
            if period is not None:
                row["index"]["cells"] = bit_rate(density) * period / CPU_HZ
        rows.append(row)
    return {"halftrack": args.halftrack, "densities": rows}


def _drive(args, cbm, truth):
    model = identify_model(cbm, args.dev)
    with (
        Monitor(cbm, args.dev, args.transport) as mon,
        Nibbler(mon, model, settle_ms=args.settle_ms) as nib,
    ):
        report = {"model": model, "located": locate(nib, args.max_steps)}
        report["streaming"] = nib.streaming
        if args.action == "write":
            report |= write(nib, truth, Archive(args.save), args.lead)
            report["truth"] = {"bits": len(truth.bits), "regions": layout(truth)}
        elif args.action == "verify":
            named = captures(nib, truth.halftrack, truth.density, args, index=True)
            report |= compare(truth, named, args.window)
        elif args.action == "cells":
            report |= cells(nib, args)
        else:
            report |= halftracks(nib, truth, args)
    return report


def _load_truth(path):
    return pt.Truth.from_json(json.loads(path.read_text()))


def execute(args, cbm):
    """Run the action and print its JSON report."""
    truth = None
    if args.action in ("compare", "halftracks"):
        truth = _load_truth(args.truth)
    elif args.action != "cells":
        truth = pt.make_truth(
            args.halftrack,
            args.density,
            args.seed,
            getattr(args, "cells", None),
            variant=args.region,
        )
    if args.action == "compare":
        named = [(str(p), Capture.load(p)) for p in args.captures]
        report = compare(truth, named, args.window)
    else:
        report = _drive(args, cbm, truth)
        if args.action in ("write", "verify"):
            _save_truth(truth, args.save)
    print(json.dumps(report))
    return report
