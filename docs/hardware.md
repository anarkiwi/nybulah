# Hardware check

`nybulah.hwcheck` runs every hardware step in one session so a single run
answers everything: per drive it identifies the model, probes for expansion
RAM (`nybulah.ramprobe`), benchmarks the monitor transport at the start of
the first unaliased RAM run (`nybulah.bench`, 8192 bytes, 2 repetitions)
and cross-checks the written pattern through M-R (alias check).

Each step is isolated: on any failure the host releases its IEC lines,
pulses RESET (twice if the drive stays silent) and waits for the drive's
status channel with the readiness waits of [bus sessions](#bus-sessions)
before moving on. Records stream as JSON lines to
`artifacts/hwcheck-<timestamp>.jsonl`; the last line is the summary.

## Bus sessions

All multi-step hardware work goes through `nybulah bus` or one nybulah
process (`hwcheck`, `read`, ...), never a chain of containers such as one
`docker run ... cbmctrl` per step with `sleep`s between them. Each container
opens and closes the adapter, a sleep only guesses when a drive is ready, and
a killed container leaves the adapter mid-transaction.

`nybulah bus` opens the adapter once and runs its steps in order:

| step | does |
|---|---|
| `reset` | pulse RESET, watch until no drive holds CLK or DATA |
| `wait DEV...` | wait until each drive's DOS answers its error channel |
| `status DEV...` | read the error channel |
| `command DEV "CMD"` | send a DOS command; done when its status reads back |
| `dir DEV` | list the directory |
| `identify DEV...` | model (`ramprobe.identify_model`) |
| `detect` | model of every drive answering on 8-30 |

Steps come from `--script FILE` (one a line, `#` comments) and then the
command line. `--reset-first` starts with `reset`; the end check
(`--no-end-check` turns it off) reads the status of every drive the script
touched. Each step prints one JSON line (`step`, `dev`, `result`, `status`,
`seconds`, and `files`, `model` or `devices`); a summary with every drive's
final status ends the run, and the exit status is 1 unless all steps
succeeded. A DOS status of 20 or more (other than 73) is a `dos-error`.

Examples:

```sh
# drive check after a power cycle
nybulah bus --reset-first "wait 8 9 10" "identify 8 9 10"
# 1571 mode, then a directory listing
nybulah bus 'command 8 "U0>M1"' "dir 8"
# reset and wait on all drives
nybulah bus reset "wait 8 9 10"
```

### Settle detection

The tool watches the bus settle instead of guessing how long a boot takes.

- **Lines.** During RESET and the power-on diagnostic, a 1541's or 1571's
  VIA1 port B is an input and its 7406 inverters assert CLK and DATA (the
  OpenCBM xum1541 firmware, `xum1541/iec.c` `iec_reset`, sees a 1541 grab DATA
  25 ms after RESET). After a reset the host releases its lines and waits,
  asserting nothing, until no device holds CLK or DATA. It waits on the adapter
  (`cbm_iec_wait`, bounded by the adapter's I/O timeout) rather than polling,
  and samples the lines after each wait; every change of line state goes into
  the reset's timeline.
- **Quiet window.** The stock ROMs release the lines once and do not take
  them back before idle: the 1541 and 1571 release them when `dskint` sets up
  the serial port after the diagnostic (`pb` 0, `ddrb1`), and the 1581 sets
  its port before its diagnostic with CLK and DATA released (`init_prt_pb`
  `%11010101`, `iodef.src`). So the derived quiet window is zero: the first
  sample with both lines released means the bus has settled. JiffyDOS's source
  is not published, so its boot cannot be checked. Instead the tool times
  every release a drive takes back (released, then held again) and from then
  on calls the bus settled only after the lines have stayed released for the
  longest such gap. A drive that takes the lines back for the first time after
  the tool has started a transaction cannot be foreseen; the timeline shows it.
- **Readiness proof.** Each drive is then confirmed by its error channel. A
  busy DOS acknowledges ATN in hardware (ATNA) but serves it only from its
  idle loop: `atnirq` only sets `atnpnd` and `idle` calls `atnsrv`, while
  `watjob` does not (1541 `seratn.src`, `idlesf.src`, `jobssf.src`); the 1581's
  IRQ likewise only flags ATN (`irq.src`). The adapter waits on that
  transaction, and its return is the proof. The lines are sampled again
  before every transaction; none starts while one is held.
- **Never abandon a transaction.** Limits only stop the tool from starting a
  new transaction. Each transaction gets the adapter I/O timeout its kind can
  legitimately need: a readiness probe the outer limit (below); a DOS command
  and its status `--command-seconds` (the adapter default,
  `XUM1541_IO_TIMEOUT_MS`, 30 s; validating a full disk can take longer); the
  status after `UJ` or `U:` the outer limit. Only a drive past those bounds is
  given up on.
- **Pairing.** Status reads (`cbm_device_status`: TALK 15, read, UNTALK) and
  commands (`cbm_exec_command`: LISTEN 15, write, UNLISTEN) are paired inside
  libopencbm; `dir` pairs OPEN with CLOSE and TALK with UNTALK in `finally`
  blocks.

### Outer limit

The outer limit only triggers a report. It is the longest boot derived from
the ROMs: the adapter's RESET hold (100 ms, `iec_reset`), the slowest DOS
power-on diagnostic, and a stock 1581's boot file search, 28.64 s in all
(`bus.OUTER_S`; `--boot-seconds` replaces it). If CLK or DATA is still held
then, the tool does nothing more to the bus: no ATN, no reset, no release. It
reports the line history (the transitions seen, which lines are held and for
how long) and that a drive is holding the bus and needs a power cycle, and
ends the run. A host that gives up mid-boot and then acts on the bus can hang
a drive, so giving up never means going on.

The diagnostic is the same code in each ROM (1541 `dskintsf.src` in
`DOS_1541_05`, 1571 `dskintsf.src`, 1581 `dskint.src`): a zero page count test
(730 880 cycles), a ROM checksum (2 569 cycles a page) and a RAM pattern test
(15 378 cycles a page), counted from each loop's instruction cycles
(`bus.diagnostic_cycles`):

| model | ROM pages | RAM pages | clock | diagnostic |
|---|---|---|---|---|
| 1541 | 64 | 7 | 1 MHz | 1.003 s |
| 1571 | 128 | 7 | at most 1 MHz | 1.167 s |
| 1581 | 128 | 31 | 2 MHz | 0.768 s |

The 1571 switches to 1 MHz after its diagnostic (`ptch29`), so 1 MHz bounds
it. The 1541 figure fits the "about 1.2 seconds" after RESET that OpenCBM's
`iec_reset` notes for a 1541.

#### 1581 boot file search

After its diagnostic a 1581 resets its controller and restores the head, then
looks for a `COPYRIGHT CBM 86` file (`dskint.src` sets `dejavu` bit 7, then
`cbmboot` and `utlodr.src` run `autoi` and `lookup`). It serves ATN only
afterwards, with the lines released throughout. The bound
(`bus.boot_file_search_s`, 27.38 s) is built from these terms, all from the
`DOS_1581` source and the WD177x data sheet:

| term | value | source |
|---|---|---|
| controller tick | 10 ms | CIA timer `$4E20` cycles at 2 MHz (`mrout.src` `reset_ctl`) |
| controller reset | 2 x 255 ms | `reset_ctl`: `xms` with Y = 255, twice ("no access for 500 mS") |
| step | at most 12 ms | restore/seek commands `$08`/`$18`, `+1` on a WD1772 (`reset_ctl`): rate field 00 or 01, 6 or 12 ms on both WD1770 and WD1772 |
| settle | 18 ms | `setval` (`reset_ctl`), after every seek and restore |
| restore | 79 steps | 80 cylinders (`pmaxtrk` 79); the head can be anywhere at power on |
| spin-up | `$50` ticks = 0.8 s | `motoracc` (`dskint.src`), counted down by the controller IRQ (`end_ctl`) |
| disk-change check | 2 steps + settle | `wait_mtr` steps in and out |
| seek to the directory | 39 steps + settle | track 40 is cylinder 39 (`trans_ts`) |
| revolution | 0.2 s | 300 rpm |
| ID search | 5 revolutions | WD177x: Record Not Found after 5 index pulses |
| one read try | ID search + 1 revolution + ID search | `read_ctl`: seek a header, then read the side's 10 sectors into the track cache |
| tries, `autoi` and `initdr` | 3 | `jobrtn` set: the job, then `dorec` with `revcnt` = 2 (`job.src`) |
| tries, `lookup` | 5, plus a restore and a re-seek | `jobrtn` clear: `dorec`, restore, `dorec` again (`job.src` `recov`) |

The disk jobs are two header seeks (`itrial` in `autoi` and in `initdr`), the
side-0 track read for the header and BAM (directory sectors 3-19 are then in
the track cache), and the side-1 track read when the directory chain reaches
sectors 20-39:

    mechanics  0.51 + 79 x 0.012 + 0.8 + 41 x 0.012 + 3 x 0.018  = 2.80 s
    autoi/initdr  3 x 1.0 + 3 x 1.0 + 3 x 2.2                 = 12.60 s
    lookup     5 x 2.2 + 2 x 39 x 0.012 + 2 x 0.018           = 11.97 s

A readable disk takes a small part of this: the mechanics and about a
revolution per job. The bound covers the search only; a boot file that is
found then runs, for as long as it likes. JiffyDOS replaces the ROM and its
source is not published, so whether it keeps, changes or drops the search
cannot be sourced; the bound is the stock ROM's. A JiffyDOS 1581 may also fail
`cbm_identify`, which matches the stock ROM's footprint at `$FF40` (`0x01BA`,
OpenCBM `detect.c`).

#### Measured

With a 1571 (JiffyDOS) on 8, a 1581 (JiffyDOS, disk inserted) on 9 and a
1541-II (JiffyDOS 5.0) on 10, after a reset CLK and DATA stayed low until
8.33 s, the first `wait` (device 8) returned then, 9 answered 73 (1581) and
10 answered 73 (JiffyDOS 5.0 1541), and the whole `reset`, `wait 8 9 10`,
`status 8 9 10` run took 9.8 s. The user's reading is that the 1571 held the
lines; the run did not isolate it. Each reset's `timeline` records the
evidence: the time each line was last released and each drive's first answer.

### JSON

Each step prints one record. `reset` returns `settled_s` and its `timeline`:
`transitions` (`[seconds, [lines low]]` at every change), `released` (the
last release of each line), `settled` and `answered` (each drive's first
answer, in seconds after the reset); `wait` and a first `status` give the
drive's `answered_s`. The summary lists every timeline (the run start, each
reset, each `UJ`/`U:`).

Every failed, refused-by-DOS or interrupted step carries a `bus` record, and
the summary carries one for the end of the run and for each failed drive: the
lines found low (`ATN`, `CLK`, `DATA`, `RESET`, `SRQ`) by `iec_poll`, how
long each has been low, the time since the last reset and the last ATN
sequence, and the addressed drive. `text` says it in a line, for example
`CLK low for 2.10 s since reset; DATA low for 2.10 s since reset; ATN not
asserted; reset 2.10 s ago; no ATN yet; no drive addressed`. `held_since` says
where each time starts: `seen` (the poll that first saw it low), `reset`, or
`unknown` (low when the run started, so the time is a lower bound).

### Failures and signals

Held lines name no drive, so reaching the outer limit fails the bus, not the
drive being waited for: the summary's `held` says how long the lines have
been held since the reset or since the run started, that this is past every
derived boot bound, what was seen, that a drive is holding the bus and needs a
power cycle, and that nothing was sent. No drive entry is marked failed for
it, and `hwcheck`'s recovery does not reset again.

A drive that does not answer once the lines are free fails its step and the
script stops; `--keep-going` skips only that drive's later steps. A hung DOS
holds its ATN acknowledge, so it blocks every other drive's transactions too:
`--keep-going` helps with absent drives and DOS errors, and a hung drive needs
a power cycle.

SIGINT and SIGTERM stop the session after the transaction or line wait in
progress returns; the host then releases its lines (unless it is leaving a
held bus alone) and closes the adapter. A transaction may run for its full
span, and the plugin allows 3 s more to unwind (`XUM1541_RESET_MS`), so
`docker stop` must wait longer than the longest span plus 3 s: the outer
limit, 28.64 s, or `--command-seconds` (30 s) if longer, so
`docker run --stop-timeout 34` with the defaults.

### Head safety

`command` refuses, unless `--allow-dos-bump`, the DOS commands that can step
a 1571 head to the stop ([disk.md](disk.md#dos-commands-that-move-a-1571-head-to-the-stop)):
`N` with an ID (format), `U0` burst commands other than `U0>` utilities,
`M-W` into the job queue (`$00-$0A`, which covers the 1541, 1571 and 1581
queues) and commands that run drive code (`M-E`, `B-E`, `U3`-`U8`, `&`). `I`,
`V` and directory reads are allowed; they bump only in error recovery.

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

The firmware hex is built from the same OpenCBM tree as the plugin, branch
`xum1541-stream` of the fork (v12; it also serves every older protocol):

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
