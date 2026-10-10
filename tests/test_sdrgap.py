"""drive/sdrgap.s under the simulated v12 adapter: lone bytes after idles on a 1581
(8520) and a 1571 (6526), the log around each write, the ATN end."""

import numpy as np
import pytest

from nybulah import bench, simfast, simsrq
from nybulah.link import WATCHDOG_IDLE_S
from nybulah.monitor import Monitor
from nybulah.stream import Stream

DEVS = {"1581": 9, "1571": 8}


def rig(model):
    """(adapter, started s4 monitor) on a timed drive at 2 MHz."""
    cbm = simsrq.make(
        1.0, model=model, dev=DEVS[model], firmware=12, timeout_us=WATCHDOG_IDLE_S * 1e6
    )
    mon = Monitor(cbm, DEVS[model], "s4", clock=lambda: cbm.now * 1e-6)
    mon.start()
    if model == "1571":
        mon.set_fast(True)
    return cbm, mon


@pytest.mark.parametrize("model", ["1581", "1571"])
@pytest.mark.parametrize("gap_ms", [0.0, 6.5])
@pytest.mark.parametrize("icr", [False, True])
def test_lone_bytes_after_an_idle_arrive(model, gap_ms, icr):
    """Every byte, metadata or plain, after any idle: framed by the adapter, flagged
    by the shifter when asked, the stream ended by the drive, the monitor in step."""
    _, mon = rig(model)
    out = bench.sdr_gap(mon, gap_ms, reps=6, icr_clear=icr)
    mon.stop()
    assert (out["adapter"], out["drive"], out["in_step"]) == ("done", "done", True)
    assert out["first_lost"] is None and out["received"] == out["sent"] == 8
    rows = out["log"]
    assert len(rows) == 8 and all(r["seen"] for r in rows)
    assert [r["shifted"] for r in rows[1:-1]] == [True if icr else None] * 6
    assert [r["shifted"] for r in rows[::7]] == [None, None]
    assert all(r["icr_after"] == 0 for r in rows) or icr
    assert [r["kind"] for r in rows] == ["meta"] + ["meta", "plain"] * 3 + ["meta"]
    assert [r["value"] for r in rows[1:4]] == [bench.M_KEEP, 0xAA, bench.M_KEEP]
    assert all(r["cra"] == 0x41 for r in rows)
    assert all(r["icr_before"] == 0 for r in rows) or icr
    assert all(r["port"] & 0x08 == 0 for r in rows)
    if model == "1581":
        over = np.diff([r["t_us"] for r in rows[1:-1]]) % (1 << 16) - gap_ms * 1000
        assert np.all((over >= 100) & (over <= 0.03 * gap_ms * 1000 + 250))


def test_flags_clear_icr_and_rearm_cra():
    """With ICR read after every write, the flag before the next is clear again (START
    read none, so the first entry sees its flag); CRA rewritten before the write
    keeps the shifter in output mode."""
    _, mon = rig("1581")
    out = bench.sdr_gap(mon, 0.5, reps=4, kinds=("meta",), icr_clear=True, rearm=True)
    mon.stop()
    assert out["drive"] == "done" and out["first_lost"] is None
    rows = out["log"][1:-1]
    assert all(r["flags"] == bench.F_META | bench.F_ICR | bench.F_CRA for r in rows)
    assert [bool(r["icr_before"] & bench.ICR_SP) for r in rows] == [True] + [False] * 3
    assert all(r["shifted"] and r["cra"] == 0x41 for r in rows)


def test_adapter_timeout_ends_the_probe_in_step():
    """An idle past the adapter's gap timeout: it gives up after START, its ATN ends the
    probe with END_ATN, the reply is read in step and the log holds what was sent."""
    _, mon = rig("1581")
    out = bench.sdr_gap(mon, 25.0, reps=3)
    mon.stop()
    assert (out["adapter"], out["drive"], out["in_step"]) == ("timeout", "atn", True)
    assert out["first_lost"] == 1 and out["received"] == 1
    kinds = [(r["kind"], r["value"], r["seen"]) for r in out["log"]]
    assert kinds == [("meta", bench.M_START, True), ("meta", bench.M_END_ATN, False)]


def test_no_host_go_returns_nogo():
    """The host never asserts CLK: the probe returns ST_NOGO after under a second,
    the CIA untouched."""
    cbm, _ = rig("1581")
    drive = cbm.drive
    cra = drive.cia.cra
    drive.load(bench.SDRGAP, bench.drivecode("sdrgap_1581"))
    drive.call(bench.SDRGAP)
    simfast.run_drive(drive, drive.cycles + 2_500_000)
    assert drive.halted and drive.mpu.a == bench.ST_NOGO and drive.mpu.x == 0
    assert drive.cia.cra == cra and drive.cycles > 1_500_000


def test_sequence_and_log_decoding():
    """Adapter framing to an ordered byte sequence; log records to rows."""
    raw = bytes(
        [0x00, 0x04, 0x55, 0x00, 0x14, 0x00, 0x00, 0xAA, 0x00, 0x40, 0x00, 0x80]
    )
    seq = bench.sdr_sequence(Stream.parse(raw))
    assert seq == [
        (True, 4),
        (False, 0x55),
        (True, 0x14),
        (False, 0),
        (False, 0xAA),
    ] + [(True, 0x40)]
    rec = bytes([0x20, 0x41, 1, 0, 0x89, 0xFE, 0x80, 0xFD, 0x14, 3, 0x20] + [0] * 5)
    row = bench.sdr_log(rec + rec[:3], "1581")[0]
    assert row["shifted"] and row["kind"] == "meta" and row["t_us"] == 0xFFFF - 0xFD80
    assert bench.sdr_log(rec, "1571")[0]["t_us"] is None
    assert bench.sdr_table([(130, 0x14, 1)]) == b"\x82\x00\x14\x01\0\0\0\xff"


def test_dos_read_retries_until_dos_answers():
    """The log over M-R once the drive is back in DOS; nothing when it never is."""
    calls = []

    class Cbm:
        """M-R that fails twice."""

        def download(self, dev, addr, size):
            calls.append((dev, addr, size))
            if len(calls) < 3:
                raise IOError("busy")
            return bytes(size)

    class Mon:  # pylint: disable=too-few-public-methods
        """A lost monitor."""

        cbm, dev = Cbm(), 9

    assert bench.sdr_dos_read(Mon(), 0x0C00, 32, 1e-3, 0.0) == b""
    assert len(calls) == 2
    assert bench.sdr_dos_read(Mon(), 0x0C00, 32, 1e-3, 0.0) == bytes(32)
    assert (
        bench.sdr_dos_read(
            type("M", (), {"cbm": object(), "dev": 9})(), 0, 4, 1e-3, 0.0
        )
        == b""
    )


def test_line_summarises_a_run():
    out = {
        "gap_ms": 6.5,
        "flags": bench.F_ICR,
        "adapter": "timeout",
        "drive": "atn",
        "sent": 4,
        "first_lost": 1,
        "log": [
            {"seen": True, "shifted": True, "kind": "meta"},
            {"seen": False, "shifted": True, "kind": "meta"},
        ],
    }
    line = bench.sdr_gap_line(out)
    assert "gap 6.5 ms (icr): adapter timeout, drive atn, 1/4 seen, 2/2 shifted" in line
    assert "first lost #1 (meta)" in line
