"""Cron tool for scheduling reminders and tasks."""

from typing import Any

from nanobot.agent.tools.base import Tool
from nanobot.cron.service import CronService
from nanobot.cron.types import CronSchedule


class CronTool(Tool):
    """Tool to schedule reminders and recurring tasks."""
    
    def __init__(self, cron_service: CronService):
        self._cron = cron_service
        self._channel = ""
        self._chat_id = ""
    
    def set_context(self, channel: str, chat_id: str) -> None:
        """Set the current session context for delivery."""
        self._channel = channel
        self._chat_id = chat_id
    
    @property
    def name(self) -> str:
        return "cron"
    
    @property
    def description(self) -> str:
        return "Schedule reminders and recurring tasks. Actions: add, list, remove, enable, run."
    
    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "list", "remove", "enable", "run"],
                    "description": "Action to perform"
                },
                "message": {
                    "type": "string",
                    "description": "Reminder/task message (for add, optional when command is provided)"
                },
                "command": {
                    "type": "string",
                    "description": "Shell command for deterministic exec cron job (for add)"
                },
                "every_seconds": {
                    "type": "integer",
                    "description": "Interval in seconds (for recurring tasks)"
                },
                "cron_expr": {
                    "type": "string",
                    "description": "Cron expression like '0 9 * * *' (for scheduled tasks)"
                },
                "at_time": {
                    "type": "string",
                    "description": "One-shot run time in ISO format, e.g. '2026-04-15T09:00:00'"
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": "Timeout for command/RUN: tasks (seconds)"
                },
                "deliver": {
                    "type": "boolean",
                    "description": "Deliver cron result to current channel. Defaults to true for message tasks and false for command/RUN jobs."
                },
                "enabled": {
                    "type": "boolean",
                    "description": "Enable or disable a job (for enable)"
                },
                "force": {
                    "type": "boolean",
                    "description": "Run disabled job anyway (for run)"
                },
                "job_id": {
                    "type": "string",
                    "description": "Job ID (for remove/enable/run)"
                }
            },
            "required": ["action"]
        }
    
    async def execute(
        self,
        action: str,
        message: str = "",
        command: str = "",
        every_seconds: int | None = None,
        cron_expr: str | None = None,
        at_time: str | None = None,
        timeout_seconds: int | None = None,
        deliver: bool | None = None,
        enabled: bool = True,
        force: bool = False,
        job_id: str | None = None,
        **kwargs: Any
    ) -> str:
        if action == "add":
            return self._add_job(message, command, every_seconds, cron_expr, at_time, timeout_seconds, deliver)
        elif action == "list":
            return self._list_jobs()
        elif action == "remove":
            return self._remove_job(job_id)
        elif action == "enable":
            return self._enable_job(job_id, enabled)
        elif action == "run":
            return await self._run_job(job_id, force)
        return f"Unknown action: {action}"
    
    def _add_job(
        self,
        message: str,
        command: str,
        every_seconds: int | None,
        cron_expr: str | None,
        at_time: str | None,
        timeout_seconds: int | None,
        deliver: bool | None,
    ) -> str:
        if not self._channel or not self._chat_id:
            return "Error: no session context (channel/chat_id)"
        if timeout_seconds is not None and timeout_seconds <= 0:
            return "Error: timeout_seconds must be > 0"
        
        # Build schedule
        if every_seconds:
            schedule = CronSchedule(kind="every", every_ms=every_seconds * 1000)
        elif cron_expr:
            schedule = CronSchedule(kind="cron", expr=cron_expr)
        elif at_time:
            import datetime

            try:
                dt = datetime.datetime.fromisoformat(at_time)
            except ValueError:
                return "Error: at_time must be valid ISO format like 2026-04-15T09:00:00"
            schedule = CronSchedule(kind="at", at_ms=int(dt.timestamp() * 1000))
        else:
            return "Error: one of every_seconds, cron_expr, or at_time is required"

        normalized_message = message.strip()
        normalized_command = (command or "").strip()
        payload_kind = "agent_turn"
        payload_command = ""
        payload_message = message

        if normalized_command:
            payload_kind = "exec"
            payload_command = normalized_command
            payload_message = message or f"RUN:{normalized_command}"
        elif normalized_message.startswith("RUN:"):
            payload_command = normalized_message[4:].strip()
            if not payload_command:
                return "Error: RUN: command is empty"
            payload_kind = "exec"
        elif not normalized_message:
            return "Error: message is required when command is not provided"

        job_name_source = payload_message if payload_message.strip() else f"RUN:{payload_command}"
        should_deliver = payload_kind != "exec" if deliver is None else deliver
        
        job = self._cron.add_job(
            name=job_name_source[:30],
            schedule=schedule,
            message=payload_message,
            kind=payload_kind,
            command=payload_command,
            timeout_s=timeout_seconds,
            deliver=should_deliver,
            channel=self._channel,
            to=self._chat_id,
        )
        return f"Created job '{job.name}' (id: {job.id})"
    
    def _list_jobs(self) -> str:
        jobs = self._cron.list_jobs()
        if not jobs:
            return "No scheduled jobs."
        lines = [f"- {j.name} (id: {j.id}, {j.schedule.kind})" for j in jobs]
        return "Scheduled jobs:\n" + "\n".join(lines)
    
    def _remove_job(self, job_id: str | None) -> str:
        if not job_id:
            return "Error: job_id is required for remove"
        if self._cron.remove_job(job_id):
            return f"Removed job {job_id}"
        return f"Job {job_id} not found"

    def _enable_job(self, job_id: str | None, enabled: bool) -> str:
        if not job_id:
            return "Error: job_id is required for enable"
        job = self._cron.enable_job(job_id, enabled=enabled)
        if not job:
            return f"Job {job_id} not found"
        return f"Job {job_id} {'enabled' if enabled else 'disabled'}"

    async def _run_job(self, job_id: str | None, force: bool) -> str:
        if not job_id:
            return "Error: job_id is required for run"
        ok = await self._cron.run_job(job_id, force=force)
        if ok:
            return f"Executed job {job_id}"
        return f"Failed to run job {job_id}"
