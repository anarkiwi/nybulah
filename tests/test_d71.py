import numpy as np
import pytest

from nybulah.analysis.sector import SectorError
from nybulah.formats import D64, D71, read_d71, write_d71

SIZE = 1366 * 256
BAM = 357


def _d71(seed=0):
    image = D71(np.random.default_rng(seed).integers(0, 256, (1366, 256), np.uint8))
    image.data[BAM, 0xA2:0xA4] = list(b"QZ")
    return image


def test_roundtrip_without_errors():
    image = _d71()
    raw = write_d71(image)
    assert len(raw) == SIZE
    back = read_d71(raw)
    assert np.array_equal(back.data, image.data)
    assert (back.errors == SectorError.OK).all()


def test_roundtrip_with_errors():
    image = _d71()
    image.errors[1000] = SectorError.HEADER_NOT_FOUND
    raw = write_d71(image)
    assert len(raw) == SIZE + 1366 and raw[SIZE + 1000] == 2
    back = read_d71(raw)
    assert np.array_equal(back.data, image.data)
    assert np.array_equal(back.errors, image.errors)
    assert len(write_d71(image, errors=False)) == SIZE
    assert len(write_d71(_d71(), errors=True)) == SIZE + 1366


@pytest.mark.parametrize("size", [0, 174848, SIZE - 256, SIZE + 1, SIZE + 1367])
def test_bad_sizes(size):
    with pytest.raises(ValueError):
        read_d71(b"\0" * size)


def test_bad_geometry():
    with pytest.raises(ValueError):
        D71(np.zeros((683, 256), np.uint8))
    with pytest.raises(ValueError):
        D71(np.zeros((1366, 256), np.uint8), np.ones(683, np.uint8))
    with pytest.raises(ValueError):
        D71.side(71)
    with pytest.raises(ValueError):
        _d71().span(0)


def test_span_and_side():
    image = _d71()
    assert image.tracks == 70
    assert D71.side(1) == (0, 1) and D71.side(35) == (0, 35)
    assert D71.side(36) == (1, 1) and D71.side(70) == (1, 35)
    assert image.span(1) == slice(0, 21)
    assert image.span(18) == slice(BAM, BAM + 19)
    assert image.span(36) == slice(683, 704)
    assert image.span(53) == slice(683 + BAM, 683 + BAM + 19)
    assert image.span(70) == slice(1349, 1366)
    sizes = [image.span(t).stop - image.span(t).start for t in range(1, 71)]
    assert sizes[:35] == sizes[35:] and sum(sizes) == 1366


def test_disk_id():
    assert _d71().disk_id == b"QZ"


def test_sides_roundtrip():
    image = _d71()
    image.errors[700] = SectorError.DATA_CHECKSUM
    side0, side1 = image.sides()
    assert side0.tracks == side1.tracks == 35
    assert side0.disk_id == b"QZ"
    assert side1.errors[17] == SectorError.DATA_CHECKSUM
    assert np.array_equal(side1.data[side1.span(18)], image.data[image.span(53)])
    side0.data[0, 0] ^= 0xFF
    assert side0.data[0, 0] != image.data[0, 0]
    side0.data[0, 0] ^= 0xFF
    back = D71.from_sides(side0, side1)
    assert np.array_equal(back.data, image.data)
    assert np.array_equal(back.errors, image.errors)


def test_from_sides_rejects_extended_d64():
    side = D64(np.zeros((683, 256), np.uint8))
    with pytest.raises(ValueError):
        D71.from_sides(side, D64(np.zeros((768, 256), np.uint8)))
