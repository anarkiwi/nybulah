import numpy as np
import pytest

from nybulah.analysis import gcr
from nybulah.analysis.cycle import Cycle, TrackKind
from nybulah.analysis.sector import SectorError
from nybulah.analysis.synth import simulate_capture
from nybulah.formats import (
    D64,
    G64,
    G64Track,
    Nib,
    NibEntry,
    d64_to_g64,
    g64_to_d64,
    nib_to_g64,
    read_d64,
    read_g64,
    read_nib,
    revolution_bytes,
    write_d64,
    write_g64,
    write_nib,
)
from nybulah.formats.nib import BM_FF_TRACK, NIB_TRACK

BAM = 357


def _d64(sectors=683, seed=0):
    image = D64(np.random.default_rng(seed).integers(0, 256, (sectors, 256), np.uint8))
    image.data[BAM, 0xA2:0xA4] = list(b"XY")
    return image


@pytest.mark.parametrize("sectors,size", [(683, 174848), (768, 196608), (802, 205312)])
def test_d64_sizes_and_roundtrip(sectors, size):
    image = _d64(sectors)
    assert len(write_d64(image)) == size
    image.errors[3] = SectorError.DATA_CHECKSUM
    raw = write_d64(image)
    assert len(raw) == size + sectors and raw[size + 3] == 5
    back = read_d64(raw)
    assert np.array_equal(back.data, image.data)
    assert np.array_equal(back.errors, image.errors)
    assert len(write_d64(image, errors=False)) == size
    assert image.disk_id == b"XY"
    assert image.span(18) == slice(BAM, BAM + 19)


def test_d64_invalid():
    with pytest.raises(ValueError):
        read_d64(b"\0" * 1000)
    with pytest.raises(ValueError):
        D64(np.zeros((700, 256), np.uint8))


def test_g64_known_header_bytes():
    raw = write_g64(d64_to_g64(_d64(), progress=False))
    assert raw[:12] == b"GCR-1541\x00\x54\xf8\x1e"
    offsets = np.frombuffer(raw, "<u4", 84, 12)
    speeds = np.frombuffer(raw, "<u4", 84, 12 + 336)
    assert offsets[0] == 0x2AC and offsets[1] == 0
    assert offsets[2] - offsets[0] == 7928 + 2
    assert raw[0x2AC:0x2AE] == (7692).to_bytes(2, "little")
    assert raw[0x2AE : 0x2AE + 5] == bytes([0xFF] * 5)
    assert speeds[[0, 34, 48, 60, 68]].tolist() == [3, 2, 1, 0, 0]
    assert len(raw) == 0x2AC + 35 * (7928 + 2)


def test_g64_roundtrip_halftracks_and_speed_map():
    rng = np.random.default_rng(1)
    zones = rng.integers(0, 4, 7001, dtype=np.uint8)
    image = G64(
        {
            2: G64Track(rng.integers(0, 256, 7692, dtype=np.uint8), 3),
            3: G64Track(rng.integers(0, 256, 8000, dtype=np.uint8), 2),
            40: G64Track(rng.integers(0, 256, 7001, dtype=np.uint8), zones),
        }
    )
    raw = write_g64(image)
    assert int.from_bytes(raw[10:12], "little") == 8000
    back = read_g64(raw)
    assert sorted(back.tracks) == [2, 3, 40] and back.max_track_size == 8000
    for key, track in image.tracks.items():
        assert np.array_equal(back.tracks[key].data, track.data)
        assert np.array_equal(back.tracks[key].speed, track.speed)
    with pytest.raises(ValueError):
        read_g64(b"GCR-1571" + raw[8:])


def test_d64_g64_d64_with_errors():
    image = _d64(768)
    image.data[700:] = 0
    for index, err in ((5, 5), (100, 2), (400, 0x0B), (450, 6), (600, 9), (700, 4)):
        image.errors[index] = err
    image.errors[image.span(33)] = SectorError.NO_SYNC
    g64 = read_g64(write_g64(d64_to_g64(image, progress=False)))
    back = g64_to_d64(g64, progress=False)
    assert back.tracks == 40
    assert np.array_equal(back.errors, image.errors)
    good = image.errors == SectorError.OK
    assert np.array_equal(back.data[good], image.data[good])
    assert np.array_equal(back.data[5], image.data[5])


def test_g64_to_d64_missing_tracks():
    image = _d64()
    g64 = d64_to_g64(image, progress=False)
    del g64.tracks[2 * 5]
    back = g64_to_d64(g64, progress=False)
    assert back.tracks == 35
    assert (back.errors[image.span(5)] == SectorError.NO_SYNC).all()
    assert (back.errors[image.span(6)] == SectorError.OK).all()
    back = g64_to_d64(g64, tracks=40, progress=False)
    assert back.tracks == 40
    assert (back.errors[back.span(36).start :] == SectorError.NO_SYNC).all()


def _captures(image, seed=0):
    rng = np.random.default_rng(seed)
    g64 = d64_to_g64(image, progress=False)
    entries = []
    for halftrack, track in g64.tracks.items():
        bits = gcr.to_bits(track.data)
        cap = simulate_capture(bits, 8 * NIB_TRACK, int(rng.integers(0, len(bits))))
        entries.append(NibEntry(halftrack, track.speed, gcr.to_bytes(cap)))
    return g64, entries


def test_nib_header_roundtrip_and_conversion():
    image = _d64()
    g64, entries = _captures(image)
    entries.append(NibEntry(72, BM_FF_TRACK, np.full(NIB_TRACK, 0xFF, np.uint8)))
    noise = np.random.default_rng(9).integers(0, 256, NIB_TRACK, dtype=np.uint8)
    entries.append(NibEntry(74, 0, noise))
    raw = write_nib(Nib(entries))
    assert raw[:16] == b"MNIB-1541-RAW\x03\x00\x00"
    assert raw[0x10:0x14] == bytes([2, 3, 4, 3])
    assert len(raw) == 0x100 + len(entries) * NIB_TRACK
    nib = read_nib(raw)
    assert [(e.halftrack, e.density) for e in nib.entries] == [
        (e.halftrack, e.density) for e in entries
    ]
    out = nib_to_g64(nib, progress=False)
    assert 74 not in out.tracks
    assert (out.tracks[72].data == 0xFF).all() and out.tracks[72].speed == 0
    for halftrack, track in g64.tracks.items():
        assert len(out.tracks[halftrack].data) == len(track.data)
        assert out.tracks[halftrack].speed == track.speed
    back = g64_to_d64(out, progress=False)
    assert (back.errors == SectorError.OK).all()
    assert np.array_equal(back.data, image.data)


def test_nb2_selects_clean_pass():
    image = _d64()
    _, entries = _captures(image)
    rng = np.random.default_rng(3)
    nb2 = []
    for entry in entries[17:19]:
        passes = rng.integers(0, 256, (4, 4, NIB_TRACK), dtype=np.uint8)
        for pass_ in range(4):
            passes[entry.density, pass_] = entry.data
        bits = gcr.to_bits(entry.data)
        bits[4000:4100] ^= 1
        passes[entry.density, 0] = gcr.to_bytes(bits)
        nb2.append(NibEntry(entry.halftrack, entry.density, passes))
    raw = write_nib(Nib(nb2, version=2, halftracks=True, passes=4))
    assert len(raw) == 0x100 + 2 * 16 * NIB_TRACK and raw[13] == 2 and raw[15] == 1
    back = read_nib(raw, nb2=True)
    assert back.entries[0].data.shape == (4, 4, NIB_TRACK)
    assert np.array_equal(back.entries[1].data, nb2[1].data)
    out = nib_to_g64(back, progress=False)
    assert sorted(out.tracks) == [36, 38]
    d64 = g64_to_d64(out, progress=False)
    for track in (18, 19):
        assert (d64.errors[image.span(track)] == SectorError.OK).all()
        assert np.array_equal(
            d64.data[image.span(track)], image.data[image.span(track)]
        )


def test_nib_invalid():
    with pytest.raises(ValueError):
        read_nib(b"NOTNIB" + bytes(0x200))
    raw = write_nib(Nib([NibEntry(2, 3, np.zeros(NIB_TRACK, np.uint8))]))
    with pytest.raises(ValueError):
        read_nib(raw[:-1])


def test_revolution_bytes_completes_partial_byte():
    bits = np.random.default_rng(4).integers(0, 2, 1000, dtype=np.uint8)
    out = revolution_bytes(bits, Cycle(TrackKind.FORMATTED, 10, 21))
    expected = np.concatenate((bits[10:31], bits[10:13]))
    assert out.tolist() == gcr.to_bytes(expected).tolist()
    assert revolution_bytes(bits, Cycle(TrackKind.KILLER, 0, 17)).tolist() == [255] * 3
