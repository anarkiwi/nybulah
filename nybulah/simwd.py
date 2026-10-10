"""WD1772, 1581 mechanism and MFM media as numba kernels over simfast's state vector.

Sources: WD1772 datasheet ("ds"), 1581 service manual/schematic ("sm"), 1581 DOS. Byte j
of an n-byte track passes in rotation units [jP/n, (j+1)P/n) of P (2 MHz cycles), index
first; the spindle turns while PA2 runs the motor; events run lazily up to each access.
"""

# pylint: disable=too-many-return-statements,too-many-branches

import numpy as np
from numba import njit

from .analysis.crc import SYNC3, TABLE

CPU_HZ = 2_000_000  # sm schematic sheet 2: 16 MHz Y1 / 74LS93 U10 = PHI0 2 MHz
MFM_BPS = 250_000  # ds general description: double density data rate
NOMINAL_RPM = 300.0
INDEX_FRACTION = 0.02
NEVER = 1 << 62
MARK, WEAK = 1, 2
kernel = njit(cache=True, _nrt=False)

W0 = 136  # after simfast's and simcia's slots (asserted in simfast)
WCMD, WSTAT, WTRK, WSEC, WDAT, WTYPE, WMO, WSU, WPH, WT, WB, WN, WIP, WSYN = range(
    W0, W0 + 14
)
WCRC, WLEN, WCRCN, WAFTER, WIDLE, WDIR, WCYL, WHEAD, WMOTOR, WSPC, WSPR = range(
    W0 + 14, W0 + 25
)
WBUMPS, WINNER, WOUTER, WISTOP, WT0MODE, WWP, WDISK, WCHG, WPULSES = range(
    W0 + 25, W0 + 34
)
WPER, WIDX, WSPIN, WWLEN, WNT, WCAP, WCHIP, WVIOL, WEARLY, WRNG, WFI = range(
    W0 + 34, W0 + 45
)
WCW, WOLD, WFIB, WFBUSY, WT0LIVE = range(W0 + 45, W0 + 50)
WID = W0 + 50
WLW = WID + 6
WEND = WLW + 4

IDLE, STEP, DELAY, SPIN, RTWAIT, WTWAIT = range(6)
IDSRCH, IDFLD, DAMSRCH, RDATA, RTDATA, WGAP, WDATA, WTFIRST, WTDATA = range(6, 15)

BUSY, DRQ, LD, CRCE, RNF, RT, WPROT, MO = (1 << i for i in range(8))
IP, T0, SU = DRQ, LD, RT
H_FLAG, V_FLAG, E_FLAG, U_FLAG, M_FLAG = 0x08, 0x04, 0x04, 0x10, 0x10
T0_OK, T0_NEVER, T0_ALWAYS = range(3)
CHIPS = (1772, 1770)
# ds command summary r1r0 (WD1772) and the WD177X-00 WD1770 rates, in ms
STEP_CYCLES = np.array([[6, 12, 2, 3], [6, 12, 20, 30]], np.int64) * (CPU_HZ // 1000)
SETTLE = 15 * CPU_HZ // 1000  # ds type I/II: 15 ms head settling (v, e flags)
IP_LIMIT = 6  # ds flowcharts: "6 IP passed" ends a verify (SE) or search (RNF)
SPINUP_IP = 6  # ds: h = 0 with MO low waits 6 index pulses
MO_IDLE_IP = 9  # ds: MO drops after 9 idle revolutions
DAM_WINDOW = 43  # ds read sector: DAM within 43 bytes of the ID CRC (MFM)
WSEC_DRQ, WSEC_GAP = 2, 22  # ds write sector flowchart and text (MFM)
WT_FIRST = 3  # ds write track: first byte within 3 byte times
# ds status register table, MFM (sm sheet 2: DDEN grounded), WD CLK 8 MHz from the same
# Y1 as the CPU: command write -> busy bit, -> status bits 1-7; register write -> read-back
# of the same register; ds type IV: force interrupt -> next command
BUSY_VALID, STATUS_VALID = (us * CPU_HZ // 1_000_000 for us in (24, 32))
EARLY = FORCE_GAP = 16 * CPU_HZ // 1_000_000
DIR_SETUP = (
    24 * CPU_HZ // 1_000_000
)  # ds type I: DIRC valid 24 us before the first pulse
IDAM_MIN, DAM_FIRST, DAM_LAST, DAM_NORMAL = 0xFC, 0xF8, 0xFB, 0xFA  # ds type II
SYNC = 0xA1
PA_SIDE, PA_RDY, PA_MOTOR, PA_CHNG = 0x01, 0x02, 0x04, 0x80  # iodef.src port A
OUTER_STOP, INNER_STOP = -1, 83  # simulation bounds of the head carriage
CYLINDERS = INNER_STOP + 1


@kernel
def rot(w, c):
    """Spindle rotation units at cycle c."""
    if not w[WMOTOR]:
        return w[WSPR]
    return w[WSPR] + max(0, c - w[WSPC])


@kernel
def tkey(w):
    """Media track index under the head, -1 off the media or without a disk."""
    cyl = w[WCYL]
    if not w[WDISK] or cyl < 0 or 2 * cyl >= w[WNT]:
        return -1
    return 2 * cyl + w[WHEAD]


@kernel
def tlen(w, m, t):
    """Bytes in track t (one revolution at the drive's speed off the media)."""
    if t < 0:
        return w[WWLEN]
    return np.int64(m[2 * t]) | np.int64(m[2 * t + 1]) << 8


@kernel
def at(w, b, n):
    """Cycle at which byte boundary b (b // n revolutions plus byte b % n) passes."""
    if w[WMOTOR] == 0 or w[WDISK] == 0:
        return NEVER
    p = w[WPER]
    return w[WSPC] + b // n * p + (b % n * p + n - 1) // n - w[WSPR]


@kernel
def cursor(w, m, c, index):
    """Next byte boundary (or index boundary) of the track under the head after c."""
    n, r, p = tlen(w, m, tkey(w)), rot(w, c), w[WPER]
    w[WB] = (r // p + 1) * n if index else r // p * n + r % p * n // p + 1


@kernel
def rnd(w):
    """Next weak-bit byte (31-bit LCG)."""
    w[WRNG] = (w[WRNG] * 1103515245 + 12345) & 0x7FFFFFFF
    return w[WRNG] >> 16 & 0xFF


@kernel
def get(w, m, j):
    """(value, mark) of byte j under the head; weak bytes and no media read random."""
    t = tkey(w)
    if t < 0:
        return rnd(w), 0
    i = 2 * w[WNT] + t * w[WCAP] + j
    f = np.int64(m[i + w[WNT] * w[WCAP]])
    if f & WEAK:
        return rnd(w), 0
    return np.int64(m[i]), f & MARK


@kernel
def put(w, m, j, v, mark):
    """Write byte j under the head."""
    t = tkey(w)
    if t >= 0:
        i = 2 * w[WNT] + t * w[WCAP] + j
        m[i] = v
        m[i + w[WNT] * w[WCAP]] = mark


@kernel
def crc(c, v):
    """CRC-16-CCITT of one more byte (analysis.crc)."""
    return (c << 8 & 0xFFFF) ^ np.int64(TABLE[(c >> 8 ^ v) & 0xFF])


@kernel
def tr00(w):
    """Drive TR00: the head at cylinder 0 (Shugart interface), or a stuck sensor."""
    mode = w[WT0MODE]
    return mode == T0_ALWAYS or mode == T0_OK and w[WCYL] == 0


@kernel
def index_pulse(w, c):
    """Drive index pulse."""
    return w[WDISK] != 0 and rot(w, c) % w[WPER] < w[WIDX]


@kernel
def pulse(w, d):
    """One step pulse in direction d, stopped at the outer and inner stops; with a disk
    in it clears /DISK CHNG (dskint.src wait_mtr)."""
    w[WDIR], w[WPULSES] = d, w[WPULSES] + 1
    pos = w[WCYL] + d
    if pos < w[WOUTER]:
        w[WBUMPS] += 1
    elif pos > w[WISTOP]:
        w[WINNER] += 1
    else:
        w[WCYL] = pos
    if w[WDISK]:
        w[WCHG] = 0


@kernel
def done(w, c):
    """Command complete (INTRQ is not wired on the 1581: BUSY only)."""
    w[WPH], w[WCRCN], w[WIDLE] = IDLE, 0, rot(w, c)
    w[WSTAT] &= ~BUSY


@kernel
def deliver(w, v):
    """A byte into the data register: DRQ, lost data if the last was never read."""
    if w[WSTAT] & DRQ:
        w[WSTAT] |= LD
    w[WDAT] = v
    w[WSTAT] |= DRQ


@kernel
def take(w):
    """The data register for writing: zero and lost data if not loaded since DRQ."""
    if w[WSTAT] & DRQ:
        w[WSTAT] |= LD
        return 0
    return w[WDAT]


@kernel
def mo_idle(w, c):
    """MO drops after MO_IDLE_IP index pulses without a command."""
    if w[WMO] != 0 and w[WPH] == IDLE and w[WDISK] != 0:
        if rot(w, c) // w[WPER] - w[WIDLE] // w[WPER] >= MO_IDLE_IP:
            w[WMO] = w[WSU] = 0


@kernel
def start(w, m, c):
    """A command (not Force Interrupt) written while not busy: MO rises for every one
    (ds pin MO: "enable the spindle motor prior to read, write or stepping operations");
    h = 0 with MO low adds the spin-up wait."""
    cmd = w[WCMD]
    mo_idle(w, c)
    w[WTYPE] = 1 if cmd < 0x80 else (2 if cmd < 0xC0 else 3)
    w[WT0LIVE] = w[WTYPE] == 1
    w[WSTAT] = BUSY
    w[WN] = w[WIP] = w[WSYN] = w[WCRCN] = 0
    spin = not cmd & H_FLAG and not w[WMO]
    w[WMO] = 1
    if not spin:
        w[WSU] = 1
    if spin:
        w[WPH] = SPIN
        cursor(w, m, c, True)
    else:
        go(w, m, c)


@kernel
def go(w, m, c):
    """After any spin-up: the e flag's settling delay of type II/III commands."""
    if w[WTYPE] > 1 and w[WCMD] & E_FLAG:
        w[WPH], w[WT], w[WAFTER] = DELAY, c + SETTLE, 0
    else:
        main(w, m, c)


@kernel
def main(w, m, c):
    """The command proper."""
    cmd = w[WCMD]
    if w[WTYPE] == 1:
        if cmd < 0x10:
            w[WTRK], w[WDAT] = 0xFF, 0
        w[WPH], w[WT], w[WN] = STEP, c + DIR_SETUP, 0
        return
    if ((cmd & 0xE0) == 0xA0 or cmd >= 0xF0) and w[WWP]:
        w[WSTAT] |= WPROT
        done(w, c)
    elif (cmd & 0xF0) == 0xE0:
        w[WPH] = RTWAIT
        cursor(w, m, c, True)
    else:
        w[WPH] = WTFIRST if cmd >= 0xF0 else IDSRCH
        if cmd >= 0xF0:
            w[WSTAT] |= DRQ
        cursor(w, m, c, False)


@kernel
def t1_end(w, t):
    """Stepping done: verify after the settling time, else complete."""
    if w[WCMD] & V_FLAG:
        w[WPH], w[WT], w[WAFTER] = DELAY, t + SETTLE, 1
    else:
        done(w, t)


@kernel
def step_event(w, t):
    """One pass of the type I loop at t: Restore/Seek compare and step, or Step."""
    cmd = w[WCMD]
    rate = STEP_CYCLES[w[WCHIP], cmd & 3]
    if cmd >= 0x20:
        if w[WN]:
            t1_end(w, t)
            return
        d = w[WDIR] if cmd < 0x40 else (1 if cmd < 0x60 else -1)
        if cmd & U_FLAG:
            w[WTRK] = (w[WTRK] + d) & 0xFF
        pulse(w, d)
        w[WN], w[WT] = 1, t + rate
        return
    if w[WTRK] == w[WDAT]:
        t1_end(w, t)
        return
    d = 1 if w[WDAT] > w[WTRK] else -1
    w[WDIR], w[WTRK] = d, (w[WTRK] + d) & 0xFF
    if d < 0 and tr00(w):
        w[WTRK] = 0
        t1_end(w, t)
        return
    pulse(w, d)
    w[WT] = t + rate


@kernel
def delay_event(w, m, t):
    """End of a settling delay: verify (type I) or the command (type II/III)."""
    if w[WAFTER]:
        w[WPH], w[WIP], w[WSYN] = IDSRCH, 0, 0
        cursor(w, m, t, False)
    else:
        main(w, m, t)


@kernel
def track_slot(w, m, j):
    """Write Track: byte slot j from the data register (ds write track table, MFM)."""
    if w[WCRCN]:
        w[WCRCN] = 0
        put(w, m, j, w[WCRC] & 0xFF, 0)
        return
    v = take(w)
    w[WSTAT] |= DRQ
    if v == 0xF5:
        put(w, m, j, SYNC, MARK)
        w[WCRC] = SYNC3
    elif v == 0xF6:
        put(w, m, j, 0xC2, MARK)
        w[WCRC] = crc(w[WCRC], 0xC2)
    elif v == 0xF7:
        put(w, m, j, w[WCRC] >> 8, 0)
        w[WCRCN] = 1
    else:
        put(w, m, j, v, 0)
        w[WCRC] = crc(w[WCRC], v)


@kernel
def index_event(w, m, b, n, t):
    """An index leading edge awaited by spin-up, Read Track or Write Track."""
    ph = w[WPH]
    if ph == SPIN:
        w[WB], w[WN] = b + n, w[WN] + 1
        if w[WN] >= SPINUP_IP:
            w[WSU], w[WN] = 1, 0
            go(w, m, t)
    elif ph == RTWAIT:
        w[WPH], w[WN] = RTDATA, 0
    else:
        key, size = tkey(w), w[WWLEN]
        if key >= 0:
            m[2 * key], m[2 * key + 1] = size & 0xFF, size >> 8
        w[WPH], w[WN], w[WB] = WTDATA, 0, b // n * size + 1
        track_slot(w, m, 0)


@kernel
def search(w, v, mk):
    """ID search: exactly three A1* then an ID address mark (ds type II)."""
    if mk and v == SYNC:
        w[WSYN] += 1
        return
    if w[WSYN] == 3 and not mk and v >= IDAM_MIN:
        w[WPH], w[WN], w[WCRC] = IDFLD, 0, crc(SYNC3, v)
    w[WSYN] = 0


@kernel
def id_byte(w, v, t):
    """One of the six ID field bytes: track, side, sector, length, CRC."""
    w[WID + w[WN]] = v
    w[WCRC], w[WN] = crc(w[WCRC], v), w[WN] + 1
    cmd = w[WCMD]
    ra = (cmd & 0xF0) == 0xC0
    if ra:
        deliver(w, v)
    if w[WN] < 6:
        return
    ok = w[WCRC] == 0
    w[WPH], w[WSYN] = IDSRCH, 0
    if ra:
        w[WSTAT] |= 0 if ok else CRCE
        w[WSEC] = w[WID]
        done(w, t)
        return
    if w[WID] != w[WTRK] or w[WTYPE] == 2 and w[WID + 2] != w[WSEC]:
        return
    if not ok:
        w[WSTAT] |= CRCE
        return
    w[WSTAT] &= ~CRCE
    if w[WTYPE] == 1:
        done(w, t)
        return
    w[WLEN], w[WN] = 128 << (w[WID + 3] & 3), 0
    w[WPH] = WGAP if cmd & 0x20 else DAMSRCH


@kernel
def dam_byte(w, v, mk):
    """Data address mark search after a matching ID: three or more A1*, then F8-FB."""
    w[WN] += 1
    if mk and v == SYNC:
        w[WSYN] += 1
    elif w[WSYN] >= 3 and not mk and DAM_FIRST <= v <= DAM_LAST:
        w[WSTAT] = w[WSTAT] & ~RT | (RT if v < DAM_NORMAL else 0)
        w[WCRC], w[WPH], w[WN] = crc(SYNC3, v), RDATA, 0
        return
    else:
        w[WSYN] = 0
    if w[WN] >= DAM_WINDOW:
        w[WPH], w[WSYN] = IDSRCH, 0


@kernel
def data_byte(w, v, t):
    """Read Sector data and CRC bytes; m continues with the next sector."""
    w[WCRC] = crc(w[WCRC], v)
    if w[WN] < w[WLEN]:
        deliver(w, v)
    w[WN] += 1
    if w[WN] < w[WLEN] + 2:
        return
    if w[WCRC]:
        w[WSTAT] |= CRCE
        done(w, t)
    elif w[WCMD] & M_FLAG:
        w[WSEC] = (w[WSEC] + 1) & 0xFF
        w[WPH], w[WIP], w[WSYN] = IDSRCH, 0, 0
    else:
        done(w, t)


@kernel
def read_event(w, m, b, n, t):
    """Byte (b - 1) % n assembled at boundary b while reading."""
    v, mk = get(w, m, (b - 1) % n)
    ph = w[WPH]
    if ph == RTDATA:
        deliver(w, v)
        if b % n == 0:
            done(w, t)
        return
    if ph == RDATA:
        data_byte(w, v, t)
        return
    if b % n == 0:
        w[WIP] += 1
    if ph == IDSRCH:
        search(w, v, mk)
    elif ph == IDFLD:
        id_byte(w, v, t)
    else:
        dam_byte(w, v, mk)
    if w[WPH] == IDSRCH and w[WIP] >= IP_LIMIT:
        w[WSTAT] |= RNF
        done(w, t)


@kernel
def sector_slot(w, m, j, t):
    """Write Sector: slot k from 22 bytes after the ID CRC (ds write sector, MFM)."""
    k, size = w[WN], w[WLEN]
    w[WN] = k + 1
    if k < 12:
        put(w, m, j, 0, 0)
    elif k < 15:
        put(w, m, j, SYNC, MARK)
    elif k == 15:
        v = DAM_FIRST if w[WCMD] & 1 else DAM_LAST
        w[WCRC] = crc(SYNC3, v)
        put(w, m, j, v, 0)
    elif k < 16 + size:
        v = take(w)
        if k < 15 + size:
            w[WSTAT] |= DRQ
        w[WCRC] = crc(w[WCRC], v)
        put(w, m, j, v, 0)
    elif k < 18 + size:
        put(w, m, j, w[WCRC] >> 8 if k == 16 + size else w[WCRC] & 0xFF, 0)
    elif k == 18 + size:
        put(w, m, j, 0xFF, 0)
    else:
        done(w, t)


@kernel
def write_event(w, m, b, n, t):
    """Byte slot b % n starting at boundary b while writing (or counting to a write)."""
    ph, j = w[WPH], b % n
    if ph == WTFIRST:
        w[WN] += 1
        if w[WN] < WT_FIRST:
            return
        if w[WSTAT] & DRQ:
            w[WSTAT] |= LD
            done(w, t)
            return
        w[WPH], w[WB] = WTWAIT, (b // n + 1) * n
    elif ph == WTDATA:
        if j == 0:
            done(w, t)
        else:
            track_slot(w, m, j)
    elif ph == WGAP:
        w[WN] += 1
        if w[WN] == WSEC_DRQ:
            w[WSTAT] |= DRQ
        if w[WN] < WSEC_GAP:
            return
        if w[WSTAT] & DRQ:
            w[WSTAT] |= LD
            done(w, t)
            return
        w[WPH], w[WN] = WDATA, 0
        sector_slot(w, m, j, t)
    else:
        sector_slot(w, m, j, t)


@kernel
def due(w, m):
    """Cycle of the controller's next event."""
    ph = w[WPH]
    if ph == IDLE:
        return NEVER
    if ph <= DELAY:
        return w[WT]
    return at(w, w[WB], tlen(w, m, tkey(w)))


@kernel
def event(w, m, t):
    """Process the event due at t."""
    ph = w[WPH]
    if ph == STEP:
        step_event(w, t)
    elif ph == DELAY:
        delay_event(w, m, t)
    else:
        b, n = w[WB], tlen(w, m, tkey(w))
        w[WB] = b + 1
        if ph <= WTWAIT:
            index_event(w, m, b, n, t)
        elif ph <= RTDATA:
            read_event(w, m, b, n, t)
        else:
            write_event(w, m, b, n, t)


@kernel
def wd_run(w, m, c):
    """Process every controller event up to cycle c."""
    t = due(w, m)
    while t <= c:
        event(w, m, t)
        t = due(w, m)


@kernel
def access(w, pc):
    """mfmmacro.src WDTEST: no WD access instruction at an address ending in %00."""
    if pc >= 0 and pc & 3 == 0:
        w[WVIOL] += 1


@kernel
def status(w, c):
    """Status register: type I shows MO, WP, SU, SE, CRC, T0, IP live (ds summary)."""
    mo_idle(w, c)
    mo = MO if w[WMO] else 0
    if c < w[WFIB]:
        return BUSY | mo
    if w[WTYPE] != 1:
        return w[WSTAT] & ~MO | mo
    v = w[WSTAT] & (BUSY | CRCE | RNF) | mo | (SU if w[WSU] else 0)
    v |= (T0 if w[WT0LIVE] and tr00(w) else 0) | (IP if index_pulse(w, c) else 0)
    return v | (WPROT if w[WWP] else 0)


@kernel
def visible(w, c, count):
    """Status as read at c: the register before the last command write until its bit 0
    is valid BUSY_VALID cycles on and its bits 1-7 STATUS_VALID on (ds); count adds an
    early read."""
    v, dt = status(w, c), c - w[WCW]
    if dt >= STATUS_VALID:
        return v
    w[WEARLY] += count
    if dt < BUSY_VALID:
        return w[WOLD]
    return v & BUSY | w[WOLD] & ~BUSY


@kernel
def wd_read(w, m, reg, c, pc):
    """Register read at cycle c by the instruction at pc."""
    wd_run(w, m, c)
    access(w, pc)
    if reg == 0:
        return visible(w, c, 1)
    if c - w[WLW + reg] < EARLY:
        w[WEARLY] += 1
    if reg == 3:
        w[WSTAT] &= ~DRQ
    return w[WTRK - 1 + reg]


@kernel
def force(w, c):
    """Force Interrupt: end a command (status kept) or show type I status without T0
    until a type I command runs (ds status note 4: T0 is polled after a type I command;
    1581 hardware reads $80 idle on cylinder 0), which a $D0
    written while idle holds back for WFBUSY cycles: busy with the type I bits clear
    (1581 hardware; the datasheet gives no duration, the DOS waits for busy to clear,
    msub.src wdabort)."""
    w[WFI] = c
    if w[WSTAT] & BUSY:
        done(w, c)
    else:
        w[WTYPE], w[WSTAT], w[WSU], w[WFIB] = 1, 0, 0, c + w[WFBUSY]
        w[WT0LIVE] = 0


@kernel
def wd_write(w, m, reg, v, c, pc):
    """Register write at cycle c by the instruction at pc; while busy only Force
    Interrupt and the data register are accepted (ds)."""
    wd_run(w, m, c)
    access(w, pc)
    busy = w[WSTAT] & BUSY or c < w[WFIB]
    if reg == 0:
        w[WOLD], w[WCW] = visible(w, c, 0), c
        if v & 0xF0 == 0xD0:
            force(w, c)
        elif not busy:
            if c - w[WFI] < FORCE_GAP:
                w[WEARLY] += 1
            w[WCMD] = v
            start(w, m, c)
    elif reg == 3:
        w[WDAT] = v
        w[WSTAT] &= ~DRQ
    elif not busy:
        w[WTRK - 1 + reg] = v
    w[WLW + reg] = c


@kernel
def wd_control(w, m, c, pa):
    """CIA port A output pins at cycle c: PA2 low runs the motor, PA0 low selects
    physical head 1 (iodef.src; sm sheet 3, 7407 to drive pins 16 and 32)."""
    wd_run(w, m, c)
    motor = 0 if pa & PA_MOTOR else 1
    head = 0 if pa & PA_SIDE else 1
    if motor != w[WMOTOR]:
        if motor:
            w[WSPC] = c + w[WSPIN]
        else:
            w[WSPR] = rot(w, c)
        w[WMOTOR] = motor
    if head != w[WHEAD]:
        w[WHEAD] = head
        if w[WPH] >= SPIN:
            cursor(w, m, c, w[WPH] <= WTWAIT)


@kernel
def wd_inputs(w, m, c, pa):
    """Port A input pins pa with /RDY (disk in and up to speed) and /DISK CHNG."""
    wd_run(w, m, c)
    if w[WDISK] != 0 and w[WMOTOR] != 0 and c >= w[WSPC]:
        pa &= ~PA_RDY
    if w[WCHG]:
        pa &= ~PA_CHNG
    return pa


def fmtrk_stream(cyl, side, sectors=None, gap3=35, fill=0x4E):
    """Write Track image of a 1581 track as mrout.src fmtrk issues it (sectors 1..10 of
    512 bytes, ID side byte side), without the fill to the index."""
    out = [np.full(32, fill, np.uint8)]
    data = np.zeros((10, 512), np.uint8) if sectors is None else np.asarray(sectors)
    for r, payload in enumerate(data, 1):
        out.append(np.array([0] * 12 + [0xF5] * 3 + [0xFE, cyl, side, r, 2, 0xF7]))
        out.append(np.array([fill] * 22 + [0] * 12 + [0xF5] * 3 + [0xFB]))
        out += [np.asarray(payload, np.uint8), np.array([0xF7] + [fill] * gap3)]
    return np.concatenate(out).astype(np.uint8)


def stream_track(stream, n):
    """(data, mark) of the n-byte track Write Track records from stream (ds write track
    table, MFM), the last stream byte repeating to the index."""
    data, mark = np.zeros(n, np.uint8), np.zeros(n, bool)
    c, j, k = 0xFFFF, 0, 0
    while j < n:
        v = int(stream[min(k, len(stream) - 1)])
        k += 1
        if v == 0xF7:
            for b in (c >> 8, c & 0xFF)[: n - j]:
                data[j], j = b, j + 1
            continue
        data[j], mark[j] = {0xF5: SYNC, 0xF6: 0xC2}.get(v, v), v in (0xF5, 0xF6)
        c = SYNC3 if v == 0xF5 else int(crc(c, int(data[j])))
        j += 1
    return data, mark


class MfmMedia:
    """A double-density disk: tracks keyed (cylinder, physical head) in one flat array.

    The spindle turns at rpm with the index active for index_fraction of a turn; never
    written tracks are weak (random) and write_len (one turn at MFM_BPS) long.
    """

    def __init__(self, tracks=None, rpm=NOMINAL_RPM, **kw):
        self.rpm = rpm
        self.index_fraction = kw.pop("index_fraction", INDEX_FRACTION)
        self.cylinders = kw.pop("cylinders", CYLINDERS)
        self.write_len = round(MFM_BPS / 8 * 60 / rpm)
        tracks = dict(tracks or {})
        cap = max([self.write_len] + [len(v[0]) for v in tracks.values()])
        self.capacity = kw.pop("capacity", cap)
        if kw:
            raise TypeError(f"unexpected {sorted(kw)}")
        nt, cap = 2 * self.cylinders, self.capacity
        self.flat = np.zeros(2 * nt + 2 * nt * cap, np.uint8)
        self.data = self.flat[2 * nt : 2 * nt + nt * cap].reshape(nt, cap)
        self.flags = self.flat[2 * nt + nt * cap :].reshape(nt, cap)
        self.flags[:] = WEAK
        self.lengths = self.flat[: 2 * nt].view("<u2")
        self.lengths[:] = self.write_len
        for (cyl, head), track in tracks.items():
            self.set_track(cyl, head, *track)

    @property
    def period(self):
        """Cycles per revolution."""
        return round(CPU_HZ * 60 / self.rpm)

    def set_track(self, cyl, head, data, mark=None, weak=None):
        """Replace a track; mark and weak default to none."""
        t, n = 2 * cyl + head, len(data)
        if n > self.capacity:
            raise ValueError(f"{n} bytes exceed the capacity {self.capacity}")
        self.lengths[t] = n
        self.data[t, :n] = data
        none = np.zeros(n, bool)
        mark = np.asarray(none if mark is None else mark, bool)
        weak = np.asarray(none if weak is None else weak, bool)
        self.flags[t, :n] = mark * MARK | weak * WEAK

    def track(self, cyl, head):
        """(data, mark, weak) copies of a track."""
        t = 2 * cyl + head
        n, f = int(self.lengths[t]), self.flags[t]
        return self.data[t, :n].copy(), f[:n] & MARK > 0, f[:n] & WEAK > 0

    @classmethod
    def formatted(cls, cylinders=80, sectors=None, **kw):
        """A disk formatted as the 1581 DOS does (fmtrk): H = 0 on physical head 1;
        sectors(cyl, side) may supply ten 512-byte sectors (no $F5-$F7: Write Track)."""
        media = cls(**kw)
        for cyl in range(cylinders):
            for side in (0, 1):
                payload = None if sectors is None else sectors(cyl, side)
                stream = fmtrk_stream(cyl, side, payload)
                media.set_track(cyl, 1 - side, *stream_track(stream, media.write_len))
        return media


class Wd:  # pylint: disable=too-many-public-methods
    """WD1772 (or WD1770 step rates) and the 1581 mechanism holding a disk.

    Steps past ``stops`` (outer, inner cylinder) count in bumps / inner_stops; ``tr00`` is
    T0_OK or a failed sensor (T0_NEVER, T0_ALWAYS); ``spinup`` is in cycles, as is
    ``force_busy``, how long a $D0 written while idle keeps busy set.
    """

    def __init__(self, media=None, cylinder=0, write_protect=False, **kw):
        w = self.w = np.zeros(WEND, np.int64)
        w[WCHIP] = CHIPS.index(kw.pop("chip", 1772))
        w[WSPIN] = kw.pop("spinup", 0)
        w[WFBUSY] = kw.pop("force_busy", 0)
        w[WT0MODE] = kw.pop("tr00", T0_OK)
        w[WOUTER], w[WISTOP] = kw.pop("stops", (OUTER_STOP, INNER_STOP))
        w[WRNG] = kw.pop("seed", 0) & 0x7FFFFFFF
        changed = kw.pop("changed", True)
        if kw:
            raise TypeError(f"unexpected {sorted(kw)}")
        w[WCYL], w[WWP], w[WDIR], w[WTYPE], w[WHEAD] = cylinder, write_protect, 1, 1, 1
        w[WLW : WLW + 4] = w[WFI] = w[WCW] = -NEVER
        self.media, self.flat = None, np.zeros(1, np.uint8)
        self.insert(media, 0, changed)

    def insert(self, media, c=0, changed=True):
        """Put media (None: no disk) in the drive at cycle c; /DISK CHNG latches."""
        w = self.w
        self.sync(c)
        self.media = media
        self.flat = np.zeros(1, np.uint8) if media is None else media.flat
        w[WDISK], w[WCHG] = media is not None, changed
        if media is not None:
            w[WPER], w[WWLEN] = media.period, media.write_len
            w[WIDX] = round(media.index_fraction * media.period)
            w[WNT], w[WCAP] = 2 * media.cylinders, media.capacity
        else:
            w[WPER], w[WWLEN] = round(CPU_HZ * 60 / NOMINAL_RPM), 1
        if w[WPH] >= SPIN:
            cursor(w, self.flat, c, w[WPH] <= WTWAIT)

    def sync(self, c):
        """Run the controller to cycle c."""
        wd_run(self.w, self.flat, c)

    def read(self, reg, c, pc=-1):
        """Register read at cycle c by the instruction at pc."""
        return int(wd_read(self.w, self.flat, reg, c, pc))

    def write(self, reg, value, c, pc=-1):
        """Register write at cycle c by the instruction at pc."""
        wd_write(self.w, self.flat, reg, value, c, pc)

    def control(self, c, pa):
        """CIA port A output pins changed at cycle c."""
        wd_control(self.w, self.flat, c, pa)

    def inputs(self, c, pa):
        """Port A input pins with /RDY and /DISK CHNG at cycle c."""
        return int(wd_inputs(self.w, self.flat, c, pa))

    def _get(self, slot):
        return int(self.w[slot])

    @property
    def bumps(self):
        """Step pulses against the outer stop."""
        return self._get(WBUMPS)

    @property
    def inner_stops(self):
        """Step pulses against the inner stop."""
        return self._get(WINNER)

    @property
    def cylinder(self):
        """Head carriage position."""
        return self._get(WCYL)

    @property
    def head(self):
        """Selected physical head."""
        return self._get(WHEAD)

    @property
    def pulses(self):
        """Step pulses issued."""
        return self._get(WPULSES)

    @property
    def violations(self):
        """WD accesses breaking the WDTEST address rule."""
        return self._get(WVIOL)

    @property
    def early(self):
        """Accesses inside the datasheet's delays: status reads after a command write,
        read-backs after a register write, commands after a Force Interrupt."""
        return self._get(WEARLY)

    @property
    def busy(self):
        """A command is executing."""
        return bool(self.w[WSTAT] & BUSY)

    @property
    def track(self):
        """Track register."""
        return self._get(WTRK)

    @property
    def write_protect(self):
        """Write protect sensor (PB6 low and WD WPRT)."""
        return bool(self.w[WWP])

    @write_protect.setter
    def write_protect(self, value):
        self.w[WWP] = bool(value)
