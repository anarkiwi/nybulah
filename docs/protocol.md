# X transport: drive-timed two-bit IEC transfers

X moves bytes between a ZoomFloppy (xum1541 firmware v9+) and drive code in a
1541/1571 over CLK and DATA only. ATN is never touched, so DOS drives that
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

`fastx.XLink` sends at most 4 KiB per block, compares (s1, s2) with its own
`xsum` and repeats a block whose check fails; a transport error abandons the
session (drive watchdog or reset) and restarts it.

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

Drive cycles per byte in steady state (co-simulation, `test_block_throughput`)
and the resulting rate:

| Path | cycles/byte | 1 MHz | 2 MHz (1571) |
|---|---|---|---|
| X block read (`sendblk`) | 100 | 10.0 KB/s | 20 KB/s |
| X block write (`recvblk`) | 112 | 8.9 KB/s | 17.9 KB/s |
| X monitor 'R'/'W' per byte | 148 / 151 | 6.8 / 6.6 KB/s | |
| S2 monitor (drive-bound only) | 271 / 328 | <= 3.7 KB/s | ATN: single drive only |
| S1 monitor (drive-bound only) | 597 / 671 | <= 1.7 KB/s | |
| M-R (measured) | | 0.46 KB/s | |

S1/S2 figures exclude the adapter's own per-bit delays, so they are upper
bounds. Each 4 KiB block adds about 16 monitor bytes and three USB
round trips (a few ms), under 2 % at 1 MHz. The adapter's USB work happens
between bytes while go is released, so it never stretches a timed window.

## 1571 SRQ fast serial

The 1571's CIA shift register clocks a byte out on DATA with SRQ as the bit
clock at up to phi2/4 (500 kbit/s at 2 MHz), with no per-bit drive code; the
ZoomFloppy already samples SRQ edges (`iec_srq_read`). That is roughly 5x X
at 2 MHz and also ATN-free, so idle drives on the bus are unaffected (1541s
ignore SRQ). It needs the 1571 in fast-serial mode and its own drive routine,
and only helps the 1571; X stays the common path. Both are separate from
track capture, so a capture routine that starts at the index pulse can hand
its buffer to either transport afterwards.
