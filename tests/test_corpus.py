"""Opt-in: NYBULAH_CORPUS names a directory of NIB/NBZ images (loose or zipped)."""

import os
import pathlib
import zipfile

import pytest
from tqdm import tqdm

from nybulah.analysis.cycle import TrackKind, header_period, lag_window
from nybulah.formats import loads
from nybulah.formats.image import revolution

CORPUS = os.environ.get("NYBULAH_CORPUS")
SAMPLE = int(os.environ.get("NYBULAH_CORPUS_SAMPLE", "20"))
SUFFIXES = (".nib", ".nbz")


def _images(root):
    """``(name, bytes)`` of every NIB-family image."""
    for path in sorted(pathlib.Path(root).rglob("*")):
        if path.suffix.lower() in SUFFIXES:
            yield path.name, path.read_bytes()
        elif path.suffix.lower() == ".zip":
            with zipfile.ZipFile(path) as z:
                for name in z.namelist():
                    if name.lower().endswith(SUFFIXES):
                        yield name, z.read(name)


def _sample(root, count):
    """Up to half of ``count`` images of each suffix."""
    taken = {s: [] for s in SUFFIXES}
    for name, raw in _images(root):
        bucket = taken[name.lower()[-4:]]
        if len(bucket) < (count + 1) // 2:
            bucket.append(raw)
        if all(len(b) >= (count + 1) // 2 for b in taken.values()):
            break
    return [raw for bucket in taken.values() for raw in bucket]


@pytest.mark.skipif(not CORPUS, reason="NYBULAH_CORPUS not set")
def test_corpus_periods_match_headers():
    agree = checked = outside = formatted = 0
    for raw in tqdm(_sample(CORPUS, SAMPLE), desc="images"):
        for key, captures in loads(raw).tracks.items():
            if key % 2:
                continue
            for cap in captures:
                bits, cycle = revolution(cap)
                lo, hi = lag_window(cap.zone)
                slack = cap.framed.sync_error * max(cycle.segments, 1)
                if cycle.kind == TrackKind.FORMATTED:
                    formatted += 1
                    outside += not lo - slack <= len(bits) <= hi + slack
                ref = header_period(cap.framed, cap.zone)
                if ref is not None:
                    checked += 1
                    agree += (
                        cycle.kind == TrackKind.FORMATTED
                        and abs(cycle.length - ref[0]) <= ref[1] + slack
                    )
    print(f"agree {agree}/{checked}, formatted {formatted}, outside window {outside}")
    assert checked and agree == checked and not outside
