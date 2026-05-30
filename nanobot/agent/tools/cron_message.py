"""Send explicit guidance from a main session to an autonomous cron worker."""

from __future__ import annotations

import json
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.context import RequestContext
from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema
from nanobot.cron.service import CronService
from nanobot.cron.types import CronJob
from nanobot.session.manager import SessionManager
from nanobot.utils.helpers import safe_filename, truncate_string


@tool_parameters(
    tool_parameters_schema(
        action=StringSchema(
            "Action to perform. Use send to inject guidance into a cron worker, "
            "or list to show available autonomous cron targets.",
            enum=["send", "list"],
        ),
        target=StringSchema(
            "Optional target job id, exact job name, or worker session key. "
            "If omitted, the tool uses the single autonomous cron job linked to "
            "the current session when exactly one exists."
        ),
        message=StringSchema(
            "Guidance or steering instruction to inject into the cron worker. "
            "Required for action='send'."
        ),
        subject=StringSchema("Optional short subject for the injected guidance."),
        priority=StringSchema(
            "Guidance priority. Use urgent only when the worker should stop or change course.",
            enum=["normal", "high", "urgent"],
        ),
        research_id=StringSchema(
            "Optional research-state directory name under workspace/research. "
            "When omitted, it is inferred from the target job name."
        ),
        required=["action"],
    )
)
class CronMessageTool(Tool):
    """Inject main-session guidance into independent autonomous cron workers."""

    def __init__(
        self,
        *,
        sessions: SessionManager,
        cron_service: CronService,
        workspace: str | Path,
    ) -> None:
        self._sessions = sessions
        self._cron = cron_service
        self._workspace = Path(workspace).expanduser()
        self._request_ctx: ContextVar[RequestContext | None] = ContextVar(
            "cron_message_request_ctx",
            default=None,
        )

    @property
    def name(self) -> str:
        return "cron_message"

    @property
    def description(self) -> str:
        return (
            "Send explicit steering guidance from the current/main session to an "
            "autonomous cron worker. The guidance is written into the worker session "
            "and its research inbox so the next cron wake-up can act on it without "
            "calling session_sync or replaying the whole main conversation."
        )

    @property
    def read_only(self) -> bool:
        return False

    def set_context(self, ctx: RequestContext) -> None:
        self._request_ctx.set(ctx)

    async def execute(
        self,
        action: str,
        target: str | None = None,
        message: str = "",
        subject: str = "",
        priority: str = "normal",
        research_id: str = "",
        **kwargs: Any,
    ) -> str:
        if action == "list":
            return self._list_targets()
        if action != "send":
            return f"Unknown action: {action}"

        text = (message or "").strip()
        if not text:
            return "Error: message is required for cron_message action='send'"
        if priority not in {"normal", "high", "urgent"}:
            return "Error: priority must be one of normal, high, urgent"

        resolved = self._resolve_target(target)
        if isinstance(resolved, str):
            return resolved
        job, worker_key = resolved
        inbox_path = self._append_guidance(
            job=job,
            worker_key=worker_key,
            message=text,
            subject=subject.strip(),
            priority=priority,
            research_id=research_id.strip(),
        )

        label = f"{job.name} ({job.id})" if job else worker_key
        return (
            f"Injected guidance into cron worker: {label}\n"
            f"Worker session: {worker_key}\n"
            f"Research inbox: {inbox_path}"
        )

    def _list_targets(self) -> str:
        jobs = [
            j
            for j in self._cron.list_jobs(include_disabled=True)
            if j.payload.kind == "agent_turn_with_context"
        ]
        if not jobs:
            return "No autonomous cron worker jobs found."
        lines = ["Autonomous cron workers:"]
        for job in jobs:
            worker_key = job.payload.worker_session_key or f"cron:{job.id}"
            state = "enabled" if job.enabled else "disabled"
            linked = job.payload.sync_session_key or job.payload.session_key or ""
            lines.append(
                f"- {job.name} (id: {job.id}, {state})\n"
                f"  worker: {worker_key}\n"
                f"  linked session: {linked or 'none'}"
            )
        return "\n".join(lines)

    def _resolve_target(self, target: str | None) -> tuple[CronJob | None, str] | str:
        requested = (target or "").strip()
        jobs = [
            j
            for j in self._cron.list_jobs(include_disabled=True)
            if j.payload.kind == "agent_turn_with_context"
        ]
        if requested:
            for job in jobs:
                worker_key = job.payload.worker_session_key or f"cron:{job.id}"
                if requested in {job.id, job.name, worker_key}:
                    return job, worker_key
            if requested.startswith("cron:"):
                return None, requested
            return f"Error: no autonomous cron worker matched target: {requested}"

        ctx = self._request_ctx.get()
        session_key = ctx.session_key if ctx else ""
        linked = [
            j
            for j in jobs
            if session_key
            and session_key
            in {
                j.payload.session_key,
                j.payload.context_session_key,
                j.payload.sync_session_key,
            }
        ]
        candidates = linked or jobs
        if len(candidates) == 1:
            job = candidates[0]
            return job, job.payload.worker_session_key or f"cron:{job.id}"
        if not candidates:
            return "Error: no autonomous cron worker jobs found"
        return (
            "Error: multiple autonomous cron workers exist; specify target. "
            "Use cron_message(action='list') to inspect target ids."
        )

    @staticmethod
    def _infer_research_id(job: CronJob | None, worker_key: str, explicit: str) -> str:
        if explicit:
            return safe_filename(explicit)
        if job and job.name == "complex-reasoning-workflow-autonomous-iteration":
            return "complex-reasoning"
        if job and job.name:
            return safe_filename(job.name)
        return safe_filename(worker_key.replace(":", "_"))

    def _append_guidance(
        self,
        *,
        job: CronJob | None,
        worker_key: str,
        message: str,
        subject: str,
        priority: str,
        research_id: str,
    ) -> Path:
        ctx = self._request_ctx.get()
        source_session = ctx.session_key if ctx else ""
        now = datetime.now().isoformat()
        job_id = job.id if job else ""
        job_name = job.name if job else ""
        guidance = (
            "[Guidance from Main Session]\n"
            f"time: {now}\n"
            f"source_session: {source_session or 'unknown'}\n"
            f"priority: {priority}\n"
            f"subject: {subject or 'none'}\n"
            f"target_job: {job_name or 'none'}{f' ({job_id})' if job_id else ''}\n\n"
            f"{message}"
        )

        session = self._sessions.get_or_create(worker_key)
        session.add_message(
            "user",
            guidance,
            _cron_guidance=True,
            _source_session_key=source_session,
            _guidance_priority=priority,
            _guidance_subject=subject,
            _target_job_id=job_id,
        )
        self._sessions.save(session)

        research_slug = self._infer_research_id(job, worker_key, research_id)
        research_dir = self._workspace / "research" / research_slug
        research_dir.mkdir(parents=True, exist_ok=True)
        inbox_path = research_dir / "inbox.jsonl"
        record = {
            "timestamp": now,
            "source_session": source_session,
            "target_worker_session": worker_key,
            "target_job_id": job_id,
            "target_job_name": job_name,
            "priority": priority,
            "subject": subject,
            "message": message,
            "session_message_preview": truncate_string(guidance, 1000),
        }
        with open(inbox_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return inbox_path
