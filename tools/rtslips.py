"""Where a 1581 Read Track stream loses data bytes, against a Read Sector reference.

Homes, reads the cylinder's sectors through drive/mfm.s readsec, streams Read Sector
and --captures single-revolution Read Tracks; per bad data field a re-frame at a false
sync or a lost byte with the timer B wraps in its span. A line per capture, JSON.
"""

import argparse
import json

import numpy as np
from tqdm import tqdm

from nybulah import disk1581, r1581
from nybulah.analysis import mfm
from nybulah.analysis.crc import SYNC3, crc16
from nybulah.formats.mfmcap import MfmCapture, save_captures

WRAP = r1581.TB_WRAP_US
DAM_LOW_BIT = mfm.DAM & 1


def classify(got, ref):
    """How a data field read by Read Track (its bytes on from the first, CRC and gap
    included) departs from ref: None when it matches; "reframe" at a false sync
    (mfm.false_syncs); "lost" k bytes, the rest of the field and its CRC arriving k
    early, somewhere in the span of equal bytes ending at the first wrong one; else
    "other"."""
    crc = crc16(np.concatenate(([mfm.DAM], ref)).astype(np.uint8), SYNC3)
    want = np.concatenate((ref, [crc >> 8, crc & 0xFF])).astype(np.uint8)
    bad = np.flatnonzero(got[: len(want)] != want[: len(got)])
    if bad.size == 0:
        return None
    i = int(bad[0])
    fs = mfm.false_syncs(ref, DAM_LOW_BIT) // 16
    if np.any((fs <= i) & (i <= fs + 1)):
        return {"kind": "reframe", "offset": i, "false_sync_byte": int(fs[fs <= i][-1])}
    lost = next(
        (
            k
            for k in range(1, 9)
            if np.array_equal(got[i : len(want) - k], want[i + k :])
        ),
        None,
    )
    if lost is None:
        return {"kind": "other", "offset": i}
    j = i
    while j and ref[j - 1] == ref[i]:
        j -= 1
    return {"kind": "lost", "lost": lost, "span": [j, i]}


def analyse(cap, refs):
    """One revolution's status and, per sector whose data CRC fails, how; a loss's
    span in track bytes and the timer B wraps (stamp clock) inside it."""
    data = np.asarray(cap.data, np.uint8)
    t0, t1 = float(cap.rev_start_us[0]), float(cap.rev_end_us[0])
    per = (t1 - t0) / max(len(data) - 1, 1)
    wraps = (np.arange(np.ceil(t0 / WRAP) * WRAP, t1, WRAP) - t0) / per
    out = []
    for s in mfm.decode_track(data).sectors:
        if s["data_ok"] or s["r"] not in refs or s["data_pos"] < 0:
            continue
        start = int(s["data_pos"]) + 1
        row = {"r": int(s["r"])} | (
            classify(data[start : start + mfm.SECTOR_BYTES + 2], refs[s["r"]])
            or {"kind": "crc"}
        )
        if row["kind"] == "lost":
            lo, hi = (start + v for v in row["span"])
            row["span"] = [lo, hi]
            row["wraps_in_span"] = [
                round(float(w), 1) for w in wraps if lo - 2 <= w <= hi + 2
            ]
        out.append(row)
    return {
        "status": int(cap.rev_status[0]),
        "bytes": len(data),
        "us": round(t1 - t0, 1),
        "wrap_bytes": [round(float(w), 1) for w in wraps],
        "bad": out,
    }


def fields(data, refs):
    """Kinds of the bad data fields of complete sectors in data (classify)."""
    out = []
    for s in mfm.decode_track(data).sectors:
        start = int(s["data_pos"]) + 1
        if (
            s["data_ok"]
            or s["r"] not in refs
            or start + mfm.SECTOR_BYTES + 2 > len(data)
        ):
            continue
        got = classify(data[start : start + mfm.SECTOR_BYTES + 2], refs[s["r"]])
        out.append(f"r{s['r']} {(got or {'kind': 'crc'})['kind']}")
    return out


def sweep(drive, pads, reps, refs):
    """Read Track at each DRQ poll period (drive/rtpoll.s): status, bytes, bad fields."""
    out = []
    for pad in tqdm(pads, desc="poll periods", unit="pad"):
        for _ in range(reps):
            status, n, ring = drive.poll_track(pad)
            out.append(
                {
                    "pad": pad,
                    "pass_cycles": 18 + 5 * pad,
                    "max_wait_cycles": 31 + 5 * pad,
                    "status": status,
                    "bytes": n,
                    "bad": fields(ring, refs),
                }
            )
            tqdm.write(
                f"pass {18 + 5 * pad:3d}  ${status:02X}  {n} bytes  {out[-1]['bad']}"
            )
    return out


def run(
    drive, cylinder, side, captures, max_steps, npz=None, pads=(), reps=3
):  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    """Home, reference sectors, streamed Read Sector, then the captures."""
    drive.motor(True)
    plan = disk1581.dry(drive)
    disk1581.home(drive, plan, max_steps)
    drive.seek(cylinder)
    drive.side(side)
    tid = cylinder
    refs, ref_status = {}, {}
    for r in tqdm(range(1, mfm.SECTORS + 1), desc="readsec", unit="sec"):
        data, status = drive.read_sector(tid, r)
        ref_status[r] = int(status)
        if not status & 0x1C and len(data) == mfm.SECTOR_BYTES:
            refs[r] = data
    polled = sweep(drive, pads, reps, refs)
    streamed = [int(st) for _, _, _, st in drive.read_sectors(tid, 1, mfm.SECTORS)]
    revs, caps = [], [
        MfmCapture(
            "track",
            cylinder,
            0,
            meta={"readsec": sorted(refs)},
            data=np.concatenate(list(refs.values())),
        )
    ]
    for _ in tqdm(range(captures), desc="read track", unit="rev"):
        caps.append(drive.read_track(1))
        revs.append(analyse(caps[-1], refs))
        tqdm.write(line(revs[-1]))
    if npz:
        save_captures(npz, caps)
    return {
        "cylinder": cylinder,
        "side": side,
        "period_us": drive.period_us,
        "readsec_status": ref_status,
        "stream_readsec_status": streamed,
        "polled": polled,
        "tally": tally(revs),
        "revolutions": revs,
    }


def line(rev):
    """A capture's summary line."""
    bad = " ".join(
        f"r{b['r']} {b['kind']} {b.get('span', b.get('offset', ''))}"
        + (f" wraps {b['wraps_in_span']}" if "wraps_in_span" in b else "")
        for b in rev["bad"]
    )
    return f"${rev['status']:02X} {rev['bytes']} bytes  {bad or 'clean'}"


def tally(revs):
    """Bad fields by kind; losses with a timer B wrap in their span."""
    rows = [b for r in revs for b in r["bad"]]
    lost = [b for b in rows if b["kind"] == "lost"]
    return {
        "revolutions": len(revs),
        "status": sorted({r["status"] for r in revs}),
        "kinds": {
            k: sum(b["kind"] == k for b in rows) for k in {b["kind"] for b in rows}
        },
        "lost_with_wrap": sum(bool(b["wraps_in_span"]) for b in lost),
        "lost": len(lost),
    }


def main():
    """CLI."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dev", type=int, default=9)
    ap.add_argument("--cylinder", type=int, default=39)
    ap.add_argument("--side", type=int, choices=(0, 1), default=0)
    ap.add_argument("--max-steps", type=int, required=True)
    ap.add_argument("--captures", type=int, default=20)
    ap.add_argument("--save", help="JSON report path")
    ap.add_argument("--npz", help="the references and captures (.npz)")
    ap.add_argument(
        "--pads", type=int, nargs="*", default=[], help="drive/rtpoll.s P_PAD sweep"
    )
    ap.add_argument("--reps", type=int, default=3, help="Read Tracks per pad")
    args = ap.parse_args()
    from nybulah.opencbm import OpenCBM  # pylint: disable=import-outside-toplevel

    with OpenCBM() as cbm, r1581.session(cbm, args.dev, "s4") as drive:
        report = run(
            drive,
            args.cylinder,
            args.side,
            args.captures,
            args.max_steps,
            args.npz,
            args.pads,
            args.reps,
        )
    print(json.dumps({k: v for k, v in report.items() if k != "revolutions"}))
    if args.save:
        with open(args.save, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=1)


if __name__ == "__main__":
    main()
