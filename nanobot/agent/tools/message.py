"""Message tool for sending messages to users."""

import base64
import mimetypes
import os
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import ArraySchema, StringSchema, tool_parameters_schema
from nanobot.bus.events import OutboundMessage
from nanobot.config.paths import get_media_dir, get_workspace_path


@tool_parameters(
    tool_parameters_schema(
        content=StringSchema("The message content to send"),
        channel=StringSchema("Optional: target channel (telegram, discord, etc.)"),
        chat_id=StringSchema("Optional: target chat/user ID"),
        media=ArraySchema(
            StringSchema(""),
            description="Optional: list of file paths to attach (images, video, audio, documents)",
        ),
        image_path=StringSchema("Optional legacy single image path to attach"),
        image_base64=StringSchema("Optional legacy base64 image data or data URI to attach"),
        image_mime_type=StringSchema("Optional MIME type for image_base64 (default image/png)"),
        file_path=StringSchema("Optional legacy single file path to attach"),
        file_base64=StringSchema("Optional legacy base64 file data or data URI to attach"),
        file_name=StringSchema("Optional filename for file_base64"),
        file_mime_type=StringSchema(
            "Optional MIME type for file_base64 (default application/octet-stream)"
        ),
        title=StringSchema("Optional title metadata for channels that support it"),
        buttons=ArraySchema(
            ArraySchema(StringSchema("Button label")),
            description="Optional: inline keyboard buttons as list of rows, each row is list of button labels.",
        ),
        required=[],
    )
)
class MessageTool(Tool):
    """Tool to send messages to users on chat channels."""

    def __init__(
        self,
        send_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
        default_channel: str = "",
        default_chat_id: str = "",
        default_message_id: str | None = None,
        workspace: str | Path | None = None,
    ):
        self._send_callback = send_callback
        self._workspace = Path(workspace).expanduser() if workspace is not None else get_workspace_path()
        self._default_channel: ContextVar[str] = ContextVar("message_default_channel", default=default_channel)
        self._default_chat_id: ContextVar[str] = ContextVar("message_default_chat_id", default=default_chat_id)
        self._default_message_id: ContextVar[str | None] = ContextVar(
            "message_default_message_id",
            default=default_message_id,
        )
        self._default_metadata: ContextVar[dict[str, Any]] = ContextVar(
            "message_default_metadata",
            default={},
        )
        self._sent_in_turn_var: ContextVar[bool] = ContextVar("message_sent_in_turn", default=False)
        self._record_channel_delivery_var: ContextVar[bool] = ContextVar(
            "message_record_channel_delivery",
            default=False,
        )

    def set_context(
        self,
        channel: str,
        chat_id: str,
        message_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Set the current message context."""
        self._default_channel.set(channel)
        self._default_chat_id.set(chat_id)
        self._default_message_id.set(message_id)
        self._default_metadata.set(metadata or {})

    def set_send_callback(self, callback: Callable[[OutboundMessage], Awaitable[None]]) -> None:
        """Set the callback for sending messages."""
        self._send_callback = callback

    def start_turn(self) -> None:
        """Reset per-turn send tracking."""
        self._sent_in_turn = False

    def set_record_channel_delivery(self, active: bool):
        """Mark tool-sent messages as proactive channel deliveries."""
        return self._record_channel_delivery_var.set(active)

    def reset_record_channel_delivery(self, token) -> None:
        """Restore previous proactive delivery recording state."""
        self._record_channel_delivery_var.reset(token)

    @property
    def _sent_in_turn(self) -> bool:
        return self._sent_in_turn_var.get()

    @_sent_in_turn.setter
    def _sent_in_turn(self, value: bool) -> None:
        self._sent_in_turn_var.set(value)

    @property
    def name(self) -> str:
        return "message"

    @property
    def description(self) -> str:
        return (
            "Send a message to the user, optionally with file attachments. "
            "This is the ONLY way to deliver files (images, documents, audio, video) to the user. "
            "Use the 'media' parameter with file paths to attach files. "
            "Do NOT use read_file to send files — that only reads content for your own analysis."
        )

    async def execute(
        self,
        content: str | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
        message_id: str | None = None,
        media: list[str] | None = None,
        image_path: str | None = None,
        image_base64: str | None = None,
        image_mime_type: str | None = None,
        file_path: str | None = None,
        file_base64: str | None = None,
        file_name: str | None = None,
        file_mime_type: str | None = None,
        title: str | None = None,
        buttons: list[list[str]] | None = None,
        **kwargs: Any
    ) -> str:
        from nanobot.utils.helpers import strip_think
        content = strip_think(content or "")

        if buttons is not None:
            if not isinstance(buttons, list) or any(
                not isinstance(row, list) or any(not isinstance(label, str) for label in row)
                for row in buttons
            ):
                return "Error: buttons must be a list of list of strings"
        default_channel = self._default_channel.get()
        default_chat_id = self._default_chat_id.get()
        channel = channel or default_channel
        chat_id = chat_id or default_chat_id
        # Only inherit default message_id when targeting the same channel+chat.
        # Cross-chat sends must not carry the original message_id, because
        # some channels (e.g. Feishu) use it to determine the target
        # conversation via their Reply API, which would route the message
        # to the wrong chat entirely.
        same_target = channel == default_channel and chat_id == default_chat_id
        if same_target:
            message_id = message_id or self._default_message_id.get()
        else:
            message_id = None

        if not channel or not chat_id:
            return "Error: No target channel/chat specified"

        if not self._send_callback:
            return "Error: Message sending not configured"

        media = list(media or [])
        if image_path:
            media.append(image_path)
        if file_path:
            media.append(file_path)
        if image_base64:
            try:
                media.append(self._save_base64_media(image_base64, None, image_mime_type or "image/png"))
            except Exception as e:
                return f"Error: failed to save base64 image: {str(e)}"
        if file_base64:
            try:
                media.append(
                    self._save_base64_media(
                        file_base64,
                        file_name,
                        file_mime_type or "application/octet-stream",
                    )
                )
            except Exception as e:
                return f"Error: failed to save base64 file: {str(e)}"

        resolved = []
        for p in media:
            if p.startswith(("http://", "https://")) or os.path.isabs(p):
                resolved.append(p)
            else:
                resolved.append(str(self._workspace / p))
        media = resolved

        if not content.strip() and not media:
            return "Error: No content or media provided"

        metadata = dict(self._default_metadata.get()) if same_target else {}
        if message_id:
            metadata["message_id"] = message_id
        if title:
            metadata["title"] = title
        if self._record_channel_delivery_var.get():
            metadata["_record_channel_delivery"] = True

        msg = OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=content,
            media=media or [],
            buttons=buttons or [],
            metadata=metadata,
        )

        try:
            await self._send_callback(msg)
            if channel == default_channel and chat_id == default_chat_id:
                self._sent_in_turn = True
            media_info = f" with {len(media)} attachments" if media else ""
            button_info = f" with {sum(len(row) for row in buttons)} button(s)" if buttons else ""
            return f"Message sent to {channel}:{chat_id}{media_info}{button_info}"
        except Exception as e:
            return f"Error sending message: {str(e)}"

    @staticmethod
    def _split_data_uri(data: str, mime_type: str) -> tuple[str, str]:
        if not data.startswith("data:"):
            return data, mime_type
        header, b64 = data.split(",", 1)
        if ";base64" in header:
            detected = header.split(";", 1)[0].replace("data:", "")
            if detected:
                mime_type = detected
        return b64, mime_type

    def _save_base64_media(
        self,
        data: str,
        file_name: str | None,
        mime_type: str,
    ) -> str:
        b64, mime_type = self._split_data_uri(data, mime_type)
        raw = base64.b64decode(b64)
        extension = Path(file_name).suffix if file_name else ""
        if not extension:
            extension = mimetypes.guess_extension(mime_type) or ""
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        safe_name = Path(file_name).name if file_name else f"outbound_{stamp}{extension}"
        if not safe_name:
            safe_name = f"outbound_{stamp}{extension}"
        target = get_media_dir("outbound") / safe_name
        target.write_bytes(raw)
        return str(target)
