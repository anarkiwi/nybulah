"""Choose the ALIGN4 paddings of a drive source so every WD access assembles (the
WDOK rule of drive/mfm.inc) and the image links; the smallest image wins.

    python tools/align_wd.py drive/mfmstream.s [--cfg drive/mfm.cfg] [--write]
"""

import argparse
import itertools
import pathlib
import re
import subprocess
import tempfile

ALIGN = re.compile(r"^(\s*ALIGN4 )\d+\s*$", re.M)


def build(src, cfg, include, tmp):
    """Size of the linked image, or None when it does not assemble or link."""
    asm, obj, out = (tmp / n for n in ("a.s", "a.o", "a.bin"))
    asm.write_text(src)
    for cmd in (
        ["ca65", "-t", "none", "-I", str(include), "-o", str(obj), str(asm)],
        ["ld65", "-C", str(cfg), "-o", str(out), str(obj)],
    ):
        if subprocess.run(cmd, capture_output=True, check=False).returncode:
            return None
    return out.stat().st_size


def _padder(combo):
    it = iter(combo)
    return lambda m: f"{m.group(1)}{next(it)}"


def search(path, cfg):
    """(size, source) of the smallest working choice of paddings, or None."""
    src = path.read_text()
    n = len(ALIGN.findall(src))
    found = []
    with tempfile.TemporaryDirectory() as tmp:
        for combo in itertools.product(range(4), repeat=n):
            trial = ALIGN.sub(_padder(combo), src)
            size = build(trial, cfg, path.parent, pathlib.Path(tmp))
            if size is not None:
                found.append((size, trial))
    return min(found, default=None, key=lambda f: f[0])


def main(argv=None):
    """Search and report; --write keeps the result."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source", type=pathlib.Path)
    ap.add_argument("--cfg", type=pathlib.Path, default=None)
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args(argv)
    cfg = args.cfg or args.source.parent / "mfm.cfg"
    found = search(args.source.resolve(), cfg.resolve())
    if found is None:
        raise SystemExit("no padding choice assembles and links")
    size, text = found
    print(f"{args.source}: {size} bytes")
    if args.write:
        args.source.write_text(text)


if __name__ == "__main__":
    main()
