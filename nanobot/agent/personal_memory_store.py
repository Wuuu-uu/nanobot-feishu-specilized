"""Personal long-term memory store backed by SQLite."""

from __future__ import annotations

import json
import math
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nanobot.config.schema import MemorySystemConfig
from nanobot.utils.helpers import ensure_dir


KIND_WEIGHTS = {
    "constraint": 1.4,
    "preference": 1.3,
    "decision": 1.2,
    "reference": 1.0,
    "profile": 0.8,
}


class PersonalMemoryStore:
    """SQLite-backed canonical personal memory store."""

    def __init__(self, workspace: Path, config: MemorySystemConfig):
        self.workspace = workspace
        self.config = config
        self.db_path = Path(config.db_path).expanduser()
        ensure_dir(self.db_path.parent)
        self.memory_md_path = workspace / "memory" / "MEMORY.md"
        self.init_db()

    def init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    scope_key TEXT DEFAULT '',
                    slot TEXT DEFAULT '',
                    content TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    tags_json TEXT NOT NULL,
                    keywords_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 0,
                    evidence_count INTEGER NOT NULL DEFAULT 1,
                    first_seen_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_used_at TEXT,
                    source_refs_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_candidates (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    extracted_from TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    scope_key TEXT DEFAULT '',
                    slot TEXT DEFAULT '',
                    content TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    tags_json TEXT NOT NULL,
                    keywords_json TEXT NOT NULL,
                    source_refs_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    merged INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_events (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    memory_id TEXT,
                    candidate_id TEXT,
                    action TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    before_json TEXT,
                    after_json TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                    memory_id UNINDEXED,
                    text,
                    tokenize='trigram'
                )
                """
            )
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).astimezone().isoformat()

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False)

    @staticmethod
    def _loads_json(value: str | None, default: Any) -> Any:
        if not value:
            return default
        try:
            return json.loads(value)
        except Exception:
            return default

    def _row_to_memory(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        data = dict(row)
        for key in ("tags_json", "keywords_json", "source_refs_json"):
            parsed_key = key.replace("_json", "")
            data[parsed_key] = self._loads_json(data.pop(key, "[]"), [])
        return data

    def _upsert_fts(self, conn: sqlite3.Connection, memory_id: str, summary: str, content: str, tags: list[str], keywords: list[str]) -> None:
        conn.execute("DELETE FROM memory_fts WHERE memory_id = ?", (memory_id,))
        text = "\n".join([summary or "", content or "", " ".join(tags), " ".join(keywords)])
        conn.execute(
            "INSERT INTO memory_fts(memory_id, text) VALUES (?, ?)",
            (memory_id, text),
        )

    def add_candidates(self, candidates: list[dict[str, Any]], extracted_from: str, user_id: str | None = None) -> list[str]:
        if not candidates:
            return []
        user_id = user_id or self.config.default_user_id
        created_ids: list[str] = []
        with self._connect() as conn:
            for candidate in candidates[: max(1, self.config.max_candidates_per_run)]:
                cid = candidate.get("id") or f"cand_{uuid.uuid4().hex[:12]}"
                created_ids.append(cid)
                conn.execute(
                    """
                    INSERT OR REPLACE INTO memory_candidates(
                        id, user_id, extracted_from, kind, scope, scope_key, slot,
                        content, summary, tags_json, keywords_json, source_refs_json,
                        created_at, merged
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                    """,
                    (
                        cid,
                        user_id,
                        extracted_from,
                        candidate.get("kind", "reference"),
                        candidate.get("scope", "global"),
                        candidate.get("scope_key", ""),
                        candidate.get("slot", ""),
                        candidate.get("content", "").strip(),
                        candidate.get("summary", "").strip(),
                        self._json(candidate.get("tags", [])),
                        self._json(candidate.get("keywords", [])),
                        self._json(candidate.get("source_refs", [])),
                        self._now_iso(),
                    ),
                )
            conn.commit()
        return created_ids

    def get_unmerged_candidates(self, limit: int = 100, user_id: str | None = None) -> list[dict[str, Any]]:
        user_id = user_id or self.config.default_user_id
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM memory_candidates WHERE user_id = ? AND merged = 0 ORDER BY created_at ASC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        result = []
        for row in rows:
            data = dict(row)
            for key in ("tags_json", "keywords_json", "source_refs_json"):
                parsed_key = key.replace("_json", "")
                data[parsed_key] = self._loads_json(data.pop(key, "[]"), [])
            result.append(data)
        return result

    def find_related_memories(self, candidate: dict[str, Any], limit: int = 5, user_id: str | None = None) -> list[dict[str, Any]]:
        user_id = user_id or self.config.default_user_id
        slot = (candidate.get("slot") or "").strip()
        keywords = candidate.get("keywords") or []
        tags = candidate.get("tags") or []
        with self._connect() as conn:
            rows: list[sqlite3.Row] = []
            seen: set[str] = set()
            if slot:
                slot_rows = conn.execute(
                    "SELECT * FROM memories WHERE user_id = ? AND status = 'active' AND slot = ? ORDER BY updated_at DESC LIMIT ?",
                    (user_id, slot, limit),
                ).fetchall()
                for row in slot_rows:
                    seen.add(row["id"])
                    rows.append(row)
            tokens = [t for t in keywords + tags if isinstance(t, str) and t.strip()]
            if tokens and len(rows) < limit:
                like_sql = " OR ".join(["summary LIKE ?", "content LIKE ?"] * len(tokens))
                params: list[Any] = [user_id]
                for token in tokens:
                    like = f"%{token.strip()}%"
                    params.extend([like, like])
                params.append(limit * 3)
                query = (
                    "SELECT * FROM memories WHERE user_id = ? AND status = 'active' AND ("
                    + like_sql
                    + ") ORDER BY updated_at DESC LIMIT ?"
                )
                fuzzy_rows = conn.execute(query, tuple(params)).fetchall()
                for row in fuzzy_rows:
                    if row["id"] not in seen:
                        seen.add(row["id"])
                        rows.append(row)
            rows = rows[:limit]
        return [self._row_to_memory(row) for row in rows if row is not None]

    def get_memory(self, memory_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        return self._row_to_memory(row)

    def create_memory(self, memory: dict[str, Any], user_id: str | None = None) -> str:
        user_id = user_id or self.config.default_user_id
        memory_id = memory.get("id") or f"mem_{uuid.uuid4().hex[:12]}"
        now = self._now_iso()
        tags = list(dict.fromkeys(memory.get("tags", [])))
        keywords = list(dict.fromkeys(memory.get("keywords", [])))
        source_refs = list(dict.fromkeys(memory.get("source_refs", [])))
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO memories(
                    id, user_id, kind, scope, scope_key, slot, content, summary,
                    tags_json, keywords_json, status, priority, evidence_count,
                    first_seen_at, updated_at, last_used_at, source_refs_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, NULL, ?)
                """,
                (
                    memory_id,
                    user_id,
                    memory.get("kind", "reference"),
                    memory.get("scope", "global"),
                    memory.get("scope_key", ""),
                    memory.get("slot", ""),
                    memory.get("content", "").strip(),
                    memory.get("summary", memory.get("content", "")).strip(),
                    self._json(tags),
                    self._json(keywords),
                    int(memory.get("priority", 0) or 0),
                    int(memory.get("evidence_count", max(1, len(source_refs))) or 1),
                    now,
                    now,
                    self._json(source_refs),
                ),
            )
            self._upsert_fts(conn, memory_id, memory.get("summary", ""), memory.get("content", ""), tags, keywords)
            conn.commit()
        return memory_id

    def update_memory(self, memory_id: str, new_memory: dict[str, Any]) -> None:
        current = self.get_memory(memory_id)
        if not current:
            raise ValueError(f"Memory not found: {memory_id}")
        tags = list(dict.fromkeys((current.get("tags") or []) + (new_memory.get("tags") or [])))
        keywords = list(dict.fromkeys((current.get("keywords") or []) + (new_memory.get("keywords") or [])))
        source_refs = list(dict.fromkeys((current.get("source_refs") or []) + (new_memory.get("source_refs") or [])))
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE memories SET
                    kind = ?,
                    scope = ?,
                    scope_key = ?,
                    slot = ?,
                    content = ?,
                    summary = ?,
                    tags_json = ?,
                    keywords_json = ?,
                    status = 'active',
                    priority = ?,
                    evidence_count = ?,
                    updated_at = ?,
                    source_refs_json = ?
                WHERE id = ?
                """,
                (
                    new_memory.get("kind", current.get("kind", "reference")),
                    new_memory.get("scope", current.get("scope", "global")),
                    new_memory.get("scope_key", current.get("scope_key", "")),
                    new_memory.get("slot", current.get("slot", "")),
                    new_memory.get("content", current.get("content", "")).strip(),
                    new_memory.get("summary", current.get("summary", "")).strip(),
                    self._json(tags),
                    self._json(keywords),
                    int(new_memory.get("priority", current.get("priority", 0)) or 0),
                    max(int(current.get("evidence_count", 1) or 1), len(source_refs), int(new_memory.get("evidence_count", 1) or 1)),
                    self._now_iso(),
                    self._json(source_refs),
                    memory_id,
                ),
            )
            self._upsert_fts(
                conn,
                memory_id,
                new_memory.get("summary", current.get("summary", "")),
                new_memory.get("content", current.get("content", "")),
                tags,
                keywords,
            )
            conn.commit()

    def supersede_memory(self, old_id: str, new_memory: dict[str, Any], user_id: str | None = None) -> str:
        user_id = user_id or self.config.default_user_id
        with self._connect() as conn:
            conn.execute("UPDATE memories SET status = 'superseded', updated_at = ? WHERE id = ?", (self._now_iso(), old_id))
            conn.execute("DELETE FROM memory_fts WHERE memory_id = ?", (old_id,))
            conn.commit()
        return self.create_memory(new_memory, user_id=user_id)

    def archive_memory(self, memory_id: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE memories SET status = 'archived', updated_at = ? WHERE id = ?", (self._now_iso(), memory_id))
            conn.execute("DELETE FROM memory_fts WHERE memory_id = ?", (memory_id,))
            conn.commit()

    def mark_candidate_merged(self, candidate_id: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE memory_candidates SET merged = 1 WHERE id = ?", (candidate_id,))
            conn.commit()

    def log_event(self, event: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO memory_events(
                    id, user_id, memory_id, candidate_id, action, reason,
                    before_json, after_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.get("id") or f"evt_{uuid.uuid4().hex[:12]}",
                    event.get("user_id") or self.config.default_user_id,
                    event.get("memory_id"),
                    event.get("candidate_id"),
                    event.get("action", "noop"),
                    event.get("reason", ""),
                    self._json(event.get("before")) if event.get("before") is not None else None,
                    self._json(event.get("after")) if event.get("after") is not None else None,
                    event.get("created_at") or self._now_iso(),
                ),
            )
            conn.commit()

    def list_core_candidates(self, user_id: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        user_id = user_id or self.config.default_user_id
        limit = limit or self.config.core_memory_max_items
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM memories
                WHERE user_id = ? AND status = 'active' AND scope = 'global'
                ORDER BY priority DESC, updated_at DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        return [self._row_to_memory(row) for row in rows if row is not None]

    def list_active_memories(self, user_id: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        user_id = user_id or self.config.default_user_id
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM memories WHERE user_id = ? AND status = 'active' ORDER BY priority DESC, updated_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [self._row_to_memory(row) for row in rows if row is not None]

    def get_stats(self, user_id: str | None = None) -> dict[str, Any]:
        user_id = user_id or self.config.default_user_id
        with self._connect() as conn:
            active = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE user_id = ? AND status = 'active'",
                (user_id,),
            ).fetchone()[0]
            superseded = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE user_id = ? AND status = 'superseded'",
                (user_id,),
            ).fetchone()[0]
            archived = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE user_id = ? AND status = 'archived'",
                (user_id,),
            ).fetchone()[0]
            candidates_unmerged = conn.execute(
                "SELECT COUNT(*) FROM memory_candidates WHERE user_id = ? AND merged = 0",
                (user_id,),
            ).fetchone()[0]
            candidates_total = conn.execute(
                "SELECT COUNT(*) FROM memory_candidates WHERE user_id = ?",
                (user_id,),
            ).fetchone()[0]
            events_total = conn.execute(
                "SELECT COUNT(*) FROM memory_events WHERE user_id = ?",
                (user_id,),
            ).fetchone()[0]
            latest_update = conn.execute(
                "SELECT updated_at FROM memories WHERE user_id = ? AND status = 'active' ORDER BY updated_at DESC LIMIT 1",
                (user_id,),
            ).fetchone()
        return {
            "user_id": user_id,
            "active": int(active or 0),
            "superseded": int(superseded or 0),
            "archived": int(archived or 0),
            "candidates_unmerged": int(candidates_unmerged or 0),
            "candidates_total": int(candidates_total or 0),
            "events_total": int(events_total or 0),
            "latest_update": latest_update[0] if latest_update else None,
            "db_path": str(self.db_path),
        }

    def list_recent_events(self, user_id: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
        user_id = user_id or self.config.default_user_id
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM memory_events WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            data = dict(row)
            for key in ("before_json", "after_json"):
                parsed_key = key.replace("_json", "")
                data[parsed_key] = self._loads_json(data.pop(key, None), None)
            result.append(data)
        return result

    def retrieve(self, query: str, scope_hints: dict[str, Any] | None = None, top_k: int | None = None, user_id: str | None = None) -> list[dict[str, Any]]:
        user_id = user_id or self.config.default_user_id
        top_k = top_k or self.config.retrieval_top_k
        scope_hints = scope_hints or {}
        all_memories = self.list_active_memories(user_id=user_id, limit=500)
        if not all_memories:
            return []

        q = (query or "").lower().strip()
        q_tokens = self._tokenize(q)
        weights = self.config.retrieval_weights
        scored: list[tuple[float, dict[str, Any]]] = []
        now = datetime.now(timezone.utc)
        desired_scope = str(scope_hints.get("scope") or "").strip().lower()
        desired_scope_key = str(scope_hints.get("scope_key") or "").strip().lower()

        for memory in all_memories:
            summary = (memory.get("summary") or "").lower()
            content = (memory.get("content") or "").lower()
            tags = [str(x).lower() for x in memory.get("tags", [])]
            keywords = [str(x).lower() for x in memory.get("keywords", [])]
            token_score = sum(1 for t in q_tokens if t in summary or t in content)
            tag_score = sum(1 for t in q_tokens if t in tags)
            keyword_score = sum(1 for t in q_tokens if t in keywords)
            summary_score = sum(1 for t in q_tokens if t in summary)
            content_score = sum(1 for t in q_tokens if t in content)
            priority_score = float(memory.get("priority", 0) or 0)
            kind_score = KIND_WEIGHTS.get(str(memory.get("kind") or "reference"), 1.0)
            scope_score = 0.0
            if desired_scope and str(memory.get("scope") or "").lower() == desired_scope:
                scope_score += 1.0
            if desired_scope_key and str(memory.get("scope_key") or "").lower() == desired_scope_key:
                scope_score += 1.0
            recency_bonus = 0.0
            updated_at = memory.get("updated_at")
            try:
                updated_dt = datetime.fromisoformat(str(updated_at))
                age_days = max(0.0, (now - updated_dt).total_seconds() / 86400.0)
                recency_bonus = 1.0 / (1.0 + math.log1p(age_days + 1.0))
            except Exception:
                recency_bonus = 0.0

            score = (
                weights.keyword * (token_score + keyword_score)
                + weights.tag * tag_score
                + weights.summary * summary_score
                + weights.content * content_score
                + weights.priority * priority_score
                + weights.scope * scope_score
                + weights.recency * recency_bonus
                + weights.kind * kind_score
            )
            scored.append((score, memory))

        scored.sort(key=lambda item: (item[0], item[1].get("priority", 0), item[1].get("updated_at", "")), reverse=True)
        selected = [m for score, m in scored if score > 0][:top_k]
        if not selected:
            selected = self.list_core_candidates(user_id=user_id, limit=self.config.fallback_top_k)
        return selected

    def mark_used(self, memory_ids: list[str]) -> None:
        if not memory_ids:
            return
        now = self._now_iso()
        with self._connect() as conn:
            conn.executemany(
                "UPDATE memories SET last_used_at = ? WHERE id = ?",
                [(now, memory_id) for memory_id in memory_ids],
            )
            conn.commit()

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        if not text:
            return []
        rough = []
        buf = []
        for ch in text:
            if ch.isalnum() or ch in {"_", "-", "/", "."}:
                buf.append(ch)
            else:
                if buf:
                    rough.append("".join(buf))
                    buf = []
                if not ch.isspace():
                    rough.append(ch)
        if buf:
            rough.append("".join(buf))
        tokens = [t for t in rough if t and t.strip()]
        return list(dict.fromkeys(tokens))[:64]

    def sync_memory_md(self, extra_notes: list[str] | None = None, user_id: str | None = None) -> None:
        """Rewrite MEMORY.md index with integrated core memory section."""
        if not self.config.update_memory_md:
            return
        user_id = user_id or self.config.default_user_id
        existing = self.memory_md_path.read_text(encoding="utf-8") if self.memory_md_path.exists() else "# MEMORY\n"
        prefix, _, _ = existing.partition("## Auto Core Memory")
        prefix = prefix.rstrip()
        core_items = self.list_core_candidates(user_id=user_id, limit=self.config.core_memory_max_items)
        lines = []
        lines.append("## Auto Core Memory")
        lines.append("")
        if core_items:
            for item in core_items:
                slot = item.get("slot") or item.get("kind") or "memory"
                lines.append(f"- [{slot}] {item.get('summary') or item.get('content')}")
        else:
            lines.append("- (暂无自动核心记忆条目)")
        if extra_notes:
            lines.append("")
            lines.append("## Auto Memory System")
            lines.append("")
            for note in extra_notes:
                lines.append(f"- {note}")
        content = prefix + "\n\n" + "\n".join(lines) + "\n"
        self.memory_md_path.write_text(content, encoding="utf-8")
