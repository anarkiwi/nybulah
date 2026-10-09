import io
import json
import sys
import types
import zipfile

import numpy as np
import pytest

from nybulah import cli, scenarios, survey
from nybulah.analysis import gcr
from nybulah.analysis.cycle import TrackKind
from nybulah.analysis.sector import (
    GAP_BYTE,
    HEADER_GAP_BYTES,
    HEADER_GCR_BYTES,
    SECTOR_BYTES,
    SYNC_BYTES,
    SectorError,
    format_track,
)
from nybulah.analysis.synth import simulate_capture
from nybulah.formats import Nib, NibEntry, loads, to_g64, write_g64, write_nib
from nybulah.formats.nib import NIB_TRACK

DISK_ID = b"AB"
BAM_ID = b"XY"
ERROR_TRACK, TAIL_TRACK, FAT_TRACK = 5, 7, 34
KILLER_HT, NOISE_HT, NOSYNC_HT = 72, 74, 76
BLOCK_START = 2 * SYNC_BYTES + HEADER_GCR_BYTES + HEADER_GAP_BYTES


def _sectors(track, seed):
    data = np.random.default_rng(seed).integers(
        0, 256, (gcr.sectors_per_track(track), 256), np.uint8
    )
    if track == 18:
        data[0, 0xA2:0xA4] = list(BAM_ID)
    return data


def _sector_start(track, sector):
    n = gcr.sectors_per_track(track)
    gap = (gcr.track_capacity(gcr.speed_zone(track)) - n * SECTOR_BYTES) // n
    return sector * (SECTOR_BYTES + gap)


def _gcr(track):
    errors = None
    if track == ERROR_TRACK:
        errors = np.full(gcr.sectors_per_track(track), SectorError.OK, np.uint8)
        errors[[2, 4]] = SectorError.DATA_CHECKSUM, SectorError.HEADER_NOT_FOUND
    out = format_track(track, _sectors(track, track), DISK_ID, errors)
    if track == TAIL_TRACK:
        out[_sector_start(track, 1) + BLOCK_START + 323] = 0
    return out


def _nib_disk():
    """Standard tracks 1-35 with errors, a tail-only bad GCR block, a fat 34/34.5/35,
    a half track, a killer, a noise and a no-sync track."""
    rng = np.random.default_rng(1)
    entries = []
    for half in range(2, 78):
        track = half // 2
        zone = gcr.speed_zone(min(track, 35))
        if half in (KILLER_HT, NOISE_HT, NOSYNC_HT):
            bits = {
                KILLER_HT: np.ones(8 * NIB_TRACK, np.uint8),
                NOISE_HT: rng.integers(0, 2, 8 * NIB_TRACK, np.uint8),
                NOSYNC_HT: simulate_capture(
                    gcr.encode_bits(rng.integers(0, 256, 5000, np.uint8)), 8 * NIB_TRACK
                ),
            }[half]
        elif track > 35 or (half % 2 and track not in (1, FAT_TRACK)):
            continue
        else:
            source = FAT_TRACK if FAT_TRACK <= half / 2 <= FAT_TRACK + 1 else track
            bits = simulate_capture(gcr.to_bits(_gcr(source)), 8 * NIB_TRACK)
        entries.append(NibEntry(half, zone, gcr.to_bytes(bits)))
    return Nib(entries)


def _zip(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buf.getvalue()


@pytest.fixture(name="corpus", scope="module")
def corpus_fixture(tmp_path_factory):
    root = tmp_path_factory.mktemp("corpus")
    nib = write_nib(_nib_disk())
    g64 = write_g64(to_g64(loads(nib)))
    inner = _zip({"disk.nib": nib, "disk.g64": g64, "notes.txt": b"x"})
    (root / "outer.zip").write_bytes(_zip({"set/inner.zip": inner, "bad.zip": b"no"}))
    (root / "broken.nib").write_bytes(b"MNIB-1541-RAW" + bytes(19))
    (root / "corrupt.zip").write_bytes(b"PK no zip")
    return root


@pytest.fixture(name="rows", scope="module")
def rows_fixture(corpus):
    items = survey.list_items(corpus, progress=False)
    nib = next(i for i in items if i[-1] == "disk.nib")
    jobs = dict(survey.pair_jobs(items))
    meta, rows, lengths, sync_rows = survey.run_job(str(corpus), nib, jobs[nib])
    assert meta["error"] == "" and meta["fmt"] == "nib"
    assert meta["disk_id"] == int.from_bytes(DISK_ID, "big")
    assert meta["cosmetic_id"] == int.from_bytes(BAM_ID, "big")
    assert len(lengths) == len(sync_rows) == rows["n_sync"].sum()
    return {int(r["key"]): r for r in rows}


def test_listing_and_pairing(corpus):
    items = survey.list_items(corpus, progress=False)
    assert sorted(i[-1] for i in items) == ["broken.nib", "disk.g64", "disk.nib"]
    nested = next(i for i in items if i[-1] == "disk.nib")
    assert nested == ("outer.zip", "set/inner.zip", "disk.nib")
    assert survey.read_item(corpus, nested)[:13] == b"MNIB-1541-RAW"
    jobs = dict(survey.pair_jobs(items))
    assert jobs[nested][-1] == "disk.g64" and jobs[("broken.nib",)] is None
    meta = survey.run_job(str(corpus), ("broken.nib",))[0]
    assert meta["error"].startswith("ValueError") and meta["tracks"] == -1


def test_standard_tracks(rows):
    for track in (1, 18, 25, 31):
        row = rows[2 * track]
        bits = 8 * gcr.track_capacity(gcr.speed_zone(track))
        assert row["kind"] == TrackKind.FORMATTED and row["in_window"]
        assert row["hdr_period"] == row["cycle_len"] == row["rev_len"] == bits
        assert row["n_sect"] == row["n_sect_cap"] == gcr.sectors_per_track(track)
        assert row["n_sync"] == 2 * row["n_sect"] and row["sync_min"] >= 40
        assert row["errors_cap"][SectorError.OK] == row["n_sect"]
        assert row["gap_top"] == GAP_BYTE and row["n_extra"] == row["n_dupe"] == 0
        assert row["hdr_track"] == track and row["hdr_track_frac"] == 1
        assert row["pair_agree"] == 1 and row["pair_len"] == bits
        assert row["sim_next"] < 0.9 and row["bad_span"] == 0


def test_error_and_tail_tracks(rows):
    errors = rows[2 * ERROR_TRACK]["errors_cap"]
    assert (
        errors[SectorError.DATA_CHECKSUM] == errors[SectorError.HEADER_NOT_FOUND] == 1
    )
    tail = rows[2 * TAIL_TRACK]
    assert tail["errors"][SectorError.BAD_GCR] == 1 and tail["n_gcr_tail"] == 1
    assert tail["errors_cap"][SectorError.OK] == 21 and tail["n_gcr_payload"] == 0


def test_protection_tracks(rows):
    assert rows[KILLER_HT]["kind"] == TrackKind.KILLER
    assert (
        rows[NOISE_HT]["kind"] == TrackKind.UNFORMATTED and rows[NOISE_HT]["n_hdr"] == 0
    )
    assert rows[NOISE_HT]["bad_span"] > 1000
    nosync = rows[NOSYNC_HT]
    assert nosync["kind"] == TrackKind.FORMATTED and nosync["n_sync"] == 0
    assert nosync["cycle_len"] == 5000 * 10 and nosync["gap_bytes"] > 0
    fat = rows[2 * FAT_TRACK]
    assert fat["sim_next"] == fat["sim_half"] == 1
    assert rows[2 * FAT_TRACK + 2]["hdr_track"] == FAT_TRACK
    assert rows[2]["sim_half"] == 1 and np.isnan(rows[3]["sim_next"])


def test_multipass_weak_region():
    track_bits = gcr.to_bits(_gcr(18))
    data = np.zeros((4, 4, NIB_TRACK), np.uint8)
    for k in range(4):
        cap = simulate_capture(track_bits, 8 * NIB_TRACK, weak=(20000, 400), rng=k)
        data[2, k] = gcr.to_bytes(cap)
    image = loads(write_nib(Nib([NibEntry(36, 2, data)], 2, False, 4)))
    rows = survey.survey_image(image)[0]
    assert rows["captures"][0] == 4 and 0 < rows["mp_disagree"][0] < 0.01
    assert 300 <= rows["mp_span"][0] <= 400 + 2 * survey.GROUP_BITS


def test_primitives():
    bits = np.array([0, 1] + [1] * 40 + [0, 1, 0] * 3, np.uint8)
    assert survey.canonical(bits).tolist() == [0] + [1] * 10 + [0, 1, 0] * 3
    assert len(survey.canonical(np.ones(50, np.uint8))) == gcr.SYNC_MIN_BITS
    starts, ends = np.array([0, 10, 100]), np.array([5, 20, 110])
    assert survey.longest_chain(starts, ends, link=8) == 20
    assert survey.longest_chain(starts[:0], ends[:0]) == 0
    rng = np.random.default_rng(0)
    ref = rng.integers(0, 2, 5000, np.uint8)
    probe = np.roll(ref, -1234)
    probe[100:110] ^= 1
    agree, z, lag, miss = survey.agreement(ref, probe)
    assert lag == 1234 and agree == 1 - 10 / 5000 and z > 60
    assert np.flatnonzero(miss).tolist() == list(range(100, 110))
    assert np.isnan(survey.agreement(ref, ref[:0])[0])
    assert survey.ROTATION_CLASS[0xAA] == survey.ROTATION_CLASS[0x55] == 0x55
    assert survey.header_period(
        np.zeros(0, np.int64), np.zeros((0, 8), np.uint8), 100
    ) == (
        -1,
        0,
        0,
    )


def test_dos_errors_tail_and_payload():
    track = gcr.to_bits(_gcr(TAIL_TRACK))
    assert (survey.dos_errors(track, TAIL_TRACK) == SectorError.OK).all()
    payload = track.copy()
    payload[8 * (_sector_start(TAIL_TRACK, 2) + BLOCK_START + 100) :][:16] = 0
    errors = survey.dos_errors(payload, TAIL_TRACK)
    assert errors[2] == SectorError.BAD_GCR
    assert (np.delete(errors, 2) == SectorError.OK).all()


def test_scan_resume_summary_and_cli(corpus, tmp_path, monkeypatch, capsys):
    out = tmp_path / "survey"
    assert survey.scan(corpus, out, workers=2, progress=False) == 3
    assert survey.scan(corpus, out, workers=2, progress=False) == 0
    data = survey.load_survey(out)
    assert len(data["image_key"]) == 3 and (data["image_error"] != "").sum() == 1
    assert data["sync_row"].max() < len(data["tracks"])
    nib = np.flatnonzero(data["image_fmt"] == "nib")[0]
    assert data["image_pair"][nib].endswith("disk.g64")

    record = types.SimpleNamespace(
        halftrack=36, side=0, density=2, bits=lambda: gcr.to_bits(_gcr(18))
    )
    module = types.SimpleNamespace(
        Capture=types.SimpleNamespace(load=lambda path: record)
    )
    monkeypatch.setitem(sys.modules, "nybulah.nibbler", module)
    caps = tmp_path / "caps"
    caps.mkdir()
    (caps / "read-s0-t18-0.npz").write_bytes(b"")
    result = cli.main(
        [
            "survey",
            str(corpus),
            "--out",
            str(out),
            "--workers",
            "1",
            "--captures",
            str(caps),
        ]
    )
    assert result["surveyed"] == 0 and json.loads(capsys.readouterr().out) == result
    summary = json.loads((out / "summary.json").read_text())
    assert summary["images"]["distinct"] == 2 and summary["images"]["failed"] == 1
    assert summary["disk_ids"] == {"track18_read": 2, "bam_id_differs": 2}
    found = {k: v["prevalence"]["tracks"] for k, v in summary["scenarios"].items()}
    assert found["killer"] == 2 and found["no_sync"] == 2 and found["unformatted"] >= 1
    assert found["fat_track"] >= 2 and found["header_track_mismatch"] >= 2
    assert found["half_track_crosstalk"] == 4
    assert summary["scenarios"]["dos_with_errors"]["errors"] == {
        "0": 38,
        "20": 2,
        "23": 2,
    }
    assert summary["pairs"]["agreement"][0] == 1
    assert summary["cycle_all_linear"]["vs_period"]["exact"] == 1
    reference = summary["reference"]
    assert reference["tracks"] == 1 and reference["errors_capture"] == {"0": 19}


def test_illegal_fill_classes():
    fill = scenarios.ILLEGAL_FILL
    assert fill[0x00] and fill[0x11] and fill[0x88] and not fill[-1]
    assert not fill[0x55] and not fill[0x52] and not fill[0xFF]


def test_summarise_empty():
    assert scenarios.summarise({"tracks": np.zeros(0, survey.TRACK_DTYPE)}) == {
        "images": 0
    }
    assert (
        scenarios.cycle_stats(np.zeros(0, survey.TRACK_DTYPE), np.zeros(0, bool))
        is None
    )
