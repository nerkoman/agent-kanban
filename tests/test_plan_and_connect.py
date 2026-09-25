"""PLAN.md import hygiene, the CLAUDE.md block, and kanban_mcp.connect."""
from __future__ import annotations

import json

import pytest

from kanban_mcp import connect as connect_mod
from kanban_ui.automation import plan_md


def test_import_skips_placeholder_and_cleans_titles(store, tmp_path):
    long = "Very long line " * 20
    plan = tmp_path / "PLAN.md"
    plan.write_text(
        "## **Фаза 0** — setup\n"
        "- [ ] (new tasks land here)\n"
        "- [ ] **Bold** `code` title\n"
        f"- [ ] {long}\n",
        encoding="utf-8",
    )
    counts = plan_md.import_plan_md(store, "proj", plan)
    assert counts == {"created": 2, "skipped": 0}
    tasks = {t.title: t for t in store.list_tasks(project_id="proj")}
    assert "Bold code title" in tasks
    short = [t for t in tasks if t.endswith("…")][0]
    assert len(short) == plan_md.MAX_TITLE
    assert long.strip() in tasks[short].description
    assert "_From section:_ **Фаза 0 — setup**" in tasks["Bold code title"].description
    assert plan_md.import_plan_md(store, "proj", plan) == {"created": 0, "skipped": 2}


def test_new_plan_template_has_no_placeholder_task(tmp_path, store):
    plan = plan_md.init_plan_md(tmp_path, "proj", "Proj")
    assert plan_md.import_plan_md(store, "proj", plan)["created"] == 0


def test_claude_block_is_mcp_first_and_refreshes_old_block(tmp_path):
    old_block = (
        "# My project\n\nkeep me\n\n<!-- KANBAN-BOARD-BLOCK -->\n## Канбан-доска\n"
        "Все новые задачи пиши в PLAN.md ... синхронизируется\n"
        "<!-- KANBAN-BOARD-BLOCK --> end\n\nafter\n"
    )
    cm = tmp_path / "CLAUDE.md"
    cm.write_text(old_block, encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("# Codex\n", encoding="utf-8")
    written = plan_md.update_claude_md(cm, "proj", "Proj", alias="team-kanban",
                                       plan_files=["PLAN.md"])
    text = cm.read_text(encoding="utf-8")
    assert text.startswith("# My project\n\nkeep me") and text.rstrip().endswith("after")
    assert "синхронизируется" not in text
    assert "mcp__team-kanban__kanban_*" in text and "kanban_create" in text
    assert "imported into the board once" in text
    assert text.count("<!-- KANBAN-BOARD-BLOCK -->") == 2   # opening + "... end"
    assert len(written) == 2 and "Kanban board (Proj)" in (tmp_path / "AGENTS.md").read_text()


def test_connect_merges_mcp_json_and_keeps_alias(tmp_path, store):
    proj_dir = tmp_path / "code"
    proj_dir.mkdir()
    (proj_dir / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "db": {"type": "stdio", "command": "db-mcp"},
        "team-kanban": {"type": "stdio", "command": "python", "args": ["-m", "kanban_mcp"]},
    }}), encoding="utf-8")
    out = connect_mod.connect(proj_dir, "proj", db=store.db_path)
    cfg = json.loads((proj_dir / ".mcp.json").read_text())
    assert out["alias"] == "team-kanban"
    assert set(cfg["mcpServers"]) == {"db", "team-kanban"}
    env = cfg["mcpServers"]["team-kanban"]["env"]
    assert env["KANBAN_PROJECT_ID"] == "proj" and env["KANBAN_DB"] == str(store.db_path.resolve())
    assert env["PYTHONPATH"] == str(connect_mod.REPO_ROOT)
    assert "mcp__team-kanban__kanban_*" in (proj_dir / "CLAUDE.md").read_text()


def test_connect_unknown_project(tmp_path, store):
    with pytest.raises(ValueError, match="unknown project"):
        connect_mod.connect(tmp_path, "nope", db=store.db_path)
