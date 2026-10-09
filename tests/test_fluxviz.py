import base64
import json
import re
import zlib

import numpy as np
import pytest
from PIL import Image

from nybulah import cli, fluxcmd, fluxhtml, fluxviz
from nybulah.analysis.fluxsynth import synthetic_flux_disk
from nybulah.analysis.fluxview import flux_disk
from nybulah.analysis.synth import synthetic_disk
from nybulah.fluxcmd import track_keys
from nybulah.formats import to_g64, write_g64
from nybulah.formats.g64 import SIDE1

KEYS = {2, 6, 10, 14, 24, 41, 62}


@pytest.fixture(name="disk", scope="module")
def disk_fixture():
    image, _ = synthetic_flux_disk(revolutions=2)
    disk = flux_disk(image, keys=KEYS)
    disk.name = "synthetic"
    return disk


@pytest.fixture(name="g64_path", scope="module")
def g64_path_fixture(tmp_path_factory):
    image, _ = synthetic_disk(revolutions=1)
    image.tracks = {k: v for k, v in image.tracks.items() if k in (2, 20, 40)}
    path = tmp_path_factory.mktemp("flux") / "synth.g64"
    path.write_bytes(write_g64(to_g64(image)))
    return path


def _unblob(text, dtype):
    return np.frombuffer(zlib.decompress(base64.b64decode(text)), dtype)


def test_lut_lightness_follows_density_and_hue_follows_sign():
    lut = fluxviz.colour_lut().astype(float)
    mid = fluxviz.LUT_CHROMA // 2
    assert np.ptp(lut[:, mid], axis=1).max() <= 1
    light = fluxviz.to_oklab(lut / 255)[..., 0]
    assert (np.diff(light, axis=0) > 0).all()
    short, long_ = lut[fluxviz.LUT_DENSITY // 2, 0], lut[fluxviz.LUT_DENSITY // 2, -1]
    assert short[2] > short[0] and long_[0] > long_[2]
    arms = fluxviz.to_oklab(lut[:, [0, -1]] / 255)
    assert np.allclose(
        np.hypot(*arms[:, 0, 1:].T), np.hypot(*arms[:, 1, 1:].T), atol=0.01
    )


def test_max_chroma_is_gamut_edge():
    light, hue = np.array([0.3, 0.6, 0.9]), np.array([0.5, 2.0, 4.0])
    c = fluxviz.max_chroma(light, hue)
    for scale, inside in ((0.99, True), (1.02, False)):
        lab = np.stack((light, scale * c * np.cos(hue), scale * c * np.sin(hue)), -1)
        lin = fluxviz.oklab_linear(lab)
        assert ((lin >= 0) & (lin <= 1)).all(axis=-1).all() == inside


def test_compose_marks_faults_and_inferred_no_flux():
    ch = {
        "density": np.array([[0.0, 1.0, 0.5, 0.5]]),
        "delta": np.array([[0.0, 0.0, -1.0, 1.0]]),
        "var": np.zeros((1, 4)),
        "noflux": np.array([[1.0, 0, 0, 0]]),
        "fault": np.array([[0, 0, 0, 0.1]]),
    }
    rgb, marks = fluxviz.compose(ch, 2, fluxviz.colour_lut())
    assert marks[0].tolist() == [fluxviz.STIPPLE_MARK, 0, 0, fluxviz.FAULT_MARK]
    assert rgb[0, 1].min() > rgb[0, 0].max()
    assert rgb[0, 2, 2] > rgb[0, 2, 0]
    ch["var"][:] = 1
    grey, _ = fluxviz.compose(ch, 2, fluxviz.colour_lut())
    assert np.ptp(grey[0, 2].astype(int)) <= 1


def test_polar_puts_a_fault_where_it_is():
    bins = 4096
    rgb = np.zeros((10, bins, 3), np.uint8)
    marks = np.zeros((10, bins), np.uint8)
    marks[0, bins // 4] = fluxviz.FAULT_MARK
    img = fluxviz.polar(rgb, marks, 400)
    green = np.argwhere((img == fluxviz.rgb8(fluxviz.FAULT)).all(-1))
    assert len(green)
    y, x = green.mean(axis=0)
    assert x > 390 and abs(y - 200) < 3


def test_strip_rows_follow_halftracks(disk):
    rgb, marks, owner = fluxviz.strip(disk, 64, 4)
    assert rgb.shape == ((62 - 2 + 1) * 4, 64, 3) and marks.shape == rgb.shape[:2]
    keys = set(owner[owner[:, 0] >= 0, 0].tolist())
    assert keys == KEYS
    gap = owner[(3 - 2) * 4 : (4 - 2) * 4, 0]
    assert (gap == -1).all() and (rgb[4:8] == fluxviz.rgb8(fluxviz.SURFACE)).all()


def test_png_apng_html(disk, tmp_path):
    png = tmp_path / "flux.png"
    assert fluxcmd.save(disk, png, size=300) == png.stat().st_size
    assert Image.open(png).size[0] > 300
    assert fluxcmd.save(disk, tmp_path / "flux.apng", size=200) == 2
    assert Image.open(tmp_path / "flux.apng").n_frames == 2
    html = tmp_path / "flux.html"
    fluxcmd.save(disk, html, zoom=8)
    page = html.read_text()
    assert not re.search(r"""(src|href)=["']?https?:""", page)
    data = json.loads(
        re.search(r'<script id="data"[^>]*>(.*?)</script>', page, re.S)[1]
    )
    assert data["maxCellPx"] == 8 and len(data["tracks"]) == len(KEYS)
    with pytest.raises(ValueError):
        fluxcmd.save(disk, tmp_path / "flux.svg")


def test_html_payload_round_trips(disk):
    data = fluxhtml.payload(disk, 16)
    record = next(t for t in data["tracks"] if t["key"] == 2)["revs"][1]
    rev = disk.tracks[2].revs[1]
    bits = np.unpackbits(_unblob(record["bits"], np.uint8))[: rev.n]
    assert (bits == rev.bits).all()
    kb = np.cumsum(_unblob(record["kb"], "<i4"))
    res = _unblob(record["kt"], "<i4")
    kt = np.cumsum(res + record["q"] * np.diff(kb, prepend=0)) / record["q"]
    assert np.abs(kt - rev.knots[1]).max() <= 0.5 / record["q"] + 1e-9
    lut = _unblob(data["lut"], np.uint8).reshape(*data["lutShape"], 3)
    assert (lut == fluxviz.colour_lut()).all()


def test_eye_key_prefers_measured_spread(disk):
    assert fluxviz.eye_key(disk) == 2


def test_track_keys():
    assert track_keys("18-18.5") == {36, 37, 36 | SIDE1, 37 | SIDE1}
    assert track_keys("3") == {6, 6 | SIDE1}


def test_flux_command(g64_path, tmp_path):
    out = tmp_path / "f.png"
    found = cli.main(
        ["flux", str(g64_path), "-o", str(out), "--size", "200", "--track", "20"]
    )
    assert found["tracks"] == 3 and found["timing"] == {"track": 3} and out.exists()
    found = cli.main(
        [
            "map",
            str(g64_path),
            "-o",
            str(tmp_path / "m.apng"),
            "--analog",
            "--tracks",
            "1-10",
        ]
    )
    assert found["tracks"] == 2 and found["frames"] == 1
