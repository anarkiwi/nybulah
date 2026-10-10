import json

import numpy as np
import pytest

from nybulah import cli, ramtest
from nybulah.monitor import drivecode
from nybulah.sim import Drive1541, Drive1571, Drive1581
from nybulah.simhost import SimMonitor
from nybulah.simx import adapter

SMALL = {"t": np.arange(2, 8)}


def monitor(drive):
    mon = SimMonitor(drive)
    mon.code = drivecode("monitor_s1")
    return mon


class StuckBit(Drive1541):
    """Reads of addr return mask bits forced to value."""

    def __init__(self, addr, mask, value, **kw):
        super().__init__(**kw)
        self.fault = (addr, mask, value)

    def read(self, addr):
        v = super().read(addr)
        a, mask, value = self.fault
        return v & ~mask | value if addr == a else v


class Coupled(Drive1541):
    """A rising aggressor bit sets the same bit of the victim (idempotent CF)."""

    def __init__(self, aggressor, victim, bit, **kw):
        super().__init__(**kw)
        self.pair = (aggressor, victim, bit)

    def write(self, addr, value):
        aggressor, victim, bit = self.pair
        rising = addr == aggressor and value & bit and not self.read(addr) & bit
        super().write(addr, value)
        if rising:
            super().write(victim, self.read(victim) | bit)


@pytest.mark.parametrize(
    "cls, runs, places",
    [
        (
            Drive1541,
            {"base": [[0x200, 0x500]], "expansion": [[0x8000, 0xA000]]},
            [0x200, 0x8000],
        ),
        (
            Drive1571,
            {"base": [[0x200, 0x500]], "expansion": [[0x6000, 0x8000]]},
            [0x200, 0x6000],
        ),
        (Drive1581, {"ram": [[0x200, 0x500], [0x800, 0x2000]]}, [0x200, 0x800]),
    ],
)
def test_clean_ram_passes_and_is_restored(cls, runs, places):
    drive = cls(device=8)
    rng = np.random.default_rng(1)
    spans = [r for rs in runs.values() for r in rs]
    for lo, hi in spans:
        drive.load(lo, rng.integers(0, 256, hi - lo, dtype=np.uint8).tobytes())
    before = [drive.dump(lo, hi - lo) for lo, hi in spans]
    out = ramtest.RamTest(monitor(drive), cls.MODEL).run()
    assert [drive.dump(lo, hi - lo) for lo, hi in spans] == before
    assert out["ok"] and out["placements"] == places
    assert {k: v["runs"] for k, v in out["regions"].items()} == runs
    for r in out["regions"].values():
        assert r["failures"] == r["transfer"]["mismatches"] == 0
        assert r["bytes"] == sum(hi - lo for lo, hi in r["runs"])
        assert r["transfer"]["bytes"] == 2 * r["bytes"]
        assert r["passes"] == len(ramtest.BACKGROUNDS)


@pytest.mark.parametrize("bg", list(ramtest.BACKGROUNDS))
def test_drive_writes_the_host_background(bg):
    drive = Drive1541(device=8)
    table = [0x04, 0x80, 0x9F]
    march = ramtest.March(monitor(drive), 0x02, table)
    for d in (0, 1):
        per_page, log = march.element((d == 1, None, d), bg)
        assert not per_page.any() and log.size == 0
        for page in table:
            addr = np.arange(page << 8, (page + 1) << 8)
            want = (ramtest.background(addr, bg) ^ 0xFF * d).astype(np.uint8)
            assert drive.dump(page << 8, 256) == want.tobytes()


def test_stuck_bit_is_located():
    drive = StuckBit(0x0642, 0x08, 0x08, device=8)
    out = ramtest.RamTest(monitor(drive), "1541", SMALL, ["solid"]).run()
    region = out["regions"]["t"]
    assert not out["ok"] and out["unsafe_pages"] == [0x0600]
    assert out["placements"] == [0x0200, 0x0400]
    assert region["failing_pages"] == {0x0600: 6}
    assert region["addresses"] == [
        {
            "addr": 0x0642,
            "count": 6,
            "xor_or": 8,
            "xor_and": 8,
            "stuck0": 0,
            "stuck1": 8,
        }
    ]
    assert region["stuck1"] == 8 and region["transfer"]["first"] == [0x0642]
    assert region["address_check"]["first"] == [0x0642, 0x0642]


def test_coupling_fault_is_located():
    drive = Coupled(0x0510, 0x0511, 0x01, device=8)
    out = ramtest.RamTest(monitor(drive), "1541", SMALL, ["solid"]).run()
    region = out["regions"]["t"]
    assert region["failing_pages"] == {0x0500: region["failures"]}
    assert region["failures"] > 0
    assert {a["addr"] for a in region["addresses"]} == {0x0511}
    assert region["xor_or"] == region["xor_and"] == 0x01
    assert {e["element"] for e in region["logged"]} <= {1, 3, 5}


def test_address_alias_is_located():
    drive = Drive1541(device=8, expansion=((0x8000, 0xA000, 0x1000),))
    out = ramtest.RamTest(monitor(drive), "1541", backgrounds=["addr_hi"]).run()
    exp, base = out["regions"]["expansion"], out["regions"]["base"]
    assert out["unsafe_pages"] == list(range(0x8000, 0xA000, 0x100))
    assert out["placements"] == [0x0200]
    assert base["untested"] == [0x0200, 0x0300] and base["failures"] == 0
    assert sorted(exp["failing_pages"]) == list(range(0x8000, 0xA000, 0x100))
    assert {e["addr"] >> 12 for e in exp["logged"]} <= {0x8, 0x9}
    assert exp["xor_or"] & 0x10 and exp["address_check"]["xor_and"] == 0x10
    assert exp["address_check"]["first"][0] == 0x8000
    assert exp["transfer"]["mismatches"] and exp["transfer"]["first"][0] >> 12 == 0x8


def test_log_is_capped_but_counts_are_complete():
    drive = Drive1541(device=8)
    march = ramtest.March(monitor(drive), 0x06, [0x02, 0x03])
    march.element((False, None, 0), "solid")
    drive.load(0x0200, b"\x01" * 512)
    per_page, log = march.element((False, 0, None), "solid")
    assert per_page.tolist() == [256, 256] and len(log) == ramtest.NLOG
    assert log[:, 0].tolist() == list(range(0x0200, 0x0200 + ramtest.NLOG))
    assert (log[:, 1:] == [0, 1]).all()


def test_regions_avoid_io_monitor_stack_and_zero_page():
    for model in ("1541", "1571", "1581"):
        for name, pages in ramtest.regions(model, 0x300).items():
            assert pages.min() >= ramtest.FIRST_PAGE, name
            assert not np.isin(pages, [5, 6, 7]).any()
    with pytest.raises(ValueError, match="I/O"):
        ramtest.RamTest(monitor(Drive1541()), "1541", {"io": np.array([0x18])})


def test_placements_avoid_unsafe_pages():
    assert ramtest.placements(np.array([2, 3, 4, 8, 9]), 2) == (2, 8)
    assert ramtest.placements(np.array([2, 3, 4, 5]), 2, [2]) == (3,)
    with pytest.raises(ValueError, match="sound RAM"):
        ramtest.placements(np.array([2, 4]), 2)
    with pytest.raises(ValueError, match="at most"):
        ramtest.March(monitor(Drive1541()), 2, np.arange(ramtest.MAXP + 1))


def test_relocation_only_moves_address_high_bytes(monkeypatch):
    code, moved = ramtest.relocatable()
    entry = int.from_bytes(code[1:3].tobytes(), "little") - (ramtest.ORIGINS[0] << 8)
    for page in (0x02, 0x80):
        at = ramtest.relocate(code, moved, page)
        assert int.from_bytes(at[1:3], "little") == (page << 8) + entry
        assert [at[i] for i in range(len(at)) if i not in moved] == [
            code[i] for i in range(len(code)) if i not in moved
        ]
    monkeypatch.setattr(ramtest, "drivecode", lambda n: bytes([2 * n.endswith("11")]))
    with pytest.raises(ValueError, match="high bytes"):
        ramtest.relocatable()


def test_cli_over_the_monitor(capsys):
    cbm = adapter("s1", device=8)
    out = cli.main(
        ["ramtest", "--dev", "8", "--transport", "s1", "--backgrounds", "addr_lo"],
        cbm=cbm,
    )
    assert json.loads(capsys.readouterr().out) == json.loads(json.dumps(out))
    assert out["ok"] and out["model"] == "1541" and out["transport"] == "s1"
    assert out["regions"]["expansion"]["bytes"] == 0x2000
