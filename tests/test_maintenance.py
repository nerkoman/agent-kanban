"""kanban_store.maintenance: history de-duplication and archive restore."""
from __future__ import annotations

from kanban_store import Store
from kanban_store import maintenance


def _spam(store: Store, task_id: str, n: int) -> None:
    for _ in range(n):
        store._insert_history(task_id, "2026-06-01T00:00:00+00:00", "automation",
                              "update", None, None, "priority")
        store._insert_history(task_id, "2026-06-01T00:00:00+00:00", "automation",
                              "comment", None, None, "Auto-bump")


def test_dedupe_keeps_first_row_per_stay(store, tmp_path):
    t = store.create_task("stuck", project_id="proj", status="blocked")
    _spam(store, t.id, 50)
    store.move_task(t.id, "backlog", actor="u")
    store.move_task(t.id, "blocked", actor="u")
    _spam(store, t.id, 3)
    store.add_comment(t.id, "Auto-bump", actor="user")          # humans are never touched
    db = store.db_path
    dry = maintenance.dedupe_history(db, apply=False)
    assert dry == {"repeated_rows": 49 * 2 + 2 * 2, "applied": False}
    res = maintenance.dedupe_history(db, apply=True, backup_dir=tmp_path / "bak")
    assert res["applied"] and (tmp_path / "bak").exists()
    fresh = Store(db)
    actions = [(h.actor, h.action) for h in fresh.get_task(t.id).history]
    assert actions.count(("automation", "update")) == 2          # one per stay
    assert ("user", "comment") in actions
    fresh.close()
    assert maintenance.dedupe_history(db, apply=False)["repeated_rows"] == 0


def test_restore_archived_turns_cancelled_back_into_done(store):
    t = store.create_task("shipped", project_id="proj", status="done")
    store.move_task(t.id, "cancelled", actor="automation",
                    comment="Auto-archive: 30 days in done")
    real = store.create_task("really cancelled", project_id="proj", status="cancelled")
    report = maintenance.doctor(store.db_path)
    assert {p["id"] for p in report["problems"]} >= {"done-as-cancelled"}
    res = maintenance.restore_archived(store.db_path, apply=True)
    assert res["tasks"] == 1 and res["applied"]
    fresh = Store(store.db_path)
    restored = fresh.get_task(t.id)
    assert restored.status == "done" and restored.archived_at is not None
    assert fresh.get_task(real.id).status == "cancelled"
    assert fresh.list_tasks(project_id="proj", status="done") == []   # hidden from board
    fresh.close()


def test_vacuum_runs(store):
    out = maintenance.vacuum(store.db_path)
    assert out["after_mb"] <= out["before_mb"] + 0.1
