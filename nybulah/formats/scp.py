"""SuperCard Pro (.scp) flux images, and the flux track model shared with KryoFlux."""

from dataclasses import dataclass, field

import numpy as np

from ..analysis.flux import ROTATION_TICKS
from ..analysis.gcr import CLOCK_HZ

SCP_SIGNATURE = b"SCP"
SCP_TRACKS = 168
SCP_BASE_HZ = 40_000_000
FLAG_INDEXED = 0x01
FLAG_TPI96 = 0x02
_TLUT = 0x10
_DATA = _TLUT + 4 * SCP_TRACKS
_WRSP = b"WRSP"


@dataclass
class FluxTrack:
    """Transition intervals and index instants in sample-clock units.

    ``index`` holds sample times measured from the start of the first
    interval; ``splice`` is an optional write-splice time.
    """

    intervals: np.ndarray
    index: np.ndarray
    sample_hz: float
    splice: float = None

    def ticks(self, normalize=True):
        """``(transition times, index times)`` in 16 MHz clocks.

        With ``normalize`` and two or more index pulses, time is scaled so
        the mean revolution lasts exactly 300 rpm.
        """
        scale = CLOCK_HZ / self.sample_hz
        if normalize and len(self.index) >= 2:
            scale = ROTATION_TICKS / np.diff(self.index).mean()
        times = np.cumsum(self.intervals, dtype=np.float64)
        return times * scale, np.asarray(self.index, np.float64) * scale

    def revolutions(self):
        """Interval arrays split at each index pulse (whole intervals only)."""
        edges = np.searchsorted(np.cumsum(self.intervals), self.index, side="right")
        return [self.intervals[a:b] for a, b in zip(edges[:-1], edges[1:])]


@dataclass
class SCP:
    """Flux tracks keyed by SCP track number, plus header fields."""

    tracks: dict = field(default_factory=dict)
    disk_type: int = 0
    flags: int = FLAG_INDEXED
    heads: int = 0

    @property
    def tpi96(self):
        """The capturing drive stepped at 96 tpi."""
        return bool(self.flags & FLAG_TPI96)


def _flux_values(raw, width):
    """Interval values; a zero word carries 2**width into the next interval."""
    dtype = ">u2" if width == 16 else np.uint8
    words = np.frombuffer(raw, dtype).astype(np.int64)
    nonzero = np.flatnonzero(words)
    carries = np.diff(np.concatenate(([-1], nonzero))) - 1
    return words[nonzero] + (carries << width)


def _splices(buf, first_track):
    """Write splice positions from a WRSP extension chunk, if any."""
    pos, end = _DATA + 8, _DATA + 8
    if buf[_DATA : _DATA + 4] == b"EXTS":
        end = min(
            first_track, pos + int.from_bytes(buf[_DATA + 4 : _DATA + 8], "little")
        )
    while pos + 8 <= end:
        tag, size = buf[pos : pos + 4], int.from_bytes(buf[pos + 4 : pos + 8], "little")
        if tag == _WRSP and size >= 4 * (SCP_TRACKS + 1):
            return np.frombuffer(buf, "<u4", SCP_TRACKS, pos + 12)
        pos += 8 + size
    return None


def _read_track(buf, base, revs, width, indexed):
    """``(intervals, index times)`` of the track data header at ``base``, or None."""
    if buf[base : base + 3] != b"TRK" or base + 4 + 12 * revs > len(buf):
        raise ValueError(f"SCP track header at {base:#x} is corrupt")
    table = np.frombuffer(buf, "<u4", 3 * revs, base + 4).reshape(revs, 3)
    table = table[table[:, 1] > 0].astype(np.int64)
    if len(table) == 0:
        return None
    start = base + int(table[0, 2])
    end = base + int(table[-1, 2] + table[-1, 1] * width // 8)
    durations = np.cumsum(table[:, 0])
    index = np.concatenate(([0], durations)) if indexed else durations
    return _flux_values(buf[start:end], width), index.astype(np.float64)


def read_scp(buf):
    """Parse an SCP image; every revolution in a track becomes one index span."""
    buf = bytes(buf)
    if buf[:3] != SCP_SIGNATURE or len(buf) < _DATA:
        raise ValueError("not an SCP image")
    width = buf[9] or 16
    if width not in (8, 16):
        raise ValueError(f"SCP bit cell width {width} is not supported")
    sample_hz = SCP_BASE_HZ / (buf[11] + 1)
    offsets = np.frombuffer(buf, "<u4", SCP_TRACKS, _TLUT)
    used = offsets[(offsets >= _DATA) & (offsets < len(buf))]
    splices = _splices(buf, int(used.min()) if len(used) else len(buf))
    image = SCP({}, buf[4], buf[8], buf[10])
    for tnr in np.flatnonzero(offsets):
        found = _read_track(
            buf, int(offsets[tnr]), buf[5], width, buf[8] & FLAG_INDEXED
        )
        if found is not None:
            splice = (
                None if splices is None or not splices[tnr] else float(splices[tnr])
            )
            image.tracks[int(tnr)] = FluxTrack(*found, sample_hz, splice)
    return image


def _encode_flux(intervals):
    """16-bit SCP words: overflow zeros, then a non-zero remainder."""
    intervals = np.maximum(np.asarray(intervals, np.int64), 1)
    zeros = intervals >> 16
    rest = np.maximum(intervals & 0xFFFF, 1)
    counts = zeros + 1
    words = np.zeros(int(counts.sum()), np.int64)
    words[np.cumsum(counts) - 1] = rest
    return words.astype(">u2").tobytes()


def _track_block(tnr, track, revs):
    """TRK header and data of the first ``revs`` revolutions at 40 MHz."""
    scale = SCP_BASE_HZ / track.sample_hz
    data = [_encode_flux(np.rint(r * scale)) for r in track.revolutions()[:revs]]
    sizes = np.array([len(d) for d in data], np.int64)
    durations = np.rint(np.diff(track.index)[:revs] * scale)
    starts = 4 + 12 * revs + np.cumsum(sizes) - sizes
    table = np.stack((durations, sizes // 2, starts), axis=1).astype("<u4")
    return b"TRK" + bytes([tnr]) + table.tobytes() + b"".join(data)


def _extension(tracks):
    """EXTS area holding a WRSP chunk when any track has a write splice."""
    splices = np.zeros(SCP_TRACKS + 1, "<u4")
    for tnr, track in tracks.items():
        if track.splice is not None:
            splices[tnr + 1] = round(track.splice * SCP_BASE_HZ / track.sample_hz)
    if not splices.any():
        return b""
    chunk = _WRSP + len(splices.tobytes()).to_bytes(4, "little") + splices.tobytes()
    return b"EXTS" + len(chunk).to_bytes(4, "little") + chunk


def write_scp(image):
    """Serialise index-cued SCP tracks at 40 MHz (``sample_hz`` is rescaled)."""
    revs = min((len(t.index) - 1 for t in image.tracks.values()), default=0)
    offsets = np.zeros(SCP_TRACKS, "<u4")
    blocks = [_extension(image.tracks)]
    pos = _DATA + len(blocks[0])
    for tnr in sorted(image.tracks):
        blocks.append(_track_block(tnr, image.tracks[tnr], revs))
        offsets[tnr] = pos
        pos += len(blocks[-1])
    keys = sorted(image.tracks) or [0]
    flags = image.flags | FLAG_INDEXED
    head = bytes(
        [0x19, image.disk_type, revs, keys[0], keys[-1], flags, 0, image.heads, 0]
    )
    body = offsets.tobytes() + b"".join(blocks)
    checksum = int(np.frombuffer(body, np.uint8).sum(dtype=np.int64)) & 0xFFFFFFFF
    return SCP_SIGNATURE + head + checksum.to_bytes(4, "little") + body
