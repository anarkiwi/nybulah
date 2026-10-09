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
  `TrackDecode`. It tries every sync at any bit alignment. Each sector gets the
  best read found and its error code, in D64 error-byte terms.
- `merge_decodes(a, b)` keeps the better read of each sector across two decodes
  of one track. `header_tracks(bits)` lists the track numbers found in valid
  headers.

## Captures (`nybulah.analysis.capture`)

`capture_bits(data, positions, runs, lead=0)` turns a byte-ready capture back
into a bit stream. It inserts the sync ones that were never latched, so each
sync's run (the latched trailing ones plus the inserted ones) matches its
measured length. `trailing_ones(bits, ends)` counts the latched part.

## Revolution detection (`nybulah.analysis.cycle`)

`find_cycle(bits, zone, period=None, index_aligned=False, tolerance=None, alpha=1e-3)`
returns a `Cycle(kind, start, length, match, z)`.

1. If sync bits make up most of the capture, it is classed `KILLER`.
2. The candidate lags are limited to what is physically possible:
   `bit_rate(zone) × period × (1 ± tolerance)`. Without a hint, `period` is
   0.2 s and the tolerance is ±5%. When the period was measured with the 1571
   index sensor, the tolerance is ±2%.
3. One FFT autocorrelation scores every lag by its bit agreement above chance,
   p² + (1−p)². A track is `FORMATTED` only if both of these hold:
   - the best lag is significant at family-wise level `alpha`, using a
     Bonferroni correction over the searched lags;
   - most of the overlapping bits repeat.

   Anything else is `UNFORMATTED`.
4. The start is the sync before a valid sector-0 header, otherwise the longest
   sync. When `index_aligned=True`, the start is bit 0 of the capture.

`extract_revolution(bits, cycle)` cuts out one revolution.
`index_align({key: (bits, cycle)})` gives every track a start at its index pulse.

Detection needs captures longer than one revolution, and the overlap is what it
measures. Weak bits that fall inside a short overlap can make a formatted track
look unformatted, so captures should overlap by as much as drive RAM allows.

## Formats (`nybulah.formats`)

| Format | Read / write | Notes |
|---|---|---|
| D64 | `read_d64` / `write_d64` | 35, 40 or 42 tracks, with or without error bytes |
| G64 | `read_g64` / `write_g64` | v0; half tracks; per-track or per-byte speed zones |
| NIB / NB2 | `read_nib(buf, nb2=False)` / `write_nib` | Density flags are kept; entries come from the header table |

Conversions (`nybulah.formats.convert`), with tqdm progress:

- `nib_to_g64(image, period=None, index_aligned=False)` trims each track to one
  revolution. For NB2 it keeps the pass with the fewest sector errors, then the
  strongest match.
- `g64_to_d64(image)` decodes sectors and writes error bytes.
- `d64_to_g64(image)` writes standard formatting.

`nybulah.analysis.synth.simulate_capture` generates multi-revolution captures
with a chosen start, bit noise and weak regions, for tests and tools.
