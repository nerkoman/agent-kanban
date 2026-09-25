"""agent-kanban — MCP server.

stdio transport. Registered in ``~/.claude.json`` (Claude Code) or
``.mcp.json`` (workspace scope) using the format:

    {
      "mcpServers": {
        "agent-kanban": {
          "type": "stdio",
          "command": "<repo>/.venv/bin/python",
          "args":    ["-m", "kanban_mcp"],
          "cwd":     "<repo>",
          "env":     {"PYTHONPATH": "<repo>", "KANBAN_DB": "<repo>/tasks.db",
                      "KANBAN_PROJECT_ID": "myproj", "KANBAN_ACTOR": "claude"}
        }
      }
    }

The same setup works for Cline (Settings → MCP Servers → Add).

Environment:

* ``KANBAN_DB``         — SQLite file shared with the web UI.
* ``KANBAN_PROJECT_ID`` — this agent's project: default for ``kanban_create``
  and the scope of list/search/board/my_active (pass ``project_id="*"`` to
  look across all projects).
* ``KANBAN_ACTOR``      — name written to history for this agent's changes
  (default ``claude``); a tool's ``actor`` argument overrides it.
* ``KANBAN_MCP_HUMAN_ONLY`` — comma-separated statuses agents may not move
  or create cards into (default ``done``: closing is the human's call after
  review). Set to an empty string to allow everything.
* ``KANBAN_URL``        — base URL of the web UI for card links
  (default ``http://localhost:7777``).

Tool arguments are strict: an unknown argument name (``project`` instead of
``project_id``, ``body`` instead of ``description``) is an error with a
suggestion, not a silently dropped value.

Tools:

* ``kanban_columns``   — column descriptions (so the agent gets oriented)
* ``kanban_projects``  — projects with task counts
* ``kanban_board``     — compact board overview of one project
* ``kanban_list``      — list tasks (filter by status / assignee / project)
* ``kanban_search``    — substring search in title/description
* ``kanban_my_active`` — the agent's tasks in analyst/in_progress/testing
* ``kanban_get``       — full card with the newest history rows
* ``kanban_history``   — older history, page by page
* ``kanban_pull``      — atomically "claim a task" (approved → analyst)
* ``kanban_move``      — move a task to a new status
* ``kanban_assign``    — set or clear the assignee
* ``kanban_comment``   — comment in history
* ``kanban_create``    — new card
* ``kanban_update``    — edit fields
* ``kanban_link``      — add a link (memory / file / pr / url / plan)
* ``kanban_blockers``  — replace internal blockers (dependencies)

Changes made here land in ``task_history``; the web server's event feed
picks them up within about a second, so automation rules and webhooks fire
for agent moves exactly as for drag-drops in the UI (the web server must be
running for that part).

On failure a human-readable message is returned; the MCP layer does not crash.
"""
from __future__ import annotations

import difflib
import logging
import os
from dataclasses import asdict
from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import ConfigDict, model_validator

from kanban_mcp.policy import check_agent_may_use as _check_agent_may_use
from kanban_store import Store, STATUSES, StatusConflict, status_meta
from kanban_store.store import DEFAULT_PROJECT_ID, LINK_TYPES, normalize_status

log = logging.getLogger("kanban.mcp")


# ---------------------------------------------------------------------------
# Singletons
# ---------------------------------------------------------------------------

_store: Store | None = None

# History rows returned by kanban_get unless asked otherwise. A long-lived
# card can have thousands of rows; dumping them all into an agent's context
# is expensive and rarely useful.
DEFAULT_HISTORY_LIMIT = 30


def _get_store() -> Store:
    global _store
    if _store is None:
        # Never create a fresh database here: a wrong KANBAN_DB must be an
        # error the agent reports, not a private empty board.
        _store = Store(create=False)
    return _store


def _actor(explicit: str | None) -> str:
    return (explicit or "").strip() or os.environ.get("KANBAN_ACTOR", "").strip() or "claude"


def _scope(project_id: str | None) -> str | None:
    """Project filter for read tools: explicit slug, "*" for all projects,
    or this agent's KANBAN_PROJECT_ID."""
    if project_id in ("*", "all"):
        return None
    return project_id or os.environ.get("KANBAN_PROJECT_ID") or None


def _url(task_id: str) -> str:
    base = os.environ.get("KANBAN_URL", "http://localhost:7777").rstrip("/")
    return f"{base}/t/{task_id}"


def _err(msg: str) -> dict[str, Any]:
    return {"ok": False, "error": msg}


def _ok(payload: Any) -> dict[str, Any]:
    return {"ok": True, "data": payload}


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

mcp = FastMCP("agent-kanban")


@mcp.tool()
def kanban_columns() -> dict[str, Any]:
    """Describe every kanban column and who moves cards in/out of it.

    Call this at the start of a session to understand the current status model.
    """
    return _ok({"columns": status_meta(), "statuses": STATUSES})


def _short_task(t: Any) -> dict[str, Any]:
    return {
        "id": t.id,
        "title": t.title,
        "status": t.status,
        "priority": t.priority,
        "size": t.size,
        "assignee": t.assignee,
        "external_blocker": t.external_blocker,
        "moved_at": t.moved_at,
        "blockers": t.blockers,
        "project_id": t.project_id,
        "archived": bool(t.archived_at),
        "url": _url(t.id),
    }


@mcp.tool()
def kanban_list(
    status: str | None = None,
    assignee: str | None = None,
    project_id: str | None = None,
    include_archived: bool = False,
) -> dict[str, Any]:
    """List tasks with optional filters.

    Args:
        status: one of backlog/approved/analyst/in_progress/testing/uat/done/blocked/cancelled,
                or None for all.
        assignee: claude / agent:<name> / user, or None for all.
        project_id: project slug (see kanban_projects); default = this agent's
                    project (KANBAN_PROJECT_ID), "*" = all projects.
        include_archived: also list archived tasks (hidden from the board).

    Returns:
        {"ok": true, "data": {"tasks": [{...short fields}], "count": N}}
    """
    try:
        if status is not None:
            status = normalize_status(status)
        tasks = _get_store().list_tasks(
            status=status, assignee=assignee, project_id=_scope(project_id),
            include_archived=include_archived,
        )
    except Exception as e:
        return _err(str(e))
    return _ok({
        "tasks": [_short_task(t) for t in tasks],
        "count": len(tasks),
    })


@mcp.tool()
def kanban_projects() -> dict[str, Any]:
    """List projects with task counts per column.

    Call this at the start of a session to learn which projects exist.

    Returns:
        {"ok": true, "data": {"projects": [
            {"id": "myproj", "name": "My project", "color": "#F10D30",
             "path": "/abs/path", "task_counts": {"backlog": 5, ...},
             "total_tasks": 33, "archived": false}
        ]}}
    """
    try:
        projects = _get_store().list_projects(include_archived=False)
    except Exception as e:
        return _err(str(e))
    return _ok({"projects": [p.to_public() for p in projects]})


@mcp.tool()
def kanban_board(project_id: str | None = None, per_column: int = 5) -> dict[str, Any]:
    """Compact overview of a project board: counts + the first tasks of each column.

    Perfect for a quick "what's in progress?" answer without enumerating
    all 100+ tasks.

    Args:
        project_id: project slug (see kanban_projects); default = KANBAN_PROJECT_ID.
        per_column: how many tasks to show per column (default 5).
    """
    project_id = _scope(project_id)
    if not project_id:
        return _err("project_id is required (or set KANBAN_PROJECT_ID)")
    try:
        proj = _get_store().get_project(project_id)
        if proj is None:
            return _err(f"project {project_id} not found")
        tasks = _get_store().list_tasks(project_id=project_id)
    except Exception as e:
        return _err(str(e))
    by_status: dict[str, list[dict[str, Any]]] = {s: [] for s in STATUSES}
    for t in tasks:
        by_status[t.status].append(_short_task(t))
    summary = {
        "project": {"id": proj.id, "name": proj.name, "path": proj.path},
        "total": len(tasks),
        "by_status": {
            s: {
                "count": len(by_status[s]),
                "first_5": by_status[s][:max(0, per_column)],
            }
            for s in STATUSES if by_status[s]
        },
    }
    return _ok(summary)


@mcp.tool()
def kanban_search(query: str, project_id: str | None = None) -> dict[str, Any]:
    """Search tasks by substring in title/description (case-insensitive).

    Args:
        query: search term; rejected if shorter than 2 characters.
        project_id: default = KANBAN_PROJECT_ID; "*" = all projects.
    """
    if not query or len(query.strip()) < 2:
        return _err("query must be at least 2 characters")
    q = query.strip().lower()
    try:
        tasks = _get_store().list_tasks(project_id=_scope(project_id))
    except Exception as e:
        return _err(str(e))
    hits = [t for t in tasks if q in t.title.lower() or q in (t.description or "").lower()]
    return _ok({
        "tasks": [_short_task(t) for t in hits],
        "count": len(hits),
        "query": query,
    })


@mcp.tool()
def kanban_my_active(
    assignee: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Active tasks for the given assignee — in analyst/in_progress/testing.

    Perfect as the first query of a session: "what am I working on right now?"

    Args:
        assignee: defaults to this agent (KANBAN_ACTOR, else "claude");
                  or agent:<name>, user, ...
        project_id: default = KANBAN_PROJECT_ID; "*" = all projects.
    """
    who = _actor(assignee)
    try:
        tasks = _get_store().list_tasks(assignee=who, project_id=_scope(project_id))
    except Exception as e:
        return _err(str(e))
    active = [t for t in tasks if t.status in ("analyst", "in_progress", "testing")]
    return _ok({
        "tasks": [_short_task(t) for t in active],
        "count": len(active),
        "assignee": who,
    })


@mcp.tool()
def kanban_get(task_id: str, history_limit: int = DEFAULT_HISTORY_LIMIT) -> dict[str, Any]:
    """Full task card: description, acceptance, links, blockers and the
    newest ``history_limit`` history rows (``history_total`` tells how many
    exist; use kanban_history for older ones)."""
    try:
        t = _get_store().get_task(task_id, history_limit=max(0, history_limit))
    except Exception as e:
        return _err(str(e))
    if not t:
        return _err(f"task {task_id} not found")
    return _ok(t.to_public())


@mcp.tool()
def kanban_history(task_id: str, limit: int = 50, before_id: int | None = None) -> dict[str, Any]:
    """One page of a task's history, oldest→newest within the page.

    Args:
        limit: rows per page (max 1000).
        before_id: pass ``next_before_id`` from the previous page to go further back.
    """
    try:
        items, total = _get_store().history(task_id, limit=limit, before_id=before_id)
    except KeyError:
        return _err(f"task {task_id} not found")
    except Exception as e:
        return _err(str(e))
    return _ok({
        "items": [asdict(h) for h in items],
        "total": total,
        "next_before_id": items[0].id if items and len(items) == max(1, min(limit, 1000)) else None,
    })


@mcp.tool()
def kanban_pull(task_id: str, assignee: str | None = None) -> dict[str, Any]:
    """Atomically "claim a task" from Approved → Analyst.

    Conditions: the task must be in status ``approved`` AND its assignee
    must be either None or equal to ``assignee`` (default: this agent,
    KANBAN_ACTOR or "claude"). If another agent has already taken it, you
    get an error — try a different task. project_id is taken from the task
    itself, so you do not need to provide it.
    """
    try:
        _get_store().pull_task(task_id, assignee=_actor(assignee))
        # The claim is when the agent starts work: hand back the full card.
        t = _get_store().get_task(task_id, history_limit=10)
    except KeyError:
        return _err(f"task {task_id} not found")
    except Exception as e:
        return _err(str(e))
    return _ok(t.to_public() if t else {"id": task_id})


@mcp.tool()
def kanban_move(
    task_id: str,
    to_status: str,
    comment: str | None = None,
    expected_from: str | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Move a task to a new status.

    Args:
        task_id: T-XXX
        to_status: backlog / approved / analyst / in_progress / testing / uat /
                   done / blocked / cancelled (see kanban_columns).
        comment: optional comment, recorded in history.
        expected_from: only move if the task is still in this status — protects
                       against acting on a card a human or another agent has
                       just moved.
        actor: defaults to KANBAN_ACTOR or "claude".
    """
    try:
        to_status = normalize_status(to_status)
    except ValueError as e:
        return _err(str(e))
    denied = _check_agent_may_use(to_status)
    if denied:
        return _err(denied)
    try:
        t = _get_store().move_task(
            task_id, to_status, actor=_actor(actor), comment=comment,
            expected_from=expected_from,
        )
    except KeyError:
        return _err(f"task {task_id} not found")
    except StatusConflict as e:
        return _err(str(e))
    except Exception as e:
        return _err(str(e))
    return _ok(_short_task(t))


@mcp.tool()
def kanban_assign(
    task_id: str,
    assignee: str | None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Set the task's assignee (e.g. "claude", "agent:reviewer", "user");
    null or "" clears it. Use this to hand a card to another agent or lane."""
    try:
        t = _get_store().assign_task(task_id, assignee, actor=_actor(actor))
    except KeyError:
        return _err(f"task {task_id} not found")
    except Exception as e:
        return _err(str(e))
    return _ok(_short_task(t))


@mcp.tool()
def kanban_comment(
    task_id: str,
    text: str,
    actor: str | None = None,
) -> dict[str, Any]:
    """Add a comment to the task's history.

    Useful for recording the plan once the task lands in Analyst, or for
    capturing a test result.
    """
    try:
        _get_store().add_comment(task_id, text, actor=_actor(actor))
    except KeyError:
        return _err(f"task {task_id} not found")
    except Exception as e:
        return _err(str(e))
    return _ok({"task_id": task_id, "comment_added": True})


@mcp.tool()
def kanban_create(
    title: str,
    description: str = "",
    acceptance: str = "",
    status: str = "backlog",
    priority: str = "normal",
    size: str = "M",
    external_blocker: str | None = None,
    actor: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Create a new card. Defaults to Backlog; for immediate work pass status='in_progress'.

    Args:
        title: a short single-line title.
        description: markdown with the details.
        acceptance: acceptance criteria (what counts as "done").
        status: backlog (default) / approved / in_progress / ... (see kanban_columns).
        priority: critical / high / normal / low.
        size: S (<30 min) / M (<2 h) / L (>2 h) / XL.
        project_id: project slug; None = default (KANBAN_PROJECT_ID env,
                    else KANBAN_DEFAULT_PROJECT_ID). An unknown slug is an error.
    """
    try:
        status = normalize_status(status)
    except ValueError as e:
        return _err(str(e))
    denied = _check_agent_may_use(status)
    if denied:
        return _err(denied)
    pid = project_id or os.environ.get("KANBAN_PROJECT_ID") or DEFAULT_PROJECT_ID
    try:
        t = _get_store().create_task(
            title=title,
            description=description,
            acceptance=acceptance,
            status=status,
            priority=priority,
            size=size,
            external_blocker=external_blocker,
            actor=_actor(actor),
            project_id=pid,
        )
    except Exception as e:
        return _err(str(e))
    return _ok(_short_task(t))


@mcp.tool()
def kanban_link(task_id: str, link_type: str, value: str) -> dict[str, Any]:
    """Attach a link to a task.

    Args:
        link_type: memory | file | pr | url | plan
        value: file name or URL.
    """
    if link_type not in LINK_TYPES:
        return _err(f"unknown link_type: {link_type}; valid: {list(LINK_TYPES)}")
    try:
        added = _get_store().add_link(task_id, link_type, value)
    except KeyError:
        return _err(f"task {task_id} not found")
    except Exception as e:
        return _err(str(e))
    return _ok({"task_id": task_id, "link": {"type": link_type, "value": value}, "added": added})


@mcp.tool()
def kanban_blockers(
    task_id: str, blocker_ids: list[str], actor: str | None = None
) -> dict[str, Any]:
    """Replace the list of internal blockers (dependencies): ``task_id``
    waits for every task in ``blocker_ids``. Unknown ids and cycles are
    rejected. Pass [] to clear."""
    try:
        stored = _get_store().set_blockers(task_id, blocker_ids, actor=_actor(actor))
    except KeyError:
        return _err(f"task {task_id} not found")
    except Exception as e:
        return _err(str(e))
    return _ok({"task_id": task_id, "blockers": stored})


@mcp.tool()
def kanban_update(
    task_id: str,
    title: str | None = None,
    description: str | None = None,
    acceptance: str | None = None,
    priority: str | None = None,
    size: str | None = None,
    external_blocker: str | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Update card fields (status: kanban_move; assignee: kanban_assign).

    Only the fields you pass are changed; external_blocker="" clears it.
    """
    try:
        t = _get_store().update_fields(
            task_id,
            actor=_actor(actor),
            title=title,
            description=description,
            acceptance=acceptance,
            priority=priority,
            size=size,
            external_blocker=external_blocker,
        )
    except KeyError:
        return _err(f"task {task_id} not found")
    except Exception as e:
        return _err(str(e))
    return _ok(_short_task(t))


# ---------------------------------------------------------------------------
# Strict arguments
# ---------------------------------------------------------------------------

# Wrong names agents actually used, → the parameter they meant.
_ARG_HINTS: dict[str, tuple[str, ...]] = {
    "project": ("project_id",),
    "column": ("to_status", "status"),
    "status": ("to_status",),
    "body": ("description", "text"),
    "content": ("text", "description"),
    "message": ("text", "comment"),
    "comment": ("text",),
    "id": ("task_id",),
    "task": ("task_id",),
    "type": ("link_type",),
    "blockers": ("blocker_ids",),
    "limit": ("history_limit",),
}


def _strict_arg_model(model: Any, tool_name: str) -> Any:
    allowed = set(model.model_fields)

    def _reject_unknown(cls: Any, data: Any) -> Any:
        if isinstance(data, dict):
            unknown = [k for k in data if k not in allowed]
            if unknown:
                parts = []
                for k in unknown:
                    guess = next((h for h in _ARG_HINTS.get(k, ()) if h in allowed), None)
                    if guess is None:
                        close = difflib.get_close_matches(k, sorted(allowed), n=1)
                        guess = close[0] if close else None
                    parts.append(f"'{k}'" + (f" (did you mean '{guess}'?)" if guess else ""))
                raise ValueError(
                    f"unknown argument {', '.join(parts)}; {tool_name} accepts: "
                    + ", ".join(sorted(allowed))
                )
        return data

    namespace = {
        "__module__": model.__module__,
        "model_config": ConfigDict(arbitrary_types_allowed=True, extra="forbid"),
        "_reject_unknown": model_validator(mode="before")(classmethod(_reject_unknown)),
    }
    return type(model.__name__, (model,), namespace)


def _make_arguments_strict(server: FastMCP) -> None:
    """Unknown argument names are rejected instead of silently ignored.

    FastMCP's argument models ignore extra keys by default, so an agent that
    wrote ``project=`` / ``column=`` / ``body=`` created empty cards in the
    default project without noticing. Uses FastMCP internals; if they change,
    the server keeps working without the check.
    """
    try:
        for tool in server._tool_manager.list_tools():
            meta = tool.fn_metadata
            meta.arg_model = _strict_arg_model(meta.arg_model, tool.name)
            tool.parameters["additionalProperties"] = False
    except Exception:  # noqa: BLE001
        log.warning("could not enable strict tool arguments", exc_info=True)


_make_arguments_strict(mcp)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
