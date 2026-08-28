"""Smoke test: the package imports and exposes a version."""

import pace_predict


def test_version_is_exposed() -> None:
    assert pace_predict.__version__
