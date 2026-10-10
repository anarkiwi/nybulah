"""1581 drive code (drive/mfm.s, drive/mfmstream.s) on the simulated 1581: transports,
bounded homing, sector and track I/O, streams; no head ever reaches a stop."""

import json

import numpy as np
import pytest

from nybulah import cli, disk1581, monitor, r1581, simsrq, simx
from nybulah import mfmstream as ms
from nybulah.analysis import mfm
from nybulah.formats import d81
from nybulah.formats.mfmcap import load_captures
from nybulah.link import BASE, WATCHDOG_IDLE_S
from nybulah.monitor import Monitor, drivecode
from nybulah.sim import Drive1581
from nybulah.r1581 import ST_BUSY, ST_T0
from nybulah.simwd import CPU_HZ, NEVER, STATUS_VALID, T0_NEVER, WTRK, MfmMedia, Wd
from nybulah.simwd import stream_track

DEV = 9
BAM = 0x0A00  # equate.src bam1


def sectors(cyl, side):
    """Ten distinct 512-byte sectors without $F5-$F7 (Write Track writes them)."""
    rng = np.random.default_rng(cyl * 2 + side)
    data = rng.integers(0, 0xF5, (mfm.SECTORS, mfm.SECTOR_BYTES), dtype=np.uint8)
    return data


@pytest.fixture(name="media", scope="module")
def media_fixture():
    return MfmMedia.formatted(sectors=sectors)


def no_stops(drive):
    drive.sync()
    wd = drive.wd
    assert (wd.bumps, wd.inner_stops, wd.violations, wd.early) == (0, 0, 0, 0)


def ram_rig(media, cylinder=37, protocol="s1", **wd):
    """(sim drive, opened Mfm1581) under an untimed monitor."""
    cbm = simx.adapter(
        protocol, Drive1581, device=DEV, media=media, cylinder=cylinder, **wd
    )
    cbm.budget = 20_000_000
    mon = Monitor(cbm, DEV, protocol)
    mon.start()
    sim = cbm.drives[DEV] if hasattr(cbm, "drives") else cbm.drive
    return sim, r1581.Mfm1581(mon, sleep=lambda s: None, spinup_s=0).open()


def stream_rig(media, cylinder=37, **kw):
    """(adapter, sim drive, opened Mfm1581) on the timed 1581 with the v12 adapter."""
    wd = {k: kw.pop(k) for k in ("tr00", "write_protect") if k in kw}
    kw.setdefault("timeout_us", WATCHDOG_IDLE_S * 1e6)
    cbm = simsrq.make(model="1581", dev=DEV, firmware=12, **kw)
    cbm.drive.wd = Wd(media, cylinder=cylinder, **wd)
    mon = Monitor(cbm, DEV, "s4", clock=lambda: cbm.now * 1e-6)
    mon.start()
    drive = r1581.Mfm1581(mon, sleep=lambda s: None, spinup_s=0).open()
    return cbm, cbm.drive, drive


def payload(cyl, side, r):
    """The 512 data bytes of sector r as formatted."""
    return sectors(cyl, side)[r - 1]


@pytest.mark.parametrize("protocol", ["s1", "s3", "s4"])
def test_transports_move_memory(protocol, media):
    if protocol == "s4":
        _, sim, drive = stream_rig(media)
    else:
        sim, drive = ram_rig(media, protocol=protocol)
    data = np.random.default_rng(1).integers(0, 256, 700, dtype=np.uint8).tobytes()
    drive.mon.write(r1581.BUFFER, data)
    assert drive.mon.read(r1581.BUFFER, len(data)) == data
    drive.close()
    drive.mon.stop()
    no_stops(sim)


def test_sense_estimate_and_bounded_home(media):
    sim, drive = ram_rig(media, cylinder=37)
    state = drive.sense()
    assert not state["t0"] and not state["motor"]
    drive.motor(True)
    assert abs(drive.period_us - 200_000) < 50
    assert drive.estimate() == (37, "id")
    with pytest.raises(r1581.TrackError) as short:
        drive.home(20)
    trace = short.value.trace
    assert trace["busy_seen"] and trace["end"] == "deadline" and drive.cylinder is None
    assert trace["pulses"] == 20 == sim.wd.pulses and trace["track_register"] == 0xEB
    assert not trace["forced_status"] & r1581.ST_T0
    assert 19.5 * r1581.STEP_US <= trace["elapsed_us"] < 19.5 * r1581.STEP_US + 1000
    drive.home(17)
    trace = drive.home_trace
    assert trace["end"] == "deadline" and trace["pulses"] == 17 and trace["settled_t0"]
    assert sim.wd.pulses == 37 and trace["track_register"] == 0xEE
    assert trace["last_status"] & r1581.ST_T0 and trace["forced_status"] & r1581.ST_T0
    assert trace["result"] == 0
    drive.home(3)
    trace = drive.home_trace
    assert (
        trace["end"] == "busy_fell" and trace["pulses"] == 0 == trace["track_register"]
    )
    assert sim.wd.pulses == 37
    assert drive.cylinder == 0 and drive.sense()["t0"]
    no_stops(sim)


def test_home_never_passes_the_estimate_without_tr00(media):
    sim, drive = ram_rig(media, cylinder=12, tr00=T0_NEVER)
    drive.motor(True)
    with pytest.raises(r1581.TrackError) as err:
        drive.home(12)
    assert err.value.trace["pulses"] == 12 == sim.wd.pulses
    assert err.value.trace["end"] == "deadline"
    no_stops(sim)


def test_home_needs_tr00_again_after_settling(media, monkeypatch):
    sim, drive = ram_rig(media, cylinder=5)
    sense = drive.sense
    monkeypatch.setattr(drive, "sense", lambda: sense() | {"t0": False})
    with pytest.raises(r1581.TrackError, match="after settling") as err:
        drive.home(5)
    assert err.value.trace["result"] == 0 and not err.value.trace["settled_t0"]
    assert drive.cylinder is None and sim.wd.cylinder == 0
    with pytest.raises(r1581.TrackError, match="before homing"):
        drive.seek(3)
    no_stops(sim)


def test_tr00_is_read_once_an_idle_force_interrupt_clears_busy(media):
    sim, drive = ram_rig(media, cylinder=39, force_busy=4 * STATUS_VALID)
    drive.motor(True)
    drive.home(39)
    trace = drive.home_trace
    assert trace["settled_t0"] and trace["settled_status"] & (ST_BUSY | ST_T0) == ST_T0
    assert trace["pulses"] == 39 and trace["first_status"] == 0xA1
    assert (trace["last_status"], trace["forced_status"]) == (0xA5, 0xA4)
    no_stops(sim)


def test_sense_reads_tr00_at_rest_without_a_step_pulse(media):
    sim, drive = ram_rig(media, cylinder=0, force_busy=4 * STATUS_VALID)
    drive.motor(True)
    assert drive.read_id()["c"] == 0
    state = drive.sense()
    assert state["t0"] and state["status"] & ST_BUSY == 0
    assert drive.estimate() == (0, "tr00")
    drive.home(0)
    assert (drive.home_trace["result"], drive.home_trace["settled_t0"]) == (0, True)
    assert sim.wd.pulses == 0 and sim.wd.cylinder == 0
    no_stops(sim)


def test_restore_trace_of_a_command_the_wd_never_ran():
    trace = r1581.restore_trace(3, 0x80, 120, (0x80, 0x80, 0x80, 39))
    assert not trace["busy_seen"] and trace["end"] == "busy_fell"
    assert trace["pulses"] == 0 and trace["track_register"] == 39
    assert r1581.restore_trace(0, 0, 0, (0, 0, 0, 0)) == {"steps": 0, "result": 0}


def test_seek_bounds_and_ids(media):
    sim, drive = ram_rig(media, cylinder=5)
    drive.motor(True)
    drive.home(drive.estimate()[0])
    for cyl in (0, 40, 79, 3):
        drive.seek(cyl)
        assert drive.read_id()["c"] == cyl
    with pytest.raises(r1581.TrackError):
        drive.seek(r1581.MAX_CYL + 1)
    drive.close()
    no_stops(sim)


def test_read_sector_ram(media):
    sim, drive = ram_rig(media, cylinder=10)
    drive.motor(True)
    drive.home(10)
    drive.seek(10)
    drive.side(0)
    data, status = drive.read_sector(10, 4)
    assert status & (mfm.ST_CRC | mfm.ST_RNF) == 0
    assert np.array_equal(data, payload(10, 0, 4))
    _, status = drive.read_sector(10, 11)
    assert status & mfm.ST_RNF
    no_stops(sim)


# drive/mfm.s writetrk and writesec: a DRQ is written within 40 cycles of the
# 64-cycle byte.
WT_LEAD = 64 - 40


@pytest.mark.parametrize("layout", ["short_tokens", "cylinder_0"])
def test_write_track_loads_every_byte_wt_lead_ahead(layout):
    """The WD taking each byte WT_LEAD cycles before its slot still gets every one,
    whatever the tokens: short ones back to back, or cylinder 0 side 0 as formatted."""
    sim, drive = ram_rig(MfmMedia(cylinders=2), cylinder=0, drq_lead=WT_LEAD)
    drive.motor(True)
    drive.home(0)
    if layout == "short_tokens":
        dr = np.array([0x11, 0x22, 0x22, 0x22, 0x33, 0x44, 0x44, 0x44] * 400 + [0x4E])
    else:
        dr = mfm.unrle(mfm.plan_track(mfm.standard_layout(0, 0)).image)
    sim.wd.drq_slack = NEVER
    status = drive.write_track(mfm.rle(dr))
    data, _, _ = sim.wd.media.track(0, 1)
    assert not status & mfm.ST_LOST and sim.wd.drq_slack >= WT_LEAD
    assert np.array_equal(data, stream_track(dr, len(data))[0])
    no_stops(sim)


@pytest.mark.parametrize("deleted", [False, True])
def test_write_sectors_load_every_byte_wt_lead_ahead(deleted):
    """Ten sectors back to back with the WD taking each byte WT_LEAD cycles early:
    all written, data and data mark ($F8 deleted, $FB normal) as given."""
    media = MfmMedia.formatted(cylinders=2)
    sim, drive = ram_rig(media, cylinder=0, drq_lead=WT_LEAD)
    drive.motor(True)
    drive.home(0)
    rows = np.random.default_rng(int(deleted)).integers(0, 256, (10, 512), np.uint8)
    sim.wd.drq_slack = NEVER
    status, written = drive.write_sectors(0, 1, rows, deleted)
    assert (written, status & mfm.ST_LOST) == (10, 0) and sim.wd.drq_slack >= WT_LEAD
    for r in (1, 10):
        data, st = drive.read_sector(0, r)
        assert np.array_equal(data, rows[r - 1]) and bool(st & 0x20) == deleted
    no_stops(sim)


def test_write_track_and_sectors_ram():
    media = MfmMedia.formatted(cylinders=4)
    sim, drive = ram_rig(media, cylinder=2)
    drive.motor(True)
    drive.home(2)
    drive.seek(3)
    drive.side(1)
    data = sectors(3, 1)
    data[2] = 0xF6
    specs = mfm.standard_layout(3, 1, data)
    specs[5].deleted = True
    plan = mfm.plan_track(specs)
    assert drive.write_track(plan.image) & (r1581.ST_WP | mfm.ST_LOST) == 0
    for first, rows, deleted in disk1581.write_runs(plan.writes):
        assert drive.write_sectors(3, first, rows, deleted)[1] == len(rows)
    for r in (1, 3, 6):
        got, status = drive.read_sector(3, r)
        assert np.array_equal(got, data[r - 1])
        assert bool(status & mfm.ST_DELETED) == (r == 6)
    drive.close()
    no_stops(sim)


def test_stream_read_track_revolutions(media):
    _, sim, drive = stream_rig(media, cylinder=20)
    writes = []
    write = sim.cia.write

    def hooked(reg, v, c):
        if reg == 12:
            writes.append(c)
        return write(reg, v, c)

    sim.cia.write = hooked
    drive.motor(True)
    drive.home(20)
    drive.seek(20)
    drive.side(0)
    cap = drive.read_track(2)
    assert cap.meta["adapter"] == "done" and cap.meta["drive_end"] == "done"
    want, _, _ = media.track(20, 1)
    for rev in cap.revolutions():
        assert len(want) - 1 <= len(rev) <= len(want)
        assert np.array_equal(rev, want[: len(rev)])
    turns = np.diff(cap.rev_start_us) / drive.period_us
    assert np.allclose(turns, np.round(turns), atol=1e-3) and (turns >= 1).all()
    assert (np.diff(writes) >= 40).all()
    span = cap.rev_end_us - cap.rev_start_us
    assert np.all(np.abs(span - len(want) * mfm.BYTE_US) < 2 * mfm.BYTE_US)
    no_stops(sim)


def test_stream_sectors_and_ids(media):
    _, sim, drive = stream_rig(media, cylinder=40)
    drive.motor(True)
    drive.home(40)
    drive.seek(40)
    drive.side(1)
    reads = drive.read_sectors(40, 1, 10)
    for _, r, data, status in reads:
        assert status & (mfm.ST_CRC | mfm.ST_RNF) == 0
        assert np.array_equal(data, payload(40, 1, r))
    ids = drive.read_ids(12)
    assert set(ids.ids[:, 2]) == set(range(1, 11))
    assert (ids.ids[:, 0] == 40).all() and (ids.ids[:, 1] == 1).all()
    assert len(ids.index_us) == 3
    turns = np.diff(ids.index_us) / drive.period_us
    assert np.all(np.abs(turns - np.round(turns)) < 1e-3) and turns[
        -1
    ] == pytest.approx(1, abs=1e-3)
    angle = (ids.id_us - ids.index_us[0]) % drive.period_us / drive.period_us
    assert np.all(np.diff(angle[ids.ids[:, 2] > 1][:9]) > 0)
    no_stops(sim)


def test_stream_without_a_turning_disk_times_out(media):
    _, sim, drive = stream_rig(media, cylinder=0)
    drive.home(0)
    got = drive.stream([ms.entry(ms.OP_READ_SECTOR, 0, 1)])
    assert got.drive_end == "timeout"
    assert got.commands[-1].timeout
    no_stops(sim)


def test_stream_index_wait_timeout_returns_to_the_monitor(media):
    """An index wait that times out (no disk turning) ends the stream and returns
    END_TIMEOUT from J; the drive is back in the monitor."""
    _, sim, drive = stream_rig(media, cylinder=0)
    drive.home(0)
    got = drive.stream([ms.entry(ms.OP_INDEX)])
    assert got.drive_end == "timeout" and got.reply[:2] == (0x44, 1)
    assert got.commands[-1].timeout and got.commands[-1].data.size == 0
    assert drive.sense()["t0"]
    no_stops(sim)


# Longer than the adapter's wait for the next byte (simsrq.STREAM_TIMEOUT_US).
PAST_GAP = 2 * int(simsrq.STREAM_TIMEOUT_US * CPU_HZ / 1_000_000)
CIA_CRB = 0x400F


def test_stream_keepalives_do_not_need_timer_b(media):
    """Keepalives count wait passes: with timer B stopped the wait for Read Track's
    index still sends them inside the adapter's gap."""
    _, sim, drive = stream_rig(media, cylinder=5)
    drive.motor(True)
    drive.home(5)
    drive.mon.write(CIA_CRB, b"\x00")
    got = drive.stream([ms.entry(ms.OP_READ_TRACK, 5)])
    assert got.complete and got.keepalives > 0
    assert len(got.commands[0].data) >= mfm.TRACK_BYTES - 1
    no_stops(sim)


def test_stream_index_wait_after_a_command_keeps_the_stream_alive(media):
    """A force interrupt whose busy outlasts the adapter's gap, after the stream has
    started, is waited out with keepalives."""
    _, sim, drive = stream_rig(media, cylinder=3)
    sim.wd = Wd(media, cylinder=3, force_busy=PAST_GAP)
    drive.motor(True)
    drive.home(3)
    got = drive.stream([ms.entry(ms.OP_READ_ADDRESS, rep=2), ms.entry(ms.OP_INDEX)])
    assert got.complete and len(got.index_us) == 1 and got.keepalives > 0
    no_stops(sim)


def test_stream_out_of_step_reply_sends_nothing_more(media, monkeypatch):
    """A reply that is no J return ends the session without another transfer."""
    cbm, sim, drive = stream_rig(media, cylinder=2)
    drive.motor(True)
    drive.home(2)
    monkeypatch.setattr(drive.mon.link, "response", bytes)
    sent = []
    monkeypatch.setattr(cbm, "srq2_write", sent.append)
    with pytest.raises(r1581.StreamLost) as lost:
        drive.read_track(1)
    diag = lost.value.meta["diagnosis"]
    assert diag["drive"] == "reply $00 is no end code"
    state = diag["drive_state"]
    assert "error" not in state, state
    assert state["entries_started"] == 1 and state["op"] == ms.OP_READ_TRACK
    assert state["first_set"]
    assert state["count"] >= mfm.TRACK_BYTES - 1 and state["wd_status"] & ST_BUSY == 0
    assert state["t_end_us"] - state["t_first_us"] == pytest.approx(
        drive.period_us, rel=1e-3
    )
    assert isinstance(lost.value, monitor.RECOVERABLE) and not drive.mon.running
    drive.close()
    assert not sent
    no_stops(sim)


def test_stream_state_holds_the_status_after_the_command_write(media, monkeypatch):
    """Stopped by the adapter mid-command, the state keeps the WD status read once
    valid after the Read Track write (busy, MO raised) and the first byte's stamp."""
    cbm, sim, drive = stream_rig(media, cylinder=4)
    drive.motor(True)
    drive.home(4)
    slow = cbm.srq2_stream
    monkeypatch.setattr(cbm, "srq2_stream", lambda n: slow(n, packet_us=4 * 1024))
    got = drive.stream([ms.entry(ms.OP_INDEX), ms.entry(ms.OP_READ_TRACK, 4)])
    state = got.state
    assert got.adapter == "overrun" and got.reply[0] == 0x48
    assert state["entries_started"] == 2 and state["first_set"]
    assert state["wd_status"] & (ST_BUSY | r1581.ST_MO) == ST_BUSY | r1581.ST_MO
    no_stops(sim)


def random_d81(seed):
    """A D81 of random sectors, no errors."""
    rng = np.random.default_rng(seed)
    data = rng.integers(0, 256, (d81.D81_SECTORS, 256), np.uint8)
    return d81.D81(data, np.ones(d81.D81_SECTORS, np.uint8))


@pytest.mark.slow
def test_d81_whole_disk_round_trip():
    media = MfmMedia(cylinders=d81.TRACKS)
    sim, drive = ram_rig(media, cylinder=0)
    drive.motor(True)
    drive.home(0)
    image = random_d81(3)
    assert not disk1581.write_disk(drive, image, progress=False)["mismatched"]
    got = disk1581.read_disk(drive, progress=False)
    assert np.array_equal(got.data, image.data)
    assert (got.errors == 1).all()
    assert d81.write_d81(got) == d81.write_d81(image)
    drive.close()
    no_stops(sim)


def test_d81_streamed_cylinder(monkeypatch, tmp_path):
    monkeypatch.setattr(disk1581, "CYLINDERS", 1)
    media = MfmMedia.formatted(cylinders=2)
    _, sim, drive = stream_rig(media, cylinder=1)
    drive.motor(True)
    drive.home(1)
    image = random_d81(4)
    got = disk1581.read_disk(drive, archive=tmp_path, progress=False)
    caps = load_captures(tmp_path / disk1581.ARCHIVE_NAME)
    assert [c.kind for c in caps] == ["track", "ids"] * 2
    assert not disk1581.write_disk(drive, image, progress=False)["mismatched"]
    got = disk1581.read_disk(drive, progress=False)
    rows = np.concatenate([d81.side_rows(0, s) for s in (0, 1)])
    assert np.array_equal(got.data[rows], image.data[rows])
    no_stops(sim)


def test_s4_session_leaves_idle_drives_clean(media):
    cbm, sim, drive = stream_rig(media, cylinder=3, fast_peers=2)
    drive.motor(True)
    drive.home(3)
    drive.read_ids(2)
    peers = [d for d in cbm.bus.devices if hasattr(d, "fast_host")]
    assert peers and all(p.fast_host for p in peers)
    drive.close()
    drive.mon.stop()
    assert not any(p.fast_host for p in peers)
    no_stops(sim)


def test_throttled_host_overruns_and_the_drive_stops(media, monkeypatch):
    cbm, sim, drive = stream_rig(media, cylinder=8)
    drive.motor(True)
    drive.home(8)
    slow = cbm.srq2_stream
    monkeypatch.setattr(cbm, "srq2_stream", lambda n: slow(n, packet_us=4 * 1024))
    got = drive.stream([ms.entry(ms.OP_READ_TRACK, 8)])
    assert got.adapter == "overrun" and got.reply[0] == 0x48
    state = got.state
    assert state["entries_started"] == 1 and state["flags"] == 0
    assert state["first_set"] and state["count"] > 1
    assert got.diagnosis()["drive"] == "atn" and got.reply[1] == 1
    monkeypatch.setattr(cbm, "srq2_stream", slow)
    assert drive.read_track(1).meta["adapter"] == "done"
    no_stops(sim)


class Jobs:
    """cbm stub whose DOS finishes a job after polls polls."""

    def __init__(self, polls):
        self.mem, self.polls = {}, polls

    def upload(self, _dev, addr, data):
        self.mem[addr] = data[0]

    def download(self, _dev, addr, _n):
        self.polls -= 1
        return bytes([1 if self.polls <= 0 else self.mem[addr]])


def test_dos_cache_invalidation_job():
    assert r1581.invalidate(Jobs(3), DEV, sleep=lambda s: None)
    t = iter(range(10))
    assert not r1581.invalidate(
        Jobs(99), DEV, sleep=lambda s: None, clock=lambda: next(t)
    )


@pytest.fixture(name="cli_rig")
def cli_rig_fixture(monkeypatch):
    """A function: (adapter, sim drive) of a timed 1581 for the CLI, delays stubbed."""
    monkeypatch.setattr(r1581.time, "sleep", lambda s: None)
    monkeypatch.setattr(r1581, "invalidate", lambda cbm, dev, sleep=None: True)

    def make(media, cylinder):
        cbm = simsrq.make(
            model="1581", dev=DEV, firmware=12, timeout_us=WATCHDOG_IDLE_S * 1e6
        )
        cbm.drive.wd = Wd(media, cylinder=cylinder)
        return cbm, cbm.drive

    return make


def test_cli_homeprobe_dry_and_step(cli_rig, media, capsys):
    cbm, sim = cli_rig(media, 25)
    dry = cli.main(["homeprobe", "--dev", "9", "--transport", "s4", "--headers"], cbm)
    assert (dry["estimate"], dry["source"], dry["steps"]) == (25, "id", 25)
    assert sim.wd.cylinder == 25
    capsys.readouterr()
    with pytest.raises(r1581.TrackError, match="within 0 steps"):
        cli.main(["homeprobe", "--dev", "9", "--transport", "s4", "--step"], cbm)
    failed = json.loads(capsys.readouterr().out)
    assert failed["homed"] is False and failed["restore"]["steps"] == 0
    assert failed["restore"]["result"] & 0x80 and "error" in failed
    assert sim.wd.cylinder == 25
    with pytest.raises(ValueError):
        cli.main(
            [
                "homeprobe",
                "--dev",
                "9",
                "--transport",
                "s4",
                "--headers",
                "--step",
                "--max-steps",
                "24",
            ],
            cbm,
        )
    out = cli.main(
        [
            "homeprobe",
            "--dev",
            "9",
            "--transport",
            "s4",
            "--headers",
            "--step",
            "--max-steps",
            "25",
        ],
        cbm,
    )
    assert out["homed"] and sim.wd.cylinder == 25
    assert out["restore"]["pulses"] == 25 and out["restore"]["end"] == "deadline"
    assert capsys.readouterr().out
    no_stops(sim)


def test_cli_streamprobe(cli_rig, media, tmp_path):
    cbm, sim = cli_rig(media, 2)
    save = tmp_path / "s.npz"
    out = cli.main(
        [
            "streamprobe",
            "--dev",
            "9",
            "--cylinder",
            "39",
            "--ids",
            "12",
            "--save",
            str(save),
        ],
        cbm,
    )
    assert out["adapter"] == ["done", "done"] and out["drive"] == ["done", "done"]
    assert 6249 <= out["revolution_bytes"][0] <= 6250
    assert {tuple(i[:3]) for i in out["ids"]} >= {(39, 0, r) for r in range(1, 11)}
    info = cli.main(["info", str(save)])
    assert info
    no_stops(sim)


def test_cli_streamprobe_sequence_of_hw11(media, monkeypatch, tmp_path):
    """streamprobe --max-steps 0 --cylinder 39 --revolutions 2 from cylinder 0, the
    WD holding busy after an idle force interrupt as the 1581 does."""
    monkeypatch.setattr(r1581.time, "sleep", lambda s: None)
    monkeypatch.setattr(r1581, "invalidate", lambda cbm, dev, sleep=None: True)
    cbm = simsrq.make(
        model="1581", dev=DEV, firmware=12, timeout_us=WATCHDOG_IDLE_S * 1e6
    )
    cbm.drive.wd = Wd(media, cylinder=0, force_busy=4 * STATUS_VALID)
    args = ["streamprobe", "--dev", "9", "--max-steps", "0", "--cylinder", "39"]
    out = cli.main(
        args + ["--revolutions", "2", "--save", str(tmp_path / "s.npz")], cbm
    )
    assert out["home"]["source"] == "tr00" and out["home"]["homed"]
    assert out["adapter"] == ["done", "done"] and out["drive"] == ["done", "done"]
    assert out["track_stream"]["bytes"] >= 2 * 6249
    assert {tuple(i[:3]) for i in out["ids"]} >= {(39, 0, r) for r in range(1, 11)}
    assert cbm.drive.wd.pulses == 2 * 39
    no_stops(cbm.drive)


def test_cli_streamprobe_stops_at_a_short_track_stream(cli_rig, media, monkeypatch):
    cbm, sim = cli_rig(media, 0)
    slow = cbm.srq2_stream
    monkeypatch.setattr(cbm, "srq2_stream", lambda n: slow(n, packet_us=4 * 1024))
    monkeypatch.setattr(r1581.Mfm1581, "read_ids", None)
    args = ["streamprobe", "--dev", "9", "--max-steps", "0", "--cylinder", "39"]
    out = cli.main(args, cbm)
    stream = out["track_stream"]
    assert stream["adapter"] == "overrun" and "ids" not in out
    diag = stream["diagnosis"]
    assert diag["drive"] == "atn" and diag["entries_started"] == 1
    assert diag["codes"]["start"] == 1 and diag["data_bytes"] >= max(stream["bytes"], 1)
    assert diag["elapsed_s"] > 0
    no_stops(sim)


def test_cli_streamprobe_reports_a_lost_stream(cli_rig, media, monkeypatch, capsys):
    cbm, sim = cli_rig(media, 0)
    parse = ms.MfmStream.parse
    monkeypatch.setattr(
        ms.MfmStream, "parse", lambda raw, reply: parse(raw, bytes(len(reply)))
    )
    recovered = []
    monkeypatch.setattr(monitor, "recover", lambda c, d: recovered.append(d))
    args = ["streamprobe", "--dev", "9", "--max-steps", "0", "--cylinder", "39"]
    with pytest.raises(r1581.StreamLost):
        cli.main(args, cbm)
    out = json.loads(capsys.readouterr().out)
    assert out["track_stream"]["diagnosis"]["codes"]["start"] == 1
    assert recovered == [DEV]
    no_stops(sim)


def test_cli_streamprobe_reports_the_track_stream_when_a_later_step_fails(
    cli_rig, media, monkeypatch, capsys
):
    cbm, sim = cli_rig(media, 0)

    def lost(_self, _count):
        raise r1581.TrackError("drive left the monitor")

    monkeypatch.setattr(r1581.Mfm1581, "read_ids", lost)
    with pytest.raises(r1581.TrackError, match="left the monitor"):
        cli.main(
            ["streamprobe", "--dev", "9", "--max-steps", "0", "--cylinder", "39"], cbm
        )
    out = json.loads(capsys.readouterr().out)
    assert out["home"]["homed"] and out["home"]["restore"]["result"] == 0
    stream = out["track_stream"]
    assert (stream["adapter"], stream["drive_end"]) == ("done", "done")
    assert stream["bytes"] >= 6249 and stream["rev_status"] == [0x80]
    assert sim.wd.cylinder == 0
    no_stops(sim)


def protected_layout(cyl, side):
    """Nine 512-byte sectors (3 with a bad data CRC, 4 with a bad ID CRC, 5 deleted),
    a 256-byte R 10, an extra 128-byte R 11 and a duplicate R 2 of 128 bytes."""
    rng = np.random.default_rng(7)
    fill = [rng.integers(0, 0xF5, 128 << n, dtype=np.uint8) for n in (2, 1, 0)]
    specs = [mfm.SectorSpec(cyl, side, r, data=fill[0]) for r in range(1, 10)]
    specs[2].bad_data_crc, specs[3].bad_id_crc, specs[4].deleted = True, True, True
    specs.append(mfm.SectorSpec(cyl, side, 10, n=1, data=fill[1]))
    specs.append(mfm.SectorSpec(cyl, side, 11, n=0, data=fill[2]))
    specs.append(mfm.SectorSpec(cyl, side, 2, n=0, data=fill[2]))
    return specs


def test_nonstandard_layout_written_and_captured():
    media = MfmMedia(cylinders=4)
    _, sim, drive = stream_rig(media, cylinder=2)
    drive.motor(True)
    drive.home(2)
    drive.seek(3)
    drive.side(0)
    plan = mfm.plan_track(protected_layout(3, 0))
    assert drive.write_track(plan.image) & mfm.ST_LOST == 0
    for first, rows, deleted in disk1581.write_runs(plan.writes):
        drive.write_sectors(3, first, rows, deleted)
    ids = drive.read_ids(16).decode()[0].sectors
    rs = set(ids["r"].tolist())
    assert rs == set(range(1, 12))
    assert (ids["r"] == 2).sum() >= 2
    assert not ids["id_ok"][ids["r"] == 4].all()
    sizes = {int(r): int(n) for r, n in zip(ids["r"], ids["n"])}
    assert sizes[10] == 1 and sizes[11] == 0
    reads = drive.read_sectors(3, 1, 11)
    status = {r: st for _, r, _, st in reads}
    assert status[3] & mfm.ST_CRC and status[5] & mfm.ST_DELETED
    assert status[4] & mfm.ST_RNF
    assert len(reads[9][2]) == 256 and len(reads[10][2]) == 128
    track = drive.read_track(1).decode()[0]
    flags = track.sectors["flags"]
    assert (flags & mfm.Flag.DUPLICATE).any() and (flags & mfm.Flag.ODD_SIZE).any()
    assert (flags & mfm.Flag.DELETED).any()
    no_stops(sim)


def test_cylinder_80_like_wheels():
    media = MfmMedia(cylinders=84)
    sim, drive = ram_rig(media, cylinder=0)
    drive.motor(True)
    drive.home(0)
    drive.seek(80)
    drive.side(0)
    plan = mfm.plan_track(mfm.standard_layout(80, 0, sectors(80, 0)))
    assert drive.write_track(plan.image) & (r1581.ST_WP | mfm.ST_LOST) == 0
    for first, rows, deleted in disk1581.write_runs(plan.writes):
        drive.write_sectors(80, first, rows, deleted)
    data, status = drive.read_sector(80, 5)
    assert status & (mfm.ST_CRC | mfm.ST_RNF) == 0
    assert np.array_equal(data, sectors(80, 0)[4])
    drive.close()
    assert sim.wd.cylinder == 0
    no_stops(sim)


@pytest.mark.slow
def test_cli_read_and_write_d81(tmp_path, monkeypatch):
    monkeypatch.setattr(r1581.time, "sleep", lambda s: None)
    monkeypatch.setattr(r1581, "invalidate", lambda cbm, dev, sleep=None: True)
    image = random_d81(5)
    path = tmp_path / "in.d81"
    path.write_bytes(d81.write_d81(image))
    cbm = simx.adapter("s1", Drive1581, device=DEV, media=MfmMedia(), cylinder=12)
    cbm.budget = 20_000_000
    cbm.drives[DEV].wd.w[WTRK] = 12  # where DOS's seeks leave it
    out = cli.main(["write", "--dev", "9", str(path)], cbm)
    assert out["failed"] == [] and out["verified"] == 2 * d81.TRACKS
    back = tmp_path / "out.d81"
    out = cli.main(["read", "--dev", "9", str(back)], cbm)
    assert out["errors"] == 0 and back.read_bytes() == path.read_bytes()
    with pytest.raises(ValueError, match="d81"):
        cli.main(["read", "--dev", "9", str(tmp_path / "x.d64")], cbm)
    no_stops(cbm.drives[DEV])


@pytest.mark.parametrize("name", ["s1", "s2", "xb", "s4"])
def test_monitors_leave_the_second_code_window(name):
    assert BASE + len(drivecode(f"monitor_{name}_1581")) <= r1581.CODE2


@pytest.mark.parametrize("name", [r1581.MFM_CODE, r1581.STREAM_CODE])
def test_drive_code_stays_out_of_the_bam(name):
    code = drivecode(name)
    assert r1581.CODE2 + len(code) - r1581.SPLIT <= BAM
    assert r1581.TAGS[name] in code[: r1581.SPLIT]
