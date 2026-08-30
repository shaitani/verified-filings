"""Shared test fixtures."""

from __future__ import annotations

import pytest

from app.ingest import sic_index, xbrl_store


@pytest.fixture(autouse=True)
def _isolate_sic_index(tmp_path, monkeypatch):
    """Redirect ``sic_numbers.json`` to a temp path for every test, so the
    real project-root file is never read or written by the suite.

    ``sic_index`` resolves ``SIC_INDEX_FILE`` at call time, so patching the
    module attribute is enough.
    """
    monkeypatch.setattr(sic_index, "SIC_INDEX_FILE", tmp_path / "sic_numbers.json")


@pytest.fixture(autouse=True)
def _isolate_xbrl_store(tmp_path, monkeypatch):
    """Redirect the curated XBRL-data store (``data/xbrl/``) to a temp path for
    every test. ``xbrl_store`` resolves ``XBRL_STORE_DIR`` at call time."""
    monkeypatch.setattr(xbrl_store, "XBRL_STORE_DIR", tmp_path / "data" / "xbrl")
