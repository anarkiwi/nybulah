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
