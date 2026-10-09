"""nybulah command line: one subcommand per tool module."""

import argparse

from . import bench, diskcmd, hwcheck, ramprobe
from .imagecmd import Convert, Info
from .survey import Survey
from .tool import run

COMMANDS = {
    "hwcheck": hwcheck,
    "bench": bench,
    "ramprobe": ramprobe,
    "read": diskcmd.READ,
    "write": diskcmd.WRITE,
}

COMMANDS.update(convert=Convert, info=Info, survey=Survey)


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
    """Console-script entry: run a command, exit status 0 unless it raises."""
    main()
    return 0


if __name__ == "__main__":
    console()
