"""D64/D71 imaging through a Nibbler: host-decoded captures, encoded and verified writes."""

import dataclasses
import pathlib
from collections import Counter

import numpy as np
from tqdm import tqdm

from .analysis.cycle import MEASURED_TOLERANCE
from .analysis.gcr import bit_rate, sectors_per_track, speed_zone
from .analysis.sector import (
    GAP_BYTE,
    SectorError,
    decode_track,
    format_track,
    header_tracks,
    merge_decodes,
)
from .formats.d64 import BAM_TRACK, D64
from .formats.d71 import D71, SIDE_TRACKS
from .nibbler import NPAGES, TrackError

SIDE1_TRACK_BASE = SIDE_TRACKS
PROBE_SYNC = 5


@dataclasses.dataclass(frozen=True)
class TrackJob:
    """One physical track: head side, physical track, header track number."""

    side: int
    track: int
    header: int

    @property
    def halftrack(self):
        """Head position."""
        return 2 * self.track

    @property
    def sectors(self):
        """DOS sectors on the track (by physical zone)."""
        return sectors_per_track(self.track)


def d64_jobs(tracks):
    """Tracks of a single-sided image."""
    return [TrackJob(0, t, t) for t in range(1, tracks + 1)]


def d71_jobs():
    """Tracks of a D71: side 1 headers carry track numbers 36-70."""
    return d64_jobs(SIDE_TRACKS) + [
        TrackJob(1, t, t + SIDE1_TRACK_BASE) for t in range(1, SIDE_TRACKS + 1)
    ]


class Archive:
    """Saves every capture as an .npz record when given a directory."""

    def __init__(self, root=None):
        self.root = None if root is None else pathlib.Path(root)
        self.count = Counter()
        if self.root is not None:
            self.root.mkdir(parents=True, exist_ok=True)

    def __call__(self, kind, job, cap):
        if self.root is not None:
            key = (kind, job.side, job.track)
            name = f"{kind}-s{job.side}-t{job.track:02d}-{self.count[key]}.npz"
            self.count[key] += 1
            cap.save(self.root / name)
        return cap


def _read_job(nib, job, disk_id, retries, archive, kind="read"):
    best = None
    for _ in range(retries + 1):
        cap = archive(kind, job, nib.scan(job.halftrack, side=job.side))
        dec = decode_track(cap.bits(), job.header, disk_id, job.sectors)
        best = dec if best is None else merge_decodes(best, dec)
        if (best.errors == SectorError.OK).all():
            break
    return best


def calibrate(nib, archive):
    """Relabel the head position from the headers found where track 18 should be."""
    job = TrackJob(0, BAM_TRACK, BAM_TRACK)
    cap = archive("locate", job, nib.scan(job.halftrack))
    found = header_tracks(cap.bits())
    if len(found):
        nib.halftrack = 2 * int(np.bincount(found).argmax())
    return len(found)


def format_id(nib, retries, archive):
    """Header disk ID of track 18 (BAM order), or None if unreadable."""
    job = TrackJob(0, BAM_TRACK, BAM_TRACK)
    dec = _read_job(nib, job, None, retries, archive)
    ok = dec.errors == SectorError.OK
    if not ok.any():
        return None
    ids, counts = np.unique(dec.ids[ok], axis=0, return_counts=True)
    return bytes(ids[counts.argmax()])


def read_jobs(nib, jobs, retries=2, archive=None, progress=True):
    """Decode every job's sectors; returns (data, errors) stacked in job order."""
    archive = archive if isinstance(archive, Archive) else Archive(archive)
    calibrate(nib, archive)
    disk_id = format_id(nib, retries, archive)
    data, errors = [], []
    for job in tqdm(jobs, desc="read", unit="trk", disable=not progress):
        dec = _read_job(nib, job, disk_id, retries, archive)
        data.append(dec.data)
        errors.append(dec.errors)
    return np.concatenate(data), np.concatenate(errors)


def read_d64(nib, tracks=35, **kw):
    """Read a 35 or 40 track single-sided disk into a D64 with error bytes."""
    return D64(*read_jobs(nib, d64_jobs(tracks), **kw))


def read_d71(nib, **kw):
    """Read a double-sided 1571 disk into a D71 with error bytes."""
    if nib.model != "1571":
        raise ValueError("D71 needs a 1571")
    return D71(*read_jobs(nib, d71_jobs(), **kw))


def revolution_cells(nib, halftrack, archive=None):
    """Bit cells per revolution this drive writes at density 0 (destroys the track).

    Writes filler ending in a single sync over more than one revolution, then
    measures the distance from that sync to its next pass.
    """
    archive = archive if isinstance(archive, Archive) else Archive(archive)
    stream = bytes([GAP_BYTE]) * (NPAGES * 256 - PROBE_SYNC) + b"\xff" * PROBE_SYNC
    nib.write_track(halftrack, stream, density=0)
    job = TrackJob(0, halftrack // 2, halftrack // 2)
    cap = archive("probe", job, nib.capture(halftrack, density=0, start="sync"))
    if not cap.positions.size:
        raise TrackError("probe sync not found")
    return 8 * int(cap.positions[0]) + int(cap.hidden[0])


def track_stream(job, data, errors, disk_id, cells0):
    """Formatted track for a drive writing cells0 cells per revolution at density 0.

    Filler goes first so the stream covers a fast revolution in whole pages.
    """
    zone = speed_zone(job.track)
    cells = cells0 * bit_rate(zone) / bit_rate(0)
    capacity = int(cells * (1 - MEASURED_TOLERANCE) // 8)
    gcr = format_track(job.header, data, disk_id, errors, capacity)
    cover = int(np.ceil(cells * (1 + MEASURED_TOLERANCE) / 8))
    total = -(-max(cover, len(gcr)) // 256) * 256
    if total > NPAGES * 256:
        raise TrackError(f"track {job.track} needs {total} bytes of drive RAM")
    return bytes([GAP_BYTE]) * (total - len(gcr)) + gcr.tobytes()


REPRODUCED = [
    int(e) for e in SectorError if e.dos_code in (0, 20, 21, 22, 23, 24, 27, 29)
]


def written_errors(errors):
    """Error bytes a written track reads back with: what format_track reproduces."""
    errors = np.asarray(errors, np.uint8)
    if (errors == SectorError.NO_SYNC).any():
        return np.full_like(errors, SectorError.NO_SYNC)
    return np.where(np.isin(errors, REPRODUCED), errors, SectorError.OK).astype(
        np.uint8
    )


def _verified(dec, data, errors):
    expect = written_errors(errors)
    ok = expect == SectorError.OK
    same = (dec.errors == expect).all()
    return bool(same and (dec.data[ok] == np.asarray(data)[ok]).all())


def _write_job(nib, job, track, cells0, retries, archive):
    """Attempts until verify passed, or None."""
    data, errors, disk_id = track
    stream = track_stream(job, data, errors, disk_id, cells0)
    for attempt in range(1, retries + 2):
        nib.write_track(job.halftrack, stream, side=job.side)
        if _verified(_read_job(nib, job, disk_id, 0, archive, "verify"), data, errors):
            return attempt
    return None


def write_jobs(
    nib, jobs, data, errors, disk_id, retries=2, archive=None, progress=True
):
    """Format, write and verify each job; returns per-track attempt records."""
    archive = archive if isinstance(archive, Archive) else Archive(archive)
    cells0 = revolution_cells(nib, jobs[0].halftrack, archive)
    offsets = np.cumsum([0] + [j.sectors for j in jobs])
    report = []
    for i, job in enumerate(tqdm(jobs, desc="write", unit="trk", disable=not progress)):
        rows = slice(offsets[i], offsets[i + 1])
        track = (data[rows], errors[rows], disk_id)
        report.append(
            {
                "side": job.side,
                "track": job.track,
                "attempts": _write_job(nib, job, track, cells0, retries, archive),
            }
        )
    return {"rpm": 60.0 * bit_rate(0) / cells0, "tracks": report}


def write_d64(nib, image, **kw):
    """Write a D64 (35 or 40 tracks) with its error bytes reproduced."""
    if image.tracks > 40:
        raise ValueError("at most 40 tracks can be written")
    jobs = d64_jobs(image.tracks)
    return write_jobs(nib, jobs, image.data, image.errors, image.disk_id, **kw)


def write_d71(nib, image, **kw):
    """Write a D71 to a 1571."""
    if nib.model != "1571":
        raise ValueError("D71 needs a 1571")
    return write_jobs(nib, d71_jobs(), image.data, image.errors, image.disk_id, **kw)


def failed(report):
    """Tracks whose verify never passed."""
    return [t for t in report["tracks"] if t["attempts"] is None]


SURVEY_TRACKS = (1, 18, 25, 31)


def survey(nib, tracks=SURVEY_TRACKS):
    """Read-only check: one capture per density zone, plus the index period on a 1571.

    The head is located and calibrated first, as for a disk read.
    """
    nib.locate()
    calibrate(nib, Archive())
    out = []
    for track in tracks:
        cap = nib.capture(2 * track, start="now")
        dec = decode_track(cap.bits(), track)
        runs = cap.sync_bits
        out.append(
            {
                "track": track,
                "zone": cap.density,
                "status": cap.status,
                "bytes": len(cap.data),
                "syncs": len(runs),
                "lost": cap.lost,
                "sync_bits": (
                    [int(f(runs)) for f in (np.min, np.median, np.max)]
                    if len(runs)
                    else None
                ),
                "byte_cycles": cap.byte_cycles,
                "overrun_risk": cap.overrun_risk,
                "sectors_ok": int((dec.errors == SectorError.OK).sum()),
            }
        )
    rpm = None
    if nib.model == "1571":
        rpm = nib.capture(2 * BAM_TRACK, start="index").rpm
    return {"zones": out, "rpm": rpm}
