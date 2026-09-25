"""Kanban — SQLite store.

Single source of truth: ``tasks.db`` next to the repo root (gitignored), or
the path from env ``KANBAN_DB``.

Public API:
    Store.list_tasks(status=..., assignee=..., project_id=..., include_archived=False)
    Store.get_task(task_id, history_limit=None)
    Store.history(task_id, limit=100, before_id=None)
    Store.create_task(title, status="backlog", ...)
    Store.move_task(task_id, to_status, actor, comment=None)
    Store.assign_task(task_id, assignee, actor)
    Store.archive_task(task_id, archived=True, actor)
    Store.add_comment(task_id, text, actor)
    Store.add_link(task_id, type, value)
    Store.set_blockers(task_id, blocker_ids)
    Store.snapshot()  -> dict (for JSON snapshots)

Every mutation that changes something writes one row to ``task_history``.
Calls that would not change anything (same priority, same assignee, move
into the column the task is already in) write nothing — the history is the
event log that automation and webhooks consume, so no-op rows are noise.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading

DEFAULT_PROJECT_ID = os.environ.get("KANBAN_DEFAULT_PROJECT_ID", "default")
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# ============================================================================
# Status model
# ============================================================================

# 9 columns in left-to-right UI order.
STATUSES: list[str] = [
    "backlog",
    "approved",
    "analyst",
    "in_progress",
    "testing",
    "uat",
    "done",
    "blocked",
    "cancelled",
]

# Link types accepted by add_link / create_task(links=...).
# ``plan`` — a planning document (e.g. a Claude Code plan-mode file).
LINK_TYPES: tuple[str, ...] = ("memory", "file", "pr", "url", "plan")

# Statuses in which a blocker counts as resolved.
RESOLVED_STATUSES: tuple[str, ...] = ("done", "cancelled")

PRIORITIES: tuple[str, ...] = ("critical", "high", "normal", "low")
SIZES: tuple[str, ...] = ("S", "M", "L", "XL")

# Spellings agents and people actually use for columns → canonical id.
_STATUS_ALIASES: dict[str, str] = {
    "todo": "backlog", "бэклог": "backlog",
    "согласовано": "approved",
    "analysis": "analyst", "analytics": "analyst", "аналитика": "analyst",
    "inprogress": "in_progress", "wip": "in_progress", "doing": "in_progress",
    "в_работе": "in_progress",
    "review": "testing", "qa": "testing", "тестирование": "testing",
    "acceptance": "uat", "приёмка": "uat", "приемка": "uat",
    "closed": "done", "закрыто": "done",
    "заблокировано": "blocked",
    "canceled": "cancelled", "отменено": "cancelled",
}


def normalize_status(value: str) -> str:
    """``"In progress"``, ``"in-progress"``, ``"Testing"``, ``"В работе"`` → canonical id.

    Raises ``ValueError`` listing the valid ids for anything unrecognised.
    """
    raw = str(value if value is not None else "").strip()
    key = raw.lower().replace("-", "_").replace(" ", "_")
    if key in STATUSES:
        return key
    if key in _STATUS_ALIASES:
        return _STATUS_ALIASES[key]
    if key.replace("_", "") in _STATUS_ALIASES:
        return _STATUS_ALIASES[key.replace("_", "")]
    raise ValueError(f"unknown status: {value!r}; valid: {', '.join(STATUSES)}")


def normalize_priority(value: str) -> str:
    key = str(value if value is not None else "").strip().lower()
    if key == "medium":
        return "normal"
    if key in PRIORITIES:
        return key
    raise ValueError(f"unknown priority: {value!r}; valid: {', '.join(PRIORITIES)}")


def normalize_size(value: str) -> str:
    key = str(value if value is not None else "").strip().upper()
    if key in SIZES:
        return key
    raise ValueError(f"unknown size: {value!r}; valid: {', '.join(SIZES)}")


def status_meta() -> list[dict[str, str]]:
    """Column metadata for the UI (label + cssClass)."""
    return [
        {"id": "backlog",     "title": "Backlog",      "owner": "user"},
        {"id": "approved",    "title": "Approved",     "owner": "agent"},
        {"id": "analyst",     "title": "Analyst",      "owner": "agent"},
        {"id": "in_progress", "title": "In progress",  "owner": "agent"},
        {"id": "testing",     "title": "Testing",      "owner": "agent"},
        {"id": "uat",         "title": "UAT",          "owner": "user"},
        {"id": "done",        "title": "Done",         "owner": "user"},
        {"id": "blocked",     "title": "Blocked",      "owner": "any"},
        {"id": "cancelled",   "title": "Cancelled",    "owner": "user"},
    ]


# ============================================================================
# Models
# ============================================================================


@dataclass
class TaskHistory:
    id: int
    task_id: str
    ts: str
    actor: str
    action: str
    from_status: str | None
    to_status: str | None
    comment: str | None


@dataclass
class Task:
    id: str
    title: str
    status: str
    priority: str
    size: str
    assignee: str | None
    description: str
    acceptance: str
    external_blocker: str | None
    created_at: str
    moved_at: str
    column_order: int
    project_id: str = DEFAULT_PROJECT_ID
    archived_at: str | None = None
    links: list[dict[str, str]] = field(default_factory=list)
    history: list[TaskHistory] = field(default_factory=list)
    # Total number of history rows; ``history`` may hold only the newest ones.
    history_total: int = 0
    blockers: list[str] = field(default_factory=list)

    def to_public(self) -> dict[str, Any]:
        d = asdict(self)
        d["history"] = [asdict(h) for h in self.history]
        return d


@dataclass
class Project:
    id: str
    name: str
    color: str
    icon: str
    sort_order: int
    archived: bool
    created_at: str
    path: str | None = None
    task_counts: dict[str, int] = field(default_factory=dict)
    total_tasks: int = 0

    def to_public(self) -> dict[str, Any]:
        return asdict(self)


# ============================================================================
# Store
# ============================================================================


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StatusConflict(RuntimeError):
    """The task is not in the status the caller expected (see move_task)."""


# Actors that are software, not a person. Moving a card out of Blocked resets
# run_command's max_runs budget only when a person does it — otherwise a
# "retry blocked cards" rule would defeat the limit it is meant to respect.
MACHINE_ACTORS = {"automation", "claude", "codex", "plan-import", "maintenance", "inbox"}


def is_machine_actor(actor: str | None) -> bool:
    a = (actor or "").strip().lower()
    return a in MACHINE_ACTORS or a.startswith(("agent:", "claude", "codex"))


def _default_db_path() -> Path:
    env = os.environ.get("KANBAN_DB", "").strip()
    if env:
        return Path(os.path.expanduser(env))
    return Path(__file__).resolve().parent.parent / "tasks.db"


def normalize_links(links: Iterable[Any] | None) -> list[tuple[str, str]]:
    """Validate ``[{"type": ..., "value": ...}]`` and return (type, value) pairs.

    ``link_type`` is accepted as an alias of ``type`` (the MCP tool uses that
    name). Raises ``ValueError`` with a readable message instead of letting a
    malformed payload surface as ``KeyError``.
    """
    out: list[tuple[str, str]] = []
    for i, ln in enumerate(links or []):
        if not isinstance(ln, dict):
            raise ValueError(f"links[{i}] must be an object with 'type' and 'value'")
        type_ = ln.get("type", ln.get("link_type"))
        value = ln.get("value")
        if not isinstance(type_, str) or type_ not in LINK_TYPES:
            raise ValueError(
                f"links[{i}].type must be one of {list(LINK_TYPES)}, got {type_!r}"
            )
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"links[{i}].value must be a non-empty string")
        out.append((type_, value.strip()))
    return out


class Store:
    """Thread-safe wrapper around SQLite. One instance per process."""

    _lock = threading.RLock()

    def __init__(self, db_path: str | Path | None = None, *, create: bool = True):
        """``create=False`` refuses to start a new, empty database: used by
        the MCP server, where a wrong ``KANBAN_DB`` would otherwise give the
        agent an empty board of its own instead of an error."""
        self.db_path = Path(db_path) if db_path is not None else _default_db_path()
        if not create and not self.db_path.exists():
            raise FileNotFoundError(
                f"kanban database not found: {self.db_path} — check KANBAN_DB "
                "(the web server creates the database on its first start)"
            )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            isolation_level=None,  # autocommit; explicit transactions use BEGIN
            # The UI server, every MCP process and helper scripts share one
            # file — wait for a concurrent writer instead of failing fast.
            timeout=15.0,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    # ------------------------------------------------------------------
    # Migration
    # ------------------------------------------------------------------

    def _migrate(self) -> None:
        schema = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")
        with self._lock:
            self._conn.executescript(schema)
            self._migrate_v2()
            self._migrate_v3()
            self._migrate_v4()
            self._migrate_v5()

    def _schema_version(self) -> int:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        return int(row["value"]) if row else 1

    def _migrate_v5(self) -> None:
        """v4 → v5: tasks.archived_at (rule_firings comes from schema.sql)."""
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(tasks)").fetchall()}
        if "archived_at" not in cols:
            self._conn.execute("ALTER TABLE tasks ADD COLUMN archived_at TEXT")
        if self._schema_version() < 5:
            self._conn.execute("UPDATE meta SET value='5' WHERE key='schema_version'")

    def _migrate_v4(self) -> None:
        """v3 → v4: project_sources table (created via schema.sql,
        this method only bumps the version)."""
        if self._schema_version() < 4:
            self._conn.execute("UPDATE meta SET value='4' WHERE key='schema_version'")

    def _migrate_v3(self) -> None:
        """v2 → v3: projects.path TEXT (Claude Code project directory)."""
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(projects)").fetchall()}
        if "path" not in cols:
            self._conn.execute("ALTER TABLE projects ADD COLUMN path TEXT")
        if self._schema_version() < 3:
            self._conn.execute("UPDATE meta SET value='3' WHERE key='schema_version'")

    def _migrate_v2(self) -> None:
        """v1 → v2: adds tasks.project_id for existing databases and
        creates a default project (id/name are configurable via env).

        Idempotent: checks PRAGMA table_info before running ALTER.
        """
        version = self._schema_version()
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(tasks)").fetchall()}
        if "project_id" not in cols:
            # old pre-v2 database — add the column with a default
            default_id = os.environ.get("KANBAN_DEFAULT_PROJECT_ID", "default")
            self._conn.execute(
                f"ALTER TABLE tasks ADD COLUMN project_id TEXT NOT NULL DEFAULT '{default_id}'"
            )
        # The project_id index is always created (idempotent). For a fresh
        # database the column appeared from CREATE TABLE in schema.sql; for
        # older databases it is created after the ALTER above.
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_project_status "
            "ON tasks(project_id, status, column_order)"
        )
        # The default project is created ONLY when the database has no
        # projects at all (fresh install). Existing databases keep their
        # own projects without an extra "default" being added on top.
        row = self._conn.execute("SELECT COUNT(*) AS n FROM projects").fetchone()
        if row["n"] == 0:
            default_id = os.environ.get("KANBAN_DEFAULT_PROJECT_ID", "default")
            default_name = os.environ.get("KANBAN_DEFAULT_PROJECT_NAME", "Default")
            default_color = os.environ.get("KANBAN_DEFAULT_PROJECT_COLOR", "#F10D30")
            default_icon = os.environ.get(
                "KANBAN_DEFAULT_PROJECT_ICON", default_name[:1].upper()
            )
            self._conn.execute(
                "INSERT INTO projects (id, name, color, icon, sort_order, archived, created_at) "
                "VALUES (?, ?, ?, ?, 0, 0, ?)",
                (default_id, default_name, default_color, default_icon, _now()),
            )
        if version < 2:
            self._conn.execute(
                "UPDATE meta SET value='2' WHERE key='schema_version'"
            )

    # ------------------------------------------------------------------
    # ID generation
    # ------------------------------------------------------------------

    def _next_id(self) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key='next_id'"
            ).fetchone()
            n = int(row["value"]) if row else 1
            self._conn.execute(
                "UPDATE meta SET value=? WHERE key='next_id'", (str(n + 1),)
            )
            return f"T-{n:03d}"

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def list_tasks(
        self,
        status: str | Iterable[str] | None = None,
        assignee: str | None = None,
        project_id: str | None = None,
        *,
        include_archived: bool = False,
    ) -> list[Task]:
        """List of tasks with filters, sorted by (status, column_order).

        ``project_id=None`` means "all projects". The board UI always
        passes a concrete project_id. Archived tasks are skipped unless
        ``include_archived=True``.
        """
        sql = "SELECT * FROM tasks WHERE 1=1"
        params: list[Any] = []
        if status is not None:
            if isinstance(status, str):
                statuses = [status]
            else:
                statuses = list(status)
            placeholders = ",".join("?" * len(statuses))
            sql += f" AND status IN ({placeholders})"
            params.extend(statuses)
        if assignee is not None:
            sql += " AND assignee = ?"
            params.append(assignee)
        if project_id is not None:
            sql += " AND project_id = ?"
            params.append(project_id)
        if not include_archived:
            sql += " AND archived_at IS NULL"
        sql += " ORDER BY status, column_order, id"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
            tasks = [self._row_to_task(r) for r in rows]
            self._attach_links_and_blockers(tasks)
        return tasks

    def get_task(self, task_id: str, *, history_limit: int | None = None) -> Task | None:
        """Full card. ``history_limit`` keeps only the newest N history rows
        (``history_total`` still reports the full count)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if not row:
                return None
            t = self._row_to_task(row)
            self._attach_links_and_blockers([t])
            t.history_total = self._conn.execute(
                "SELECT COUNT(*) AS n FROM task_history WHERE task_id=?", (task_id,)
            ).fetchone()["n"]
            if history_limit is None:
                h_rows = self._conn.execute(
                    "SELECT * FROM task_history WHERE task_id=? ORDER BY id ASC",
                    (task_id,),
                ).fetchall()
            else:
                h_rows = self._conn.execute(
                    "SELECT * FROM task_history WHERE task_id=? ORDER BY id DESC LIMIT ?",
                    (task_id, max(0, int(history_limit))),
                ).fetchall()[::-1]
            t.history = [self._row_to_history(r) for r in h_rows]
        return t

    def history(
        self, task_id: str, *, limit: int = 100, before_id: int | None = None
    ) -> tuple[list[TaskHistory], int]:
        """One page of a task's history, oldest→newest within the page.

        Pages go backwards in time: pass the smallest ``id`` of the current
        page as ``before_id`` to get the previous page. Returns
        ``(items, total)``. Raises ``KeyError`` for an unknown task.
        """
        with self._lock:
            if not self._task_exists(task_id):
                raise KeyError(task_id)
            total = self._conn.execute(
                "SELECT COUNT(*) AS n FROM task_history WHERE task_id=?", (task_id,)
            ).fetchone()["n"]
            sql = "SELECT * FROM task_history WHERE task_id=?"
            params: list[Any] = [task_id]
            if before_id is not None:
                sql += " AND id < ?"
                params.append(int(before_id))
            sql += " ORDER BY id DESC LIMIT ?"
            params.append(max(1, min(int(limit), 1000)))
            rows = self._conn.execute(sql, params).fetchall()[::-1]
        return [self._row_to_history(r) for r in rows], total

    def history_since(self, after_id: int, *, limit: int = 500) -> list[TaskHistory]:
        """History rows with ``id > after_id`` in insertion order (event feed)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM task_history WHERE id > ? ORDER BY id ASC LIMIT ?",
                (int(after_id), int(limit)),
            ).fetchall()
        return [self._row_to_history(r) for r in rows]

    def max_history_id(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(id), 0) AS m FROM task_history"
            ).fetchone()
        return int(row["m"])

    def board(self) -> dict[str, list[Task]]:
        """Group tasks by status, in column order."""
        result: dict[str, list[Task]] = {s: [] for s in STATUSES}
        for t in self.list_tasks():
            result.setdefault(t.status, []).append(t)
        return result

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def create_task(
        self,
        title: str,
        *,
        status: str = "backlog",
        priority: str = "normal",
        size: str = "M",
        description: str = "",
        acceptance: str = "",
        assignee: str | None = None,
        external_blocker: str | None = None,
        actor: str = "user",
        links: list[dict[str, str]] | None = None,
        task_id: str | None = None,
        project_id: str = DEFAULT_PROJECT_ID,
    ) -> Task:
        status = normalize_status(status)
        priority = normalize_priority(priority)
        size = normalize_size(size)
        if not title or not title.strip():
            raise ValueError("title must not be empty")
        link_pairs = normalize_links(links)
        ts = _now()
        with self._lock:
            if self.get_project(project_id) is None:
                known = ", ".join(p.id for p in self.list_projects(include_archived=True))
                raise ValueError(f"unknown project: {project_id} (known: {known})")
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                tid = task_id or self._next_id()
                # column_order — last in the column + 1 (per project)
                row = self._conn.execute(
                    "SELECT COALESCE(MAX(column_order), -1) AS m FROM tasks "
                    "WHERE status=? AND project_id=?",
                    (status, project_id),
                ).fetchone()
                col_order = (row["m"] + 1) if row else 0
                self._conn.execute(
                    """
                    INSERT INTO tasks (id, title, status, priority, size, assignee,
                                        description, acceptance, external_blocker,
                                        created_at, moved_at, column_order, project_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tid,
                        title.strip(),
                        status,
                        priority,
                        size,
                        assignee or None,
                        description,
                        acceptance,
                        external_blocker or None,
                        ts,
                        ts,
                        col_order,
                        project_id,
                    ),
                )
                for type_, value in link_pairs:
                    self._conn.execute(
                        "INSERT OR IGNORE INTO task_links (task_id, type, value) VALUES (?, ?, ?)",
                        (tid, type_, value),
                    )
                self._insert_history(tid, ts, actor, "create", None, status, None)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        task = self.get_task(tid)
        assert task is not None
        return task

    def move_task(
        self,
        task_id: str,
        to_status: str,
        *,
        actor: str = "user",
        comment: str | None = None,
        column_order: int | None = None,
        expected_from: str | None = None,
    ) -> Task:
        """Move a task to ``to_status``.

        Moving into the column the task is already in is not a status
        change: only the order within the column changes (when
        ``column_order`` is given), ``moved_at`` is left alone and the
        optional comment is recorded as a plain comment.

        ``expected_from`` makes the move conditional: if the task is no
        longer in that status (a human or another agent moved it), a
        ``StatusConflict`` is raised and nothing changes.
        """
        to_status = normalize_status(to_status)
        if expected_from is not None:
            expected_from = normalize_status(expected_from)
        ts = _now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT status, project_id FROM tasks WHERE id=?", (task_id,)
                ).fetchone()
                if not row:
                    raise KeyError(task_id)
                from_status = row["status"]
                project_id = row["project_id"]
                if expected_from is not None and from_status != expected_from:
                    raise StatusConflict(
                        f"task {task_id} is in '{from_status}', not '{expected_from}' — "
                        "someone else moved it; re-read the card before acting"
                    )
                if from_status == to_status:
                    if column_order is not None:
                        self._place_in_column(task_id, project_id, to_status, column_order)
                    if comment:
                        self._insert_history(task_id, ts, actor, "comment", None, None, comment)
                else:
                    self._conn.execute(
                        "UPDATE tasks SET status=?, moved_at=? WHERE id=?",
                        (to_status, ts, task_id),
                    )
                    if column_order is None:
                        # append to the end of the project's column
                        r2 = self._conn.execute(
                            "SELECT COALESCE(MAX(column_order), -1) AS m FROM tasks "
                            "WHERE status=? AND project_id=? AND id != ?",
                            (to_status, project_id, task_id),
                        ).fetchone()
                        self._conn.execute(
                            "UPDATE tasks SET column_order=? WHERE id=?",
                            ((r2["m"] + 1) if r2 else 0, task_id),
                        )
                    else:
                        self._place_in_column(task_id, project_id, to_status, column_order)
                    self._insert_history(
                        task_id, ts, actor, "move", from_status, to_status, comment
                    )
                    if from_status == "blocked" and not is_machine_actor(actor):
                        # A person unblocking the card is the "try again"
                        # signal: fresh max_runs budget for run_command rules.
                        self._conn.execute(
                            "DELETE FROM command_runs WHERE task_id=?", (task_id,)
                        )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        task = self.get_task(task_id, history_limit=0)
        assert task is not None
        return task

    def _place_in_column(
        self, task_id: str, project_id: str, status: str, index: int
    ) -> None:
        """Put ``task_id`` at ``index`` of its column and renumber the column
        0..n-1 (drag-drop sends the target index, not a free order value)."""
        ids = [
            r["id"]
            for r in self._conn.execute(
                "SELECT id FROM tasks WHERE project_id=? AND status=? AND id != ? "
                "AND archived_at IS NULL ORDER BY column_order, id",
                (project_id, status, task_id),
            ).fetchall()
        ]
        index = max(0, min(int(index), len(ids)))
        ids.insert(index, task_id)
        for i, tid in enumerate(ids):
            self._conn.execute("UPDATE tasks SET column_order=? WHERE id=?", (i, tid))

    def assign_task(self, task_id: str, assignee: str | None, *, actor: str) -> Task:
        """Set (or clear with ``None``/``""``) the assignee. No-op if unchanged."""
        assignee = (assignee or "").strip() or None
        ts = _now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT assignee FROM tasks WHERE id=?", (task_id,)
                ).fetchone()
                if not row:
                    raise KeyError(task_id)
                if row["assignee"] != assignee:
                    self._conn.execute(
                        "UPDATE tasks SET assignee=? WHERE id=?", (assignee, task_id)
                    )
                    self._insert_history(
                        task_id, ts, actor, "assign", None, None,
                        f"assignee → {assignee if assignee else '—'}",
                    )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        t = self.get_task(task_id, history_limit=0)
        assert t is not None
        return t

    def pull_task(self, task_id: str, assignee: str = "claude") -> Task:
        """Atomic: assignee IS NULL → assignee, status approved → analyst.

        Used by Claude/an agent for a safe "claim the task" operation.
        """
        ts = _now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT assignee, status, project_id FROM tasks WHERE id=?", (task_id,)
                ).fetchone()
                if not row:
                    raise KeyError(task_id)
                if row["assignee"] is not None and row["assignee"] != assignee:
                    raise RuntimeError(
                        f"task {task_id} already assigned to {row['assignee']}"
                    )
                if row["status"] != "approved":
                    raise RuntimeError(
                        f"task {task_id} is in '{row['status']}', not 'approved'"
                    )
                # move to analyst and claim (per project)
                r2 = self._conn.execute(
                    "SELECT COALESCE(MAX(column_order), -1) AS m FROM tasks "
                    "WHERE status='analyst' AND project_id=?",
                    (row["project_id"],),
                ).fetchone()
                col_order = (r2["m"] + 1) if r2 else 0
                self._conn.execute(
                    """UPDATE tasks SET status='analyst', assignee=?, moved_at=?, column_order=?
                       WHERE id=?""",
                    (assignee, ts, col_order, task_id),
                )
                self._insert_history(task_id, ts, assignee, "move", "approved", "analyst", "pulled")
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        t = self.get_task(task_id, history_limit=0)
        assert t is not None
        return t

    def add_comment(self, task_id: str, text: str, *, actor: str) -> None:
        if not text or not text.strip():
            raise ValueError("comment text must not be empty")
        ts = _now()
        with self._lock:
            if not self._task_exists(task_id):
                raise KeyError(task_id)
            self._insert_history(task_id, ts, actor, "comment", None, None, text)

    def add_link(self, task_id: str, type_: str, value: str) -> bool:
        """Attach a link. Returns False if the same link already exists."""
        ((type_, value),) = normalize_links([{"type": type_, "value": value}])
        with self._lock:
            if not self._task_exists(task_id):
                raise KeyError(task_id)
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO task_links (task_id, type, value) VALUES (?, ?, ?)",
                (task_id, type_, value),
            )
            return cur.rowcount > 0

    def set_blockers(
        self, task_id: str, blocker_ids: list[str], *, actor: str = "user"
    ) -> list[str]:
        """Replace the task's internal blockers. Returns the stored list.

        Rejects unknown ids, self-references and cycles with ``ValueError``
        (``KeyError`` for an unknown ``task_id``).
        """
        wanted: list[str] = []
        for b in blocker_ids:
            b = (b or "").strip()
            if b and b not in wanted:
                wanted.append(b)
        ts = _now()
        with self._lock:
            if not self._task_exists(task_id):
                raise KeyError(task_id)
            if task_id in wanted:
                raise ValueError(f"{task_id} cannot block itself")
            unknown = [b for b in wanted if not self._task_exists(b)]
            if unknown:
                raise ValueError(f"unknown blocker task(s): {', '.join(unknown)}")
            cycle = self._find_cycle(task_id, wanted)
            if cycle:
                raise ValueError("blockers would create a cycle: " + " → ".join(cycle))
            current = sorted(
                r["blocker_id"]
                for r in self._conn.execute(
                    "SELECT blocker_id FROM task_blockers WHERE task_id=?", (task_id,)
                ).fetchall()
            )
            if current == sorted(wanted):
                return wanted
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "DELETE FROM task_blockers WHERE task_id=?", (task_id,)
                )
                for b in wanted:
                    self._conn.execute(
                        "INSERT INTO task_blockers (task_id, blocker_id) VALUES (?, ?)",
                        (task_id, b),
                    )
                self._insert_history(
                    task_id, ts, actor, "update", None, None,
                    "blockers: " + (", ".join(wanted) if wanted else "—"),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return wanted

    def _find_cycle(self, task_id: str, new_blockers: list[str]) -> list[str] | None:
        """Path back to ``task_id`` through existing blocker edges, if any."""
        stack: list[tuple[str, list[str]]] = [(b, [task_id, b]) for b in new_blockers]
        seen: set[str] = set()
        while stack:
            node, path = stack.pop()
            if node == task_id:
                return path
            if node in seen:
                continue
            seen.add(node)
            for r in self._conn.execute(
                "SELECT blocker_id FROM task_blockers WHERE task_id=?", (node,)
            ).fetchall():
                stack.append((r["blocker_id"], path + [r["blocker_id"]]))
        return None

    # Fields whose old → new values are short enough to show in history.
    _SHOW_VALUES = ("priority", "size")

    def update_fields(
        self,
        task_id: str,
        *,
        actor: str,
        title: str | None = None,
        priority: str | None = None,
        size: str | None = None,
        description: str | None = None,
        acceptance: str | None = None,
        external_blocker: str | None = None,
    ) -> Task:
        """Update the given fields (``None`` = leave alone).

        Only fields whose value actually changes are written, and the
        history row lists just those (``priority: normal → high; title``).
        ``external_blocker=""`` clears the external blocker. A call that
        changes nothing writes nothing.
        """
        requested = {
            "title": title.strip() if isinstance(title, str) else None,
            "priority": priority,
            "size": size,
            "description": description,
            "acceptance": acceptance,
            "external_blocker": external_blocker,
        }
        if requested["title"] == "":
            raise ValueError("title must not be empty")
        if requested["priority"] is not None:
            requested["priority"] = normalize_priority(requested["priority"])
        if requested["size"] is not None:
            requested["size"] = normalize_size(requested["size"])
        ts = _now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT * FROM tasks WHERE id=?", (task_id,)
                ).fetchone()
                if not row:
                    raise KeyError(task_id)
                sets: list[str] = []
                params: list[Any] = []
                notes: list[str] = []
                for col, val in requested.items():
                    if val is None:
                        continue
                    if col == "external_blocker":
                        val = val.strip() or None
                    if row[col] == val:
                        continue
                    sets.append(f"{col} = ?")
                    params.append(val)
                    if col in self._SHOW_VALUES:
                        notes.append(f"{col}: {row[col]} → {val}")
                    else:
                        notes.append(col)
                if sets:
                    params.append(task_id)
                    self._conn.execute(
                        f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", params
                    )
                    self._insert_history(
                        task_id, ts, actor, "update", None, None, "; ".join(notes)
                    )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        t = self.get_task(task_id, history_limit=0)
        assert t is not None
        return t

    def archive_task(
        self, task_id: str, *, archived: bool = True, actor: str = "user",
        comment: str | None = None,
    ) -> Task:
        """Hide a task from the board without changing its status.

        Archiving keeps ``done`` tasks ``done`` (instead of relabelling them
        ``cancelled``); ``archived=False`` brings the task back. No-op if the
        task is already in the requested state.
        """
        ts = _now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT archived_at FROM tasks WHERE id=?", (task_id,)
                ).fetchone()
                if not row:
                    raise KeyError(task_id)
                is_archived = row["archived_at"] is not None
                if is_archived != archived:
                    self._conn.execute(
                        "UPDATE tasks SET archived_at=? WHERE id=?",
                        (ts if archived else None, task_id),
                    )
                    self._insert_history(
                        task_id, ts, actor, "archive" if archived else "unarchive",
                        None, None, comment,
                    )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        t = self.get_task(task_id, history_limit=0)
        assert t is not None
        return t

    def reorder(self, task_id: str, new_order: int) -> None:
        """Change the order within the current column."""
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET column_order=? WHERE id=?", (new_order, task_id)
            )

    # ------------------------------------------------------------------
    # Rule firings (idempotency for polling automation rules)
    # ------------------------------------------------------------------

    def rule_last_fired(self, rule_key: str, task_id: str, anchor: str) -> str | None:
        """When ``rule_key`` last acted on ``task_id`` in episode ``anchor``."""
        with self._lock:
            row = self._conn.execute(
                "SELECT fired_at FROM rule_firings WHERE rule_key=? AND task_id=? AND anchor=?",
                (rule_key, task_id, anchor),
            ).fetchone()
        return row["fired_at"] if row else None

    def record_rule_firing(self, rule_key: str, task_id: str, anchor: str) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO rule_firings (rule_key, task_id, anchor, fired_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(rule_key, task_id, anchor) DO UPDATE SET fired_at=excluded.fired_at""",
                (rule_key, task_id, anchor, _now()),
            )

    def command_runs(self, rule_key: str, task_id: str) -> int:
        """How many times run_command ``rule_key`` was started for ``task_id``."""
        with self._lock:
            row = self._conn.execute(
                "SELECT runs FROM command_runs WHERE rule_key=? AND task_id=?",
                (rule_key, task_id),
            ).fetchone()
        return int(row["runs"]) if row else 0

    def bump_command_runs(self, rule_key: str, task_id: str, delta: int = 1) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO command_runs (rule_key, task_id, runs, last_started_at)
                   VALUES (?, ?, MAX(0, ?), ?)
                   ON CONFLICT(rule_key, task_id) DO UPDATE
                   SET runs = MAX(0, runs + ?),
                       last_started_at = CASE WHEN ? > 0 THEN excluded.last_started_at
                                              ELSE last_started_at END""",
                (rule_key, task_id, delta, _now(), delta, delta),
            )

    def set_command_rc(self, rule_key: str, task_id: str, rc: int | None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE command_runs SET last_rc=? WHERE rule_key=? AND task_id=?",
                (rc, rule_key, task_id),
            )

    def reset_command_runs(self, task_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM command_runs WHERE task_id=?", (task_id,))

    def save_command_job(
        self, rule_key: str, task_id: str, *, state: str, rule_name: str,
        ctx: dict[str, Any], pid: int | None = None,
    ) -> None:
        now = _now()
        with self._lock:
            self._conn.execute(
                """INSERT INTO command_jobs
                   (rule_key, task_id, state, rule_name, ctx, pid, queued_at, started_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(rule_key, task_id) DO UPDATE SET
                     state=excluded.state, rule_name=excluded.rule_name, ctx=excluded.ctx,
                     pid=excluded.pid,
                     started_at=CASE WHEN excluded.state='running' THEN excluded.started_at
                                     ELSE command_jobs.started_at END""",
                (rule_key, task_id, state, rule_name,
                 json.dumps(ctx, ensure_ascii=False), pid, now,
                 now if state == "running" else None),
            )

    def delete_command_job(self, rule_key: str, task_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM command_jobs WHERE rule_key=? AND task_id=?", (rule_key, task_id)
            )

    def list_command_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM command_jobs ORDER BY queued_at, rule_key, task_id"
            ).fetchall()
        return [{**dict(r), "ctx": json.loads(r["ctx"])} for r in rows]

    # ------------------------------------------------------------------
    # Projects
    # ------------------------------------------------------------------

    def list_projects(self, *, include_archived: bool = False) -> list[Project]:
        """All projects with task_counts aggregated by status (archived
        tasks are not counted)."""
        sql = "SELECT * FROM projects"
        if not include_archived:
            sql += " WHERE archived = 0"
        sql += " ORDER BY sort_order, name"
        with self._lock:
            rows = self._conn.execute(sql).fetchall()
            counts_rows = self._conn.execute(
                "SELECT project_id, status, COUNT(*) AS n "
                "FROM tasks WHERE archived_at IS NULL GROUP BY project_id, status"
            ).fetchall()
        counts: dict[str, dict[str, int]] = {}
        totals: dict[str, int] = {}
        for r in counts_rows:
            counts.setdefault(r["project_id"], {})[r["status"]] = r["n"]
            totals[r["project_id"]] = totals.get(r["project_id"], 0) + r["n"]
        result = []
        for r in rows:
            p = self._row_to_project(r)
            p.task_counts = counts.get(p.id, {})
            p.total_tasks = totals.get(p.id, 0)
            result.append(p)
        return result

    def get_project(self, project_id: str) -> Project | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM projects WHERE id=?", (project_id,)
            ).fetchone()
        if not row:
            return None
        return self._row_to_project(row)

    def create_project(
        self,
        project_id: str,
        name: str,
        *,
        color: str = "#F10D30",
        icon: str = "",
        sort_order: int | None = None,
        path: str | None = None,
    ) -> Project:
        ts = _now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if sort_order is None:
                    r = self._conn.execute(
                        "SELECT COALESCE(MAX(sort_order), -1) AS m FROM projects"
                    ).fetchone()
                    sort_order = (r["m"] + 1) if r else 0
                self._conn.execute(
                    """INSERT INTO projects
                       (id, name, color, icon, sort_order, archived, path, created_at)
                       VALUES (?, ?, ?, ?, ?, 0, ?, ?)""",
                    (project_id, name, color, icon or name[:1].upper(), sort_order, path, ts),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        p = self.get_project(project_id)
        assert p is not None
        return p

    def update_project(
        self,
        project_id: str,
        *,
        name: str | None = None,
        color: str | None = None,
        icon: str | None = None,
        sort_order: int | None = None,
        path: str | None = None,
    ) -> Project:
        sets: list[str] = []
        params: list[Any] = []
        # path is forwarded as-is (None means "leave alone", "" means "clear").
        for col, val in (
            ("name", name), ("color", color), ("icon", icon),
            ("sort_order", sort_order), ("path", path),
        ):
            if val is not None:
                sets.append(f"{col} = ?")
                # an empty string for path becomes NULL in the database
                params.append(None if (col == "path" and val == "") else val)
        if not sets:
            p = self.get_project(project_id)
            if p is None:
                raise KeyError(project_id)
            return p
        params.append(project_id)
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM projects WHERE id=?", (project_id,)
            ).fetchone()
            if not row:
                raise KeyError(project_id)
            self._conn.execute(
                f"UPDATE projects SET {', '.join(sets)} WHERE id=?", params
            )
        p = self.get_project(project_id)
        assert p is not None
        return p

    # ------------------------------------------------------------------
    # Project sources (one source per project)
    # ------------------------------------------------------------------

    def set_project_source(
        self, project_id: str, type_: str, config: dict[str, Any]
    ) -> None:
        ts = _now()
        with self._lock:
            self._conn.execute(
                """INSERT INTO project_sources (project_id, type, config, created_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(project_id) DO UPDATE
                   SET type=excluded.type, config=excluded.config""",
                (project_id, type_, json.dumps(config, ensure_ascii=False), ts),
            )

    def get_project_source(self, project_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM project_sources WHERE project_id=?", (project_id,)
            ).fetchone()
        if not row:
            return None
        return {
            "project_id": row["project_id"],
            "type": row["type"],
            "config": json.loads(row["config"]),
            "last_sync_at": row["last_sync_at"],
            "created_at": row["created_at"],
        }

    def update_source_sync_time(self, project_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE project_sources SET last_sync_at=? WHERE project_id=?",
                (_now(), project_id),
            )

    def delete_project_source(self, project_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM project_sources WHERE project_id=?", (project_id,)
            )

    def archive_project(self, project_id: str, archived: bool = True) -> Project:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM projects WHERE id=?", (project_id,)
            ).fetchone()
            if not row:
                raise KeyError(project_id)
            self._conn.execute(
                "UPDATE projects SET archived=? WHERE id=?",
                (1 if archived else 0, project_id),
            )
        p = self.get_project(project_id)
        assert p is not None
        return p

    def _row_to_project(self, row: sqlite3.Row) -> Project:
        return Project(
            id=row["id"],
            name=row["name"],
            color=row["color"],
            icon=row["icon"],
            sort_order=row["sort_order"],
            archived=bool(row["archived"]),
            created_at=row["created_at"],
            path=row["path"] if "path" in row.keys() else None,
        )

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Dump the whole board as a plain dict for JSON persistence."""
        tasks = self.list_tasks(include_archived=True)
        projects = self.list_projects(include_archived=True)
        return {
            "exported_at": _now(),
            "schema_version": 5,
            "projects": [p.to_public() for p in projects],
            "tasks": [t.to_public() for t in tasks],
        }

    def save_snapshot(self, dest_dir: str | Path | None = None) -> Path:
        if dest_dir is None:
            dest_dir = Path(__file__).resolve().parent.parent / "snapshots"
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        date_part = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        fp = dest_dir / f"{date_part}.json"
        fp.write_text(
            json.dumps(self.snapshot(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return fp

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _task_exists(self, task_id: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM tasks WHERE id=?", (task_id,)
        ).fetchone() is not None

    def _insert_history(
        self,
        task_id: str,
        ts: str,
        actor: str,
        action: str,
        from_status: str | None,
        to_status: str | None,
        comment: str | None,
    ) -> None:
        self._conn.execute(
            """INSERT INTO task_history
               (task_id, ts, actor, action, from_status, to_status, comment)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (task_id, ts, actor or "user", action, from_status, to_status, comment),
        )

    def _attach_links_and_blockers(self, tasks: list[Task]) -> None:
        """Load links and blockers for many tasks with two queries."""
        if not tasks:
            return
        by_id = {t.id: t for t in tasks}
        ids = list(by_id)
        for chunk_start in range(0, len(ids), 500):
            chunk = ids[chunk_start:chunk_start + 500]
            ph = ",".join("?" * len(chunk))
            for r in self._conn.execute(
                f"SELECT task_id, type, value FROM task_links WHERE task_id IN ({ph}) "
                "ORDER BY type, value",
                chunk,
            ).fetchall():
                by_id[r["task_id"]].links.append({"type": r["type"], "value": r["value"]})
            for r in self._conn.execute(
                f"SELECT task_id, blocker_id FROM task_blockers WHERE task_id IN ({ph}) "
                "ORDER BY blocker_id",
                chunk,
            ).fetchall():
                by_id[r["task_id"]].blockers.append(r["blocker_id"])

    @staticmethod
    def _row_to_history(r: sqlite3.Row) -> TaskHistory:
        return TaskHistory(
            id=r["id"],
            task_id=r["task_id"],
            ts=r["ts"],
            actor=r["actor"],
            action=r["action"],
            from_status=r["from_status"],
            to_status=r["to_status"],
            comment=r["comment"],
        )

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> Task:
        keys = row.keys()
        return Task(
            id=row["id"],
            title=row["title"],
            status=row["status"],
            priority=row["priority"],
            size=row["size"],
            assignee=row["assignee"],
            description=row["description"],
            acceptance=row["acceptance"],
            external_blocker=row["external_blocker"],
            created_at=row["created_at"],
            moved_at=row["moved_at"],
            column_order=row["column_order"],
            project_id=row["project_id"] if "project_id" in keys else DEFAULT_PROJECT_ID,
            archived_at=row["archived_at"] if "archived_at" in keys else None,
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()
