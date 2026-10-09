"""Opt-in: NYBULAH_CORPUS names a directory of NIB images (loose or zipped)."""

import os
import pathlib
import zipfile

import pytest
from tqdm import tqdm

from nybulah.analysis.capture import framed_capture
from nybulah.analysis.cycle import TrackKind, find_cycle, header_period, lag_window
from nybulah.formats.nib import read_nib

CORPUS = os.environ.get("NYBULAH_CORPUS")
SAMPLE = int(os.environ.get("NYBULAH_CORPUS_SAMPLE", "20"))


def _images(root):
    root = pathlib.Path(root)
    for path in sorted(root.rglob("*")):
        if path.suffix.lower() == ".nib":
            yield path.read_bytes()
        elif path.suffix.lower() == ".zip":
            with zipfile.ZipFile(path) as z:
                for name in z.namelist():
                    if name.lower().endswith(".nib"):
                        yield z.read(name)


@pytest.mark.skipif(not CORPUS, reason="NYBULAH_CORPUS not set")
def test_corpus_periods_match_headers():
    agree = checked = outside = formatted = 0
    for raw, _ in tqdm(zip(_images(CORPUS), range(SAMPLE)), total=SAMPLE, desc="nib"):
        for entry in read_nib(raw).entries:
            if entry.halftrack % 2:
                continue
            cap = framed_capture(entry.data)
            cycle = find_cycle(cap, entry.zone)
            lo, hi = lag_window(entry.zone)
            if cycle.kind == TrackKind.FORMATTED and cycle.segments:
                formatted += 1
                outside += (
                    not lo - 8 * cycle.segments
                    <= cycle.length
                    <= hi + 8 * cycle.segments
                )
            ref = header_period(cap, entry.zone)
            if ref is not None:
                checked += 1
                agree += (
                    cycle.kind == TrackKind.FORMATTED
                    and abs(cycle.length - ref[0]) <= ref[1]
                )
    print(f"agree {agree}/{checked}, formatted {formatted}, outside window {outside}")
    assert checked and agree == checked and not outside
