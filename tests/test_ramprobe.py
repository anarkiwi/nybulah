import json

import pytest

from nybulah import ramprobe
from nybulah.opencbm import OpenCBMError
from nybulah.sim import Drive1541, Drive1571
from nybulah.simhost import SimCBM


def probed(drive, **kw):
    cbm = SimCBM(drive, dev=drive.device)
    before = bytes(drive.store)
    out = ramprobe.probe(cbm, drive.device, **kw)
    assert bytes(drive.store) == before
    return out, {b["addr"]: b for b in out["blocks"]}


def test_1541_expansion_and_base_mirrors():
    out, blocks = probed(Drive1541(device=10))
    assert out["model"] == "1541" and out["ram"] == [[0x8000, 0xA000]]
    assert blocks[0x0800]["kind"] == "io" and blocks[0x6800]["kind"] == "io"
    assert blocks[0x2000] == {"addr": 0x2000, "kind": "ram", "alias": 0x0000}
    assert blocks[0x6400]["alias"] == 0x0400
    assert blocks[0xA000]["kind"] == "none" and blocks[0xC000]["kind"] == "none"
    assert ramprobe.expansion_base(out, 0x2000) == 0x8000
    assert ramprobe.expansion_base(out, 0x2001) is None


def test_1571_expansion():
    out, blocks = probed(Drive1571(device=8))
    assert out["model"] == "1571" and out["ram"] == [[0x6000, 0x8000]]
    assert {blocks[a]["kind"] for a in range(0x0800, 0x6000, 0x400)} == {"io"}
    assert {blocks[a]["kind"] for a in range(0x8000, 0x10000, 0x400)} == {"none"}


def test_stock_1541_has_no_expansion():
    out, _ = probed(Drive1541(device=8, expansion=()))
    assert out["ram"] == [] and ramprobe.expansion_base(out, 1) is None


def test_partially_decoded_expansion_aliases():
    out, blocks = probed(Drive1541(device=8, expansion=((0x8000, 0xA000, 0x800),)))
    assert out["ram"] == [[0x8000, 0x8800]]
    assert [blocks[a]["alias"] for a in range(0x8000, 0xA000, 0x800)] == [
        None,
        0x8000,
        0x8000,
        0x8000,
    ]


def test_markers_avoid_existing_contents():
    d = Drive1541(device=8)
    d.load(0x03F0, b"\x00\xff")
    d.load(0x83F0, bytes((24, 24 ^ 0xFF)))
    out, blocks = probed(d)
    assert out["ram"] == [[0x8000, 0xA000]] and blocks[0x2000]["alias"] == 0


def test_marker_candidates_distinct():
    avoid = {bytes((5, 5 ^ k)) for k in (0xFF, 0x55, 0xAA)}
    assert ramprobe.marker(5, avoid) == bytes((5, 5 ^ 0x0F))
    with pytest.raises(AssertionError):
        ramprobe.marker(5, avoid | {bytes((5, 5 ^ 0x0F))})


class Ident:
    """cbm stub answering identify."""

    def __init__(self, code, desc):
        self.ident = code, desc

    def identify(self, _dev):
        return self.ident


@pytest.mark.parametrize(
    "code,desc,model", [(0, "1541", "1541"), (2, "1571", "1571"), (-1, "1570", "1571")]
)
def test_identify_model(code, desc, model):
    assert ramprobe.identify_model(Ident(code, desc), 8) == model


def test_rejects_bad_arguments():
    with pytest.raises(ValueError, match="unsupported"):
        ramprobe.identify_model(Ident(3, "1581"), 8)
    with pytest.raises(ValueError, match="unknown model"):
        ramprobe.io_mask("1581")
    cbm = SimCBM()
    for kw in (
        {"block": 0x300},
        {"block": 0x1000},
        {"start": 0x900},
        {"offset": 0x3FF},
    ):
        with pytest.raises(ValueError):
            ramprobe.probe(cbm, 8, "1541", **kw)
    with pytest.raises(ValueError, match="256"):
        ramprobe.probe(cbm, 8, "1541", start=0, block=0x80, offset=0x10)
    with pytest.raises(OpenCBMError):
        ramprobe.probe(cbm, 9)


def test_verify_flags_mismatch():
    cbm = SimCBM()
    data = bytes(range(256)) * 8
    cbm.drive.load(0x8000, data)
    assert ramprobe.verify(cbm, 8, 0x8000, data) == {"checked": 2, "mismatched": []}
    cbm.drive.load(0x8401, b"\xee")
    assert ramprobe.verify(cbm, 8, 0x8000, data)["mismatched"] == [0x8400]


def test_main_prints_json(capsys):
    out = ramprobe.main(
        ["--dev", "8", "--start", "0x6000", "--end", "0x8000"], SimCBM(Drive1571())
    )
    assert json.loads(capsys.readouterr().out) == out and out["ram"] == [
        [0x6000, 0x8000]
    ]
