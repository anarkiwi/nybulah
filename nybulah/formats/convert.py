"""Conversions between NIB/NB2 captures, G64 and D64 images."""

import numpy as np
from tqdm import tqdm

from ..analysis.capture import framed_capture, segments
from ..analysis.cycle import TrackKind, extract_revolution, find_cycle
from ..analysis.gcr import speed_zone, to_bits, to_bytes
from ..analysis.sector import SectorError, decode_track, format_track
from .d64 import BAM_TRACK, D64, track_offsets
from .g64 import G64, G64Track
from .nib import Nib

MAX_D64_TRACK = 42


def revolution_bytes(bits, cycle):
    """One revolution as bytes, its final partial byte completed circularly.

    An unformatted capture has no revolution: its first ``cycle.length`` bits
    (the nominal length) are kept as they were read.
    """
    if cycle.kind == TrackKind.KILLER:
        return np.full(-(-cycle.length // 8), 0xFF, np.uint8)
    if cycle.kind == TrackKind.UNFORMATTED:
        stream = segments(bits).bits if hasattr(bits, "positions") else bits
        return to_bytes(np.asarray(stream, np.uint8)[: cycle.length])
    rev = extract_revolution(bits, cycle)
    return to_bytes(np.concatenate((rev, rev[: -len(rev) % 8])))


def _score(bits, cycle, halftrack):
    if cycle.kind != TrackKind.FORMATTED:
        return (cycle.kind, 0, -cycle.match)
    errors = 0
    if halftrack % 2 == 0 and halftrack // 2 <= MAX_D64_TRACK:
        decoded = decode_track(extract_revolution(bits, cycle), halftrack // 2)
        errors = int((decoded.errors != SectorError.OK).sum())
    return (cycle.kind, errors, -cycle.match)


def nib_to_g64(
    image: Nib, period=None, index_aligned=False, progress=True, unformatted=None
):
    """Trim each capture to one revolution and store it in a G64.

    NB2 entries use the best pass (fewest sector errors, then highest
    repetition) read at the header density. Every track is written: an
    unformatted one as its capture cut to the nominal length, and its
    halftrack appended to the ``unformatted`` list when one is given.
    """
    out = G64()
    for entry in tqdm(image.entries, desc="nib->g64", unit="trk", disable=not progress):
        passes = entry.data[None] if image.passes is None else entry.data[entry.zone]
        reads = [framed_capture(capture) for capture in passes]
        reads = [(b, find_cycle(b, entry.zone, period, index_aligned)) for b in reads]
        bits, cycle = min(reads, key=lambda r, h=entry.halftrack: _score(*r, h))
        if cycle.kind == TrackKind.UNFORMATTED and unformatted is not None:
            unformatted.append(entry.halftrack)
        out.tracks[entry.halftrack] = G64Track(
            revolution_bytes(bits, cycle), entry.zone
        )
    return out


def _decode(image, track, disk_id):
    entry = image.tracks.get(2 * track)
    if entry is None:
        return None
    return decode_track(to_bits(entry.data), track, disk_id)


def g64_to_d64(image: G64, tracks=None, progress=True):
    """Decode the sectors of a G64 into a D64 with error info.

    ``tracks`` (35/40/42) defaults to 40 when a sector beyond track 35 reads.
    """
    bam = _decode(image, BAM_TRACK, None)
    disk_id = None
    if bam is not None and bam.errors[0] == SectorError.OK:
        disk_id = bytes(bam.ids[0])
    decoded = {}
    for track in tqdm(
        range(1, MAX_D64_TRACK + 1), desc="g64->d64", unit="trk", disable=not progress
    ):
        decoded[track] = _decode(image, track, disk_id)
    if tracks is None:
        extra = [d for t, d in decoded.items() if 35 < t <= 40 and d is not None]
        tracks = 40 if any((d.errors == SectorError.OK).any() for d in extra) else 35
    offsets = track_offsets(tracks)
    data = np.zeros((offsets[-1], 256), np.uint8)
    errors = np.full(offsets[-1], SectorError.NO_SYNC, np.uint8)
    for track in range(1, tracks + 1):
        if decoded[track] is not None:
            span = slice(offsets[track - 1], offsets[track])
            data[span] = decoded[track].data
            errors[span] = decoded[track].errors
    return D64(data, errors)


def d64_to_g64(image: D64, progress=True):
    """Format every D64 track as standard GCR, reproducing its error info."""
    out = G64()
    disk_id = image.disk_id
    for track in tqdm(
        range(1, image.tracks + 1), desc="d64->g64", unit="trk", disable=not progress
    ):
        span = image.span(track)
        gcr = format_track(track, image.data[span], disk_id, image.errors[span])
        out.tracks[2 * track] = G64Track(gcr, speed_zone(track))
    return out
