"""WD1772 controller, 1581 mechanism and MFM media (nybulah.simwd) at register level."""

import numpy as np
import pytest

from nybulah import simcia, simwd
from nybulah.analysis.crc import crc16
from nybulah.simwd import BUSY, CRCE, DRQ, IP, LD, MO, RNF, RT, SU, T0, WPROT
from nybulah.simwd import MfmMedia, Wd, fmtrk_stream, stream_track

RESTORE, SEEK, STEP_IN, STEP_OUT = 0x09, 0x19, 0x49, 0x69
READ_SECTOR, WRITE_SECTOR, READ_ADDRESS = 0x88, 0xA8, 0xC8
READ_TRACK, WRITE_TRACK, FORCE = 0xE8, 0xF8, 0xD0
T12 = 24_000
REV = 400_000
MOTOR_ON, MOTOR_OFF = 0xFA, 0xFE
HEAD0 = 0x01


@pytest.fixture(name="interpreted", params=["compiled", "python"], autouse=True)
def interpreted_fixture(request, monkeypatch):
    """Run each test on the compiled kernels and on their Python sources."""
    if request.param == "python":
        for module in (simwd, simcia):
            for name, obj in vars(module).items():
                if hasattr(obj, "py_func"):
                    monkeypatch.setattr(module, name, obj.py_func)
    return request.param


def sector_payload(cyl, side):
    """Ten sectors of bytes Write Track writes plainly (not $F5-$F7)."""
    rng = np.random.default_rng(cyl * 2 + side)
    return rng.integers(0, 0xF5, (10, 512), dtype=np.uint8)


@pytest.fixture(name="media", scope="module")
def media_fixture():
    return MfmMedia.formatted(cylinders=12, sectors=sector_payload)


def drive(media, cylinder=0, **kw):
    """A Wd holding a copy of media with the motor on at cycle 0."""
    copy = None
    if media is not None:
        copy = MfmMedia(rpm=media.rpm, cylinders=media.cylinders)
        copy.flat[:] = media.flat
    wd = Wd(copy, cylinder=cylinder, **kw)
    wd.control(0, MOTOR_ON)
    return wd


def untouched(wd):
    return wd.bumps == 0 == wd.inner_stops


def command(wd, cmd, c0, data=None, poll=16, limit=12 * REV):
    """Issue cmd at c0, then service from the first valid status read."""
    wd.write(0, cmd, c0)
    return service(wd, c0 + simwd.STATUS_VALID - poll, data, poll, limit)


def service(wd, c0, data=None, poll=16, limit=12 * REV):
    """Service DRQ every poll cycles from c0 until BUSY clears: (bytes read, cycle of
    each DRQ seen, end cycle, status)."""
    out, seen, c, feed = [], [], c0, iter(data if data is not None else ())
    while c < c0 + limit:
        c += poll
        st = wd.read(0, c)
        if st & DRQ:
            seen.append(c)
            if data is None:
                out.append(wd.read(3, c))
            else:
                wd.write(3, next(feed, 0x4E), c)
        if not st & BUSY:
            return bytes(out), seen, c, st
    raise TimeoutError("command still busy")


def seek(wd, cyl, c=0):
    """Restore then Seek to cyl; cycle at the end."""
    _, _, c, _ = command(wd, RESTORE, c)
    wd.write(3, cyl, c)
    _, _, c, st = command(wd, SEEK, c + 64)
    assert not st & RNF and wd.cylinder == cyl and untouched(wd)
    return c


@pytest.mark.parametrize("start", [0, 1, 7, 79, simwd.INNER_STOP])
def test_restore_stops_at_tr00_after_exactly_c_pulses(start):
    wd = drive(None, start)
    wd.write(0, RESTORE, 100)
    first = 100 + simwd.DIR_SETUP
    for k in range(start):
        wd.sync(first + k * T12 - 1)
        assert wd.cylinder == start - k and wd.busy
        wd.sync(first + k * T12)
        assert wd.cylinder == start - k - 1
    wd.sync(first + start * T12 - 1)
    assert wd.busy
    wd.sync(first + start * T12)
    assert not wd.busy and wd.cylinder == 0 == wd.track
    assert wd.pulses == start and untouched(wd)
    assert wd.read(0, 10**7) & (T0 | BUSY | RNF) == T0


def test_status_bits_become_valid_after_the_datasheet_delays():
    wd = drive(None, 3)
    before = wd.read(0, 0)
    assert before & (BUSY | T0) == 0
    wd.write(0, STEP_OUT, 100)
    assert wd.read(0, 100 + simwd.BUSY_VALID - 1) == before
    assert wd.read(0, 100 + simwd.BUSY_VALID) == before | BUSY
    assert wd.read(0, 100 + simwd.STATUS_VALID) & (BUSY | MO) == BUSY | MO
    assert wd.early == 2 and wd.pulses == 1


def naive_restore(wd, first_read, poll=16):
    """Restore polled from first_read cycles after the command until BUSY reads clear,
    then a force interrupt; the status that read clear."""
    wd.write(0, RESTORE, 0)
    c = first_read
    while (st := wd.read(0, c)) & BUSY:
        c += poll
    wd.write(0, FORCE, c + poll)
    return st


def test_an_immediate_poll_forces_out_a_restore_before_its_first_pulse():
    wd = drive(None, 3)
    st = naive_restore(wd, 8)
    wd.sync(10 * T12)
    assert wd.pulses == 0 and wd.cylinder == 3 and not st & T0 and wd.early == 1
    wd = drive(None, 3)
    st = naive_restore(wd, simwd.STATUS_VALID)
    assert wd.pulses == 3 and wd.cylinder == 0 and st & T0 and wd.early == 0
    assert untouched(wd)


def test_idle_force_interrupt_hides_tr00_behind_busy_as_on_hardware():
    """The 1581 sequence of artifacts/hw11 (bounded Restore of 39, then the check after
    settling): $A1, $A5, $A4, then $81 with the head on TR00, $80 once idle (dry2),
    and T0 again only from a type I command: a Seek to the track register's value,
    which issues no pulse."""
    hold = 2 * simwd.STATUS_VALID
    wd = drive(None, 39, force_busy=hold)
    wd.write(0, RESTORE, 0)
    assert wd.read(0, simwd.STATUS_VALID) == MO | SU | BUSY
    deadline = (2 * 39 - 1) * T12 // 2
    assert wd.read(0, deadline - 1) == MO | SU | T0 | BUSY
    wd.write(0, FORCE, deadline)
    assert wd.read(0, deadline + simwd.STATUS_VALID) == MO | SU | T0
    assert wd.pulses == 39 and wd.track == 0xFF - 39 and wd.cylinder == 0
    settle = deadline + 18 * 2000
    wd.write(0, FORCE, settle)
    wd.write(0, RESTORE, settle + simwd.FORCE_GAP)
    assert wd.read(0, settle + simwd.FORCE_GAP + simwd.STATUS_VALID) == MO | BUSY
    assert wd.read(0, settle + hold - 1) == MO | BUSY
    assert wd.read(0, settle + hold) == MO and wd.cylinder == 0
    c = settle + hold + simwd.FORCE_GAP
    wd.write(3, wd.track, c)
    _, _, _, st = command(wd, SEEK, c + simwd.FORCE_GAP)
    assert st == MO | SU | T0 and wd.pulses == 39 and wd.early == 0


@pytest.mark.parametrize("steps, bumps", [(1, 0), (40, 0), (41, 0), (43, 2)])
def test_broken_tr00_restore_stops_pulsing_at_the_deadline(steps, bumps):
    wd = drive(None, 40, tr00=simwd.T0_NEVER)
    wd.write(0, RESTORE, 0)
    deadline = (2 * steps - 1) * T12 // 2
    wd.write(0, FORCE, deadline)
    wd.sync(100 * REV)
    assert wd.pulses == steps and wd.bumps == bumps and wd.inner_stops == 0
    assert wd.cylinder == max(40 - steps, simwd.OUTER_STOP)
    assert not wd.read(0, 100 * REV) & (T0 | BUSY)


def test_restore_gives_up_after_255_pulses_with_a_seek_error():
    wd = drive(MfmMedia(), 3, tr00=simwd.T0_NEVER, stops=(-300, 83))
    _, _, end, st = command(wd, RESTORE | 0x04, 0, limit=400 * T12)
    assert wd.pulses == 255 and wd.cylinder == 3 - 255 and wd.track == 0
    assert st & RNF and end >= 255 * T12 + simwd.SETTLE + 5 * REV
    assert untouched(wd)


def test_step_out_counts_a_bump_only_past_the_outer_stop():
    wd = drive(None, 0, stops=(-2, 3))
    c = 0
    for k in range(4):
        _, _, c, st = command(wd, STEP_OUT, c)
        assert wd.cylinder == max(-1 - k, -2) and wd.bumps == max(k - 1, 0)
        assert bool(st & T0) == (wd.cylinder == 0)
    for _ in range(7):
        _, _, c, _ = command(wd, STEP_IN | 0x10, c)
    assert wd.cylinder == 3 and wd.inner_stops == 2 and wd.track == 7


def test_step_rates_by_chip():
    for chip, rate, cycles in ((1772, 2, 4000), (1770, 2, 40_000), (1770, 1, T12)):
        wd = drive(None, 0, chip=chip)
        _, _, end, _ = command(wd, 0x48 | rate, 0, poll=1)
        assert end == simwd.DIR_SETUP + cycles


def test_seek_with_verify(media):
    wd = drive(media)
    c = seek(wd, 7)
    _, _, end, st = command(wd, SEEK | 0x04, c)
    assert not st & (RNF | CRCE) and end - c < simwd.SETTLE + REV
    wd.write(1, 6, end)
    wd.write(3, 6, end + 64)
    _, _, fail, st = command(wd, SEEK | 0x04, end + 128)
    assert st & RNF and wd.cylinder == 7 and fail - end > simwd.SETTLE + 5 * REV


def read_ids(wd, c, n):
    """n Read Address commands back to back: [(id bytes, status, end cycle)]."""
    out = []
    for _ in range(n):
        data, _, c, st = command(wd, READ_ADDRESS, c)
        out.append((data, st, c))
    return out


def test_read_address_in_rotational_order(media):
    wd = drive(media)
    c = seek(wd, 5)
    ids = read_ids(wd, c, 12)
    sectors = [d[2] for d, _, _ in ids]
    assert all(d[:2] == bytes([5, 0]) and d[3] == 2 for d, _, _ in ids)
    assert all((b - a) % 10 == 1 for a, b in zip(sectors, sectors[1:]))
    assert not any(st & (CRCE | RNF | LD) for _, st, _ in ids)
    assert wd.read(2, ids[-1][2] + 100) == 5
    wd.control(ids[-1][2] + 300, MOTOR_ON | HEAD0)
    assert {d[1] for d, _, _ in read_ids(wd, ids[-1][2] + 400, 3)} == {1}


def corrupt(wd, cyl, head, offset):
    """Flip one byte of the first ID field of a track."""
    data, mark, _ = wd.media.track(cyl, head)
    i = int(np.flatnonzero(data == 0xFE)[0]) + offset
    data[i] ^= 0x40
    wd.media.set_track(cyl, head, data, mark)
    return data, mark


def test_corrupted_id_crc_sets_bit_3(media):
    wd = drive(media)
    corrupt(wd, 0, 1, 3)
    ids = read_ids(wd, 0, 10)
    bad = [st & CRCE for d, st, _ in ids if d[2] == 0x41]
    assert bad == [CRCE] and sum(bool(st & CRCE) for _, st, _ in ids) == 1


def read_sector(wd, c, sector, track=None, **kw):
    if track is not None:
        wd.write(1, track, c)
    wd.write(2, sector, c + 40)
    return command(wd, READ_SECTOR, c + 80, **kw)


def test_read_sector_returns_the_data(media):
    wd = drive(media)
    c = seek(wd, 3)
    for side, head in ((0, 1), (1, 0)):
        wd.control(c, MOTOR_ON | (HEAD0 if head == 0 else 0))
        for r in (1, 6, 10):
            data, seen, c, st = read_sector(wd, c, r)
            assert data == sector_payload(3, side)[r - 1].tobytes()
            assert st & (CRCE | RNF | LD | RT | BUSY) == 0
            assert np.all(np.diff(seen) <= 64)


def test_read_sector_errors(media):
    wd = drive(media)
    data, mark, _ = wd.media.track(0, 1)
    dams = np.flatnonzero((data == 0xFB) & np.roll(mark, 1))
    d = dams[1]
    data[d] = 0xF8
    value = crc16(np.concatenate(([0xA1] * 3, data[d : d + 513])).astype(np.uint8))
    data[d + 513], data[d + 514] = value >> 8, value & 0xFF
    data[dams[2] + 100] ^= 1
    wd.media.set_track(0, 1, data, mark)
    _, _, c, st = read_sector(wd, 0, 2)
    assert st & RT and not st & CRCE
    _, _, c, st = read_sector(wd, c, 3)
    assert st & CRCE and not st & RNF
    start = c + 80
    _, _, end, st = read_sector(wd, c, 11)
    assert st & RNF and 5 * REV <= end - start <= 6 * REV + 64
    _, _, c, st = read_sector(wd, end, 4, track=1)
    assert st & RNF
    out, _, _, st = read_sector(wd, c, 5, track=0, poll=200)
    assert st & LD and len(out) < 512


def test_multiple_sector_read_runs_to_rnf(media):
    wd = drive(media)
    wd.write(2, 8, 0)
    data, _, _, st = command(wd, READ_SECTOR | 0x10, 40)
    assert data == sector_payload(0, 0)[7:].tobytes()
    assert st & RNF and wd.read(2, 10**8) == 11


def index_edges(start, n):
    return [(start // REV + 1 + k) * REV for k in range(n)]


@pytest.mark.parametrize(
    "phase", [0, simwd.INDEX_FRACTION * REV // 2, 250_000, REV - 1]
)
def test_read_track_is_one_revolution_from_an_index_leading_edge(media, phase):
    wd = drive(media)
    c0 = REV + int(phase)
    data, seen, _, st = command(wd, READ_TRACK, c0, poll=8)
    track, _, _ = wd.media.track(0, 1)
    edge = index_edges(c0, 1)[0]
    assert data == track.tobytes() and not st & LD
    assert edge + 64 <= seen[0] < edge + 64 + 8
    assert seen[-1] < edge + REV + 8


def test_weak_bytes_read_random(media):
    wd = drive(media)
    data, mark, weak = wd.media.track(1, 1)
    weak[1000:1100] = True
    wd.media.set_track(1, 1, data, mark, weak)
    c = seek(wd, 1)
    first, _, c, _ = command(wd, READ_TRACK, c, poll=8)
    second, _, _, _ = command(wd, READ_TRACK, c, poll=8)
    assert first[:1000] == second[:1000] and first[1000:1100] != second[1000:1100]
    assert first[1100:] == second[1100:]


def test_write_track_formats_as_the_encoder_does():
    wd = drive(MfmMedia(cylinders=4))
    stream = fmtrk_stream(2, 1, sector_payload(2, 1))
    c = seek(wd, 2)
    wd.write(1, 2, c)
    _, seen, c, st = command(wd, WRITE_TRACK, c + 64, data=stream, poll=8)
    n = wd.media.write_len
    data, mark, weak = wd.media.track(2, 1)
    assert not st & (LD | WPROT) and not weak.any() and len(seen) == 1 + n - 20
    ref_data, ref_mark = stream_track(stream, n)
    assert np.array_equal(data, ref_data) and np.array_equal(mark, ref_mark)
    i = int(np.flatnonzero(data == 0xFE)[0])
    assert data[i - 3 : i].tolist() == [0xA1] * 3 and mark[i - 3 : i].all()
    head = np.concatenate(([0xA1] * 3, data[i : i + 5])).astype(np.uint8)
    assert crc16(head) == int(data[i + 5]) << 8 | int(data[i + 6])
    out, _, c, st = read_sector(wd, c, 4)
    assert out == sector_payload(2, 1)[3].tobytes() and not st & CRCE


def test_write_track_needs_its_first_byte_within_three_byte_times():
    wd = drive(MfmMedia(cylinders=2))
    wd.write(0, WRITE_TRACK, 0)
    wd.sync(REV)
    st = wd.read(0, REV)
    assert st & LD and not st & BUSY and wd.media.track(0, 1)[2].all()


def test_crc_preset_and_c2_marks():
    assert simwd.SYNC3 == 0xCDB4
    wd = drive(MfmMedia(cylinders=1))
    stream = np.array([0x4E] * 8 + [0xF6] * 3 + [0xFC] + [0xF5] * 3 + [0x01, 0xF7])
    command(wd, WRITE_TRACK, 0, data=stream, poll=8)
    data, mark, _ = wd.media.track(0, 1)
    assert data[8:12].tolist() == [0xC2] * 3 + [0xFC] and mark[8:11].all()
    assert not mark[11] and data[12:15].tolist() == [0xA1] * 3 and mark[12:15].all()
    assert int(data[16]) << 8 | int(data[17]) == crc16(
        np.array([0xA1] * 3 + [1], np.uint8)
    )


def write_sector(wd, c, sector, payload, cmd=WRITE_SECTOR, **kw):
    wd.write(2, sector, c)
    return command(wd, cmd, c + 40, data=payload, **kw)


def test_write_sector_round_trip(media):
    wd = drive(media)
    c = seek(wd, 4)
    payload = np.arange(512, dtype=np.uint8) ^ 0x5A
    before, mark0, _ = wd.media.track(4, 1)
    _, _, c, st = write_sector(wd, c, 7, payload)
    assert not st & (LD | WPROT | RNF)
    after, mark, _ = wd.media.track(4, 1)
    i = int(np.flatnonzero((before == 0xFE) & np.roll(mark0, 1))[6]) + 7
    changed = np.flatnonzero(before != after)
    assert changed.min() >= i + 22 + 16 and changed.max() <= i + 22 + 16 + 514
    image = after[i + 22 : i + 22 + 16 + 512 + 3]
    assert image[:16].tolist() == [0] * 12 + [0xA1] * 3 + [0xFB]
    assert image[16:528].tobytes() == payload.tobytes() and image[-1] == 0xFF
    assert mark[i + 34 : i + 37].all() and not mark[i + 37 : i + 22 + 531].any()
    out, _, c, st = read_sector(wd, c, 7)
    assert out == payload.tobytes() and not st & (CRCE | RT)
    _, _, c, st = write_sector(wd, c, 8, payload, WRITE_SECTOR | 1)
    out, _, c, st = read_sector(wd, c, 8)
    assert out == payload.tobytes() and st & RT


def test_write_sector_faults(media):
    wd = drive(media)
    wd.write(2, 2, 0)
    wd.write(0, WRITE_SECTOR, 10)
    wd.sync(REV)
    st = wd.read(0, REV)
    assert st & LD and not st & BUSY
    assert np.array_equal(wd.media.track(0, 1)[0], media.track(0, 1)[0])
    _, _, c, st = write_sector(wd, REV, 3, np.ones(512, np.uint8), poll=100)
    assert st & LD
    data, _, c, _ = read_sector(wd, c, 3)
    assert 0 < data.count(1) < 512
    wd.write_protect = True
    _, _, _, st = write_sector(wd, c, 3, np.ones(512, np.uint8))
    assert st & WPROT and not st & BUSY
    _, _, _, st = command(wd, WRITE_TRACK, c + REV)
    assert st & WPROT


def test_force_interrupt(media):
    wd = drive(media)
    wd.write(2, 10, 0)
    wd.write(0, READ_SECTOR, 100)
    wd.sync(REV // 2)
    wd.read(3, REV // 2)
    st = wd.read(0, REV // 2 + 1)
    assert st & BUSY
    wd.write(0, FORCE, REV // 2 + 2)
    kept = wd.read(0, REV // 2 + 2 + simwd.STATUS_VALID)
    assert not kept & BUSY and kept & ~MO == st & ~(MO | BUSY)
    wd.write(0, FORCE, REV)
    assert wd.read(0, REV + 100) & (T0 | IP | BUSY) == IP
    assert not wd.read(0, REV + 9000) & IP
    wd.write(0, FORCE, REV + 9010)
    wd.write(0, READ_SECTOR, REV + 9020)
    assert wd.early == 1 and wd.busy
    wd.write(0, FORCE, REV + 9100)
    wd.write(1, 9, REV + 9200)
    assert wd.read(1, REV + 9210) == 9 and wd.early == 2


def test_spin_up_sequence_and_motor_timeout(media):
    wd = drive(media)
    _, _, end, st = command(wd, 0x00, 1000)
    assert st & (MO | SU) == MO | SU and 5 * REV < end < 6 * REV + 64
    _, _, end2, _ = command(wd, 0x00, end)
    assert end2 - end == simwd.STATUS_VALID
    assert wd.read(0, end2 + 10 * REV) & (MO | SU) == 0
    _, _, end3, st = command(wd, 0x04 | 0x08, end2 + 10 * REV)
    assert end3 - end2 > 10 * REV + simwd.SETTLE and st & (MO | RNF) == MO
    _, _, end4, _ = command(wd, READ_ADDRESS | 0x04, end3)
    assert end4 - end3 >= simwd.SETTLE


def test_spindle_ready_and_disk_change():
    wd = Wd(MfmMedia(cylinders=2), cylinder=1, spinup=REV)
    pa = 0xFF
    assert wd.inputs(0, pa) == pa & ~simwd.PA_CHNG
    wd.control(10, MOTOR_ON)
    assert wd.inputs(REV + 9, pa) & simwd.PA_RDY
    assert not wd.inputs(REV + 10, pa) & simwd.PA_RDY
    wd.write(0, STEP_IN, REV + 20)
    wd.sync(2 * REV)
    assert wd.inputs(2 * REV, pa) == pa & ~simwd.PA_RDY
    wd.control(2 * REV, MOTOR_OFF)
    wd.write(0, READ_TRACK, 2 * REV + 10)
    wd.sync(10 * REV)
    assert wd.busy
    wd.control(10 * REV, MOTOR_ON)
    wd.sync(12 * REV + 9)
    assert wd.busy
    wd.sync(12 * REV + 10)
    assert not wd.busy
    wd.insert(None, 14 * REV)
    assert wd.inputs(14 * REV, pa) == pa & ~(simwd.PA_CHNG)
    wd.write(0, STEP_OUT, 14 * REV + 10)
    wd.sync(15 * REV)
    assert wd.inputs(15 * REV, pa) & simwd.PA_CHNG == 0
    wd.write(0, READ_ADDRESS, 15 * REV)
    wd.sync(30 * REV)
    assert wd.busy and wd.cylinder == 1
    wd.write(0, FORCE, 30 * REV)
    assert not wd.read(0, 30 * REV + 100) & (IP | BUSY)


def test_side_change_during_a_search_follows_the_new_head(media):
    wd = drive(media)
    wd.write(2, 4, 0)
    wd.write(0, READ_SECTOR, 10)
    wd.control(5000, MOTOR_ON | HEAD0)
    data, _, _, st = service(wd, 5000)
    assert data == sector_payload(0, 1)[3].tobytes() and not st & RNF


def test_media_tracks_and_capacity():
    track = np.arange(7000, dtype=np.uint8), np.zeros(7000, bool)
    media = MfmMedia({(1, 0): track}, rpm=360.0, cylinders=3)
    assert media.write_len == 5208 and media.capacity == 7000
    assert media.period == 333_333
    assert media.track(1, 0)[0].tobytes() == track[0].tobytes()
    assert media.track(0, 0)[2].all() and len(media.track(0, 0)[0]) == 5208
    with pytest.raises(ValueError):
        media.set_track(0, 0, np.zeros(7001, np.uint8))
    with pytest.raises(TypeError):
        MfmMedia(colour=1)
    with pytest.raises(TypeError):
        Wd(media, colour=1)
    wd = Wd(media, cylinder=1)
    wd.control(0, MOTOR_ON | HEAD0)
    data, seen, _, _ = command(wd, READ_TRACK, 0, poll=4)
    assert data == track[0].tobytes()
    assert abs((seen[-1] - seen[0]) / (len(seen) - 1) - media.period / 7000) < 0.01
