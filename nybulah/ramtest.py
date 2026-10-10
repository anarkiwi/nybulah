"""Drive RAM test: March C- on the drive per data background, then link transfers.

March C- (van de Goor 1991) runs drive/ramtest.s from two disjoint placements over RAM
outside zero page, the stack and the monitor; seeded random blocks and their inverse
written and read back over the link separate link faults from RAM faults.
"""

import functools
import json
import sys

import numpy as np
from tqdm import tqdm

from . import tool
from .link import BASE
from .monitor import Monitor, drivecode, protocols
from .nibbler import BUFPG
from .ramprobe import BASE_RAM, RAM_1581, identify_model, io_mask

EXPANSION_SIZE = 0x2000
FIRST_PAGE = 2
READ, WRITE = 0x80, 0x40
ORIGINS = (0x10, 0x11)

# drive/ramtest.s layout: parameters, failure count and log, page and count tables.
PARAMS, COUNT, LOG, PAGES, PCL, PCH = 0x03, 0x0F, 0x11, 0x55, 0x95, 0xD5
NLOG, MAXP = (PAGES - 4 - LOG) // 4, PCL - PAGES

# (descending, read D, write D); D 1 is the inverted background, None no access.
MARCH_C_MINUS = (
    (False, None, 0),
    (False, 0, 1),
    (False, 1, 0),
    (True, 0, 1),
    (True, 1, 0),
    (False, 0, None),
)

# (ymask, cmask, pmask, c0): K = offset & ymask ^ (cmask if odd) ^ page & pmask ^ c0.
BACKGROUNDS = {
    "solid": (0, 0, 0, 0x00),
    "checker": (0, 0xFF, 0, 0x55),
    "0x33": (0, 0, 0, 0x33),
    "0x0f": (0, 0, 0, 0x0F),
    "addr_lo": (0xFF, 0, 0, 0),
    "addr_hi": (0, 0, 0xFF, 0),
}


def background(addr, name):
    """Background byte at each address (D = 0)."""
    ym, cm, pm, c0 = BACKGROUNDS[name]
    addr = np.asarray(addr)
    lo, hi = addr & 0xFF, addr >> 8
    return (lo & ym) ^ np.where(lo & 1, cm, 0) ^ (hi & pm) ^ c0


def relocatable():
    """The test code and the offsets of its address high bytes."""
    a, b = (np.frombuffer(drivecode(f"ramtest_{o:02x}"), np.uint8) for o in ORIGINS)
    moved = np.flatnonzero(a != b)
    if len(a) != len(b) or np.any(b[moved] - a[moved] != ORIGINS[1] - ORIGINS[0]):
        raise ValueError("ramtest builds differ in more than address high bytes")
    return a, moved


def relocate(code, moved, page):
    """The code assembled for page."""
    out = code.copy()
    out[moved] += np.uint8((page - ORIGINS[0]) & 0xFF)
    return out.tobytes()


def runs(pages):
    """Consecutive page runs of a sorted page array."""
    return np.split(pages, np.flatnonzero(np.diff(pages) != 1) + 1)


def regions(model, monitor_size):
    """{name: sorted page array}: RAM outside zero page, the stack and the monitor."""
    if model == "1581":
        spans = {"ram": (0, RAM_1581)}
    else:
        lo = BUFPG[model] << 8
        spans = {"base": (0, BASE_RAM), "expansion": (lo, lo + EXPANSION_SIZE)}
    page_io = io_mask(model).reshape(-1, 256).any(axis=1)
    out = {}
    for name, (lo, hi) in spans.items():
        p = np.arange(max(lo >> 8, FIRST_PAGE), hi >> 8)
        mon = (p >= BASE >> 8) & (p < -(-(BASE + monitor_size) // 256))
        out[name] = p[~mon & ~page_io[p]]
    return out


def placements(pages, n, unsafe=()):
    """Up to two disjoint runs of n consecutive pages outside unsafe, lowest first."""
    pages = np.setdiff1d(pages, unsafe)
    head = pages[: max(len(pages) - n + 1, 0)]
    starts = head[pages[n - 1 :] - head == n - 1]
    if not starts.size:
        raise ValueError(f"no {n}-page run of sound RAM to hold the test code")
    second = starts[starts >= starts[0] + n][:1]
    return (int(starts[0]),) + tuple(int(s) for s in second)


def address_check(mon, pages):
    """Rows [addr, written, read, pass] differing after every page is filled with its
    number (pass 0), then its offsets (pass 1), all written before any is read; and
    the pages unsafe to hold code: those differing or whose number another read."""
    addr = (np.asarray(pages)[:, None] << 8 | np.arange(256)).ravel()
    rows = []
    for k, pattern in enumerate((addr >> 8, addr & 0xFF)):
        data = pattern.astype(np.uint8)
        spans = [(int(r[0]) << 8, len(r) << 8) for r in runs(np.asarray(pages))]
        offs = np.cumsum([0] + [n for _, n in spans])
        for (lo, n), o in zip(spans, offs):
            mon.write(lo, data[o : o + n].tobytes())
        back = np.concatenate(
            [np.frombuffer(mon.read(lo, n), np.uint8) for lo, n in spans]
        )
        d = np.flatnonzero(back != data)
        rows.append(np.column_stack((addr[d], data[d], back[d], np.full(len(d), k))))
    rows = np.concatenate(rows).astype(np.int64)
    named = rows[rows[:, 3] == 0, 2]
    unsafe = np.union1d(rows[:, 0] >> 8, np.intersect1d(named, pages))
    return rows, unsafe


def element_params(table_len, element, bg):
    """Parameter bytes for one march element over table_len pages."""
    down, rd, wr = element
    flags = (READ if rd is not None else 0) | (WRITE if wr is not None else 0)
    first, end, step = (table_len - 1, 0xFF, 0xFF) if down else (0, table_len, 1)
    head = (flags, 0xFF * (rd or 0), 0xFF * (wr or 0))
    return bytes(head + BACKGROUNDS[bg] + (first, end, step, 0xFF * down, step))


class March:
    """drive/ramtest.s loaded at one placement, testing a page table."""

    def __init__(self, mon, page, table):
        if len(table) > MAXP:
            raise ValueError(f"at most {MAXP} pages per placement")
        code, moved = relocatable()
        self.mon, self.at, self.table = mon, page << 8, np.asarray(table)
        mon.write(self.at, relocate(code, moved, page))
        mon.write(self.at + PAGES, self.table.astype(np.uint8).tobytes())

    def element(self, element, bg):
        """Run one element; per-page failure counts and logged [addr, expected, read]."""
        n = len(self.table)
        self.mon.write(self.at + PARAMS, element_params(n, element, bg))
        a, x, _ = self.mon.jsr(self.at)
        count = a | x << 8
        if not count:
            return np.zeros(n, np.int64), np.zeros((0, 3), np.int64)
        raw = np.frombuffer(self.mon.read(self.at + PCL, PCH - PCL + n), np.uint8)
        per_page = raw[:n].astype(np.int64) | raw[PCH - PCL :].astype(np.int64) << 8
        e = np.frombuffer(self.mon.read(self.at + LOG, 4 * min(count, NLOG)), np.uint8)
        e = e.reshape(-1, 4).astype(np.int64)
        return per_page, np.column_stack((e[:, 0] | e[:, 1] << 8, e[:, 2], e[:, 3]))


def bit_faults(expected, read):
    """OR/AND of the failing XORs; bits only ever read low (stuck0) or high (stuck1)."""
    expected, read = np.asarray(expected, np.int64), np.asarray(read, np.int64)
    if not expected.size:
        return {"xor_or": 0, "xor_and": 0, "stuck0": 0, "stuck1": 0}
    x = expected ^ read
    low = int(np.bitwise_or.reduce(expected & ~read))
    high = int(np.bitwise_or.reduce(~expected & read))
    return {
        "xor_or": int(np.bitwise_or.reduce(x)),
        "xor_and": int(np.bitwise_and.reduce(x)),
        "stuck0": low & ~high,
        "stuck1": high & ~low,
    }


def by_address(rows):
    """Per failing address of rows [addr, expected, read, ...]: count and bits."""
    rows = rows[np.argsort(rows[:, 0], kind="stable")]
    addrs, starts, counts = np.unique(rows[:, 0], return_index=True, return_counts=True)
    return [
        {"addr": int(a), "count": int(c)} | bit_faults(*rows[s : s + c, 1:3].T)
        for a, s, c in zip(addrs, starts, counts)
    ]


def transfer(mon, pages, rng):
    """Rows [addr, written, read] differing after seeded random blocks, then their
    inverse, went to each run of pages and were read back."""
    rows = [np.zeros((0, 3), np.int64)]
    for run in runs(pages):
        lo, n = int(run[0]) << 8, len(run) << 8
        data = rng.integers(0, 256, n, dtype=np.uint8)
        for block in (data, ~data):
            mon.write(lo, block.tobytes())
            back = np.frombuffer(mon.read(lo, n), np.uint8)
            d = np.flatnonzero(back != block)
            rows.append(np.column_stack((lo + d, block[d], back[d])).astype(np.int64))
    return np.concatenate(rows)


class RamTest:
    """Address, transfer and march tests of a model's RAM regions through a monitor."""

    def __init__(self, mon, model, region_map=None, backgrounds=None):
        self.mon, self.model = mon, model
        self.regions = region_map or regions(model, len(mon.code))
        self.backgrounds = list(backgrounds or BACKGROUNDS)
        self.pages = np.unique(np.concatenate(list(self.regions.values())))
        if io_mask(model).reshape(-1, 256)[self.pages].any():
            raise ValueError("a tested page decodes I/O")
        self.code_pages = -(-len(relocatable()[0]) // 256)

    def table(self, place):
        """The tested pages outside the code placed at place."""
        pages = self.pages
        return pages[(pages < place) | (pages >= place + self.code_pages)]

    def march(self, places, progress):
        """Every background's March C- from each placement: per-page counts and
        logged rows [addr, expected, read, placement, background, element]."""
        counts, logs = np.zeros(0x100, np.int64), [np.zeros((0, 6), np.int64)]
        for place in places:
            table = self.table(place)
            m = March(self.mon, place, table)
            for b, bg in enumerate(self.backgrounds):
                for i, el in enumerate(MARCH_C_MINUS):
                    per_page, log = m.element(el, bg)
                    counts[table] += per_page
                    tags = np.tile([place, b, i], (len(log), 1))
                    logs.append(np.column_stack((log, tags)))
                    progress.update()
        return counts, np.concatenate(logs)

    def run(self, repeats=1, seed=0):
        """Back up the tested RAM, test it repeats times, restore it; the report."""
        spans = [(int(r[0]) << 8, len(r) << 8) for r in runs(self.pages)]
        saved = [(a, self.mon.read(a, n)) for a, n in spans]
        rng = np.random.default_rng(seed)
        counts, logs, moved = np.zeros(0x100, np.int64), [], []
        try:
            checked, unsafe = address_check(self.mon, self.pages)
            places = placements(self.pages, self.code_pages, unsafe)
            calls = repeats * len(places) * len(self.backgrounds) * len(MARCH_C_MINUS)
            with tqdm(total=calls, desc=f"ramtest {self.model}") as progress:
                for _ in range(repeats):
                    moved.append(transfer(self.mon, self.pages, rng))
                    c, log = self.march(places, progress)
                    counts += c
                    logs.append(log)
        finally:
            for addr, data in saved:
                self.mon.write(addr, data)
        tested = np.zeros(0x100, bool)
        for p in places:
            tested[self.table(p)] = True
        result = Results(counts, np.concatenate(logs), checked, moved, tested)
        out = {
            name: result.region(pages, self.backgrounds)
            for name, pages in self.regions.items()
        }
        return {
            "model": self.model,
            "algorithm": "March C-",
            "backgrounds": self.backgrounds,
            "placements": [p << 8 for p in places],
            "unsafe_pages": [int(p) << 8 for p in unsafe],
            "repeats": repeats,
            "seed": seed,
            "ok": all(r["ok"] for r in out.values()),
            "regions": out,
        }


def _mismatches(rows, nbytes):
    """Summary of rows [addr, written, read, ...]."""
    return {
        "bytes": nbytes,
        "mismatches": len(rows),
        "first": rows[:NLOG, 0].tolist(),
    } | bit_faults(rows[:, 1], rows[:, 2])


class Results:  # pylint: disable=too-few-public-methods
    """Per-page march counts, march log, address check and transfer rows."""

    def __init__(self, counts, log, checked, moved, tested):
        self.counts, self.log, self.checked, self.tested = counts, log, checked, tested
        self.moved = np.concatenate(moved)
        self.repeats = len(moved)

    def region(self, pages, backgrounds):
        """The report of one region's pages."""

        def mine(rows):
            return rows[np.isin(rows[:, 0] >> 8, pages)]

        rows = mine(self.log)
        counts = self.counts[pages]
        tested = pages[self.tested[pages]]
        out = {
            "runs": [[int(r[0]) << 8, (int(r[-1]) + 1) << 8] for r in runs(pages)],
            "bytes": len(tested) << 8,
            "untested": [int(p) << 8 for p in pages[~self.tested[pages]]],
            "passes": self.repeats * len(backgrounds),
            "failures": int(counts.sum()),
            "failing_pages": {int(p) << 8: int(c) for p, c in zip(pages, counts) if c},
            "logged": [
                {"addr": int(a), "expected": int(e), "read": int(r)}
                | {"placement": int(p) << 8, "background": backgrounds[b]}
                | {"element": int(el)}
                for a, e, r, p, b, el in rows
            ],
            "addresses": by_address(rows),
        } | bit_faults(rows[:, 1], rows[:, 2])
        out["address_check"] = _mismatches(mine(self.checked), 2 * len(pages) << 8)
        out["transfer"] = _mismatches(
            mine(self.moved), 2 * self.repeats * len(pages) << 8
        )
        out["ok"] = not (
            out["failures"]
            or out["untested"]
            or out["address_check"]["mismatches"]
            or out["transfer"]["mismatches"]
        )
        return out


def add_arguments(ap):
    """Command line options."""
    ap.add_argument("--dev", type=int, default=8)
    ap.add_argument("--transport", choices=protocols() or ("s1",), default="s4")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--seed", type=functools.partial(int, base=0), default=0)
    ap.add_argument("--backgrounds", nargs="+", choices=tuple(BACKGROUNDS))


def execute(args, cbm):
    """Test the drive's RAM and print the report as JSON."""
    model = identify_model(cbm, args.dev)
    with Monitor(cbm, args.dev, args.transport) as mon:
        test = RamTest(mon, model, backgrounds=args.backgrounds)
        out = {"dev": args.dev, "transport": mon.protocol}
        out |= test.run(args.repeats, args.seed)
    print(json.dumps(out))
    return out


def main(argv=None, cbm=None):
    """CLI entry point."""
    return tool.standalone(sys.modules[__name__], argv, cbm)


if __name__ == "__main__":
    main()
