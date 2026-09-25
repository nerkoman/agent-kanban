"""PLAN.md → kanban import (one-shot) and the CLAUDE.md board block.

File format:

    # ProjectName — Plan

    > The kanban reads this file. Every `- [ ] ...` line under a section
    > maps to a card in that column. Do not change the heading format —
    > the engine matches them strictly.

    ## Backlog
    - [ ] TTL for internal.* tables
    - [ ] Rotate the web password

    ## In progress
    - [ ] Coder anti-loop counter

    ## Done
    - [x] Inbox watcher

This module provides:
- ``parse_plan_md(text)`` — sections → ``status -> [titles]``.
- ``init_plan_md(path, project)`` — create an empty template.
- ``update_claude_md(path, project)`` — add/refresh the kanban-board block
  (also in AGENTS.md when the project has one).
- ``import_plan_md(store, project_id, file_path)`` — create tasks in the
  kanban from the file's contents; idempotent by (project_id, title).

The import is one-shot: editing the file later does not update the board.
The generated CLAUDE.md block says so and points agents at ``kanban_create``.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from kanban_store import Store

log = logging.getLogger("kanban.plan_md")

# Heading-to-status mapping (case-insensitive, punctuation stripped).
HEADING_TO_STATUS: dict[str, str] = {
    "backlog": "backlog", "бэклог": "backlog",
    "approved": "approved", "согласовано": "approved",
    "analyst": "analyst", "analytics": "analyst", "аналитика": "analyst",
    "in progress": "in_progress", "wip": "in_progress",
    "in_progress": "in_progress", "в работе": "in_progress",
    "testing": "testing", "qa": "testing", "тестирование": "testing",
    "uat": "uat", "acceptance": "uat", "приёмка": "uat", "приемка": "uat",
    "done": "done", "closed": "done", "закрыто": "done",
    "blocked": "blocked", "заблокировано": "blocked",
    "cancelled": "cancelled", "canceled": "cancelled", "отменено": "cancelled",
}

STATUS_LABELS_RU = {
    "backlog":     "Backlog",
    "approved":    "Approved",
    "analyst":     "Analyst",
    "in_progress": "In progress",
    "testing":     "Testing",
    "uat":         "UAT",
    "done":        "Done",
    "blocked":     "Blocked",
    "cancelled":   "Cancelled",
}

_HEADING_RE = re.compile(r"^##+\s+(.+?)\s*$")
_TASK_RE = re.compile(r"^\s*-\s*\[([ xX])\]\s+(.+?)\s*$")

# Longer lines become a shortened title + the full text in the description.
MAX_TITLE = 120

# Template placeholder lines (v0.1 templates shipped one as a real task line).
_PLACEHOLDERS = {"(new tasks land here)", "(новые задачи попадают сюда)"}

# Files that are agent instructions, not plans.
NOT_PLAN_FILES = {"claude.md", "agents.md"}


def clean_title(raw: str) -> str:
    """Strip markdown emphasis/code marks and surrounding punctuation."""
    t = re.sub(r"(\*\*|__|`)", "", raw).strip()
    t = re.sub(r"^[*_\s]+|[*_\s]+$", "", t)
    return re.sub(r"\s+", " ", t)


@dataclass(frozen=True)
class ParsedTask:
    """A single `- [ ]` line from a plan file.

    ``status`` — where the task ends up: either a canonical status
    (``backlog``, ``done`` etc.) or ``backlog`` if the section does not
    map to a status. ``section_label`` is the raw section heading (with
    emoji or numbers preserved) for loose sections; ``None`` for
    canonical sections (when the heading was found in ``HEADING_TO_STATUS``).
    """
    title: str
    done: bool
    status: str
    section_label: str | None
    # The line as written (before markdown was stripped) — v0.1 imported that
    # spelling, so re-imports must recognise it.
    raw_title: str | None = field(default=None, compare=False)


def _norm_heading(s: str) -> str:
    return re.sub(r"[^\w\s]", "", s.strip().lower()).strip()


def parse_plan_md(text: str) -> list[ParsedTask]:
    """Parses PLAN.md into a list of ``ParsedTask`` (order preserved).

    Behaviour:

    * Headings that map via ``HEADING_TO_STATUS`` (``## Backlog``,
      ``## In progress``, ``## Done`` etc.) — tasks are placed into the
      corresponding column with ``section_label=None``.
    * Any other heading (``## 🔴 Tier 0``, ``## v2 stages``,
      ``## Status snapshot``) is a loose section; tasks under it land in
      ``backlog`` and the section name (as written) goes into
      ``section_label`` for later display in the description.
    * Tasks above the first ``##`` heading are ignored.
    """
    result: list[ParsedTask] = []
    current_status: str | None = None
    current_label: str | None = None
    for raw in text.splitlines():
        m_h = _HEADING_RE.match(raw)
        if m_h:
            raw_heading = m_h.group(1).strip()
            mapped = HEADING_TO_STATUS.get(_norm_heading(raw_heading))
            if mapped is not None:
                current_status = mapped
                current_label = None
            else:
                current_status = "backlog"
                current_label = clean_title(raw_heading)
            continue
        if current_status is None:
            continue
        m_t = _TASK_RE.match(raw)
        if m_t:
            done = m_t.group(1).lower() == "x"
            title = clean_title(m_t.group(2))
            if title and title.lower() not in _PLACEHOLDERS:
                result.append(ParsedTask(
                    title=title,
                    done=done,
                    status=current_status,
                    section_label=current_label,
                    raw_title=m_t.group(2).strip(),
                ))
    return result


def render_plan_template(project_name: str, project_id: str) -> str:
    """Template for a new PLAN.md."""
    return (
        f"# {project_name} — Plan\n\n"
        f"> Imported once into the kanban board when connected: "
        f"http://localhost:7777/p/{project_id}\n"
        f"> Every `- [ ] ...` line under a column heading becomes a card in "
        f"that column. Later edits here do not reach the board — create "
        f"cards in the UI or with `kanban_create`.\n"
        f"> Allowed column headings: Backlog, Approved, Analyst, "
        f"In progress, Testing, UAT, Done, Blocked, Cancelled (Russian also OK).\n\n"
        f"## Backlog\n\n"
        f"<!-- one card per line: - [ ] Title -->\n\n"
        f"## In progress\n\n"
        f"## Done\n"
    )


CLAUDE_MD_BLOCK_MARKER = "<!-- KANBAN-BOARD-BLOCK -->"

CLAUDE_MD_TEMPLATE = """\
{marker}
<!-- generated by agent-kanban; edit outside the markers — this block is rewritten -->
## Kanban board ({project_name})

Board: {board_url}/p/{project_id} · MCP server `{alias}` (tools
`mcp__{alias}__kanban_*`, configured in `.mcp.json`; project `{project_id}`).

### Agent workflow rules (strict)

When you take a task from the kanban:

1. **Before editing any files** — `kanban_move(task_id, "in_progress")`.
   The human needs to see that work has actually started, not just been
   announced in chat.
2. **As you work** — `kanban_comment(task_id, ...)` with the plan,
   blockers, decisions. These land in the task history and show up in
   the UI.
3. **When implementation is complete** — `kanban_move(task_id, "testing",
   comment="what was done and how it was verified")`. **Never move a task
   to `done` yourself** — `done` is the human's call after review (the
   MCP server refuses it by default).
4. If you get stuck — `kanban_move(task_id, "blocked", comment="what
   exactly blocks")`.
5. New work you discover — `kanban_create(title, description,
   acceptance)`; it lands in this project's Backlog. Do not keep a
   parallel task list in markdown files: the board does not read them.
6. Acting on a card you have not looked at for a while — pass
   `expected_from=<status you saw>` to `kanban_move`, so a card a human
   has meanwhile moved is not overwritten.

Skipping step 1 (silently editing files) or jumping to `done` makes
the board lie about the real state — the human can't tell what you
actually claimed vs. just described in chat.
{plan_note}{marker_end}
"""


def _plan_note(plan_files: list[str]) -> str:
    if not plan_files:
        return ""
    names = ", ".join(f"`{f}`" for f in plan_files)
    return (
        f"\n{names} {'was' if len(plan_files) == 1 else 'were'} imported into the "
        "board once, when connected. Editing it does not update the board — "
        "create cards with `kanban_create`.\n"
    )


def render_claude_block(
    project_id: str,
    project_name: str,
    *,
    alias: str = "agent-kanban",
    plan_files: list[str] | None = None,
    board_url: str = "http://localhost:7777",
) -> str:
    return CLAUDE_MD_TEMPLATE.format(
        marker=CLAUDE_MD_BLOCK_MARKER,
        marker_end=CLAUDE_MD_BLOCK_MARKER + " end",
        project_name=project_name,
        project_id=project_id,
        alias=alias,
        board_url=board_url.rstrip("/"),
        plan_note=_plan_note(plan_files or []),
    )


def _write_block(path: Path, block: str) -> None:
    if not path.exists():
        path.write_text(block, encoding="utf-8")
        return
    existing = path.read_text(encoding="utf-8")
    if CLAUDE_MD_BLOCK_MARKER in existing:
        # Replace the block between marker and marker_end
        pattern = re.compile(
            re.escape(CLAUDE_MD_BLOCK_MARKER) + r".*?"
            + re.escape(CLAUDE_MD_BLOCK_MARKER + " end") + r"\n?",
            re.DOTALL,
        )
        path.write_text(pattern.sub(lambda _m: block, existing, count=1), encoding="utf-8")
    else:
        sep = "\n" if not existing.endswith("\n") else ""
        path.write_text(existing + sep + "\n" + block, encoding="utf-8")


def update_claude_md(claude_md_path: Path, project_id: str, project_name: str,
                     plan_relative: str | None = None, *,
                     alias: str = "agent-kanban",
                     plan_files: list[str] | None = None) -> list[Path]:
    """Adds/refreshes the "Kanban board" block in CLAUDE.md (created if
    missing) and in a sibling AGENTS.md if the project has one. Returns the
    files written."""
    files = list(plan_files or ([plan_relative] if plan_relative else []))
    block = render_claude_block(project_id, project_name, alias=alias, plan_files=files)
    written = [claude_md_path]
    _write_block(claude_md_path, block)
    agents = claude_md_path.with_name("AGENTS.md")
    if agents.exists():
        _write_block(agents, block)
        written.append(agents)
    return written


def init_plan_md(path: Path, project_id: str, project_name: str,
                 *, overwrite: bool = False) -> Path:
    """Creates PLAN.md in the given directory. Returns the path to the created file."""
    plan_path = path / "PLAN.md"
    if plan_path.exists() and not overwrite:
        return plan_path
    path.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        render_plan_template(project_name, project_id), encoding="utf-8"
    )
    return plan_path


def import_plan_md(store: Store, project_id: str, plan_file: Path) -> dict[str, int]:
    """Parses PLAN.md and creates tasks in the project.

    Idempotent: a task with the same title in this project is not created
    twice — also when an earlier version imported the line with its markdown
    intact or without shortening it. If a task came from a loose section
    (``section_label`` is not None), its description gets a
    ``_From section:_ **<label>**`` prefix that is visible on the card as
    context. Returns ``{"created": N, "skipped": K}``.
    """
    text = plan_file.read_text(encoding="utf-8")
    parsed = parse_plan_md(text)
    existing_titles = {
        t.title for t in store.list_tasks(project_id=project_id, include_archived=True)
    }
    created = 0
    skipped = 0
    for task in parsed:
        title = task.title
        extra = ""
        if len(title) > MAX_TITLE:
            extra = task.title + "\n\n"
            title = title[:MAX_TITLE - 1].rstrip() + "…"
        known = {title, task.title, task.raw_title}
        if known & existing_titles:
            skipped += 1
            continue
        # If a [x] line and the status is not cancelled, mark it as done.
        real_status = (
            "done" if task.done and task.status != "cancelled" else task.status
        )
        description = (
            f"_From section:_ **{task.section_label}**\n\n"
            if task.section_label
            else ""
        ) + extra
        store.create_task(
            title=title,
            description=description,
            status=real_status,
            project_id=project_id,
            actor="plan-import",
        )
        existing_titles.add(title)
        created += 1
    log.info("plan_md import for %s: created=%d skipped=%d", project_id, created, skipped)
    return {"created": created, "skipped": skipped}
