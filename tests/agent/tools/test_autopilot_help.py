from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nanobot.agent.tools.autopilot_help import AutopilotHelpTool
from nanobot.agent.tools.context import RequestContext
from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.cron.service import CronService
from nanobot.cron.types import CronSchedule
from nanobot.session.manager import SessionManager


class _NoTools:
    def get(self, _name: str) -> None:
        return None


class _FakeLoop:
    def __init__(
        self,
        *,
        bus: MessageBus,
        sessions: SessionManager,
        response: str,
    ) -> None:
        self.bus = bus
        self.sessions = sessions
        self.response = response
        self.tools = _NoTools()
        self.channels_config = SimpleNamespace(send_autonomous_progress=True)
        self.calls: list[dict[str, Any]] = []

    async def process_direct(
        self,
        content: str,
        *,
        session_key: str,
        channel: str,
        chat_id: str,
        metadata: dict[str, Any],
        on_progress,
        on_stream=None,
        on_stream_end=None,
    ) -> OutboundMessage:
        self.calls.append(
            {
                "content": content,
                "session_key": session_key,
                "channel": channel,
                "chat_id": chat_id,
                "metadata": metadata,
                "helper_messages": list(self.sessions.get_or_create(session_key).messages),
            }
        )
        await on_progress(
            "helper started",
            tool_events=[{
                "version": 1,
                "phase": "usage",
                "usage": {
                    "prompt_tokens": 111,
                    "completion_tokens": 22,
                    "cached_tokens": 33,
                },
            }],
        )
        return OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=self.response,
            metadata=metadata,
        )


def _add_job(cron: CronService, *, enabled: bool = True):
    job = cron.add_job(
        name="complex-reasoning-autopilot",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        message="continue research",
        kind="agent_turn_with_context",
        deliver=True,
        channel="feishu",
        to="ou_123",
        session_key="feishu:ou_123:main",
        context_session_key="feishu:ou_123:main",
        sync_session_key="feishu:ou_123:main",
        worker_session_key="cron:complex-reasoning-autopilot",
    )
    if not enabled:
        cron.enable_job(job.id, enabled=False)
    return job


@pytest.mark.asyncio
async def test_autopilot_help_syncs_main_context_and_publishes_helper_card(tmp_path: Path) -> None:
    sessions = SessionManager(tmp_path)
    main = sessions.get_or_create("feishu:ou_123:main")
    main.add_message("user", "主会话里的研究目标")
    main.add_message("assistant", "主会话里的已有结论")
    sessions.save(main)

    bus = MessageBus()
    cron = CronService(tmp_path / "cron" / "jobs.json")
    _add_job(cron)
    helper_response = json.dumps(
        {
            "decision": "revise",
            "reason": ["先缩小验证面板。"],
            "next_action": {"summary": "补齐验收标准后再实验。"},
        },
        ensure_ascii=False,
    )
    loop = _FakeLoop(bus=bus, sessions=sessions, response=helper_response)
    tool = AutopilotHelpTool(loop=loop, sessions=sessions, cron_service=cron, workspace=tmp_path)
    tool.set_context(
        RequestContext(
            channel="feishu",
            chat_id="ou_123",
            session_key="cron:complex-reasoning-autopilot",
            metadata={
                "_context_profile": "autopilot_worker",
                "_autopilot_sync_session_key": "feishu:ou_123:main",
                "_autopilot_worker_session_key": "cron:complex-reasoning-autopilot",
            },
        )
    )

    out = await tool.execute(
        request_type="idea_review",
        question="这个新 idea 是否值得进入实验？",
        current_state="已有 staged panel，idea 还缺少验收标准。",
        research_id="complex-reasoning",
    )

    assert "Decision: revise" in out
    assert "valid JSON object" in loop.calls[0]["content"]
    assert "no markdown fences" in loop.calls[0]["content"]
    assert "default job is to make the best bounded autonomous decision" in loop.calls[0]["content"]
    assert "Choose decision: human_required only when no safe autonomous option remains" in loop.calls[0]["content"]
    assert '"human_note": "..."' in loop.calls[0]["content"]
    assert "Helper Agent 消息" in loop.calls[0]["metadata"]["_source_title"]
    assert loop.calls[0]["metadata"]["_source_label"].startswith("Helper · sessions/helper_")
    assert loop.calls[0]["metadata"]["_context_profile"] == "autopilot_helper"
    assert loop.calls[0]["metadata"]["_stream_id"].startswith("helper:")
    assert [m["content"] for m in loop.calls[0]["helper_messages"]] == [
        "主会话里的研究目标",
        "主会话里的已有结论",
    ]

    progress = await bus.consume_outbound()
    final = await bus.consume_outbound()
    assert progress.metadata["_source_title"] == "Helper Agent 消息"
    assert progress.metadata["_tool_events"][0]["phase"] == "usage"
    assert json.loads(final.content)["decision"] == "revise"
    assert final.metadata["_source_title"] == "Helper Agent 消息"
    assert final.metadata["_source_label"].startswith("Helper · sessions/helper_")

    request_log = tmp_path / "research" / "complex-reasoning" / "help_requests.jsonl"
    response_log = tmp_path / "research" / "complex-reasoning" / "helper_responses.jsonl"
    request_payload = json.loads(request_log.read_text(encoding="utf-8").splitlines()[-1])
    response_payload = json.loads(response_log.read_text(encoding="utf-8").splitlines()[-1])
    assert request_payload["request_type"] == "idea_review"
    assert response_payload["decision"] == "revise"


@pytest.mark.asyncio
async def test_autopilot_help_pauses_cron_job_when_human_required(tmp_path: Path) -> None:
    sessions = SessionManager(tmp_path)
    main = sessions.get_or_create("feishu:ou_123:main")
    main.add_message("user", "主会话上下文")
    sessions.save(main)

    bus = MessageBus()
    cron = CronService(tmp_path / "cron" / "jobs.json")
    job = _add_job(cron)
    loop = _FakeLoop(
        bus=bus,
        sessions=sessions,
        response=json.dumps(
            {"decision": "human_required", "reason": ["需要用户选择。"]},
            ensure_ascii=False,
        ),
    )
    tool = AutopilotHelpTool(loop=loop, sessions=sessions, cron_service=cron, workspace=tmp_path)
    tool.set_context(
        RequestContext(
            channel="feishu",
            chat_id="ou_123",
            session_key="cron:complex-reasoning-autopilot",
            metadata={
                "_autopilot_sync_session_key": "feishu:ou_123:main",
                "_autopilot_worker_session_key": "cron:complex-reasoning-autopilot",
            },
        )
    )

    out = await tool.execute(
        request_type="human_escalation",
        question="是否需要暂停等待人类？",
        research_id="complex-reasoning",
    )

    assert "Decision: human_required" in out
    assert "Cron job paused: true" in out
    assert cron.get_job(job.id).enabled is False


@pytest.mark.asyncio
async def test_autopilot_help_reclones_main_session_on_every_call(tmp_path: Path) -> None:
    sessions = SessionManager(tmp_path)
    main = sessions.get_or_create("feishu:ou_123:main")
    main.add_message("user", "第一版主会话")
    sessions.save(main)

    bus = MessageBus()
    cron = CronService(tmp_path / "cron" / "jobs.json")
    _add_job(cron)
    loop = _FakeLoop(
        bus=bus,
        sessions=sessions,
        response=json.dumps({"decision": "approve", "reason": ["可以继续。"]}, ensure_ascii=False),
    )
    tool = AutopilotHelpTool(loop=loop, sessions=sessions, cron_service=cron, workspace=tmp_path)
    ctx = RequestContext(
        channel="feishu",
        chat_id="ou_123",
        session_key="cron:complex-reasoning-autopilot",
        metadata={
            "_autopilot_sync_session_key": "feishu:ou_123:main",
            "_autopilot_worker_session_key": "cron:complex-reasoning-autopilot",
        },
    )
    tool.set_context(ctx)

    await tool.execute(request_type="strategy_review", question="第一次审阅？")
    main = sessions.get_or_create("feishu:ou_123:main")
    main.add_message("assistant", "第二版新增结论")
    sessions.save(main)
    await tool.execute(request_type="strategy_review", question="第二次审阅？")

    first_messages = [m["content"] for m in loop.calls[0]["helper_messages"]]
    second_messages = [m["content"] for m in loop.calls[1]["helper_messages"]]
    assert first_messages == ["第一版主会话"]
    assert second_messages == ["第一版主会话", "第二版新增结论"]


@pytest.mark.asyncio
async def test_autopilot_help_can_suppress_helper_content_progress(tmp_path: Path) -> None:
    class _ProgressLoop(_FakeLoop):
        async def process_direct(
            self,
            content: str,
            *,
            session_key: str,
            channel: str,
            chat_id: str,
            metadata: dict[str, Any],
            on_progress,
            on_stream=None,
            on_stream_end=None,
        ) -> OutboundMessage:
            self.calls.append({
                "content": content,
                "session_key": session_key,
                "channel": channel,
                "chat_id": chat_id,
                "metadata": metadata,
                "helper_messages": list(self.sessions.get_or_create(session_key).messages),
            })
            await on_progress("helper model content before tool call")
            await on_progress(
                "tool hint text",
                tool_hint=True,
                tool_events=[{
                    "version": 1,
                    "phase": "usage",
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }],
            )
            return OutboundMessage(
                channel=channel,
                chat_id=chat_id,
                content=self.response,
                metadata=metadata,
            )

    sessions = SessionManager(tmp_path)
    main = sessions.get_or_create("feishu:ou_123:main")
    main.add_message("user", "主会话上下文")
    sessions.save(main)

    bus = MessageBus()
    cron = CronService(tmp_path / "cron" / "jobs.json")
    _add_job(cron)
    loop = _ProgressLoop(
        bus=bus,
        sessions=sessions,
        response=json.dumps({"decision": "approve"}, ensure_ascii=False),
    )
    loop.channels_config = SimpleNamespace(send_autonomous_progress=False)
    tool = AutopilotHelpTool(loop=loop, sessions=sessions, cron_service=cron, workspace=tmp_path)
    tool.set_context(
        RequestContext(
            channel="feishu",
            chat_id="ou_123",
            session_key="cron:complex-reasoning-autopilot",
            metadata={
                "_autopilot_sync_session_key": "feishu:ou_123:main",
                "_autopilot_worker_session_key": "cron:complex-reasoning-autopilot",
            },
        )
    )

    await tool.execute(request_type="strategy_review", question="是否继续？")

    progress = await bus.consume_outbound()
    final = await bus.consume_outbound()
    assert progress.content == ""
    assert progress.metadata["_tool_events"][0]["phase"] == "usage"
    assert "helper model content before tool call" not in final.content
    assert json.loads(final.content)["decision"] == "approve"


@pytest.mark.asyncio
async def test_autopilot_help_streams_when_origin_channel_wants_stream(tmp_path: Path) -> None:
    class _StreamingLoop(_FakeLoop):
        async def process_direct(
            self,
            content: str,
            *,
            session_key: str,
            channel: str,
            chat_id: str,
            metadata: dict[str, Any],
            on_progress,
            on_stream=None,
            on_stream_end=None,
        ) -> OutboundMessage:
            self.calls.append({
                "content": content,
                "session_key": session_key,
                "channel": channel,
                "chat_id": chat_id,
                "metadata": metadata,
                "helper_messages": list(self.sessions.get_or_create(session_key).messages),
            })
            assert on_stream is not None
            assert on_stream_end is not None
            await on_stream('{"decision": "approve"}')
            await on_stream_end(resuming=False)
            return OutboundMessage(
                channel=channel,
                chat_id=chat_id,
                content=self.response,
                metadata={**metadata, "_streamed": True},
            )

    sessions = SessionManager(tmp_path)
    main = sessions.get_or_create("feishu:ou_123:main")
    main.add_message("user", "主会话上下文")
    sessions.save(main)

    bus = MessageBus()
    cron = CronService(tmp_path / "cron" / "jobs.json")
    _add_job(cron)
    loop = _StreamingLoop(
        bus=bus,
        sessions=sessions,
        response=json.dumps({"decision": "approve"}, ensure_ascii=False),
    )
    tool = AutopilotHelpTool(loop=loop, sessions=sessions, cron_service=cron, workspace=tmp_path)
    tool.set_context(
        RequestContext(
            channel="feishu",
            chat_id="ou_123",
            session_key="cron:complex-reasoning-autopilot",
            metadata={
                "_wants_stream": True,
                "_autopilot_sync_session_key": "feishu:ou_123:main",
                "_autopilot_worker_session_key": "cron:complex-reasoning-autopilot",
            },
        )
    )

    await tool.execute(request_type="strategy_review", question="是否继续？")

    delta = await bus.consume_outbound()
    end = await bus.consume_outbound()
    final = await bus.consume_outbound()
    assert delta.metadata["_stream_delta"] is True
    assert delta.metadata["_source_title"] == "Helper Agent 消息"
    assert delta.metadata["_source_label"].startswith("Helper · sessions/helper_")
    assert end.metadata["_stream_end"] is True
    assert end.metadata["_stream_id"] == delta.metadata["_stream_id"]
    assert final.metadata["_streamed"] is True
