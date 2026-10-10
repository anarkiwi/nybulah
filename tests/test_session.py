"""A Nibbler session holds DOS's zero page: a drive that leaves the monitor then (idle
watchdog, stuck routine) is reported by a DriveLost naming why and a bus reset; the
monitor's exit puts DOS's listen and talk addresses back, so DOS answers meanwhile."""

import functools

import pytest
from test_stream import rig

from nybulah import monitor
from nybulah.bus import recover
from nybulah.link import WATCHDOG_IDLE_S
from nybulah.monitor import DriveLost, Monitor, answers
from nybulah.nibbler import READ
from nybulah.sim import LISTEN, LSNADR, Drive1541
from nybulah.simdisk import Media
from nybulah.simhost import SimCBM

HALFTRACK = 40
JMP = 0x4C


def test_dos_answers_only_its_listen_address():
    cbm = SimCBM(Drive1541(device=9), dev=9)
    assert answers(cbm.status(9))
    cbm.drive.write(LSNADR, 0x0D)
    assert cbm.status(9).startswith("99")
    cbm.reset()
    assert answers(cbm.status(9)) and cbm.drive.read(LSNADR) == LISTEN | 9


def test_idle_out_mid_session_is_lost_and_reset():
    cbm, _, nib = rig(Media({}), halftrack=HALFTRACK, timeout_us=2_000_000)
    nib.capture(HALFTRACK, timing="none")
    assert cbm.drive.read(LSNADR) != LISTEN | 9
    cbm.host_wait(1.2 * WATCHDOG_IDLE_S)
    assert cbm.drive.halted and cbm.drive.responsive and answers(cbm.status(9))
    with pytest.raises(DriveLost, match="no command for .* the 1571 nibbler") as e:
        nib.close()
    assert answers(e.value.recovered) and answers(cbm.status(9))
    assert nib.mon.holding is None and not nib.mon.running
    nib.close()


def test_stuck_routine_is_named_and_reset():
    cbm, _, nib = rig(Media({}), halftrack=HALFTRACK, timeout_us=2_000_000)
    nib.capture(HALFTRACK, timing="none")
    nib.mon.write(READ, bytes([JMP, READ & 0xFF, READ >> 8]))
    with pytest.raises(DriveLost, match=r"J \$0303 \(read\) failed") as e:
        nib.capture(HALFTRACK, timing="none")
    assert answers(e.value.recovered) and not nib.mon.link.fast
    nib.close()
    assert answers(cbm.status(9))


def test_routine_names(make_rig):
    _, nib = make_rig("1571")
    assert nib.routines[READ] == "read" and str(nib) == "the 1571 nibbler"
    assert nib.mon.holding is nib
    nib.close()
    assert nib.mon.holding is None


def test_lost_drive_that_stays_silent(monkeypatch):
    monkeypatch.setattr(monitor, "recover", functools.partial(recover, timeout=0.01))
    t = [0.0]
    cbm = SimCBM(Drive1541(device=9, resets_to_boot=3), dev=9)
    mon = Monitor(cbm, 9, "s1", clock=lambda: t[0])
    mon.start()
    mon.holding = "a session"
    t[0] += 2 * mon.idle_s
    with pytest.raises(
        DriveLost, match="a session held DOS's zero page; device 9 silent"
    ):
        mon.read(0x0300, 1)
    assert mon.holding is None and not mon.running
