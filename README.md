# nybulah

Raw-track ("nibbler") imaging and writing for Commodore 1541 and 1571 disk
drives over the standard serial IEC bus. It uses OpenCBM with a ZoomFloppy or
another xum1541 adapter. You don't need a parallel cable; the drive needs an
8 KB RAM expansion.

It is for preserving copy-protected and non-standard disks, and for writing
them back.

> Status: early development. The transport, the drive code and the analysis
> are tested against a cycle-stepped 1541/1571 simulator. Testing on real
> hardware is in progress.

## Features

- **Serial-only raw capture.** The drive captures each track into its RAM
  expansion, which is detected automatically. The host then fetches the track
  over IEC. Other drives can stay powered on the bus.
- **Fast, recoverable transport.** Drive-resident 6502 code works with the
  stock S1/S2 protocols. With the modified xum1541 firmware it uses the X
  protocol: CLK/DATA only, two bits per edge, and a 16-bit block check with
  retry. Watchdogs on the drive, in the firmware and on the host return
  everything to a usable state after a stall, with no power cycling.
- **Analysis on the host:**
  - vectorised GCR codec;
  - sector decode with D64 error codes;
  - revolution detection by FFT autocorrelation, with a significance test over
    the physically possible track lengths;
  - killer and unformatted track classification;
  - index alignment.
- **Formats:** G64, NIB, NB2, D64 and D71, with conversion between them. Error
  bytes are supported.
- **Disk operations:** read and write D64 on the 1541 and 1571, and D71 on the
  1571, through the fast transport. Every write is verified by re-capture.
- **Telemetry:** sync lengths are timed from the drive's SYNC line. On a stock
  1571 the index sensor gives index-aligned captures and a measured RPM.
  Every capture can be archived with its telemetry, so images can be
  re-derived later.

## Compared with existing tools

| | nybulah | nibtools | OpenCBM d64copy/cbmcopy | Flux boards (KryoFlux, Greaseweazle, SCP) |
|---|---|---|---|---|
| Raw tracks from a 1541 | serial IEC + 8 KB RAM expansion | parallel cable required | no (sectors only) | flux, with a PC drive or modified hardware |
| Raw tracks from a 1571 | serial IEC + 8 KB RAM expansion | SRQ or parallel | no | flux |
| Other drives powered on the bus | yes | depends on transport | with S1 or original transfer | n/a |
| Recovers from stalls without power cycling | yes (firmware v9) | no | no | n/a |
| Exact sync lengths | timed from the SYNC line | no | no | yes |
| Index alignment and RPM on a stock 1571 | WD1770 index sensor | needs an SC+-style sensor mod | no | yes |
| Revolution detection | bit-level FFT autocorrelation with a significance test | byte matching within a fixed window | n/a | tool-dependent |
| Licence | Apache-2.0 | GPL-3.0 | GPL-2.0 | various |

## Requirements

- A ZoomFloppy or another xum1541 adapter. Firmware v9 from
  [anarkiwi/OpenCBM](https://github.com/anarkiwi/OpenCBM/tree/xum1541-timeouts)
  is needed for the X protocol and for stall recovery; stock firmware works
  with S1/S2.
- A 1541 or 1571 with an 8 KB RAM expansion, which is a drive modification.
  It holds a little more than one revolution of any track, and nybulah finds
  it automatically. Stock drives work only with M-R/M-W and sector-level
  tools.
- Docker. The image bundles OpenCBM, the assembled drive code and Python.

## Usage

```sh
docker build --target runtime -t nybulah .
alias nybulah='docker run --rm --device=/dev/bus/usb -v "$PWD:/data" nybulah'

nybulah hwcheck --devs 8 10        # identify drives, probe RAM, benchmark transport
nybulah read --dev 10 disk.d64     # 1541: read with error bytes
nybulah read --dev 8 disk.d71      # 1571: double-sided
nybulah write --dev 8 disk.d71     # encode, write, verify
```

`nybulah <command> --help` lists the options, for example `--transport
s1|s2|s3` and `--retries`.

## Documentation

- [docs/hardware.md](docs/hardware.md): hardware setup, firmware flashing and
  the hardware check
- [docs/protocol.md](docs/protocol.md): X wire protocol and timing
- [docs/disk.md](docs/disk.md): D64/D71 reading and writing
- [docs/analysis.md](docs/analysis.md): GCR, revolution detection and format
  APIs

## Development

```sh
docker build --target test -t nybulah:test .
docker run --rm -v "$PWD:/app" -w /app nybulah:test python -m pytest -n auto
```

The drive code is ca65 assembly in `drive/`. It is assembled in the Docker
build.

## Licence

Apache-2.0. nybulah contains no nibtools or OpenCBM code. The modified
firmware lives in its own GPL-2.0 repository.
