from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import GenerationSettings


@pytest.mark.asyncio
async def test_streaming_progress_events_share_stream_id_with_delta(tmp_path: Path) -> None:
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings(max_tokens=128)
    loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path, model="test-model")

    async def fake_process_message(
        msg: InboundMessage,
        *,
        on_progress=None,
        on_stream=None,
        on_stream_end=None,
        pending_queue=None,
        **_kwargs,
    ) -> OutboundMessage:
        assert on_progress is not None
        assert on_stream is not None
        assert on_stream_end is not None
        await on_progress(
            "",
            tool_events=[{
                "version": 1,
                "phase": "usage",
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            }],
        )
        await on_stream("hello")
        await on_stream_end(resuming=False)
        meta = dict(msg.metadata)
        meta["_streamed"] = True
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content="hello",
            metadata=meta,
        )

    loop._process_message = fake_process_message  # type: ignore[method-assign]

    await loop._dispatch(
        InboundMessage(
            channel="feishu",
            sender_id="ou_user",
            chat_id="ou_chat",
            content="hi",
            metadata={"message_id": "om_1", "_wants_stream": True},
        )
    )

    progress = await bus.consume_outbound()
    delta = await bus.consume_outbound()
    stream_end = await bus.consume_outbound()
    final = await bus.consume_outbound()

    assert progress.metadata["_progress"] is True
    assert progress.metadata["_stream_id"]
    assert progress.metadata["_stream_id"] == delta.metadata["_stream_id"]
    assert stream_end.metadata["_stream_id"] == delta.metadata["_stream_id"]
    assert delta.metadata["_source_title"] == "Main Session 消息"
    assert delta.metadata["_source_label"] == "Main Session · sessions/feishu_ou_chat.jsonl"
    assert progress.metadata["_source_label"] == delta.metadata["_source_label"]
    assert final.metadata["_streamed"] is True


@pytest.mark.asyncio
async def test_resuming_stream_keeps_one_stream_id_across_tool_rounds(tmp_path: Path) -> None:
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings(max_tokens=128)
    loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path, model="test-model")

    async def fake_process_message(
        msg: InboundMessage,
        *,
        on_progress=None,
        on_stream=None,
        on_stream_end=None,
        pending_queue=None,
        **_kwargs,
    ) -> OutboundMessage:
        assert on_progress is not None
        assert on_stream is not None
        assert on_stream_end is not None
        await on_progress(
            "",
            tool_events=[{
                "version": 1,
                "phase": "usage",
                "usage": {"prompt_tokens": 5, "completion_tokens": 1},
            }],
        )
        await on_stream("first")
        await on_stream_end(resuming=True)
        await on_progress(
            "",
            tool_events=[{
                "version": 1,
                "phase": "usage",
                "usage": {"prompt_tokens": 7, "completion_tokens": 2},
            }],
        )
        await on_stream("second")
        await on_stream_end(resuming=False)
        meta = dict(msg.metadata)
        meta["_streamed"] = True
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content="first\nsecond",
            metadata=meta,
        )

    loop._process_message = fake_process_message  # type: ignore[method-assign]

    await loop._dispatch(
        InboundMessage(
            channel="feishu",
            sender_id="ou_user",
            chat_id="ou_chat",
            content="hi",
            metadata={"message_id": "om_1", "_wants_stream": True},
        )
    )

    progress1 = await bus.consume_outbound()
    delta1 = await bus.consume_outbound()
    resuming_end = await bus.consume_outbound()
    progress2 = await bus.consume_outbound()
    delta2 = await bus.consume_outbound()
    final_end = await bus.consume_outbound()
    final = await bus.consume_outbound()

    stream_id = delta1.metadata["_stream_id"]
    assert progress1.metadata["_stream_id"] == stream_id
    assert resuming_end.metadata["_stream_id"] == stream_id
    assert progress2.metadata["_stream_id"] == stream_id
    assert delta2.metadata["_stream_id"] == stream_id
    assert final_end.metadata["_stream_id"] == stream_id
    assert delta1.metadata["_source_label"] == "Main Session · sessions/feishu_ou_chat.jsonl"
    assert delta2.metadata["_source_label"] == delta1.metadata["_source_label"]
    assert resuming_end.metadata["_resuming"] is True
    assert final_end.metadata["_resuming"] is False
    assert final.metadata["_streamed"] is True
