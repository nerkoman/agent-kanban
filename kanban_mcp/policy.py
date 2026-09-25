"""Guard rails that apply to AI agents, whichever transport they use
(stdio MCP in ``server.py``, HTTP MCP bridged onto the REST API in
``kanban_ui/main.py``)."""
from __future__ import annotations

import os


def human_only() -> set[str]:
    """Statuses agents may not move or create cards into (``KANBAN_MCP_HUMAN_ONLY``,
    default ``done``: closing a card is the human's call after review)."""
    raw = os.environ.get("KANBAN_MCP_HUMAN_ONLY", "done")
    return {x.strip() for x in raw.split(",") if x.strip()}


def check_agent_may_use(status: str) -> str | None:
    """Error text if agents may not put cards into ``status``, else None."""
    reserved = human_only()
    if status in reserved:
        return (
            f"'{status}' is reserved for a human (KANBAN_MCP_HUMAN_ONLY="
            f"{','.join(sorted(reserved))}). Move the card to 'testing' with a "
            "comment describing what was done and how it was verified; the human "
            "closes it after review."
        )
    return None
