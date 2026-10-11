#!/usr/bin/env python3
"""Runner de tests sin dependencias.

Los tests están escritos con sintaxis de pytest (funciones ``test_*``,
``pytest.raises``, ``pytest.approx``), que es lo estándar y lo que querrás usar
en cuanto tengas pytest instalado. Este script los ejecuta igual en un Python
pelado, inyectando un módulo ``pytest`` mínimo.

    python3 tools/run_tests.py            # todo
    python3 tools/run_tests.py test_dsl   # solo un archivo
"""
from __future__ import annotations

import importlib.util
import math
import os
import sys
import traceback
import types
from contextlib import contextmanager

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


# --- módulo `pytest` mínimo -------------------------------------------------
if "pytest" not in sys.modules:
    fake = types.ModuleType("pytest")

    class _Raises:
        def __init__(self, exc):
            self.exc = exc
            self.value = None

        def __enter__(self):
            return self

        def __exit__(self, et, ev, tb):
            if et is None:
                raise AssertionError(f"se esperaba {self.exc.__name__} y no se lanzó nada")
            if not issubclass(et, self.exc):
                return False
            self.value = ev
            return True

    def raises(exc, *a, **k):
        return _Raises(exc)

    class _Approx:
        def __init__(self, v, rel=1e-6, abs_=1e-12):
            self.v = v

        def __eq__(self, other):
            try:
                if isinstance(self.v, (list, tuple)):
                    return all(math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-12)
                               for a, b in zip(self.v, other)) and len(self.v) == len(other)
                return math.isclose(float(other), float(self.v), rel_tol=1e-6, abs_tol=1e-12)
            except (TypeError, ValueError):
                return False

        def __repr__(self):
            return f"approx({self.v!r})"

    def approx(v, rel=None, abs=None):
        return _Approx(v)

    def skip(reason=""):
        raise _Skip(reason)

    class _Skip(Exception):
        pass

    fake.raises = raises
    fake.approx = approx
    fake.skip = skip
    fake._Skip = _Skip
    def parametrize(argnames, argvalues, **kw):
        """Parametrize mínimo: envuelve el test para que se ejecute por caso."""
        if isinstance(argnames, str):
            nombres = [a.strip() for a in argnames.split(",") if a.strip()]
        else:
            nombres = list(argnames)

        def deco(fn):
            def envoltura():
                for caso in argvalues:
                    vals = caso if len(nombres) > 1 else (caso,)
                    fn(**dict(zip(nombres, vals)))
            envoltura.__name__ = fn.__name__
            envoltura.__doc__ = fn.__doc__
            return envoltura
        return deco

    fake.mark = types.SimpleNamespace(
        skipif=lambda *a, **k: (lambda f: f),
        parametrize=parametrize,
    )
    sys.modules["pytest"] = fake

_Skip = sys.modules["pytest"]._Skip


def discover(names):
    tdir = os.path.join(ROOT, "tests")
    files = sorted(f for f in os.listdir(tdir) if f.startswith("test_") and f.endswith(".py"))
    if names:
        files = [f for f in files if any(n in f for n in names)]
    for f in files:
        path = os.path.join(tdir, f)
        spec = importlib.util.spec_from_file_location(f[:-3], path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f[:-3]] = mod
        try:
            spec.loader.exec_module(mod)
        except Exception:
            print(f"\n!! no se pudo importar {f}")
            traceback.print_exc()
            continue
        yield f, mod


def main(argv):
    names = argv[1:]
    total = passed = failed = skipped = 0
    failures = []
    for fname, mod in discover(names):
        tests = [(n, getattr(mod, n)) for n in sorted(dir(mod))
                 if n.startswith("test_") and callable(getattr(mod, n))]
        if not tests:
            continue
        print(f"\n{fname} " + "·" * max(1, 60 - len(fname)))
        for name, fn in tests:
            total += 1
            try:
                fn()
                passed += 1
                print(f"  \033[32m✓\033[0m {name}")
            except _Skip as e:
                skipped += 1
                print(f"  \033[33ms\033[0m {name} ({e})")
            except Exception:
                failed += 1
                print(f"  \033[31m✗\033[0m {name}")
                failures.append((fname, name, traceback.format_exc()))
    print("\n" + "=" * 64)
    print(f"{passed} pasaron, {failed} fallaron, {skipped} omitidos  (de {total})")
    if failures:
        print("\n" + "=" * 64)
        for fname, name, tb in failures:
            print(f"\n--- {fname}::{name} ---")
            print(tb)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
