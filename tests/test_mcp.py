"""MCP server: strict arguments, compact responses, env defaults, guard rails."""
from __future__ import annotations

import json

import pytest

from kanban_mcp import server


@pytest.fixture
def mcp_store(store, monkeypatch):
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.delenv("KANBAN_ACTOR", raising=False)
    monkeypatch.delenv("KANBAN_PROJECT_ID", raising=False)
    monkeypatch.delenv("KANBAN_MCP_HUMAN_ONLY", raising=False)
    store.create_project("other", "Other")
    return store


async def _call(name: str, **args):
    content, _structured = await server.mcp.call_tool(name, args)
    return json.loads(content[0].text)


@pytest.mark.asyncio
async def test_unknown_argument_is_an_error_with_hint(mcp_store):
    with pytest.raises(Exception) as ei:
        await _call("kanban_create", title="x", project="proj", body="the details")
    msg = str(ei.value)
    assert "'project' (did you mean 'project_id'?)" in msg
    assert "'body' (did you mean 'description'?)" in msg
    assert mcp_store.list_tasks(include_archived=True) == []


@pytest.mark.asyncio
async def test_env_actor_and_project_scope(mcp_store, monkeypatch):
    monkeypatch.setenv("KANBAN_ACTOR", "agent:dev")
    monkeypatch.setenv("KANBAN_PROJECT_ID", "proj")
    r = await _call("kanban_create", title="mine")
    assert r["ok"] and r["data"]["project_id"] == "proj"
    mcp_store.create_task("theirs", project_id="other")
    listed = await _call("kanban_list")
    assert [t["title"] for t in listed["data"]["tasks"]] == ["mine"]
    everything = await _call("kanban_list", project_id="*")
    assert everything["data"]["count"] == 2
    t_id = r["data"]["id"]
    assert mcp_store.get_task(t_id).history[0].actor == "agent:dev"
    board = await _call("kanban_board")
    assert board["ok"] and board["data"]["project"]["id"] == "proj"


@pytest.mark.asyncio
async def test_move_response_is_compact_and_normalised(mcp_store):
    t = mcp_store.create_task("x", project_id="proj")
    for i in range(50):
        mcp_store.add_comment(t.id, f"note {i}", actor="claude")
    r = await _call("kanban_move", task_id=t.id, to_status="In Progress")
    assert r["ok"] and r["data"]["status"] == "in_progress"
    assert "history" not in r["data"] and r["data"]["url"].endswith(f"/t/{t.id}")
    got = await _call("kanban_get", task_id=t.id)
    assert got["data"]["history_total"] == 52
    assert len(got["data"]["history"]) == server.DEFAULT_HISTORY_LIMIT


@pytest.mark.asyncio
async def test_agents_cannot_close_tasks_by_default(mcp_store, monkeypatch):
    t = mcp_store.create_task("x", project_id="proj", status="testing")
    r = await _call("kanban_move", task_id=t.id, to_status="done")
    assert not r["ok"] and "reserved for a human" in r["error"]
    r = await _call("kanban_create", title="y", project_id="proj", status="done")
    assert not r["ok"]
    monkeypatch.setenv("KANBAN_MCP_HUMAN_ONLY", "")
    r = await _call("kanban_move", task_id=t.id, to_status="done")
    assert r["ok"]


@pytest.mark.asyncio
async def test_expected_from_conflict(mcp_store):
    t = mcp_store.create_task("x", project_id="proj", status="in_progress")
    mcp_store.move_task(t.id, "backlog", actor="user")   # a human pulls it back
    r = await _call("kanban_move", task_id=t.id, to_status="testing",
                    expected_from="in_progress")
    assert not r["ok"] and "someone else moved it" in r["error"]
    assert mcp_store.get_task(t.id).status == "backlog"


@pytest.mark.asyncio
async def test_create_rejects_unknown_project(mcp_store):
    r = await _call("kanban_create", title="x", project_id="typo")
    assert not r["ok"] and "unknown project: typo" in r["error"]


@pytest.mark.asyncio
async def test_assign_history_and_blockers(mcp_store):
    a = mcp_store.create_task("a", project_id="proj")
    b = mcp_store.create_task("b", project_id="proj")
    r = await _call("kanban_assign", task_id=a.id, assignee="agent:reviewer")
    assert r["data"]["assignee"] == "agent:reviewer"
    r = await _call("kanban_blockers", task_id=a.id, blocker_ids=[b.id])
    assert r["data"]["blockers"] == [b.id]
    r = await _call("kanban_blockers", task_id=b.id, blocker_ids=[a.id])
    assert not r["ok"] and "cycle" in r["error"]
    page = await _call("kanban_history", task_id=a.id, limit=2)
    assert page["data"]["total"] == 3 and len(page["data"]["items"]) == 2


@pytest.mark.asyncio
async def test_pull_returns_the_card(mcp_store):
    t = mcp_store.create_task("x", project_id="proj", status="approved",
                              description="what to do")
    r = await _call("kanban_pull", task_id=t.id)
    assert r["ok"] and r["data"]["status"] == "analyst"
    assert r["data"]["description"] == "what to do" and r["data"]["assignee"] == "claude"


@pytest.mark.asyncio
async def test_tool_schemas_forbid_extra_properties():
    tools = await server.mcp.list_tools()
    assert len(tools) == 16
    assert all(t.inputSchema.get("additionalProperties") is False for t in tools)
