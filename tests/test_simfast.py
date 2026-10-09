"""The compiled drive model (nybulah.simfast) against py65 and the Python devices."""

import numpy as np
import pytest
from py65.assembler import Assembler
from py65.devices.mpu6502 import MPU

from nybulah import simcia, simfast
from nybulah.monitor import Monitor
from nybulah.opencbm import IEC_DATA, IEC_SRQ
from nybulah.nibbler import Nibbler
from nybulah.sim import Drive1541, Drive1571, SimIOWrite, SimTimeout
from nybulah.simhost import SimCBM
from nybulah.simdisk import Media, disk_drive

HIGH = (0x00, 0x01, 0x03, 0x07, 0x18, 0x1C, 0x20, 0x40, 0x60, 0x80, 0xC0, 0xFF)
LOW = (0x00, 0x01, 0x04, 0x05, 0x08, 0x09, 0x0C, 0x0D, 0x0F, 0x10, 0xFE, 0xFF)
LINKS = {"drive", "media", "bus", "mpu", "via1", "cia", "mech", "memory", "corrupt"}
SKIP = LINKS | {"fast", "tracks", "_cells", "excycles", "addcycles"}


def snapshot(obj):
    """Attributes of obj other than links, hooks and py65's per-instruction scratch."""
    out = {}
    for k, v in vars(obj).items():
        if k in SKIP or callable(v):
            continue
        if isinstance(v, np.random.Generator):
            v = v.bit_generator.state
        out[k] = bytes(v) if isinstance(v, (bytearray, np.ndarray)) else v
    return out


def state(drive):
    """Everything py65, Via, Mechanism and Media hold for a drive."""
    out = [snapshot(drive), snapshot(drive.mpu), snapshot(drive.via1)]
    out.append(drive.bus.host_lines)
    out.append(None if drive.cia is None else snapshot(drive.cia))
    if drive.mech is not None:
        out += [snapshot(drive.mech), snapshot(drive.mech.media)]
        out.append({k: v.tobytes() for k, v in drive.mech.media.tracks.items()})
    return out


def differences(ref, fast):
    """(part, attribute) of the state that differs between two drives."""
    out = []
    for i, (a, b) in enumerate(zip(state(ref), state(fast))):
        if not isinstance(a, dict):
            a, b = {None: a}, {None: b}
        out += [(i, k) for k in a.keys() | b.keys() if a.get(k) != b.get(k)]
    return out


def registers(drive):
    mpu = drive.mpu
    return mpu.a, mpu.x, mpu.y, mpu.sp, mpu.p, mpu.pc, mpu.processorCycles


def pair(make):
    """(py65 drive, compiled drive) built alike."""
    ref, fast = make(), make()
    ref.fast, fast.fast = False, True
    assert simfast.eligible(fast) and not simfast.eligible(ref)
    return ref, fast


def step_both(ref, fast):
    """One instruction on each; the exception both raised, if any."""
    errors = []
    for run in (ref.step, lambda: simfast.run_drive(fast, simfast.NEVER, steps=1)):
        try:
            run()
            errors.append(None)
        except (SimIOWrite, IndexError) as e:
            errors.append(type(e))
    assert errors[0] == errors[1]
    return errors[0]


def mechanism_drive(model):
    return lambda: disk_drive(model, Media(rpm=301.0, wander=(2.0, 0.5)))


def opcode_trials(make, trials, seed):
    """Every opcode from random registers, operands, timers and mechanism states."""
    rng = np.random.default_rng(seed)
    ref, fast = pair(make)
    ram = rng.integers(0, 256, 0x800, dtype=np.uint8)
    for d in (ref, fast):
        d.store[:0x800] = ram.tobytes()
        d.halted = False
    for op in range(256):
        for _ in range(trials):
            pc = int(rng.integers(0x200, 0x7F0))
            lo, hi = rng.choice(LOW), rng.choice(HIGH)
            regs = rng.integers(0, 256, 5)
            via = rng.integers(0, 0x10000, 4)
            pb = int(rng.integers(0, 256))
            for d in (ref, fast):
                d.halted = False
                d.store[pc : pc + 3] = bytes([op, lo, hi])
                mpu = d.mpu
                mpu.pc, (mpu.a, mpu.x, mpu.y, mpu.p, mpu.sp) = pc, map(int, regs)
                d.via1.latch, d.via1.t1_start, d.via1.t2_latch, d.via1.acr = map(
                    int, via & [0xFFFF, 0xFFFF, 0xFFFF, 0xFF]
                )
                if d.mech is not None:
                    d.mech.pb, d.mech.pcr = pb, int(regs[0]) | 0x0C
                if d.cia is not None:
                    randomize_cia(d, via, pb, regs)
            step_both(ref, fast)
            assert registers(ref) == registers(fast), hex(op)
    assert not differences(ref, fast)


def randomize_cia(d, via, pb, regs):
    """CIA timer/shifter state and the bus driver bit from a trial's random values."""
    c, cia = d.cycles, d.cia
    cia.latch, cia.cra = int(via[0]) & 7, pb & 0x49
    cia.t0, cia.c0 = c - int(via[1]) % 9, int(via[2]) % 5
    cia.u1 = c + int(regs[1]) % 40 - 20 if pb & 0x80 else -1
    cia.out, cia.pend = int(regs[2]), int(regs[3]) if pb & 0x20 else -1
    cia.ticr, cia.flags, cia.mask = c - 3, pb & 0x09, pb & 0x08
    d.via1.regs[1] = pb & 0x02


def lockstep(ref, fast, instructions):
    """Instruction-by-instruction equality; returns how many raised."""
    raised = 0
    for _ in range(instructions):
        if ref.halted:
            break
        raised += step_both(ref, fast) is not None
        assert registers(ref) == registers(fast)
        assert ref.cycles == fast.cycles
    assert not differences(ref, fast)
    return raised


IMPLEMENTED = [
    i for i, f in enumerate(MPU.instruct) if f.__name__ != "inst_not_implemented"
]


def random_program(rng, drive):
    """Implemented opcodes with random operands across RAM, run from $0200."""
    code = rng.integers(0, 256, 0x600, dtype=np.uint8)
    code[::3] = rng.choice(IMPLEMENTED, len(code[::3]))
    drive.store[0x200:0x800] = code.tobytes()
    drive.call(0x200)


@pytest.mark.parametrize("make", [Drive1541, Drive1571, mechanism_drive("1571")])
def test_every_opcode_matches_py65(make):
    opcode_trials(make, 24, 1)


@pytest.mark.parametrize("seed", range(3))
def test_random_programs_match_py65(seed):
    rng = np.random.default_rng(seed)
    ref, fast = pair(mechanism_drive("1541"))
    for _ in range(40):
        seed, p = rng.integers(1 << 30), int(rng.integers(0, 256))
        for d in (ref, fast):
            random_program(np.random.default_rng(seed), d)
            d.mpu.p = p
        lockstep(ref, fast, 500)


def test_bulk_runs_match_single_steps():
    rng = np.random.default_rng(5)
    ref, fast = pair(Drive1541)
    loop = bytes([0xF8, 0xA2, 0x00, 0xCA, 0xD0, 0xFD, 0x69, 0x07, 0xC8, 0xD0, 0xF6])
    for d in (ref, fast):
        d.load(0x400, loop + bytes([0x60]))
        d.call(0x400)
    for limit in np.cumsum(rng.integers(1, 5000, 20)):
        while not ref.halted and ref.cycles < limit:
            ref.step()
        simfast.run_drive(fast, int(limit))
        assert not differences(ref, fast)
    while not ref.halted:
        ref.step()
    simfast.run_drive(fast, simfast.NEVER)
    assert fast.halted and not differences(ref, fast)


class Twin:
    """Monitor over a py65 drive and a compiled one: equal after every routine,
    instruction by instruction for the first ``window`` of each."""

    def __init__(self, ref, fast, window):
        self.ref, self.fast, self.window = ref, fast, window
        self.py65 = [0, 0]
        for i, d in enumerate((ref, fast)):
            d.mpu.step = self.counted(d.mpu.step, i)

    def counted(self, step, i):
        def wrapped():
            self.py65[i] += 1
            return step()

        return wrapped

    def read(self, addr, size):
        data = self.ref.dump(addr, size)
        assert self.fast.dump(addr, size) == data
        return data

    def write(self, addr, data):
        for d in (self.ref, self.fast):
            d.load(addr, bytes(data))

    def jsr(self, addr):
        for d in (self.ref, self.fast):
            d.call(addr)
        lockstep(self.ref, self.fast, self.window)
        limit = self.ref.cycles + 50_000_000
        while not self.ref.halted and self.ref.cycles < limit:
            self.ref.step()
        simfast.run_drive(self.fast, limit)
        assert not differences(self.ref, self.fast)
        return self.ref.mpu.a, self.ref.mpu.x, self.ref.mpu.y


def twin_rig(g64, model, window, **kw):
    def make():
        media = Media.from_g64(g64, rpm=302.0, wander=(3.0, 0.7), seed=3)
        drive = disk_drive(model, media, **kw)
        drive.mech.log = []
        return drive

    ref, fast = pair(make)
    twin = Twin(ref, fast, window)
    nib = Nibbler(twin, model, stepms=1, settle_ms=1, spinup_s=0, sleep=lambda s: 0)
    return twin, nib.open()


@pytest.mark.parametrize(
    "model, start", [("1541", "now"), ("1541", "sync"), ("1571", "index")]
)
def test_drive_programs_match_py65(g64, model, start):
    twin, nib = twin_rig(g64, model, 3000)
    nib.capture(36, start=start)
    nib.write_track(40, bytes(range(256)) * 20)
    nib.capture(40, side=1 if model == "1571" else 0)
    nib.close()
    assert twin.fast.mech.log == twin.ref.mech.log
    assert twin.py65[1] < twin.py65[0] / 1000


def test_corrupt_writes_match_py65(g64):
    twin, nib = twin_rig(g64, "1541", 0)
    for d in (twin.ref, twin.fast):
        d.mech.corrupt = lambda key, cell, bit: bit ^ (cell % 97 == 0)
    nib.write_track(20, bytes(range(256)) * 20)
    nib.capture(20, timing="none")


@pytest.mark.parametrize("proto", ["s1", "s2"])
def test_bus_transfers_match_py65(proto):
    rng = np.random.default_rng(2)
    data = rng.integers(0, 256, 700, dtype=np.uint8).tobytes()
    drives = []
    for fast in (False, True):
        cbm = SimCBM(Drive1541(device=10), dev=10)
        cbm.drive.fast = fast
        with Monitor(cbm, 10, proto) as mon:
            mon.write(0x8000, data)
            assert mon.read(0x8000, len(data)) == data
        cbm.settle()
        drives.append(cbm.drive)
    assert not differences(drives[0], drives[1])


def test_transfer_times_out_like_py65():
    for fast in (False, True):
        cbm = SimCBM(Drive1541(device=10), dev=10, budget=500)
        cbm.drive.fast = fast
        cbm.drive.load(0x400, bytes([0x4C, 0x00, 0x04]))
        cbm.drive.call(0x400)
        with pytest.raises(SimTimeout, match="budget"):
            cbm.s1_read(1)
        cbm.drive.load(0x400, bytes([0x60]))
        cbm.drive.call(0x400)
        with pytest.raises(SimTimeout, match="halted"):
            cbm.s2_write(b"\x01")


def test_ineligible_drives_run_on_py65():
    drive = disk_drive("1541", Media())
    drive.fast = True
    drive.mech.media.turns = lambda now: 0.0
    assert not simfast.eligible(drive)
    drive.mech.media = Media()
    assert simfast.eligible(drive)
    drive.store[0x400:0x403] = bytes([0x4C, 0x00, 0x04])
    drive.call(0x400)
    drive.fast = False
    simfast.run_drive(drive, drive.cycles + 30, steps=4)
    assert drive.cycles == 12


def test_decoded_tables_follow_py65():
    names = [n for n, _ in MPU.disassemble]
    kinds = simfast.OPS[:, 0]
    assert (kinds >= 0).sum() == len(IMPLEMENTED)
    assert {simfast.KINDS[k] for k in kinds[kinds >= 0]} >= {"LD", "BR", "TR"}
    assert all(
        simfast.KINDS[kinds[i]] in names[i] or kinds[i] < simfast.ADC
        for i in IMPLEMENTED
    )


def test_interpreted_kernels_match_compiled(g64, monkeypatch):
    """The kernels as plain Python agree with py65 too (and are measured by coverage)."""
    for module in (simfast, simcia):
        for name, obj in vars(module).items():
            if hasattr(obj, "py_func"):
                monkeypatch.setattr(module, name, obj.py_func)
    opcode_trials(mechanism_drive("1571"), 2, 4)
    twin, nib = twin_rig(g64, "1571", 200)
    nib.write_track(4, bytes(range(256)) * 4, start="index")
    test_bus_transfers_match_py65("s2")
    test_cia_shift_out_matches_py65(1)
    assert twin.ref.mech.log == twin.fast.mech.log


def assemble(org, *lines):
    """py65-assembled code at org; "lbl:" lines define labels for later branches."""
    asm, out, labels = Assembler(MPU()), bytearray(), {}
    for line in lines:
        if line.endswith(":"):
            labels[line[:-1]] = org + len(out)
            continue
        for name, at in labels.items():
            line = line.replace(name, f"${at:04x}")
        out += bytes(asm.assemble(line, org + len(out)))
    return bytes(out)


SETUP = ("lda #$01", "sta $4004", "lda #$00", "sta $4005")
SHIFT_OUT = assemble(
    0x400,
    *SETUP,
    "lda #$41",
    "sta $400e",
    "lda #$02",
    "sta $180f",
    "ldx #$00",
    "next:",
    "stx $400c",
    "lda #$08",
    "wait:",
    "bit $400d",
    "beq wait",
    "lda $1800",
    "inx",
    "bne next",
    "rts",
)
SHIFT_IN = assemble(
    0x400,
    *SETUP,
    "lda #$01",
    "sta $400e",
    "ldy #$00",
    "next:",
    "lda #$08",
    "wait:",
    "bit $400d",
    "beq wait",
    "lda $400c",
    "sta $0500,y",
    "iny",
    "cpy #$20",
    "bne next",
    "rts",
)


@pytest.mark.parametrize("delay", [0, 2])
def test_cia_shift_out_matches_py65(delay):
    ref, fast = pair(Drive1571)
    for d in (ref, fast):
        d.cia.delay = delay
        d.load(0x400, SHIFT_OUT)
        d.call(0x400)
    lines = set()
    while not ref.halted:
        step_both(ref, fast)
        assert registers(ref) == registers(fast)
        lines.add(ref.drive_lines())
        fast_lines = simfast.drive_lines(simfast.pack(fast, -1, False)[0])
        assert fast_lines == ref.drive_lines()
    assert lines >= {0, IEC_SRQ, IEC_DATA, IEC_SRQ | IEC_DATA}
    assert fast.halted and not differences(ref, fast)


def test_cia_shift_in_matches_py65():
    ref, fast = pair(Drive1571)
    for d in (ref, fast):
        d.load(0x400, SHIFT_IN)
        d.call(0x400)
    for n, bit in enumerate(np.random.default_rng(3).integers(0, 2, 4096)):
        if ref.halted:
            break
        step_both(ref, fast)
        if n % 2:
            for d in (ref, fast):
                d.cia.edge(int(bit))
    assert ref.dump(0x500, 32) == fast.dump(0x500, 32) != bytes(32)
    assert fast.halted and not differences(ref, fast)
