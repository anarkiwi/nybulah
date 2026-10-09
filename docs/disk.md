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
| Disk | `nybulah.disk` | Track jobs: decodes with `nybulah.analysis`, formats tracks, verifies |
| Simulator | `nybulah.simdisk` | Media, stepper, VIA2 and WD1770 index for the py65 drive model |

## Drive routines

`track.s` is assembled once per model and linked at `$0300`
(`track_1541.bin`, `track_1571.bin`). Its parameters and results live in zero
page `$60-$7A`. The host saves that range, PCR and (on the 1571) VIA1 port A
when it opens a session, and restores them on close. On a 1571 it also selects
1 MHz.

| | 1541 | 1571 |
|---|---|---|
| Buffer (31 pages) | `$8000-$9EFF` | `$6000-$7EFF` |
| Sync table (1 page) | `$9F00` | `$7F00` |

- **prep** sets the motor, LED and density bits. On a 1571 it also sets the
  side bit (VIA1 PA2). It steps a signed number of halftracks with a delay per
  step, then waits a settle time.
- **read** captures 31 pages. It has three start modes:
  - `now`;
  - `sync`: after a sync, optionally followed by a given byte under a mask;
  - `index` (1571): on the second of two index edges. The two edges give
    the revolution period.
- **write** checks write protect, can start at index, and then streams the
  buffer. It waits until the last byte has shifted out, then returns to read
  mode.

Every exit turns SOE off, so byte ready cannot disturb the monitor's V-flag
polling.

**Read loop.** A byte takes 18 cycles from the poll that sees byte ready to
the latch read. In the worst case, with a page change, two consecutive bytes
take 46 cycles. Zone 3 at 300 rpm allows 52. `Capture.overrun_risk` flags a
capture whose measured byte period leaves less than that.

**Sync telemetry.** For each sync the drive records the following:

- the byte position;
- the VIA1 timer 2 value when SYNC was seen asserted and when it was seen
  released;
- a release-wait loop count, which resolves 8-bit timer wraps.

The host converts the SYNC duration to cells, using the measured byte period
where it has one. The run length is that number of cells plus the nine ones
before SYNC asserts. It corrects for the poll latencies the loop structure
implies. Accuracy is within ±3 bits, and the error is mostly 0–1. The ones
that byte ready never latched (`Capture.hidden`) are restored by
`analysis.capture_bits`. A full table (50 entries) truncates the usable bytes
at the last recorded sync.

## Disk operations

**Reading:**

1. The head is first bumped to track 1. Then `calibrate` relabels the head
   position from the header track numbers it finds near track 18.
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

`nybulah hwcheck --disk` reads one track in each zone (1, 18, 25, 31). It
never writes. It reports the following:

- bytes;
- syncs and their length range;
- byte period and overrun risk;
- decoded sectors;
- the 1571 index RPM.

These are not yet confirmed on hardware:

- the phase convention for the home position (track 1 at stepper phase 2);
- the step and settle delays;
- the 1571 side bit polarity and WD1770 index after `$D0`;
- whether SYNC asserts on the tenth one;
- the zone 3 margin on a fast motor.
