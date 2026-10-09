import json
import os
import signal

import numpy as np
import pytest

from nybulah import bus, cli
from nybulah.opencbm import IEC_CLOCK, IEC_DATA
from nybulah.simhost import DOSBus, DOSDrive

INF = float("inf")
SCRIPT = [
    "reset",
    "wait 8 9 10",
    "status 8 9 10",
    'command 8 "U0>M1"',
    'command 8 "I0"',
    "dir 8",
    "identify 8 9 10",
    'command 9 "UJ"',
    "status 9",
    'command 10 "I0"',
    "detect",
]


def run(cbm, *argv, capsys=None):
    out = cli.main(["bus", *argv], cbm)
    records = None
    if capsys is not None:
        records = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
        assert records[-1] == out or "refused" in out
    return out, records


def random_rig(seed):
    rng = np.random.default_rng(seed)

    def busy():
        times = {}
        return lambda cmd: times.setdefault(cmd, rng.uniform(0, 5))

    drives = []
    for dev, model in {8: "1571", 9: "1541", 10: "1541"}.items():
        drive = DOSDrive(
            dev, model, rng.uniform(0, bus.BOOT_S), busy(), files=[("A", 3)]
        )
        drive.held_s = rng.uniform(0, drive.boot_s)
        drives.append(drive)
    return DOSBus(drives)


def assert_idle(cbm):
    assert cbm.host_lines == 0 and cbm.addressed is None and cbm.closed


@pytest.mark.parametrize("seed", range(12))
def test_random_boot_and_command_times_never_meet_a_busy_drive(seed, capsys):
    cbm = random_rig(seed)
    out, recs = run(cbm, *SCRIPT, capsys=capsys)
    assert not cbm.violations and out["ok"], recs
    assert_idle(cbm)
    by = {(r["step"], r["dev"], r.get("arg")): r for r in recs[:-1]}
    boot = max(d.held_s for d in cbm.drives.values())
    assert by[("reset", None, None)]["seconds"] >= boot
    for dev, cmd in ((8, b"U0>M1"), (8, b"I0"), (10, b"I0")):
        rec = by[("command", dev, cmd.decode())]
        assert rec["seconds"] >= cbm.drives[dev].command_s(cmd) - 1e-6
    assert by[("command", 9, "UJ")]["status"].startswith("73,")
    assert by[("dir", 8, None)]["files"] == [
        '0 \x12"SIM" 00 2A',
        '3 "A" PRG',
        "664 BLOCKS FREE.",
    ]
    assert by[("detect", None, None)]["devices"] == {
        "8": "1571",
        "9": "1541",
        "10": "1541",
    }
    assert out["summary"]["8"] == {
        "status": "00, OK,00,00",
        "model": "1571",
        "failed": False,
    }
    assert [r["step"] for r in recs[-4:-1]] == ["status"] * 3


@pytest.mark.parametrize("seed", range(12))
def test_steps_without_waits_still_wait_for_boot(seed, capsys):
    cbm = random_rig(seed)
    script = ["reset", 'command 8 "I0"', "dir 9", "identify 10", 'command 9 "UJ"']
    out, _ = run(cbm, *script, 'command 9 "I0"', "dir 9", capsys=capsys)
    assert not cbm.violations and out["ok"]


@pytest.mark.parametrize("pause", [0.0, 0.5])
def test_fixed_sleep_host_meets_busy_drive(pause):
    cbm = DOSBus([DOSDrive(8, boot_s=1.0)])
    cbm.reset()
    cbm.sleep(pause)
    cbm.set_timeout(30000)
    cbm.command(8, b"I0")
    assert cbm.violations


def test_hung_command_fails_fast_and_leaves_bus_idle(capsys):
    hang = DOSDrive(9, command_s=lambda cmd: INF if cmd == b"I0" else 0.0)
    cbm = DOSBus([DOSDrive(8), hang])
    argv = ['command 9 "I0"', "status 8", "--command-seconds", "5"]
    out, recs = run(cbm, "wait 8 9", *argv, capsys=capsys)
    assert [r.get("step") for r in recs] == ["wait", "wait", "command", None]
    bad = recs[2]
    assert bad["result"] == "error" and "DeviceHung" in bad["error"]
    assert 5 <= bad["seconds"] <= 5.2
    assert not out["ok"] and out["summary"]["9"]["failed"]
    assert_idle(cbm)
    assert not cbm.violations


def test_hung_boot_is_bus_held_without_atn(capsys):
    cbm = DOSBus([DOSDrive(8, boot_s=INF)])
    out, recs = run(cbm, "reset", "status 8", capsys=capsys)
    assert recs[0]["result"] == "error" and "BusHeld" in recs[0]["error"]
    assert recs[0]["seconds"] == pytest.approx(bus.RESET_HOLD_S + bus.BOOT_S, abs=0.11)
    assert len(recs) == 2 and not out["ok"] and not cbm.log
    assert_idle(cbm)


def test_keep_going_skips_only_the_absent_drive(capsys):
    cbm = DOSBus([DOSDrive(8, "1571")])
    out, recs = run(
        cbm, "wait 8 9", "status 9 8", 'command 8 "I0"', "--keep-going", capsys=capsys
    )
    results = [(r["step"], r["dev"], r["result"]) for r in recs[:-1]]
    assert results == [
        ("wait", 8, "ok"),
        ("wait", 9, "error"),
        ("status", 9, "skipped"),
        ("status", 8, "ok"),
        ("command", 8, "ok"),
        ("status", 8, "ok"),
    ]
    assert recs[1]["seconds"] <= bus.BOOT_S + 0.11
    assert out["summary"]["9"]["failed"] and not out["summary"]["8"]["failed"]
    assert not cbm.violations


def test_dos_error_stops_the_script(capsys):
    drive = DOSDrive(8, reply=lambda cmd: "74,DRIVE NOT READY,00,00")
    out, recs = run(DOSBus([drive]), 'command 8 "I0"', "status 8", capsys=capsys)
    assert recs[0]["result"] == "dos-error" and len(recs) == 2
    assert out["summary"]["8"]["status"].startswith("74,")


@pytest.mark.parametrize("where", ["transaction", "backoff"])
def test_signal_mid_script_releases_bus(where, capsys):
    cbm = DOSBus([DOSDrive(8, boot_s=0.5), DOSDrive(9)])
    if where == "transaction":
        cbm.hook = lambda kind, dev: kind == "listen" and os.kill(
            os.getpid(), signal.SIGINT
        )
    else:
        sleep = cbm.sleep
        cbm.sleep = lambda s: (os.kill(os.getpid(), signal.SIGTERM), sleep(s))
    out, recs = run(cbm, "reset", "wait 8", 'command 8 "I0"', "status 9", capsys=capsys)
    assert out["interrupted"] == ("SIGINT" if where == "transaction" else "SIGTERM")
    ran = [(r["step"], r["result"]) for r in recs[:-1]]
    if where == "transaction":
        assert ran == [("reset", "ok"), ("wait", "ok"), ("command", "ok")]
    else:
        assert ran == [("reset", "interrupted")]
    assert not out["ok"]
    assert all(dev != 9 for _, _, dev, _ in cbm.log)
    assert_idle(cbm)
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


REFUSED = [
    "N0:DISK,ID",
    "U0\\x0a",
    "M-W\\x00\\x00\\x01\\xc0",
    "M-W\\x03\\x00\\x02\\x80\\x00",
    "M-E\\x00\\x05",
    "B-E 2 0 1 0",
    "&BOOT",
    "U3",
    "UC",
]
ALLOWED = ["N0:DISK", "U0>M1", "M-W\\x00\\x05\\x01\\x00", "I0", "UJ", "U9", "V0"]


@pytest.mark.parametrize("cmd", REFUSED)
def test_bump_guard_refuses_before_touching_the_bus(cmd, capsys):
    cbm = DOSBus([DOSDrive(8)])
    out, recs = run(cbm, "reset", f'command 8 "{cmd}"', capsys=capsys)
    assert recs[0]["result"] == "refused" and recs[0]["error"]
    assert not out["ok"] and not cbm.log and cbm.now == 0
    out, _ = run(DOSBus([DOSDrive(8)]), f'command 8 "{cmd}"', "--allow-dos-bump")
    assert out["ok"]


@pytest.mark.parametrize("cmd", ALLOWED)
def test_bump_guard_allows(cmd):
    raw = cmd.encode().decode("unicode_escape").encode("latin-1")
    assert bus.bump_risk(raw) is None


def test_script_file_reset_first_and_no_end_check(tmp_path, capsys):
    script = tmp_path / "steps"
    script.write_text("# drive check\nwait 8  # after power on\n\nidentify 8\n")
    cbm = DOSBus([DOSDrive(8, boot_s=0.3)])
    argv = ["--script", str(script), "status 8", "--reset-first", "--no-end-check"]
    out, recs = run(cbm, *argv, capsys=capsys)
    assert [r["step"] for r in recs[:-1]] == ["reset", "wait", "identify", "status"]
    assert out["summary"]["8"]["model"] == "1541" and out["ok"]


@pytest.mark.parametrize(
    "line",
    ["format 8", "reset 8", "status", "status 7", "dir 8 9", "command 8", "wait x"],
)
def test_parse_rejects(line):
    with pytest.raises(ValueError):
        bus.parse(line)


def test_parse_expands_devices_and_escapes():
    assert bus.parse("  # nothing") == []
    assert bus.parse("status 8 9") == [("status", 8, None), ("status", 9, None)]
    assert bus.parse('command 8 "U0\\x3eM1"') == [("command", 8, b"U0>M1")]


def test_unsupported_model_is_an_error(capsys):
    cbm = DOSBus([DOSDrive(8)])
    cbm.identify = lambda dev: (99, "*unknown*")
    out, recs = run(cbm, "identify 8", "detect", "--keep-going", capsys=capsys)
    assert recs[0]["result"] == "error" and "ValueError" in recs[0]["error"]
    assert recs[1]["devices"]["8"].startswith("device 8: unsupported")
    assert not out["ok"]


def test_dos_status_rules():
    assert bus.answers("73,CBM DOS V2.6 1541,00,00")
    assert not bus.answers("99 DRIVER ERROR,01,00") and not bus.answers(None)
    assert bus.dos_ok(None) and bus.dos_ok("01, FILES SCRATCHED,01,00")
    assert bus.dos_ok("73,CBM DOS V3.0 1571,00,00") and not bus.dos_ok("21,READ ERROR")
    assert not bus.dos_ok("")


def test_boot_deadline_matches_opencbm_reset_wait():
    """OpenCBM's xum1541 reset documents a 1541 answering about 1.2 s after
    RESET and bounds its own wait at 1.5 s; the derivation must agree."""
    assert 0.9 < bus.diagnostic_cycles(64, 7) / 1e6 < 1.2 < bus.BOOT_S + 0.1 < 1.5
    assert bus.BOOT_S == bus.diagnostic_cycles(128, 7) / 1e6


def test_recover_waits_for_boot_then_gives_up_on_silence():
    cbm = DOSBus([DOSDrive(8, boot_s=0.7)])
    assert bus.recover(cbm, 8).startswith("73,")
    assert cbm.now >= 0.8 and not cbm.violations
    with pytest.raises(bus.DriveUnresponsive, match="device 9 silent after 2"):
        bus.recover(cbm, 9, timeout=0.2)


def test_console_exit_status(monkeypatch):
    monkeypatch.setattr(cli, "main", lambda: {"ok": False})
    assert cli.console() == 1
    monkeypatch.setattr(cli, "main", lambda: {"summary": {}})
    assert cli.console() == 0


def test_bus_line_masks():
    cbm = DOSBus([DOSDrive(8, boot_s=1)])
    cbm.reset()
    assert cbm.iec_poll() == IEC_CLOCK | IEC_DATA
    cbm.sleep(1)
    assert cbm.iec_poll() == 0
