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

The xum1541 firmware (v13) answers both reset requests on its control
endpoint. `cbm_reset` (`XUM1541_RESET`) releases ATN, CLK, DATA and SRQ at
once and leaves the RESET pulse to the command loop. `cbm_adapter_reset`
(`XUM1541_ADAPTER_RESET`) always releases those lines and aborts the transfer
in progress; its command loop then reinitialises the port (`board_init_iec`,
`iec_init`) and the host library waits for that, clears the endpoint stalls
and restores the I/O timeout. Its bus flag only adds the same RESET as
`cbm_reset` (`iec_reset`: a RESET pulse, then `wait_for_free_bus`). The line
state alone after a RESET cannot tell the adapter from a drive: the first poll
is answered only after `iec_reset` returns, and a drive that keeps or retakes
CLK and DATA reads the same as a wedged adapter.

So `reset` (and `recover`) do not guess. Lines still held at the outer limit,
or a RESET request that fails, run `adapterreset`: `cbm_adapter_reset(fd, 0)`
(`OpenCBM.adapter_reset`), then one poll. The adapter has provably released
its lines, so CLK or DATA free blames the adapter and CLK or DATA low is a
drive's. It then pulses RESET and settles within a new outer limit; a drive
still holding gets one more pulse and limit (`RECOVERY_PULSES`), and lines
still held after that are reported as above, naming a drive. Only when the
adapter-reset request itself fails (older library or firmware, or no answer)
does it USB-reset the adapter instead (`USBDEVFS_RESET` on the `16d0:0504`
usbfs node, `OpenCBM.usb_reset`); a USB reset cannot free a line a drive
holds. The step record's `recovery` gives each reset's outcome
(`adapter_reset`, `usb_reset`), `held_by` (`adapter` or `drive`) and the
RESET pulses sent after the adapter reset (`pulses`). The `adapterreset` step
does the same on request.

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
image's default plugin is v13's.

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

## Drive RAM test

`nybulah ramtest` tests the drive RAM the captures use, separating RAM faults from
link faults. Tested pages: zero page, the stack, the monitor (`$0500` up) and I/O
(`ramprobe.io_mask`) excluded.

| model | regions |
| --- | --- |
| 1541 | base `$0200-$04FF`, expansion `$8000-$9FFF` |
| 1571 | base `$0200-$04FF`, expansion `$6000-$7FFF` |
| 1581 | `$0200-$04FF`, `$0800-$1FFF` |

1. Address check: every page filled with its page number, then with its offsets,
   all written before any is read back. Pages that differ, and pages whose number
   was read elsewhere, are unsafe to hold the test code.
2. Transfer: seeded (`--seed`) random blocks and their inverse written over the
   link and read back.
3. March: `drive/ramtest.s` runs March C- (A. J. van de Goor, *Testing
   Semiconductor Memories: Theory and Practice*, Wiley 1991:
   ⇕(w0) ⇑(r0,w1) ⇑(r1,w0) ⇓(r0,w1) ⇓(r1,w0) ⇕(r0), detecting stuck-at,
   transition, address decoder and coupling faults) at full CPU speed, one
   element per monitor `J`, for each data background (`--backgrounds`): solid
   (`$00`, `$FF` as its inverse), checkerboard (`$55`/`$AA` by address), the
   word-oriented `$33` and `$0F` (van de Goor and Tlili, DATE 1998, intra-word
   coupling) and the address's low and high bytes (decoder faults, aliasing). The
   code is relocated to two disjoint sound placements (lowest first) and run from
   each over every tested page outside it, so every page is tested at least once.
   Each element counts mismatches per page and logs the first 16 (address,
   expected, read).

Tested RAM is backed up first and restored afterwards; `--repeats` repeats the
transfer and march. The JSON report has per region the tested `bytes`,
`untested` pages, `passes` (backgrounds x repeats), march `failures` with
`failing_pages`, the `logged` failures and per failing address the OR/AND of the
failing XORs with `stuck0`/`stuck1` (bits only ever read low/high); a coupling
fault shows at its victim's address. The
address check and transfer report their mismatches the same way; `ok` is false on
any failure, mismatch or untested page.

```sh
docker run --rm --device=/dev/bus/usb nybulah ramtest --dev 8 --transport s4
docker run --rm --device=/dev/bus/usb nybulah ramtest --dev 9 --transport s4
docker run --rm --device=/dev/bus/usb nybulah ramtest --dev 10 --transport s3
```

The image's entrypoint is the `nybulah` command; `bench` and `ramprobe` are
its other subcommands (`nybulah <command> --help`), e.g.
`nybulah bench --dev 10 --protocol s3`.

## Test pattern

`nybulah pattern` writes a known track and checks every capture path against
it (`nybulah/analysis/pattern.py`). The pattern is one revolution, regenerated
from `--halftrack`, `--density` (default: the track's zone; any density on any
halftrack), `--seed` and `--region`:

| group | contents | expected |
|---|---|---|
| `gcr_all` | GCR of every byte $00-$FF | exact |
| `sync<n>` | four syncs: n = 10, the hardware minimum; 40, DOS's; one just past the TB pass's T2 low byte span and one past the TS release-wait counter span, each tagged and followed by $55 | exact, runs measured |
| `weak` | 32 $00 bytes (no flux) | unstable |
| `resync` | a 40-one sync and tag | exact |
| `random` | seeded GCR filling the revolution | exact |
| `gap55` | 64+ $55 bytes ending on a byte boundary | exact, framing |
| `dos` | 4 DOS sectors (header track `H/2`, ID `NY`) | exact |

`--region weak` replaces `weak` by runs of k, 4k, 16k and 64k bytes, k the
bytes spanning the TB pass's T2 low byte at the density (8, 32, 128, 512 at
density 0): per length a `noflux<n>` run of $00 and a `badgcr<n>` run of $44
(zero runs of three, which GCR never writes), each after its own tagged
40-one sync (`tag<i>`) so it can be found. A run ends with the zero before
the next sync, since the byte a sync interrupts is never latched.

It is sized for the shortest revolution within the unmeasured speed tolerance.
The write first measures the drive's cells per revolution at the pattern's
density (a probe sync, `cells` below), then writes `--lead` $55 bytes (default:
all the filler, so the pattern comes last), the pattern, and $55 to whole pages
covering a fast revolution. The lead varies the time from the write's start to
the pattern's; a lead too short lets the write's end reach the pattern again
and is refused with the range that fits. `truth.json` (under `--save`) holds
each region's bit offset, length and expected bits, the density, the measured
`cells`, the `lead` and the variant.

`verify` aligns each capture to the pattern: FFT cross-correlation places each
revolution's copy, a banded edit distance then counts per group bit errors,
its band widened to reach every stable stretch between unstable regions (each
placed by its own exact bits, since an unstable region reads at any length, so
a misread there shows as its insertions or deletions only),
insertions and deletions (sync length differences reported apart, per sync as
written against found), the drift and the $55 byte framing, the revolution
length read against `--cells`, and per unstable group (`weak`, or each
`noflux`/`badgcr` run) its instability across repeats (`summary.<path>.unstable`,
all paths in `summary.unstable_all`). Verify takes no `--lead` and refuses
one: alignment finds the pattern wherever the write's lead put it, and the
index angles come from the index-started capture below, so the lead only
changes where the pattern sits, not how it is found (`truth.lead` in a verify
report is null). Each capture is also digested against the
latched truth and, for RAM captures, the streams (`ramcheck`). RAM captures with
a TB pass get a speed trace (byte period over `--window` bytes against byte
index) with each excursion's start byte, peak percent, oscillation period and
decay in ms.

On a 1571, verify takes one more RAM capture whose BITS pass starts at an index
edge (`ram-index-H.npz`); its aligned start places the index on the pattern.
Every capture start (`start_angle.index`) and speed excursion (`angle.index`)
then gets its angle after the index, and `index.pattern_angle` is the pattern
start's. Stream INDEX metadata is reported per stream (`index`: edges, their
track positions, how the stream ended, `wd_status`: the WD1770 status the
stream began from, `index_level`: the index level at its end) and against that
reference (`index.stream_edge_offsets`) but not used for it; `index.ram_index`
lists each index-started RAM capture's status and the WD1770 status prep left.
A stream whose drive saw no index edge ends `noindex` (END_NOINDEX after its
two-revolution timeout) with no INDEX; `index.drive_end` lists the ends. A
`noindex` stream whose `wd_status` has bit 0 set (busy) never had the WD1770
in type I status; with it clear, the index signal itself did not toggle.

BITS, TB and TS each read their own revolution, so the no-flux `weak` region can
read as bytes in one pass and as ones run into the `resync` sync in another,
and the passes then latch different byte counts after it. The merge places TB
and TS on the BITS bytes allowing such slips: TB and TS land, where they can,
on each other's syncs that fit their timing, a BITS sync that swallowed bytes
another pass latched is timed across them, TS's timer wraps go to the TB byte
on its BITS boundary, and where TB slipped unseen TS measures the syncs. The
speed trace takes only TB intervals the merge placed on single latched bytes.
Bytes read from no flux are not 8 written cells and can still move it.
Over a known revolution a pass position inside the BITS bytes is that byte;
only positions past their ends stand for the byte a whole turn away, since the
turns need not latch the same count across the weak region. One ambiguity
remains: a TB or TS event in the weak region (a cell or two of extra wait) and
the `resync` sync can both land, after one slip, on capable bytes of BITS weak
data, costing no more than the true slip with the weak event unmatched; the
sync weights then favour the false landing, restoring a run of hidden ones the
BITS pass never saw (inside the weak region, so `verify` counts it as `weak`
insertions).

`cells` writes a probe sync on `--halftrack` (destroying that halftrack only,
once per density) and measures the cells per revolution at each of
`--densities`, with the implied rpm; on a 1571 it also takes a RAM capture
started at the index, whose two index edges give the index period (µs) and the
cells it implies at that density. Captures run at 1 MHz (the track code is
cycle-timed for it), so there is no clock option.

`halftracks` only reads: per halftrack, `--repeats` streams (1571 with s4) and
RAM captures, each aligned to `--truth` with its copies found, covered exact
bits, bit errors, slips and `match` (the fraction of covered exact bits read as
written), which measures cross-talk from the pattern's halftrack. Halftracks
outside 2..`max_halftrack` (84, `nibbler.MAX_HALFTRACK`, reached without
touching either stop) are skipped and listed.

Commands, with H the pattern's halftrack, N the homing bound, C the written
`cells` and T a `truth.json`; drive 8 is the 1571 (s4), drive 10 the 1541:

1. Measure cells per revolution on a scratch halftrack (destroys halftrack S
   only):

   ```sh
   docker run --rm --device=/dev/bus/usb -v "$PWD/artifacts:/data/artifacts" nybulah pattern cells --dev 8 --transport s4 --halftrack S --densities 0 1 2 3 --max-steps N --save /data/artifacts/pattern/cells8
   docker run --rm --device=/dev/bus/usb -v "$PWD/artifacts:/data/artifacts" nybulah pattern cells --dev 10 --transport s3 --halftrack S --densities 0 1 2 3 --save /data/artifacts/pattern/cells10
   ```

2. Write on #8 (homes within `--max-steps` outward steps, never bumps; seeks to
   H; writes a probe then the pattern on halftrack H side 0 only). Note `cells`
   and `lead`. Add `--density D` for a non-zone density, `--lead L` to move the
   pattern within the write, `--region weak` for the weak variant.

   ```sh
   docker run --rm --device=/dev/bus/usb -v "$PWD/artifacts:/data/artifacts" nybulah pattern write --dev 8 --transport s4 --halftrack H --max-steps N --save /data/artifacts/pattern/write
   ```

3. Verify on #8 (same homing; reads H only: `--repeats` streams and RAM
   captures, and one RAM capture from the index), with the same `--density`
   and `--region` as the write:

   ```sh
   docker run --rm --device=/dev/bus/usb -v "$PWD/artifacts:/data/artifacts" nybulah pattern verify --dev 8 --transport s4 --halftrack H --max-steps N --repeats 3 --cells C --save /data/artifacts/pattern/dev8
   ```

4. Read neighbouring halftracks against the truth (reads only):

   ```sh
   docker run --rm --device=/dev/bus/usb -v "$PWD/artifacts:/data/artifacts" nybulah pattern halftracks --dev 8 --transport s4 --truth /data/artifacts/pattern/write/truth.json --halftracks H-1 H+1 --max-steps N --save /data/artifacts/pattern/near8
   ```

5. The user moves the disk to #10.

6. Verify on #10 (locates from DOS's track and the headers under the head,
   never bumps, refuses if neither places it; reads H only by RAM captures).

   ```sh
   docker run --rm --device=/dev/bus/usb -v "$PWD/artifacts:/data/artifacts" nybulah pattern verify --dev 10 --transport s3 --halftrack H --repeats 3 --cells C --save /data/artifacts/pattern/dev10
   ```

When neither DOS's track nor the headers under the head place it (a blank or
unformatted halftrack after a reset), `--search-steps S` on `write`, `verify`,
`cells` and `halftracks` steps the head outwards one halftrack at a time, at
most S steps, until headers place it or, on a 1571, the track 00 sensor turns
on and homing takes over; S steps with neither is refused. S is the caller's
promise that the head is at least S + 2 halftracks from the stop (it never
reaches the stop). On a 1571 the search's steps count towards `--max-steps`.

Saved captures are compared again offline with
`nybulah pattern compare --truth artifacts/pattern/dev8/truth.json artifacts/pattern/dev8/*.npz`.

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

Streaming needs firmware v12 or later and the plugin from the same tree (branch
`xum1541-stream`, the image's default).

Flash `xum1541-ZOOMFLOPPY-v13.hex` as below (`info` must print
`model 2 version 13`, `devinfo` firmware version 13). Then, in order, with
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

## 1581

### Hardware (sources)

| fact | value | source |
|---|---|---|
| CPU | 6502A at 2 MHz (16 MHz oscillator Y1 / 8 by 74LS93 U10; WD clock 8 MHz) | service manual PN-314982-01 parts list, schematic 252380 sheet 2 |
| memory map | RAM `$0000-$1FFF`, `$2000-$3FFF` unused, 8520A CIA `$4000-$5FFF` (16 registers mirrored), WD1770/1772 `$6000-$7FFF` (4 registers mirrored), ROM `$8000-$FFFF` | sheet 1 (74LS139 U6), service manual memory map |
| DOS RAM | buffers `$0300-$09FF`, BAM `$0A00-$0BFF`, track cache `$0C00-$1FFF` | service manual RAM usage, DOS `equate.src` |
| CIA port A | PA0 side select (0: head 1), PA1 /RDY, PA2 /MOTOR (0: on), PA3-4 device switches, PA5 power LED, PA6 activity LED (1: lit), PA7 /DISK CHANGE | DOS `iodef.src`, sheets 2-3 |
| CIA port B | PB0 DATA in, PB1 DATA out, PB2 CLK in, PB3 CLK out, PB4 ATN acknowledge, PB5 fast serial direction, PB6 /WPRT, PB7 ATN in | `iodef.src`, sheet 3 |
| ATN | FLAG = ATN (ICR bit 4 on its falling edge); DATA pulled iff PB4 = 1 and ATN asserted (74LS00 U7) | sheet 3, `irq.src`, `sieee.src` |
| fast serial | CNT on SRQ, SP on DATA through 74LS241 U13 and 7407 U11, direction PB5; timer A is the shift clock | sheet 3, service manual "programmable baud rate" |
| WD177x | DDEN tied low (MFM only); DRQ not connected; index (drive pin 8) and TR00 (pin 26) only to the WD; /WPRT also on PB6 | sheet 2 |
| WD step rates | 1772: 6/12/2/3 ms, 1770: 6/12/20/30 ms; DOS uses `$08` on 1770 boards and `$09` on 1772 boards (J1 sensed through the 8520 TOD counter); nybulah uses 12 ms (r1r0 = 01) | WD1772 datasheet, `mrout.src` reset_ctl, sheet 3 note |
| format | 80 cylinders x 2 sides x 10 sectors of 512 bytes, 250 kbit/s at 300 rpm (6250 bytes, 32 us a byte); ID side byte H = 0 on head 1 | service manual specifications, `mrout.src` fmtrk |
| logical | track 1-80 = cylinder 0-79; sectors 0-19 on H = 0, 20-39 on H = 1, two per physical sector | `msub.src` trans_ts |
| head motion by the DOS | Restore at power-on and reset, in job error recovery, format and burst query | `dskint.src`, `job.src`, `burstc.src`, `patch.src` |
| fast host flag | an idle 1581 sets it after 8 SRQ rises (shift register in input mode, `irq.src`, lock bit set at init) and clears it only on UNLISTEN, UNTALK or a framing error (`sieee.src`); while set a TALK answers over the shift register | DOS source |

### WD177x programmed I/O timing

The WD's 8 MHz CLK and the CPU's 2 MHz come from the same 16 MHz Y1 (sheet 2), and
DDEN is grounded, so the data sheet's MFM column applies in exact CPU cycles:

| from | to | delay | cycles | source |
|---|---|---|---|---|
| command register write | busy (status bit 0) valid | 24 us | 48 | WD1772 datasheet, status register "Delay Req'd", MFM |
| command register write | status bits 1-7 valid | 32 us | 64 | same table |
| register write | read of the same register | 16 us | 32 | same table; "Floppy Disk Controller Devices" |
| Force Interrupt | next command | 16 us | 32 | type IV commands |
| type I command | first step pulse | 24 us or more (DIRC valid before it) | 48 | type I commands |
| Write Track | first Data Register load | within 3 byte times (32 us each) | 192 | Write Track |
| Write Track / Write Sector DRQ | next Data Register load | before the WD takes it, one byte time (32 us at 250 kbit/s) after DRQ | 64 | Write Track, Write Sector |

The datasheet allows a write up to the next byte boundary; the DOS (`fmtrk`)
writes within about 21 cycles of a DRQ, an unrolled loop per field. nybulah's
Write Track feed (`writetrk`) decodes its run-length image between writes and
keeps every write-to-write path, a token decode included, under a byte time, so
a DRQ is written within one poll pass and the write (40 cycles) whatever the
image, 24 or more cycles before the WD takes the byte; Write Sector's feed is
built the same way; a decode between a DRQ
and its write would eat into that margin, and on hardware a thin margin sets
Lost Data (`$84`). `simwd` measures that margin (`drq_slack`) and can make
the WD take each byte early (`drq_lead`); the tests run Write Track and Write
Sector with the WD taking bytes 24 cycles early.

The DOS issues every command through `wdbusy` (`msub.src`): write, poll until busy
reads set, then `delay16`; `wdunbusy` polls until it reads clear; `wdabort` waits
three `delay40` after `$D0`. nybulah issues every command and force interrupt
through `WDISSUE` (`drive/mfm.inc`): the first status read comes 5 n + 5 = 65
cycles after the write (n = 12 loop passes), past both validity delays, so a
busy bit that reads clear there means the command has ended; no command follows
a force interrupt sooner. The simulator (`simwd`) returns the register as it was
before a command write until each delay has passed and counts such reads in
`early`; every 1581 drive-code test asserts none.

Type I status (IP, MO) is shown after a type I command or a force interrupt
while idle, but T0 is updated only by a type I command (datasheet status note 4):
on the 1581 an idle `$D0` reads `$80` with the head on cylinder 0
(`artifacts/hw11/dry2.json`). TR00 reaches only the WD (sheet 2: drive pin 26 to
WD pin 23, not the CIA), and the DOS never reads T0 (only Restore uses TR00). So
`sense` and every homing check force an interrupt, then issue a Seek to the
track register's own value (data register = track register: no step pulse,
Seek flowchart) and read T0 once it ends. A `$D0` written while idle can read busy, with T0 and IP clear, past the
datasheet's delays (status `$81` with the head on TR00, `artifacts/hw11`); the
datasheet gives no duration and the DOS waits for busy to clear after `$D0`
(`wdabort`). nybulah does the same (`WDIDLE`, bounded by a timer B wrap) after
every force interrupt, so T0 is read only once busy reads clear. A command
written in that window is not accepted. The h flag (bit 3, set in every command as by the DOS) skips the six-index
spin-up wait; MO still rises with each command and falls after nine idle index
pulses, so with the spindle stopped status bit 7 stays set (hardware runs show
`$80` idle). MO is not wired on the 1581: CIA PA2 runs the motor, and Restore and
Seek step without it.

A bounded Restore reports what the WD did (`restore` in `homeprobe` JSON, also
when homing fails): `first_status` (busy should be set: `busy_seen`), `end`
(`busy_fell`: the WD found TR00 itself, before the last allowed pulse;
`deadline`: the (c - 1/2) x 12 ms force interrupt stopped it, the normal end
when c equals the distance, since the WD checks TR00 a step time after each
pulse), `last_status`, `forced_status` (after the zero-step Seek; T0 decides), `track_register` (`$FF`
less the pulses when stopped by the deadline, 0 when the WD found TR00),
`elapsed_us`, `pulses`, `settled_status` and `settled_t0` (TR00 sensed again
after the 18 ms settling time; homing fails without it and the head is not moved
again).

nybulah uses no ROM routine and no DOS variable, so a JiffyDOS 1581 ROM behaves as the
stock one. Its only DOS interface is the job queue at `$0002` (job 0), documented in
the 1581 user's guide, to run job `$82` (controller reset: track cache invalidated,
no head movement) after a session that used the track cache RAM.

### What can be captured and written

| | capture | write back |
|---|---|---|
| IDs (C, H, R, N incl. wrong track, side or size codes, duplicates, extra sectors, cylinder 80) | Read Address stream, with times | Write Track |
| sector data, data CRC errors, deleted data marks, odd sizes 128-1024 | Read Sector | Write Track / Write Sector |
| ID CRC errors | Read Address status | Write Track (two plain bytes for the CRC) |
| gaps, marks, layout | Read Track (gap bytes may read wrong at write splices and false C2 resyncs in data fields; data from Read Sector) | Write Track, except bytes `$F5-$F7` |
| revolution time, sector timing | stamps per command (first byte, end), index edges | no (fixed 8 MHz clock and motor speed) |
| fuzzy bits | differences between reads | no |
| no-flux areas, clock bits | no (reads as data `$00`) | no |
| long/short tracks, IDs over the index | from stamps and Read Track | no |

### What to run (1581 on device 9)

Build the image from the local OpenCBM tree as for streaming (firmware v12 is
already flashed; nothing changes on the adapter). Insert a 1581 disk you can afford
to lose only for step 5.

1. Memory only, the head does not move: the monitor transports and the 8520
   shift register timing.

   ```sh
   docker run --rm --device=/dev/bus/usb -v "$PWD/artifacts:/data/artifacts" nybulah hwcheck --devs 9 --proto s1 --proto s3 --proto s4
   docker run --rm --device=/dev/bus/usb -v "$PWD/tools:/tools" --entrypoint python3 nybulah /tools/xprobe.py --dev 9 --cia
   ```

   Expect `bench_*` errors 0 and the `dos_cache` step true; `--cia` `first` at 39
   or less on both phases (the stream's 40-cycle period needs it).
2. Dry run, no stepping (the motor turns, one ID is read):

   ```sh
   docker run --rm --device=/dev/bus/usb nybulah homeprobe --dev 9 --transport s4 --headers
   ```

   Expect `t0` false unless the head is on cylinder 0, `index` toggling between
   runs, `period_us` near 200000, `id` with `c` equal to `estimate` (source
   `id`), `track_register` equal to it if DOS last moved the head, and `steps` =
   `estimate`.
3. Homing, bounded by the dry run's `steps` (N): Restore never sends more than N
   step pulses and fails if TR00 is not sensed; the head returns to the estimate.

   ```sh
   docker run --rm --device=/dev/bus/usb nybulah homeprobe --dev 9 --transport s4 --headers --step --max-steps N
   ```

   Expect `homed` true and `restore` with `busy_seen` true, `end` `deadline`,
   `pulses` equal to the estimate, T0 (`$04`) in `forced_status` and
   `settled_t0` true.
4. One track, streamed (homes as in 3 first):

   ```sh
   docker run --rm --device=/dev/bus/usb -v "$PWD/artifacts:/data/artifacts" nybulah streamprobe --dev 9 --max-steps N --cylinder 39 --revolutions 2 --save /data/artifacts/stream-1581-39.npz
   docker run --rm -v "$PWD/artifacts:/data/artifacts" nybulah info /data/artifacts/stream-1581-39.npz
   ```

   Expect `adapter` and `drive` "done", `revolution_bytes` near 6250,
   `revolution_us` near 200000, 10 IDs a revolution with `c` 39, no ID CRC errors.
5. A whole disk, read (and, on a scratch disk, written and verified):

   ```sh
   docker run --rm --device=/dev/bus/usb -v "$PWD/artifacts:/data/artifacts" nybulah read --dev 9 --transport s4 --max-steps N /data/artifacts/disk.d81
   docker run --rm --device=/dev/bus/usb -v "$PWD/artifacts:/data/artifacts" nybulah write --dev 9 --transport s4 --max-steps N /data/artifacts/disk.d81
   ```

   Expect `errors` 0 on a good disk, `failed` empty after the write. Each session
   ends with DOS job `$82`; after a write, use DOS on the disk only after that.

## Flashing the ZoomFloppy firmware

The firmware hex is built from the same OpenCBM tree as the plugin, branch
`xum1541-stream` of the fork (v13; it also serves every older protocol).
v13 adds an adapter reset served from the control endpoint, which aborts any
transfer and returns the adapter to its idle state without a USB reset
(`cbmctrl adapterreset`, `-b` also resets the drives):

```sh
git clone https://github.com/anarkiwi/OpenCBM && cd OpenCBM
git checkout xum1541-stream
docker build -f Dockerfile.nybulah --target firmware-hex -o fw .
docker run --rm -v "$PWD/fw:/fw" --entrypoint xum1541cfg nybulah info /fw/xum1541-ZOOMFLOPPY-v13.hex
```

The build steps the compiled timing routines (`misc/x_timing.py`) and checks
the SRQ schedule (`misc/srq_timing_test.c`); it fails rather than produce a
hex that misses them. `info` must print `model 2 version 13` (it exits with
status 1 regardless). Then, with the ZoomFloppy plugged in (drives may stay
connected), flash it; the adapter re-enumerates as a DFU bootloader during the
update, so the container gets the whole USB tree:

```sh
docker run --rm --privileged -v /dev/bus/usb:/dev/bus/usb -v "$PWD/fw:/fw" \
  --entrypoint xum1541cfg nybulah update /fw/xum1541-ZOOMFLOPPY-v13.hex
docker run --rm --privileged -v /dev/bus/usb:/dev/bus/usb --entrypoint xum1541cfg nybulah devinfo
```

`devinfo` should report firmware version 13 (the image's plugin must be at
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

- 1581: nothing is probed; its free run is the DOS track cache `$0C00-$1FFF`,
  after which hwcheck runs DOS job `$82` (`dos_cache` step).

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
