"""Feishu/Lark channel implementation using lark-oapi SDK with WebSocket long connection."""

import asyncio
import json
import mimetypes
import re
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx

from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import FeishuConfig
from nanobot.utils.helpers import get_media_path

try:
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import (
        CreateImageRequest,
        CreateImageRequestBody,
        CreateMessageRequest,
        CreateMessageRequestBody,
        CreateMessageReactionRequest,
        CreateMessageReactionRequestBody,
        Emoji,
        P2ImMessageReceiveV1,
    )
    FEISHU_AVAILABLE = True
except ImportError:
    FEISHU_AVAILABLE = False
    lark = None
    Emoji = None

# Message type display mapping
MSG_TYPE_MAP = {
    "image": "[image]",
    "audio": "[audio]",
    "file": "[file]",
    "sticker": "[sticker]",
}


@dataclass
class _FeishuStreamState:
    """Runtime state for one streaming card session."""

    card_id: str
    message_id: str
    element_id: str
    tool_logs_element_id: str
    sequence: int = 1
    flushed_text: str = ""
    pending_text: str = ""
    last_flush_ts: float = 0.0
    tool_logs_flushed_text: str = ""
    tool_logs_pending_text: str = ""
    tool_logs_last_flush_ts: float = 0.0
    chart_element_id: str | None = None
    chart_flushed_spec: dict[str, Any] | None = None
    chart_pending_spec: dict[str, Any] | None = None
    chart_last_flush_ts: float = 0.0


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
    
    def __init__(self, config: FeishuConfig, bus: MessageBus):
        super().__init__(config, bus)
        self.config: FeishuConfig = config
        self._client: Any = None
        self._ws_client: Any = None
        self._ws_thread: threading.Thread | None = None
        self._processed_message_ids: OrderedDict[str, None] = OrderedDict()  # Ordered dedup cache
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tenant_access_token: str | None = None
        self._tenant_access_token_expire_at: float = 0.0
        self._media_dir = get_media_path(self.config.media_dir)
        self._stream_states: dict[str, _FeishuStreamState] = {}
        self._stream_locks: dict[str, asyncio.Lock] = {}
        self._stream_degraded: set[str] = set()
        self._stream_fallback_sent: set[str] = set()
    
    async def start(self) -> None:
        """Start the Feishu bot with WebSocket long connection."""
        if not FEISHU_AVAILABLE:
            logger.error("Feishu SDK not installed. Run: pip install lark-oapi")
            return
        
        if not self.config.app_id or not self.config.app_secret:
            logger.error("Feishu app_id and app_secret not configured")
            return
        
        self._running = True
        self._loop = asyncio.get_running_loop()
        
        # Create Lark client for sending messages
        self._client = lark.Client.builder() \
            .app_id(self.config.app_id) \
            .app_secret(self.config.app_secret) \
            .log_level(lark.LogLevel.INFO) \
            .build()
        
        # Create event handler (only register message receive, ignore other events)
        event_handler = lark.EventDispatcherHandler.builder(
            self.config.encrypt_key or "",
            self.config.verification_token or "",
        ).register_p2_im_message_receive_v1(
            self._on_message_sync
        ).build()
        
        # Create WebSocket client for long connection
        self._ws_client = lark.ws.Client(
            self.config.app_id,
            self.config.app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO
        )
        
        # Start WebSocket client in a separate thread
        def run_ws():
            try:
                self._ws_client.start()
            except Exception as e:
                logger.error(f"Feishu WebSocket error: {e}")
        
        self._ws_thread = threading.Thread(target=run_ws, daemon=True)
        self._ws_thread.start()
        
        logger.info("Feishu bot started with WebSocket long connection")
        logger.info("No public IP required - using WebSocket to receive events")
        
        # Keep running until stopped
        while self._running:
            await asyncio.sleep(1)
    
    async def stop(self) -> None:
        """Stop the Feishu bot."""
        self._running = False
        if self._ws_client:
            try:
                self._ws_client.stop()
            except Exception as e:
                logger.warning(f"Error stopping WebSocket client: {e}")
        logger.info("Feishu bot stopped")
    
    def _add_reaction_sync(self, message_id: str, emoji_type: str) -> None:
        """Sync helper for adding reaction (runs in thread pool)."""
        try:
            request = CreateMessageReactionRequest.builder() \
                .message_id(message_id) \
                .request_body(
                    CreateMessageReactionRequestBody.builder()
                    .reaction_type(Emoji.builder().emoji_type(emoji_type).build())
                    .build()
                ).build()
            
            response = self._client.im.v1.message_reaction.create(request)
            
            if not response.success():
                logger.warning(f"Failed to add reaction: code={response.code}, msg={response.msg}")
            else:
                logger.debug(f"Added {emoji_type} reaction to message {message_id}")
        except Exception as e:
            logger.warning(f"Error adding reaction: {e}")

    async def _add_reaction(self, message_id: str, emoji_type: str = "THUMBSUP") -> None:
        """
        Add a reaction emoji to a message (non-blocking).
        
        Common emoji types: THUMBSUP, OK, EYES, DONE, OnIt, HEART
        """
        if not self._client or not Emoji:
            return
        
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._add_reaction_sync, message_id, emoji_type)
    
    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through Feishu."""
        if not self._client:
            logger.warning("Feishu client not initialized")
            return
        
        try:
            receive_id_type = self._resolve_receive_id_type(msg.chat_id)
            metadata = msg.metadata if isinstance(msg.metadata, dict) else {}
            stream_payload = metadata.get("feishu_stream") if isinstance(metadata.get("feishu_stream"), dict) else None
            
            # Prepare outbound message payload and media uploads
            image_keys: list[str] = []
            file_keys: list[str] = []
            if msg.media:
                for media_path in msg.media:
                    try:
                        if self._is_image(media_path):
                            image_key = self._upload_image(media_path)
                            image_keys.append(image_key)
                        else:
                            file_key = await self._upload_file(media_path)
                            file_keys.append(file_key)
                    except Exception as e:
                        logger.warning(f"Failed to upload media {media_path}: {e}")

            if msg.content or (self.config.streaming_enabled and stream_payload):
                if self.config.streaming_enabled and stream_payload:
                    stream_id = str(stream_payload.get("stream_id") or f"{msg.chat_id}:default")
                    handled = await self._handle_streaming_message(
                        msg=msg,
                        receive_id_type=receive_id_type,
                        stream_payload=stream_payload,
                    )
                    if (
                        not handled
                        and msg.content
                        and stream_id not in self._stream_fallback_sent
                    ):
                        self._stream_fallback_sent.add(stream_id)
                        await self._send_template_message(msg, receive_id_type)
                else:
                    await self._send_template_message(msg, receive_id_type)

            for image_key in image_keys:
                await self._send_image_message(image_key, msg.chat_id, receive_id_type)

            for file_key in file_keys:
                await self._send_file_message(file_key, msg.chat_id, receive_id_type)
                
        except Exception as e:
            logger.error(f"Error sending Feishu message: {e}")

    def _resolve_receive_id_type(self, chat_id: str) -> str:
        """Infer receive_id_type from Feishu identifier prefix."""
        return "chat_id" if chat_id.startswith("oc_") else "open_id"

    async def _send_template_message(self, msg: OutboundMessage, receive_id_type: str) -> None:
        """Send one-shot interactive template card (existing behavior)."""
        template_id = self.config.card_template_id
        template_version_name = self.config.card_template_version_name
        metadata = msg.metadata if isinstance(msg.metadata, dict) else {}
        template_id = metadata.get("template_id", template_id)
        template_version_name = metadata.get("template_version_name", template_version_name)

        card_text = await self._replace_local_md_images_with_keys(msg.content)
        content = self._build_interactive_content(
            card_text,
            template_id=template_id,
            template_version_name=template_version_name,
            token_monitor=metadata.get("token_monitor"),
        )

        request = CreateMessageRequest.builder() \
            .receive_id_type(receive_id_type) \
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(msg.chat_id)
                .msg_type("interactive")
                .content(content)
                .build()
            ).build()

        response = self._client.im.v1.message.create(request)
        if not response.success():
            logger.error(
                f"Failed to send Feishu message: code={response.code}, "
                f"msg={response.msg}, log_id={response.get_log_id()}"
            )
            return
        logger.debug(f"Feishu template message sent to {msg.chat_id}")

    async def _handle_streaming_message(
        self,
        msg: OutboundMessage,
        receive_id_type: str,
        stream_payload: dict[str, Any],
    ) -> bool:
        """Handle Feishu CardKit streaming init/append/finalize lifecycle."""
        action = str(stream_payload.get("action", "")).strip().lower()
        metadata = msg.metadata if isinstance(msg.metadata, dict) else {}
        if "token_monitor" not in stream_payload and isinstance(metadata.get("token_monitor"), dict):
            stream_payload = dict(stream_payload)
            stream_payload["token_monitor"] = metadata.get("token_monitor")
        if action not in {"init", "tool_update", "append", "finalize"}:
            logger.warning(f"Unknown feishu_stream action: {action}")
            return False

        stream_id = str(stream_payload.get("stream_id") or f"{msg.chat_id}:default")
        if stream_id in self._stream_degraded:
            if action == "finalize":
                self._cleanup_stream_state(stream_id)
            return True

        lock = self._stream_locks.setdefault(stream_id, asyncio.Lock())
        async with lock:
            try:
                if action == "init":
                    await self._stream_init(msg, receive_id_type, stream_id, stream_payload)
                    return True

                state = self._stream_states.get(stream_id)
                if state is None:
                    logger.warning(f"Missing stream state for {stream_id}, auto-initializing")
                    await self._stream_init(msg, receive_id_type, stream_id, stream_payload)
                    state = self._stream_states.get(stream_id)
                    if state is None:
                        return False

                if action == "append":
                    await self._stream_append(msg, stream_payload, state)
                    return True

                if action == "tool_update":
                    await self._stream_update_tool_logs(stream_payload, state)
                    return True

                await self._stream_finalize(msg, stream_payload, stream_id, state)
                return True
            except Exception as e:
                logger.error(f"Feishu streaming action failed ({action}, {stream_id}): {e}")
                self._stream_degraded.add(stream_id)
                return False

    def _cleanup_stream_state(self, stream_id: str) -> None:
        """Release in-memory state for one stream lifecycle."""
        self._stream_states.pop(stream_id, None)
        self._stream_locks.pop(stream_id, None)
        self._stream_degraded.discard(stream_id)
        self._stream_fallback_sent.discard(stream_id)

    async def _stream_init(
        self,
        msg: OutboundMessage,
        receive_id_type: str,
        stream_id: str,
        stream_payload: dict[str, Any],
    ) -> None:
        """Create streaming card and send it once using card_id payload."""
        initial_text = stream_payload.get("full_text")
        if not isinstance(initial_text, str):
            initial_text = msg.content or ""
        if not initial_text.strip():
            initial_text = "Generating..."

        initial_text = await self._replace_local_md_images_with_keys(initial_text)
        metadata = msg.metadata if isinstance(msg.metadata, dict) else {}
        token_monitor = metadata.get("token_monitor") if isinstance(metadata.get("token_monitor"), dict) else None

        card_json = self._build_streaming_card_json(initial_text, token_monitor=token_monitor)
        try:
            card_id = await self._cardkit_create_card(card_json)
        except Exception:
            # Keep streaming available even if chart schema is rejected by server/client constraints.
            logger.warning("CardKit create with token chart failed, retrying without chart")
            card_json = self._build_streaming_card_json(initial_text, token_monitor=None)
            card_id = await self._cardkit_create_card(card_json)
        message_id = await self._cardkit_send_card_message(
            receive_id=msg.chat_id,
            receive_id_type=receive_id_type,
            card_id=card_id,
        )
        self._stream_states[stream_id] = _FeishuStreamState(
            card_id=card_id,
            message_id=message_id,
            element_id="answer_markdown",
            tool_logs_element_id="tool_logs_markdown",
            sequence=1,
            flushed_text=initial_text,
            pending_text=initial_text,
            last_flush_ts=time.monotonic(),
            tool_logs_flushed_text=self._default_tool_logs_markdown(),
            tool_logs_pending_text=self._default_tool_logs_markdown(),
            tool_logs_last_flush_ts=time.monotonic(),
            chart_element_id="token_budget_chart" if isinstance(token_monitor, dict) else None,
            chart_flushed_spec=self._chart_spec_from_token_monitor(token_monitor),
            chart_pending_spec=self._chart_spec_from_token_monitor(token_monitor),
            chart_last_flush_ts=time.monotonic(),
        )
        logger.debug(f"Feishu streaming initialized: stream_id={stream_id}, card_id={card_id}")

    async def _stream_append(
        self,
        msg: OutboundMessage,
        stream_payload: dict[str, Any],
        state: _FeishuStreamState,
    ) -> None:
        """Push full text update to CardKit stream API with local throttling."""
        full_text = stream_payload.get("full_text")
        if not isinstance(full_text, str):
            full_text = msg.content or ""

        full_text = await self._replace_local_md_images_with_keys(full_text)
        if not full_text:
            return

        state.pending_text = full_text
        force_flush = bool(stream_payload.get("force", False))
        max_updates = max(1, int(self.config.streaming_max_updates_per_sec))
        min_interval = 1.0 / max_updates
        now = time.monotonic()
        if not force_flush and (now - state.last_flush_ts) < min_interval:
            return

        if state.pending_text == state.flushed_text:
            return

        await self._cardkit_stream_text(
            card_id=state.card_id,
            element_id=state.element_id,
            full_text=state.pending_text,
            sequence=state.sequence,
        )
        state.sequence += 1
        state.flushed_text = state.pending_text
        state.last_flush_ts = time.monotonic()
        await self._stream_update_token_chart(stream_payload, state)

    async def _stream_update_tool_logs(
        self,
        stream_payload: dict[str, Any],
        state: _FeishuStreamState,
    ) -> None:
        """Push full tool logs markdown update to collapsible panel."""
        logs_text = stream_payload.get("tool_logs_markdown")
        if not isinstance(logs_text, str):
            return

        if not logs_text.strip():
            logs_text = self._default_tool_logs_markdown()

        state.tool_logs_pending_text = logs_text
        force_flush = bool(stream_payload.get("force", False))
        max_updates = max(1, int(self.config.streaming_max_updates_per_sec))
        min_interval = 1.0 / max_updates
        now = time.monotonic()
        if not force_flush and (now - state.tool_logs_last_flush_ts) < min_interval:
            return

        if state.tool_logs_pending_text == state.tool_logs_flushed_text:
            return

        await self._cardkit_stream_text(
            card_id=state.card_id,
            element_id=state.tool_logs_element_id,
            full_text=state.tool_logs_pending_text,
            sequence=state.sequence,
        )
        state.sequence += 1
        state.tool_logs_flushed_text = state.tool_logs_pending_text
        state.tool_logs_last_flush_ts = time.monotonic()
        await self._stream_update_token_chart(stream_payload, state)

    async def _stream_update_token_chart(
        self,
        stream_payload: dict[str, Any],
        state: _FeishuStreamState,
    ) -> None:
        """Keep token chart synchronized during tool and answer streaming."""
        if not state.chart_element_id:
            return

        token_monitor = stream_payload.get("token_monitor")
        if not isinstance(token_monitor, dict):
            return

        chart_spec = self._chart_spec_from_token_monitor(token_monitor)
        if not isinstance(chart_spec, dict):
            return

        state.chart_pending_spec = chart_spec
        if state.chart_pending_spec == state.chart_flushed_spec:
            return

        max_updates = max(1, int(self.config.streaming_max_updates_per_sec))
        min_interval = 1.0 / max_updates
        now = time.monotonic()
        if (now - state.chart_last_flush_ts) < min_interval and not bool(stream_payload.get("force", False)):
            return

        try:
            await self._cardkit_patch_element(
                card_id=state.card_id,
                element_id=state.chart_element_id,
                partial_element={"chart_spec": state.chart_pending_spec},
                sequence=state.sequence,
            )
            state.sequence += 1
            state.chart_flushed_spec = state.chart_pending_spec
            state.chart_last_flush_ts = time.monotonic()
        except Exception as e:
            # Do not let chart refresh errors break the whole streaming lifecycle.
            logger.warning(f"Failed to update token chart, disabling chart updates for this stream: {e}")
            state.chart_element_id = None

    async def _stream_finalize(
        self,
        msg: OutboundMessage,
        stream_payload: dict[str, Any],
        stream_id: str,
        state: _FeishuStreamState,
    ) -> None:
        """Flush latest text then close streaming mode."""
        final_text = stream_payload.get("full_text")
        if not isinstance(final_text, str):
            final_text = msg.content or state.pending_text or state.flushed_text

        if final_text:
            state.pending_text = await self._replace_local_md_images_with_keys(final_text)

        if state.pending_text and state.pending_text != state.flushed_text:
            await self._cardkit_stream_text(
                card_id=state.card_id,
                element_id=state.element_id,
                full_text=state.pending_text,
                sequence=state.sequence,
            )
            state.sequence += 1
            state.flushed_text = state.pending_text

        if state.tool_logs_pending_text and state.tool_logs_pending_text != state.tool_logs_flushed_text:
            await self._cardkit_stream_text(
                card_id=state.card_id,
                element_id=state.tool_logs_element_id,
                full_text=state.tool_logs_pending_text,
                sequence=state.sequence,
            )
            state.sequence += 1
            state.tool_logs_flushed_text = state.tool_logs_pending_text

        await self._stream_update_token_chart(stream_payload, state)

        await self._cardkit_update_settings(
            card_id=state.card_id,
            settings={"config": {"streaming_mode": False}},
            sequence=state.sequence,
        )
        self._cleanup_stream_state(stream_id)
        logger.debug(f"Feishu streaming finalized: stream_id={stream_id}, card_id={state.card_id}")

    def _upload_image(self, image_path: str) -> str:
        if not self._client or not FEISHU_AVAILABLE:
            raise RuntimeError("Feishu client not initialized")

        with open(image_path, "rb") as f:
            request = CreateImageRequest.builder() \
                .request_body(
                    CreateImageRequestBody.builder()
                    .image_type("message")
                    .image(f)
                    .build()
                ).build()

            response = self._client.im.v1.image.create(request)

        if not response.success():
            raise RuntimeError(f"image upload failed: code={response.code}, msg={response.msg}")

        return response.data.image_key

    async def _upload_file(self, file_path: str) -> str:
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        size = path.stat().st_size
        if size <= 0:
            raise RuntimeError(f"File is empty: {file_path}")
        if size > 30 * 1024 * 1024:
            raise RuntimeError(f"File too large (>30MB): {file_path}")

        token = await self._get_tenant_access_token()
        url = "https://open.feishu.cn/open-apis/im/v1/files"
        file_type = self._file_type_from_path(path)
        mime_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"

        data = {
            "file_type": file_type,
            "file_name": path.name,
        }
        files = {
            "file": (path.name, path.read_bytes(), mime_type),
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                data=data,
                files=files,
            )
            response.raise_for_status()
            payload = response.json()

        if payload.get("code") != 0:
            raise RuntimeError(f"Feishu upload failed: code={payload.get('code')}, msg={payload.get('msg')}")

        file_key = (payload.get("data") or {}).get("file_key")
        if not file_key:
            raise RuntimeError("Feishu upload missing file_key")
        return file_key

    async def _send_file_message(self, file_key: str, chat_id: str, receive_id_type: str) -> None:
        content = json.dumps({"file_key": file_key}, ensure_ascii=False)
        request = CreateMessageRequest.builder() \
            .receive_id_type(receive_id_type) \
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("file")
                .content(content)
                .build()
            ).build()

        response = self._client.im.v1.message.create(request)
        if not response.success():
            raise RuntimeError(f"file message send failed: code={response.code}, msg={response.msg}")

    async def _send_image_message(self, image_key: str, chat_id: str, receive_id_type: str) -> None:
        content = json.dumps({"image_key": image_key}, ensure_ascii=False)
        request = CreateMessageRequest.builder() \
            .receive_id_type(receive_id_type) \
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("image")
                .content(content)
                .build()
            ).build()

        response = self._client.im.v1.message.create(request)
        if not response.success():
            raise RuntimeError(f"image message send failed: code={response.code}, msg={response.msg}")

    @staticmethod
    def _file_type_from_path(path: Path) -> str:
        ext = path.suffix.lower().lstrip(".")
        if ext in {"opus", "mp4", "pdf", "doc", "xls", "ppt"}:
            return ext
        if ext in {"docx"}:
            return "doc"
        if ext in {"xlsx"}:
            return "xls"
        if ext in {"pptx"}:
            return "ppt"
        return "stream"

    @staticmethod
    def _is_image(file_path: str) -> bool:
        mime = mimetypes.guess_type(file_path)[0] or ""
        return mime.startswith("image/")

    def _resolve_local_md_image_path(self, markdown_url: str) -> Path | None:
        """Resolve a markdown image url to an existing local image path, if possible."""
        raw = markdown_url.strip()
        if not raw:
            return None

        # Markdown image url may include optional title: ![alt](url "title")
        if raw.startswith("<") and ">" in raw:
            raw = raw[1:raw.find(">")]
        else:
            raw = raw.split(maxsplit=1)[0]

        lowered = raw.lower()
        if lowered.startswith(("http://", "https://", "data:")):
            return None
        if raw.startswith("img_v"):
            return None

        if lowered.startswith("file://"):
            parsed = urlparse(raw)
            candidate = Path(unquote(parsed.path)).expanduser()
        else:
            candidate = Path(unquote(raw)).expanduser()

        candidates = [candidate]
        if not candidate.is_absolute():
            candidates.append(Path.cwd() / candidate)
            candidates.append(self._media_dir / candidate)

        for path in candidates:
            if path.exists() and path.is_file() and self._is_image(str(path)):
                return path
        return None

    async def _replace_local_md_images_with_keys(self, text: str) -> str:
        """Upload local markdown images and replace their paths with Feishu image keys."""
        pattern = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
        matches = list(pattern.finditer(text))
        if not matches:
            return text

        upload_cache: dict[str, str] = {}
        parts: list[str] = []
        last = 0

        for match in matches:
            parts.append(text[last:match.start()])
            alt_text = match.group(1)
            raw_url = match.group(2)

            normalized_url = raw_url.strip()
            if normalized_url.startswith("<") and ">" in normalized_url:
                normalized_url = normalized_url[1:normalized_url.find(">")]
            else:
                normalized_url = normalized_url.split(maxsplit=1)[0]
            lowered_url = normalized_url.lower()

            replacement = match.group(0)
            local_path = self._resolve_local_md_image_path(raw_url)
            if local_path:
                key = upload_cache.get(str(local_path))
                if key is None:
                    try:
                        key = self._upload_image(str(local_path))
                        upload_cache[str(local_path)] = key
                    except Exception as e:
                        logger.warning(f"Failed to upload markdown image {local_path}: {e}")
                        key = None
                if key:
                    replacement = f"![{alt_text}]({key})"
            elif not lowered_url.startswith(("http://", "https://", "data:")) and not normalized_url.startswith("img_v"):
                # Prevent CardKit from treating unresolved local file names as invalid image keys.
                fallback_name = Path(unquote(normalized_url)).name or normalized_url
                fallback_alt = alt_text.strip() or fallback_name or "local-image"
                replacement = f"[image omitted: {fallback_alt}]"

            parts.append(replacement)
            last = match.end()

        parts.append(text[last:])
        return "".join(parts)

    @staticmethod
    def _build_interactive_content(
        text: str,
        template_id: str,
        template_version_name: str,
        token_monitor: dict[str, Any] | None = None,
    ) -> str:
        default_chart = {
            "type": "bar",
            "direction": "horizontal",
            "title": {"text": "token用量占比图"},
            "data": {
                "values": [
                    {"category": "token用量", "item": "input_cached", "value": 0},
                    {"category": "token用量", "item": "input_uncached", "value": 0},
                    {"category": "token用量", "item": "output", "value": 0},
                    {"category": "token用量", "item": "sum_tokens", "value": 0},
                ]
            },
            "xField": "value",
            "yField": "category",
            "seriesField": "item",
            "stack": True,
            "legends": {"visible": True, "orient": "bottom"},
            "label": {"visible": True, "formatter": "value"},
        }

        chart = default_chart
        if isinstance(token_monitor, dict) and isinstance(token_monitor.get("chart"), dict):
            chart = token_monitor["chart"]

        template_variable: dict[str, Any] = {
            "content": text,
            "token_budget": chart,
        }

        payload = {
            "type": "template",
            "data": {
                "template_id": template_id,
                "template_version_name": template_version_name,
                "template_variable": template_variable,
            },
        }
        return json.dumps(payload, ensure_ascii=False)

    @staticmethod
    def _default_token_chart() -> dict[str, Any]:
        return {
            "type": "bar",
            "direction": "horizontal",
            "title": {"text": "token用量占比图"},
            "data": {
                "values": [
                    {"category": "token用量", "item": "input_cached", "value": 0},
                    {"category": "token用量", "item": "input_uncached", "value": 0},
                    {"category": "token用量", "item": "output", "value": 0},
                    {"category": "token用量", "item": "sum_tokens", "value": 0},
                ]
            },
            "xField": "value",
            "yField": "category",
            "seriesField": "item",
            "stack": True,
            "legends": {"visible": True, "orient": "bottom"},
            "label": {"visible": True, "formatter": "value"},
        }

    @staticmethod
    def _default_tool_logs_markdown() -> str:
        return "工具调用记录\n暂无工具调用。"

    def _chart_spec_from_token_monitor(self, token_monitor: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(token_monitor, dict):
            return None
        chart = token_monitor.get("chart")
        if isinstance(chart, dict):
            return chart
        return self._default_token_chart()

    def _build_streaming_card_json(
        self,
        text: str,
        token_monitor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build a JSON 2.0 streaming card; chart is optional for compatibility."""
        safe_text = text if text.strip() else "Generating..."
        chart = self._default_token_chart()
        if isinstance(token_monitor, dict) and isinstance(token_monitor.get("chart"), dict):
            chart = token_monitor["chart"]

        elements: list[dict[str, Any]] = [
            {
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
                        "element_id": "tool_logs_markdown",
                        "text_size": "notation",
                        "content": self._default_tool_logs_markdown(),
                    }
                ],
            },
            {
                "tag": "markdown",
                "content": safe_text,
                "element_id": "answer_markdown",
            }
        ]
        if isinstance(token_monitor, dict):
            elements.append(
                {
                "tag": "hr",
                "margin": "0px 0px 0px 0px",
            }
            )
            elements.append(
                {
                    "tag": "chart",
                    "chart_spec": chart,
                    "element_id": "token_budget_chart",
                    "color_theme": "complementary",
                    "height": "50px",
                    "margin": "0px 0px 0px 0px",
                }
            )

        return {
            "schema": "2.0",
            "config": {
                "update_multi": True,
                "streaming_mode": True,
                "summary": {"content": ""},
                "streaming_config": {
                    "print_frequency_ms": {
                        "default": int(self.config.streaming_print_frequency_ms_default),
                    },
                    "print_step": {
                        "default": int(self.config.streaming_print_step_default),
                    },
                    "print_strategy": self.config.streaming_print_strategy,
                },
            },
            "body": {
                "elements": elements
            },
        }

    @staticmethod
    def _build_card_id_message_content(card_id: str) -> str:
        """Build im.v1.message content payload using card_id reference."""
        payload = {
            "type": "card",
            "data": {
                "card_id": card_id,
            },
        }
        return json.dumps(payload, ensure_ascii=False)

    @staticmethod
    def _build_sequence_uuid(sequence: int) -> str:
        return f"nanobot-{sequence}-{uuid.uuid4()}"

    async def _cardkit_create_card(self, card_json: dict[str, Any]) -> str:
        """Create CardKit card entity and return card_id."""
        payload = {
            "type": "card_json",
            "data": json.dumps(card_json, ensure_ascii=False, separators=(",", ":")),
        }
        data = await self._cardkit_request(
            method="POST",
            path="/open-apis/cardkit/v1/cards",
            payload=payload,
        )
        card_id = (data.get("data") or {}).get("card_id")
        if not card_id:
            raise RuntimeError("CardKit create card missing card_id")
        return str(card_id)

    async def _cardkit_send_card_message(self, receive_id: str, receive_id_type: str, card_id: str) -> str:
        """Send created card entity as an interactive message and return message_id."""
        request = CreateMessageRequest.builder() \
            .receive_id_type(receive_id_type) \
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(receive_id)
                .msg_type("interactive")
                .content(self._build_card_id_message_content(card_id))
                .build()
            ).build()

        response = self._client.im.v1.message.create(request)
        if not response.success():
            raise RuntimeError(
                f"Failed to send card_id message: code={response.code}, msg={response.msg}, log_id={response.get_log_id()}"
            )
        message_id = getattr(response.data, "message_id", "") if response.data else ""
        return str(message_id)

    async def _cardkit_stream_element_content(
        self,
        card_id: str,
        element_id: str,
        content: Any,
        sequence: int,
    ) -> None:
        """Update element content for streaming-capable CardKit element."""
        payload = {
            "content": content,
            "uuid": self._build_sequence_uuid(sequence),
            "sequence": sequence,
        }
        await self._cardkit_request(
            method="PUT",
            path=f"/open-apis/cardkit/v1/cards/{card_id}/elements/{element_id}/content",
            payload=payload,
        )

    async def _cardkit_stream_text(self, card_id: str, element_id: str, full_text: str, sequence: int) -> None:
        """Update markdown/plain_text content in streaming mode using full text."""
        await self._cardkit_stream_element_content(
            card_id=card_id,
            element_id=element_id,
            content=full_text,
            sequence=sequence,
        )

    async def _cardkit_patch_element(
        self,
        card_id: str,
        element_id: str,
        partial_element: dict[str, Any],
        sequence: int,
    ) -> None:
        """Patch one CardKit element property set (e.g. chart_spec)."""
        payload = {
            "partial_element": json.dumps(partial_element, ensure_ascii=False, separators=(",", ":")),
            "uuid": self._build_sequence_uuid(sequence),
            "sequence": sequence,
        }
        await self._cardkit_request(
            method="PATCH",
            path=f"/open-apis/cardkit/v1/cards/{card_id}/elements/{element_id}",
            payload=payload,
        )

    async def _cardkit_update_settings(self, card_id: str, settings: dict[str, Any], sequence: int) -> None:
        """Update card settings such as streaming_mode."""
        payload = {
            "settings": json.dumps(settings, ensure_ascii=False, separators=(",", ":")),
            "uuid": self._build_sequence_uuid(sequence),
            "sequence": sequence,
        }
        await self._cardkit_request(
            method="PATCH",
            path=f"/open-apis/cardkit/v1/cards/{card_id}/settings",
            payload=payload,
        )

    async def _cardkit_request(self, method: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Issue one CardKit OpenAPI request with tenant token."""
        token = await self._get_tenant_access_token()
        url = f"https://open.feishu.cn{path}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.request(method=method, url=url, headers=headers, json=payload)
            try:
                data = response.json()
            except ValueError:
                data = {"code": -1, "msg": response.text[:500]}

        if response.status_code >= 400:
            raise RuntimeError(
                "CardKit HTTP error: "
                f"status={response.status_code}, code={data.get('code')}, msg={data.get('msg')}, path={path}"
            )

        if data.get("code") != 0:
            raise RuntimeError(f"CardKit API error: code={data.get('code')}, msg={data.get('msg')}, path={path}")
        return data
    
    def _on_message_sync(self, data: "P2ImMessageReceiveV1") -> None:
        """
        Sync handler for incoming messages (called from WebSocket thread).
        Schedules async handling in the main event loop.
        """
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._on_message(data), self._loop)
    
    async def _on_message(self, data: "P2ImMessageReceiveV1") -> None:
        """Handle incoming message from Feishu."""
        try:
            event = data.event
            message = event.message
            sender = event.sender
            
            # Deduplication check
            message_id = message.message_id
            if message_id in self._processed_message_ids:
                return
            self._processed_message_ids[message_id] = None
            
            # Trim cache: keep most recent 500 when exceeds 1000
            while len(self._processed_message_ids) > 1000:
                self._processed_message_ids.popitem(last=False)
            
            # Skip bot messages
            sender_type = sender.sender_type
            if sender_type == "bot":
                return
            
            sender_id = sender.sender_id.open_id if sender.sender_id else "unknown"
            chat_id = message.chat_id
            chat_type = message.chat_type  # "p2p" or "group"
            msg_type = message.message_type
            
            # Add reaction to indicate "seen"
            await self._add_reaction(message_id, "THUMBSUP")
            
            # Parse message content
            media_paths: list[str] = []
            if msg_type == "text":
                try:
                    content = json.loads(message.content).get("text", "")
                except json.JSONDecodeError:
                    content = message.content or ""
            elif msg_type == "image":
                image_key = self._extract_image_key(message.content)
                if image_key:
                    try:
                        saved_path = await self._download_image_resource(message_id, image_key)
                        media_paths.append(str(saved_path))
                        content = f"[image: {saved_path}]"
                    except Exception as e:
                        logger.warning(f"Failed to download Feishu image: {e}")
                        content = "[image: download failed]"
                else:
                    content = "[image: missing image_key]"
            elif msg_type == "file":
                file_key, file_name = self._extract_file_info(message.content)
                if file_key:
                    try:
                        saved_path = await self._download_file_resource(message_id, file_key, file_name)
                        media_paths.append(str(saved_path))
                        content = f"[file: {saved_path}]"
                    except Exception as e:
                        logger.warning(f"Failed to download Feishu file: {e}")
                        fallback_name = file_name or file_key
                        content = f"[file: download failed ({fallback_name})]"
                else:
                    content = "[file: missing file_key]"
            else:
                content = MSG_TYPE_MAP.get(msg_type, f"[{msg_type}]")
            
            if not content:
                return
            
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
                }
            )
            
        except Exception as e:
            logger.error(f"Error processing Feishu message: {e}")

    async def _get_tenant_access_token(self) -> str:
        """Fetch and cache Feishu tenant_access_token."""
        now = time.time()
        if self._tenant_access_token and now < self._tenant_access_token_expire_at - 60:
            return self._tenant_access_token

        if not self.config.app_id or not self.config.app_secret:
            raise RuntimeError("Feishu app_id/app_secret not configured")

        url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        payload = {"app_id": self.config.app_id, "app_secret": self.config.app_secret}

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()

        if data.get("code") != 0:
            raise RuntimeError(f"Feishu token error: code={data.get('code')}, msg={data.get('msg')}")

        token = data.get("tenant_access_token")
        if not token:
            raise RuntimeError("Feishu token missing in response")

        expire = int(data.get("expire", 3600))
        self._tenant_access_token = token
        self._tenant_access_token_expire_at = now + expire
        return token

    async def _download_image_resource(self, message_id: str, image_key: str) -> Path:
        """Download an image resource and save it to the media directory."""
        token = await self._get_tenant_access_token()
        url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/resources/{image_key}"
        headers = {"Authorization": f"Bearer {token}"}

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, params={"type": "image"}, headers=headers)
            response.raise_for_status()

        content_type = response.headers.get("Content-Type", "").split(";")[0].strip()
        extension = mimetypes.guess_extension(content_type) or ".jpg"
        safe_key = self._sanitize_filename(image_key)

        self._media_dir.mkdir(parents=True, exist_ok=True)
        file_path = self._media_dir / f"{message_id}_{safe_key}{extension}"
        file_path.write_bytes(response.content)
        return file_path

    async def _download_file_resource(self, message_id: str, file_key: str, file_name: str | None = None) -> Path:
        """Download a file resource and save it to the media directory."""
        token = await self._get_tenant_access_token()
        url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/resources/{file_key}"
        headers = {"Authorization": f"Bearer {token}"}

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.get(url, params={"type": "file"}, headers=headers)
            response.raise_for_status()

        # Prefer name from message payload, fallback to Content-Disposition.
        header_name = self._extract_filename_from_content_disposition(
            response.headers.get("Content-Disposition", "")
        )
        effective_name = self._sanitize_filename(file_name or header_name or f"{file_key}.bin")

        self._media_dir.mkdir(parents=True, exist_ok=True)
        file_path = self._media_dir / f"{message_id}_{effective_name}"
        file_path.write_bytes(response.content)
        return file_path

    @staticmethod
    def _sanitize_filename(value: str) -> str:
        return "".join(ch for ch in value if ch.isalnum() or ch in ("-", "_", ".")) or "file"

    @staticmethod
    def _extract_image_key(content: str | None) -> str | None:
        if not content:
            return None
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return None
        return (
            data.get("image_key")
            or data.get("file_key")
            or data.get("imageKey")
            or data.get("fileKey")
        )

    @staticmethod
    def _extract_file_info(content: str | None) -> tuple[str | None, str | None]:
        if not content:
            return None, None
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return None, None
        file_key = data.get("file_key") or data.get("fileKey")
        file_name = data.get("file_name") or data.get("fileName")
        return file_key, file_name

    @staticmethod
    def _extract_filename_from_content_disposition(content_disposition: str) -> str | None:
        if not content_disposition:
            return None

        # RFC5987 form: filename*=UTF-8''hello%20world.txt
        extended = re.search(r"filename\*=([^;]+)", content_disposition, flags=re.IGNORECASE)
        if extended:
            raw = extended.group(1).strip().strip('"')
            if "''" in raw:
                _, encoded_name = raw.split("''", 1)
                return unquote(encoded_name)
            return unquote(raw)

        basic = re.search(r"filename=([^;]+)", content_disposition, flags=re.IGNORECASE)
        if basic:
            return basic.group(1).strip().strip('"')
        return None
