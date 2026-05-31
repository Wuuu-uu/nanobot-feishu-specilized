"""Synchronous helper-agent review for autonomous cron workers."""

from __future__ import annotations

import asyncio
import json
import re
import time
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.context import RequestContext
from nanobot.agent.tools.cron import CronTool
from nanobot.agent.tools.message import MessageTool
from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema
from nanobot.bus.events import OutboundMessage
from nanobot.cron.service import CronService
from nanobot.cron.types import CronJob
from nanobot.session.manager import SessionManager
from nanobot.utils.helpers import safe_filename, truncate_string

if TYPE_CHECKING:
    from nanobot.agent.loop import AgentLoop


@tool_parameters(
    tool_parameters_schema(
        request_type=StringSchema(
            "Type of help requested.",
            enum=[
                "idea_review",
                "blocker",
                "experiment_gate",
                "result_interpretation",
                "strategy_review",
                "human_escalation",
            ],
        ),
        question=StringSchema(
            "Concrete question for the helper. Include the decision needed, not just status."
        ),
        current_state=StringSchema("Compact current state and what the cron worker has already tried."),
        evidence_paths=StringSchema(
            "Relevant file/report/log paths, one per line or comma separated. Do not include secrets."
        ),
        proposed_options=StringSchema(
            "Candidate actions/options the helper should critique, if any."
        ),
        risk_level=StringSchema(
            "Risk level for the decision.",
            enum=["low", "medium", "high"],
        ),
        research_id=StringSchema(
            "Optional research-state directory under workspace/research. "
            "When omitted, inferred from the cron job name."
        ),
        required=["request_type", "question"],
    )
)
class AutopilotHelpTool(Tool):
    """Ask a synced helper agent for review/diagnosis and wait for its answer."""

    _VALID_DECISIONS = {"approve", "revise", "reject", "need_more_info", "human_required"}

    def __init__(
        self,
        *,
        loop: AgentLoop,
        sessions: SessionManager,
        cron_service: CronService,
        workspace: str | Path,
    ) -> None:
        self._loop = loop
        self._sessions = sessions
        self._cron = cron_service
        self._workspace = Path(workspace).expanduser()
        self._request_ctx: ContextVar[RequestContext | None] = ContextVar(
            "autopilot_help_request_ctx",
            default=None,
        )
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def name(self) -> str:
        return "autopilot_help"

    @property
    def description(self) -> str:
        return (
            "For autonomous cron workers only: synchronously ask a helper agent to review an idea, "
            "diagnose a blocker, gate an experiment, or decide whether human intervention is needed. "
            "The helper uses a fresh clone of the main session context, sends a visible Helper Agent "
            "message to the user channel, and returns the helper decision to the cron worker."
        )

    @property
    def read_only(self) -> bool:
        return False

    def set_context(self, ctx: RequestContext) -> None:
        self._request_ctx.set(ctx)

    async def execute(
        self,
        request_type: str,
        question: str,
        current_state: str = "",
        evidence_paths: str = "",
        proposed_options: str = "",
        risk_level: str = "medium",
        research_id: str = "",
        **kwargs: Any,
    ) -> str:
        ctx = self._request_ctx.get()
        if not ctx:
            return "Error: autopilot_help has no request context."
        if ctx.metadata.get("_autopilot_helper_active"):
            return "Error: helper agents cannot call autopilot_help recursively."
        if request_type not in {
            "idea_review",
            "blocker",
            "experiment_gate",
            "result_interpretation",
            "strategy_review",
            "human_escalation",
        }:
            return (
                "Error: request_type must be one of idea_review, blocker, "
                "experiment_gate, result_interpretation, strategy_review, human_escalation."
            )
        if risk_level not in {"low", "medium", "high"}:
            return "Error: risk_level must be one of low, medium, high."
        if not (question or "").strip():
            return "Error: question is required."

        worker_key = (
            ctx.metadata.get("_autopilot_worker_session_key")
            or ctx.session_key
            or ""
        )
        if not worker_key:
            return "Error: could not determine cron worker session."

        job = self._find_job_for_worker(str(worker_key))
        if job is None and not str(worker_key).startswith("cron:"):
            return "Error: autopilot_help is only available inside autonomous cron worker sessions."

        sync_key = (
            ctx.metadata.get("_autopilot_sync_session_key")
            or ctx.metadata.get("_record_session_key")
            or (job.payload.sync_session_key if job else None)
            or (job.payload.context_session_key if job else None)
            or (job.payload.session_key if job else None)
            or ""
        )
        if not sync_key:
            return "Error: could not determine main/sync session for helper context."

        helper_key = self._helper_session_key(str(worker_key))
        helper_rel = self._session_rel_path(helper_key)
        request_id = f"{int(time.time() * 1000)}-{safe_filename(request_type)[:24]}"
        research_slug = self._infer_research_id(job, str(worker_key), research_id.strip())

        payload = {
            "request_id": request_id,
            "timestamp": datetime.now().isoformat(),
            "request_type": request_type,
            "risk_level": risk_level,
            "question": question.strip(),
            "current_state": current_state.strip(),
            "evidence_paths": evidence_paths.strip(),
            "proposed_options": proposed_options.strip(),
            "worker_session_key": str(worker_key),
            "helper_session_key": helper_key,
            "main_session_key": str(sync_key),
            "cron_job_id": job.id if job else "",
            "cron_job_name": job.name if job else "",
        }
        request_path = self._append_help_request(research_slug, payload)

        lock = self._locks.setdefault(str(worker_key), asyncio.Lock())
        async with lock:
            self._sync_helper_session(helper_key, str(sync_key), payload)
            helper_prompt = self._build_helper_prompt(payload, request_path)
            helper_meta = self._helper_metadata(ctx, helper_rel, request_id)

            async def _helper_progress(
                content: str,
                *,
                tool_hint: bool = False,
                tool_events: list[dict[str, Any]] | None = None,
            ) -> None:
                channels_config = getattr(self._loop, "channels_config", None)
                if not getattr(channels_config, "send_autonomous_progress", True):
                    if not tool_events:
                        return
                    content = ""
                meta = dict(helper_meta)
                meta["_progress"] = True
                meta["_tool_hint"] = tool_hint
                if tool_events:
                    meta["_tool_events"] = tool_events
                await self._loop.bus.publish_outbound(
                    OutboundMessage(
                        channel=ctx.channel,
                        chat_id=ctx.chat_id,
                        content=content,
                        metadata=meta,
                        )
                    )

            wants_stream = bool(helper_meta.get("_wants_stream"))

            async def _helper_stream(delta: str) -> None:
                meta = dict(helper_meta)
                meta["_stream_delta"] = True
                await self._loop.bus.publish_outbound(
                    OutboundMessage(
                        channel=ctx.channel,
                        chat_id=ctx.chat_id,
                        content=delta,
                        metadata=meta,
                    )
                )

            async def _helper_stream_end(*, resuming: bool = False) -> None:
                meta = dict(helper_meta)
                meta["_stream_end"] = True
                meta["_resuming"] = resuming
                await self._loop.bus.publish_outbound(
                    OutboundMessage(
                        channel=ctx.channel,
                        chat_id=ctx.chat_id,
                        content="",
                        metadata=meta,
                    )
                )

            async def _run_helper():
                message_tool = self._loop.tools.get("message")
                message_token = None
                if isinstance(message_tool, MessageTool):
                    message_token = message_tool.set_record_channel_delivery(False)
                cron_tool = self._loop.tools.get("cron")
                cron_token = None
                if isinstance(cron_tool, CronTool):
                    cron_token = cron_tool.set_cron_context(False)
                try:
                    return await self._loop.process_direct(
                        helper_prompt,
                        session_key=helper_key,
                        channel=ctx.channel,
                        chat_id=ctx.chat_id,
                        metadata=helper_meta,
                        on_progress=_helper_progress,
                        on_stream=_helper_stream if wants_stream else None,
                        on_stream_end=_helper_stream_end if wants_stream else None,
                    )
                finally:
                    if isinstance(cron_tool, CronTool) and cron_token is not None:
                        cron_tool.reset_cron_context(cron_token)
                    if isinstance(message_tool, MessageTool) and message_token is not None:
                        message_tool.reset_record_channel_delivery(message_token)

            response = await asyncio.create_task(_run_helper())

        helper_text = response.content if response else ""
        if helper_text.strip():
            final_meta = {**helper_meta, **(response.metadata if response else {})}
            await self._loop.bus.publish_outbound(
                OutboundMessage(
                    channel=ctx.channel,
                    chat_id=ctx.chat_id,
                    content=helper_text,
                    metadata=final_meta,
                )
            )

        decision = self._extract_decision(helper_text)
        paused = False
        if decision == "human_required" and job is not None:
            paused = self._pause_job(job)

        self._append_help_response(
            research_slug,
            {
                **payload,
                "helper_response_at": datetime.now().isoformat(),
                "decision": decision or "unknown",
                "paused_cron_job": paused,
                "helper_response_preview": truncate_string(helper_text, 4000),
            },
        )

        pause_note = "\nCron job paused: true" if paused else ""
        return (
            f"Helper agent completed request {request_id}.\n"
            f"Decision: {decision or 'unknown'}\n"
            f"Helper session: {helper_rel}\n"
            f"Request log: {request_path}{pause_note}\n\n"
            f"{helper_text}"
        )

    def _find_job_for_worker(self, worker_key: str) -> CronJob | None:
        for job in self._cron.list_jobs(include_disabled=True):
            if job.payload.kind != "agent_turn_with_context":
                continue
            if (job.payload.worker_session_key or f"cron:{job.id}") == worker_key:
                return job
        return None

    @staticmethod
    def _helper_session_key(worker_key: str) -> str:
        return f"helper:{safe_filename(worker_key)}"

    def _session_rel_path(self, session_key: str) -> str:
        return f"sessions/{self._sessions.safe_key(session_key)}.jsonl"

    @staticmethod
    def _infer_research_id(job: CronJob | None, worker_key: str, explicit: str) -> str:
        if explicit:
            return safe_filename(explicit)
        if job and job.name == "complex-reasoning-workflow-autonomous-iteration":
            return "complex-reasoning"
        if job and "complex-reasoning" in job.name:
            return "complex-reasoning"
        if job and job.name:
            return safe_filename(job.name)
        return safe_filename(worker_key.replace(":", "_"))

    def _append_help_request(self, research_slug: str, payload: dict[str, Any]) -> Path:
        research_dir = self._workspace / "research" / research_slug
        research_dir.mkdir(parents=True, exist_ok=True)
        path = research_dir / "help_requests.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return path

    def _append_help_response(self, research_slug: str, payload: dict[str, Any]) -> Path:
        research_dir = self._workspace / "research" / research_slug
        research_dir.mkdir(parents=True, exist_ok=True)
        path = research_dir / "helper_responses.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return path

    def _sync_helper_session(
        self,
        helper_key: str,
        main_key: str,
        payload: dict[str, Any],
    ) -> None:
        source = self._sessions.get_or_create(main_key)
        helper = self._sessions.get_or_create(helper_key)
        helper.messages = [deepcopy(m) for m in source.messages]
        helper.last_consolidated = min(source.last_consolidated, len(helper.messages))
        helper.metadata = deepcopy(source.metadata)
        helper.metadata.update(
            {
                "_helper_agent": True,
                "_helper_synced_from": main_key,
                "_helper_synced_at": datetime.now().isoformat(),
                "_helper_request_id": payload["request_id"],
                "_helper_worker_session": payload["worker_session_key"],
            }
        )
        self._sessions.save(helper)

    @staticmethod
    def _helper_metadata(
        ctx: RequestContext,
        helper_rel: str,
        request_id: str,
    ) -> dict[str, Any]:
        meta = dict(ctx.metadata or {})
        for key in (
            "_record_channel_delivery",
            "_record_session_key",
            "_record_prefix",
            "_delivery_prefix",
            "_cron_source_session",
            "_cron_source_label",
        ):
            meta.pop(key, None)
        meta.update(
            {
                "_source_title": "Helper Agent 消息",
                "_source_label": f"Helper · {helper_rel}",
                "_helper_source_session": helper_rel,
                "_autopilot_helper_active": True,
                "_stream_id": f"helper:{request_id}",
            }
        )
        return meta

    @staticmethod
    def _build_helper_prompt(payload: dict[str, Any], request_path: Path) -> str:
        return (
            "[Autopilot Helper Request]\n"
            "You are the helper agent for an autonomous cron research worker. "
            "Use the `research-autopilot-helper` skill. Your context has just been "
            "synchronized from the main user session; do not rely on old helper-session state. "
            "Review the worker request critically and help it escape local loops, weak ideas, "
            "or unresolved blockers. Do not perform broad experiments or destructive actions. "
            "Your default job is to make the best bounded autonomous decision, not to ask the "
            "human to decide. Prefer approve, revise, reject, or need_more_info with an executable "
            "next action whenever the action is bounded, reversible, already within the user's "
            "research direction, or can be validated by a small evidence-gathering step. Choose "
            "decision: human_required only when no safe autonomous option remains because the next "
            "step needs explicit user preference, substantial cost/compute expansion, credential "
            "or secret handling, destructive cleanup, production promotion, commit/push, policy "
            "boundary changes, or a major research-direction change.\n\n"
            "When uncertain, do not escalate by default: either revise the plan with stricter "
            "acceptance criteria, reject the weak option and name a better one, or request the "
            "minimal missing evidence through need_more_info. If you choose human_required, explain "
            "why each bounded autonomous alternative is insufficient.\n\n"
            "Your final answer must be exactly one valid JSON object, with no markdown fences, "
            "no prose before it, and no prose after it. Use this shape:\n"
            "{\n"
            '  "decision": "approve | revise | reject | need_more_info | human_required",\n'
            '  "reason": ["..."],\n'
            '  "next_action": {"summary": "...", "steps": ["..."]},\n'
            '  "stop_or_acceptance_criteria": ["..."],\n'
            '  "required_evidence": ["..."],\n'
            '  "human_note": "..."\n'
            "}\n"
            "Omit optional keys only when they do not apply.\n\n"
            f"Request log: {request_path}\n\n"
            "## Request Payload\n"
            f"```json\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n```\n\n"
            "Answer in Chinese unless exact English identifiers are necessary. Be specific: "
            "state the reason, the next action the cron worker should take, and the stop/accept "
            "criterion. If asking for more information, name the minimal evidence required."
        )

    @classmethod
    def _extract_decision(cls, text: str) -> str | None:
        if not text:
            return None
        patterns = [
            r"(?im)^\s*decision\s*[:：]\s*([a-z_]+)\b",
            r'"decision"\s*:\s*"([a-z_]+)"',
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                value = match.group(1).strip().lower()
                if value in cls._VALID_DECISIONS:
                    return value
        lower = text.lower()
        if "human_required" in lower:
            return "human_required"
        return None

    def _pause_job(self, job: CronJob) -> bool:
        updated = self._cron.enable_job(job.id, enabled=False)
        return updated is not None
