import base64
import os

import pytest

from nanobot.agent.tools.message import MessageTool
from nanobot.bus.events import OutboundMessage
from nanobot.config.paths import get_workspace_path


@pytest.mark.asyncio
async def test_message_tool_returns_error_when_no_target_context() -> None:
    tool = MessageTool()
    result = await tool.execute(content="test")
    assert result == "Error: No target channel/chat specified"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        "not a list",
        [["ok"], "row-not-a-list"],
        [["ok", 42]],
        [[None]],
    ],
)
async def test_message_tool_rejects_malformed_buttons(bad) -> None:
    """``buttons`` must be ``list[list[str]]``; the tool validates the shape
    up front so a malformed LLM payload errors visibly instead of slipping
    into the channel layer where Telegram would silently reject the frame."""
    tool = MessageTool()
    result = await tool.execute(
        content="hi", channel="telegram", chat_id="1", buttons=bad,
    )
    assert result == "Error: buttons must be a list of list of strings"


@pytest.mark.asyncio
async def test_message_tool_marks_channel_delivery_only_when_enabled() -> None:
    sent: list[OutboundMessage] = []

    async def _send(msg: OutboundMessage) -> None:
        sent.append(msg)

    tool = MessageTool(send_callback=_send)

    await tool.execute(content="normal", channel="telegram", chat_id="1")
    token = tool.set_record_channel_delivery(True)
    try:
        await tool.execute(content="cron", channel="telegram", chat_id="1")
    finally:
        tool.reset_record_channel_delivery(token)

    assert sent[0].metadata == {}
    assert sent[1].metadata == {"_record_channel_delivery": True}


@pytest.mark.asyncio
async def test_message_tool_inherits_metadata_for_same_target() -> None:
    sent: list[OutboundMessage] = []

    async def _send(msg: OutboundMessage) -> None:
        sent.append(msg)

    tool = MessageTool(send_callback=_send)
    slack_meta = {"slack": {"thread_ts": "111.222", "channel_type": "channel"}}
    tool.set_context("slack", "C123", metadata=slack_meta)

    await tool.execute(content="thread reply")

    assert sent[0].metadata == slack_meta


@pytest.mark.asyncio
async def test_message_tool_does_not_inherit_metadata_for_cross_target() -> None:
    sent: list[OutboundMessage] = []

    async def _send(msg: OutboundMessage) -> None:
        sent.append(msg)

    tool = MessageTool(send_callback=_send)
    tool.set_context(
        "slack",
        "C123",
        metadata={"slack": {"thread_ts": "111.222", "channel_type": "channel"}},
    )

    await tool.execute(content="channel reply", channel="slack", chat_id="C999")

    assert sent[0].metadata == {}


@pytest.mark.asyncio
async def test_message_tool_resolves_relative_media_paths() -> None:
    sent: list[OutboundMessage] = []

    async def _send(msg: OutboundMessage) -> None:
        sent.append(msg)

    tool = MessageTool(send_callback=_send)

    await tool.execute(
        content="see attached",
        channel="telegram",
        chat_id="1",
        media=["output/image.png"],
    )

    expected = str(get_workspace_path() / "output/image.png")
    assert sent[0].media == [expected]


@pytest.mark.asyncio
async def test_message_tool_resolves_relative_media_paths_from_active_workspace(tmp_path) -> None:
    sent: list[OutboundMessage] = []

    async def _send(msg: OutboundMessage) -> None:
        sent.append(msg)

    workspace = tmp_path / "workspace"
    tool = MessageTool(send_callback=_send, workspace=workspace)

    await tool.execute(
        content="see attached",
        channel="telegram",
        chat_id="1",
        media=["output/image.png"],
    )

    assert sent[0].media == [str(workspace / "output/image.png")]


@pytest.mark.asyncio
async def test_message_tool_passes_through_absolute_media_paths() -> None:
    sent: list[OutboundMessage] = []

    async def _send(msg: OutboundMessage) -> None:
        sent.append(msg)

    tool = MessageTool(send_callback=_send)

    abs_path = os.path.abspath(os.path.join(os.sep, "tmp", "abs_image.png"))

    await tool.execute(
        content="see attached",
        channel="telegram",
        chat_id="1",
        media=[abs_path],
    )

    assert sent[0].media == [abs_path]


@pytest.mark.asyncio
async def test_message_tool_passes_through_url_media_paths() -> None:
    sent: list[OutboundMessage] = []

    async def _send(msg: OutboundMessage) -> None:
        sent.append(msg)

    tool = MessageTool(send_callback=_send)

    url = "https://example.com/image.png"

    await tool.execute(
        content="see attached",
        channel="telegram",
        chat_id="1",
        media=[url],
    )

    assert sent[0].media == [url]


@pytest.mark.asyncio
async def test_message_tool_allows_media_without_content() -> None:
    sent: list[OutboundMessage] = []

    async def _send(msg: OutboundMessage) -> None:
        sent.append(msg)

    tool = MessageTool(send_callback=_send)

    result = await tool.execute(
        channel="telegram",
        chat_id="1",
        image_path="/tmp/generated.png",
        title="Generated image",
    )

    assert result == "Message sent to telegram:1 with 1 attachments"
    assert sent[0].content == ""
    assert sent[0].media == ["/tmp/generated.png"]
    assert sent[0].metadata["title"] == "Generated image"


@pytest.mark.asyncio
async def test_message_tool_saves_base64_image_media(monkeypatch, tmp_path) -> None:
    sent: list[OutboundMessage] = []

    async def _send(msg: OutboundMessage) -> None:
        sent.append(msg)

    media_root = tmp_path / "media"
    (media_root / "outbound").mkdir(parents=True)
    monkeypatch.setattr(
        "nanobot.agent.tools.message.get_media_dir",
        lambda kind: media_root / kind,
    )
    tool = MessageTool(send_callback=_send)
    image_b64 = base64.b64encode(b"png-bytes").decode("ascii")

    result = await tool.execute(
        channel="telegram",
        chat_id="1",
        image_base64=f"data:image/png;base64,{image_b64}",
    )

    assert result == "Message sent to telegram:1 with 1 attachments"
    assert len(sent[0].media) == 1
    saved = sent[0].media[0]
    assert saved.startswith(str(media_root / "outbound"))
    assert os.path.exists(saved)
    assert open(saved, "rb").read() == b"png-bytes"


@pytest.mark.asyncio
async def test_message_tool_resolves_mixed_media_paths() -> None:
    sent: list[OutboundMessage] = []

    async def _send(msg: OutboundMessage) -> None:
        sent.append(msg)

    tool = MessageTool(send_callback=_send)

    abs_path = os.path.abspath(os.path.join(os.sep, "tmp", "absolute.png"))

    await tool.execute(
        content="see attached",
        channel="telegram",
        chat_id="1",
        media=[
            "output/relative.png",
            abs_path,
            "https://example.com/url.png",
            "http://example.com/http.png",
        ],
    )

    expected_relative = str(get_workspace_path() / "output/relative.png")
    assert sent[0].media == [
        expected_relative,
        abs_path,
        "https://example.com/url.png",
        "http://example.com/http.png",
    ]
