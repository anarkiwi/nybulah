# X and SRQ transports: drive-timed IEC transfers

X moves bytes between a ZoomFloppy (xum1541 firmware v9+) and drive code in a
1541/1571 over CLK and DATA only. Firmware v10 adds burst X (below), which the
host uses whenever plugin and firmware offer it; this section describes the v9
per-byte form it falls back to. ATN is never touched, so DOS drives that
share the bus stay idle (they only react to ATN). The drive is the timing
master: its code runs a fixed, branch-free schedule after a SYNC edge, and the
16 MHz adapter, which can time edges to a fraction of a microsecond, samples or
drives the lines at offsets derived from that schedule.

Sources: drive side `drive/proto_x.inc` (assembled into `monitor_s3.bin`),
reference model `nybulah/simx.py`, host helper `nybulah/fastx.py`; adapter
side `xum1541/x.c` and the plugin's `opencbm_plugin_x[2]_read_n/_write_n`.

## Lines and idle state

| Line | Idle | Use |
|---|---|---|
| ATN | released | unused |
| DATA | released | host "go"; data bit A of each pair |
| CLK | released | drive SYNC; data bit B of each pair |

The monitor's VIA1 writes never set ATNA (PB4): with ATN released that would
pull DATA through the auto-acknowledge gate.

## Session

```
start  host: M-W monitor_s3, M-E; release ATN/CLK/DATA
       drive: assert CLK (a DOS drive never asserts CLK alone)
       host: wait CLK, assert DATA;  drive: release CLK
       host: wait CLK released, release DATA;  drive: wait DATA released -> idle
stop   host sends 'Q'; drive asserts CLK; host asserts DATA;
       drive releases everything and returns to DOS; host releases DATA
```

Monitor commands ('R', 'W', 'J', 'Q') travel as X bytes. Block transfers use
'J' on the `xread`/`xwrite` entries, located after the `NYBX` tag in the binary
(`xparm` = tag + 4: addr, len little endian; `xread` = xparm + 4; `xwrite` =
xparm + 10):

```
read:  W xparm 4 addr len | J xread  -> len data bytes, then A=s1 X=s2 Y
write: W xparm 4 addr len | J xwrite | len data bytes -> A=s1 X=s2 Y
```

`Monitor(cbm, dev, "s3")` drives all of this through `fastx.XLink`, its S3
link: `read`/`write` move at most 4 KiB per block, compare (s1, s2) with
`fastx.xsum` and repeat a block whose check fails; transport errors surface
like S1/S2 (`RECOVERABLE`), and a command the drive takes none of raises
`HandshakeTimeout("drive left the monitor")`, since the X idle bus has no
line that shows the drive is still there. The link sets the adapter's I/O
timeout to the drive's 10 s idle window.

Block check, per byte b: `s1 = s1 + b` (carry c out), `s2 = s2 + s1 + c`,
both mod 256, starting at 0.

## Byte cycle

```
host:  assert DATA (go) ........ (read: release DATA at SYNC+0.125us)
drive:  poll DATA ---> SYNC: assert CLK at t=0 (<=18 cycles after reading go)
drive -> host (read):   P0..P3 written at fixed cycles, then REL (release)
host  -> drive (write): drive releases CLK at t=6, then reads P0..P3
```

The adapter asserts go only when it can take (read) or supply (write) a byte
and has interrupts masked; it then waits for CLK released followed by CLK
asserted. If no SYNC arrives within a slice (65536 polls, about 24.6 ms) it
withdraws go and keeps watching CLK for 32 drive cycles (the longest path from
the drive's go read to its SYNC write is 18 cycles, via `jsr xgo`), so a drive that saw go just before the withdrawal is still
served. Otherwise it services USB/TimerWorker and retries; the firmware I/O
timeout bounds the whole wait.

### Pair encoding

| Slot | read (drive -> host) DATA, CLK | write (host -> drive) DATA, CLK |
|---|---|---|
| P0 | b1, b3 | b0, b2 |
| P1 | b5, b7 | b1, b3 |
| P2 | b0, b2 | b4, b6 |
| P3 | b4, b6 | b5, b7 |

Read pairs come from `b & $AA`, `>> 4`, `(b & $55) << 1`, `>> 4`, which never
set bit 4. Write pairs let the drive rebuild the byte as
`((s1 << 1 | s0) & $0F) | (s3 << 1 | s2) << 4` from raw port reads.

### Drive schedule (drive cycles after the SYNC write)

| | P0 | P1 | P2 | P3 | REL |
|---|---|---|---|---|---|
| read: port write | 13 | 25 | 35 | 47 | >= 57 (57 sendbyte, 71 sendblk) |
| write: port read | 12 | 21 | 34 | 43 | (CLK released at 6) |

Port accesses happen in the last cycle of `sta/lda $1800`; no timed section
contains a page-crossing operand, so every count is exact. The tests trace
these offsets from the assembled code (`test_drive_schedule_matches_timing_table`).

## Adapter offsets and margins

Budgets: released line reads released within R = 1 us (twice the 0.5 us
settling noted in `board-zoomfloppy.h`); SYNC poll loop P = 6 clocks
(0.375 us); input synchroniser S = 1 clock; drive port sampling V = half a
drive cycle. Asserting a line is taken as immediate.

- read sample k (after detection): `((w_k + w_k+1) * cyc + R - P) / 2`,
  slack each side `((w_k+1 - w_k) * cyc - R - P) / 2`
- write change k: `((r_k + r_k+1) * cyc - P - R) / 2 - S`,
  slack `((r_k+1 - r_k) * cyc - 2V - P - R) / 2`; P0 is output at detection
  (slack `(r_0 - 6) * cyc - V - R`); final release at `(r_3 + 4) * cyc`

| | 1 MHz (us) | 1 MHz (clocks) | 2 MHz (us) | 2 MHz (clocks) |
|---|---|---|---|---|
| read samples | 19.31 30.31 41.31 52.31 | 309 485 661 837 | 9.81 15.31 20.81 26.31 | 157 245 333 421 |
| read slack | 5.31 4.31 5.31 4.31 | | 2.31 1.81 2.31 1.81 | |
| write changes | 0.25 15.75 26.75 37.75 47 | 4 252 428 604 752 | 0.25 7.5 13 18.5 23.5 | 4 120 208 296 376 |
| write slack | 4.5 3.31 5.31 3.31 | | 1.75 1.31 2.31 1.31 | |

`python -m nybulah.simx` prints this table from `Timing`; the firmware
computes the same clocks with `X_SAMPLE`/`X_CHANGE` in `x.c`, and
`xum1541/misc/x_timing.py` steps the compiled routines from each detecting
`sbic` to confirm them. Crystal drift (two 100 ppm parts) over one byte is
under 0.02 us. In the co-simulation transfers survive a skew of 0.95x the
smallest slack with R at its budget and V jitter, and fail beyond the largest
slack plus P, 2V and R (`test_margins_*`, `test_violating_margins_corrupts`).

## Adapter command

`XUM1541_READ`/`XUM1541_WRITE` with protocol `XUM1541_X` (12 << 4), flag
`XUM_X_2MHZ` for a 1571 running at 2 MHz. After the data phase (short on error)
the firmware always sends a status block: `XUM1541_IO_READY` or
`XUM1541_IO_ERROR` and the 16-bit count moved. Capability bit
`XUM1541_CAP_X` (0x20) in the INIT reply; the plugin refuses X below firmware
version 9.

## Recovery

| Event | Drive | Adapter | Host |
|---|---|---|---|
| host stops (USB stall, unplug) | WAIT watchdog returns to DOS after 1 s without progress | go is released whenever it is not serving a byte | next session restarts the monitor |
| drive stops | completes nothing, stays in DOS or crashed | slices expire, I/O timeout -> IO_ERROR, lines released | XLink restarts (reset + upload) |
| bit error | check differs | none | XLink repeats the block |
| adapter wedged | watchdog | `XUM1541_ABORT` unwinds, FIFOs reset | plugin `xum1541_resync()` |

## Throughput

Drive cycles per byte in steady state (co-simulation, `test_block_throughput`;
block loops restart the watchdog every byte, and a taken branch that crosses a
page adds a cycle depending on code layout) and the resulting rate:

| Path | cycles/byte | 1 MHz | 2 MHz (1571) |
|---|---|---|---|
| X block read (`sendblk`) | 107 | 9.3 KB/s | 18.7 KB/s |
| X block write (`recvblk`) | 120 | 8.3 KB/s | 16.7 KB/s |
| X monitor 'R'/'W' per byte | 148 / 151 | 6.8 / 6.6 KB/s | |
| S2 monitor (drive-bound only) | 271 / 328 | <= 3.7 KB/s | ATN: single drive only |
| S1 monitor (drive-bound only) | 597 / 671 | <= 1.7 KB/s | |
| M-R (measured) | | 0.46 KB/s | |

S1/S2 figures exclude the adapter's own per-bit delays, so they are upper
bounds. Each 4 KiB block adds about 16 monitor bytes and three USB
round trips (a few ms), under 2 % at 1 MHz. The adapter's USB work happens
between bytes while go is released, so it never stretches a timed window.

## Burst X (firmware v10)

Burst X keeps the X lines, idle state, session handshakes and recovery, but pays
one go/SYNC per burst of up to 64 bytes instead of per byte: after SYNC the
drive runs a branch-free loop with a fixed cycle count per byte and the adapter
follows that schedule open-loop, interrupts masked, until the burst ends.

Sources: `drive/proto_xb.inc` (`monitor_xb.bin`), `nybulah/simx.py`
(`BurstTiming`, `xb_read`/`xb_write`), `nybulah/fastx.py` (`XBLink`);
adapter `xum1541/x.c` (`xb_rx`/`xb_tx`), plugin
`opencbm_plugin_xb[2]_read_n/_write_n` (`XUM1541_X | XUM_X_BURST`, `XUM_X_2MHZ`
for a 1571 at 2 MHz; -1 below firmware 10).

### Bursts

The adapter cuts every transfer into 64-byte bursts from its start (two
32-byte USB packets, one per bank of the double-banked endpoint, switched at a
fixed point of its loop); the drive ends its bursts at 64-byte aligned
addresses. The host therefore moves the head of a block whose address is not
aligned in a transfer of its own. Before a burst the adapter waits until both
IN banks are free (read) or the burst's bytes are buffered (write), so the bus
never waits on USB; it then asserts go, detects SYNC as in v9 (slices, retract,
grace) and runs the burst.

### Drive schedule (drive cycles after the SYNC write; byte i adds i periods)

| | P0 | P1 | P2 | P3 | release | period |
|---|---|---|---|---|---|---|
| send: port write (DATA, CLK) | 14 (b1,b3) | 26 (b5,b7) | 50 (b0,b2) | 62 (b4,b6) | 74 after the last byte | 67 |
| receive: port read (DATA, CLK) | 12 (b5,b7) | 20 (b4,b6) | 30 (b1,b3) | 38 (b0,b2) | CLK released at 6 | 52 |

The receive loop rebuilds `b = r0<<5 ^ r1<<4 ^ r2<<1 ^ r3` from raw port reads
(`lda`/`eor $1800`, three `asl` between r1 and r2, one elsewhere). The device
number inputs PB5/PB6 (K) add the constant `K ^ K<<1`, which `open` patches
into an `eor #` in the loop's free slot. Loops sit in one page each
(`PAGEFIT`), operands are page-aligned or self-modified, so every count is
exact; `test_drive_schedule_matches_timing_table` traces them.

### Adapter offsets (clocks from the detecting poll, byte i adds i periods)

Same budgets and formulas as v9 (`X_SAMPLE`, `X_CHANGE`); P0 of the first
written byte goes out at detection, P0 of each later byte midway between the
previous byte's last read and its first.

| | 1 MHz (16 clocks/cycle) | 2 MHz (8 clocks/cycle) |
|---|---|---|
| read samples, period | 325 613 901 1093, 1072 | 165 309 453 549, 536 |
| write changes (P0 first, P1-P3, next P0, release) | 4 244 388 532 804 672, 832 | 4 116 188 260 396 336, 416 |
| smallest slack, read / write | 5.31 / 2.81 us | 2.31 / 1.06 us |
| with 200 ppm over a 64-byte burst | 4.45 / 2.15 us | 1.88 / 0.73 us |

`python -m nybulah.simx` prints these from `BurstTiming`; `xum1541/misc/x_timing.py`
steps the compiled burst routines (three bytes, bank switch included) against
the same `X_SAMPLE`/`X_CHANGE` values. The co-simulation passes at 0.95x the
smallest slack with R at its budget and drive sampling jitter, and at
+-200 ppm, and fails beyond the largest slack plus P, 2V and R or at the drift
that moves the last byte that far (`tests/test_proto_xb.py`).

### Monitor commands

`monitor_xb.bin` replaces the byte commands with 5-byte command bursts
`addr len op` (little endian) received into zero page `$30-$34`:

```
'R' addr len  -> len bytes from addr, then s1 s2 op
'W' addr len  <- len bytes to addr,   then s1 s2 op
'J' addr      -> jsr addr, then A X Y
'Q'           exit handshake, return to DOS
```

A truncated command burst leaves op 0, which the drive ignores. The check is
the v9 one over command and data, except that the send loop enters each update
with carry = bit 3 of the byte (`fastx.xbsum`); blocks are up to 8 KiB and
repeated on a mismatch. Zero page `$30-$36` is saved and restored.

### Throughput

| Path | cycles/byte | 1 MHz | 2 MHz (1571) |
|---|---|---|---|
| burst read (67 + about 165 per burst) | 69.6 | 14.4 KB/s | 28.8 KB/s |
| burst write (52 + about 165 per burst) | 54.6 | 18.3 KB/s | 36.7 KB/s |

Co-simulation, drive-bound; the same model gives the per-byte X rates within
1 % of the hardware measurements.

### Design constraints

| | send | receive |
|---|---|---|
| loop (cycles) | fetch 4, `tax` 2, P0 6, P1 12, check 14, P2 10, P3 12, loop 7 | 4 port reads 16, shifts 10, `iny` 2, `eor #kk` 2, store 5, check 12, loop 5 |
| per byte | 67 | 52 |

- **Burst length.** USB bounds it, not drift: the ATmega32U2 has 32-byte
  double-banked bulk endpoints (176 bytes of endpoint RAM), so 64 bytes is the
  most the adapter can buffer without stalling the bus. Two ±100 ppm crystals
  move the last byte of a 64-byte burst by at most 0.86 / 0.43 us (read,
  1 / 2 MHz) and 0.66 / 0.33 us (write). The per-burst bookkeeping, watchdog
  restart, go poll and SYNC cost about 165 cycles, 2.6 per byte.
- **No lookup tables.** Pair tables would give a 41-cycle send loop but need
  1 KB: base RAM holds track code (`$0300-$04FF`), the monitor
  (`$0500-$07FF`) and DOS, the expansion the capture buffer. A single
  256-byte table saves 4 cycles but needs the monitor under 503 bytes (it is
  564); nibble tables cost more in index splitting than the shifts they
  replace. The block check stays in the loop (14 cycles, zero page, the
  loop's own carry part of the check); a separate pass would cost 21.
- **Tightest windows.** The write windows between the 8-cycle read pairs;
  widening them to 10 cycles would cost 4 cycles per byte (7 %) for 0.5 us at
  2 MHz.
- **USB.** A block is three plugin calls (command, data, 3-byte reply); with
  8 KiB blocks this is under 1.5 % of a block at 2 MHz.
- **Compatibility.** Firmware v10 is 16406 bytes of flash. A plugin older
  than v10 refuses v10 firmware; the v10 plugin falls back to per-byte X on
  older firmware, and nybulah uses burst X only when the plugin's `xb` entry
  points answer.

## 1571 SRQ fast serial (s4, firmware v11)

s4 moves the burst X commands and blocks over the 1571's 6526 shift register
instead of CLK/DATA pairs. It needs a 1571 (a 1541 has no CIA; `Monitor`
refuses s4 there) and firmware v11; with older firmware `Monitor(..., "s4")`
warns and falls back to s3.

Sources: drive side `drive/proto_srq.inc` (`monitor_s4.bin`, command loop
`drive/burst.inc` shared with burst X), adapter model `nybulah/simsrq.py`
(`SrqTiming`, `SimSRQ`), host link `fastx.SrqLink`; adapter `xum1541/x.c`
(`srq_rx`/`srq_tx`), schedule `xum1541/x_timing.h`, plugin
`opencbm_plugin_srq[2]_read_n/_write_n` (`XUM1541_X | XUM_X_SRQ`, capability
`XUM1541_CAP_SRQ`, -1 below firmware 11).

### Hardware

- Memory map (1571 service manual PN-314002-04, Oct 1986, p. 3): VIA1 (U9)
  `$1800`, WD1770 `$2000`, 6526 CIA (U20) `$4000-$7FFF`. Only the base
  registers are used: timer A `$4004/5`, SDR `$400C`, ICR `$400D`, CRA `$400E`.
- Bus drivers (same manual, schematic p. 19): CIA CNT and SP reach the bus
  through a 74LS241 (U19) and a 7407 open-collector buffer (U14) onto SRQ
  ("FAST CLK", DIN pin 1) and DATA; the 241's enables come from VIA1 PA1
  ("SER DIR"). The 1571 DOS source (310654-05 `var.src`) names `$180F` bits:
  PA1 fast serial direction (1 = out), PA2 side, PA5 1/2 MHz, PA6 ATN out.
  Its `spout` sets PA1 before CRA bit 6 and `spinp` clears CRA bit 6 before
  PA1, so CNT/SP never drive the bus in input mode; proto_srq.inc keeps that
  order. Levels are non-inverting: SP = 1 leaves DATA released, CNT low pulls
  SRQ (as the xum1541's `iec_srq_read/write` read and drive them).
- 6526 (MOS datasheet, serial port, ICR and CRA): in output mode timer A
  clocks the port, one bit per two underflows, at most phi2/4; an SDR byte
  loads into the shift register at the next CNT pulse and goes out MSB
  first, each bit valid from a falling CNT edge to the next; after 8 pulses
  ICR bit 3 is set, CNT returns high and SP holds the last bit; a byte
  written before that interrupt follows without a gap. In input mode SP is
  shifted in on each rising CNT and the 8th fills SDR and sets the same
  flag. CRA bit 4 force-loads the latch into the counter, which is how the
  drive reads the write-only latch to restore it.
- Idle drives: DOS enters its bus code only on an ATN interrupt (VIA1 CA1,
  `irq.src`/`irq1571.src`), so DATA or CLK activity without ATN is ignored,
  as X already relies on. A 1541 has no CIA and nothing on SRQ. An idle
  1571, however, keeps its CIA in input mode, and where its ROM enables the
  SP interrupt (`spinp` in 310654-05, ICR mask `$88`) every 8 SRQ rises it
  sees make `irq1571.src` set the "fast host" flag (`fastsr` bit 6), which
  DOS clears only on UNLISTEN/UNTALK; an idle 1581 does the same (`irq.src`,
  `sieee.src`) and while the flag is set answers a TALK over its shift register.
  `Monitor.stop` therefore sends UNLISTEN after every s4 session; drives under s4
  clear their own ICR before returning to DOS.

### Session and commands

Open/close and the 5-byte command bursts are those of burst X
([Burst X](#burst-x-firmware-v10)); the block check is `fastx.xsum` over
command and data in both directions. On entry the drive saves CRA, the timer A
latch and VIA1 port A, stops timer A, sets latch 1 and runs it in input mode;
every exit (`Q`, watchdog, host gone) restores them, PA1 and PA5 included, and
reads ICR so DOS sees no stale serial byte.

### Drive -> host

| step | drive | adapter |
|---|---|---|
| go | output mode (PA1, then CRA bit 6); polls CLK | asserts CLK with interrupts masked |
| byte 0 | writes SDR; CNT falls at the next underflow plus the 6526's start delay | sees SRQ released then asserted (6-clock poll, slices, grace as X), releases CLK |
| byte i | writes SDR 40 cycles (`SR_PERIOD`) after byte i-1, open loop | first poll for SRQ high, then low (5 clocks each), bounded |
| end | clears ICR 4 cycles after the last write, waits for its flag, input mode before the reply | |

A byte written to SDR before the shifter has finished follows the previous
one without a gap, which the adapter cannot frame, so the period must exceed
the time F from a write to the end of its byte. The 6526 datasheet does not
give F; the CIA model puts it at 31 + d cycles (an underflow, the start delay
d, 30 to the 8th rise). On a 1571 32 < F <= 39 (`drive/ciaprobe.s` measures
the ICR flag 34 cycles after the write on both timer phases), and
`SR_PERIOD` = 40. With an even period
every byte starts on the same timer phase, so the gap from a byte's last rise
to the next first fall is `SR_PERIOD` - 30 = 10 cycles whatever d is, inside
the adapter's [7, 14]. The adapter times every byte from its own first fall,
so drift does not accumulate. `drive/ciaprobe.s` measures F to the cycle per
timer phase (`tools/xprobe.py --cia`); `tests/test_proto_srq.py` runs every
d the bound allows and shows the next d failing the block check.

Adapter clocks from the detecting poll (`SRQ_SAMPLE`, `SRQ_START`,
`SRQ_POLLS`):

| | 1 MHz (f = 16) | 2 MHz (f = 8) |
|---|---|---|
| DATA samples, bits 7..0 | 37 101 165 229 293 357 421 485 | 21 53 85 117 149 181 213 245 |
| window per bit | [64j + 16, 64j + 64) | [32j + 16, 32j + 32) |
| next byte's first poll | 541, window [496, 592) | 273, window [256, 296) |
| polls before giving up | 137 | 69 |
| smallest slack | 1.31 us | 0.31 us |

### Host -> drive

| step | drive | adapter |
|---|---|---|
| go | input mode, ICR read (no stale flag), polls DATA | asserts DATA |
| SYNC | CLK asserted t=0, released t=6 | detects as burst X |
| bits | CIA shifts DATA in on each SRQ rise | 8 bits MSB first: SRQ asserted with DATA = bit, released after `SRQ_LOW`, next bit `SRQ_BIT` later |
| bytes | polls ICR every 11 cycles (39 once behind), reads SDR 7 after the hit; leaves for DOS after 255 empty polls | next byte at 8 `SRQ_BIT`, from the USB FIFO between bits |

A bit needs DATA settled before the CIA can sample SRQ high (SRQ low >= R)
and held until it certainly has (SRQ high >= R + one drive cycle of sampling):
2R + f clocks. The schedule adds sigma, the tightest 2 MHz read slack
(5 clocks), to both sides, and the byte period must cover the drive's 39-cycle
loop plus a rise and sigma:

| | 1 MHz | 2 MHz |
|---|---|---|
| bit, SRQ low / high (clocks) | 83, 33 / 50 | 50, 21 / 29 |
| byte period | 664 clocks, 41.5 us (drive loop) | 400 clocks, 25 us (bit timing) |
| set-up / hold / loop slack | 1.06 / 1.13 / 0.5 us | 0.31 / 0.31 / 4.0 us |

`misc/x_timing.py` steps the compiled `srq_rx`/`srq_tx` (samples, next-byte
poll, every port write over three bytes and a bank switch) against these
formulas, and `misc/srq_timing_test.c` checks every window on the host.
`python -m nybulah.simsrq` prints the same table.

### Margins in co-simulation

`tests/test_proto_srq.py` runs the firmware's clocks against the timed 1571
(R = 1 us, CIA start delays of 0 and 3 cycles, idle 1541 peers): reads pass
at 0.95x the smallest slack either way and fail beyond the largest slack plus
P, R and a cycle; writes pass with DATA moved 0.95x the set-up or hold slack
against SRQ and fail beyond it plus 2R and a cycle; both pass at +-200 ppm.
A flipped bit is caught by the check and the block repeated; a host that
vanishes leaves the drive in DOS within the watchdog, one that stops
mid-burst within 255 polls; a drive that stops leaves the adapter with an
error and the bytes so far, lines released.

### Throughput

| path | cycles/byte | 1 MHz | 2 MHz | measured 1 / 2 MHz |
|---|---|---|---|---|
| s4 read (40 + per burst) | 42.4 | 23.6 KB/s | 47.2 KB/s | 23.4 / 46.4 KB/s |
| s4 write (bit timing or drive loop) | 44.6 / 53 | 22.4 KB/s | 37.7 KB/s | 22.3 / 37.1 KB/s |

Co-simulation of 8 KiB blocks, drive-bound as for burst X (`tools/srq_rate.py`
prints the read column per CIA start delay); measured on hardware with
firmware v12 ([hardware.md](hardware.md#expected-results)). Writes are
bounded by the line release budget (2R per bit) at 2 MHz and gain little
there.

### Read time on the host clock

A checked block of N bytes takes C + N t on the host: t is the drive's period
plus its per-burst overhead plus the adapter's per-burst wait, C the command
burst, the reply and their USB round trips. The adapter waits about 22 us per
burst (0.35 us per byte): before a burst both IN banks must be empty, so the
previous burst's second 32-byte packet, about 30 us of full-speed bus time,
has to reach the host first, partly overlapping the adapter's turnaround.
These terms predict 46.2 KB/s at 2 MHz and 23.3 KB/s at 1 MHz for 8 KiB
blocks; `tools/xprobe.py --sweep --fast` measures 21.40 us per byte and
1.08 ms per block at 2 MHz. `bench.sweep` fitted to the adapter's clock in
co-simulation gives the drive-bound t (`test_sweep_fits_the_drive_bound_rate`).

## 1571 streaming capture (firmware v12)

A stream reads a track continuously for whole revolutions with no expansion
RAM: every byte the drive latches goes out through the CIA shift register as
it arrives and the adapter forwards it to USB with no handshake. Only a 1571
at 2 MHz under s4 streams; `Nibbler(stream=None)` picks it when the adapter
reports `XUM1541_CAP_STREAM`, and D64/D71 reads then use it. Firmware v12
keeps the v9-v11 commands.

Sources: drive `drive/stream.s` (`stream_1571.bin`, loaded at `$0300` over
`seek_1571.bin`, the `SEEK` build of `track.s`, whose prep placed the head),
host `Nibbler.stream`, decoding `nybulah/stream.py`, adapter model
`SimSRQ.srq2_stream`; adapter `srq_stream_loop`/`srq_stream8` in
`xum1541/x.c`, plugin `opencbm_plugin_srq2_stream` (asynchronous IN
transfers, `lib/plugin/xum1541/stream.c`). The head moves only through
`Nibbler.seek`, `locate` and `home`.

### Bytes on the wire

The adapter samples CLK at each byte's bit 7: released is a data byte,
asserted a metadata byte. Metadata is never `$00`:

| value | name | meaning |
|---|---|---|
| `%tttttt01` | SYNC_START | t = T2 bits 7-2 when SYNC was seen low |
| `%tttttt10` | SYNC_CONT | inside a sync, timestamps under 256 cycles apart |
| `%tttttt11` | SYNC_END | t when SYNC was seen high; ends the oldest open sync |
| `$04` / `$08` | START / INDEX | stream start; rising index edge |
| `$40` / `$44` / `$48` | END / END_NOINDEX / END_ATN | how the drive stopped |

USB carries a data byte as itself, data `$00` as `ESC $00`, metadata m as
`ESC m`, and ends with `ESC` and an adapter code: `$80` done (END seen), `$84`
overrun (both IN banks full when a byte arrived), `$88` framing (a byte
began before SRQ rose), `$8C` timeout (no SRQ fall within 20 ms), `$90`
truncated (requested length reached); a short packet or ZLP ends the
transfer, with no status block. The requested length counts 64-byte units.
On any code but done the adapter holds ATN at least 8622 us (256 bytes at
32 us, at 285 rpm: the drive's longest interval between ATN checks), then
until SRQ has been quiet for 1 ms, at most 50 ms; the drive sends END_ATN,
restores the CIA (CRA, timer A latch), PA1, PA5 and clears ICR, as on every
exit. The drive refuses with `ST_SLOW` at 1 MHz and `ST_NOGO` when the host
never asserts CLK; `Nibbler` refuses a 1541 or older firmware.

### Drive rules

t = 0 at an SDR write; the shifter needs 32 cycles per byte and the ICR flag
shows within 39 (see s4 above), so:

- a write follows the previous one by 40 or more cycles, or follows the ICR
  flag;
- a waiting byte is read at the first V sample after a write and held in X;
  X and the VIA latch are the only buffers;
- data goes first: metadata waits for an idle shifter and no waiting byte,
  except SYNC_START and SYNC_CONT, which no byte can be waiting behind; a
  metadata slot is given up when V appears by t = 22 (24 after a timed
  write) and the byte goes out at 41 (45);
- CLK changes 14 or more cycles after a write and 2 or more before the next:
  the adapter samples it 4 to 14 cycles after a write;
- inside a sync only SYNC_CONT is sent; metadata due then waits for the
  bytes after the sync, so timestamps unwrap and SYNC_ENDs keep their order.

### Drive schedule (cycles after the write)

| path | entered | V samples | write of the next byte | otherwise |
|---|---|---|---|---|
| `nw` idle poll | 28+ | 0 and 6 of 15, SYNC at 5 | 12 after the `bvs` | ATN, T1 every 256 polls |
| `pwo` after a write from `nw` | 0 | | (due metadata at 40) | `nw` at 29 |
| `pwm` metadata slot | 12 | 12, 18, 22 | 41 / 45 (`pv`) | metadata at 40 |
| `pwb` after a timed write | 1 | 1, 28 | 43 | `nw` at 33 |
| `mpw` after metadata | 4 | 4, 28 | 46 / 43 | `nw` at 33 |
| `ee` byte already waiting | 1-24 | | 42 after the read | |
| sync loop `sp` | | SYNC every 11 | | ATN, T1, index, SYNC_CONT |
| `se` sync end | 7-8 after the read | each poll until ICR | at the ICR flag | |

### Margins

The shortest byte period is zone 3 at 310 rpm, 52 x 300 / 310 = 50.3 cycles.

| quantity | worst case | budget | margin |
|---|---|---|---|
| write spacing | 40 (`pwm`), 43 (`tv`) | ICR flag at 39 or less | 1 cycle |
| byte ready to read | 21 (V at 23, read at `mpw` 4) | 50.3 | 29 cycles |
| back-to-back writes | 43 | 50.3 | backlog drains 7.3 cycles per byte |
| adapter frame | 264-296 clocks | SRQ_FRAME 256 | 8 clocks |
| adapter next poll | 303 clocks | SRQ_WAIT 306 | 3 clocks |

A metadata byte goes out only when nothing is waiting, so it delays at most
the byte that lands during its 40-cycle slot (the slot is abandoned up to t =
22), and that delay drains by the next metadata slot. INDEX lands within 4
bytes of the edge. `misc/x_timing.py` steps the compiled `srq_stream8` (6-clock
fall wait) against these bounds and `misc/srq_timing_test.c` checks them.

### Host decoding

`Stream.syncs(cell)` pairs SYNC_START/SYNC_END first in first out, adds
SYNC_CONT spans and bounds each sync's low time: the SYNC read that saw the
change takes 12 cycles to its T2 read at the start and 7-8 at the end, a
SYNC read waits at most 125 cycles at the start (data paths) and 70 at the
end (sync loop), timestamps lose 3 bits, and the start lag follows the
latched trailing ones (`start_lag`). Multi-revolution captures carry their
index positions, which `index_bits`, `passes.stream_syncs`, the cycle finder
and the disk map use as for RAM captures.

### Tests

`tests/test_stream.py` streams every zone at 300 and 310 rpm on the timed
1571 with the adapter model: every latched byte arrives in order, every sync
lies inside its bounds, writes are 40 or more cycles apart, and no stream read
hits either stop (`Mechanism.bumps`, `inner_stops`). A throttled host drain
gives overrun, a silent drive a timeout with ATN, and multi-revolution streams
merge on their index edges.

## 1581 (monitor_*_1581, firmware v10-v12 unchanged)

The 1581 speaks the same transports at 2 MHz with the adapter's 2 MHz timings (`x2`
is not built: burst X `xb2`, `srq2`, `srq2_stream`), so no firmware change is
needed. Its serial bus sits on the 8520 CIA's port B at `$4001` with the 1541's bit
values (PB0 DATA in, PB1 DATA out, PB2 CLK in, PB3 CLK out, PB4 ATN acknowledge,
PB7 ATN in), and the shift register reaches SRQ/DATA through a 74LS241 and 7407s
turned by PB5 (1581 service manual PN-314982-01, schematic 252380 sheet 3; DOS
`iodef.src`). Every timed loop keeps its instruction sequence, so the drive
schedules above hold cycle for cycle.

Sources: `drive/monitor.s` and the `proto_*.inc` files built with `-D M1581=1`
(`monitor_s1_1581`, `monitor_s2_1581`, `monitor_xb_1581`, `monitor_s4_1581`),
`drive/ciaprobe.s` (`ciaprobe_1581`), host `Monitor` (model from `cbm_identify`, type
3), `fastx.XBLink`/`SrqLink`.

| | 1571 | 1581 |
|---|---|---|
| IEC port | VIA1 PB `$1800` | CIA PB `$4001` |
| ATN acknowledge | XOR: pulls DATA unless PB4 = ATN in | NAND (U7): pulls DATA iff PB4 = 1 and ATN asserted |
| S2's ATNA under ATN | PB4 = 1 | PB4 = 0 (`ACK_HELD`) |
| fast serial drivers | VIA1 PA1 | CIA PB5 |
| entering output mode | CRA bit 6 | CRA out, in, out (DOS `patch.src` spout_patch) |
| watchdog | VIA1 T1 free-run, IFR bit 6 | CIA timer B counting timer A underflows, ICR bit 7 |
| clock | 1 or 2 MHz (PA5) | 2 MHz (16 MHz / 8, U10) |

Timer A runs at latch 1 (an underflow every 2 cycles) for the whole session; timer B
counts its underflows from `$FFFF`, so it falls by one every microsecond and wraps
every 65.536 ms. The watchdog takes 16 wraps (1.05 s) inside a command and 152
(9.96 s) between commands. WAIT polls `bit $400D / bpl`: ICR bit 7 is set by any
source in the mask, which the DOS sets to FLAG, SP and timer B (`dskint.src` `$9A`)
and the monitor ensures holds timer B (`$82`); an ATN edge or a shift register byte
during a WAIT therefore also counts a wrap, shortening the window by one sixteenth.
Entry saves CRA, CRB and both timer latches (stopped and force-loaded counters read
back); every exit puts the shift register in input mode before the drivers turn in,
restores the timers and reads ICR.

The per-byte X monitor does not fit beside the CIA code (`$0500-$07FF`); on a 1581
`s3` is burst X (firmware v10). The monitor is at most `$0290` bytes, so the 1581
drive code can use `$0782-$09FF` as well as `$0300-$04FF`.

`ciaprobe_1581` measures the 8520's SDR-write-to-ICR-flag latency exactly as on the
1571 (`tools/xprobe.py --cia`); the 40-cycle send period needs it at 39 or less, as
the 1571's 6526 shows (34).

## 1581 capture and streaming (drive/mfm.s, drive/mfmstream.s)

The WD177x at `$6000-$6003` runs from 8 MHz, exactly four of its clocks per CPU
cycle (both divided from Y1 by U10). DRQ and INTRQ are not wired to the CPU; the code
polls the status register (bit 0 BUSY; bit 1 DRQ, or in the type I status after a
force interrupt, the live index pulse; bit 2 TR00 in type I status). Index and TR00
reach the CPU only through that status. Every instruction that touches the WD sits
at an address whose low two bits are not 00, as the DOS places its own
(`mfmmacro.src` WDTEST); the assembler checks it. After a command write the code
waits 67 cycles before reading status (datasheet: BUSY valid after 24 us, the other
bits after 32 us, a new command 16 us after a force interrupt).

### Homing and seeks

`restore` issues Restore with h = 1 and the 12 ms rate (r1r0 = 01: 12 ms on both the
WD1770 and the WD1772). The WD pulses STEP at 0, T, 2T, ... and samples TR00 T after
each pulse, so the c-th pulse comes at (c - 1)T and the (c + 1)-th at cT. The drive
force-interrupts at (c - 1/2)T after its timestamp just after the command (the
timestamp is at most 60 us late), between the two, and then reads the type I status:
TR00 sensed means the track register is set to 0; otherwise the call fails with
`$80 | status`. c comes from the host (`Mfm1581.estimate`: a good ID's C, else the WD
track register through which the DOS seeks, at most 80) and is never exceeded: a
head further out than c stops short of TR00 and errs; nothing steps blindly.
Seek moves from the homed track register to 0..80 (DOS formats 0..79; Wheels writes
cylinder 80).

### Stream engine

`mfmstream_1581` runs a list of up to seven entries (`op trk sec rep`): Read
Address, Read Sector or Read Track, or an index edge wait, each up to 127 times
(Read Sector optionally advancing the sector). Every byte the WD delivers goes
straight to the shift register; metadata (CLK asserted) carries a record per command
(`$0C`: two stamps, the WD status, a timeout flag and the byte count, packed into
six-bit chunks `%dddddd01`), index stamps (`$1C`), keepalives (`$14`, every 256
passes of a wait loop: before a command's first DRQ, for an index edge, and for
busy to clear after a force interrupt; the passes are counted, not timed by timer
B, and 256 of the longest pass fit in the adapter's 20 ms wait for the next byte) and the v12
END family. `nybulah.mfmstream.MfmStream` parses it.

Cycle tables (t = 0 at an SDR write, from the code):

| path | status reads | data register read | next SDR write |
|---|---|---|---|
| `dl` data loop | 24 (28 with a count carry), then every 27 (ATN on each pass) | 37 | 41 (45) |
| `w0` before the first DRQ | every 30 or less | 11 or less after the read that sees DRQ | 11 after the read |
| `send` (one metadata byte, w0) | -14, 4, 28 | 13 (DRQ at 4), 37 (at 28) | metadata at 0, data at 40 or 48 |
| `plain` (WD idle) | | | 40 |

Bounds (the shifter flags a byte within 39 cycles; a byte waits in the data
register for at most 64 cycles before the next one overwrites it, 32 us at 250
kbit/s):

| quantity | worst case | budget | margin |
|---|---|---|---|
| SDR write spacing | 40 (`send` s4 path), 41 (`dl`) | > 39 | 1 cycle |
| byte waiting in `dl` | 40 (27 + 13) | 64 | 24 |
| first byte, from `w0` | read 41 after arrival; second byte read 106 after the first's arrival | 128 (third arrival) | 22 |
| first byte during a metadata write | arrival after -14; second byte read at 94 | > 114 | 20 |
| backlog | writes 40 apart against bytes 64 apart | | drains 24 cycles a byte |
| CLK for metadata | asserted at -4, released at 16 to 24 | adapter samples 4-14 | 2 cycles |
| ATN seen | every pass of `dl` and `w0` | adapter holds ATN 8622 us | |

A stamp reads ICR 4 cycles and timer B low 22 cycles after its reference (the
first byte's SDR write, the status read that saw the command end, or the one that
saw the index): its microsecond time is the wrap count, plus one when ICR showed a
wrap, plus one when it did not and the elapsed count is at most 9 (a wrap between
the two reads). Every other ICR read is at least 32 cycles before a stamp's, so that
case cannot come from a wrap already counted.

Every read loop takes a pending DRQ before BUSY: Read Address and Read Sector clear
BUSY with their last byte still in the data register.

Read Track starts at the leading edge of an index pulse and ends at the next
(datasheet); the engine reissues it as soon as BUSY clears, and the records' t_first
show whether the WD caught the next edge or waited a revolution. A command that
outlasts TMO wraps (six revolutions at the measured period: the WD gives up Read
Sector and Read Address after five) is force-interrupted and ends the stream with
END_TIMEOUT; an index wait that outlasts it ends the same way.

J returns A = the END code (or `ST_NOGO` $FF when the host never asserted CLK),
X = the entries started (commands issued plus index waits) and Y = the WD status.
A stream that is not `done`/`done` carries a diagnosis in its capture meta
(`MfmStream.diagnosis`): the reply decoded, the metadata codes and data bytes the
adapter delivered whether or not a record framed them, the adapter output size and
how long the receive took (the adapter's 20 ms gap timeout against its I/O timeout
for no first fall). `streamprobe` stops after such a Read Track stream. A reply that is neither an END
code nor `ST_NOGO` was read from a stream still running: `StreamLost` ends the
session with no further transfer and the monitor is recovered by a bus reset.

The last 20 bytes of the first block (`$04EC-$04FF`, `r1581.STATE_AT`) hold the
stream's state: entries started, the entry and repeats left, and the last record
(both stamps, WD status, flags, data bytes). An incomplete stream reads it through
the monitor; an out-of-step one, after the drive's longest remaining stream and its
monitor's watchdog, over DOS M-R before the recovery's bus reset, which the ROM's
RAM test (dskint.src) would overwrite. It appears as `diagnosis.drive_state`.

### Without streaming

`mfm_1581` reads one ID or one sector into the DOS track cache (`$0C00-$1FFF`) and
writes there from: Write Track from a run-length image (token 0 ends, 1-127 repeat
the next byte, 128-255 copy t - 127 bytes; the last byte repeats until the WD stops
at the index) and runs of Write Sector, back to back. Each byte is loaded into X
before its DRQ wait, so it is written within one status poll of the DRQ; a token is
read between runs, inside the byte time the WD shifts the last one. The first byte
is ready before the Write Track command goes out (the WD gives up unless it is
written within three of its byte clocks). Read loops read the data register within
one poll plus 13 cycles. Write commands carry the DOS's precompensation
bit (`msub.src` precmp: set from cylinder 44 on). Afterwards the host runs DOS job
`$82` (controller reset: cache invalidated, no head movement) through the job queue
at `$0002`, and waits 0.32 s before a session that writes the cache (DOS writes a
dirty cache back after 32 controller ticks of bus silence, `idle.src`).
