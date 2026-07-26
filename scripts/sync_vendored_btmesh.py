#!/usr/bin/env python3
"""Re-vendor the ``btmesh`` library into the HA integration.

HACS installs ``custom_components/bluetooth_mesh/`` as-is, so the integration
ships a copy of the library under ``custom_components/bluetooth_mesh/btmesh/``.
``src/btmesh/`` is the canonical source (published to PyPI); this script keeps
the vendored copy in sync. Run it after any change to ``src/btmesh``:

    python scripts/sync_vendored_btmesh.py

It copies every ``*.py`` from ``src/btmesh`` into the vendored package and
removes vendored modules that no longer exist in the source.

``--check`` verifies the two trees are identical without writing anything and
exits non-zero if they are not — CI runs it so a forgotten sync can never ship
a stale stack to HACS users while the test suite stays green (each tree is
imported by a different half of the suite, so drift alone breaks nothing).
"""

from __future__ import annotations

import filecmp
import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "btmesh"
DST = ROOT / "custom_components" / "bluetooth_mesh" / "btmesh"


def check() -> int:
    """Report drift between the source and the vendored copy (0 = in sync)."""
    src_names = {p.name for p in SRC.glob("*.py")}
    dst_names = {p.name for p in DST.glob("*.py")}
    problems = [f"missing from the vendored copy: {n}" for n in sorted(src_names - dst_names)]
    problems += [f"stale in the vendored copy: {n}" for n in sorted(dst_names - src_names)]
    problems += [
        f"differs from src/btmesh: {n}"
        for n in sorted(src_names & dst_names)
        if not filecmp.cmp(SRC / n, DST / n, shallow=False)
    ]
    if problems:
        print("vendored btmesh is out of sync:")
        for problem in problems:
            print(f"  - {problem}")
        print("\nrun: python scripts/sync_vendored_btmesh.py")
        return 1
    print(f"vendored btmesh is in sync ({len(src_names)} modules)")
    return 0


def main() -> int:
    if not SRC.is_dir():
        print(f"source not found: {SRC}")
        return 1
    if "--check" in sys.argv:
        return check()
    DST.mkdir(parents=True, exist_ok=True)

    src_names = {p.name for p in SRC.glob("*.py")}
    for p in SRC.glob("*.py"):
        shutil.copy2(p, DST / p.name)
    # Drop vendored modules that no longer exist in src.
    for p in DST.glob("*.py"):
        if p.name not in src_names:
            p.unlink()
    print(f"synced {len(src_names)} modules -> {DST.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
