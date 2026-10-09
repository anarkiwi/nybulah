import numpy as np
import pytest

from nybulah import disk
from nybulah.analysis.sector import SectorError
from nybulah.formats import D71, d64_to_g64
from nybulah.nibbler import Capture
from nybulah.simdisk import Media

FEW = (1, 17, 18, 19, 24, 25, 30, 31, 35)


def few_jobs(sides=(0,)):
    return [j for j in disk.d71_jobs() if j.side in sides and j.track in FEW]


def with_errors(image):
    errors = image.errors.copy()
    for track, sector, code in (
        (1, 3, SectorError.DATA_CHECKSUM),
        (17, 0, SectorError.BAD_GCR),
        (19, 2, SectorError.ID_MISMATCH),
        (24, 5, SectorError.HEADER_CHECKSUM),
        (25, 1, SectorError.DATA_NOT_FOUND),
        (30, 4, SectorError.HEADER_NOT_FOUND),
    ):
        errors[image.span(track).start + sector] = code
    image.errors = errors
    return image


def rows(image, jobs):
    return np.concatenate(
        [np.arange(image.span(j.header).start, image.span(j.header).stop) for j in jobs]
    )


def test_read_reports_error_bytes(make_rig, image, tmp_path):
    bad = with_errors(type(image)(image.data.copy()))
    _, nib = make_rig("1541", Media.from_g64(d64_to_g64(bad, progress=False)))
    jobs = few_jobs()
    data, errors = disk.read_jobs(
        nib, jobs, retries=1, archive=tmp_path, progress=False
    )
    want = rows(bad, jobs)
    assert (errors == bad.errors[want]).all()
    ok = errors == SectorError.OK
    assert (data[ok] == bad.data[want][ok]).all()
    saved = sorted(tmp_path.glob("read-*.npz"))
    assert saved and Capture.load(saved[0]).halftrack in {2 * t for t in FEW}


def test_calibrate_relabels_head(make_rig, g64):
    drive, nib = make_rig("1541", Media.from_g64(g64))
    nib.seek(36)
    nib.halftrack = 30
    assert disk.calibrate(nib, disk.Archive()) > 0
    assert nib.halftrack == drive.mech.halftrack == 42


def test_write_verify_round_trip(make_rig, image):
    bad = with_errors(type(image)(image.data.copy()))
    drive, nib = make_rig("1541")
    jobs = few_jobs()
    want = rows(bad, jobs)
    report = disk.write_jobs(
        nib, jobs, bad.data[want], bad.errors[want], bad.disk_id, progress=False
    )
    assert report["rpm"] == pytest.approx(300.0, rel=1e-3)
    assert [t["attempts"] for t in report["tracks"]] == [1] * len(jobs)
    assert drive.mech.underruns <= len(jobs) + 1
    _, errors = disk.read_jobs(nib, jobs, progress=False)
    assert (errors == disk.written_errors(bad.errors[want])).all()


def test_verify_catches_corrupt_write(make_rig, image):
    drive, nib = make_rig("1541")
    jobs = few_jobs()[:2]
    want = rows(image, jobs)
    writes = []
    write_track = nib.write_track

    def counted(*args, **kw):
        writes.append(args[0])
        return write_track(*args, **kw)

    def corrupt(key, cell, bit):
        first_data = writes.count(2) == 2
        return 0 if key == (0, 2) and first_data and 4000 <= cell < 12000 else bit

    nib.write_track = counted
    drive.mech.corrupt = corrupt
    report = disk.write_jobs(
        nib, jobs, image.data[want], image.errors[want], image.disk_id, progress=False
    )
    assert [t["attempts"] for t in report["tracks"]] == [2, 1]
    drive.mech.corrupt = lambda key, cell, bit: (
        0 if key == (0, 2) and drive.mech.zone == 3 else bit
    )
    report = disk.write_jobs(
        nib,
        jobs,
        image.data[want],
        image.errors[want],
        image.disk_id,
        retries=1,
        progress=False,
    )
    assert disk.failed(report) == [{"side": 0, "track": 1, "attempts": None}]


def test_written_errors():
    errs = np.array([1, 5, 7, 8, 0x0F, 2], np.uint8)
    assert disk.written_errors(errs).tolist() == [1, 5, 1, 1, 1, 2]
    assert (disk.written_errors(np.array([1, 3], np.uint8)) == 3).all()


def test_model_and_size_gates(make_rig, image):
    _, nib = make_rig("1541")
    with pytest.raises(ValueError):
        disk.read_d71(nib)
    with pytest.raises(ValueError):
        disk.write_d71(nib, D71.from_sides(image, image))
    with pytest.raises(disk.TrackError):
        disk.track_stream(
            disk.TrackJob(0, 1, 1), image.data[:21], None, image.disk_id, 70000
        )


def test_survey(make_rig, g64):
    _, nib = make_rig("1571", Media.from_g64(g64))
    out = disk.survey(nib)
    assert [z["zone"] for z in out["zones"]] == [3, 2, 1, 0]
    assert all(
        z["sectors_ok"] == {3: 21, 2: 19, 1: 18, 0: 17}[z["zone"]] for z in out["zones"]
    )
    assert out["rpm"] == pytest.approx(300.0, rel=1e-3)


def test_d71_sides_subset(make_rig, image):
    other = type(image)(image.data[::-1].copy())
    d71 = D71.from_sides(image, other)
    drive, nib = make_rig("1571")
    jobs = few_jobs((0, 1))
    want = np.concatenate(
        [np.arange(d71.span(j.header).start, d71.span(j.header).stop) for j in jobs]
    )
    report = disk.write_jobs(
        nib, jobs, d71.data[want], d71.errors[want], d71.disk_id, progress=False
    )
    assert not disk.failed(report)
    assert (0, 2) in drive.mech.media.tracks and (1, 2) in drive.mech.media.tracks
    data, errors = disk.read_jobs(nib, jobs, progress=False)
    assert (errors == SectorError.OK).all() and (data == d71.data[want]).all()


@pytest.mark.slow
def test_full_d64_read(make_rig, image, g64):
    _, nib = make_rig("1541", Media.from_g64(g64))
    out = disk.read_d64(nib, progress=False)
    assert (out.errors == SectorError.OK).all() and (out.data == image.data).all()


@pytest.mark.slow
def test_full_d71_round_trip(make_rig, image):
    d71 = D71.from_sides(image, type(image)(image.data[::-1].copy()))
    _, nib = make_rig("1571")
    assert not disk.failed(disk.write_d71(nib, d71, progress=False))
    out = disk.read_d71(nib, progress=False)
    assert (out.errors == SectorError.OK).all() and (out.data == d71.data).all()
