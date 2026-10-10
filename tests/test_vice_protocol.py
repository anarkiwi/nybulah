"""The VICE binary monitor client and trace decoding against the documented wire
format (VICE manual, "Binary Monitor"), with no emulator."""

import os
import socket
import struct
import subprocess
import threading

import numpy as np
import pytest

from nybulah import mfmstream as ms
from nybulah import r1581, vice
from nybulah import vicebench as vb


def doc_bytes(text):
    return bytes.fromhex(text.replace("|", " "))


def test_request_framing_matches_manual_example():
    """The manual's checkpoint example: temporary exec checkpoint $FCE2-$FCE3."""
    body = vice.checkpoint_body(0xFCE2, 0xFCE3, temporary=True)
    assert body[-1] == vice.MAIN
    framed = vice.request(vice.CHECKPOINT_SET, body[:-1], 0x1234DEAD)
    assert framed == doc_bytes(
        "02 | 02 | 08 00 00 00 | ad de 34 12 | 12 | e2 fc | e3 fc | 01 | 01 | 04 | 01"
    )


def test_register_info_matches_manual_example():
    """The manual's register response items: PC $E5CF (id 3) and A 0 (id 0)."""
    body = doc_bytes("02 00 | 03 03 cf e5 | 03 00 00 00")
    assert vice.parse_registers(body) == {3: 0xE5CF, 0: 0}


def _named(items):
    out = struct.pack("<H", len(items))
    for rid, name in items:
        raw = name.encode()
        out += bytes([3 + len(raw), rid, 16, len(raw)]) + raw
    return out


def test_parse_names_and_banks():
    assert vice.parse_names(_named([(3, "PC"), (0, "A")])) == {"PC": 3, "A": 0}
    banks = struct.pack("<H", 2)
    for bank, name in ((0, "cpu"), (1, "ram")):
        banks += bytes([3 + len(name)]) + struct.pack("<HB", bank, len(name))
        banks += name.encode()
    assert vice.parse_banks(banks) == {"cpu": 0, "ram": 1}


def history_body(rows):
    """CPUHISTORY response of (clock, pc, a, x, y, opcode bytes) rows: registers PC,
    A, X, Y, SP, FL, LIN, CYC as the manual lays them out."""
    out = struct.pack("<I", len(rows))
    for clock, pc, a, x, y, op in rows:
        regs = [(3, pc), (0, a), (1, x), (2, y), (4, 0xFF), (5, 0x24), (53, 0), (54, 0)]
        item = struct.pack("<H", len(regs))
        item += b"".join(struct.pack("<BBH", 3, rid, v) for rid, v in regs)
        item += struct.pack("<QB", clock, 4) + bytes(op) + bytes(4 - len(op))
        out += bytes([len(item)]) + item
    return out


NAMES = {"PC": 3, "A": 0, "X": 1, "Y": 2, "SP": 4, "FL": 5, "LIN": 53, "CYC": 54}
LDA_WD, STA_SDR, BIT_ICR, LDX_WD3, STX_SDR, NOP = (
    (0xAD, 0x00, 0x60),
    (0x8D, 0x0C, 0x40),
    (0x2C, 0x0D, 0x40),
    (0xAE, 0x03, 0x60),
    (0x8E, 0x0C, 0x40),
    (0xEA,),
)


def sample_history():
    return vice.parse_history(
        history_body(
            [
                (100, 0x0400, 0x00, 0, 0, LDA_WD),
                (104, 0x0403, 0x83, 0, 0, BIT_ICR),
                (108, 0x0406, 0x83, 0, 0, LDX_WD3),
                (112, 0x0409, 0x83, 0x5A, 0, STX_SDR),
                (116, 0x040C, 0x83, 0x5A, 0, STA_SDR),
                (120, 0x040F, 0x83, 0x5A, 0, NOP),
            ]
        ),
        NAMES,
    )


def test_parse_history_fields():
    h = sample_history()
    assert h["clock"].tolist() == [100, 104, 108, 112, 116, 120]
    assert h["pc"][3] == 0x0409 and h["x"][3] == 0x5A and h["sp"][0] == 0xFF
    assert h["op"][0].tolist() == list(LDA_WD)
    assert len(vice.parse_history(struct.pack("<I", 0), NAMES)) == 0


def test_accesses_values_loaded_and_stored():
    """A load's value is the next row's register, a store's its own, others -1."""
    at, addr, value = vice.accesses(sample_history(), 0x4000, 0x6003)
    assert at.tolist() == [0, 1, 2, 3, 4]
    assert addr.tolist() == [0x6000, 0x400D, 0x6003, 0x400C, 0x400C]
    assert value.tolist() == [0x83, -1, 0x5A, 0x5A, 0x83]
    ops = np.array([[0xA5, 0x10, 0], [0x4C, 0x00, 0x05], [0x1C, 0, 0]], np.uint8)
    probe = np.zeros(3, vice.HISTORY_DTYPE)
    probe["op"] = ops
    assert vice.operands(probe).tolist() == [-1, 0x0500, -1]


def test_io_trace_and_summary():
    trace = vb.io_trace(sample_history())
    assert [r["reg"] for r in trace] == ["WDCMD", "ICR", "WDDAT", "SDR", "SDR"]
    lines = vb.summarise(trace, limit=1)
    assert lines[0].split() == ["0", "$0400", "WDCMD", "$83"]
    assert lines[1].split()[-2:] == ["SDR", "$83"]
    assert vb.summarise([]) == []


def test_wd_commands_gaps():
    """A command, a DRQ status, three data writes: the gap after the second write."""
    sta_cmd, sta_dat = (0x8D, 0x00, 0x60), (0x8D, 0x03, 0x60)
    h = vice.parse_history(
        history_body(
            [
                (0, 0x0300, 0x88, 0, 0, sta_cmd),
                (10, 0x0303, 0x00, 0, 0, LDA_WD),
                (14, 0x0306, 0x83, 0, 0, sta_dat),
                (80, 0x0309, 0x83, 0, 0, sta_dat),
                (180, 0x030C, 0x83, 0, 0, sta_dat),
                (190, 0x030F, 0x83, 0, 0, LDA_WD),
                (194, 0x0312, 0x80, 0, 0, NOP),
            ]
        ),
        NAMES,
    )
    cmds = vb.wd_commands(h)
    assert len(cmds) == 1
    cmd = cmds[0]
    assert (
        cmd["command"] == "$88" and cmd["first_drq"] == 10 and cmd["first_data"] == 14
    )
    assert (cmd["data"], cmd["max_gap"], cmd["status"]) == (3, 100, "$80")
    assert cmd["max_gap_pcs"] == "0309 030C"


def test_adapter_raw_parses_as_a_stream():
    """Received bytes with CLK reads frame as the adapter's output: metadata escaped,
    data zero escaped, the done code at the end."""
    data = [ms.M_START, 0x00, 0x55, 0x40]
    lines = [0x00, 0xC7, 0xC7, 0x87]
    raw = vice.adapter_raw(data, lines)
    assert raw == bytes([0, ms.M_START, 0, 0, 0x55, 0, 0x40, 0, 0x80])
    got = ms.MfmStream.parse(raw)
    assert (got.adapter, got.drive_end) == ("done", "done")


def test_stream_code_places_list_and_tmo():
    code = bytes(3) + vb.STREAM_TAG + bytes(r1581.SPLIT + 10)
    block = ms.command_list([ms.entry(ms.OP_READ_TRACK, 39, rep=2)])
    (lo_at, lo), (hi_at, hi) = vb.stream_code(code, block, 20, 0x0782)
    assert (lo_at, hi_at, len(hi)) == (r1581.CODE_BASE, 0x0782, len(code) - r1581.SPLIT)
    assert lo[7 : 7 + len(block)] == block and lo[7 + vb.TAG_TMO] == 20


def test_loader_and_memspace(tmp_path):
    (tmp_path / "x.bin").write_bytes(b"\x01\x02")
    assert vb.loader(tmp_path)("x") == b"\x01\x02"
    assert vb.loader() is vb.drivecode
    assert [vice.memspace(u) for u in (None, 8, 9, 11)] == [0, 1, 2, 4]
    with pytest.raises(ValueError):
        vice.memspace(12)


def test_command_line_units():
    args = vice.command_line("x64sc", 6502, {9: ("1581", "d.d81")}, 1000, warp=False)
    assert "-warp" not in args and args[args.index("-monchislines") + 1] == "1000"
    assert args[args.index("-drive9type") + 1] == "1581" and "-drive9truedrive" in args
    assert args[args.index("-9") + 1] == "d.d81"
    assert args[args.index("-drive8type") + 1] == "0"
    assert "ip4://127.0.0.1:6502" in args


def test_listening_port_of_a_process():
    """The port a process listens on, from its own sockets only."""
    with socket.socket() as other, socket.socket() as srv:
        other.bind(("127.0.0.1", 0))
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        assert vice.listening_port(os.getpid()) == srv.getsockname()[1]
    with subprocess.Popen(["sleep", "30"]) as idle:
        try:
            assert vice.listening_port(idle.pid) is None
        finally:
            idle.kill()


def response(kind, body=b"", rid=vice.EVENT, error=0):
    return vice.HEADER.pack(vice.STX, vice.API, len(body), kind, error, rid) + body


class FakeVice:
    """A scripted server: answers each request with a function of (command, body,
    request id) returning the bytes to send."""

    def __init__(self, answer):
        self.client, self.server = socket.socketpair()
        self.answer, self.seen = answer, []
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _read(self, n):
        out = b""
        while len(out) < n:
            try:
                chunk = self.server.recv(n - len(out))
            except OSError:
                return None
            if not chunk:
                return None
            out += chunk
        return out

    def _run(self):
        while (head := self._read(vice.REQUEST.size + 1)) is not None:
            _, _, length, rid = vice.REQUEST.unpack(head[:-1])
            body = self._read(length) if length else b""
            self.seen.append((head[-1], body))
            try:
                self.server.sendall(self.answer(head[-1], body, rid))
            except OSError:
                return

    def close(self):
        """Drop the server end."""
        self.server.close()


def test_monitor_commands_events_and_errors():
    """Replies match by request id; events update state; errors raise."""

    def mem(body, rid):
        start, end = struct.unpack_from("<HH", body, 1)
        n = end - start + 1
        return response(
            vice.MEM_GET, struct.pack("<H", n) + bytes([start & 0xFF]) * n, rid
        )

    def checkpoint(_, rid):
        hit = response(vice.R_CHECKPOINT, struct.pack("<I", 7))
        stop = response(vice.R_STOPPED, b"\x03\x05")
        return hit + stop + response(vice.CHECKPOINT_SET, struct.pack("<I", 7), rid)

    replies = {
        vice.MEM_GET: mem,
        vice.CHECKPOINT_SET: checkpoint,
        vice.EXIT: lambda _, rid: response(vice.EXIT, rid=rid)
        + response(vice.R_RESUMED, b"\x00\x05"),
        vice.INFO: lambda _, rid: response(
            vice.INFO, bytes([4, 3, 10, 0, 0, 4, 0, 0, 0, 0]), rid
        ),
        vice.HISTORY: lambda _, rid: response(
            vice.HISTORY, history_body([(5, 0x300, 1, 2, 3, NOP)]), rid
        ),
        vice.QUIT: lambda _, rid: response(0, rid=rid, error=0x8F),
    }

    def answer(cmd, body, rid):
        return replies.get(cmd, lambda _, r: response(cmd, rid=r))(body, rid)

    fake = FakeVice(answer)
    mon = vice.BinaryMonitor(fake.client, timeout=2)
    got = mon.mem_get(0x10, vice.MEM_CHUNK + 2)
    assert len(got) == vice.MEM_CHUNK + 2 and set(got) == {0x10}
    assert len(fake.seen) == 2
    mon.mem_set(0x1000, bytes(3), space=2, effects=True)
    assert fake.seen[-1][1] == struct.pack("<BHHBH", 1, 0x1000, 0x1002, 2, 0) + bytes(3)
    assert mon.checkpoint(0x0500, op=vice.OP_STORE, space=2) == 7
    assert (mon.hits, mon.stopped, mon.pc) == ([7], True, 0x0503)
    assert fake.seen[-1][1] == struct.pack("<HHBBBBB", 0x500, 0x500, 1, 1, 2, 0, 2)
    mon.resume()
    assert mon.stopped is False and not mon.wait_stopped(0.05)
    assert mon.info() == (3, 10, 0, 0)
    assert mon.history(10, NAMES)["pc"].tolist() == [0x300]
    with pytest.raises(vice.ViceError):
        mon.command(vice.QUIT)
    mon.quit()
    mon.delete(7)
    mon.feed("SYS4864\r")
    assert fake.seen[-1] == (vice.KEYBOARD_FEED, b"\x08SYS4864\r")
    fake.close()
    with pytest.raises((vice.ViceError, OSError)):
        mon.stop()


def test_bad_response_start():
    fake = FakeVice(lambda cmd, body, rid: b"\x01" + response(cmd, rid=rid)[1:])
    with pytest.raises(vice.ViceError):
        vice.BinaryMonitor(fake.client, timeout=2).stop()


def test_main_prints_report(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(vb, "writes", lambda *a: calls.append(a) or {"w": 1})
    monkeypatch.setattr(vb, "stream", lambda *a, **k: calls.append((a, k)) or {"s": 2})
    assert vb.main(["writes", "--cylinder", "3"]) == {"w": 1}
    assert vb.main(["stream", "--code2", "0x782", "--head", "4"]) == {"s": 2}
    assert calls[0] == (None, 3, 0)
    args, kw = calls[1]
    assert args[4] == 0x782 and kw == {"under": None, "head_writes": 4}
    assert '"s": 2' in capsys.readouterr().out
