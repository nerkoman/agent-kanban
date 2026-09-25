"""Regression tests for defects found in review before v0.2.0 shipped."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kanban_mcp import connect as connect_mod
from kanban_mcp import server as mcp_server
from kanban_store import Store
from kanban_store import maintenance
from kanban_ui.automation import plan_md
from kanban_ui.automation import rules as rules_mod
from kanban_ui.automation.events import EventFeed
from kanban_ui.automation.rules import _load_rules, _run_once

from .test_rules import _engine, _script, _wait_for


@pytest.fixture(autouse=True)
def _fresh_runner():
    rules_mod._commands = rules_mod.CommandRunner()
    rules_mod._status["last_errors"] = []
    rules_mod._status["paused"] = False
    yield
    rules_mod._engine = None


# --- store: write transactions wait for other processes -------------------


_HAMMER = """
import sys
from kanban_store import Store
s = Store(sys.argv[1])
errors = 0
for i in range(int(sys.argv[3])):
    try:
        s.move_task(sys.argv[2], "in_progress" if i % 2 else "backlog", actor="u")
        s.update_fields(sys.argv[2], actor="u", description=f"{sys.argv[4]}-{i}")
    except Exception as e:
        errors += 1
        last = e
print(errors, repr(last) if errors else "")
"""


def test_writers_in_two_processes_do_not_fail_with_database_locked(tmp_path):
    """The web server and every MCP server are separate processes sharing
    one file. A deferred BEGIN that reads, then writes, failed instantly
    with "database is locked" when the other process had committed in
    between — the busy timeout never applied."""
    db = tmp_path / "shared.db"
    s = Store(db)
    s.create_project("p", "P")
    t = s.create_task("x", project_id="p")
    s.close()
    root = Path(__file__).resolve().parent.parent
    env = {**os.environ, "PYTHONPATH": str(root)}
    procs = [subprocess.Popen([sys.executable, "-c", _HAMMER, str(db), t.id, "250", tag],
                              stdout=subprocess.PIPE, text=True, env=env)
             for tag in ("a", "b")]
    outs = [p.communicate(timeout=120)[0].strip() for p in procs]
    assert [o.split(" ", 1)[0] for o in outs] == ["0", "0"], outs


# --- rules: keys, reactive wip_limit, restart persistence -----------------


def test_rule_key_includes_project_and_action():
    base = {"name": "Claude on approved",
            "trigger": {"type": "task_moved", "to_status": "approved"},
            "action": {"type": "run_command", "cmd": "/a.sh"}}
    other_project = {**base, "project_id": "web"}
    other_cmd = {**base, "action": {"type": "run_command", "cmd": "/b.sh"}}
    comment_only = {**base, "action": {**base["action"], "comment": "hi"}}
    keys = {rules_mod.rule_key(r) for r in (base, other_project, other_cmd)}
    assert len(keys) == 3
    assert rules_mod.rule_key(comment_only) == rules_mod.rule_key(base)


def test_wip_limit_on_reactive_rule_is_rejected(tmp_path):
    f = tmp_path / "rules.json"
    f.write_text(json.dumps({"rules": [{
        "name": "promote", "trigger": {"type": "task_moved", "to_status": "approved"},
        "action": {"type": "move_to", "status": "in_progress"},
        "wip_limit": {"statuses": ["in_progress"], "max": 1},
    }]}))
    rules, errs, _ = _load_rules(f)
    assert rules == [] and "max_concurrent" in errs[0]


@pytest.mark.asyncio
async def test_queued_and_running_jobs_survive_a_restart(store, tmp_path):
    out = tmp_path / "out.txt"
    hook = _script(tmp_path, f'echo "start $1" >> "{out}"; sleep 1.5; echo "end $1" >> "{out}"')
    rule = {"name": "agent", "trigger": {"type": "task_moved", "to_status": "approved"},
            "action": {"type": "run_command", "cmd": str(hook), "args": ["{task_id}"],
                       "max_concurrent": 1}}
    eng = _engine(store, tmp_path, [rule])
    feed = EventFeed(store)
    a = store.create_task("a", project_id="proj")
    b = store.create_task("b", project_id="proj")
    store.move_task(a.id, "approved", actor="u")
    store.move_task(b.id, "approved", actor="u")
    await feed.dispatch_pending()
    assert await _wait_for(lambda: any(j["state"] == "running" and j["pid"]
                                       for j in store.list_command_jobs()))
    assert {(j["task_id"], j["state"]) for j in store.list_command_jobs()} == {
        (a.id, "running"), (b.id, "queued")}

    # "restart": a fresh runner knows nothing until it restores from the DB
    rules_mod._commands = rules_mod.CommandRunner()
    rules_mod._commands.restore(store, eng._rules)
    st = rules_mod.rules_status()["commands"]
    assert [r["task_id"] for r in st["running"]] == [a.id]       # re-attached by pid
    assert [q["task_id"] for q in st["queued"]] == [b.id]         # still waiting its turn
    desc = rules_mod._apply_action(store, a.id, rule["action"], {"task_id": a.id}, rule=rule)
    assert "already running" in desc
    assert await _wait_for(lambda: f"end {b.id}" in (out.read_text() if out.exists() else ""),
                           timeout=10)
    assert await _wait_for(lambda: store.list_command_jobs() == [], timeout=5)


def test_polling_rule_records_a_queued_launch(store, tmp_path):
    """A queued launch counts as the rule having acted (the queue is
    persisted): otherwise every tick queued — and later ran — it again."""
    rule = {"name": "stuck", "trigger": {"type": "task_idle", "status": "approved", "minutes": 0},
            "action": {"type": "run_command", "cmd": "/bin/true", "max_concurrent": 1}}
    eng = _engine(store, tmp_path, [rule], paused=False)
    t = store.create_task("x", project_id="proj", status="approved")
    rules_mod._status["paused"] = True          # queue instead of starting
    _run_once(store, eng._rules)
    _run_once(store, eng._rules)
    assert store.rule_last_fired(rules_mod.rule_key(rule), t.id, t.moved_at) is not None
    assert [j["state"] for j in store.list_command_jobs()] == ["queued"]
    assert len(rules_mod.rules_status()["commands"]["queued"]) == 1


# --- max_runs is reset by people, not by machines ---------------------------


@pytest.mark.asyncio
async def test_unblocking_by_automation_keeps_the_run_budget(store, tmp_path):
    hook = _script(tmp_path, "exit 0")
    rule = {"name": "agent", "trigger": {"type": "task_moved", "to_status": "approved"},
            "action": {"type": "run_command", "cmd": str(hook), "max_runs": 1}}
    _engine(store, tmp_path, [rule])
    feed = EventFeed(store)
    key = rules_mod.rule_key(rule)
    t = store.create_task("x", project_id="proj")
    store.move_task(t.id, "approved", actor="u")
    await feed.dispatch_pending()
    assert await _wait_for(lambda: store.command_runs(key, t.id) == 1
                           and not rules_mod.rules_status()["commands"]["running"])
    store.move_task(t.id, "backlog", actor="u")
    store.move_task(t.id, "approved", actor="u")                  # 2nd trigger → blocked
    await feed.dispatch_pending()
    assert store.get_task(t.id).status == "blocked"
    store.move_task(t.id, "approved", actor="automation")          # a retry rule
    await feed.dispatch_pending()
    assert store.get_task(t.id).status == "blocked"               # still over budget
    store.move_task(t.id, "approved", actor="user")                # a person
    assert store.command_runs(key, t.id) == 0


# --- connect keeps what people configured -----------------------------------


def test_connect_updates_only_kanban_keys(tmp_path, store):
    proj = tmp_path / "code"
    proj.mkdir()
    (proj / ".mcp.json").write_text(json.dumps({"mcpServers": {"kb": {
        "type": "stdio", "command": "/old/python", "args": ["-m", "kanban_mcp"],
        "env": {"KANBAN_DB": "/old.db", "KANBAN_PROJECT_ID": "old",
                "KANBAN_MCP_HUMAN_ONLY": "uat,done", "KANBAN_URL": "http://kb.local",
                "KANBAN_ACTOR": "agent:dev", "EXTRA": "1"},
    }}}))
    connect_mod.connect(proj, "proj", db=store.db_path)
    env = json.loads((proj / ".mcp.json").read_text())["mcpServers"]["kb"]["env"]
    assert env["KANBAN_DB"] == str(store.db_path.resolve()) and env["KANBAN_PROJECT_ID"] == "proj"
    assert env["KANBAN_MCP_HUMAN_ONLY"] == "uat,done" and env["KANBAN_URL"] == "http://kb.local"
    assert env["KANBAN_ACTOR"] == "agent:dev" and env["EXTRA"] == "1"
    connect_mod.connect(proj, "proj", db=store.db_path, actor="claude")
    env = json.loads((proj / ".mcp.json").read_text())["mcpServers"]["kb"]["env"]
    assert env["KANBAN_ACTOR"] == "claude"


# --- KANBAN_DB pointing nowhere is an error, not a new empty board ----------


@pytest.mark.asyncio
async def test_mcp_refuses_to_create_a_database(tmp_path, monkeypatch):
    missing = tmp_path / "typo" / "tasks.db"
    monkeypatch.setenv("KANBAN_DB", str(missing))
    monkeypatch.setattr(mcp_server, "_store", None)
    content, _ = await mcp_server.mcp.call_tool("kanban_projects", {})
    out = json.loads(content[0].text)
    assert not out["ok"] and "KANBAN_DB" in out["error"]
    assert not missing.exists()
    monkeypatch.setattr(mcp_server, "_store", None)


def test_doctor_reports_mcp_json_with_another_database(store, tmp_path):
    proj = tmp_path / "code"
    proj.mkdir()
    store.update_project("proj", path=str(proj))
    (proj / ".mcp.json").write_text(json.dumps({"mcpServers": {"kb": {
        "command": "python", "args": ["-m", "kanban_mcp"], "env": {"KANBAN_DB": "/elsewhere.db"}}}}))
    ids = [p["id"] for p in maintenance.doctor(store.db_path)["problems"]]
    assert "mcp-db-mismatch" in ids


# --- plan re-import after v0.1 --------------------------------------------


def test_reimport_recognises_v01_titles(store, tmp_path):
    long = "Rework the ingestion job so that " + "it handles late data, " * 8
    store.create_task("**Fix login bug**", project_id="proj", actor="plan-import")
    store.create_task(long.strip(), project_id="proj", actor="plan-import")
    plan = tmp_path / "PLAN.md"
    plan.write_text(f"## Backlog\n- [ ] **Fix login bug**\n- [ ] {long}\n", encoding="utf-8")
    assert plan_md.import_plan_md(store, "proj", plan) == {"created": 0, "skipped": 2}


# --- dedupe-history leaves legitimate automation rows alone ----------------


def _row(store: Store, task_id: str, ts: str, actor: str, action: str, comment: str) -> None:
    store._insert_history(task_id, ts, actor, action, None, None, comment)


def test_dedupe_keeps_rows_separated_by_people_or_time(store):
    t = store.create_task("x", project_id="proj", status="blocked")
    _row(store, t.id, "2026-06-01T10:00:00+00:00", "automation", "comment", "escalated")
    _row(store, t.id, "2026-06-01T10:00:30+00:00", "user", "update", "priority: high → low")
    _row(store, t.id, "2026-06-01T10:01:00+00:00", "automation", "comment", "escalated")
    _row(store, t.id, "2026-06-02T10:00:00+00:00", "automation", "comment", "daily reminder")
    _row(store, t.id, "2026-06-03T10:00:00+00:00", "automation", "comment", "daily reminder")
    _row(store, t.id, "2026-06-03T10:01:00+00:00", "automation", "comment", "daily reminder")
    res = maintenance.dedupe_history(store.db_path, apply=True)
    assert res["repeated_rows"] == 1                     # only the 1-minute repeat
    comments = [h.comment for h in Store(store.db_path).get_task(t.id).history
                if h.actor == "automation"]
    assert comments == ["escalated", "escalated", "daily reminder", "daily reminder"]


# --- shutdown / restore details ------------------------------------------------


def _sleeper(seconds: int = 30) -> tuple[subprocess.Popen, str]:
    proc = subprocess.Popen(["sleep", str(seconds)])
    start = subprocess.run(["ps", "-o", "lstart=", "-p", str(proc.pid)],
                           capture_output=True, text=True).stdout.strip()
    return proc, start


@pytest.mark.asyncio
async def test_shutdown_keeps_agents_and_starts_nothing(store, tmp_path):
    hook = _script(tmp_path, "sleep 3")
    rule = {"name": "agent", "trigger": {"type": "task_moved", "to_status": "approved"},
            "action": {"type": "run_command", "cmd": str(hook), "max_concurrent": 1}}
    eng = _engine(store, tmp_path, [rule])
    feed = EventFeed(store)
    a = store.create_task("a", project_id="proj")
    b = store.create_task("b", project_id="proj")
    store.move_task(a.id, "approved", actor="u")
    store.move_task(b.id, "approved", actor="u")
    await feed.dispatch_pending()
    assert await _wait_for(lambda: any(j["pid"] for j in store.list_command_jobs()))
    running = rules_mod._commands._running[(rules_mod.rule_key(rule), a.id)]
    eng.stop()                                    # lifespan shutdown …
    running["_task"].cancel()                     # … then the loop cancels tasks
    await asyncio.sleep(0.2)
    jobs = {j["task_id"]: j["state"] for j in store.list_command_jobs()}
    assert jobs == {a.id: "running", b.id: "queued"}   # nothing forgotten, B not started
    assert rules_mod.rules_status()["commands"]["running"] == []

    rules_mod._commands = rules_mod.CommandRunner()      # next server
    rules_mod._commands.restore(store, eng._rules)
    st = rules_mod.rules_status()["commands"]
    assert [r["task_id"] for r in st["running"]] == [a.id]
    assert [q["task_id"] for q in st["queued"]] == [b.id]


@pytest.mark.asyncio
async def test_restore_reattaches_before_resuming_the_queue(store, tmp_path):
    rule = {"name": "agent", "trigger": {"type": "task_moved", "to_status": "approved"},
            "action": {"type": "run_command", "cmd": "/bin/true", "max_concurrent": 1}}
    key = rules_mod.rule_key(rule)
    a = store.create_task("a", project_id="proj", status="approved")
    b = store.create_task("b", project_id="proj", status="approved")
    proc, start = _sleeper()
    try:
        # queued row written first (lower rowid), running row second
        store.save_command_job(key, a.id, state="queued", rule_name="agent", ctx={"task_id": a.id})
        store.save_command_job(key, b.id, state="running", rule_name="agent",
                               ctx={"task_id": b.id}, pid=proc.pid, proc_start=start)
        rules_mod._commands.restore(store, [rule])
        st = rules_mod.rules_status()["commands"]
        assert [r["task_id"] for r in st["running"]] == [b.id]
        assert [q["task_id"] for q in st["queued"]] == [a.id]
    finally:
        proc.kill()


@pytest.mark.asyncio
async def test_restore_does_not_trust_a_reused_pid(store, tmp_path):
    rule = {"name": "agent", "trigger": {"type": "task_moved", "to_status": "approved"},
            "action": {"type": "run_command", "cmd": "/bin/true", "max_concurrent": 1}}
    t = store.create_task("a", project_id="proj", status="approved")
    proc, _start = _sleeper()
    try:
        store.save_command_job(rules_mod.rule_key(rule), t.id, state="running", rule_name="agent",
                               ctx={"task_id": t.id}, pid=proc.pid,
                               proc_start="Mon Jan  1 00:00:00 2024")
        rules_mod._commands.restore(store, [rule])
        assert rules_mod.rules_status()["commands"]["running"] == []
        assert store.list_command_jobs() == []
    finally:
        proc.kill()


@pytest.mark.asyncio
async def test_queued_jobs_of_a_missing_rule_are_kept(store, tmp_path):
    t = store.create_task("a", project_id="proj", status="approved")
    store.save_command_job("h:gone", t.id, state="queued", rule_name="old rule",
                           ctx={"task_id": t.id})
    rules_mod._commands.restore(store, [], rules_ok=False)
    assert [j["task_id"] for j in store.list_command_jobs()] == [t.id]
    assert rules_mod.rules_status()["commands"]["orphaned"][0]["rule"] == "old rule"
    # an edited rule (new key, same name) picks the job up
    edited = {"name": "old rule", "trigger": {"type": "task_moved", "to_status": "approved"},
              "action": {"type": "run_command", "cmd": "/bin/true", "max_concurrent": 1}}
    rules_mod._status["paused"] = True
    rules_mod._commands.restore(store, [edited])
    jobs = store.list_command_jobs()
    assert [(j["rule_key"], j["state"]) for j in jobs] == [(rules_mod.rule_key(edited), "queued")]
