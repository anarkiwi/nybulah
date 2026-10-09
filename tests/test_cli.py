import json

import pytest

from nybulah import bench, cli, opencbm
from nybulah.sim import Drive1541, Drive1571, SimCBM


def tracked(drive):
    cbm = SimCBM(drive, dev=drive.device)
    cbm.closed = False

    def close():
        cbm.closed = True

    cbm.close = close
    return cbm


def test_ramprobe_dispatch_closes_adapter(capsys):
    cbm = tracked(Drive1571(device=8))
    out = cli.main(["ramprobe", "--start", "0x6000", "--end", "0x8000"], cbm)
    assert out["ram"] == [[0x6000, 0x8000]] and cbm.closed
    assert json.loads(capsys.readouterr().out) == out


def test_bench_dispatch():
    cbm = tracked(Drive1541(device=10))
    argv = ["bench", "--dev", "10", "--addr", "0x8000", "--size", "64", "--reps", "1"]
    out = cli.main(argv, cbm)
    assert out["errors"] == 0 and out["read"]["bytes"] == 64 and cbm.closed


def test_hwcheck_dispatch(tmp_path):
    argv = ["hwcheck", "--devs", "10", "--size", "64", "--reps", "1"]
    out = cli.main(argv + ["--out", str(tmp_path)], tracked(Drive1541(device=10)))
    assert out["10"]["bench_s1"]["errors"] == 0


def test_module_entry_opens_default_adapter(monkeypatch):
    cbm = tracked(Drive1541(device=10))
    monkeypatch.setattr(opencbm, "OpenCBM", lambda: cbm)
    out = bench.main(["--dev", "10", "--size", "32", "--reps", "1"])
    assert out["errors"] == 0 and cbm.closed


def test_usage(capsys):
    with pytest.raises(SystemExit):
        cli.main([])
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    help_text = capsys.readouterr().out
    assert all(name in help_text for name in cli.COMMANDS)


def test_drivecode_env_fallback(tmp_path, monkeypatch):
    from nybulah.monitor import drivecode, protocols

    (tmp_path / "monitor_s9.bin").write_bytes(b"\x60")
    monkeypatch.setenv("NYBULAH_DRIVECODE", str(tmp_path))
    assert "s9" in protocols() and drivecode("monitor_s9") == b"\x60"
    with pytest.raises(FileNotFoundError, match="NYBULAH_DRIVECODE"):
        drivecode("monitor_s8")


def disk_cli(monkeypatch, model, media=None, dev=10):
    import contextlib
    import functools

    from nybulah import diskcmd
    from nybulah.nibbler import Nibbler
    from nybulah.simdisk import Media, SimMonitor, disk_drive

    drive = disk_drive(model, media if media is not None else Media(), dev)
    monkeypatch.setattr(
        diskcmd,
        "Monitor",
        lambda cbm, dev, proto: contextlib.nullcontext(SimMonitor(cbm.drive)),
    )
    monkeypatch.setattr(
        diskcmd,
        "Nibbler",
        functools.partial(
            Nibbler, stepms=1, settle_ms=1, spinup_s=0, sleep=lambda s: None
        ),
    )
    return tracked(drive)


def test_read_and_write_commands(monkeypatch, tmp_path):
    import numpy as np

    from nybulah import disk
    from nybulah.formats import D64, d64_to_g64, read_d64, write_d64
    from nybulah.simdisk import Media

    image = D64(np.random.default_rng(5).integers(0, 256, (683, 256), np.uint8))
    path = tmp_path / "in.d64"
    path.write_bytes(write_d64(image))
    cbm = disk_cli(monkeypatch, "1541")
    jobs = disk.d64_jobs
    monkeypatch.setattr(disk, "d64_jobs", lambda tracks: jobs(2))
    out = cli.main(["write", "--dev", "10", str(path)], cbm)
    assert out["failed"] == [] and cbm.closed
    monkeypatch.setattr(disk, "d64_jobs", jobs)
    cbm = disk_cli(
        monkeypatch, "1541", Media.from_g64(d64_to_g64(image, progress=False))
    )
    target = tmp_path / "out.d64"
    out = cli.main(
        ["read", "--dev", "10", str(target), "--archive", str(tmp_path / "caps")], cbm
    )
    assert out["errors"] == 0
    assert (read_d64(target.read_bytes()).data == image.data).all()


def test_disk_command_refusals(monkeypatch, tmp_path):
    import numpy as np

    from nybulah.formats import D71, write_d71

    path = tmp_path / "in.d71"
    path.write_bytes(write_d71(D71(np.zeros((1366, 256), np.uint8))))
    with pytest.raises(ValueError, match="needs a 1571"):
        cli.main(["write", "--dev", "10", str(path)], disk_cli(monkeypatch, "1541"))
    with pytest.raises(ValueError, match="expected"):
        cli.main(
            ["read", "--dev", "10", str(tmp_path / "x.g64")],
            disk_cli(monkeypatch, "1541"),
        )
    with pytest.raises(ValueError, match="transport"):
        cli.main(
            ["read", "--dev", "10", "--transport", "s3", str(path)],
            disk_cli(monkeypatch, "1571"),
        )


def test_console_exit_status_is_zero(monkeypatch):
    monkeypatch.setattr(cli, "main", lambda: {"result": "dict"})
    assert cli.console() == 0
