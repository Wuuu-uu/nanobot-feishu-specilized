"""Session sync helper for autonomous cron worker sessions."""

from __future__ import annotations

import json
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.context import RequestContext
from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema
from nanobot.session.goal_state import goal_state_raw, parse_goal_state
from nanobot.session.manager import Session, SessionManager
from nanobot.utils.helpers import truncate_string


@dataclass(frozen=True)
class SessionSyncContext:
    """Cron worker sync scope supplied by the gateway cron bridge."""

    context_session_key: str
    sync_session_key: str
    worker_session_key: str
    last_sync_message_timestamp: str | None = None
    sync_max_messages: int = 12
    sync_max_chars: int = 12000


@tool_parameters(
    tool_parameters_schema(
        action=StringSchema(
            "Action to perform. Use check for a lightweight cursor/status view, "
            "or fetch_updates to read limited non-tool main-session messages since "
            "last_sync_message_timestamp.",
            enum=["check", "fetch_updates"],
        ),
        required=["action"],
    )
)
class SessionSyncTool(Tool):
    """Read limited main-session updates for cron worker sessions."""

    def __init__(self, sessions: SessionManager):
        self._sessions = sessions
        self._request_ctx: ContextVar[RequestContext | None] = ContextVar(
            "session_sync_request_ctx",
            default=None,
        )
        self._sync_ctx: ContextVar[SessionSyncContext | None] = ContextVar(
            "session_sync_ctx",
            default=None,
        )

    @property
    def name(self) -> str:
        return "session_sync"

    @property
    def description(self) -> str:
        return (
            "For cron worker sessions only: check or fetch limited user/assistant "
            "updates from the linked main session since the last tagged cron sync "
            "timestamp. Tool messages and empty tool-call assistant records are omitted."
        )

    @property
    def read_only(self) -> bool:
        return True

    def set_context(self, ctx: RequestContext) -> None:
        self._request_ctx.set(ctx)

    def set_sync_context(self, ctx: SessionSyncContext):
        return self._sync_ctx.set(ctx)

    def reset_sync_context(self, token) -> None:
        self._sync_ctx.reset(token)

    async def execute(self, action: str, **kwargs: Any) -> str:
        ctx = self._sync_ctx.get()
        if ctx is None:
            return "Error: session_sync is only available inside agent_turn_with_context cron jobs."

        if action == "check":
            return self._check(ctx)
        if action == "fetch_updates":
            return self._fetch_updates(ctx)
        return f"Unknown action: {action}"

    def _load_session(self, key: str) -> Session | None:
        if not key:
            return None
        if not self._sessions.session_exists(key):
            return None
        return self._sessions.get_or_create(key)

    def _base_status(self, ctx: SessionSyncContext, source: Session | None) -> dict[str, Any]:
        latest_ts = None
        if source and source.messages:
            latest_ts = next(
                (
                    str(m.get("timestamp"))
                    for m in reversed(source.messages)
                    if isinstance(m, dict) and m.get("timestamp")
                ),
                None,
            )
        goal = parse_goal_state(goal_state_raw(source.metadata if source else None))
        active_goal = goal if isinstance(goal, dict) and goal.get("status") == "active" else None
        return {
            "worker_session_key": ctx.worker_session_key,
            "context_session_key": ctx.context_session_key,
            "sync_session_key": ctx.sync_session_key,
            "source_session_key": ctx.sync_session_key or ctx.context_session_key,
            "last_sync_message_timestamp": ctx.last_sync_message_timestamp,
            "source_exists": source is not None,
            "source_message_count": len(source.messages) if source else 0,
            "latest_source_timestamp": latest_ts,
            "active_goal": {
                "objective": str(active_goal.get("objective") or ""),
                "ui_summary": str(active_goal.get("ui_summary") or ""),
            } if active_goal else None,
        }

    def _cursor_index(self, session: Session, timestamp: str | None) -> tuple[int | None, str]:
        if not timestamp:
            return None, "never_synced"
        for idx, message in enumerate(session.messages):
            if isinstance(message, dict) and message.get("timestamp") == timestamp:
                return idx, "matched"
        return None, "missing"

    @staticmethod
    def _message_content(message: dict[str, Any]) -> str:
        content = message.get("content", "")
        if isinstance(content, str):
            return content
        try:
            return json.dumps(content, ensure_ascii=False)
        except TypeError:
            return str(content)

    def _format_message(self, message: dict[str, Any]) -> str | None:
        role = str(message.get("role") or "")
        if role == "tool":
            return None
        content = self._message_content(message).strip()
        if not content:
            return None
        if role == "assistant" and message.get("tool_calls"):
            return None
        timestamp = str(message.get("timestamp") or "unknown-time")
        delivery = " proactive_delivery" if message.get("_channel_delivery") else ""
        return f"- [{timestamp}] {role}{delivery}: {content}"

    def _limited_messages(
        self,
        messages: list[dict[str, Any]],
        *,
        max_messages: int,
        max_chars: int,
    ) -> tuple[list[str], int, bool]:
        formatted = [line for msg in messages if (line := self._format_message(msg))]
        dropped_for_role = len(messages) - len(formatted)
        selected = formatted[-max_messages:]
        dropped = dropped_for_role + max(0, len(formatted) - len(selected))

        out_reversed: list[str] = []
        used = 0
        truncated = False
        for line in reversed(selected):
            remaining = max_chars - used
            if remaining <= 0:
                truncated = True
                break
            candidate = line
            if len(candidate) > remaining:
                candidate = truncate_string(candidate, remaining)
                truncated = True
            out_reversed.append(candidate)
            used += len(candidate)
        return list(reversed(out_reversed)), dropped, truncated

    def _check(self, ctx: SessionSyncContext) -> str:
        source = self._load_session(ctx.sync_session_key or ctx.context_session_key)
        status = self._base_status(ctx, source)
        if source is None:
            status["cursor_status"] = "source_missing"
        else:
            _idx, cursor_status = self._cursor_index(source, ctx.last_sync_message_timestamp)
            status["cursor_status"] = cursor_status
        return json.dumps(status, ensure_ascii=False, indent=2)

    def _fetch_updates(self, ctx: SessionSyncContext) -> str:
        source = self._load_session(ctx.sync_session_key or ctx.context_session_key)
        status = self._base_status(ctx, source)
        if source is None:
            status["cursor_status"] = "source_missing"
            return json.dumps(status, ensure_ascii=False, indent=2)

        cursor_idx, cursor_status = self._cursor_index(source, ctx.last_sync_message_timestamp)
        status["cursor_status"] = cursor_status
        if cursor_idx is None:
            candidate_messages = source.messages
            status["sync_note"] = (
                "No matching last_sync_message_timestamp was found; treating this "
                "cron worker as not synchronized and returning a bounded latest tail."
            )
        else:
            candidate_messages = source.messages[cursor_idx + 1:]

        latest_ts = None
        if candidate_messages:
            latest_ts = next(
                (
                    str(m.get("timestamp"))
                    for m in reversed(candidate_messages)
                    if isinstance(m, dict) and m.get("timestamp")
                ),
                None,
            )
        lines, dropped, truncated = self._limited_messages(
            list(candidate_messages),
            max_messages=max(1, ctx.sync_max_messages),
            max_chars=max(1, ctx.sync_max_chars),
        )
        status["candidate_message_count"] = len(candidate_messages)
        status["returned_message_count"] = len(lines)
        status["dropped_or_omitted_count"] = dropped
        status["truncated"] = truncated
        status["latest_returned_timestamp"] = latest_ts
        status["messages"] = lines
        if not lines:
            status["sync_note"] = status.get("sync_note") or "No new non-tool messages since the sync timestamp."
        return json.dumps(status, ensure_ascii=False, indent=2)
