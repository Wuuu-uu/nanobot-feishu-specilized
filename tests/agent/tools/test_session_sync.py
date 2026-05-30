from __future__ import annotations

import json

import pytest

from nanobot.agent.tools.session_sync import SessionSyncContext, SessionSyncTool
from nanobot.session.manager import SessionManager


def _set_ts(message: dict, timestamp: str) -> None:
    message["timestamp"] = timestamp


@pytest.mark.asyncio
async def test_session_sync_fetches_updates_after_timestamp_and_omits_tool_messages(tmp_path):
    sessions = SessionManager(tmp_path)
    source = sessions.get_or_create("feishu:ou_main")
    source.add_message("user", "first")
    _set_ts(source.messages[-1], "2026-05-21T01:00:00")
    source.add_message("tool", "secret tool output", name="exec", tool_call_id="call_1")
    _set_ts(source.messages[-1], "2026-05-21T01:01:00")
    source.add_message("assistant", "synced marker", _channel_delivery=True)
    _set_ts(source.messages[-1], "2026-05-21T01:02:00")
    source.add_message("user", "new instruction")
    _set_ts(source.messages[-1], "2026-05-21T01:03:00")
    sessions.save(source)

    tool = SessionSyncTool(sessions)
    token = tool.set_sync_context(
        SessionSyncContext(
            context_session_key="feishu:ou_main",
            sync_session_key="feishu:ou_main",
            worker_session_key="cron:abc123",
            last_sync_message_timestamp="2026-05-21T01:02:00",
        )
    )
    try:
        out = await tool.execute(action="fetch_updates")
    finally:
        tool.reset_sync_context(token)

    payload = json.loads(out)
    assert payload["cursor_status"] == "matched"
    assert payload["returned_message_count"] == 1
    assert "new instruction" in "\n".join(payload["messages"])
    assert "secret tool output" not in "\n".join(payload["messages"])


@pytest.mark.asyncio
async def test_session_sync_missing_timestamp_treats_worker_as_unsynced(tmp_path):
    sessions = SessionManager(tmp_path)
    source = sessions.get_or_create("feishu:ou_main")
    for idx in range(3):
        source.add_message("user", f"message {idx}")
        _set_ts(source.messages[-1], f"2026-05-21T01:0{idx}:00")
    sessions.save(source)

    tool = SessionSyncTool(sessions)
    token = tool.set_sync_context(
        SessionSyncContext(
            context_session_key="feishu:ou_main",
            sync_session_key="feishu:ou_main",
            worker_session_key="cron:abc123",
            last_sync_message_timestamp="2026-05-21T09:09:09",
            sync_max_messages=2,
        )
    )
    try:
        out = await tool.execute(action="fetch_updates")
    finally:
        tool.reset_sync_context(token)

    payload = json.loads(out)
    assert payload["cursor_status"] == "missing"
    assert "not synchronized" in payload["sync_note"]
    joined = "\n".join(payload["messages"])
    assert "message 0" not in joined
    assert "message 1" in joined
    assert "message 2" in joined


@pytest.mark.asyncio
async def test_session_sync_rejects_non_cron_context(tmp_path):
    tool = SessionSyncTool(SessionManager(tmp_path))

    out = await tool.execute(action="check")

    assert "only available inside agent_turn_with_context" in out
