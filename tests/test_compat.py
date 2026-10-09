import base64
import hashlib
import io
import json
import os
import pathlib
import zipfile

import numpy as np
import pytest

from nybulah import cli
from nybulah.analysis import gcr
from nybulah.analysis.cycle import TrackKind
from nybulah.analysis.flux import (
    ROTATION_TICKS,
    decode_flux,
    estimate_zone,
    interval_bits,
)
from nybulah.analysis.sector import SectorError, format_track
from nybulah.analysis.synth import simulate_capture, simulate_flux
from nybulah.formats import (
    D64,
    G64,
    G64Track,
    Nib,
    NibEntry,
    d64_to_g64,
    image as images,
    load,
    loads,
    read_g64,
    to_d64,
    to_g64,
    to_p64,
    write_d64,
    write_g64,
    write_nib,
)
from nybulah.formats import kryoflux, nbz, p64
from nybulah.formats.g64 import EXT_DTYPE, SIDE1
from nybulah.formats.kryoflux import read_stream, write_stream
from nybulah.formats.nbz import lz_compress, lz_decompress, read_nbz, write_nbz
from nybulah.formats.nib import NIB_TRACK
from nybulah.formats.p64 import (
    P64,
    STRONG,
    P64Track,
    read_p64,
    track_from_bits,
    track_times,
    write_p64,
)
from nybulah.formats.scp import FLAG_TPI96, SCP, FluxTrack, read_scp, write_scp

# Produced by BCL 1.2.0 LZ_CompressFast from _BCL_PLAIN.
_BCL_PLAIN = b"nybulah " * 40 + bytes(range(256)) + bytes(300) + b"nybulah " * 3
_BCL_VECTOR = base64.b64decode(
    "AW55YnVsYWggAQgIARAQASAgAUBAAYEAgQABQEAAAQACAwQFBgcICQoLDA0ODxAREhMUFRYXGBka"
    "GxwdHh8gISIjJCUmJygpKissLS4vMDEyMzQ1Njc4OTo7PD0+P0BBQkNERUZHSElKS0xNTk9QUVJT"
    "VFVWV1hZWltcXV5fYGFiY2RlZmdoaWprbG1ub3BxcnN0dXZ3eHl6e3x9fn+AgYKDhIWGh4iJiouM"
    "jY6PkJGSk5SVlpeYmZqbnJ2en6ChoqOkpaanqKmqq6ytrq+wsbKztLW2t7i5uru8vb6/wMHCw8TF"
    "xsfIycrLzM3Oz9DR0tPU1dbX2Nna29zd3t/g4eLj5OXm5+jp6uvs7e7v8PHy8/T19vf4+fr7/P3+"
    "/wAAAAABBAQBCAgBEBABICABQEABgQCBAAEsLAEYhEQ="
)
# SHA-256 of the P64 reference implementation's output for _P64_PULSES.
_P64_REFERENCE = "13d02c7f43b6121c2daa8d9d32db39ec714d3ef094929e842a657ccb967fe3bf"
_P64_PULSES = {
    2: ([100, 152, 204, 256, 3199990], [STRONG, STRONG, 1 << 31, STRONG, STRONG]),
    36: ([5000, 5052], [STRONG, 1]),
}
# Standard 1541 track per density zone.
_ZONE_TRACK = {3: 1, 2: 20, 1: 26, 0: 31}
_JITTER = 0.05


def _d64(seed=0):
    image = D64(np.random.default_rng(seed).integers(0, 256, (683, 256), np.uint8))
    image.data[357, 0xA2:0xA4] = list(b"ID")
    return image


def _track_bits(track, seed=0):
    data = np.random.default_rng(seed).integers(
        0, 256, (gcr.sectors_per_track(track), 256)
    )
    return gcr.to_bits(format_track(track, data, b"ID"))


def _flux_track(bits, zone, revs=3, rpm=300.0, seed=0, hz=40e6):
    times, index = simulate_flux(bits, zone, revs, _JITTER, rpm, rng=seed)
    samples = np.rint(times * hz).astype(np.int64)
    return FluxTrack(np.diff(np.concatenate(([0], samples))), np.rint(index * hz), hz)


def test_bcl_known_vector_and_roundtrip():
    assert lz_decompress(_BCL_VECTOR).tobytes() == _BCL_PLAIN
    rng = np.random.default_rng(0)
    for data in (b"", b"a", b"\0\1", bytes(3), _BCL_PLAIN, rng.bytes(5000)):
        packed = lz_compress(data)
        assert lz_decompress(packed).tobytes() == data
    runs = np.tile(rng.integers(0, 256, 700, dtype=np.uint8), 17).tobytes()
    assert len(lz_compress(runs)) < len(runs) // 4
    for bad in (b"\1\1", b"\1\1\x84", b"\1x\1\4\x09"):
        with pytest.raises(ValueError):
            lz_decompress(bad)


def test_nbz_roundtrip_and_sniff():
    g64 = d64_to_g64(_d64(), progress=False)
    entries = [
        NibEntry(
            key,
            track.speed,
            gcr.to_bytes(simulate_capture(gcr.to_bits(track.data), 8 * NIB_TRACK)),
        )
        for key, track in g64.tracks.items()
    ]
    nib = Nib(entries)
    packed = write_nbz(nib)
    assert len(packed) < len(write_nib(nib))
    back = read_nbz(packed)
    assert [e.halftrack for e in back.entries] == [e.halftrack for e in entries]
    image = loads(packed, "x.nbz")
    assert image.kind == "nbz" and not image.meta["nb2"]
    d64 = to_d64(image)
    assert (d64.errors == SectorError.OK).all()
    assert np.array_equal(d64.data, _d64().data)
    nb2 = Nib([NibEntry(36, 2, np.tile(entries[17].data, (4, 4, 1)))], 2, True, 4)
    assert loads(write_nib(nb2)).kind == "nb2"
    assert loads(write_nbz(nb2)).meta["nb2"]
    with pytest.raises(ValueError):
        loads(b"\x07garbage", "x.bin")


def test_g71_ext_roundtrip():
    rng = np.random.default_rng(2)
    ext = np.zeros(168, EXT_DTYPE)
    ext[0] = (100, 7000, 3250, 0x55, 0, 2, 1)
    tracks = {
        2: G64Track(rng.integers(0, 256, 7692, dtype=np.uint8), 3),
        3
        | SIDE1: G64Track(
            rng.integers(0, 256, 7000, dtype=np.uint8), rng.integers(0, 4, 7000)
        ),
    }
    raw = write_g64(G64(tracks, ext=ext))
    assert (
        raw[:8] == b"GCR-1571"
        and raw[9] == 168
        and raw[12 + 168 * 8 : 12 + 168 * 8 + 4] == b"EXT\x01"
    )
    back = read_g64(raw)
    assert np.array_equal(back.ext, ext) and sorted(back.tracks) == sorted(tracks)
    for key, track in tracks.items():
        assert np.array_equal(back.tracks[key].data, track.data)
        assert np.array_equal(back.tracks[key].speed, track.speed)
    image = loads(raw)
    assert image.kind == "g71" and image.meta["ext"][0]["bitcell_ns"] == 3250
    assert write_g64(to_g64(image)) == raw
    single = write_g64(G64({2: tracks[2]}))
    assert single[:8] == b"GCR-1541" and read_g64(single).ext is None
    with pytest.raises(ValueError):
        read_g64(raw[:-1000])


@pytest.mark.parametrize("zone", range(4))
def test_read_circuit_cell_windows(zone):
    quarter = 16 - zone
    for cells in range(1, 6):
        nominal = 4 * quarter * cells
        lo, hi = nominal - 2 * quarter + 1, nominal + 2 * quarter
        assert interval_bits([lo, hi, lo - 1, hi + 1], zone).tolist() == [
            cells,
            cells,
            cells - 1,
            cells + 1,
        ]
    times = np.cumsum([0, 5 * 4 * quarter, 4 * quarter, 4 * quarter])
    bits, index = decode_flux(times, zone, [0.0, times[1] + 1])
    assert bits.tolist() == [1, 0, 0, 0, 1, 1, 1] and index.tolist() == [0, 5]
    assert decode_flux([5.0], zone)[0].size == 0


@pytest.mark.parametrize("zone", range(4))
@pytest.mark.parametrize("rpm", [297.0, 303.0])
def test_flux_bit_recovery_with_jitter(zone, rpm):
    """Intervals decode within half a cell; index normalisation removes the 1% speed error.

    Transition jitter of 0.05 cell gives intervals a 0.071 cell spread, so the
    half-cell margin is 7 standard deviations: about 2e-12 errors per interval.
    """
    track = _ZONE_TRACK[zone]
    bits = _track_bits(track, zone)
    flux = _flux_track(bits, zone, rpm=rpm, seed=zone)
    assert estimate_zone(np.diff(flux.ticks()[0])) == zone
    cap = images.flux_capture(*flux.ticks())
    assert cap.zone == zone and cap.revolutions == 3
    for rev in range(1, 3):
        assert np.array_equal(cap.bits[cap.index[rev] : cap.index[rev + 1]], bits)
    rev, cycle = images.revolution(cap)
    assert cycle.kind == TrackKind.FORMATTED and cycle.length == len(bits)
    assert np.array_equal(
        np.roll(rev, -int(np.flatnonzero(rev)[0])),
        np.roll(bits, -int(np.flatnonzero(bits)[0])),
    )


def test_scp_and_kryoflux_roundtrip(tmp_path):
    bits = _track_bits(18)
    flux = _flux_track(bits, 2)
    scp = read_scp(write_scp(SCP({34: flux})))
    back = scp.tracks[34]
    assert back.sample_hz == 40e6 and np.array_equal(back.index, flux.index)
    assert np.array_equal(back.intervals, flux.intervals[: len(back.intervals)])
    flux.splice = 1234.0
    assert read_scp(write_scp(SCP({7: flux}))).tracks[7].splice == 1234.0
    long = FluxTrack(np.array([10, 70000, 65536, 5]), np.array([0.0, 135551]), 40e6)
    assert np.array_equal(
        read_scp(write_scp(SCP({0: long}))).tracks[0].intervals, [10, 70000, 65537, 5]
    )
    kf = FluxTrack(
        np.concatenate(([3, 13, 300, 70000], flux.intervals[4:])),
        flux.index + 7e4,
        24e6,
    )
    again = read_stream(write_stream(kf))
    assert again.sample_hz == 24e6 and np.array_equal(again.intervals, kf.intervals)
    assert np.allclose(again.index, kf.index, atol=1)
    for cyl in (17, 18):
        (tmp_path / f"track{cyl:02d}.0.raw").write_bytes(
            write_stream(_flux_track(_track_bits(cyl + 1), 2))
        )
    (tmp_path / "other00.0.raw").write_bytes(b"\x0d\x0d\x0d\x0d")
    image = load(tmp_path / "track17.0.raw")
    assert image.kind == "kryoflux" and image.meta["layout"] == "cylinders"
    assert sorted(image.tracks) == [36, 38]
    assert [r["errors"] for r in images.info(image)] == [0, 0]
    assert len(load(tmp_path).tracks) == 3


def test_flux_layout_vote():
    formatted = {
        t: _flux_track(_track_bits(t), gcr.speed_zone(t), revs=2) for t in (1, 2, 3)
    }
    half = SCP({2 * (t - 1) * 2: f for t, f in formatted.items()}, flags=0)
    assert loads(write_scp(half)).meta["layout"] == "half-cylinders"
    full = SCP({2 * (t - 1): f for t, f in formatted.items()}, flags=FLAG_TPI96)
    image = loads(write_scp(full))
    assert image.meta["layout"] == "cylinders" and sorted(image.tracks) == [2, 4, 6]
    assert loads(write_scp(full), layout="halftracks").meta["layout"] == "halftracks"
    blank = FluxTrack(np.full(40000, 400), np.array([0.0, 8e6, 16e6]), 40e6)
    assert (
        loads(write_scp(SCP({0: blank}, flags=FLAG_TPI96))).meta["layout"]
        == "half-cylinders"
    )


def test_p64_reference_vector_and_errors():
    image = P64({k: P64Track(*v) for k, v in _P64_PULSES.items()})
    raw = write_p64(image)
    assert hashlib.sha256(raw).hexdigest() == _P64_REFERENCE
    back = read_p64(raw)
    for key, (positions, strengths) in _P64_PULSES.items():
        assert back.tracks[key].positions.tolist() == positions
        assert back.tracks[key].strengths.tolist() == strengths
    two = read_p64(write_p64(P64({2 | SIDE1: P64Track([1, 2, 3])}, write_protect=True)))
    assert two.sides == 2 and two.write_protect and list(two.tracks) == [2 | SIDE1]
    corrupt = bytearray(raw)
    corrupt[40] ^= 1
    for bad in (bytes(corrupt), raw[:-1], b"P64-1541" + bytes(4)):
        with pytest.raises(ValueError):
            read_p64(bad)


def test_p64_from_per_byte_speed():
    """Cells take their zone's duration: 52 clocks at zone 3, 64 at zone 0."""
    mixed = gcr.encode(np.arange(200, dtype=np.uint8))
    speed = np.repeat([3, 0], 125)
    pulses = to_p64(images.from_g64(G64({2: G64Track(mixed, speed)}))).tracks[2]
    gaps = np.diff(pulses.positions)
    assert gaps[:200].min() / gaps[-200:].min() == pytest.approx(52 / 64, abs=1e-3)


def test_g64_p64_roundtrip_and_weak_bits():
    d64 = _d64(3)
    g64 = d64_to_g64(d64, progress=False)
    image = loads(write_p64(to_p64(images.from_g64(g64))))
    assert image.kind == "p64" and to_p64(image) is image.source
    for key, track in g64.tracks.items():
        cap = image.tracks[key][0]
        assert cap.circular and cap.zone == track.speed
        ref = gcr.to_bits(track.data)
        assert np.array_equal(cap.bits, np.roll(ref, -int(np.flatnonzero(ref)[0])))
    assert np.array_equal(to_d64(image).data, d64.data)
    bits = gcr.to_bits(g64.tracks[2].data)
    weak = track_from_bits(bits, weak=[(1000, 400)])
    assert (weak.strengths < STRONG).sum() == 400
    times, index = track_times(weak, 3, rng=1)
    assert len(index) == 4 and times[-1] >= 3 * ROTATION_TICKS
    cap = loads(write_p64(P64({2: weak}))).tracks[2][0]
    revs = [cap.bits[a:b] for a, b in zip(cap.index[:-1], cap.index[1:])]
    assert cap.revolutions == 5 and not all(np.array_equal(r, revs[0]) for r in revs)
    assert images.info(images.DiskImage("p64", {2: [cap]}))[0]["errors"] is not None


def test_nib_flux_and_d64_conversions():
    d64 = _d64(4)
    image = loads(write_d64(d64))
    assert image.kind == "d64" and np.array_equal(to_d64(image).data, d64.data)
    flux = {
        2 * (t - 1): _flux_track(_track_bits(t, t), gcr.speed_zone(t), revs=2)
        for t in (1, 18)
    }
    noise = np.random.default_rng(6).integers(100, 400, 30000)
    flux[4] = FluxTrack(noise, np.array([0, noise.sum() / 2, noise.sum()]), 40e6)
    scp_image = loads(write_scp(SCP(flux)))
    pulses = to_p64(scp_image)
    assert sorted(pulses.tracks) == [2, 6, 36]
    assert (pulses.tracks[2].positions < ROTATION_TICKS).all()
    rows = {r["track"]: r for r in images.info(scp_image)}
    assert rows["1"]["errors"] == 0 and rows["1"]["revolutions"] == 2
    assert rows["3"]["kind"] == "UNFORMATTED" and rows["3"]["errors"] is None
    unformatted = []
    g64 = to_g64(scp_image, unformatted=unformatted)
    assert sorted(g64.tracks) == [2, 6, 36] and unformatted == [6]
    nib = Nib(
        [
            NibEntry(2, 3, np.full(NIB_TRACK, 0xFF, np.uint8)),
            NibEntry(3, 3, np.full(NIB_TRACK, 0x55, np.uint8)),
        ]
    )
    rows = images.info(loads(write_nib(nib)))
    assert [r["kind"] for r in rows] == ["KILLER", "UNFORMATTED"] and rows[1][
        "track"
    ] == "1.5"
    short = images.Capture(np.ones(100, np.uint8), 3)
    assert images.revolution(short)[1].kind == TrackKind.KILLER


def _rng_bytes(n, seed=11):
    return np.random.default_rng(seed).integers(0, 256, n, np.uint8)


def test_cli_convert_and_info(tmp_path, capsys):
    src = tmp_path / "in.d64"
    src.write_bytes(write_d64(_d64(5)))
    for suffix in (".g64", ".g71", ".p64", ".d64"):
        out = cli.main(["convert", str(src), str(tmp_path / f"out{suffix}")])
        assert out["source"] == "d64" and out["tracks"] == 35
        assert out["unformatted"] == []
    noisy = Nib([NibEntry(74, 0, _rng_bytes(NIB_TRACK))])
    (tmp_path / "noise.nib").write_bytes(write_nib(noisy))
    out = cli.main(["convert", str(tmp_path / "noise.nib"), str(tmp_path / "n.g64")])
    assert out["unformatted"] == ["37"]
    assert 74 in read_g64((tmp_path / "n.g64").read_bytes()).tracks
    assert loads((tmp_path / "out.p64").read_bytes()).kind == "p64"
    info = cli.main(["info", str(tmp_path / "out.g64"), "--layout", "cylinders"])
    assert len(info["tracks"]) == 35 and all(r["errors"] == 0 for r in info["tracks"])
    assert json.loads(capsys.readouterr().out.split("\n", 1)[0])["target"].endswith(
        "g64"
    )
    with pytest.raises(ValueError):
        cli.main(["convert", str(src), str(tmp_path / "out.txt")])


CORPUS_SUFFIXES = {".nib", ".nbz", ".nb2", ".g64", ".g71", ".p64", ".scp", ".d64"}


def _corpus_items(root):
    """``(file, zip member or None)`` of every corpus file, without reading data."""
    if not root:
        return []
    items = []
    for path in sorted(pathlib.Path(root).rglob("*")):
        if path.suffix.lower() == ".zip":
            try:
                with zipfile.ZipFile(path) as archive:
                    items += [
                        (str(path), m) for m in archive.namelist() if m[-1] != "/"
                    ]
            except zipfile.BadZipFile:
                continue
        elif path.is_file():
            items.append((str(path), None))
    return items


def _images(name, data):
    """``(name, bytes)`` of disk images in a file, descending into zips."""
    if name.lower().endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for member in archive.namelist():
                yield from _images(f"{name}:{member}", archive.read(member))
    elif pathlib.PurePath(name).suffix.lower() in CORPUS_SUFFIXES:
        yield name, data


@pytest.mark.parametrize(
    "path,member",
    _corpus_items(os.environ.get("NYBULAH_CORPUS")),
)
def test_corpus_loads(path, member):
    """Each disk image in the corpus (``NYBULAH_CORPUS``) loads and summarises."""
    if member is None:
        name, data = path, pathlib.Path(path).read_bytes()
    else:
        with zipfile.ZipFile(path) as archive:
            name, data = f"{path}:{member}", archive.read(member)
    for image_name, image_data in _images(name, data):
        if not image_data:
            with pytest.raises(ValueError):
                loads(image_data, image_name)
            continue
        images.info(loads(image_data, image_name))


@pytest.mark.parametrize("module", [nbz, p64, kryoflux])
def test_interpreted_codecs_match_compiled(module, monkeypatch):
    """The numba kernels give the same results when run as plain Python."""
    rng = np.random.default_rng(7)
    data = np.tile(rng.integers(0, 256, 300, dtype=np.uint8), 7).tobytes()
    positions = np.sort(rng.choice(ROTATION_TICKS, 300, replace=False))
    pulses = P64({2: P64Track(positions, rng.integers(1, 2**32, 300))})
    stream = FluxTrack(
        np.concatenate(([5, 300, 70000], rng.integers(40, 400, 300))),
        np.array([10.0, 3e4]),
        24e6,
    )
    cases = {
        nbz: lambda: lz_decompress(lz_compress(data + bytes(range(256)))).tobytes(),
        p64: lambda: write_p64(read_p64(write_p64(pulses)))[24:],
        kryoflux: lambda: list(read_stream(write_stream(stream)).intervals),
    }
    compiled = cases[module]()
    for name, obj in vars(module).items():
        if hasattr(obj, "py_func"):
            monkeypatch.setattr(module, name, obj.py_func)
    assert cases[module]() == compiled
