# X transport: drive-timed two-bit IEC transfers

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
Design and cycle budgets: [protocol-review.md](protocol-review.md).

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

Co-simulation, drive-bound; the same model gives the v9 rates within 1 % of
the hardware measurements.

## 1571 SRQ fast serial

Not implemented; see [protocol-review.md](protocol-review.md) for the
estimate (about 2x burst X on a 1571 at 2 MHz) and what it needs.
