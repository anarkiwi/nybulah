"""Model-aware drive RAM expansion probe over M-R/M-W.

Each candidate block gets a distinct two-byte marker at block+offset; blocks
decoding to I/O for the model are never written. Reading every marker back
after all writes exposes RAM, aliasing between blocks and onto base RAM.
"""

import functools
import json
import sys

import numpy as np
from tqdm import tqdm

from . import tool

BASE_RAM = 0x0800


# 1581 (service manual memory map): 8 KB RAM, nothing from $2000 to the CIA
# ($4000-$5FFF) and the WD177x ($6000-$7FFF); its free RAM is the DOS track cache.
RAM_1581 = 0x2000
CACHE_1581 = (0x0C00, 0x2000)


def io_mask(model):
    """Per-address True where the model decodes I/O (or undocumented space)."""
    a = np.arange(0x10000)
    if model == "1541":
        return (a < 0x8000) & (a & 0x1800 != 0)
    if model == "1571":
        return (a >= BASE_RAM) & (a < 0x6000)
    if model == "1581":
        return (a >= RAM_1581) & (a < 0x8000)
    raise ValueError(f"unknown model {model}")


def identify_model(cbm, dev):
    """1541, 1571 or 1581 from cbm_identify (type codes 0 = 1541, 1/2 = 1570/1571,
    3 = 1581; OpenCBM cbm_device_type_e)."""
    code, desc = cbm.identify(dev)
    if code == 3 or "1581" in desc:
        return "1581"
    if code in (1, 2) or "157" in desc:
        return "1571"
    if code == 0 or "1541" in desc:
        return "1541"
    raise ValueError(f"device {dev}: unsupported drive {desc!r} (type {code})")


def marker(i, avoid):
    """Two unequal bytes led by the block index, so open bus never matches."""
    for low in (0xFF ^ i, 0x55 ^ i, 0xAA ^ i, 0x0F ^ i):
        m = bytes((i, low))
        if m not in avoid:
            return m
    raise AssertionError("unreachable: four distinct candidates, at most three avoided")


def _runs(starts, flag, block):
    edges = np.flatnonzero(np.diff(np.concatenate(([0], flag.astype(np.int8), [0]))))
    return [
        [int(starts[a]), int(starts[b - 1]) + block] for a, b in edges.reshape(-1, 2)
    ]


def _exchange(cbm, dev, probes, bases):
    """Mark every probe, read probes and bases back, restore what changed."""
    addrs = probes + bases
    with tqdm(total=2 * len(addrs) + len(probes), desc=f"ramprobe #{dev}") as progress:

        def read(addr):
            progress.update()
            return cbm.download(dev, addr, 2)

        orig = [read(a) for a in addrs]
        markers = [
            marker(i, {o, *orig[len(probes) :]})
            for i, o in enumerate(orig[: len(probes)])
        ]
        for p, m in zip(probes, markers):
            cbm.upload(dev, p, m)
            progress.update()
        back = [read(a) for a in addrs]
        for a, o, v in zip(addrs, orig, back):
            if v != o:
                cbm.upload(dev, a, o)
    return markers, back


def _homes(markers, back, probes, bases, offset):
    """Per probe: block address of the storage it hit (base RAM first), or None."""
    owner = {m: i for i, m in enumerate(markers)}
    cls = [owner.get(v) for v in back]
    home = {}
    for a, c in zip(bases + probes, cls[len(probes) :] + cls[: len(probes)]):
        if c is not None:
            home.setdefault(c, a - offset)
    return [None if c is None else home[c] for c in cls[: len(probes)]]


def probe(cbm, dev, model=None, start=BASE_RAM, end=0x10000, block=0x400, offset=0x3F0):
    """Map RAM in [start, end) for dev, restoring every byte it changes. A 1581 has
    no expansion: its free run is the DOS track cache, which is not probed.

    Returns {"dev", "model", "blocks": [{"addr", "kind", "alias"}], "ram": [[lo, hi]]}
    where kind is io (never touched), ram or none, and ram lists unaliased runs.
    """
    if block & (block - 1) or block > BASE_RAM or start % block or end % block:
        raise ValueError("block must be a power of two <= $0800 aligning start and end")
    if not 0 <= offset <= block - 2:
        raise ValueError("offset must leave two bytes inside a block")
    model = model or identify_model(cbm, dev)
    if model == "1581":
        return {"dev": dev, "model": model, "blocks": [], "ram": [list(CACHE_1581)]}
    starts = np.arange(start, end, block)
    if len(starts) > 256:
        raise ValueError("at most 256 blocks")
    io = io_mask(model)[start:end].reshape(-1, block).any(axis=1)
    probes = [int(s) + offset for s in starts[~io]]
    bases = sorted({p & (BASE_RAM - 1) for p in probes})
    homes = _homes(*_exchange(cbm, dev, probes, bases), probes, bases, offset)
    return {"dev": dev, "model": model} | _report(starts, io, homes, block)


def _report(starts, io, homes, block):
    blocks = [{"addr": int(s), "kind": "io", "alias": None} for s in starts]
    unique = np.zeros(len(starts), bool)
    for k, h in zip(np.flatnonzero(~io), homes):
        entry = blocks[k]
        entry["kind"] = "none" if h is None else "ram"
        entry["alias"] = None if h in (None, entry["addr"]) else h
        unique[k] = h == entry["addr"]
    return {"blocks": blocks, "ram": _runs(starts, unique, block)}


def expansion_base(result, size):
    """Start of the first unaliased RAM run holding size bytes, else None."""
    return next((lo for lo, hi in result["ram"] if hi - lo >= size), None)


def verify(cbm, dev, addr, data, stride=0x400, sample=16):
    """M-R a sample from each stride of data at addr; list mismatching addresses."""
    bad = []
    for off in tqdm(range(0, len(data), stride), desc=f"verify #{dev}"):
        want = bytes(data[off : off + sample])
        if cbm.download(dev, addr + off, len(want)) != want:
            bad.append(addr + off)
    return {"checked": -(-len(data) // stride), "mismatched": bad}


def add_arguments(ap):
    """Command line options."""
    num = functools.partial(int, base=0)
    ap.add_argument("--dev", type=int, default=8)
    ap.add_argument("--model", choices=("1541", "1571", "1581"))
    ap.add_argument("--start", type=num, default=BASE_RAM)
    ap.add_argument("--end", type=num, default=0x10000)
    ap.add_argument("--block", type=num, default=0x400)


def execute(args, cbm):
    """Probe and print the map as JSON."""
    out = probe(cbm, args.dev, args.model, args.start, args.end, args.block)
    print(json.dumps(out))
    return out


def main(argv=None, cbm=None):
    """CLI entry point."""
    return tool.standalone(sys.modules[__name__], argv, cbm)


if __name__ == "__main__":
    main()
