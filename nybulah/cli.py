"""nybulah command line: one subcommand per tool module."""

import argparse

from . import bench, bus, diskcmd, homeprobe, hwcheck, pattern, ramcheck, ramprobe
from . import ramtest, streamprobe
from .imagecmd import Convert, Flux, Info, Map
from .survey import Survey
from .tool import run

COMMANDS = {
    "bus": bus,
    "hwcheck": hwcheck,
    "bench": bench,
    "ramprobe": ramprobe,
    "ramcheck": ramcheck,
    "ramtest": ramtest,
    "pattern": pattern,
    "homeprobe": homeprobe,
    "streamprobe": streamprobe,
    "read": diskcmd.READ,
    "write": diskcmd.WRITE,
}

COMMANDS.update(convert=Convert, info=Info, map=Map, survey=Survey, flux=Flux)


def main(argv=None, cbm=None):
    """nybulah entry point."""
    ap = argparse.ArgumentParser(prog="nybulah", description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    for name, module in COMMANDS.items():
        doc = module.__doc__
        module.add_arguments(
            sub.add_parser(name, help=doc.splitlines()[0], description=doc)
        )
    args = ap.parse_args(argv)
    return run(COMMANDS[args.command], args, cbm)


def console():
    """Console-script entry: exit status 1 when a command reports ok false."""
    out = main()
    return int(isinstance(out, dict) and out.get("ok") is False)


if __name__ == "__main__":
    console()
