# VICE test bench

The drive code also runs on VICE's true drive emulation: the real DOS ROMs, the
6502, the CIA and VIAs and the WD1770/1772 of the 1541, 1571 and 1581, as VICE
models them. Our own simulators (`nybulah.sim*`) model the same parts. VICE is a
second, independent model to check them against.

VICE is GPL software. It runs as a separate program, controlled over its binary
monitor. None of its source and none of its ROMs are in this repository.

## Image

The `vice-build` stage of the `Dockerfile` builds VICE 3.10 headless from the
release tarball, pinned by SHA-256. It builds `x64sc`, `x128` and `c1541`, and
copies VICE's own `data/C64`, `C128` and `DRIVES` ROMs into `/opt/vice`. The
`test-vice` stage adds `/opt/vice` to the test image.

```sh
docker build --target test-vice -t nybulah:test-vice .
docker run --rm nybulah:test-vice python -m pytest -n auto -m vice
```

Tests marked `vice` are skipped unless `x64sc` and `x128` are on `PATH`. In CI,
the `vice` job builds this image and runs them.

## Pieces

`nybulah.vice`:

- `BinaryMonitor` is a client for the binary monitor protocol (VICE manual,
  "Binary Monitor"). It can read and write memory in any memspace (0 is the
  computer, 1-4 are drives 8-11, and I/O can be peeked without side effects or
  written through the bus), set checkpoints (exec, load or store, stopping or
  counting), read and set registers, read the CPU history, feed the keyboard,
  and stop, resume and quit the emulator.
- `Vice` starts `x64sc` or `x128` with true drive emulation, sound off and every
  other unit off, and connects to it. VICE binds the binary monitor to a port the
  system picks, and the harness reads that port from the process's own sockets in
  `/proc`, so parallel sessions cannot race for a port. Its `run_until` resumes
  the emulator until a CPU executes an address. Its `history` decodes the CPU history into a numpy
  array (`HISTORY_DTYPE`): the clock, PC, A, X, Y, SP and flags at the start of
  each instruction, and the instruction's bytes. `accesses` turns that history
  into register reads and writes, with the value each one loaded or stored.
- `DriveMonitor` offers `read`, `write` and `jsr`, the same interface as
  `nybulah.monitor.Monitor`, on an emulated drive, so host code runs unchanged.
  `Mfm1581(DriveMonitor(...), sleep=mon.sleep)` drives the real `mfm_1581`
  routines. `start` first runs the drive until its DOS takes an interrupt (the
  handler in the ROM's `$FFFE` vector). The DOS ROMs mask interrupts from reset
  until their initialisation is done, so the drive is never taken over during
  its RAM and ROM tests, before the DOS has set its stack pointer. A call:
  - pushes the address of a parked `jmp *` at `$0500` (the monitor's own load
    address) as the return address;
  - sets A, X, Y and the PC, with interrupts masked;
  - runs until the drive parks again.

  On a 1581, `start` sets the CIA timers as `drive/monitor.s` `ciasave` does for
  J. `run_cycles` and `sleep` run a delay loop of known length on the drive.
- `C128Receiver` loads `drive/vicerx.s` into the emulated C128 and acts as the
  xum1541 stream adapter. It asserts go (CLK), takes every fast serial byte from
  CIA1's SDR, and records the CLK line beside each byte. `adapter_raw` frames
  what it received as the adapter's output, which `MfmStream.parse` reads.

`nybulah.vicebench` runs complete scenarios on a 1581 holding a random D81, and
prints a JSON report:

```sh
python -m nybulah.vicebench writes [--drivecode DIR]
python -m nybulah.vicebench stream [--drivecode DIR] [--code2 0x0782] [--under DIR] [--head N] [--peers]
```

- `writes` runs, on one side:
  - Write Track of a pattern image, then Write Track of the standard layout;
  - Write Sector of ten sectors with deleted marks, then with data marks, each
    read back. For each write it records the WD commands: cycles to the first
    DRQ and the first data byte, the longest gap between data bytes and the PCs
    in that gap, and the final status;
  - finally it quits VICE, which writes the disk back, and compares the D81.
- `stream` runs Read Track of `mfmstream_1581` to the C128. The call starts with
  the zero page the monitor's J leaves: `drive/burst.inc` reads each command
  burst (address, length, op) into `ptr..ptr+4` (`$30-$34`), so J to `$0300`
  enters with `$31 = $03`. It reports the drive's return, the parsed records,
  the sectors decoded from each revolution against the D81, and the drive cycles
  from START to the first KEEP. `--head N` also traces every CIA and WD register
  access from the call to the Nth SDR write. `--peers` puts a 1571 on unit 8 and
  a 1541 on unit 10, as on the hardware bus. `--index-waits N` stamps N index
  edges before the Read Track (`index_us`; every command carries its stamps).

`--drivecode DIR` takes another build's `.bin` files, for example a branch built
with `make -C drive OUT=DIR`. `--code2` sets where the stream code's second part
loads. `--under` names the build whose `mfm_1581` places the head.

## 1581 stream: the early KEEP and the 8520

On hardware the adapter received START, one KEEP shortly after it, then nothing
until its gap timeout. The early KEEP comes from the stream's pass counter `kc`
(`$31`), which the stream code did not initialise. Under the monitor, J leaves
`$03` there (the high byte of the call address), so the first KEEP was queued on
the third `w0` pass: 296 drive cycles after START, on VICE with the J zero page
and in `nybulah.sim` alike. `DriveMonitor.call` had left `$31 = 0`, a full 256
passes. `stream` now clears `kc`, so the first KEEP comes a full count of passes
after START on every entry (`test_stream_first_keepalive_counts_every_pass`,
`test_1581_read_track_streams_to_c128`).

The early KEEP does not explain the silence after it. An earlier build, whose
keepalives followed timer B and had no such counter, showed START then silence as
well. With the J zero page, and with a 1571 and a 1541 on the bus, VICE runs the
same build to the end: START, KEEPs every 256 passes, two full revolutions, every
sector matching. The relevant 8520 points, VICE against the MOS 6526/8520 data
sheets:

- An ICR read returns and clears every flag, including SP (data sheet: "the
  interrupt data register is cleared ... when read"; VICE
  `core/ciacore.c:1288-1352`), and touches nothing else: the read path updates
  the timers and the interrupt state, never the shifter, its bit count, the
  pending SDR byte or CNT (`ciacore_intsdr`). No send path in `mfmstream.s`
  waits for SP: writes are spaced by cycle count (40 or more apart), and the one
  SP wait, in `finish`, is bounded. The stream's loops poll timer B for wraps,
  not ICR (protocol.md, "Stream engine"), so nothing in them clears SP now; a
  cleared flag could not have hung the stream before either.
- In output mode a written SDR byte starts at the next timer A underflow and
  shifts at half the underflow rate; the flag follows the eighth CNT pulse (data
  sheet, Serial Port). VICE models the same inside the drive's CIA, cycle by cycle
  (`ciacore.c:914-929`, `ciacore_intsdr` at `1723-1830`), and only the handover to
  the computer is byte-level (`drive/iec/cia1581d.c` `store_sdr`).
- Wraps come from timer B bit 15 against the phase taken at entry, so a timer
  B flag left from before the call counts nothing.

Every exit of the wait (`dfirst`, `wend`, `wwrap`, `wabort`) sends within a pass
count or a record, and every one is reproduced on VICE. The silence was the
adapter's: firmware v12 and v13 computed their fall-wait poll count in the AVR's
16-bit `unsigned int`, so the 20 ms gap timeout of `x_timing.h` was 3.6 ms, under
the stream's keepalive interval (256 `w0` passes, 6.4 ms). `drive/sdrgap.s`
(`tools/sdrgap.py`) showed it on the 1581 and on the 1571 alike: lone bytes after
idles of 3 ms arrive, after 3.5 ms or more the adapter times out, and the drive's
log holds END_ATN with ATN seen 3.76 ms after START. The v13 hex loads 9642 polls
into the wait, not 53333. Firmware v14 computes the count in long arithmetic and
its build checks the compiled immediate (`misc/x_timing.py`). Neither VICE nor
`nybulah.sim` could show it: the simulated adapter (`simsrq.STREAM_TIMEOUT_US`)
waits the intended 20 ms.

## Limits

- VICE models the 1581's fast serial byte by byte. `store_sdr` in
  `src/drive/iec/cia1581d.c` hands the C128's CIA the whole byte once the 8520
  has shifted it out (`ciacore.c`). There is no bit-level SRQ timing. A byte
  therefore reaches the C128 after the drive has released CLK.

  For that reason the receiver flags a byte as metadata when CLK was asserted at
  any port read since the byte before it. A bus fault at the bit level, such as
  an adapter sampling SRQ or CLK at the wrong cycle, cannot be reproduced on
  VICE. Everything inside the drive can be.
- A drive checkpoint stops the emulator at once, in the drive CPU
  (`DO_INTERRUPT` in `src/6510core.c` calls `monitor_startup`). The drive then
  lags the main CPU. Every binary monitor command first runs the drives up to the
  main CPU's clock (`drive_cpu_execute_all` in
  `monitor_binary_process_command`, `src/monitor/monitor_binary.c`). While the
  monitor is open, checkpoints count hits but do not stop
  (`monitor_startup` returns when `inside_monitor` is set). So the drive runs on
  past the checkpoint at the next command. The harness therefore stops a drive
  only at loops, such as the park, and `run_until` reports whether its
  checkpoint was hit rather than where the drive is.
- When VICE is stopped in drive context it reports stale main CPU registers.
  `C128Receiver.received` takes the 8502's X from the CPU history.
- VICE models the 6526's ICR races on the 1581's CIA (`CIA_IM_TBB` in
  `src/core/ciacore.c`), so drive code that counts timer B wraps from ICR loses
  some (see "1581 capture and streaming" in [protocol.md](protocol.md)).
- VICE starts running before the client connects, so each session begins at a
  different disk rotation and timer phase. Faults that depend on phase show up
  intermittently.
- VICE 3.10 crashes on startup when the log is colourised and stdout is not a
  terminal. The harness passes `+logcolorize`.

