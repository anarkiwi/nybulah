"""1581 streams (drive/mfmstream.s through xum1541 firmware v12): records and stamps.

Framing is the 1571's (:mod:`nybulah.stream`); a metadata code has its low two bits clear
and the bytes with low bits 01 after it carry its payload, six bits each, MSB first.
"""

import dataclasses

import numpy as np

from .stream import Stream

M_START, M_REC, M_KEEP, M_INDEX = 0x04, 0x0C, 0x14, 0x1C
CODES = {M_START: "start", M_REC: "rec", M_KEEP: "keep", M_INDEX: "index"}
ST_NOGO = 0xFF  # drive/mfmstream.s: the host never asserted CLK
END = {0x40: "done", 0x44: "timeout", 0x48: "atn"}
CHUNK = 0x01
STAMP_ICR, STAMP_LO = 4, 22  # drive/mfmstream.s STAMP: cycles after the reference
WINDOW_US = (STAMP_LO - STAMP_ICR) // 2  # a wrap in between leaves elapsed <= this
TB_WRAP = 1 << 16
EPOCH = 1 << 8  # the drive counts wraps in a byte
REC_BYTES = 14  # two stamps, status, flags, count (lo, hi)
PAYLOAD = {M_REC: REC_BYTES, M_INDEX: 5}
F_TIMEOUT = 0x01
OP_INDEX, OP_READ_ADDRESS, OP_READ_SECTOR, OP_READ_TRACK = 0x00, 0xC8, 0x88, 0xE8
OP_END = 0xFF
LIST_ENTRIES = 4  # drive/mfmstream.s L: 16 bytes
REP_MAX = 0x7F
INCREMENT = 0x80


def stamp_us(raw):
    """Microseconds (modulo EPOCH wraps) of a stamp: wraps, ICR bit 1, timer B high,
    low, high; the high read on the low read's side of a borrow, plus a wrap flagged at
    the ICR read or one between it and the timer reads (no flag, elapsed <= WINDOW_US).
    """
    wraps, flag, hi1, lo, hi2 = (int(v) for v in raw)
    hi = hi2 if lo & 0x80 else hi1
    elapsed = (0xFF - hi) << 8 | (0xFF - lo)
    epoch = wraps + bool(flag) + (not flag and elapsed <= WINDOW_US)
    return (epoch % EPOCH) * TB_WRAP + elapsed


def unwrap_us(stamps):
    """Monotonic microseconds from stamps taken in order, each under EPOCH wraps on."""
    t = np.asarray(stamps, np.int64)
    steps = np.diff(t, prepend=t[:1]) % (EPOCH * TB_WRAP)
    return np.cumsum(steps) + (t[0] if len(t) else 0)


def unpack(chunks, nbytes):
    """The nbytes payload carried by six-bit chunk values."""
    bits = np.unpackbits(np.asarray(chunks, np.uint8)[:, None] << 2, axis=1)[:, :6]
    return np.packbits(bits.ravel()[: 8 * nbytes]).tobytes()


def chunks_for(nbytes):
    """Chunks the drive sends for nbytes."""
    return -(-8 * nbytes // 6)


@dataclasses.dataclass
class Command:
    """One WD command of a stream: its bytes and its REC."""

    data: np.ndarray
    status: int
    timeout: bool
    t_first: int
    t_end: int


@dataclasses.dataclass
class MfmStream:  # pylint: disable=too-many-instance-attributes
    """A parsed 1581 stream: commands, index times, how it ended.

    ``reply`` is the (A, X, Y) the drive's J returned: the end code, the entries
    it started, the WD status; ``codes`` counts the metadata codes received and
    ``data_bytes`` every data byte, recorded or not.
    """

    commands: list
    index_us: np.ndarray
    adapter: str
    drive_end: str | None
    keepalives: int
    reply: tuple | None = None
    codes: dict = dataclasses.field(default_factory=dict)
    raw_bytes: int = 0
    data_bytes: int = 0
    elapsed_s: float | None = None

    @property
    def complete(self):
        """The drive ended the list itself and the adapter lost nothing."""
        return self.adapter == "done" and self.drive_end == "done"

    def diagnosis(self):
        """What the drive and the adapter reported, for a stream that fell short."""
        a, issued, status = self.reply or (None, None, None)
        drive = (
            "no reply"
            if a is None
            else (
                "never saw the host's go"
                if a == ST_NOGO
                else END.get(a, f"reply ${a:02X} is no end code")
            )
        )
        return {
            "drive": drive,
            "entries_started": issued,
            "wd_status": status,
            "codes": self.codes,
            "raw_bytes": self.raw_bytes,
            "data_bytes": self.data_bytes,
            "elapsed_s": self.elapsed_s,
        }

    @classmethod
    def parse(cls, raw, reply=None):
        """Split adapter output into commands with their records; reply is the
        (A, X, Y) the drive's J returned."""
        s = Stream.parse(raw)
        records, ends, keep = _records(s.val)
        stamps = [stamp_us(r[k : k + 5]) for kind, r in records for k in _stamps(kind)]
        times = iter(unwrap_us(stamps).tolist())
        commands, index, at = [], [], 0
        for kind, r in records:
            if kind == M_INDEX:
                index.append(next(times))
                continue
            first, end = next(times), next(times)
            n = r[12] | r[13] << 8
            data = s.data[at : at + n]
            commands.append(Command(data, r[10], bool(r[11] & F_TIMEOUT), first, end))
            at += n
        end = ends[-1] if ends else None
        index = np.array(index, np.int64)
        names = {**CODES, **{k: f"end_{v}" for k, v in END.items()}}
        values, counts = np.unique(s.val[s.val & 0x03 != CHUNK], return_counts=True)
        codes = {
            names.get(v, f"${v:02X}"): c
            for v, c in zip(values.tolist(), counts.tolist())
        }
        reply = None if reply is None else tuple(int(v) for v in reply)
        return cls(
            commands, index, s.adapter, end, keep, reply, codes, len(raw), len(s.data)
        )


def _stamps(kind):
    return (0, 5) if kind == M_REC else (0,)


def _records(val):
    """(kind, payload bytes) of every record, END names, keepalive count."""
    records, ends, keep, code, chunks = [], [], 0, None, []
    for v in val.tolist():
        if v & 0x03 == CHUNK:
            chunks.append(v >> 2)
            if code in PAYLOAD and len(chunks) == chunks_for(PAYLOAD[code]):
                records.append((code, unpack(chunks, PAYLOAD[code])))
                code = None
            continue
        code, chunks = v, []
        if v in END:
            ends.append(END[v])
        keep += v == M_KEEP
    return records, ends, keep


def entry(op, trk=0, sec=0, rep=1, increment=False):
    """One list entry for drive/mfmstream.s."""
    if not 1 <= rep <= REP_MAX:
        raise ValueError(f"rep must be 1..{REP_MAX}")
    return bytes([op, trk & 0xFF, sec & 0xFF, rep | (INCREMENT if increment else 0)])


def command_list(entries):
    """The list block: the entries and the end marker, LIST_ENTRIES slots at most."""
    if len(entries) >= LIST_ENTRIES:
        raise ValueError(f"at most {LIST_ENTRIES - 1} entries")
    return b"".join(entries) + bytes([OP_END])
