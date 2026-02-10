"""Session management for conversation history."""

import json
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from loguru import logger

from nanobot.utils.helpers import ensure_dir, safe_filename, truncate_string


@dataclass
class Session:
    """
    A conversation session.
    
    Stores messages in JSONL format for easy reading and persistence.
    """
    
    key: str  # channel:chat_id
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)
    
    def add_message(self, role: str, content: str, **kwargs: Any) -> None:
        """Add a message to the session."""
        msg = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
            **kwargs
        }
        self.messages.append(msg)
        self.updated_at = datetime.now()
    
    def get_history(self, max_messages: int = 50) -> list[dict[str, Any]]:
        """
        Get message history for LLM context.
        
        Args:
            max_messages: Maximum messages to return.
        
        Returns:
            List of messages in LLM format.
        """
        # Get recent messages
        recent = self.messages[-max_messages:] if len(self.messages) > max_messages else self.messages
        
        # Convert to LLM format (just role and content)
        return [{"role": m["role"], "content": m["content"]} for m in recent]
    
    def clear(self) -> None:
        """Clear all messages in the session."""
        self.messages = []
        self.updated_at = datetime.now()


class SessionManager:
    """
    Manages conversation sessions.
    
    Sessions are stored as JSONL files in the sessions directory.
    """
    
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.sessions_dir = ensure_dir(Path.home() / ".nanobot" / "sessions")
        self._cache: dict[str, Session] = {}
        self._active_path = self.sessions_dir / "_active.json"
        self._active_map = self._load_active_map()

    def _load_active_map(self) -> dict[str, str]:
        if not self._active_path.exists():
            return {}
        try:
            data = json.loads(self._active_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
        except Exception as e:
            logger.warning(f"Failed to load active sessions map: {e}")
        return {}

    def _save_active_map(self) -> None:
        try:
            self._active_path.write_text(
                json.dumps(self._active_map, ensure_ascii=True, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"Failed to save active sessions map: {e}")
    
    def _get_session_path(self, key: str) -> Path:
        """Get the file path for a session."""
        safe_key = safe_filename(key.replace(":", "_"))
        return self.sessions_dir / f"{safe_key}.jsonl"

    def session_exists(self, key: str) -> bool:
        """Check if a session exists on disk or in cache."""
        return key in self._cache or self._get_session_path(key).exists()

    def get_active_session_key(self, channel: str, chat_id: str) -> str | None:
        """Get active session key for a channel/chat pair, if set."""
        map_key = f"{channel}:{chat_id}"
        session_key = self._active_map.get(map_key)
        if session_key and not self.session_exists(session_key):
            self._active_map.pop(map_key, None)
            self._save_active_map()
            return None
        return session_key

    def set_active_session_key(self, channel: str, chat_id: str, session_key: str) -> None:
        """Set active session key for a channel/chat pair."""
        map_key = f"{channel}:{chat_id}"
        self._active_map[map_key] = session_key
        self._save_active_map()

    def clear_active_session_key(self, channel: str, chat_id: str) -> None:
        """Clear active session key for a channel/chat pair."""
        map_key = f"{channel}:{chat_id}"
        if map_key in self._active_map:
            self._active_map.pop(map_key, None)
            self._save_active_map()
    
    def get_or_create(self, key: str) -> Session:
        """
        Get an existing session or create a new one.
        
        Args:
            key: Session key (usually channel:chat_id).
        
        Returns:
            The session.
        """
        # Check cache
        if key in self._cache:
            return self._cache[key]
        
        # Try to load from disk
        session = self._load(key)
        if session is None:
            session = Session(key=key)
        
        self._cache[key] = session
        return session
    
    def _load(self, key: str) -> Session | None:
        """Load a session from disk."""
        path = self._get_session_path(key)
        
        if not path.exists():
            return None
        
        try:
            messages = []
            metadata = {}
            created_at = None
            
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    
                    data = json.loads(line)
                    
                    if data.get("_type") == "metadata":
                        metadata = data.get("metadata", {})
                        created_at = datetime.fromisoformat(data["created_at"]) if data.get("created_at") else None
                    else:
                        messages.append(data)
            
            return Session(
                key=key,
                messages=messages,
                created_at=created_at or datetime.now(),
                metadata=metadata
            )
        except Exception as e:
            logger.warning(f"Failed to load session {key}: {e}")
            return None
    
    def save(self, session: Session) -> None:
        """Save a session to disk."""
        path = self._get_session_path(session.key)

        if "title" not in session.metadata:
            session.metadata["title"] = self._derive_title_from_messages(session)
        session.metadata.setdefault("key", session.key)
        
        with open(path, "w") as f:
            # Write metadata first
            metadata_line = {
                "_type": "metadata",
                "created_at": session.created_at.isoformat(),
                "updated_at": session.updated_at.isoformat(),
                "metadata": session.metadata,
            }
            f.write(json.dumps(metadata_line) + "\n")
            
            # Write messages
            for msg in session.messages:
                f.write(json.dumps(msg) + "\n")
        
        self._cache[session.key] = session
    
    def delete(self, key: str) -> bool:
        """
        Delete a session.
        
        Args:
            key: Session key.
        
        Returns:
            True if deleted, False if not found.
        """
        # Remove from cache
        self._cache.pop(key, None)
        
        # Remove file
        path = self._get_session_path(key)
        if path.exists():
            path.unlink()
            return True
        return False
    
    def list_sessions(self) -> list[dict[str, Any]]:
        """
        List all sessions.
        
        Returns:
            List of session info dicts.
        """
        sessions = []
        
        for path in self.sessions_dir.glob("*.jsonl"):
            try:
                # Read just the metadata line
                with open(path) as f:
                    first_line = f.readline().strip()
                    if first_line:
                        data = json.loads(first_line)
                        if data.get("_type") == "metadata":
                            meta = data.get("metadata", {}) if isinstance(data.get("metadata"), dict) else {}
                            key = meta.get("key") or path.stem.replace("_", ":")
                            title = meta.get("title") or self._infer_title_from_file(path)
                            sessions.append({
                                "key": key,
                                "title": title,
                                "created_at": data.get("created_at"),
                                "updated_at": data.get("updated_at"),
                                "path": str(path)
                            })
            except Exception:
                continue
        
        return sorted(sessions, key=lambda x: x.get("updated_at", ""), reverse=True)

    def get_session_title(self, key: str) -> str | None:
        """Get the title of a session if available."""
        session = self._cache.get(key)
        if session and session.metadata.get("title"):
            return session.metadata.get("title")
        path = self._get_session_path(key)
        if not path.exists():
            return None
        try:
            with open(path) as f:
                first_line = f.readline().strip()
                if first_line:
                    data = json.loads(first_line)
                    if data.get("_type") == "metadata":
                        meta = data.get("metadata", {}) if isinstance(data.get("metadata"), dict) else {}
                        title = meta.get("title")
                        if title:
                            return str(title)
        except Exception:
            return None
        return self._infer_title_from_file(path)

    def _derive_title_from_messages(self, session: Session) -> str:
        for msg in session.messages:
            if msg.get("role") == "user" and isinstance(msg.get("content"), str):
                text = msg.get("content", "").strip()
                if text:
                    return truncate_string(text, max_len=60)
        stamp = session.created_at.strftime("%Y-%m-%d %H:%M")
        return f"Session {stamp}"

    def _infer_title_from_file(self, path: Path) -> str:
        try:
            with open(path) as f:
                # Skip metadata
                _ = f.readline()
                for _ in range(50):
                    line = f.readline()
                    if not line:
                        break
                    data = json.loads(line)
                    if data.get("role") == "user" and isinstance(data.get("content"), str):
                        text = data.get("content", "").strip()
                        if text:
                            return truncate_string(text, max_len=60)
        except Exception:
            pass
        return ""
