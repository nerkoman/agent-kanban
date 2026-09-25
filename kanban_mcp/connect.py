"""Connect a code project to the kanban: ``python -m kanban_mcp.connect``.

    python -m kanban_mcp.connect ~/code/myapp --project myapp

Writes (or merges into) ``<dir>/.mcp.json`` an entry that starts this
repo's MCP server with the right environment — ``PYTHONPATH``, ``KANBAN_DB``,
``KANBAN_PROJECT_ID``, ``KANBAN_ACTOR`` — and refreshes the "Kanban board"
block in ``CLAUDE.md`` (and ``AGENTS.md`` if present). Other servers in
``.mcp.json`` are left alone; if the file already has an entry that runs
``kanban_mcp`` under another name (say ``my-kanban``), that name is kept
so existing ``mcp__<name>__*`` permissions keep working.

Registering a project on the board used to leave this file to the user, so
agents often had no kanban tools while CLAUDE.md said they did.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ALIAS = "agent-kanban"


def _python() -> str:
    venv = REPO_ROOT / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


def detect_alias(project_dir: Path) -> str | None:
    """Name of an existing ``.mcp.json`` entry that runs ``kanban_mcp``."""
    f = project_dir / ".mcp.json"
    if not f.exists():
        return None
    try:
        servers = json.loads(f.read_text(encoding="utf-8")).get("mcpServers") or {}
    except (OSError, json.JSONDecodeError):
        return None
    for name, cfg in servers.items():
        if isinstance(cfg, dict) and "kanban_mcp" in " ".join(map(str, cfg.get("args") or [])):
            return name
    return None


def server_entry(project_id: str, *, db: Path, actor: str = "claude") -> dict[str, Any]:
    return {
        "type": "stdio",
        "command": _python(),
        "args": ["-m", "kanban_mcp"],
        "cwd": str(REPO_ROOT),
        "env": {
            # Claude Code ignores "cwd" for stdio servers; PYTHONPATH is what
            # lets python find the kanban_mcp package.
            "PYTHONPATH": str(REPO_ROOT),
            "KANBAN_DB": str(db),
            "KANBAN_PROJECT_ID": project_id,
            "KANBAN_ACTOR": actor,
        },
    }


def write_mcp_json(
    project_dir: Path, project_id: str, *, db: Path, alias: str | None = None,
    actor: str | None = None, dry_run: bool = False,
) -> tuple[Path, str, dict[str, Any]]:
    """Merge the kanban server into ``.mcp.json``. Returns (path, alias, config).

    An existing entry is updated in place: only what says *which* kanban
    (``command``, ``args``, ``cwd``, ``PYTHONPATH``, ``KANBAN_DB``,
    ``KANBAN_PROJECT_ID``) is rewritten. Other keys — ``KANBAN_MCP_HUMAN_ONLY``,
    ``KANBAN_URL``, custom env, ``KANBAN_ACTOR`` unless ``actor`` is given —
    are kept, so re-running connect never loosens a setting someone made.
    """
    f = project_dir / ".mcp.json"
    data: dict[str, Any] = {}
    if f.exists():
        data = json.loads(f.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"{f}: top level must be an object")
    name = alias or detect_alias(project_dir) or DEFAULT_ALIAS
    servers = data.setdefault("mcpServers", {})
    fresh = server_entry(project_id, db=db, actor=actor or "claude")
    current = servers.get(name)
    if isinstance(current, dict):
        merged = dict(current)
        for k in ("command", "args", "cwd"):
            merged[k] = fresh[k]
        merged.setdefault("type", "stdio")
        env = dict(current.get("env") or {})
        for k in ("PYTHONPATH", "KANBAN_DB", "KANBAN_PROJECT_ID"):
            env[k] = fresh["env"][k]
        if actor is not None or "KANBAN_ACTOR" not in env:
            env["KANBAN_ACTOR"] = fresh["env"]["KANBAN_ACTOR"]
        merged["env"] = env
        servers[name] = merged
    else:
        servers[name] = fresh
    if not dry_run:
        f.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return f, name, data


def connect(
    project_dir: Path, project_id: str, *, db: Path | None = None, alias: str | None = None,
    actor: str | None = None, claude_md: bool = True, dry_run: bool = False,
) -> dict[str, Any]:
    from kanban_store import Store
    from kanban_store.store import _default_db_path
    from kanban_ui.automation import plan_md

    project_dir = project_dir.expanduser().resolve()
    if not project_dir.is_dir():
        raise ValueError(f"not a directory: {project_dir}")
    db = (db or _default_db_path()).expanduser().resolve()
    store = Store(db, create=False)
    try:
        project = store.get_project(project_id)
        if project is None:
            known = ", ".join(p.id for p in store.list_projects(include_archived=True))
            raise ValueError(f"unknown project: {project_id} (known: {known})")
        src = store.get_project_source(project_id) or {}
    finally:
        store.close()
    mcp_file, name, _cfg = write_mcp_json(
        project_dir, project_id, db=db, alias=alias, actor=actor, dry_run=dry_run
    )
    result: dict[str, Any] = {"mcp_json": str(mcp_file), "alias": name, "db": str(db),
                              "project_id": project_id, "dry_run": dry_run}
    if claude_md:
        cfg = src.get("config") or {}
        plan_files = cfg.get("files") or ([cfg["file"]] if cfg.get("file") else [])
        if dry_run:
            result["claude_md"] = [str(project_dir / "CLAUDE.md")]
        else:
            written = plan_md.update_claude_md(
                project_dir / "CLAUDE.md", project_id, project.name,
                alias=name, plan_files=plan_files,
            )
            result["claude_md"] = [str(p) for p in written]
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m kanban_mcp.connect",
        description="Write .mcp.json + CLAUDE.md so agents in a project get the kanban tools",
    )
    ap.add_argument("project_dir", type=Path)
    ap.add_argument("--project", required=True, help="kanban project slug")
    ap.add_argument("--alias", default=None,
                    help=f"server name in .mcp.json (default: existing kanban entry or {DEFAULT_ALIAS})")
    ap.add_argument("--actor", default=None,
                    help="name written to history (default: keep existing, else claude)")
    ap.add_argument("--db", type=Path, default=None, help="SQLite file (default: KANBAN_DB)")
    ap.add_argument("--no-claude-md", action="store_true", help="leave CLAUDE.md alone")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    try:
        out = connect(
            args.project_dir, args.project, db=args.db, alias=args.alias, actor=args.actor,
            claude_md=not args.no_claude_md, dry_run=args.dry_run,
        )
    except (ValueError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(json.dumps(out, ensure_ascii=False, indent=2))
    if not args.dry_run:
        print(f"\nNext: restart Claude Code in {args.project_dir} (or run `claude mcp list`) "
              f"and approve the '{out['alias']}' server when asked.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
