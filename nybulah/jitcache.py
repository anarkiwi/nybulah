"""Numba on-disk caches keyed on a module's package import closure.

Numba stamps a cached function with its own file, yet it compiles in callees and
constants imported from sibling modules; package functions are stamped instead with
every package module their file imports, directly or not. Installing waits until
numba loads its caching module, so importing the package does not import numba.
"""

import ast
import functools
import hashlib
import importlib
import importlib.abc
import importlib.machinery
import os
import pathlib
import sys

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


CACHING = "numba.core.caching"
LOCATORS = ()


def _locators(caching):
    """Closure-stamped subclasses of numba's user dir, in-tree and user-wide
    locators, for package functions only."""

    def get_source_stamp(self):
        return stamp(self._py_file)  # pylint: disable=protected-access

    def from_function(cls, py_func, py_file):
        if not pathlib.Path(py_file).resolve().is_relative_to(ROOT):
            return None
        return super(cls, cls).from_function(py_func, py_file)

    out = []
    for base in (
        caching.UserProvidedCacheLocator,
        caching.InTreeCacheLocator,
        caching.UserWideCacheLocator,
    ):
        cls = type(f"Closure{base.__name__}", (base,), {})
        cls.get_source_stamp = get_source_stamp
        cls.from_function = classmethod(from_function)
        out.append(cls)
    return tuple(out)


def install():
    """Put the closure-stamped locators ahead of numba's own (numba imported)."""
    global LOCATORS  # pylint: disable=global-statement
    caching = sys.modules.get(CACHING) or importlib.import_module(CACHING)
    if not LOCATORS:
        LOCATORS = _locators(caching)
    classes = caching.CacheImpl._locator_classes  # pylint: disable=protected-access
    classes[:] = list(LOCATORS) + [c for c in classes if c not in LOCATORS]


class _Hook(importlib.abc.MetaPathFinder):
    """Runs install() once numba's caching module has executed."""

    def find_spec(self, fullname, path, target=None):
        """numba's caching module, its loader wrapped to install afterwards."""
        if fullname != CACHING:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is not None and spec.loader is not None:
            run = spec.loader.exec_module

            def exec_module(module):
                run(module)
                install()

            spec.loader.exec_module = exec_module
        return spec


def hook():
    """install() now if numba's caching module is loaded, else when it loads."""
    if CACHING in sys.modules:
        install()
    elif not any(isinstance(f, _Hook) for f in sys.meta_path):
        sys.meta_path.insert(0, _Hook())
