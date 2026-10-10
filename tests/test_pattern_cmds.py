"""pattern write options (density, lead, weak region), cells, halftracks and the
index: on simulated 1571s and 1541s, and a hardware stream that saw no index."""

import json

import numpy as np
import pytest
from numpy.lib.stride_tricks import sliding_window_view
from test_pattern import HALFTRACK, HW_PATTERN, patch, track_of
from test_stream import RAM_PASS_US, rig

from nybulah import cli, disk, pattern
from nybulah.analysis import pattern as pt
from nybulah.analysis.gcr import bits_per_revolution
from nybulah.nibbler import MAX_HALFTRACK, Capture, TrackError
from nybulah.simdisk import Media, disk_drive
from nybulah.simhost import SimCBM, SimMonitor

BASE = ["--settle-ms", "1", "--max-steps", "40"]


def where(cells, bits):
    """Cell offsets on a circular track where ``bits`` start."""
    span = np.concatenate((cells, cells[: len(bits) - 1]))
    return np.flatnonzero((sliding_window_view(span, len(bits)) == bits).all(axis=1))


def test_write_then_verify_places_the_index(monkeypatch, tmp_path):
    """The pattern start's angle after the index and every capture start's agree
    with where the write put the pattern on the simulated disk (index: cell 0)."""
    cbm, mech, nib = rig(Media({}), halftrack=40, timeout_us=RAM_PASS_US)
    patch(monkeypatch, "1571", nib.mon)
    argv = ["pattern", "write", "--dev", "9", "--halftrack", str(HALFTRACK), *BASE]
    out = cli.main(argv + ["--save", str(tmp_path / "w")], cbm)
    cells = mech.media.tracks[(0, HALFTRACK)]
    truth = pt.Truth.from_json(json.loads((tmp_path / "w" / pattern.TRUTH).read_text()))
    assert truth.lead == out["lead"] == out["stream_bytes"] - len(truth.data)
    (at,) = where(cells, truth.bits)
    argv = ["pattern", "verify", "--dev", "9", "--halftrack", str(HALFTRACK), *BASE]
    v8 = cli.main(argv + ["--repeats", "1", "--cells", str(out["cells"])], cbm)
    index = v8["index"]
    assert index["captures"] == 1 and index["start_offsets"] == [0]
    n = len(cells)
    assert abs((v8["index_bits"] + at + n / 2) % n - n / 2) <= 16
    assert abs(index["pattern_angle"] - at / n) < 1e-3
    assert index["stream_edges"] >= 2 and min(index["stream_edge_offsets"]) >= -16
    angles = [c["start_angle"] for c in v8["captures"]]
    assert all(
        abs((a["index"][0] - a["pattern"][0] - index["pattern_angle"] + 0.5) % 1 - 0.5)
        < 1e-3
        for a in angles
    )
    names = [c["name"] for c in v8["captures"]]
    assert names[-1] == f"ram-index-h{HALFTRACK}.npz"
    stream = next(c for c in v8["captures"] if c["path"] == "stream")
    assert stream["index"]["edges"] == len(stream["index"]["bits"]) >= 2
    assert stream["index"]["drive"] == "done"


def test_write_density_lead_and_weak_region(monkeypatch, tmp_path):
    """A non-zone density measures its own cells per revolution; the lead and
    the weak variant go into truth.json; verify reports each unstable group."""
    cbm, mech, nib = rig(Media({}), halftrack=40, timeout_us=RAM_PASS_US)
    patch(monkeypatch, "1571", nib.mon)
    opts = ["--halftrack", str(HALFTRACK), "--density", "3", "--region", "weak"]
    argv = ["pattern", "write", "--dev", "9", *opts, *BASE, "--lead", "500"]
    out = cli.main(argv + ["--save", str(tmp_path / "w")], cbm)
    cells = mech.media.tracks[(0, HALFTRACK)]
    assert out["cells"] == len(cells) == round(bits_per_revolution(3))
    rec = json.loads((tmp_path / "w" / pattern.TRUTH).read_text())
    assert (rec["lead"], rec["density"], rec["cells"]) == (500, 3, out["cells"])
    assert rec["variant"] == "weak"
    argv = ["pattern", "verify", "--dev", "9", *opts, *BASE, "--repeats", "2"]
    v8 = cli.main(argv + ["--cells", str(out["cells"])], cbm)
    for path in ("stream", "ram"):
        assert v8["summary"][path]["bit_errors"] == v8["summary"][path]["slips"] == 0
    for cap in v8["captures"]:
        for sync in cap["syncs"]:
            off = np.abs(np.array(sync["found"]) - sync["written"])
            assert len(off) and off.max() <= cap["sync_error"]
    groups = {f"{k}{n}" for n in pt.weak_runs(3) for k in ("noflux", "badgcr")}
    assert set(v8["summary"]["unstable_all"]) == groups
    assert all(u["copies"] >= 2 for u in v8["summary"]["unstable_all"].values())
    argv = ["pattern", "write", "--dev", "9", *opts, *BASE, "--lead", "0"]
    with pytest.raises(TrackError, match="leads of"):
        cli.main(argv, cbm)


def test_revolution_stream_lead():
    payload = bytes(range(256)) * 20
    out = disk.revolution_stream(payload, 60000, lead=600)
    assert out[600 : 600 + len(payload)] == payload and len(out) % 256 == 0
    assert set(out[:600] + out[600 + len(payload) :]) == {disk.GAP_BYTE}
    capacity = int(60000 * (1 - disk.MEASURED_TOLERANCE) // 8)
    assert len(out) <= 600 + capacity
    default = disk.revolution_stream(payload, 60000)
    assert default.endswith(payload) and len(default) == len(out)
    with pytest.raises(TrackError, match="leads of"):
        disk.revolution_stream(payload, 60000, lead=0)


@pytest.mark.parametrize("model", ["1571", "1541"])
def test_cells_per_density(monkeypatch, tmp_path, model):
    """Each density's probe measures its cells per revolution; a streaming 1571
    adds the index period, which matches it. Only the scratch halftrack is written."""
    if model == "1571":
        cbm, mech, nib = rig(Media({}), halftrack=40, timeout_us=RAM_PASS_US)
        mon, dev = nib.mon, "9"
    else:
        drive = disk_drive("1541", Media({}), 10, halftrack=40)
        cbm, mech, mon, dev = SimCBM(drive), drive.mech, SimMonitor(drive), "10"
    patch(monkeypatch, model, mon)
    transport = [] if model == "1571" else ["--transport", "s1"]
    argv = ["pattern", "cells", "--dev", dev, "--halftrack", "72", *BASE, *transport]
    out = cli.main(argv + ["--densities", "0", "3", "--save", str(tmp_path)], cbm)
    assert [r["density"] for r in out["densities"]] == [0, 3]
    for row in out["densities"]:
        assert row["cells"] == round(row["nominal"]) and abs(row["rpm"] - 300) < 0.01
        if model == "1571":
            index = row["index"]
            assert index["status"] == 0 and abs(index["period_us"] - 2e5) < 20
            assert abs(index["cells"] - row["cells"]) < 8
        else:
            assert "index" not in row
    written = {k for k, v in mech.media.tracks.items() if v.any()}
    assert (0, 72) in written and mech.bumps == mech.inner_stops == 0
    assert len(list(tmp_path.glob("probe-*.npz"))) == 2
    assert len(list(tmp_path.glob("index-*.npz"))) == 2 * (model == "1571")


CLIPPED = HW_PATTERN / "cells" / "h73-d1-clipped.npz"
WHOLE = HW_PATTERN / "cells" / "h72-d1.npz"


class ProbeNib:
    """Writes nothing; returns saved probe captures in turn."""

    def __init__(self, *paths):
        self.caps = [Capture.load(p) for p in paths]
        self.taken = 0

    def write_track(self, *_, **__):
        """The probe write."""

    def capture(self, *_, **__):
        """The next saved capture."""
        self.taken += 1
        return self.caps[self.taken - 1]


def test_probe_sync_cut_by_the_capture_start_is_not_a_mark():
    """Hardware: a 1541 probe whose timing put a sync at byte 1, its run reaching
    back past the capture start, and whose sync context ends before the next
    pass. Neither sync is whole, so the capture measures nothing."""
    cap = Capture.load(CLIPPED)
    assert cap.positions.tolist() == [1, 6658] and cap.valid_bytes < 6658
    assert cap.latched[0] == 8 * cap.positions[0]
    assert cap.syncs.whole.tolist() == [False, False]
    assert disk.probe_cells(cap) is None
    whole = Capture.load(WHOLE)
    assert whole.syncs.whole.tolist() == [True]
    assert disk.probe_cells(whole) == 8 * 6659 + 41


def test_probe_retakes_a_capture_without_the_next_pass(tmp_path, capsys):
    """A clipped probe capture is retaken (and still saved); the retake measures."""
    nib = ProbeNib(CLIPPED, WHOLE)
    assert disk.revolution_cells(nib, 73, tmp_path, 1) == disk.probe_cells(nib.caps[1])
    assert nib.taken == 2 and "capture 1/3" in capsys.readouterr().err
    assert len(list(tmp_path.glob("probe-s0-t36-*.npz"))) == 2


def test_probe_fails_after_bounded_retakes():
    nib = ProbeNib(*[CLIPPED] * disk.PROBE_TRIES)
    with pytest.raises(TrackError, match="next pass whole"):
        disk.revolution_cells(nib, 73, None, 1)
    assert nib.taken == disk.PROBE_TRIES


def test_halftracks_crosstalk(monkeypatch, tmp_path):
    """The written halftrack matches exactly, a neighbour holding a corrupted
    copy matches partly, noise not at all; halftracks past the reach are skipped."""
    truth = pt.make_truth(HALFTRACK, seed=5)
    n = int(round(bits_per_revolution(truth.density)))
    track = track_of(truth, n - len(truth.bits))
    truth.cells = n
    noisy = track.copy()
    flips = np.random.default_rng(0).choice(n, n // 50, replace=False)
    noisy[flips] = 0
    media = Media({(0, HALFTRACK): track, (0, HALFTRACK - 1): noisy})
    path = tmp_path / pattern.TRUTH
    path.write_text(json.dumps(truth.to_json()))
    cbm, mech, nib = rig(media, halftrack=40, timeout_us=RAM_PASS_US)
    patch(monkeypatch, "1571", nib.mon)
    wanted = [str(h) for h in (HALFTRACK + 2, HALFTRACK, HALFTRACK - 1, 1, 86)]
    argv = ["pattern", "halftracks", "--dev", "9", "--truth", str(path), *BASE]
    out = cli.main(argv + ["--halftracks", *wanted], cbm)
    assert out["max_halftrack"] == MAX_HALFTRACK and out["skipped"] == [1, 86]
    rows = {r["halftrack"]: r["captures"] for r in out["halftracks"]}
    assert list(rows) == [HALFTRACK - 1, HALFTRACK, HALFTRACK + 2]
    assert {c["path"] for c in rows[HALFTRACK]} == {"stream", "ram"}
    for c in rows[HALFTRACK]:
        assert c["found"] >= 1 and c["bit_errors"] <= 8 and c["match"] > 0.9999
    for c in rows[HALFTRACK - 1]:
        assert c["found"] >= 1
        assert 0.9 < c["match"] < 1.0
    for c in rows[HALFTRACK + 2]:
        assert c["found"] == 0 and c["match"] is None
    assert (mech.media.tracks[(0, HALFTRACK)] == track).all()
    assert mech.bumps == mech.inner_stops == 0


def test_hw_stream_saw_no_index():
    """A 1571 stream of the pattern whose drive saw no index edge (END_NOINDEX):
    the pattern aligns, the index is reported missing with the drive's end."""
    root = HW_PATTERN / "h34"
    truth = pt.Truth.from_json(json.loads((root / pattern.TRUTH).read_text()))
    cap = Capture.load(root / "dev8-stream-1.npz")
    assert cap.stream_status == {"adapter": "done", "drive": "noindex"}
    report = pattern.compare(truth, [("stream", cap)])
    assert len(report["captures"]) == 1
    entry = report["captures"][0]
    assert entry["found"] >= 2 and entry["index"]["edges"] == 0
    assert report["index_bits"] is None and "index" not in entry["start_angle"]
    assert report["index"] == {
        "captures": 0,
        "stream_edges": 0,
        "stream_edges_unstable": [],
        "drive_end": ["noindex"],
        "ram_index": [],
        "pattern_angle": None,
    }
    revs = np.array(entry["revolution_bits"])
    assert (np.abs(revs - truth.cells) < truth.cells * pt.UNMEASURED_TOLERANCE).all()


def test_circular_mean_wraps():
    assert pattern.circular_mean([], 100) is None
    mean = pattern.circular_mean([2, 98], 100)
    assert min(mean, 100 - mean) < 1e-9
    assert abs(pattern.circular_mean([20, 30], 100) - 25) < 1e-9


def test_negative_lead_refused():
    argv = ["pattern", "write", "--halftrack", "36", "--lead", "-1"]
    with pytest.raises(SystemExit):
        cli.main(argv)


def test_search_steps_bound_a_lost_1541(monkeypatch):
    """--search-steps steps a 1541 DOS has lost no further outwards than it says."""
    drive = disk_drive("1541", Media({}), 10, halftrack=74)
    drive.write(0x22, 0)
    patch(monkeypatch, "1541", SimMonitor(drive))
    argv = ["pattern", "cells", "--dev", "10", "--halftrack", "72", *BASE]
    with pytest.raises(TrackError, match="within 3 outward"):
        cli.main(argv + ["--transport", "s1", "--search-steps", "3"], SimCBM(drive))
    assert drive.mech.halftrack == 71
    assert drive.mech.bumps == drive.mech.inner_stops == 0


def test_verify_refuses_lead(capsys):
    """verify places the pattern by alignment, so a write's --lead is refused."""
    argv = ["pattern", "verify", "--halftrack", "36", "--lead", "500"]
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2
    assert "--lead is write's only" in capsys.readouterr().err
