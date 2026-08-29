# Working in this repository

A Home Assistant custom integration for Bluetooth SIG Mesh lighting, plus the
pure-Python mesh stack it runs on. Read `README.md` first — it covers the
architecture, the two deliverables, and the development commands.

## Before you change anything

**`src/btmesh/` is the canonical library; `custom_components/bluetooth_mesh/btmesh/`
is a vendored copy of it.** HACS ships the `custom_components/` tree as-is, so a
change to one without the other ships a stale library to users while every test
still passes. After touching `src/btmesh/`:

```bash
python scripts/sync_vendored_btmesh.py        # --check in CI
```

## Tests

Run `pytest` with **no path argument**. `testpaths` covers the library,
`tests/ha/`, and the `phase0/` harness.

Do not judge a change by running one test file: `tests/ha/test_init.py` and
`tests/ha/test_diagnostics.py` reach for `custom_components` and the Bluetooth
manager lazily, inside the test bodies, and depend on an earlier file in the
session having imported the package and set the manager up. Alone they fail with
`ModuleNotFoundError` or `RuntimeError: BluetoothManager has not been set` —
failures that look like a broken checkout and have nothing to do with your
change. If a single file goes red, re-run the whole suite before believing it.

## Releasing

See [`docs/release-flow.md`](docs/release-flow.md). It carries four rules that
each exist because breaking one cost real rework — re-reading the remote before
tagging, keeping `Closes #N` out of merge bodies for issues awaiting a
reporter's confirmation, hardware-validating through an rc, and identifying
entities by integration rather than by name.

More than one agent session may be working in this repository at once. Assume
the remote moved since you last looked.

## Documentation habits

Comments and docstrings here explain *why*, and name the failure the code
prevents — often with the issue number. Match that: a change that removes a
workaround should say what disproved it, not just what it now does. The audit
roadmap (`docs/2026-07-26-audit-debug-and-improvements.md`) is a dated snapshot;
when an entry stops being true, correct it and say when it shipped.
