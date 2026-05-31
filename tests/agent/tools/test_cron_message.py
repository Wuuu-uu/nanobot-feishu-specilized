from __future__ import annotations

import json

import pytest

from nanobot.agent.tools.context import RequestContext
from nanobot.agent.tools.cron_message import CronMessageTool
from nanobot.cron.service import CronService
from nanobot.cron.types import CronSchedule
from nanobot.session.manager import SessionManager


@pytest.mark.asyncio
async def test_cron_message_injects_guidance_into_linked_worker(tmp_path):
    sessions = SessionManager(tmp_path)
    cron = CronService(tmp_path / "cron" / "jobs.json")
    job = cron.add_job(
        name="research-loop",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        message="wake up",
        kind="agent_turn_with_context",
        deliver=True,
        channel="feishu",
        to="ou_123",
        session_key="feishu:ou_123:main",
        context_session_key="feishu:ou_123:main",
        sync_session_key="feishu:ou_123:main",
        worker_session_key="cron:research-loop",
    )

    tool = CronMessageTool(sessions=sessions, cron_service=cron, workspace=tmp_path)
    tool.set_context(
        RequestContext(
            channel="feishu",
            chat_id="ou_123",
            session_key="feishu:ou_123:main",
        )
    )

    out = await tool.execute(
        action="send",
        message="下一轮改为先验收 staged panel。",
        subject="steer next turn",
        priority="high",
        research_id="complex-reasoning",
    )

    assert f"{job.name} ({job.id})" in out
    worker = sessions.get_or_create("cron:research-loop")
    assert worker.messages[-1]["role"] == "user"
    assert worker.messages[-1]["_cron_guidance"] is True
    assert "下一轮改为先验收 staged panel" in worker.messages[-1]["content"]

    inbox = tmp_path / "research" / "complex-reasoning" / "inbox.jsonl"
    payload = json.loads(inbox.read_text(encoding="utf-8").splitlines()[-1])
    assert payload["source_session"] == "feishu:ou_123:main"
    assert payload["target_worker_session"] == "cron:research-loop"
    assert payload["priority"] == "high"


@pytest.mark.asyncio
async def test_cron_message_uses_existing_complex_reasoning_research_dir(tmp_path):
    sessions = SessionManager(tmp_path)
    cron = CronService(tmp_path / "cron" / "jobs.json")
    (tmp_path / "research" / "complex-reasoning").mkdir(parents=True)
    cron.add_job(
        name="complex-reasoning-prompt-alignment-autopilot",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        message="wake up",
        kind="agent_turn_with_context",
        deliver=True,
        session_key="feishu:ou_123:main",
        context_session_key="feishu:ou_123:main",
        sync_session_key="feishu:ou_123:main",
        worker_session_key="cron:complex-reasoning-prompt-alignment-autopilot",
    )

    tool = CronMessageTool(sessions=sessions, cron_service=cron, workspace=tmp_path)
    tool.set_context(
        RequestContext(
            channel="feishu",
            chat_id="ou_123",
            session_key="feishu:ou_123:main",
        )
    )

    await tool.execute(action="send", message="下一轮验收 panel。", priority="high")

    inbox = tmp_path / "research" / "complex-reasoning" / "inbox.jsonl"
    assert inbox.exists()
    assert "下一轮验收 panel" in inbox.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_cron_message_requires_target_when_ambiguous(tmp_path):
    sessions = SessionManager(tmp_path)
    cron = CronService(tmp_path / "cron" / "jobs.json")
    for name in ("research-a", "research-b"):
        cron.add_job(
            name=name,
            schedule=CronSchedule(kind="every", every_ms=60_000),
            message="wake up",
            kind="agent_turn_with_context",
            deliver=True,
            worker_session_key=f"cron:{name}",
        )

    tool = CronMessageTool(sessions=sessions, cron_service=cron, workspace=tmp_path)
    out = await tool.execute(action="send", message="hello")

    assert "multiple autonomous cron workers" in out
