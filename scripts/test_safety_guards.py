"""Safety guards: non-local listen address, mock-mode banner, LLM usage log failures. Disposable DB, no network."""
import logging

import pytest
from nicegui.testing import User

from x_operator.core.scheduler import Jobs
from x_operator.db import database
from x_operator.db.database import init_db
from x_operator.llm import client as llm_client
from x_operator.main import check_listen_host
from x_operator.ui import dashboard

pytest_plugins = ["nicegui.testing.user_plugin"]
MOCK_BANNER = "模拟模式：不会真的发推"


@pytest.fixture
def pages(tmp_path):
    if getattr(database._local, "conn", None) is not None:
        database._local.conn.close()
        del database._local.conn
    init_db(tmp_path / "guards.db")
    dashboard.register(Jobs())  # The scheduler is deliberately not started.


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_local_host_starts(host):
    check_listen_host(host, allow_remote=False)


def test_remote_host_refused_unless_allowed():
    with pytest.raises(SystemExit):
        check_listen_host("0.0.0.0", allow_remote=False)
    check_listen_host("0.0.0.0", allow_remote=True)


async def test_mock_mode_shows_banner(user: User, pages, monkeypatch):
    monkeypatch.setenv("X_OPERATOR_MOCK", "1")
    await user.open("/")
    await user.should_see(MOCK_BANNER)


async def test_real_mode_has_no_banner(user: User, pages, monkeypatch):
    monkeypatch.delenv("X_OPERATOR_MOCK", raising=False)
    await user.open("/")
    await user.should_not_see(MOCK_BANNER)


def test_llm_usage_log_failure_is_reported(monkeypatch, caplog):
    def broken():
        raise RuntimeError("db unavailable")
    monkeypatch.setattr(llm_client, "get_conn", broken)
    with caplog.at_level(logging.WARNING, logger="x_operator.llm"):
        llm_client.LLMClient()._log("ping", True, {}, 0.0)
    assert "db unavailable" in caplog.text
