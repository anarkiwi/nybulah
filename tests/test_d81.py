import numpy as np
import pytest

from nybulah.analysis import mfm
from nybulah.analysis.sector import SectorError as E
from nybulah.formats import D81, MfmDisk, read_d81, write_d81
from nybulah.formats import d81

SIZE = 819200


def formatted(name=b"NYBULAH", disk_id=b"QZ"):
    """A blank disk laid out as the DOS new-disk code writes it."""
    image = D81(np.zeros((3200, 256), np.uint8))
    h = image.data[D81.index(40, 0)]
    h[:4] = [40, 3, ord("D"), 0]
    h[4:20] = 0xA0
    h[4 : 4 + len(name)] = list(name)
    h[20:22] = 0xA0
    h[22:24] = list(disk_id)
    h[24:29] = [0xA0, ord("3"), ord("D"), 0xA0, 0xA0]
    for s, link in ((1, (40, 2)), (2, (0, 0xFF))):
        b = image.data[D81.index(40, s)]
        b[:8] = [*link, ord("D"), ord("D") ^ 0xFF, *disk_id, 0xC0, 0]
        entries = b[16:].reshape(40, 6)
        entries[:, 0] = 40
        entries[:, 1:] = 0xFF
    used = image.data[D81.index(40, 1)][16:].reshape(40, 6)[39]
    used[0], used[1] = 36, 0xF0
    return image


def test_header_and_bam():
    info = formatted().info()
    assert info["name"] == "NYBULAH" and info["id"] == "QZ"
    assert info["dos"] == "3" and info["format"] == "D"
    assert info["directory"] == (40, 3) and info["blocks_free"] == 3160
    assert info["bam_mismatch"] == [] and info["errors"] == 0
    free, bitmap = formatted().bam()
    assert free[39] == 36 and not bitmap[39, :4].any() and bitmap[39, 4:].all()
    image = formatted()
    image.data[D81.index(40, 2)][16] = 39
    assert image.info()["bam_mismatch"] == [41]


def test_roundtrip_bytes():
    image = formatted()
    raw = write_d81(image)
    assert len(raw) == SIZE and np.array_equal(read_d81(raw).data, image.data)
    image.errors[5] = E.DATA_CHECKSUM
    raw = write_d81(image)
    assert len(raw) == SIZE + 3200 and raw[SIZE + 5] == E.DATA_CHECKSUM
    assert np.array_equal(read_d81(raw).errors, image.errors)
    assert len(write_d81(image, errors=False)) == SIZE


@pytest.mark.parametrize("size", [0, 174848, SIZE - 256, SIZE + 1])
def test_bad_sizes(size):
    with pytest.raises(ValueError):
        read_d81(bytes(size))


def test_bad_shapes():
    with pytest.raises(ValueError):
        D81(np.zeros((3199, 256), np.uint8))
    with pytest.raises(ValueError):
        D81(np.zeros((3200, 256), np.uint8), np.zeros(5, np.uint8))


def test_side_rows_follow_trans_ts():
    rows = d81.side_rows(39, 1)
    assert list(rows) == list(range(39 * 40 + 20, 39 * 40 + 40))
    assert list(d81.side_rows(0, 0)) == list(range(20))


def test_tracks_roundtrip_with_errors():
    rng = np.random.default_rng(5)
    image = D81(rng.integers(0, 256, (3200, 256), np.uint8))
    pairs = {
        100: E.HEADER_NOT_FOUND,
        202: E.HEADER_CHECKSUM,
        304: E.DATA_NOT_FOUND,
        406: E.DATA_CHECKSUM,
    }
    for row, err in pairs.items():
        image.errors[row : row + 2] = err
        if err != E.DATA_CHECKSUM:
            image.data[row : row + 2] = 0
    image.errors[500], image.errors[501] = E.OK, E.DATA_CHECKSUM
    media = d81.to_tracks(image)
    assert len(media) == 160 and all(
        len(m[0]) == mfm.TRACK_BYTES for m in media.values()
    )
    data, mark = media[(0, 1)]
    rec = mfm.decode_track(data, mark, True).sectors
    assert (rec["h"] == 0).all() and (rec["c"] == 0).all()
    back = d81.from_decodes(MfmDisk.from_media("d81", media).tracks)
    expect = image.errors.copy()
    expect[500] = E.DATA_CHECKSUM
    assert np.array_equal(back.errors, expect)
    assert np.array_equal(back.data, image.data)


def test_missing_tracks_are_header_not_found():
    back = d81.from_decodes({(0, 1): [], (99, 0): []})
    assert (back.errors == E.HEADER_NOT_FOUND).all()


def test_unencodable_error():
    image = formatted()
    image.errors[7] = E.ID_MISMATCH
    with pytest.raises(ValueError):
        d81.to_tracks(image)
