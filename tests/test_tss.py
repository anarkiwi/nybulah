"""1571 sync sensor pass: drive/tss.s on the timed 1571 and its host side."""

import json

import numpy as np
import pytest

from nybulah import cli, streamprobe, tss
from nybulah.analysis.gcr import bits_per_revolution
from nybulah.monitor import drivecode
from nybulah.nibbler import CODE_BASE, Capture, TrackError
from nybulah.simdisk import Media, log_bytes, sync_track, true_syncs

# pylint: disable-next=wrong-import-order
from test_stream import FASTEST, ZONE_TRACKS, probe_rig, rig

TIMEOUT_US = 3_000_000  # the sim adapter's reply wait, over a two-revolution pass
MHZ = 2  # drive cycles per mechanism microsecond


def sense(media, halftrack, revolutions=1, **kw):
    """(mechanism, nibbler, Sensed) after a sensor pass on a fresh rig."""
    _, mech, nib = rig(media, halftrack=halftrack, timeout_us=TIMEOUT_US)
    nib.seek(halftrack)
    mech.log = []
    sensed = nib.sense_syncs(halftrack, revolutions, **kw)
    return mech, nib, sensed


def cell_cycles(media, halftrack, rpm):
    """Drive cycles per cell of the media's track at ``rpm``."""
    return MHZ * 60e6 / rpm / len(media.cells(0, halftrack))


def logged(log):
    """Per logged sync: bytes latched before it, its start, SYNC low cycles, ones."""
    _, times = log_bytes(log)
    syncs = [e for e in log if e[0] == "sync"]
    before = np.searchsorted(times, [e[1] for e in syncs])
    start = np.array([e[1] for e in syncs])
    width = MHZ * np.array([e[2] - e[1] for e in syncs])
    return before, start, width, np.array([e[3] for e in syncs])


def pair(records, log):
    """Logged sync index per sensed sync, aligned by the byte counts; raises when
    the sensed syncs are no subset of the logged ones."""
    before, *_ = logged(log)
    count = records.pulses()[0]
    at = {b: j for j, b in enumerate(before.tolist())}
    for b in before:
        offset = b - count[0]
        hits = [at.get(int(c + offset)) for c in count]
        if offset >= 0 and None not in hits and len(set(hits)) == len(hits):
            return np.array(hits), offset
    raise AssertionError("sensed syncs do not align with the logged syncs")


def check(records, log, cell):
    """Every logged sync of two cells or more inside the pass is sensed, every
    sensed width bounds the true one, stamps follow the log; returns the
    sensed fraction of one-cell syncs."""
    _, start, width, ones = logged(log)
    hits, _ = pair(records, log)
    _, est, lo, hi = records.pulses(cell)
    assert ((lo <= width[hits]) & (width[hits] <= hi)).all()
    assert (np.abs(est - width[hits]) <= (hi - lo) / 2).all()
    inside = np.arange(hits[0], hits[-1] + 1)
    missed = np.setdiff1d(inside, hits)
    assert (ones[missed] == 10).all(), ones[missed]
    assert (width[inside] >= cell * (ones[inside] - 9) - 1e-6).all()
    seen = records.seen[records.is_sync]
    dt = (seen[:-1] - seen[1:]) & 0xFFFF
    true = MHZ * np.diff(start[hits])
    prompt = np.isfinite(hi) & (hi - lo <= tss.SEEN_GAP + tss.RELEASE_GAP)
    close = (true < 0x8000) & prompt[1:] & prompt[:-1]
    assert (np.abs(dt[close] - true[close]) <= tss.SEEN_GAP).all()
    short = inside[ones[inside] == 10]
    return np.isin(short, hits).mean() if len(short) else 1.0


@pytest.mark.parametrize("zone", sorted(ZONE_TRACKS))
@pytest.mark.parametrize("rpm", [300.0, FASTEST])
def test_senses_every_dos_sync_and_the_index(g64, zone, rpm):
    track = ZONE_TRACKS[zone]
    media = Media.from_g64(g64, rpm=rpm)
    mech, _, sensed = sense(media, 2 * track)
    rec = sensed.records
    assert sensed.status == 0 and len(rec.index) == 2
    assert (rec.kind[rec.is_sync] == tss.K_LONG).all()
    check(rec, mech.log, cell_cycles(media, 2 * track, rpm))
    before, start, _, _ = logged(mech.log)
    later = int(np.argmin(np.abs(start - start[0] - 60e6 / rpm)))
    turn = int(np.diff(rec.index)[0])
    assert abs(turn - (before[later] - before[0])) <= 2 * tss.INDEX_POLL_BYTES
    assert mech.wd_violations == 0


@pytest.mark.parametrize("written", [FASTEST, 300.0])
@pytest.mark.parametrize("seed", range(3))
def test_short_syncs_at_the_fastest_zone(seed, written):
    """Runs of 10 to 14 ones between 2 and 30 bytes at zone 3, 310 rpm: every
    two-cell pulse is sensed, one-cell pulses often."""
    rng = np.random.default_rng(seed)
    runs = rng.choice([10, 11, 12, 14, 20, 40], 40).tolist()
    cells = int(round(bits_per_revolution(3, written)))
    media = Media(
        {(0, 2): sync_track(runs, cells, seed=seed, gaps=(2, 30))}, rpm=FASTEST
    )
    mech, _, sensed = sense(media, 2)
    rec = sensed.records
    assert sensed.status == 0
    assert check(rec, mech.log, cell_cycles(media, 2, FASTEST)) >= 0.3
    kinds = np.bincount(rec.kind, minlength=7)
    assert kinds[1] > 0 and kinds[tss.K_LONG] > 0


def test_one_cell_pulses_are_mostly_sensed_at_every_zone():
    runs = [10] * 60
    rates = []
    for zone, track in sorted(ZONE_TRACKS.items()):
        cells = int(round(bits_per_revolution(zone, 300.0)))
        media = Media({(0, 2 * track): sync_track(runs, cells, seed=zone)}, rpm=FASTEST)
        mech, _, sensed = sense(media, 2 * track)
        rates.append(
            check(sensed.records, mech.log, cell_cycles(media, 2 * track, FASTEST))
        )
    assert min(rates) >= 0.5


def test_sensed_syncs_are_placed_on_the_stream():
    """A stream and a sensor pass of the same track: the sensor's syncs land on
    the stream's byte positions, the runs within the sensed bounds."""
    rng = np.random.default_rng(5)
    runs = rng.choice([10, 11, 12, 40], 40).tolist()
    cells = int(round(bits_per_revolution(3, 300.0)))
    media = Media({(0, 2): sync_track(runs, cells, seed=5, gaps=(2, 30))}, rpm=FASTEST)
    _, mech, nib = rig(media, halftrack=2, timeout_us=TIMEOUT_US)
    mech.log = []
    cap = nib.stream(2)
    stream_only = cap.positions.copy()
    cap = nib.sense_syncs(2).attach(cap)
    pos, true_runs = true_syncs(mech.log, cap.data)
    want = dict(zip(pos.tolist(), true_runs.tolist()))
    assert set(cap.positions.tolist()) <= set(want)
    assert len(cap.positions) > len(stream_only)
    lo, hi = cap.sync_bounds
    for p, run, l, h in zip(cap.positions, cap.sync_bits, lo, hi):
        assert (
            l <= want[int(p)] <= (h if h >= 0 else np.inf) and abs(run - want[p]) <= 2
        )
    assert cap.lost <= 1 and cap.records is not None  # a sync at the data's end


def test_capture_record_round_trips_with_the_sensor(g64, tmp_path):
    _, _, nib = rig(Media.from_g64(g64), timeout_us=TIMEOUT_US)
    cap = nib.stream(36)
    cap = nib.sense_syncs(36).attach(cap)
    cap.save(tmp_path / "s.npz")
    back = Capture.load(tmp_path / "s.npz")
    assert (back.sensed == cap.sensed).all() and back.sensed_status == 0
    assert (back.positions == cap.positions).all()
    assert (back.sync_bits == cap.sync_bits).all()


def test_killer_track_exhausts_the_wait_budget():
    cells = int(round(bits_per_revolution(2, 300.0)))
    _, _, sensed = sense(Media({(0, 36): np.ones(cells, np.uint8)}), 36, budget=4)
    assert sensed.status == tss.ST_KILLER and len(sensed.records.kind) == 0


def test_full_records_end_the_pass(g64, monkeypatch):
    monkeypatch.setattr(tss, "REC_END_PAGE", tss.REC_PAGE + 1)
    _, _, sensed = sense(Media.from_g64(g64), 36, revolutions=3)
    rec = sensed.records
    assert sensed.status == tss.ST_FULL
    assert len(rec.kind) == 256 // tss.RECLEN and (rec.count[1:] > 0).all()


def test_silent_drive_times_out(g64):
    """Motor off: no byte ready, so the idle bodies run the budget out."""
    _, _, nib = rig(Media.from_g64(g64), timeout_us=TIMEOUT_US)
    nib.mon.write(CODE_BASE, drivecode("tss_1571"))
    nib.mon.write(tss.ZP, bytes([0, 2, tss.REC_END_PAGE]))
    nib.mon.set_fast(True)
    assert nib.mon.jsr(CODE_BASE)[0] == tss.ST_TIMEOUT
    nib.mon.set_fast(False)


def test_refused_at_1_mhz_and_on_a_1541(g64, make_rig):
    _, _, nib = rig(Media.from_g64(g64))
    nib.mon.write(CODE_BASE, drivecode("tss_1571"))
    assert nib.mon.jsr(CODE_BASE)[0] == tss.ST_SLOW
    _, nib41 = make_rig("1541")
    with pytest.raises(TrackError, match="needs a 1571"):
        nib41.sense_syncs(36)


def test_records_parse_stamps_and_counts():
    rec = bytes([2, 0x05, 0x12, 0, 0, 7, 1, 0]) + bytes(
        [5, 0x50, 0x10, 0x20, 0x10, 3, 0, 0]
    )
    rec += bytes([6, 0x01, 0x00, 0, 0, 1, 0, 0])
    r = tss.Records.parse(rec)
    assert r.kind.tolist() == [2, 5, 6] and r.count.tolist() == [263, 3, 1]
    assert r.cumulative.tolist() == [263, 266, 267] and r.index.tolist() == [267]
    assert r.seen[0] == ((0x12 + 1) << 8 | 0x05) + 13
    assert r.seen[1] == 0x1050 + 13 and r.end[1] == 0x1020 + 7
    assert r.seen[2] == 0x0101
    count, est, lo, hi = r.pulses()
    assert count.tolist() == [263, 266]
    assert (lo[0], hi[0]) == (7, 17 + tss.SEEN_GAP)
    assert (lo[1], hi[1]) == (54 - 11, 54 + tss.SEEN_GAP) and est[1] == (
        lo[1] + hi[1]
    ) / 2
    _, _, _, wide = r.pulses(2.0)
    assert wide[1] == hi[1] + tss.TAIL_BLIND - 8 * 2.0 * 3 and wide[0] == hi[0]
    first = tss.Records.parse(bytes([3, 0x05, 0x12, 0, 0, 1, 0, 0]))
    assert np.isinf(first.pulses()[3][0]) and first.pulses()[1][0] == tss.PREV[3]
    assert tss.status_names(tss.ST_FULL | tss.ST_KILLER) == ["killer", "full"]


def test_syncprobe_streams_then_senses(monkeypatch, capsys, g64, tmp_path):
    cbm, mech = probe_rig(monkeypatch, Media.from_g64(g64), 9, timeout_us=TIMEOUT_US)
    path = tmp_path / "p.npz"
    args = ["syncprobe", "--dev", "9", "--halftrack", "4", "--save", str(path)]
    out = cli.main(args + ["--table"], cbm)
    assert json.loads(capsys.readouterr().out) == out
    assert out["adapter"] == out["drive"] == "done" and out["status"] == []
    assert out["sensed"] > 0 and out["index_records"] == 2
    assert out["syncs"] >= out["stream_syncs"] and out["unmatched"] <= 1
    assert all(row["cells"] > 0 for row in out["table"])
    back = Capture.load(path)
    assert back.records is not None and len(back.positions) == out["syncs"]
    assert mech.halftrack == 4 and mech.bumps == 0


def test_syncprobe_refuses_other_models(monkeypatch, g64):
    cbm, _ = probe_rig(monkeypatch, Media.from_g64(g64), 36, timeout_us=TIMEOUT_US)
    monkeypatch.setattr(streamprobe, "identify_model", lambda cbm, dev: "1541")
    with pytest.raises(ValueError, match="needs a 1571"):
        cli.main(["syncprobe", "--dev", "9"], cbm)
