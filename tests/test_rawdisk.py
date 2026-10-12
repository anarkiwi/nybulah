import numpy as np
import pytest

from nybulah import disk, rawdisk
from nybulah.analysis.gcr import encode, runs_of_ones, to_bits, to_bytes
from nybulah.analysis.regions import agreement, canonical
from nybulah.analysis.sector import SectorError, decode_track
from nybulah.formats import G64, G64Track
from nybulah.formats.image import load, to_g64
from nybulah.nibbler import TrackError

FILL = 0x52


def protected_track(syncs, length, seed=0):
    """Non-DOS track: each sync (in 0xFF bytes) followed by a marker, GCR data and
    FILL bytes, the fill sharing out ``length``; returns the bytes and the data."""
    rng = np.random.default_rng(seed)
    blocks = [encode(rng.integers(0, 256, 320, dtype=np.uint8)) for _ in syncs]
    room = length - sum(syncs) - sum(len(b) + 1 for b in blocks)
    fills = np.diff(np.linspace(0, room, len(syncs) + 1).astype(int))
    parts = [
        np.concatenate(([0xFF] * k, [FILL], b, [FILL] * f)).astype(np.uint8)
        for k, b, f in zip(syncs, blocks, fills)
    ]
    return np.concatenate(parts), blocks


def contains(bits, part):
    """``part`` bits appear in the circular ``bits``."""
    ref = np.concatenate((bits, bits[: len(part)]))
    hits = np.lib.stride_tricks.sliding_window_view(ref, len(part))
    return bool((hits == part).all(axis=1).any())


def same_cycle(a, b):
    """Two circular bit streams equal up to rotation, syncs cut to SYNC_MIN_BITS."""
    a, b = canonical(a), canonical(b)
    return len(a) == len(b) and agreement(a, b)[0] == 1.0


def test_shave():
    assert rawdisk.shave([5, 3, 8], 2, 6).tolist() == [1, 0, 5]
    assert rawdisk.shave([5, 3, 8], 2, 0).tolist() == [0, 0, 0]
    assert rawdisk.shave([9, 9, 9], 2, 4).sum() == 4


def test_sync_floor_matches_the_stream_guarantee():
    # protocol.md, Sync detection: guaranteed ones at 310 rpm, zones 3..0
    assert [rawdisk.sync_floor(z, True) for z in (3, 2, 1, 0)] == [28, 27, 26, 25]
    ram = [rawdisk.sync_floor(z) for z in range(4)]
    assert ram == sorted(ram) and min(ram) > rawdisk.SYNC_MIN_BITS


def test_fit_dos_track(g64):
    data = g64.tracks[2].data
    runs = runs_of_ones(to_bits(data), circular=True)[1]
    few = rawdisk.fit_track(data, len(data) - 10)
    after = runs_of_ones(to_bits(few), circular=True)[1]
    assert len(few) == len(data) - 10 and runs.sum() - after.sum() == 80
    assert after.max() - after.min() <= 1
    assert rawdisk.gap_fill(few) == 0x55
    for src in (data, to_bytes(np.roll(to_bits(data), 3))):
        fitted = rawdisk.fit_track(src, len(src) - 200, 23)
        assert len(fitted) == len(src) - 200
        assert runs_of_ones(to_bits(fitted), circular=True)[1].min() >= 23
        dec = decode_track(to_bits(fitted), 1)
        assert (dec.errors == SectorError.OK).all()
    assert rawdisk.fit_track(data, len(data) + 5) is data
    with pytest.raises(TrackError, match="do not fit"):
        rawdisk.fit_track(data, 6000, 23)


def test_fit_protected_track_keeps_its_data():
    track, blocks = protected_track([3, 8, 20, 50], 7800)
    fitted = rawdisk.fit_track(track, 7500, 23)
    bits = to_bits(fitted)
    assert len(fitted) == 7500 and rawdisk.gap_fill(fitted) == FILL
    assert all(contains(bits, to_bits(b)) for b in blocks)
    runs = runs_of_ones(bits, circular=True)[1]
    assert len(runs) == 4 and runs.min() >= 23 and runs.max() <= 24


def test_gap_fill_without_gaps():
    assert rawdisk.gap_fill(np.full(100, 0xFF, np.uint8)) == 0xFF
    assert rawdisk.gap_fill(np.full(100, 0x52, np.uint8)) == rawdisk.GAP_BYTE


def test_write_jobs_skip_halftracks():
    image = G64({k: G64Track(np.zeros(4, np.uint8)) for k in (2, 3, 4, 0x82)})
    jobs, skipped = rawdisk.write_jobs(image)
    assert [j.key for j in jobs] == [2, 4, 0x82] and skipped == [3]
    assert jobs[-1].header == 36


def test_raw_jobs():
    assert [j.halftrack for j in rawdisk.raw_jobs(2, halftracks=True)] == [2, 3, 4, 5]
    jobs = rawdisk.raw_jobs(35, (0, 1))
    assert len(jobs) == 70 and jobs[35].key == 0x82 and jobs[35].header == 36


def test_raw_round_trip(make_rig, g64, tmp_path):
    long, blocks = protected_track([3, 8, 20, 50, 3, 8, 20, 50], 7800, seed=1)
    nosync = to_bytes(to_bits(encode(np.arange(4000) % 256))[: 8 * 5000])
    image = G64(
        {
            2: G64Track(g64.tracks[2].data, 3),
            4: G64Track(long, 3),
            6: G64Track(np.full(7000, 0xFF, np.uint8), 3),
            8: G64Track(nosync, 3),
            48: G64Track(g64.tracks[48].data, 2),
        }
    )
    drive, nib = make_rig("1541")
    report = rawdisk.write_g64(nib, image, progress=False)
    assert [t["attempts"] for t in report["tracks"]] == [1] * 5
    assert report["tracks"][1]["trimmed"] > 0 and not disk.failed(report)
    media = {k: drive.mech.media.tracks[(0, k)] for k in image.tracks}
    assert all(contains(media[4], to_bits(b)) for b in blocks)
    assert media[6].all() and contains(media[8], to_bits(nosync))
    jobs = [j for j in rawdisk.raw_jobs(24) if j.key in (2, 4, 6, 48)]
    caps = tmp_path / "caps"
    back = to_g64(rawdisk.read_raw(nib, jobs, archive=caps, progress=False))
    for key in (2, 4, 48):
        assert same_cycle(to_bits(back.tracks[key].data), media[key])
    assert (back.tracks[6].data == 0xFF).all()
    dec = decode_track(to_bits(back.tracks[48].data), 24)
    assert (dec.errors == SectorError.OK).all()
    loaded = load(caps)
    assert loaded.kind == "capture" and sorted(loaded.tracks) == [2, 4, 6, 48]
    again = to_g64(loaded)
    assert all((again.tracks[k].data == back.tracks[k].data).all() for k in back.tracks)


def test_verify_catches_corrupt_write(make_rig, g64):
    drive, nib = make_rig("1541")
    drive.mech.corrupt = lambda key, cell, bit: (
        bit ^ 1 if key == (0, 4) and 3000 <= cell < 3008 else bit
    )
    image = G64({2: G64Track(g64.tracks[2].data, 3), 4: G64Track(g64.tracks[4].data)})
    report = rawdisk.write_g64(nib, image, retries=1, progress=False)
    assert disk.failed(report) == [
        {
            "side": 0,
            "track": 2,
            "attempts": None,
            "trimmed": report["tracks"][1]["trimmed"],
        }
    ]


def test_align_index_reproduces_track_starts(make_rig, g64, tmp_path):
    drive, nib = make_rig("1571")
    keys = (2, 62, 0x82)
    image = G64({k: g64.tracks[k & 0x7F] for k in keys})
    report = rawdisk.write_g64(nib, image, "index", archive=tmp_path, progress=False)
    assert not disk.failed(report)
    assert len(list(tmp_path.glob("verify-*.npz"))) == 3
    cells0 = 60.0 * rawdisk.bit_rate(0) / report["rpm"]
    starts = []
    for key in keys:
        density, payload, _, _ = rawdisk.plan(image.tracks[key], cells0, True)
        media = drive.mech.media.tracks[(key >> 7, key & 0x7F)]
        head = to_bits(payload[:64])
        window = media[: 2 * rawdisk.ALIGN_BITS + len(head)]
        hits = np.lib.stride_tricks.sliding_window_view(window, len(head))
        found = np.flatnonzero((hits == head).all(axis=1))
        assert found.size == 1, key
        starts.append(found[0] / rawdisk.bit_rate(density))
    assert np.ptp(starts) <= 8 / rawdisk.bit_rate(0)


def test_refusals(make_rig, g64):
    drive, nib = make_rig("1541")
    image = G64({2: G64Track(g64.tracks[2].data, 3)})
    with pytest.raises(ValueError, match="no index sensor"):
        rawdisk.write_g64(nib, image, "index", progress=False)
    with pytest.raises(ValueError, match="align"):
        rawdisk.write_g64(nib, image, "sync", progress=False)
    with pytest.raises(ValueError, match="second side"):
        rawdisk.write_g64(nib, G64({0x82: image.tracks[2]}), progress=False)
    with pytest.raises(ValueError, match="no whole tracks"):
        rawdisk.write_g64(nib, G64({3: image.tracks[2]}), progress=False)
    with pytest.raises(ValueError, match="G71"):
        rawdisk.read_g64(nib, 35, (0, 1))
    assert not drive.mech.media.tracks


def test_verify_catches_a_misaligned_write(make_rig, g64):
    _, nib = make_rig("1571")
    write_track = nib.write_track

    def unaligned(*args, **kw):
        return write_track(*args[:4], **kw)

    nib.write_track = unaligned
    image = G64({2: g64.tracks[2]})
    report = rawdisk.write_g64(nib, image, "index", retries=0, progress=False)
    assert [t["attempts"] for t in report["tracks"]] == [None]
