import datetime

import numpy as np
import pytest

from nybulah.analysis import mfm
from nybulah.analysis.mfm import SectorSpec
from nybulah.formats import MfmDisk, imd, read_imd, write_imd

HEADER = b"IMD 1.18: 09/10/2026 12:00:00\r\ntest"


def handmade():
    """Two tracks written byte by byte from the IMD.TXT chapter 6 layout."""
    t0 = bytes([5, 0, 1 | 0x40, 3, 2]) + bytes([1, 3, 2]) + bytes([0, 0, 0])
    t0 += bytes([1]) + bytes(range(256)) * 2
    t0 += bytes([4, 0xE5]) + bytes([0])
    t1 = bytes([5, 1, 0 | 0x80, 2, 0xFF]) + bytes([7, 9]) + bytes([3, 3])
    t1 += (256).to_bytes(2, "little") + (1024).to_bytes(2, "little")
    t1 += bytes([7]) + bytes([0x55]) * 256 + bytes([6, 0x11])
    return HEADER + b"\x1a" + t0 + t1


def test_parse_handmade():
    image = read_imd(handmade())
    assert image.header == HEADER and len(image.tracks) == 2
    t0, t1 = image.tracks
    assert (t0.mode, t0.cylinder, t0.head) == (5, 0, 1)
    assert list(t0.r) == [1, 3, 2] and list(t0.h) == [0, 0, 0] and list(t0.c) == [0] * 3
    assert np.array_equal(t0.data[0], np.tile(np.arange(256, dtype=np.uint8), 2))
    assert (t0.data[1] == 0xE5).all() and t0.deleted[1] and not t0.error[1]
    assert t0.data[2] is None and t0.size_code == 2
    assert (
        list(t1.c) == [3, 3] and list(t1.h) == [0, 0] and list(t1.sizes) == [256, 1024]
    )
    assert t1.deleted[0] and t1.error[0] and (t1.data[0] == 0x55).all()
    assert t1.error[1] and not t1.deleted[1] and (t1.data[1] == 0x11).all()
    assert t1.size_code == 0xFF


def test_write_is_inverse_of_read():
    raw = handmade()
    assert write_imd(read_imd(raw)) == raw.replace(
        bytes([7]) + bytes([0x55]) * 256, bytes([8, 0x55])
    )


@pytest.mark.parametrize(
    "raw",
    [b"IMX 1.18", b"IMD 1.18 no eof", HEADER + b"\x1a" + bytes([5, 0, 0, 1, 2, 1, 9])],
)
def test_bad_images(raw):
    with pytest.raises(ValueError):
        read_imd(raw)


def test_media_roundtrip():
    image = read_imd(handmade())
    media = imd.to_tracks(image)
    data, mark = media[(0, 1)]
    rec = mfm.decode_track(data, mark, True).sectors
    assert list(rec["r"]) == [1, 3, 2] and list(rec["error"]) == [1, 1, 4]
    assert rec["flags"][1] & mfm.Flag.DELETED
    disk = MfmDisk.from_media("imd", media, image)
    back = imd.from_decodes(disk.tracks, HEADER)
    assert write_imd(back) == write_imd(image)


def test_from_decodes_skips_unreadable_ids():
    specs = mfm.standard_layout(4, 1, errors=[1, 9, 1, 2, 1, 1, 1, 1, 1, 1])
    data, mark = mfm.encode_track(specs)
    track = imd.track_from_decodes((4, 0), [mfm.decode_track(data, mark, True)])
    assert list(track.r) == [1, 3, 5, 6, 7, 8, 9, 10]
    assert list(track.h) == [1] * 8 and track.head == 0
    raw = write_imd(imd.Imd(HEADER, [track]))
    assert raw[len(HEADER) + 3] == 0x40
    assert imd.track_from_decodes((4, 0), []) is None


def test_crowded_track_shrinks_gap():
    specs = [SectorSpec(0, 0, r, 1, np.full(256, r, np.uint8)) for r in range(1, 19)]
    data, mark = mfm.encode_track(specs, n=12000)
    track = imd.track_from_decodes((0, 1), [mfm.decode_track(data, mark, True)])
    layout = imd.specs(track)
    assert {s.gap3 for s in layout} == {
        (6250 - 32 - 18 * (2 * 15 + 7 + 22 + 3 + 256)) // 18
    }
    rec = mfm.decode_track(*mfm.encode_track(layout), True).sectors
    assert len(rec) == 18 and (rec["error"] == 1).all()
    track.sizes[:] = 1024
    track.data = [np.zeros(1024, np.uint8)] * 18
    with pytest.raises(ValueError):
        imd.specs(track)


def test_non_mfm_mode_rejected():
    image = read_imd(handmade())
    image.tracks[0].mode = 2
    with pytest.raises(ValueError):
        imd.to_tracks(image)


def test_default_header():
    when = datetime.datetime(2026, 10, 9, 8, 7, 6)
    assert imd.default_header("hi", when) == b"IMD 1.18: 09/10/2026 08:07:06\r\nhi"
    assert imd.default_header().startswith(imd.MAGIC)
