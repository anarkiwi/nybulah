"""Longest wait of a WD read byte for its data register read, per 1581 read path.

drive/mfm.s (readsec, Read Address) and drive/mfmstream.s streams (Read Track, Read
Sector, Read Address) run on the simulated 1581 without the lost-data window at each
--rpm; JSON lines of simwd Wd.drq_wait in cycles against simwd.DRQ_WINDOW.
"""

import argparse
import json

import numpy as np
from tqdm import tqdm

from nybulah import r1581, simfast, simsrq, simx
from nybulah.analysis import mfm
from nybulah.link import WATCHDOG_IDLE_S
from nybulah.monitor import Monitor
from nybulah.sim import Drive1581
from nybulah.simwd import DRQ_WINDOW, MfmMedia, Wd

DEV = 9


def sectors(cyl, side):
    """Ten random 512-byte sectors without $F5-$F7."""
    rng = np.random.default_rng(cyl * 2 + side)
    return rng.integers(0, 0xF5, (mfm.SECTORS, mfm.SECTOR_BYTES), dtype=np.uint8)


def ram_drive(media, cylinder):
    """(sim drive, Mfm1581) on drive/mfm.s under an untimed monitor."""
    cbm = simx.adapter(
        "s1", Drive1581, device=DEV, media=media, cylinder=cylinder, drq_window=0
    )
    cbm.budget = 20_000_000
    mon = Monitor(cbm, DEV, "s1")
    mon.start()
    return cbm.drive, r1581.Mfm1581(mon, sleep=lambda s: None, spinup_s=0).open()


def stream_drive(media, cylinder):
    """(sim drive, Mfm1581) streaming through the v12 adapter on the timed 1581."""
    cbm = simsrq.make(
        model="1581", dev=DEV, firmware=12, timeout_us=WATCHDOG_IDLE_S * 1e6
    )
    cbm.drive.wd = Wd(media, cylinder=cylinder, drq_window=0)
    mon = Monitor(cbm, DEV, "s4", clock=lambda: cbm.now * 1e-6)
    mon.start()
    return cbm.drive, r1581.Mfm1581(mon, sleep=lambda s: None, spinup_s=0).open()


def placed(make, media, cylinder, side):
    """A drive from make with its head on cylinder, side."""
    sim, drive = make(media, cylinder)
    drive.motor(True)
    drive.home(cylinder)
    drive.seek(cylinder)
    drive.side(side)
    return sim, drive


def measure(sim, op):
    """op()'s longest read byte wait."""
    sim.wd.drq_wait = 0
    op()
    return sim.wd.drq_wait


def run(rpm, cylinder=20, side=0):
    """Longest waits per read path at rpm."""
    media = MfmMedia.formatted(sectors=sectors, rpm=rpm)
    sim, drive = placed(ram_drive, media, cylinder, side)
    out = {
        "readsec": max(
            measure(sim, lambda r=r: drive.read_sector(cylinder, r))
            for r in range(1, mfm.SECTORS + 1)
        ),
        "readaddr": measure(sim, drive.read_id),
    }
    sim, drive = placed(stream_drive, media, cylinder, side)
    out["stream_track"] = measure(sim, lambda: drive.read_track(2))
    out["stream_sectors"] = measure(
        sim, lambda: drive.read_sectors(cylinder, 1, mfm.SECTORS)
    )
    out["stream_ids"] = measure(sim, lambda: drive.read_ids(mfm.SECTORS))
    return out


def main():
    """CLI."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rpm", type=float, nargs="+", default=[297.0, 300.0, 303.0])
    ap.add_argument("--cylinder", type=int, default=20)
    args = ap.parse_args()
    simfast.warm()
    worst = 0
    for rpm in tqdm(args.rpm, desc="rpm", unit="speed"):
        row = {"rpm": rpm} | run(rpm, args.cylinder)
        worst = max([worst] + [v for k, v in row.items() if k != "rpm"])
        tqdm.write(json.dumps(row))
    print(json.dumps({"worst": worst, "window": DRQ_WINDOW}))


if __name__ == "__main__":
    main()
