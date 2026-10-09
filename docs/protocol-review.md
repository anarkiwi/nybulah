# X transport review

Where v9 X spends its drive cycles, which of the candidate speed-ups pay off
under the drive's RAM and the ZoomFloppy's USB limits, and the burst X design
(firmware v10) that follows. Cycle counts are drive cycles; rates are
drive-bound co-simulation figures (`nybulah.simx`), which reproduce the v9
hardware measurements within 1 %.

## v9 cost per byte

| | read (`sendblk`) | write (`recvblk`) |
|---|---|---|
| watchdog restart, go poll, SYNC, release | 8 + 9 + 6 + 6 | 8 + 9 + 6 + 6 |
| fetch / store | 5 + 2 | 6 |
| pairs: encode + 4 port writes / 4 reads + decode | 40 | 56 |
| block check (`clc`, absolute `s2`) | 18 | 16 |
| index and 16-bit count | 13 | 13 |
| total (drive code) | 107 | 120 |

29 cycles per byte are synchronisation and watchdog, 13 bookkeeping.

## Candidates

1. **One go/SYNC per burst.** Adopted. The burst length is bounded by USB, not
   drift: the ATmega32U2 has 32-byte double-banked bulk endpoints (176 bytes of
   endpoint RAM), so 64 bytes is the most the adapter can buffer without
   stalling the bus. Two +-100 ppm crystals shift the last byte of a 64-byte
   burst by at most 0.86 us (read, 1 MHz) / 0.43 us (read, 2 MHz) /
   0.66 / 0.33 us (write), against slacks of 5.31 / 2.31 / 2.81 / 1.06 us.
   Per burst the drive spends about 165 cycles (block bookkeeping, watchdog
   restart, go poll, SYNC): 2.6 cycles per byte.
2. **Fewer cycles per byte.** Lookup tables for the four pair values would give
   a 41-cycle read loop, but need 1 KB: base RAM is taken by track code
   (`$0300-$04FF`), the monitor (`$0500-$07FF`) and DOS, the expansion by the
   capture buffer (31 pages + sync table). A single page-aligned 256-byte table
   saves 4 cycles but needs the monitor under 503 bytes (it is 564). Nibble
   tables cost more in index splitting (>= 6 cycles per nibble) than the shifts
   they replace. Without tables, two chains (`and #$AA`, `lsr` x4 and
   `and #$55`, `asl`, `lsr` x4) are the shortest: one shift between two port
   values cannot both bring two new bits to PB1/PB3 and keep PB4 (ATNA) clear.
   The watchdog restart moves to the burst; operands are self-modified and
   page-aligned, loops page-fitted. The block check stays in the loop
   (14 cycles, zero page, no `clc`: the loop's own carry is part of the
   check); a separate pass would cost 21.
3. **Write.** The drive rebuilds the byte from raw port reads,
   `b = r0<<5 ^ r1<<4 ^ r2<<1 ^ r3` with `lda`/`eor $1800` (26 cycles instead
   of 43). The device-number inputs add a constant that an `eor #` in the
   loop's otherwise idle slot cancels.
4. **1571 CIA shift register on SRQ/DATA.** Not implemented. The SR shifts at
   up to phi2/4, 16 us per byte at 2 MHz, with about 29 CPU cycles per byte
   for fetch, `sta $400C`, check and loop: about 60 KB/s each way, roughly 2x
   burst X at 2 MHz. It is ATN-free and 1541s ignore SRQ, but it only helps
   the 1571, needs the fast-serial direction switch (VIA1 PA1) and a CIA SR
   model in the simulator before it can be verified; worth a second step.
5. **USB.** A block is three plugin calls (command, data, 3-byte reply), each a
   command packet, data and status block. With 8 KiB blocks (v9: 4 KiB plus
   16 monitor bytes) this is under 1.5 % of a block at 2 MHz. Merging command
   and data into one transfer would put the drive's address decode inside a
   timed burst; not worth it.

## Burst X

| | read | write |
|---|---|---|
| loop | fetch 4, `tax` 2, P0 6, P1 12, check 14, P2 10, P3 12, loop 7 | 4 port reads 16, shifts 10, `iny` 2, `eor #kk` 2, store 5, check 12, loop 5 |
| cycles per byte | 67 | 52 |
| with per-burst cost | 69.6 | 54.6 |
| 1 MHz | 14.4 KB/s (v9 9.1 measured) | 18.3 KB/s (v9 8.3) |
| 2 MHz, 1571 | 28.8 KB/s (v9 18.2) | 36.7 KB/s (v9 16.5) |

Slack each side (us) of the tightest window, R = 1 us release, P = 0.375 us
poll, drive sampling +-cyc/2 for writes:

| | read 1 MHz | read 2 MHz | write 1 MHz | write 2 MHz |
|---|---|---|---|---|
| v9 X | 4.31 | 1.81 | 3.31 | 1.31 |
| burst X | 5.31 | 2.31 | 2.81 | 1.06 |
| burst X, 200 ppm, last byte of 64 | 4.45 | 1.88 | 2.15 | 0.73 |

The write windows between the 8-cycle read pairs are the tightest; widening
them to 10 cycles costs 4 cycles per byte (7 %) for 0.5 us at 2 MHz.

Firmware v10 is 16406 bytes of flash (v9: 15050). An old plugin refuses v10
firmware (its version check); the v10 plugin falls back to v9 X on older
firmware, and nybulah uses burst X only when the plugin's `xb` entry points
answer.

## Measure on hardware

- `hwcheck --devs 8 10 --proto s3` and `bench --dev 8 --protocol s3 --fast`:
  rates against the table above, `rejects` (block retries) 0.
- Long runs (for example 100 x 8 KiB each way per drive and clock) for the
  error rate; a non-zero `rejects` count with good data points at margins.
