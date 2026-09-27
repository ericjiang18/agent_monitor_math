"""Keep tests from writing into the real usage ledger."""

import pytest

from agent_monitor import usage_ledger


@pytest.fixture(autouse=True)
def _isolated_usage_ledger(tmp_path_factory, monkeypatch):
    monkeypatch.setattr(usage_ledger, "DB_PATH", tmp_path_factory.mktemp("ledger") / "usage.db")
    monkeypatch.setattr(usage_ledger, "_INITIALIZED", set())
    monkeypatch.setattr(usage_ledger, "_OWNER_CACHE", {})
