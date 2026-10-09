# Disk reading and writing

```sh
nybulah read  --dev 10 disk.d64            # 35 tracks (--tracks 40), error bytes kept
nybulah read  --dev 8  disk.d71            # 1571, both sides
nybulah write --dev 8  disk.d71            # format, write, verify every track
nybulah read  --dev 10 disk.d64 --transport s3 --retries 4 --archive caps/
```

The image type comes from the file suffix. D71 needs a 1571. `--archive DIR`
saves every capture (reads, write probes and verifies) as a `.npz` record.
`nybulah.nibbler.Capture.load` reads a record back, and every image can be
re-derived from the records. `write` exits with an error if a track never
verified.

## Layers

| Layer | Module | Role |
|---|---|---|
| Drive | `drive/track.s` | Step, select motor, density and side, capture or write raw bytes, time syncs |
| Nibbler | `nybulah.nibbler` | Talks to `track.s` through any `Monitor` (s1, s2, s3). Returns `Capture` records |
| Passes | `nybulah.passes` | Host model of the capture passes: sample windows, anchors, merge |
| Disk | `nybulah.disk` | Track jobs: decodes with `nybulah.analysis`, formats tracks, verifies |
| Simulator | `nybulah.sim`, `simdisk`, `simfast`, `simhost` | py65 drive model with media, stepper, VIA2 and WD1770 index; its compiled mirror; host stand-ins |

## Drive routines

`track.s` is assembled once per model (`track_1541.bin`, `track_1571.bin`):
512 bytes linked at `$0300`, then a page (`PASS`) that runs from expansion RAM
just after the buffer. Its parameters and results live in zero page
`$60-$80`. The host saves that range, PCR and (on the 1571) VIA1 port A when
it opens a session, and restores them on close. On a 1571 it also selects
1 MHz.

| | 1541 | 1571 |
|---|---|---|
| Buffer (31 pages) | `$8000-$9EFF` | `$6000-$7EFF` |
| `PASS` code (1 page, holds **write**) | `$9F00` | `$7F00` |

- **prep** sets the motor, LED and density bits. On a 1571 it also sets the
  side bit (VIA1 PA2). It steps a signed number of halftracks with a delay per
  step, then waits a settle time.
- **read** runs one capture pass of 31 pages. Start modes are `now`, `sync`
  (the pass starts inside a sync, so its first byte follows it), `index`
  (1571: at the second of two index edges, which also give the revolution
  period) and `anchor` (after a given run of up to 8 bytes).
- **write** checks write protect, can start at index, and then streams the
  buffer. It waits until the last byte has shifted out, then returns to read
  mode.

Every wait is bounded by a counter: a stopped byte ready ends a read with
`ST_TIMEOUT` (`ST_KILLER` if SYNC is held) and a write with `ST_TIMEOUT`,
after which the drive is back in read mode with SOE off. Every exit turns SOE
off, so byte ready cannot disturb the monitor's V-flag polling.

## Capture passes

A sync hides ones the hardware never latches: SYNC holds the bit counter
from the tenth one, so the byte after a sync arrives late by exactly the
cells that byte ready skipped. `Nibbler.capture` therefore reads a track in
up to three passes and merges them (`nybulah.passes`) into one `Capture`.

| Pass | Stores per byte or sync | Purpose |
|---|---|---|
| BITS | the latched byte | the data, never losing a byte |
| TB | VIA1 T2 low byte at each byte ready | sync positions and hidden ones, byte period |
| TS | per SYNC: bytes counted so far, release-wait length | positions, and the length of long syncs (TB's 8-bit timer wraps) |

`timing="full"` (default) runs all three, `"syncs"` only BITS and TS (exact
positions, lengths to about a tenth of the run: what decoding needs), and
`"none"` BITS alone. Disk reads and verifies use `"syncs"`.

**Alignment.** TB and TS start after an anchor: up to 8 bytes chosen from
the BITS pass that occur at one angle of the track and that the drive's
matcher is sure to find. Their byte 0 is then a known BITS byte. The bytes
must repeat one revolution on, which proves they are framed as on every
revolution (bytes read before the first sync after a step are not). A `now`
capture without such bytes is retaken from a sync. With no anchor at all
(for example a track of identical bytes) TB and TS start like BITS did and
are aligned by cross-correlating their syncs with the boundaries a sync can
occupy. Slips (a weak area changing the byte count) are followed by dynamic
programming over a ±8 byte band.

**BITS loss bound.** The wait polls byte ready (V) at most 7 cycles apart.
A byte is read 8 cycles after the poll that sees it, and the next wait starts
19 cycles after that poll, or 32 after a page change. A byte seen u cycles
late leaves the next at most max(7, 7 + 32 − T) late, so nothing is lost
while 7 + 32 + 8 < 2T, where T is the shortest byte period. Syncs only
lengthen byte intervals (T is 8 cells whatever the sync length), so this
holds for every sync length: at zone 3, T = 26·300/rpm cycles, so up to
331 rpm. `Capture.overrun_risk` flags a capture whose measured byte period
is below 23.5 cycles. Writing obeys 7 + 31 + 12 < 2T: up to 312 rpm at zone 3.

**TB sample windows.** After each byte the TB wait samples V every 2 cycles
for 24 cycles, then every 3 or 4 cycles (11-cycle loop), with one 7-cycle gap
per 256 iterations for the timeout check. From two consecutive timer reads
the host recomputes which sample saw each byte and so its arrival window,
exactly; a byte already waiting when its wait began is pinned from a timed
neighbour. The extra cycles across a sync are known to the sum of the two
windows, at most 2 + 7 cycles, against a cell of 3.25 cycles at zone 3. The
hidden-one count is the feasible integer nearest the middle (0, or enough to
make 10 ones with the latched ones), and every sync carries the bounds the
windows allow (`Capture.sync_bounds`). Since 9 cycles is under 3 cells at
any supported speed, the estimate is within ±1 bit (a few in 10⁴ reach
±2 or ±3, inside their bounds), and exact when the windows are narrow.
A waiting byte pinned across a known sync is shifted by that sync's
hidden ones. The byte period comes from runs of bytes with no
uncertain sync between them (`P_SPAN` bytes either side, centred so steady
motor drift cancels); its error enters the bounds, which grow for very long
syncs among dense syncs.

**TS.** TS polls V and SYNC every 13 cycles and counts every byte (two bytes
cannot arrive between two polls). After SYNC is seen it times the release in
11-cycle steps; that coarse length (±57 cycles) picks TB's 256-cycle wraps.
A sync too short for TS to see is too short to wrap. The one exception to
exact counting: a release that falls in the 34-cycle bookkeeping of a
256-iteration wrap is seen up to 45 cycles after it and the poll resumes
up to 80 cycles after it, which can merge up to `TS_LATE_MERGE` = 3 byte
readies at the fastest byte period, so the counts after such a sync
(iterations a multiple of 256) may run short. The alignment lets the offset
grow by that much there at no cost. A TB wait takes the wrap counts that
put its extra cycles inside the TS window, widened by `DRIFT` (2%) of motor
speed change between the TB and TS passes, and that end on a T2 read the
drive loop can make; when more than one fits, the run's bounds span them
all. TS records 256 syncs; a
fuller track ends TS (`ST_FULL`) and syncs past it get an unbounded upper
length.

**Simulated accuracy.** `tools/sync_accuracy.py --seeds 100` captures a
track holding two of each sync length 10-20, 24, 28, 32, 40, 48, 64, 80,
100, 128, 200, 255, 256, 300, 400, 500, 640, 800 and 1000 bits with 1-24
data bytes between syncs, on both models, all four zones, 297/300/303 rpm,
with and without 3 rpm of wander (2 s period), every start mode: 12000
captures, 1154439 syncs.

| Measure | Result |
|---|---|
| Captures losing a byte | 0 |
| Syncs missed / invented | 0 / 0 |
| True length outside `sync_bounds` | 0 |
| Run length error (bits): 0 / ±1 / ±2 / ±3 | 962983 / 190915 / 519 / 22 |
| Bound width (bits): 0 / 1 / 2 / 3-15 | 239648 / 606995 / 166062 / 141734 |

**Revolution.** On a 1571 the index period gives the rpm. Otherwise the
byte period of the BITS pass's own repetition, timed by TB across it, gives
`Capture.revolution_cycles` and `Capture.rpm`.

**Records.** `Capture.save` stores the raw passes (version 2). Version 1
records (one combined pass with a polled sync table, ±3 bits) still load.

## Disk operations

**Reading:**

1. The head is located without touching the stop. A 1571 steps outwards one
   halftrack at a time until its track 0 sensor (VIA1 PA0, low) trips. A
   1541 reads the sector headers under the head, falling back to DOS's
   current track (`$22`), with the stepper phase choosing between adjacent
   halftracks. Only if all of these fail (unformatted media) does it bump the
   head against the stop, and only with `--allow-bump`. Then `calibrate`
   relabels the head position from the header track numbers it finds near
   track 18.
2. The header ID of track 18 is used for ID-mismatch (29) detection.
3. Each track is captured from a sync. The whole capture is decoded:
   1.03–1.3 revolutions, so every sector appears whole at least once.
4. Retries merge the best read of each sector until all are OK or `--retries`
   is exhausted.

On the D71 side 1, headers carry tracks 36–70, but the zone is the physical
track's.

**Writing:**

1. `revolution_cells` writes filler with one sync at the end, at density 0, on
   the first track. It measures the cells between the sync's passes, which
   gives the drive's RPM.
2. Each track is formatted with `format_track` to the measured capacity, less
   2% (`MEASURED_TOLERANCE`).
3. Filler is prepended, so the stream covers a revolution at +2% speed and
   fills whole pages. The tail of the track lands on the filler, so no old
   data survives.
4. Each track is verified by capture and decode against the error bytes that
   `format_track` reproduces (20–24, 27, 29). Unverified tracks are rewritten
   up to `--retries` times.

## Hardware validation

`nybulah hwcheck --disk` locates and calibrates the head as a read does,
then reads one track in each zone (1, 18, 25, 31). It never writes. It reports the following:

- bytes;
- syncs and their length range;
- byte period and overrun risk;
- decoded sectors;
- the 1571 index RPM.

These are not yet confirmed on hardware:

- the stepper phase convention (track 1 at phase 0, from a bump that landed
  one track outside the old phase 2 assumption);
- the 1571 track 0 sensor polarity (PA0 low on track 1);
- the step and settle delays;
- the 1571 side bit polarity and WD1770 index after `$D0`;
- whether SYNC asserts on the tenth one (TS's SYNC low time against TB's
  hidden ones tests it);
- the TB sample windows: the cycle offset between SO setting V and a branch
  testing it, and SO behaviour when byte ready and `clv` coincide;
- the zone 3 margin on a fast motor;
- the motor speed change between the TB and TS passes (`DRIFT`, 2%);
- TS's late release on its wrap path and the byte readies it merges.
