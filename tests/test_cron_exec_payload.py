import asyncio
import datetime
import json

import pytest

from nanobot.agent.tools.cron import CronTool
from nanobot.cron.service import CronService
from nanobot.cron.types import CronSchedule


def test_add_exec_job_persists_payload_fields(tmp_path):
    store_path = tmp_path / "jobs.json"
    service = CronService(store_path)

    schedule = CronSchedule(kind="every", every_ms=30_000)
    created = service.add_job(
        name="run-script",
        schedule=schedule,
        kind="exec",
        command="echo hello",
        timeout_s=45,
    )

    assert created.payload.kind == "exec"
    assert created.payload.command == "echo hello"
    assert created.payload.timeout_s == 45

    data = json.loads(store_path.read_text())
    payload = data["jobs"][0]["payload"]
    assert payload["kind"] == "exec"
    assert payload["command"] == "echo hello"
    assert payload["timeoutS"] == 45

    reloaded = CronService(store_path)
    job = reloaded.list_jobs(include_disabled=True)[0]
    assert job.payload.kind == "exec"
    assert job.payload.command == "echo hello"
    assert job.payload.timeout_s == 45


def test_add_exec_job_accepts_legacy_run_message(tmp_path):
    store_path = tmp_path / "jobs.json"
    service = CronService(store_path)

    schedule = CronSchedule(kind="every", every_ms=10_000)
    job = service.add_job(
        name="legacy-run",
        schedule=schedule,
        kind="exec",
        message="RUN:python scripts/do_work.py",
    )

    assert job.payload.command == "python scripts/do_work.py"


def test_add_non_exec_job_requires_message(tmp_path):
    store_path = tmp_path / "jobs.json"
    service = CronService(store_path)

    with pytest.raises(ValueError, match="message is required"):
        service.add_job(
            name="missing-message",
            schedule=CronSchedule(kind="every", every_ms=5_000),
            kind="agent_turn",
            message="",
        )


def test_cron_tool_maps_run_message_to_exec_payload(tmp_path):
    store_path = tmp_path / "jobs.json"
    service = CronService(store_path)
    tool = CronTool(service)
    tool.set_context("feishu", "chat-1")

    result = asyncio.run(
        tool.execute(
            action="add",
            message="RUN:echo via-tool",
            every_seconds=30,
            timeout_seconds=12,
        )
    )

    assert "Created job" in result
    job = service.list_jobs(include_disabled=True)[0]
    assert job.payload.kind == "exec"
    assert job.payload.command == "echo via-tool"
    assert job.payload.timeout_s == 12
    assert job.payload.deliver is False


def test_cron_tool_accepts_command_mode(tmp_path):
    store_path = tmp_path / "jobs.json"
    service = CronService(store_path)
    tool = CronTool(service)
    tool.set_context("feishu", "chat-2")

    result = asyncio.run(
        tool.execute(
            action="add",
            command="echo via-command",
            every_seconds=45,
            timeout_seconds=20,
        )
    )

    assert "Created job" in result
    job = service.list_jobs(include_disabled=True)[0]
    assert job.payload.kind == "exec"
    assert job.payload.command == "echo via-command"
    assert job.payload.message == "RUN:echo via-command"
    assert job.payload.timeout_s == 20
    assert job.payload.deliver is False


def test_cron_tool_message_mode_still_delivers_by_default(tmp_path):
    store_path = tmp_path / "jobs.json"
    service = CronService(store_path)
    tool = CronTool(service)
    tool.set_context("feishu", "chat-reminder")

    result = asyncio.run(
        tool.execute(
            action="add",
            message="drink water",
            every_seconds=45,
        )
    )

    assert "Created job" in result
    job = service.list_jobs(include_disabled=True)[0]
    assert job.payload.kind == "agent_turn"
    assert job.payload.deliver is True


def test_cron_tool_can_explicitly_deliver_command_result(tmp_path):
    store_path = tmp_path / "jobs.json"
    service = CronService(store_path)
    tool = CronTool(service)
    tool.set_context("feishu", "chat-command")

    result = asyncio.run(
        tool.execute(
            action="add",
            command="echo opt-in",
            every_seconds=45,
            deliver=True,
        )
    )

    assert "Created job" in result
    job = service.list_jobs(include_disabled=True)[0]
    assert job.payload.kind == "exec"
    assert job.payload.deliver is True


def test_cron_tool_enable_and_run_actions(tmp_path):
    store_path = tmp_path / "jobs.json"
    service = CronService(store_path)
    created = service.add_job(
        name="plain-job",
        schedule=CronSchedule(kind="every", every_ms=10_000),
        message="do something",
    )

    tool = CronTool(service)
    tool.set_context("feishu", "chat-3")

    disabled = asyncio.run(tool.execute(action="enable", job_id=created.id, enabled=False))
    assert "disabled" in disabled

    run_without_force = asyncio.run(tool.execute(action="run", job_id=created.id, force=False))
    assert "Failed to run" in run_without_force

    run_with_force = asyncio.run(tool.execute(action="run", job_id=created.id, force=True))
    assert "Executed job" in run_with_force


def test_cron_tool_supports_one_shot_at_time(tmp_path):
    store_path = tmp_path / "jobs.json"
    service = CronService(store_path)
    tool = CronTool(service)
    tool.set_context("feishu", "chat-4")

    run_at = (datetime.datetime.now() + datetime.timedelta(hours=1)).replace(microsecond=0).isoformat()
    result = asyncio.run(
        tool.execute(
            action="add",
            command="echo one-shot",
            at_time=run_at,
            timeout_seconds=25,
        )
    )

    assert "Created job" in result
    job = service.list_jobs(include_disabled=True)[0]
    assert job.schedule.kind == "at"
    assert job.payload.kind == "exec"
    assert job.payload.command == "echo one-shot"
