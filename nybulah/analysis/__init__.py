"""Transport-independent GCR analysis: codec, sectors and revolution detection."""

from .cycle import Cycle, TrackKind, extract_revolution, find_cycle, index_align
from .gcr import (
    bit_rate,
    bits_per_revolution,
    decode,
    decode_bits,
    encode,
    encode_bits,
    rotate,
    runs_of_ones,
    sectors_per_track,
    speed_zone,
    sync_mask,
    to_bits,
    to_bytes,
    track_capacity,
)
from .capture import capture_bits, trailing_ones
from .sector import (
    SectorError,
    TrackDecode,
    decode_track,
    format_track,
    header_tracks,
    merge_decodes,
)

__all__ = [
    "Cycle",
    "SectorError",
    "TrackDecode",
    "TrackKind",
    "bit_rate",
    "bits_per_revolution",
    "capture_bits",
    "decode",
    "decode_bits",
    "decode_track",
    "encode",
    "encode_bits",
    "extract_revolution",
    "find_cycle",
    "format_track",
    "header_tracks",
    "index_align",
    "merge_decodes",
    "rotate",
    "runs_of_ones",
    "sectors_per_track",
    "speed_zone",
    "sync_mask",
    "to_bits",
    "to_bytes",
    "track_capacity",
    "trailing_ones",
]
