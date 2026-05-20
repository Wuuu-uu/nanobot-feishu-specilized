"""Feishu/Lark channel implementation using lark-oapi SDK with WebSocket long connection."""

import asyncio
import importlib.util
import json
import os
import re
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Literal

from lark_oapi.api.im.v1.model import MentionEvent, P2ImMessageReceiveV1
from lark_oapi.core.const import FEISHU_DOMAIN, LARK_DOMAIN
from loguru import logger
from pydantic import Field

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.paths import get_media_dir
from nanobot.config.schema import Base

FEISHU_AVAILABLE = importlib.util.find_spec("lark_oapi") is not None

# Message type display mapping
MSG_TYPE_MAP = {
    "image": "[image]",
    "audio": "[audio]",
    "file": "[file]",
    "sticker": "[sticker]",
}


def _extract_share_card_content(content_json: dict, msg_type: str) -> str:
    """Extract text representation from share cards and interactive messages."""
    parts = []

    if msg_type == "share_chat":
        parts.append(f"[shared chat: {content_json.get('chat_id', '')}]")
    elif msg_type == "share_user":
        parts.append(f"[shared user: {content_json.get('user_id', '')}]")
    elif msg_type == "interactive":
        parts.extend(_extract_interactive_content(content_json))
    elif msg_type == "share_calendar_event":
        parts.append(f"[shared calendar event: {content_json.get('event_key', '')}]")
    elif msg_type == "system":
        parts.append("[system message]")
    elif msg_type == "merge_forward":
        parts.append("[merged forward messages]")

    return "\n".join(parts) if parts else f"[{msg_type}]"


def _extract_interactive_content(content: dict) -> list[str]:
    """Recursively extract text and links from interactive card content."""
    parts = []

    if isinstance(content, str):
        try:
            content = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            return [content] if content.strip() else []

    if not isinstance(content, dict):
        return parts

    if "title" in content:
        title = content["title"]
        if isinstance(title, dict):
            title_content = title.get("content", "") or title.get("text", "")
            if title_content:
                parts.append(f"title: {title_content}")
        elif isinstance(title, str):
            parts.append(f"title: {title}")

    for elements in (
        content.get("elements", []) if isinstance(content.get("elements"), list) else []
    ):
        for element in elements:
            parts.extend(_extract_element_content(element))

    card = content.get("card", {})
    if card:
        parts.extend(_extract_interactive_content(card))

    header = content.get("header", {})
    if header:
        header_title = header.get("title", {})
        if isinstance(header_title, dict):
            header_text = header_title.get("content", "") or header_title.get("text", "")
            if header_text:
                parts.append(f"title: {header_text}")

    return parts


def _extract_element_content(element: dict) -> list[str]:
    """Extract content from a single card element."""
    parts = []

    if not isinstance(element, dict):
        return parts

    tag = element.get("tag", "")

    if tag in ("markdown", "lark_md"):
        content = element.get("content", "")
        if content:
            parts.append(content)

    elif tag == "div":
        text = element.get("text", {})
        if isinstance(text, dict):
            text_content = text.get("content", "") or text.get("text", "")
            if text_content:
                parts.append(text_content)
        elif isinstance(text, str):
            parts.append(text)
        for field in element.get("fields", []):
            if isinstance(field, dict):
                field_text = field.get("text", {})
                if isinstance(field_text, dict):
                    c = field_text.get("content", "")
                    if c:
                        parts.append(c)

    elif tag == "a":
        href = element.get("href", "")
        text = element.get("text", "")
        if href:
            parts.append(f"link: {href}")
        if text:
            parts.append(text)

    elif tag == "button":
        text = element.get("text", {})
        if isinstance(text, dict):
            c = text.get("content", "")
            if c:
                parts.append(c)
        url = element.get("url", "") or element.get("multi_url", {}).get("url", "")
        if url:
            parts.append(f"link: {url}")

    elif tag == "img":
        alt = element.get("alt", {})
        parts.append(alt.get("content", "[image]") if isinstance(alt, dict) else "[image]")

    elif tag == "note":
        for ne in element.get("elements", []):
            parts.extend(_extract_element_content(ne))

    elif tag == "column_set":
        for col in element.get("columns", []):
            for ce in col.get("elements", []):
                parts.extend(_extract_element_content(ce))

    elif tag == "plain_text":
        content = element.get("content", "")
        if content:
            parts.append(content)

    else:
        for ne in element.get("elements", []):
            parts.extend(_extract_element_content(ne))

    return parts


def _extract_post_content(content_json: dict) -> tuple[str, list[str]]:
    """Extract text and image keys from Feishu post (rich text) message.

    Handles three payload shapes:
    - Direct:    {"title": "...", "content": [[...]]}
    - Localized: {"zh_cn": {"title": "...", "content": [...]}}
    - Wrapped:   {"post": {"zh_cn": {"title": "...", "content": [...]}}}
    """

    def _parse_block(block: dict) -> tuple[str | None, list[str]]:
        if not isinstance(block, dict) or not isinstance(block.get("content"), list):
            return None, []
        texts, images = [], []
        if title := block.get("title"):
            texts.append(title)
        for row in block["content"]:
            if not isinstance(row, list):
                continue
            for el in row:
                if not isinstance(el, dict):
                    continue
                tag = el.get("tag")
                if tag in ("text", "a"):
                    texts.append(el.get("text", ""))
                elif tag == "at":
                    texts.append(f"@{el.get('user_name', 'user')}")
                elif tag == "code_block":
                    lang = el.get("language", "")
                    code_text = el.get("text", "")
                    texts.append(f"\n```{lang}\n{code_text}\n```\n")
                elif tag == "img" and (key := el.get("image_key")):
                    images.append(key)
        return (" ".join(texts).strip() or None), images

    # Unwrap optional {"post": ...} envelope
    root = content_json
    if isinstance(root, dict) and isinstance(root.get("post"), dict):
        root = root["post"]
    if not isinstance(root, dict):
        return "", []

    # Direct format
    if "content" in root:
        text, imgs = _parse_block(root)
        if text or imgs:
            return text or "", imgs

    # Localized: prefer known locales, then fall back to any dict child
    for key in ("zh_cn", "en_us", "ja_jp"):
        if key in root:
            text, imgs = _parse_block(root[key])
            if text or imgs:
                return text or "", imgs
    for val in root.values():
        if isinstance(val, dict):
            text, imgs = _parse_block(val)
            if text or imgs:
                return text or "", imgs

    return "", []


def _extract_post_text(content_json: dict) -> str:
    """Extract plain text from Feishu post (rich text) message content.

    Legacy wrapper for _extract_post_content, returns only text.
    """
    text, _ = _extract_post_content(content_json)
    return text


class FeishuConfig(Base):
    """Feishu/Lark channel configuration using WebSocket long connection."""

    enabled: bool = False
    app_id: str = ""
    app_secret: str = ""
    encrypt_key: str = ""
    verification_token: str = ""
    allow_from: list[str] = Field(default_factory=list)
    react_emoji: str = "THUMBSUP"
    done_emoji: str | None = None  # Emoji to show when task is completed (e.g., "DONE", "OK")
    tool_hint_prefix: str = "\U0001f527"  # Prefix for inline tool hints (default: 🔧)
    group_policy: Literal["open", "mention"] = "mention"
    reply_to_message: bool = False  # If True, bot replies quote the user's original message
    streaming: bool = True
    domain: Literal["feishu", "lark"] = "feishu"  # Set to "lark" for international Lark


_STREAM_ELEMENT_ID = "streaming_md"
_TOOL_LOGS_ELEMENT_ID = "tool_logs_markdown"
_TOKEN_CHART_ELEMENT_ID = "token_budget_chart"
_TOKEN_CHART_HEIGHT = "56px"
_TOOL_LOGS_TEXT_SIZE = "normal"
_STREAM_SEGMENT_SEPARATOR = "\n\n---\n\n"
_EMPTY_TOOL_LOGS_MARKDOWN = "**工具调用记录**\n\n暂无工具调用。"

_TOOL_DISPLAY_NAMES: dict[str, str] = {
    "append_file": "追加文件",
    "cron": "定时任务",
    "edit_file": "编辑文件",
    "exec": "执行命令",
    "grep": "搜索文件",
    "image_generate": "图像生成",
    "list_dir": "列出目录",
    "memory_search": "记忆检索",
    "message": "发送消息",
    "my": "运行状态",
    "notebook_edit": "编辑 Notebook",
    "notion": "Notion",
    "pdf_mineru": "文档解析",
    "read_file": "读取文件",
    "session_manage": "会话管理",
    "spawn": "子任务",
    "web_fetch": "读取网页",
    "web_search": "网络搜索",
    "write_file": "写入文件",
}

_TOOL_PHASE_LABELS: dict[str, str] = {
    "start": "执行中",
    "end": "已完成",
    "error": "失败",
}


@dataclass
class _FeishuStreamBuf:
    """Per-chat streaming accumulator using CardKit streaming API."""

    text: str = ""
    card_id: str | None = None
    sequence: int = 0
    last_edit: float = 0.0
    tool_events: list[dict[str, Any]] = field(default_factory=list)
    token_usage: dict[str, int] = field(default_factory=dict)
    needs_segment_separator: bool = False


class FeishuChannel(BaseChannel):
    """
    Feishu/Lark channel using WebSocket long connection.

    Uses WebSocket to receive events - no public IP or webhook required.

    Requires:
    - App ID and App Secret from Feishu Open Platform
    - Bot capability enabled
    - Event subscription enabled (im.message.receive_v1)
    """

    name = "feishu"
    display_name = "Feishu"

    _STREAM_EDIT_INTERVAL = 0.5  # throttle between CardKit streaming updates

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return FeishuConfig().model_dump(by_alias=True)

    def __init__(self, config: Any, bus: MessageBus):
        import lark_oapi as lark

        if isinstance(config, dict):
            config = FeishuConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: FeishuConfig = config
        self._client: lark.Client = None
        self._ws_client: Any = None
        self._ws_thread: threading.Thread | None = None
        self._processed_message_ids: OrderedDict[str, None] = OrderedDict()  # Ordered dedup cache
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stream_bufs: dict[str, _FeishuStreamBuf] = {}
        self._bot_open_id: str | None = None
        self._background_tasks: set[asyncio.Task] = set()
        self._reaction_ids: dict[str, str] = {}  # message_id → reaction_id

    @staticmethod
    def _register_optional_event(builder: Any, method_name: str, handler: Any) -> Any:
        """Register an event handler only when the SDK supports it."""
        method = getattr(builder, method_name, None)
        return method(handler) if callable(method) else builder

    async def start(self) -> None:
        """Start the Feishu bot with WebSocket long connection."""
        if not FEISHU_AVAILABLE:
            logger.error("Feishu SDK not installed. Run: pip install lark-oapi")
            return

        if not self.config.app_id or not self.config.app_secret:
            logger.error("Feishu app_id and app_secret not configured")
            return

        import lark_oapi as lark

        self._running = True
        self._loop = asyncio.get_running_loop()

        # Create Lark client for sending messages
        domain = LARK_DOMAIN if self.config.domain == "lark" else FEISHU_DOMAIN
        self._client = (
            lark.Client.builder()
            .app_id(self.config.app_id)
            .app_secret(self.config.app_secret)
            .domain(domain)
            .log_level(lark.LogLevel.INFO)
            .build()
        )
        builder = lark.EventDispatcherHandler.builder(
            self.config.encrypt_key or "",
            self.config.verification_token or "",
        ).register_p2_im_message_receive_v1(self._on_message_sync)
        builder = self._register_optional_event(
            builder, "register_p2_im_message_reaction_created_v1", self._on_reaction_created
        )
        builder = self._register_optional_event(
            builder, "register_p2_im_message_reaction_deleted_v1", self._on_reaction_deleted
        )
        builder = self._register_optional_event(
            builder, "register_p2_im_message_message_read_v1", self._on_message_read
        )
        builder = self._register_optional_event(
            builder,
            "register_p2_im_chat_access_event_bot_p2p_chat_entered_v1",
            self._on_bot_p2p_chat_entered,
        )
        event_handler = builder.build()

        # Create WebSocket client for long connection
        self._ws_client = lark.ws.Client(
            self.config.app_id,
            self.config.app_secret,
            domain=domain,
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO,
        )

        # Start WebSocket client in a separate thread with reconnect loop.
        # A dedicated event loop is created for this thread so that lark_oapi's
        # module-level `loop = asyncio.get_event_loop()` picks up an idle loop
        # instead of the already-running main asyncio loop, which would cause
        # "This event loop is already running" errors.
        def run_ws():
            import time

            import lark_oapi.ws.client as _lark_ws_client

            ws_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(ws_loop)
            # Patch the module-level loop used by lark's ws Client.start()
            _lark_ws_client.loop = ws_loop
            try:
                while self._running:
                    try:
                        self._ws_client.start()
                    except Exception as e:
                        logger.warning("Feishu WebSocket error: {}", e)
                    if self._running:
                        time.sleep(5)
            finally:
                ws_loop.close()

        self._ws_thread = threading.Thread(target=run_ws, daemon=True)
        self._ws_thread.start()

        # Fetch bot's own open_id for accurate @mention matching
        self._bot_open_id = await asyncio.get_running_loop().run_in_executor(
            None, self._fetch_bot_open_id
        )
        if self._bot_open_id:
            logger.info("Feishu bot open_id: {}", self._bot_open_id)
        else:
            logger.warning("Could not fetch bot open_id; @mention matching may be inaccurate")

        logger.info("Feishu bot started with WebSocket long connection")
        logger.info("No public IP required - using WebSocket to receive events")

        # Keep running until stopped
        while self._running:
            await asyncio.sleep(1)

    async def stop(self) -> None:
        """
        Stop the Feishu bot.

        Notice: lark.ws.Client does not expose stop method， simply exiting the program will close the client.

        Reference: https://github.com/larksuite/oapi-sdk-python/blob/v2_main/lark_oapi/ws/client.py#L86
        """
        self._running = False
        logger.info("Feishu bot stopped")

    def _fetch_bot_open_id(self) -> str | None:
        """Fetch the bot's own open_id via GET /open-apis/bot/v3/info."""
        try:
            import lark_oapi as lark

            request = (
                lark.BaseRequest.builder()
                .http_method(lark.HttpMethod.GET)
                .uri("/open-apis/bot/v3/info")
                .token_types({lark.AccessTokenType.APP})
                .build()
            )
            response = self._client.request(request)
            if response.success():
                import json

                data = json.loads(response.raw.content)
                bot = (data.get("data") or data).get("bot") or data.get("bot") or {}
                return bot.get("open_id")
            logger.warning("Failed to get bot info: code={}, msg={}", response.code, response.msg)
            return None
        except Exception as e:
            logger.warning("Error fetching bot info: {}", e)
            return None

    @staticmethod
    def _resolve_mentions(text: str, mentions: list[MentionEvent] | None) -> str:
        """Replace @_user_n placeholders with actual user info from mentions.

        Args:
            text: The message text containing @_user_n placeholders
            mentions: List of mention objects from Feishu message

        Returns:
            Text with placeholders replaced by @姓名 (open_id)
        """
        if not mentions or not text:
            return text

        for mention in mentions:
            key = mention.key or None
            if not key or key not in text:
                continue

            user_id_obj = mention.id or None
            if not user_id_obj:
                continue

            open_id = user_id_obj.open_id
            user_id = user_id_obj.user_id
            name = mention.name or key

            # Format: @姓名 (open_id, user_id: xxx)
            if open_id and user_id:
                replacement = f"@{name} ({open_id}, user id: {user_id})"
            elif open_id:
                replacement = f"@{name} ({open_id})"
            else:
                replacement = f"@{name}"

            text = text.replace(key, replacement)

        return text

    def _is_bot_mentioned(self, message: Any) -> bool:
        """Check if the bot is @mentioned in the message."""
        raw_content = message.content or ""
        if "@_all" in raw_content:
            return True

        for mention in getattr(message, "mentions", None) or []:
            mid = getattr(mention, "id", None)
            if not mid:
                continue
            mention_open_id = getattr(mid, "open_id", None) or ""
            if self._bot_open_id:
                if mention_open_id == self._bot_open_id:
                    return True
            else:
                # Fallback heuristic when bot open_id is unavailable
                if not getattr(mid, "user_id", None) and mention_open_id.startswith("ou_"):
                    return True
        return False

    def _is_group_message_for_bot(self, message: Any) -> bool:
        """Allow group messages when policy is open or bot is @mentioned."""
        if self.config.group_policy == "open":
            return True
        return self._is_bot_mentioned(message)

    def _add_reaction_sync(self, message_id: str, emoji_type: str) -> str | None:
        """Sync helper for adding reaction (runs in thread pool)."""
        from lark_oapi.api.im.v1 import (
            CreateMessageReactionRequest,
            CreateMessageReactionRequestBody,
            Emoji,
        )

        try:
            request = (
                CreateMessageReactionRequest.builder()
                .message_id(message_id)
                .request_body(
                    CreateMessageReactionRequestBody.builder()
                    .reaction_type(Emoji.builder().emoji_type(emoji_type).build())
                    .build()
                )
                .build()
            )

            response = self._client.im.v1.message_reaction.create(request)

            if not response.success():
                logger.warning(
                    "Failed to add reaction: code={}, msg={}", response.code, response.msg
                )
                return None
            else:
                logger.debug("Added {} reaction to message {}", emoji_type, message_id)
                return response.data.reaction_id if response.data else None
        except Exception as e:
            logger.warning("Error adding reaction: {}", e)
            return None

    async def _add_reaction(self, message_id: str, emoji_type: str = "THUMBSUP") -> str | None:
        """Add a reaction emoji to a message.

        Returns the reaction_id on success, None on failure.
        When called via a tracked background task, the returned reaction_id
        is stored in ``_reaction_ids`` for later cleanup by ``send_delta``.

        Common emoji types: THUMBSUP, OK, EYES, DONE, OnIt, HEART
        """
        if not self._client:
            return None

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._add_reaction_sync, message_id, emoji_type)

    def _remove_reaction_sync(self, message_id: str, reaction_id: str) -> None:
        """Sync helper for removing reaction (runs in thread pool)."""
        from lark_oapi.api.im.v1 import DeleteMessageReactionRequest

        try:
            request = (
                DeleteMessageReactionRequest.builder()
                .message_id(message_id)
                .reaction_id(reaction_id)
                .build()
            )

            response = self._client.im.v1.message_reaction.delete(request)
            if response.success():
                logger.debug("Removed reaction {} from message {}", reaction_id, message_id)
            else:
                logger.debug(
                    "Failed to remove reaction: code={}, msg={}", response.code, response.msg
                )
        except Exception as e:
            logger.debug("Error removing reaction: {}", e)

    async def _remove_reaction(self, message_id: str, reaction_id: str) -> None:
        """
        Remove a reaction emoji from a message (non-blocking).

        Used to clear the "processing" indicator after bot replies.
        """
        if not self._client or not reaction_id:
            return

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._remove_reaction_sync, message_id, reaction_id)

    def _on_background_task_done(self, task: asyncio.Task) -> None:
        """Callback: remove from tracking set and log unhandled exceptions."""
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception as exc:
            logger.warning("Background task failed: {}", exc)

    def _on_reaction_added(self, message_id: str, task: asyncio.Task) -> None:
        """Callback: store reaction_id after background add-reaction completes."""
        if task.cancelled():
            return
        # Failures already logged by _on_background_task_done.
        with suppress(Exception):
            reaction_id = task.result()
            if reaction_id:
                self._reaction_ids[message_id] = reaction_id
        # Trim cache to prevent unbounded growth
        if len(self._reaction_ids) > 500:
            self._reaction_ids.pop(next(iter(self._reaction_ids)))

    @staticmethod
    def _stream_key(chat_id: str, metadata: dict[str, Any] | None = None) -> str:
        """Scope streaming buffers to the inbound message when available."""
        meta = metadata or {}
        return meta.get("message_id") or chat_id

    # Regex to match markdown tables (header + separator + data rows)
    _TABLE_RE = re.compile(
        r"((?:^[ \t]*\|.+\|[ \t]*\n)(?:^[ \t]*\|[-:\s|]+\|[ \t]*\n)(?:^[ \t]*\|.+\|[ \t]*\n?)+)",
        re.MULTILINE,
    )

    _HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)

    _CODE_BLOCK_RE = re.compile(r"(```[\s\S]*?```)", re.MULTILINE)
    _INDENTED_CODE_FENCE_RE = re.compile(r"^([ \t]{1,3})(`{3,}|~{3,})(.*)$")

    # Markdown formatting patterns that should be stripped from plain-text
    # surfaces like table cells and heading text.
    _MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
    _MD_BOLD_UNDERSCORE_RE = re.compile(r"__(.+?)__")
    _MD_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
    _MD_STRIKE_RE = re.compile(r"~~(.+?)~~")

    @classmethod
    def _strip_md_formatting(cls, text: str) -> str:
        """Strip markdown formatting markers from text for plain display.

        Feishu table cells do not support markdown rendering, so we remove
        the formatting markers to keep the text readable.
        """
        # Remove bold markers
        text = cls._MD_BOLD_RE.sub(r"\1", text)
        text = cls._MD_BOLD_UNDERSCORE_RE.sub(r"\1", text)
        # Remove italic markers
        text = cls._MD_ITALIC_RE.sub(r"\1", text)
        # Remove strikethrough markers
        text = cls._MD_STRIKE_RE.sub(r"\1", text)
        return text

    @classmethod
    def _parse_md_table(cls, table_text: str) -> dict | None:
        """Parse a markdown table into a Feishu table element."""
        lines = [_line.strip() for _line in table_text.strip().split("\n") if _line.strip()]
        if len(lines) < 3:
            return None

        def split(_line: str) -> list[str]:
            return [c.strip() for c in _line.strip("|").split("|")]

        headers = [cls._strip_md_formatting(h) for h in split(lines[0])]
        rows = [[cls._strip_md_formatting(c) for c in split(_line)] for _line in lines[2:]]
        columns = [
            {"tag": "column", "name": f"c{i}", "display_name": h, "width": "auto"}
            for i, h in enumerate(headers)
        ]
        return {
            "tag": "table",
            "page_size": len(rows) + 1,
            "columns": columns,
            "rows": [
                {f"c{i}": r[i] if i < len(r) else "" for i in range(len(headers))} for r in rows
            ],
        }

    def _build_card_elements(self, content: str) -> list[dict]:
        """Split content into div/markdown + table elements for Feishu card."""
        elements, last_end = [], 0
        for m in self._TABLE_RE.finditer(content):
            before = content[last_end : m.start()]
            if before.strip():
                elements.extend(self._split_headings(before))
            elements.append(
                self._parse_md_table(m.group(1)) or {"tag": "markdown", "content": m.group(1)}
            )
            last_end = m.end()
        remaining = content[last_end:]
        if remaining.strip():
            elements.extend(self._split_headings(remaining))
        return elements or [{"tag": "markdown", "content": content}]

    @staticmethod
    def _split_elements_by_table_limit(
        elements: list[dict], max_tables: int = 1
    ) -> list[list[dict]]:
        """Split card elements into groups with at most *max_tables* table elements each.

        Feishu cards have a hard limit of one table per card (API error 11310).
        When the rendered content contains multiple markdown tables each table is
        placed in a separate card message so every table reaches the user.
        """
        if not elements:
            return [[]]
        groups: list[list[dict]] = []
        current: list[dict] = []
        table_count = 0
        for el in elements:
            if el.get("tag") == "table":
                if table_count >= max_tables:
                    if current:
                        groups.append(current)
                    current = []
                    table_count = 0
                current.append(el)
                table_count += 1
            else:
                current.append(el)
        if current:
            groups.append(current)
        return groups or [[]]

    def _split_headings(self, content: str) -> list[dict]:
        """Split content by headings, converting headings to div elements."""
        content = self._normalize_card_markdown(content)
        protected = content
        code_blocks = []
        for m in self._CODE_BLOCK_RE.finditer(content):
            code_blocks.append(m.group(1))
            protected = protected.replace(m.group(1), f"\x00CODE{len(code_blocks) - 1}\x00", 1)

        elements = []
        last_end = 0
        for m in self._HEADING_RE.finditer(protected):
            before = protected[last_end : m.start()].strip()
            if before:
                elements.append({"tag": "markdown", "content": before})
            text = self._strip_md_formatting(m.group(2).strip())
            display_text = f"**{text}**" if text else ""
            elements.append(
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": display_text,
                    },
                }
            )
            last_end = m.end()
        remaining = protected[last_end:].strip()
        if remaining:
            elements.append({"tag": "markdown", "content": remaining})

        for i, cb in enumerate(code_blocks):
            for el in elements:
                if el.get("tag") == "markdown":
                    el["content"] = el["content"].replace(f"\x00CODE{i}\x00", cb)

        return elements or [{"tag": "markdown", "content": content}]

    @classmethod
    def _normalize_card_markdown(cls, content: str) -> str:
        """Normalize Markdown constructs that Feishu CardKit renders unreliably.

        Feishu often drops fenced code blocks when they are indented under list
        items. CommonMark allows up to three leading spaces before a fence, so
        lift those blocks to column zero before sending them to CardKit.
        """
        if "```" not in content and "~~~" not in content:
            return content

        lines = content.splitlines(keepends=True)
        normalized: list[str] = []
        fence_indent = ""
        fence_char = ""
        fence_len = 0
        in_fence = False

        for line in lines:
            raw = line.rstrip("\r\n")
            newline = line[len(raw):]
            if not in_fence:
                match = cls._INDENTED_CODE_FENCE_RE.match(raw)
                if match:
                    fence_indent, fence_marker, rest = match.groups()
                    fence_char = fence_marker[0]
                    fence_len = len(fence_marker)
                    in_fence = True
                    normalized.append(f"{fence_marker}{rest}{newline}")
                    continue
                normalized.append(line)
                continue

            lifted = raw[len(fence_indent):] if raw.startswith(fence_indent) else raw
            normalized.append(f"{lifted}{newline}")
            if lifted.startswith(fence_char * fence_len):
                in_fence = False
                fence_indent = ""
                fence_char = ""
                fence_len = 0

        return "".join(normalized)

    # ── Smart format detection ──────────────────────────────────────────
    # Patterns that indicate "complex" markdown needing card rendering
    _COMPLEX_MD_RE = re.compile(
        r"```"  # fenced code block
        r"|^\|.+\|.*\n\s*\|[-:\s|]+\|"  # markdown table (header + separator)
        r"|^#{1,6}\s+",  # headings
        re.MULTILINE,
    )

    # Simple markdown patterns (bold, italic, strikethrough)
    _SIMPLE_MD_RE = re.compile(
        r"\*\*.+?\*\*"  # **bold**
        r"|__.+?__"  # __bold__
        r"|(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)"  # *italic* (single *)
        r"|~~.+?~~",  # ~~strikethrough~~
        re.DOTALL,
    )

    # Markdown link: [text](url)
    _MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\)]+)\)")

    # Unordered list items
    _LIST_RE = re.compile(r"^[\s]*[-*+]\s+", re.MULTILINE)

    # Ordered list items
    _OLIST_RE = re.compile(r"^[\s]*\d+\.\s+", re.MULTILINE)

    # Max length for plain text format
    _TEXT_MAX_LEN = 200

    # Max length for post (rich text) format; beyond this, use card
    _POST_MAX_LEN = 2000

    @classmethod
    def _detect_msg_format(cls, content: str) -> str:
        """Determine the optimal Feishu message format for *content*.

        Returns one of:
        - ``"text"``        – plain text, short and no markdown
        - ``"post"``        – rich text (links only, moderate length)
        - ``"interactive"`` – card with full markdown rendering
        """
        stripped = content.strip()

        # Complex markdown (code blocks, tables, headings) → always card
        if cls._COMPLEX_MD_RE.search(stripped):
            return "interactive"

        # Long content → card (better readability with card layout)
        if len(stripped) > cls._POST_MAX_LEN:
            return "interactive"

        # Has bold/italic/strikethrough → card (post format can't render these)
        if cls._SIMPLE_MD_RE.search(stripped):
            return "interactive"

        # Has list items → card (post format can't render list bullets well)
        if cls._LIST_RE.search(stripped) or cls._OLIST_RE.search(stripped):
            return "interactive"

        # Has links → post format (supports <a> tags)
        if cls._MD_LINK_RE.search(stripped):
            return "post"

        # Short plain text → text format
        if len(stripped) <= cls._TEXT_MAX_LEN:
            return "text"

        # Medium plain text without any formatting → post format
        return "post"

    @classmethod
    def _markdown_to_post(cls, content: str) -> str:
        """Convert markdown content to Feishu post message JSON.

        Handles links ``[text](url)`` as ``a`` tags; everything else as ``text`` tags.
        Each line becomes a paragraph (row) in the post body.
        """
        lines = content.strip().split("\n")
        paragraphs: list[list[dict]] = []

        for line in lines:
            elements: list[dict] = []
            last_end = 0

            for m in cls._MD_LINK_RE.finditer(line):
                # Text before this link
                before = line[last_end : m.start()]
                if before:
                    elements.append({"tag": "text", "text": before})
                elements.append(
                    {
                        "tag": "a",
                        "text": m.group(1),
                        "href": m.group(2),
                    }
                )
                last_end = m.end()

            # Remaining text after last link
            remaining = line[last_end:]
            if remaining:
                elements.append({"tag": "text", "text": remaining})

            # Empty line → empty paragraph for spacing
            if not elements:
                elements.append({"tag": "text", "text": ""})

            paragraphs.append(elements)

        post_body = {
            "zh_cn": {
                "content": paragraphs,
            }
        }
        return json.dumps(post_body, ensure_ascii=False)

    _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".tiff", ".tif"}
    _AUDIO_EXTS = {".opus"}
    _VIDEO_EXTS = {".mp4", ".mov", ".avi"}
    _FILE_TYPE_MAP = {
        ".opus": "opus",
        ".mp4": "mp4",
        ".pdf": "pdf",
        ".doc": "doc",
        ".docx": "doc",
        ".xls": "xls",
        ".xlsx": "xls",
        ".ppt": "ppt",
        ".pptx": "ppt",
    }

    def _upload_image_sync(self, file_path: str) -> str | None:
        """Upload an image to Feishu and return the image_key."""
        from lark_oapi.api.im.v1 import CreateImageRequest, CreateImageRequestBody

        try:
            with open(file_path, "rb") as f:
                request = (
                    CreateImageRequest.builder()
                    .request_body(
                        CreateImageRequestBody.builder().image_type("message").image(f).build()
                    )
                    .build()
                )
                response = self._client.im.v1.image.create(request)
                if response.success():
                    image_key = response.data.image_key
                    logger.debug("Uploaded image {}: {}", os.path.basename(file_path), image_key)
                    return image_key
                else:
                    logger.error(
                        "Failed to upload image: code={}, msg={}", response.code, response.msg
                    )
                    return None
        except Exception as e:
            logger.error("Error uploading image {}: {}", file_path, e)
            return None

    def _upload_file_sync(self, file_path: str) -> str | None:
        """Upload a file to Feishu and return the file_key."""
        from lark_oapi.api.im.v1 import CreateFileRequest, CreateFileRequestBody

        ext = os.path.splitext(file_path)[1].lower()
        file_type = self._FILE_TYPE_MAP.get(ext, "stream")
        file_name = os.path.basename(file_path)
        try:
            with open(file_path, "rb") as f:
                request = (
                    CreateFileRequest.builder()
                    .request_body(
                        CreateFileRequestBody.builder()
                        .file_type(file_type)
                        .file_name(file_name)
                        .file(f)
                        .build()
                    )
                    .build()
                )
                response = self._client.im.v1.file.create(request)
                if response.success():
                    file_key = response.data.file_key
                    logger.debug("Uploaded file {}: {}", file_name, file_key)
                    return file_key
                else:
                    logger.error(
                        "Failed to upload file: code={}, msg={}", response.code, response.msg
                    )
                    return None
        except Exception as e:
            logger.error("Error uploading file {}: {}", file_path, e)
            return None

    def _download_image_sync(
        self, message_id: str, image_key: str
    ) -> tuple[bytes | None, str | None]:
        """Download an image from Feishu message by message_id and image_key."""
        from lark_oapi.api.im.v1 import GetMessageResourceRequest

        try:
            request = (
                GetMessageResourceRequest.builder()
                .message_id(message_id)
                .file_key(image_key)
                .type("image")
                .build()
            )
            response = self._client.im.v1.message_resource.get(request)
            if response.success():
                file_data = response.file
                # GetMessageResourceRequest returns BytesIO, need to read bytes
                if hasattr(file_data, "read"):
                    file_data = file_data.read()
                return file_data, response.file_name
            else:
                logger.error(
                    "Failed to download image: code={}, msg={}", response.code, response.msg
                )
                return None, None
        except Exception as e:
            logger.error("Error downloading image {}: {}", image_key, e)
            return None, None

    def _download_file_sync(
        self, message_id: str, file_key: str, resource_type: str = "file"
    ) -> tuple[bytes | None, str | None]:
        """Download a file/audio/media from a Feishu message by message_id and file_key."""
        from lark_oapi.api.im.v1 import GetMessageResourceRequest

        # Feishu resource download API only accepts 'image' or 'file' as type.
        # Both 'audio' and 'media' (video) messages use type='file' for download.
        if resource_type in ("audio", "media"):
            resource_type = "file"

        try:
            request = (
                GetMessageResourceRequest.builder()
                .message_id(message_id)
                .file_key(file_key)
                .type(resource_type)
                .build()
            )
            response = self._client.im.v1.message_resource.get(request)
            if response.success():
                file_data = response.file
                if hasattr(file_data, "read"):
                    file_data = file_data.read()
                return file_data, response.file_name
            else:
                logger.error(
                    "Failed to download {}: code={}, msg={}",
                    resource_type,
                    response.code,
                    response.msg,
                )
                return None, None
        except Exception:
            logger.exception("Error downloading {} {}", resource_type, file_key)
            return None, None

    async def _download_and_save_media(
        self, msg_type: str, content_json: dict, message_id: str | None = None
    ) -> tuple[str | None, str]:
        """
        Download media from Feishu and save to local disk.

        Returns:
            (file_path, content_text) - file_path is None if download failed
        """
        loop = asyncio.get_running_loop()
        media_dir = get_media_dir("feishu")

        data, filename = None, None

        if msg_type == "image":
            image_key = content_json.get("image_key")
            if image_key and message_id:
                data, filename = await loop.run_in_executor(
                    None, self._download_image_sync, message_id, image_key
                )
                if not filename:
                    filename = f"{image_key[:16]}.jpg"

        elif msg_type in ("audio", "file", "media"):
            file_key = content_json.get("file_key")
            if not file_key:
                logger.warning("Feishu {} message missing file_key: {}", msg_type, content_json)
                return None, f"[{msg_type}: missing file_key]"
            if not message_id:
                logger.warning("Feishu {} message missing message_id", msg_type)
                return None, f"[{msg_type}: missing message_id]"

            data, filename = await loop.run_in_executor(
                None, self._download_file_sync, message_id, file_key, msg_type
            )

            if not data:
                logger.warning("Feishu {} download failed: file_key={}", msg_type, file_key)
                return None, f"[{msg_type}: download failed]"

            if not filename:
                filename = file_key[:16]

            # Feishu voice messages are opus in OGG container.
            # Use .ogg extension for better Whisper compatibility.
            if msg_type == "audio":
                if not any(filename.endswith(ext) for ext in (".opus", ".ogg", ".oga")):
                    filename = f"{filename}.ogg"

        if data and filename:
            file_path = media_dir / filename
            file_path.write_bytes(data)
            logger.debug("Downloaded {} to {}", msg_type, file_path)
            return str(file_path), f"[{msg_type}: {filename}]"

        return None, f"[{msg_type}: download failed]"

    _REPLY_CONTEXT_MAX_LEN = 200

    def _get_message_content_sync(self, message_id: str) -> str | None:
        """Fetch the text content of a Feishu message by ID (synchronous).

        Returns a "[Reply to: ...]" context string, or None on failure.
        """
        from lark_oapi.api.im.v1 import GetMessageRequest

        try:
            request = GetMessageRequest.builder().message_id(message_id).build()
            response = self._client.im.v1.message.get(request)
            if not response.success():
                logger.debug(
                    "Feishu: could not fetch parent message {}: code={}, msg={}",
                    message_id,
                    response.code,
                    response.msg,
                )
                return None
            items = getattr(response.data, "items", None)
            if not items:
                return None
            msg_obj = items[0]
            raw_content = getattr(msg_obj, "body", None)
            raw_content = getattr(raw_content, "content", None) if raw_content else None
            if not raw_content:
                return None
            try:
                content_json = json.loads(raw_content)
            except (json.JSONDecodeError, TypeError):
                return None
            msg_type = getattr(msg_obj, "msg_type", "")
            if msg_type == "text":
                text = content_json.get("text", "").strip()
            elif msg_type == "post":
                text, _ = _extract_post_content(content_json)
                text = text.strip()
            else:
                text = ""
            if not text:
                return None
            if len(text) > self._REPLY_CONTEXT_MAX_LEN:
                text = text[: self._REPLY_CONTEXT_MAX_LEN] + "..."
            return f"[Reply to: {text}]"
        except Exception as e:
            logger.debug("Feishu: error fetching parent message {}: {}", message_id, e)
            return None

    def _reply_message_sync(self, parent_message_id: str, msg_type: str, content: str, *, reply_in_thread: bool = False) -> bool:
        """Reply to an existing Feishu message using the Reply API (synchronous).

        Args:
            reply_in_thread: If True, reply as a thread/topic message
                in the Feishu client.
        """
        from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody

        try:
            body_builder = ReplyMessageRequestBody.builder().msg_type(msg_type).content(content)
            if reply_in_thread:
                body_builder = body_builder.reply_in_thread(True)
            request = (
                ReplyMessageRequest.builder()
                .message_id(parent_message_id)
                .request_body(body_builder.build())
                .build()
            )
            response = self._client.im.v1.message.reply(request)
            if not response.success():
                logger.error(
                    "Failed to reply to Feishu message {}: code={}, msg={}, log_id={}",
                    parent_message_id,
                    response.code,
                    response.msg,
                    response.get_log_id(),
                )
                return False
            logger.debug("Feishu reply sent to message {}", parent_message_id)
            return True
        except Exception as e:
            logger.error("Error replying to Feishu message {}: {}", parent_message_id, e)
            return False

    def _should_use_reply_in_thread(self, metadata: dict[str, Any]) -> bool:
        """Return whether a group reply should create a Feishu thread/topic."""
        return metadata.get("chat_type", "group") == "group" and self.config.reply_to_message

    def _thread_reply_target(self, metadata: dict[str, Any]) -> str | None:
        """Return the message_id that should receive a Reply API response."""
        if metadata.get("chat_type", "group") != "group":
            return None
        message_id = metadata.get("message_id")
        if not message_id:
            return None
        if metadata.get("thread_id") or self.config.reply_to_message:
            return message_id
        return None

    def _send_message_sync(
        self, receive_id_type: str, receive_id: str, msg_type: str, content: str
    ) -> str | None:
        """Send a single message and return the message_id on success."""
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        try:
            request = (
                CreateMessageRequest.builder()
                .receive_id_type(receive_id_type)
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(receive_id)
                    .msg_type(msg_type)
                    .content(content)
                    .build()
                )
                .build()
            )
            response = self._client.im.v1.message.create(request)
            if not response.success():
                logger.error(
                    "Failed to send Feishu {} message: code={}, msg={}, log_id={}",
                    msg_type,
                    response.code,
                    response.msg,
                    response.get_log_id(),
                )
                return None
            msg_id = getattr(response.data, "message_id", None)
            logger.debug("Feishu {} message sent to {}: {}", msg_type, receive_id, msg_id)
            return msg_id
        except Exception as e:
            logger.error("Error sending Feishu {} message: {}", msg_type, e)
            return None

    def _create_streaming_card_sync(
        self,
        receive_id_type: str,
        chat_id: str,
        reply_message_id: str | None = None,
        *,
        reply_in_thread: bool = False,
        tool_logs: str | None = None,
        token_usage: dict[str, int] | None = None,
    ) -> str | None:
        """Create a CardKit streaming card, send it to chat, return card_id.

        When *reply_message_id* is provided the card is delivered via the
        reply API. *reply_in_thread* controls whether Feishu creates a
        thread/topic for that reply. Otherwise the plain create-message API is
        used.
        """
        from lark_oapi.api.cardkit.v1 import CreateCardRequest, CreateCardRequestBody

        def _card_payload(include_observability: bool, include_chart: bool) -> dict[str, Any]:
            elements: list[dict[str, Any]] = []
            if include_observability:
                elements.append({
                    "tag": "collapsible_panel",
                    "element_id": "tool_logs_panel",
                    "expanded": False,
                    "vertical_spacing": "8px",
                    "padding": "8px 8px 8px 8px",
                    "background_color": "grey",
                    "header": {
                        "title": {
                            "tag": "markdown",
                            "content": "**工具调用详情**",
                        },
                        "background_color": "grey",
                        "icon": {
                            "tag": "standard_icon",
                            "token": "down-small-ccm_outlined",
                            "size": "16px 16px",
                        },
                        "icon_position": "right",
                        "icon_expanded_angle": -180,
                    },
                    "border": {
                        "color": "grey",
                        "corner_radius": "5px",
                    },
                    "elements": [
                        {
                            "tag": "markdown",
                            "content": tool_logs or self._format_observability_markdown([], {}),
                            "element_id": _TOOL_LOGS_ELEMENT_ID,
                            "text_size": _TOOL_LOGS_TEXT_SIZE,
                        }
                    ],
                })
            elements.append({"tag": "markdown", "content": "", "element_id": _STREAM_ELEMENT_ID})
            if include_chart:
                elements.extend([
                    {"tag": "hr", "margin": "0px 0px 0px 0px"},
                    {
                        "tag": "chart",
                        "chart_spec": self._token_chart_spec(token_usage or {}),
                        "element_id": _TOKEN_CHART_ELEMENT_ID,
                        "color_theme": "complementary",
                        "height": _TOKEN_CHART_HEIGHT,
                        "margin": "0px 0px 0px 0px",
                    },
                ])
            return {
                "schema": "2.0",
                "config": {"wide_screen_mode": True, "update_multi": True, "streaming_mode": True},
                "body": {"elements": elements},
            }

        card_json = _card_payload(include_observability=True, include_chart=True)

        def _create_card(payload: dict[str, Any]) -> str | None:
            request = (
                CreateCardRequest.builder()
                .request_body(
                    CreateCardRequestBody.builder()
                    .type("card_json")
                    .data(json.dumps(payload, ensure_ascii=False))
                    .build()
                )
                .build()
            )
            response = self._client.cardkit.v1.card.create(request)
            if not response.success():
                logger.warning(
                    "Failed to create streaming card: code={}, msg={}", response.code, response.msg
                )
                return None
            return getattr(response.data, "card_id", None)

        try:
            card_id = _create_card(card_json)
            if not card_id:
                logger.warning("Retrying streaming card creation without token chart")
                card_id = _create_card(_card_payload(include_observability=True, include_chart=False))
            if not card_id:
                logger.warning("Retrying streaming card creation without observability panel")
                card_id = _create_card(_card_payload(include_observability=False, include_chart=False))
            if card_id:
                card_content = json.dumps(
                    {"type": "card", "data": {"card_id": card_id}}, ensure_ascii=False
                )
                if reply_message_id:
                    sent = self._reply_message_sync(
                        reply_message_id, "interactive", card_content,
                        reply_in_thread=reply_in_thread,
                    )
                else:
                    sent = self._send_message_sync(
                        receive_id_type, chat_id, "interactive", card_content,
                    ) is not None
                if sent:
                    return card_id
                logger.warning(
                    "Created streaming card {} but failed to send it to {}", card_id, chat_id
                )
            return None
        except Exception as e:
            logger.warning("Error creating streaming card: {}", e)
            return None

    def _stream_update_text_sync(
        self,
        card_id: str,
        content: str,
        sequence: int,
        element_id: str = _STREAM_ELEMENT_ID,
    ) -> bool:
        """Stream-update the markdown element on a CardKit card (typewriter effect)."""
        from lark_oapi.api.cardkit.v1 import (
            ContentCardElementRequest,
            ContentCardElementRequestBody,
        )

        try:
            content = self._normalize_card_markdown(content)
            request = (
                ContentCardElementRequest.builder()
                .card_id(card_id)
                .element_id(element_id)
                .request_body(
                    ContentCardElementRequestBody.builder()
                    .content(content)
                    .sequence(sequence)
                    .build()
                )
                .build()
            )
            response = self._client.cardkit.v1.card_element.content(request)
            if not response.success():
                logger.warning(
                    "Failed to stream-update card {}: code={}, msg={}",
                    card_id,
                    response.code,
                    response.msg,
                )
                return False
            return True
        except Exception as e:
            logger.warning("Error stream-updating card {}: {}", card_id, e)
            return False

    def _close_streaming_mode_sync(self, card_id: str, sequence: int) -> bool:
        """Turn off CardKit streaming_mode so the chat list preview exits the streaming placeholder.

        Per Feishu docs, streaming cards keep a generating-style summary in the session list until
        streaming_mode is set to false via card settings (after final content update).
        Sequence must strictly exceed the previous card OpenAPI operation on this entity.
        """
        from lark_oapi.api.cardkit.v1 import SettingsCardRequest, SettingsCardRequestBody

        settings_payload = json.dumps({"config": {"streaming_mode": False}}, ensure_ascii=False)
        try:
            request = (
                SettingsCardRequest.builder()
                .card_id(card_id)
                .request_body(
                    SettingsCardRequestBody.builder()
                    .settings(settings_payload)
                    .sequence(sequence)
                    .uuid(str(uuid.uuid4()))
                    .build()
                )
                .build()
            )
            response = self._client.cardkit.v1.card.settings(request)
            if not response.success():
                logger.warning(
                    "Failed to close streaming on card {}: code={}, msg={}",
                    card_id,
                    response.code,
                    response.msg,
                )
                return False
            return True
        except Exception as e:
            logger.warning("Error closing streaming on card {}: {}", card_id, e)
            return False

    @staticmethod
    def _safe_int(value: Any) -> int:
        try:
            if value is None or isinstance(value, bool):
                return 0
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _tool_display_name(cls, raw_name: str) -> str:
        label = _TOOL_DISPLAY_NAMES.get(raw_name)
        if label:
            return f"{label} (`{raw_name}`)"
        return f"`{raw_name or 'tool'}`"

    @staticmethod
    def _truncate_inline(value: Any, max_chars: int = 220) -> str:
        if isinstance(value, str):
            text = value
        else:
            try:
                text = json.dumps(value, ensure_ascii=False)
            except TypeError:
                text = str(value)
        text = " ".join(str(text).split())
        return text if len(text) <= max_chars else text[:max_chars] + "...（已截断）"

    @classmethod
    def _format_json_preview(cls, value: Any, max_chars: int = 900) -> str:
        if value in (None, "", {}, []):
            return ""
        if isinstance(value, str):
            text = value
        else:
            try:
                text = json.dumps(value, ensure_ascii=False, indent=2)
            except TypeError:
                text = str(value)
        if len(text) > max_chars:
            text = text[:max_chars] + "\n...（已截断）"
        return text.replace("```", "'''")

    @classmethod
    def _merge_tool_events(cls, tool_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        merged: OrderedDict[str, dict[str, Any]] = OrderedDict()
        for index, event in enumerate(tool_events[-50:]):
            if not isinstance(event, dict) or event.get("phase") == "usage":
                continue
            call_id = str(event.get("call_id") or "")
            name = str(event.get("name") or "tool")
            key = call_id or f"{name}:{index}"
            item = merged.setdefault(
                key,
                {
                    "name": name,
                    "call_id": call_id,
                    "phase": "start",
                    "arguments": {},
                    "result": None,
                    "error": None,
                    "files": [],
                    "embeds": [],
                },
            )
            if name and item.get("name") == "tool":
                item["name"] = name
            if event.get("arguments"):
                item["arguments"] = event.get("arguments")
            phase = str(event.get("phase") or event.get("status") or "").lower()
            if phase:
                item["phase"] = phase
            if event.get("result") not in (None, "", {}, []):
                item["result"] = event.get("result")
            if event.get("error"):
                item["error"] = event.get("error")
                item["phase"] = "error"
            for key_name in ("files", "embeds"):
                values = event.get(key_name)
                if isinstance(values, list) and values:
                    item[key_name] = values
        return list(merged.values())

    @classmethod
    def _format_observability_markdown(
        cls,
        tool_events: list[dict[str, Any]],
        token_usage: dict[str, int],
    ) -> str:
        merged = cls._merge_tool_events(tool_events)
        if not merged and not token_usage:
            return _EMPTY_TOOL_LOGS_MARKDOWN

        lines: list[str] = ["**工具调用记录**"]
        if merged:
            for idx, item in enumerate(merged[-20:], start=1):
                phase = str(item.get("phase") or "start").lower()
                status = _TOOL_PHASE_LABELS.get(phase, phase or "已更新")
                name = str(item.get("name") or "tool")
                lines.append(f"{idx}. **{cls._tool_display_name(name)}** - {status}")
                args_preview = cls._format_json_preview(item.get("arguments"), max_chars=700)
                if args_preview:
                    lines.append("   - 参数:")
                    lines.append(f"```json\n{args_preview}\n```\n")
                if item.get("error"):
                    lines.append(f"   - 错误: `{cls._truncate_inline(item['error'], 260)}`")
                elif item.get("result") not in (None, "", {}, []):
                    result_preview = cls._format_json_preview(item["result"], max_chars=900)
                    result_lang = "" if isinstance(item["result"], str) else "json"
                    lines.append("   - 结果:")
                    lines.append(f"```{result_lang}\n{result_preview}\n```\n")
                files = item.get("files") if isinstance(item.get("files"), list) else []
                embeds = item.get("embeds") if isinstance(item.get("embeds"), list) else []
                if files or embeds:
                    lines.append(f"   - 附件: {len(files)} 个文件，{len(embeds)} 个嵌入结果")
        else:
            lines.append("暂无工具调用。")

        return "\n".join(lines)

    @classmethod
    def _token_chart_spec(cls, token_usage: dict[str, int]) -> dict[str, Any]:
        prompt = cls._safe_int(token_usage.get("prompt_tokens"))
        completion = cls._safe_int(token_usage.get("completion_tokens"))
        total = cls._safe_int(token_usage.get("total_tokens"))
        cached = max(
            cls._safe_int(token_usage.get("cached_tokens")),
            cls._safe_int(token_usage.get("cache_tokens")),
        )
        input_tokens = max(prompt, max(0, total - completion))
        cached = min(cached, input_tokens)
        uncached = max(0, input_tokens - cached)
        values = [
            {"category": "token用量", "item": "缓存输入", "value": cached},
            {"category": "token用量", "item": "非缓存输入", "value": uncached},
            {"category": "token用量", "item": "输出", "value": completion},
        ]
        return {
            "type": "bar",
            "direction": "horizontal",
            "title": {"text": "token用量占比图"},
            "data": {"values": values},
            "xField": "value",
            "yField": "category",
            "seriesField": "item",
            "stack": True,
            "legends": {"visible": True, "orient": "bottom"},
            "label": {"visible": True, "formatter": "value"},
        }

    def _patch_card_element_sync(
        self,
        card_id: str,
        element_id: str,
        partial_element: dict[str, Any],
        sequence: int,
    ) -> bool:
        """Patch a CardKit element property set, used for token chart refresh."""
        from lark_oapi.api.cardkit.v1 import PatchCardElementRequest, PatchCardElementRequestBody

        try:
            request = (
                PatchCardElementRequest.builder()
                .card_id(card_id)
                .element_id(element_id)
                .request_body(
                    PatchCardElementRequestBody.builder()
                    .partial_element(json.dumps(partial_element, ensure_ascii=False))
                    .sequence(sequence)
                    .uuid(str(uuid.uuid4()))
                    .build()
                )
                .build()
            )
            response = self._client.cardkit.v1.card_element.patch(request)
            if not response.success():
                logger.debug(
                    "Failed to patch card {} element {}: code={}, msg={}",
                    card_id,
                    element_id,
                    response.code,
                    response.msg,
                )
                return False
            return True
        except Exception as e:
            logger.debug("Error patching card {} element {}: {}", card_id, element_id, e)
            return False

    @staticmethod
    def _apply_tool_events(buf: _FeishuStreamBuf, events: list[dict[str, Any]]) -> bool:
        changed = False
        for event in events:
            if not isinstance(event, dict):
                continue
            if event.get("phase") == "usage" and isinstance(event.get("usage"), dict):
                cached_values: list[int] = []
                for key, value in event["usage"].items():
                    try:
                        parsed = int(value or 0)
                    except (TypeError, ValueError):
                        continue
                    if parsed < 0:
                        continue
                    if key in {"cached_tokens", "cache_tokens"}:
                        cached_values.append(parsed)
                        continue
                    buf.token_usage[key] = buf.token_usage.get(key, 0) + parsed
                if cached_values:
                    buf.token_usage["cached_tokens"] = (
                        buf.token_usage.get("cached_tokens", 0) + max(cached_values)
                    )
                changed = True
                continue
            if event.get("name") or event.get("phase"):
                buf.tool_events.append(dict(event))
                buf.tool_events = buf.tool_events[-50:]
                changed = True
        return changed

    async def _flush_observability(self, buf: _FeishuStreamBuf) -> None:
        if not buf.card_id:
            return
        text = self._format_observability_markdown(buf.tool_events, buf.token_usage)
        buf.sequence += 1
        loop = asyncio.get_running_loop()
        ok = await loop.run_in_executor(
            None,
            self._stream_update_text_sync,
            buf.card_id,
            text,
            buf.sequence,
            _TOOL_LOGS_ELEMENT_ID,
        )
        if not ok:
            logger.debug("Feishu observability panel update failed; continuing text stream")
        if buf.token_usage:
            buf.sequence += 1
            await loop.run_in_executor(
                None,
                self._patch_card_element_sync,
                buf.card_id,
                _TOKEN_CHART_ELEMENT_ID,
                {"chart_spec": self._token_chart_spec(buf.token_usage)},
                buf.sequence,
            )

    async def send_delta(
        self, chat_id: str, delta: str, metadata: dict[str, Any] | None = None
    ) -> None:
        """Progressive streaming via CardKit: create card on first delta, stream-update on subsequent.

        Supported metadata keys:
            _stream_end: Finalize the streaming card.
            _tool_hint:  Delta is a formatted tool hint (for display only).
            message_id:  Original message id (used with _stream_end for reaction cleanup).
            chat_type:   "group" or "p2p" — controls reply-in-thread for streaming cards.
        """
        if not self._client:
            return
        meta = metadata or {}
        stream_key = self._stream_key(chat_id, meta)
        loop = asyncio.get_running_loop()
        rid_type = "chat_id" if chat_id.startswith("oc_") else "open_id"

        if isinstance(meta.get("_tool_events"), list):
            buf = self._stream_bufs.get(stream_key)
            if buf is None:
                buf = _FeishuStreamBuf()
                self._stream_bufs[stream_key] = buf
            if self._apply_tool_events(buf, meta["_tool_events"]) and buf.card_id:
                await self._flush_observability(buf)

        # --- stream end: final update or fallback ---
        if meta.get("_stream_end"):
            message_id = meta.get("message_id")
            # Only finalize the OnIt -> DONE reaction transition on the truly
            # final stream end. _resuming=True means the agent will keep
            # working (more tool-call rounds), so leave the reaction state
            # in place — otherwise the OnIt indicator disappears prematurely
            # and the DONE reaction fires after every tool call.
            if message_id and not meta.get("_resuming"):
                reaction_id = self._reaction_ids.pop(message_id, None)
                if reaction_id:
                    await self._remove_reaction(message_id, reaction_id)
                # Add completion emoji if configured
                if self.config.done_emoji:
                    await self._add_reaction(message_id, self.config.done_emoji)

            if meta.get("_resuming"):
                buf = self._stream_bufs.get(stream_key)
                if not buf:
                    return
                if buf.text.strip():
                    buf.needs_segment_separator = True
                if buf.card_id and buf.text:
                    buf.sequence += 1
                    ok = await loop.run_in_executor(
                        None,
                        self._stream_update_text_sync,
                        buf.card_id,
                        buf.text,
                        buf.sequence,
                    )
                    if ok:
                        buf.last_edit = time.monotonic()
                    else:
                        logger.debug(
                            "Feishu resuming stream update failed; keeping buffer for final fallback"
                        )
                return

            buf = self._stream_bufs.pop(stream_key, None)
            if not buf:
                return
            # Try to finalize via streaming card; if that fails (e.g.
            # streaming mode was closed by Feishu due to timeout), fall
            # back to sending a regular interactive card.
            if buf.card_id:
                ok = True
                if buf.text:
                    buf.sequence += 1
                    ok = await loop.run_in_executor(
                        None,
                        self._stream_update_text_sync,
                        buf.card_id,
                        buf.text,
                        buf.sequence,
                    )
                if ok:
                    buf.sequence += 1
                    await loop.run_in_executor(
                        None,
                        self._close_streaming_mode_sync,
                        buf.card_id,
                        buf.sequence,
                    )
                    return
                logger.warning(
                    "Streaming card {} final update failed, falling back to regular card",
                    buf.card_id,
                )
            if not buf.text:
                return
            fallback_elements = self._build_card_elements(buf.text)
            obs = self._format_observability_markdown(buf.tool_events, buf.token_usage)
            if obs != _EMPTY_TOOL_LOGS_MARKDOWN:
                fallback_elements.insert(0, {
                    "tag": "collapsible_panel",
                    "expanded": False,
                    "background_color": "grey",
                    "header": {
                        "title": {"tag": "markdown", "content": "**工具调用详情**"},
                        "background_color": "grey",
                    },
                    "border": {"color": "grey", "corner_radius": "5px"},
                    "elements": [{
                        "tag": "markdown",
                        "content": obs,
                        "text_size": _TOOL_LOGS_TEXT_SIZE,
                    }],
                })
            if buf.token_usage:
                fallback_elements.append({"tag": "hr", "margin": "0px 0px 0px 0px"})
                fallback_elements.append({
                    "tag": "chart",
                    "chart_spec": self._token_chart_spec(buf.token_usage),
                    "element_id": _TOKEN_CHART_ELEMENT_ID,
                    "color_theme": "complementary",
                    "height": _TOKEN_CHART_HEIGHT,
                    "margin": "0px 0px 0px 0px",
                })
            for chunk in self._split_elements_by_table_limit(fallback_elements):
                card = json.dumps(
                    {"config": {"wide_screen_mode": True}, "elements": chunk},
                    ensure_ascii=False,
                )
                # Fallback replies stay in existing topics, but only create a
                # new topic when reply-to-message is enabled.
                fallback_msg_id = self._thread_reply_target(meta)
                if fallback_msg_id:
                    await loop.run_in_executor(
                        None, lambda: self._reply_message_sync(
                            fallback_msg_id, "interactive", card,
                            reply_in_thread=self._should_use_reply_in_thread(meta),
                        ),
                    )
                else:
                    await loop.run_in_executor(
                        None, self._send_message_sync, rid_type, chat_id, "interactive", card
                    )
            return

        # --- accumulate delta ---
        buf = self._stream_bufs.get(stream_key)
        if buf is None:
            buf = _FeishuStreamBuf()
            self._stream_bufs[stream_key] = buf
        if buf.needs_segment_separator and delta.strip():
            buf.text = buf.text.rstrip() + _STREAM_SEGMENT_SEPARATOR
            delta = delta.lstrip()
            buf.needs_segment_separator = False
        buf.text += delta
        if not buf.text.strip():
            return

        now = time.monotonic()
        if buf.card_id is None:
            # Use the Reply API for existing topics, and only create new topics
            # when reply-to-message is enabled.
            use_reply_in_thread = self._should_use_reply_in_thread(meta)
            reply_msg_id = self._thread_reply_target(meta)
            card_id = await loop.run_in_executor(
                None,
                lambda: self._create_streaming_card_sync(
                    rid_type,
                    chat_id,
                    reply_msg_id,
                    reply_in_thread=use_reply_in_thread,
                    tool_logs=self._format_observability_markdown(
                        buf.tool_events,
                        buf.token_usage,
                    ),
                    token_usage=buf.token_usage,
                ),
            )
            if card_id:
                buf.card_id = card_id
                buf.sequence = 1
                await loop.run_in_executor(
                    None, self._stream_update_text_sync, card_id, buf.text, 1
                )
                buf.last_edit = now
        elif (now - buf.last_edit) >= self._STREAM_EDIT_INTERVAL:
            buf.sequence += 1
            await loop.run_in_executor(
                None, self._stream_update_text_sync, buf.card_id, buf.text, buf.sequence
            )
            buf.last_edit = now

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through Feishu, including media (images/files) if present."""
        if not self._client:
            logger.warning("Feishu client not initialized")
            return

        try:
            receive_id_type = "chat_id" if msg.chat_id.startswith("oc_") else "open_id"
            loop = asyncio.get_running_loop()

            if isinstance(msg.metadata.get("_tool_events"), list):
                buf = self._stream_bufs.get(self._stream_key(msg.chat_id, msg.metadata))
                if buf is None:
                    buf = _FeishuStreamBuf()
                    self._stream_bufs[self._stream_key(msg.chat_id, msg.metadata)] = buf
                changed = self._apply_tool_events(buf, msg.metadata["_tool_events"])
                if changed and buf.card_id:
                    await self._flush_observability(buf)
                if not msg.content and not msg.metadata.get("_tool_hint"):
                    return

            # Handle tool hint messages.  When a streaming card is active for
            # this chat, the structured tool panel already carries the visible
            # progress; keep the main answer area free of raw function-call text.
            if msg.metadata.get("_tool_hint"):
                hint = (msg.content or "").strip()
                if not hint:
                    return
                buf = self._stream_bufs.get(self._stream_key(msg.chat_id, msg.metadata))
                if msg.metadata.get("_wants_stream"):
                    if buf is None:
                        buf = _FeishuStreamBuf()
                        self._stream_bufs[self._stream_key(msg.chat_id, msg.metadata)] = buf
                    if not msg.metadata.get("_tool_events"):
                        buf.tool_events.append({
                            "version": 1,
                            "phase": "start",
                            "name": "tool",
                            "arguments": {"调用": hint},
                            "result": None,
                            "error": None,
                            "files": [],
                            "embeds": [],
                        })
                        buf.tool_events = buf.tool_events[-50:]
                    if buf.card_id:
                        await self._flush_observability(buf)
                    return
                if buf and buf.card_id:
                    if not msg.metadata.get("_tool_events"):
                        buf.tool_events.append({
                            "version": 1,
                            "phase": "start",
                            "name": "tool",
                            "arguments": {"调用": hint},
                            "result": None,
                            "error": None,
                            "files": [],
                            "embeds": [],
                        })
                        buf.tool_events = buf.tool_events[-50:]
                        await self._flush_observability(buf)
                    return
                # Non-streaming fallback: use a small structured card instead
                # of a raw English function-call line.
                card = json.dumps(
                    {"config": {"wide_screen_mode": True}, "elements": [
                        {
                            "tag": "collapsible_panel",
                            "expanded": True,
                            "background_color": "grey",
                            "header": {
                                "title": {"tag": "markdown", "content": "**工具调用详情**"},
                                "background_color": "grey",
                            },
                            "border": {"color": "grey", "corner_radius": "5px"},
                            "elements": [{
                                "tag": "markdown",
                                "content": f"**工具调用记录**\n\n- `{hint}`",
                                "text_size": _TOOL_LOGS_TEXT_SIZE,
                            }],
                        },
                    ]},
                    ensure_ascii=False,
                )
                _th_msg_id = self._thread_reply_target(msg.metadata)
                if _th_msg_id:
                    await loop.run_in_executor(
                        None, lambda: self._reply_message_sync(
                            _th_msg_id, "interactive", card,
                            reply_in_thread=self._should_use_reply_in_thread(msg.metadata),
                        ),
                    )
                else:
                    await loop.run_in_executor(
                        None, self._send_message_sync, receive_id_type, msg.chat_id, "interactive", card
                    )
                return

            # Determine whether the first message should quote the user's message.
            # Only the very first send (media or text) in this call uses reply; subsequent
            # chunks/media fall back to plain create to avoid redundant quote bubbles.
            # Always target message_id — the Feishu Reply API keeps replies in the
            # same topic automatically when the target message is inside a topic.
            reply_message_id: str | None = None
            _msg_id = msg.metadata.get("message_id")
            if self.config.reply_to_message and not msg.metadata.get("_progress", False):
                reply_message_id = _msg_id
            # For topic group messages, always reply to keep context in thread
            elif msg.metadata.get("thread_id"):
                reply_message_id = _msg_id

            final_buf = None
            if (
                not msg.metadata.get("_progress")
                and not msg.metadata.get("_tool_hint")
                and not msg.metadata.get("_stream_delta")
                and not msg.metadata.get("_stream_end")
                and not msg.metadata.get("_streamed")
            ):
                final_buf = self._stream_bufs.pop(self._stream_key(msg.chat_id, msg.metadata), None)

            first_send = True  # tracks whether the reply has already been used

            def _do_send(m_type: str, content: str) -> None:
                """Send via reply (first message) or create (subsequent).

                Group chats only set reply_in_thread=True when
                reply_to_message is enabled; otherwise a Reply API call for an
                existing topic must not create a new topic.
                """
                nonlocal first_send
                if reply_message_id and first_send:
                    first_send = False
                    ok = self._reply_message_sync(
                        reply_message_id, m_type, content,
                        reply_in_thread=self._should_use_reply_in_thread(msg.metadata),
                    )
                    if ok:
                        return
                    # Fall back to regular send if reply fails
                self._send_message_sync(receive_id_type, msg.chat_id, m_type, content)

            for file_path in msg.media:
                if not os.path.isfile(file_path):
                    logger.warning("Media file not found: {}", file_path)
                    continue
                ext = os.path.splitext(file_path)[1].lower()
                if ext in self._IMAGE_EXTS:
                    key = await loop.run_in_executor(None, self._upload_image_sync, file_path)
                    if key:
                        await loop.run_in_executor(
                            None,
                            _do_send,
                            "image",
                            json.dumps({"image_key": key}, ensure_ascii=False),
                        )
                else:
                    key = await loop.run_in_executor(None, self._upload_file_sync, file_path)
                    if key:
                        # Feishu's OpenAPI names video messages "media".
                        # Use "audio" for audio, "media" for video, "file" for documents.
                        # Feishu requires these specific msg_types for inline playback.
                        if ext in self._AUDIO_EXTS:
                            media_type = "audio"
                        elif ext in self._VIDEO_EXTS:
                            media_type = "media"
                        else:
                            media_type = "file"
                        await loop.run_in_executor(
                            None,
                            _do_send,
                            media_type,
                            json.dumps({"file_key": key}, ensure_ascii=False),
                        )

            if msg.content and msg.content.strip():
                if final_buf and (final_buf.tool_events or final_buf.token_usage):
                    elements = self._build_card_elements(msg.content)
                    obs = self._format_observability_markdown(
                        final_buf.tool_events,
                        final_buf.token_usage,
                    )
                    if obs != _EMPTY_TOOL_LOGS_MARKDOWN:
                        elements.insert(0, {
                            "tag": "collapsible_panel",
                            "expanded": False,
                            "background_color": "grey",
                            "header": {
                                "title": {"tag": "markdown", "content": "**工具调用详情**"},
                                "background_color": "grey",
                            },
                            "border": {"color": "grey", "corner_radius": "5px"},
                            "elements": [{
                                "tag": "markdown",
                                "content": obs,
                                "text_size": _TOOL_LOGS_TEXT_SIZE,
                            }],
                        })
                    if final_buf.token_usage:
                        elements.append({"tag": "hr", "margin": "0px 0px 0px 0px"})
                        elements.append({
                            "tag": "chart",
                            "chart_spec": self._token_chart_spec(final_buf.token_usage),
                            "element_id": _TOKEN_CHART_ELEMENT_ID,
                            "color_theme": "complementary",
                            "height": _TOKEN_CHART_HEIGHT,
                            "margin": "0px 0px 0px 0px",
                        })
                    for chunk in self._split_elements_by_table_limit(elements):
                        card = {"config": {"wide_screen_mode": True}, "elements": chunk}
                        await loop.run_in_executor(
                            None,
                            _do_send,
                            "interactive",
                            json.dumps(card, ensure_ascii=False),
                        )
                    return

                fmt = self._detect_msg_format(msg.content)

                if fmt == "text":
                    # Short plain text – send as simple text message
                    text_body = json.dumps({"text": msg.content.strip()}, ensure_ascii=False)
                    await loop.run_in_executor(None, _do_send, "text", text_body)

                elif fmt == "post":
                    # Medium content with links – send as rich-text post
                    post_body = self._markdown_to_post(msg.content)
                    await loop.run_in_executor(None, _do_send, "post", post_body)

                else:
                    # Complex / long content – send as interactive card
                    elements = self._build_card_elements(msg.content)
                    for chunk in self._split_elements_by_table_limit(elements):
                        card = {"config": {"wide_screen_mode": True}, "elements": chunk}
                        await loop.run_in_executor(
                            None,
                            _do_send,
                            "interactive",
                            json.dumps(card, ensure_ascii=False),
                        )

        except Exception as e:
            logger.error("Error sending Feishu message: {}", e)
            raise

    def _on_message_sync(self, data: Any) -> None:
        """
        Sync handler for incoming messages (called from WebSocket thread).
        Schedules async handling in the main event loop.
        """
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._on_message(data), self._loop)

    async def _on_message(self, data: P2ImMessageReceiveV1) -> None:
        """Handle incoming message from Feishu."""
        try:
            event = data.event
            message = event.message
            sender = event.sender

            logger.debug("Feishu raw message: {}", message.content)
            logger.debug("Feishu mentions: {}", getattr(message, "mentions", None))

            # Deduplication check
            message_id = message.message_id
            if message_id in self._processed_message_ids:
                return
            self._processed_message_ids[message_id] = None

            # Trim cache
            while len(self._processed_message_ids) > 1000:
                self._processed_message_ids.popitem(last=False)

            # Skip bot messages
            if sender.sender_type == "bot":
                return

            sender_id = sender.sender_id.open_id if sender.sender_id else "unknown"
            chat_id = message.chat_id
            chat_type = message.chat_type
            msg_type = message.message_type

            if chat_type == "group" and not self._is_group_message_for_bot(message):
                logger.debug("Feishu: skipping group message (not mentioned)")
                return

            # Add reaction (non-blocking — tracked background task)
            task = asyncio.create_task(
                self._add_reaction(message_id, self.config.react_emoji)
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._on_background_task_done)
            task.add_done_callback(lambda t: self._on_reaction_added(message_id, t))

            # Parse content
            content_parts = []
            media_paths = []

            try:
                content_json = json.loads(message.content) if message.content else {}
            except json.JSONDecodeError:
                content_json = {}

            if msg_type == "text":
                text = content_json.get("text", "")
                if text:
                    mentions = getattr(message, "mentions", None)
                    text = self._resolve_mentions(text, mentions)
                    content_parts.append(text)

            elif msg_type == "post":
                text, image_keys = _extract_post_content(content_json)
                if text:
                    content_parts.append(text)
                # Download images embedded in post
                for img_key in image_keys:
                    file_path, content_text = await self._download_and_save_media(
                        "image", {"image_key": img_key}, message_id
                    )
                    if file_path:
                        media_paths.append(file_path)
                    content_parts.append(content_text)

            elif msg_type in ("image", "audio", "file", "media"):
                file_path, content_text = await self._download_and_save_media(
                    msg_type, content_json, message_id
                )
                if file_path:
                    media_paths.append(file_path)

                if msg_type == "audio" and file_path:
                    transcription = await self.transcribe_audio(file_path)
                    if transcription:
                        content_text = f"[transcription: {transcription}]"

                content_parts.append(content_text)

            elif msg_type in (
                "share_chat",
                "share_user",
                "interactive",
                "share_calendar_event",
                "system",
                "merge_forward",
            ):
                # Handle share cards and interactive messages
                text = _extract_share_card_content(content_json, msg_type)
                if text:
                    content_parts.append(text)

            else:
                content_parts.append(MSG_TYPE_MAP.get(msg_type, f"[{msg_type}]"))

            # Extract reply context (parent/root message IDs)
            parent_id = getattr(message, "parent_id", None) or None
            root_id = getattr(message, "root_id", None) or None
            thread_id = getattr(message, "thread_id", None) or None

            # Prepend quoted message text when the user replied to another message
            if parent_id and self._client:
                loop = asyncio.get_running_loop()
                reply_ctx = await loop.run_in_executor(
                    None, self._get_message_content_sync, parent_id
                )
                if reply_ctx:
                    content_parts.insert(0, reply_ctx)

            content = "\n".join(content_parts) if content_parts else ""

            if not content and not media_paths:
                return

            # Build topic-scoped session key for conversation isolation.
            # Group chat: each topic gets its own session via root_id (replies
            # inside a topic) or message_id (top-level messages start a new topic).
            # Private chat: no override — same behavior as Telegram/Slack.
            if chat_type == "group":
                session_key = f"feishu:{chat_id}:{root_id or message_id}"
            else:
                session_key = None

            # Forward to message bus
            reply_to = chat_id if chat_type == "group" else sender_id
            await self._handle_message(
                sender_id=sender_id,
                chat_id=reply_to,
                content=content,
                media=media_paths,
                metadata={
                    "message_id": message_id,
                    "chat_type": chat_type,
                    "msg_type": msg_type,
                    "parent_id": parent_id,
                    "root_id": root_id,
                    "thread_id": thread_id,
                },
                session_key=session_key,
            )

        except Exception as e:
            logger.error("Error processing Feishu message: {}", e)

    def _on_reaction_created(self, data: Any) -> None:
        """Ignore reaction events so they do not generate SDK noise."""
        pass

    def _on_reaction_deleted(self, data: Any) -> None:
        """Ignore reaction deleted events so they do not generate SDK noise."""
        pass

    def _on_message_read(self, data: Any) -> None:
        """Ignore read events so they do not generate SDK noise."""
        pass

    def _on_bot_p2p_chat_entered(self, data: Any) -> None:
        """Ignore p2p-enter events when a user opens a bot chat."""
        logger.debug("Bot entered p2p chat (user opened chat window)")
        pass

    @staticmethod
    def _format_tool_hint_lines(tool_hint: str) -> str:
        """Split tool hints across lines on top-level call separators only."""
        parts: list[str] = []
        buf: list[str] = []
        depth = 0
        in_string = False
        quote_char = ""
        escaped = False

        for i, ch in enumerate(tool_hint):
            buf.append(ch)

            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == quote_char:
                    in_string = False
                continue

            if ch in {'"', "'"}:
                in_string = True
                quote_char = ch
                continue

            if ch == "(":
                depth += 1
                continue

            if ch == ")" and depth > 0:
                depth -= 1
                continue

            if ch == "," and depth == 0:
                next_char = tool_hint[i + 1] if i + 1 < len(tool_hint) else ""
                if next_char == " ":
                    parts.append("".join(buf).rstrip())
                    buf = []

        if buf:
            parts.append("".join(buf).strip())

        return "\n".join(part for part in parts if part)

    def _format_tool_hint_delta(self, tool_hint: str) -> str:
        """Format a tool hint string with the 🔧 prefix for each line."""
        lines = self.__class__._format_tool_hint_lines(tool_hint).split("\n")
        return "\n".join(
            f"{self.config.tool_hint_prefix} {ln}" for ln in lines if ln.strip()
        )
