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
   different sector, track or ID, or if such a header pair lies at a distance
   one of the shift's significant pairs could span (their distance bounds
   overlap). On a uniformly formatted disk, every data block is identical, so
   a shift a few sectors short of a revolution can score higher than the true
   period, which an 8 KB capture spans with only a couple of pairs. When one
   pass missed a sync (two blocks read as one segment), that alias's pairs
   fall under two segment shifts, and the header pair that contradicts it may
   sit under only one of them.
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

- For a capture, it takes the exact bits between a measured sync and the
  measured sync nearest one `cycle.length` later, rotated to the start. The
  length can differ from `cycle.length` within the sync error.
  `revolution_spans(capture, cycle)` chains these passes into successive whole
  revolutions, cut inside syncs; unlike stepping `cycle.segments` syncs at a
  time, it survives a missed sync.
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

## Flux view

`nybulah flux IMAGE -o out.{png,apng,html}` (or `nybulah map --analog`) draws
the disk as an image of its magnetic flux (`nybulah.analysis.fluxview`,
`nybulah.fluxviz`, `nybulah.fluxhtml`). `--size` sets the disk diameter in
pixels, `--zoom` the viewer's largest pixels per bit cell, `--tracks 18-25` a
track range, `--track` the track of the eye and drift panels, `--captures`
more images of the disk as more revolutions. `tools/diskmap_captures.py
FOLDER... --analog -o out.png` does the same for saved capture records.

![Flux view of a synthetic disk](img/fluxview.png)

**Time.** Each revolution is a list of flux transitions on a time axis in
nominal bit cells of its density zone, with knots tying decoded bit positions
to times. What is measured depends on the source:

| source | transitions | time of each cell | `timing` |
|---|---|---|---|
| SCP, KryoFlux, P64 | measured | measured at every transition (16 MHz clock) | `flux` |
| captures with a TB pass | decoded bits, placed at their cell | measured per latched byte, within its arrival window | `tb` |
| G64 with a speed map | decoded bits | the image's density per byte | `zones` |
| other bit sources (NIB, G64, D64, TS-only and streamed captures) | decoded bits | one revolution in 200 ms (300 rpm) | `track` |

A bit-source transition sits at the start of its cell, where the read circuit
(`analysis.flux`) restarts its cell counter. Angle 0 is the index where there
is one; other tracks are rotated like the disk map, the sync before sector 0
at 0, and later revolutions follow it through their paired syncs.

**Channels.** Each angular bin of each revolution gets:

| channel | drawn as | derivation |
|---|---|---|
| density | lightness | transitions per cell, each transition spread over one divider carry (a quarter cell, the read circuit's time resolution); 0 with no flux, 1 when every cell reverses; at full zoom each transition is a stripe |
| delta | hue: blue shorter, orange longer, saturating at one zone step (1 / (16 - zone)) | cell length against the track's standard DOS zone, less one, shrunk towards 0 by the timing's error bound (the TB arrival window, one 16 MHz clock for flux); 0 where the bin holds no transition |
| var | chroma lost, towards grey | 4 p (1 - p) per bit, p the share of aligned revolutions with a one there; 0 with one revolution |
| noflux | dot lattice, transitions not drawn | inferred: runs of three or more zeros cannot be GCR, so the read saw no flux there; runs chained within `GROUP_BITS` make a span whose ones are read noise; bit sources only |
| fault | green tick across the halftrack | where a decode slip (`faults`) starts |

Each halftrack is a band (a polar ring), split among its revolutions, so
weak bits show as grain across the band; uncaptured halftracks stay empty, so
half tracks, fat tracks and crosstalk show as filled gaps. With `track` timing
a revolution's hue is its length against 300 rpm (long and short tracks, or a
track read at another density); with `flux` or `tb` it follows speed wobble,
density changes and write splices within the revolution.

**Panels** (PNG, and live in the viewer): interval histograms per zone at
16 MHz resolution, measured solid and decoded bits dashed, with ≥4T (no legal
GCR) shaded; the timing eye of one track (interval against angle, all
revolutions); and each revolution's drift: measured time less uniform cells,
or for `track` timing its bit offset from revolution 0 through the paired
syncs.

**Viewer.** The HTML page embeds every revolution's bits, knots, no-flux and
fault masks and variance (zlib, base64; decoded with the browser's
`DecompressionStream`) and recomputes the channels for each pixel shown, from
the whole disk down to single transitions; hovering reads out track, angle,
bit, the interval at the cursor (measured or inferred), cell length with its
bound, stability, no flux and faults. `tools/html_snapshot.py` loads a page
in headless Chromium (playwright) to check it.

**Colour.** Lightness and chroma are in OKLab; both arms reach the same
chroma at each lightness. The blue/orange poles and the green fault mark pass
the dataviz palette validator's colour-vision (protan, deutan, tritan) and
normal-vision separation checks against the dark surface; dots and ticks also
carry the marks as shapes.

`fluxsynth.synthetic_flux_disk(revolutions)` builds index-aligned flux reads of
a DOS disk with a speed wobble, a long sync, a killer track, a no-flux gap, a
weak span, a bit slip, a half-written density change, heavy jitter and a
half track reading both neighbours; `tools/diskmap_example.py` renders it to
`docs/img/fluxview.png`, and `tests/test_fluxview.py` checks each feature's
channel statistics at its known angles.

## MFM tracks (`nybulah.analysis.mfm`)

The 1581 records MFM through a WD1772. Sources: WD1772 datasheet (J.L. Guerin
edit v1.3), and the 1581 DOS 318045-01 sources `mrout.src` (`fmtrk`),
`dskint.src` (`psetdef`), `burstc.src` (`nsecks`) and `msub.src` (`trans_ts`,
`wdstatus`).

**Media and Read Track.** A track is `(data, mark)` bytes from the index;
`mark` flags A1*/C2* bytes written without a clock. Read Track output carries
no `mark`. The WD resynchronises on every A1*/C2*, so the first sync byte may
come out misframed. `find_marks` therefore accepts two A1 bytes before an ID
mark ($FC–$FF) or a data mark ($F8–$FB), and two C2 bytes before $FC (index
mark). `decode_track` accepts fields in order, as the WD does: a mark inside an
accepted field is data. A data mark less than 43 bytes after an ID's CRC
belongs to that ID (the Read Sector window).

**Fields.** An ID field is the mark, C, H, R, N and the CRC. A data field is
the mark, 128 << (N & 3) bytes and the CRC. Both CRCs are CRC-16-CCITT preset
to that of three A1 (`crc.SYNC3` = $CDB4). The records (`SECTOR_DTYPE`) hold:

- positions, the ID fields and the data size;
- CRC results;
- `gap`: the bytes from the end of the previous field to the next sync, and
  its 1581 value `gap_std`;
- `split`: the bytes from the ID to the data sync;
- the error byte and `Flag`s.

| error byte (DOS) | condition |
|---|---|
| 01 (00) | ID and data CRC good |
| 02 (20) | data field with no ID before it |
| 04 (22) | good ID with no data mark within 43 bytes |
| 05 (23) | data CRC |
| 09 (27) | ID CRC |

| flag | condition |
|---|---|
| `DELETED` | data mark $F8/$F9 (the DOS masks record type out of `wdstatus`, so it reads as OK) |
| `ODD_SIZE` | N ≠ 2 |
| `DUPLICATE` | another good ID with the same C, H, R in the revolution |
| `FOREIGN` | C ≠ cylinder, H ≠ side, or R outside 1–10 |
| `TRUNCATED` | field cut by the end of the read |
| `GAP` | a gap differs from the format by more than one byte, the most a write splice adds or drops |

The 1581 values follow `fmtrk` with `psetdef` gap3 35. The lead is 32 × $4E;
each ID is followed by 22 × $4E and each data field by 35 × $4E; each sync is
12 × $00 then 3 × A1*. The track is 6250 bytes (250 kbit/s at 300 rpm), so
each byte takes 32 µs.

`decode_ids` turns Read Address lists (six bytes, status, µs) into the same
records. The position is the time since the index divided by 32 µs.
`decode_reads` turns Read Sector results into records. Status maps as in the
datasheet status summary: RNF+CRC gives 27, RNF 20 and CRC 23. Lost data
gives 27, as in `wdstatus`.

`best_sectors(tracks, cylinder, side)` picks the best read of each R over all
revolutions or reads, and splits it into the two logical 256-byte sectors.

**Logical mapping** (`trans_ts`). `physical(track, sector)` gives
`(cylinder, side, R, half)`, and `logical` is its inverse:

- cylinder = track − 1;
- side H = 0 for sectors 0–19 and 1 for sectors 20–39;
- R = (sector mod 20) // 2 + 1;
- half = sector mod 2.

H equals CIA PA0. The physical head is 1 − H (`head_side`).

**Encoder.** `SectorSpec` describes one sector. It can carry an extra or
duplicate R, any N, a deleted mark, a bad ID or data CRC, a missing ID or data
field, and its own gap 2 and gap 3. `standard_layout(cylinder, side, data,
errors)` gives the `fmtrk` layout and reproduces D81 error bytes 20, 21, 22,
23 and 27. `encode_track` writes the media directly.

`plan_track` gives a `TrackPlan`:

- a Write Track image in the drive's RLE form (`rle`/`unrle`: token 0 ends;
  1–127 repeats the next byte; 128–255 copies t − 127 literal bytes; the last
  byte repeats until the index);
- Write Sector jobs for data that is not constant or that holds $F5–$F7 (Write
  Track writes those as A1*, C2* and CRC);
- the media that both leave, with Write Sector's trailing $FF byte.

Layouts that neither command can write are rejected. `wd_write_track` and
`wd_write_sector` model the two commands on media.

**Revolutions.** `compare_revolutions` pairs reads of each good ID by
(C, H, R, copy). It reports revolutions read, good reads, distinct contents
and stability. Weak runs are the payload bytes that differ from the per-byte
majority.

## MFM disk map (`nybulah.analysis.mfmmap`)

`mfm_disk_map(decodes)` builds a `DiskMap` on the GCR map's region table,
`stability` and raster, so `nybulah map` and `info --map` render it unchanged.
Read Track starts at the index, so revolutions are compared without alignment.
Positions are data bits (8 × byte). Rows are keyed by the halftrack of logical
track cylinder + 1, with `SIDE1` for H = 1.

| MFM kind | `Kind` | `detail` |
|---|---|---|
| sync, ID, data, gap | `SYNC`, `HEADER`, `DATA`, `GAP` | 3, R, R, length |
| ID CRC | `HDR_CHECKSUM` | R |
| C ≠ cylinder / H ≠ side / R outside 1–10 (extra sector) | `HDR_TRACK` / `HDR_ID` / `HDR_SECTOR` | R |
| duplicate C, H, R | `HDR_DUPLICATE` | R |
| ID without data (22) | `ID_NO_DATA` | R |
| data without ID (20) | `DATA_ORPHAN` | −1 |
| data CRC | `DATA_CHECKSUM` | R |
| deleted data mark | `DATA_DELETED` | R |
| N ≠ 2 | `DATA_SIZE` | R |
| gap off the format by more than one byte | `GAP_LONG`, `GAP_SHORT` | length |
| weak bytes (minority across revolutions) | `DISAGREE` | bytes |
| no address mark | `UNFORMATTED` | – |
