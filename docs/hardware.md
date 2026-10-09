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
docker run --rm --device=/dev/bus/usb -v "$PWD/artifacts:/data/artifacts" \
  --entrypoint python3 nybulah -m nybulah.hwcheck --devs 8 10
```

## S2, one drive powered

S2 strobes ATN, so every other drive must be switched off:

```sh
docker run --rm --device=/dev/bus/usb -v "$PWD/artifacts:/data/artifacts" \
  --entrypoint python3 nybulah -m nybulah.hwcheck --devs 10 --s2
```

`--proto s3` adds the firmware-assisted protocol once both
`drive/proto_x.inc` and the adapter plugin provide it; otherwise the step is
recorded as skipped.

## Expected results

| device | model   | expansion RAM  |
|--------|---------|----------------|
| 8      | 1571    | `$6000-$7FFF`  |
| 10     | 1541-II | `$8000-$9FFF`  |

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
(including idle time between commands) makes the drive release the bus,
restore zero page `$30-$35` and VIA1 (ACR, IER, T1 latches) and return to
DOS, as `Q` does. A 1571 running at 2 MHz times out after about half a second.
