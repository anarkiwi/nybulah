import numpy as np
import pytest

from nybulah.formats import G64, G64Track
from nybulah.sim import SimIOWrite
from nybulah.simdisk import HT_MAX, HT_STOP, Media, SimMonitor, disk_drive


def test_stepper_stops_on_the_detent_of_its_phase():
    drive = disk_drive("1541", Media())
    mech = drive.mech
    for phase in (3, 2, 1, 0) * 30:
        drive.write(0x1C00, phase)
    assert mech.halftrack == HT_STOP + ((0 - HT_STOP) & 3)
    for k in range(200):
        drive.write(0x1C00, (k + 1) & 3)
    assert HT_MAX - 3 <= mech.halftrack <= HT_MAX
    assert mech.halftrack & 3 == drive.read(0x1C00) & 3


def test_media_noise_and_g64():
    image = G64({4: G64Track(np.full(10, 0xFF, np.uint8), 3)})
    media = Media.from_g64(image)
    assert media.cells(0, 4).all() and len(media.cells(0, 4)) == 80
    noise = media.cells(0, 70)
    assert media.cells(0, 70) is noise and 0.4 < noise.mean() < 0.6


def test_io_decoding():
    drive = disk_drive("1571", Media())
    drive.write(0x2000, 0xD0)
    assert drive.mech.wd_command == 0xD0
    with pytest.raises(SimIOWrite):
        drive.write(0x2001, 0)
    with pytest.raises(SimIOWrite):
        drive.write(0x1C10, 0)
    drive.write(0x1C0E, 0x7F)
    assert drive.read(0x1C0E) == 0x7F and drive.read(0x2001) == 0
    assert drive.read(0x1C02) == 0x6F and drive.read(0x1C0D) == 0
    drive1541 = disk_drive("1541", Media())
    assert drive1541.mech.side == 0


def test_motor_off_stops_byte_ready():
    drive = disk_drive("1541", Media())
    drive.write(0x1C00, 0x04)
    drive.cycles += 1000
    drive.mech.update(drive.cycles)
    assert drive.mech.due < np.inf
    drive.write(0x1C00, 0x00)
    assert drive.mech.due == np.inf


def test_sim_monitor_budget():
    drive = disk_drive("1541", Media())
    mon = SimMonitor(drive, budget=100)
    mon.write(0x0300, bytes([0x4C, 0x00, 0x03]))
    assert mon.read(0x0300, 3) == bytes([0x4C, 0x00, 0x03])
    with pytest.raises(TimeoutError):
        mon.jsr(0x0300)
