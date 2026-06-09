"""Cron tool for scheduling reminders and tasks."""

from contextvars import ContextVar
from datetime import datetime
from typing import Any

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import (
    BooleanSchema,
    IntegerSchema,
    StringSchema,
    tool_parameters_schema,
)
from nanobot.cron.service import CronService
from nanobot.cron.types import CronJob, CronJobState, CronSchedule
from nanobot.utils.helpers import truncate_string

_CRON_PARAMETERS = tool_parameters_schema(
    action=StringSchema("Action to perform", enum=["add", "list", "remove", "enable", "run", "status"]),
    name=StringSchema(
        "Optional short human-readable label for the job "
        "(e.g., 'weather-monitor', 'daily-standup'). Defaults to first 30 chars of message."
    ),
    message=StringSchema(
        "REQUIRED when action='add' unless command is provided. Instruction for the agent "
        "to execute when the job triggers (e.g., 'Send a reminder to WeChat: xxx' or "
        "'Check system status and report'). Prefix with RUN: for deterministic exec jobs."
    ),
    kind=StringSchema(
        "Optional job type for action='add'. Use agent_turn for ordinary scheduled agent "
        "messages, agent_turn_with_context for an autonomous worker session with lightweight "
        "main-session delivery/linkage, or exec for deterministic commands.",
        enum=["agent_turn", "agent_turn_with_context", "exec"],
    ),
    command=StringSchema(
        "Optional shell command for deterministic exec cron jobs when action='add'. "
        "Executed through nanobot's exec tool policy when the job fires."
    ),
    context_session_key=StringSchema(
        "Optional for kind='agent_turn_with_context': main session key that owns/steers "
        "the worker. Defaults to the current session."
    ),
    sync_session_key=StringSchema(
        "Optional for kind='agent_turn_with_context': session key that receives the "
        "worker's lightweight tagged progress summaries. Defaults to context_session_key."
    ),
    worker_session_key=StringSchema(
        "Optional for kind='agent_turn_with_context': independent worker session key. "
        "Defaults to cron:<job_id>. Do not set this to the main/session sync key."
    ),
    research_id=StringSchema(
        "Optional for kind='agent_turn_with_context': workspace/research/<id> state directory "
        "used by the autonomous worker profile. Defaults to the job name or worker key."
    ),
    sync_max_messages=IntegerSchema(
        12,
        description="Legacy compatibility field for older context-sync jobs; ignored by new cron workers.",
        minimum=1,
        maximum=100,
    ),
    sync_max_chars=IntegerSchema(
        12000,
        description="Legacy compatibility field for older context-sync jobs; ignored by new cron workers.",
        minimum=1000,
        maximum=100000,
    ),
    every_seconds=IntegerSchema(0, description="Interval in seconds (for recurring tasks)"),
    cron_expr=StringSchema("Cron expression like '0 9 * * *' (for scheduled tasks)"),
    tz=StringSchema(
        "Optional IANA timezone for cron expressions (e.g. 'America/Vancouver'). "
        "When omitted with cron_expr, the tool's default timezone applies."
    ),
    at=StringSchema(
        "ISO datetime for one-time execution (e.g. '2026-02-12T10:30:00'). "
        "Naive values use the tool's default timezone."
    ),
    at_time=StringSchema("Legacy alias for at."),
    timeout_seconds=IntegerSchema(
        0,
        description="Optional timeout in seconds for command/RUN cron jobs",
        minimum=1,
        maximum=600,
    ),
    deliver=BooleanSchema(
        description=(
            "Whether to deliver the execution result to the user channel. "
            "When omitted, message jobs deliver by default and command/RUN jobs do not."
        ),
        nullable=True,
    ),
    enabled=BooleanSchema(description="Whether to enable or disable a job for action='enable'."),
    force=BooleanSchema(description="Run a disabled job anyway for action='run'.", default=False),
    wait=BooleanSchema(
        description=(
            "For action='run': whether to wait for the triggered run to finish. "
            "Defaults to false for agent_turn_with_context autonomous workers and true for short jobs."
        ),
        nullable=True,
    ),
    wait_timeout_seconds=IntegerSchema(
        0,
        description=(
            "For action='run' with wait=true: maximum seconds to wait before returning a running handle. "
            "0 means wait until completion for short jobs."
        ),
        minimum=0,
        maximum=600,
    ),
    result_max_chars=IntegerSchema(
        2000,
        description="Maximum result preview characters returned by run/status.",
        minimum=200,
        maximum=20000,
    ),
    include_disabled=BooleanSchema(
        description="Include disabled jobs when action='list'.",
        default=False,
    ),
    job_id=StringSchema("REQUIRED when action='remove', 'enable', 'run', or 'status'. Job ID to operate on."),
    required=["action"],
    description=(
        "Action-specific parameters: add requires a non-empty message or command plus one schedule "
        "(every_seconds, cron_expr, or at); remove/enable/run/status require job_id; list only needs action. "
        "Per-action requirements are enforced at runtime (see field descriptions) so the "
        "top-level schema stays compatible with providers (e.g. OpenAI Codex/Responses) that "
        "reject oneOf/anyOf/allOf/enum/not at the root of function parameters."
    ),
)


@tool_parameters(_CRON_PARAMETERS)
class CronTool(Tool):
    """Tool to schedule reminders and recurring tasks."""

    def __init__(self, cron_service: CronService, default_timezone: str = "UTC"):
        self._cron = cron_service
        self._default_timezone = default_timezone
        self._channel: ContextVar[str] = ContextVar("cron_channel", default="")
        self._chat_id: ContextVar[str] = ContextVar("cron_chat_id", default="")
        self._metadata: ContextVar[dict] = ContextVar("cron_metadata", default={})
        self._session_key: ContextVar[str] = ContextVar("cron_session_key", default="")
        self._in_cron_context: ContextVar[bool] = ContextVar("cron_in_context", default=False)

    def set_context(
        self, channel: str, chat_id: str,
        metadata: dict | None = None, session_key: str | None = None,
    ) -> None:
        """Set the current session context for delivery."""
        self._channel.set(channel)
        self._chat_id.set(chat_id)
        self._metadata.set(metadata or {})
        self._session_key.set(session_key or f"{channel}:{chat_id}")

    def set_cron_context(self, active: bool):
        """Mark whether the tool is executing inside a cron job callback."""
        return self._in_cron_context.set(active)

    def reset_cron_context(self, token) -> None:
        """Restore previous cron context."""
        self._in_cron_context.reset(token)

    @staticmethod
    def _validate_timezone(tz: str) -> str | None:
        from zoneinfo import ZoneInfo

        try:
            ZoneInfo(tz)
        except (KeyError, Exception):
            return f"Error: unknown timezone '{tz}'"
        return None

    def _display_timezone(self, schedule: CronSchedule) -> str:
        """Pick the most human-meaningful timezone for display."""
        return schedule.tz or self._default_timezone

    @staticmethod
    def _format_timestamp(ms: int, tz_name: str) -> str:
        from zoneinfo import ZoneInfo

        dt = datetime.fromtimestamp(ms / 1000, tz=ZoneInfo(tz_name))
        return f"{dt.isoformat()} ({tz_name})"

    @property
    def name(self) -> str:
        return "cron"

    @property
    def description(self) -> str:
        return (
            "Schedule reminders and recurring tasks. Actions: add, list, remove, enable, run, status. "
            f"If tz is omitted, cron expressions and naive ISO times default to {self._default_timezone}."
        )

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        errors = super().validate_params(params)
        action = params.get("action")
        command = str(params.get("command") or "").strip()
        message = str(params.get("message") or "").strip()
        if action == "add" and not command and not message:
            errors.append("message or command is required when action='add'")
        if action in {"remove", "enable", "run", "status"} and not str(params.get("job_id") or "").strip():
            errors.append(f"job_id is required when action='{action}'")
        return errors

    async def execute(
        self,
        action: str,
        name: str | None = None,
        message: str = "",
        every_seconds: int | None = None,
        cron_expr: str | None = None,
        tz: str | None = None,
        at: str | None = None,
        at_time: str | None = None,
        kind: str | None = None,
        command: str = "",
        timeout_seconds: int | None = None,
        job_id: str | None = None,
        deliver: bool | None = None,
        enabled: bool = True,
        force: bool = False,
        wait: bool | None = None,
        wait_timeout_seconds: int = 0,
        result_max_chars: int = 2000,
        include_disabled: bool = False,
        context_session_key: str | None = None,
        sync_session_key: str | None = None,
        worker_session_key: str | None = None,
        research_id: str | None = None,
        sync_max_messages: int = 12,
        sync_max_chars: int = 12000,
        **kwargs: Any,
    ) -> str:
        if action == "add":
            if self._in_cron_context.get():
                return "Error: cannot schedule new jobs from within a cron job execution"
            return self._add_job(
                name,
                message,
                every_seconds,
                cron_expr,
                tz,
                at or at_time,
                deliver,
                command=command,
                timeout_seconds=timeout_seconds,
                kind=kind,
                context_session_key=context_session_key,
                sync_session_key=sync_session_key,
                worker_session_key=worker_session_key,
                research_id=research_id,
                sync_max_messages=sync_max_messages,
                sync_max_chars=sync_max_chars,
            )
        elif action == "list":
            return self._list_jobs(include_disabled=include_disabled)
        elif action == "remove":
            return self._remove_job(job_id)
        elif action == "enable":
            return self._enable_job(job_id, enabled)
        elif action == "run":
            return await self._run_job(
                job_id,
                force,
                wait=wait,
                wait_timeout_seconds=wait_timeout_seconds,
                result_max_chars=result_max_chars,
            )
        elif action == "status":
            return self._status_job(job_id, result_max_chars=result_max_chars)
        return f"Unknown action: {action}"

    def _add_job(
        self,
        name: str | None,
        message: str,
        every_seconds: int | None,
        cron_expr: str | None,
        tz: str | None,
        at: str | None,
        deliver: bool | None = None,
        *,
        command: str = "",
        timeout_seconds: int | None = None,
        kind: str | None = None,
        context_session_key: str | None = None,
        sync_session_key: str | None = None,
        worker_session_key: str | None = None,
        research_id: str | None = None,
        sync_max_messages: int = 12,
        sync_max_chars: int = 12000,
    ) -> str:
        normalized_message = (message or "").strip()
        normalized_command = (command or "").strip()
        requested_kind = (kind or "").strip()
        if requested_kind and requested_kind not in {"agent_turn", "agent_turn_with_context", "exec"}:
            return "Error: kind must be one of agent_turn, agent_turn_with_context, exec"

        payload_kind = requested_kind or "agent_turn"
        payload_command = ""
        payload_message = message or ""
        if normalized_command:
            if requested_kind == "agent_turn_with_context":
                return "Error: command cannot be combined with kind='agent_turn_with_context'"
            payload_kind = "exec"
            payload_command = normalized_command
            payload_message = payload_message or f"RUN:{payload_command}"
        elif normalized_message.startswith("RUN:"):
            if requested_kind == "agent_turn_with_context":
                return "Error: RUN: command jobs cannot use kind='agent_turn_with_context'"
            payload_command = normalized_message[4:].strip()
            if not payload_command:
                return "Error: RUN: command is empty"
            payload_kind = "exec"
        elif not normalized_message:
            return (
                "Error: cron action='add' requires a non-empty 'message' parameter "
                "or a non-empty 'command' parameter describing what to do when the job triggers "
                "(e.g. the reminder text). Retry including message=\"...\"."
            )
        should_deliver = payload_kind != "exec" if deliver is None else bool(deliver)
        if timeout_seconds is not None and timeout_seconds <= 0:
            return "Error: timeout_seconds must be > 0"
        if sync_max_messages <= 0:
            return "Error: sync_max_messages must be > 0"
        if sync_max_chars <= 0:
            return "Error: sync_max_chars must be > 0"
        channel = self._channel.get()
        chat_id = self._chat_id.get()
        if not channel or not chat_id:
            return "Error: no session context (channel/chat_id)"
        if tz and not cron_expr:
            return "Error: tz can only be used with cron_expr"
        if tz:
            if err := self._validate_timezone(tz):
                return err

        # Build schedule
        delete_after = False
        if every_seconds:
            schedule = CronSchedule(kind="every", every_ms=every_seconds * 1000)
        elif cron_expr:
            effective_tz = tz or self._default_timezone
            if err := self._validate_timezone(effective_tz):
                return err
            schedule = CronSchedule(kind="cron", expr=cron_expr, tz=effective_tz)
        elif at:
            from zoneinfo import ZoneInfo

            try:
                dt = datetime.fromisoformat(at)
            except ValueError:
                return f"Error: invalid ISO datetime format '{at}'. Expected format: YYYY-MM-DDTHH:MM:SS"
            if dt.tzinfo is None:
                if err := self._validate_timezone(self._default_timezone):
                    return err
                dt = dt.replace(tzinfo=ZoneInfo(self._default_timezone))
            at_ms = int(dt.timestamp() * 1000)
            schedule = CronSchedule(kind="at", at_ms=at_ms)
            delete_after = True
        else:
            return "Error: either every_seconds, cron_expr, or at is required"

        current_session_key = self._session_key.get() or f"{channel}:{chat_id}"
        context_key = (context_session_key or "").strip() or None
        sync_key = (sync_session_key or "").strip() or None
        worker_key = (worker_session_key or "").strip() or None
        research_key = (research_id or "").strip() or None
        if payload_kind == "agent_turn_with_context":
            context_key = context_key or current_session_key
            sync_key = sync_key or context_key
            if worker_key and worker_key in {context_key, sync_key}:
                return "Error: worker_session_key must be independent from context/sync session keys"
        elif any([context_key, sync_key, worker_key, research_key]):
            return (
                "Error: context_session_key, sync_session_key, worker_session_key, and research_id "
                "are only valid with kind='agent_turn_with_context'"
            )

        job = self._cron.add_job(
            name=name or payload_message[:30] or f"RUN:{payload_command}"[:30],
            schedule=schedule,
            message=payload_message,
            kind=payload_kind,
            command=payload_command,
            timeout_s=timeout_seconds,
            deliver=should_deliver,
            channel=channel,
            to=chat_id,
            delete_after_run=delete_after,
            channel_meta=self._metadata.get(),
            session_key=current_session_key or None,
            context_session_key=context_key,
            sync_session_key=sync_key,
            worker_session_key=worker_key,
            research_id=research_key,
            sync_max_messages=sync_max_messages,
            sync_max_chars=sync_max_chars,
        )
        return f"Created job '{job.name}' (id: {job.id})"

    def _format_timing(self, schedule: CronSchedule) -> str:
        """Format schedule as a human-readable timing string."""
        if schedule.kind == "cron":
            tz = f" ({schedule.tz})" if schedule.tz else ""
            return f"cron: {schedule.expr}{tz}"
        if schedule.kind == "every" and schedule.every_ms:
            ms = schedule.every_ms
            if ms % 3_600_000 == 0:
                return f"every {ms // 3_600_000}h"
            if ms % 60_000 == 0:
                return f"every {ms // 60_000}m"
            if ms % 1000 == 0:
                return f"every {ms // 1000}s"
            return f"every {ms}ms"
        if schedule.kind == "at" and schedule.at_ms:
            return f"at {self._format_timestamp(schedule.at_ms, self._display_timezone(schedule))}"
        return schedule.kind

    def _format_state(self, state: CronJobState, schedule: CronSchedule) -> list[str]:
        """Format job run state as display lines."""
        lines: list[str] = []
        display_tz = self._display_timezone(schedule)
        if state.last_run_at_ms:
            info = (
                f"  Last run: {self._format_timestamp(state.last_run_at_ms, display_tz)}"
                f" — {state.last_status or 'unknown'}"
            )
            if state.last_error:
                info += f" ({state.last_error})"
            lines.append(info)
        if state.next_run_at_ms:
            lines.append(f"  Next run: {self._format_timestamp(state.next_run_at_ms, display_tz)}")
        return lines

    @staticmethod
    def _system_job_purpose(job: CronJob) -> str:
        if job.name == "dream":
            return "Dream memory consolidation for long-term memory."
        return "System-managed internal job."

    def _list_jobs(self, include_disabled: bool = False) -> str:
        jobs = self._cron.list_jobs(include_disabled=include_disabled)
        if not jobs:
            return "No scheduled jobs."
        lines = []
        for j in jobs:
            timing = self._format_timing(j.schedule)
            state = "enabled" if j.enabled else "disabled"
            suffix = f", {state}" if include_disabled else ""
            parts = [f"- {j.name} (id: {j.id}, {timing}{suffix})"]
            if j.payload.kind == "system_event":
                parts.append(f"  Purpose: {self._system_job_purpose(j)}")
                parts.append("  Protected: visible for inspection, but cannot be removed.")
            elif j.payload.kind == "exec":
                parts.append("  Type: exec command")
                if j.payload.deliver:
                    parts.append("  Delivery: command result will be delivered")
            elif j.payload.kind == "agent_turn_with_context":
                parts.append("  Type: autonomous worker agent turn")
                if j.payload.context_session_key:
                    parts.append(f"  Context session: {j.payload.context_session_key}")
                if j.payload.sync_session_key:
                    parts.append(f"  Sync session: {j.payload.sync_session_key}")
                worker = j.payload.worker_session_key or f"cron:{j.id}"
                parts.append(f"  Worker session: {worker}")
                if j.payload.last_sync_message_timestamp:
                    parts.append(f"  Last sync timestamp: {j.payload.last_sync_message_timestamp}")
            parts.extend(self._format_state(j.state, j.schedule))
            lines.append("\n".join(parts))
        return "Scheduled jobs:\n" + "\n".join(lines)

    def _remove_job(self, job_id: str | None) -> str:
        if not job_id:
            return "Error: job_id is required for remove"
        result = self._cron.remove_job(job_id)
        if result == "removed":
            return f"Removed job {job_id}"
        if result == "protected":
            job = self._cron.get_job(job_id)
            if job and job.name == "dream":
                return (
                    "Cannot remove job `dream`.\n"
                    "This is a system-managed Dream memory consolidation job for long-term memory.\n"
                    "It remains visible so you can inspect it, but it cannot be removed."
                )
            return (
                f"Cannot remove job `{job_id}`.\n"
                "This is a protected system-managed cron job."
            )
        return f"Job {job_id} not found"

    def _enable_job(self, job_id: str | None, enabled: bool) -> str:
        if not job_id:
            return "Error: job_id is required for enable"
        job = self._cron.enable_job(job_id, enabled=enabled)
        if not job:
            return f"Job {job_id} not found"
        return f"Job {job_id} {'enabled' if enabled else 'disabled'}"

    @staticmethod
    def _preview(text: str | None, max_chars: int = 2000) -> str | None:
        if not text:
            return None
        return truncate_string(text, max_chars, suffix="\n... (truncated)")

    def _job_route_lines(self, job: CronJob) -> list[str]:
        lines: list[str] = []
        if job.payload.kind == "agent_turn_with_context":
            worker = job.payload.worker_session_key or f"cron:{job.id}"
            lines.append(f"Worker session: {worker}")
            if job.payload.sync_session_key:
                lines.append(f"Sync session: {job.payload.sync_session_key}")
            if job.payload.context_session_key:
                lines.append(f"Context session: {job.payload.context_session_key}")
            if job.payload.research_id:
                lines.append(f"Research ID: {job.payload.research_id}")
            if job.payload.deliver:
                lines.append("Delivery: final response will be delivered to the configured channel.")
        elif job.payload.deliver:
            lines.append("Delivery: result will be delivered to the configured channel.")
        return lines

    def _format_run_result(
        self,
        result: dict[str, Any],
        *,
        waited: bool,
        wait_timeout_seconds: int,
        result_max_chars: int,
    ) -> str:
        if not result.get("found", False):
            return f"Job {result.get('job_id') or ''} not found"
        if not result.get("started", False):
            status = result.get("status") or "unknown"
            error = result.get("error")
            suffix = f": {error}" if error else ""
            return f"Failed to run job {result['job_id']} ({status}){suffix}"

        completed = bool(result.get("completed"))
        status = result.get("status") or "unknown"
        if completed and waited:
            lines = [f"Executed job {result['job_id']} (run {result.get('run_id')})"]
        elif completed:
            lines = [f"Triggered job {result['job_id']} (run {result.get('run_id')}) completed quickly"]
        elif waited and wait_timeout_seconds > 0:
            lines = [
                f"Triggered job {result['job_id']} (run {result.get('run_id')}); "
                f"still running after {wait_timeout_seconds}s"
            ]
        else:
            lines = [f"Triggered job {result['job_id']} (run {result.get('run_id')})"]
        lines.append(f"Status: {status}")
        if result.get("duration_ms"):
            lines.append(f"Duration: {result['duration_ms']}ms")
        if result.get("error"):
            lines.append(f"Error: {result['error']}")
        if preview := self._preview(result.get("result_preview"), result_max_chars):
            lines.append(f"Result preview:\n{preview}")
        if result.get("kind") == "agent_turn_with_context":
            if worker := result.get("worker_session_key"):
                lines.append(f"Worker session: {worker}")
            if sync := result.get("sync_session_key"):
                lines.append(f"Sync session: {sync}")
            if context := result.get("context_session_key"):
                lines.append(f"Context session: {context}")
            if research_id := result.get("research_id"):
                lines.append(f"Research ID: {research_id}")
            if result.get("deliver"):
                lines.append("Delivery: final response will be delivered to the configured channel.")
        lines.append(f"Inspect later with cron(action='status', job_id='{result['job_id']}').")
        return "\n".join(lines)

    async def _run_job(
        self,
        job_id: str | None,
        force: bool,
        *,
        wait: bool | None,
        wait_timeout_seconds: int,
        result_max_chars: int,
    ) -> str:
        if not job_id:
            return "Error: job_id is required for run"
        job = self._cron.get_job(job_id)
        if not job:
            return f"Job {job_id} not found"
        effective_wait = (job.payload.kind != "agent_turn_with_context") if wait is None else bool(wait)
        preview_chars = result_max_chars if result_max_chars and result_max_chars > 0 else 2000
        result = await self._cron.trigger_job(
            job_id,
            force=force,
            wait=effective_wait,
            wait_timeout_s=wait_timeout_seconds if wait_timeout_seconds and wait_timeout_seconds > 0 else None,
            result_max_chars=preview_chars,
        )
        return self._format_run_result(
            result,
            waited=effective_wait,
            wait_timeout_seconds=wait_timeout_seconds or 0,
            result_max_chars=preview_chars,
        )

    def _status_job(self, job_id: str | None, *, result_max_chars: int = 2000) -> str:
        if not job_id:
            return "Error: job_id is required for status"
        job = self._cron.get_job(job_id)
        if not job:
            return f"Job {job_id} not found"

        timing = self._format_timing(job.schedule)
        state = "enabled" if job.enabled else "disabled"
        lines = [f"Job {job.name} (id: {job.id}, {timing}, {state})"]
        lines.append(f"Type: {job.payload.kind}")
        lines.extend(self._job_route_lines(job))
        lines.extend(self._format_state(job.state, job.schedule))
        if job.state.run_history:
            lines.append("Recent runs:")
            for record in job.state.run_history[-5:]:
                run_id = f" {record.run_id}" if record.run_id else ""
                detail = f"-{run_id}: {record.status}, duration={record.duration_ms}ms"
                if record.error:
                    detail += f", error={record.error}"
                lines.append(detail)
            latest = job.state.run_history[-1]
            if preview := self._preview(latest.result_preview, result_max_chars):
                lines.append(f"Latest result preview:\n{preview}")
        return "\n".join(lines)
