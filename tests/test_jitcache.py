"""Numba cache stamps follow the package import closure."""

import os
import subprocess
import sys
import textwrap

from numba.core import caching

from nybulah import jitcache

RUN = """
import pathlib, sys
from nybulah import jitcache
jitcache.ROOT = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(jitcache.ROOT.parent))
from pkg import a
print(a.f())
"""


def write_pkg(root, k):
    pkg = root / "pkg"
    pkg.mkdir(exist_ok=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "a.py").write_text(textwrap.dedent("""
            from numba import njit
            from .b import K
            from pkg.c import twice

            @njit(cache=True)
            def f():
                return twice(K)
            """))
    (pkg / "c.py").write_text(
        "from numba import njit\n\n@njit(cache=True)\ndef twice(x):\n    return 2 * x\n"
    )
    (pkg / "b.py").write_text(f"K = {k}\n")
    (pkg / "d.py").write_text("UNUSED = 0\n")
    return pkg


def run(pkg):
    env = {k: v for k, v in os.environ.items() if k != "NUMBA_CACHE_DIR"}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(jitcache.ROOT.parent), env.get("PYTHONPATH")))
    )
    out = subprocess.run(
        [sys.executable, "-c", RUN, str(pkg)],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return int(out.stdout)


def test_closure_of_simfast():
    names = {f.name for f in jitcache.closure(jitcache.ROOT / "simfast.py")}
    assert {"simfast.py", "simwd.py", "simcia.py", "sim.py", "simdisk.py"} <= names
    assert "cli.py" not in names


def test_stamp_tracks_imports_only(tmp_path, monkeypatch):
    pkg = write_pkg(tmp_path, 1)
    monkeypatch.setattr(jitcache, "ROOT", pkg.resolve())
    jitcache.closure.cache_clear()
    names = {f.name for f in jitcache.closure(pkg / "a.py")}
    assert names == {"a.py", "b.py", "c.py"}
    first = jitcache.stamp(pkg / "a.py")
    (pkg / "d.py").write_text("UNUSED = 1\n")
    assert jitcache.stamp(pkg / "a.py") == first
    (pkg / "b.py").write_text("K = 22\n")
    assert jitcache.stamp(pkg / "a.py") != first
    jitcache.closure.cache_clear()


def test_cached_kernel_sees_imported_constant_change(tmp_path):
    pkg = write_pkg(tmp_path, 3)
    assert run(pkg) == 6
    assert any(p.suffix == ".nbi" for p in (pkg / "__pycache__").iterdir())
    (pkg / "b.py").write_text("K = 5\n")
    assert run(pkg) == 10


def test_installed_first_once():
    jitcache.install()
    classes = caching.CacheImpl._locator_classes  # pylint: disable=protected-access
    assert tuple(classes[: len(jitcache.LOCATORS)]) == jitcache.LOCATORS
    assert len(set(classes)) == len(classes)


def test_hook_installs_when_numba_caching_loads():
    code = (
        "import sys\nimport nybulah\n"
        "assert 'numba' not in sys.modules\n"
        "from numba.core import caching\n"
        "from nybulah import jitcache\n"
        "c = caching.CacheImpl._locator_classes\n"
        "assert tuple(c[:3]) == jitcache.LOCATORS and len(jitcache.LOCATORS) == 3\n"
        "jitcache.hook()\n"
        "assert len(set(c)) == len(c)\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
