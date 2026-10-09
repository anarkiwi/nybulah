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
  16 MHz clocks, with one revolution normalised to 300 rpm.

The other functions work on a `DiskImage`:

- `info(image)` summarises each track: cycle kind, length, z, zone and sector
  errors.
- `to_g64`, `to_d64` and `to_p64` convert it.

From the command line:

- `nybulah convert in out` converts any readable format to `.g64`, `.g71`,
  `.d64` or `.p64`.
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
