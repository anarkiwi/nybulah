"""Boot a disk image in VICE with a true-drive 1541 and report where it ends up.

Types the load command at READY, runs warp for --seconds, sampling the C64 and
drive PCs, VIC mode and text screen. Usage: ``python tools/vice_boot.py IMAGE``.
"""

import argparse
import collections
import json
import pathlib
import shutil
import tempfile
import time

from tqdm import tqdm

from nybulah.vice import MAIN, OP_STORE, Vice

KEY_WAIT = 0xE5CD
DRIVE8 = 1


def screen_text(mon):
    """The VIC's current text screen as 25 lines of printable ASCII."""
    bank = 3 - (mon.mem_get(0xDD00, 1)[0] & 3)
    base = bank * 0x4000 + (mon.mem_get(0xD018, 1)[0] >> 4) * 0x400
    codes = mon.mem_get(base, 1000)
    chars = "".join(
        chr(c + 64) if 0 < c < 27 else chr(c) if 32 <= c < 64 else "." if c else "@"
        for c in (b & 0x7F for b in codes)
    )
    return [chars[i : i + 40] for i in range(0, 1000, 40)]


def sample(vice):
    """PCs, VIC registers and screen of a stopped emulator."""
    vic = vice.mon.mem_get(0xD011, 2) + vice.mon.mem_get(0xD020, 2)
    port = vice.mon.mem_get(0x1C00, 1, space=DRIVE8)[0]
    return {
        "drive_density": port >> 5 & 3,
        "drive_phase": port & 3,
        "pc": vice.registers(MAIN)["PC"],
        "drive_pc": vice.registers(DRIVE8)["PC"],
        "d011": vic[0],
        "border": vic[2] & 15,
        "background": vic[3] & 15,
    }


def trace_port(vice, limit, timeout):
    """Every drive write to VIA2 port B: (drive clock, density, halftrack delta
    from the first write, drive PC), until ``limit`` writes or ``timeout``."""
    cp = vice.mon.checkpoint(0x1C00, op=OP_STORE, space=DRIVE8)
    rows, phase, half = [], None, 0
    try:
        for _ in tqdm(range(limit), desc="port", unit="wr"):
            vice.mon.resume()
            if not vice.mon.wait_stopped(timeout):
                vice.mon.stop()
                break
            port = vice.mon.mem_get(0x1C00, 1, space=DRIVE8)[0]
            if phase is not None:
                half += {1: 1, 3: -1}.get((port - phase) & 3, 0)
            phase = port & 3
            pc = vice.registers(DRIVE8)["PC"]
            row = (vice.clock(DRIVE8), port >> 5 & 3, half, pc)
            if not rows or row[1:3] != rows[-1][1:3]:
                rows.append(row)
    finally:
        vice.mon.delete(cp)
    return rows


def boot(
    image, seconds, steps, command, trace=0, extra=()
):  # pylint: disable=too-many-arguments,too-many-positional-arguments
    """Samples taken while ``image`` boots on unit 8."""
    work = pathlib.Path(tempfile.mkdtemp(prefix="boot-"))
    try:
        disk = work / pathlib.Path(image).name
        shutil.copy(image, disk)
        with Vice({8: ("1541", disk)}, timeout=120.0, extra=extra) as vice:
            if not vice.run_until(MAIN, KEY_WAIT, 180.0):
                regs = vice.registers(MAIN)
                tail = vice.log.read_text(errors="replace")[-1500:]
                raise RuntimeError(f"no READY: PC {regs['PC']:#06x}\n{tail}")
            vice.mon.feed(command)
            for clock, density, half, pc in (
                trace_port(vice, trace, 60.0) if trace else []
            ):
                print(
                    json.dumps(
                        {
                            "clock": clock,
                            "density": density,
                            "half": half,
                            "drive_pc": hex(pc),
                        }
                    )
                )
            rows = []
            for _ in tqdm(range(steps), desc="boot", unit="step"):
                vice.mon.resume()
                time.sleep(seconds / steps)
                vice.mon.stop()
                rows.append(sample(vice))
            screen = screen_text(vice.mon)
        return rows, screen
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("image")
    ap.add_argument("--seconds", type=float, default=60.0, help="wall time, warp")
    ap.add_argument("--steps", type=int, default=12)
    ap.add_argument("--trace", type=int, default=0, help="drive port writes to log")
    ap.add_argument("--video", choices=("ntsc", "pal"), default="ntsc")
    ap.add_argument("--quiet", action="store_true", help="omit the screen")
    ap.add_argument("--command", default='LOAD"*",8,1\r')
    args = ap.parse_args()
    rows, screen = boot(
        args.image,
        args.seconds,
        args.steps,
        args.command,
        args.trace,
        [f"-{args.video}"],
    )
    for row in rows:
        print(json.dumps({k: hex(v) if "pc" in k else v for k, v in row.items()}))
    pages = collections.Counter(r["pc"] >> 8 for r in rows)
    print(json.dumps({"c64_pc_pages": {hex(p): n for p, n in pages.items()}}))
    if not args.quiet:
        print("\n".join(line.rstrip() for line in screen).rstrip())


if __name__ == "__main__":
    main()
