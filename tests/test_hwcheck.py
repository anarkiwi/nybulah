import json

from nybulah import hwcheck
from nybulah.sim import Bus, Drive1541, Drive1571
from nybulah.simhost import SimCBM


def two_drives():
    bus = Bus()
    cbm = SimCBM(Drive1571(device=8, bus=bus), dev=8)
    cbm.attach(10, Drive1541(device=10, bus=bus))
    return cbm


def run(cbm, tmp_path, *extra):
    argv = ["--size", "512", "--reps", "1", "--out", str(tmp_path)]
    argv += ["--recover-timeout", "0.01", *extra]
    summary = hwcheck.main(argv, cbm)
    (log,) = tmp_path.glob("hwcheck-*.jsonl")
    lines = [json.loads(line) for line in log.read_text().splitlines()]
    assert lines[-1]["summary"] == summary
    return summary, lines


def test_session_covers_both_models_and_absent_drive(tmp_path):
    summary, lines = run(
        two_drives(), tmp_path, "--devs", "8", "10", "9", "--proto", "s3"
    )
    s8, s10, s9 = summary["8"], summary["10"], summary["9"]
    assert s8["model"] == "1571" and s8["ram"] == [[0x6000, 0x8000]]
    assert s10["model"] == "1541" and s10["ram"] == [[0x8000, 0xA000]]
    for s, base in ((s8, 0x6000), (s10, 0x8000)):
        assert s["bench_s1"]["errors"] == 0 and s["bench_s1"]["addr"] == base
        assert (
            s["alias_s1"] == [] and s["failed"] == [] and s["skipped"] == ["bench_s3"]
        )
    assert s9["failed"] == ["identify", "ramprobe"] and s9["skipped"] == [
        "bench_s1",
        "bench_s3",
    ]
    assert any("recover_error" in r for r in lines if r.get("dev") == 9)


def test_failed_transfer_recovers_and_continues(tmp_path):
    cbm = two_drives()
    cbm.unplug(3)
    summary, lines = run(cbm, tmp_path, "--devs", "8", "10")
    assert summary["8"]["failed"] == ["bench_s1"] and summary["8"]["alias_s1"]
    (bad,) = [r for r in lines if r.get("ok") is False]
    assert bad["recovered"].startswith("73,") and "HostGone" in bad["error"]
    assert summary["10"]["failed"] == [] and summary["10"]["bench_s1"]["errors"] == 0


def test_s2_session(tmp_path):
    summary, _ = run(
        SimCBM(Drive1541(device=10), dev=10), tmp_path, "--devs", "10", "--s2"
    )
    assert summary["10"]["bench_s2"]["errors"] == 0 and "bench_s1" not in summary["10"]


def test_s3_session(tmp_path):
    from nybulah.simx import adapter

    summary, _ = run(
        adapter("s3", device=10), tmp_path, "--devs", "10", "--proto", "s3"
    )
    assert summary["10"]["bench_s3"]["errors"] == 0 and summary["10"]["alias_s3"] == []


def test_disk_survey_step(tmp_path, monkeypatch):
    import contextlib
    import functools

    import numpy as np

    from nybulah.formats import D64, d64_to_g64
    from nybulah.nibbler import Nibbler
    from nybulah.simdisk import Media, disk_drive
    from nybulah.simhost import SimMonitor

    image = D64(np.zeros((683, 256), np.uint8))
    drive = disk_drive("1541", Media.from_g64(d64_to_g64(image, progress=False)), 10)
    monkeypatch.setattr(
        hwcheck,
        "Monitor",
        lambda cbm, dev, proto: contextlib.nullcontext(SimMonitor(cbm.drives[dev])),
    )
    monkeypatch.setattr(
        hwcheck,
        "Nibbler",
        functools.partial(
            Nibbler, stepms=1, settle_ms=1, spinup_s=0, sleep=lambda s: None
        ),
    )
    summary, _ = run(SimCBM(drive, dev=10), tmp_path, "--devs", "10", "--disk")
    zones = summary["10"]["disk"]["zones"]
    assert [z["sectors_ok"] for z in zones] == [21, 19, 18, 17]
    assert summary["10"]["disk"]["rpm"] is None
