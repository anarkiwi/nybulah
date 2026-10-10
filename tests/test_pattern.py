"""pattern: write the test track on a simulated 1571, verify it there by stream and
RAM captures and on a simulated 1541 by RAM captures; alignment and slips."""

import contextlib
import functools
import json
import pathlib
import types

import numpy as np
import pytest
from test_stream import RAM_PASS_US, rig

from nybulah import cli, disk, passes, pattern, speed
from nybulah.analysis import pattern as pt
from nybulah.analysis.gcr import bit_rate, bits_per_revolution, decode_bits
from nybulah.analysis.gcr import track_capacity
from nybulah.analysis.sector import SectorError, decode_track
from nybulah.nibbler import BITS, TB, TS, Capture, Nibbler, TrackError
from nybulah.simdisk import Media, disk_drive, true_syncs
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
    cells = len(mech.media.tracks[(0, HALFTRACK)])
    assert abs(out["cells"] - cells) <= 1
    with pytest.raises(ValueError, match="34 outward steps"):
        cli.main(["pattern", "verify", "--dev", "9", *base[:-1], "33"], cbm)

    argv = ["pattern", "verify", "--dev", "9", *base, "--repeats", "1"]
    argv += ["--cells", str(out["cells"]), "--save", str(tmp_path / "v8")]
    v8 = cli.main(argv, cbm)
    assert v8["streaming"] and set(v8["summary"]) == {"stream", "ram", "unstable_all"}
    clean(v8, "ram")
    clean(v8, "stream")
    exact_syncs(v8, "ram")
    assert set(v8["summary"]["ram"]["revolution_bits"]) == {cells}
    ram = next(c for c in v8["captures"] if c["path"] == "ram")
    assert ram["speed"]["excursions"] == [] and len(ram["speed"]["bytes"]) > 1
    assert "index" in ram["start_angle"] and v8["index_bits"] is not None
    assert ram["digest"]["sectors_ok"] == pt.DOS_SECTORS
    truth_vs = ram["digest"]["against"][0]
    assert truth_vs["reference"] == "truth" and truth_vs["sectors"] == pt.DOS_SECTORS
    assert truth_vs["differ"] == 0
    assert v8["summary"]["unstable_all"]["weak"]["copies"] >= 2

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
    assert set(v10["summary"]) == {"ram", "unstable_all"}
    clean(v10, "ram")
    exact_syncs(v10, "ram")
    assert drive.mech.bumps == drive.mech.inner_stops == 0


@pytest.mark.parametrize("variant", pt.VARIANTS)
@pytest.mark.parametrize("density", range(4))
def test_truth_layout(density, variant):
    truth = pt.make_truth(HALFTRACK, density, seed=7, variant=variant)
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
            byte = pt.BAD_GCR if r.group.startswith("badgcr") else 0
            assert (np.packbits(bits[: r.length // 8 * 8]) == byte).all()
            assert not bits[r.length // 8 * 8 :].any()
    unstable = {r.group for r in truth.regions if r.kind == pt.UNSTABLE}
    lengths = {8 * n + 1 for n in pt.weak_runs(density)}
    if variant == "weak":
        assert lengths == {r.length for r in truth.regions if r.kind == pt.UNSTABLE}
        assert len(unstable) == 2 * pt.WEAK_RUNS
        assert (min(lengths) - 1) * pt.CPU_HZ >= pt.T2_SPAN * bit_rate(density)
    else:
        assert unstable == {"weak"}
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
        reads += rep["weak"]["weak"]
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


def merged(tb):
    """A TB pass merged with BITS bytes that hold no sync."""
    syncs = passes.merge_tb(np.full(len(tb), 0x55, np.uint8), 0, tb)
    return types.SimpleNamespace(tb=tb, syncs=syncs)


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
    out = speed.speed_trace(merged(tb_pass(byte * (1 + dev))))
    (x,) = out["excursions"]
    assert abs(x["start"] - s) < out["window"] and x["peak_pct"] > 0
    assert x["lobes"] >= 2 and abs(x["period_ms"] - period) < period / 4
    assert abs(x["decay_ms"] - tau) < tau / 2
    assert speed.speed_trace(merged(tb_pass(np.full(n, byte))))["excursions"] == []
    assert speed.speed_trace(types.SimpleNamespace(tb=None)) is None
    assert speed.excursions(np.full(4, np.nan), byte) == ([], None)


HW_PATTERN = pathlib.Path(__file__).parent / "data" / "hw" / "pattern"
HW_RAM = sorted(HW_PATTERN.glob("*/*-ram-*.npz"))
HW_WEAK = [
    p
    for p in HW_RAM
    if json.loads((p.parent / pattern.TRUTH).read_text()).get("variant") == "weak"
]
HW_STANDARD = [p for p in HW_RAM if p not in HW_WEAK]


def leading(truth):
    """Groups written before the first unstable region."""
    weak = next(r for r in truth.regions if r.kind == pt.UNSTABLE)
    return {r.group for r in truth.regions if r.offset < weak.offset}


def stray_syncs(truth, cap):
    """Pattern regions where restored syncs of a capture end, other than written
    syncs and the weak region."""
    bits, _, begins = pt.capture_bits(cap)
    al = pt.align(bits, truth, period=truth.cells)
    end = al.track_position(np.asarray(begins) - 1)
    end = end[(end >= 0) & (end < len(truth.bits))]
    kinds = np.array([r.kind for r in truth.regions])[truth.kinds()[end]]
    return {
        truth.regions[i].name
        for i, k in zip(truth.kinds()[end], kinds)
        if k not in (pt.SYNC, pt.UNSTABLE)
    }


@pytest.mark.parametrize("path", HW_STANDARD, ids=lambda p: f"{p.parent.name}-{p.stem}")
def test_hw_unstable_region_read_unlike_the_timing_passes(path):
    """1541 and 1571 RAM captures of the pattern whose weak region read into the
    resync's sync in some passes and as bytes in others: the leading regions
    read exactly, every restored sync ends on a written sync or the weak
    region, and no speed excursion lies in the long syncs and weak region."""
    truth = pt.Truth.from_json(json.loads((path.parent / pattern.TRUTH).read_text()))
    cap = Capture.load(path)
    report = pattern.compare(truth, [(path.name, cap)])
    entry = report["captures"][0]
    for name in leading(truth):
        g = entry["groups"][name]
        assert g["errors"] == g["slips"] == 0, name
    resync = region(truth, "resync.sync0")
    lead = (resync.offset + resync.length) / report["revolution_bits"]
    for x in entry["speed"]["excursions"]:
        assert x["angle"]["pattern"][0] > lead


@pytest.mark.parametrize("path", HW_STANDARD, ids=lambda p: f"{p.parent.name}-{p.stem}")
def test_hw_restored_syncs_end_on_written_syncs(path):
    """Every restored sync of a hardware RAM capture ends on a written sync or
    the weak region."""
    truth = pt.Truth.from_json(json.loads((path.parent / pattern.TRUTH).read_text()))
    assert not stray_syncs(truth, Capture.load(path))


@pytest.mark.parametrize("path", HW_WEAK, ids=lambda p: f"{p.parent.name}-{p.stem}")
def test_hw_weak_variant_runs_read_unlike_the_timing_passes(path):
    """1571 RAM captures of the weak variant, its runs read as bytes in some
    passes and as syncs in others: the leading regions read exactly, no exact
    group slips by more than a bit, and the revolution over the stable bits is
    the written one."""
    truth = pt.Truth.from_json(json.loads((path.parent / pattern.TRUTH).read_text()))
    entry = pattern.compare(truth, [(path.name, Capture.load(path))])["captures"][0]
    for name, g in entry["groups"].items():
        if g["kind"] != pt.UNSTABLE:
            assert g["slips"] <= 1, name
            assert name not in leading(truth) or g["errors"] == g["slips"] == 0
    for rev in entry["revolution_bits"]:
        assert abs(rev - truth.cells) < truth.cells * pt.UNMEASURED_TOLERANCE


def test_unstable_region_read_longer_than_the_band():
    """A weak region read far longer than the sync band (a misplaced sync run)
    leaves the stable regions on either side exact, the excess as insertions in
    the weak region."""
    truth = pt.make_truth(HALFTRACK, seed=3)
    track = track_of(truth, 3000)
    weak = region(truth, "weak.0")
    extra = 8 * sum(r.kind == pt.SYNC for r in truth.regions) + 3
    start = 21000
    c = np.roll(np.tile(track, 2), -start)[: len(track) + 9000]
    at = (weak.offset - start) % len(track)
    c = np.insert(c, at + weak.length // 2, np.ones(extra, np.uint8))
    al = pt.align(c, truth)
    rep = pt.region_report(truth, al)
    for name, g in rep["groups"].items():
        if g["kind"] != pt.UNSTABLE:
            assert g["errors"] == g["slips"] == 0, name
    assert rep["groups"]["weak"]["ins"] == extra
    assert al.band > extra


def test_revolution_over_stable_bits_less_unstable_reads():
    """A weak region read longer in the second copy leaves the revolution over
    the stable bits the written one; with no stable bits matched in both copies
    there is none. Stable positions come from stable bits only."""
    truth = pt.make_truth(HALFTRACK, seed=3)
    track = track_of(truth, 3000)
    weak = region(truth, "weak.0")
    c = np.tile(track, 2)
    at = len(track) + weak.offset + weak.length // 2
    c = np.insert(c, at, np.random.default_rng(1).integers(0, 2, 60, dtype=np.uint8))
    al = pt.align(c, truth)
    stable = truth.stable()
    assert not stable[weak.offset : weak.offset + weak.length].any()
    assert al.revolution(len(truth.bits), stable) == [len(track)]
    assert al.revolution(len(truth.bits), np.zeros_like(stable)) == [None]
    probe = np.array([100, len(track) + 100])
    assert al.stable_position(probe, stable).tolist() == [100, 100]


def test_stretch_shifts_skip_partial_and_insignificant_stretches():
    truth = pt.make_truth(HALFTRACK, seed=3)
    track = track_of(truth, 3000)
    weak = region(truth, "weak.0")
    c = np.roll(track, -(weak.offset - 100))
    offsets = pt.placements(c, truth)[0]
    assert pt.stretch_shifts(c, truth, offsets).tolist() == [0]
    noise = np.random.default_rng(2).integers(0, 2, len(c), dtype=np.uint8)
    assert pt.stretch_shifts(noise, truth, offsets).size == 0


def unstable_capture(make_rig, monkeypatch, at, ones):
    """A 1541 RAM capture of the pattern starting ``at`` a pattern bit, its weak
    region and the zero after it reading as ones, into the resync's sync, in
    the passes ``ones``."""
    truth = pt.make_truth(HALFTRACK, seed=3)
    pattern_cells = track_of(truth, int(round(bits_per_revolution(truth.density))))
    pattern_cells = pattern_cells[: int(round(bits_per_revolution(truth.density)))]
    weak = region(truth, "weak.0")
    media = Media({(0, HALFTRACK): pattern_cells.copy()})
    drive, nib = make_rig("1541", media)
    cells = media.tracks[(0, HALFTRACK)]
    run = nib._pass  # pylint: disable=protected-access

    def unstable(kind, mode, anchor=b""):
        if kind == BITS:
            shift[0] = drive.mech._k - at  # pylint: disable=protected-access
            cells[:] = np.roll(pattern_cells, shift[0])
        weak_cells = shift[0] + weak.offset + np.arange(weak.length + 1)
        cells[weak_cells % len(cells)] = kind in ones
        return run(kind, mode, anchor)

    shift = [0]
    monkeypatch.setattr(nib, "_pass", unstable)
    nib.seek(HALFTRACK, truth.density)
    drive.mech.log = []
    return nib.capture(HALFTRACK, truth.density), drive.mech.log


@pytest.mark.parametrize("ones", [(), (BITS,), (TB,), (TS,), (BITS, TS)])
@pytest.mark.parametrize("where", [0, 1, 3])
def test_ram_capture_around_the_long_sync(make_rig, monkeypatch, where, ones):
    """Starting inside the longest sync, in the tag after it or in the weak
    region, and with the weak region read into the resync's sync by some
    passes and as bytes by the rest, every sync of the BITS bytes is found
    within its measured bounds."""
    truth = pt.make_truth(HALFTRACK, seed=3)
    longest = max(range(len(truth.regions)), key=lambda i: truth.regions[i].run)
    start = truth.regions[longest + where]
    at = start.offset + start.length // 2
    cap, log = unstable_capture(make_rig, monkeypatch, at, ones)
    pos, runs = true_syncs(log, cap.data)
    lo, hi = cap.sync_bounds
    assert np.array_equal(cap.positions, pos)
    assert (lo <= runs).all() and ((hi < 0) | (runs <= hi)).all()
