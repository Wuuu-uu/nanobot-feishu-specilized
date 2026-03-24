"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMProvider
from nanobot.agent.context import ContextBuilder
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.filesystem import (
    AppendFileTool,
    EditFileTool,
    ListDirTool,
    ReadFileTool,
    WriteFileTool,
)
from nanobot.agent.tools.shell import ExecTool
from nanobot.agent.tools.web import WebSearchTool, WebFetchTool
from nanobot.agent.tools.message import MessageTool
from nanobot.agent.tools.image_generate import ImageGenerateTool
from nanobot.agent.tools.notion import NotionTool
from nanobot.agent.tools.spawn import SpawnTool
from nanobot.agent.tools.session_manage import SessionManageTool
from nanobot.agent.tools.cron import CronTool
from nanobot.agent.subagent import SubagentManager
from nanobot.session.compressor import SessionContextCompressor
from nanobot.session.manager import Session, SessionManager

if TYPE_CHECKING:
    from nanobot.config.schema import (
        ExecToolConfig,
        FeishuConfig,
        ImageGenConfig,
        ContextCompressionConfig,
        MineruConfig,
        NotionToolConfig,
        ToolHistoryConfig,
        WebSearchConfig,
    )
    from nanobot.cron.service import CronService


class AgentLoop:
    """
    The agent loop is the core processing engine.
    
    It:
    1. Receives messages from the bus
    2. Builds context with history, memory, skills
    3. Calls the LLM
    4. Executes tool calls
    5. Sends responses back
    """
    
    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        max_tokens: int = 4096,
        context_window_tokens: int | None = None,
        token_budget_mode: str = "output",
        merge_subagent_usage: bool = True,
        max_iterations: int = 30,
        reasoning_effort: str | None = None,
        web_search_config: WebSearchConfig | None = None,
        exec_config: ExecToolConfig | None = None,
        mineru_config: MineruConfig | None = None,
        image_gen_config: ImageGenConfig | None = None,
        notion_config: NotionToolConfig | None = None,
        tool_history_config: ToolHistoryConfig | None = None,
        context_compression_config: ContextCompressionConfig | None = None,
        feishu_config: FeishuConfig | None = None,
        cron_service: CronService | None = None,
        restrict_to_workspace: bool = False,
    ):
        from nanobot.config.schema import ExecToolConfig
        from nanobot.config.schema import FeishuConfig
        from nanobot.config.schema import ImageGenConfig
        from nanobot.config.schema import ContextCompressionConfig
        from nanobot.config.schema import MineruConfig
        from nanobot.config.schema import NotionToolConfig
        from nanobot.config.schema import ToolHistoryConfig
        from nanobot.config.schema import WebSearchConfig
        from nanobot.cron.service import CronService
        self.bus = bus
        self.provider = provider
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.max_tokens = max(1, int(max_tokens))
        self.context_window_tokens = self._safe_int(context_window_tokens)
        self.token_budget_mode = token_budget_mode if token_budget_mode in {"output", "context"} else "output"
        self.merge_subagent_usage = merge_subagent_usage
        self.max_iterations = max_iterations
        self.reasoning_effort = reasoning_effort
        self.web_search_config = web_search_config or WebSearchConfig()
        self.exec_config = exec_config or ExecToolConfig()
        self.mineru_config = mineru_config or MineruConfig()
        self.image_gen_config = image_gen_config or ImageGenConfig()
        self.notion_config = notion_config or NotionToolConfig()
        self.tool_history_config = tool_history_config or ToolHistoryConfig()
        self.context_compression_config = context_compression_config or ContextCompressionConfig()
        self.feishu_config = feishu_config or FeishuConfig()
        self.cron_service = cron_service
        self.restrict_to_workspace = restrict_to_workspace
        
        self.context = ContextBuilder(workspace)
        self.sessions = SessionManager(workspace)
        self.compressor = SessionContextCompressor(
            provider=provider,
            sessions_dir=self.sessions.sessions_dir,
            config=self.context_compression_config,
            default_model=self.model,
        )
        self.tools = ToolRegistry()
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            model=self.model,
            max_tokens=self.max_tokens,
            reasoning_effort=self.reasoning_effort,
            web_search_config=self.web_search_config,
            exec_config=self.exec_config,
            mineru_config=self.mineru_config,
            notion_config=self.notion_config,
            image_gen_config=self.image_gen_config,
            feishu_config=self.feishu_config,
            restrict_to_workspace=restrict_to_workspace,
        )
        
        self._running = False
        self._tool_digest_max_events = max(1, self.tool_history_config.max_events)
        self._tool_digest_max_chars = max(200, self.tool_history_config.max_chars)
        self._tool_preview_chars = max(80, self.tool_history_config.preview_chars)
        self._history_max_messages = 50
        self._history_precompress_max_messages = self._history_max_messages
        self._history_postcompress_max_messages = self._history_max_messages
        self._history_no_gap_cap = self._history_max_messages
        if self.context_compression_config.enabled:
            self._history_precompress_max_messages = max(
                self._history_max_messages,
                self.context_compression_config.trigger_by_message_count,
            )
            self._history_postcompress_max_messages = max(
                10,
                self.context_compression_config.keep_recent_messages + 5,
            )
            self._history_no_gap_cap = max(
                self._history_precompress_max_messages,
                self.context_compression_config.trigger_by_message_count,
            )
        self._register_default_tools()

    def _resolve_history_limit(self, session: Session, session_summary: str) -> int:
        """Resolve history size while preventing any summary-to-window gaps."""
        if not self.context_compression_config.enabled:
            return self._history_precompress_max_messages

        active_count = sum(1 for m in session.messages if m.get("include_in_context", True))
        active_floor = max(1, min(active_count, self._history_no_gap_cap))

        # With summary enabled, always include all active (not-yet-compressed) messages
        # up to next compression cap to avoid memory gaps between summary and recent window.
        if session_summary:
            return max(self._history_postcompress_max_messages, active_floor)
        return max(self._history_precompress_max_messages, active_floor)

    @staticmethod
    def _safe_int(value: Any) -> int:
        try:
            if value is None:
                return 0
            if isinstance(value, bool):
                return 0
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _accumulate_usage(cls, target: dict[str, int], usage: dict[str, Any] | None) -> None:
        if not usage:
            return
        target["prompt_tokens"] += cls._safe_int(usage.get("prompt_tokens"))
        target["completion_tokens"] += cls._safe_int(usage.get("completion_tokens"))
        target["total_tokens"] += cls._safe_int(usage.get("total_tokens"))
        target["cache_tokens"] += cls._safe_int(usage.get("cache_tokens"))

    @classmethod
    def _build_token_monitor(
        cls,
        usage: dict[str, int],
        output_budget_tokens: int,
        context_window_tokens: int = 0,
        token_budget_mode: str = "output",
        tool_calls_completed: int = 0,
    ) -> dict[str, Any]:
        input_tokens = cls._safe_int(usage.get("prompt_tokens"))
        output_tokens = cls._safe_int(usage.get("completion_tokens"))
        cache_tokens = cls._safe_int(usage.get("cache_tokens"))
        total_tokens = cls._safe_int(usage.get("total_tokens"))

        output_budget = max(1, cls._safe_int(output_budget_tokens))
        output_used = output_tokens
        output_raw_residue = output_budget - output_used
        output_residue = max(0, output_raw_residue)
        output_ratio = min(1.0, output_used / output_budget)

        has_context_budget = cls._safe_int(context_window_tokens) > 0
        context_budget = cls._safe_int(context_window_tokens) if has_context_budget else output_budget
        context_used = input_tokens + output_tokens
        context_raw_residue = context_budget - context_used
        context_residue = max(0, context_raw_residue)
        context_ratio = min(1.0, context_used / context_budget)

        effective_mode = "context" if token_budget_mode == "context" and has_context_budget else "output"
        selected_residue = context_residue if effective_mode == "context" else output_residue
        selected_budget = context_budget if effective_mode == "context" else output_budget
        selected_used = context_used if effective_mode == "context" else output_used
        selected_ratio = context_ratio if effective_mode == "context" else output_ratio

        chart_values = [
            {
                "category": "token用量",
                "item": "input",
                "value": input_tokens,
            },
            {
                "category": "token用量",
                "item": "output",
                "value": output_tokens,
            },
        ]
        completed_tools = max(0, cls._safe_int(tool_calls_completed))
        if completed_tools > 0:
            chart_values.append(
                {
                    "category": "token用量",
                    "item": "tool_calls",
                    "value": completed_tools,
                }
            )

        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_tokens": cache_tokens,
            "task_total_tokens": total_tokens,
            "output_budget_total_tokens": output_budget,
            "output_budget_used_tokens": output_used,
            "output_budget_residue_tokens": output_residue,
            "output_budget_usage_ratio": output_ratio,
            "output_budget_usage_percent": round(output_ratio * 100, 2),
            "output_budget_exceeded": output_raw_residue < 0,
            "context_window_total_tokens": cls._safe_int(context_window_tokens),
            "context_budget_total_tokens": context_budget,
            "context_budget_used_tokens": context_used,
            "context_budget_residue_tokens": context_residue,
            "context_budget_usage_ratio": context_ratio,
            "context_budget_usage_percent": round(context_ratio * 100, 2),
            "context_budget_exceeded": context_raw_residue < 0,
            "selected_budget_mode": effective_mode,
            "selected_budget_total_tokens": selected_budget,
            "selected_budget_used_tokens": selected_used,
            "selected_budget_residue_tokens": selected_residue,
            "selected_budget_usage_ratio": selected_ratio,
            "selected_budget_usage_percent": round(selected_ratio * 100, 2),
            "chart": {
                "type": "bar",
                "direction": "horizontal",
                "title": {"text": "token用量占比图"},
                "data": {
                    "values": chart_values
                },
                "xField": "value",
                "yField": "category",
                "seriesField": "item",
                "stack": True,
                "legends": {"visible": True, "orient": "bottom"},
                "label": {"visible": True, "formatter": "value"},
            },
        }


    
    def _register_default_tools(self) -> None:
        """Register the default set of tools."""
        # File tools (restrict to workspace if configured)
        allowed_dir = self.workspace if self.restrict_to_workspace else None
        self.tools.register(ReadFileTool(allowed_dir=allowed_dir))
        self.tools.register(WriteFileTool(allowed_dir=allowed_dir))
        self.tools.register(AppendFileTool(allowed_dir=allowed_dir))
        self.tools.register(EditFileTool(allowed_dir=allowed_dir))
        self.tools.register(ListDirTool(allowed_dir=allowed_dir))
        
        # Shell tool
        self.tools.register(ExecTool(
            working_dir=str(self.workspace),
            timeout=self.exec_config.timeout,
            restrict_to_workspace=self.restrict_to_workspace,
        ))
        
        # Web tools
        self.tools.register(WebSearchTool(
            api_key=self.web_search_config.api_key or None,
            max_results=self.web_search_config.max_results,
            endpoint=self.web_search_config.endpoint,
            country=self.web_search_config.country,
            language=self.web_search_config.language,
            tbs=self.web_search_config.tbs,
            page=self.web_search_config.page,
            autocorrect=self.web_search_config.autocorrect,
            search_type=self.web_search_config.search_type,
        ))
        self.tools.register(WebFetchTool())

        # PDF tool (MinerU)
        if self.mineru_config and self.mineru_config.enabled:
            from nanobot.agent.tools.pdf_mineru import MineruPdfParseTool
            self.tools.register(MineruPdfParseTool(
                config=self.mineru_config,
                allowed_dir=allowed_dir,
            ))
        
        # Message tool
        message_tool = MessageTool(send_callback=self.bus.publish_outbound)
        self.tools.register(message_tool)

        # Image generation tool
        image_tool = ImageGenerateTool(
            config=self.image_gen_config,
            feishu_config=self.feishu_config,
            workspace=self.workspace,
            allowed_dir=allowed_dir,
        )
        self.tools.register(image_tool)

        # Notion tool (single database management)
        self.tools.register(NotionTool(
            config=self.notion_config,
            allowed_dir=allowed_dir,
        ))

        # Spawn tool (for subagents)
        spawn_tool = SpawnTool(manager=self.subagents)
        self.tools.register(spawn_tool)

        # Session management tool
        self.tools.register(SessionManageTool(manager=self.sessions))
        
        # Cron tool (for scheduling)
        if self.cron_service:
            self.tools.register(CronTool(self.cron_service))
    
    async def run(self) -> None:
        """Run the agent loop, processing messages from the bus."""
        self._running = True
        logger.info("Agent loop started")
        
        while self._running:
            try:
                # Wait for next message
                msg = await asyncio.wait_for(
                    self.bus.consume_inbound(),
                    timeout=1.0
                )
                
                # Process it
                try:
                    response = await self._process_message(msg)
                    if response:
                        await self.bus.publish_outbound(response)
                except Exception as e:
                    logger.error(f"Error processing message: {e}")
                    # Send error response
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content=f"Sorry, I encountered an error: {str(e)}"
                    ))
            except asyncio.TimeoutError:
                continue
    
    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        logger.info("Agent loop stopping")
    
    async def _process_message(self, msg: InboundMessage) -> OutboundMessage | None:
        """
        Process a single inbound message.
        
        Args:
            msg: The inbound message to process.
        
        Returns:
            The response message, or None if no response needed.
        """
        # Handle system messages (subagent announces)
        # The chat_id contains the original "channel:chat_id" to route back to
        if msg.channel == "system":
            return await self._process_system_message(msg)
        
        logger.info(f"Processing message from {msg.channel}:{msg.sender_id}")
        
        # Get or create session (respect active override)
        active_key = self.sessions.get_active_session_key(msg.channel, msg.chat_id)
        session_key = active_key or msg.session_key
        session = self.sessions.get_or_create(session_key)
        await self.compressor.compress_if_needed(session)
        session_summary = self.compressor.get_summary(session.key)
        history_limit = self._resolve_history_limit(session, session_summary)
        
        # Update tool contexts
        message_tool = self.tools.get("message")
        if isinstance(message_tool, MessageTool):
            message_tool.set_context(msg.channel, msg.chat_id)

        image_tool = self.tools.get("image_generate")
        if isinstance(image_tool, ImageGenerateTool):
            image_tool.set_context(msg.channel, msg.chat_id)
        
        spawn_tool = self.tools.get("spawn")
        if isinstance(spawn_tool, SpawnTool):
            spawn_tool.set_context(msg.channel, msg.chat_id)
        
        cron_tool = self.tools.get("cron")
        if isinstance(cron_tool, CronTool):
            cron_tool.set_context(msg.channel, msg.chat_id)

        session_tool = self.tools.get("session_manage")
        if isinstance(session_tool, SessionManageTool):
            session_tool.set_context(msg.channel, msg.chat_id)
        
        # Build initial messages (use get_history for LLM-formatted messages)
        messages = self.context.build_messages(
            history=session.get_history(
                max_messages=history_limit,
                tool_max_events=self._tool_digest_max_events,
                tool_preview_chars=self._tool_preview_chars,
                tool_max_chars=self._tool_digest_max_chars,
            ),
            current_message=msg.content,
            session_summary=session_summary,
            media=msg.media if msg.media else None,
            channel=msg.channel,
            chat_id=msg.chat_id,
        )
        
        # Agent loop
        iteration = 0
        final_content = None
        task_usage = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cache_tokens": 0,
        }
        if isinstance(message_tool, MessageTool):
            message_tool.set_token_monitor_factory(
                lambda: self._build_token_monitor(
                    task_usage,
                    self.max_tokens,
                    self.context_window_tokens,
                    self.token_budget_mode,
                )
            )

        feishu_stream_enabled = msg.channel == "feishu" and self.feishu_config.streaming_enabled
        stream_id = self._build_stream_id(msg) if feishu_stream_enabled else ""
        stream_initialized = False
        tool_log_entries: list[str] = []
        completed_tool_calls = 0

        session.add_message("user", msg.content)
        try:
            while iteration < self.max_iterations:
                iteration += 1
                
                # Call LLM
                response = await self.provider.chat(
                    messages=messages,
                    tools=self.tools.get_definitions(),
                    model=self.model,
                    max_tokens=self.max_tokens,
                    reasoning_effort=self.reasoning_effort,
                )
                self._accumulate_usage(task_usage, response.usage)
                
                # Handle tool calls
                if response.has_tool_calls:
                    # Add assistant message with tool calls
                    tool_call_dicts = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments, ensure_ascii=False)  # Must be JSON string
                            }
                        }
                        for tc in response.tool_calls
                    ]
                    messages = self.context.add_assistant_message(
                        messages, response.content, tool_call_dicts
                    )
                    
                    # Persist tool calls onto session list natively
                    session.add_message("assistant", response.content, tool_calls=tool_call_dicts)
                    
                    # Execute tools
                    for tool_call in response.tool_calls:
                        args_str = json.dumps(tool_call.arguments, indent=2, ensure_ascii=False)
                        logger.debug(f"Executing tool: {tool_call.name} with arguments: {args_str}")
                        if feishu_stream_enabled:
                            if not stream_initialized:
                                await self._publish_feishu_stream_init(
                                    msg=msg,
                                    stream_id=stream_id,
                                    token_monitor=self._build_token_monitor(
                                        task_usage,
                                        self.max_tokens,
                                        self.context_window_tokens,
                                        self.token_budget_mode,
                                        tool_calls_completed=completed_tool_calls,
                                    ),
                                    initial_text="正在调用工具，请稍候...",
                                )
                                stream_initialized = True

                            entry_prefix = f"###### {len(tool_log_entries) + 1}. `{tool_call.name}`"
                            args_preview = self._truncate_text(args_str, 2000)
                            tool_log_entries.append(
                                (
                                    f"{entry_prefix}\n"
                                    f"- 状态: 执行中\n"
                                    f"- 参数:\n"
                                    f"```json\n{args_preview}\n```"
                                )
                            )
                            await self._publish_feishu_tool_update(
                                msg=msg,
                                stream_id=stream_id,
                                tool_logs_markdown=self._build_tool_panel_markdown(tool_log_entries),
                                token_monitor=self._build_token_monitor(
                                    task_usage,
                                    self.max_tokens,
                                    self.context_window_tokens,
                                    self.token_budget_mode,
                                    tool_calls_completed=completed_tool_calls,
                                ),
                            )
                        else:
                            push_message = OutboundMessage(
                                channel=msg.channel,
                                chat_id=msg.chat_id,
                                content=(
                                    f"🛠️**正在调用工具**： `{tool_call.name}`\n"
                                    f"🔢**参数列表**：\n"
                                    f"```json\n{args_str}\n```"
                                ),
                                metadata={
                                    "token_monitor": self._build_token_monitor(
                                        task_usage,
                                        self.max_tokens,
                                        self.context_window_tokens,
                                        self.token_budget_mode,
                                    )
                                },
                            )
                            await self.bus.publish_outbound(push_message)
                        started = time.perf_counter()
                        result = await self.tools.execute(tool_call.name, tool_call.arguments)
                        result_text = result if isinstance(result, str) else str(result)
                        completed_tool_calls += 1

                        if feishu_stream_enabled and tool_log_entries:
                            elapsed = time.perf_counter() - started
                            entry_prefix = f"###### {len(tool_log_entries)}. `{tool_call.name}`"
                            args_preview = self._truncate_text(args_str, 2000)
                            tool_log_entries[-1] = (
                                f"{entry_prefix}\n"
                                f"- 状态: 已完成 ({elapsed:.2f}s)\n"
                                f"- 参数:\n"
                                f"```json\n{args_preview}\n```"
                            )
                            await self._publish_feishu_tool_update(
                                msg=msg,
                                stream_id=stream_id,
                                tool_logs_markdown=self._build_tool_panel_markdown(tool_log_entries),
                                token_monitor=self._build_token_monitor(
                                    task_usage,
                                    self.max_tokens,
                                    self.context_window_tokens,
                                    self.token_budget_mode,
                                    tool_calls_completed=completed_tool_calls,
                                ),
                            )
                        
                        # Instead of _record_tool_event digest, append natively
                        session.add_message("tool", result_text, tool_call_id=tool_call.id, name=tool_call.name)
                        
                        messages = self.context.add_tool_result(
                            messages, tool_call.id, tool_call.name, result
                        )
                else:
                    # No tool calls, we're done
                    final_content = response.content
                    break
        finally:
            if isinstance(message_tool, MessageTool):
                message_tool.set_token_monitor_factory(None)
        
        if final_content is None:
            final_content = "I've completed processing but have no response to give."
        
        # Save to session
        session.add_message("assistant", final_content)
        await self.compressor.compress_if_needed(session)
        self.sessions.save(session)

        token_monitor = self._build_token_monitor(
            task_usage,
            self.max_tokens,
            self.context_window_tokens,
            self.token_budget_mode,
            tool_calls_completed=completed_tool_calls if feishu_stream_enabled else 0,
        )

        if self._should_publish_feishu_streaming(msg, final_content):
            await self._publish_feishu_streaming_response(
                msg,
                final_content,
                token_monitor,
                stream_id=stream_id,
                send_init=not stream_initialized,
            )
            return None
        
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final_content,
            metadata={"token_monitor": token_monitor},
        )

    def _should_publish_feishu_streaming(self, msg: InboundMessage, final_content: str) -> bool:
        """Use phase-1 pseudo streaming for Feishu outbound delivery."""
        return (
            msg.channel == "feishu"
            and self.feishu_config.streaming_enabled
            and bool(final_content)
        )

    @staticmethod
    def _build_stream_id(msg: InboundMessage) -> str:
        """Generate one stream identifier shared across tool logs and final answer."""
        return f"{msg.channel}:{msg.chat_id}:{int(time.time() * 1000)}"

    @staticmethod
    def _truncate_text(value: str, limit: int) -> str:
        if len(value) <= limit:
            return value
        return f"{value[:limit]}\n...\n(内容已截断，原始长度: {len(value)} 字符)"

    def _build_tool_panel_markdown(self, entries: list[str]) -> str:
        header = "###### 工具调用记录"
        if not entries:
            return f"{header}\n\n暂无工具调用。"
        return f"{header}\n\n" + "\n\n".join(entries)

    async def _publish_feishu_stream_init(
        self,
        msg: InboundMessage,
        stream_id: str,
        token_monitor: dict[str, Any],
        initial_text: str = "",
    ) -> None:
        await self.bus.publish_outbound(
            OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=initial_text,
                metadata={
                    "token_monitor": token_monitor,
                    "feishu_stream": {
                        "action": "init",
                        "stream_id": stream_id,
                        "full_text": initial_text,
                    },
                },
            )
        )

    async def _publish_feishu_tool_update(
        self,
        msg: InboundMessage,
        stream_id: str,
        tool_logs_markdown: str,
        token_monitor: dict[str, Any],
    ) -> None:
        await self.bus.publish_outbound(
            OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content="",
                metadata={
                    "token_monitor": token_monitor,
                    "feishu_stream": {
                        "action": "tool_update",
                        "stream_id": stream_id,
                        "tool_logs_markdown": tool_logs_markdown,
                        "force": True,
                    },
                },
            )
        )

    async def _publish_feishu_streaming_response(
        self,
        msg: InboundMessage,
        final_content: str,
        token_monitor: dict[str, Any],
        stream_id: str | None = None,
        send_init: bool = True,
    ) -> None:
        """Publish init/append/finalize events consumed by FeishuChannel streaming state machine."""
        stream_id = stream_id or self._build_stream_id(msg)
        chunk_chars = max(20, int(self.feishu_config.streaming_print_step_default) * 16)
        interval = max(0.08, float(self.feishu_config.streaming_print_frequency_ms_default) / 1000.0 * 4.0)

        if send_init:
            await self._publish_feishu_stream_init(
                msg=msg,
                stream_id=stream_id,
                token_monitor=token_monitor,
                initial_text="",
            )

        total = len(final_content)
        cursor = 0
        while cursor < total:
            cursor = min(total, cursor + chunk_chars)
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=final_content[:cursor],
                    metadata={
                        "token_monitor": token_monitor,
                        "feishu_stream": {
                            "action": "append",
                            "stream_id": stream_id,
                            "full_text": final_content[:cursor],
                        },
                    },
                )
            )
            if cursor < total:
                await asyncio.sleep(interval)

        await self.bus.publish_outbound(
            OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=final_content,
                metadata={
                    "token_monitor": token_monitor,
                    "feishu_stream": {
                        "action": "finalize",
                        "stream_id": stream_id,
                        "full_text": final_content,
                    },
                },
            )
        )
    
    async def _process_system_message(self, msg: InboundMessage) -> OutboundMessage | None:
        """
        Process a system message (e.g., subagent announce).
        
        The chat_id field contains "original_channel:original_chat_id" to route
        the response back to the correct destination.
        """
        logger.info(f"Processing system message from {msg.sender_id}")
        
        # Parse origin from chat_id (format: "channel:chat_id")
        if ":" in msg.chat_id:
            parts = msg.chat_id.split(":", 1)
            origin_channel = parts[0]
            origin_chat_id = parts[1]
        else:
            # Fallback
            origin_channel = "cli"
            origin_chat_id = msg.chat_id
        
        # Use the origin session for context (respect active override)
        active_key = self.sessions.get_active_session_key(origin_channel, origin_chat_id)
        session_key = active_key or f"{origin_channel}:{origin_chat_id}"
        session = self.sessions.get_or_create(session_key)
        await self.compressor.compress_if_needed(session)
        session_summary = self.compressor.get_summary(session.key)
        history_limit = self._resolve_history_limit(session, session_summary)
        
        # Update tool contexts
        message_tool = self.tools.get("message")
        if isinstance(message_tool, MessageTool):
            message_tool.set_context(origin_channel, origin_chat_id)

        image_tool = self.tools.get("image_generate")
        if isinstance(image_tool, ImageGenerateTool):
            image_tool.set_context(origin_channel, origin_chat_id)
        
        spawn_tool = self.tools.get("spawn")
        if isinstance(spawn_tool, SpawnTool):
            spawn_tool.set_context(origin_channel, origin_chat_id)
        
        cron_tool = self.tools.get("cron")
        if isinstance(cron_tool, CronTool):
            cron_tool.set_context(origin_channel, origin_chat_id)

        session_tool = self.tools.get("session_manage")
        if isinstance(session_tool, SessionManageTool):
            session_tool.set_context(origin_channel, origin_chat_id)
        
        # Build messages with the announce content
        messages = self.context.build_messages(
            history=session.get_history(
                max_messages=history_limit,
                tool_max_events=self._tool_digest_max_events,
                tool_preview_chars=self._tool_preview_chars,
                tool_max_chars=self._tool_digest_max_chars,
            ),
            current_message=msg.content,
            session_summary=session_summary,
            channel=origin_channel,
            chat_id=origin_chat_id,
        )
        
        # Agent loop (limited for announce handling)
        iteration = 0
        final_content = None
        task_usage = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cache_tokens": 0,
        }
        if (
            self.merge_subagent_usage
            and msg.sender_id == "subagent"
            and isinstance(msg.metadata, dict)
            and isinstance(msg.metadata.get("subagent_usage"), dict)
        ):
            self._accumulate_usage(task_usage, msg.metadata.get("subagent_usage"))
        if isinstance(message_tool, MessageTool):
            message_tool.set_token_monitor_factory(
                lambda: self._build_token_monitor(
                    task_usage,
                    self.max_tokens,
                    self.context_window_tokens,
                    self.token_budget_mode,
                )
            )
        
        session.add_message("user", f"[System: {msg.sender_id}] {msg.content}")
        try:
            while iteration < self.max_iterations:
                iteration += 1
                
                response = await self.provider.chat(
                    messages=messages,
                    tools=self.tools.get_definitions(),
                    model=self.model,
                    max_tokens=self.max_tokens,
                    reasoning_effort=self.reasoning_effort,
                )
                self._accumulate_usage(task_usage, response.usage)
                
                if response.has_tool_calls:
                    tool_call_dicts = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments, ensure_ascii=False)
                            }
                        }
                        for tc in response.tool_calls
                    ]
                    messages = self.context.add_assistant_message(
                        messages, response.content, tool_call_dicts
                    )
                    
                    # Persist tool calls onto session list natively
                    session.add_message("assistant", response.content, tool_calls=tool_call_dicts)
                    
                    for tool_call in response.tool_calls:
                        args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                        logger.debug(f"Executing tool: {tool_call.name} with arguments: {args_str}")
                        started = time.perf_counter()
                        result = await self.tools.execute(tool_call.name, tool_call.arguments)
                        result_text = result if isinstance(result, str) else str(result)
                        
                        session.add_message("tool", result_text, tool_call_id=tool_call.id, name=tool_call.name)
                        
                        messages = self.context.add_tool_result(
                            messages, tool_call.id, tool_call.name, result
                        )
                else:
                    final_content = response.content
                    break
        finally:
            if isinstance(message_tool, MessageTool):
                message_tool.set_token_monitor_factory(None)
        
        if final_content is None:
            final_content = "Background task completed."
        
        # Save to session (mark as system message in history)
        session.add_message("assistant", final_content)
        await self.compressor.compress_if_needed(session)
        self.sessions.save(session)

        token_monitor = self._build_token_monitor(
            task_usage,
            self.max_tokens,
            self.context_window_tokens,
            self.token_budget_mode,
        )
        
        return OutboundMessage(
            channel=origin_channel,
            chat_id=origin_chat_id,
            content=final_content,
            metadata={"token_monitor": token_monitor},
        )
    
    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
    ) -> str:
        """
        Process a message directly (for CLI or cron usage).
        
        Args:
            content: The message content.
            session_key: Session identifier.
            channel: Source channel (for context).
            chat_id: Source chat ID (for context).
        
        Returns:
            The agent's response.
        """
        msg = InboundMessage(
            channel=channel,
            sender_id="user",
            chat_id=chat_id,
            content=content
        )
        
        response = await self._process_message(msg)
        return response.content if response else ""
