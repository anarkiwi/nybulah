"""Streaming capture: drive/stream.s on the timed 1571 with the firmware v12 adapter model."""

import contextlib
import functools
import json
import struct

import numpy as np
import pytest

from nybulah import cli, disk, simsrq, simx, streamprobe
from nybulah import stream as fmt
from nybulah.analysis.cycle import find_cycle
from nybulah.analysis.gcr import bits_per_revolution
from nybulah.analysis.sector import SectorError, decode_track
from nybulah.formats.image import Capture as ImageCapture
from nybulah.formats.image import revolution
from nybulah.link import BASE
from nybulah.monitor import Monitor, drivecode
from nybulah.nibbler import CODE_BASE, CODE_SIZE, SENSE_STREAM, STREAM_ZP
from nybulah.nibbler import Capture, Nibbler, TrackError
from nybulah.simdisk import DOS_TRACK, Media, Mechanism, log_bytes, sync_track
from nybulah.simsrq import SR_PERIOD, UsbIn
from nybulah.simx import XError

ZONE_TRACKS = {3: 17, 2: 18, 1: 25, 0: 31}
FASTEST = 310.0
CLOCK_CODE = 9
INDEX_SLACK = 4  # bytes an INDEX may wait for a metadata slot
ST_SLOW = 0x10


def rig(media, halftrack=36, firmware=12, peers=0, timeout_us=None, **kw):
    """(adapter, mechanism, opened Nibbler) on a timed 1571 at device 9."""
    io = {} if timeout_us is None else {"timeout_us": timeout_us}
    cbm = simsrq.make(1.0, rise=1.0, peers=peers, dev=9, firmware=firmware, **io)
    mech = Mechanism(cbm.drive, media, halftrack=halftrack)
    cbm.drive.write(DOS_TRACK, halftrack // 2)
    mon = Monitor(cbm, 9, "s4", clock=lambda: cbm.now * 1e-6)
    mon.start()
    nib = Nibbler(
        mon, "1571", stepms=1, settle_ms=1, spinup_s=0, sleep=lambda s: None, **kw
    )
    return cbm, mech, nib.open()


def sdr_writes(cbm):
    """Record the cycle of every shift register write."""
    cia, out = cbm.drive.cia, []
    write = cia.write

    def hooked(reg, v, c):
        if reg == 12:
            out.append(c)
        return write(reg, v, c)

    cia.write = hooked
    return out


def check_stream(mech, cap):
    """The capture is one contiguous run of the latched bytes, and every sync it
    reports lies there with its true length inside its bounds."""
    data, times = log_bytes(mech.log)
    want, first = bytes(cap.data), -1
    while first < 0 or data[first : first + len(want)] != want:
        first = data.find(want[:32], first + 1)
        assert first >= 0, "capture is no run of the latched bytes"
    syncs = [e for e in mech.log if e[0] == "sync"]
    at = np.searchsorted(times, [e[2] for e in syncs]) - first
    low = np.array([2 * (e[2] - e[1]) for e in syncs])
    pos, est, lo, hi = cap.parsed.syncs(2 * cap.cell_cycles)
    done = (hi >= 0) & (pos > 0)
    assert done.sum() >= 1
    true = []
    for p, l, h in zip(pos[done], lo[done], hi[done]):
        k = np.flatnonzero(at == p)
        assert len(k) == 1 and l <= low[k[0]] <= h
        true.append(low[k[0]])
    cell = 2 * cap.cell_cycles
    assert np.median(np.abs(np.array(true) - est[done])) < 4 * cell


@pytest.mark.parametrize("zone", sorted(ZONE_TRACKS))
@pytest.mark.parametrize("rpm", [300.0, FASTEST])
def test_streams_every_byte_and_sync(g64, zone, rpm):
    track = ZONE_TRACKS[zone]
    cbm, mech, nib = rig(Media.from_g64(g64, rpm=rpm))
    writes = sdr_writes(cbm)
    mech.log = []
    cap = nib.stream(2 * track)
    assert cap.stream_status == {"adapter": "done", "drive": "done"}
    assert cap.status == 0 and len(cap.index) == 2
    check_stream(mech, cap)
    assert np.diff(writes).min() >= SR_PERIOD
    assert mech.bumps == mech.inner_stops == 0
    dec = decode_track(cap.bits(), track)
    assert (dec.errors == SectorError.OK).all()


@pytest.mark.parametrize("seed", range(4))
def test_stress_syncs_at_the_fastest_zone(seed):
    """Short, long and back-to-back syncs between 1 and 30 bytes, zone 3, fast disk."""
    rpm = FASTEST
    rng = np.random.default_rng(seed)
    runs = rng.choice([10, 11, 12, 14, 20, 40, 80, 300, 1500], 30).tolist()
    cells = int(round(bits_per_revolution(3, rpm)))
    media = Media({(0, 2): sync_track(runs, cells, seed=seed, gaps=(1, 30))}, rpm=rpm)
    cbm, mech, nib = rig(media, halftrack=4)
    writes = sdr_writes(cbm)
    mech.log = []
    cap = nib.stream(2, revolutions=2)
    assert cap.stream_status == {"adapter": "done", "drive": "done"}
    check_stream(mech, cap)
    assert np.diff(writes).min() >= SR_PERIOD
    assert mech.bumps == mech.inner_stops == 0


def test_multi_revolution_merge(g64):
    """Index edges cut revolutions of one length; between them every sector reads
    once per revolution, and the image layer takes a revolution from them."""
    _, mech, nib = rig(Media.from_g64(g64))
    mech.log = []
    cap = nib.stream(36, revolutions=3)
    idx = cap.index_bits()
    assert len(idx) == 4
    bits = cap.bits()
    revs = [bits[a:b] for a, b in zip(idx[:-1], idx[1:])]
    assert max(map(len, revs)) - min(map(len, revs)) <= 8 * INDEX_SLACK
    whole = decode_track(bits[idx[0] : idx[-1]], 18)
    assert (whole.errors == SectorError.OK).all() and (whole.copies >= 2).all()
    image = ImageCapture(bits, cap.density, index=idx)
    one, cycle = revolution(image)
    assert abs(len(one) - np.median(np.diff(idx))) <= 8 * INDEX_SLACK
    assert cycle.length == len(one)
    found = find_cycle(bits[idx[0] :], cap.density)
    assert abs(found.length - np.median(np.diff(idx))) <= 8 * INDEX_SLACK
    assert cap.revolution_bytes() == pytest.approx(np.median(np.diff(cap.index)), abs=1)
    assert mech.bumps == mech.inner_stops == 0


def test_capture_record_round_trips(g64, tmp_path):
    _, _, nib = rig(Media.from_g64(g64))
    cap = nib.stream(36)
    cap.save(tmp_path / "s.npz")
    back = Capture.load(tmp_path / "s.npz")
    assert back.version == 3 and (back.data == cap.data).all()
    assert (back.positions == cap.positions).all() and (back.index == cap.index).all()
    assert (back.bits() == cap.bits()).all() and back.rpm is None


@pytest.mark.parametrize("which", [0, 1])
def test_frame_poll_window_edges(g64, which):
    """The adapter's released-SRQ poll anywhere from SRQ_FRAME to SRQ_WAIT."""
    cbm, mech, nib = rig(Media.from_g64(g64, rpm=FASTEST))
    frame = cbm._srq_timing(8).frame[which]  # pylint: disable=protected-access
    stream = cbm.srq2_stream
    cbm.srq2_stream = lambda size: stream(size, frame=frame)
    mech.log = []
    cap = nib.stream(34)
    assert cap.stream_status["adapter"] == "done"
    check_stream(mech, cap)


def test_overrun_is_reported_and_the_drive_stops(g64):
    """A host draining one packet per 2 ms falls behind: the bytes before the
    overrun are intact, the adapter says so, ATN ends the drive's stream and the
    session goes on."""
    cbm, mech, nib = rig(Media.from_g64(g64))
    nib.seek(36)
    stream = cbm.srq2_stream
    cbm.srq2_stream = lambda size: stream(size, packet_us=2000.0)
    mech.log = []
    cap = nib.stream(36)
    assert cap.stream_status["adapter"] == "overrun" and cap.status
    data, _ = log_bytes(mech.log)
    assert len(cap.data) > 32 and bytes(cap.data) in data
    assert cbm.count["atn_stops"] == 1
    cbm.srq2_stream = stream
    assert nib.stream(36).stream_status == {"adapter": "done", "drive": "done"}


def test_killer_track_ends_at_the_index():
    cells = int(round(bits_per_revolution(2, 300.0)))
    _, _, nib = rig(Media({(0, 36): np.ones(cells, np.uint8)}))
    cap = nib.stream(36)
    assert cap.stream_status == {"adapter": "done", "drive": "done"}
    assert len(cap.data) == 0
    _, _, _, hi = cap.parsed.syncs()
    assert (hi == -1).all()


def stream_direct(cbm, mon, ticks=2, revs=3):
    """Call drive/stream.s without the Nibbler: (adapter output, reply)."""
    code = drivecode("stream_1571")
    mon.write(CODE_BASE, code[:CODE_SIZE])
    mon.write(STREAM_ZP, code[CODE_SIZE:])
    mon.write(0x60 + 5, bytes([ticks, revs]))
    mon.transact(b"J" + struct.pack("<H", CODE_BASE))
    raw = cbm.srq2_stream(1 << 16)
    return raw, mon.link.response(3)


def test_silent_drive_times_out_and_atn_ends_it(g64):
    """Motor off: nothing after START; the adapter times out and its ATN stops the
    drive through the idle checks of nw."""
    cbm, _, nib = rig(Media.from_g64(g64))
    nib.mon.set_fast(True)
    raw, reply = stream_direct(cbm, nib.mon)
    s = fmt.Stream.parse(raw)
    assert s.adapter == "timeout" and s.val.tolist() == [fmt.M_START]
    assert reply[0] == fmt.M_END_ATN


def test_no_index_ends_the_stream(g64, monkeypatch):
    monkeypatch.setattr("nybulah.simdisk.INDEX_FRACTION", 0.0)
    cbm, _, nib = rig(Media.from_g64(g64))
    nib.seek(36)
    nib.mon.set_fast(True)
    raw, reply = stream_direct(cbm, nib.mon)
    s = fmt.Stream.parse(raw)
    assert s.adapter == "done" and s.drive_end == "noindex" and len(s.data)
    assert reply[0] == fmt.M_END_NOINDEX and len(s.index) == 0


def test_refused_at_1_mhz(g64):
    _, _, nib = rig(Media.from_g64(g64))
    code = drivecode("stream_1571")
    nib.mon.write(CODE_BASE, code[:CODE_SIZE])
    nib.mon.write(STREAM_ZP, code[CODE_SIZE:])
    assert nib.mon.jsr(CODE_BASE)[0] == ST_SLOW


def test_refused_without_firmware_12_or_a_1571(g64):
    _, _, nib = rig(Media.from_g64(g64), firmware=11)
    assert not nib.streaming
    with pytest.raises(TrackError):
        nib.stream(36)
    with pytest.raises(ValueError):
        Nibbler(nib.mon, "1571", stream=True)
    with pytest.raises(ValueError):
        Nibbler(nib.mon, "1541", stream=True)


def test_1mhz_adapter_refuses():
    cbm = simsrq.make(1.0, dev=9, firmware=11)
    with pytest.raises(XError):
        cbm.srq2_stream(64)


def test_d64_tracks_read_by_streaming_without_expansion_ram(image, g64):
    cbm, mech, nib = rig(Media.from_g64(g64))
    expansion = []
    write = cbm.drive.write

    def watched(addr, value):
        if 0x6000 <= addr < 0x8000:
            expansion.append(addr)
        return write(addr, value)

    cbm.drive.write = watched
    assert nib.streaming
    jobs = [j for j in disk.d64_jobs(35) if j.track in (17, 18)]
    data, errors = disk.read_jobs(nib, jobs, progress=False)
    want = np.concatenate(
        [np.arange(image.span(j.track).start, image.span(j.track).stop) for j in jobs]
    )
    assert (errors == SectorError.OK).all() and (data == image.data[want]).all()
    assert not expansion and mech.bumps == mech.inner_stops == 0


RAM_PASS_US = 2_000_000  # adapter I/O timeout covering a capture pass's bound


def test_streaming_drive_writes_and_captures_through_the_track_code():
    """A streaming Nibbler loads the track code for RAM passes and writes, then
    streams again; the probe sync written to an empty track is found."""
    _, mech, nib = rig(Media({}), halftrack=2, timeout_us=RAM_PASS_US)
    nib.halftrack = 2
    assert disk.revolution_cells(nib, 2) == round(bits_per_revolution(0))
    assert nib.stream(2).stream_status == {"adapter": "done", "drive": "done"}
    assert mech.bumps == mech.inner_stops == 0


def test_ram_passes_need_expansion_ram(monkeypatch):
    monkeypatch.setattr(simx.TimedDrive1571, "EXPANSION", ())
    _, _, nib = rig(Media({}), halftrack=2)
    nib.halftrack = 2
    with pytest.raises(TrackError, match="no expansion RAM"):
        nib.capture(2, start="sync")


def test_homes_and_seeks_through_the_seek_code(g64):
    """Stream mode homes by the track 00 rule and seeks with prep in base RAM."""
    _, mech, nib = rig(Media.from_g64(g64), halftrack=40)
    nib.halftrack = None
    assert nib.locate() == 2
    nib.seek(34)
    assert mech.halftrack == 34 and mech.bumps == mech.inner_stops == 0


def test_code_layout():
    seek = drivecode("seek_1571")
    assert not any(seek[SENSE_STREAM - CODE_BASE :])
    assert len(drivecode("sense_1571")) <= CODE_BASE + CODE_SIZE - SENSE_STREAM
    zp = drivecode("stream_1571")[CODE_SIZE:]
    assert STREAM_ZP + len(zp) <= 0x100
    assert BASE + len(drivecode("monitor_s4")) + CLOCK_CODE <= 0x0800


def adapter_output(items, code=fmt.A_DONE, cut=None):
    """UsbIn output for (byte, metadata) items; cut: overrun after that many puts."""
    usb = UsbIn(1 << 16, 0.0)
    for i, (b, meta) in enumerate(items[:cut]):
        assert usb.put(float(i), b, meta) is None
    if cut is None:
        return usb.close(code)
    usb.out.append(fmt.ESC)
    return usb.close(fmt.A_OVERRUN)


def test_framing_round_trips():
    rng = np.random.default_rng(1)
    data = rng.integers(0, 4, 300).astype(np.uint8)
    meta = rng.random(300) < 0.1
    vals = np.where(meta, rng.choice([0x04, 0x08, 0x41, 0x92, 0xF3], 300), data)
    s = fmt.Stream.parse(adapter_output(list(zip(vals.tolist(), meta.tolist()))))
    assert s.adapter == "done" and (s.data == vals[~meta]).all()
    assert (s.val == vals[meta]).all()
    assert (s.pos == np.cumsum(~meta)[meta]).all()


def test_dangling_escape_takes_the_trailer():
    s = fmt.Stream.parse(adapter_output([(5, False), (0, False)], cut=2))
    assert s.adapter == "overrun" and s.data.tolist() == [5, 0]


def test_truncated_and_cut_streams():
    usb = UsbIn(64, 0.0)
    codes = [usb.put(0.0, 1, False) for _ in range(70)]
    assert fmt.A_TRUNCATED in codes
    assert fmt.Stream.parse(usb.close(fmt.A_TRUNCATED)).adapter == "truncated"
    assert fmt.Stream.parse(b"\x01\x02\x00").adapter == "cut"


def test_sync_end_closes_the_oldest_open_sync():
    """A late SYNC_END (due behind data) can follow the next SYNC_START."""
    pos = np.array([5, 9, 9, 9])
    val = np.array(
        [0x81, 0x41, 0x7F, 0x3B]
    )  # START 0x80, START 0x40, END 0x7C, END 0x38
    p, est, lo, hi = fmt.sync_bounds(pos, val)
    assert p.tolist() == [5, 9] and est[1] - est[0] == 8 - 4
    assert (lo <= est).all() and (est <= hi).all()


def test_sync_continuations_unwrap_long_syncs():
    """SYNC_CONT stamps under 256 cycles apart carry a sync past T2 wraps."""
    stamps = (0x00 - 200 * np.arange(6)) & 0xFF
    val = np.concatenate(
        ([stamps[0] & 0xFC | fmt.SSTART], stamps[1:-1] & 0xFC | fmt.SCONT)
    )
    val = np.append(val, stamps[-1] & 0xFC | fmt.SEND)
    p, est, lo, hi = fmt.sync_bounds(np.full(len(val), 7), val)
    span = 200 * 5
    assert p.tolist() == [7] and lo[0] <= span <= hi[0]
    assert abs(est[0] - span) < 2 * fmt.NW_POLL


def test_open_sync_and_drive_end():
    pos = np.array([0, 3, 3, 3])
    val = np.array([fmt.M_START, 0x81, 0x82 - 0x40, fmt.M_END_NOINDEX])
    s = fmt.Stream(np.zeros(3, np.uint8), pos, val, "done")
    p, est, lo, hi = s.syncs()
    assert p.tolist() == [3] and hi[0] == -1 and lo[0] == est[0] == 0x40
    assert s.drive_end == "noindex" and not s.complete
    assert fmt.Stream(np.zeros(0, np.uint8), pos[:1], val[:1], "cut").drive_end is None


def test_start_lag_follows_latched_ones():
    """A byte ending in nine ones (the 10th starts SYNC) puts SYNC low one cell
    after it, so nw sees it late; with none latched it waits half a poll."""
    cell = 7.0
    late, prompt = fmt.start_lag([9, 0], cell)
    assert late == np.mean(fmt.NW_WRITE) + fmt.NW_SYNC - cell
    assert prompt == fmt.NW_POLL / 2


def probe_rig(monkeypatch, g64, halftrack):
    """streamprobe against the timed 1571; returns (cbm, mechanism)."""
    cbm, mech, nib = rig(Media.from_g64(g64), halftrack=halftrack)
    monkeypatch.setattr(streamprobe, "identify_model", lambda cbm, dev: "1571")
    monkeypatch.setattr(
        streamprobe, "Monitor", lambda cbm, dev, proto: contextlib.nullcontext(nib.mon)
    )
    monkeypatch.setattr(
        streamprobe,
        "Nibbler",
        functools.partial(
            Nibbler, stepms=1, settle_ms=1, spinup_s=0, sleep=lambda s: None
        ),
    )
    return cbm, mech


def test_streamprobe_homes_then_streams(monkeypatch, capsys, g64, tmp_path):
    cbm, mech = probe_rig(monkeypatch, g64, 9)
    path = tmp_path / "p.npz"
    out = cli.main(
        ["streamprobe", "--dev", "9", "--halftrack", "4", "--save", str(path)], cbm
    )
    assert json.loads(capsys.readouterr().out) == out
    assert out["adapter"] == out["drive"] == "done" and out["halftrack"] == 4
    assert out["home"]["estimate"] == 9 and len(out["index"]) == 2
    assert out["syncs"] > 0 and out["sync_cycles_median"] > 0
    assert len(Capture.load(path).data) == out["bytes"] and out["saved"] == str(path)
    assert mech.halftrack == 4 and mech.bumps == mech.inner_stops == 0


def test_streamprobe_refusals(monkeypatch, g64):
    cbm, mech = probe_rig(monkeypatch, g64, 36)
    with pytest.raises(ValueError, match="34 outward steps"):
        cli.main(["streamprobe", "--dev", "9", "--max-steps", "33"], cbm)
    assert mech.halftrack == 36 and mech.bumps == 0
    monkeypatch.setattr(streamprobe, "identify_model", lambda cbm, dev: "1541")
    with pytest.raises(ValueError, match="needs a 1571"):
        cli.main(["streamprobe", "--dev", "9"], cbm)
