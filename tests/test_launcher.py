"""examples/agent-launcher/launch-claude.sh against a real server and a fake
``claude`` binary: no tokens spent, the real bash script runs end to end."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "examples" / "agent-launcher" / "launch-claude.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("curl") is None or os.name != "posix", reason="needs curl + POSIX shell"
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _req(url: str, data: dict | None = None) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(data).encode() if data is not None else None,
        headers={"Content-Type": "application/json"},
        method="POST" if data is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("launcher")
    port = _free_port()
    env = {**os.environ,
           "KANBAN_DB": str(tmp / "k.db"),
           "KANBAN_RULES_FILE": str(tmp / "rules.json"),
           "KANBAN_WEBHOOKS_FILE": str(tmp / "webhooks.json"),
           "KANBAN_INBOX_DIR": str(tmp / "inbox")}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "kanban_ui.main:app", "--port", str(port)],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            _req(url + "/api/projects")
            break
        except OSError:
            time.sleep(0.1)
    code_dir = tmp / "code"
    code_dir.mkdir()
    _req(url + "/api/projects", {"id": "demo", "name": "Demo", "path": str(code_dir)})
    yield {"url": url, "tmp": tmp, "code": code_dir}
    proc.terminate()
    proc.wait(timeout=10)


def _fake_claude(tmp: Path, body: str, logged_in: bool = True) -> Path:
    p = tmp / f"claude-{time.monotonic_ns()}"
    p.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "auth" ]; then echo \'{{"loggedIn": {str(logged_in).lower()}}}\'; exit 0; fi\n'
        + body + "\n",
        encoding="utf-8",
    )
    p.chmod(0o755)
    return p


def _run(server, task_id: str, claude: Path, **env) -> subprocess.CompletedProcess:
    full = {**os.environ, "KANBAN_URL": server["url"], "CLAUDE_BIN": str(claude),
            "KANBAN_RUNS_DIR": str(server["tmp"] / "runs"), "FAKE_TASK": task_id,
            "FAKE_URL": server["url"], **env}
    return subprocess.run(["/bin/bash", str(LAUNCHER), task_id, "demo"],
                          env=full, capture_output=True, text=True, timeout=60)


def _task(server, **kw) -> dict:
    return _req(server["url"] + "/api/tasks",
                {"title": "demo task", "project_id": "demo", "status": "approved", **kw})


def _card(server, task_id: str) -> dict:
    return _req(server["url"] + f"/api/tasks/{task_id}?history_limit=50")


AGENT_OK = (
    'curl -s -X POST "$FAKE_URL/api/tasks/$FAKE_TASK/move" -H "Content-Type: application/json" '
    '-d \'{"to_status":"testing","comment":"fake agent: done"}\' >/dev/null\n'
    'echo \'{"type":"result","is_error":false,"num_turns":3,"total_cost_usd":0.01,'
    '"session_id":"s-1","result":"ok"}\''
)


def test_happy_path_and_mcp_config(server):
    t = _task(server)
    r = _run(server, t["id"], _fake_claude(server["tmp"], AGENT_OK))
    assert r.returncode == 0, r.stderr
    card = _card(server, t["id"])
    assert card["status"] == "testing"
    last = card["history"][-1]
    assert last["actor"] == "agent:launcher" and "3 turns" in last["comment"]
    cfg = json.loads((server["tmp"] / "runs" / t["id"] / "mcp.json").read_text())
    env = cfg["mcpServers"]["agent-kanban"]["env"]
    assert env["KANBAN_PROJECT_ID"] == "demo" and env["KANBAN_DB"].endswith("k.db")


def test_agent_that_does_nothing_blocks_the_card(server):
    t = _task(server)
    r = _run(server, t["id"], _fake_claude(server["tmp"], "echo '{}'"))
    assert r.returncode == 0, r.stderr
    card = _card(server, t["id"])
    assert card["status"] == "blocked"
    assert "left the card in 'approved'" in card["history"][-1]["comment"]


def test_usage_limit_exits_75_and_keeps_card(server):
    t = _task(server)
    claude = _fake_claude(server["tmp"],
                          "echo \"Claude AI usage limit reached|1760000000\" >&2; exit 1")
    r = _run(server, t["id"], claude)
    assert r.returncode == 75
    card = _card(server, t["id"])
    assert card["status"] == "approved"
    assert "usage limit" in card["history"][-1]["comment"]


def test_not_logged_in_blocks_with_reason(server):
    t = _task(server)
    r = _run(server, t["id"], _fake_claude(server["tmp"], AGENT_OK, logged_in=False))
    assert r.returncode == 0
    card = _card(server, t["id"])
    assert card["status"] == "blocked" and "not logged in" in card["history"][-1]["comment"]


def test_timeout_blocks(server):
    t = _task(server)
    r = _run(server, t["id"], _fake_claude(server["tmp"], "sleep 30"), AGENT_TIMEOUT_SEC="1")
    assert r.returncode == 0
    card = _card(server, t["id"])
    assert card["status"] == "blocked" and "timed out" in card["history"][-1]["comment"]


def test_failing_gate_blocks(server):
    t = _task(server)
    r = _run(server, t["id"], _fake_claude(server["tmp"], AGENT_OK),
             KANBAN_GATE_CMD="echo '2 failed'; exit 1")
    assert r.returncode == 0
    card = _card(server, t["id"])
    assert card["status"] == "blocked" and "gate failed" in card["history"][-1]["comment"]


def test_card_moved_away_is_left_alone(server):
    t = _task(server, status="backlog")
    r = _run(server, t["id"], _fake_claude(server["tmp"], AGENT_OK))
    assert r.returncode == 0 and "nothing to do" in r.stderr
    assert _card(server, t["id"])["status"] == "backlog"


def test_timeout_after_the_agent_moved_on_leaves_the_card(server):
    """The agent put the card in testing but its process lingered; a human
    may already have closed the card — the timeout must not drag it back."""
    t = _task(server)
    lingering = (
        'curl -s -X POST "$FAKE_URL/api/tasks/$FAKE_TASK/move" -H "Content-Type: application/json" '
        '-d \'{"to_status":"testing"}\' >/dev/null\nsleep 30'
    )
    r = _run(server, t["id"], _fake_claude(server["tmp"], lingering), AGENT_TIMEOUT_SEC="1")
    assert r.returncode == 0
    card = _card(server, t["id"])
    assert card["status"] == "testing"
    assert "left as is" in card["history"][-1]["comment"]


def test_process_group_is_cleaned_up(server):
    """A child that ignores TERM (or a server the agent started) must not
    outlive the run."""
    t = _task(server)
    pidfile = server["tmp"] / f"child-{t['id']}.pid"
    body = (f"(trap '' TERM; sleep 60) & echo $! > \"{pidfile}\"\n" + AGENT_OK)
    r = _run(server, t["id"], _fake_claude(server["tmp"], body))
    assert r.returncode == 0, r.stderr
    child = int(pidfile.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        os.kill(child, 9)
        pytest.fail("TERM-ignoring child outlived the launcher")


def test_numbers_containing_429_are_not_a_usage_limit(server):
    t = _task(server)
    claude = _fake_claude(server["tmp"], (
        'echo \'{"type":"result","is_error":false,"num_turns":2,"total_cost_usd":0.0429,'
        '"usage":{"input_tokens":14290},"session_id":"s-429","result":"hit a snag"}\'\n'
        "echo 'rate limiter module refactored' >&2\nexit 1"
    ))
    r = _run(server, t["id"], claude)
    assert r.returncode == 0
    card = _card(server, t["id"])
    assert card["status"] == "blocked" and "left the card in 'approved'" in card["history"][-1]["comment"]


def test_session_limit_message_is_a_usage_limit(server):
    t = _task(server)
    claude = _fake_claude(server["tmp"],
                          "echo \"You've hit your session limit · resets 3pm\" >&2; exit 1")
    r = _run(server, t["id"], claude)
    assert r.returncode == 75
    assert _card(server, t["id"])["status"] == "approved"
