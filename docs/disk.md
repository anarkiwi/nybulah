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
| Nibbler | `nybulah.nibbler` | Talks to `track.s` through any `Monitor` (s1-s4), or streams (1571, s4, firmware v12). Returns `Capture` records |
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

A streaming Nibbler (1571, s4, firmware v12) keeps the `SEEK` build of
`track.s` at `$0300` and loads the stream code over it to stream; RAM passes
and writes load `track_1571.bin` there first, and refuse with `TrackError`
when its `PASS` page does not read back from expansion RAM.

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

An anchor unique in the BITS bytes can still match elsewhere in a later pass:
a byte that reads differently each revolution (a format's write splice) can
spell it in a gap. On a blank-formatted track the anchored placement then
looks consistent, because GCR of zero-filled data blocks lets a sync fit every
5 bytes. So over a known revolution the anchored placement is checked against
the circular alignment that lands the most syncs where a sync can fit, and
among those, the one where the latched ones are least common in the BITS
bytes. A sync's latched ones depend on its bit phase, not on the data, so
this is the likelier alignment. The anchor is kept unless the other alignment
leaves fewer syncs unplaced or is likelier.

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
make 10 ones with the latched ones; an excess that fits neither takes the
nearer, so one below zero is never a sync), and every sync carries the bounds the
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

1. The head is located without touching the stop ([Head location](#head-location)).
   Then `calibrate` relabels the head position from the header track numbers
   it finds near track 18.
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

## Head location

The estimate comes first and never moves the head: the track in the sector
headers under the head, else DOS's current track (`$22`), placed on the
halftrack of the stepper phase (VIA2 PB0-1); a phase two halftracks away
takes the outer one. A 1541 uses the estimate; with none it raises unless
`--allow-bump`. A 1571 is then homed (`Nibbler.home`), and is never bumped:
`bump()` refuses it whatever `allow_bump` says. `close()` steps an odd
halftrack inwards and writes its track to `$22`, so DOS (and the next
session's estimate) stays right.

### The 1571 track 00 rule (DOS ROM)

From the 1571 DOS source ([listings](https://www.devili.iki.fi/Computers/Commodore/C1571/firmware/)):

| fact | source |
|---|---|
| Track 00 is VIA1 PA0 **clear**: `ror` moves PA0 into carry and carry set means "not track 00". | `lccutil1` 150-170, `mfmcntrl` 255-275 |
| Debounce: up to 99 iterations, each two PA reads (9 cycles apart, 33 cycles per iteration on track 00). A pair that disagrees ends the test as "not track 00"; after 99 agreeing pairs the last read decides. | `lccutil1` 150-170, `mfmcntrl` 255-275 |
| The head is on track 00 only if, in addition, the phase is 0 (`dskcnt & 3`, `dskcnt` = VIA2 PB, so the bits are PB0-1). `adrsed` non-zero disables the sensor in the GCR stepper. | `lccutil1` 172-177, `mfmcntrl` 277-279 |
| The test runs before every outward step and stops stepping when it passes: the stepper takes the first sensed phase 0 detent coming from inside. | `lccutil1` 144-196, `lccend` 158 (`stpout` → `patch9`), `mfmcntrl` 222-245 |
| Track 00 is DOS track 1: the MFM side counts half steps in `cur_trk` (0 at track 00, seeks to `2 * cmd_trk`), sets the phase bits to `cur_trk & 3`, and maps GCR track T to `cur_trk = 2 (T - 1)`. A bump sets phase 0, steps 92 half steps out and sets `drvtrk = 1`. | `mfmcntrl` 222-297, `fastutl` 1205-1213, `lcccntrl` 197-210 |

So, in nybulah's halftracks (track T on halftrack 2T): track 1 is halftrack 2
at phase 0, phase = (halftrack + 2) & 3 (`PHASE_OFFSET`). The sensor covers
halftrack 2, and halftrack 6 (the next phase 0 detent inwards) must read
clear, or DOS would take it as track 1: the sensor's inner edge is
halftrack 2-5 (`SENSOR_EDGE` = 5). A bump that ends at phase 0 against the
stop is DOS's track 1, so the stop lies within halftracks -1 to 2
(`HT_OUTER` = -1).

`sense_1571` (drive/sense.s, run from the capture buffer) is the same test:
99 pairs of `$180F` reads (port A without the ATN handshake), 9 cycles
apart, 33 cycles per pair, and returns the phase with the result.

### Homing

`home(estimate)`:

1. Sense. While the sensor is on, step inwards (at most `SENSOR_WALK` = 7,
   from `HT_OUTER` past `SENSOR_EDGE`); more raises TrackError (stuck on).
2. Without an estimate, the halftrack where the sensor clears is
   `edge + 1`, 3-6, one per phase; the phase fixes it. A sensor clear at the
   start with no estimate raises.
3. Step outwards one halftrack at a time to halftrack 2, sensing after each
   step. Each reading is checked against the halftrack: the phase must be
   its phase, the sensor must be on at halftrack 2 or below and clear above
   `SENSOR_EDGE`. Any mismatch raises TrackError.

Outward steps are at most `estimate - 2` (or 4 after the walk); every
outward step leaves a halftrack of 3 or more, where the last reading was
consistent with it. A sensor stuck clear runs out at halftrack 2 and raises;
one stuck on raises at the first halftrack above `SENSOR_EDGE` or after the
walk. Reaching the stop would take two faults at once: an estimate too far
in **and** a sensor stuck clear. With no estimate at all, a sensor stuck on
walks at most 7 halftracks inwards, which reaches the inner end only from
within 7 of it.

### DOS commands that move a 1571 head to the stop

nybulah sends only `M-R`, `M-W` and `M-E`; none of these step the head.
These DOS paths step outwards until the track 00 test passes (with the
sensor disabled by `adrsed` or failed, 92 or 180 half steps against the
stop):

- `N` (new/format): `lccfmt1` 45-61 (1541 mode), `lccfmt2a` 83-105 (1571
  mode).
- Error recovery on any GCR read or write job (`LOAD`, `I`, `V`, directory,
  `B-R`, `U1`, ...): after the head-offset retries, a `bump` job unless the
  caller set `jobrtn` (`jobssf` 246-270, `dskintsf` 300 enables it).
- A bump job (`$C0`) written to the job queue.
- Burst (`U0`) MFM commands: restore (`cmdone`, 180 half steps), read
  address, format, sector-table and logical seeks that fail (`mfmsubr` 29-62,
  `mfmsubr1` 439-447, `mfmsubr3` 130, `mfmcntrl` 496-510).

Power-on and reset only set phase 0 (`lccinit` 29), which pulls the head to
the nearest phase 0 detent.

`nybulah bus` refuses the commands in this list unless `--allow-dos-bump`
([hardware.md](hardware.md#head-safety)).

## Hardware validation

`nybulah hwcheck --disk` locates and calibrates the head as a read does,
then reads one track in each zone (1, 18, 25, 31). It never writes. It reports the following:

- bytes;
- syncs and their length range;
- byte period and overrun risk;
- decoded sectors;
- the 1571 index RPM.

These are not yet confirmed on hardware:

- the 1571 track 00 sensor's exact edge (`nybulah homeprobe --step`); homing
  from a header estimate is confirmed on hardware (68 and 34 checked steps,
  sensor on at halftrack 2, phase rule held);
- the step and settle delays;
- the 1571 side bit polarity and WD1770 index after `$D0`;
- whether SYNC asserts on the tenth one (TS's SYNC low time against TB's
  hidden ones tests it);
- the TB sample windows: the cycle offset between SO setting V and a branch
  testing it, and SO behaviour when byte ready and `clv` coincide;
- the zone 3 margin on a fast motor (streaming zone 3 is confirmed at the
  test drive's speed);
- the motor speed change between the TB and TS passes (`DRIFT`, 2%);
- TS's late release on its wrap path and the byte readies it merges.
