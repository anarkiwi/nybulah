# Track scenarios in preserved disk images

Aggregate statistics of a corpus of raw 1541 disk images, and what each track
scenario means for nybulah: how it shows up in the data, how the current code
handles it, and what a serial capture needs to keep it. The corpus is not part
of the repository. No titles or track contents are given here, only counts and
distributions.

## Survey

```sh
nybulah survey CORPUS --out artifacts/survey --workers 18 [--captures CAPTURE_DIR]
```

`nybulah.survey` lists every image under `CORPUS`, including members of nested
zip archives. It loads each one with `nybulah.formats.loads` and writes one row
per halftrack (`TRACK_DTYPE`), plus every sync length, to
`artifacts/survey/part-*.npz`. Interrupted scans resume from those parts.
`nybulah.scenarios.summarise` writes `summary.json`. `--captures` adds a
reference disk from saved `nybulah.nibbler` records.

Every track is measured on the revolution nybulah extracts today
(`formats.image.best_revolution` → `analysis.cycle.find_cycle`).
`survey_image(..., revolution=)` accepts another extractor. The capture-level
columns do not depend on that revolution:

| column | meaning |
|---|---|
| `hdr_period` | Distance between repeats of the same checksum-valid header in a linear capture. This is the revolution length in capture bits, independent of `find_cycle` |
| `errors` | `decode_track` error codes on the extracted revolution: what `to_d64` reports |
| `errors_cap` | Codes over the whole capture. Invalid GCR that is confined to the two bytes after the data checksum is not counted (`survey.dos_errors`) |
| `n_gcr_payload`, `n_gcr_tail` | Data blocks with invalid GCR in marker/data/checksum, or only after the checksum |
| `bad_span` | Longest stretch of runs of three or more zero cells, joined across gaps of up to one GCR group (40 bits) |
| `sim_half`, `sim_next` | Best circular agreement with the track ½ and 1 track further out, after cutting every sync to 10 bits (`survey.canonical`, FFT cross-correlation) |
| `pair_agree` | The same agreement against the G64 of the same disk, where one sits next to the NIB |
| `mp_disagree`, `mp_span` | Disagreement between captures of one track (NB2 passes), and the longest disagreeing region |
| `gap_top`, `gap_entropy` | Dominant byte of the sync-framed gaps, up to bit rotation, and the byte entropy |

**Thresholds** are not chosen by hand. Each one is the 0.1% or 99.9% quantile of
the same feature on *clean DOS tracks*. A clean DOS track is a whole track
≤ 35 at its standard density whose headers carry its own track number, with
every standard sector reading OK exactly once.

| threshold | linear (NIB/NBZ) | circular (G64) |
|---|---|---|
| short sync (sync shorter than) | 11 bits | 12 bits |
| long sync (sync longer than) | 686 bits | 657 bits |
| track length / nominal bits per revolution | 0.966 – 1.029 | 0.966 – 1.014 |
| illegal-GCR span (`bad_span`) | 1733 bits | 1719 bits |
| neighbour agreement (`sim_*`) | 0.99923 | 0.99924 |
| multi-capture disagreement | none: the corpus has no NB2 | – |

Two thresholds are definitions, not quantiles:

- A **no-flux fill** is a track whose majority gap byte class itself contains
  three zero cells (`scenarios.ILLEGAL_FILL`).
- **Cycle "exact"** means within 8 bits (one byte of framing) of `hdr_period`.

## Corpus

| | |
|---|---|
| images listed (files and zip members) | 12,340 |
| failed to load | 1 (truncated) |
| distinct by content | 12,280: 6,181 G64, 5,888 NBZ, 211 NIB, 0 NB2 |
| tracks (halftrack entries) | 476,227 |
| track 18 sector 0 readable | 8,393 disks; BAM ID ≠ header ID on 2,938 |
| NIB density flags (tracks) | match 5,997; no-sync 24,863; killer 1,300; no-cycle 1 |

Sync lengths (bits, quantiles 0.1% / 1% / 50% / 99% / 99.9%):

| | all tracks | clean DOS tracks |
|---|---|---|
| NIB/NBZ | 10 / 11 / 41 / 121 / 2018 | 11 / 25 / 42 / 52 / 686 |
| G64 | 10 / 11 / 31 / 92 / 2051 | 12 / 24 / 33 / 52 / 657 |

On clean tracks, the median sync is 42 bits in NIB and 33 bits in G64.
Neither format keeps the written sync length. NIB stores the sync as the
nibbler's byte loop saw it, and G64 stores the sync as the converter wrote it.

## Scenarios

The scenarios overlap: one track can be in several. "Disks" counts distinct
images. "%" is the share of the 12,280 disks.

| scenario | definition | tracks | disks | % | G64 / NIB tracks |
|---|---|---|---|---|---|
| standard DOS | clean DOS track | 351,459 | 11,849 | 96.5 | 192,768 / 158,691 |
| DOS with errors | own-track DOS headers, ≤ 35, fewer OK sectors than standard (`errors_cap`) | 39,322 | 4,316 | 35.1 | 16,292 / 23,030 |
| extended 36–42, own content | track > 35, formatted, not a copy of a lower track, not a no-flux fill | 7,060 | 2,266 | 18.4 | 4,414 / 2,646 |
| extended, DOS headers | track > 35 with headers carrying its own number | 922 | 485 | 4.0 | 488 / 434 |
| extended, copy of a lower track | track > 35 whose headers carry a lower track number | 3,324 | 839 | 6.8 | 1,664 / 1,660 |
| half-track data | odd halftrack, formatted, unlike both neighbours | 233 | 18 | 0.1 | 223 / 10 |
| half-track crosstalk | odd halftrack identical to a neighbour | 1,475 | 567 | 4.6 | 1,473 / 2 |
| fat track | whole track ≤ 34 identical to the next whole track | 461 | 309 | 2.5 | 217 / 244 |
| killer | sync covers most of the track | 5,824 | 1,755 | 14.3 | 3,012 / 2,812 |
| unformatted / noise | `find_cycle` UNFORMATTED, no header | 25,755 | 5,148 | 41.9 | 0 / 25,755 |
| no-flux fill | periodic fill with ≥ 3 zero cells | 9,297 | 4,019 | 32.7 | 2,977 / 6,320 |
| no-sync custom | formatted, no sync, not a fill | 3,641 | 925 | 7.5 | 2,092 / 1,549 |
| long sync | sync above the long threshold | 26,079 | 3,735 | 30.4 | 13,399 / 12,680 |
| short sync | a sync below the short threshold (a 10-bit sync) | 16,350 | 3,549 | 28.9 | 10,636 / 5,714 |
| extra sectors | DOS headers with sector numbers ≥ the zone count | 3,087 | 293 | 2.4 | 1,539 / 1,548 |
| custom sectors | formatted, syncs, no DOS header | 9,467 | 2,183 | 17.8 | 5,487 / 3,980 |
| non-standard density | own content at a zone other than the standard one | 13,711 | 2,344 | 19.1 | 7,909 / 5,802 |
| density label ≠ content | NIB density byte disagrees with the zone that `hdr_period` implies | 1,709 | 360 | 2.9 | 0 / 1,709 |
| mixed density | G64 per-byte speed map with more than one zone | 1 | 1 | 0.0 | 1 / 0 |
| long track | length / nominal above the threshold | 1,670 | 229 | 1.9 | 901 / 769 |
| short track | length / nominal below the threshold | 1,408 | 316 | 2.6 | 387 / 1,021 |
| weak bits (multi-capture) | not measurable: no multi-capture images | 0 | 0 | 0 | – |
| illegal-GCR region | `bad_span` above the threshold, not a fill | 2,155 | 633 | 5.1 | 1,287 / 868 |
| duplicate headers | a sector number twice in one revolution | 6,127 | 1,186 | 9.7 | 3,216 / 2,911 |
| ID mismatch | header ID ≠ track 18 header ID | 819 | 322 | 2.6 | 449 / 370 |
| header track ≠ physical | track ≤ 35 whose headers carry another number | 782 | 256 | 2.1 | 451 / 331 |
| non-standard data mark | the block after a header starts with a byte other than `0x07` | 15,618 | 1,011 | 8.2 | 8,279 / 7,339 |
| non-standard gap fill | DOS track whose gaps are not mostly `0x55` | 203,262 | 6,367 | 51.8 | 102,317 / 100,945 |

### How each scenario presents, and what handles it

Telemetry codes are from the protection survey:

| code | meaning |
|---|---|
| BITS | byte-ready capture |
| TS | timed sync length |
| TB | per-byte timing |
| IDX | index-relative position |
| REL | relative skew |
| MC | multi-capture |
| DEN | density sweep |
| HT | halftrack stepping to 42 |

| scenario | presentation (medians unless stated) | nybulah today | limitation | capture needs |
|---|---|---|---|---|
| standard DOS | 39 syncs, 19 headers; length / nominal 0.9998 (1–99%: 0.972–1.019) | `decode_track`, `to_d64` | Most error codes the pipeline reports here are artefacts (below) | BITS |
| DOS with errors | Codes over the capture: 20: 241,852; 22: 184,234; 24: 100,630; 23: 8,825; 29: 6,694; 27: 242 sectors | `sector.decode_track` reproduces 20–29; `format_track` writes them | `_best_per_sector` keeps one read per sector | BITS; MC to tell a written error from a read fault |
| extended 36–42 | No header in most; sync count from 0 to more than 800 | Keys up to halftrack 84; `g64_to_d64` keeps 40 tracks when 36–40 decode | `info()`/D64 cover whole tracks only | HT to 42; stepping past 40 risks the stop |
| copy of a lower track | Headers name track 35; 36% are UNFORMATTED to `find_cycle`, and 78% of the formatted ones have the wrong length | Stored as a track | Indistinguishable from a deliberate copy without HT telemetry | HT with step verification (header track numbers) |
| half-track data | 111 syncs, 1 header | Kept as a halftrack key | `info()` does not decode odd halftracks | HT, REL/IDX for alignment to neighbours |
| half-track crosstalk | Identical to a neighbour; header track = neighbour | Kept | It wastes space and looks like data | HT; compare with neighbours |
| fat track | Agreement with N+1 ≥ 0.99923; headers of N repeated on N+1 | No detection; G64 keeps both | Write-back needs aligned writes | HT + IDX/REL |
| killer | Sync covers ~100% of the track; no header | `find_cycle` KILLER; `revolution_bytes` writes 0xFF | Length is nominal, not measured | TS (SYNC held) and a timeout |
| unformatted / noise | `bad_span` ≈ whole track; `z` median 10.5 | UNFORMATTED; `to_g64` omits the track | An omitted track leaves whatever the target disk holds on write-back | MC to prove randomness |
| no-flux fill | A constant fill: `0x00` in G64, a 4-cell pattern in NIB. 96% are FORMATTED to `find_cycle` | Kept as content | A periodic fill has every multiple of its period as a valid lag, so the length is arbitrary | MC; write as no flux |
| no-sync custom | No sync; `bad_span` 7 bits | `find_cycle` works without syncs; `_anchor` falls back to bit 0 | No `hdr_period` exists, so the cycle cannot be checked | BITS free-running; bit-level alignment |
| long sync | Longest sync median 1,957 bits | Kept in the bit stream | NIB/G64 do not keep the written length; the median sync differs by format (42 vs 33 bits) | TS |
| short sync | 10-bit syncs on 29% of disks, mostly next to normal ones | Kept | 10 ones split differently at other framings | TS, bit-exact stream |
| extra sectors | Median 1 header (sector ≥ count) | `decode_track` ignores sectors ≥ `sectors` | The extra sectors are dropped from the D64 | BITS |
| custom sectors | 15 syncs, no DOS header | Bits kept; D64 reports 20 | No decode | BITS |
| non-standard density | Own content at another zone | Zone from NIB byte / G64 speed | `lag_window` trusts the label | DEN |
| density label ≠ content | Length / nominal median 0.874 (= zone 0 content labelled zone 2) | `find_cycle` searches the labelled zone: 33% UNFORMATTED, 75% of the rest wrong length | Wrong window | DEN, TB |
| long track | Length / nominal median 1.029 (99%: 1.25) | Window ±5% | Tracks more than 5% long cannot be found | TB/IDX for RPM; slower write |
| short track | Median 0.875 | – | Mostly the density-label case | DEN |
| illegal-GCR region | Span median 3,573 bits | Kept as bits | One read shows one random draw | MC; write as no flux |
| duplicate headers | Up to 22 headers on 21-sector tracks | `_best_per_sector` keeps one | The duplicates disappear from the D64 | BITS; keep every header |
| ID mismatch | Code 29 on 7,853 sectors | `g64_to_d64` reads the ID from track 18 | Disks with an unreadable track 18 get no check | BITS |
| header track ≠ physical | Headers name another track | D64 reports 20 | – | BITS + HT |
| non-standard data mark | 24 syncs; mostly code 22 | 22 | Custom block formats are not decoded | BITS |
| non-standard gap fill | 191,450 DOS tracks fill gaps with GCR-encoded zero bytes; 214,393 with `0x55` | Kept in G64; `format_track` writes `0x55` | Re-formatting changes the gaps | BITS |

## Decode artefacts in the current pipeline

| DOS tracks | one revolution (`errors`) | whole capture, off bytes ignored (`errors_cap`) |
|---|---|---|
| NIB/NBZ, code 24 | 1,041,455 | 53,167 |
| NIB/NBZ, code 20 | 141,140 | 114,193 |
| G64, code 24 | 1,043,748 | 47,825 |

- **Code 24 over-reporting.** `sector._read_errors` flags BAD_GCR when any of
  the 325 GCR bytes of a data block is invalid. In 25.7% of DOS data blocks,
  in both formats, the only invalid codes lie in the two bytes after the
  checksum, where a write splice ends. These blocks hold no data. As a result
  `to_d64` currently writes error 24 for about a quarter of all sectors in
  the corpus. Invalid codes in the payload occur in 1.6% (NIB) and 1.3% (G64)
  of data blocks.
- **Cycle cuts.** 26,947 sectors of NIB DOS tracks are lost on the extracted
  revolution but present in the capture, because of the one-sector-short alias
  below.

## Revolution detection (`find_cycle`) per scenario

These figures cover linear captures (NIB/NBZ) only. "Header period" is the
share of tracks with an `hdr_period`. "In window" is the share of those
periods inside `lag_window(zone)` for the labelled zone. The last three
columns are shares of FORMATTED tracks that have a period: the cycle is
exact, one sector short, or something else.

> **Flag:** these are the numbers for the current FFT path. A segment-based
> detector for byte-framed captures (`analysis.capture.framed_capture`) is
> pending on another branch. Once it reaches `main`, the same survey must be
> re-run (`--out` to a new directory) and this table updated with both sets of
> numbers.

| scenario | tracks | FORMATTED % | UNFORMATTED % | header period % | in window % | exact % | 1 sector short % | other % |
|---|---|---|---|---|---|---|---|---|
| all linear | 249,571 | 87.6 | 11.2 | 81.6 | 99.3 | 85.5 | 13.6 | 0.5 |
| standard DOS | 158,691 | 100.0 | 0.0 | 99.8 | 100.0 | 99.3 | 0.6 | 0.0 |
| DOS with errors | 23,030 | 98.6 | 1.1 | 77.0 | 99.6 | 78.8 | 20.5 | 0.6 |
| extended, own content | 2,646 | 100.0 | 0.0 | 13.6 | 99.2 | 74.7 | 20.6 | 3.9 |
| extended, copy of lower | 1,660 | 63.3 | 36.4 | 96.6 | 15.2 | 20.3 | 0.5 | 77.6 |
| fat track | 244 | 100.0 | 0.0 | 73.4 | 100.0 | 88.3 | 10.6 | 0.6 |
| killer | 2,812 | 0 (KILLER) | 0.0 | 1.4 | – | – | – | – |
| unformatted / noise | 25,755 | 0.0 | 100.0 | 0.1 | – | – | – | – |
| no-flux fill | 6,320 | 95.7 | 0.0 | 1.2 | – | – | – | – |
| no-sync custom | 1,549 | 100.0 | 0.0 | 0.1 | – | – | – | – |
| long sync | 12,680 | 78.6 | 0.0 | 70.2 | 99.8 | 88.3 | 11.4 | 0.3 |
| short sync | 5,714 | 89.6 | 0.0 | 72.6 | 99.1 | 85.6 | 13.0 | 1.2 |
| extra sectors | 1,548 | 95.5 | 0.7 | 48.8 | 99.9 | 74.8 | 16.7 | 8.2 |
| non-standard density | 5,802 | 100.0 | 0.0 | 35.3 | 59.7 | 49.0 | 8.6 | 41.4 |
| density label ≠ content | 1,709 | 66.6 | 32.7 | 100.0 | 16.3 | 22.1 | 1.7 | 74.7 |
| long track | 769 | 100.0 | 0.0 | 100.0 | 98.7 | 74.0 | 23.7 | 2.3 |
| short track | 1,021 | 100.0 | 0.0 | 100.0 | 15.3 | 15.0 | 0.0 | 82.9 |
| illegal-GCR region | 868 | 100.0 | 0.0 | 51.7 | 100.0 | 90.2 | 9.8 | 0.0 |
| duplicate headers | 2,911 | 77.2 | 22.4 | 78.6 | 41.7 | 6.7 | 2.1 | 52.8 |
| ID mismatch | 370 | 96.8 | 3.2 | 75.1 | 95.7 | 76.1 | 20.2 | 2.6 |
| header track ≠ physical | 331 | 93.0 | 7.0 | 75.2 | 96.8 | 73.6 | 21.5 | 2.9 |
| non-standard data mark | 7,339 | 99.0 | 0.9 | 45.0 | 99.6 | 61.4 | 38.3 | 0.2 |

Findings:

- **One sector short.** The dominant error is the alias one sector short of
  the true period, at 13.6% of all linear tracks. A zone 3 capture of 8 KB
  overlaps itself by only about 4,000 bits. A lag one sector short aligns
  every sector with its successor: the gaps, syncs and fill agree, and only
  the data differs. In that short overlap, such a lag can score as well as
  the true period.
- **Wrong window.** When the density label is wrong, or the track is copied
  from a lower track, the true period lies outside the searched window
  (15–16% in window).
- **Periodic tracks.** No-flux fills and noise have no header period to check
  against. Fills are classed FORMATTED with an arbitrary length.
- **Captures that start after a sync.** The first header of such a capture
  is invisible to a sync-only scan, and at zone 3 that header is the only one
  that repeats. `survey.linear_headers` counts bit 0 as a sync end.

## NIB and G64 of the same disk

There are 6,007 tracks on 163 disks where a NIB and a G64 with the same name sit
together.

- **Agreement** after sync normalisation and alignment: median 1.0, 1%
  quantile 0.713, 0.1% quantile 0.657.
- **Length difference** (G64 length − NIB header period): median 0 bits, 1%
  quantile −344, 0.1% quantile −1147. The G64 was cut from the NIB, sometimes
  with gap or sync compaction.

## Reference: our own captures of a blank-formatted disk

These are 35 tracks, captured once each on a 1571 from sync (31 pages) with
timed syncs restored (`nibbler.Capture.bits`). The disk was formatted by the
same drive. Run with `--captures artifacts/hw1/dev8`.

| zone | tracks | RPM (from `hdr_period`) | `find_cycle` |
|---|---|---|---|
| 3 | 17 | 300.11 – 300.25 | 58.8% exact, 41.2% one sector short |
| 2 | 7 | 300.03 – 300.15 | 100% exact |
| 1 | 6 | 300.14 – 300.20 | 66.7% exact, 33.3% at the upper window edge (one sector long) |
| 0 | 5 | 299.73 – 300.35 | 80% UNFORMATTED, 20% exact |

- **Sync lengths:** 31 / 39 / 44 bits at the 1% / 50% / 99% quantiles. DOS
  writes 40.
- **Sectors:** 670 of 683 standard sectors appear in the captures.
- **Error codes over the capture:** 611 OK; 56 × 24, 12 × 20, 3 × 22, 1 × 27.
- **Error codes on one extracted revolution:** 584 OK; 63 × 24, 20 × 20,
  12 × 23.
- **Error 24 is a capture fault, not a disk fault.**
  - 10.0% of data blocks have invalid GCR inside the payload. On corpus DOS
    tracks the rate is 1.3–1.6%. Only 0.3% have invalid codes confined to the
    off bytes.
  - The first invalid code falls anywhere in the block.
  - In 23 of 76 such blocks, one fix makes the rest of the block valid:
    - a shift of 1–3 bits in 16;
    - a single bad code in 5;
    - a shift of 8–10 bits in 2.

    The other 53 contain several slips.
  - These are bit slips (cells gained or lost) in the byte-ready stream, not
    errors on the disk. A blank-formatted disk has no intentional errors.
  - Retries and merging (`disk.py`) hide them for D64. Raw captures need MC to
    tell a slip from weak media.
- **Revolution detection:** on zone 0, an overlap of ~15,000 bits still yields
  UNFORMATTED. Restored sync lengths differ by up to ±3 bits between
  revolutions, so no single lag agrees over the whole overlap. This is the
  case the segment-based detector addresses.

## Files

- `artifacts/survey/part-*.npz`: per-track rows (`survey.TRACK_DTYPE`), image
  columns `image_*`, `sync_len`/`sync_row`
- `artifacts/survey/items.json`: the listing
- `artifacts/survey/summary.json`: every number above
