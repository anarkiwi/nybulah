import pathlib

import numpy as np
import pytest

from nybulah import survey
from nybulah.analysis import fluxsynth
from nybulah.analysis.fluxsynth import synthetic_flux_disk, track_flux
from nybulah.analysis.fluxview import (
    KERNEL_CELLS,
    box_integral,
    flux_disk,
    spans_mask,
    standard_zone,
)
from nybulah.analysis.synth import synthetic_disk
from nybulah.formats.image import Capture, DiskImage, flux_capture
from nybulah.nibbler import Capture as Record

BINS = 2048
HW = pathlib.Path(__file__).parent / "data" / "hw"
CENTRES = (np.arange(BINS) + 0.5) / BINS


@pytest.fixture(name="flux", scope="module")
def flux_fixture():
    image, truth = synthetic_flux_disk()
    disk = flux_disk(image)
    return disk, disk.raster(BINS), {t[1]: t for t in truth}


@pytest.fixture(name="bits", scope="module")
def bits_fixture():
    image, truth = synthetic_disk()
    disk = flux_disk(image, keys={6, 20, 24, 40, 41, 62})
    return disk, disk.raster(BINS), {t[1]: t for t in truth}


def _inside(start, end, margin=0.0):
    return (CENTRES >= start - margin) & (CENTRES < end + margin)


def test_box_integral_matches_dense_sampling():
    rng = np.random.default_rng(3)
    t = np.sort(rng.uniform(0, 50, 40))
    x = np.linspace(-1, 51, 300)
    fine = np.linspace(-2, 52, 540_001)
    density = (np.abs(fine[:, None] - t[None]) < KERNEL_CELLS / 2).sum(1) / KERNEL_CELLS
    dense = np.interp(x, fine, np.cumsum(density) * (fine[1] - fine[0]))
    assert np.allclose(box_integral(t, x), dense, atol=2e-3)


def test_spans_mask_wraps():
    mask = spans_mask(10, [8, 2], [12, 3])
    assert mask.tolist() == [1, 1, 1, 0, 0, 0, 0, 0, 1, 1]


def test_density_conserves_transitions(flux):
    disk, raster, _ = flux
    for key in (2, 30, 62):
        rev = disk.tracks[key].revs[0]
        cells = raster[key]["density"][0] * rev.period / BINS
        assert cells.sum() == pytest.approx(len(rev.trans), rel=1e-9)


def test_long_sync_and_killer_reverse_every_cell(flux):
    _, raster, truth = flux
    _, _, start, end, _, _ = truth["long_sync"]
    density = raster[6]["density"].mean(axis=0)
    assert density[_inside(start + 1 / BINS, end - 1 / BINS)].min() > 0.95
    assert density[~_inside(start, end, 2 / BINS)].mean() < 0.7
    assert raster[62]["density"].min() > 0.98


def test_measured_no_flux_is_dark_and_not_inferred(flux):
    _, raster, truth = flux
    _, _, start, end, _, _ = truth["no_flux"]
    inside = _inside(start + 1 / BINS, end - 1 / BINS)
    assert raster[10]["density"][:, inside].max() < 0.05
    assert raster[10]["noflux"].max() == 0
    assert raster[10]["delta"][:, inside].max() == 0


def test_weak_span_varies_between_revolutions(flux):
    _, raster, truth = flux
    _, _, start, end, _, _ = truth["weak"]
    var = raster[14]["var"].mean(axis=0)
    assert var[_inside(start, end)].mean() > 0.5
    assert np.median(var[~_inside(start, end, 4 / BINS)]) < 0.01


def test_wobble_shows_as_cell_length(flux):
    _, raster, truth = flux
    amplitude = truth["wobble"][-1]
    delta = raster[2]["delta"].mean(axis=0)
    model = amplitude * np.sin(2 * np.pi * CENTRES)
    assert np.corrcoef(delta, model)[0, 1] > 0.98
    fit = (delta @ model) / (model @ model)
    assert fit == pytest.approx(1, abs=0.1)


def test_density_change_measured_against_standard_zone(flux):
    disk, raster, truth = flux
    _, _, start, end, _, value = truth["density"]
    delta = raster[52]["delta"].mean(axis=0)
    assert disk.tracks[52].ref.timing == "flux"
    assert delta[_inside(start + 2 / BINS, end - 2 / BINS)].mean() == pytest.approx(
        value, abs=0.005
    )
    assert np.abs(delta[_inside(4 / BINS, start - 4 / BINS)]).max() < 0.01


def test_jitter_widens_intervals_not_cell_length(flux):
    disk, raster, _ = flux

    def spread(key):
        gaps = np.concatenate([np.diff(r.trans) for r in disk.tracks[key].revs])
        return np.std(gaps - np.rint(gaps))

    ratio = spread(30) / spread(28)
    assert ratio == pytest.approx(fluxsynth.NOISY_JITTER / fluxsynth.JITTER, rel=0.2)
    assert np.abs(raster[30]["delta"]).mean() < 0.002


def test_crosstalk_correlates_with_both_neighbours(flux):
    disk, _, _ = flux
    fine = disk.raster(1 << 16)

    def corr(a, b):
        return np.corrcoef(fine[a]["density"][0], fine[b]["density"][0])[0, 1]

    assert min(corr(41, 40), corr(41, 42)) > 1.5 * max(corr(41, 44), corr(40, 42))
    assert fine[41]["var"].mean() > 0.5


def test_slip_faults_only_its_revolution(flux):
    _, raster, truth = flux
    _, _, start, _, rev, _ = truth["slip"]
    near = _inside(start - 8 / BINS, start + 24 / BINS)
    fault = raster[24]["fault"]
    assert fault[rev, near].max() > 0
    others = np.delete(np.arange(len(fault)), rev)
    assert fault[others][:, near].max() == 0


def test_eye_and_intervals(flux):
    disk, _, _ = flux
    edges = np.arange(0, 8.25, 0.25)
    eye = disk.eye(2, 16, edges)
    assert eye.sum() == sum(len(r.trans) - 1 for r in disk.tracks[2].revs)
    rows = eye.sum(axis=1)
    assert rows[edges[:-1] == 1.0].item() > 0 and rows[edges[:-1] >= 3.5].sum() == 0
    measured, inferred = disk.intervals()[3]
    assert measured.size and not inferred.size
    assert disk.sources() == {"flux": 4 * len(disk.tracks)}


def test_drift_follows_wobble(flux):
    disk, _, _ = flux
    _, cells = disk.tracks[2].drift(0, 512)
    written = np.linspace(0, 1, 512, endpoint=False)
    period = disk.tracks[2].ref.period
    model = fluxsynth.WOBBLE * period / (2 * np.pi) * (1 - np.cos(2 * np.pi * written))
    assert np.abs(cells - model).max() < 2


def test_inferred_no_flux_from_bits(bits):
    disk, raster, truth = bits
    _, _, start, end, _ = truth["NOFLUX_SPAN"]
    n = disk.tracks[20].ref.n
    inside = _inside(start / n + 2 / BINS, end / n - 2 / BINS)
    rows = raster[20]
    assert rows["noflux"][:, inside].min() > 0.9
    assert rows["density"][:, inside].max() < 0.1
    assert rows["noflux"][:, ~_inside(start / n, end / n, 2 / BINS)].max() == 0
    assert rows["var"][:, inside].mean() > 0.3


def test_bit_sources_time_by_revolution(bits):
    disk, raster, _ = bits
    assert set(disk.sources()) == {"track"}
    assert np.abs(raster[6]["delta"]).max() < 1e-3
    expected = (16 - 3) / (16 - standard_zone(40)) - 1
    assert np.median(raster[40]["delta"]) == pytest.approx(expected, abs=1e-3)
    _, cells = disk.tracks[24].drift(2, 256)
    assert set(np.unique(np.round(cells))) <= {-1.0, 0.0, 1.0}


def test_track_flux_slip_adds_one_cell():
    bits = np.tile(np.array([1, 0, 1, 1, 0], np.uint8), 2000)
    times, index = track_flux(
        bits, 3, 2, np.random.default_rng(0), jitter=0, slip=(1, 5000)
    )
    cap = flux_capture(times, index)
    assert cap.zone == 3
    first, second = np.diff(cap.index)
    assert second - first == 0
    gaps = np.diff(times[times > index[1]]) / (4 * 13)
    assert np.isclose(gaps, np.rint(gaps)).all() and gaps.max() == 3


def test_tb_capture_is_timed_per_byte():
    rec = Record.load(HW / "s4-1571" / "ram-h36-0.npz")
    from nybulah.analysis.capture import segments

    image = DiskImage("c", {36: [Capture(segments(rec).bits, rec.density, framed=rec)]})
    rev = flux_disk(image).tracks[36].ref
    assert rev.timing == "tb" and len(rev.knots[0]) > 1000
    assert rev.period / rev.n == pytest.approx(300.0 / rec.rpm, abs=2e-3)
    assert 0 < np.median(rev.knots[2]) < 0.5


def test_two_reads_of_a_blank_disk_agree():
    caps = {}
    for side in ("a", "b"):
        for key, found in survey.load_captures(HW / "dev10" / side).items():
            caps.setdefault(key, []).extend(found)
    disk = flux_disk(DiskImage("c", caps), keys={2, 36, 70})
    raster = disk.raster(512)
    for key, track in disk.tracks.items():
        assert len(track.revs) >= 2
        assert 0.4 < raster[key]["density"].mean() < 0.7
        assert raster[key]["var"].mean() < 0.02


def test_stream_with_index_is_index_aligned():
    caps = survey.load_captures(HW / "s4-1571", "stream-*.npz")
    disk = flux_disk(DiskImage("c", caps))
    for track in disk.tracks.values():
        assert all(r.indexed and r.shift == 0 for r in track.revs)


def test_speed_map_times_each_byte():
    from nybulah.analysis.gcr import encode

    data = np.random.default_rng(1).integers(0, 256, 5600, dtype=np.uint8)
    gcr = encode(data).ravel()
    speed = np.full(len(gcr), 3, np.uint8)
    speed[3500:] = 2
    image = DiskImage("g64", {2: [Capture(np.unpackbits(gcr), 3, True, speed=speed)]})
    rev = flux_disk(image).tracks[2].ref
    assert rev.timing == "zones"
    rev.shift = 0.0
    delta = rev.channels(np.linspace(0, 1, 65))["delta"]
    turn = rev.tau(8 * 3500) / rev.period
    assert np.abs(delta[: int(64 * turn) - 1]).max() < 1e-9
    assert delta[int(64 * turn) + 1 :] == pytest.approx((16 - 2) / (16 - 3) - 1)
