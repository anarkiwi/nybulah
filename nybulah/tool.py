"""Shared runner for tool modules exposing add_arguments(ap) and execute(args, cbm)."""

import argparse
import contextlib


def run(module, args, cbm=None):
    """Execute a tool module against cbm, or a freshly opened adapter."""
    if not getattr(module, "NEEDS_ADAPTER", True):
        return module.execute(args, cbm)
    if cbm is None:
        from .opencbm import OpenCBM

        cbm = OpenCBM()
    with contextlib.closing(cbm):
        return module.execute(args, cbm)


def standalone(module, argv=None, cbm=None):
    """python -m entry point for a tool module."""
    ap = argparse.ArgumentParser(description=module.__doc__)
    module.add_arguments(ap)
    return run(module, ap.parse_args(argv), cbm)
