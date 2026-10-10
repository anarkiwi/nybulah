# Features

## Transports

Drive-resident 6502 code moves data over the stock S1/S2 protocols or, with
the modified xum1541 firmware, over X and SRQ with a 16-bit block check and
retry ([protocol.md](protocol.md)):

| transport | lines | firmware | drives |
|---|---|---|---|
| s1, s2 | stock serial (s2 strobes ATN: one drive on the bus) | any | 1541, 1571 |
| s3 | X: CLK/DATA only; burst X (one handshake per 64 bytes) with v10 | v9, v10 | 1541, 1571 |
| s4 | 1571 CIA / 1581 8520 shift register on SRQ | v11 (v12 streams) | 1571, 1581 |

Watchdogs on the drive, in the firmware and on the host return everything to
a usable state after a stall, without power cycling. Other drives can stay
powered on the bus except under s2. Transfer rates per transport and drive
are in [hardware.md](hardware.md#expected-results).

## Raw capture

On a 1541, and for writes and RAM capture passes on a 1571, the drive
captures a track into an 8 KB RAM expansion (found automatically) and the
host fetches it. A 1571 with firmware v12 streams whole revolutions in real
time through s4 with no RAM expansion, so D64/D71 reads need none
([disk.md](disk.md#capture-passes)).

## Telemetry

Sync lengths come from per-byte arrival times, bounded per sync; no byte is
lost for any sync length. Revolution time comes from the 1571 index sensor
or from the track's own repetition ([disk.md](disk.md#capture-passes)).

## Head location

A 1541 is located from sector headers or DOS's track; bumping needs
`--allow-bump`. A 1571 is homed on its track 00 sensor by the DOS rule and
is never bumped ([disk.md](disk.md#head-location)). A 1581 is homed by a WD
Restore bounded to the estimated cylinder and confirmed by TR00
([protocol.md](protocol.md#homing-and-seeks)).

## Disk operations

Read D64 on both drives and D71 on a 1571, with error bytes and retries that
merge the best read of each sector. Write D64/D71, verifying every track by
re-capture. `--archive` keeps every raw capture so images can be re-derived
([disk.md](disk.md#disk-operations)).

## Analysis

Vectorised GCR codec; sector decode with D64 error codes; revolution
detection with a significance test (segment shifts checked against sector
headers for byte-ready captures, FFT autocorrelation for continuous streams);
killer and unformatted track classes; index alignment; GCR fault
classification ([analysis.md](analysis.md)). Track scenarios found in
preserved images, and how each is handled, are in
[scenarios.md](scenarios.md).

## Disk map

Every revolution of every track is classified against clean-DOS statistics,
with stable, weak and capture-fault regions told apart
([analysis.md](analysis.md#disk-map)).

![Disk map of a synthetic disk](img/diskmap.apng)

A synthetic disk (`tools/diskmap_example.py`) over four revolutions: grey is
standard DOS content, hatched regions change between revolutions, outlines
are capture faults ([static view](img/diskmap.png)).

## Flux view

The disk as an image of its flux: transitions per cell as lightness, cell
length as hue, revolution-to-revolution variance as lost colour, inferred
no-flux as dots; interval histograms, timing eye and drift; per-revolution
APNG and a zoomable HTML viewer down to single transitions
([analysis.md](analysis.md#flux-view)).

![Flux view of a synthetic disk](img/fluxview.png)

A synthetic flux image (`tools/diskmap_example.py`): a speed wobble on track
1, a long sync, a no-flux gap, a half-written faster zone (blue), a killer
track (white), crosstalk on half track 20.5.

## 1581

ID lists with timing, sector reads with status, Read Track, and Write
Track/Write Sector, streamed through the 8520 shift register (s4, firmware
v12) or through the track cache RAM. An MFM layer decodes marks, CRCs and
layouts and maps CRC errors, deleted data, odd sizes, duplicate or missing
IDs and unstable bytes
([protocol.md](protocol.md#1581-capture-and-streaming-drivemfms-drivemfmstreams),
[analysis.md](analysis.md#mfm-tracks-nybulahanalysismfm),
[hardware.md](hardware.md#1581)).

## Formats

Read and write NIB, NB2, NBZ, G64, G71, P64, SCP, KryoFlux, D64 and D81;
1581 captures also export to IMD. Flux is decoded through a 1541
read-circuit model. Conversion to G64, G71, D64 and P64 keeps every track
([formats.md](formats.md)).

## Hardware probes

`hwcheck` (identify drives, probe RAM, bench transports), `homeprobe` (1571
track 00 sensor and homing plan), `streamprobe` (one streamed track),
`ramcheck` (RAM captures against a stream), `ramprobe` and `bench`
([hardware.md](hardware.md)). `nybulah <command> --help` lists the options.
