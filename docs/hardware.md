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
default (`OPENCBM_SOURCE=git`, pinned to OpenCBM branch `xum1541-stream`). With firmware
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

S4 needs firmware v11 or later (below) and a plugin from the same tree; the
image's default plugin is v12's.

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

### s4 timing probes

`tools/xprobe.py` is not installed in the image; mount it. `--cia` runs
`drive/ciaprobe.s` under s3 (its SRQ pulses would confuse an s4 reply) and
prints, per timer phase, the first cycle after an SDR write at which ICR
shows the byte done (`first`) and the full hit table; s4's send period must
exceed both (protocol.md, 1571 SRQ). `--sweep` times reads of 64..8192 bytes
on the host clock and fits per-byte and per-block costs:

```sh
docker run --rm --device=/dev/bus/usb -v "$PWD/tools:/tools" --entrypoint python3 nybulah /tools/xprobe.py --dev 8 --cia
docker run --rm --device=/dev/bus/usb -v "$PWD/tools:/tools" --entrypoint python3 nybulah /tools/xprobe.py --dev 8 --protocol s4 --base 0x6000 --sweep
docker run --rm --device=/dev/bus/usb -v "$PWD/tools:/tools" --entrypoint python3 nybulah /tools/xprobe.py --dev 8 --protocol s4 --base 0x6000 --sweep --fast
```

Expected: `first` between 33 and 39 for both phases; `per_byte_us` near
42.8 (1 MHz) and 21.5 (2 MHz), `per_block_us` near 840.

## Disk survey (read-only)

With a formatted disk in each drive, `--disk` adds one read-only step per
drive. It captures one track per density zone and reports bytes, sync lengths,
byte period, overrun risk and decoded sectors, plus the RPM measured from the
index on a 1571 (see [disk.md](disk.md)). It never writes:

```sh
docker run --rm --device=/dev/bus/usb -v "$PWD/artifacts:/data/artifacts" nybulah hwcheck --devs 8 10 --disk
```

Every capture of the step is saved next to the log, in
`hwcheck-<time>/dev<N>/` (`locate`, `survey` and, on a 1571, `index` records),
for `Capture.load` and `ramcheck --reference`.

## RAM capture check

`nybulah ramcheck` homes a 1571 through `Nibbler.home` (within `--max-steps`
outward steps, never bumping) or locates a 1541 from its headers, then per
`--halftracks` entry streams one revolution (s4, firmware v12) and takes
`--repeats` RAM captures (BITS, TB and TS passes, started as `--start`, after
`--settle-ms`). Each RAM capture is compared with the stream and any
`--reference` records of the same halftrack, sector by sector from the header:
`differ` counts bytes the drive latched differently, `extra_syncs` the syncs
(byte offset, bits) the merge found where the reference has none over the same
bytes. Every capture is saved under `--save`.

```sh
docker run --rm --device=/dev/bus/usb -v "$PWD/artifacts:/data/artifacts" nybulah ramcheck --dev 8 --max-steps N --halftracks 36 50 --save /data/artifacts/ramcheck
```

The image's entrypoint is the `nybulah` command; `bench` and `ramprobe` are
its other subcommands (`nybulah <command> --help`), e.g.
`nybulah bench --dev 10 --protocol s3`.

## 1571 head homing probe

`nybulah homeprobe` reads the 1571's track 00 sensor (VIA1 PA0, 16 raw reads
and DOS's debounced test), the stepper phase and DOS's track, and prints the
homing plan as JSON. Without `--step` it never steps. What to run, in order,
with drive 8 the 1571 and a formatted disk inserted:

1. Dry run; the head does not step (`--headers` spins the disk to read them):

   ```sh
   docker run --rm --device=/dev/bus/usb nybulah homeprobe --dev 8 --headers
   ```

   Expect `sensed` false and `pa0` all 1 unless the head is within
   halftracks 2-5;
   `phase` = (2 × `dos_track` + 2) & 3 when DOS left the head on its track;
   `estimate` = 2 × the header (or DOS) track; `outward_steps` =
   `estimate` - 2, `inward_steps` 0 (7 when sensed).

2. Step, capped at the plan from step 1 (it refuses a larger one):

   ```sh
   docker run --rm --device=/dev/bus/usb nybulah homeprobe --dev 8 --headers --step --max-steps N
   ```

   with N the dry run's `outward_steps` (34 from track 18). Expect `trace`
   to end `[2, true, 0]`, every phase = (halftrack + 2) & 3, the sensor clear
   above `sensor_edge` and on from it down, and `sensor_edge` 2-5. The head
   returns to the estimate, and `$22` names it. A `TrackError` means a
   reading contradicted the position and the head stayed where it was read;
   every outward step started from halftrack 3 or more by the estimate.

## 1571 streaming (firmware v12)

Measured on drive 8 (1571, 2 MHz), firmware v12:

| probe | result |
|---|---|
| `xprobe.py --cia` | CIA flag 34 cycles after SDR on both timer phases |
| `xprobe.py --sweep --fast` | 21.40 µs per byte, 1.08 ms per block |
| `streamprobe --halftrack 36` | adapter and drive `done`; 2 index edges, 6982 bytes per revolution; 76 syncs; 19/19 sectors |
| `streamprobe --halftrack 2 --revolutions 3` | adapter and drive `done`; 4 index edges, 7522 bytes per revolution each; 164 syncs; 21/21 sectors |
| `read --transport s4` (D64) | 683 sectors, 0 errors, one capture per track, 25.5 s; identical to the 1541-II's RAM-path read of the same disk |

Streaming needs firmware v12 and the plugin from the same tree (branch
`xum1541-stream`, the image's default).

Flash `xum1541-ZOOMFLOPPY-v12.hex` as below (`info` must print
`model 2 version 12`, `devinfo` firmware version 12). Then, in order, with
drive 8 the 1571 and a formatted disk inserted:

1. Memory only, no head movement: the s4 benches and timing probes above
   (`bench --protocol s4 --addr 0x6000`, with and without `--fast`;
   `xprobe.py --cia` and `--sweep`). Expect the s4 rows of the rates table below.
2. Homing dry run (`homeprobe --dev 8 --transport s4 --headers`, no steps).
3. One track, one revolution, homed through `Nibbler.home` within the dry
   run's `outward_steps` (N); the probe refuses a larger plan and never
   bumps:

   ```sh
   docker run --rm --device=/dev/bus/usb -v "$PWD/artifacts:/data/artifacts" nybulah streamprobe --dev 8 --headers --max-steps N --halftrack 36 --save /data/artifacts/stream-36.npz
   ```

   Expect `adapter` and `drive` "done", 2 `index` positions about 6980
   bytes apart (`revolution_bytes`, zone 2; sync bits are not bytes) and `syncs` near 38 (19 sectors).
4. Zone 3 (the tightest byte period) and several revolutions:

   ```sh
   docker run --rm --device=/dev/bus/usb -v "$PWD/artifacts:/data/artifacts" nybulah streamprobe --dev 8 --max-steps N --halftrack 2 --revolutions 3 --save /data/artifacts/stream-2.npz
   ```

   (N again from the plan; 34 with the head left on track 18). Expect about
   7690 bytes per revolution and 4 `index` positions.
   `adapter` "overrun" means the host did not drain the stream in time;
   "framing" or "timeout" a drive or line fault (the drive stops on ATN).
5. A whole disk without expansion RAM; every track streams on a 1571 with
   v12 (`.d71` reads both sides):

   ```sh
   docker run --rm --device=/dev/bus/usb -v "$PWD/artifacts:/data/artifacts" nybulah read --dev 8 --transport s4 /data/artifacts/disk.d64
   ```

## Flashing the ZoomFloppy firmware

The firmware hex is built from the same OpenCBM tree as the plugin: commit
`07a95bdf` (branch `xum1541-xfast`) for v10, commit `89920a0d`
(branch `xum1541-srq`) for v11
(SRQ fast serial; also builds v10's protocols), branch `xum1541-stream` for
v12 (streaming; also builds v11's):

```sh
git clone https://github.com/anarkiwi/OpenCBM && cd OpenCBM
git checkout xum1541-stream
docker build -f Dockerfile.nybulah --target firmware-hex -o fw .
docker run --rm -v "$PWD/fw:/fw" --entrypoint xum1541cfg nybulah info /fw/xum1541-ZOOMFLOPPY-v12.hex
```

The build steps the compiled timing routines (`misc/x_timing.py`) and checks
the SRQ schedule (`misc/srq_timing_test.c`); it fails rather than produce a
hex that misses them. `info` must print `model 2 version 12` (it exits with
status 1 regardless). Then, with the ZoomFloppy plugged in (drives may stay
connected), flash it; the adapter re-enumerates as a DFU bootloader during the
update, so the container gets the whole USB tree:

```sh
docker run --rm --privileged -v /dev/bus/usb:/dev/bus/usb -v "$PWD/fw:/fw" \
  --entrypoint xum1541cfg nybulah update /fw/xum1541-ZOOMFLOPPY-v12.hex
docker run --rm --privileged -v /dev/bus/usb:/dev/bus/usb --entrypoint xum1541cfg nybulah devinfo
```

`devinfo` should report firmware version 12 (the image's plugin must be at
least as new as the firmware, or it refuses it). If `update` reports no devices
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
(bytes/s). Burst X and s4: 100 blocks each way per column, no data errors and
no checksum retries; s4 also at 1–4096 bytes across the USB bank boundaries:

| path | firmware | 1541-II | 1571 1 MHz | 1571 2 MHz |
|------|----------|---------|------------|------------|
| M-R | any | 463 | 461 | |
| S1 read / write | any | 1634 / 1493 | 1669 / 1498 | |
| X read / write | v9 | 9102 / 8278 | 9105 / 8277 | 18176 / 16523 |
| burst X read / write | v10 | 14263 / 18158 | 14271 / 18153 | 28300 / 35877 |
| s4 read / write | v12 | | 23420 / 22261 | 46372 / 37052 |
| s4 read / write, simulated | v12 | | 23600 / 22400 | 47200 / 37700 |

`nybulah bench --protocol s3 --fast` (or s4) runs a 1571 at 2 MHz for the
transfer (VIA1 PA5) and returns it to 1 MHz before handing back to DOS; an s4
drive restores PA5 itself on any exit. Burst X and s4
predictions and margins are in [protocol.md](protocol.md).

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
