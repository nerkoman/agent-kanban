"""Background automation: inbox watcher + rule engine + event feed.

Wired into the FastAPI app through the lifespan (see kanban_ui/main.py):
the inbox watcher, the rule engine and the event feed run as asyncio tasks
and are stopped on shutdown.
"""
from .inbox import InboxWatcher, inbox_status
from .rules import RuleEngine, rules_status, emit_rule_event, set_paused
from .webhooks import (
    init_dispatcher,
    shutdown_dispatcher,
    emit_event,
    webhooks_status,
)
from .events import EventFeed, events_status
from . import plan_md

__all__ = [
    "InboxWatcher",
    "RuleEngine",
    "EventFeed",
    "inbox_status",
    "rules_status",
    "events_status",
    "emit_rule_event",
    "set_paused",
    "init_dispatcher",
    "shutdown_dispatcher",
    "emit_event",
    "webhooks_status",
    "plan_md",
]
