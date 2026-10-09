# Track scenarios in preserved disk images

This page gives aggregate statistics from a corpus of raw 1541 disk images,
and what each track scenario means for nybulah:

- how the scenario shows up in the data;
- how the current code handles it;
- what a serial capture needs to preserve it.

The corpus is not part of the repository. This page gives counts and
distributions only: no titles, names, file names or track contents.

## Survey

```sh
nybulah survey CORPUS --out DIR --workers 18 [--captures CAPTURE_DIR]
```

`nybulah.survey` works as follows:

1. It lists every image under `CORPUS`, including members of nested zip
   archives.
2. It loads each image with `nybulah.formats.loads`.
3. It writes one row per halftrack (`TRACK_DTYPE`), and every sync length, to
   `DIR/part-*.npz`. An interrupted scan resumes from these parts.

`nybulah.scenarios.summarise` then writes `DIR/summary.json`. `--captures` adds
a reference disk from saved `nybulah.nibbler` records.

Each track is measured on the revolution that nybulah extracts
(`formats.image.best_revolution`). `survey_image(..., revolution=)` takes a
different extractor.

The survey was run twice, with the same feature code:

| run | revolution detection | output |
|---|---|---|
| **old** | FFT autocorrelation on NIB bit streams | `artifacts/survey/` |
| **new** | segment-based `find_cycle` on `framed_capture` (NIB/NBZ); `decode_track` with the error-24 fix below | `artifacts/survey-new/` |

The prevalence figures below come from the new run. Cycle and error figures
show both runs.

| column | meaning |
|---|---|
| `hdr_period` | Distance between repeats of the same checksum-valid header in a linear capture. This is the revolution length in capture bits, measured independently of `find_cycle` |
| `errors` | `decode_track` codes on the extracted revolution, which is what `to_d64` reports |
| `errors_cap` | `decode_track` codes over the whole capture |
| `n_gcr_payload`, `n_gcr_tail` | Data blocks with invalid GCR in the block ID, data or checksum, or only in the off bytes after the checksum |
| `bad_span` | Longest stretch of runs of three or more zero cells, joined across gaps of up to one GCR group (40 bits) |
| `sim_half`, `sim_next` | Best circular agreement with the track ½ track and 1 track further out, after every sync is cut to 10 bits (`survey.canonical`, FFT cross-correlation) |
| `pair_agree` | The same agreement, measured against a G64 of the same disk stored next to the NIB |
| `mp_disagree`, `mp_span` | Disagreement between captures of one track, and the longest region that disagrees |
| `gap_top`, `gap_entropy` | Dominant byte of the sync-framed gaps (up to bit rotation), and the gap byte entropy |

### Thresholds

Each threshold is the 0.1% or 99.9% quantile of the same feature on *clean DOS
tracks*. A clean DOS track meets all of these conditions:

- It is a whole track, 35 or lower, at its standard density.
- Its headers carry its own track number.
- Every standard sector reads OK, once.

| threshold | linear (NIB/NBZ) | circular (G64) |
|---|---|---|
| short sync (below) | 11 bits | 12 bits |
| long sync (above) | 677 bits | 657 bits |
| track length / nominal bits per revolution | 0.966 – 1.029 | 0.966 – 1.014 |
| illegal-GCR span | 1745 bits | 1719 bits |
| neighbour agreement | 0.99925 | 0.99924 |
| multi-capture disagreement | none (no multi-capture images) | – |

Two cut-offs are definitions rather than quantiles:

- **No-flux fill:** the majority gap byte class itself contains three zero
  cells (`scenarios.ILLEGAL_FILL`).
- **Exact cycle:** within 8 bits of `hdr_period`.

## Corpus

| | |
|---|---|
| images listed (files and zip members) | 12,340 |
| failed to load | 1 (truncated) |
| distinct by content | 12,280: 6,181 G64, 5,888 NBZ, 211 NIB, 0 NB2 |
| tracks (halftrack entries) | 476,227 |
| track 18 sector 0 readable | 12,194 disks (old run: 8,393) |
| BAM ID ≠ header ID | 4,284 disks |
| NIB density flags (tracks) | match 5,997; no-sync 24,863; killer 1,300; no-cycle 1 |

### Caveats

- **Sync lengths are not measurements of the disk.** NIB stores each sync as
  the 0xFF bytes that the nibbler's read loop collected. The length is
  therefore byte-quantised and depends on that loop's timing, not on the bits
  written. G64 stores the sync length that the converter chose.
  - On clean tracks the median sync is 42 bits in NIB and 33 bits in G64 for
    the same kind of track. The DOS writes 40.
  - The figures below measure runs of ten or more ones in the stored stream.
    A run of ten or more ones cannot occur inside valid GCR, so it marks a real
    sync position.
  - The *existence* of very long syncs (hundreds of bits) is reliable. Exact
    lengths are not, and a "10-bit" sync may be a longer sync that the
    capture shortened.
  - Exact lengths need timed syncs (TS).
- **Weak bits are not measured.** The corpus has no NB2 or other multi-capture
  image. The multi-capture columns are tested on synthetic data only.
- **Unformatted tracks cannot be told apart by intent.** Only NIB images hold
  unformatted tracks, because the G64 converters dropped them.
  - Tracks 36–42 that hold only noise are almost always unused: the DOS never
    writes there. A deliberately unformatted key track beyond 35 cannot be
    told from an unused one without the loader.
  - Tracks 1–35 that hold only noise are non-standard on a DOS disk, because
    the DOS formats all 35. They may be deliberate, a disk that was never fully
    formatted, or damage. The data cannot tell these apart.

## Scenarios

The scenarios overlap, so one track can belong to several. "Disks" counts
distinct images. "%" is the share of the 12,280 disks.

| scenario | definition | tracks | disks | % | G64 / NIB tracks |
|---|---|---|---|---|---|
| standard DOS | clean DOS track | 377,861 | 11,868 | 96.6 | 192,739 / 185,122 |
| DOS with errors | own-track DOS headers, track ≤ 35, fewer OK sectors than standard (`errors_cap`) | 37,467 | 3,605 | 29.4 | 16,321 / 21,146 |
| extended 36–42, own content | track > 35, formatted, not a lower-track copy, not a no-flux fill | 7,322 | 2,380 | 19.4 | 4,414 / 2,908 |
| extended, DOS headers | track > 35 whose headers carry its own number | 922 | 485 | 4.0 | 488 / 434 |
| extended, copy of a lower track | track > 35 whose headers carry a lower number | 3,324 | 839 | 6.8 | 1,664 / 1,660 |
| half-track data | odd halftrack, formatted, unlike both neighbours | 254 | 18 | 0.1 | 223 / 31 |
| half-track crosstalk | odd halftrack identical to a neighbour | 1,474 | 567 | 4.6 | 1,473 / 1 |
| fat track | whole track ≤ 34 identical to the next whole track | 527 | 356 | 2.9 | 217 / 310 |
| killer | sync covers most of the track | 5,680 | 1,718 | 14.0 | 3,012 / 2,668 |
| unformatted, tracks 1–35 | noise, no header (NIB only) | 2,549 | 375 | 3.0 | 0 / 2,549 |
| unformatted, tracks 36–42 | noise, no header (NIB only; mostly unused) | 23,253 | 5,224 | 42.5 | 0 / 23,253 |
| no-flux fill | periodic fill containing three or more zero cells | 9,291 | 4,096 | 33.4 | 2,977 / 6,314 |
| no-sync custom | formatted, no sync, not a fill | 3,621 | 912 | 7.4 | 2,092 / 1,529 |
| long sync (stored) | a stored run of ones above the long threshold | 25,817 | 3,631 | 29.6 | 13,399 / 12,418 |
| 10-bit sync (stored) | a stored run of ones below the short threshold | 16,693 | 3,676 | 29.9 | 10,636 / 6,057 |
| extra sectors | DOS headers with sector number ≥ the zone count | 3,086 | 294 | 2.4 | 1,539 / 1,547 |
| custom sectors | formatted, has syncs, no DOS header | 9,551 | 2,292 | 18.7 | 5,487 / 4,064 |
| non-standard density | own content at a zone other than the standard | 12,868 | 2,282 | 18.6 | 7,909 / 4,959 |
| density label ≠ content | NIB density byte disagrees with the zone implied by `hdr_period` | 1,709 | 360 | 2.9 | 0 / 1,709 |
| mixed density | G64 per-byte speed map with more than one zone | 1 | 1 | 0.0 | 1 / 0 |
| long track | length / nominal above the threshold | 1,570 | 209 | 1.7 | 901 / 669 |
| short track | length / nominal below the threshold | 699 | 191 | 1.6 | 387 / 312 |
| weak bits | not measurable (see caveats) | – | – | – | – |
| illegal-GCR region | `bad_span` above the threshold, not a fill | 2,415 | 743 | 6.0 | 1,287 / 1,128 |
| duplicate headers | a sector number twice in one revolution | 5,403 | 777 | 6.3 | 3,216 / 2,187 |
| ID mismatch | header ID ≠ track 18 header ID | 1,175 | 424 | 3.5 | 633 / 542 |
| header track ≠ physical | track ≤ 35 whose headers carry another number | 781 | 256 | 2.1 | 451 / 330 |
| non-standard data mark | the block after a header starts with a byte other than `0x07` | 15,286 | 746 | 6.1 | 8,279 / 7,007 |
| non-standard gap fill | DOS track whose gaps are not mostly `0x55` | 203,276 | 6,367 | 51.8 | 102,317 / 100,959 |

### How each scenario presents, and what handles it

The telemetry codes come from the protection survey:

| code | telemetry |
|---|---|
| BITS | byte-ready capture |
| TS | timed sync length |
| TB | per-byte timing |
| IDX | index-relative position |
| REL | relative skew |
| MC | multi-capture |
| DEN | density sweep |
| HT | halftrack stepping to 42 |

Values in "presentation" are medians unless stated.

| scenario | presentation | nybulah today | limitation | capture needs |
|---|---|---|---|---|
| standard DOS | 39 syncs, 19 headers; length / nominal 0.9998 (1–99%: 0.972–1.019) | `decode_track`, `to_d64` | – | BITS |
| DOS with errors | Sector codes: 20: 241,727; 22: 183,955; 24: 100,573; 29: 8,218; 23: 6,898; 27: 242 | `sector.decode_track` reproduces 20–29; `format_track` writes them | `_best_per_sector` keeps one read per sector | BITS; MC to tell written errors from read faults |
| extended 36–42 | Usually no header; 7 syncs | Track keys up to halftrack 84; `g64_to_d64` keeps 40 tracks when 36–40 decode | `info()` and D64 cover whole tracks only | HT to 42 (stepping past 40 risks the stop) |
| lower-track copy | Headers name track 35 | Stored as a track | Indistinguishable from a deliberate copy | HT with step verification |
| half-track data | 102 syncs; 1 header | Kept as a halftrack key | `info()` does not decode odd halftracks | HT; REL/IDX for alignment |
| half-track crosstalk | Identical to a neighbour | Kept | Uses space; looks like data | HT; neighbour comparison |
| fat track | Agreement with N+1 ≥ 0.99925; headers of N repeated on N+1 | Not detected; G64 keeps both | Write-back needs aligned writes | HT + IDX/REL |
| killer | Sync covers nearly the whole track | `find_cycle` returns KILLER; `revolution_bytes` writes 0xFF | Length is nominal | TS (SYNC held) with a timeout |
| unformatted | `bad_span` ≈ whole track | UNFORMATTED. Conversions write the capture cut to the nominal length and list it under `unformatted` | Written as read noise, not as no flux | MC to prove randomness |
| no-flux fill | Constant fill: `0x00` in G64, a 4-cell pattern in NIB | UNFORMATTED: the 8-bit word test rejects a lag that only repeats the fill; written at nominal length | One read cannot show whether the fill reads back random | MC; write as no flux |
| no-sync custom | No sync | `find_cycle` works without syncs | No `hdr_period` to check against | BITS free-running |
| long sync | Longest stored run: 1,933 bits | Kept in the bit stream | Length not preserved (caveats) | TS |
| 10-bit sync | Stored next to normal syncs | Kept | Reliability is low (caveats) | TS; bit-exact stream |
| extra sectors | 1 header with sector number ≥ count | `decode_track` ignores sectors ≥ `sectors` | Dropped from D64 | BITS |
| custom sectors | 18 syncs; no DOS header | Bits kept; D64 reports 20 | Not decoded | BITS |
| non-standard density | Own content at another zone | Zone from the NIB density byte or the G64 speed | `lag_window` trusts the label | DEN |
| density label ≠ content | Length / nominal 0.874 (zone 0 content labelled zone 2) | When headers repeat, all zones' windows are searched: 98.7% FORMATTED | The label still sets the zone written to G64 | DEN, TB |
| long track | Length / nominal 1.029 (99%: 1.25) | Window ±5% | More than 5% long cannot be found | TB/IDX for RPM; slower write |
| illegal-GCR region | Span 3,776 bits | Kept as bits | One read is one random draw | MC; write as no flux |
| duplicate headers | Up to 22 headers on 21-sector tracks | `_best_per_sector` keeps one | Duplicates are lost in D64 | BITS; keep every header |
| ID mismatch | 9,565 sectors with code 29 | `g64_to_d64` takes the ID from track 18 | Unreadable track 18 means no check | BITS |
| header track ≠ physical | Headers name another track | D64 reports 20 | – | BITS + HT |
| non-standard data mark | Mostly code 22 | 22 | Custom blocks are not decoded | BITS |
| non-standard gap fill | 191,450 DOS tracks fill gaps with GCR-encoded zero bytes; 214,393 with `0x55` | Kept in G64; `format_track` writes `0x55` | Re-formatting changes the gaps | BITS |

## Error 24

`decode_track` now checks GCR validity only over the bytes DOS uses:

- the header up to its ID: 6 bytes;
- the data block ID, the 256 data bytes and the checksum:
  `DATA_CHECKED_BYTES` = 258 of the 260 bytes.

The two off bytes are the last two of the final 5-byte GCR group, bits
2580–2599 of the block. The old code also checked them. In 25.7% of DOS data
blocks, in both NIB and G64, the only invalid codes lie there. That is where
a write splice ends.

| DOS tracks, sectors with code 24 | old: one revolution | new: one revolution | over the capture |
|---|---|---|---|
| NIB/NBZ | 1,041,455 | 47,773 | 53,133 |
| G64 | 1,043,748 | 47,798 | 47,798 |

Invalid GCR inside the checked bytes occurs in 1.3% of DOS data blocks in both
formats. Code 20 on NIB DOS tracks fell from 141,140 to 114,622 sectors. The
difference is sectors that the old one-sector-short cycle cut off.

## Revolution detection per scenario

These figures cover linear captures (NIB/NBZ) only:

- **period** is the share of tracks that have an `hdr_period`;
- **in window** is the share of those periods that fall inside
  `lag_window(zone)` for the labelled zone;
- **exact**, **short** and **other** are shares of the FORMATTED tracks that
  have a period: exact, one sector short, or anything else.

| scenario | tracks | period % | old FORMATTED % | old UNFORMATTED % | old in window % | old exact % | old short % | old other % | new FORMATTED % | new UNFORMATTED % | new in window % | new exact % | new short % | new other % |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| all linear | 249,571 | 81.6 | 87.6 | 11.2 | 99.3 | 85.5 | 13.6 | 0.5 | 87.3 | 11.6 | 99.3 | 99.8 | 0.0 | 0.1 |
| standard DOS | 185,122 | 99.9 | 100.0 | 0.0 | 100.0 | 99.3 | 0.6 | 0.0 | 100.0 | 0.0 | 100.0 | 100.0 | 0.0 | 0.1 |
| DOS with errors | 21,146 | 74.9 | 98.6 | 1.1 | 99.6 | 78.8 | 20.5 | 0.6 | 92.3 | 7.4 | 99.6 | 99.2 | 0.2 | 0.6 |
| extended, own content | 2,908 | 12.3 | 100.0 | 0.0 | 99.2 | 74.7 | 20.6 | 3.9 | 100.0 | 0.0 | 99.7 | 96.9 | 0.0 | 2.8 |
| extended, lower-track copy | 1,660 | 96.6 | 63.3 | 36.4 | 15.2 | 20.3 | 0.5 | 77.6 | 20.4 | 79.3 | 15.2 | 73.2 | 0.0 | 26.5 |
| fat track | 310 | 76.8 | 100.0 | 0.0 | 100.0 | 88.3 | 10.6 | 0.6 | 100.0 | 0.0 | 100.0 | 100.0 | 0.0 | 0.0 |
| killer | 2,668 | 1.4 | KILLER | – | – | – | – | – | KILLER | – | – | – | – | – |
| no-flux fill | 6,314 | 1.3 | 95.7 | 0.0 | 100.0 | 81.0 | 17.7 | 1.3 | 96.3 | 0.0 | 96.4 | 96.4 | 0.0 | 3.6 |
| no-sync custom | 1,529 | 0.1 | 100.0 | 0.0 | – | – | – | – | 100.0 | 0.0 | – | – | – | – |
| long sync | 12,418 | 71.5 | 78.6 | 0.0 | 99.8 | 88.3 | 11.4 | 0.3 | 79.5 | 0.0 | 100.0 | 99.5 | 0.1 | 0.4 |
| 10-bit sync | 6,057 | 68.4 | 89.6 | 0.0 | 99.1 | 85.6 | 13.0 | 1.2 | 89.9 | 0.0 | 99.9 | 99.7 | 0.0 | 0.3 |
| extra sectors | 1,547 | 48.8 | 95.5 | 0.7 | 99.9 | 74.8 | 16.7 | 8.2 | 74.3 | 22.0 | 99.9 | 92.3 | 0.0 | 7.7 |
| non-standard density | 4,959 | 26.7 | 100.0 | 0.0 | 59.7 | 49.0 | 8.6 | 41.4 | 100.0 | 0.0 | 93.3 | 90.8 | 0.0 | 9.2 |
| density label ≠ content | 1,709 | 100.0 | 66.6 | 32.7 | 16.3 | 22.1 | 1.7 | 74.7 | 23.4 | 75.9 | 16.3 | 66.7 | 0.2 | 32.3 |
| long track | 669 | 100.0 | 100.0 | 0.0 | 98.7 | 74.0 | 23.7 | 2.3 | 100.0 | 0.0 | 98.7 | 98.1 | 0.8 | 1.2 |
| short track | 312 | 100.0 | 100.0 | 0.0 | 15.3 | 15.0 | 0.0 | 82.9 | 100.0 | 0.0 | 59.0 | 58.0 | 0.0 | 39.4 |
| illegal-GCR region | 1,128 | 42.5 | 100.0 | 0.0 | 100.0 | 90.2 | 9.8 | 0.0 | 100.0 | 0.0 | 99.8 | 99.4 | 0.2 | 0.4 |
| duplicate headers | 2,187 | 71.6 | 77.2 | 22.4 | 41.7 | 6.7 | 2.1 | 52.8 | 16.9 | 82.6 | 14.4 | 43.8 | 0.3 | 53.3 |
| ID mismatch | 542 | 67.7 | 96.8 | 3.2 | 95.7 | 76.1 | 20.2 | 2.6 | 92.6 | 7.4 | 94.8 | 94.8 | 0.0 | 4.1 |
| header track ≠ physical | 330 | 75.4 | 93.0 | 7.0 | 96.8 | 73.6 | 21.5 | 2.9 | 85.8 | 14.2 | 96.8 | 97.9 | 0.0 | 0.8 |
| non-standard data mark | 7,007 | 42.2 | 99.0 | 0.9 | 99.6 | 61.4 | 38.3 | 0.2 | 90.6 | 9.3 | 99.4 | 99.6 | 0.1 | 0.3 |

Findings:

- **The one-sector-short alias is gone.** It fell from 13.6% of linear tracks
  to 0.02%, and exact cycles rose from 85.5% to 99.8%.
  - The old FFT path was fooled because a zone 3 capture of 8 KB overlaps
    itself by only ~4,000 bits. A lag one sector short aligns every sector
    with the next one, so it can score as high as the true period.
  - The segment-based detector rejects that lag on header IDs.
- **The first segment-based version classed some tracks UNFORMATTED** when
  their true period lay outside the labelled zone's window: the density-label
  case (76%), lower-track copies (79%) and duplicate-header tracks (83%).
  `to_g64` used to omit them. Three changes followed, measured in the table
  below (`artifacts/survey-v4/`):
  - `find_cycle` searches every zone's window when headers repeat.
  - It falls back to bit-level scoring, tested on 8-bit words, when no
    measured sync pair spans a revolution.
  - Conversions write every track.
- **Periodic fills** have no header period. They are now UNFORMATTED, and the
  8-bit word test rejects them; they are still written.

| scenario (linear) | tracks | FORMATTED % | UNFORMATTED % | exact % |
|---|---|---|---|---|
| all linear | 249,571 | 87.1 | 11.8 | 99.8 |
| standard DOS | 185,198 | 100.0 | 0.0 | 100.0 |
| DOS with errors | 21,151 | 98.0 | 1.7 | 99.2 |
| extended, lower-track copy | 1,660 | 96.9 | 2.8 | 94.4 |
| density label ≠ content | 1,709 | 98.7 | 0.6 | 91.9 |
| duplicate headers | 995 | 91.1 | 7.9 | 43.6 |
| extra sectors | 1,548 | 95.4 | 0.8 | 92.2 |
| header track ≠ physical | 331 | 97.0 | 3.0 | 98.0 |
| short track | 1,590 | 100.0 | 0.0 | 91.6 |
| non-standard density | 6,093 | 100.0 | 0.0 | 95.3 |

The duplicate-header tracks that remain hold one sector written several times
around the track. Their `hdr_period` is the median gap between identical
headers within ±50% of nominal. With several copies per revolution that gap
is not the revolution, so "exact" understates them there.

Conversions now emit all 476,227 tracks. Before, `to_g64` dropped the 29,586
linear tracks classed UNFORMATTED and emitted 446,641. Those 29,586 are now
written at nominal length and reported.
- **Captures that start just after a sync** hide their first header from a
  sync-only scan. At zone 3 that header is the only one that repeats, so
  `survey.linear_headers` counts bit 0 as a sync end.

## NIB and G64 of the same disk

There are 6,007 tracks on 163 disks where a NIB and a G64 of the same name sit
together.

- **Agreement** after sync normalisation and alignment:

  | quantile | old | new |
  |---|---|---|
  | median | 1.0 | 1.0 |
  | 1% | 0.713 | 0.812 |
  | 0.1% | 0.657 | 0.679 |

- **Length difference** (G64 length − NIB `hdr_period`): median 0 bits, 1%
  quantile −344 bits, 0.1% quantile −1,147 bits. The G64 was cut from the
  NIB, sometimes with gap or sync compaction.

## Reference: our own captures of a blank-formatted disk

These are 35 tracks from `tests/data/hw`. The disk was formatted on a 1571 and
captured once per track, starting at a sync (31 pages), with timed syncs.
Measured speed was 299.7–300.4 rpm, derived from `hdr_period`.

| zone | tracks | old `find_cycle` (bit stream) | new `find_cycle` (segments) |
|---|---|---|---|
| 3 | 17 | 58.8% exact, 41.2% one sector short | 100% exact (within ±5 bits) |
| 2 | 7 | 100% exact | 100% exact |
| 1 | 6 | 66.7% exact, 33.3% at the upper window edge | 100% exact |
| 0 | 5 | 80% UNFORMATTED, 20% exact | 100% exact |

- **Sync lengths:** 31.5 / 39 / 44 bits at the 1% / 50% / 99% quantiles.
  The DOS writes 40.
- **Sectors:** 670 of the 683 standard sectors appear in the captures.

| error codes | one revolution, old | one revolution, new | whole capture |
|---|---|---|---|
| OK | 584 | 609 | 611 |
| 24 | 63 | 58 | 56 |
| 20 | 20 | 12 | 12 |
| 23 | 12 | 0 | 0 |
| 22 | 3 | 3 | 3 |
| 27 | 1 | 1 | 1 |

- **The remaining code-24 sectors (56 over the capture) are capture faults,
  not disk faults.**
  - Fixing the off bytes removed only about 3 of them on this disk: 0.4% of
    its blocks have invalid codes confined to the off bytes.
  - 9.5% of its data blocks have invalid GCR inside the checked bytes. On
    corpus DOS tracks the rate is 1.3%.
- **`analysis.faults.capture_faults` localises 152 decode failures** over all
  segments:

  | kind | count |
  |---|---|
  | one-bit slips | 62 |
  | corrupt codes, no shift | 30 |
  | ambiguous (two-bit or never resynchronised) | 60 |

  These are cells gained or lost in the byte-ready stream. A blank-formatted
  disk has no intentional errors. Retries and merging (`disk.py`) clear them
  for D64 imaging. Raw archives need MC to tell a slip from weak media.

## Files

| path | contents |
|---|---|
| `artifacts/survey-new/part-*.npz` | Per-track rows (`survey.TRACK_DTYPE`), image columns `image_*`, `sync_len`/`sync_row` |
| `artifacts/survey-new/summary.json` | Every number above for the new run, including the reference disk |
| `artifacts/survey/` | The old run |
