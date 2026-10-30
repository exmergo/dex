import pytest

from exmergo_dex_core.budget import effective_session_ceiling


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("DEX_SESSION_CEILING_CAP", raising=False)


def test_no_ceiling_anywhere():
    assert effective_session_ceiling(None) == (None, "none")


def test_file_only():
    assert effective_session_ceiling(3600) == (3600, "config")


def test_env_cap_only(monkeypatch):
    monkeypatch.setenv("DEX_SESSION_CEILING_CAP", "100")
    assert effective_session_ceiling(None) == (100, "environment")


def test_cap_lower_than_file(monkeypatch):
    monkeypatch.setenv("DEX_SESSION_CEILING_CAP", "100")
    assert effective_session_ceiling(3600) == (100, "environment")


def test_file_lower_than_cap(monkeypatch):
    monkeypatch.setenv("DEX_SESSION_CEILING_CAP", "9999")
    assert effective_session_ceiling(3600) == (3600, "config")


def test_equal_values(monkeypatch):
    monkeypatch.setenv("DEX_SESSION_CEILING_CAP", "3600")
    assert effective_session_ceiling(3600) == (3600, "config")
