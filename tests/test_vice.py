"""This tree's drive code on VICE's true drive emulation (real DOS ROMs, 6502, CIA,
VIA, WD177x) through the binary monitor; skipped unless x64sc and x128 are installed
(the Dockerfile's test-vice image)."""

import numpy as np
import pytest

from nybulah import r1581, vice
from nybulah import vicebench as vb
from nybulah.analysis import mfm
from nybulah.formats import d71, d81

pytestmark = [
    pytest.mark.vice,
    pytest.mark.skipif(
        not (vice.available("x64sc") and vice.available("x128")),
        reason="VICE (x64sc, x128) not installed",
    ),
]

SCRATCH = 0x0600
TIMER_B = vice.CIA1581 + 6
CYL = 39


@pytest.fixture(name="image", scope="module")
def image_fixture():
    return vb.random_d81(3)


def side_sectors(image, cylinder, side):
    return image.data[d81.side_rows(cylinder, side)].reshape(mfm.SECTORS, -1)


def test_drive_memspace_calls_and_cycles(image):
    """Memory, registers, calls, checkpoints, cycles and history on the drive CPU."""
    with vb.Bench(image) as b:
        v, mon = b.vice, b.mon
        assert v.mon.info()[:2] >= (3, 5)
        mon.write(SCRATCH + 0x80, bytes(range(32)))
        assert mon.read(SCRATCH + 0x80, 32) == bytes(range(32))
        # clc; adc #$10; inx; dey; rts
        mon.write(SCRATCH, bytes([0x18, 0x69, 0x10, 0xE8, 0x88, 0x60]))
        cp = v.mon.checkpoint(SCRATCH, stop=False, space=vb.SPACE)
        assert mon.jsr(SCRATCH, a=1, x=2, y=3) == (0x11, 3, 2)
        assert cp in v.mon.hits
        v.mon.delete(cp)
        history = mon.history()
        ran = history["pc"][(history["pc"] >= SCRATCH) & (history["pc"] < SCRATCH + 6)]
        assert ran.tolist() == [
            SCRATCH,
            SCRATCH + 1,
            SCRATCH + 3,
            SCRATCH + 4,
            SCRATCH + 5,
        ]
        c0, timer_b = v.clock(vb.SPACE), mon.read(TIMER_B, 2)
        mon.run_cycles(100_000)
        assert v.clock(vb.SPACE) - c0 >= 100_000
        assert mon.read(TIMER_B, 2) != timer_b


def test_1581_reads_match_d81(image):
    """Restore, Seek, Read Address and Read Sector of both sides against the D81."""
    with vb.Bench(image) as b:
        b.place(CYL, 0)
        assert abs(b.drive.period_us - r1581.NOMINAL_US) < r1581.NOMINAL_US // 100
        trace = b.drive.home_trace
        assert trace["result"] == 0 and trace["settled_t0"]
        assert b.mon.read(vb.WD_STATUS + 1, 1)[0] == CYL
        for side in (0, 1):
            b.drive.side(side)
            ident = b.drive.read_id()
            assert (ident["c"], ident["h"], ident["crc_ok"]) == (CYL, side, True)
            want = side_sectors(image, CYL, side)
            for r in range(1, mfm.SECTORS + 1):
                data, status = b.drive.read_sector(CYL, r)
                assert not status & (mfm.ST_RNF | mfm.ST_CRC | mfm.ST_LOST)
                assert np.array_equal(data, want[r - 1])


def test_1581_write_track_lands_in_d81(image):
    """Write Track of a standard layout with constant sectors; the D81 VICE writes
    back holds them and nothing else changed."""
    cyl, side = 12, 1
    fill = np.repeat(np.arange(1, mfm.SECTORS + 1, dtype=np.uint8), mfm.SECTOR_BYTES)
    fill = fill.reshape(mfm.SECTORS, -1)
    plan = mfm.plan_track(mfm.standard_layout(cyl, side, fill))
    assert not plan.writes
    b = vb.Bench(image)
    try:
        b.place(cyl, side)
        status = b.drive.write_track(plan.image)
        assert not status & (r1581.ST_WP | mfm.ST_LOST)
        for r in range(1, mfm.SECTORS + 1):
            assert np.array_equal(b.drive.read_sector(cyl, r)[0], fill[r - 1])
        back = b.image()
    finally:
        b.close()
    rows = d81.side_rows(cyl, side)
    assert np.array_equal(back.data[rows].reshape(fill.shape), fill)
    rest = np.setdiff1d(np.arange(d81.D81_SECTORS), rows)
    assert np.array_equal(back.data[rest], image.data[rest])


def test_1581_writes_report():
    """Write Track (pattern, layout) then Write Sector with both marks, every data
    byte served within its byte time; the D81 VICE writes back holds the sectors."""
    report = vb.writes(cylinder=CYL, side=0, seed=5)
    assert report["pattern"]["ok"] and report["layout"]["ok"] and report["d81_rest_ok"]
    for case in (report["sectors_deleted_1"], report["sectors_deleted_0"]):
        assert not case["lost_data"], case["last_commands"]
        assert case["written"] == mfm.SECTORS and case["data_ok"] and case["marks_ok"]
    assert report["d81_side_ok"]


def test_1581_read_track_streams_to_c128():
    """mfmstream_1581 Read Track, two revolutions, to the emulated C128's fast serial
    port: the parsed stream ends cleanly and every sector matches the D81."""
    report = vb.stream(cylinder=CYL, side=1, revolutions=2, head_writes=2, seed=7)
    assert report["returned"]["a"] == "$40"
    assert (report["adapter"], report["drive_end"]) == ("done", "done")
    assert [c["bytes"] for c in report["commands"]] == [mfm.TRACK_BYTES] * 2
    assert report["sectors_ok"] == [mfm.SECTORS] * 2
    assert report["metadata_head"][0] == "$04"
    assert any("SDR   $04" in line for line in report["head"])


def test_1571_via_registers_match_cpu(tmp_path):
    """On a 1571 the CPU's VIA reads equal the monitor's peeks; cycles at 1 MHz."""
    path = tmp_path / "disk.d71"
    path.write_bytes(d71.write_d71(d71.D71(np.zeros((d71.D71_SECTORS, 256)))))
    with vice.Vice({8: ("1571", path)}) as v:
        mon = vice.DriveMonitor(v, 8, model="1571").start()
        # lda $1c02; ldx $1802; ldy $1c03; rts
        mon.write(
            SCRATCH, bytes([0xAD, 0x02, 0x1C, 0xAE, 0x02, 0x18, 0xAC, 0x03, 0x1C])
        )
        mon.write(SCRATCH + 9, bytes([0x60]))
        a, x, y = mon.jsr(SCRATCH)
        assert (a, x, y) == (
            mon.read(0x1C02, 1)[0],
            mon.read(0x1802, 1)[0],
            mon.read(0x1C03, 1)[0],
        )
        space = vice.memspace(8)
        c0 = v.clock(space)
        mon.sleep(0.05)
        assert v.clock(space) - c0 >= 50_000
