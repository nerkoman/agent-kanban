"""Event feed: ``task_history`` → webhooks + reactive rules.

Every writer — REST endpoints, MCP agents, helper scripts that import the
store, the rule engine itself — records its change as a row in
``task_history``. The feed tails that table and turns each new row into an
event, so automation reacts the same way no matter who moved the card.

(Before v0.2 only moves made through the REST API reached the rule engine
and webhooks: an agent moving a card over MCP silently skipped both.)

Row → event mapping:

    create                     → task_created    (+ reactive rules, from_status=None)
    move                       → task_moved      (+ reactive rules)
    comment                    → task_commented
    update/assign/archive/...  → task_updated    (changed_fields)

Events are dispatched in history order. The feed starts at the newest row
when the server boots — changes made while the server was down are not
replayed. Bulk writers listed in ``KANBAN_EVENTS_QUIET_ACTORS`` (default
``plan-import,maintenance``) trigger neither webhooks nor rules: importing a
200-line plan must not post 200 chat messages or launch an agent for every
line that sat under ``## Approved``.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from kanban_store import Store
from kanban_store.store import TaskHistory

from .rules import emit_rule_event
from .webhooks import emit_event

log = logging.getLogger("kanban.automation.events")

EVENT_FOR_ACTION = {
    "create": "task_created",
    "move": "task_moved",
    "comment": "task_commented",
    "update": "task_updated",
    "assign": "task_updated",
    "archive": "task_updated",
    "unarchive": "task_updated",
}

# History rows included in event payloads (full history can be huge).
HISTORY_IN_PAYLOAD = 20

_status: dict[str, Any] = {
    "running": False,
    "cursor": None,
    "dispatched_total": 0,
    "last_event_at": None,
    "last_errors": [],
}


def events_status() -> dict[str, Any]:
    return dict(_status)


def _quiet_actors() -> set[str]:
    raw = os.environ.get("KANBAN_EVENTS_QUIET_ACTORS", "plan-import,maintenance")
    return {a.strip() for a in raw.split(",") if a.strip()}


def changed_fields(row: TaskHistory) -> list[str]:
    """Field names touched by an update/assign/archive history row."""
    if row.action == "assign":
        return ["assignee"]
    if row.action in ("archive", "unarchive"):
        return ["archived"]
    # update rows look like "priority: normal → high; description"
    parts = [p.strip() for p in (row.comment or "").split(";")]
    return [p.split(":", 1)[0].strip() for p in parts if p]


class EventFeed:
    def __init__(self, store: Store, interval: float | None = None):
        self.store = store
        self.interval = interval if interval is not None else float(
            os.environ.get("KANBAN_EVENTS_INTERVAL", "1")
        )
        self.cursor = store.max_history_id()
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        _status["cursor"] = self.cursor

    def kick(self) -> None:
        """Wake the feed right away (called after a write in this process)."""
        self._wake.set()

    async def dispatch_pending(self, max_rows: int = 2000) -> int:
        """Dispatch every new history row (up to ``max_rows``). Returns the count."""
        done = 0
        quiet = _quiet_actors()
        while done < max_rows:
            rows = self.store.history_since(self.cursor, limit=min(200, max_rows - done))
            if not rows:
                break
            for row in rows:
                self.cursor = row.id
                done += 1
                try:
                    await self._dispatch(row, quiet)
                except Exception as e:  # noqa: BLE001 — one bad row must not stop the feed
                    log.exception("event feed: row %s failed", row.id)
                    _status["last_errors"].insert(0, {"history_id": row.id, "error": str(e)})
                    _status["last_errors"] = _status["last_errors"][:10]
        _status["cursor"] = self.cursor
        return done

    async def _dispatch(self, row: TaskHistory, quiet: set[str]) -> None:
        event = EVENT_FOR_ACTION.get(row.action)
        if event is None:
            return
        task = self.store.get_task(row.task_id, history_limit=HISTORY_IN_PAYLOAD)
        if task is None:
            return
        project = self.store.get_project(task.project_id)
        payload: dict[str, Any] = {
            "task": task.to_public(),
            "project": project.to_public() if project else None,
            "actor": row.actor,
            "history_id": row.id,
            "ts": row.ts,
        }
        if event == "task_moved":
            payload.update(
                from_status=row.from_status, to_status=row.to_status, comment=row.comment
            )
        elif event == "task_commented":
            payload["comment"] = row.comment
        elif event == "task_updated":
            payload["changed_fields"] = changed_fields(row)
        if row.actor in quiet:
            _status["dispatched_total"] += 1
            return
        await emit_event(event, payload)
        if event == "task_moved":
            emit_rule_event("task_moved", payload)
        elif event == "task_created":
            # A card created straight into a column has "arrived" there just
            # like a moved one: rules on to_status must see it (a task an
            # agent files directly into Approved used to wait forever).
            emit_rule_event("task_moved", {
                **payload, "from_status": None, "to_status": row.to_status,
                "comment": None,
            })
        _status["dispatched_total"] += 1
        _status["last_event_at"] = row.ts

    async def run(self) -> None:
        _status["running"] = True
        log.info("event feed started at history id %s (interval=%ss)", self.cursor, self.interval)
        try:
            while not self._stop.is_set():
                # Clear before dispatching: a kick() that lands mid-dispatch
                # must trigger another pass instead of being swallowed.
                self._wake.clear()
                try:
                    await self.dispatch_pending()
                except Exception:
                    log.exception("event feed: dispatch failed")
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=self.interval)
                except asyncio.TimeoutError:
                    pass
        finally:
            _status["running"] = False

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
