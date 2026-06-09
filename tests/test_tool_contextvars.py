from __future__ import annotations

import asyncio

import pytest

from nanobot.agent.tools.cron import CronTool
from nanobot.agent.tools.message import MessageTool
from nanobot.agent.tools.spawn import SpawnTool
from nanobot.cron.service import CronService
from nanobot.cron.types import CronSchedule


@pytest.mark.asyncio
async def test_message_tool_keeps_task_local_context() -> None:
    seen: list[tuple[str, str, str]] = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def send_callback(msg):
        seen.append((msg.channel, msg.chat_id, msg.content))
        return None

    tool = MessageTool(send_callback=send_callback)

    async def task_one() -> str:
        tool.set_context("feishu", "chat-a")
        entered.set()
        await release.wait()
        return await tool.execute(content="one")

    async def task_two() -> str:
        await entered.wait()
        tool.set_context("email", "chat-b")
        release.set()
        return await tool.execute(content="two")

    result_one, result_two = await asyncio.gather(task_one(), task_two())

    assert result_one == "Message sent to feishu:chat-a"
    assert result_two == "Message sent to email:chat-b"
    assert ("feishu", "chat-a", "one") in seen
    assert ("email", "chat-b", "two") in seen


@pytest.mark.asyncio
async def test_spawn_tool_keeps_task_local_context() -> None:
    seen: list[tuple[str, str, str]] = []
    entered = asyncio.Event()
    release = asyncio.Event()

    class _Manager:
        async def spawn(
            self,
            *,
            task: str,
            label: str | None,
            origin_channel: str,
            origin_chat_id: str,
            session_key: str,
            origin_message_id: str | None = None,
        ) -> str:
            seen.append((origin_channel, origin_chat_id, session_key))
            return f"{origin_channel}:{origin_chat_id}:{task}"

    tool = SpawnTool(_Manager())

    async def task_one() -> str:
        tool.set_context("whatsapp", "chat-a")
        entered.set()
        await release.wait()
        return await tool.execute(task="one")

    async def task_two() -> str:
        await entered.wait()
        tool.set_context("telegram", "chat-b")
        release.set()
        return await tool.execute(task="two")

    result_one, result_two = await asyncio.gather(task_one(), task_two())

    assert result_one == "whatsapp:chat-a:one"
    assert result_two == "telegram:chat-b:two"
    assert ("whatsapp", "chat-a", "whatsapp:chat-a") in seen
    assert ("telegram", "chat-b", "telegram:chat-b") in seen


@pytest.mark.asyncio
async def test_cron_tool_keeps_task_local_context(tmp_path) -> None:
    tool = CronTool(CronService(tmp_path / "jobs.json"))
    entered = asyncio.Event()
    release = asyncio.Event()

    async def task_one() -> str:
        tool.set_context("feishu", "chat-a")
        entered.set()
        await release.wait()
        return await tool.execute(action="add", message="first", every_seconds=60)

    async def task_two() -> str:
        await entered.wait()
        tool.set_context("email", "chat-b")
        release.set()
        return await tool.execute(action="add", message="second", every_seconds=60)

    result_one, result_two = await asyncio.gather(task_one(), task_two())

    assert result_one.startswith("Created job")
    assert result_two.startswith("Created job")

    jobs = tool._cron.list_jobs()
    assert {job.payload.channel for job in jobs} == {"feishu", "email"}
    assert {job.payload.to for job in jobs} == {"chat-a", "chat-b"}


# --- Basic single-task regression tests ---


@pytest.mark.asyncio
async def test_message_tool_basic_set_context_and_execute() -> None:
    """Single task: set_context then execute should route correctly."""
    seen: list[tuple[str, str, str]] = []

    async def send_callback(msg):
        seen.append((msg.channel, msg.chat_id, msg.content))

    tool = MessageTool(send_callback=send_callback)
    tool.set_context("telegram", "chat-123", "msg-456")

    result = await tool.execute(content="hello")
    assert result == "Message sent to telegram:chat-123"
    assert seen == [("telegram", "chat-123", "hello")]


@pytest.mark.asyncio
async def test_message_tool_default_values_without_set_context() -> None:
    """Without set_context, constructor defaults should be used."""
    seen: list[tuple[str, str, str]] = []

    async def send_callback(msg):
        seen.append((msg.channel, msg.chat_id, msg.content))

    tool = MessageTool(
        send_callback=send_callback,
        default_channel="discord",
        default_chat_id="general",
    )

    result = await tool.execute(content="hi")
    assert result == "Message sent to discord:general"
    assert seen == [("discord", "general", "hi")]


@pytest.mark.asyncio
async def test_spawn_tool_basic_set_context_and_execute() -> None:
    """Single task: set_context then execute should pass correct origin."""
    seen: list[tuple[str, str, str]] = []

    class _Manager:
        async def spawn(
            self,
            *,
            task,
            label,
            origin_channel,
            origin_chat_id,
            session_key,
            origin_message_id=None,
        ):
            seen.append((origin_channel, origin_chat_id, session_key))
            return f"ok: {task}"

    tool = SpawnTool(_Manager())
    tool.set_context("feishu", "chat-abc")

    result = await tool.execute(task="do something")
    assert result == "ok: do something"
    assert seen == [("feishu", "chat-abc", "feishu:chat-abc")]


@pytest.mark.asyncio
async def test_spawn_tool_default_values_without_set_context() -> None:
    """Without set_context, default cli:direct should be used."""
    seen: list[tuple[str, str, str]] = []

    class _Manager:
        async def spawn(
            self,
            *,
            task,
            label,
            origin_channel,
            origin_chat_id,
            session_key,
            origin_message_id=None,
        ):
            seen.append((origin_channel, origin_chat_id, session_key))
            return "ok"

    tool = SpawnTool(_Manager())

    await tool.execute(task="test")
    assert seen == [("cli", "direct", "cli:direct")]


@pytest.mark.asyncio
async def test_cron_tool_basic_set_context_and_execute(tmp_path) -> None:
    """Single task: set_context then add job should use correct target."""
    tool = CronTool(CronService(tmp_path / "jobs.json"))
    tool.set_context("wechat", "user-789")

    result = await tool.execute(action="add", message="standup", every_seconds=300)
    assert result.startswith("Created job")

    jobs = tool._cron.list_jobs()
    assert len(jobs) == 1
    assert jobs[0].payload.channel == "wechat"
    assert jobs[0].payload.to == "user-789"


@pytest.mark.asyncio
async def test_cron_tool_context_job_defaults_to_current_session(tmp_path) -> None:
    tool = CronTool(CronService(tmp_path / "jobs.json"))
    tool.set_context("feishu", "ou_123", session_key="feishu:ou_123:main")

    result = await tool.execute(
        action="add",
        kind="agent_turn_with_context",
        message="monitor progress",
        every_seconds=1800,
        research_id="demo-research",
    )

    assert result.startswith("Created job")
    jobs = tool._cron.list_jobs()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.payload.kind == "agent_turn_with_context"
    assert job.payload.deliver is True
    assert job.payload.session_key == "feishu:ou_123:main"
    assert job.payload.context_session_key == "feishu:ou_123:main"
    assert job.payload.sync_session_key == "feishu:ou_123:main"
    assert job.payload.worker_session_key is None
    assert job.payload.research_id == "demo-research"


@pytest.mark.asyncio
async def test_cron_tool_run_autonomous_worker_defaults_to_async_status(tmp_path) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow(_job):
        entered.set()
        await release.wait()
        return "autopilot final report"

    service = CronService(tmp_path / "jobs.json", on_job=slow)
    tool = CronTool(service)
    job = service.add_job(
        name="autopilot",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        message="continue autonomous research",
        kind="agent_turn_with_context",
        deliver=True,
        context_session_key="feishu:main",
        sync_session_key="feishu:main",
        worker_session_key="cron:autopilot",
        research_id="demo-research",
    )

    result = await tool.execute(action="run", job_id=job.id)

    assert result.startswith(f"Triggered job {job.id}")
    assert "Status: running" in result
    assert "Worker session: cron:autopilot" in result
    assert "Sync session: feishu:main" in result
    assert "Inspect later with cron(action='status'" in result
    assert entered.is_set()

    release.set()
    deadline = asyncio.get_running_loop().time() + 1.0
    while asyncio.get_running_loop().time() < deadline:
        status = await tool.execute(action="status", job_id=job.id)
        if "Status: running" not in status and "autopilot final report" in status:
            break
        await asyncio.sleep(0.01)

    status = await tool.execute(action="status", job_id=job.id)
    assert "Latest result preview:\nautopilot final report" in status


@pytest.mark.asyncio
async def test_cron_tool_no_context_returns_error(tmp_path) -> None:
    """Without set_context, add should fail with a clear error."""
    tool = CronTool(CronService(tmp_path / "jobs.json"))

    result = await tool.execute(action="add", message="test", every_seconds=60)
    assert result == "Error: no session context (channel/chat_id)"
