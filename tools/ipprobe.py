"""WD177x type I status edges on a 1581 (drive/ipprobe.s), on hardware or --sim.

After each command sequence, every change of busy, IP, T0 and MO with its time:
a line per sequence and a JSON report (IP rises and spacing, MO and busy, the log).
"""

import argparse
import json

import numpy as np
from tqdm import tqdm

from nybulah import r1581

FORCE, SEEK, READ_TRACK = 0xD0, 0x19, 0xE8  # drive/mfm.inc WD_FORCE, WD_SEEK
TYPE1 = r1581.ST_MO | r1581.ST_T0 | r1581.ST_IP | r1581.ST_BUSY
SEQUENCES = {
    "force": ([FORCE], TYPE1),
    "seek": ([FORCE, SEEK], TYPE1),
    "seek_force": ([FORCE, SEEK, FORCE], TYPE1),
    "read_track": ([FORCE, READ_TRACK], r1581.ST_MO | r1581.ST_BUSY),
}


def changes(t, st, bit):
    """[(microseconds from the first row, bit after)] at each change of bit."""
    on = (st & bit) != 0
    at = np.flatnonzero(on[1:] != on[:-1]) + 1
    return [(int(t[i] - t[0]), bool(on[i])) for i in at]


def summarize(name, t, st):
    """One sequence's edges, times from the probe's first poll."""
    ip = [u for u, on in changes(t, st, r1581.ST_IP) if on]
    return {
        "sequence": name,
        "cmds": [f"${c:02X}" for c in SEQUENCES[name][0]],
        "start_status": f"${int(st[0]):02X}",
        "ip_rises_us": ip,
        "ip_periods_us": np.diff(ip).astype(int).tolist(),
        "ip": changes(t, st, r1581.ST_IP),
        "busy": changes(t, st, r1581.ST_BUSY),
        "mo": changes(t, st, r1581.ST_MO),
        "t0": changes(t, st, r1581.ST_T0),
        "log": [(int(a - t[0]), f"${int(b):02X}") for a, b in zip(t, st)],
    }


def line(s):
    """A sequence's summary line."""
    per = s["ip_periods_us"] or ["-"]
    return (
        f"{s['sequence']:<11} start {s['start_status']}  IP rises "
        f"{len(s['ip_rises_us'])}  period {min(per)}..{max(per)} us"
        f"  MO {s['mo']}  busy {s['busy'][:4]}"
    )


def run(drive, names, revolutions):
    """Motor on (spin-up, index period), each sequence in turn, motor off."""
    drive.motor(True)
    out = []
    try:
        for name in tqdm(names, desc="sequences", unit="seq"):
            cmds, mask = SEQUENCES[name]
            out.append(summarize(name, *drive.status_edges(cmds, mask, revolutions)))
            tqdm.write(line(out[-1]))
    finally:
        drive.motor(False)
    return {"period_us": drive.period_us, "revolutions": revolutions, "runs": out}


def sim_drive():
    """An opened Mfm1581 on the timed simulated 1581 (v12 adapter, s4)."""
    # pylint: disable=import-outside-toplevel
    from nybulah import simsrq
    from nybulah.link import WATCHDOG_IDLE_S
    from nybulah.monitor import Monitor
    from nybulah.simwd import MfmMedia, Wd

    cbm = simsrq.make(
        model="1581", dev=9, firmware=12, timeout_us=WATCHDOG_IDLE_S * 1e6
    )
    cbm.drive.wd = Wd(MfmMedia.formatted(), cylinder=39)
    mon = Monitor(cbm, 9, "s4", clock=lambda: cbm.now * 1e-6)
    mon.start()
    return r1581.Mfm1581(mon, sleep=cbm.host_wait, spinup_s=0).open()


def main():
    """CLI."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dev", type=int, default=9)
    ap.add_argument("--transport", default="s4")
    ap.add_argument("--sim", action="store_true", help="the simulated 1581")
    ap.add_argument("--revolutions", type=float, default=12)
    ap.add_argument(
        "--sequences", nargs="+", default=list(SEQUENCES), choices=list(SEQUENCES)
    )
    ap.add_argument("--save", help="JSON report path")
    args = ap.parse_args()
    if args.sim:
        report = run(sim_drive(), args.sequences, args.revolutions)
    else:
        from nybulah.opencbm import OpenCBM  # pylint: disable=import-outside-toplevel

        with OpenCBM() as cbm, r1581.session(cbm, args.dev, args.transport) as drive:
            report = run(drive, args.sequences, args.revolutions)
    if args.save:
        with open(args.save, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=1)


if __name__ == "__main__":
    main()
