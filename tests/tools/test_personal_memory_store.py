from pathlib import Path

from nanobot.agent.personal_memory_store import PersonalMemoryStore
from nanobot.config.schema import MemorySystemConfig


def _make_store(tmp_path: Path, *, update_memory_md: bool = True) -> PersonalMemoryStore:
    workspace = tmp_path / "workspace"
    (workspace / "memory").mkdir(parents=True, exist_ok=True)
    (workspace / "memory" / "MEMORY.md").write_text("# MEMORY\n", encoding="utf-8")
    cfg = MemorySystemConfig(
        enabled=True,
        db_path=str(workspace / "memory" / "personal_memory.db"),
        default_user_id="test-user",
        retrieval_top_k=5,
        fallback_top_k=2,
        core_memory_max_items=8,
        update_memory_md=update_memory_md,
    )
    return PersonalMemoryStore(workspace, cfg)


def test_memory_store_create_retrieve_and_stats(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.create_memory({
        "kind": "preference",
        "scope": "global",
        "slot": "preference.reply.language",
        "content": "Default reply language is Chinese.",
        "summary": "Default reply language is Chinese",
        "tags": ["language", "Chinese"],
        "keywords": ["Chinese", "reply", "language"],
        "priority": 8,
        "source_refs": ["test:a"],
    })
    store.create_memory({
        "kind": "decision",
        "scope": "topic",
        "scope_key": "nanobot-memory",
        "slot": "decision.memory.architecture",
        "content": "Use SQLite retrieval as an optional personal memory layer.",
        "summary": "Use SQLite retrieval as optional personal memory",
        "tags": ["memory", "architecture"],
        "keywords": ["memory", "retrieval", "database"],
        "priority": 6,
        "source_refs": ["test:b"],
    })

    hits = store.retrieve("please answer in Chinese", top_k=3)
    assert hits
    assert hits[0]["slot"] == "preference.reply.language"

    stats = store.get_stats()
    assert stats["active"] == 2
    assert stats["candidates_total"] == 0
    assert Path(stats["db_path"]).exists()


def test_memory_store_fallback_returns_core_memory(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.create_memory({
        "kind": "reference",
        "scope": "global",
        "slot": "reference.user.school",
        "content": "The user studied at Shanghai Jiao Tong University.",
        "summary": "The user studied at Shanghai Jiao Tong University",
        "tags": ["SJTU"],
        "keywords": ["Shanghai Jiao Tong University", "SJTU"],
        "priority": 9,
        "source_refs": ["test:c"],
    })

    hits = store.retrieve("completely unrelated query zxqv", top_k=3)
    assert len(hits) == 1
    assert hits[0]["slot"] == "reference.user.school"


def test_sync_memory_md_writes_auto_sections_when_enabled(tmp_path: Path) -> None:
    store = _make_store(tmp_path, update_memory_md=True)
    store.create_memory({
        "kind": "preference",
        "scope": "global",
        "slot": "preference.reply.style",
        "content": "Replies should be concise and structured.",
        "summary": "Replies should be concise and structured",
        "tags": ["style"],
        "keywords": ["concise", "structured"],
        "priority": 7,
        "source_refs": ["test:d"],
    })

    store.sync_memory_md(extra_notes=["personal_memory.db: test.db", "retrieval top-k: 5"])
    text = (tmp_path / "workspace" / "memory" / "MEMORY.md").read_text(encoding="utf-8")
    assert "## Auto Core Memory" in text
    assert "[preference.reply.style] Replies should be concise and structured" in text
    assert "## Auto Memory System" in text
    assert "personal_memory.db: test.db" in text


def test_sync_memory_md_is_disabled_by_default(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "memory").mkdir(parents=True, exist_ok=True)
    memory_md = workspace / "memory" / "MEMORY.md"
    memory_md.write_text("# MEMORY\n", encoding="utf-8")
    cfg = MemorySystemConfig(
        enabled=True,
        db_path=str(workspace / "memory" / "personal_memory.db"),
        default_user_id="test-user",
    )
    store = PersonalMemoryStore(workspace, cfg)

    store.sync_memory_md(extra_notes=["should not be written"])

    assert memory_md.read_text(encoding="utf-8") == "# MEMORY\n"
