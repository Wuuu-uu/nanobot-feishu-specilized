from pathlib import Path
from unittest.mock import MagicMock

from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.autopilot_help import AutopilotHelpTool
from nanobot.agent.tools.cron import CronTool
from nanobot.agent.tools.cron_message import CronMessageTool
from nanobot.bus.queue import MessageBus
from nanobot.cron.service import CronService


def test_agent_loop_registers_cron_tool_with_configured_timezone(tmp_path: Path) -> None:
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"

    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        cron_service=CronService(tmp_path / "cron" / "jobs.json"),
        timezone="Asia/Shanghai",
    )

    cron_tool = loop.tools.get("cron")
    autopilot_help_tool = loop.tools.get("autopilot_help")
    cron_message_tool = loop.tools.get("cron_message")

    assert isinstance(cron_tool, CronTool)
    assert cron_tool._default_timezone == "Asia/Shanghai"
    assert isinstance(autopilot_help_tool, AutopilotHelpTool)
    assert isinstance(cron_message_tool, CronMessageTool)
