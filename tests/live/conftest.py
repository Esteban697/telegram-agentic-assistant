"""Live tests: real credentials, real network.

PLAN.md §9.4's table says `live` needs credentials and network, and the default run must exclude
it. Two lines make that structural rather than a per-module convention: every module collected
from this directory is `live`-marked here, so a new live test cannot forget to opt in, and any
test module outside this directory that reads `OLLAMA_API_KEY` is caught by
`tests/unit/test_phase4_guards.py`.
"""

from __future__ import annotations

import pathlib

import pytest

LIVE_DIR = pathlib.Path(__file__).resolve().parent


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if LIVE_DIR in pathlib.Path(str(item.fspath)).resolve().parents:
            item.add_marker(pytest.mark.live)
