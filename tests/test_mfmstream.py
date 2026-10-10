"""1581 stream decoding (nybulah.mfmstream): stamps, chunk packing, records."""

import numpy as np
import pytest

from nybulah import mfmstream as ms
from nybulah.stream import ESC


def stamp(us_epoch, elapsed, flag=0):
    """A drive stamp for elapsed microseconds into a wrap: timer B = ~elapsed."""
    tb = 0xFFFF - elapsed
    return bytes([us_epoch, flag, tb >> 8, tb & 0xFF, tb >> 8])


def pack(payload):
    """The drive's pack: bits MSB first in six-bit chunks, zero padded."""
    bits = np.unpackbits(np.frombuffer(bytes(payload), np.uint8))
    bits = np.concatenate([bits, np.zeros(-len(bits) % 6, np.uint8)]).reshape(-1, 6)
    return bytes(int(b) << 2 | ms.CHUNK for b in np.packbits(bits, axis=1)[:, 0] >> 2)


def frame(data=b"", meta=()):
    """Adapter output: data bytes then metadata (ESC m), then ESC done."""
    out = bytearray()
    for b in data:
        out += bytes([ESC, ESC]) if b == ESC else bytes([b])
    for m in meta:
        out += bytes([ESC, m])
    return bytes(out + bytes([ESC, 0x80]))


def test_stamp_rules():
    assert ms.stamp_us(stamp(3, 15)) == 3 * ms.TB_WRAP + 15
    assert ms.stamp_us(stamp(3, 15, flag=2)) == 4 * ms.TB_WRAP + 15
    assert ms.stamp_us(stamp(3, ms.WINDOW_US)) == 4 * ms.TB_WRAP + ms.WINDOW_US
    assert ms.stamp_us(stamp(3, ms.WINDOW_US + 1)) == 3 * ms.TB_WRAP + 10
    lo_high = bytes([1, 0, 0x12, 0xF0, 0x11])  # borrow between the reads
    assert ms.stamp_us(lo_high) == ms.TB_WRAP + ((0xFF - 0x11) << 8 | 0x0F)
    lo_low = bytes([1, 0, 0x12, 0x02, 0x11])
    assert ms.stamp_us(lo_low) == ms.TB_WRAP + ((0xFF - 0x12) << 8 | 0xFD)


def test_unwrap_across_the_wrap_counter():
    period = ms.EPOCH * ms.TB_WRAP
    raw = [period - 10, 5, 100, period - 1]
    got = ms.unwrap_us(raw)
    assert list(np.diff(got)) == [15, 95, period - 101]


@pytest.mark.parametrize("n", [1, 3, 5, 14])
def test_chunks_round_trip(n):
    payload = np.random.default_rng(n).integers(0, 256, n, dtype=np.uint8).tobytes()
    chunks = pack(payload)
    assert len(chunks) == ms.chunks_for(n)
    assert ms.unpack([c >> 2 for c in chunks], n) == payload


def test_parse_commands_and_index():
    data = bytes([0x4E] * 5 + [ESC] + [1, 2, 3])
    rec1 = stamp(0, 100) + stamp(0, 300) + bytes([0x00, 0, 6, 0])
    rec2 = stamp(0, 400) + stamp(0, 500) + bytes([0x10, 1, 3, 0])
    meta = [ms.M_START, ms.M_KEEP, ms.M_INDEX, *pack(stamp(0, 50))]
    meta += [ms.M_REC, *pack(rec1), ms.M_REC, *pack(rec2), 0x44]
    raw = frame(data, meta)
    got = ms.MfmStream.parse(raw, reply=b"\x44\x02\x80")
    assert got.adapter == "done" and got.drive_end == "timeout" and not got.complete
    assert got.keepalives == 1 and got.reply == (0x44, 2, 0x80)
    diag = got.diagnosis()
    assert diag["drive"] == "timeout" and diag["entries_started"] == 2
    assert diag["wd_status"] == 0x80 and diag["data_bytes"] == len(data)
    assert diag["raw_bytes"] == len(raw)
    assert diag["codes"] == {
        "start": 1,
        "rec": 2,
        "keep": 1,
        "index": 1,
        "end_timeout": 1,
    }
    assert list(got.index_us) == [50]
    first, second = got.commands
    assert bytes(first.data) == data[:6] and bytes(second.data) == data[6:]
    assert (first.t_first, first.t_end, first.status) == (100, 300, 0)
    assert second.timeout and second.status == 0x10


@pytest.mark.parametrize(
    "reply, drive",
    [(None, "no reply"), ((0xFF, 0, 0x80), "never saw the host's go")]
    + [((0x00, 0, 0), "reply $00 is no end code"), ((0x40, 1, 0), "done")],
)
def test_diagnosis_names_the_reply(reply, drive):
    raw = bytes([ESC, ms.M_START, 0x4E, ESC, 0x8C])
    got = ms.MfmStream.parse(raw, reply=reply)
    diag = got.diagnosis()
    assert diag["drive"] == drive and diag["codes"] == {"start": 1}
    assert got.adapter == "timeout" and diag["data_bytes"] == 1


def test_list_entries():
    assert ms.entry(ms.OP_READ_SECTOR, 3, 1, 10, True) == bytes([0x88, 3, 1, 0x8A])
    with pytest.raises(ValueError):
        ms.entry(ms.OP_READ_TRACK, rep=0)
    entries = [ms.entry(ms.OP_INDEX)] * (ms.LIST_ENTRIES - 1)
    assert ms.command_list(entries)[-1] == ms.OP_END
    with pytest.raises(ValueError):
        ms.command_list(entries + [ms.entry(ms.OP_INDEX)])
