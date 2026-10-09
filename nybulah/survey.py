"""Per-halftrack features of every disk image in a tree, for corpus statistics.

Files and (nested) zip members are loaded with :func:`nybulah.formats.loads`;
each track's best revolution is measured as nybulah handles it today. Results
are written as columnar ``part-NNNNN.npz`` files so a scan can resume.
"""

import hashlib
import importlib
import io
import json
import multiprocessing
import os
import pathlib
import time
import zipfile

import numpy as np
from tqdm import tqdm

from .analysis.capture import segments
from .analysis.cycle import TrackKind, lag_window
from .analysis.gcr import (
    SYNC_MIN_BITS,
    bits_per_revolution,
    decode_bits,
    runs_of_ones,
    sectors_per_track,
    speed_zone,
)
from .analysis.sector import (
    DATA_CHECKED_BYTES,
    DATA_GCR_BYTES,
    DATA_ID,
    HEADER_GCR_BYTES,
    HEADER_ID,
    SectorError,
    decode_track,
)
from .formats.g64 import SIDE1
from .formats.image import (
    SIDE1_TRACK_BASE,
    Capture,
    DiskImage,
    best_revolution,
    loads,
)
from .formats.nib import write_nib

SUFFIXES = {".nib", ".nbz", ".nb2", ".g64", ".g71", ".p64", ".scp"}
NIB_KINDS = {"nib", "nbz", "nb2"}
GCR_KINDS = {"g64", "g71"}
GROUP_BITS = 40
HEADER_BITS = 8 * HEADER_GCR_BYTES
DATA_BITS = 8 * DATA_GCR_BYTES
BAM_TRACK = 18
MAX_TRACK = 42
BAM_ID = slice(0xA2, 0xA4)
PART_IMAGES = 256
THREAD_VARS = ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS")
NUMBA_THREADS = "NUMBA_NUM_THREADS"
ERROR_CODES = 16
_BYTE = np.arange(256)
ROTATION_CLASS = np.min(
    [(_BYTE << r | _BYTE >> (8 - r)) & 0xFF for r in range(8)], axis=0
)

TRACK_DTYPE = np.dtype(
    [
        ("image", "<i4"),
        ("key", "<u2"),
        ("halftrack", "u1"),
        ("side", "u1"),
        ("density", "u1"),
        ("zone", "i1"),
        ("std_zone", "i1"),
        ("zones", "u1"),
        ("captures", "u1"),
        ("nbits", "<i4"),
        ("kind", "i1"),
        ("cycle_len", "<i4"),
        ("cycle_z", "<f4"),
        ("cycle_match", "<f4"),
        ("nominal", "<f4"),
        ("in_window", "?"),
        ("hdr_period", "<i4"),
        ("hdr_period_n", "<i2"),
        ("hdr_period_spread", "<i4"),
        ("rev_len", "<i4"),
        ("n_sync", "<i2"),
        ("sync_frac", "<f4"),
        ("sync_min", "<i4"),
        ("sync_max", "<i4"),
        ("zero_runs", "<i4"),
        ("zero_frac", "<f4"),
        ("zero_max", "<i4"),
        ("bad_span", "<i4"),
        ("n_hdr", "<i2"),
        ("n_hdr_bad", "<i2"),
        ("hdr_track", "<i2"),
        ("hdr_track_frac", "<f4"),
        ("n_sect", "<i2"),
        ("n_sect_cap", "<i2"),
        ("n_dupe", "<i2"),
        ("sect_max", "<i2"),
        ("n_extra", "<i2"),
        ("n_id_var", "<i2"),
        ("id_mis", "<i2"),
        ("n_data", "<i2"),
        ("n_data_nonstd", "<i2"),
        ("n_dataid_var", "<i2"),
        ("n_gcr_payload", "<i2"),
        ("n_gcr_tail", "<i2"),
        ("errors", "u1", (ERROR_CODES,)),
        ("errors_cap", "u1", (ERROR_CODES,)),
        ("gap_bytes", "<i4"),
        ("gap_entropy", "<f4"),
        ("gap_top", "<i2"),
        ("gap_top_frac", "<f4"),
        ("sim_half", "<f4"),
        ("sim_half_z", "<f4"),
        ("sim_next", "<f4"),
        ("sim_next_z", "<f4"),
        ("mp_disagree", "<f4"),
        ("mp_span", "<i4"),
        ("pair_agree", "<f4"),
        ("pair_len", "<i4"),
    ]
)
GAP_FIELDS = ("gap_bytes", "gap_entropy", "gap_top", "gap_top_frac")
IMAGE_FIELDS = ("key", "fmt", "sha1", "pair", "error")
IMAGE_INTS = ("disk_id", "cosmetic_id", "tracks")


def _ranges(starts, counts, step=1):
    """Concatenated ``starts[i] + step * arange(counts[i])``."""
    counts = np.asarray(counts, np.int64)
    offsets = np.arange(counts.sum()) - np.repeat(np.cumsum(counts) - counts, counts)
    return np.repeat(np.asarray(starts, np.int64), counts) + step * offsets


def canonical(bits):
    """A circular bit stream with every sync run cut to ``SYNC_MIN_BITS`` ones.

    Captures frame and time syncs differently; this makes them comparable.
    """
    bits = np.asarray(bits, np.uint8)
    starts, lengths = runs_of_ones(bits, circular=True)
    if len(starts) and lengths[0] >= len(bits):
        return bits[:SYNC_MIN_BITS]
    keep = np.ones(len(bits), bool)
    keep[
        _ranges(starts + SYNC_MIN_BITS, lengths - SYNC_MIN_BITS) % max(len(bits), 1)
    ] = 0
    return bits[keep]


def longest_chain(starts, ends, link=GROUP_BITS):
    """Longest span of sorted intervals joined when at most ``link`` bits apart."""
    if len(starts) == 0:
        return 0
    brk = np.flatnonzero(starts[1:] - ends[:-1] > link) + 1
    first = np.concatenate(([0], brk))
    last = np.concatenate((brk - 1, [len(starts) - 1]))
    return int((ends[last] - starts[first]).max())


def agreement(ref, bits):
    """Best alignment of ``bits`` against the circular ``ref``.

    Returns ``(fraction of agreeing bits, z above chance, lag, mismatch mask)``
    over the first ``min(len(ref), len(bits))`` bits of ``bits``.
    """
    n = min(len(ref), len(bits))
    if n == 0:
        return np.nan, np.nan, 0, np.zeros(0, bool)
    ref = np.asarray(ref, np.uint8)
    probe = np.asarray(bits[:n], np.uint8)
    tiled = np.concatenate((ref, ref[:n]))
    size = 1 << (len(tiled) + n).bit_length()
    spec = np.fft.rfft(2.0 * tiled - 1, size) * np.conj(
        np.fft.rfft(2.0 * probe - 1, size)
    )
    corr = np.fft.irfft(spec, size)[: len(ref)]
    lag = int(np.argmax(corr))
    agree = (n + corr[lag]) / (2 * n)
    p, q = ref.mean(), probe.mean()
    chance = p * q + (1 - p) * (1 - q)
    z = (agree - chance) * np.sqrt(n / max(chance * (1 - chance), 1e-12))
    return float(agree), float(z), lag, probe != tiled[lag : lag + n]


def _blocks(bits, ends):
    """Decoded first ``HEADER_GCR_BYTES`` GCR bytes after each sync end (circular)."""
    window = (np.asarray(ends)[:, None] + np.arange(HEADER_BITS)) % len(bits)
    return decode_bits(bits[window])


def _headers_ok(hdr, valid):
    return (
        (hdr[:, 0] == HEADER_ID)
        & valid[:, :6].all(axis=1)
        & (np.bitwise_xor.reduce(hdr[:, 1:6], axis=1) == 0)
    )


def linear_headers(bits):
    """Positions (sync ends) and bytes of the valid headers of a linear capture.

    Bit 0 counts as a sync end: captures usually start just after a sync.
    """
    starts, lengths = runs_of_ones(bits)
    ends = np.concatenate(([0], starts + lengths))
    ends = ends[ends + HEADER_BITS <= len(bits)]
    if len(ends) == 0:
        return ends, np.zeros((0, HEADER_GCR_BYTES * 4 // 5), np.uint8)
    hdr, valid = _blocks(bits, ends)
    ok = _headers_ok(hdr, valid)
    return ends[ok], hdr[ok]


def header_period(pos, hdr, nominal):
    """Revolution length from repeats of identical headers in a linear capture.

    Returns ``(median bits, repeats used, spread)``; repeats are kept within
    half a nominal revolution of ``nominal``. ``(-1, 0, 0)`` when none repeat.
    """
    keys = (hdr[:, 1:6].astype(np.int64) << (8 * np.arange(5))).sum(axis=1)
    order = np.lexsort((pos, keys))
    keys, pos = keys[order], pos[order]
    gaps = np.diff(pos)[keys[1:] == keys[:-1]]
    gaps = gaps[np.abs(gaps - nominal) < nominal / 2]
    if len(gaps) == 0:
        return -1, 0, 0
    return int(np.median(gaps)), len(gaps), int(np.ptp(gaps))


def _sectors(good):
    """Sector numbers of the headers carrying the most common track number."""
    if len(good) == 0:
        return np.zeros(0, np.uint8), -1
    mode = int(np.bincount(good[:, 3]).argmax())
    return good[good[:, 3] == mode, 2], mode


def _gap_stats(rev, ends, seg_len, skip):
    """Gap bytes framed from each sync end: count, entropy (bits/byte), and the
    dominant rotation class and its share (independent of bit framing)."""
    counts = np.maximum((seg_len - skip) // 8, 0)
    starts = _ranges(ends + skip, counts, 8)
    if len(starts) == 0:
        return 0, 0.0, -1, 0.0
    gap = np.packbits(rev[(starts[:, None] + np.arange(8)) % len(rev)], axis=1)
    hist = np.bincount(gap.ravel(), minlength=256)
    p = hist[hist > 0] / len(starts)
    classes = np.bincount(ROTATION_CLASS[gap.ravel()], minlength=256)
    top = int(np.argmax(classes))
    return len(starts), float(-(p * np.log2(p)).sum()), top, classes[top] / len(starts)


def _zone_track(key):
    """Whole track number whose standard zone and sector count apply."""
    return (key & ~SIDE1) // 2


def _physical_track(key):
    half = key & ~SIDE1
    return half // 2 + (SIDE1_TRACK_BASE if key & SIDE1 else 0)


def _header_features(row, hdr, valid, key, disk_id):
    ok = _headers_ok(hdr, valid)
    is_hdr = (hdr[:, 0] == HEADER_ID) & valid[:, 0]
    good = hdr[ok]
    row["n_hdr"] = len(good)
    row["n_hdr_bad"] = int((is_hdr & valid[:, :6].all(axis=1) & ~ok).sum())
    if len(good):
        sectors, row["hdr_track"] = _sectors(good)
        row["hdr_track_frac"] = float((good[:, 3] == _physical_track(key)).mean())
        unique = np.unique(sectors)
        row["n_sect"], row["n_dupe"] = len(unique), len(sectors) - len(unique)
        row["sect_max"] = int(unique.max())
        row["n_extra"] = int((unique >= sectors_per_track(_zone_track(key))).sum())
        ids = good[:, 5].astype(np.int32) << 8 | good[:, 4]
        row["n_id_var"] = len(np.unique(ids))
        row["id_mis"] = int((ids != disk_id).sum()) if disk_id >= 0 else 0
    nxt = np.roll(np.arange(len(hdr)), -1)
    row["n_data"] = int(((hdr[:, 0] == DATA_ID) & valid[:, 0]).sum())
    follow = ok & ~is_hdr[nxt] & valid[nxt, 0]
    marks = hdr[nxt[follow], 0]
    row["n_data_nonstd"] = int((marks != DATA_ID).sum())
    row["n_dataid_var"] = len(np.unique(marks))
    return np.where(is_hdr, HEADER_BITS, np.where(hdr[:, 0] == DATA_ID, DATA_BITS, 0))


def _set(row, names, values):
    for name, value in zip(names, values):
        row[name] = value


def _run_features(row, rev, lengths):
    """Sync and illegal-GCR (three or more zero cells) run statistics."""
    n = len(rev)
    row["rev_len"], row["n_sync"] = n, len(lengths)
    row["sync_frac"] = lengths.sum() / max(n, 1)
    row["sync_min"] = lengths.min() if len(lengths) else 0
    row["sync_max"] = lengths.max() if len(lengths) else 0
    zs, zl = runs_of_ones(1 - rev, min_len=3, circular=True)
    row["zero_runs"], row["zero_frac"] = len(zs), zl.sum() / max(n, 1)
    row["zero_max"] = zl.max() if len(zl) else 0
    row["bad_span"] = longest_chain(zs, zs + zl)


def _data_gcr(row, rev, data):
    """Data blocks with invalid GCR in marker, payload or checksum, or only after it."""
    if len(data):
        _, bvalid = decode_bits(rev[(data[:, None] + np.arange(DATA_BITS)) % len(rev)])
        payload = bvalid[:, :DATA_CHECKED_BYTES].all(axis=1)
        row["n_gcr_payload"] = (~payload).sum()
        row["n_gcr_tail"] = (payload & ~bvalid.all(axis=1)).sum()


def _rev_features(row, rev, key, disk_id):
    """Sync, illegal-GCR, header, sector and gap features of one revolution."""
    n = len(rev)
    starts, lengths = runs_of_ones(rev, circular=True)
    _run_features(row, rev, lengths)
    if len(starts) and lengths[0] >= n:
        return lengths
    if len(starts) == 0:
        _set(row, GAP_FIELDS, _gap_stats(rev, np.zeros(1, np.int64), np.array([n]), 0))
        return lengths
    ends = (starts + lengths) % n
    hdr, valid = _blocks(rev, ends)
    skip = _header_features(row, hdr, valid, key, disk_id)
    _data_gcr(row, rev, ends[(hdr[:, 0] == DATA_ID) & valid[:, 0]])
    _set(row, GAP_FIELDS, _gap_stats(rev, ends, (np.roll(starts, -1) - ends) % n, skip))
    if _decodable(key):
        decoded = decode_track(rev, *_decode_args(key, disk_id))
        row["errors"] = np.bincount(decoded.errors, minlength=ERROR_CODES)
    return lengths


def _decodable(key):
    return not key & 1 and _zone_track(key) <= MAX_TRACK


def _decode_args(key, disk_id):
    disk_id = None if disk_id < 0 else bytes([disk_id >> 8, disk_id & 0xFF])
    return _physical_track(key), disk_id, sectors_per_track(_zone_track(key))


def _capture_features(row, caps, cap, cycle, key, disk_id):
    half = key & ~SIDE1
    speed = np.asarray(cap.speed) if cap.speed is not None else np.zeros(0)
    row["key"], row["halftrack"], row["side"] = key, half, int(bool(key & SIDE1))
    row["zone"], row["std_zone"] = cap.zone, speed_zone(half // 2)
    row["zones"] = len(np.unique(speed)) if speed.size > 1 else 1
    row["captures"], row["nbits"] = len(caps), len(cap.bits)
    row["kind"], row["cycle_len"] = cycle.kind, cycle.length
    row["cycle_z"], row["cycle_match"] = cycle.z, cycle.match
    row["nominal"] = bits_per_revolution(cap.zone)
    lo, hi = lag_window(cap.zone)
    row["in_window"] = lo <= cycle.length <= hi
    if _decodable(key) and len(cap.bits):
        errors = decode_track(cap.bits, *_decode_args(key, disk_id)).errors
        row["errors_cap"] = np.bincount(errors, minlength=ERROR_CODES)
    if cap.circular:
        row["n_sect_cap"] = row["n_sect"]
        return
    pos, good = linear_headers(cap.bits)
    row["hdr_period"], row["hdr_period_n"], row["hdr_period_spread"] = header_period(
        pos, good, row["nominal"]
    )
    row["n_sect_cap"] = len(np.unique(_sectors(good)[0]))


def _multipass(row, others, ref):
    """Disagreement of the other captures with the chosen revolution."""
    worst, span = [], 0
    for cap in others:
        agree, _, _, miss = agreement(ref, canonical(cap.bits))
        worst.append(1 - agree)
        hits = np.flatnonzero(miss)
        span = max(span, longest_chain(hits, hits + 1))
    row["mp_disagree"], row["mp_span"] = float(np.median(worst)), span


def disk_ids(revs):
    """``(format ID, BAM ID)`` as 16-bit values from track 18 sector 0, -1 if unread.

    ``revs`` maps track keys to ``(capture, revolution bits, ...)``.
    """
    if BAM_TRACK * 2 not in revs:
        return -1, -1
    decoded = decode_track(revs[BAM_TRACK * 2][1], BAM_TRACK)
    if decoded.errors[0] != SectorError.OK:
        return -1, -1
    fmt = int(decoded.ids[0, 0]) << 8 | int(decoded.ids[0, 1])
    bam = decoded.data[0, BAM_ID]
    return fmt, int(bam[0]) << 8 | int(bam[1])


def fast_size(n):
    """Smallest 5-smooth integer (2^a 3^b 5^c) of at least ``n``: a fast FFT length."""
    best, p5 = 1 << max(int(n - 1).bit_length(), 0), 1
    while p5 < best:
        p35 = p5
        while p35 < best:
            p235 = p35 << max(int(-(-n // p35) - 1).bit_length(), 0)
            best = min(best, p235)
            p35 *= 3
        p5 *= 5
    return best


class Spectra:
    """Best circular alignments between many tracks, each spectrum computed once.

    The longer track of a pair is the circular reference; the shorter one is
    slid over it whole. One FFT size per set keeps every spectrum reusable.
    """

    def __init__(self, tracks):
        self.tracks = {k: np.asarray(v, np.uint8) for k, v in tracks.items()}
        self.size = fast_size(
            3 * max((len(v) for v in self.tracks.values()), default=1)
        )
        self._ref, self._probe = {}, {}

    def _spectrum(self, cache, key, tiles):
        if key not in cache:
            signal = 2.0 * np.tile(self.tracks[key], tiles) - 1
            spec = np.fft.rfft(signal, self.size)
            cache[key] = spec if tiles == 2 else np.conj(spec)
        return cache[key]

    def agreement(self, a, b):
        """``(fraction of agreeing bits, z above chance)`` at the best alignment."""
        long, short = (a, b) if len(self.tracks[a]) >= len(self.tracks[b]) else (b, a)
        ref, probe = self.tracks[long], self.tracks[short]
        n = len(probe)
        if n == 0:
            return np.nan, np.nan
        spec = self._spectrum(self._ref, long, 2) * self._spectrum(
            self._probe, short, 1
        )
        corr = np.fft.irfft(spec, self.size)[: len(ref)].max()
        agree = (n + corr) / (2 * n)
        p, q = ref.mean(), probe.mean()
        chance = p * q + (1 - p) * (1 - q)
        z = (agree - chance) * np.sqrt(n / max(chance * (1 - chance), 1e-12))
        return float(agree), float(z)


def _similar(spectra, kinds, key, other):
    if other not in kinds or TrackKind.KILLER in (kinds[key], kinds[other]):
        return np.nan, np.nan
    return spectra.agreement(key, other)


def _density(image):
    if image.kind not in NIB_KINDS:
        return {}
    return {e.halftrack: e.density for e in image.source.entries}


def _compare(row, key, canon, kinds, pair):
    """Neighbour and paired-image similarity of one track's revolution."""
    row["sim_half"], row["sim_half_z"] = _similar(canon, kinds, key, key + 1)
    row["sim_next"], row["sim_next_z"] = _similar(canon, kinds, key, key + 2)
    if pair is not None and key in pair.tracks:
        bits = pair.tracks[key][0].bits
        row["pair_agree"] = agreement(canonical(bits), canon.tracks[key])[0]
        row["pair_len"] = len(bits)


def _track(row, caps, key, best, disk_id):
    cap, rev, cycle = best
    lengths = _rev_features(row, rev, key, disk_id)
    _capture_features(row, caps, cap, cycle, key, disk_id)
    others = [c for c in caps if c is not cap]
    if others:
        _multipass(row, others, canonical(rev))
    return lengths


def survey_image(image, pair=None, revolution=best_revolution):
    """Per-track feature rows (``TRACK_DTYPE``) and sync lengths of a DiskImage.

    ``pair`` is a G64/G71 image of the same disk to compare against;
    ``revolution(captures, key)`` returns the ``(capture, bits, cycle)`` under
    test. Returns ``(rows, sync_lengths, sync_rows, (format ID, BAM ID))``.
    """
    keys = sorted(image.tracks)
    revs = {k: revolution(image.tracks[k], k) for k in keys}
    ids = disk_ids(revs)
    canon = Spectra({k: canonical(r[1]) for k, r in revs.items()})
    kinds = {k: r[2].kind for k, r in revs.items()}
    density = _density(image)
    rows = _empty_rows(len(keys))
    syncs = []
    for i, key in enumerate(keys):
        rows[i]["density"] = density.get(key, revs[key][0].zone)
        syncs.append(_track(rows[i], image.tracks[key], key, revs[key], ids[0]))
        _compare(rows[i], key, canon, kinds, pair)
    sync_rows = np.repeat(np.arange(len(keys)), [len(s) for s in syncs])
    lengths = np.concatenate(syncs) if syncs else np.zeros(0, np.int64)
    return rows, lengths.astype(np.int32), sync_rows.astype(np.int32), ids


def _empty_rows(n):
    """Feature rows with every measurement marked absent (nan or -1)."""
    rows = np.zeros(n, TRACK_DTYPE)
    for name in TRACK_DTYPE.names[1:]:
        if TRACK_DTYPE[name].kind == "f":
            rows[name] = np.nan
    rows[["hdr_track", "sect_max", "gap_top", "mp_span", "pair_len"]] = (-1,) * 5
    return rows


def _zip_items(source, prefix):
    with zipfile.ZipFile(source) as archive:
        for info in archive.infolist():
            suffix = pathlib.PurePath(info.filename).suffix.lower()
            if suffix == ".zip":
                try:
                    yield from _zip_items(
                        io.BytesIO(archive.read(info)), prefix + (info.filename,)
                    )
                except (zipfile.BadZipFile, OSError, EOFError):
                    continue
            elif suffix in SUFFIXES and not info.is_dir():
                yield prefix + (info.filename,)


def list_items(root, progress=True):
    """Every image under ``root`` as a tuple: relative path, then zip members."""
    root = pathlib.Path(root)
    items = []
    for path in tqdm(
        sorted(root.rglob("*")), desc="list", unit="file", disable=not progress
    ):
        rel = str(path.relative_to(root))
        if path.suffix.lower() == ".zip":
            try:
                items += list(_zip_items(path, (rel,)))
            except (zipfile.BadZipFile, OSError, EOFError):
                continue
        elif path.suffix.lower() in SUFFIXES and path.is_file():
            items.append((rel,))
    return items


def read_item(root, item):
    """Bytes of an item from :func:`list_items`.

    Archives are read through their central directory, so only the member's
    compressed bytes are fetched, not the whole archive.
    """
    path = pathlib.Path(root) / item[0]
    if len(item) == 1:
        return path.read_bytes()
    source = path
    for member in item[1:]:
        with zipfile.ZipFile(source) as archive:
            data = archive.read(member)
        source = io.BytesIO(data)
    return data


def item_key(item):
    """Stable text key of an item."""
    return "::".join(item)


def pair_jobs(items):
    """``(item, G64 item of the same disk or None)``: NIB-family and G64 by stem."""
    groups = {}
    for item in items:
        path = pathlib.PurePath(item[-1])
        groups.setdefault((item[:-1], str(path.parent), path.stem.lower()), []).append(
            item
        )
    jobs = []
    for group in groups.values():
        g64 = [i for i in group if pathlib.PurePath(i[-1]).suffix.lower() == ".g64"]
        for item in group:
            nib = pathlib.PurePath(item[-1]).suffix.lower() in {".nib", ".nbz", ".nb2"}
            jobs.append((item, g64[0] if nib and g64 else None))
    return jobs


def _fingerprint(image, buf):
    data = write_nib(image.source) if image.kind in NIB_KINDS else buf
    return hashlib.sha1(data).hexdigest()


def run_job(root, item, pair=None):
    """Survey one item: ``(image record, rows, sync lengths, sync rows)``."""
    start = time.perf_counter()
    meta = dict.fromkeys(IMAGE_FIELDS, "") | dict.fromkeys(IMAGE_INTS, -1)
    meta["key"], meta["pair"] = item_key(item), item_key(pair) if pair else ""
    rows = np.zeros(0, TRACK_DTYPE)
    lengths = sync_rows = np.zeros(0, np.int32)
    try:
        buf = read_item(root, item)
        image = loads(buf, item[-1])
        meta["fmt"], meta["sha1"] = image.kind, _fingerprint(image, buf)
        other = loads(read_item(root, pair), pair[-1]) if pair else None
        rows, lengths, sync_rows, ids = survey_image(image, other)
        meta["disk_id"], meta["cosmetic_id"] = ids
        meta["tracks"] = len(rows)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        meta["error"] = f"{type(exc).__name__}: {exc}"
    meta["seconds"] = time.perf_counter() - start
    return meta, rows, lengths, sync_rows


def _job(args):
    return run_job(*args)


def save_part(path, results):
    """Write survey results as one columnar part (atomic rename)."""
    rows = [r[1] for r in results]
    image = np.repeat(np.arange(len(rows)), [len(r) for r in rows])
    tracks = np.concatenate(rows) if rows else np.zeros(0, TRACK_DTYPE)
    tracks["image"] = image
    offsets = np.cumsum([0] + [len(r) for r in rows])[:-1]
    arrays = {
        f"image_{name}": np.array([r[0][name] for r in results])
        for name in IMAGE_FIELDS + IMAGE_INTS + ("seconds",)
    }
    arrays["tracks"] = tracks
    arrays["sync_len"] = np.concatenate(
        [np.zeros(0, np.int32)] + [r[2] for r in results]
    )
    arrays["sync_row"] = np.concatenate(
        [np.zeros(0, np.int64)] + [r[3] + o for r, o in zip(results, offsets)]
    ).astype(np.int32)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "wb") as handle:
        np.savez(handle, **arrays)
    os.replace(tmp, path)


def load_survey(out):
    """Concatenate every part in ``out``: image columns, tracks and sync lengths."""
    parts = [dict(np.load(p)) for p in sorted(pathlib.Path(out).glob("part-*.npz"))]
    if not parts:
        return {
            "tracks": np.zeros(0, TRACK_DTYPE),
            "sync_len": np.zeros(0, np.int32),
            "sync_row": np.zeros(0, np.int32),
        }
    images = np.cumsum([0] + [len(p["image_key"]) for p in parts])
    rows = np.cumsum([0] + [len(p["tracks"]) for p in parts])
    out = {
        k: np.concatenate([p[k] for p in parts]) for k in parts[0] if k != "sync_row"
    }
    out["tracks"]["image"] += np.repeat(images[:-1], [len(p["tracks"]) for p in parts])
    out["sync_row"] = np.concatenate(
        [p["sync_row"] + r for p, r in zip(parts, rows[:-1])]
    )
    return out


def _listing(root, out, progress):
    """Items under ``root``, listed once and cached in ``out``."""
    listing = out / "items.json"
    if listing.exists():
        return [tuple(i) for i in json.loads(listing.read_text())]
    items = list_items(root, progress)
    listing.write_text(json.dumps(items))
    return items


def _surveyed(parts):
    done = set()
    for part in parts:
        with np.load(part) as data:
            done.update(np.asarray(data["image_key"], str).tolist())
    return done


def scan(root, out, workers=18, limit=None, progress=True):
    """Survey every not yet surveyed item under ``root`` into ``out``."""
    out = pathlib.Path(out)
    out.mkdir(parents=True, exist_ok=True)
    items = _listing(root, out, progress)
    parts = sorted(out.glob("part-*.npz"))
    done = _surveyed(parts)
    todo = [j for j in pair_jobs(items) if item_key(j[0]) not in done][:limit]
    for var in THREAD_VARS:
        os.environ.setdefault(var, "2")
    os.environ.setdefault(NUMBA_THREADS, "2")
    number = len(parts)
    batch = []
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(workers) as pool:
        for result in tqdm(
            pool.imap_unordered(_job, [(str(root), *j) for j in todo]),
            total=len(todo),
            desc="survey",
            unit="img",
            disable=not progress,
        ):
            batch.append(result)
            if len(batch) >= PART_IMAGES:
                save_part(out / f"part-{number:05d}.npz", batch)
                number, batch = number + 1, []
    if batch:
        save_part(out / f"part-{number:05d}.npz", batch)
    return len(todo)


def survey_captures(captures):
    """Survey captures keyed by halftrack: ``{key: [formats.image.Capture]}``."""
    return survey_image(DiskImage("capture", captures))


def load_captures(folder):
    """``{halftrack | side: [Capture]}`` from saved nibbler capture records.

    Each keeps its record as ``framed``, so revolutions are found per segment.
    """
    nibbler = importlib.import_module(f"{__package__}.nibbler")
    captures = {}
    for path in sorted(pathlib.Path(folder).glob("read-*.npz")):
        cap = nibbler.Capture.load(path)
        key = cap.halftrack | (SIDE1 if cap.side else 0)
        captures.setdefault(key, []).append(
            Capture(segments(cap).bits, cap.density, framed=cap)
        )
    return captures


class Survey:
    """Survey a corpus of disk images into per-track feature tables and a summary."""

    NEEDS_ADAPTER = False

    @staticmethod
    def add_arguments(ap):
        ap.add_argument("corpus", type=pathlib.Path, nargs="?")
        ap.add_argument("--out", type=pathlib.Path, default=pathlib.Path("survey"))
        ap.add_argument("--workers", type=int, default=os.cpu_count())
        ap.add_argument("--limit", type=int, help="survey at most this many images")
        ap.add_argument(
            "--captures",
            type=pathlib.Path,
            help="nibbler capture records of a reference disk (read-*.npz)",
        )

    @staticmethod
    def execute(args, _cbm=None):
        from .scenarios import summarise

        surveyed = (
            0
            if args.corpus is None
            else scan(args.corpus, args.out, args.workers, args.limit)
        )
        reference = None
        if args.captures is not None:
            rows, lengths, _, _ = survey_captures(load_captures(args.captures))
            reference = (rows, lengths)
        summary = summarise(load_survey(args.out), reference)
        (args.out / "summary.json").write_text(json.dumps(summary, indent=1))
        out = {"surveyed": surveyed, "summary": str(args.out / "summary.json")}
        print(json.dumps(out))
        return out
