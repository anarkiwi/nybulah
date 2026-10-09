import numpy as np
import pytest

from nybulah.formats import D64, d64_to_g64
from nybulah import simfast
from nybulah.nibbler import Nibbler
from nybulah.simdisk import Media, disk_drive
from nybulah.simhost import SimMonitor


def random_d64(seed=1, tracks=35):
    rng = np.random.default_rng(seed)
    n = {35: 683, 40: 768}[tracks]
    return D64(rng.integers(0, 256, (n, 256), dtype=np.uint8))


def rig(model="1541", media=None, **kw):
    """(drive, nibbler) over a SimMonitor, delays shortened for simulation."""
    drive = disk_drive(model, media if media is not None else Media(), **kw)
    nib = Nibbler(
        SimMonitor(drive),
        model,
        stepms=1,
        settle_ms=1,
        spinup_s=0,
        sleep=lambda s: None,
    )
    return drive, nib.open()


@pytest.fixture(name="compiled_simulator", scope="session", autouse=True)
def compiled_simulator_fixture():
    """Compile the drive simulator before any test times a bus wait."""
    simfast.warm()


@pytest.fixture(name="image", scope="session")
def image_fixture():
    return random_d64()


@pytest.fixture(name="g64", scope="session")
def g64_fixture(image):
    return d64_to_g64(image, progress=False)


@pytest.fixture(name="make_rig")
def make_rig_fixture():
    return rig
