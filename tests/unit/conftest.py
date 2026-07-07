"""Pytest configuration for the unit-test lane.

CI runs ``pytest -q tests/unit -m unit``. Historically the ``unit`` marker was
applied per test via ``@pytest.mark.unit``, so any file that forgot the
decorator (most router/service suites) silently dropped out of the CI lane.

Mark every test collected from this directory as ``unit`` so the CI lane always
matches the directory contents and the coverage gap cannot recur. Adding the
marker is idempotent, so files that already decorate their tests are unaffected.
"""

import os

import pytest

_UNIT_DIR = os.path.dirname(os.path.abspath(__file__))


def pytest_collection_modifyitems(config, items):
    for item in items:
        item_path = getattr(item, "fspath", None)
        if item_path is None:
            continue
        if str(item_path).startswith(_UNIT_DIR):
            item.add_marker(pytest.mark.unit)
