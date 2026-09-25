"""Shared fixtures.

``kanban_ui.main`` opens its store at import time, so the environment is
pointed at a throw-away directory before any test module imports it — the
suite never touches the repo's own ``tasks.db`` or ``kanban_data/``.
"""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="agent-kanban-tests-"))
os.environ["KANBAN_DB"] = str(_TMP / "api.db")
os.environ["KANBAN_RULES_FILE"] = str(_TMP / "rules.json")
os.environ["KANBAN_WEBHOOKS_FILE"] = str(_TMP / "webhooks.json")
os.environ["KANBAN_INBOX_DIR"] = str(_TMP / "inbox")
os.environ["KANBAN_LOG_DIR"] = str(_TMP / "logs")
os.environ["KANBAN_EVENTS_INTERVAL"] = "0.05"
os.environ.pop("KANBAN_ACTOR", None)
os.environ.pop("KANBAN_PROJECT_ID", None)

from kanban_store import Store  # noqa: E402


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "tasks.db")
    s.create_project("proj", "Proj")
    yield s
    s.close()


def wait_until(pred, timeout: float = 5.0, step: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(step)
    return pred()
