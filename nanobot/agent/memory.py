"""Memory system: pure file I/O store, lightweight Consolidator, and Dream processor."""

from __future__ import annotations

import asyncio
import json
import os
import re
import weakref
from contextlib import suppress
import tiktoken
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterator

from loguru import logger

from nanobot.utils.prompt_templates import render_template
from nanobot.utils.helpers import ensure_dir, estimate_message_tokens, estimate_prompt_tokens_chain, strip_think, truncate_text

from nanobot.agent.runner import AgentRunSpec, AgentRunner
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.utils.gitstore import GitStore

if TYPE_CHECKING:
    from nanobot.providers.base import LLMProvider
    from nanobot.session.manager import Session, SessionManager


# ---------------------------------------------------------------------------
# MemoryStore — pure file I/O layer
# ---------------------------------------------------------------------------

class MemoryStore:
    """Pure file I/O for memory files: MEMORY.md, history.jsonl, SOUL.md, USER.md."""

    _DEFAULT_MAX_HISTORY = 1000
    _LEGACY_ENTRY_START_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}[^\]]*)\]\s*")
    _LEGACY_TIMESTAMP_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\]\s*")
    _LEGACY_RAW_MESSAGE_RE = re.compile(
        r"^\[\d{4}-\d{2}-\d{2}[^\]]*\]\s+[A-Z][A-Z0-9_]*(?:\s+\[tools:\s*[^\]]+\])?:"
    )

    def __init__(self, workspace: Path, max_history_entries: int = _DEFAULT_MAX_HISTORY):
        self.workspace = workspace
        self.max_history_entries = max_history_entries
        self.memory_dir = ensure_dir(workspace / "memory")
        self.memory_file = self.memory_dir / "MEMORY.md"
        self.history_file = self.memory_dir / "history.jsonl"
        self.legacy_history_file = self.memory_dir / "HISTORY.md"
        self.soul_file = workspace / "SOUL.md"
        self.user_file = workspace / "USER.md"
        self._cursor_file = self.memory_dir / ".cursor"
        self._dream_cursor_file = self.memory_dir / ".dream_cursor"
        self._corruption_logged = False  # rate-limit non-int cursor warning
        self._oversize_logged = False  # rate-limit oversized-entry warning
        self.cold_archive_older_than_days = 30
        self.cold_archive_dir = self.memory_dir / "archive" / "session-raw"
        self._git = GitStore(workspace, tracked_files=[
            "SOUL.md", "USER.md", "memory/MEMORY.md", "memory/topics/*.md",
        ])
        self._maybe_migrate_legacy_history()

    @property
    def git(self) -> GitStore:
        return self._git

    # -- generic helpers -----------------------------------------------------

    @staticmethod
    def read_file(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def _maybe_migrate_legacy_history(self) -> None:
        """One-time upgrade from legacy HISTORY.md to history.jsonl.

        The migration is best-effort and prioritizes preserving as much content
        as possible over perfect parsing.
        """
        if not self.legacy_history_file.exists():
            return
        if self.history_file.exists() and self.history_file.stat().st_size > 0:
            return

        try:
            legacy_text = self.legacy_history_file.read_text(
                encoding="utf-8",
                errors="replace",
            )
        except OSError:
            logger.exception("Failed to read legacy HISTORY.md for migration")
            return

        entries = self._parse_legacy_history(legacy_text)
        try:
            if entries:
                self._write_entries(entries)
                last_cursor = entries[-1]["cursor"]
                self._cursor_file.write_text(str(last_cursor), encoding="utf-8")
                # Default to "already processed" so upgrades do not replay the
                # user's entire historical archive into Dream on first start.
                self._dream_cursor_file.write_text(str(last_cursor), encoding="utf-8")

            backup_path = self._next_legacy_backup_path()
            self.legacy_history_file.replace(backup_path)
            logger.info(
                "Migrated legacy HISTORY.md to history.jsonl ({} entries)",
                len(entries),
            )
        except Exception:
            logger.exception("Failed to migrate legacy HISTORY.md")

    def _parse_legacy_history(self, text: str) -> list[dict[str, Any]]:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not normalized:
            return []

        fallback_timestamp = self._legacy_fallback_timestamp()
        entries: list[dict[str, Any]] = []
        chunks = self._split_legacy_history_chunks(normalized)

        for cursor, chunk in enumerate(chunks, start=1):
            timestamp = fallback_timestamp
            content = chunk
            match = self._LEGACY_TIMESTAMP_RE.match(chunk)
            if match:
                timestamp = match.group(1)
                remainder = chunk[match.end():].lstrip()
                if remainder:
                    content = remainder

            entries.append({
                "cursor": cursor,
                "timestamp": timestamp,
                "content": content,
            })
        return entries

    def _split_legacy_history_chunks(self, text: str) -> list[str]:
        lines = text.split("\n")
        chunks: list[str] = []
        current: list[str] = []
        saw_blank_separator = False

        for line in lines:
            if saw_blank_separator and line.strip() and current:
                chunks.append("\n".join(current).strip())
                current = [line]
                saw_blank_separator = False
                continue
            if self._should_start_new_legacy_chunk(line, current):
                chunks.append("\n".join(current).strip())
                current = [line]
                saw_blank_separator = False
                continue
            current.append(line)
            saw_blank_separator = not line.strip()

        if current:
            chunks.append("\n".join(current).strip())
        return [chunk for chunk in chunks if chunk]

    def _should_start_new_legacy_chunk(self, line: str, current: list[str]) -> bool:
        if not current:
            return False
        if not self._LEGACY_ENTRY_START_RE.match(line):
            return False
        if self._is_raw_legacy_chunk(current) and self._LEGACY_RAW_MESSAGE_RE.match(line):
            return False
        return True

    def _is_raw_legacy_chunk(self, lines: list[str]) -> bool:
        first_nonempty = next((line for line in lines if line.strip()), "")
        match = self._LEGACY_TIMESTAMP_RE.match(first_nonempty)
        if not match:
            return False
        return first_nonempty[match.end():].lstrip().startswith("[RAW]")

    def _legacy_fallback_timestamp(self) -> str:
        try:
            return datetime.fromtimestamp(
                self.legacy_history_file.stat().st_mtime,
            ).strftime("%Y-%m-%d %H:%M")
        except OSError:
            return datetime.now().strftime("%Y-%m-%d %H:%M")

    def _next_legacy_backup_path(self) -> Path:
        candidate = self.memory_dir / "HISTORY.md.bak"
        suffix = 2
        while candidate.exists():
            candidate = self.memory_dir / f"HISTORY.md.bak.{suffix}"
            suffix += 1
        return candidate

    # -- MEMORY.md (long-term facts) -----------------------------------------

    def read_memory(self) -> str:
        return self.read_file(self.memory_file)

    def write_memory(self, content: str) -> None:
        self.memory_file.write_text(content, encoding="utf-8")

    # -- SOUL.md -------------------------------------------------------------

    def read_soul(self) -> str:
        return self.read_file(self.soul_file)

    def write_soul(self, content: str) -> None:
        self.soul_file.write_text(content, encoding="utf-8")

    # -- USER.md -------------------------------------------------------------

    def read_user(self) -> str:
        return self.read_file(self.user_file)

    def write_user(self, content: str) -> None:
        self.user_file.write_text(content, encoding="utf-8")

    # -- context injection (used by context.py) ------------------------------

    def get_memory_context(self) -> str:
        long_term = self.read_memory()
        return f"## Long-term Memory\n{long_term}" if long_term else ""

    # -- history.jsonl — append-only, JSONL format ---------------------------

    def append_history(self, entry: str, *, max_chars: int | None = None) -> int:
        """Append *entry* to history.jsonl and return its auto-incrementing cursor.

        Entries are passed through `strip_think` to drop template-level leaks
        (e.g. unclosed `<think` prefixes, `<channel|>` markers) before being
        persisted. If the cleaned content is empty but the raw entry wasn't,
        the record is persisted with an empty string rather than falling back
        to the raw leak — otherwise `strip_think`'s guarantees would be
        undone by history replay / consolidation downstream.

        A defensive cap (*max_chars*, default ``_HISTORY_ENTRY_HARD_CAP``) is
        applied as a final safety net: individual callers should cap their own
        content more tightly; this default only exists to catch unintentional
        large writes (e.g. an LLM echoing its input back as a "summary").
        """
        limit = max_chars if max_chars is not None else _HISTORY_ENTRY_HARD_CAP
        cursor = self._next_cursor()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        raw = entry.rstrip()
        if len(raw) > limit:
            if not self._oversize_logged:
                self._oversize_logged = True
                logger.warning(
                    "history entry exceeds {} chars ({}); truncating. "
                    "Usually means a caller forgot its own cap; "
                    "further occurrences suppressed.",
                    limit, len(raw),
                )
            raw = truncate_text(raw, limit)
        content = strip_think(raw)
        if raw and not content:
            logger.debug(
                "history entry {} stripped to empty (likely template leak); "
                "persisting empty content to avoid re-polluting context",
                cursor,
            )
        record = {"cursor": cursor, "timestamp": ts, "content": content}
        with open(self.history_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._cursor_file.write_text(str(cursor), encoding="utf-8")
        return cursor

    @staticmethod
    def _valid_cursor(value: Any) -> int | None:
        """Int cursors only — reject bool (``isinstance(True, int)`` is True)."""
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value

    def _iter_valid_entries(self) -> Iterator[tuple[dict[str, Any], int]]:
        """Yield ``(entry, cursor)`` for entries with int cursors; warn once on corruption."""
        poisoned: Any = None
        for entry in self._read_entries():
            raw = entry.get("cursor")
            if raw is None:
                continue
            cursor = self._valid_cursor(raw)
            if cursor is None:
                poisoned = raw
                continue
            yield entry, cursor
        if poisoned is not None and not self._corruption_logged:
            self._corruption_logged = True
            logger.warning(
                "history.jsonl contains a non-int cursor ({!r}); dropping it. "
                "Usually caused by an external writer; further occurrences suppressed.",
                poisoned,
            )

    def _next_cursor(self) -> int:
        """Read the current cursor counter and return the next value."""
        if self._cursor_file.exists():
            with suppress(ValueError, OSError):
                return int(self._cursor_file.read_text(encoding="utf-8").strip()) + 1
        # Fast path: trust the tail when intact.  Otherwise scan the whole
        # file and take ``max`` — that stays correct even if the monotonic
        # invariant was broken by external writes.
        last = self._read_last_entry() or {}
        cursor = self._valid_cursor(last.get("cursor"))
        if cursor is not None:
            return cursor + 1
        return max((c for _, c in self._iter_valid_entries()), default=0) + 1

    def read_unprocessed_history(self, since_cursor: int) -> list[dict[str, Any]]:
        """Return history entries with a valid cursor > *since_cursor*."""
        return [e for e, c in self._iter_valid_entries() if c > since_cursor]

    def compact_history(self) -> None:
        """Drop oldest entries if the file exceeds *max_history_entries*."""
        if self.max_history_entries <= 0:
            return
        entries = self._read_entries()
        if len(entries) <= self.max_history_entries:
            return
        kept = entries[-self.max_history_entries:]
        self._write_entries(kept)

    # -- JSONL helpers -------------------------------------------------------

    def _read_entries(self) -> list[dict[str, Any]]:
        """Read all entries from history.jsonl."""
        entries: list[dict[str, Any]] = []
        with suppress(FileNotFoundError):
            with open(self.history_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            entries.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue

        return entries

    def _read_last_entry(self) -> dict[str, Any] | None:
        """Read the last entry from the JSONL file efficiently."""
        try:
            with open(self.history_file, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                if size == 0:
                    return None
                read_size = min(size, 4096)
                f.seek(size - read_size)
                data = f.read().decode("utf-8")
                lines = [l for l in data.split("\n") if l.strip()]
                if not lines:
                    return None
                return json.loads(lines[-1])
        except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
            return None

    def _write_entries(self, entries: list[dict[str, Any]]) -> None:
        """Overwrite history.jsonl with the given entries (atomic write)."""
        tmp_path = self.history_file.with_suffix(self.history_file.suffix + ".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                for entry in entries:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.history_file)

            # fsync the directory so the rename is durable.
            # On Windows, opening a directory with O_RDONLY raises
            # PermissionError — skip the dir sync there (NTFS
            # journals metadata synchronously).
            with suppress(PermissionError):
                fd = os.open(str(self.history_file.parent), os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

    # -- dream cursor --------------------------------------------------------

    def get_last_dream_cursor(self) -> int:
        if self._dream_cursor_file.exists():
            with suppress(ValueError, OSError):
                return int(self._dream_cursor_file.read_text(encoding="utf-8").strip())
        return 0

    def set_last_dream_cursor(self, cursor: int) -> None:
        self._dream_cursor_file.write_text(str(cursor), encoding="utf-8")

    # -- message formatting utility ------------------------------------------

    @staticmethod
    def _format_messages(messages: list[dict]) -> str:
        lines = []
        for message in messages:
            if not message.get("content"):
                continue
            tools = f" [tools: {', '.join(message['tools_used'])}]" if message.get("tools_used") else ""
            lines.append(
                f"[{message.get('timestamp', '?')[:16]}] {message['role'].upper()}{tools}: {message['content']}"
            )
        return "\n".join(lines)

    def raw_archive(self, messages: list[dict], *, max_chars: int | None = None) -> None:
        """Fallback archive for raw messages.

        Recent messages are written to history.jsonl for Dream. Messages older
        than ``cold_archive_older_than_days`` are preserved under
        memory/archive/session-raw/ and intentionally skipped by Dream.
        """
        if not messages:
            return
        recent_count = 0
        cold_count = 0
        for is_cold, chunk in self._split_raw_archive_chunks(messages):
            if is_cold:
                self._write_cold_archive(chunk, max_chars=max_chars)
                cold_count += len(chunk)
            else:
                self._append_raw_history(chunk, max_chars=max_chars)
                recent_count += len(chunk)
        if recent_count:
            logger.warning(
                "Memory consolidation degraded: raw-archived {} recent messages",
                recent_count,
            )
        if cold_count:
            logger.info("Memory consolidation cold-archived {} old messages", cold_count)

    def _append_raw_history(self, messages: list[dict], *, max_chars: int | None = None) -> None:
        """Dump raw recent messages to history.jsonl without LLM summarization."""
        limit = max_chars if max_chars is not None else _RAW_ARCHIVE_MAX_CHARS
        formatted = truncate_text(self._format_messages(messages), limit)
        self.append_history(
            f"[RAW] {len(messages)} messages\n"
            f"{formatted}"
        )

    def append_formatted_history_chunks(
        self,
        messages: list[dict],
        *,
        label: str = "[RAW]",
        max_chars: int | None = None,
    ) -> int:
        """Append raw formatted messages to history.jsonl without LLM summarization."""
        if not messages:
            return 0
        limit = max_chars if max_chars is not None else _RAW_ARCHIVE_MAX_CHARS
        entries_written = 0
        current_messages: list[dict] = []
        current_lines: list[str] = []

        def flush() -> None:
            nonlocal entries_written, current_messages, current_lines
            if not current_lines:
                current_messages = []
                return
            header = f"{label} {len(current_messages)} messages\n"
            available = max(256, limit - len(header))
            suffix_len = len("\n... (truncated)")
            formatted = truncate_text("\n".join(current_lines), max(0, available - suffix_len))
            self.append_history(header + formatted, max_chars=limit)
            entries_written += 1
            current_messages = []
            current_lines = []

        for message in messages:
            line = self._format_messages([message])
            if not line:
                continue
            if len(line) > limit:
                suffix_len = len("\n... (truncated)")
                line = truncate_text(line, max(0, limit - suffix_len))
            header_len = len(f"{label} {len(current_messages) + 1} messages\n")
            projected = header_len + len("\n".join([*current_lines, line]))
            if current_lines and projected > limit:
                flush()
            current_messages.append(message)
            current_lines.append(line)

        flush()
        return entries_written

    def _split_raw_archive_chunks(self, messages: list[dict]) -> Iterator[tuple[bool, list[dict]]]:
        current_is_cold: bool | None = None
        current: list[dict] = []
        for message in messages:
            is_cold = self._is_cold_archive_message(message)
            if current and is_cold != current_is_cold:
                yield bool(current_is_cold), current
                current = []
            current_is_cold = is_cold
            current.append(message)
        if current:
            yield bool(current_is_cold), current

    def _is_cold_archive_message(self, message: dict) -> bool:
        if self.cold_archive_older_than_days <= 0:
            return False
        timestamp = self._parse_message_timestamp(message.get("timestamp"))
        if timestamp is None:
            return False
        cutoff = datetime.now() - timedelta(days=self.cold_archive_older_than_days)
        return timestamp < cutoff

    @staticmethod
    def _parse_message_timestamp(value: Any) -> datetime | None:
        if not isinstance(value, str) or not value.strip():
            return None
        raw = value.strip()
        with suppress(ValueError):
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
        for fmt, length in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d %H:%M", 16)):
            with suppress(ValueError):
                return datetime.strptime(raw[:length], fmt)
        return None

    def _write_cold_archive(self, messages: list[dict], *, max_chars: int | None = None) -> None:
        limit = max_chars if max_chars is not None else _RAW_ARCHIVE_MAX_CHARS
        formatted = truncate_text(self._format_messages(messages), limit)
        timestamps = [
            ts for ts in (self._parse_message_timestamp(m.get("timestamp")) for m in messages)
            if ts is not None
        ]
        first_ts = timestamps[0] if timestamps else datetime.now()
        archive_path = self.cold_archive_dir / f"{first_ts.strftime('%Y-%m')}.jsonl"
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "archived_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "message_count": len(messages),
            "start_timestamp": timestamps[0].isoformat() if timestamps else None,
            "end_timestamp": timestamps[-1].isoformat() if timestamps else None,
            "content": f"[RAW-COLD] {len(messages)} messages\n{formatted}",
        }
        with open(archive_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")



# ---------------------------------------------------------------------------
# Consolidator — lightweight token-budget triggered consolidation
# ---------------------------------------------------------------------------


# Individual history.jsonl writers cap their own payloads tightly; the
# _HISTORY_ENTRY_HARD_CAP at append_history() is a belt-and-suspenders default
# that catches any new caller that forgot to set its own cap.
_RAW_ARCHIVE_MAX_CHARS = 16_000       # fallback dump (LLM failed)
_ARCHIVE_SUMMARY_MAX_CHARS = 8_000    # LLM-produced consolidation summary
_HISTORY_ENTRY_HARD_CAP = 64_000      # emergency cap in append_history


class Consolidator:
    """Lightweight consolidation: summarizes evicted messages into history.jsonl."""

    _MAX_CONSOLIDATION_ROUNDS = 5

    _SAFETY_BUFFER = 1024  # extra headroom for tokenizer estimation drift

    def __init__(
        self,
        store: MemoryStore,
        provider: LLMProvider,
        model: str,
        sessions: SessionManager,
        context_window_tokens: int,
        build_messages: Callable[..., list[dict[str, Any]]],
        get_tool_definitions: Callable[[], list[dict[str, Any]]],
        max_completion_tokens: int = 4096,
        consolidation_ratio: float = 0.5,
        active_context_archive_buffer_messages: int = 100,
    ):
        self.store = store
        self.provider = provider
        self.model = model
        self.sessions = sessions
        self.context_window_tokens = context_window_tokens
        self.max_completion_tokens = max_completion_tokens
        self.consolidation_ratio = consolidation_ratio
        self._build_messages = build_messages
        self._get_tool_definitions = get_tool_definitions
        self.active_context_archive_buffer_messages = active_context_archive_buffer_messages
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )

    def set_provider(
        self,
        provider: LLMProvider,
        model: str,
        context_window_tokens: int,
    ) -> None:
        self.provider = provider
        self.model = model
        self.context_window_tokens = context_window_tokens
        self.max_completion_tokens = provider.generation.max_tokens

    def get_lock(self, session_key: str) -> asyncio.Lock:
        """Return the shared consolidation lock for one session."""
        return self._locks.setdefault(session_key, asyncio.Lock())

    def pick_consolidation_boundary(
        self,
        session: Session,
        tokens_to_remove: int,
        *,
        keep_recent_tokens: int = 0,
    ) -> tuple[int, int] | None:
        """Pick a user-turn boundary that removes enough old prompt tokens."""
        start = session.last_consolidated
        if start >= len(session.messages) or tokens_to_remove <= 0:
            return None

        total_tokens = 0
        if keep_recent_tokens > 0:
            total_tokens = sum(
                estimate_message_tokens(message)
                for message in session.messages[start:]
            )

        removed_tokens = 0
        last_boundary: tuple[int, int] | None = None
        for idx in range(start, len(session.messages)):
            message = session.messages[idx]
            if idx > start and message.get("role") == "user":
                remaining_tokens = total_tokens - removed_tokens
                if keep_recent_tokens <= 0 or remaining_tokens >= keep_recent_tokens:
                    last_boundary = (idx, removed_tokens)
                    if removed_tokens >= tokens_to_remove:
                        return last_boundary
            removed_tokens += estimate_message_tokens(message)

        return last_boundary

    def estimate_session_prompt_tokens(
        self,
        session: Session,
        *,
        session_summary: str | None = None,
    ) -> tuple[int, str]:
        """Estimate current prompt size for the full unconsolidated backlog."""
        history = session.get_history_token_first(
            max_tokens=0,
            include_timestamps=True,
            safety_message_cap=0,
        )
        channel, chat_id = (session.key.split(":", 1) if ":" in session.key else (None, None))
        probe_messages = self._build_messages(
            history=history,
            current_message="[token-probe]",
            channel=channel,
            chat_id=chat_id,
            session_summary=session_summary,
            sender_id=None,
        )
        return estimate_prompt_tokens_chain(
            self.provider,
            self.model,
            probe_messages,
            self._get_tool_definitions(),
        )

    @property
    def _input_token_budget(self) -> int:
        """Available input token budget for consolidation LLM."""
        reserve_tokens = max(self.max_completion_tokens + 4096, 16_384)
        budget = self.context_window_tokens - reserve_tokens
        return budget if budget > 0 else max(128, self.context_window_tokens // 2)

    @property
    def _keep_recent_tokens(self) -> int:
        budget = self._input_token_budget
        return min(20_000, max(8_000, budget // 5))

    def _truncate_to_token_budget(self, text: str) -> str:
        """Truncate text so it fits within the consolidation LLM's token budget."""
        budget = self._input_token_budget
        if budget <= 0:
            return truncate_text(text, _RAW_ARCHIVE_MAX_CHARS)
        try:
            enc = tiktoken.get_encoding("cl100k_base")
            tokens = enc.encode(text)
            if len(tokens) <= budget:
                return text
            return enc.decode(tokens[:budget]) + "\n... (truncated)"
        except Exception:
            return truncate_text(text, budget * 4)

    async def archive(self, messages: list[dict]) -> str | None:
        """Summarize messages via LLM and append to history.jsonl.

        Returns the summary text on success, None if nothing to archive.
        """
        if not messages:
            return None
        try:
            formatted = MemoryStore._format_messages(messages)
            formatted = self._truncate_to_token_budget(formatted)
            response = await self.provider.chat_with_retry(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": render_template(
                            "agent/consolidator_archive.md",
                            strip=True,
                        ),
                    },
                    {"role": "user", "content": formatted},
                ],
                tools=None,
                tool_choice=None,
            )
            if response.finish_reason == "error":
                raise RuntimeError(f"LLM returned error: {response.content}")
            summary = response.content or "[no summary]"
            self.store.append_history(summary, max_chars=_ARCHIVE_SUMMARY_MAX_CHARS)
            return summary
        except Exception:
            logger.warning("Consolidation LLM call failed, raw-dumping to history")
            self.store.raw_archive(messages)
            return None

    async def maybe_consolidate_by_tokens(
        self,
        session: Session,
        *,
        session_summary: str | None = None,
    ) -> None:
        """Loop: archive old messages until prompt fits within safe budget.

        The budget reserves space for completion tokens and a safety buffer
        so the LLM request never exceeds the context window.
        """
        if not session.messages or self.context_window_tokens <= 0:
            return

        lock = self.get_lock(session.key)
        async with lock:
            budget = self._input_token_budget
            target = int(budget * self.consolidation_ratio)
            try:
                estimated, source = self.estimate_session_prompt_tokens(
                    session,
                    session_summary=session_summary,
                )
            except Exception:
                logger.exception("Token estimation failed for {}", session.key)
                estimated, source = 0, "error"
            if estimated <= 0:
                return
            if estimated < budget:
                unconsolidated_count = len(session.messages) - session.last_consolidated
                logger.debug(
                    "Token consolidation idle {}: {}/{} via {}, msgs={}",
                    session.key,
                    estimated,
                    self.context_window_tokens,
                    source,
                    unconsolidated_count,
                )
                return

            last_summary = None
            for round_num in range(self._MAX_CONSOLIDATION_ROUNDS):
                if estimated <= target:
                    break

                boundary = self.pick_consolidation_boundary(
                    session,
                    max(1, estimated - target),
                    keep_recent_tokens=self._keep_recent_tokens,
                )
                if boundary is None:
                    boundary = self.pick_consolidation_boundary(
                        session,
                        max(1, estimated - target),
                    )
                if boundary is None:
                    logger.debug(
                        "Token consolidation: no safe boundary for {} (round {})",
                        session.key,
                        round_num,
                    )
                    break

                end_idx = boundary[0]

                chunk = session.messages[session.last_consolidated:end_idx]
                if not chunk:
                    break

                logger.info(
                    "Token consolidation round {} for {}: {}/{} via {}, chunk={} msgs",
                    round_num,
                    session.key,
                    estimated,
                    self.context_window_tokens,
                    source,
                    len(chunk),
                )
                summary = await self.archive(chunk)
                # Advance the cursor either way: on success the chunk was
                # summarized; on failure archive() already raw-archived it as
                # a breadcrumb. Re-archiving the same chunk on the next call
                # would just emit duplicate [RAW] entries.
                if summary:
                    last_summary = summary
                session.last_consolidated = end_idx
                self.sessions.save(session)
                if not summary:
                    # LLM is degraded — stop hammering it this call;
                    # the next invocation can retry a fresh chunk.
                    break

                try:
                    estimated, source = self.estimate_session_prompt_tokens(
                        session,
                        session_summary=session_summary,
                    )
                except Exception:
                    logger.exception("Token estimation failed for {}", session.key)
                    estimated, source = 0, "error"
                if estimated <= 0:
                    break

            # Persist the last summary to session metadata so it can be injected
            # into the runtime context on the next prepare_session() call, aligning
            # the summary injection strategy with AutoCompact._archive().
            if last_summary and last_summary != "(nothing)":
                session.metadata["_last_summary"] = {
                    "text": last_summary,
                    "last_active": session.updated_at.isoformat(),
                }
                self.sessions.save(session)

    async def maybe_archive_active_context_buffer(
        self,
        session: Session,
        *,
        max_tokens: int,
        safety_message_cap: int = 1000,
    ) -> None:
        """Archive old messages that token-first replay already excludes.

        Token consolidation is threshold-based on the full unconsolidated
        backlog. This method covers the complementary case where the active
        replay window/safety cap has already pushed older messages out of the
        model prompt, but the full backlog is still below the hard context
        threshold. Those messages remain in the raw session file, but once the
        excluded prefix reaches a small buffer threshold we summarize it into
        history.jsonl so Dream can process it promptly.
        """
        threshold = int(self.active_context_archive_buffer_messages or 0)
        if threshold <= 0 or not session.messages:
            return

        lock = self.get_lock(session.key)
        async with lock:
            visible = session.get_history_token_first_with_indexes(
                max_tokens=max_tokens,
                include_timestamps=True,
                safety_message_cap=safety_message_cap,
            )
            if not visible:
                return

            visible_start = max(session.last_consolidated, visible[0][0])
            hidden_count = visible_start - session.last_consolidated
            if hidden_count <= 0:
                return
            if hidden_count < threshold:
                logger.debug(
                    "Active context archive buffer idle {}: {}/{} msgs outside replay",
                    session.key,
                    hidden_count,
                    threshold,
                )
                return

            chunk = session.messages[session.last_consolidated:visible_start]
            if not chunk:
                return

            entries = self.store.append_formatted_history_chunks(
                chunk,
                label="[RAW-ACTIVE-CONTEXT]",
            )
            logger.info(
                "Active context raw archive for {}: {} msgs outside replay, {} history entries (threshold={})",
                session.key,
                len(chunk),
                entries,
                threshold,
            )
            session.last_consolidated = visible_start
            self.sessions.save(session)


# ---------------------------------------------------------------------------
# Dream — heavyweight cron-scheduled memory consolidation
# ---------------------------------------------------------------------------


# Single source of truth for the staleness threshold used in _annotate_with_ages
# *and* in the Phase 1 prompt template (passed as `stale_threshold_days`).
# Keep code and prompt aligned — if you bump this, the LLM's instruction string
# updates automatically.
_STALE_THRESHOLD_DAYS = 14


class Dream:
    """Two-phase memory processor: analyze history.jsonl, then edit files via AgentRunner.

    Phase 1 produces an analysis summary (plain LLM call).
    Phase 2 delegates to AgentRunner with read_file / edit_file tools so the
    LLM can make targeted, incremental edits instead of replacing entire files.
    """

    # Caps on prompt-bound inputs so Dream's LLM calls never exceed the model's
    # context window just because a file (or a legacy large history entry) grew
    # unexpectedly. Each file still appears in full via read_file when the agent
    # needs it in Phase 2 — these caps only bound the Phase 1/2 prompt preview.
    _MEMORY_FILE_MAX_CHARS = 32_000
    _SOUL_FILE_MAX_CHARS = 16_000
    _USER_FILE_MAX_CHARS = 16_000
    _TOPIC_FILE_MAX_CHARS = 8_000
    _TOPICS_TOTAL_MAX_CHARS = 32_000
    _HISTORY_ENTRY_PREVIEW_MAX_CHARS = 4_000
    _RUN_LOG_MAX_CHARS = 64_000

    def __init__(
        self,
        store: MemoryStore,
        provider: LLMProvider,
        model: str,
        max_batch_size: int = 20,
        max_iterations: int = 10,
        max_tool_result_chars: int = 16_000,
        annotate_line_ages: bool = True,
        allow_topic_edits: bool = True,
    ):
        self.store = store
        self.provider = provider
        self.model = model
        self.model_override: str | None = None
        self.max_batch_size = max_batch_size
        self.max_iterations = max_iterations
        self.max_tool_result_chars = max_tool_result_chars
        # Kill switch for the git-blame-based per-line age annotation in Phase 1.
        # Default True keeps the #3212 behavior; set False to feed MEMORY.md raw
        # (e.g. if a specific LLM reacts poorly to the `← Nd` suffix).
        self.annotate_line_ages = annotate_line_ages
        self.allow_topic_edits = allow_topic_edits
        self._runner = AgentRunner(provider)
        self._tools = self._build_tools()

    def set_provider(self, provider: LLMProvider, model: str) -> None:
        self.provider = provider
        self.model = self.model_override or model
        self._runner.provider = provider

    def set_model_override(self, model: str | None) -> None:
        self.model_override = model or None
        if self.model_override:
            self.model = self.model_override

    # -- tool registry -------------------------------------------------------

    def _build_tools(self) -> ToolRegistry:
        """Build a minimal tool registry for the Dream agent."""
        from nanobot.agent.skills import BUILTIN_SKILLS_DIR
        from nanobot.agent.tools.file_state import FileStates
        from nanobot.agent.tools.filesystem import EditFileTool, ReadFileTool, WriteFileTool

        tools = ToolRegistry()
        workspace = self.store.workspace
        # Allow reading builtin skills for reference during skill creation
        extra_read = [BUILTIN_SKILLS_DIR] if BUILTIN_SKILLS_DIR.exists() else None
        # Dream gets its own FileStates so its caches stay isolated from the
        # main loop's sessions (issue #3571).
        file_states = FileStates()
        tools.register(ReadFileTool(
            workspace=workspace,
            allowed_dir=workspace,
            extra_allowed_dirs=extra_read,
            file_states=file_states,
        ))
        tools.register(EditFileTool(workspace=workspace, allowed_dir=workspace, file_states=file_states))
        # write_file resolves relative paths from workspace root, but can only
        # write under skills/ so the prompt can safely use skills/<name>/SKILL.md.
        skills_dir = workspace / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)
        tools.register(WriteFileTool(workspace=workspace, allowed_dir=skills_dir, file_states=file_states))
        return tools

    # -- skill listing --------------------------------------------------------

    def _list_existing_skills(self) -> list[str]:
        """List existing skills as 'name — description' for dedup context."""
        import re as _re

        from nanobot.agent.skills import BUILTIN_SKILLS_DIR

        _DESC_RE = _re.compile(r"^description:\s*(.+)$", _re.MULTILINE | _re.IGNORECASE)
        entries: dict[str, str] = {}
        for base in (self.store.workspace / "skills", BUILTIN_SKILLS_DIR):
            if not base.exists():
                continue
            for d in base.iterdir():
                if not d.is_dir():
                    continue
                skill_md = d / "SKILL.md"
                if not skill_md.exists():
                    continue
                # Prefer workspace skills over builtin (same name)
                if d.name in entries and base == BUILTIN_SKILLS_DIR:
                    continue
                content = skill_md.read_text(encoding="utf-8")[:500]
                m = _DESC_RE.search(content)
                desc = m.group(1).strip() if m else "(no description)"
                entries[d.name] = desc
        return [f"{name} — {desc}" for name, desc in sorted(entries.items())]

    # -- main entry ----------------------------------------------------------

    def _annotate_with_ages(self, content: str) -> str:
        """Append per-line age suffixes to MEMORY.md content.

        Each non-blank line whose age exceeds ``_STALE_THRESHOLD_DAYS`` gets a
        suffix like ``← 30d`` indicating days since last modification.
        Returns the original content unchanged if git is unavailable,
        annotate fails, or the line count doesn't match the age count
        (which can happen with an uncommitted working-tree edit — better to
        skip annotation than to tag the wrong line).
        SOUL.md and USER.md are never annotated.
        """
        file_path = "memory/MEMORY.md"
        try:
            ages = self.store.git.line_ages(file_path)
        except Exception:
            logger.debug("line_ages failed for {}", file_path)
            return content
        if not ages:
            return content

        had_trailing = content.endswith("\n")
        lines = content.splitlines()
        # If HEAD-blob line count disagrees with the working-tree content we
        # received, ages would be assigned to the wrong lines — skip entirely
        # and feed the LLM un-annotated content rather than misleading data.
        if len(lines) != len(ages):
            logger.debug(
                "line_ages length mismatch for {} (lines={}, ages={}); skipping annotation",
                file_path, len(lines), len(ages),
            )
            return content

        annotated: list[str] = []
        for line, age in zip(lines, ages):
            if not line.strip():
                annotated.append(line)
                continue
            if age.age_days > _STALE_THRESHOLD_DAYS:
                annotated.append(f"{line}  \u2190 {age.age_days}d")
            else:
                annotated.append(line)
        result = "\n".join(annotated)
        if had_trailing:
            result += "\n"
        return result

    def _read_topic_previews(self) -> str:
        """Return bounded previews of memory/topics/*.md for Dream prompts."""
        if not self.allow_topic_edits:
            return ""
        topics_dir = self.store.memory_dir / "topics"
        if not topics_dir.exists():
            return ""
        sections: list[str] = []
        total = 0
        for path in sorted(topics_dir.glob("*.md")):
            if not path.is_file():
                continue
            rel = path.relative_to(self.store.workspace).as_posix()
            content = truncate_text(
                path.read_text(encoding="utf-8", errors="replace") or "(empty)",
                self._TOPIC_FILE_MAX_CHARS,
            )
            section = f"### {rel}\n{content}"
            if sections and total + len(section) > self._TOPICS_TOTAL_MAX_CHARS:
                sections.append("### (truncated)\nAdditional topic files omitted from prompt preview.")
                break
            sections.append(section)
            total += len(section)
        if not sections:
            return ""
        return "## Current memory/topics/*.md\n" + "\n\n".join(sections)

    def _write_run_log(
        self,
        *,
        started_at: datetime,
        last_cursor: int,
        batch: list[dict[str, Any]],
        phase1_output: str | None,
        phase2_output: str | None,
        stop_reason: str,
        tool_events: list[dict[str, Any]] | None = None,
    ) -> None:
        """Persist Dream phase outputs for local debugging and later review."""
        if not batch:
            return
        try:
            logs_dir = ensure_dir(self.store.memory_dir / "dream_logs")
            end_cursor = batch[-1].get("cursor", "?")
            stamp = started_at.strftime("%Y%m%d-%H%M%S")
            path = logs_dir / f"{stamp}_cursor-{last_cursor}-to-{end_cursor}.md"
            phase1 = truncate_text(phase1_output or "", self._RUN_LOG_MAX_CHARS)
            phase2 = truncate_text(phase2_output or "", self._RUN_LOG_MAX_CHARS)
            events = tool_events or []
            event_lines = "\n".join(
                "- {name} [{status}]: {detail}".format(
                    name=event.get("name", "?"),
                    status=event.get("status", "?"),
                    detail=str(event.get("detail", "")).replace("\n", " ")[:500],
                )
                for event in events
            ) or "(none)"
            content = (
                f"# Dream Run Log\n\n"
                f"- started_at: {started_at.isoformat(timespec='seconds')}\n"
                f"- cursor_range: {last_cursor} -> {end_cursor}\n"
                f"- batch_size: {len(batch)}\n"
                f"- stop_reason: {stop_reason}\n"
                f"- tool_event_count: {len(events)}\n\n"
                f"## Phase 1 Final Output\n\n"
                f"```text\n{phase1}\n```\n\n"
                f"## Phase 2 Final Output\n\n"
                f"```text\n{phase2}\n```\n\n"
                f"## Phase 2 Tool Events\n\n"
                f"{event_lines}\n"
            )
            path.write_text(content, encoding="utf-8")
            logger.debug("Dream run log written: {}", path)
        except Exception:
            logger.exception("Dream run log write failed")

    async def run(self) -> bool:
        """Process unprocessed history entries. Returns True if work was done."""
        from nanobot.agent.skills import BUILTIN_SKILLS_DIR

        started_at = datetime.now()
        last_cursor = self.store.get_last_dream_cursor()
        entries = self.store.read_unprocessed_history(since_cursor=last_cursor)
        if not entries:
            return False

        batch = entries[: self.max_batch_size]
        logger.info(
            "Dream: processing {} entries (cursor {}→{}), batch={}",
            len(entries), last_cursor, batch[-1]["cursor"], len(batch),
        )

        # Build history text for LLM — cap each entry so a legacy oversized
        # record (e.g. pre-#3412 raw_archive dump) can't blow up the prompt.
        history_text = "\n".join(
            f"[{e['timestamp']}] "
            f"{truncate_text(e['content'], self._HISTORY_ENTRY_PREVIEW_MAX_CHARS)}"
            for e in batch
        )

        # Current file contents + per-line age annotations (MEMORY.md only).
        # Each file is capped in the *prompt preview* only; Phase 2 still sees
        # the full file via the read_file tool.
        current_date = datetime.now().strftime("%Y-%m-%d")
        raw_memory = self.store.read_memory() or "(empty)"
        annotated_memory = (
            self._annotate_with_ages(raw_memory)
            if self.annotate_line_ages
            else raw_memory
        )
        current_memory = truncate_text(annotated_memory, self._MEMORY_FILE_MAX_CHARS)
        current_soul = truncate_text(
            self.store.read_soul() or "(empty)", self._SOUL_FILE_MAX_CHARS,
        )
        current_user = truncate_text(
            self.store.read_user() or "(empty)", self._USER_FILE_MAX_CHARS,
        )
        current_topics = self._read_topic_previews()

        file_context = (
            f"## Current Date\n{current_date}\n\n"
            f"## Current MEMORY.md ({len(current_memory)} chars)\n{current_memory}\n\n"
            f"## Current SOUL.md ({len(current_soul)} chars)\n{current_soul}\n\n"
            f"## Current USER.md ({len(current_user)} chars)\n{current_user}"
        )
        if current_topics:
            file_context += f"\n\n{current_topics}"

        # Phase 1: Analyze (no skills list — dedup is Phase 2's job)
        phase1_prompt = (
            f"## Conversation History\n{history_text}\n\n{file_context}"
        )

        try:
            phase1_response = await self.provider.chat_with_retry(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": render_template(
                            "agent/dream_phase1.md",
                            strip=True,
                            stale_threshold_days=_STALE_THRESHOLD_DAYS,
                        ),
                    },
                    {"role": "user", "content": phase1_prompt},
                ],
                tools=None,
                tool_choice=None,
            )
            if phase1_response.finish_reason == "error":
                logger.warning(
                    "Dream Phase 1 returned LLM error; cursor not advanced: {}",
                    (phase1_response.content or "")[:300],
                )
                self._write_run_log(
                    started_at=started_at,
                    last_cursor=last_cursor,
                    batch=batch,
                    phase1_output=phase1_response.content or "",
                    phase2_output="",
                    stop_reason="phase1_error",
                    tool_events=[],
                )
                return False
            analysis = phase1_response.content or ""
            logger.debug("Dream Phase 1 analysis ({} chars): {}", len(analysis), analysis[:500])
        except Exception:
            logger.exception("Dream Phase 1 failed")
            return False

        # Phase 2: Delegate to AgentRunner with read_file / edit_file
        existing_skills = self._list_existing_skills()
        skills_section = ""
        if existing_skills:
            skills_section = (
                "\n\n## Existing Skills\n"
                + "\n".join(f"- {s}" for s in existing_skills)
            )
        phase2_prompt = f"## Analysis Result\n{analysis}\n\n{file_context}{skills_section}"

        tools = self._tools
        skill_creator_path = BUILTIN_SKILLS_DIR / "skill-creator" / "SKILL.md"
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": render_template(
                    "agent/dream_phase2.md",
                    strip=True,
                    skill_creator_path=str(skill_creator_path),
                ),
            },
            {"role": "user", "content": phase2_prompt},
        ]

        try:
            result = await self._runner.run(AgentRunSpec(
                initial_messages=messages,
                tools=tools,
                model=self.model,
                max_iterations=self.max_iterations,
                max_tool_result_chars=self.max_tool_result_chars,
                fail_on_tool_error=False,
            ))
            logger.debug(
                "Dream Phase 2 complete: stop_reason={}, tool_events={}",
                result.stop_reason, len(result.tool_events),
            )
            for ev in (result.tool_events or []):
                logger.info("Dream tool_event: name={}, status={}, detail={}", ev.get("name"), ev.get("status"), ev.get("detail", "")[:200])
        except Exception:
            logger.exception("Dream Phase 2 failed")
            result = None

        self._write_run_log(
            started_at=started_at,
            last_cursor=last_cursor,
            batch=batch,
            phase1_output=analysis,
            phase2_output=(result.final_content if result else ""),
            stop_reason=(result.stop_reason if result else "phase2_exception"),
            tool_events=(result.tool_events if result else []),
        )

        # Build changelog from tool events
        changelog: list[str] = []
        if result and result.tool_events:
            for event in result.tool_events:
                if event["status"] == "ok":
                    changelog.append(f"{event['name']}: {event['detail']}")

        # Advance cursor — always, to avoid re-processing Phase 1
        new_cursor = batch[-1]["cursor"]
        self.store.set_last_dream_cursor(new_cursor)
        self.store.compact_history()

        if result and result.stop_reason == "completed":
            logger.info(
                "Dream done: {} change(s), cursor advanced to {}",
                len(changelog), new_cursor,
            )
        else:
            reason = result.stop_reason if result else "exception"
            logger.warning(
                "Dream incomplete ({}): cursor advanced to {}",
                reason, new_cursor,
            )

        # Git auto-commit (only when there are actual changes)
        if changelog and self.store.git.is_initialized():
            ts = batch[-1]["timestamp"]
            summary = f"dream: {ts}, {len(changelog)} change(s)"
            commit_msg = f"{summary}\n\n{analysis.strip()}"
            sha = self.store.git.auto_commit(commit_msg)
            if sha:
                logger.info("Dream commit: {}", sha)

        return True
