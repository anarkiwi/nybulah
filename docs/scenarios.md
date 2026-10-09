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
(`formats.image.best_revolution`): `find_cycle` per segment of the
`framed_capture` for NIB/NBZ; a G64 track is already one revolution.
`survey_image(..., revolution=)` takes a different extractor.

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
| gap after a header | 19 – 90 bits | 19 – 90 bits |
| gap after a data block | 0 – 2411 bits | 0 – 2356 bits |
| share of a gap's bytes in its dominant fill class (below) | 0.327 | 0.333 |
| fill classes dominating at least 0.1% of gaps | 0x25, 0x3D, 0x55 | 0x00, 0x25, 0x3D, 0x55 |

The gap rows come from the per-gap columns (`gap_len`, `gap_after`,
`gap_top`, `gap_hits`, `gap_row`). Fill classes are bytes up to bit rotation (`regions.ROTATION_CLASS`).
`nybulah/thresholds.json` ships these bounds for `nybulah map`.

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
| track 18 sector 0 readable | 12,194 disks |
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
| standard DOS | clean DOS track | 377,937 | 11,868 | 96.6 | 192,739 / 185,198 |
| DOS with errors | own-track DOS headers, track ≤ 35, fewer OK sectors than standard (`errors_cap`) | 37,472 | 3,604 | 29.3 | 16,321 / 21,151 |
| extended 36–42, own content | track > 35, formatted, not a lower-track copy, not a no-flux fill | 6,862 | 2,224 | 18.1 | 4,414 / 2,448 |
| extended, DOS headers | track > 35 whose headers carry its own number | 923 | 486 | 4.0 | 488 / 435 |
| extended, copy of a lower track | track > 35 whose headers carry a lower number | 3,324 | 839 | 6.8 | 1,664 / 1,660 |
| half-track data | odd halftrack, formatted, unlike both neighbours | 255 | 18 | 0.1 | 223 / 32 |
| half-track crosstalk | odd halftrack identical to a neighbour | 1,474 | 567 | 4.6 | 1,473 / 1 |
| fat track | whole track ≤ 34 identical to the next whole track | 487 | 350 | 2.9 | 206 / 281 |
| killer | sync covers most of the track | 5,680 | 1,718 | 14.0 | 3,012 / 2,668 |
| unformatted, tracks 1–35 | noise, no header (NIB only) | 2,506 | 377 | 3.1 | 0 / 2,506 |
| unformatted, tracks 36–42 | noise, no header (NIB only; mostly unused) | 26,536 | 5,581 | 45.5 | 0 / 26,536 |
| no-flux fill | periodic fill containing three or more zero cells | 6,405 | 3,876 | 31.6 | 2,977 / 3,428 |
| no-sync custom | formatted, no sync, not a fill | 3,185 | 769 | 6.3 | 2,092 / 1,093 |
| long sync (stored) | a stored run of ones above the long threshold | 25,853 | 3,623 | 29.5 | 13,399 / 12,454 |
| 10-bit sync (stored) | a stored run of ones below the short threshold | 16,882 | 3,683 | 30.0 | 10,636 / 6,246 |
| extra sectors | DOS headers with sector number ≥ the zone count | 3,087 | 294 | 2.4 | 1,539 / 1,548 |
| custom sectors | formatted, has syncs, no DOS header | 9,633 | 2,226 | 18.1 | 5,487 / 4,146 |
| non-standard density | own content at a zone other than the standard | 14,002 | 2,390 | 19.5 | 7,909 / 6,093 |
| density label ≠ content | NIB density byte disagrees with the zone implied by `hdr_period` | 1,709 | 360 | 2.9 | 0 / 1,709 |
| mixed density | G64 per-byte speed map with more than one zone | 1 | 1 | 0.0 | 1 / 0 |
| long track | length / nominal above the threshold | 1,579 | 216 | 1.8 | 901 / 678 |
| short track | length / nominal below the threshold | 1,977 | 412 | 3.4 | 387 / 1,590 |
| weak bits | not measurable (see caveats) | – | – | – | – |
| illegal-GCR region | `bad_span` above the threshold, not a fill | 2,368 | 736 | 6.0 | 1,287 / 1,081 |
| duplicate headers | a sector number twice in one revolution | 4,211 | 581 | 4.7 | 3,216 / 995 |
| ID mismatch | header ID ≠ track 18 header ID | 1,175 | 424 | 3.5 | 633 / 542 |
| header track ≠ physical | track ≤ 35 whose headers carry another number | 782 | 257 | 2.1 | 451 / 331 |
| non-standard data mark | the block after a header starts with a byte other than `0x07` | 15,273 | 738 | 6.0 | 8,279 / 6,994 |
| non-standard gap fill | DOS track whose gaps are not mostly `0x55` | 203,270 | 6,367 | 51.8 | 102,317 / 100,953 |

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

| scenario | presentation | nybulah | limitation | capture needs |
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
| non-standard density | Own content at another zone | Zone from the NIB density byte or the G64 speed | Without repeating headers `lag_window` trusts the label | DEN |
| density label ≠ content | Length / nominal 0.874 (zone 0 content labelled zone 2) | When headers repeat, all zones' windows are searched: 98.7% FORMATTED | The label still sets the zone written to G64 | DEN, TB |
| long track | Length / nominal 1.029 (99%: 1.25) | Window ±5% | More than 5% long cannot be found | TB/IDX for RPM; slower write |
| illegal-GCR region | Span 3,776 bits | Kept as bits | One read is one random draw | MC; write as no flux |
| duplicate headers | Up to 22 headers on 21-sector tracks | `_best_per_sector` keeps one | Duplicates are lost in D64 | BITS; keep every header |
| ID mismatch | 9,565 sectors with code 29 | `g64_to_d64` takes the ID from track 18 | Unreadable track 18 means no check | BITS |
| header track ≠ physical | Headers name another track | D64 reports 20 | – | BITS + HT |
| non-standard data mark | Mostly code 22 | 22 | Custom blocks are not decoded | BITS |
| non-standard gap fill | 191,450 DOS tracks fill gaps with GCR-encoded zero bytes; 214,393 with `0x55` | Kept in G64; `format_track` writes `0x55` | Re-formatting changes the gaps | BITS |

## Error 24

`decode_track` checks GCR validity only over the bytes DOS uses:

- the header up to its ID: 6 bytes;
- the data block ID, the 256 data bytes and the checksum:
  `DATA_CHECKED_BYTES` = 258 of the 260 bytes.

The two off bytes are the last two of the final 5-byte GCR group, bits
2580–2599 of the block, where a write splice ends. In 25.7% of DOS data
blocks, in both NIB and G64, the only invalid codes lie there; 1.3% have
invalid GCR inside the checked bytes.

| DOS tracks, sectors with code 24 | one revolution | over the capture |
|---|---|---|
| NIB/NBZ | 47,713 | 53,133 |
| G64 | 47,798 | 47,798 |

## Revolution detection per scenario

These figures cover linear captures (NIB/NBZ) only:

- **period** is the share of tracks that have an `hdr_period`;
- **in window** is the share of those periods that fall inside
  `lag_window(zone)` for the labelled zone;
- **exact**, **short** and **other** are shares of the FORMATTED tracks that
  have a period: exact, one sector short, or anything else.

| scenario | tracks | period % | FORMATTED % | UNFORMATTED % | in window % | exact % | short % | other % |
|---|---|---|---|---|---|---|---|---|
| all linear | 249,571 | 81.6 | 87.1 | 11.8 | 99.3 | 99.8 | 0.0 | 0.1 |
| standard DOS | 185,198 | 99.9 | 100.0 | 0.0 | 100.0 | 100.0 | 0.0 | 0.1 |
| DOS with errors | 21,151 | 74.9 | 98.0 | 1.7 | 99.6 | 99.2 | 0.2 | 0.6 |
| extended, own content | 2,448 | 14.7 | 100.0 | 0.0 | 99.2 | 97.2 | 0.0 | 2.5 |
| extended, lower-track copy | 1,660 | 96.6 | 96.9 | 2.8 | 15.2 | 94.4 | 0.0 | 5.5 |
| fat track | 281 | 85.0 | 100.0 | 0.0 | 100.0 | 100.0 | 0.0 | 0.0 |
| killer | 2,668 | 1.4 | KILLER | – | – | – | – | – |
| no-flux fill | 3,428 | 2.5 | 93.3 | 0.0 | 96.4 | 96.4 | 0.0 | 3.6 |
| no-sync custom | 1,093 | 0.0 | 100.0 | 0.0 | – | – | – | – |
| long sync | 12,454 | 71.5 | 79.6 | 0.0 | 99.7 | 99.5 | 0.1 | 0.4 |
| 10-bit sync | 6,246 | 67.5 | 90.2 | 0.0 | 98.1 | 99.6 | 0.0 | 0.4 |
| extra sectors | 1,548 | 48.8 | 95.4 | 0.8 | 99.9 | 92.2 | 0.0 | 7.8 |
| non-standard density | 6,093 | 42.7 | 100.0 | 0.0 | 47.6 | 95.3 | 0.0 | 4.7 |
| density label ≠ content | 1,709 | 100.0 | 98.7 | 0.6 | 16.3 | 91.9 | 0.1 | 7.8 |
| long track | 678 | 100.0 | 100.0 | 0.0 | 97.4 | 97.9 | 0.7 | 1.3 |
| short track | 1,590 | 100.0 | 100.0 | 0.0 | 11.6 | 91.6 | 0.0 | 7.9 |
| illegal-GCR region | 1,081 | 45.0 | 100.0 | 0.0 | 99.2 | 99.2 | 0.2 | 0.6 |
| duplicate headers | 995 | 37.6 | 91.1 | 7.9 | 60.7 | 43.6 | 0.3 | 53.5 |
| ID mismatch | 542 | 67.7 | 94.5 | 5.5 | 94.8 | 94.8 | 0.0 | 4.1 |
| header track ≠ physical | 331 | 75.2 | 97.0 | 3.0 | 96.8 | 98.0 | 0.0 | 0.8 |
| non-standard data mark | 6,994 | 42.2 | 97.9 | 2.0 | 99.6 | 99.6 | 0.1 | 0.3 |

- **One sector short.** A zone 3 capture of 8 KB overlaps itself by only
  about 4,000 bits, and a lag one sector short aligns every sector with the
  next, so content scoring alone can rate it as high as the true period. The
  segment detector rejects any shift that pairs two valid headers with
  different sector, track or ID.
- **Period outside the labelled window** (density label ≠ content,
  lower-track copies): `find_cycle` searches every zone's window when headers
  repeat.
- **No sync pair spans a revolution:** the restored stream is scored bit by
  bit, and the lag must also be significant for 8-bit words. Periodic fills
  have no header period; the word test classes them UNFORMATTED.
- **Duplicate headers** hold one sector written several times around the
  track. Their `hdr_period` is the median gap between identical headers
  within ±50% of nominal, which is not the revolution when there are several
  copies per revolution, so "exact" understates them.
- **Captures that start just after a sync** hide their first header from a
  sync-only scan. At zone 3 that header is the only one that repeats, so
  `survey.linear_headers` counts bit 0 as a sync end.

Conversions write all 476,227 tracks; the 29,586 linear tracks classed
UNFORMATTED are written at nominal length and reported.

## NIB and G64 of the same disk

There are 6,007 tracks on 163 disks where a NIB and a G64 of the same name sit
together.

- **Agreement** after sync normalisation and alignment: median 1.0, 1%
  quantile 0.812, 0.1% quantile 0.679.
- **Length difference** (G64 length − NIB `hdr_period`): median 0 bits, 1%
  quantile −344 bits, 0.1% quantile −1,147 bits. The G64 was cut from the
  NIB, sometimes with gap or sync compaction.

## Reference: our own captures of a blank-formatted disk

These are 35 tracks from `tests/data/hw`. The disk was formatted on a 1571 and
captured once per track, starting at a sync (31 pages), with timed syncs.
Measured speed was 299.7–300.4 rpm, derived from `hdr_period`.

- **Cycles:** `find_cycle` finds every track's period exactly in all four
  zones (zone 3: 17 tracks, within ±5 bits; zone 2: 7; zone 1: 6; zone 0: 5).
- **Sync lengths:** 31.5 / 39 / 44 bits at the 1% / 50% / 99% quantiles.
  The DOS writes 40.
- **Sectors:** 670 of the 683 standard sectors appear in the captures.

| error codes | one revolution | whole capture |
|---|---|---|
| OK | 609 | 611 |
| 24 | 58 | 56 |
| 20 | 12 | 12 |
| 22 | 3 | 3 |
| 27 | 1 | 1 |

- **The code-24 sectors (56 over the capture) are capture faults, not disk
  faults.** 0.4% of the disk's data blocks have invalid codes confined to the
  off bytes; 9.5% have invalid GCR inside the checked bytes, against 1.3% on
  corpus DOS tracks.
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

`nybulah survey` writes to its `--out` directory:

| path | contents |
|---|---|
| `part-*.npz` | Per-track rows (`survey.TRACK_DTYPE`), image columns `image_*`, `sync_len`/`sync_row`, per-gap `gap_*` columns |
| `summary.json` | Every number above, including the reference disk with `--captures` |
