"""Numba on-disk caches keyed on a module's package import closure.

Numba stamps a cached function with its own file, yet it compiles in callees and
constants imported from sibling modules; package functions are stamped instead with
every package module their file imports, directly or not.
"""

import ast
import functools
import hashlib
import os
import pathlib

from numba.core import caching

ROOT = pathlib.Path(__file__).resolve().parent


def _module_file(parts):
    """The package source file of a module path relative to ROOT, or None."""
    base = ROOT.joinpath(*parts)
    for f in (base.with_suffix(".py") if parts else None, base / "__init__.py"):
        if f is not None and f.is_file():
            return f
    return None


def _targets(node, pkg):
    """Module paths relative to ROOT that an import node may name."""
    if isinstance(node, ast.Import):
        names = [a.name.split(".") for a in node.names]
        return [n[1:] for n in names if n[0] == ROOT.name]
    if not isinstance(node, ast.ImportFrom):
        return []
    mod = node.module.split(".") if node.module else []
    if node.level:
        head = list(pkg[: len(pkg) - node.level + 1]) + mod
    elif mod and mod[0] == ROOT.name:
        head = mod[1:]
    else:
        return []
    return [head] + [head + [a.name] for a in node.names]


def _imports(path):
    """Package source files named by path's import statements."""
    pkg = path.relative_to(ROOT).parts[:-1]
    tree = ast.parse(path.read_bytes(), str(path))
    found = (_module_file(t) for n in ast.walk(tree) for t in _targets(n, pkg))
    return {f for f in found if f is not None}


@functools.cache
def closure(path):
    """path and every package source file it imports, transitively, sorted."""
    seen, todo = set(), [pathlib.Path(path).resolve()]
    while todo:
        f = todo.pop()
        if f not in seen:
            seen.add(f)
            todo.extend(_imports(f) - seen)
    return tuple(sorted(seen))


@functools.cache
def _digest(path, mtime_ns, size):  # pylint: disable=unused-argument
    """Content digest of path, memoized while its stat stays the same."""
    return hashlib.sha256(path.read_bytes()).digest()


def stamp(path):
    """Digest of the contents of path's import closure."""
    h = hashlib.sha256()
    for f in closure(path):
        st = os.stat(f)
        h.update(str(f.relative_to(ROOT)).encode())
        h.update(_digest(f, st.st_mtime_ns, st.st_size))
    return h.digest()


class _ClosureStamped(caching._CacheLocator):  # pylint: disable=protected-access
    """Locator mixin: package functions stamped with their import closure."""

    def get_source_stamp(self):
        """The import closure digest of the function's file."""
        return stamp(self._py_file)

    @classmethod
    def from_function(cls, py_func, py_file):
        """A locator for package functions only."""
        if not pathlib.Path(py_file).resolve().is_relative_to(ROOT):
            return None
        return super().from_function(py_func, py_file)


class UserDirLocator(_ClosureStamped, caching.UserProvidedCacheLocator):
    """NUMBA_CACHE_DIR, closure stamped."""


class InTreeLocator(_ClosureStamped, caching.InTreeCacheLocator):
    """The package's __pycache__, closure stamped."""


class UserWideLocator(_ClosureStamped, caching.UserWideCacheLocator):
    """The user-wide cache directory, closure stamped."""


LOCATORS = (UserDirLocator, InTreeLocator, UserWideLocator)


def install():
    """Put the closure-stamped locators ahead of numba's own."""
    classes = caching.CacheImpl._locator_classes  # pylint: disable=protected-access
    classes[:] = list(LOCATORS) + [c for c in classes if c not in LOCATORS]
