"""REST API: validation, actors, assign/archive, board shape, event feed wiring."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kanban_store import Store
from kanban_ui import main
from kanban_ui.automation import rules as rules_mod

from .conftest import wait_until


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        if main._store.get_project("api") is None:
            main._store.create_project("api", "API")
        yield c


def _new(client, **kw):
    body = {"title": "task", "project_id": "api", **kw}
    r = client.post("/api/tasks", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def test_links_without_type_are_400_not_500(client):
    r = client.post("/api/tasks", json={"title": "x", "project_id": "api",
                                        "links": [{"value": "https://example.com"}]})
    assert r.status_code == 422
    r = client.post("/api/tasks", json={"title": "x", "project_id": "api",
                                        "links": [{"type": "bogus", "value": "v"}]})
    assert r.status_code == 400 and "type must be one of" in r.json()["detail"]


def test_unknown_fields_are_rejected(client):
    r = client.post("/api/tasks", json={"title": "x", "project_id": "api", "body": "lost?"})
    assert r.status_code == 422
    assert "body" in r.text


def test_comment_accepts_comment_alias(client):
    t = _new(client)
    r = client.post(f"/api/tasks/{t['id']}/comment", json={"comment": "via alias"})
    assert r.status_code == 201
    assert main._store.get_task(t["id"]).history[-1].comment == "via alias"


def test_actor_header(client):
    t = _new(client)
    r = client.post(f"/api/tasks/{t['id']}/move", json={"to_status": "approved"},
                    headers={"X-Kanban-Actor": "agent:launcher"})
    assert r.status_code == 200
    assert main._store.get_task(t["id"]).history[-1].actor == "agent:launcher"
    r = client.post(f"/api/tasks/{t['id']}/comment", json={"text": "x"},
                    headers={"X-Kanban-Actor": "bad\nname"})
    assert r.status_code == 400


def test_status_aliases_and_expected_from(client):
    t = _new(client)
    r = client.post(f"/api/tasks/{t['id']}/move", json={"to_status": "In progress"})
    assert r.status_code == 200 and r.json()["status"] == "in_progress"
    r = client.post(f"/api/tasks/{t['id']}/move",
                    json={"to_status": "testing", "expected_from": "approved"})
    assert r.status_code == 409
    assert main._store.get_task(t["id"]).status == "in_progress"
    r = client.post(f"/api/tasks/{t['id']}/move", json={"to_status": "nowhere"})
    assert r.status_code == 400


def test_assign_and_clear_external_blocker(client):
    t = _new(client, external_blocker="waiting for keys")
    r = client.post(f"/api/tasks/{t['id']}/assign", json={"assignee": "agent:reviewer"})
    assert r.json()["assignee"] == "agent:reviewer"
    r = client.patch(f"/api/tasks/{t['id']}", json={"external_blocker": ""})
    assert r.json()["external_blocker"] is None


def test_archive_hides_from_board(client):
    t = _new(client, status="done")
    client.post(f"/api/tasks/{t['id']}/archive", json={"archived": True})
    board = client.get("/api/board", params={"project_id": "api"}).json()
    assert t["id"] not in [x["id"] for x in board["tasks"]["done"]]
    assert board["archived_count"] >= 1
    board = client.get("/api/board", params={"project": "api", "include_archived": True}).json()
    card = [x for x in board["tasks"]["done"] if x["id"] == t["id"]][0]
    assert card["status"] == "done" and card["archived_at"]


def test_task_history_is_limited_and_paginated(client):
    t = _new(client)
    for i in range(12):
        main._store.add_comment(t["id"], f"c{i}", actor="u")
    full = client.get(f"/api/tasks/{t['id']}", params={"history_limit": 5}).json()
    assert full["history_total"] == 13 and len(full["history"]) == 5
    page = client.get(f"/api/tasks/{t['id']}/history", params={"limit": 10}).json()
    assert page["total"] == 13 and len(page["items"]) == 10 and page["next_before_id"]
    older = client.get(f"/api/tasks/{t['id']}/history",
                       params={"limit": 10, "before_id": page["next_before_id"]}).json()
    assert len(older["items"]) == 3


def test_blockers_errors(client):
    a = _new(client)
    r = client.post(f"/api/tasks/{a['id']}/blockers", json={"blocker_ids": ["T-99999"]})
    assert r.status_code == 400
    r = client.post("/api/tasks/T-99999/links", json={"type": "url", "value": "x"})
    assert r.status_code == 404


def test_deep_link_serves_the_app(client):
    assert client.get("/t/T-001").status_code == 200
    assert client.get("/p/api/t/T-001").status_code == 200


def test_mcp_style_write_reaches_webhooks_and_rules(client, tmp_path):
    """A write from another process (MCP) is dispatched by the running server."""
    out = tmp_path / "hit.txt"
    hook = tmp_path / "hook.sh"
    hook.write_text(f'#!/bin/sh\necho "$1" >> "{out}"\n')
    hook.chmod(0o755)
    rules_file = Path(os.environ["KANBAN_RULES_FILE"])
    rules_file.write_text(json.dumps({"rules": [{
        "name": "api-test-launch", "project_id": "api",
        "trigger": {"type": "task_moved", "to_status": "approved"},
        "action": {"type": "run_command", "cmd": str(hook), "args": ["{task_id}"]},
    }]}))
    try:
        t = _new(client)
        other = Store(main._store.db_path)            # like kanban_mcp
        other.move_task(t["id"], "approved", actor="claude")
        assert wait_until(out.exists, timeout=5)
        assert out.read_text().strip() == t["id"]
        status = client.get("/api/automation/status").json()
        assert status["events"]["running"] is True
        assert any(r["rule"] == "api-test-launch" for r in status["rules"]["last_reactive"])
    finally:
        rules_file.unlink()


def test_pause_endpoint(client):
    rules_file = Path(os.environ["KANBAN_RULES_FILE"])
    try:
        r = client.post("/api/automation/pause", json={"paused": True})
        assert r.json()["paused"] is True
        assert json.loads(rules_file.read_text())["paused"] is True
        r = client.post("/api/automation/pause", json={"paused": False})
        assert r.json()["paused"] is False
    finally:
        rules_file.unlink(missing_ok=True)
        rules_mod._status["paused"] = False


def test_http_mcp_hides_host_endpoints():
    assert any("pick_folder" in op for op in main._MCP_EXCLUDED)
    assert any("pause_automation" in op for op in main._MCP_EXCLUDED)
    assert not any("move_task" in op for op in main._MCP_EXCLUDED)


def test_http_mcp_callers_are_agents(client):
    t = _new(client, status="testing")
    via = {"X-Kanban-Via": main.MCP_VIA}
    r = client.post(f"/api/tasks/{t['id']}/move", json={"to_status": "done"}, headers=via)
    assert r.status_code == 403 and "reserved for a human" in r.text
    r = client.post("/api/tasks", json={"title": "x", "project_id": "api", "status": "done"},
                    headers=via)
    assert r.status_code == 403
    r = client.post(f"/api/tasks/{t['id']}/move", json={"to_status": "blocked"}, headers=via)
    assert r.status_code == 200
    assert main._store.get_task(t["id"]).history[-1].actor == "agent:mcp-http"
    # people (no marker) may still close cards
    assert client.post(f"/api/tasks/{t['id']}/move", json={"to_status": "done"}).status_code == 200


def test_http_mcp_bridge_marks_its_requests(client):
    t = _new(client, status="testing")
    h = {"Accept": "application/json, text/event-stream"}
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "t", "version": "1"}}}
    r = client.post("/mcp", json=init, headers=h)
    assert r.status_code == 200
    sid = r.headers.get("mcp-session-id")
    if sid:
        h["mcp-session-id"] = sid
    client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=h)
    tools = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                        headers=h).text
    assert "move_task_api_tasks__task_id__move_post" in tools
    assert "update_project" not in tools and "pick_folder" not in tools
    call = {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "move_task_api_tasks__task_id__move_post",
                       "arguments": {"task_id": t["id"], "to_status": "done"}}}
    out = client.post("/mcp", json=call, headers=h).text
    assert "reserved for a human" in out
    assert main._store.get_task(t["id"]).status == "testing"
