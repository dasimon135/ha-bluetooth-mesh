#!/usr/bin/env python3
"""Re-vendor the ``btmesh`` library into the HA integration.

HACS installs ``custom_components/bluetooth_mesh/`` as-is, so the integration
ships a copy of the library under ``custom_components/bluetooth_mesh/btmesh/``.
``src/btmesh/`` is the canonical source (published to PyPI); this script keeps
the vendored copy in sync. Run it after any change to ``src/btmesh``:

    python scripts/sync_vendored_btmesh.py

It copies every ``*.py`` from ``src/btmesh`` into the vendored package and
removes vendored modules that no longer exist in the source.
"""

from __future__ import annotations

import pathlib
import shutil

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "btmesh"
DST = ROOT / "custom_components" / "bluetooth_mesh" / "btmesh"


def main() -> int:
    if not SRC.is_dir():
        print(f"source not found: {SRC}")
        return 1
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
