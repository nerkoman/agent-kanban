"""Database maintenance: ``python -m kanban_store.maintenance <command>``.

Commands (all read-only unless ``--apply`` is given; ``--apply`` always
writes a backup copy of the database first):

    doctor            health report with suggested fixes (read-only)
    dedupe-history    delete repeated automation rows — the v0.1 rule-engine
                      bug wrote the same "priority" update + comment every
                      minute for tasks sitting in a column. Only identical
                      automation rows less than --window-minutes apart with no
                      other activity in between go; the first of each run stays
    restore-archived  tasks that v0.1 auto-archive rules moved done → cancelled
                      go back to done and are archived instead (status kept,
                      hidden from the board)
    vacuum            reclaim disk space after deleting rows

Options: ``--db PATH`` (default: ``KANBAN_DB`` or ``<repo>/tasks.db``),
``--apply``, ``--backup-dir DIR``.

Stop the web server before ``vacuum``; the other commands are safe to run
while it is up (SQLite WAL + a single short write transaction).
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .store import Store, _default_db_path

MAINTENANCE_ACTOR = "maintenance"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect(db: Path) -> sqlite3.Connection:
    if not db.exists():
        raise SystemExit(f"database not found: {db}")
    # Open through Store once so an older database is migrated to the
    # current schema before we look at it.
    Store(db).close()
    conn = sqlite3.connect(str(db), isolation_level=None, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def backup(db: Path, backup_dir: Path | None = None) -> Path:
    """Consistent copy of the live database (SQLite online backup API)."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest_dir = backup_dir or db.parent
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{db.name}.bak-{stamp}"
    src = sqlite3.connect(str(db))
    dst = sqlite3.connect(str(dest))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return dest


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


# The v0.1 engine re-applied a rule every KANBAN_AUTOMATION_INTERVAL (60 s by
# default). Identical automation rows closer together than this are spam;
# rows further apart (a daily reminder, a rule that fired again a week later)
# are left alone.
DEFAULT_WINDOW_MINUTES = 5.0


def find_repeated_automation_rows(
    conn: sqlite3.Connection, window_minutes: float = DEFAULT_WINDOW_MINUTES
) -> list[int]:
    """Ids of automation update/comment rows that repeat the same row within
    ``window_minutes`` with nothing but automation in between."""
    return [i for i, _task in _repeated_rows(conn, window_minutes)]


def _parse_ts(ts: str) -> datetime | None:
    try:
        d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _repeated_rows(
    conn: sqlite3.Connection, window_minutes: float = DEFAULT_WINDOW_MINUTES
) -> list[tuple[int, str]]:
    window = window_minutes * 60
    to_delete: list[tuple[int, str]] = []
    cur_task = None
    last: dict[tuple[str, str | None], datetime] = {}
    for r in conn.execute(
        "SELECT id, task_id, ts, actor, action, comment FROM task_history ORDER BY task_id, id"
    ):
        if r["task_id"] != cur_task:
            cur_task = r["task_id"]
            last = {}
        # Anything that isn't the automation's own update/comment — a move, a
        # person's comment, a priority someone changed by hand — ends the run:
        # a rule acting again after that is news, not a repeat.
        if r["actor"] != "automation" or r["action"] not in ("update", "comment"):
            last = {}
            continue
        ts = _parse_ts(r["ts"])
        key = (r["action"], r["comment"])
        prev = last.get(key)
        if ts is not None and prev is not None and 0 <= (ts - prev).total_seconds() <= window:
            to_delete.append((r["id"], r["task_id"]))
        if ts is not None:
            last[key] = ts
    return to_delete


def find_relabelled_done(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Tasks now in 'cancelled' whose last move was automation done → cancelled."""
    rows = conn.execute(
        """
        SELECT t.id, t.project_id, t.title, h.ts AS archived_ts
        FROM tasks t
        JOIN task_history h ON h.id = (
            SELECT MAX(id) FROM task_history WHERE task_id = t.id AND action = 'move'
        )
        WHERE t.status = 'cancelled'
          AND h.actor = 'automation'
          AND h.from_status = 'done'
          AND h.to_status = 'cancelled'
        ORDER BY t.id
        """
    ).fetchall()
    return [dict(r) for r in rows]


def _check_mcp_json(f: Path, db: Path, project_id: str) -> dict[str, str] | None:
    """v0.1 ignored KANBAN_DB, so a stale value in .mcp.json was harmless.
    Since v0.2 it is honoured: an entry pointing elsewhere means that
    project's agents work on a different (or missing) database."""
    try:
        servers = json.loads(f.read_text(encoding="utf-8")).get("mcpServers") or {}
    except (OSError, json.JSONDecodeError, AttributeError):
        return None
    for name, cfg in servers.items():
        if not isinstance(cfg, dict) or "kanban_mcp" not in " ".join(map(str, cfg.get("args") or [])):
            continue
        env_db = (cfg.get("env") or {}).get("KANBAN_DB")
        if env_db and Path(os.path.expanduser(env_db)).resolve() != db.resolve():
            return {
                "id": "mcp-db-mismatch",
                "what": f"{f}: server '{name}' uses KANBAN_DB={env_db}, not this database",
                "fix": f"python -m kanban_mcp.connect {shlex.quote(str(f.parent))} "
                       f"--project {project_id} --db {shlex.quote(str(db))}",
            }
    return None


def _rules_file() -> Path:
    root = Path(__file__).resolve().parent.parent
    return Path(os.environ.get("KANBAN_RULES_FILE") or (root / "kanban_data" / "rules.json"))


def doctor(db: Path) -> dict[str, Any]:
    conn = _connect(db)
    try:
        report: dict[str, Any] = {"db": str(db), "problems": [], "notes": []}
        size = db.stat().st_size + sum(
            p.stat().st_size for p in (db.with_name(db.name + "-wal"),) if p.exists()
        )
        report["size_mb"] = round(size / 1024 / 1024, 1)
        report["schema_version"] = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0]
        counts = {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("projects", "tasks", "task_history", "task_links", "task_blockers")
        }
        report["rows"] = counts
        top = conn.execute(
            "SELECT task_id, COUNT(*) AS n FROM task_history GROUP BY task_id "
            "ORDER BY n DESC LIMIT 5"
        ).fetchall()
        report["largest_histories"] = {r["task_id"]: r["n"] for r in top}

        repeated = _repeated_rows(conn)
        if repeated:
            per_task = Counter(task for _id, task in repeated)
            worst = ", ".join(f"{t} ×{n:,}" for t, n in per_task.most_common(5))
            report["problems"].append({
                "id": "history-spam",
                "what": f"{len(repeated):,} repeated automation history rows ({worst})",
                "fix": "python -m kanban_store.maintenance dedupe-history --apply, then vacuum",
            })
        relabelled = find_relabelled_done(conn)
        if relabelled:
            by_project = Counter(r["project_id"] for r in relabelled)
            report["problems"].append({
                "id": "done-as-cancelled",
                "what": f"{len(relabelled)} done tasks were relabelled 'cancelled' by an "
                        f"auto-archive rule ({dict(by_project)})",
                "fix": "python -m kanban_store.maintenance restore-archived --apply; "
                       "switch the rule's action to {\"type\": \"archive\"}",
            })
        orphans = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE project_id NOT IN (SELECT id FROM projects)"
        ).fetchone()[0]
        if orphans:
            report["problems"].append({
                "id": "orphan-tasks",
                "what": f"{orphans} tasks belong to a project that does not exist",
                "fix": "create the project (POST /api/projects) or move the tasks",
            })
        for p in conn.execute("SELECT id, path FROM projects WHERE archived = 0"):
            if not p["path"]:
                continue
            path = Path(p["path"])
            if not path.exists():
                report["notes"].append(f"project '{p['id']}': path does not exist: {path}")
            elif not (path / ".mcp.json").exists():
                report["notes"].append(
                    f"project '{p['id']}': no .mcp.json in {path} — agents there have no "
                    f"kanban tools (python -m kanban_mcp.connect {shlex.quote(str(path))} "
                    f"--project {p['id']})"
                )
            else:
                problem = _check_mcp_json(path / ".mcp.json", db, p["id"])
                if problem:
                    report["problems"].append(problem)
        rules = _rules_file()
        if rules.exists():
            try:
                data = json.loads(rules.read_text(encoding="utf-8"))
                for r in data.get("rules", []):
                    t, a = r.get("trigger") or {}, r.get("action") or {}
                    if (t.get("status") == "done" and a.get("type") == "move_to"
                            and a.get("status") == "cancelled"):
                        report["problems"].append({
                            "id": "archive-rule-relabels",
                            "what": f"rule '{r.get('name')}' moves done tasks to cancelled",
                            "fix": "use {\"type\": \"archive\"} — the task stays done, "
                                   "hidden from the board",
                        })
            except (OSError, json.JSONDecodeError) as e:
                report["notes"].append(f"rules.json unreadable: {e}")
        return report
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Fixes
# ---------------------------------------------------------------------------


def dedupe_history(
    db: Path, *, apply: bool, backup_dir: Path | None = None,
    window_minutes: float = DEFAULT_WINDOW_MINUTES,
) -> dict[str, Any]:
    conn = _connect(db)
    try:
        ids = find_repeated_automation_rows(conn, window_minutes)
        result: dict[str, Any] = {"repeated_rows": len(ids), "applied": False}
        if not apply or not ids:
            return result
        result["backup"] = str(backup(db, backup_dir))
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.executemany("DELETE FROM task_history WHERE id = ?", ((i,) for i in ids))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        result["applied"] = True
        return result
    finally:
        conn.close()


def restore_archived(db: Path, *, apply: bool, backup_dir: Path | None = None) -> dict[str, Any]:
    conn = _connect(db)
    try:
        tasks = find_relabelled_done(conn)
        result: dict[str, Any] = {
            "tasks": len(tasks),
            "sample": [f"{t['id']} [{t['project_id']}] {t['title'][:60]}" for t in tasks[:10]],
            "applied": False,
        }
        if not apply or not tasks:
            return result
        result["backup"] = str(backup(db, backup_dir))
        ts = _now()
        note = ("Restored: an auto-archive rule had relabelled this done task as "
                "cancelled. Status is done again; the task stays archived.")
        conn.execute("BEGIN IMMEDIATE")
        try:
            for t in tasks:
                conn.execute(
                    "UPDATE tasks SET status='done', archived_at=? WHERE id=?",
                    (t["archived_ts"], t["id"]),
                )
                conn.execute(
                    """INSERT INTO task_history
                       (task_id, ts, actor, action, from_status, to_status, comment)
                       VALUES (?, ?, ?, 'move', 'cancelled', 'done', ?)""",
                    (t["id"], ts, MAINTENANCE_ACTOR, note),
                )
                conn.execute(
                    """INSERT INTO task_history
                       (task_id, ts, actor, action, from_status, to_status, comment)
                       VALUES (?, ?, ?, 'archive', NULL, NULL, NULL)""",
                    (t["id"], ts, MAINTENANCE_ACTOR),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        result["applied"] = True
        return result
    finally:
        conn.close()


def vacuum(db: Path) -> dict[str, Any]:
    conn = _connect(db)
    try:
        before = db.stat().st_size
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        after = db.stat().st_size
        return {"before_mb": round(before / 1048576, 1), "after_mb": round(after / 1048576, 1)}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m kanban_store.maintenance",
        description="agent-kanban database maintenance",
    )
    ap.add_argument("command", choices=["doctor", "dedupe-history", "restore-archived", "vacuum"])
    ap.add_argument("--db", type=Path, default=None, help="SQLite file (default: KANBAN_DB)")
    ap.add_argument("--apply", action="store_true", help="write changes (backup first)")
    ap.add_argument("--backup-dir", type=Path, default=None)
    ap.add_argument("--window-minutes", type=float, default=DEFAULT_WINDOW_MINUTES,
                    help="dedupe-history: identical automation rows closer than this "
                         f"are repeats (default {DEFAULT_WINDOW_MINUTES:g})")
    args = ap.parse_args(argv)
    db = (args.db or _default_db_path()).expanduser()

    if args.command == "doctor":
        out = doctor(db)
    elif args.command == "dedupe-history":
        out = dedupe_history(db, apply=args.apply, backup_dir=args.backup_dir,
                             window_minutes=args.window_minutes)
    elif args.command == "restore-archived":
        out = restore_archived(db, apply=args.apply, backup_dir=args.backup_dir)
    else:
        out = vacuum(db)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    if args.command in ("dedupe-history", "restore-archived") and not args.apply:
        print("\n(dry run — nothing changed; add --apply to write, a backup is made first)",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
