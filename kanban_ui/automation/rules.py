"""Rule engine: applies rules from ``KANBAN_RULES_FILE``.

``rules.json`` file::

    {
      "paused": false,
      "rules": [
        {
          "name": "Archive done tasks after 30 days",
          "enabled": true,
          "project_id": null,
          "trigger": {"type": "task_idle", "status": "done", "days": 30},
          "action":  {"type": "archive", "comment": "Auto-archive: 30 days in done"}
        }
      ]
    }

Triggers:
    - task_idle: status=X + days/hours/minutes=N — tasks that have been in
      column X for longer than N (polling)
    - task_count_in_status: status=X, gt=N | lt=N — task counter in a column (polling)
    - blockers_done: status=X (default backlog), resolved_statuses? — tasks
      in X that have internal blockers, all of them done/cancelled (polling)
    - task_moved: to_status=X, from_status?, project_id? — reactive, fires
      right after a move made by anyone: UI, REST, MCP agents, scripts.
      A task *created* directly in X counts as moved into X (from_status null).

``project_id`` (rule-level or in the trigger) may be a slug or a list of slugs.

Actions:
    - move_to: status=X, comment? — move to a column
    - add_comment: comment — record in history
    - set_priority: priority=critical|high|normal|low, comment?
    - assign: assignee=<name> (null/"" clears), comment?
    - archive: comment? — hide the task from the board, status unchanged
    - run_command: cmd, args?, env?, log_file?, max_concurrent?, max_runs? —
      spawn a process (one per task at a time; see CommandRunner)

Polling rules act on a task **once per stay in the column**: the engine
remembers (rule, task, moved_at) in ``rule_firings``. A rule-level
``"repeat_every": {"hours": 6}`` allows repeating while the task stays put.
A rule-level ``"wip_limit": {"statuses": [...], "max": N}`` caps how many
tasks of the project may sit in those statuses — the rule acts on at most
``max - current`` tasks per run (handy for "promote the next ready card").

A top-level ``"paused": true`` stops all actions (rules keep loading and
validating, queued commands wait). All mutations use actor=automation.
Hot-reload by file mtime.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from collections import deque
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from kanban_store import Store, STATUSES
from kanban_store.store import RESOLVED_STATUSES

log = logging.getLogger("kanban.automation.rules")

DEFAULT_INTERVAL = float(os.environ.get("KANBAN_AUTOMATION_INTERVAL", "60"))

VALID_TRIGGERS = {"task_idle", "task_count_in_status", "task_moved", "blockers_done"}
VALID_ACTIONS = {"move_to", "add_comment", "set_priority", "run_command", "archive", "assign"}
VALID_PRIORITIES = {"critical", "high", "normal", "low"}

# Polling-mode triggers are processed in _run_once.
# Reactive-mode triggers are processed in emit_rule_event() — invoked by the
# event feed right after the history row appears, whoever wrote it.
REACTIVE_TRIGGERS = {"task_moved"}

# Loop guard for reactive rules: the same rule may fire on the same task at
# most LOOP_LIMIT times within LOOP_WINDOW (e.g. two rules bouncing a card
# between columns). Excess events are dropped and reported in last_errors.
LOOP_LIMIT = int(os.environ.get("KANBAN_RULE_LOOP_LIMIT", "5"))
LOOP_WINDOW = timedelta(minutes=10)

_status: dict[str, Any] = {
    "running": False,
    "paused": False,
    "rules_file": None,
    "interval_sec": DEFAULT_INTERVAL,
    "rules_loaded": 0,
    "last_run_at": None,
    "last_run_actions": [],     # actions from the last run: {ts, rule, task_id, action}
    "last_reactive": [],        # reactive triggers (task_moved): {ts, rule, task_id, action}
    "last_errors": [],          # parsing or apply errors: {ts, rule, error}
    "config_mtime": None,
}


def rules_status() -> dict[str, Any]:
    st = dict(_status)
    st["commands"] = _commands.status()
    return st


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts() -> str:
    return _now().isoformat(timespec="seconds")


def _parse_iso(s: str) -> datetime:
    # ISO with offset (e.g. "2026-05-08T13:38:14+00:00") or without — both fine.
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _push_error(rule: str, error: str) -> None:
    _status["last_errors"].insert(0, {"ts": _ts(), "rule": rule, "error": error})
    _status["last_errors"] = _status["last_errors"][:10]


def _duration(spec: dict[str, Any]) -> timedelta | None:
    """``{"days": 1, "hours": 2, "minutes": 30}`` → timedelta; None if no field set."""
    parts = {k: spec.get(k) for k in ("days", "hours", "minutes")}
    if all(v is None for v in parts.values()):
        return None
    return timedelta(**{k: float(v) for k, v in parts.items() if v is not None})


def _duration_errors(spec: dict[str, Any], where: str) -> list[str]:
    errs = []
    present = [k for k in ("days", "hours", "minutes") if spec.get(k) is not None]
    for k in present:
        v = spec[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            errs.append(f"{where}.{k} must be a non-negative number")
    return errs


def _project_filter(rule: dict[str, Any]) -> list[str] | None:
    """Projects a rule applies to (rule-level ``project_id`` wins over the
    trigger's); None = all projects."""
    raw = rule.get("project_id")
    if raw is None:
        raw = (rule.get("trigger") or {}).get("project_id")
    if raw is None or raw == "" or raw == []:
        return None
    return [raw] if isinstance(raw, str) else [str(x) for x in raw]


def _project_errors(rule: dict[str, Any], name: str) -> list[str]:
    errs = []
    for where, raw in (("project_id", rule.get("project_id")),
                       ("trigger.project_id", (rule.get("trigger") or {}).get("project_id"))):
        if raw is None or isinstance(raw, str):
            continue
        if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
            errs.append(f"{name}: {where} must be a slug or a list of slugs")
    return errs


def rule_key(rule: dict[str, Any]) -> str:
    """Stable identity of a rule (rule_firings, run counters, command queues):
    an explicit ``id``, else a hash of name, project, trigger and action.
    Editing only the action's comment keeps the key; any other change makes
    it a new rule."""
    if rule.get("id"):
        return str(rule["id"])
    action = {k: v for k, v in (rule.get("action") or {}).items() if k != "comment"}
    basis = json.dumps(
        {"name": rule.get("name"), "project_id": rule.get("project_id"),
         "trigger": rule.get("trigger"), "action": action},
        sort_keys=True, ensure_ascii=False, default=str,
    )
    return "h:" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]


def _validate_rule(rule: dict[str, Any], idx: int) -> list[str]:
    errs: list[str] = []
    if not isinstance(rule, dict):
        return [f"#{idx}: rule must be an object"]
    name = rule.get("name", f"#{idx}")
    if "trigger" not in rule or not isinstance(rule["trigger"], dict):
        errs.append(f"{name}: missing 'trigger' object")
        return errs
    if "action" not in rule or not isinstance(rule["action"], dict):
        errs.append(f"{name}: missing 'action' object")
        return errs
    t = rule["trigger"]
    a = rule["action"]
    if t.get("type") not in VALID_TRIGGERS:
        errs.append(f"{name}: trigger.type must be one of {sorted(VALID_TRIGGERS)}")
    else:
        if t["type"] == "task_idle":
            if t.get("status") not in STATUSES:
                errs.append(f"{name}: trigger.status invalid")
            if _duration(t) is None:
                errs.append(f"{name}: task_idle needs days, hours or minutes")
            errs.extend(_duration_errors(t, f"{name}: trigger"))
        elif t["type"] == "task_count_in_status":
            if t.get("status") not in STATUSES:
                errs.append(f"{name}: trigger.status invalid")
            if "gt" not in t and "lt" not in t:
                errs.append(f"{name}: trigger needs 'gt' or 'lt'")
        elif t["type"] == "blockers_done":
            if t.get("status", "backlog") not in STATUSES:
                errs.append(f"{name}: trigger.status invalid")
            rs = t.get("resolved_statuses")
            if rs is not None and (not isinstance(rs, list) or not rs
                                   or any(x not in STATUSES for x in rs)):
                errs.append(f"{name}: trigger.resolved_statuses must be a list of statuses")
        elif t["type"] == "task_moved":
            # to_status is required, from_status and project_id are optional
            if t.get("to_status") not in STATUSES:
                errs.append(f"{name}: task_moved.to_status invalid")
            if t.get("from_status") is not None and t.get("from_status") not in STATUSES:
                errs.append(f"{name}: task_moved.from_status invalid")
    if a.get("type") not in VALID_ACTIONS:
        errs.append(f"{name}: action.type must be one of {sorted(VALID_ACTIONS)}")
    else:
        if a["type"] == "move_to" and a.get("status") not in STATUSES:
            errs.append(f"{name}: action.status invalid")
        if a["type"] == "add_comment" and not a.get("comment"):
            errs.append(f"{name}: action.comment required")
        if a["type"] == "set_priority" and a.get("priority") not in VALID_PRIORITIES:
            errs.append(f"{name}: action.priority must be one of {sorted(VALID_PRIORITIES)}")
        if a["type"] == "assign" and "assignee" not in a:
            errs.append(f"{name}: action.assignee required (null to clear)")
        if a["type"] == "run_command":
            if not a.get("cmd"):
                errs.append(f"{name}: run_command.cmd required (path to executable)")
            if "args" in a and not isinstance(a["args"], list):
                errs.append(f"{name}: run_command.args must be a list")
            if "env" in a and not isinstance(a["env"], dict):
                errs.append(f"{name}: run_command.env must be an object")
            for opt in ("max_concurrent", "max_runs"):
                v = a.get(opt)
                if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < 1):
                    errs.append(f"{name}: run_command.{opt} must be a positive integer")
    errs.extend(_project_errors(rule, name))
    rep = rule.get("repeat_every")
    if rep is not None:
        if not isinstance(rep, dict) or _duration(rep) is None:
            errs.append(f"{name}: repeat_every must be an object with days/hours/minutes")
        else:
            errs.extend(_duration_errors(rep, f"{name}: repeat_every"))
    wip = rule.get("wip_limit")
    if wip is not None and t.get("type") in REACTIVE_TRIGGERS:
        errs.append(f"{name}: wip_limit works with polling triggers only — for "
                    "task_moved + run_command use action.max_concurrent")
    elif wip is not None:
        if (not isinstance(wip, dict) or not isinstance(wip.get("statuses"), list)
                or not wip["statuses"]
                or any(s not in STATUSES for s in wip["statuses"])
                or isinstance(wip.get("max"), bool)
                or not isinstance(wip.get("max"), int) or wip["max"] < 0):
            errs.append(f"{name}: wip_limit must be {{\"statuses\": [...], \"max\": N}}")
    return errs


def _load_rules(path: Path) -> tuple[list[dict[str, Any]], list[str], bool]:
    """Returns (enabled valid rules, errors, paused).

    An invalid rule is skipped and reported; the other rules keep working.
    """
    if not path.exists():
        return [], [], False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return [], [f"rules.json invalid JSON: {e}"], False
    if not isinstance(data, dict):
        return [], ["rules.json: top level must be an object"], False
    rules = data.get("rules", [])
    paused = bool(data.get("paused", False))
    if not isinstance(rules, list):
        return [], ["rules.json: 'rules' must be a list"], paused
    errs: list[str] = []
    valid: list[dict[str, Any]] = []
    for i, r in enumerate(rules):
        rule_errs = _validate_rule(r, i)
        if rule_errs:
            errs.extend(rule_errs)
            continue
        if r.get("enabled", True):
            valid.append(r)
    return valid, errs, paused


def set_paused(path: Path, paused: bool) -> None:
    """Flip the top-level ``paused`` flag in rules.json, keeping everything else."""
    data: dict[str, Any] = {"rules": []}
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("rules.json: top level must be an object")
    data["paused"] = bool(paused)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    if _engine is not None and _engine.rules_file == path:
        _engine._mtime = None
        _engine._maybe_reload()


def _tasks_in(store: Store, status: Any, projects: list[str] | None) -> list[Any]:
    if projects is None:
        return store.list_tasks(status=status)
    out: list[Any] = []
    for pid in projects:
        out.extend(store.list_tasks(status=status, project_id=pid))
    return out


def _wip_room(store: Store, rule: dict[str, Any]) -> int | None:
    """How many more tasks the rule may act on under its wip_limit (None = unlimited)."""
    wip = rule.get("wip_limit")
    if not wip:
        return None
    busy = len(_tasks_in(store, wip["statuses"], _project_filter(rule)))
    return max(0, wip["max"] - busy)


def _matching_tasks(store: Store, rule: dict[str, Any]) -> list[Any]:
    t = rule["trigger"]
    status = t.get("status", "backlog")
    tasks = _tasks_in(store, status, _project_filter(rule))
    if t["type"] == "task_idle":
        cutoff = _now() - (_duration(t) or timedelta(0))
        return [task for task in tasks if _parse_iso(task.moved_at) < cutoff]
    elif t["type"] == "task_count_in_status":
        n = len(tasks)
        if "gt" in t and n > t["gt"]:
            return tasks
        if "lt" in t and n < t["lt"]:
            return tasks
        return []
    elif t["type"] == "blockers_done":
        resolved = set(t.get("resolved_statuses") or RESOLVED_STATUSES)
        ready = []
        for task in tasks:
            if not task.blockers:
                continue
            states = [store.get_task(b, history_limit=0) for b in task.blockers]
            if all(b is not None and b.status in resolved for b in states):
                ready.append(task)
        return sorted(ready, key=lambda x: (x.column_order, x.id))
    return []


def _should_fire(store: Store, rule: dict[str, Any], key: str, task: Any) -> bool:
    last = store.rule_last_fired(key, task.id, task.moved_at)
    if last is None:
        return True
    rep = _duration(rule["repeat_every"]) if rule.get("repeat_every") else None
    if rep is None:
        return False
    return _now() - _parse_iso(last) >= rep


def _task_context(task: Any, **extra: Any) -> dict[str, Any]:
    ctx = {
        "task_id": task.id,
        "title": task.title,
        "project_id": task.project_id,
        "status": task.status,
        "from_status": "",
        "to_status": task.status,
    }
    ctx.update({k: v for k, v in extra.items() if v is not None})
    return ctx


def _apply_action(
    store: Store,
    task_id: str,
    action: dict[str, Any],
    context: dict[str, Any] | None = None,
    *,
    rule: dict[str, Any] | None = None,
) -> tuple[str, bool]:
    """Applies an action. Returns (short description for the log, settled).

    ``settled`` is False only when a run_command launch is waiting in the
    queue — a polling rule then does not mark the task as handled yet, so a
    launch lost to a server restart is retried on a later tick.

    ``context`` is an optional dict with values used to substitute
    run_command placeholders (task_id, project_id, from_status,
    to_status, title, status).
    """
    if action["type"] == "run_command":
        return _submit_command(store, task_id, action, context, rule)
    return _apply_simple_action(store, task_id, action), True


def _apply_simple_action(store: Store, task_id: str, action: dict[str, Any]) -> str:
    a_type = action["type"]
    comment = action.get("comment", "")
    if a_type == "move_to":
        task = store.get_task(task_id, history_limit=0)
        if task is not None and task.status == action["status"]:
            return f"move_to {action['status']}: already there"
        store.move_task(
            task_id,
            action["status"],
            actor="automation",
            comment=comment or None,
        )
        return f"move_to {action['status']}"
    elif a_type == "add_comment":
        store.add_comment(task_id, comment, actor="automation")
        return f"comment: {comment[:40]}"
    elif a_type == "set_priority":
        task = store.get_task(task_id, history_limit=0)
        if task is not None and task.priority == action["priority"]:
            return f"priority already {action['priority']}"
        store.update_fields(
            task_id,
            actor="automation",
            priority=action["priority"],
        )
        if comment:
            store.add_comment(task_id, comment, actor="automation")
        return f"priority -> {action['priority']}"
    elif a_type == "assign":
        before = store.get_task(task_id, history_limit=0)
        after = store.assign_task(task_id, action.get("assignee"), actor="automation")
        if before is not None and before.assignee == after.assignee:
            return f"assignee already {after.assignee or '—'}"
        if comment:
            store.add_comment(task_id, comment, actor="automation")
        return f"assignee -> {after.assignee or '—'}"
    elif a_type == "archive":
        before = store.get_task(task_id, history_limit=0)
        if before is not None and before.archived_at:
            return "already archived"
        store.archive_task(task_id, archived=True, actor="automation", comment=comment or None)
        return "archived"
    return f"unknown action {a_type}"


def _submit_command(
    store: Store, task_id: str, action: dict[str, Any],
    context: dict[str, Any] | None, rule: dict[str, Any] | None,
) -> tuple[str, bool]:
    # context is substituted into args as {task_id}, {project_id},
    # {from_status}, {to_status}, {title}, {status}.
    ctx = dict(context or {})
    ctx.setdefault("task_id", task_id)
    raw_args = action.get("args", [])
    try:
        args = [str(a).format(**ctx) for a in raw_args]
    except (KeyError, IndexError, ValueError) as e:
        return f"run_command: bad placeholder {e}", True
    name = (rule or {}).get("name", "run_command")
    key = rule_key(rule) if rule else f"cmd:{action['cmd']}"
    return _commands.submit(
        store=store,
        key=key,
        rule_name=name,
        task_id=task_id,
        cmd=action["cmd"],
        args=args,
        env_extra=action.get("env") or {},
        log_file=action.get("log_file") or _default_log_file(name),
        max_concurrent=action.get("max_concurrent"),
        max_runs=action.get("max_runs"),
        ctx=ctx,
    )


def _default_log_file(rule_name: str) -> str:
    """Commands without an explicit log_file still log somewhere: a silent
    death with output sent to /dev/null is the hardest failure to debug."""
    base = Path(os.environ.get("KANBAN_LOG_DIR") or (
        _engine.rules_file.parent / "logs" if _engine else Path("kanban_data/logs")
    ))
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", rule_name).strip("-")[:60] or "run_command"
    return str(base / f"{slug}.log")


# ---------------------------------------------------------------------------
# run_command: spawning, de-duplication, concurrency
# ---------------------------------------------------------------------------


class CommandRunner:
    """Spawns run_command processes.

    * One process per (rule, task) at a time — a second trigger while the
      first launch is still alive is skipped (no duplicate agents).
    * ``max_concurrent`` per rule: extra launches wait in a FIFO queue and
      start when a slot frees up.
    * ``max_runs`` per (rule, task): after that many launches the task is
      moved to ``blocked`` instead of launching again — a guard against
      pipelines that bounce a card between "agent" and "checks failed" all
      night. Moving the task out of ``blocked`` resets the counter.
    * Exit code 75 (EX_TEMPFAIL, e.g. the model's usage limit was hit) does
      not count as a run.
    * Processes start in their own session, so restarting the kanban
      server (or launchd killing its process group) does not kill agents.
    * The child gets a UTF-8 locale when the server has none (launchd
      starts services with an empty LANG).
    * Queued and running jobs are kept in the ``command_jobs`` table: after a
      server restart queued launches are resumed and agents still running
      keep their slot (tracked by pid) instead of being forgotten.
    """

    def __init__(self) -> None:
        self._running: dict[tuple[str, str], dict[str, Any]] = {}
        self._queues: dict[str, deque[dict[str, Any]]] = {}
        self._limits: dict[str, int | None] = {}
        self.history: list[dict[str, Any]] = []   # last finished: {ts, rule, task_id, pid, rc}

    def _running_for(self, key: str) -> int:
        return sum(1 for (k, _t) in self._running if k == key)

    def submit(self, *, store: Store | None, key: str, rule_name: str, task_id: str,
               cmd: str, args: list[str], env_extra: dict[str, Any], log_file: str,
               max_concurrent: int | None, max_runs: int | None,
               ctx: dict[str, Any]) -> tuple[str, bool]:
        """Start or queue a launch. Returns (description, settled) — see
        _apply_action; settled is False while the job waits in the queue."""
        job = {"key": key, "rule": rule_name, "task_id": task_id, "cmd": cmd,
               "args": args, "env": env_extra, "log_file": log_file, "ctx": ctx,
               "queued_at": _ts(), "store": store}
        self._limits[key] = max_concurrent
        name = Path(cmd).name
        if (key, task_id) in self._running:
            return f"run_command {name}: already running for {task_id}", True
        queue = self._queues.setdefault(key, deque())
        if any(j["task_id"] == task_id for j in queue):
            return f"run_command {name}: already queued for {task_id}", False
        if store is not None and max_runs is not None:
            runs = store.command_runs(key, task_id)
            if runs >= max_runs:
                store.move_task(
                    task_id, "blocked", actor="automation",
                    comment=(f"'{rule_name}' already ran {runs} times for this task "
                             f"(max_runs={max_runs}); not launching again. "
                             "Move the card out of Blocked to allow new runs."),
                )
                return f"run_command {name}: max_runs={max_runs} reached, task blocked", True
        if _status.get("paused") or (
            max_concurrent is not None and self._running_for(key) >= max_concurrent
        ):
            queue.append(job)
            self._persist(job, "queued")
            return f"run_command {name} queued ({len(queue)} waiting)", False
        self._start(job)
        return f"run_command {name} {args}", True

    @staticmethod
    def _persist(job: dict[str, Any], state: str, pid: int | None = None) -> None:
        st = job.get("store")
        if st is None:
            return
        try:
            st.save_command_job(job["key"], job["task_id"], state=state,
                                rule_name=job["rule"], ctx=job["ctx"], pid=pid)
        except Exception:  # noqa: BLE001 — bookkeeping only
            log.exception("run_command: could not persist job state")

    @staticmethod
    def _forget(job: dict[str, Any]) -> None:
        st = job.get("store")
        if st is None:
            return
        try:
            st.delete_command_job(job["key"], job["task_id"])
        except Exception:  # noqa: BLE001 — bookkeeping only
            log.exception("run_command: could not clear job state")

    def _start(self, job: dict[str, Any]) -> None:
        # Raises RuntimeError outside an event loop — before anything is reserved.
        asyncio.get_running_loop().create_task(self._run(job))
        # Reserve the slot synchronously (the coroutine hasn't run yet) so a
        # burst of events can't overshoot max_concurrent.
        self._running[(job["key"], job["task_id"])] = {**job, "pid": None, "started_at": _ts()}
        if job.get("store") is not None:
            job["store"].bump_command_runs(job["key"], job["task_id"])

    def restore(self, store: Store, rules: list[dict[str, Any]]) -> None:
        """Pick up jobs a previous server process left in ``command_jobs``."""
        by_key = {rule_key(r): r for r in rules}
        for row in store.list_command_jobs():
            key, task_id = row["rule_key"], row["task_id"]
            if (key, task_id) in self._running:
                continue
            rule = by_key.get(key)
            if row["state"] == "running":
                pid = row["pid"]
                if not pid or not _pid_alive(pid):
                    store.delete_command_job(key, task_id)
                    continue
                if rule is not None:
                    self._limits[key] = rule["action"].get("max_concurrent")
                job = {"key": key, "rule": row["rule_name"], "task_id": task_id,
                       "cmd": "(started before restart)", "args": [], "ctx": row["ctx"],
                       "queued_at": row["queued_at"], "store": store}
                self._running[(key, task_id)] = {**job, "pid": pid,
                                                 "started_at": row["started_at"]}
                asyncio.get_running_loop().create_task(self._watch(job, pid))
                log.info("run_command: re-attached to pid %s for %s (%s)",
                         pid, task_id, row["rule_name"])
                continue
            task = store.get_task(task_id, history_limit=0)
            if rule is None or task is None or task.archived_at:
                store.delete_command_job(key, task_id)
                continue
            store.delete_command_job(key, task_id)
            desc, _settled = _apply_action(store, task_id, rule["action"], row["ctx"], rule=rule)
            log.info("run_command: resumed queued launch for %s: %s", task_id, desc)

    async def _watch(self, job: dict[str, Any], pid: int) -> None:
        """Follow a process that is not our child (started before a restart)."""
        try:
            while _pid_alive(pid):
                await asyncio.sleep(2)
        finally:
            self._forget(job)
            self._running.pop((job["key"], job["task_id"]), None)
            self.history.insert(0, {"ts": _ts(), "rule": job["rule"],
                                    "task_id": job["task_id"], "pid": pid, "rc": None})
            self.history = self.history[:20]
            self.drain(job["key"])

    async def _run(self, job: dict[str, Any]) -> None:
        slot = (job["key"], job["task_id"])
        rc: int | None = None
        pid: int | None = None
        fh = None
        try:
            log_path = Path(job["log_file"]).expanduser()
            log_path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(log_path, "ab")
            env = dict(os.environ)
            if not env.get("LANG") and not env.get("LC_ALL"):
                env["LANG"] = "en_US.UTF-8"
            env.update({str(k): str(v) for k, v in job["env"].items()})
            proc = await asyncio.create_subprocess_exec(
                job["cmd"], *job["args"],
                stdin=asyncio.subprocess.DEVNULL,
                stdout=fh, stderr=asyncio.subprocess.STDOUT,
                env=env,
                start_new_session=True,
            )
            pid = proc.pid
            self._running[slot]["pid"] = pid
            self._persist(job, "running", pid)
            log.info("run_command spawned: pid=%d cmd=%s args=%s ctx=%s",
                     pid, job["cmd"], job["args"], job["ctx"])
            rc = await proc.wait()
            if rc == EX_TEMPFAIL:
                log.info("run_command %s for %s: temporary failure (75), not counted",
                         job["cmd"], job["task_id"])
            elif rc != 0:
                _push_error(job["rule"], f"{job['task_id']}: {Path(job['cmd']).name} exited {rc}")
        except FileNotFoundError:
            log.error("run_command: cmd not found: %s", job["cmd"])
            _push_error(job["rule"], f"executable not found: {job['cmd']}")
        except PermissionError:
            log.error("run_command: not executable: %s", job["cmd"])
            _push_error(job["rule"], f"not executable (chmod +x?): {job['cmd']}")
        except Exception as e:  # noqa: BLE001 — report, never kill the engine
            log.exception("run_command failed: %s", job["cmd"])
            _push_error(job["rule"], f"{job['cmd']}: {e}")
        finally:
            if fh is not None:
                fh.close()
            st = job.get("store")
            if st is not None:
                try:
                    if rc == EX_TEMPFAIL or pid is None:
                        st.bump_command_runs(job["key"], job["task_id"], -1)
                    st.set_command_rc(job["key"], job["task_id"], rc)
                except Exception:  # noqa: BLE001 — bookkeeping only
                    log.exception("run_command bookkeeping failed")
            self._forget(job)
            self._running.pop(slot, None)
            self.history.insert(0, {"ts": _ts(), "rule": job["rule"],
                                    "task_id": job["task_id"], "pid": pid, "rc": rc})
            self.history = self.history[:20]
            self.drain(job["key"])

    def drain(self, key: str | None = None) -> None:
        """Start queued jobs while slots are free (and automation isn't paused)."""
        if _status.get("paused"):
            return
        keys = [key] if key is not None else list(self._queues)
        for k in keys:
            queue = self._queues.get(k)
            limit = self._limits.get(k)
            while queue and (limit is None or self._running_for(k) < limit):
                job = queue[0]
                if (job["key"], job["task_id"]) not in self._running:
                    self._start(job)
                queue.popleft()

    def status(self) -> dict[str, Any]:
        return {
            "running": [
                {"rule": j["rule"], "task_id": j["task_id"], "pid": j.get("pid"),
                 "started_at": j.get("started_at")}
                for j in self._running.values()
            ],
            "queued": [
                {"rule": j["rule"], "task_id": j["task_id"], "queued_at": j["queued_at"]}
                for q in self._queues.values() for j in q
            ],
            "finished": list(self.history),
        }


EX_TEMPFAIL = 75  # sysexits.h: "try again later" (rate limit, service down)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True

_commands = CommandRunner()


# ---------------------------------------------------------------------------
# Polling run
# ---------------------------------------------------------------------------


def _run_once(store: Store, rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    actions_log: list[dict[str, Any]] = []
    for rule in rules:
        # reactive rules do not run on the timer — only through emit_rule_event
        if rule["trigger"]["type"] in REACTIVE_TRIGGERS:
            continue
        name = rule.get("name", "?")
        key = rule_key(rule)
        try:
            tasks = _matching_tasks(store, rule)
            room = _wip_room(store, rule)
        except Exception as e:
            log.exception("rule '%s' selection failed", name)
            _push_error(name, str(e))
            continue
        for task in tasks:
            if room is not None and room <= 0:
                break
            try:
                if not _should_fire(store, rule, key, task):
                    continue
                desc, settled = _apply_action(
                    store, task.id, rule["action"], _task_context(task), rule=rule
                )
                # Remember the episode even when the action was a no-op
                # ("priority already high"): re-checking every tick is what
                # used to flood the history. A launch still waiting in the
                # queue is not remembered yet, so it is retried if lost.
                if settled:
                    store.record_rule_firing(key, task.id, task.moved_at)
                if room is not None:
                    room -= 1
                actions_log.append(
                    {
                        "ts": _ts(),
                        "rule": name,
                        "task_id": task.id,
                        "action": desc,
                    }
                )
                log.info("rule '%s' applied to %s: %s", name, task.id, desc)
            except Exception as e:
                log.exception("rule '%s' action on %s failed", name, task.id)
                _push_error(name, f"{task.id}: {e}")
    return actions_log


_engine: "RuleEngine | None" = None


def emit_rule_event(event: str, payload: dict[str, Any]) -> list[str]:
    """Reactive event handling (invoked by the event feed).

    Applies all enabled rules with trigger.type=event whose filters match
    the payload. Currently ``task_moved`` is supported:
        payload = {"task": {...}, "from_status": "...", "to_status": "...",
                   "comment": "..." | None, "actor": "..."}
    Returns short descriptions of the applied actions.
    """
    if _engine is None:
        return []
    _engine._maybe_reload()
    if _status.get("paused"):
        return []
    rules = _engine._rules
    task = payload.get("task") or {}
    task_id = task.get("id")
    if not task_id:
        return []
    applied: list[str] = []
    for rule in rules:
        trig = rule["trigger"]
        if trig["type"] != event:
            continue
        if event == "task_moved":
            if trig.get("to_status") and trig["to_status"] != payload.get("to_status"):
                continue
            if trig.get("from_status") and trig["from_status"] != payload.get("from_status"):
                continue
            projects = _project_filter(rule)
            if projects is not None and task.get("project_id") not in projects:
                continue
            name = rule.get("name", "?")
            if not _engine._loop_guard(rule_key(rule), task_id):
                _push_error(name, f"{task_id}: fired {LOOP_LIMIT}+ times in "
                                  f"{int(LOOP_WINDOW.total_seconds() // 60)} min — "
                                  "looks like a rule loop, skipped")
                continue
            ctx = {
                "task_id":     task_id,
                "title":       task.get("title", ""),
                "project_id":  task.get("project_id", ""),
                "status":      payload.get("to_status", ""),
                "from_status": payload.get("from_status", "") or "",
                "to_status":   payload.get("to_status", ""),
            }
            try:
                desc, _settled = _apply_action(_engine.store, task_id, rule["action"], ctx, rule=rule)
                applied.append(desc)
                _status["last_reactive"].insert(0, {
                    "ts": _ts(),
                    "rule": name,
                    "event": event,
                    "task_id": task_id,
                    "actor": payload.get("actor"),
                    "action": desc,
                })
                _status["last_reactive"] = _status["last_reactive"][:20]
                log.info("reactive rule '%s' on %s: %s", name, task_id, desc)
            except Exception as e:
                log.exception("reactive rule '%s' failed", name)
                _push_error(name, f"{task_id}: {e}")
    return applied


class RuleEngine:
    """Async loop that runs polling-rules every interval_sec seconds.

    Reactive rules (task_moved) are handled through emit_rule_event(),
    which the event feed invokes for every new move in task_history.
    """

    def __init__(self, store: Store, rules_file: Path, interval: float = DEFAULT_INTERVAL):
        global _engine
        _engine = self
        self.store = store
        self.rules_file = rules_file
        self.interval = interval
        self._stop = asyncio.Event()
        self._rules: list[dict[str, Any]] = []
        self._mtime: float | None = None
        self._recent: dict[tuple[str, str], deque[datetime]] = {}

    def _loop_guard(self, key: str, task_id: str) -> bool:
        now = _now()
        q = self._recent.setdefault((key, task_id), deque())
        while q and now - q[0] > LOOP_WINDOW:
            q.popleft()
        if len(q) >= LOOP_LIMIT:
            return False
        q.append(now)
        return True

    def _maybe_reload(self) -> None:
        if not self.rules_file.exists():
            self._rules = []
            self._mtime = None
            _status["rules_loaded"] = 0
            _status["config_mtime"] = None
            _status["paused"] = False
            return
        mtime = self.rules_file.stat().st_mtime
        if mtime == self._mtime:
            return
        rules, errs, paused = _load_rules(self.rules_file)
        for e in errs:
            _push_error("_config_", e)
        was_paused = _status.get("paused")
        self._rules = rules
        self._mtime = mtime
        _status["paused"] = paused
        _status["rules_loaded"] = len(rules)
        _status["config_mtime"] = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(timespec="seconds")
        log.info("rules.json reloaded: %d active rules (errors: %d)%s",
                 len(rules), len(errs), " — PAUSED" if paused else "")
        for e in errs:
            log.warning("rules.json: %s", e)
        if was_paused and not paused:
            try:
                _commands.drain()
            except RuntimeError:
                pass  # no running loop (sync caller); the next tick drains

    async def run(self) -> None:
        _status["running"] = True
        _status["rules_file"] = str(self.rules_file)
        _status["interval_sec"] = self.interval
        log.info("rule engine started: %s (interval=%ss)", self.rules_file, self.interval)
        try:
            self._maybe_reload()
            _commands.restore(self.store, self._rules)
        except Exception:
            log.exception("rule engine: could not restore command jobs")
        try:
            while not self._stop.is_set():
                try:
                    self._maybe_reload()
                    if self._rules and not _status.get("paused"):
                        actions = _run_once(self.store, self._rules)
                        if actions:
                            _status["last_run_actions"] = actions[-20:]
                    _commands.drain()
                except Exception:
                    log.exception("rule engine: run failed")
                _status["last_run_at"] = _ts()
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
                except asyncio.TimeoutError:
                    pass
        finally:
            _status["running"] = False
            log.info("rule engine stopped")

    def stop(self) -> None:
        self._stop.set()
