"""Session management tool for creating and switching sessions."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from nanobot.agent.tools.base import Tool
from nanobot.session.manager import SessionManager
from nanobot.utils.helpers import truncate_string


class SessionManageTool(Tool):
    """Tool to create, switch, and list sessions for the current chat."""

    def __init__(self, manager: SessionManager):
        self._sessions = manager
        self._channel = ""
        self._chat_id = ""

    def set_context(self, channel: str, chat_id: str) -> None:
        """Set current channel/chat context."""
        self._channel = channel
        self._chat_id = chat_id

    @property
    def name(self) -> str:
        return "session_manage"

    @property
    def description(self) -> str:
        return (
            "Manage conversation sessions. Actions: create, switch, list, current, reset. "
            "Create generates a title and can activate the new session; switch activates an existing session." 
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "switch", "list", "current", "reset"],
                    "description": "Action to perform",
                },
                "session_key": {
                    "type": "string",
                    "description": "Target session key for switch/create",
                },
                "title": {
                    "type": "string",
                    "description": "Optional title for new session",
                },
                "seed": {
                    "type": "string",
                    "description": "Optional seed text to auto-generate a title",
                },
                "activate": {
                    "type": "boolean",
                    "description": "Whether to activate the session after create",
                    "default": True,
                },
                "limit": {
                    "type": "integer",
                    "description": "Max sessions to list",
                    "default": 20,
                },
                "allow_existing": {
                    "type": "boolean",
                    "description": "Allow create to reuse an existing session key",
                    "default": False,
                },
            },
            "required": ["action"],
        }

    async def execute(
        self,
        action: str,
        session_key: str | None = None,
        title: str | None = None,
        seed: str | None = None,
        activate: bool = True,
        limit: int = 20,
        allow_existing: bool = False,
        **kwargs: Any,
    ) -> str:
        if action == "create":
            return self._create_session(session_key, title, seed, activate, allow_existing)
        if action == "switch":
            return self._switch_session(session_key)
        if action == "list":
            return self._list_sessions(limit)
        if action == "current":
            return self._current_session()
        if action == "reset":
            return self._reset_session()
        return f"Unknown action: {action}"

    def _create_session(
        self,
        session_key: str | None,
        title: str | None,
        seed: str | None,
        activate: bool,
        allow_existing: bool,
    ) -> str:
        if not self._channel or not self._chat_id:
            return "Error: no session context (channel/chat_id)"

        key = self._resolve_key_for_create(session_key)
        if not allow_existing and self._sessions.session_exists(key):
            return f"Error: session already exists: {key}"

        session = self._sessions.get_or_create(key)
        if title:
            session.metadata["title"] = title.strip()
        elif "title" not in session.metadata:
            session.metadata["title"] = self._generate_title(seed)

        self._sessions.save(session)

        if activate:
            self._sessions.set_active_session_key(self._channel, self._chat_id, key)
            return f"Created and activated session: {key} (title: {session.metadata.get('title', '')})"
        return f"Created session: {key} (title: {session.metadata.get('title', '')})"

    def _switch_session(self, session_key: str | None) -> str:
        if not self._channel or not self._chat_id:
            return "Error: no session context (channel/chat_id)"
        if not session_key:
            return "Error: session_key is required for switch"

        target_key = self._resolve_key_for_switch(session_key)
        if not target_key:
            return f"Error: session not found: {session_key}"

        self._sessions.set_active_session_key(self._channel, self._chat_id, target_key)
        title = self._sessions.get_session_title(target_key) or ""
        title_part = f" (title: {title})" if title else ""
        return f"Switched active session to: {target_key}{title_part}"

    def _list_sessions(self, limit: int) -> str:
        sessions = self._sessions.list_sessions()
        if not sessions:
            return "No sessions found."

        active = None
        if self._channel and self._chat_id:
            active = self._sessions.get_active_session_key(self._channel, self._chat_id)

        lines = []
        for idx, info in enumerate(sessions[: max(limit, 1)], start=1):
            key = info.get("key", "")
            title = info.get("title", "")
            updated = info.get("updated_at", "")
            mark = "*" if active and key == active else " "
            label = f"{idx}. {mark} {key}"
            if title:
                label += f" | {title}"
            if updated:
                label += f" | updated {updated}"
            lines.append(label)

        return "Sessions:\n" + "\n".join(lines)

    def _current_session(self) -> str:
        if not self._channel or not self._chat_id:
            return "Error: no session context (channel/chat_id)"
        active = self._sessions.get_active_session_key(self._channel, self._chat_id)
        if not active:
            return "No active session override (using default channel session)."
        title = self._sessions.get_session_title(active) or ""
        title_part = f" (title: {title})" if title else ""
        return f"Current active session: {active}{title_part}"

    def _reset_session(self) -> str:
        if not self._channel or not self._chat_id:
            return "Error: no session context (channel/chat_id)"
        self._sessions.clear_active_session_key(self._channel, self._chat_id)
        return "Cleared active session override (back to default channel session)."

    def _resolve_key_for_create(self, session_key: str | None) -> str:
        base = f"{self._channel}:{self._chat_id}"
        if session_key:
            if ":" in session_key:
                return session_key
            return f"{base}:{self._slugify(session_key)}"
        stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        return f"{base}:{stamp}"

    def _resolve_key_for_switch(self, session_key: str) -> str | None:
        if self._sessions.session_exists(session_key):
            return session_key
        if ":" not in session_key and self._channel and self._chat_id:
            candidate = f"{self._channel}:{self._chat_id}:{self._slugify(session_key)}"
            if self._sessions.session_exists(candidate):
                return candidate
        return None

    def _generate_title(self, seed: str | None) -> str:
        if seed:
            clean = seed.strip().splitlines()[0].strip()
            if clean:
                return truncate_string(clean, max_len=60)
        stamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
        return f"Session {stamp}"

    def _slugify(self, text: str) -> str:
        text = text.strip().lower()
        text = re.sub(r"[^a-z0-9\- ]", "", text)
        text = re.sub(r"\s+", "-", text)
        text = re.sub(r"-+", "-", text)
        return text.strip("-") or "session"
