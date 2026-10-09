"""The 1581 drive (sim.Drive1581): 8520, WD177x and bus in py65 and compiled, hosts."""

import re

import numpy as np
import pytest
from py65.assembler import Assembler
from py65.devices.mpu6502 import MPU
from test_simfast import IMPLEMENTED, LOW, assemble, pair, registers, snapshot
from test_simfast import shift_in, step_both

from nybulah import simcia, simfast, simsrq, simwd, simx
from nybulah.opencbm import IEC_ATN, IEC_CLOCK, IEC_DATA, IEC_SRQ
from nybulah.sim import Bus, Drive1581, IdleFastDrive
from nybulah.simdisk import disk_drive
from nybulah.simhost import SimCBM
from nybulah.simwd import MfmMedia

HIGH81 = (0x00, 0x01, 0x1F, 0x20, 0x3F, 0x40, 0x41, 0x5F, 0x60, 0x61, 0x7F, 0x80)
PAYLOAD = np.arange(512, dtype=np.uint8) % 0xF5
CIA, WD = 0x4000, 0x6000
REV = 400_000


def media():
    return MfmMedia.formatted(
        cylinders=4, sectors=lambda c, s: np.tile(np.roll(PAYLOAD, c), (10, 1))
    )


def wd_access(line):
    """Whether an instruction addresses the WD ($6000-$7FFF)."""
    return any(
        0x6000 <= int(a, 16) < 0x8000 for a in re.findall(r"\$([0-9a-f]{4})", line)
    )


def asm(org, *lines, wdtest=True):
    """Two-pass py65 assembly with "lbl:" labels; wdtest puts a nop before any WD
    access that would start at an address ending in %00 (mfmmacro.src WDTEST)."""
    labels = {}
    for _ in range(2):
        out, a = bytearray(), Assembler(MPU())
        for line in lines:
            if line.endswith(":"):
                labels[line[:-1]] = org + len(out)
                continue
            op, _, arg = line.partition(" ")
            if re.fullmatch(r"[a-z_]\w+", arg):
                line = f"{op} ${labels.get(arg, org):04x}"
            if wdtest and wd_access(line) and (org + len(out)) & 3 == 0:
                out.append(0xEA)
            out += bytes(a.assemble(line, org + len(out)))
    return bytes(out)


def make(**kw):
    return lambda: disk_drive("1581", media(), **kw)


def state(drive):
    """Everything py65, the CIA, the WD and the media hold."""
    vars_ = {k: v for k, v in snapshot(drive).items() if k != "wd"}
    return [
        vars_,
        snapshot(drive.mpu),
        snapshot(drive.cia),
        drive.bus.host_lines,
        drive.wd.w.tobytes(),
        drive.wd.flat.tobytes(),
    ]


def lockstep(ref, fast, n):
    for _ in range(n):
        if ref.halted:
            break
        step_both(ref, fast)
        assert registers(ref) == registers(fast) and ref.cycles == fast.cycles
    assert state(ref) == state(fast)


def test_memory_map():
    d = Drive1581()
    d.load(0x1FFF, b"\x5a")
    assert d.dump(0x1FFF, 1) == b"\x5a" and d.dump(0x2000, 2) == b"\x20\x20"
    assert d.dump(0x3FFF, 1) == b"\x3f" and len(d.store) == 0xA000
    rom = d.store[0x2000]
    d.load(0x8000, bytes([rom ^ 1]))
    assert d.dump(0x8000, 1)[0] == rom and d.dump(0xFFFF, 1)[0] == d.store[0x9FFF]
    d.load(0x6001, b"\x07")
    assert d.dump(0x7FF9, 1) == b"\x07" and d.wd.track == 7
    d.load(0x5FF8, b"\x33")
    assert d.dump(0x4008, 1) == b"\x33"
    assert d.dump(0x400E, 1)[0] & 1 and d.dump(0x400F, 1)[0] & 1


@pytest.mark.parametrize("seed", range(3))
def test_every_opcode_matches_py65(seed):
    rng = np.random.default_rng(seed)
    ref, fast = pair(make())
    ram = rng.integers(0, 256, 0x2000, dtype=np.uint8).tobytes()
    for d in (ref, fast):
        d.store[:0x2000] = ram
    for op in IMPLEMENTED:
        for _ in range(6):
            pc = int(rng.integers(0x200, 0x1FF0))
            lo, hi = int(rng.choice(LOW)), int(rng.choice(HIGH81))
            regs = list(map(int, rng.integers(0, 256, 5)))
            host = int(rng.choice([0, IEC_ATN, IEC_DATA | IEC_CLOCK | IEC_SRQ]))
            for d in (ref, fast):
                d.halted = False
                d.store[pc : pc + 3] = bytes([op, lo, hi])
                d.mpu.pc, (d.mpu.a, d.mpu.x, d.mpu.y, d.mpu.p, d.mpu.sp) = pc, regs
                d.bus.host_lines = host
            step_both(ref, fast)
            assert registers(ref) == registers(fast), hex(op)
    assert state(ref) == state(fast)


def random_program(rng, drive):
    code = rng.integers(0, 256, 0x1000, dtype=np.uint8)
    code[::3] = rng.choice(IMPLEMENTED, len(code[::3]))
    code[1::3] = rng.choice(LOW, len(code[1::3]))
    code[2::3] = rng.choice(HIGH81, len(code[2::3]))
    drive.store[0x200:0x1200] = code.tobytes()
    drive.call(0x200 + int(rng.integers(0, 3)))


@pytest.mark.parametrize("seed", range(4))
def test_random_programs_match_py65(seed):
    rng = np.random.default_rng(seed)
    ref, fast = pair(make(spinup=1000))
    for _ in range(30):
        sub = int(rng.integers(1 << 30))
        for d in (ref, fast):
            random_program(np.random.default_rng(sub), d)
            d.bus.host_lines = IEC_ATN if sub & 1 else 0
        lockstep(ref, fast, 400)


TIMERS = assemble(
    0x300,
    "lda #$7f",
    "sta $400d",
    "lda #$01",
    "sta $4004",
    "lda #$00",
    "sta $4005",
    "lda #$11",
    "sta $400e",
    "lda #$ff",
    "sta $4006",
    "sta $4007",
    "lda #$51",
    "sta $400f",
    "ldx $4006",
    "ldy $4007",
    "stx $0500",
    "sty $0501",
    "nop",
    "nop",
    "nop",
    "ldx $4006",
    "stx $0502",
    "lda #$03",
    "sta $4006",
    "lda #$00",
    "sta $4007",
    "lda #$82",
    "sta $400d",
    "lda #$59",
    "sta $400f",
    "lda $400d",
    "sta $0503",
    "wait:",
    "lda $400d",
    "tax",
    "and #$02",
    "beq wait",
    "stx $0504",
    "lda $400f",
    "sta $0505",
    "lda $400d",
    "sta $0506",
    "rts",
)


def test_timer_b_cascade_and_icr():
    ref, fast = pair(make())
    for d in (ref, fast):
        d.load(0x300, TIMERS)
        d.call(0x300)
    lockstep(ref, fast, 1000)
    out = ref.dump(0x500, 7)
    assert 0xFFFF - (out[0] | out[1] << 8) == 4 // 2
    assert (out[0] - out[2]) & 0xFF == (4 * 4 + 3 * 2) // 2
    assert not out[3] & 0x82 and out[4] & 0x82 == 0x82
    assert not out[5] & 1 and not out[6] & 0x82


def test_timer_b_counts_phi2_one_shot():
    for d in pair(make()):
        cia = d.cia
        cia.write(14, 0, 10)
        cia.read(13, 10)
        cia.write(13, 0x7F, 10)
        cia.write(13, 0x82, 10)
        cia.write(6, 9, 10)
        cia.write(7, 0, 10)
        cia.write(15, 0x19, 20)
        assert cia.read(6, 29) == 0 and cia.read(15, 29) & 1
        assert cia.read(6, 30) == 9 and not cia.read(15, 30) & 1
        assert cia.read(13, 31) == 0x82 and cia.read(13, 32) == 0


def run_snippet(drive, code, host=0, steps=10_000):
    drive.bus.host_lines = host
    drive.load(0x300, code)
    drive.call(0x300)
    simfast.run_drive(drive, drive.cycles + steps * 8, steps=steps)


FLAG_WAIT = assemble(
    0x300, "lda $400d", "wait:", "lda $400d", "and #$10", "beq wait", "sta $0500", "rts"
)


def test_flag_on_atn_falling_edge():
    for d in pair(make()):
        run_snippet(d, FLAG_WAIT, 0, 200)
        assert not d.halted
        d.bus.host_lines = IEC_ATN
        simfast.run_drive(d, d.cycles + 100)
        assert d.halted and d.dump(0x500, 1) == b"\x10"
        run_snippet(d, FLAG_WAIT, IEC_ATN, 200)
        assert not d.halted


@pytest.mark.parametrize("fast", [False, True])
def test_atn_acknowledge_gate_and_data_out(fast):
    d = Drive1581()
    d.fast = fast
    cases = ((0xD5, IEC_ATN, IEC_DATA), (0xD5, 0, 0), (0xC5, IEC_ATN, 0))
    cases += ((0xC7, 0, IEC_DATA), (0xCD, 0, IEC_CLOCK))
    for pb, atn, lines in cases:
        run_snippet(d, assemble(0x300, f"lda #${pb:02x}", "sta $4001", "rts"), atn)
        assert d.bus.lines() & ~IEC_ATN == lines
        port = d.dump(0x4001, 1)[0]
        want = (0x80 if atn else 0) | (0x01 if lines & IEC_DATA else 0)
        assert port & 0x85 == want | (0x04 if lines & IEC_CLOCK else 0)


SHIFT = assemble(
    0x300,
    "lda #$01",
    "sta $4004",
    "lda #$00",
    "sta $4005",
    "lda $4001",
    "ora #$20",
    "and $0400",
    "sta $4001",
    "lda #$41",
    "sta $400e",
    "ldx #$00",
    "next:",
    "lda $0401,x",
    "sta $400c",
    "lda #$08",
    "wait:",
    "bit $400d",
    "beq wait",
    "inx",
    "cpx #$04",
    "bne next",
    "lda #$01",
    "sta $400e",
    "rts",
)


@pytest.mark.parametrize("fsdir", [True, False])
def test_fsdir_puts_cnt_and_sp_on_srq_and_data(fsdir):
    ref, fast = pair(make())
    for d in (ref, fast):
        d.load(0x400, bytes([0xFF if fsdir else 0xDF, 0x55, 0x0F, 0xA0, 0x00]))
        d.load(0x300, SHIFT)
        d.call(0x300)
    seen = set()
    while not ref.halted:
        step_both(ref, fast)
        seen.add(ref.drive_lines())
        s = simfast.pack(fast, -1, False)[0]
        assert simfast.drive_lines(s) == ref.drive_lines()
    assert state(ref) == state(fast)
    want = {0, IEC_SRQ, IEC_DATA, IEC_SRQ | IEC_DATA} if fsdir else {0}
    assert seen == want


SECTOR = asm(
    0x300,
    "lda $4000",
    "and #$fb",
    "sta $4000",
    "lda #$09",
    "sta $6000",
    "busy:",
    "lda $6000",
    "lsr a",
    "bcs busy",
    "lda #$02",
    "sta $6003",
    "lda #$19",
    "sta $6000",
    "seek:",
    "lda $6000",
    "lsr a",
    "bcs seek",
    "lda #$03",
    "sta $6002",
    "lda #$00",
    "sta $fb",
    "lda #$08",
    "sta $fc",
    "ldy #$00",
    "lda #$88",
    "sta $6000",
    "poll:",
    "lda $6000",
    "lsr a",
    "bcc end",
    "lsr a",
    "bcc poll",
    "lda $6003",
    "sta ($fb),y",
    "iny",
    "bne poll",
    "inc $fc",
    "bne poll",
    "end:",
    "asl a",
    "sta $0600",
    "rts",
)


def test_drive_code_reads_a_sector_on_both_cores():
    ref, fast = pair(make())
    for d in (ref, fast):
        d.load(0x300, SECTOR)
        d.call(0x300)
    lockstep(ref, fast, 3000)
    simfast.run_drive(fast, simfast.NEVER)
    while not ref.halted:
        ref.step()
    assert state(ref) == state(fast)
    for d in (ref, fast):
        d.sync()
        assert d.wd.cylinder == 2 and d.wd.bumps == 0 == d.wd.inner_stops
        assert d.dump(0x800, 512) == np.roll(PAYLOAD, 2).tobytes()
        assert d.dump(0x600, 1)[0] & 0x1C == 0
        assert d.wd.violations == 0 and d.wd.early == 0


@pytest.mark.parametrize(
    "org, wdtest, count",
    [(0x300, False, 1), (0x2FF, False, 0), (0x301, False, 1)]
    + [(0x300, True, 0), (0x301, True, 0)],
)
def test_wdtest_violations_are_counted(org, wdtest, count):
    code = asm(org, "lda $6000", "ldx $6003", "sta $7ffd", "rts", wdtest=wdtest)
    for d in pair(make()):
        d.load(org, code)
        d.call(org)
        simfast.run_drive(d, d.cycles + 100)
        assert d.halted and d.wd.violations == count


@pytest.mark.parametrize("fast", [False, True])
def test_motor_side_and_ready_through_port_a(fast):
    d = disk_drive("1581", media(), device=10, spinup=500)
    d.fast = fast
    pa = d.dump(CIA, 1)[0]
    assert pa & 0x18 == 0x10 and not pa & 0x80 and pa & 0x02
    delay = ("ldx #$00", "loop:", "dex", "bne loop", "rts")
    run_snippet(d, asm(0x300, "lda #$fa", "sta $4000", *delay))
    assert d.wd.head == 1 and not d.dump(CIA, 1)[0] & 0x02
    run_snippet(d, asm(0x300, "lda #$fb", "sta $4000", "lda #$48", "sta $6000", *delay))
    assert d.wd.head == 0 and d.dump(CIA, 1)[0] & 0x80 and d.wd.cylinder == 1
    d.wd.write_protect = True
    assert not d.dump(CIA + 1, 1)[0] & 0x40
    d.reset()
    assert not d.wd.busy and d.dump(CIA + 14, 1)[0] & 1


def test_interpreted_kernels_match_compiled(monkeypatch):
    for module in (simfast, simcia, simwd):
        for name, obj in vars(module).items():
            if hasattr(obj, "py_func"):
                monkeypatch.setattr(module, name, obj.py_func)
    test_timer_b_cascade_and_icr()
    test_fsdir_puts_cnt_and_sp_on_srq_and_data(True)
    test_atn_acknowledge_gate_and_data_out(True)
    test_motor_side_and_ready_through_port_a(True)
    rng = np.random.default_rng(9)
    ref, fast = pair(make())
    for _ in range(3):
        sub = int(rng.integers(1 << 30))
        for d in (ref, fast):
            random_program(np.random.default_rng(sub), d)
        lockstep(ref, fast, 200)


def test_simcbm_identifies_a_1581_and_peers_track_the_fast_host_flag():
    bus = Bus()
    cbm = SimCBM(Drive1581(device=9, bus=bus), dev=9)
    peer = IdleFastDrive(bus)
    assert cbm.identify(9) == (3, "1581")
    assert cbm.status(9) == "73,COPYRIGHT CBM DOS V10 1581,00,00"
    for k in range(8):
        assert not peer.fast_host
        cbm.iec_set(IEC_SRQ)
        cbm.iec_release(IEC_SRQ)
        assert peer.bits == (k + 1) % 8
    assert peer.fast_host
    cbm.upload(9, 0x500, b"\x60")
    assert not peer.fast_host
    for _ in range(8):
        cbm.iec_set(IEC_SRQ)
        cbm.iec_release(IEC_SRQ)
    cbm.download(9, 0x500, 1)
    assert not peer.fast_host


RECEIVE = assemble(
    0x300,
    "lda #$01",
    "sta $400e",
    "lda $400c",
    "lda $400d",
    "go:",
    "lda $4001",
    "lsr a",
    "bcc go",
    "lda #$18",
    "sta $4001",
    *shift_in(0x10),
    "lda #$10",
    "sta $4001",
    "rts",
)


def test_srq_write_to_a_timed_1581_with_fast_peers():
    cbm = simsrq.make(model="1581", dev=9, fast_peers=2)
    drive, peers = cbm.drive, cbm.bus.devices[1:]
    assert isinstance(drive, simx.TimedDrive1581) and drive.cyc == 0.5
    drive.load(0x300, RECEIVE)
    drive.call(0x300)
    data = bytes(range(0x30, 0x40))
    cbm.srq2_write(data)
    cbm.settle()
    assert drive.dump(0x500, 16) == data
    assert all(p.fast_host for p in peers)
    cbm.command(9, b"M-E\x00\x03")
    assert not any(p.fast_host for p in peers)
    assert simx.make("1581").drive.cyc == 0.5
