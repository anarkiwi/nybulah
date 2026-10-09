import json
import xml.etree.ElementTree as ET

import numpy as np
import pytest
from PIL import Image

from nybulah import cli, viz
from nybulah.analysis.diskmap import Cls, Kind, disk_map, load_thresholds
from nybulah.analysis.synth import synthetic_disk
from nybulah.formats import to_g64, write_g64

BINS = 256


@pytest.fixture(name="synth", scope="module")
def synth_fixture():
    image, truth = synthetic_disk()
    return image, truth, disk_map(image, bins=BINS)


@pytest.fixture(name="g64_path", scope="module")
def g64_path_fixture(synth, tmp_path_factory):
    path = tmp_path_factory.mktemp("viz") / "synth.g64"
    path.write_bytes(write_g64(to_g64(synth[0])))
    return path


def test_terminal_strip(synth):
    _, _, dmap = synth
    lines = viz.terminal_strip(dmap, 40)
    assert len(lines) == len(dmap.keys)
    assert {len(line) for line in lines} == {5 + 2 + 40 + 1}
    rows = dict(zip(dmap.keys.tolist(), (line[7:-1] for line in lines)))
    assert rows[62] == "s" * 40 and rows[40] == "z" * 40
    assert "W" in rows[20] and set(rows[2]) == {"."}
    assert all(c in viz.legend_line() for c in "zgshdw!")


def test_svg_has_one_tooltip_per_region(synth):
    _, _, dmap = synth
    root = ET.fromstring(viz.strip_svg(dmap, width=400))
    ns = "{http://www.w3.org/2000/svg}"
    titles = root.findall(f".//{ns}title")
    assert len(titles) == len(dmap.regions)
    assert any("noflux_span" in t.text and "unstable" in t.text for t in titles)
    labels = {t.text for t in root.findall(f".//{ns}text")}
    assert {"0", "20", "20.5"} <= labels


def test_html_table_lists_nonstandard_regions(synth, tmp_path):
    _, _, dmap = synth
    path = tmp_path / "map.html"
    viz.save(dmap, path)
    page = path.read_text()
    shown = (viz.painted(dmap.regions) > Cls.STANDARD) | (
        dmap.regions["cls"] == Cls.FAULT
    )
    assert page.count("<tr>") == 1 + shown.sum()
    assert page.count("<title>") == 1 + len(dmap.regions)


def test_apng_steps_through_revolutions(synth, tmp_path):
    _, _, dmap = synth
    path = tmp_path / "map.apng"
    assert viz.save(dmap, path) == dmap.revs.max() == 4
    with Image.open(path) as im:
        assert im.is_animated and im.n_frames == 4 and im.size == (640, 440)
        frames = []
        for i in range(im.n_frames):
            im.seek(i)
            frames.append(np.asarray(im.convert("RGB")))
    assert path.stat().st_size < 1 << 20
    assert any((frames[0] != f).any() for f in frames[1:])


def test_apng_rotates_under_head(synth, tmp_path):
    _, truth, dmap = synth
    path = tmp_path / "map.png"
    assert viz.save(dmap, path, animate=True, mode="rotate") == 36
    with Image.open(path) as im:
        assert im.n_frames == 36
    key, _, start, end, _ = next(t for t in truth if t[1] == "NOFLUX_SPAN")
    n = dmap.length[np.searchsorted(dmap.keys, key)]
    assert (key, Kind.NOFLUX_SPAN) in viz.head_regions(dmap, (start + end) / 2 / n)
    assert viz.head_regions(dmap, 0.99) == []


def test_png_and_unknown_format(synth, tmp_path):
    _, _, dmap = synth
    viz.save(dmap, tmp_path / "map.png")
    with Image.open(tmp_path / "map.png") as im:
        assert im.size == (640, 440) and not getattr(im, "is_animated", False)
    with pytest.raises(ValueError):
        viz.save(dmap, tmp_path / "map.txt")


@pytest.mark.parametrize("name", ["m.png", "m.svg", "m.html", "m.apng"])
def test_cli_map(g64_path, tmp_path, capsys, name):
    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps({"thresholds": load_thresholds()}))
    out = tmp_path / name
    argv = ["map", str(g64_path), "-o", str(out), "--bins", "64"]
    result = cli.main(
        argv + ["--thresholds", str(summary), "--captures", str(g64_path)]
    )
    assert out.stat().st_size > 0 and result["output"] == str(out)
    assert result["anomalies"] > 0 and json.loads(capsys.readouterr().out) == result


def test_cli_info_map(g64_path, capsys):
    result = cli.main(["info", str(g64_path), "--map", "--width", "32"])
    printed = capsys.readouterr().out.splitlines()
    assert printed[:-1] == result["map"] and len(result["map"]) == 36
    assert {len(line) for line in result["map"]} == {5 + 2 + 32 + 1}
