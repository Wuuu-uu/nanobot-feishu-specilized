"""Message tool for sending messages to users."""

import base64
import mimetypes
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from nanobot.utils.helpers import get_data_path

from nanobot.agent.tools.base import Tool
from nanobot.bus.events import OutboundMessage


class MessageTool(Tool):
    """Tool to send messages to users on chat channels."""
    
    def __init__(
        self, 
        send_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
        default_channel: str = "",
        default_chat_id: str = ""
    ):
        self._send_callback = send_callback
        self._default_channel = default_channel
        self._default_chat_id = default_chat_id
    
    def set_context(self, channel: str, chat_id: str) -> None:
        """Set the current message context."""
        self._default_channel = channel
        self._default_chat_id = chat_id
    
    def set_send_callback(self, callback: Callable[[OutboundMessage], Awaitable[None]]) -> None:
        """Set the callback for sending messages."""
        self._send_callback = callback
    
    @property
    def name(self) -> str:
        return "message"
    
    @property
    def description(self) -> str:
        return "Send a message to the user. Use this when you want to communicate something, send local pictures or local files to the user."
    
    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The message content to send"
                },
                "media": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional local file paths for media (images) to send"
                },
                "image_path": {
                    "type": "string",
                    "description": "Optional local image path to send"
                },
                "image_base64": {
                    "type": "string",
                    "description": "Optional base64 image data or data URI"
                },
                "image_mime_type": {
                    "type": "string",
                    "description": "Optional mime type for base64 image (default: image/png)"
                },
                "file_path": {
                    "type": "string",
                    "description": "Optional local file path to send"
                },
                "file_base64": {
                    "type": "string",
                    "description": "Optional base64 file data or data URI"
                },
                "file_name": {
                    "type": "string",
                    "description": "Optional filename for base64 file data"
                },
                "file_mime_type": {
                    "type": "string",
                    "description": "Optional mime type for base64 file (default: application/octet-stream)"
                },
                "title": {
                    "type": "string",
                    "description": "Optional title for rich media messages"
                },
                "channel": {
                    "type": "string",
                    "description": "Optional: target channel (telegram, discord, etc.)"
                },
                "chat_id": {
                    "type": "string",
                    "description": "Optional: target chat/user ID"
                }
            },
            "required": []
        }
    
    async def execute(
        self, 
        content: str | None = None,
        channel: str | None = None, 
        chat_id: str | None = None,
        media: list[str] | None = None,
        image_path: str | None = None,
        image_base64: str | None = None,
        image_mime_type: str | None = None,
        file_path: str | None = None,
        file_base64: str | None = None,
        file_name: str | None = None,
        file_mime_type: str | None = None,
        title: str | None = None,
        **kwargs: Any
    ) -> str:
        channel = channel or self._default_channel
        chat_id = chat_id or self._default_chat_id
        
        if not channel or not chat_id:
            return "Error: No target channel/chat specified"
        
        if not self._send_callback:
            return "Error: Message sending not configured"
        
        content = content or ""
        media_paths = list(media or [])

        if image_path:
            media_paths.append(image_path)

        if image_base64:
            try:
                saved_path = self._save_base64_image(image_base64, image_mime_type)
                media_paths.append(saved_path)
            except Exception as e:
                return f"Error: failed to save base64 image: {str(e)}"

        if file_path:
            media_paths.append(file_path)

        if file_base64:
            try:
                saved_path = self._save_base64_file(file_base64, file_name, file_mime_type)
                media_paths.append(saved_path)
            except Exception as e:
                return f"Error: failed to save base64 file: {str(e)}"

        if not content and not media_paths:
            return "Error: No content or media provided"

        metadata = {"title": title} if title else {}

        msg = OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=content,
            media=media_paths,
            metadata=metadata,
        )
        
        try:
            await self._send_callback(msg)
            return f"Message sent to {channel}:{chat_id}"
        except Exception as e:
            return f"Error sending message: {str(e)}"

    def _save_base64_image(self, data: str, mime_type: str | None = None) -> str:
        """Decode base64 image data to a file and return the path."""
        if data.startswith("data:"):
            header, b64 = data.split(",", 1)
            if ";base64" in header:
                mime_type = header.split(";")[0].replace("data:", "") or mime_type
            data = b64

        raw = base64.b64decode(data)
        ext = mimetypes.guess_extension(mime_type or "image/png") or ".png"
        stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
        media_dir = get_data_path() / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        file_path = media_dir / f"outbound_{stamp}{ext}"
        Path(file_path).write_bytes(raw)
        return str(file_path)

    def _save_base64_file(
        self,
        data: str,
        file_name: str | None = None,
        mime_type: str | None = None,
    ) -> str:
        """Decode base64 file data to a file and return the path."""
        if data.startswith("data:"):
            header, b64 = data.split(",", 1)
            if ";base64" in header:
                mime_type = header.split(";")[0].replace("data:", "") or mime_type
            data = b64

        raw = base64.b64decode(data)
        extension = ""
        if file_name:
            extension = Path(file_name).suffix
        if not extension:
            extension = mimetypes.guess_extension(mime_type or "application/octet-stream") or ""

        stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
        media_dir = get_data_path() / "media"
        media_dir.mkdir(parents=True, exist_ok=True)

        safe_name = Path(file_name).name if file_name else f"file_{stamp}{extension}"
        if not safe_name:
            safe_name = f"file_{stamp}{extension}"

        file_path = media_dir / safe_name
        Path(file_path).write_bytes(raw)
        return str(file_path)
