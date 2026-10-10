"""Compiled drive simulator: py65's 6502 with the drive's memory map, VIA1, CIA, disk.

``run_drive`` executes whole instructions as Drive1541.step, Via, Cia (simcia) and
simdisk.Mechanism would; an instruction needing Python (unimplemented opcode, raising
access, unfetched track, corrupt hook) runs through py65, then any mechanism update owed.
"""

# pylint: disable=too-many-return-statements,too-many-branches

import contextlib
import functools
import math
import operator
import weakref

import numpy as np
from py65.devices.mpu6502 import MPU

from .opencbm import IEC_ATN, IEC_CLOCK, IEC_DATA
from .sim import ACR_T1_FREERUN, FDC, IO_ACCESS_CYCLE, IRQ_T1, IRQ_T2, OPEN, RAM
from .sim import CIA, PA_FSDIR, RETURN_TRAP, ROM, VIA1, VIA2, Bus, SimTimeout
from .sim import PB_ATN_IN, PB_ATNA, PB_CLK_IN, PB_CLK_OUT, PB_DATA_IN, PB_DATA_OUT
from .sim import Drive1541, Drive1571, Drive1581
from .sim import HOST_LENGTHS, HOST_PROGRAMS, PUT, REL, SAMPLE, SET, SHL, WAIT
from .simdisk import CA1_FLAG, CELL_EPS, CPU_HZ, HT_MAX, HT_STOP
from .simdisk import INDEX_FRACTION, PB_INPUTS, PB_MOTOR, PB_PHASE, PB_SYNC, PB_WE
from .simdisk import PCR_MODE, PCR_SOE, PCR_WRITE, PHASE_OFFSET, SYNC_ONES, V_FLAG
from .simdisk import WANDER_NEWTON, Mechanism, Media, disk_drive, wd71_read, wd71_write
from .simcia import CCRA, END, cia81_lines, cia81_read, cia81_write, cia_atn
from .simcia import cia_lines, cia_read, cia_write, kernel, pack_cia, unpack_cia
from .simwd import W0, WEND, wd_read, wd_write

A, X, Y, SP, P, PC, CYC, PCYC, HALT, STEPS = range(10)
ACR, IER, LATCH, T1START, T1ACK, T2LATCH, T2START, T2ACK = range(10, 18)
PBOUT, DEVICE, EXT, EXTA, LINES, TRK0 = range(18, 24)
MECH, MPB, PCR, DDRB, ORA, DDRA, MLATCH, KNONE, K, N = range(24, 34)
ONES, COUNT, SHREG, PENDING, CA1, WRITTEN, ARMED, OVER, UNDER = range(34, 43)
WD, WPROT, LOGGING, NEV, HT, BUMPS, KSIDE, KHT, CSIDE, CHT = range(43, 53)
FRESH, CORRUPT, DEFER, RESAMPLE = range(53, 57)
VREGS, MREGS, HOSTL = 57, 73, 89
SENSOR, INNER, M81, OPPC = range(END, END + 4)
STATE = WEND
DUE, SYNC_START, RPM, AMP, PERIOD = range(5)

MPU_FIELDS = {A: "a", X: "x", Y: "y", SP: "sp", P: "p", PC: "pc"}
MPU_FIELDS[PCYC] = "processorCycles"
VIA_FIELDS = {ACR: "acr", IER: "ier", LATCH: "latch", T1START: "t1_start"}
VIA_FIELDS |= {T1ACK: "t1_ack", T2LATCH: "t2_latch", T2START: "t2_start"}
VIA_FIELDS[T2ACK] = "t2_ack"
MECH_FIELDS = {MPB: "pb", PCR: "pcr", DDRB: "ddrb", ORA: "ora", DDRA: "ddra"}
MECH_FIELDS |= {MLATCH: "latch", N: "_n", ONES: "_ones", COUNT: "_count"}
MECH_FIELDS |= {SHREG: "_shreg", PENDING: "_pending", CA1: "_ca1", WRITTEN: "_written"}
MECH_FIELDS |= {ARMED: "_armed", OVER: "overruns", UNDER: "underruns"}
MECH_FIELDS |= {HT: "halftrack", BUMPS: "bumps", WPROT: "write_protect"}
MECH_FIELDS |= {SENSOR: "sensor_edge", INNER: "inner_stops"}
assert HOSTL < CCRA and OPPC < W0

OK, HALTED, LIMIT, PYTHON, FULL, DEFERRED, MOVED = range(7)
PROTO, PLEN, HOP, HBYTE, HC, HB, HWAIT, BUDGET = range(8)
BYTE_EVENT, SYNC_EVENT = 0, 1
KIND_BITS = 3
EVENTS, EVENT_MARGIN = 1 << 12, 64
NEVER = 1 << 62
N_FLAG, U_FLAG, B_FLAG, D_FLAG, I_FLAG, Z_FLAG, C_FLAG = 128, 32, 16, 8, 4, 2, 1
PA_TRK0 = Drive1571.PA_TRK0

MODES = "imp acc imm zpg zpx zpy abs abx aby inx iny ind rel".split()
M_IMP, M_ACC, M_IMM, M_ZPG, M_ZPX, M_ZPY, M_ABS = range(7)
M_ABX, M_ABY, M_INX, M_INY, M_IND, M_REL = range(7, 13)
OPERAND_BYTES = np.array([0, 0, 1, 1, 1, 1, 2, 2, 2, 1, 1, 2, 1], np.int64)
KINDS = "LD ST TR IN CP FL BR ADC SBC AND ORA EOR BIT ASL LSR ROL ROR INC DEC".split()
KINDS += "JMP JSR BRK RTS RTI PHA PHP PLA PLP NOP".split()
LD, ST, TR, IN, CP, FL, BR, ADC, SBC, AND, ORA_, EOR, BIT = range(13)
ASL, LSR, ROL, ROR, INC, DEC, JMP, JSR, BRK, RTS, RTI = range(13, 24)
PHA, PHP, PLA, PLP = range(24, 28)
REGS = {"A": A, "X": X, "Y": Y, "S": SP}
FLAGS = {"C": C_FLAG, "D": D_FLAG, "I": I_FLAG, "V": V_FLAG, "Z": Z_FLAG, "N": N_FLAG}
BRANCHES = {"CC": "C0", "CS": "C1", "NE": "Z0", "EQ": "Z1", "PL": "N0", "MI": "N1"}
BRANCHES |= {"VC": "V0", "VS": "V1"}


def decode(name):
    """(kind, p1, p2) of a py65 mnemonic: registers, flags and deltas as parameters."""
    if name[:2] in ("LD", "ST"):
        return KINDS.index(name[:2]), REGS[name[2]], 0
    if name[0] == "T":
        return TR, REGS[name[1]], REGS[name[2]]
    if name[:2] in ("IN", "DE") and name[2] in "XY":
        return IN, REGS[name[2]], 1 if name[0] == "I" else -1
    if name in ("CMP", "CPX", "CPY"):
        return CP, REGS[name[2] if name[1] == "P" else "A"], 0
    if name[:2] in ("CL", "SE"):
        return FL, FLAGS[name[2]], int(name[0] == "S")
    if name[0] == "B" and name[1:] in BRANCHES:
        flag, want = BRANCHES[name[1:]]
        return BR, FLAGS[flag], int(want)
    return KINDS.index(name), 0, 0


def tables():
    """Per opcode: kind (-1 unimplemented), mode, cycles, extra cycles, p1, p2."""
    out = np.full((256, 6), -1, np.int64)
    for i, (name, mode) in enumerate(MPU.disassemble):
        if MPU.instruct[i].__name__ != "inst_not_implemented":
            kind, p1, p2 = decode(name)
            mode = MODES.index(mode)
            out[i] = kind, mode, MPU.cycletime[i], MPU.extracycles[i], p1, p2
    return out


OPS = tables()


@kernel
def turns(f, now):
    """Media.turns."""
    w = 2 * math.pi / (f[PERIOD] * CPU_HZ)
    return (f[RPM] * now + f[AMP] * (1 - math.cos(w * now)) / w) / (60.0 * CPU_HZ)


@kernel
def time_at(f, revs):
    """Media.time_at."""
    w = 2 * math.pi / (f[PERIOD] * CPU_HZ)
    t = revs * 60.0 * CPU_HZ / f[RPM]
    for _ in range(WANDER_NEWTON if f[AMP] else 0):
        rate = (f[RPM] + f[AMP] * math.sin(w * t)) / (60.0 * CPU_HZ)
        t -= (turns(f, t) - revs) / rate
    return t


@kernel
def cell_at(s, f, now):
    """Mechanism._cell."""
    return np.int64(math.floor(turns(f, now) * s[N] + CELL_EPS))


@kernel
def log_event(s, ev, kind, t0, t1, value):
    """Append a mechanism log entry."""
    i = s[NEV]
    ev[i, 0], ev[i, 1], ev[i, 2], ev[i, 3] = kind, value, t0, t1
    s[NEV] = i + 1


@kernel
def byte_ready(s, f, j, ev):
    """Mechanism._event."""
    s[CA1] = 1
    if s[PCR] & PCR_SOE == PCR_SOE:
        s[P] |= V_FLAG
    if s[LOGGING]:
        log_event(s, ev, BYTE_EVENT, time_at(f, (j + 1) / s[N]), 0.0, s[MLATCH])


@kernel
def read_cell(s, f, cells, j, ev):
    """Mechanism._read_cell."""
    b = np.int64(cells[j % s[N]])
    s[SHREG] = (s[SHREG] << 1 | b) & 0xFF
    if b:
        s[ONES] += 1
        if s[ONES] >= SYNC_ONES:
            s[COUNT] = 0
            if math.isnan(f[SYNC_START]):
                f[SYNC_START] = time_at(f, (j + 1) / s[N])
            return
    else:
        if not math.isnan(f[SYNC_START]) and s[LOGGING]:
            end = time_at(f, (j + 1) / s[N])
            log_event(s, ev, SYNC_EVENT, f[SYNC_START], end, s[ONES])
        f[SYNC_START] = math.nan
        s[ONES] = 0
    s[COUNT] += 1
    if s[COUNT] == 8:
        s[COUNT] = 0
        s[MLATCH] = s[SHREG]
        s[PENDING] += 1
        byte_ready(s, f, j, ev)


@kernel
def write_cell(s, f, cells, j, ev):
    """Mechanism._write_cell without a corrupt hook."""
    cells[j % s[N]] = s[SHREG] >> 7 & 1
    s[SHREG] = s[SHREG] << 1 & 0xFF
    s[COUNT] += 1
    if s[COUNT] == 8:
        s[COUNT] = 0
        if s[ARMED] and not s[WRITTEN]:
            s[UNDER] += 1
        s[WRITTEN] = 0
        s[SHREG] = s[ORA] if s[DDRA] == 0xFF else 0xFF
        s[MLATCH] = s[SHREG]
        byte_ready(s, f, j, ev)


@kernel
def writing(s):
    """Mechanism.writing."""
    return s[PCR] & PCR_MODE == PCR_WRITE


@kernel
def side(s):
    """Mechanism.side."""
    return s[VREGS + 1] >> 2 & 1 if s[TRK0] else 0


@kernel
def can_update(s):
    """Mechanism.update needs nothing from Python now."""
    if not s[MPB] & PB_MOTOR:
        return True
    if s[CORRUPT] and writing(s):
        return False
    sd = side(s)
    if not s[KNONE] and sd == s[KSIDE] and s[HT] == s[KHT]:
        return True
    return s[FRESH] != 0 and sd == s[CSIDE] and s[HT] == s[CHT]


@kernel
def update(s, f, cells, ev, now):
    """Mechanism.update (the caller has checked can_update)."""
    if not s[MPB] & PB_MOTOR:
        s[KNONE], f[DUE] = 1, math.inf
        return
    sd = side(s)
    if s[KNONE] or sd != s[KSIDE] or s[HT] != s[KHT]:
        s[KSIDE], s[KHT], s[N], s[KNONE] = sd, s[HT], len(cells), 0
        s[K] = cell_at(s, f, now)
    end = cell_at(s, f, now)
    if writing(s):
        for j in range(s[K], end):
            write_cell(s, f, cells, j, ev)
    else:
        for j in range(s[K], end):
            read_cell(s, f, cells, j, ev)
    s[K] = max(end, s[K])
    due = time_at(f, (s[K] + 8 - s[COUNT]) / s[N])
    f[DUE] = math.ceil(due) if math.isfinite(due) else math.inf


@kernel
def t1_events(s):
    """Via._t1_events."""
    e = s[CYC] - s[T1START] - s[LATCH] - 1
    if e < 0:
        return 0
    return e // (s[LATCH] + 2) + 1 if s[ACR] & ACR_T1_FREERUN else 1


@kernel
def drive_lines(s):
    """Drive1541.drive_lines with Drive1581.via_lines and fsdir."""
    pb, atn = s[PBOUT], s[HOSTL] & IEC_ATN != 0
    if s[M81]:
        return cia81_lines(s, pb, atn, s[CYC])
    lines = IEC_CLOCK if pb & PB_CLK_OUT else 0
    if pb & PB_DATA_OUT or atn != (pb & PB_ATNA != 0):
        lines |= IEC_DATA
    return lines | cia_lines(s, s[CYC], s[VREGS + 1] & PA_FSDIR)


@kernel
def bus_lines(s):
    """Bus.lines: the host, the other devices (by host ATN) and this drive."""
    host = s[HOSTL]
    return host | (s[EXTA] if host & IEC_ATN else s[EXT]) | drive_lines(s)


@kernel
def via_read(s, reg):
    """Via.read with Drive1541.port_b on a still bus and Drive1571.port_a."""
    if reg == 0:
        bus = bus_lines(s)
        v = s[PBOUT] | ((s[DEVICE] - 8) & 3) << 5
        v |= PB_DATA_IN if bus & IEC_DATA else 0
        v |= PB_CLK_IN if bus & IEC_CLOCK else 0
        return v | (PB_ATN_IN if bus & IEC_ATN else 0)
    if reg in (1, 15):
        v = s[VREGS + 1]
        if s[TRK0] and s[MECH]:
            v = v & ~PA_TRK0 | (0 if s[HT] <= s[SENSOR] else PA_TRK0)
        return v
    if reg in (4, 5):
        count = (s[LATCH] - (s[CYC] - s[T1START])) & 0xFFFF
        if reg == 4:
            s[T1ACK] = t1_events(s)
        return count >> 8 * (reg - 4) & 0xFF
    if reg in (6, 7):
        return s[LATCH] >> 8 * (reg - 6) & 0xFF
    if reg in (8, 9):
        if reg == 8:
            s[T2ACK] = 1
        return ((s[T2LATCH] - (s[CYC] - s[T2START])) & 0xFFFF) >> 8 * (reg - 8) & 0xFF
    if reg == 0xB:
        return s[ACR]
    if reg == 0xD:
        v = IRQ_T1 if t1_events(s) > s[T1ACK] else 0
        if not s[T2ACK] and s[CYC] - s[T2START] > s[T2LATCH]:
            v |= IRQ_T2
        return v | 0x80 if v & s[IER] else v
    if reg == 0xE:
        return s[IER] | 0x80
    return s[VREGS + reg]


@kernel
def via_write(s, reg, value):
    """Via.write."""
    if reg == 0:
        s[PBOUT] = value & (PB_DATA_OUT | PB_CLK_OUT | PB_ATNA)
    elif reg in (4, 6):
        s[LATCH] = s[LATCH] & 0xFF00 | value
    elif reg in (5, 7):
        s[LATCH] = s[LATCH] & 0xFF | value << 8
        if reg == 5:
            s[T1START], s[T1ACK] = s[CYC], 0
    elif reg == 8:
        s[T2LATCH] = s[T2LATCH] & 0xFF00 | value
    elif reg == 9:
        s[T2LATCH] = s[T2LATCH] & 0xFF | value << 8
        s[T2START], s[T2ACK] = s[CYC], 0
    elif reg == 0xB:
        s[ACR] = value
    elif reg == 0xD:
        if value & IRQ_T1:
            s[T1ACK] = t1_events(s)
    elif reg == 0xE:
        s[IER] = s[IER] | value & 0x7F if value & 0x80 else s[IER] & ~value
    else:
        s[VREGS + (1 if reg == 0xF else reg)] = value


@kernel
def mech_read(s, f, cells, ev, fdc, reg):
    """Mechanism.read."""
    now = s[CYC] + IO_ACCESS_CYCLE
    update(s, f, cells, ev, now)
    if fdc:
        hole = turns(f, now) % 1.0 < INDEX_FRACTION and (s[MPB] & PB_MOTOR) != 0
        return wd71_read(s, W0, reg & 3, hole, s[OPPC])
    if reg == 0:
        v = s[MPB] & ~PB_INPUTS & 0xFF | (0 if s[WPROT] else PB_WE)
        return v | (0 if not writing(s) and s[ONES] >= SYNC_ONES else PB_SYNC)
    if reg in (1, 15):
        s[OVER] += max(s[PENDING] - 1, 0)
        s[PENDING] = 0
        if reg == 1:
            s[CA1] = 0
        return s[ORA] if writing(s) else s[MLATCH]
    if reg == 13:
        return CA1_FLAG if s[CA1] else 0
    if reg in (2, 3, 12):
        return s[DDRB] if reg == 2 else (s[DDRA] if reg == 3 else s[PCR])
    return s[MREGS + reg]


@kernel
def port_b_write(s, value):
    """Mechanism._port_b_write."""
    move = (value - s[MPB]) & PB_PHASE
    if value & PB_MOTOR and not s[MPB] & PB_MOTOR:
        s[KNONE], s[ONES], s[COUNT] = 1, 0, 0
    s[MPB] = value
    if move in (1, 3):
        ht = s[HT] + (1 if move == 1 else -1)
        phase = (value - PHASE_OFFSET) & PB_PHASE
        if ht < HT_STOP:
            s[BUMPS] += 1
            ht = HT_STOP + ((phase - HT_STOP) & PB_PHASE)
        elif ht > HT_MAX:
            s[INNER] += 1
            ht = HT_MAX - ((HT_MAX - phase) & PB_PHASE)
        s[HT] = ht


@kernel
def mech_write(s, f, cells, ev, fdc, reg, value):
    """Mechanism.write; a closing update needing Python is deferred."""
    now = s[CYC] + IO_ACCESS_CYCLE
    update(s, f, cells, ev, now)
    if fdc:
        wd71_write(s, W0, reg & 3, value, s[OPPC])
    elif reg == 0:
        port_b_write(s, value)
    elif reg in (1, 15):
        s[ORA], s[WRITTEN], s[ARMED] = value, 1, writing(s)
    elif reg in (2, 3):
        s[DDRB if reg == 2 else DDRA] = value
    elif reg == 12:
        entering = not writing(s) and value & PCR_MODE == PCR_WRITE
        s[PCR], s[ARMED] = value, 0
        s[RESAMPLE] = entering and not s[KNONE]
    else:
        s[MREGS + reg] = value
    if s[RESAMPLE] or not can_update(s):
        s[DEFER] = now
    else:
        update(s, f, cells, ev, now)


@kernel
def readable(s, amap, addr):
    """Drive1541.read at addr needs nothing from Python."""
    k, phys = amap[addr] & 7, amap[addr] >> KIND_BITS
    if k == VIA1 and phys & 0xF == 0 or s[M81] and k == CIA and phys == 1:
        return s[EXT] >= 0
    if k in (VIA2, FDC) and s[MECH] and (k == FDC or phys < 16):
        return can_update(s)
    return True


@kernel
def writable(s, amap, addr):
    """Drive1541.write at addr neither raises nor needs Python."""
    k, phys = amap[addr] & 7, amap[addr] >> KIND_BITS
    if k == VIA1:
        return phys < 16
    if k == CIA or s[M81] and k == FDC:
        return True
    if s[MECH] and k in (VIA2, FDC) and (k == FDC or phys < 16):
        return can_update(s)
    return k in (RAM, ROM, OPEN)


@kernel
def rd(s, f, amap, store, cells, ev, addr):
    """Drive1541.read (the caller has checked readable)."""
    k, phys = amap[addr] & 7, amap[addr] >> KIND_BITS
    if k <= ROM:
        return np.int64(store[phys])
    if k == VIA1:
        return via_read(s, phys & 0xF)
    c = s[CYC] + IO_ACCESS_CYCLE
    if s[M81] and k == CIA:
        return cia81_read(
            s, cells, phys, c, s[DEVICE], via_read(s, 0) if phys == 1 else 0
        )
    if s[M81] and k == FDC:
        return wd_read(s, cells, phys, c, s[OPPC])
    if k == CIA:
        return cia_read(s, phys, c)
    if k in (VIA2, FDC) and s[MECH] and (k == FDC or phys < 16):
        return mech_read(s, f, cells, ev, k == FDC, phys)
    return np.int64(addr >> 8 if k == OPEN else 0)


@kernel
def wr(s, f, amap, store, cells, ev, addr, value):
    """Drive1541.write (the caller has checked writable)."""
    k, phys = amap[addr] & 7, amap[addr] >> KIND_BITS
    if k == RAM:
        store[phys] = value
    elif k == VIA1:
        via_write(s, phys, value)
    elif s[M81] and k == CIA:
        s[PBOUT] = cia81_write(s, cells, phys, value, s[CYC] + IO_ACCESS_CYCLE)
    elif s[M81] and k == FDC:
        wd_write(s, cells, phys, value, s[CYC] + IO_ACCESS_CYCLE, s[OPPC])
    elif k == CIA:
        cia_write(s, phys, value, s[CYC] + IO_ACCESS_CYCLE)
    elif k in (VIA2, FDC):
        mech_write(s, f, cells, ev, k == FDC, phys, value)


@kernel
def plain(amap, addr):
    """RAM or ROM (reads without side effects) inside the address space."""
    return addr <= 0xFFFF and amap[addr] & 7 <= ROM


@kernel
def peek(amap, store, addr):
    """A plain byte."""
    return np.int64(store[amap[addr] >> KIND_BITS])


@kernel
def wrap_at(addr):
    """Address of MPU.WrapAt's high byte."""
    return (addr & 0xFF00) | ((addr + 1) & 0xFF)


@kernel
def push(s, amap, store, value):
    """MPU.stPush (page 1 is RAM)."""
    store[amap[s[SP] + 0x100] >> KIND_BITS] = value & 0xFF
    s[SP] = (s[SP] - 1) & 0xFF


@kernel
def pop(s, amap, store):
    """MPU.stPop."""
    s[SP] = (s[SP] + 1) & 0xFF
    return peek(amap, store, s[SP] + 0x100)


@kernel
def nz(s, value):
    """MPU.FlagsNZ."""
    s[P] = s[P] & ~(Z_FLAG | N_FLAG) | (Z_FLAG if value == 0 else value & N_FLAG)


@kernel
def adc(s, data):
    """MPU.opADC."""
    a, c = s[A], s[P] & C_FLAG
    if s[P] & D_FLAG:
        n0 = (data & 0xF) + (a & 0xF) + c
        half = 1 if n0 > 9 else 0
        n1 = (data >> 4 & 0xF) + (a >> 4 & 0xF) + half
        carry = n1 > 9
        alu = (n1 & 0xF) << 4 | n0 & 0xF
        result = (n1 + (6 if carry else 0) & 0xF) << 4 | n0 + 6 * half & 0xF
    else:
        alu = data + a + c
        carry = alu > 0xFF
        alu &= 0xFF
        result = alu
    p = s[P] & ~(C_FLAG | V_FLAG | N_FLAG | Z_FLAG) | (C_FLAG if carry else 0)
    p |= V_FLAG if ~(a ^ data) & (a ^ alu) & N_FLAG else 0
    s[P] = p | (Z_FLAG if alu == 0 else alu & N_FLAG)
    s[A] = result


@kernel
def sbc(s, data):
    """MPU.opSBC."""
    a, c = s[A], s[P] & C_FLAG
    alu = a + (~data & 0xFF) + c
    p = s[P] & ~(C_FLAG | Z_FLAG | N_FLAG | V_FLAG) | (C_FLAG if alu > 0xFF else 0)
    p |= V_FLAG if (a ^ data) & (a ^ alu) & N_FLAG else 0
    alu &= 0xFF
    s[P] = p | (Z_FLAG if alu == 0 else alu & N_FLAG)
    if s[P] & D_FLAG:
        n0 = (a & 0xF) + (~data & 0xF) + c
        n1 = (a >> 4 & 0xF) + (~data >> 4 & 0xF) + (1 if n0 > 0xF else 0)
        lo = alu + (0 if n0 > 0xF else 10) & 0xF
        alu = (alu + (0 if n1 > 0xF else 10 << 4) >> 4 & 0xF) << 4 | lo
    s[A] = alu


@kernel
def rmw(s, kind, t):
    """py65's shifts, rotates, increments and decrements of t."""
    if kind in (INC, DEC):
        t = (t + (1 if kind == INC else -1)) & 0xFF
    else:
        left = kind in (ASL, ROL)
        out = t & N_FLAG if left else t & 1
        cin = s[P] & C_FLAG if kind in (ROL, ROR) else 0
        t = (t << 1 | cin) & 0xFF if left else t >> 1 | cin << 7
        s[P] = s[P] & ~C_FLAG | (C_FLAG if out else 0)
    nz(s, t)
    return t


@kernel
def operate(s, kind, reg, data):
    """py65's instructions that read an operand value."""
    if kind == LD:
        s[reg] = data
        nz(s, data)
    elif kind in (AND, ORA_, EOR):
        a = s[A]
        s[A] = a & data if kind == AND else (a | data if kind == ORA_ else a ^ data)
        nz(s, s[A])
    elif kind == ADC:
        adc(s, data)
    elif kind == SBC:
        sbc(s, data)
    elif kind == BIT:
        p = s[P] & ~(Z_FLAG | N_FLAG | V_FLAG) | data & (N_FLAG | V_FLAG)
        s[P] = p | (Z_FLAG if s[A] & data == 0 else 0)
    else:
        r = s[reg]
        p = s[P] & ~(C_FLAG | Z_FLAG | N_FLAG) | (r - data) & N_FLAG
        s[P] = p | (C_FLAG | Z_FLAG if r == data else (C_FLAG if r > data else 0))


@kernel
def control(s, amap, store, kind, mode, p1, p2, pc):
    """Implied, stack, jump and branch instructions; extra cycles, -1 for Python."""
    if kind == FL:
        s[P] = s[P] | p1 if p2 else s[P] & ~p1
    elif kind == TR:
        s[p2] = s[p1]
        if p2 != SP:
            nz(s, s[p2])
    elif kind == IN:
        s[p1] = (s[p1] + p2) & 0xFF
        nz(s, s[p1])
    elif kind == BR:
        if (s[P] & p1 != 0) != p2:
            s[PC] = (pc + 1) & 0xFFFF
            return 0
        off, pc = peek(amap, store, pc), pc + 1
        target = pc - (off ^ 0xFF) - 1 if off & N_FLAG else pc + off
        s[PC] = target & 0xFFFF
        return 2 if pc & 0xFF00 != target & 0xFF00 else 1
    elif kind in (JMP, JSR):
        target = peek(amap, store, pc) | peek(amap, store, pc + 1) << 8
        if mode == M_IND:
            if not (plain(amap, target) and plain(amap, wrap_at(target))):
                return -1
            target = peek(amap, store, target) | peek(amap, store, wrap_at(target)) << 8
        elif kind == JSR:
            push(s, amap, store, (pc + 1) >> 8)
            push(s, amap, store, pc + 1)
        s[PC] = target
    elif kind == BRK:
        if not (plain(amap, 0xFFFE) and plain(amap, 0xFFFF)):
            return -1
        ret = (pc + 1) & 0xFFFF
        push(s, amap, store, ret >> 8)
        push(s, amap, store, ret)
        s[P] |= B_FLAG
        push(s, amap, store, s[P] | B_FLAG | U_FLAG)
        s[P] |= I_FLAG
        s[PC] = peek(amap, store, 0xFFFE) | peek(amap, store, 0xFFFF) << 8
    elif kind in (PHA, PHP):
        push(s, amap, store, s[A] if kind == PHA else s[P] | B_FLAG | U_FLAG)
    elif kind == PLA:
        s[A] = pop(s, amap, store)
        nz(s, s[A])
    elif kind == PLP:
        s[P] = pop(s, amap, store) | B_FLAG | U_FLAG
    elif kind in (RTS, RTI):
        if kind == RTI:
            s[P] = pop(s, amap, store) | B_FLAG | U_FLAG
        lo = pop(s, amap, store)
        s[PC] = ((lo | pop(s, amap, store) << 8) + (kind == RTS)) & 0xFFFF
    return 0


@kernel
def effective(s, amap, store, mode, extra, pc):
    """(address, page-crossing cycles) of a memory operand, as py65 computes it."""
    b = peek(amap, store, pc)
    if mode == M_ZPG:
        return b, 0
    if mode in (M_ZPX, M_ZPY):
        return (b + s[X if mode == M_ZPX else Y]) & 0xFF, 0
    if mode == M_INX:
        z = (b + s[X]) & 0xFF
        return peek(amap, store, z) | peek(amap, store, wrap_at(z)) << 8, 0
    if mode == M_INY:
        base = peek(amap, store, b) | peek(amap, store, wrap_at(b)) << 8
    else:
        base = b | peek(amap, store, pc + 1) << 8
    if mode == M_ABS:
        return base, 0
    addr = (base + s[Y if mode in (M_ABY, M_INY) else X]) & 0xFFFF
    return addr, 1 if extra and base & 0xFF00 != addr & 0xFF00 else 0


@kernel
def execute(s, f, amap, store, cells, ev, opcode):
    """One instruction after its opcode fetch; returns its cycles, -1 for Python."""
    row = OPS[opcode]
    kind, mode, cycles, extra, p1, p2 = row[0], row[1], row[2], row[3], row[4], row[5]
    pc = s[PC]
    size = OPERAND_BYTES[mode]
    if kind < 0 or size and not (plain(amap, pc) and plain(amap, pc + size - 1)):
        return -1
    if kind in (TR, IN, FL, BR) or kind >= JMP:
        n = control(s, amap, store, kind, mode, p1, p2, pc)
        return -1 if n < 0 else cycles + n
    if mode == M_ACC:
        s[A] = rmw(s, kind, s[A])
        return cycles
    if mode == M_IMM:
        s[PC] = (pc + 1) & 0xFFFF
        operate(s, kind, p1, peek(amap, store, pc))
        return cycles
    addr, n = effective(s, amap, store, mode, extra, pc)
    if kind == ST:
        if not writable(s, amap, addr):
            return -1
        s[PC] = (pc + size) & 0xFFFF
        wr(s, f, amap, store, cells, ev, addr, s[p1])
    elif kind >= ASL:
        if not (readable(s, amap, addr) and writable(s, amap, addr)):
            return -1
        s[PC] = (pc + size) & 0xFFFF
        v = rmw(s, kind, rd(s, f, amap, store, cells, ev, addr))
        wr(s, f, amap, store, cells, ev, addr, v)
    else:
        if not readable(s, amap, addr):
            return -1
        s[PC] = (pc + size) & 0xFFFF
        operate(s, kind, p1, rd(s, f, amap, store, cells, ev, addr))
    return cycles + n


@kernel
def step(s, f, amap, store, cells, ev):
    """Drive1541.step: OK, or why it must stop (before or after the instruction)."""
    if s[NEV] > len(ev) - EVENT_MARGIN:
        return FULL
    if s[MECH] and s[CYC] >= f[DUE]:
        if not can_update(s):
            return PYTHON
        update(s, f, cells, ev, s[CYC])
    pc = s[PC]
    if not plain(amap, pc):
        return PYTHON
    if s[M81]:
        cia_atn(s, s[HOSTL] & IEC_ATN)
    s[OPPC] = pc
    s[PC] = (pc + 1) & 0xFFFF
    n = execute(s, f, amap, store, cells, ev, peek(amap, store, pc))
    if n < 0:
        s[PC] = pc
        return PYTHON
    s[CYC] += n
    s[PCYC] += n
    s[STEPS] -= 1
    s[HALT] = s[PC] == RETURN_TRAP
    return DEFERRED if s[DEFER] >= 0 else OK


@kernel
def run(s, f, amap, store, cells, ev, limit):
    """Execute to a halt, cycle limit, step budget, IEC change, full log or Python."""
    while True:
        if s[HALT]:
            return HALTED
        if s[CYC] >= limit or s[STEPS] == 0:
            return LIMIT
        lines = drive_lines(s)
        code = step(s, f, amap, store, cells, ev)
        if code != OK:
            return code
        if s[LINES] and drive_lines(s) != lines:
            return MOVED


@kernel
def host_op(s, h, op, line, arg):
    """One xum1541 line action; whether it completed (a wait may not have)."""
    if op == WAIT:
        want = arg if arg < 2 else 1 - h[HB]
        return (bus_lines(s) & line != 0) == want
    if op in (SET, REL, PUT):
        bit = (h[HC] >> 7 & 1) ^ (arg == 1) if arg < 2 else h[HC] & 1
        on = op == SET or op == PUT and bit
        s[HOSTL] = s[HOSTL] | line if on else s[HOSTL] & ~line
    elif op == SAMPLE:
        h[HB] = bus_lines(s) & line != 0
        h[HC] = h[HC] >> 1 | h[HB] << 7
    else:
        h[HC] = h[HC] << 1 & 0xFF if op == SHL else h[HC] >> 1
    return True


@kernel
def transfer(s, f, amap, store, cells, ev, h, buf):
    """xum1541 byte transfers of buf with HOST_PROGRAMS[h[PROTO]], resumable."""
    prog = HOST_PROGRAMS[h[PROTO]]
    while h[HBYTE] < len(buf):
        op, line, arg = prog[h[HOP], 0], prog[h[HOP], 1], prog[h[HOP], 2]
        while not host_op(s, h, op, line, arg):
            if s[HALT]:
                return HALTED
            c0 = s[CYC]
            code = step(s, f, amap, store, cells, ev)
            h[HWAIT] += s[CYC] - c0
            if code != OK:
                return code
            if h[HWAIT] > h[BUDGET]:
                return LIMIT
        h[HWAIT], h[HOP] = 0, h[HOP] + 1
        if h[HOP] == h[PLEN]:
            buf[h[HBYTE]] = h[HC]
            h[HBYTE], h[HOP] = h[HBYTE] + 1, 0
            h[HC] = buf[h[HBYTE]] if h[HBYTE] < len(buf) else 0
    return OK


def fields(table):
    """(state indices, getter, names) for moving attributes in and out of the state."""
    return (
        np.array(list(table)),
        operator.attrgetter(*table.values()),
        tuple(table.values()),
    )


MPU_GROUP, VIA_GROUP, MECH_GROUP = map(fields, (MPU_FIELDS, VIA_FIELDS, MECH_FIELDS))
MEMORY = weakref.WeakKeyDictionary()
BUFFERS = np.zeros(STATE, np.int64), np.zeros(5), np.empty((EVENTS, 4))
STOCK = "step read write port_b drive_lines via_lines fsdir port_pins"


def memory(drive):
    """phys << KIND_BITS | kind per address, and the store."""
    if drive not in MEMORY:
        kind = np.array(drive._kind, np.int64)  # pylint: disable=protected-access
        phys = np.array(drive._phys, np.int64)  # pylint: disable=protected-access
        MEMORY[drive] = phys << KIND_BITS | kind, np.frombuffer(drive.store, np.uint8)
    return MEMORY[drive]


@functools.cache
def stock(cls):
    """cls computes as Drive1541 (Drive1571 for port A, Drive1581) does."""
    port_a = (Drive1571 if cls.MODEL == "1571" else Drive1541).port_a
    ref = Drive1581 if cls.MODEL == "1581" else Drive1541
    same = (getattr(cls, n, None) is getattr(ref, n, None) for n in STOCK.split())
    return cls.port_a is port_a and all(same)


def eligible(drive):
    """The drive, mechanism and media are the stock models this module mirrors."""
    mech = drive.mech
    if not (drive.fast and stock(type(drive))):
        return False
    if mech is None:
        return not any(map(callable, vars(drive).values()))
    if (type(mech), type(mech.media)) != (Mechanism, Media):
        return False
    hooks = sum(sum(map(callable, vars(o).values())) for o in (drive, mech, mech.media))
    return hooks == (mech.corrupt is not None)


def external_lines(drive):
    """IEC lines of the other devices with host ATN released and asserted, else -1."""
    bus = drive.bus
    if type(bus).lines is not Bus.lines:
        return -1, -1
    host, out = bus.host_lines, []
    for atn in (0, IEC_ATN):
        bus.host_lines = atn
        out.append(0)
        for d in bus.devices:
            out[-1] |= 0 if d is drive else d.drive_lines()
    bus.host_lines = host
    return out


def pack_mech(mech, s, f):
    """Mechanism state into s and f; the cell array run may use."""
    idx, get, _ = MECH_GROUP
    s[idx] = get(mech)
    k, key = mech._k, mech._key  # pylint: disable=protected-access
    cur = (mech.side, mech.halftrack)
    fresh = mech.media.tracks.get(cur)
    held = key == cur and k is not None
    cells = mech._cells if held else fresh  # pylint: disable=protected-access
    s[[MECH, KNONE, K, KSIDE, KHT, CSIDE, CHT, FRESH, CORRUPT, LOGGING]] = (
        1,
        k is None,
        k or 0,
        *(key or (-1, -1)),
        *cur,
        cells is not None and cells is fresh,
        mech.corrupt is not None,
        mech.log is not None,
    )
    s[W0 : W0 + len(mech.wd)] = mech.wd
    s[MREGS : MREGS + 16] = memoryview(mech.regs)
    start = mech._sync_start  # pylint: disable=protected-access
    f[:] = (
        mech.due,
        math.nan if start is None else start,
        mech.media.rpm,
        *mech.media.wander,
    )
    return np.zeros(1, np.uint8) if cells is None else cells


def pack(drive, steps, lines):
    """(s, f, amap, store, cells, ev) for run."""
    s, f, ev = BUFFERS
    s[:] = 0
    s[OPPC] = -1
    for (idx, get, _), obj in ((MPU_GROUP, drive.mpu), (VIA_GROUP, drive.via1)):
        s[idx] = get(obj)
    s[[CYC, HALT, STEPS, PBOUT, DEVICE, EXT, EXTA, HOSTL, LINES, TRK0, DEFER]] = (
        drive.cycles,
        drive.halted,
        steps,
        drive.pb_out,
        drive.device,
        *external_lines(drive),
        drive.bus.host_lines,
        lines,
        drive.MODEL == "1571",
        -1,
    )
    s[VREGS : VREGS + 16] = memoryview(drive.via1.regs)
    pack_cia(drive.cia, s)
    if drive.MODEL == "1581":
        s[M81], s[OPPC] = 1, -1
        s[W0:WEND] = drive.wd.w[W0:WEND]
        return (s, f, *memory(drive), drive.wd.flat, ev)
    cells = np.zeros(1, np.uint8) if drive.mech is None else pack_mech(drive.mech, s, f)
    return (s, f, *memory(drive), cells, ev)


def unpack(drive, s, f, cells, ev):
    """Write run's state back to the Python objects."""
    mech = drive.mech
    for (idx, _, names), obj in ((MPU_GROUP, drive.mpu), (VIA_GROUP, drive.via1)):
        vars(obj).update(zip(names, s[idx].tolist()))
    drive.via1.regs[:] = s[VREGS : VREGS + 16].astype(np.uint8).tobytes()
    unpack_cia(drive.cia, s)
    drive.cycles, drive.pb_out = int(s[CYC]), int(s[PBOUT])
    if s[HOSTL] != drive.bus.host_lines:
        drive.bus.host_lines = int(s[HOSTL])
    if s[HALT] and not drive.halted:
        drive.halted, drive._pending = True, 0  # pylint: disable=protected-access
    if s[M81]:
        drive.wd.w[W0:WEND] = s[W0:WEND]
    if mech is None:
        return
    idx, _, names = MECH_GROUP
    vars(mech).update(zip(names, s[idx].tolist()))
    mech.regs[:] = s[MREGS : MREGS + 16].astype(np.uint8).tobytes()
    key = (int(s[KSIDE]), int(s[KHT]))
    if not s[KNONE] and key != mech._key:  # pylint: disable=protected-access
        mech._key, mech._cells = key, cells  # pylint: disable=protected-access
    mech._k = None if s[KNONE] else int(s[K])  # pylint: disable=protected-access
    mech.wd[:] = s[W0 : W0 + len(mech.wd)]
    mech.due = math.inf if math.isinf(f[DUE]) else int(f[DUE])
    start = None if math.isnan(f[SYNC_START]) else float(f[SYNC_START])
    mech._sync_start = start  # pylint: disable=protected-access
    for kind, value, t0, t1 in ev[: s[NEV]].tolist():
        if kind == BYTE_EVENT:
            mech.log.append(("byte", t0, int(value)))
        else:
            mech.log.append(("sync", t0, t1, int(value)))


def python_step(drive, lines):
    """One py65 instruction; whether it changed the drive's IEC lines."""
    before = drive.drive_lines()
    drive.step()
    return lines and drive.drive_lines() != before


def finish(drive, s, code):
    """Python's share of a stop: a deferred update or a py65 step (its cycles)."""
    if code == DEFERRED:
        if s[RESAMPLE]:
            drive.mech._resample(int(s[DEFER]))  # pylint: disable=protected-access
        drive.mech.update(int(s[DEFER]))
    return drive.step() if code == PYTHON else 0


def run_transfer(drive, protocol, buf, budget):
    """Move buf with the xum1541 protocol; SimTimeout as SimCBM's waits raise it."""
    h = np.zeros(8, np.int64)
    (h[PROTO], h[PLEN]), h[BUDGET] = HOST_LENGTHS[protocol], budget
    h[HC] = buf[0] if len(buf) else 0
    while h[HBYTE] < len(buf):
        s, f, amap, store, cells, ev = pack(drive, -1, False)
        code = transfer(s, f, amap, store, cells, ev, h, buf)
        unpack(drive, s, f, cells, ev)
        if code == HALTED:
            raise SimTimeout("drive halted while host was waiting")
        h[HWAIT] += finish(drive, s, code)
        if h[HWAIT] > h[BUDGET]:
            raise SimTimeout("cycle budget exceeded")


def run_drive(drive, limit, steps=-1, lines=False):
    """Run to a halt, cycle ``limit``, ``steps`` instructions or (``lines``) an IEC change."""
    fast, limit = eligible(drive), int(min(limit, NEVER))
    while not drive.halted and drive.cycles < limit and steps:
        if not fast:
            steps -= 1
            if python_step(drive, lines):
                return
            continue
        s, f, amap, store, cells, ev = pack(drive, steps, lines)
        code = run(s, f, amap, store, cells, ev, limit)
        unpack(drive, s, f, cells, ev)
        steps = int(s[STEPS])
        if code == MOVED:
            return
        if code == PYTHON:
            steps -= 1
            if python_step(drive, lines):
                return
        finish(drive, s, code if code == DEFERRED else OK)


def warm():
    """Compile (or load) the kernels: short runs and transfers to halted drives."""
    for drive in (disk_drive("1541", Media()), Drive1581()):
        drive.load(0x300, b"\x60")
        drive.call(0x300)
        run_drive(drive, drive.cycles + 100)
        with contextlib.suppress(SimTimeout):
            run_transfer(drive, "s1_read", np.zeros(1, np.uint8), 0)
