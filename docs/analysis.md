# Analysis layer

`nybulah.analysis` and `nybulah.formats` work on raw track data only. They do not
import any transport code (`monitor`, `opencbm`, `sim`).

## GCR (`nybulah.analysis.gcr`)

Bits are `uint8` arrays holding 0/1, MSB first.

| Function | Purpose |
|---|---|
| `to_bits(data)` / `to_bytes(bits)` | Convert between bytes and a bit array |
| `rotate(bits, offset)` | Rotate a circular track |
| `encode(data)` / `encode_bits(data)` | Encode bytes 4-to-5 GCR, as bytes or as bits |
| `decode(gcr)` / `decode_bits(bits)` | Decode 5-to-4 GCR. Returns `(bytes, valid)`; `valid` flags bytes whose codes were legal. Accepts batches. |
| `runs_of_ones(bits, min_len=10, circular=False)` | Return `(starts, lengths)` of sync runs |
| `sync_mask(bits, circular=False)` | Per-bit sync membership |
| `speed_zone(track)`, `sectors_per_track(track)` | Standard 1541 layout per track |
| `bit_rate(zone)` | 16 MHz / (4·(16−zone)) |
| `bits_per_revolution(zone, rpm)`, `track_capacity(zone, rpm)` | Track size in bits and in bytes |

## Sectors (`nybulah.analysis.sector`)

- `format_track(track, data, disk_id, errors=None, capacity=None)` lays out a
  standard DOS track. `errors` maps a sector to a `SectorError`; codes 20, 21,
  22, 23, 24, 27 and 29 can be reproduced.
- `decode_track(bits, track, disk_id=None, sectors=None)` returns a
  `TrackDecode`. `bits` is a circular bit stream or a byte-ready capture. Every
  header in the stream is tried at any bit alignment, so a capture of more than
  one revolution holds some sectors twice. Each sector gets the best read found,
  its error code in D64 error-byte terms, `copies` (headers found for it) and
  `copy` (which of them, in stream order, was used).
- `merge_decodes(a, b)` keeps the better read of each sector across two decodes
  of one track. `header_tracks(bits)` lists the track numbers found in valid
  headers.

## Captures (`nybulah.analysis.capture`)

A byte-ready capture is not a continuous bit stream. Bytes are framed exactly
from the end of each sync, but each sync's length is only measured to within
`SYNC_ERROR_BITS` (±3), so the reconstructed stream can slip by a few bits at
every sync.

- `capture_bits(data, positions, runs, lead=0)` turns a byte-ready capture back
  into a bit stream. It inserts the sync ones that were never latched, so each
  sync's run (the latched trailing ones plus the inserted ones) matches its
  measured length. `trailing_ones(bits, ends)` counts the latched part.
- `segments(capture)` splits a capture into sync-delimited `Segments`. It takes
  `nybulah.nibbler.Capture` or any object with `data`, `positions`, `sync_bits`
  and `start` (and optionally `valid_bytes` and `sync_error`), such as
  `ByteCapture`. Segment `k` is a run of framed bytes. It starts at bit
  `begin[k]` of the restored stream. Its sync run starts at `run[k]`, which is
  -1 for the unmeasured sync a `start="sync"` capture began after. `content[k]`
  is the exact number of bits from the segment start to the next sync.
- `framed_capture(data)` reads a raw track that stores syncs as one bits, such
  as a nibtools NIB track. These are also byte-ready captures: syncs that end on
  a byte boundary restart framing. Their whole `0xFF` bytes are dropped, and
  each run is kept as a sync measured to within ±8 bits (one byte). A run that
  reaches the end of the buffer is filler.

## Revolution detection (`nybulah.analysis.cycle`)

`find_cycle(x, zone=None, period=None, index_aligned=False, tolerance=None, alpha=1e-3)`
returns a `Cycle(kind, start, length, match, z, sigma, segments)`. One API
covers both kinds of input:

- A byte-ready capture is analysed by segment. `zone` defaults to the capture's
  density. `period` defaults to its index period, when it has one, which
  narrows the window.
- A 0/1 array is treated as a continuous stream, for example from flux, P64 or
  G64. So is a capture with fewer than two syncs.

Both paths start the same way:

1. If sync bits make up most of the capture, it is classed `KILLER`.
2. Candidates are limited to what is physically possible:
   `bit_rate(zone) × period × (1 ± tolerance)`. Without a hint, `period` is
   0.2 s and the tolerance is ±5%. When the period was measured with the 1571
   index sensor, the tolerance is ±2%.

**Segmented captures.**

1. Pairs of segments `(i, j)` are candidates when their distance (bits between
   the two sync ends) can be one revolution. Since each sync is within ±`error`
   bits, the pair's distance must be within `error × (j − i)` of the window.
2. A shift `P = j − i` scores the bytes that agree at equal offsets, summed over
   its pairs. Chance agreement is Σf² over the capture's byte histogram. A
   partial first or last segment is compared over its common prefix.
3. A shift is rejected if any pair has two checksum-valid headers with
   different sector, track or ID. On a uniformly formatted disk, every data
   block is identical, so a shift of one sector less than a revolution would
   otherwise score as high as the true period.
4. The best remaining shift must be significant at family-wise level `alpha`,
   using a Bonferroni correction over the shifts tried. Otherwise the capture
   is `UNFORMATTED`.
5. `length` is the mean distance over the shift's pairs. Pairs whose own
   agreement is significant are used when there are any. Pairs further than
   the error bound from the median are dropped; these come from syncs missed
   in one pass.
6. `sigma` propagates a uniform ±`error` error per sync over the syncs that
   the pairs span. `segments` is the period in segments. A length outside the
   window by more than the one-sided `alpha` quantile of `sigma` is
   `UNFORMATTED`.
7. The start is the sync before a valid sector-0 header in any pass, otherwise
   the longest measured sync.
8. If the capture is `UNFORMATTED` but some valid header occurs twice, steps
   1–6 are repeated over the union of all four zones' windows
   (`any_zone_window`), with Bonferroni over that union. An image's density
   label need not be the rate its capture was read at.
9. If no pair of measured syncs can span a revolution, the restored stream is
   scored bit by bit, as for continuous streams. Bit agreement can come from
   gap fill alone, so the lag must also be significant for 8-bit words. Their
   chance agreement is taken from the word frequencies of the two overlapping
   regions, so a fill that repeats at every lag scores nothing.

**Continuous streams.** One FFT autocorrelation scores every lag by its bit
agreement above chance, p² + (1−p)². A track is `FORMATTED` only if both of
these hold:

- the best lag is significant at Bonferroni level `alpha`;
- most of the overlapping bits repeat.

The start is chosen as for segmented captures.

When `index_aligned=True`, the start is bit 0 of the capture.

`extract_revolution(x, cycle)` cuts out one revolution:

- For a capture, it takes the exact bits between a measured sync and its next
  pass, rotated to the start. The length can differ from `cycle.length` within
  the sync error.
- For a bit stream, it takes `cycle.length` bits from the start.

`index_align({key: (bits, cycle)})` gives every track a start at its index
pulse. `header_period(x, zone)` measures the revolution from headers alone,
without content scoring. It is the shortest in-window segment shift where some
pair of valid headers is identical and none differ. It returns the length and
its worst-case error.

Detection needs captures longer than one revolution, and the overlap is what it
measures. A capture whose overlap holds no header cannot rule out the
sector-period alias. An index-period hint narrows the window to less than one
sector.

## GCR faults (`nybulah.analysis.faults`)

`gcr_faults(bits)` finds and classifies the decode failures in one sync-framed
stream. `capture_faults(capture, cycle=None)` does the same for every segment
of a capture. Each returns a `FAULT_DTYPE` array with the following fields:

| Field | Meaning |
|---|---|
| `segment` | Segment the fault is in |
| `bit` | Bit offset of the fault within the segment |
| `width` | Width of the fault in bits |
| `shift` | Bits lost (negative: gained) |
| `resynced` | Decoding resumed after the fault |
| `exact` | `shift` is measured exactly, not only modulo 5 |
| `kind` | Classification (see below) |
| `byte` | Offset of the fault in the capture buffer |

A hidden Markov model tracks the 5-bit code phase:

- A clean slot at some phase must hold a legal code.
- A burst may hold anything, and decoding may resume after it at another phase.

Viterbi training fits the model to the data. It estimates how often a
misaligned code is legal, how often bursts start and how long they last. A
phase change measures the shift only modulo 5. A 2-bit slip and a lost 8-bit
byte look the same.

Given a segmented `cycle`, `capture_faults` measures a fault exactly when it is
the only one in its segment and a clean copy of the segment exists one period
away. The two segments' exact content lengths then give the shift.

`kind` is one of the following:

| Kind | Meaning |
|---|---|
| `CORRUPT` | No shift |
| `SLIP` | One bit, or an exact shift other than 0 or ±8 |
| `BYTE` | Exactly ±8 bits: a byte lost or gained by the capture loop |
| `AMBIGUOUS` | ±2 bits modulo 5 not measured exactly, or no resynchronisation |

`tools/fault_report.py CAPTURE.npz...` reports the classes over archived
captures. It also tests whether faults cluster at 256-byte buffer pages
(Rayleigh test) or at a position within the segment (KS test).

## Formats (`nybulah.formats`)

| Format | Read / write | Notes |
|---|---|---|
| D64 | `read_d64` / `write_d64` | 35, 40 or 42 tracks, with or without error bytes |
| G64 | `read_g64` / `write_g64` | v0; half tracks; per-track or per-byte speed zones |
| NIB / NB2 | `read_nib(buf, nb2=False)` / `write_nib` | Density flags are kept; entries come from the header table |

The other formats and the `DiskImage` API: [formats.md](formats.md).

Conversions (`nybulah.formats.convert`), with tqdm progress:

- `nib_to_g64(image, period=None, index_aligned=False)` trims each track to one
  revolution, found per segment of its `framed_capture`. For NB2 it keeps the
  pass with the fewest sector errors, then the strongest match. Every track is
  written. An unformatted track is kept as its capture cut to the nominal
  length, and its halftrack is appended to the optional `unformatted` list.
- `g64_to_d64(image)` decodes sectors and writes error bytes.
- `d64_to_g64(image)` writes standard formatting.

`nybulah.analysis.synth.simulate_capture` generates multi-revolution bit
streams with a chosen start, bit noise and weak regions. `byte_capture` turns
a stream into the `ByteCapture` that byte ready would latch, with sync lengths
measured to within ±3 bits. Both are for tests and tools.

`tests/data/hw` holds captures of a freshly formatted disk read on a 1571.
`tests/test_corpus.py` checks periods against `header_period` on NIB and NBZ
images, loose or zipped, loaded through `nybulah.formats.loads`. It runs only when `NYBULAH_CORPUS` names a directory, and
`NYBULAH_CORPUS_SAMPLE` sets how many images it reads.

## Corpus survey (`nybulah.survey`, `nybulah.scenarios`)

`nybulah survey CORPUS --out DIR` measures every halftrack of every image
(files and nested zip members) into resumable columnar parts. It then writes
`summary.json` with scenario prevalence, thresholds derived from clean DOS
tracks, and the per-scenario behaviour of `find_cycle`. See
[scenarios.md](scenarios.md).

## Disk map

`nybulah.analysis.diskmap.disk_map(image, captures=None, bins=2048)` classifies every interval of every
revolution of every track. `nybulah map IMAGE -o out.{png,svg,apng,html}`
renders it (`nybulah.viz`); `nybulah info --map` prints one line per track.

**Intervals.** `regions.parse(bits)` splits one circular revolution into syncs,
the block after each sync (header, data or other, by its first GCR byte), the
gap after each block's nominal width, and runs of three or more zero cells.
`nybulah.survey` computes its per-track features from these intervals, so the
survey and the map share one parse.

**Angle.** Index-aligned captures (SCP, KryoFlux, P64, indexed reads) keep bit
0 at the index. Other tracks are rotated so the sync before the sector 0 header
(else the longest sync) is at bit 0; `DiskMap.aligned` is false for them. Each
track is drawn over its own revolution length; a length outside the clean-DOS
range is its own kind.

**Thresholds.** Every bound is a clean-DOS quantile from `scenarios.thresholds`
(see [scenarios.md](scenarios.md#thresholds)), shipped as
`nybulah/thresholds.json`; `--thresholds summary.json` uses another survey.
Whole-track kinds are the survey scenarios of the image's own rows.

**Regions.** `DiskMap.regions` has one row per interval: `track` (halftrack
key), `rev`, `start_bit`/`end_bit` (end may pass the revolution length, which
wraps), `kind`, `cls`, `detail` and `stability`.

| class | kinds | `detail` |
|---|---|---|
| standard | `SYNC`, `HEADER`, `DATA`, `GAP`, `ZERO_SPAN` (illegal-GCR runs within the clean range) | length, sector, sector, length, length |
| density | `ZONE`, `ZONE_MIXED`, `ZONE_LABEL`, `LONG_TRACK`, `SHORT_TRACK`, `HALF_TRACK`, `FAT_TRACK` (whole track) | – |
| gap/fill | `GAP_LONG`, `GAP_SHORT` (by the block before), `GAP_FILL` (dominant class not a clean fill class), `GAP_IRREGULAR` (dominant class share below the clean bound) | length or fill class |
| sync | `SYNC_LONG`, `SYNC_SHORT`, `SYNC_IN_BLOCK` (starts inside the previous block's nominal width), `NO_SYNC`, `KILLER` | length, or bits into the block |
| header | `HDR_GCR`, `HDR_CHECKSUM`, `HDR_TRACK`, `HDR_SECTOR` (≥ zone sector count), `HDR_DUPLICATE`, `HDR_ID` (≠ track 18 ID), `HDR_MARK` (other mark before a data block) | sector, or mark |
| data | `DATA_SHORT` (sync before 325 GCR bytes), `DATA_GCR`, `DATA_CHECKSUM`, `DATA_ORPHAN` (no valid header before), `DATA_MARK` (other mark after a header), `BLOCK_OTHER` | sector, checksum delta, bits back to the previous block, or mark |
| no-flux/weak | `GAP_NOFLUX` (majority fill class with three zero cells), `NOFLUX_SPAN` (illegal-GCR chain above the clean bound), `DISAGREE` (bits differing from revolution 0), `UNFORMATTED` | length or disagreeing bits |
| capture fault | `CAPTURE_FAULT`: a decode failure after which framing resumes shifted (`faults.stream_faults`) and that explains a transient region; reads only, not one-revolution images | shift |

`EMPTY` marks unused tracks (unformatted beyond 35, half-track crosstalk).

**Revolutions.** Every whole revolution of every capture is parsed and
classified: index to index; for byte-ready captures other than the surveyed
one, from a sync to its next pass, each segment identified with a block of
revolution 0 (`_passes`); otherwise cycle by cycle from `find_cycle`.
`regions.pair_blocks` pairs blocks by canonical position (syncs cut to 10
ones): the rotation is swept exactly for the least total of bits differing
between each block and its nearest (compared from both block starts, the
unmatched part of the wider nominal width counting as differing) plus the
widths of blocks nearest to none; blocks then pair as mutual nearest walking
out from the best-placed pair, the offset following each pair, so sync-length
error accumulated around a revolution does not shift the pairing. `align(ref,
rev)` maps positions by the offset of the last paired sync and compares each
pair bit by bit over the narrower nominal block width (header 80 bits, data
2600, a block of other kind its segment): gaps, write splices and the bits
framed before a sync are not compared. Revolutions without syncs are compared
whole after a canonical FFT alignment.
`tools/diskmap_captures.py FOLDER...` maps saved capture folders of one disk
together and lists the tracks with non-standard or unstable regions.

**Stability.**

| value | meaning |
|---|---|
| intrinsic | every revolution has a region of the same kind overlapping it, and no unexplained disagreement does |
| unstable | some revolutions lack it, or a disagreement overlaps it (weak bits) |
| transient | not in every revolution, not no-flux, and over a framing shift of its own read (an orphan data block: of the block before; a disagreement: of either read) |
| unconfirmed | one revolution only |

Framing shifts that every revolution repeats are disk content, and shifts that
explain no region (write splices in gaps) are dropped. Blocks with non-DOS marks
are never explained by a read.

**Raster.** `DiskMap.grid[row, bin]` is the highest class covering each bin:
local anomalies over whole-track kinds over standard regions. Transient regions
draw as standard. `DiskMap.marks` flags unstable bins (1) and capture-fault
bins (2). `raster(rev=r)` gives one revolution.

`synth.synthetic_disk(revolutions)` builds index-aligned reads with one of
each injected anomaly and their positions; `tools/diskmap_example.py` renders
it to `docs/img/diskmap.{png,apng}`.
