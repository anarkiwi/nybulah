import json
import pathlib

import numpy as np
import pytest

from nybulah import cli
from nybulah.analysis.gcr import runs_of_ones, to_bits
from nybulah.analysis.sector import SectorError, decode_track, format_track
from nybulah.formats import assemble as asm
from nybulah.formats.g64 import G64, G64Track, read_g64, write_g64
from nybulah.formats.image import Capture
from nybulah.imagecmd import sectors_spec, tracks_spec

RNG = np.random.default_rng(7)
HW = pathlib.Path(__file__).parent / "data" / "hw"
SYNC_BITS = 40
OK = SectorError.OK


def sectors(n=21):
    return RNG.integers(0, 256, (n, 256), dtype=np.uint8)


def dos_bits(track, data, bad=()):
    errs = np.full(len(data), OK, np.uint8)
    errs[list(bad)] = SectorError.DATA_CHECKSUM
    return np.asarray(to_bits(format_track(track, data, b"AE", errs)), np.uint8)


def dos_revolution(track=3, into=1600, data=None, bad=()):
    """One revolution of a formatted track, starting ``into`` bits into it
    (inside sector 0's data block)."""
    data = sectors() if data is None else data
    return np.roll(dos_bits(track, data, bad), -into)


def captured(rev, start, revs, index_error):
    """``revs`` revolutions of ``rev`` from bit ``start``, index marks off by
    ``index_error`` bits per period."""
    reps = int(np.ceil(revs)) + 2
    bits = np.tile(rev, reps)[start : start + int(revs * len(rev))]
    period = len(rev) + index_error
    first = len(rev) - start % len(rev)
    index = np.arange(first, len(bits), period)
    return Capture(bits, 3, index=index)


def syncs(bits):
    return runs_of_ones(bits, circular=True)[1]


def g64_bits(g64, track):
    return np.unpackbits(np.asarray(g64.tracks[2 * track].data, np.uint8))


def write_image(path, tracks):
    """G64 at ``path`` of ``{track: (data, bad sectors, rotation bits)}``."""
    g64 = G64()
    for track, (data, bad, roll) in tracks.items():
        bits = np.roll(dos_bits(track, data, bad), -roll)
        g64.tracks[2 * track] = G64Track(np.packbits(bits), 3 - (track > 17))
    path.write_bytes(write_g64(g64))
    return path


def test_index_slice_breaks_the_sector_across_the_seam_sync_cut_does_not():
    rev = dos_revolution()
    cap = captured(rev, 1234, 2.3, index_error=5)
    ix = cap.index
    assert (decode_track(cap.bits[ix[0] : ix[1]], 3).errors != OK).any()
    err, cut = asm.best_cut(cap, 3)
    assert err == 0 and len(cut) == len(rev)
    shift = np.flatnonzero(
        [np.array_equal(np.roll(rev, -k)[:256], cut[:256]) for k in range(len(rev))]
    )
    assert len(shift) == 1 and np.array_equal(np.roll(rev, -shift[0]), cut)


def test_sync_cuts_land_inside_syncs():
    rev = dos_revolution()
    cap = captured(rev, 0, 2.3, index_error=-3)
    cuts = asm.sync_cuts(cap.bits, len(rev) - 3)
    assert len(cuts) and (cuts[:, 1] - cuts[:, 0] == len(rev)).all()
    assert cap.bits[cuts[:, 0]].all() and cap.bits[cuts[:, 1]].all()


def test_sync_cuts_need_two_syncs():
    assert asm.sync_cuts(np.zeros(1000, np.uint8), 500).shape == (0, 2)
    assert asm.best_cut(Capture(np.zeros(1000, np.uint8), 3), 3) is None


@pytest.mark.parametrize("index", [None, np.zeros(0, np.int64)])
def test_assemble_sync_cuts_a_capture_without_index_marks(index):
    data = sectors()
    rev = dos_revolution(data=data)
    cap = captured(rev, 2345, 2.1, index_error=0)
    cap.index = index
    assert asm.capture_period(cap) == len(rev)
    g64, report = asm.assemble({6: [cap]})
    assert report[6]["cut"] == "sync-cut" and report[6]["errors"] == 0
    bits = g64_bits(g64, 3)
    assert len(bits) == len(rev) and rotation_of(rev, bits) >= 0
    assert np.array_equal(np.roll(rev, -rotation_of(rev, bits)), bits)
    assert np.array_equal(decode_track(bits, 3).data, data)


def syncless(n=49932):
    rev = (RNG.random(n) < 0.5).astype(np.uint8)
    rev[rev.cumsum() % 9 == 0] = 0
    return rev


def rotation_of(rev, cut):
    windows = np.lib.stride_tricks.sliding_window_view(np.tile(rev, 2), 128)
    return int(np.flatnonzero(np.all(windows[: len(rev)] == cut[:128], axis=1))[0])


@pytest.mark.parametrize("track", [3, 20, 27, 33])
def test_header_period_is_the_revolution_without_index(track):
    rev = dos_revolution(
        track, data=sectors(17 if track > 30 else 21 if track < 18 else 18)
    )
    cap = captured(rev, 777, 2.3, index_error=0)
    cap = Capture(cap.bits, cap.zone)
    assert abs(asm.header_period(cap.bits, track) - len(rev)) <= 2
    assert abs(asm.capture_period(cap, track) - len(rev)) <= 2
    assert asm.header_period(cap.bits, track + 1) is None


def test_best_cut_never_takes_a_cut_holding_a_sector_twice(monkeypatch):
    rev = dos_revolution()
    cap = Capture(captured(rev, 777, 2.3, index_error=0).bits, 3)
    monkeypatch.setattr(asm, "capture_period", lambda c, t=None: int(len(rev) * 1.15))
    assert asm.best_cut(cap, 3) is None
    monkeypatch.undo()
    err, cut = asm.best_cut(cap, 3)
    assert err == 0 and len(cut) == len(rev)
    assert (decode_track(cut, 3).copies == 1).all()


def test_repeat_cut_recovers_a_syncless_revolution_with_and_without_index():
    rev = syncless()
    cap = captured(rev, 777, 2.5, index_error=4)
    for c in (cap, Capture(cap.bits[:63488], 0)):
        agreement, cut = asm.repeat_cut(c)
        assert agreement == 1.0 and len(cut) == len(rev)
        assert np.array_equal(np.roll(rev, -rotation_of(rev, cut)), cut)


def test_repeat_cut_refuses_a_capture_that_never_repeats():
    noise = (RNG.random(120_000) < 0.5).astype(np.uint8)
    capture = Capture(noise, 0, index=np.array([0, 50_000, 100_000]))
    assert asm.repeat_cut(capture) is None
    assert asm.repeat_cut(Capture(noise[:2000], 0)) is None


def test_whole_and_sync_bytes_round_a_revolution_at_its_seam():
    assert len(asm.whole_bytes(np.ones(8 * 10 + 5, np.uint8))) == 10
    rev = np.concatenate([np.ones(7, np.uint8), np.zeros(60, np.uint8), [1] * 6])
    out = np.unpackbits(asm.sync_bytes(rev))
    assert len(out) == 80 and np.array_equal(out[: len(rev)], rev)
    assert syncs(out).tolist() == [20]


def test_assemble_writes_a_g64_whose_tracks_decode():
    data = sectors()
    rev = dos_revolution(data=data)
    g64, report = asm.assemble({6: [captured(rev, 99, 2.3, index_error=2)]})
    assert report[6]["cut"] == "sync-cut" and report[6]["errors"] == 0
    bits = g64_bits(read_g64(write_g64(g64)), 3)
    decoded = decode_track(bits, 3)
    assert (decoded.errors == OK).all() and np.array_equal(decoded.data, data)


def test_assemble_splices_bad_sectors_from_other_captures_at_their_syncs():
    data = sectors()
    caps = [
        captured(dos_revolution(data=data, bad=[5]), 300, 2.3, index_error=3),
        captured(dos_revolution(data=data, bad=[9]), 4000, 2.3, index_error=-2),
    ]
    g64, report = asm.assemble({6: caps})
    row = report[6]
    assert row["cut"] == "sync-cut" and row["errors"] == 0
    assert row["spliced"] in ([5], [9]) and "unspliced" not in row
    bits = g64_bits(g64, 3)
    decoded = decode_track(bits, 3)
    assert (decoded.errors == OK).all() and np.array_equal(decoded.data, data)
    assert len(syncs(bits)) == 42 and syncs(bits).min() >= SYNC_BITS


def test_assemble_reports_a_sector_no_capture_holds():
    data = sectors()
    caps = [captured(dos_revolution(data=data, bad=[5]), 300, 2.3, index_error=3)]
    _, report = asm.assemble({6: caps})
    assert report[6]["errors"] == 1 and report[6]["spliced"] == []
    assert report[6]["unspliced"] == [{"sector": 5, "reason": "not decodable in donor"}]


def test_assemble_repeat_cuts_a_syncless_track():
    rev = syncless()
    _, report = asm.assemble({70: [captured(rev, 777, 2.5, index_error=4)]})
    assert report[70]["cut"] == "repeat-cut" and report[70]["agreement"] == 1.0
    assert report[70]["bytes"] == len(rev) // 8


def test_sector_span_runs_mid_sync_to_mid_sync_across_the_wrap():
    data = sectors()
    rev = dos_revolution(data=data, into=1600)
    decoded = decode_track(rev, 3)
    for sector in (0, 20):
        start, stop = asm.sector_span(rev, int(decoded.offsets[sector]), True)
        assert 0 <= start < len(rev) and start < stop
        assert rev[start] and rev[stop % len(rev)]
        piece = decode_track(np.take(rev, np.arange(start, stop), mode="wrap"), 3)
        assert np.flatnonzero(piece.errors == OK).tolist() == [sector]
        assert np.array_equal(piece.data[sector], data[sector])
    assert asm.sector_span(rev, -1, True) is None
    assert asm.sector_span(np.zeros(100, np.uint8), 0, False) is None


def test_replace_span_keeps_bit_zero_and_the_length_modulo_8():
    rev = np.arange(64) % 2
    out = asm.replace_span(rev, (10, 20), np.full(13, 7))
    assert len(out) == 72 and out[:10].tolist() == rev[:10].tolist()
    assert out[10:23].tolist() == [7] * 13 and out[23:28].tolist() == [1] * 5
    assert out[28:].tolist() == rev[20:].tolist()
    wrapped = asm.replace_span(rev, (60, 70), np.full(10, 7))
    assert wrapped[:10].tolist() == [7] * 10 and len(wrapped) == 64


def test_splice_reports_a_donor_whose_sector_has_no_closing_sync():
    data = sectors()
    good = dos_revolution(data=data, into=0)
    end = int(decode_track(good, 3).offsets[20]) + 8 * 360
    target = dos_revolution(data=data, into=0, bad=[20])
    _, done, failed = asm.splice(target, 3, [(good[:end], False)], [20, 30])
    assert not done and failed == [(20, "sync not found in donor")] + [
        (30, "no such sector")
    ]


@pytest.fixture(name="donor")
def donor_fixture(tmp_path):
    good = {3: sectors(), 5: sectors(), 20: sectors(19)}
    path = write_image(
        tmp_path / "donor.g64",
        {3: (good[3], (), 0), 5: (good[5], [12], 0), 20: (good[20], (), 777 * 8)},
    )
    return path, good


@pytest.fixture(name="target")
def target_fixture(tmp_path):
    mine = {3: sectors(), 5: sectors(), 20: sectors(19), 21: sectors(19)}
    path = write_image(
        tmp_path / "copy.g64",
        {
            3: (mine[3], [4], 0),
            5: (mine[5], [6, 8], 200),
            20: (mine[20], [0], 1600 // 8 * 8),
            21: (mine[21], (), 0),
        },
    )
    return path, mine


def test_patch_takes_tracks_and_splices_sectors_from_a_donor(donor, target):
    (donor_path, good), (target_path, mine) = donor, target
    g64 = read_g64(target_path.read_bytes())
    before = {k: np.asarray(t.data).copy() for k, t in g64.tracks.items()}
    report = asm.patch(
        g64,
        {},
        [(donor_path, [3])],
        [(donor_path, {5: [6, 8], 20: [0, 18]})],
    )
    assert report[6] == {
        "track": 3.0,
        "errors": 0,
        "bytes": len(g64.tracks[6].data),
        "cut": "take",
        "source": "donor.g64",
    }
    assert np.array_equal(decode_track(g64_bits(g64, 3), 3).data, good[3])
    for track, picked in ((5, [6, 8]), (20, [0, 18])):
        assert report[2 * track]["errors"] == 0 and "failed" not in report[2 * track]
        assert report[2 * track]["patched"] == [
            {"sector": s, "from": "donor.g64"} for s in picked
        ]
        bits = g64_bits(g64, track)
        decoded = decode_track(bits, track)
        assert (decoded.errors == OK).all()
        keep = np.setdiff1d(np.arange(len(decoded.data)), picked)
        assert np.array_equal(decoded.data[picked], good[track][picked])
        assert np.array_equal(decoded.data[keep], mine[track][keep])
        assert syncs(bits).min() >= SYNC_BITS
    assert np.array_equal(g64.tracks[42].data, before[42]) and 42 not in report
    head = 8 * 200
    assert np.array_equal(g64_bits(g64, 5)[:head], np.unpackbits(before[10])[:head])


def test_patch_reports_every_failure(donor, target):
    (donor_path, _), (target_path, _) = donor, target
    g64 = read_g64(target_path.read_bytes())
    report = asm.patch(
        g64,
        {},
        [(donor_path, [21])],
        [(donor_path, {5: [12], 21: [1], 30: [0]})],
    )
    reason = {
        k: [(f.get("sector"), f["reason"]) for f in r["failed"]]
        for k, r in report.items()
    }
    assert reason == {
        42: [(None, "no track in donor"), (1, "no track in donor")],
        10: [(12, "not decodable in donor")],
        60: [(0, "no track in target")],
    }
    assert report[10]["errors"] == 2 and report[10]["patched"] == []


def test_patch_reports_a_target_sector_without_a_header(donor):
    donor_path, _ = donor
    g64 = G64()
    errs = np.full(21, OK, np.uint8)
    errs[2] = SectorError.HEADER_NOT_FOUND
    gcr = format_track(3, sectors(), b"AE", errs)
    g64.tracks[6] = G64Track(gcr, 3)
    report = asm.patch(g64, {}, (), [(donor_path, {3: [2]})])
    assert report[6]["failed"][0]["reason"] == "sync not found in target"


def test_specs_parse_lists_and_ranges():
    assert tracks_spec("a:b.g64:12,13,36-38") == (
        pathlib.Path("a:b.g64"),
        [12, 13, 36, 37, 38],
    )
    assert sectors_spec("ref.nib:12/6,17/0-2,12/8") == (
        pathlib.Path("ref.nib"),
        {12: [6, 8], 17: [0, 1, 2]},
    )
    with pytest.raises(Exception):
        sectors_spec("ref.nib:12")
    with pytest.raises(Exception):
        tracks_spec("12,13")


def test_patch_command_writes_the_patched_image(donor, target, tmp_path, capsys):
    (donor_path, good), (target_path, _) = donor, target
    out_path = tmp_path / "out.g64"
    out = cli.main(
        [
            "patch",
            str(target_path),
            str(out_path),
            "--tracks-from",
            f"{donor_path}:3",
            "--sectors-from",
            f"{donor_path}:5/6,5/8",
        ]
    )
    assert out["ok"] and [r["track"] for r in out["tracks"]] == [3.0, 5.0]
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines[-1] == {"target": str(out_path), "ok": True}
    back = read_g64(out_path.read_bytes())
    assert np.array_equal(decode_track(g64_bits(back, 5), 5).data[6], good[5][6])
    failed = cli.main(
        [
            "patch",
            str(target_path),
            str(out_path),
            "--sectors-from",
            f"{donor_path}:5/12",
        ]
    )
    assert failed["ok"] is False


def test_merged_reads_directories_and_globs_with_later_sources_winning():
    caps = asm.merged([HW / "s4-1571" / "ram-h*.npz"], 42)
    assert {k: len(v) for k, v in caps.items()} == {36: 3, 50: 3}
    caps = asm.merged(
        [HW / "s4-1571" / "ram-h*.npz", HW / "s4-1571/stream-h36.npz"], 42
    )
    assert {k: len(v) for k, v in caps.items()} == {36: 1, 50: 3}
    assert max(asm.merged([HW], 10)) // 2 == 10


def test_assemble_command_cuts_streams_in_syncs(tmp_path, capsys):
    out_path = tmp_path / "out.g64"
    out = cli.main(["assemble", str(out_path), str(HW / "s4-1571" / "stream-*.npz")])
    assert out["ok"] and [r["cut"] for r in out["tracks"]] == ["sync-cut"] * 2
    back = read_g64(out_path.read_bytes())
    for row in out["tracks"]:
        track = int(row["track"])
        assert row["errors"] == 0 and row["bytes"] == len(back.tracks[2 * track].data)
        bits = g64_bits(back, track)
        assert (decode_track(bits, track).errors == OK).all()
        assert syncs(bits).min() >= 10
    assert len(capsys.readouterr().out.splitlines()) == 3
