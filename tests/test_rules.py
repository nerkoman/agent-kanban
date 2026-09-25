"""Rule engine: idempotent polling rules, reactive rules via the event feed,
run_command de-duplication / concurrency / max_runs."""
from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kanban_store import Store
from kanban_ui.automation import events as events_mod
from kanban_ui.automation import rules as rules_mod
from kanban_ui.automation.events import EventFeed
from kanban_ui.automation.rules import RuleEngine, _load_rules, _run_once, set_paused


def _age(store: Store, task_id: str, days: float) -> None:
    ts = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    store._conn.execute("UPDATE tasks SET moved_at=? WHERE id=?", (ts, task_id))


def _write_rules(path: Path, rules: list[dict], **top) -> None:
    path.write_text(json.dumps({"rules": rules, **top}), encoding="utf-8")


def _engine(store: Store, tmp_path: Path, rules: list[dict], **top) -> RuleEngine:
    f = tmp_path / "rules.json"
    _write_rules(f, rules, **top)
    eng = RuleEngine(store, f, interval=3600)
    eng._maybe_reload()
    return eng


@pytest.fixture(autouse=True)
def _reset_runner():
    rules_mod._commands = rules_mod.CommandRunner()
    rules_mod._status["last_errors"] = []
    rules_mod._status["paused"] = False
    yield
    rules_mod._engine = None


BUMP = {
    "name": "Long blockers — bump priority",
    "trigger": {"type": "task_idle", "status": "blocked", "days": 7},
    "action": {"type": "set_priority", "priority": "high", "comment": "Auto-bump"},
}


def test_idle_rule_fires_once_per_stay(store, tmp_path):
    """The v0.1 bug: this rule wrote two history rows every minute forever."""
    t = store.create_task("waiting for vendor", project_id="proj", status="blocked")
    _age(store, t.id, 8)
    eng = _engine(store, tmp_path, [BUMP])
    for _ in range(5):
        _run_once(store, eng._rules)
    hist = store.get_task(t.id).history
    assert [h.action for h in hist] == ["create", "update", "comment"]
    assert store.get_task(t.id).priority == "high"


def test_idle_rule_is_noop_when_priority_already_set(store, tmp_path):
    t = store.create_task("x", project_id="proj", status="blocked", priority="high")
    _age(store, t.id, 8)
    eng = _engine(store, tmp_path, [BUMP])
    _run_once(store, eng._rules)
    _run_once(store, eng._rules)
    assert [h.action for h in store.get_task(t.id).history] == ["create"]


def test_new_stay_in_column_fires_again(store, tmp_path):
    t = store.create_task("x", project_id="proj", status="blocked")
    _age(store, t.id, 8)
    rule = {**BUMP, "action": {"type": "add_comment", "comment": "still blocked"}}
    eng = _engine(store, tmp_path, [rule])
    _run_once(store, eng._rules)
    store.move_task(t.id, "backlog", actor="u")
    store.move_task(t.id, "blocked", actor="u")
    _age(store, t.id, 9)                     # a different moved_at = a new stay
    _run_once(store, eng._rules)
    _run_once(store, eng._rules)
    comments = [h for h in store.get_task(t.id).history if h.action == "comment"]
    assert len(comments) == 2


def test_repeat_every_allows_reminders(store, tmp_path):
    t = store.create_task("x", project_id="proj", status="blocked")
    _age(store, t.id, 8)
    rule = {**BUMP, "action": {"type": "add_comment", "comment": "ping"},
            "repeat_every": {"hours": 1}}
    eng = _engine(store, tmp_path, [rule])
    _run_once(store, eng._rules)
    _run_once(store, eng._rules)            # too soon
    key = rules_mod.rule_key(rule)
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")
    store._conn.execute("UPDATE rule_firings SET fired_at=? WHERE rule_key=?", (old, key))
    _run_once(store, eng._rules)
    comments = [h for h in store.get_task(t.id).history if h.action == "comment"]
    assert len(comments) == 2


def test_idle_minutes_and_archive_action(store, tmp_path):
    done = store.create_task("shipped", project_id="proj", status="done")
    _age(store, done.id, 31)
    rule = {"name": "archive", "trigger": {"type": "task_idle", "status": "done", "days": 30},
            "action": {"type": "archive"}}
    eng = _engine(store, tmp_path, [rule])
    _run_once(store, eng._rules)
    t = store.get_task(done.id)
    assert t.status == "done" and t.archived_at is not None


def test_blockers_done_with_wip_limit(store, tmp_path):
    dep = store.create_task("dep", project_id="proj", status="done")
    a = store.create_task("a", project_id="proj")
    b = store.create_task("b", project_id="proj")
    store.set_blockers(a.id, [dep.id])
    store.set_blockers(b.id, [dep.id])
    rule = {"name": "promote", "project_id": "proj",
            "trigger": {"type": "blockers_done", "status": "backlog"},
            "action": {"type": "move_to", "status": "approved"},
            "wip_limit": {"statuses": ["approved", "analyst", "in_progress"], "max": 1}}
    eng = _engine(store, tmp_path, [rule])
    _run_once(store, eng._rules)
    assert [t.id for t in store.list_tasks(status="approved")] == [a.id]
    _run_once(store, eng._rules)             # lane full
    assert store.get_task(b.id).status == "backlog"
    store.move_task(a.id, "testing", actor="agent")
    _run_once(store, eng._rules)
    assert store.get_task(b.id).status == "approved"


def test_invalid_rule_is_skipped_not_fatal(tmp_path):
    f = tmp_path / "rules.json"
    _write_rules(f, [BUMP, {"name": "broken", "trigger": {"type": "nope"}, "action": {}}])
    rules, errs, paused = _load_rules(f)
    assert [r["name"] for r in rules] == [BUMP["name"]]
    assert any("broken" in e for e in errs)
    assert paused is False


def test_pause_flag_round_trip(tmp_path, store):
    f = tmp_path / "rules.json"
    _write_rules(f, [BUMP])
    RuleEngine(store, f)
    set_paused(f, True)
    data = json.loads(f.read_text())
    assert data["paused"] is True and data["rules"][0]["name"] == BUMP["name"]
    assert rules_mod._status["paused"] is True
    set_paused(f, False)
    assert rules_mod._status["paused"] is False


# ---------------------------------------------------------------------------
# Reactive rules through the event feed
# ---------------------------------------------------------------------------


def _script(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "hook.sh"
    p.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return p


async def _settle(feed: EventFeed, seconds: float = 0.6) -> None:
    await feed.dispatch_pending()
    await asyncio.sleep(seconds)
    await feed.dispatch_pending()


async def _wait_for(pred, timeout: float = 5.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.05)
    return pred()


@pytest.mark.asyncio
async def test_move_by_another_process_triggers_rule(store, tmp_path):
    """An MCP agent writes straight to SQLite; the rule must still fire."""
    out = tmp_path / "out.txt"
    hook = _script(tmp_path, f'echo "$1 $2" >> "{out}"')
    t = store.create_task("x", project_id="proj")
    _engine(store, tmp_path, [{
        "name": "launch", "trigger": {"type": "task_moved", "to_status": "approved",
                                      "project_id": ["other", "proj"]},
        "action": {"type": "run_command", "cmd": str(hook),
                   "args": ["{task_id}", "{from_status}"]},
    }])
    feed = EventFeed(store)
    other_process = Store(store.db_path)          # e.g. kanban_mcp
    other_process.move_task(t.id, "approved", actor="claude")
    await feed.dispatch_pending()
    assert await _wait_for(out.exists)
    assert out.read_text().split() == [t.id, "backlog"]


@pytest.mark.asyncio
async def test_created_directly_in_status_triggers_rule(store, tmp_path):
    out = tmp_path / "out.txt"
    hook = _script(tmp_path, f'echo "$1" >> "{out}"')
    _engine(store, tmp_path, [{
        "name": "launch", "trigger": {"type": "task_moved", "to_status": "approved"},
        "action": {"type": "run_command", "cmd": str(hook), "args": ["{task_id}"]},
    }])
    feed = EventFeed(store)
    t = store.create_task("filed by agent", project_id="proj", status="approved")
    await feed.dispatch_pending()
    assert await _wait_for(out.exists)
    assert out.read_text().split() == [t.id]


@pytest.mark.asyncio
async def test_webhooks_get_compact_payload(store, tmp_path, monkeypatch):
    sent = []

    async def fake_emit(event, payload):
        sent.append((event, payload))

    monkeypatch.setattr(events_mod, "emit_event", fake_emit)
    t = store.create_task("x", project_id="proj")
    feed = EventFeed(store)
    for i in range(30):
        store.add_comment(t.id, f"c{i}", actor="u")
    store.update_fields(t.id, actor="u", priority="high")
    store.move_task(t.id, "approved", actor="claude")
    await feed.dispatch_pending()
    kinds = [e for e, _ in sent]
    assert kinds.count("task_commented") == 30
    upd = [p for e, p in sent if e == "task_updated"][0]
    assert upd["changed_fields"] == ["priority"]
    moved = [p for e, p in sent if e == "task_moved"][0]
    assert (moved["from_status"], moved["to_status"], moved["actor"]) == ("backlog", "approved", "claude")
    assert len(moved["task"]["history"]) <= events_mod.HISTORY_IN_PAYLOAD
    assert moved["task"]["history_total"] == 33


@pytest.mark.asyncio
async def test_run_command_dedupes_and_queues(store, tmp_path):
    out = tmp_path / "out.txt"
    hook = _script(tmp_path, f'echo "start $1" >> "{out}"; sleep 0.4; echo "end $1" >> "{out}"')
    a = store.create_task("a", project_id="proj")
    b = store.create_task("b", project_id="proj")
    rule = {"name": "agent", "trigger": {"type": "task_moved", "to_status": "approved"},
            "action": {"type": "run_command", "cmd": str(hook), "args": ["{task_id}"],
                       "max_concurrent": 1}}
    _engine(store, tmp_path, [rule])
    feed = EventFeed(store)
    store.move_task(a.id, "approved", actor="u")
    store.move_task(b.id, "approved", actor="u")
    await feed.dispatch_pending()
    st = rules_mod.rules_status()["commands"]
    assert [r["task_id"] for r in st["running"]] == [a.id]
    assert [q["task_id"] for q in st["queued"]] == [b.id]
    # a second trigger for a running task is ignored
    store.move_task(a.id, "backlog", actor="u")
    store.move_task(a.id, "approved", actor="u")
    await feed.dispatch_pending()
    await asyncio.sleep(1.3)
    lines = out.read_text().split("\n")
    assert [ln for ln in lines if ln] == [f"start {a.id}", f"end {a.id}", f"start {b.id}", f"end {b.id}"]


@pytest.mark.asyncio
async def test_max_runs_blocks_task_and_unblock_resets(store, tmp_path):
    hook = _script(tmp_path, "exit 0")
    t = store.create_task("flaky", project_id="proj")
    rule = {"name": "agent", "trigger": {"type": "task_moved", "to_status": "approved"},
            "action": {"type": "run_command", "cmd": str(hook), "args": ["{task_id}"],
                       "max_runs": 2}}
    _engine(store, tmp_path, [rule])
    feed = EventFeed(store)
    for _ in range(3):
        store.move_task(t.id, "approved", actor="u")
        await _settle(feed, 0.3)
        if store.get_task(t.id).status == "approved":
            store.move_task(t.id, "in_progress", actor="u")
    task = store.get_task(t.id)
    assert task.status == "blocked"
    assert "max_runs=2" in task.history[-1].comment
    store.move_task(t.id, "approved", actor="u")      # a human unblocks → fresh budget
    await _settle(feed, 0.3)
    assert store.get_task(t.id).status == "approved"


@pytest.mark.asyncio
async def test_tempfail_exit_code_is_not_counted(store, tmp_path):
    hook = _script(tmp_path, "exit 75")
    t = store.create_task("x", project_id="proj")
    rule = {"name": "agent", "trigger": {"type": "task_moved", "to_status": "approved"},
            "action": {"type": "run_command", "cmd": str(hook), "args": ["{task_id}"]}}
    _engine(store, tmp_path, [rule])
    feed = EventFeed(store)
    store.move_task(t.id, "approved", actor="u")
    await _settle(feed, 0.4)
    assert store.command_runs(rules_mod.rule_key(rule), t.id) == 0


@pytest.mark.asyncio
async def test_run_command_runs_in_own_session_with_utf8(store, tmp_path, monkeypatch):
    out = tmp_path / "env.txt"
    py = sys.executable
    hook = _script(tmp_path, f'"{py}" -c "import os; print(os.getsid(0))" > "{out}.tmp"; '
                             f'echo "$LANG" >> "{out}.tmp"; mv "{out}.tmp" "{out}"')
    monkeypatch.delenv("LANG", raising=False)
    monkeypatch.delenv("LC_ALL", raising=False)
    t = store.create_task("x", project_id="proj")
    _engine(store, tmp_path, [{
        "name": "agent", "trigger": {"type": "task_moved", "to_status": "approved"},
        "action": {"type": "run_command", "cmd": str(hook), "args": []},
    }])
    feed = EventFeed(store)
    store.move_task(t.id, "approved", actor="u")
    await feed.dispatch_pending()
    assert await _wait_for(out.exists)
    sid, lang = out.read_text().split()
    assert int(sid) != os.getsid(0)
    assert lang == "en_US.UTF-8"


@pytest.mark.asyncio
async def test_paused_engine_ignores_events(store, tmp_path):
    out = tmp_path / "out.txt"
    hook = _script(tmp_path, f'echo hit >> "{out}"')
    t = store.create_task("x", project_id="proj")
    _engine(store, tmp_path, [{
        "name": "agent", "trigger": {"type": "task_moved", "to_status": "approved"},
        "action": {"type": "run_command", "cmd": str(hook), "args": []},
    }], paused=True)
    feed = EventFeed(store)
    store.move_task(t.id, "approved", actor="u")
    await _settle(feed, 0.3)
    assert not out.exists()


@pytest.mark.asyncio
async def test_reactive_loop_guard(store, tmp_path):
    t = store.create_task("x", project_id="proj")
    _engine(store, tmp_path, [
        {"name": "ping", "trigger": {"type": "task_moved", "to_status": "approved"},
         "action": {"type": "move_to", "status": "testing"}},
        {"name": "pong", "trigger": {"type": "task_moved", "to_status": "testing"},
         "action": {"type": "move_to", "status": "approved"}},
    ])
    feed = EventFeed(store)
    store.move_task(t.id, "approved", actor="u")
    for _ in range(30):
        await feed.dispatch_pending()
    moves = [h for h in store.get_task(t.id).history if h.action == "move"]
    assert len(moves) <= 2 * rules_mod.LOOP_LIMIT + 1
    assert any("rule loop" in e["error"] for e in rules_mod._status["last_errors"])


@pytest.mark.asyncio
async def test_bulk_import_does_not_trigger_rules(store, tmp_path):
    out = tmp_path / "out.txt"
    hook = _script(tmp_path, f'echo hit >> "{out}"')
    _engine(store, tmp_path, [{
        "name": "agent", "trigger": {"type": "task_moved", "to_status": "approved"},
        "action": {"type": "run_command", "cmd": str(hook), "args": []},
    }])
    feed = EventFeed(store)
    store.create_task("from PLAN.md", project_id="proj", status="approved", actor="plan-import")
    await _settle(feed, 0.3)
    assert not out.exists()
