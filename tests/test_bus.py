import json
import os
import signal

import numpy as np
import pytest

from nybulah import bus, cli
from nybulah.opencbm import IEC_CLOCK, IEC_DATA, OpenCBMError
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
    for dev, model in {8: "1571", 9: "1541", 10: "1581"}.items():
        hold = rng.uniform(0, bus.BOOT_S)
        drive = DOSDrive(
            dev, model, hold + rng.uniform(0, 10), busy(), files=[("A", 3)]
        )
        drive.holds = ((0.0, hold),)
        drives.append(drive)
    return DOSBus(drives)


def assert_idle(cbm):
    assert cbm.host_lines == 0 and cbm.addressed is None and cbm.closed


@pytest.mark.parametrize("seed", range(12))
def test_random_boot_and_command_times_never_meet_a_busy_drive(seed, capsys):
    cbm = random_rig(seed)
    out, recs = run(cbm, *SCRIPT, capsys=capsys)
    assert not cbm.violations and not cbm.aborts and out["ok"], recs
    assert_idle(cbm)
    by = {(r["step"], r["dev"], r.get("arg")): r for r in recs[:-1]}
    reset = by[("reset", None, None)]
    hold = max(d.holds[0][1] for d in cbm.drives.values())
    assert reset["settled_s"] == pytest.approx(hold, abs=1e-3)
    timeline = out["timelines"][1]
    assert timeline["released"] == {
        "CLK": reset["settled_s"],
        "DATA": reset["settled_s"],
    }
    boot = max(d.boot_s for d in cbm.drives.values())
    assert max(timeline["answered"].values()) >= boot - 0.1 - 1e-3
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
        "10": "1581",
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


def held_forever(*devs, since=0.0):
    drives = [DOSDrive(d) for d in devs]
    drives[0].holds = ((since, INF),)
    drives[0].held = [(since, INF)]
    return DOSBus(drives)


class ClockFirst(DOSBus):
    """A drive holding CLK alone until data_s after the reset, then CLK and DATA."""

    def __init__(self, drives, data_s, **kw):
        super().__init__(drives, **kw)
        self.data_s, self.reset_at = data_s, 0.0

    def reset(self):
        super().reset()
        self.reset_at = self.now

    def _lines(self, t):
        lines = super()._lines(t)
        return lines & ~IEC_DATA if t < self.reset_at + self.data_s else lines


def held_by_a_drive(*devs, cls=ClockFirst, **kw):
    drives = [DOSDrive(d) for d in devs]
    drives[0].holds = ((0.0, INF),)
    return cls(drives, data_s=bus.BOOT_S, **kw)


def test_outer_limit_reports_and_leaves_the_bus_untouched(capsys):
    cbm = held_by_a_drive(8, 9)
    out, recs = run(cbm, "reset", "wait 8 9", "status 9", "--keep-going", capsys=capsys)
    limit = cbm.writes[-1][0] + 0.1 + bus.OUTER_S
    assert recs[0]["result"] == "error" and "BusHeld" in recs[0]["error"]
    assert [w for w in cbm.writes if w[0] >= limit - 0.2] == []
    assert (
        cbm.resets == 1 + bus.RECOVERY_PULSES
        and not cbm.log
        and len(recs) == 2
        and not out["ok"]
    )
    assert "needs a power cycle; nothing was sent" in out["held"]
    assert cbm.host_lines == 0 and cbm.closed


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
    assert recs[1]["seconds"] == pytest.approx(bus.OUTER_S, abs=0.11)
    assert out["summary"]["9"]["failed"] and not out["summary"]["8"]["failed"]
    assert not cbm.violations


def test_dos_error_stops_the_script(capsys):
    drive = DOSDrive(8, reply=lambda cmd: "74,DRIVE NOT READY,00,00")
    out, recs = run(DOSBus([drive]), 'command 8 "I0"', "status 8", capsys=capsys)
    assert recs[0]["result"] == "dos-error" and len(recs) == 2
    assert out["summary"]["8"]["status"].startswith("74,")


@pytest.mark.parametrize("where", ["transaction", "settle"])
def test_signal_mid_script_releases_bus(where, capsys):
    cbm = DOSBus([DOSDrive(8, boot_s=0.5), DOSDrive(9)])
    if where == "transaction":
        cbm.hook = lambda kind, dev: kind == "listen" and os.kill(
            os.getpid(), signal.SIGINT
        )
    else:
        cbm.iec_wait = lambda *a: os.kill(os.getpid(), signal.SIGTERM)
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
    [
        "format 8",
        "reset 8",
        "usbreset 8",
        "status",
        "status 7",
        "dir 8 9",
        "command 8",
        "wait x",
    ],
)
def test_parse_rejects(line):
    with pytest.raises(ValueError):
        bus.parse(line)


def test_parse_expands_devices_and_escapes():
    assert bus.parse("  # nothing") == []
    assert bus.parse("status 8 9") == [("status", 8, None), ("status", 9, None)]
    assert bus.parse('command 8 "U0\\x3eM1"') == [("command", 8, b"U0>M1")]
    assert bus.parse("adapterreset") == [("adapterreset", None, None)]


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
    assert cbm.now >= 0.8 - 1e-9 and not cbm.violations
    with pytest.raises(bus.DriveUnresponsive, match="device 9 silent after 2"):
        bus.recover(cbm, 9)
    held = held_forever(8)
    with pytest.raises(bus.DriveUnresponsive, match="nothing was sent"):
        bus.recover(held, 8, timeout=0.2)
    assert held.resets == 1 + bus.RECOVERY_PULSES and not held.log


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


def test_bus_held_snapshot_reads_like_a_diagnosis(capsys):
    cbm = held_forever(8)
    out, recs = run(cbm, "reset", "--boot-seconds", "2", capsys=capsys)
    snap = recs[0]["bus"]
    assert snap["text"] == (
        "CLK low for 2.10 s since reset; DATA low for 2.10 s since reset; "
        "ATN not asserted; reset 2.10 s ago; no ATN yet; no drive addressed"
    )
    assert snap["low"] == ["CLK", "DATA"] and snap["since_atn_s"] is None
    assert out["bus"]["low"] == ["CLK", "DATA"]
    assert out["timelines"][1]["transitions"] == [[0.0, ["CLK", "DATA"]]]


def test_hung_command_snapshot_times_the_last_atn(capsys):
    hang = DOSDrive(8, command_s=lambda cmd: INF)
    out, recs = run(
        DOSBus([hang]), 'command 8 "I0"', "--command-seconds", "5", capsys=capsys
    )
    snap = recs[0]["bus"]
    assert snap["text"] == (
        "no line low; ATN not asserted; no reset; last ATN 5.00 s ago; no drive addressed"
    )
    assert out["summary"]["8"]["bus"] == snap and out["bus"]["since_atn_s"] == 5.0


def test_lines_held_since_mid_wait_are_timed_from_first_sight():
    cbm = DOSBus([DOSDrive(8)])
    b = bus.Bus(cbm)
    b.sample()
    cbm.drives[8].held = [(0.0, 2.0)]
    cbm.sleep(0.5)
    b.sample()
    cbm.sleep(0.25)
    assert b.snapshot()["held_s"] == {"CLK": 0.25, "DATA": 0.25}


def test_failed_untalk_reports_the_talker(capsys):
    cbm = DOSBus([DOSDrive(8)])

    def fail(*_):
        raise OpenCBMError("stalled")

    cbm.raw_read = cbm.untalk = fail
    _, recs = run(cbm, "dir 8", capsys=capsys)
    assert recs[0]["bus"]["addressed"] == "device 8 talking"
    assert recs[0]["bus"]["text"].endswith("; device 8 talking")


def test_interrupt_summary_carries_the_bus_state(capsys):
    cbm = DOSBus([DOSDrive(8)])
    cbm.hook = lambda kind, dev: os.kill(os.getpid(), signal.SIGINT)
    out, _ = run(cbm, "status 8", "status 8", capsys=capsys)
    assert out["interrupted"] == "SIGINT"
    assert out["bus"]["text"].endswith(
        "ATN not asserted; no reset; last ATN 0.00 s ago; no drive addressed"
    )


def mf1581(boot_s):
    """The stock 1581 ROM sets its ports before the diagnostic: it holds no line
    while it searches for its boot file."""
    drive = DOSDrive(9, "1581", boot_s=boot_s)
    drive.holds = ()
    return DOSBus([DOSDrive(8), drive])


def test_first_atn_after_reset_waits_out_a_busy_1581(capsys):
    """Lines free but the 1581 still searching: drive 8's probe holds ATN until
    the 1581 serves it, and the adapter is never made to give up on it."""
    cbm = mf1581(8.0)
    out, recs = run(cbm, "reset", "wait 8 9", "identify 9", capsys=capsys)
    assert out["ok"] and not cbm.aborts and not cbm.violations
    assert recs[1]["answered_s"] == pytest.approx(8.0, abs=1e-3)
    assert out["summary"]["9"]["model"] == "1581"
    assert bus.model_of(mf1581(0), 9) == "1581"


def test_bus_held_before_the_run_blames_no_drive(capsys):
    cbm = held_forever(9, 8, 10, since=-60.0)
    out, recs = run(cbm, "status 8 9 10", capsys=capsys)
    assert recs[0]["seconds"] == pytest.approx(bus.OUTER_S, abs=0.11)
    assert out["held"].startswith(
        f"CLK and DATA still held {recs[0]['seconds']:.2f} s after this run started, "
        "past every derived boot bound"
    )
    assert "the adapter or a drive is holding the bus; bus step adapterreset" in (
        out["held"]
    )
    snap = recs[0]["bus"]
    assert snap["held_since"] == {"CLK": "unknown", "DATA": "unknown"}
    assert "at least (low when this run started)" in snap["text"]
    assert out["summary"]["8"] == {"status": None, "model": None, "failed": False}
    assert not out["ok"] and len(recs) == 2 and not cbm.log


def test_reset_mid_boot_is_a_violation():
    cbm = DOSBus([DOSDrive(8, boot_s=1.0)])
    cbm.reset()
    cbm.sleep(0.5)
    cbm.reset()
    assert cbm.violations and cbm.violations[0][1] == "reset"


def test_settle_needs_the_quiet_window_a_drive_was_seen_to_break():
    cbm = DOSBus([DOSDrive(8, boot_s=3.0)])
    cbm.drives[8].holds = ((0.0, 1.0), (1.3, 2.0))
    cbm.reset()
    b = bus.Bus(cbm)
    b.settle(100)
    assert cbm.now == pytest.approx(1.1) and b.quiet == 0
    cbm.sleep(0.5)
    b.settle(100)
    assert b.quiet == pytest.approx(0.5) and cbm.now == pytest.approx(2.6)
    times = [t for t, _ in b.timeline["transitions"]]
    assert times == pytest.approx([0.0, 1.0, 1.5, 2.0])


class WedgedAdapter(DOSBus):
    """An adapter stopped mid-transfer holding CLK and DATA: its command loop
    never performs a RESET request (fails: the request errors) and it ignores
    releases until the control-endpoint reset aborts the transfer."""

    def __init__(self, drives, fails=False):
        super().__init__(drives)
        self.fails, self.wedged, self.calls = fails, True, []
        self.host_lines = IEC_CLOCK | IEC_DATA

    def reset(self):
        if self.wedged and self.fails:
            raise OpenCBMError("cbm_reset: timeout")
        if not self.wedged:
            super().reset()

    def iec_release(self, lines):
        if not self.wedged:
            super().iec_release(lines)

    def adapter_reset(self, reset_bus=True):
        self.calls.append(("adapter_reset", reset_bus))
        self.wedged = False
        super().adapter_reset(reset_bus)


class OldFirmware(WedgedAdapter):
    """Firmware before v13 refuses the control-endpoint reset."""

    def adapter_reset(self, reset_bus=True):
        self.calls.append(("adapter_reset", reset_bus))
        raise OpenCBMError("cbm_adapter_reset returned -1")


class USBResettable(OldFirmware):
    """Old firmware whose USB reset reinitialises the adapter."""

    def usb_reset(self):
        self.calls.append(("usb_reset",))
        self.wedged, self.host_lines = False, 0


class Stubborn(DOSBus):
    """Drive 8 holds CLK and DATA from the start through the first ``pulses``
    RESET pulses, then boots like any other."""

    def __init__(self, drives, pulses):
        super().__init__(drives)
        self.pulses = pulses
        self.drives[8].held = [(0.0, INF)]

    def reset(self):
        super().reset()
        if self.resets <= self.pulses:
            self.drives[8].held = [(self.now, INF)]


def booting(*devs):
    return [DOSDrive(d, boot_s=0.5) for d in devs]


def resets_sent(cbm):
    return [w for _, w in cbm.writes if w.endswith("reset")]


@pytest.mark.parametrize("fails", [False, True])
def test_wedged_adapter_is_released_by_the_adapter_reset_alone(fails, capsys):
    cbm = WedgedAdapter(booting(8, 9), fails=fails)
    out, recs = run(cbm, "reset", "wait 8 9", capsys=capsys)
    assert out["ok"] and cbm.calls == [("adapter_reset", False)]
    assert resets_sent(cbm) == ["adapter reset", "reset"]
    recovery = {"adapter_reset": "ok", "pulses": 1}
    froms = ["run start", "adapter reset", "reset after adapter reset"]
    if fails:
        assert recs[0]["seconds"] == pytest.approx(0.1 + 0.5, abs=1e-3)
    else:
        recovery["held_by"] = "adapter"
        froms.insert(1, "reset")
        assert recs[0]["seconds"] >= bus.OUTER_S
        assert out["timelines"][1]["transitions"] == [[0.0, ["CLK", "DATA"]]]
    assert recs[0]["recovery"] == recovery
    assert [t["from"] for t in out["timelines"]] == froms
    assert out["timelines"][-2]["transitions"] == [[0.0, []]]
    assert [r["status"][:3] for r in recs[1:3]] == ["73,"] * 2
    assert not cbm.violations and not cbm.aborts


@pytest.mark.parametrize("pulses", [1, 2])
def test_drive_hold_gets_a_pulse_within_and_one_after_the_outer_limit(pulses, capsys):
    cbm = Stubborn(booting(8, 9), pulses)
    out, recs = run(cbm, "reset", "wait 8 9", capsys=capsys)
    assert (
        out["ok"]
        and resets_sent(cbm) == ["reset", "adapter reset"] + ["reset"] * pulses
    )
    assert recs[0]["recovery"] == {
        "adapter_reset": "ok",
        "held_by": "drive",
        "pulses": pulses,
    }
    assert out["timelines"][2]["transitions"] == [[0.0, ["CLK", "DATA"]]]
    assert pulses * bus.OUTER_S < recs[0]["seconds"] < (pulses + 1) * bus.OUTER_S
    assert [r["status"][:3] for r in recs[1:3]] == ["73,"] * 2


def test_drive_hold_past_every_pulse_is_reported_and_left_alone(capsys):
    cbm = held_by_a_drive(8, 9)
    out, recs = run(cbm, "reset", "wait 8 9", "--keep-going", capsys=capsys)
    pulses = 1 + bus.RECOVERY_PULSES
    assert resets_sent(cbm) == ["reset", "adapter reset"] + ["reset"] * (pulses - 1)
    limit = cbm.writes[-1][0] + 0.1 + bus.OUTER_S
    assert [w for w in cbm.writes if w[0] >= limit - 0.2] == []
    assert recs[0]["seconds"] == pytest.approx(pulses * (0.2 + bus.OUTER_S), abs=0.3)
    assert recs[0]["recovery"] == {
        "adapter_reset": "ok",
        "held_by": "drive",
        "pulses": bus.RECOVERY_PULSES,
    }
    assert "BusHeld" in recs[0]["error"] and len(recs) == 2 and not cbm.log
    assert out["held"].endswith(
        "the adapter has released its lines, so a drive is holding the bus and "
        "needs a power cycle; nothing was sent"
    )
    assert cbm.host_lines == 0 and cbm.closed and not out["ok"]


def test_failed_adapter_reset_falls_back_to_a_usb_reset(capsys):
    cbm = USBResettable(booting(8))
    out, recs = run(cbm, "reset", "wait 8", capsys=capsys)
    assert out["ok"] and cbm.calls == [("adapter_reset", False), ("usb_reset",)]
    assert recs[0]["recovery"] == {
        "adapter_reset": "OpenCBMError: cbm_adapter_reset returned -1",
        "usb_reset": "ok",
        "held_by": "adapter",
        "pulses": 1,
    }
    assert recs[1]["status"].startswith("73,")


@pytest.mark.parametrize("cls", [OldFirmware, None])
def test_adapter_that_cannot_be_reset_is_an_error(cls, capsys):
    if cls is None:
        cbm = held_forever(8)
        cbm.adapter_reset = None
        error = "adapter cannot be reset by command"
    else:
        cbm, error = cls(booting(8), fails=True), "cbm_adapter_reset returned -1"
    out, recs = run(cbm, "reset", "wait 8", capsys=capsys)
    assert recs[0]["error"] == f"OpenCBMError: {error}" and len(recs) == 2
    assert recs[0]["bus"]["low"] == ["CLK", "DATA"] and not out["ok"]
    assert not cbm.log and cbm.resets == (1 if cls is None else 0)


def test_adapterreset_step(capsys):
    cbm = DOSBus(booting(8))
    out, recs = run(cbm, "adapterreset", "wait 8", capsys=capsys)
    assert out["ok"] and resets_sent(cbm) == ["adapter reset", "reset"]
    assert recs[0]["recovery"] == {"adapter_reset": "ok", "pulses": 1}
    assert recs[0]["settled_s"] == pytest.approx(0.5, abs=1e-3)


def test_recover_releases_a_wedged_adapter_and_reports_a_drive_hold():
    cbm = WedgedAdapter(booting(8))
    assert bus.recover(cbm, 8).startswith("73,")
    assert cbm.calls == [("adapter_reset", False)]
    stuck = held_forever(8)
    with pytest.raises(bus.DriveUnresponsive, match="device 8: .*a drive is holding"):
        bus.recover(stuck, 8, timeout=0.2)
    assert stuck.resets == 1 + bus.RECOVERY_PULSES and not stuck.log
