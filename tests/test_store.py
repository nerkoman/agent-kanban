from __future__ import annotations

import sqlite3

import pytest

from kanban_store import Store


def _history(store: Store, task_id: str):
    return store.get_task(task_id).history


def test_kanban_db_env_is_honoured(tmp_path, monkeypatch):
    target = tmp_path / "nested" / "board.db"
    monkeypatch.setenv("KANBAN_DB", str(target))
    s = Store()
    try:
        assert s.db_path == target
        assert target.exists()
    finally:
        s.close()


def test_update_with_same_values_writes_no_history(store):
    t = store.create_task("A", project_id="proj", priority="high")
    before = len(_history(store, t.id))
    store.update_fields(t.id, actor="automation", priority="high")
    store.update_fields(t.id, actor="automation", title="A", size="M")
    assert len(_history(store, t.id)) == before


def test_update_records_only_changed_fields(store):
    t = store.create_task("A", project_id="proj")
    store.update_fields(t.id, actor="u", priority="high", size="M", description="new")
    last = _history(store, t.id)[-1]
    assert last.action == "update"
    assert last.comment == "priority: normal → high; description"


def test_empty_external_blocker_clears_it(store):
    t = store.create_task("A", project_id="proj", external_blocker="DBA approval")
    store.update_fields(t.id, actor="u", external_blocker="")
    assert store.get_task(t.id).external_blocker is None


def test_create_rejects_unknown_project(store):
    with pytest.raises(ValueError, match="unknown project: nope"):
        store.create_task("A", project_id="nope")


@pytest.mark.parametrize("links,msg", [
    ([{"value": "x"}], "type must be one of"),
    ([{"type": "url"}], "value must be a non-empty string"),
    ([{"type": "bogus", "value": "x"}], "type must be one of"),
    (["not-a-dict"], "must be an object"),
])
def test_create_validates_links(store, links, msg):
    with pytest.raises(ValueError, match=msg):
        store.create_task("A", project_id="proj", links=links)


def test_create_accepts_link_type_alias_and_plan_links(store):
    t = store.create_task("A", project_id="proj", links=[
        {"link_type": "plan", "value": "~/.claude/plans/x.md"},
        {"type": "url", "value": "https://example.com"},
    ])
    assert {(ln["type"], ln["value"]) for ln in store.get_task(t.id).links} == {
        ("plan", "~/.claude/plans/x.md"), ("url", "https://example.com"),
    }


def test_add_link_unknown_task_is_keyerror(store):
    with pytest.raises(KeyError):
        store.add_link("T-999", "url", "https://example.com")


def test_same_status_move_keeps_moved_at_and_history(store):
    t = store.create_task("A", project_id="proj", status="blocked")
    n = len(_history(store, t.id))
    moved = store.move_task(t.id, "blocked", actor="u", column_order=0)
    assert moved.moved_at == t.moved_at
    assert len(_history(store, t.id)) == n
    store.move_task(t.id, "blocked", actor="u", comment="still waiting")
    last = _history(store, t.id)[-1]
    assert (last.action, last.comment) == ("comment", "still waiting")


def test_drag_index_renumbers_column(store):
    ids = [store.create_task(f"T{i}", project_id="proj").id for i in range(3)]
    store.move_task(ids[2], "backlog", actor="u", column_order=0)
    order = [t.id for t in store.list_tasks(status="backlog", project_id="proj")]
    assert order == [ids[2], ids[0], ids[1]]
    other = store.create_task("X", project_id="proj", status="done")
    store.move_task(other.id, "backlog", actor="u", column_order=1)
    order = [t.id for t in store.list_tasks(status="backlog", project_id="proj")]
    assert order == [ids[2], other.id, ids[0], ids[1]]


def test_assign_is_noop_when_unchanged(store):
    t = store.create_task("A", project_id="proj")
    store.assign_task(t.id, "agent:dev", actor="pipeline")
    store.assign_task(t.id, "agent:dev", actor="pipeline")
    assigns = [h for h in _history(store, t.id) if h.action == "assign"]
    assert len(assigns) == 1
    assert store.assign_task(t.id, "", actor="u").assignee is None


def test_blockers_validation(store):
    a = store.create_task("A", project_id="proj")
    b = store.create_task("B", project_id="proj")
    with pytest.raises(ValueError, match="unknown blocker"):
        store.set_blockers(a.id, ["T-999"])
    with pytest.raises(ValueError, match="cannot block itself"):
        store.set_blockers(a.id, [a.id])
    assert store.set_blockers(a.id, [b.id, b.id]) == [b.id]
    with pytest.raises(ValueError, match="cycle"):
        store.set_blockers(b.id, [a.id])
    with pytest.raises(KeyError):
        store.set_blockers("T-999", [])


def test_history_limit_and_pages(store):
    t = store.create_task("A", project_id="proj")
    for i in range(25):
        store.add_comment(t.id, f"c{i}", actor="u")
    full = store.get_task(t.id)
    assert full.history_total == 26 and len(full.history) == 26
    short = store.get_task(t.id, history_limit=5)
    assert short.history_total == 26
    assert [h.comment for h in short.history] == ["c20", "c21", "c22", "c23", "c24"]
    page, total = store.history(t.id, limit=10)
    assert total == 26 and page[-1].comment == "c24"
    older, _ = store.history(t.id, limit=10, before_id=page[0].id)
    assert older[-1].id < page[0].id


def test_archive_hides_task_but_keeps_status(store):
    t = store.create_task("A", project_id="proj", status="done")
    store.archive_task(t.id, actor="automation")
    assert store.list_tasks(project_id="proj") == []
    archived = store.list_tasks(project_id="proj", include_archived=True)
    assert [(x.id, x.status) for x in archived] == [(t.id, "done")]
    assert store.list_projects()[0].task_counts == {}
    store.archive_task(t.id, archived=False, actor="u")
    assert [x.id for x in store.list_tasks(project_id="proj")] == [t.id]
    actions = [h.action for h in _history(store, t.id)]
    assert actions == ["create", "archive", "unarchive"]


def test_migration_from_v4_database(tmp_path):
    """A database created by v0.1.x gains archived_at and rule_firings."""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL,
            color TEXT NOT NULL DEFAULT '#F10D30', icon TEXT NOT NULL DEFAULT '',
            sort_order INTEGER NOT NULL DEFAULT 0, archived INTEGER NOT NULL DEFAULT 0,
            path TEXT, created_at TEXT NOT NULL);
        CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'backlog', priority TEXT NOT NULL DEFAULT 'normal',
            size TEXT NOT NULL DEFAULT 'M', assignee TEXT, description TEXT NOT NULL DEFAULT '',
            acceptance TEXT NOT NULL DEFAULT '', external_blocker TEXT,
            created_at TEXT NOT NULL, moved_at TEXT NOT NULL,
            column_order INTEGER NOT NULL DEFAULT 0, project_id TEXT NOT NULL DEFAULT 'default');
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta VALUES ('schema_version', '4'), ('next_id', '2');
        INSERT INTO projects VALUES ('p', 'P', '#000', 'P', 0, 0, NULL, '2026-01-01T00:00:00+00:00');
        INSERT INTO tasks (id, title, created_at, moved_at, project_id)
            VALUES ('T-001', 'old', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', 'p');
    """)
    conn.commit()
    conn.close()
    s = Store(db)
    try:
        t = s.get_task("T-001")
        assert t.archived_at is None and t.title == "old"
        assert s._conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "5"
        s.record_rule_firing("r", "T-001", t.moved_at)
        assert s.rule_last_fired("r", "T-001", t.moved_at) is not None
    finally:
        s.close()
