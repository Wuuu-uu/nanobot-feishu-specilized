"""Feishu/Lark channel implementation using lark-oapi SDK with WebSocket long connection."""

import asyncio
import json
import mimetypes
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

import httpx

from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import FeishuConfig

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
        self._media_dir = Path(self.config.media_dir).expanduser()
    
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
            # Determine receive_id_type based on chat_id format
            # open_id starts with "ou_", chat_id starts with "oc_"
            if msg.chat_id.startswith("oc_"):
                receive_id_type = "chat_id"
            else:
                receive_id_type = "open_id"
            
            # Build rich text (post) message content
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

            title = msg.metadata.get("title") if isinstance(msg.metadata, dict) else None
            if msg.content or image_keys:
                post_content = self._build_post_content(
                    msg.content,
                    image_keys,
                    title=title or "🐈Nanobot: ",
                )
                content = json.dumps(post_content, ensure_ascii=False)

                request = CreateMessageRequest.builder() \
                    .receive_id_type(receive_id_type) \
                    .request_body(
                        CreateMessageRequestBody.builder()
                        .receive_id(msg.chat_id)
                        .msg_type("post")
                        .content(content)
                        .build()
                    ).build()

                response = self._client.im.v1.message.create(request)

                if not response.success():
                    logger.error(
                        f"Failed to send Feishu message: code={response.code}, "
                        f"msg={response.msg}, log_id={response.get_log_id()}"
                    )
                else:
                    logger.debug(f"Feishu message sent to {msg.chat_id}")

            for file_key in file_keys:
                await self._send_file_message(file_key, msg.chat_id, receive_id_type)
                
        except Exception as e:
            logger.error(f"Error sending Feishu message: {e}")

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

    @staticmethod
    def _build_post_content(text: str, image_keys: list[str], title: str) -> dict[str, Any]:
        content_rows: list[list[dict[str, Any]]] = []
        if text:
            content_rows.append([{"tag": "md", "text": text}])
        for image_key in image_keys:
            content_rows.append([{"tag": "img", "image_key": image_key}])

        if not content_rows:
            content_rows = [[{"tag": "md", "text": "[empty message]"}]]

        return {
            "zh_cn": {
                "title": title,
                "content": content_rows,
            }
        }
    
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
