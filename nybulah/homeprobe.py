"""1571 track 00 sensor and stepper phase probe, or a 1581's WD177x type I status
and track register; steps only with --step.

The dry run reads the sensor, position and homing plan; --step homes within --max-steps
(1571 outward steps, 1581 Restore pulses), then returns to the estimate.
"""

import json
import sys

from tqdm import tqdm

from . import disk1581, r1581, tool
from .monitor import Monitor, protocols
from .nibbler import HOME_HALFTRACK, MAX_HALFTRACK, SENSOR_EDGE, SENSOR_WALK
from .nibbler import Nibbler
from .ramprobe import identify_model

VIA1PA_NH, PA_TRK00 = 0x180F, 0x01
RAW_READS = 16


class Trace(list):
    """Homing readings, counted on a progress bar as they arrive."""

    def __init__(self, total):
        super().__init__()
        self.progress = tqdm(total=total, desc="sensor readings", disable=None)

    def append(self, item):
        super().append(item)
        self.progress.update()


def plan(estimate, sensed):
    """``(outward, inward)`` step bounds homing works within."""
    start = SENSOR_EDGE + 1 if sensed or estimate is None else estimate
    return max(start - HOME_HALFTRACK, 0), SENSOR_WALK if sensed else 0


def dry(nib, headers=False):
    """Everything readable without stepping, and the homing plan."""
    raw = [nib.mon.read(VIA1PA_NH, 1)[0] & PA_TRK00 for _ in range(RAW_READS)]
    sensed, phase = nib.sense()
    dos = nib.mon.read(0x22, 1)[0]
    estimate = nib.estimate(headers)
    outward, inward = plan(estimate, sensed)
    return {
        "pa0": raw,
        "sensed": sensed,
        "phase": phase,
        "dos_track": dos,
        "estimate": estimate,
        "outward_steps": outward,
        "inward_steps": inward,
    }


def step(nib, report, max_steps):
    """Home within max_steps outward steps, then step back; adds the readings."""
    outward = report["outward_steps"]
    if outward > max_steps:
        raise ValueError(f"homing needs {outward} outward steps, over {max_steps}")
    trace = Trace(outward + report["inward_steps"] + 1)
    try:
        nib.home(report["estimate"], trace)
    finally:
        trace.progress.close()
        report["trace"] = [list(t) for t in trace]
    on = [x for x, sensed, _ in trace if sensed]
    report["sensor_edge"] = max(on, default=None)
    back = report["estimate"]
    if back is not None and HOME_HALFTRACK <= back <= MAX_HALFTRACK:
        nib.seek(back)
    return report


def add_arguments(ap):
    """Command line options."""
    ap.add_argument("--dev", type=int, default=8)
    ap.add_argument("--transport", choices=protocols() or ("s1",), default="s1")
    ap.add_argument("--headers", action="store_true", help="also read headers")
    ap.add_argument("--step", action="store_true", help="home the head")
    ap.add_argument(
        "--max-steps",
        type=int,
        default=MAX_HALFTRACK - HOME_HALFTRACK,
        help="refuse --step when homing needs more outward steps",
    )


def execute(args, cbm):
    """Probe and print the report as JSON."""
    model = identify_model(cbm, args.dev)
    if model == "1581":
        with r1581.session(cbm, args.dev, args.transport) as drive:
            report = disk1581.dry(drive, args.headers)
            if args.step:
                disk1581.home(drive, report, args.max_steps)
        print(json.dumps(report))
        return report
    if model != "1571":
        raise ValueError(f"device {args.dev} is a {model}: the probe needs a 1571/1581")
    with Monitor(cbm, args.dev, args.transport) as mon, Nibbler(mon, model) as nib:
        report = dry(nib, args.headers)
        if args.step:
            step(nib, report, args.max_steps)
    print(json.dumps(report))
    return report


def main(argv=None, cbm=None):
    """CLI entry point."""
    return tool.standalone(sys.modules[__name__], argv, cbm)


if __name__ == "__main__":
    main()
