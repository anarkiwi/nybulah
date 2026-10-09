"""Survey rows of fixed images against rows recorded before regions were introduced.

``NYBULAH_WRITE_GOLDEN=1`` rewrites ``tests/data/survey_golden.npz``.
"""

import os
import pathlib

import numpy as np
import pytest
from test_survey import _nib_disk, weak_nb2

from nybulah import survey
from nybulah.formats import loads, to_g64, write_g64, write_nib

DATA = pathlib.Path(__file__).parent / "data"
GOLDEN = DATA / "survey_golden.npz"
FLOATS = ("sim_half", "sim_half_z", "sim_next", "sim_next_z", "mp_disagree")


def _images():
    nib = loads(write_nib(_nib_disk()))
    g64 = loads(write_g64(to_g64(nib)))
    hw = survey.DiskImage("capture", survey.load_captures(DATA / "hw"))
    return {
        "nib": (nib, g64),
        "g64": (g64, None),
        "hw": (hw, None),
        "nb2": (weak_nb2(), None),
    }


def _survey():
    out = {}
    for name, (image, pair) in _images().items():
        rows, lengths, sync_rows, ids = survey.survey_image(image, pair)
        out |= {
            f"{name}_rows": rows,
            f"{name}_sync_len": lengths,
            f"{name}_sync_row": sync_rows,
            f"{name}_ids": np.array(ids),
        }
    return out


@pytest.fixture(name="current", scope="module")
def current_fixture():
    out = _survey()
    if os.environ.get("NYBULAH_WRITE_GOLDEN"):
        np.savez_compressed(GOLDEN, **out)
    return out


@pytest.mark.parametrize("name", ["nib", "g64", "hw", "nb2"])
def test_survey_matches_golden(current, name):
    with np.load(GOLDEN) as golden:
        for part in ("sync_len", "sync_row", "ids"):
            np.testing.assert_array_equal(
                current[f"{name}_{part}"], golden[f"{name}_{part}"]
            )
        rows, ref = current[f"{name}_rows"], golden[f"{name}_rows"]
        assert rows.dtype == ref.dtype and len(rows) == len(ref)
        for field in rows.dtype.names:
            if field in FLOATS:
                np.testing.assert_allclose(rows[field], ref[field], rtol=1e-9)
            else:
                np.testing.assert_array_equal(rows[field], ref[field], err_msg=field)
