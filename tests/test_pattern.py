"""pattern: write the test track on a simulated 1571, verify it there by stream and
RAM captures and on a simulated 1541 by RAM captures; alignment and slips."""

import contextlib
import functools
import json
import types

import numpy as np
import pytest
from test_stream import RAM_PASS_US, rig

from nybulah import cli, disk, passes, pattern, speed
from nybulah.analysis import pattern as pt
from nybulah.analysis.gcr import decode_bits, track_capacity
from nybulah.analysis.sector import SectorError, decode_track
from nybulah.nibbler import Nibbler, TrackError
from nybulah.simdisk import Media, disk_drive
from nybulah.simhost import SimCBM, SimMonitor

HALFTRACK = 36


def patch(monkeypatch, model, mon):
    monkeypatch.setattr(pattern, "identify_model", lambda cbm, dev: model)
    monkeypatch.setattr(
        pattern, "Monitor", lambda cbm, dev, proto: contextlib.nullcontext(mon)
    )
    monkeypatch.setattr(
        pattern,
        "Nibbler",
        functools.partial(Nibbler, stepms=1, spinup_s=0, sleep=lambda s: None),
    )


def clean(report, path):
    """Every GCR-valid region read without bit errors or slips, every sync found."""
    s = report["summary"][path]
    assert s["bit_errors"] == 0 and s["slips"] == 0
    for cap in report["captures"]:
        if cap["path"] != path:
            continue
        assert cap["found"] >= 1
        for g in cap["groups"].values():
            assert g["errors"] == 0 and g["slips"] == 0 and g["bits"] > 0
        for sync in cap["syncs"]:
            off = np.abs(np.array(sync["found"]) - sync["written"])
            assert len(off) and off.max() <= cap["sync_error"]


def exact_syncs(report, path):
    for cap in report["captures"]:
        if cap["path"] == path:
            for sync in cap["syncs"]:
                assert set(sync["found"]) == {sync["written"]}


def test_write_then_verify_on_1571_and_1541(monkeypatch, capsys, tmp_path):
    cbm, mech, nib = rig(Media({}), halftrack=40, timeout_us=RAM_PASS_US)
    patch(monkeypatch, "1571", nib.mon)
    base = ["--halftrack", str(HALFTRACK), "--settle-ms", "1", "--max-steps", "40"]
    out = cli.main(["pattern", "write", "--dev", "9", *base], cbm)
    assert json.loads(capsys.readouterr().out) == json.loads(json.dumps(out))
    assert out["located"] == 2 and mech.bumps == mech.inner_stops == 0
    assert out["cells"] == len(mech.media.tracks[(0, HALFTRACK)])
    with pytest.raises(ValueError, match="34 outward steps"):
        cli.main(["pattern", "verify", "--dev", "9", *base[:-1], "33"], cbm)

    argv = ["pattern", "verify", "--dev", "9", *base, "--repeats", "1"]
    argv += ["--cells", str(out["cells"]), "--save", str(tmp_path / "v8")]
    v8 = cli.main(argv, cbm)
    assert v8["streaming"] and set(v8["summary"]) == {"stream", "ram", "weak_all"}
    clean(v8, "ram")
    clean(v8, "stream")
    exact_syncs(v8, "ram")
    assert v8["summary"]["ram"]["revolution_bits"] == [out["cells"]]
    ram = next(c for c in v8["captures"] if c["path"] == "ram")
    assert ram["speed"]["excursions"] == [] and len(ram["speed"]["bytes"]) > 1
    assert "index" in ram["start_angle"] and v8["index_bits"] is not None
    assert ram["digest"]["sectors_ok"] == pt.DOS_SECTORS
    truth_vs = ram["digest"]["against"][0]
    assert truth_vs["reference"] == "truth" and truth_vs["sectors"] == pt.DOS_SECTORS
    assert truth_vs["differ"] == 0
    assert v8["summary"]["weak_all"]["copies"] >= 2

    saved = sorted(str(p) for p in (tmp_path / "v8").glob("*.npz"))
    argv = ["pattern", "compare", "--truth", str(tmp_path / "v8" / pattern.TRUTH)]
    offline = cli.main(argv + saved)
    assert offline["summary"] == json.loads(json.dumps(v8["summary"]))

    media = Media({(0, HALFTRACK): mech.media.tracks[(0, HALFTRACK)].copy()})
    drive = disk_drive("1541", media, 10, halftrack=50)
    patch(monkeypatch, "1541", SimMonitor(drive))
    argv = ["pattern", "verify", "--dev", "10", "--transport", "s1", *base]
    v10 = cli.main(argv + ["--repeats", "2"], SimCBM(drive))
    assert not v10["streaming"] and v10["located"] == 50
    assert set(v10["summary"]) == {"ram", "weak_all"}
    clean(v10, "ram")
    exact_syncs(v10, "ram")
    assert drive.mech.bumps == drive.mech.inner_stops == 0


@pytest.mark.parametrize("density", range(4))
def test_truth_layout(density):
    truth = pt.make_truth(HALFTRACK, density, seed=7)
    assert len(truth.bits) % 8 == 0
    assert len(truth.data) <= track_capacity(density) * (1 - pt.UNMEASURED_TOLERANCE)
    ends = [r.offset + r.length for r in truth.regions]
    assert [r.offset for r in truth.regions] == [0] + ends[:-1]
    assert ends[-1] == len(truth.bits)
    runs = [r.run for r in truth.regions if r.kind == pt.SYNC]
    assert runs[:4] == pt.sync_runs(density) and runs[0] == pt.SYNC_MIN_BITS
    for r in truth.regions:
        bits = truth.bits[r.offset : r.offset + r.length]
        if r.kind == pt.SYNC:
            assert bits.all() and r.run == r.length
        if r.kind == pt.UNSTABLE:
            assert not bits.any()
    gcr = next(r for r in truth.regions if r.group == "gcr_all")
    data, valid = decode_bits(truth.bits[gcr.offset : gcr.offset + gcr.length])
    assert valid.all() and (data == np.arange(256)).all()
    dec = decode_track(truth.bits, truth.track, None, pt.DOS_SECTORS)
    assert (dec.errors == SectorError.OK).all()
    again = pt.Truth.from_json(json.loads(json.dumps(truth.to_json())))
    assert (again.bits == truth.bits).all() and again.regions == truth.regions
    other = pt.make_truth(HALFTRACK, density, seed=8)
    assert (other.bits != truth.bits).any()


def track_of(truth, filler):
    return np.concatenate((truth.bits, pt.gap_bits(filler)))


def region(truth, name):
    return next(r for r in truth.regions if r.name == name)


def test_alignment_reports_flips_slips_and_sync_lengths():
    truth = pt.make_truth(HALFTRACK, seed=3)
    filler = 2871
    track = track_of(truth, filler)
    start = 21000
    c = np.roll(np.tile(track, 2), -start)[: len(track) + 9000].copy()

    def at(name, k=0):
        """c index of bit k of a region's second pass in c."""
        r = region(truth, name)
        return (r.offset + k - start) % len(track)

    flip, drop, add = at("random.0", 500), at("gcr_all.0", 300), at("dos.1", 100)
    sync = region(truth, "sync40.sync0")
    long_sync = at("sync40.sync0", 3)
    edits = sorted([(flip, "flip"), (drop, "drop"), (add, "add"), (long_sync, "one")])
    for pos, kind in reversed(edits):
        if kind == "flip":
            c[pos] ^= 1
        elif kind == "drop":
            c = np.delete(c, pos)
        else:
            c = np.insert(c, pos, 1)
    al = pt.align(c, truth)
    assert al.found == 2 and len(al.placements) == 2
    rep = pt.region_report(truth, al)
    g = rep["groups"]
    assert g["random"]["errors"] == 1 and g["random"]["slips"] == 0
    assert g["gcr_all"]["del"] == 1 and g["gcr_all"]["errors"] == 0
    assert g["dos"]["ins"] == 1 and g["dos"]["errors"] == 0
    assert g["sync40"]["slips"] == 0 and g["sync40"]["errors"] == 0
    found = next(s for s in rep["syncs"] if s["region"] == sync.name)["found"]
    assert found == [sync.run + 1]
    for name in ("sync10", "sync75", "resync", "gap55"):
        assert g[name]["errors"] == g[name]["slips"] == 0
    assert al.revolution(len(truth.bits)) == [len(track) + 1]
    assert rep["gap55_framing"]["expected"] is not None


def test_weak_region_instability_and_track_positions():
    truth = pt.make_truth(HALFTRACK, seed=3)
    track = track_of(truth, 3000)
    weak = region(truth, "weak.0")
    rng = np.random.default_rng(0)
    reads = []
    for k in range(3):
        c = np.roll(track, -k * 1000).copy()
        lo = (weak.offset - k * 1000) % len(track)
        c[lo : lo + weak.length] = rng.integers(0, 2, weak.length)
        al = pt.align(c, truth)
        rep = pt.region_report(truth, al)
        assert rep["groups"]["weak"]["errors"] == 0
        reads += rep["weak"]
        assert al.track_position([lo]).tolist() == [weak.offset]
    out = pt.instability(reads, weak.length)
    assert out["copies"] == 3 and out["unstable_bits"] > 0
    assert 0 < out["disagreement"] < 1 and out["spans"] == [weak.length] * 3
    assert pt.instability([], weak.length) == {"copies": 0}


def test_noise_has_no_placement():
    truth = pt.make_truth(HALFTRACK)
    c = np.random.default_rng(1).integers(0, 2, len(truth.bits), dtype=np.uint8)
    al = pt.align(c, truth)
    assert al.found == 0 and al.placements.size == 0
    assert al.track_position([3]).tolist() == [-1]
    assert pt.region_report(truth, al)["groups"]["random"]["bits"] == 0


def test_revolution_stream_bounds():
    payload = bytes(range(256)) * 20
    out = disk.revolution_stream(payload, 60000)
    assert out.endswith(payload) and len(out) % 256 == 0
    assert len(out) * 8 >= 60000 * (1 + disk.MEASURED_TOLERANCE)
    with pytest.raises(TrackError, match="exceed a"):
        disk.revolution_stream(payload, 8 * len(payload))
    with pytest.raises(TrackError, match="drive RAM"):
        disk.revolution_stream(payload, 70000)


def tb_pass(byte_cycles, seed=0):
    """A TB pass of bytes arriving byte_cycles apart, read by the drive's loop."""
    n = len(byte_cycles)
    arrive = np.cumsum(byte_cycles) + np.random.default_rng(seed).uniform(0, 1)
    body = np.where(np.arange(n) % 256 == 255, passes.TB_PAGE_BODY, passes.TB_BODY)
    _, ready = passes.tb_schedule(4 * int(np.max(byte_cycles)))
    read = np.zeros(n + 1)
    for i in range(n):
        wait = read[i] + body[i]
        read[i + 1] = wait + ready[np.searchsorted(ready, arrive[i] - wait)]
    return ((200 - np.rint(read).astype(np.int64)) % 256).astype(np.uint8)


def test_speed_excursion_period_and_decay():
    n, byte = 7900, 30.0
    t = np.arange(n) * byte / 1000
    dev = np.zeros(n)
    s, tau, period = 3000, 20.0, 28.0
    dev[s:] = (
        0.035
        * np.exp(-(t[s:] - t[s]) / tau)
        * np.sin(2 * np.pi * (t[s:] - t[s]) / period)
    )
    cap = types.SimpleNamespace(
        tb=tb_pass(byte * (1 + dev)), base=0, positions=np.array([1000, 5000])
    )
    out = speed.speed_trace(cap)
    (x,) = out["excursions"]
    assert abs(x["start"] - s) < out["window"] and x["peak_pct"] > 0
    assert x["lobes"] >= 2 and abs(x["period_ms"] - period) < period / 4
    assert abs(x["decay_ms"] - tau) < tau / 2
    steady = types.SimpleNamespace(tb=tb_pass(np.full(n, byte)), base=0, positions=[])
    assert speed.speed_trace(steady)["excursions"] == []
    assert speed.speed_trace(types.SimpleNamespace(tb=None)) is None
    assert speed.excursions(np.full(4, np.nan), byte) == ([], None)
