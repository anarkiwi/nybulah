# Image formats

`nybulah.formats.load(path)` (or `loads(bytes, name)`) sniffs the format and
returns a `DiskImage`. Its `tracks` map a key to a list of `Capture`s. The key
is the halftrack (2 = track 1), with `SIDE1` (0x80) set for the second side.
Each `Capture` holds:

- `bits`: a 0/1 array;
- `zone`: the density it was read at;
- `circular`: the bits are exactly one revolution;
- `index`: bit offsets of index pulses, so multi-revolution captures keep
  their revolution boundaries;
- for flux sources, `flux` and `flux_index`: transition and index times in
  16 MHz clocks, with one revolution normalised to 300 rpm;
- for NIB, NB2 and NBZ, `framed`: the byte-ready capture from
  `analysis.capture.framed_capture`. In this case `bits` is its restored
  stream.

Cycles are found per segment for NIB-family captures and from the continuous
bit stream for flux, P64 and G64 sources (see [analysis](analysis.md)).

The other functions work on a `DiskImage`:

- `info(image)` summarises each track: cycle kind, length, z, zone and sector
  errors.
- `to_g64`, `to_d64` and `to_p64` convert it. No track is dropped. A track
  with no repeating revolution is written as its capture cut to the nominal
  length, and its key is appended to the optional `unformatted` list.

From the command line:

- `nybulah convert in out` converts any readable format to `.g64`, `.g71`,
  `.d64` or `.p64`. Its output lists those tracks under `unformatted`.
- `nybulah info file` prints the per-track summary.

| Format | Read | Write | Keeps | Loses |
|---|---|---|---|---|
| NIB | yes | yes | multi-revolution captures, density flags | timing, weak-bit statistics |
| NB2 | yes | yes | 4 passes × 4 densities | as NIB; only the header-density passes are loaded |
| NBZ | yes | yes | as NIB/NB2 (lossless compression) | — |
| G64 v0 | yes | yes | one revolution per halftrack, per-track or per-byte speed, SPS `EXT` records (splice, write area, bit cell, fill, format code) | sub-bit timing, weak bits |
| G71 (`GCR-1571`) | yes | yes | as G64, halftracks 85–168 = side 1 | as G64 |
| P64 | yes | yes | one revolution of pulse positions (16 MHz clocks) and strengths (weak pulses) | more than one revolution |
| SCP | yes | yes | every revolution of flux, index times, WRSP write splices | — |
| KryoFlux stream | yes | yes | every revolution of flux, index sample positions | — |
| D64 | yes | yes | sector data, error bytes | everything below the sector level |
| D81 | yes | yes | 1581 sector data, error bytes | everything below the sector level |
| IMD | yes | yes | per-track sector IDs (order, C/H maps), sizes, deleted marks, data errors | gaps, ID CRC errors, data without ID |
| 1581 capture npz | yes | yes | Read Track revolutions, Read Address ID lists, times, index edges | sub-byte timing |

## 1581 MFM images

`nybulah.formats.mfmcap.load_disk(path)` loads D81, IMD and 1581 capture npz
files, or a directory of capture npz files, into an `MfmDisk`. It returns None
for other files. `MfmDisk.tracks` maps `(cylinder, physical head)` to
`analysis.mfm.MfmTrack` decodes of Read Track output or media. `MfmDisk.ids`
holds the Read Address lists. `nybulah info`, `info --map`, `map` and
`convert` (to `.d81`, `.imd` or `.npz`) accept these files.

**D81** (`formats.d81`). The image is 80 tracks × 40 sectors × 256 bytes,
in (track, sector) order, with 3200 optional error bytes. `D81.info()` reads:

- the header 40/0 (`newdsk.src`): directory link at 0, format byte at 2, name
  at 4–19, ID at 22–23, DOS version at 25;
- the BAM 40/1–2 (`mapit.src`): 40 tracks per block, 6 bytes per track from
  offset 16 (free count, then a bitmap with 1 = free).

`to_tracks` formats every side with `analysis.mfm.standard_layout`. The two
halves of a physical sector take the more severe error byte. `from_decodes`
keeps the best read of each physical sector over all revolutions.

**IMD** (`formats.imd`). Written from chapter 6 of Dave Dunfield's ImageDisk
`IMD.TXT` (1.18): the ASCII header ends with $1A; each track then has mode,
cylinder, head (bit 7: cylinder map, bit 6: head map), count and size code; the
sector numbering map; the optional maps; and records 0–8 (unavailable, normal,
compressed, deleted and data-error variants). The suggested size code $FF
(a table of 16-bit sizes) handles mixed sizes. Tracks are mode 5 (250 kbps
MFM). The head byte is the physical head; a 1581 writes H = 1 − head, so every
1581 track carries a head map. Export keeps each good ID's best read in
rotational order. Sectors with ID CRC errors are left out, as ImageDisk cannot
read them. Import encodes the sectors with 1581 gaps; gap 3 shrinks, down to
the WD1772 minimum of 2 bytes, when they overflow 6250 bytes.

**Capture record** (`formats.mfmcap.MfmCapture`, npz version 1):

| field | meaning |
|---|---|
| `kind` | `track` (Read Track) or `ids` (Read Address) |
| `cylinder`, `head`, `side_select` | physical head = 1 − PA0; `side_select` is PA0 |
| `data`, `rev_offsets` | Read Track bytes of every revolution, cut by offsets |
| `rev_start_us`, `rev_end_us`, `rev_status` | per revolution: times and WD status |
| `ids`, `id_status`, `id_us` | per ID: six bytes, WD status, time |
| `index_us` | measured index edges |
| `meta` | JSON: adapter/drive end, rpm, firmware, notes |

`save_captures(path, captures)` writes any number of records into one npz,
tagged `nybulah_mfm` = (version, count). `load_captures` reads such an npz, or
every one in a directory.

## Flux to bits

`nybulah.analysis.flux.decode_flux` decodes flux the way the 1541 read
circuit does:

- On each flux transition, the circuit reloads the 16 MHz divider (16 − zone)
  and clears the bit-cell counter that the divider drives.
- On the 2nd, 6th, 10th and 14th divider carry after a transition, it shifts
  out a bit. The first of these is 1; the rest are 0.
- When the bit-cell counter wraps, the circuit outputs a 1 without a
  transition. This is how a 1541 reads more than three zeros in a row.

As a result, an interval of *n* cells decodes as *n* bits whenever it is
within half a cell of nominal. Transition-timing errors do not carry over to
the next interval.

The zone of a flux track is the one whose cell size best fits the intervals,
by least squares. The halftrack numbering of SCP and KryoFlux tracks is also
inferred:

- The candidate layouts are `cylinders`, `half-cylinders` and `halftracks`.
- The loader picks the layout that agrees with the track numbers in decoded
  sector headers.
- `--layout` overrides it.

P64 weak pulses fire with probability strength / 2³², so such tracks are read
as several revolutions. `track_from_bits(bits, weak=…)` writes chosen bit
spans as weak pulses.

## Licences

- **BCL LZ77 (NBZ).** BCL is by Marcus Geelnard, zlib/libpng licence (see the
  `lz.c` header in BCL 1.2.0 on SourceForge). No maintained Python binding
  exists; the PyPI package `bcl` is an unrelated cryptography library. The
  codec was written from the BCL stream format. Its output decodes with BCL,
  and BCL's output decodes here. No nibtools (GPL) code was used.
- **IMD.** Implemented from the format chapter of `IMD.TXT` (ImageDisk 1.18,
  Dave Dunfield); no ImageDisk code was used.
- **P64.** Implemented from the specification in the VICE manual. The
  reference `p64.c` by Benjamin Rosseaux is under the zlib licence. The writer
  produces byte-identical output to it.
- **SCP and KryoFlux.** The `greaseweazle` package is public domain
  (Unlicense), but:
  - it is not on PyPI;
  - it needs a C toolchain to build;
  - it pulls in serial and HTTP dependencies;
  - it renumbers C64 tracks with heuristics and prints to stdout;
  - it decodes flux with per-sample Python loops.

  So nybulah parses both formats itself, vectorised and with numba, from the
  public format descriptions.
