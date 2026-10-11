"""Trace drive/stream.s on the timed 1571 around the first byte it loses or corrupts.

Streams a G64 track (random D64 by default) and prints, around the first byte of
the capture that is not the next latched byte, every latch (L), VIA2 port read
(rPA, rPB), SDR write (W) and the stream.s site, in cycles at 2 MHz.
"""

import argparse
import bisect
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

# pylint: disable=wrong-import-position,wrong-import-order,import-error
from conftest import random_d64  # noqa: E402
from stream_margin import MONITOR, STREAM_MHZ, labels  # noqa: E402
from test_stream import rig  # noqa: E402

from nybulah.formats import d64_to_g64  # noqa: E402
from nybulah.simdisk import Media, log_bytes  # noqa: E402


def trace(halftrack, rpm, revolutions):
    """(events, capture, latched bytes, latch times) of one stream."""
    addrs, names = labels()
    cbm, mech, nib = rig(
        Media.from_g64(d64_to_g64(random_d64(), progress=False), rpm=rpm)
    )
    drive, events = cbm.drive, []

    def site(pc):
        return names[max(bisect.bisect_right(addrs, pc - 1) - 1, 0)]

    cia_write, read = drive.cia.write, mech.read

    def on_write(reg, value, cycles):
        if reg == 12 and drive.mpu.pc < MONITOR:
            events.append(
                (drive.time(cycles) * STREAM_MHZ, "W", value, site(drive.mpu.pc))
            )
        return cia_write(reg, value, cycles)

    def on_read(fdc, reg, cycles, pc=-1):
        if not fdc and reg in (0, 1):
            kind = "rPB" if reg == 0 else "rPA"
            events.append((drive.time(cycles) * STREAM_MHZ, kind, 0, site(pc)))
        return read(fdc, reg, cycles, pc)

    drive.cia.write, mech.read = on_write, on_read
    nib.seek(halftrack)
    mech.log = []
    cap = nib.stream(halftrack, revolutions=revolutions)
    data, times = log_bytes(mech.log)
    events += [(e[1] * STREAM_MHZ, "L", e[2], "") for e in mech.log if e[0] == "byte"]
    return sorted(events), cap, data, times * STREAM_MHZ


def first_fault(cap, data, times):
    """Cycle of the first latched byte the capture skips or corrupts, else None."""
    k = data.find(bytes(cap.data[:16]))
    if k < 0:
        print("latched", data[:24].hex(" "))
        print("capture", bytes(cap.data[:24]).hex(" "))
        return times[0], 0
    for i, b in enumerate(cap.data.tolist()):
        if k + i >= len(data) or data[k + i] != b:
            return times[k + i] if k + i < len(times) else None, i
    return None, len(cap.data)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--halftrack", type=int, default=34)
    ap.add_argument("--rpm", type=float, default=300.0)
    ap.add_argument("--revolutions", type=int, default=1)
    ap.add_argument("--before", type=float, default=140.0)
    ap.add_argument("--after", type=float, default=80.0)
    args = ap.parse_args()
    events, cap, data, times = trace(args.halftrack, args.rpm, args.revolutions)
    print(cap.stream_status, "bytes", len(cap.data), "index", len(cap.index))
    at, i = first_fault(cap, data, times)
    if at is None:
        print("every byte of the capture is the next latched byte")
        return
    print(f"first fault at capture byte {i}")
    k = data.find(bytes(cap.data[:16]))
    print("latched", data[k + i - 4 : k + i + 8].hex(" "))
    print("capture", bytes(cap.data[i - 4 : i + 8]).hex(" "))
    near = [(int(q) - i, hex(int(v))) for q, v in zip(cap.parsed.pos, cap.parsed.val)]
    print("metadata (offset, value)", [m for m in near if abs(m[0]) <= 8])
    for t, kind, value, site in events:
        if at - args.before < t < at + args.after:
            print(f"{t - at:8.1f} {kind:3} {value:02x} {site}")


if __name__ == "__main__":
    main()
