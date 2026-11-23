import importlib
import pathlib
import sys

import numpy as np

from nybulah.analysis.gcr import to_bits
from nybulah.analysis.sector import SectorError, decode_track, format_track
from nybulah.formats.g64 import read_g64, write_g64
from nybulah.formats.image import Capture

sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / "tools"))
cg = importlib.import_module("captures_g64")

RNG = np.random.default_rng(7)


def dos_revolution(track=3, into=1600):
    """One revolution of a formatted track, starting ``into`` bits into it
    (inside sector 0's data block)."""
    data = RNG.integers(0, 256, (21, 256), dtype=np.uint8)
    bits = np.asarray(to_bits(format_track(track, data, b"AE")), np.uint8)
    return np.roll(bits, -into)


def captured(rev, start, revs, index_error):
    """``revs`` revolutions of ``rev`` from bit ``start``, index marks off by
    ``index_error`` bits per period."""
    reps = int(np.ceil(revs)) + 2
    bits = np.tile(rev, reps)[start : start + int(revs * len(rev))]
    period = len(rev) + index_error
    first = len(rev) - start % len(rev)
    index = np.arange(first, len(bits), period)
    return Capture(bits, 3, index=index)


def test_index_slice_breaks_the_sector_across_the_seam_sync_cut_does_not():
    rev = dos_revolution()
    cap = captured(rev, 1234, 2.3, index_error=5)
    ix = cap.index
    assert (decode_track(cap.bits[ix[0] : ix[1]], 3).errors != SectorError.OK).any()
    err, cut = cg.best_cut(cap, 3)
    assert err == 0 and len(cut) == len(rev)
    shift = np.flatnonzero(
        [np.array_equal(np.roll(rev, -k)[:256], cut[:256]) for k in range(len(rev))]
    )
    assert len(shift) == 1 and np.array_equal(np.roll(rev, -shift[0]), cut)


def test_sync_cuts_land_inside_syncs():
    rev = dos_revolution()
    cap = captured(rev, 0, 2.3, index_error=-3)
    cuts = cg.sync_cuts(cap.bits, len(rev) - 3)
    assert len(cuts) and (cuts[:, 1] - cuts[:, 0] == len(rev)).all()
    assert cap.bits[cuts[:, 0]].all() and cap.bits[cuts[:, 1]].all()


def test_sync_cuts_need_two_syncs():
    assert cg.sync_cuts(np.zeros(1000, np.uint8), 500).shape == (0, 2)


def test_repeat_cut_recovers_a_syncless_revolution_with_and_without_index():
    rev = (RNG.random(49932) < 0.5).astype(np.uint8)
    rev[rev.cumsum() % 9 == 0] = 0
    cap = captured(rev, 777, 2.5, index_error=4)
    for c in (cap, Capture(cap.bits[:63488], 0)):
        agreement, cut = cg.repeat_cut(c)
        assert agreement == 1.0 and len(cut) == len(rev)
        k = int(
            np.flatnonzero(
                np.all(
                    np.lib.stride_tricks.sliding_window_view(np.tile(rev, 2), 128)[
                        : len(rev)
                    ]
                    == cut[:128],
                    axis=1,
                )
            )[0]
        )
        assert np.array_equal(np.roll(rev, -k), cut)


def test_repeat_cut_refuses_a_capture_that_never_repeats():
    noise = (RNG.random(120_000) < 0.5).astype(np.uint8)
    assert (
        cg.repeat_cut(Capture(noise, 0, index=np.array([0, 50_000, 100_000]))) is None
    )


def test_whole_bytes_trims_the_seam():
    assert len(cg.whole_bytes(np.ones(8 * 10 + 5, np.uint8))) == 10


def test_build_writes_a_g64_whose_tracks_decode():
    rev = dos_revolution()
    captures = {6: [captured(rev, 99, 2.3, index_error=2)]}
    g64, report = cg.build(captures)
    assert report[6]["cut"] == "sync-cut" and report[6]["errors"] == 0
    back = read_g64(write_g64(g64))
    bits = np.unpackbits(back.tracks[6].data)
    assert (decode_track(bits, 3).errors == SectorError.OK).all()
