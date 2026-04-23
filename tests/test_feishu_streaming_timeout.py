import asyncio

from nanobot.agent.loop import AgentLoop
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import FeishuConfig
from nanobot.session.manager import Session


class _DummySessions:
    def __init__(self) -> None:
        self.saved = 0

    def save(self, _session: Session) -> None:
        self.saved += 1


def _make_loop() -> AgentLoop:
    loop = AgentLoop.__new__(AgentLoop)
    loop.bus = MessageBus()
    loop.feishu_config = FeishuConfig(
        streaming_enabled=True,
        streaming_print_frequency_ms_default=20,
        streaming_print_step_default=1,
    )
    loop.sessions = _DummySessions()
    loop._build_stream_id = lambda _msg: "test-stream-id"
    return loop


def _collect_outbound(bus: MessageBus) -> list:
    items = []
    while not bus.outbound.empty():
        items.append(bus.outbound.get_nowait())
    return items


def test_streaming_timeout_fallback_to_regular_message(monkeypatch) -> None:
    loop = _make_loop()
    msg = InboundMessage(channel="feishu", sender_id="u1", chat_id="c1", content="hello")
    final_content = "A" * 100
    token_monitor = {"chart": {"type": "bar", "data": {"values": []}}}

    # First call for stream start, second call in loop triggers timeout.
    times = iter([0.0, 481.0, 482.0])
    monkeypatch.setattr("nanobot.agent.loop.time.time", lambda: next(times))

    asyncio.run(
        loop._publish_feishu_streaming_response(
            msg=msg,
            final_content=final_content,
            token_monitor=token_monitor,
            stream_id="s1",
            send_init=False,
        )
    )

    outbound = _collect_outbound(loop.bus)
    assert len(outbound) == 3

    finalize = outbound[0]
    assert finalize.metadata["feishu_stream"]["action"] == "finalize"
    assert finalize.metadata["feishu_stream"]["full_text"] == "A" * 20

    notice = outbound[1]
    assert "feishu_stream" not in notice.metadata
    assert "超出流式窗口限制" in notice.content

    remaining = outbound[2]
    assert "feishu_stream" not in remaining.metadata
    assert remaining.content == final_content


def test_streaming_within_window_keeps_finalize_flow(monkeypatch) -> None:
    loop = _make_loop()
    msg = InboundMessage(channel="feishu", sender_id="u1", chat_id="c1", content="hello")
    final_content = "B" * 45
    token_monitor = {"chart": {"type": "bar", "data": {"values": []}}}

    times = iter([0.0, 1.0, 2.0, 3.0])
    monkeypatch.setattr("nanobot.agent.loop.time.time", lambda: next(times))

    asyncio.run(
        loop._publish_feishu_streaming_response(
            msg=msg,
            final_content=final_content,
            token_monitor=token_monitor,
            stream_id="s2",
            send_init=False,
        )
    )

    outbound = _collect_outbound(loop.bus)
    assert len(outbound) == 4
    assert outbound[0].metadata["feishu_stream"]["action"] == "append"
    assert outbound[1].metadata["feishu_stream"]["action"] == "append"
    assert outbound[2].metadata["feishu_stream"]["action"] == "append"
    assert outbound[3].metadata["feishu_stream"]["action"] == "finalize"
    assert outbound[3].metadata["feishu_stream"]["full_text"] == final_content


def test_timeout_uses_stream_init_baseline(monkeypatch) -> None:
    loop = _make_loop()
    msg = InboundMessage(channel="feishu", sender_id="u1", chat_id="c1", content="hello")
    final_content = "C" * 60
    token_monitor = {"chart": {"type": "bar", "data": {"values": []}}}

    # Simulate entering final-answer stage when stream has already been open for >8 minutes.
    times = iter([1000.0, 1001.0])
    monkeypatch.setattr("nanobot.agent.loop.time.time", lambda: next(times))

    asyncio.run(
        loop._publish_feishu_streaming_response(
            msg=msg,
            final_content=final_content,
            token_monitor=token_monitor,
            stream_id="s3",
            send_init=False,
            stream_started_at=500.0,
        )
    )

    outbound = _collect_outbound(loop.bus)
    assert len(outbound) == 3
    assert outbound[0].metadata["feishu_stream"]["action"] == "finalize"
    assert outbound[1].content.startswith("当前回答已超出流式窗口限制")
    assert outbound[2].content == final_content


def test_timeout_fallback_persists_session_events_without_context_pollution(monkeypatch) -> None:
    loop = _make_loop()
    msg = InboundMessage(channel="feishu", sender_id="u1", chat_id="c1", content="hello")
    final_content = "D" * 50
    token_monitor = {"chart": {"type": "bar", "data": {"values": []}}}
    session = Session(key="feishu:c1")

    # The full assistant answer is already persisted before streaming starts.
    session.add_message("assistant", final_content)

    times = iter([0.0, 481.0, 482.0])
    monkeypatch.setattr("nanobot.agent.loop.time.time", lambda: next(times))

    asyncio.run(
        loop._publish_feishu_streaming_response(
            msg=msg,
            final_content=final_content,
            token_monitor=token_monitor,
            stream_id="s4",
            send_init=False,
            session=session,
        )
    )

    timeout_events = [m for m in session.messages if m.get("event_type") in {"streaming_timeout", "timeout_fallback"}]
    assert len(timeout_events) == 2
    assert timeout_events[0]["event_type"] == "streaming_timeout"
    assert timeout_events[1]["event_type"] == "timeout_fallback"
    assert all(m.get("include_in_context") is False for m in timeout_events)
    assert timeout_events[1]["content"] == final_content
    assert loop.sessions.saved == 2

    history = session.get_history(max_messages=20)
    history_contents = [m["content"] for m in history if m["role"] == "assistant"]
    assert history_contents.count(final_content) == 1
    assert all("超出流式窗口限制" not in content for content in history_contents if isinstance(content, str))
