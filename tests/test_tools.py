import importlib
import json
import pathlib
import sys

import numpy as np

from nybulah.analysis import pattern as pt
from nybulah.nibbler import Capture

sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / "tools"))
event_landings, pattern_sets, pattern_windows = (
    importlib.import_module(m)
    for m in ("event_landings", "pattern_sets", "pattern_windows")
)

HW = pathlib.Path(__file__).parent / "data" / "hw" / "pattern" / "h30b"


def test_pattern_sets_tabulate_against_an_earlier_run(tmp_path):
    dest = pattern_sets._run(str(HW), tmp_path)  # pylint: disable=protected-access
    report = json.loads(dest.read_text())
    assert set(pattern_sets.scores(report)) == {p.name for p in HW.glob("*.npz")}
    lines = pattern_sets.table(tmp_path, tmp_path)
    assert len(lines) == 2 and all(dest.stem in line for line in lines)


def test_event_landings_place_both_timing_passes():
    truth = pt.Truth.from_json(json.loads((HW / "truth.json").read_text()))
    cap = Capture.load(HW / "c1571-ram-2.npz")
    tb, ts, rev = event_landings.landings(cap)
    assert rev and tb and ts and all(m for *_, m in ts)
    names, pos = event_landings.byte_regions(truth, np.asarray(cap.data, np.uint8))
    assert len(names) == len(pos) == len(cap.data) + 1
    assert "resync.1" in {names[p] for _, _, p, _ in tb}


def test_pattern_windows_find_only_unwritten_windows():
    truth = pt.Truth.from_json(json.loads((HW / "truth.json").read_text()))
    track = pattern_windows.track(truth)
    assert pattern_windows.absent_runs(track[:4000], truth, 32) == []
    bad = track[:4000].copy()
    bad[2000:2040] = 0
    runs = pattern_windows.absent_runs(bad, truth, 32)
    assert runs and runs[0][0] <= 2000 < 2040 <= runs[-1][1]
    assert (
        pattern_windows.windows(np.ones(33, np.uint8), 32).tolist() == [2**32 - 1] * 2
    )
