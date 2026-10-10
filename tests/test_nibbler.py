import numpy as np
import pytest

from nybulah.analysis.gcr import speed_zone, to_bits
from nybulah.analysis.sector import SectorError, decode_track
from nybulah.nibbler import (
    NPAGES,
    ST_FULL,
    ST_KILLER,
    ST_NOSYNC,
    Capture,
    Nibbler,
    ZP_SIZE,
    TrackError,
    t2_value,
    unwrap,
)
from nybulah.simdisk import CPU_HZ, Media, log_bytes, true_syncs

ZONE_TRACKS = (1, 18, 25, 31)


def logged(drive):
    drive.mech.log = []
    return drive.mech.log


@pytest.mark.parametrize("model", ["1541", "1571"])
@pytest.mark.parametrize("track", ZONE_TRACKS)
def test_dos_capture_decodes(make_rig, g64, image, model, track):
    drive, nib = make_rig(model, Media.from_g64(g64))
    log = logged(drive)
    cap = nib.capture(2 * track, start="now")
    pos, runs = true_syncs(log, cap.data)
    assert len(cap.data) == NPAGES * 256 and cap.status == 0 and cap.lost == 0
    assert (cap.positions == pos).all() and (np.abs(cap.sync_bits - runs) <= 1).all()
    assert cap.density == speed_zone(track) and not cap.overrun_risk
    assert cap.byte_cycles == pytest.approx(
        8 * CPU_HZ * 60 / (300 * len(to_bits(g64.tracks[2 * track].data))), rel=2e-3
    )
    dec = decode_track(cap.bits(), track, image.disk_id)
    assert (dec.errors == SectorError.OK).all()
    assert (dec.data == image.data[image.span(track)]).all()


def test_hidden_ones_complete_the_run(make_rig, g64):
    _, nib = make_rig("1541", Media.from_g64(g64))
    cap = nib.capture(36, start="sync")
    assert cap.status == 0 and (cap.hidden == cap.sync_bits - cap.latched).all()
    bits = cap.bits()
    assert len(bits) == 8 * len(cap.data) + cap.hidden.sum() + 10
    cap = nib.capture(36, start="sync", timing="none")
    assert cap.positions.size == 0 and cap.tb is None and cap.ts is None
    with pytest.raises(ValueError):
        nib.capture(36, timing="later")


def test_index_start_and_period(make_rig, g64):
    drive, nib = make_rig("1571", Media.from_g64(g64, rpm=297.0))
    log = logged(drive)
    cap = nib.capture(36, start="index")
    assert cap.rpm == pytest.approx(297.0, rel=1e-4)
    stream, times = log_bytes(log)
    t0 = times[stream.find(cap.data.tobytes())]
    rev = 60.0 * CPU_HZ / 297.0
    assert 0 <= t0 % rev < 0.03 * rev


def test_model_gates(make_rig):
    _, nib = make_rig("1541")
    with pytest.raises(ValueError):
        nib.capture(36, start="index")
    with pytest.raises(ValueError):
        nib.capture(36, side=1)
    with pytest.raises(ValueError):
        nib.capture(36, start="later")
    with pytest.raises(ValueError):
        nib.capture(90)
    with pytest.raises(ValueError):
        Nibbler(None, "1581")


def test_side_select_reads_the_other_head(make_rig, g64):
    media = Media.from_g64(g64)
    media.add_g64(g64, side=1)
    media.tracks[(1, 36)] = np.zeros_like(media.tracks[(1, 36)])
    drive, nib = make_rig("1571", media)
    assert nib.capture(36, side=1, start="sync").status & ST_NOSYNC
    assert drive.mech.side == 1
    assert nib.capture(36, side=0, start="sync").status == 0


def test_1541_locates_from_dos_track_without_bumping(make_rig):
    drive, nib = make_rig("1541")
    nib.capture(36, start="now")
    assert drive.mech.halftrack == 36 == nib.halftrack and drive.mech.bumps == 0
    nib.seek(71)
    assert drive.mech.halftrack == 71


def test_headers_override_dos_track(make_rig, g64):
    drive, nib = make_rig("1541", Media.from_g64(g64))
    drive.write(0x22, 10)
    assert nib.locate() == 36 and drive.mech.bumps == 0


def test_bump_needs_opt_in(make_rig):
    drive, nib = make_rig("1541")
    drive.write(0x22, 0)
    with pytest.raises(TrackError, match="allow_bump"):
        nib.capture(36)
    nib.allow_bump = True
    nib.capture(36)
    assert drive.mech.bumps > 0 and drive.mech.halftrack == 36
    nib.bump()
    assert drive.mech.halftrack == 2 == nib.halftrack


def test_write_then_capture(make_rig):
    drive, nib = make_rig("1541")
    payload = np.random.default_rng(3).integers(0, 256, 300, dtype=np.uint8)
    track = b"\x55" * 64 + b"\xff" * 5 + b"\x52" + payload.tobytes() + b"\x55" * 64
    nib.write_track(10, track, density=3, pad=0x55)
    assert drive.mech.underruns <= 1 and not drive.mech.writing
    cap = nib.capture(10, density=3, start="sync")
    assert b"\x52" + payload.tobytes() in cap.data.tobytes()
    with pytest.raises(ValueError):
        nib.write_track(10, b"\x00" * 100)


def test_write_times_out_safely(make_rig):
    drive, nib = make_rig("1541")
    nib.seek(10)
    stop = drive.mech.media.time_at

    def stalled(turns):
        return np.inf if drive.mech.writing else stop(turns)

    drive.mech.media.time_at = stalled
    drive.mech.media.turns = lambda now: 0.0
    with pytest.raises(TrackError, match="stopped"):
        nib.write_track(10, b"\x55" * 256)
    assert not drive.mech.writing and drive.mech.pcr & 0x0E == 0x0C


def test_write_protect(make_rig):
    drive, nib = make_rig("1541", write_protect=True)
    before = drive.mech.media.cells(0, 36).copy()
    with pytest.raises(TrackError, match="protected"):
        nib.write_track(36, b"\x00" * 256)
    assert (drive.mech.media.cells(0, 36) == before).all()


def test_index_write_needs_index(make_rig):
    _, nib = make_rig("1571")
    nib.write_track(36, b"\x55" * 256, start="index")
    drive, nib = make_rig("1571")
    drive.mech.angle = lambda now: 0.5
    with pytest.raises(TrackError, match="index"):
        nib.write_track(36, b"\x55" * 256, start="index")


def test_killer_and_crowded_syncs(make_rig):
    media = Media()
    media.tracks[(0, 36)] = np.ones(61538, np.uint8)
    crowded = np.tile(to_bits(np.frombuffer(b"\x55\x55\xff\xff", np.uint8)), 1923)
    media.tracks[(0, 38)] = crowded
    drive, nib = make_rig("1541", media)
    cap = nib.capture(36, start="now")
    assert cap.status & ST_KILLER and len(cap.data) < NPAGES * 256
    log = logged(drive)
    cap = nib.capture(38, start="sync")
    assert cap.status & ST_FULL
    pos, runs = true_syncs(log, cap.data)
    lo, hi = cap.sync_bounds
    assert (cap.positions == pos).all() and (lo <= runs).all()
    assert ((hi < 0) | (runs <= hi)).all() and (hi[-10:] == -1).all()
    tight = (hi >= 0) & (hi - lo <= 2)
    assert (
        tight.sum() > len(hi) // 20 and (np.abs(cap.sync_bits - runs)[tight] <= 1).all()
    )


def test_capture_record_round_trip(make_rig, g64, tmp_path):
    _, nib = make_rig("1571", Media.from_g64(g64))
    cap = nib.capture(36, start="index")
    cap.save(tmp_path / "c.npz")
    back = Capture.load(tmp_path / "c.npz")
    assert (back.bits() == cap.bits()).all()
    assert back.rpm == cap.rpm and back.start == "index" and back.model == "1571"


def test_close_restores_state(make_rig):
    drive, nib = make_rig("1571")
    nib.close()
    drive.via1.regs[1] = 0x20
    zp = drive.dump(0x60, ZP_SIZE)
    pcr = drive.mech.pcr
    nib.open()
    assert not drive.via1.regs[1] & 0x20
    nib.capture(36)
    nib.close()
    nib.close()
    assert drive.dump(0x60, ZP_SIZE) == zp and drive.mech.pcr == pcr
    assert drive.via1.regs[1] & 0xFE == 0x20 and not drive.mech.pb & 0x04


def test_timer_helpers():
    assert t2_value(3, 0x11) == 0x1203 and t2_value(4, 0x11) == 0x1104
    assert unwrap(0x1000, 0x0F00, 0x100) == 0x100
    assert unwrap(0x1000, 0x0F00, 0x10100) == 0x10100


@pytest.mark.parametrize("proto", ["s1", "s3"])
def test_capture_through_monitor_transport(g64, image, proto):
    from nybulah.monitor import Monitor
    from nybulah.sim import Drive1541
    from nybulah.simhost import SimCBM
    from nybulah.simdisk import Mechanism
    from nybulah.simx import make

    if proto == "s1":
        cbm = SimCBM(Drive1541(device=10), dev=10)
    else:
        cbm = make("1541", dev=10, timeout_us=5e6)
    Mechanism(cbm.drive, Media.from_g64(g64))
    zp = cbm.drive.dump(0x60, ZP_SIZE)
    with (
        Monitor(cbm, 10, proto) as mon,
        Nibbler(
            mon, "1541", stepms=1, settle_ms=1, spinup_s=0, sleep=lambda s: None
        ) as nib,
    ):
        cap = nib.capture(36, start="sync")
    dec = decode_track(cap.bits(), 18, image.disk_id)
    assert (dec.data == image.data[image.span(18)]).all()
    cbm.settle()
    assert cbm.drive.dump(0x60, ZP_SIZE) == zp and cbm.drive.responsive
