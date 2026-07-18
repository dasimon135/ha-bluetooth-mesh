"""Guard: the hardware harness must at least import and expose its entry points.

provision_and_toggle.py has no unit tests (it IS the hardware test), but a
syntax error or broken import must not land in the repo unnoticed.
"""

import importlib


def test_provision_and_toggle_imports():
    mod = importlib.import_module("provision_and_toggle")
    assert callable(mod.main)
    assert callable(mod.parse_args)


def test_parse_args_minimal():
    mod = importlib.import_module("provision_and_toggle")
    args = mod.parse_args(["--transport", "local"])
    assert args.transport == "local"
    assert args.state == "phase0_state.json"
    assert args.toggle_only is False
