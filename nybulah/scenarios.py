"""Scenario classification and aggregate statistics of a corpus survey.

Thresholds are not tuned by hand. Each one is a quantile (``OUTLIER`` or
``1 - OUTLIER``) of the same feature on clean DOS tracks: every standard
sector of the track reads OK, at the standard density, once per revolution.
"""

import numpy as np

from .analysis.cycle import TrackKind, lag_window
from .analysis.gcr import SECTORS_PER_ZONE, bits_per_revolution
from .analysis.regions import Block
from .analysis.sector import GAP_BYTE, SectorError
from .formats.image import SIDE1_TRACK_BASE
from .formats.nib import BM_FF_TRACK, BM_MATCH, BM_NO_CYCLE, BM_NO_SYNC

OUTLIER = 1e-3
MAJORITY = 0.5
QUANTILES = (OUTLIER, 0.01, 0.5, 0.99, 1 - OUTLIER)
LINEAR_KINDS = ("nib", "nbz", "nb2")
DOS_TRACKS = 35
MAX_TRACK = 42
BYTE_BITS = 8
_CYCLIC = np.unpackbits(np.arange(256, dtype=np.uint8)[:, None], axis=1)
ILLEGAL_FILL = np.zeros(257, bool)  # index -1 (no gap bytes) is False
ILLEGAL_FILL[:256] = (
    np.lib.stride_tricks.sliding_window_view(np.hstack((_CYCLIC, _CYCLIC[:, :2])), 3, 1)
    .sum(axis=2)
    .min(axis=1)
    == 0
)
NIB_FLAGS = (BM_MATCH, BM_NO_CYCLE, BM_NO_SYNC, BM_FF_TRACK)
FEATURES = ("n_sync", "sync_max", "ratio", "bad_span", "n_hdr", "cycle_z")


def _q(values):
    values = np.asarray(values, float)
    values = values[np.isfinite(values)]
    if len(values) == 0:
        return None
    return [round(float(v), 4) for v in np.quantile(values, QUANTILES)]


def _hi(values):
    values = np.asarray(values, float)
    values = values[np.isfinite(values)]
    return float(np.quantile(values, 1 - OUTLIER)) if len(values) else np.inf


def _lo(values):
    values = np.asarray(values, float)
    values = values[np.isfinite(values)]
    return float(np.quantile(values, OUTLIER)) if len(values) else -np.inf


def distinct_images(survey):
    """Mask of loaded images that are the first with their content fingerprint."""
    ok = survey["image_error"] == ""
    _, first = np.unique(np.where(ok, survey["image_sha1"], ""), return_index=True)
    keep = np.zeros(len(ok), bool)
    keep[first] = True
    return keep & ok


def derive(tracks, fmt):
    """Per-track derived columns; ``fmt`` is the source format of each track."""
    t = tracks
    half = t["halftrack"].astype(np.int64)
    track = half // 2 + SIDE1_TRACK_BASE * t["side"]
    whole = half % 2 == 0
    std_n = np.where(
        whole & (half // 2 <= MAX_TRACK), np.array(SECTORS_PER_ZONE)[t["std_zone"]], 0
    )
    linear = np.isin(fmt, LINEAR_KINDS)
    formatted = t["kind"] == TrackKind.FORMATTED
    length = np.where(
        linear,
        np.where(t["hdr_period"] > 0, t["hdr_period"], np.nan),
        np.where(t["kind"] == TrackKind.KILLER, np.nan, t["rev_len"]),
    )
    n_ok = t["errors_cap"][:, SectorError.OK]
    nominal = np.array([bits_per_revolution(z) for z in range(4)])
    period_zone = np.argmin(
        np.abs(np.log(np.maximum(t["hdr_period"], 1)[:, None] / nominal)), axis=1
    )
    dos = whole & (t["n_hdr"] > 0) & (t["hdr_track"] == track)
    clean = (
        dos
        & formatted
        & (half // 2 <= DOS_TRACKS)
        & (t["zone"] == t["std_zone"])
        & (n_ok == std_n)
        & (t["n_sect"] == std_n)
        & (t["n_dupe"] == 0)
    )
    return {
        "track": track,
        "whole": whole,
        "std_n": std_n,
        "linear": linear,
        "formatted": formatted,
        "ratio": length / t["nominal"],
        "n_ok": n_ok,
        "dos": dos,
        "clean": clean,
        "period_zone": np.where(t["hdr_period"] > 0, period_zone, -1),
    }


def neighbour(tracks, offset, column):
    """``column`` of the track ``offset`` halftracks away on the same disk (nan if absent)."""
    ident = tracks["image"].astype(np.int64) * 512 + tracks["key"]
    order = np.argsort(ident, kind="stable")
    pos = np.searchsorted(ident[order], ident + offset)
    pos = np.minimum(pos, len(ident) - 1)
    found = ident[order][pos] == ident + offset
    return np.where(found, tracks[column][order][pos].astype(float), np.nan)


def thresholds(tracks, d, spans):
    """Outlier bounds of each feature on clean DOS tracks, per capture family.

    ``spans`` holds the survey's per-sync and (optionally) per-gap columns.
    """
    out = {}
    for family, sel in (("linear", d["linear"]), ("circular", ~d["linear"])):
        ref = d["clean"] & sel
        lengths = spans["sync_len"][ref[spans["sync_row"]]]
        out[family] = {
            "sync_short": _lo(lengths),
            "sync_long": _hi(lengths),
            "ratio_short": _lo(d["ratio"][ref]),
            "ratio_long": _hi(d["ratio"][ref]),
            "bad_span": _hi(tracks["bad_span"][ref]),
            "similar": _hi(tracks["sim_next"][ref & neighbour_clean(tracks, d)]),
            "multipass": _hi(tracks["mp_disagree"][ref]),
        }
        if "gap_row" in spans:
            out[family] |= gap_thresholds(spans, ref[spans["gap_row"]])
    return out


def gap_thresholds(spans, clean):
    """Gap length bounds after headers and data blocks, the least share of a gap
    in its dominant fill class, and the fill classes that dominate clean gaps."""
    after, length = spans["gap_after"][clean], spans["gap_len"][clean]
    count = length // BYTE_BITS
    top, hits = spans["gap_top"][clean][count > 0], spans["gap_hits"][clean][count > 0]
    freq = np.bincount(top, minlength=256) / max(len(top), 1)
    out = {"gap_share": _lo(hits / count[count > 0])}
    for name, block in (("header", Block.HEADER), ("data", Block.DATA)):
        out[f"{name}_gap_short"] = _lo(length[after == block])
        out[f"{name}_gap_long"] = _hi(length[after == block])
    return out | {"fill_classes": np.flatnonzero(freq >= OUTLIER).tolist()}


def neighbour_clean(tracks, d):
    """Clean tracks whose next whole track is clean too."""
    nxt = neighbour(
        {"image": tracks["image"], "key": tracks["key"], "clean": d["clean"]},
        2,
        "clean",
    )
    return nxt == 1


def cycle_stats(tracks, mask):
    """How ``find_cycle`` classified and measured linear captures in ``mask``."""
    t = tracks[mask]
    if len(t) == 0:
        return None
    kinds = np.bincount(t["kind"], minlength=3) / len(t)
    period = t["hdr_period"] > 0
    out = {
        "tracks": int(len(t)),
        "formatted": round(float(kinds[TrackKind.FORMATTED]), 4),
        "killer": round(float(kinds[TrackKind.KILLER]), 4),
        "unformatted": round(float(kinds[TrackKind.UNFORMATTED]), 4),
        "with_period": round(float(period.mean()), 4),
        "z_median": _q(t["cycle_z"][t["kind"] == TrackKind.FORMATTED]),
    }
    windows = np.array([lag_window(z) for z in range(4)])
    p = t[period]
    if len(p):
        lo, hi = windows[p["zone"]].T
        out["period_in_window"] = round(
            float(((p["hdr_period"] >= lo) & (p["hdr_period"] <= hi)).mean()), 4
        )
        out["unformatted_with_period"] = round(
            float((p["kind"] == TrackKind.UNFORMATTED).mean()), 4
        )
    f = p[p["kind"] == TrackKind.FORMATTED]
    if len(f):
        err = f["cycle_len"].astype(np.int64) - f["hdr_period"]
        spacing = f["hdr_period"] / np.maximum(f["n_sect_cap"], 1)
        k = np.rint(err / spacing).astype(np.int64)
        exact = np.abs(err) <= BYTE_BITS
        out["vs_period"] = {
            "exact": round(float(exact.mean()), 4),
            "sector_short": round(float(((k == -1) & ~exact).mean()), 4),
            "sector_long": round(float(((k == 1) & ~exact).mean()), 4),
            "other": round(float(((np.abs(k) > 1) | ((k == 0) & ~exact)).mean()), 4),
            "error_bits": _q(err),
        }
    return out


def prevalence(tracks, mask, disks, fmt):
    """Tracks and distinct disks in ``mask``, by format."""
    by_fmt = {}
    for name in np.unique(fmt):
        sel = mask & (fmt == name)
        by_fmt[str(name)] = [int(sel.sum()), int(len(np.unique(tracks["image"][sel])))]
    return {
        "tracks": int(mask.sum()),
        "disks": int(len(np.unique(tracks["image"][mask]))),
        "disk_share": round(len(np.unique(tracks["image"][mask])) / max(disks, 1), 4),
        "by_format": by_fmt,
    }


def scenarios(tracks, d, thr):
    """Boolean track masks per scenario (they overlap)."""
    t = tracks
    fam = {
        k: np.where(d["linear"], thr["linear"][k], thr["circular"][k])
        for k, v in thr["linear"].items()
        if np.isscalar(v)
    }
    whole, formatted, dos = d["whole"], d["formatted"], d["dos"]
    upper = (t["halftrack"] // 2 > DOS_TRACKS) & (t["side"] == 0)
    crosstalk = np.fmax(neighbour(t, -1, "sim_half"), t["sim_half"]) > fam["similar"]
    present = t["kind"] != TrackKind.UNFORMATTED
    noise = (t["kind"] == TrackKind.UNFORMATTED) & (t["n_hdr"] == 0)
    fill = present & ILLEGAL_FILL[t["gap_top"]] & (t["gap_top_frac"] > MAJORITY)
    lower = upper & (t["n_hdr"] > 0) & (t["hdr_track"] < d["track"])
    own = formatted & ~fill
    return {
        "standard_dos": d["clean"],
        "dos_with_errors": dos & ~upper & (d["n_ok"] < d["std_n"]),
        "extended_36_42": whole & upper & own & ~lower,
        "extended_dos_headers": whole & upper & dos,
        "extended_lower_copy": whole & lower,
        "half_track_data": ~whole & own & ~crosstalk,
        "half_track_crosstalk": ~whole & formatted & crosstalk,
        "fat_track": whole
        & own
        & (t["sim_next"] > fam["similar"])
        & (t["halftrack"] < 2 * DOS_TRACKS),
        "killer": t["kind"] == TrackKind.KILLER,
        "unformatted_1_35": ~upper & noise,
        "unformatted_36_42": upper & noise,
        "no_flux_fill": fill,
        "no_sync": own & (t["n_sync"] == 0),
        "long_sync": present & (t["sync_max"] > fam["sync_long"]),
        "short_sync": present & (t["n_sync"] > 0) & (t["sync_min"] < fam["sync_short"]),
        "extra_sectors": dos & (t["n_extra"] > 0),
        "custom_sectors": own & (t["n_sync"] > 0) & (t["n_hdr"] == 0),
        "nonstandard_density": whole & own & (t["zone"] != t["std_zone"]),
        "density_label_mismatch": (d["period_zone"] >= 0)
        & (d["period_zone"] != t["zone"]),
        "mixed_density": t["zones"] > 1,
        "long_track": own & (d["ratio"] > fam["ratio_long"]),
        "short_track": own & (d["ratio"] < fam["ratio_short"]),
        "weak_multipass": t["mp_disagree"] > fam["multipass"],
        "illegal_gcr": own & (t["bad_span"] > fam["bad_span"]),
        "duplicate_headers": t["n_dupe"] > 0,
        "id_mismatch": t["id_mis"] > 0,
        "header_track_mismatch": whole
        & ~upper
        & (t["n_hdr"] > 0)
        & (t["hdr_track"] != d["track"]),
        "nonstandard_data_mark": t["n_data_nonstd"] > 0,
        "nonstandard_gap_fill": dos & (t["gap_top"] != GAP_BYTE),
    }


def _features(tracks, d, mask):
    cols = {k: tracks[k] for k in FEATURES if k != "ratio"} | {"ratio": d["ratio"]}
    return {k: _q(v[mask]) for k, v in cols.items()}


def _errors(tracks, mask, column="errors_cap"):
    total = tracks[column][mask].sum(axis=0)
    return {
        str(SectorError(c).dos_code): int(total[c]) for c in SectorError if total[c]
    }


def _gcr_blocks(tracks, mask):
    data = max(int(tracks["n_data"][mask].sum()), 1)
    return {
        "data_blocks": data,
        "invalid_payload": round(int(tracks["n_gcr_payload"][mask].sum()) / data, 4),
        "invalid_tail_only": round(int(tracks["n_gcr_tail"][mask].sum()) / data, 4),
    }


def reference_summary(rows, lengths):
    """Statistics of a reference disk's captures (all tracks treated as linear)."""
    fmt = np.full(len(rows), "nib")
    d = derive(rows, fmt)
    zones = {}
    for zone in np.unique(rows["zone"]):
        sel = rows["zone"] == zone
        timed = sel & (rows["hdr_period"] > 0)
        zones[int(zone)] = {
            "tracks": int(sel.sum()),
            "rpm": _q(300.0 * rows["nominal"][timed] / rows["hdr_period"][timed]),
            "cycle": cycle_stats(rows, sel),
        }
    return {
        "tracks": int(len(rows)),
        "zones": zones,
        "sync_bits": _q(lengths),
        "sectors_in_capture": int(rows["n_sect_cap"].sum()),
        "standard_sectors": int(d["std_n"].sum()),
        "errors_one_revolution": _errors(rows, np.ones(len(rows), bool), "errors"),
        "errors_capture": _errors(rows, np.ones(len(rows), bool)),
        "gcr": _gcr_blocks(rows, np.ones(len(rows), bool)),
    }


def _counts(values):
    return {str(k): int(v) for k, v in zip(*np.unique(values, return_counts=True))}


def _restrict(survey, keep):
    """Tracks of kept images, and their span columns with re-indexed rows."""
    tracks = survey["tracks"]
    kept = keep[tracks["image"]]
    row_map = np.cumsum(kept) - 1
    spans = {}
    for prefix in ("sync", "gap"):
        if f"{prefix}_row" not in survey:
            continue
        rows = survey[f"{prefix}_row"]
        sel = kept[rows]
        for name in survey:
            if name.startswith(f"{prefix}_"):
                spans[name] = survey[name][sel]
        spans[f"{prefix}_row"] = row_map[rows[sel]]
    return tracks[kept], spans


def _by_family(d, fn):
    return {
        fam: fn(sel)
        for fam, sel in (("linear", d["linear"]), ("circular", ~d["linear"]))
    }


def _images(survey, keep):
    errors = survey["image_error"][survey["image_error"] != ""]
    return {
        "listed": int(len(keep)),
        "failed": int(len(errors)),
        "failure_kinds": _counts([e.split(":")[0] for e in errors]),
        "distinct": int(keep.sum()),
        "by_format": _counts(survey["image_fmt"][keep]),
    }


def summarise(survey, reference=None):
    """Aggregate statistics of :func:`nybulah.survey.load_survey` output."""
    if "image_key" not in survey:
        return {"images": 0}
    keep = distinct_images(survey)
    tracks, spans = _restrict(survey, keep)
    sync_len, sync_row = spans["sync_len"], spans["sync_row"]
    fmt = survey["image_fmt"][tracks["image"]]
    d = derive(tracks, fmt)
    thr = thresholds(tracks, d, spans)
    clean = d["clean"]
    out = {
        "images": _images(survey, keep) | {"tracks": int(len(tracks))},
        "thresholds": thr,
        "sync_bits": _by_family(
            d,
            lambda sel: {
                "all": _q(sync_len[sel[sync_row]]),
                "clean": _q(sync_len[(sel & clean)[sync_row]]),
            },
        ),
        "ratio_clean": _by_family(d, lambda sel: _q(d["ratio"][sel & clean])),
        "disk_ids": _disk_ids(survey, keep),
        "pairs": _pairs(tracks, d),
        "gcr_dos_tracks": _by_family(
            d, lambda sel: _gcr_blocks(tracks, d["dos"] & sel)
        ),
        "cycle_all_linear": cycle_stats(tracks, d["linear"]),
        "errors_dos_tracks": _by_family(
            d,
            lambda sel: {
                "one_revolution": _errors(tracks, d["dos"] & sel, "errors"),
                "capture": _errors(tracks, d["dos"] & sel),
            },
        ),
        "nib_flags": {
            str(flag): int(((tracks["density"] & flag) > 0)[d["linear"]].sum())
            for flag in NIB_FLAGS
        },
        "scenarios": {},
    }
    for name, mask in scenarios(tracks, d, thr).items():
        out["scenarios"][name] = {
            "prevalence": prevalence(tracks, mask, int(keep.sum()), fmt),
            "features": _features(tracks, d, mask),
            "cycle": cycle_stats(tracks, mask & d["linear"]),
            "errors": _errors(tracks, mask),
        }
    if reference is not None:
        out["reference"] = reference_summary(*reference)
    return out


def _disk_ids(survey, keep):
    fmt_id, bam_id = survey["image_disk_id"][keep], survey["image_cosmetic_id"][keep]
    read = fmt_id >= 0
    return {
        "track18_read": int(read.sum()),
        "bam_id_differs": int((read & (fmt_id != bam_id)).sum()),
    }


def _pairs(tracks, d):
    sel = np.isfinite(tracks["pair_agree"])
    if not sel.any():
        return None
    length = np.where(d["linear"], tracks["hdr_period"], np.nan)
    diff = tracks["pair_len"] - length
    return {
        "tracks": int(sel.sum()),
        "disks": int(len(np.unique(tracks["image"][sel]))),
        "agreement": _q(tracks["pair_agree"][sel]),
        "length_minus_period": _q(diff[sel & (tracks["hdr_period"] > 0)]),
    }
