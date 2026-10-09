"""1581 disk operations on an :class:`r1581.Mfm1581`: D81 read and write, homing probe."""

import itertools
import pathlib

import numpy as np
from tqdm import tqdm

from .analysis import mfm
from .formats import d81
from .formats.mfmcap import save_captures
from .r1581 import MAX_CYL, ST_WP, TrackError

CYLINDERS = d81.TRACKS
SIDES = (0, 1)
ARCHIVE_NAME = "captures-1581.npz"
OK_MASK = mfm.ST_RNF | mfm.ST_CRC | mfm.ST_LOST


def _sides():
    return [(c, s) for c in range(CYLINDERS) for s in SIDES]


def _rows(image, cyl, side):
    """One side's ten 512-byte physical sectors and their error bytes."""
    rows = d81.side_rows(cyl, side)
    return image.data[rows].reshape(mfm.SECTORS, -1), image.errors[rows]


def dry(drive, headers=False):
    """Everything readable without stepping, and the homing plan (steps = estimate)."""
    report = drive.sense()
    if headers:
        drive.motor(True)
        report["period_us"] = drive.period_us
        report["id"] = drive.read_id()
    estimate, source = drive.estimate()
    return report | {"estimate": estimate, "source": source, "steps": estimate}


def home(drive, report, max_steps):
    """Restore within the dry run's steps and max_steps, then back to the estimate;
    ``homed`` and the drive's ``restore`` trace go in the report, also when
    TrackError is raised."""
    if report["steps"] > max_steps:
        raise ValueError(f"homing needs {report['steps']} steps, over {max_steps}")
    try:
        drive.home(report["steps"])
    except TrackError as e:
        report |= {"homed": False, "restore": e.trace, "error": str(e)}
        raise
    report |= {"homed": True, "restore": drive.home_trace}
    drive.seek(min(report["estimate"], MAX_CYL))
    return report


def _on_cylinder(drive, cyl):
    """TrackError unless an ID on the head names cyl (a disk with IDs)."""
    ident = drive.read_id()
    if ident is not None and ident["crc_ok"] and ident["c"] != cyl:
        raise TrackError(f"head reads cylinder {ident['c']}, expected {cyl}")


def _read_side(drive, cyl, retries):
    reads = drive.read_sectors(cyl, mfm.FIRST_SECTOR, mfm.SECTORS)
    if all(status & mfm.ST_RNF for *_, status in reads):
        _on_cylinder(drive, cyl)
    for _ in range(retries):
        bad = [i for i, (*_, st) in enumerate(reads) if st & OK_MASK]
        for i in bad:
            reads[i] = drive.read_sectors(cyl, mfm.FIRST_SECTOR + i, 1)[0]
    return reads


def read_disk(drive, retries=2, archive=None, progress=True):
    """D81 from Read Sector of every physical sector; ``archive`` (a directory) also
    gets a Read Track and an ID list of every side in ARCHIVE_NAME (``nybulah
    info``/``map`` read it)."""
    decodes, captures = {}, []
    for cyl, side in tqdm(_sides(), desc="read d81", unit="side", disable=not progress):
        drive.seek(cyl)
        drive.side(side)
        track = mfm.decode_reads(_read_side(drive, cyl, retries), side)
        decodes[(cyl, mfm.head_side(side))] = [track]
        if archive is not None and drive.streaming:
            captures += [drive.read_track(), drive.read_ids(2 * mfm.SECTORS)]
    if archive is not None and captures:
        archive = pathlib.Path(archive)
        archive.mkdir(parents=True, exist_ok=True)
        save_captures(archive / ARCHIVE_NAME, captures)
    return d81.from_decodes(decodes)


def _write_side(drive, cyl, side, image):
    data, errors = _rows(image, cyl, side)
    plan = mfm.plan_track(mfm.standard_layout(cyl, side, data, d81.pair_errors(errors)))
    status = drive.write_track(plan.image)
    if status & (ST_WP | mfm.ST_LOST):
        raise TrackError(f"write track {cyl}/{side}: status ${status:02X}")
    for first, rows, deleted in write_runs(plan.writes):
        status, written = drive.write_sectors(cyl, first, rows, deleted)
        if written != len(rows):
            raise TrackError(f"write sector {cyl}/{side}/{first}: status ${status:02X}")
    return plan


def _run_key(item):
    """Writes in one run share r minus their rank, the mark and the size."""
    rank, (_, r, data, deleted) = item
    return r - rank, deleted, len(data)


def write_runs(writes):
    """Consecutive (track_id, r, data, deleted) writes of one size and mark as
    (first r, rows, deleted)."""
    ordered = enumerate(sorted(writes, key=lambda w: w[1]))
    runs = [[w for _, w in g] for _, g in itertools.groupby(ordered, _run_key)]
    return [(g[0][1], np.array([w[2] for w in g]), g[0][3]) for g in runs]


def write_disk(drive, image, retries=2, progress=True):
    """Format every side with its sectors (Write Track, then Write Sector), verify by
    Read Sector; returns ``{"verified", "mismatched"}`` of (cylinder, side)."""
    if drive.sense()["write_protected"]:
        raise TrackError("the disk is write protected")
    bad = []
    for cyl, side in tqdm(
        _sides(), desc="write d81", unit="side", disable=not progress
    ):
        drive.seek(cyl)
        drive.side(side)
        plan = _write_side(drive, cyl, side, image)
        got = mfm.decode_reads(_read_side(drive, cyl, retries), side)
        want = mfm.decode_track(plan.data, plan.mark)
        if not _same(got, want):
            bad.append((cyl, side))
    return {"verified": len(_sides()) - len(bad), "mismatched": bad}


def _same(got, want):
    """Every sector read back as the plan left it: data and error class."""
    g = {int(r["r"]): i for i, r in enumerate(got.sectors)}
    for i, rec in enumerate(want.sectors):
        j = g.get(int(rec["r"]))
        if j is None or got.sectors[j]["error"] != rec["error"]:
            return False
        if rec["data_ok"] and not np.array_equal(got.payload(j), want.payload(i)):
            return False
    return True
