# Hardware check

`nybulah.hwcheck` runs every hardware step in one session so a single run
answers everything: per drive it identifies the model, probes for expansion
RAM (`nybulah.ramprobe`), benchmarks the monitor transport at the start of
the first unaliased RAM run (`nybulah.bench`, 8192 bytes, 2 repetitions)
and cross-checks the written pattern through M-R (alias check).

Each step is isolated: on any failure the host releases its IEC lines,
pulses RESET (twice if the drive stays silent) and waits for the drive's
status channel before moving on. Records stream as JSON lines to
`artifacts/hwcheck-<timestamp>.jsonl`; the last line is the summary.

## Build

```sh
docker build --target runtime -t nybulah .
mkdir -p artifacts
```

## S1, both drives powered

```sh
docker run --rm --device=/dev/bus/usb -v "$PWD/artifacts:/data/artifacts" nybulah hwcheck --devs 8 10
```

## S2, one drive powered

S2 strobes ATN, so every other drive must be switched off:

```sh
docker run --rm --device=/dev/bus/usb -v "$PWD/artifacts:/data/artifacts" nybulah hwcheck --devs 10 --s2
```

## S3 (X protocol), both drives powered

S3 needs xum1541 firmware v9 or later and the plugin this image builds by
default (`OPENCBM_SOURCE=git`, OpenCBM branch `xum1541-xfast`). With firmware
v10 it uses burst X ([protocol.md](protocol.md)); with v9 it falls back to
per-byte X. A local OpenCBM tree can be used instead:

```sh
docker build --build-arg OPENCBM_SOURCE=local --build-context opencbm=../OpenCBM --target runtime -t nybulah .
```

It uses only CLK/DATA, so other drives can stay on; with older firmware or the
stock plugin the step is recorded as skipped:

```sh
docker run --rm --device=/dev/bus/usb -v "$PWD/artifacts:/data/artifacts" nybulah hwcheck --devs 8 10 --proto s3
```

## S4 (1571 SRQ fast serial)

S4 needs firmware v11 (below) and a plugin built from the same tree; until the
image's default OpenCBM pin moves, build it from the local branch:

```sh
docker build --build-arg OPENCBM_SOURCE=local --build-context opencbm=../opencbm-srq --target runtime -t nybulah .
```

Only a 1571 runs it (a 1541 is skipped with "s4 needs a 1571"); other drives
stay powered. `--fast` adds a second bench of s3/s4 with the 1571 at 2 MHz:

```sh
docker run --rm --device=/dev/bus/usb -v "$PWD/artifacts:/data/artifacts" nybulah hwcheck --devs 8 10 --proto s3 --proto s4 --fast
docker run --rm --device=/dev/bus/usb nybulah bench --dev 8 --protocol s4 --size 8192 --reps 100
docker run --rm --device=/dev/bus/usb nybulah bench --dev 8 --protocol s4 --size 8192 --reps 100 --fast
```

`bench` addresses `$8000` by default; on a 1571 pass `--addr 0x6000`. Look
for `errors` 0 and `rejects` (block retries) 0. To probe the USB bank
boundaries, repeat with `--size` 31, 32, 33, 63, 64, 65.

## Disk survey (read-only)

With a formatted disk in each drive, `--disk` adds one read-only step per
drive. It captures one track per density zone and reports bytes, sync lengths,
byte period, overrun risk and decoded sectors, plus the RPM measured from the
index on a 1571 (see [disk.md](disk.md)). It never writes:

```sh
docker run --rm --device=/dev/bus/usb -v "$PWD/artifacts:/data/artifacts" nybulah hwcheck --devs 8 10 --disk
```

The image's entrypoint is the `nybulah` command; `bench` and `ramprobe` are
its other subcommands (`nybulah <command> --help`), e.g.
`nybulah bench --dev 10 --protocol s3`.

## Flashing the ZoomFloppy firmware

The firmware hex is built from the same OpenCBM tree as the plugin: commit
`07a95bdf` (branch `xum1541-xfast`) for v10, branch `xum1541-srq` for v11
(SRQ fast serial; also builds v10's protocols):

```sh
git clone https://github.com/anarkiwi/OpenCBM && cd OpenCBM
git checkout xum1541-srq
docker build -f Dockerfile.nybulah --target firmware-hex -o fw .
docker run --rm -v "$PWD/fw:/fw" --entrypoint xum1541cfg nybulah info /fw/xum1541-ZOOMFLOPPY-v11.hex
```

The build steps the compiled timing routines (`misc/x_timing.py`) and checks
the SRQ schedule (`misc/srq_timing_test.c`); it fails rather than produce a
hex that misses them. `info` must print `model 2 version 11` (it exits with
status 1 regardless). Then, with the ZoomFloppy plugged in (drives may stay
connected), flash it; the adapter re-enumerates as a DFU bootloader during the
update, so the container gets the whole USB tree:

```sh
docker run --rm --privileged -v /dev/bus/usb:/dev/bus/usb -v "$PWD/fw:/fw" \
  --entrypoint xum1541cfg nybulah update /fw/xum1541-ZOOMFLOPPY-v11.hex
docker run --rm --privileged -v /dev/bus/usb:/dev/bus/usb --entrypoint xum1541cfg nybulah devinfo
```

`devinfo` should report firmware version 11 (the image's plugin must be v11's,
or it refuses the newer firmware). If `update` reports no devices
found, the adapter may already have re-enumerated as its DFU bootloader before
the tool looked for it; run `update` again. `update` refuses a hex with the
version already installed unless given `-f` (`xum1541cfg -f update ...`). Flashing the stock
`xum1541/xum1541-ZOOMFLOPPY-v08.hex` from the same tree the same way reverts it.

## Expected results

| device | model   | expansion RAM  |
|--------|---------|----------------|
| 8      | 1571    | `$6000-$7FFF`  |
| 10     | 1541-II | `$8000-$9FFF`  |

Transfer rates measured on a ZoomFloppy, both drives powered, 8 KB blocks
(bytes/s). Burst X: 100 blocks each way per column, no data errors and no
checksum retries:

| path | firmware | 1541-II | 1571 1 MHz | 1571 2 MHz |
|------|----------|---------|------------|------------|
| M-R | any | 463 | 461 | |
| S1 read / write | any | 1634 / 1493 | 1669 / 1498 | |
| X read / write | v9 | 9102 / 8278 | 9105 / 8277 | 18176 / 16523 |
| burst X read / write | v10 | 14263 / 18158 | 14271 / 18153 | 28300 / 35877 |
| s4 read / write, predicted | v11 | | 24600 / 22400 | 49300 / 37700 |

`nybulah bench --protocol s3 --fast` (or s4) runs a 1571 at 2 MHz for the
transfer (VIA1 PA5) and returns it to 1 MHz before handing back to DOS; an s4
drive restores PA5 itself on any exit. Burst X
predictions and margins are in [protocol-review.md](protocol-review.md).

## Probe safety

Blocks that decode to I/O are never written:

- 1541: any address with A15=0 and A12 or A11 set (VIA1 `$1800`, VIA2
  `$1C00`, open bus and their mirrors).
- 1571: `$0800-$5FFF` (VIAs, WD1770 `$2000-$3FFF`, CIA `$4000-$5FFF`, and the
  undocumented decode below `$1800`).

Each probe writes a two-byte marker that never equals open-bus reads, then
restores every byte it changed, including base RAM reached through mirrors.

## Drive watchdog

The monitor runs VIA1 timer 1 free-running with its IRQ masked and polls the
flag only while a handshake spins. About one second without a completed byte
inside a command, or ten seconds waiting for the next command, makes the
drive release the bus, restore zero page `$30-$35` and VIA1 (ACR, IER, T1
latches) and return to DOS, as `Q` does; under s4 it also restores the
CIA (CRA, timer A latch), VIA1 PA1/PA5 and clears the CIA's flags. The host restarts the monitor
transparently when a command follows a longer pause. A 1571 running at 2 MHz
halves both windows; pass `idle_s=WATCHDOG_IDLE_S / 2 * 0.9` to `Monitor`
there.
