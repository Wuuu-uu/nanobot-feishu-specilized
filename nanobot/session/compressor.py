"""Session-scoped conversation context compression."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.config.schema import ContextCompressionConfig
from nanobot.providers.base import LLMProvider
from nanobot.session.manager import Session
from nanobot.utils.helpers import safe_filename, truncate_string


class SessionContextCompressor:
    """Compress long session history into a rolling summary per session."""

    def __init__(
        self,
        provider: LLMProvider,
        sessions_dir: Path,
        config: ContextCompressionConfig,
        default_model: str,
    ) -> None:
        self.provider = provider
        self.sessions_dir = sessions_dir
        self.config = config
        self.default_model = default_model
        self._locks: dict[str, asyncio.Lock] = {}

    async def compress_if_needed(self, session: Session) -> bool:
        """Compress a session if configured thresholds are exceeded."""
        if not self.config.enabled:
            return False

        lock = self._locks.setdefault(session.key, asyncio.Lock())
        async with lock:
            return await self._compress_locked(session)

    def get_summary(self, session_key: str) -> str:
        """Load rolling summary for a session key."""
        data = self._load_summary_data(session_key)
        summary = data.get("summary", "")
        return summary if isinstance(summary, str) else ""

    async def _compress_locked(self, session: Session) -> bool:
        active_messages = self._active_messages(session)
        if len(active_messages) <= max(self.config.keep_recent_messages + 1, 2):
            return False

        now = time.time()
        last_ts = float(session.metadata.get("_context_compress_last_ts", 0) or 0)
        if now - last_ts < max(self.config.min_interval_seconds, 0):
            return False

        active_count = len(active_messages)
        estimated_tokens = self._estimate_tokens(active_messages)
        over_message_limit = active_count >= max(self.config.trigger_by_message_count, 1)
        over_token_limit = estimated_tokens >= max(self.config.trigger_by_estimated_tokens, 1)

        if not (over_message_limit or over_token_limit):
            return False

        keep_recent = max(self.config.keep_recent_messages, 6)
        compress_count = active_count - keep_recent
        if compress_count <= 0:
            return False

        old_segment = active_messages[:compress_count]
        previous_summary = self.get_summary(session.key)

        try:
            new_summary = await self._generate_incremental_summary(previous_summary, old_segment)
        except Exception as e:
            logger.warning(f"Context compression failed for {session.key}: {e}")
            return False

        if not new_summary:
            logger.warning(f"Context compression produced empty summary for {session.key}")
            return False

        for idx, _ in old_segment:
            session.messages[idx]["include_in_context"] = False

        self._save_summary_data(
            session.key,
            {
                "session_key": session.key,
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "summary": new_summary,
                "active_messages_after": keep_recent,
                "compressed_messages": compress_count,
                "trigger": {
                    "message_count": active_count,
                    "estimated_tokens": estimated_tokens,
                    "over_message_limit": over_message_limit,
                    "over_token_limit": over_token_limit,
                },
            },
        )

        session.metadata["_context_compress_last_ts"] = now
        session.metadata["_context_compress_last_reason"] = {
            "message_count": active_count,
            "estimated_tokens": estimated_tokens,
        }

        logger.info(
            "Compressed session {} (messages={}, tokens~{}, compressed={})",
            session.key,
            active_count,
            estimated_tokens,
            compress_count,
        )
        return True

    def _active_messages(self, session: Session) -> list[tuple[int, dict[str, Any]]]:
        active: list[tuple[int, dict[str, Any]]] = []
        for idx, msg in enumerate(session.messages):
            if msg.get("include_in_context", True):
                active.append((idx, msg))
        return active

    def _estimate_tokens(self, messages: list[tuple[int, dict[str, Any]]]) -> int:
        payload = []
        for _, msg in messages:
            payload.append(
                {
                    "role": msg.get("role"),
                    "content": msg.get("content"),
                    "tool_calls": msg.get("tool_calls"),
                    "tool_call_id": msg.get("tool_call_id"),
                    "name": msg.get("name"),
                }
            )
        text = json.dumps(payload, ensure_ascii=False)
        return max(1, len(text) // 4)

    async def _generate_incremental_summary(
        self,
        previous_summary: str,
        old_segment: list[tuple[int, dict[str, Any]]],
    ) -> str:
        segment_text = self._render_segment(old_segment)

        system_prompt = (
            "You compress chat history into a precise rolling summary. "
            "Keep key facts, user goals, constraints, decisions, open tasks, and tool outcomes. "
            "Prefer concise bullet points. Avoid hallucinations."
        )
        user_prompt = (
            "Update the rolling summary with new archived messages.\n\n"
            f"Previous summary:\n{previous_summary or '(empty)'}\n\n"
            f"New archived messages:\n{segment_text}\n\n"
            "Output rules:\n"
            "1) Keep only durable context useful for future turns.\n"
            "2) Keep unresolved TODOs and blockers explicit.\n"
            "3) Keep tool side-effects/results that matter.\n"
            "4) Output plain text bullets only."
        )

        response = await self.provider.chat(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            tools=None,
            model=self.config.summary_model or self.default_model,
            max_tokens=max(self.config.summary_max_tokens, 128),
            temperature=0.1,
        )

        candidate = (response.content or "").strip()
        if not candidate or candidate.lower().startswith("error calling llm"):
            candidate = self._fallback_summary(previous_summary, old_segment)

        return self._trim_summary(candidate)

    def _render_segment(self, old_segment: list[tuple[int, dict[str, Any]]]) -> str:
        max_messages = 120
        skipped = max(0, len(old_segment) - max_messages)
        selected = old_segment[-max_messages:]

        lines: list[str] = []
        if skipped:
            lines.append(f"... {skipped} older messages omitted ...")

        for _, msg in selected:
            role = str(msg.get("role", "unknown"))
            content = ""
            if msg.get("content") is not None:
                content = truncate_string(str(msg.get("content")), max_len=420)

            if role == "tool":
                name = msg.get("name") or "unknown_tool"
                tool_call_id = msg.get("tool_call_id") or "unknown_id"
                lines.append(
                    f"[{role}] name={name} call_id={tool_call_id} content={content}"
                )
                continue

            if "tool_calls" in msg:
                tool_names = []
                for tc in msg.get("tool_calls") or []:
                    fn = (tc.get("function") or {}).get("name")
                    if fn:
                        tool_names.append(str(fn))
                if tool_names:
                    lines.append(
                        f"[{role}] requested_tools={', '.join(tool_names)} content={content}"
                    )
                    continue

            lines.append(f"[{role}] {content}")

        return "\n".join(lines)

    def _fallback_summary(self, previous_summary: str, old_segment: list[tuple[int, dict[str, Any]]]) -> str:
        lines = [
            "- Rolling summary fallback (LLM unavailable).",
        ]
        if previous_summary:
            lines.append(f"- Previous summary retained: {truncate_string(previous_summary, max_len=500)}")

        important = []
        for _, msg in old_segment[-12:]:
            role = msg.get("role", "unknown")
            content = truncate_string(str(msg.get("content") or ""), max_len=220)
            if not content:
                continue
            important.append(f"- [{role}] {content}")

        lines.extend(important)
        return "\n".join(lines)

    def _trim_summary(self, summary: str) -> str:
        max_chars = max(self.config.max_rolling_summary_tokens, 200) * 4
        if len(summary) <= max_chars:
            return summary
        return summary[:max_chars] + "\n- (Summary trimmed to fit context budget.)"

    def _summary_path(self, session_key: str) -> Path:
        safe_key = safe_filename(session_key.replace(":", "_"))
        return self.sessions_dir / f"{safe_key}.summary.json"

    def _load_summary_data(self, session_key: str) -> dict[str, Any]:
        path = self._summary_path(session_key)
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"Failed to load summary file for {session_key}: {e}")
            return {}

    def _save_summary_data(self, session_key: str, data: dict[str, Any]) -> None:
        path = self._summary_path(session_key)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(path)
