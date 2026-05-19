"""Tests for Feishu streaming (send_delta) via CardKit streaming API."""
import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.feishu import FeishuChannel, FeishuConfig, _FeishuStreamBuf


def _make_channel(streaming: bool = True, reply_to_message: bool = False) -> FeishuChannel:
    config = FeishuConfig(
        enabled=True,
        app_id="cli_test",
        app_secret="secret",
        allow_from=["*"],
        streaming=streaming,
        reply_to_message=reply_to_message,
    )
    ch = FeishuChannel(config, MessageBus())
    ch._client = MagicMock()
    ch._loop = None
    return ch


def _mock_create_card_response(card_id: str = "card_stream_001"):
    resp = MagicMock()
    resp.success.return_value = True
    resp.data = SimpleNamespace(card_id=card_id)
    return resp


def _mock_send_response(message_id: str = "om_stream_001"):
    resp = MagicMock()
    resp.success.return_value = True
    resp.data = SimpleNamespace(message_id=message_id)
    return resp


def _mock_content_response(success: bool = True):
    resp = MagicMock()
    resp.success.return_value = success
    resp.code = 0 if success else 99999
    resp.msg = "ok" if success else "error"
    return resp


class TestFeishuStreamingConfig:
    def test_streaming_default_true(self):
        assert FeishuConfig().streaming is True

    def test_supports_streaming_when_enabled(self):
        ch = _make_channel(streaming=True)
        assert ch.supports_streaming is True

    def test_supports_streaming_disabled(self):
        ch = _make_channel(streaming=False)
        assert ch.supports_streaming is False


class TestCreateStreamingCard:
    def test_returns_card_id_on_success(self):
        ch = _make_channel()
        ch._client.cardkit.v1.card.create.return_value = _mock_create_card_response("card_123")
        ch._client.im.v1.message.create.return_value = _mock_send_response()
        result = ch._create_streaming_card_sync("chat_id", "oc_chat1")
        assert result == "card_123"
        ch._client.cardkit.v1.card.create.assert_called_once()
        ch._client.im.v1.message.create.assert_called_once()

    def test_card_payload_includes_code_built_observability_panel(self):
        ch = _make_channel()
        ch._client.cardkit.v1.card.create.return_value = _mock_create_card_response("card_123")
        ch._client.im.v1.message.create.return_value = _mock_send_response()

        result = ch._create_streaming_card_sync("chat_id", "oc_chat1")

        assert result == "card_123"
        request = ch._client.cardkit.v1.card.create.call_args[0][0]
        payload = json.loads(request.body.data)
        elements = payload["body"]["elements"]
        assert elements[0]["tag"] == "collapsible_panel"
        assert elements[0]["background_color"] == "grey"
        assert "工具调用详情" in elements[0]["header"]["title"]["content"]
        assert elements[0]["elements"][0]["element_id"] == "tool_logs_markdown"
        assert elements[0]["elements"][0]["text_size"] == "normal"
        assert elements[1]["element_id"] == "streaming_md"
        chart = [el for el in elements if el.get("element_id") == "token_budget_chart"]
        assert chart, "streaming card should include a token chart element"
        assert chart[0]["height"] == "56px"

    def test_returns_none_on_failure(self):
        ch = _make_channel()
        resp = MagicMock()
        resp.success.return_value = False
        resp.code = 99999
        resp.msg = "error"
        ch._client.cardkit.v1.card.create.return_value = resp
        assert ch._create_streaming_card_sync("chat_id", "oc_chat1") is None

    def test_returns_none_on_exception(self):
        ch = _make_channel()
        ch._client.cardkit.v1.card.create.side_effect = RuntimeError("network")
        assert ch._create_streaming_card_sync("chat_id", "oc_chat1") is None

    def test_returns_none_when_card_send_fails(self):
        ch = _make_channel()
        ch._client.cardkit.v1.card.create.return_value = _mock_create_card_response("card_123")
        resp = MagicMock()
        resp.success.return_value = False
        resp.code = 99999
        resp.msg = "error"
        resp.get_log_id.return_value = "log1"
        ch._client.im.v1.message.create.return_value = resp
        assert ch._create_streaming_card_sync("chat_id", "oc_chat1") is None


class TestCloseStreamingMode:
    def test_returns_true_on_success(self):
        ch = _make_channel()
        ch._client.cardkit.v1.card.settings.return_value = _mock_content_response(True)
        assert ch._close_streaming_mode_sync("card_1", 10) is True

    def test_returns_false_on_failure(self):
        ch = _make_channel()
        ch._client.cardkit.v1.card.settings.return_value = _mock_content_response(False)
        assert ch._close_streaming_mode_sync("card_1", 10) is False

    def test_returns_false_on_exception(self):
        ch = _make_channel()
        ch._client.cardkit.v1.card.settings.side_effect = RuntimeError("err")
        assert ch._close_streaming_mode_sync("card_1", 10) is False


class TestStreamUpdateText:
    def test_returns_true_on_success(self):
        ch = _make_channel()
        ch._client.cardkit.v1.card_element.content.return_value = _mock_content_response(True)
        assert ch._stream_update_text_sync("card_1", "hello", 1) is True

    def test_returns_false_on_failure(self):
        ch = _make_channel()
        ch._client.cardkit.v1.card_element.content.return_value = _mock_content_response(False)
        assert ch._stream_update_text_sync("card_1", "hello", 1) is False

    def test_returns_false_on_exception(self):
        ch = _make_channel()
        ch._client.cardkit.v1.card_element.content.side_effect = RuntimeError("err")
        assert ch._stream_update_text_sync("card_1", "hello", 1) is False

    def test_lifts_indented_code_fences_before_stream_update(self):
        ch = _make_channel()
        ch._client.cardkit.v1.card_element.content.return_value = _mock_content_response(True)

        ok = ch._stream_update_text_sync(
            "card_1",
            "2. task\n   ```text\n   official image namespace\n   instance branch\n   ```",
            1,
        )

        assert ok is True
        request = ch._client.cardkit.v1.card_element.content.call_args[0][0]
        assert request.body.content == (
            "2. task\n"
            "```text\n"
            "official image namespace\n"
            "instance branch\n"
            "```"
        )


class TestSendDelta:
    @pytest.mark.asyncio
    async def test_first_delta_creates_card_and_sends(self):
        ch = _make_channel()
        ch._client.cardkit.v1.card.create.return_value = _mock_create_card_response("card_new")
        ch._client.im.v1.message.create.return_value = _mock_send_response("om_new")
        ch._client.cardkit.v1.card_element.content.return_value = _mock_content_response()

        await ch.send_delta("oc_chat1", "Hello ")

        assert "oc_chat1" in ch._stream_bufs
        buf = ch._stream_bufs["oc_chat1"]
        assert buf.text == "Hello "
        assert buf.card_id == "card_new"
        assert buf.sequence == 1
        ch._client.cardkit.v1.card.create.assert_called_once()
        ch._client.im.v1.message.create.assert_called_once()
        ch._client.cardkit.v1.card_element.content.assert_called_once()

    @pytest.mark.asyncio
    async def test_non_stream_final_message_includes_buffered_token_chart(self):
        ch = _make_channel(streaming=False)
        ch._send_message_sync = MagicMock(return_value="om_final")
        ch._stream_bufs["om_input"] = _FeishuStreamBuf(
            token_usage={
                "prompt_tokens": 1200,
                "completion_tokens": 80,
                "total_tokens": 1280,
                "cached_tokens": 1024,
            }
        )

        await ch.send(OutboundMessage(
            channel="feishu",
            chat_id="ou_user",
            content="final answer",
            metadata={"message_id": "om_input"},
        ))

        ch._send_message_sync.assert_called_once()
        _, _, msg_type, raw_card = ch._send_message_sync.call_args[0]
        assert msg_type == "interactive"
        card = json.loads(raw_card)
        chart = [el for el in card["elements"] if el.get("element_id") == "token_budget_chart"]
        assert chart
        values = chart[0]["chart_spec"]["data"]["values"]
        assert {"category": "token用量", "item": "缓存输入", "value": 1024} in values
        assert "om_input" not in ch._stream_bufs

    @pytest.mark.asyncio
    async def test_group_delta_uses_create_when_reply_disabled(self):
        ch = _make_channel(reply_to_message=False)
        ch._client.cardkit.v1.card.create.return_value = _mock_create_card_response("card_new")
        ch._client.im.v1.message.create.return_value = _mock_send_response("om_new")
        ch._client.cardkit.v1.card_element.content.return_value = _mock_content_response()

        await ch.send_delta(
            "oc_chat1",
            "Hello ",
            metadata={"message_id": "om_001", "chat_type": "group"},
        )

        ch._client.im.v1.message.create.assert_called_once()
        ch._client.im.v1.message.reply.assert_not_called()

    @pytest.mark.asyncio
    async def test_group_delta_keeps_existing_topic_when_reply_disabled(self):
        ch = _make_channel(reply_to_message=False)
        ch._client.cardkit.v1.card.create.return_value = _mock_create_card_response("card_new")
        reply_resp = MagicMock()
        reply_resp.success.return_value = True
        ch._client.im.v1.message.reply.return_value = reply_resp
        ch._client.cardkit.v1.card_element.content.return_value = _mock_content_response()

        await ch.send_delta(
            "oc_chat1",
            "Hello ",
            metadata={"message_id": "om_001", "chat_type": "group", "thread_id": "ot_001"},
        )

        ch._client.im.v1.message.reply.assert_called_once()
        ch._client.im.v1.message.create.assert_not_called()
        request = ch._client.im.v1.message.reply.call_args[0][0]
        assert request.request_body.reply_in_thread is not True

    @pytest.mark.asyncio
    async def test_group_delta_replies_in_thread_when_reply_enabled(self):
        ch = _make_channel(reply_to_message=True)
        ch._client.cardkit.v1.card.create.return_value = _mock_create_card_response("card_new")
        reply_resp = MagicMock()
        reply_resp.success.return_value = True
        ch._client.im.v1.message.reply.return_value = reply_resp
        ch._client.cardkit.v1.card_element.content.return_value = _mock_content_response()

        await ch.send_delta(
            "oc_chat1",
            "Hello ",
            metadata={"message_id": "om_001", "chat_type": "group"},
        )

        ch._client.im.v1.message.reply.assert_called_once()
        ch._client.im.v1.message.create.assert_not_called()
        request = ch._client.im.v1.message.reply.call_args[0][0]
        assert request.request_body.reply_in_thread is True

    @pytest.mark.asyncio
    async def test_second_delta_within_interval_skips_update(self):
        ch = _make_channel()
        buf = _FeishuStreamBuf(text="Hello ", card_id="card_1", sequence=1, last_edit=time.monotonic())
        ch._stream_bufs["oc_chat1"] = buf

        await ch.send_delta("oc_chat1", "world")

        assert buf.text == "Hello world"
        ch._client.cardkit.v1.card_element.content.assert_not_called()

    @pytest.mark.asyncio
    async def test_delta_after_interval_updates_text(self):
        ch = _make_channel()
        buf = _FeishuStreamBuf(text="Hello ", card_id="card_1", sequence=1, last_edit=time.monotonic() - 1.0)
        ch._stream_bufs["oc_chat1"] = buf

        ch._client.cardkit.v1.card_element.content.return_value = _mock_content_response()
        await ch.send_delta("oc_chat1", "world")

        assert buf.text == "Hello world"
        assert buf.sequence == 2
        ch._client.cardkit.v1.card_element.content.assert_called_once()

    @pytest.mark.asyncio
    async def test_stream_end_sends_final_update(self):
        ch = _make_channel()
        ch._stream_bufs["oc_chat1"] = _FeishuStreamBuf(
            text="Final content", card_id="card_1", sequence=3, last_edit=0.0,
        )
        ch._client.cardkit.v1.card_element.content.return_value = _mock_content_response()
        ch._client.cardkit.v1.card.settings.return_value = _mock_content_response()

        await ch.send_delta("oc_chat1", "", metadata={"_stream_end": True})

        assert "oc_chat1" not in ch._stream_bufs
        ch._client.cardkit.v1.card_element.content.assert_called_once()
        ch._client.cardkit.v1.card.settings.assert_called_once()
        settings_call = ch._client.cardkit.v1.card.settings.call_args[0][0]
        assert settings_call.body.sequence == 5  # after final content seq 4

    @pytest.mark.asyncio
    async def test_stream_end_fallback_when_no_card_id(self):
        """If card creation failed, stream_end falls back to a plain card message."""
        ch = _make_channel()
        ch._stream_bufs["oc_chat1"] = _FeishuStreamBuf(
            text="Fallback content", card_id=None, sequence=0, last_edit=0.0,
        )
        ch._client.im.v1.message.create.return_value = _mock_send_response("om_fb")

        await ch.send_delta("oc_chat1", "", metadata={"_stream_end": True})

        assert "oc_chat1" not in ch._stream_bufs
        ch._client.cardkit.v1.card_element.content.assert_not_called()
        ch._client.im.v1.message.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_stream_end_fallback_group_uses_create_when_reply_disabled(self):
        ch = _make_channel(reply_to_message=False)
        ch._stream_bufs["om_001"] = _FeishuStreamBuf(
            text="Fallback content", card_id=None, sequence=0, last_edit=0.0,
        )
        ch._client.im.v1.message.create.return_value = _mock_send_response("om_fb")

        await ch.send_delta(
            "oc_chat1",
            "",
            metadata={"_stream_end": True, "message_id": "om_001", "chat_type": "group"},
        )

        ch._client.im.v1.message.create.assert_called_once()
        ch._client.im.v1.message.reply.assert_not_called()

    @pytest.mark.asyncio
    async def test_stream_end_fallback_keeps_existing_topic_when_reply_disabled(self):
        ch = _make_channel(reply_to_message=False)
        ch._stream_bufs["om_001"] = _FeishuStreamBuf(
            text="Fallback content", card_id=None, sequence=0, last_edit=0.0,
        )
        reply_resp = MagicMock()
        reply_resp.success.return_value = True
        ch._client.im.v1.message.reply.return_value = reply_resp

        await ch.send_delta(
            "oc_chat1",
            "",
            metadata={
                "_stream_end": True,
                "message_id": "om_001",
                "chat_type": "group",
                "thread_id": "ot_001",
            },
        )

        ch._client.im.v1.message.reply.assert_called_once()
        ch._client.im.v1.message.create.assert_not_called()
        request = ch._client.im.v1.message.reply.call_args[0][0]
        assert request.request_body.reply_in_thread is not True

    @pytest.mark.asyncio
    async def test_stream_end_fallback_group_replies_when_reply_enabled(self):
        ch = _make_channel(reply_to_message=True)
        ch._stream_bufs["om_001"] = _FeishuStreamBuf(
            text="Fallback content", card_id=None, sequence=0, last_edit=0.0,
        )
        reply_resp = MagicMock()
        reply_resp.success.return_value = True
        ch._client.im.v1.message.reply.return_value = reply_resp

        await ch.send_delta(
            "oc_chat1",
            "",
            metadata={"_stream_end": True, "message_id": "om_001", "chat_type": "group"},
        )

        ch._client.im.v1.message.reply.assert_called_once()
        ch._client.im.v1.message.create.assert_not_called()
        request = ch._client.im.v1.message.reply.call_args[0][0]
        assert request.request_body.reply_in_thread is True

    @pytest.mark.asyncio
    async def test_stream_end_fallback_when_final_update_fails(self):
        """If streaming mode was closed (e.g. Feishu timeout), fall back to a regular card."""
        ch = _make_channel()
        ch._stream_bufs["oc_chat1"] = _FeishuStreamBuf(
            text="Lost content", card_id="card_1", sequence=3, last_edit=0.0,
        )
        ch._client.cardkit.v1.card_element.content.return_value = _mock_content_response(success=False)
        ch._client.im.v1.message.create.return_value = _mock_send_response("om_fb")

        await ch.send_delta("oc_chat1", "", metadata={"_stream_end": True})

        assert "oc_chat1" not in ch._stream_bufs
        # Should NOT attempt to close streaming mode since update failed
        ch._client.cardkit.v1.card.settings.assert_not_called()
        # Should fall back to sending a regular interactive card
        ch._client.im.v1.message.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_stream_end_without_buf_is_noop(self):
        ch = _make_channel()
        await ch.send_delta("oc_chat1", "", metadata={"_stream_end": True})
        ch._client.cardkit.v1.card_element.content.assert_not_called()

    @pytest.mark.asyncio
    async def test_stream_end_closes_tool_only_card(self):
        ch = _make_channel()
        ch._stream_bufs["oc_chat1"] = _FeishuStreamBuf(
            text="",
            card_id="card_tool_only",
            sequence=4,
            last_edit=0.0,
            tool_events=[{"phase": "start", "name": "web_search", "arguments": {"query": "x"}}],
        )
        ch._client.cardkit.v1.card.settings.return_value = _mock_content_response()

        await ch.send_delta("oc_chat1", "", metadata={"_stream_end": True})

        assert "oc_chat1" not in ch._stream_bufs
        ch._client.cardkit.v1.card_element.content.assert_not_called()
        ch._client.cardkit.v1.card.settings.assert_called_once()

    @pytest.mark.asyncio
    async def test_resuming_stream_end_preserves_tool_events_across_rounds(self):
        ch = _make_channel()
        buf = _FeishuStreamBuf(
            text="Round one answer",
            card_id="card_1",
            sequence=3,
            last_edit=0.0,
            tool_events=[{
                "phase": "end",
                "call_id": "round1",
                "name": "web_search",
                "arguments": {"query": "first"},
                "result": "first result",
            }],
        )
        ch._stream_bufs["oc_chat1"] = buf
        ch._client.cardkit.v1.card_element.content.return_value = _mock_content_response()

        await ch.send_delta(
            "oc_chat1",
            "",
            metadata={"_stream_end": True, "_resuming": True},
        )

        assert ch._stream_bufs["oc_chat1"] is buf
        ch._client.cardkit.v1.card.settings.assert_not_called()

        await ch.send_delta(
            "oc_chat1",
            "",
            metadata={
                "_tool_events": [{
                    "phase": "end",
                    "call_id": "round2",
                    "name": "grep",
                    "arguments": {"pattern": "second"},
                    "result": "second result",
                }],
            },
        )

        assert len(buf.tool_events) == 2
        request = ch._client.cardkit.v1.card_element.content.call_args[0][0]
        assert request.element_id == "tool_logs_markdown"
        assert "网络搜索 (`web_search`)" in request.body.content
        assert "搜索文件 (`grep`)" in request.body.content
        assert "first result" in request.body.content
        assert "second result" in request.body.content

    @pytest.mark.asyncio
    async def test_resuming_stream_end_inserts_separator_before_next_answer_segment(self):
        ch = _make_channel()
        buf = _FeishuStreamBuf(
            text="First answer segment",
            card_id="card_1",
            sequence=3,
            last_edit=0.0,
        )
        ch._stream_bufs["oc_chat1"] = buf
        ch._client.cardkit.v1.card_element.content.return_value = _mock_content_response()

        await ch.send_delta(
            "oc_chat1",
            "",
            metadata={"_stream_end": True, "_resuming": True},
        )

        assert buf.needs_segment_separator is True

        await ch.send_delta(
            "oc_chat1",
            "  Second answer segment",
            metadata={"_stream_delta": True},
        )

        assert buf.needs_segment_separator is False
        assert buf.text == "First answer segment\n\n---\n\nSecond answer segment"

    @pytest.mark.asyncio
    async def test_empty_delta_skips_send(self):
        ch = _make_channel()
        await ch.send_delta("oc_chat1", "   ")

        assert "oc_chat1" in ch._stream_bufs
        ch._client.cardkit.v1.card.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_client_returns_early(self):
        ch = _make_channel()
        ch._client = None
        await ch.send_delta("oc_chat1", "text")
        assert "oc_chat1" not in ch._stream_bufs

    @pytest.mark.asyncio
    async def test_sequence_increments_correctly(self):
        ch = _make_channel()
        buf = _FeishuStreamBuf(text="a", card_id="card_1", sequence=5, last_edit=0.0)
        ch._stream_bufs["oc_chat1"] = buf

        ch._client.cardkit.v1.card_element.content.return_value = _mock_content_response()
        await ch.send_delta("oc_chat1", "b")
        assert buf.sequence == 6

        buf.last_edit = 0.0  # reset to bypass throttle
        await ch.send_delta("oc_chat1", "c")
        assert buf.sequence == 7

    @pytest.mark.asyncio
    async def test_tool_events_update_observability_without_text_delta(self):
        ch = _make_channel()
        buf = _FeishuStreamBuf(
            text="Partial answer",
            card_id="card_1",
            sequence=1,
            last_edit=time.monotonic(),
        )
        ch._stream_bufs["oc_chat1"] = buf
        ch._client.cardkit.v1.card_element.content.return_value = _mock_content_response()

        await ch.send_delta(
            "oc_chat1",
            "",
            metadata={
                "_tool_events": [
                    {"phase": "start", "name": "web_search", "arguments": {"query": "nanobot"}},
                    {
                        "phase": "usage",
                        "usage": {
                            "prompt_tokens": 10,
                            "cached_tokens": 2,
                            "completion_tokens": 4,
                        },
                    },
                ]
            },
        )

        assert buf.text == "Partial answer"
        assert buf.sequence == 3
        request = ch._client.cardkit.v1.card_element.content.call_args[0][0]
        assert request.element_id == "tool_logs_markdown"
        assert "网络搜索 (`web_search`)" in request.body.content
        assert "执行中" in request.body.content
        assert "输入:" not in request.body.content
        assert "缓存命中:" not in request.body.content
        assert "输出:" not in request.body.content
        ch._client.cardkit.v1.card_element.patch.assert_called_once()
        patch_request = ch._client.cardkit.v1.card_element.patch.call_args[0][0]
        assert patch_request.element_id == "token_budget_chart"
        patched = json.loads(patch_request.body.partial_element)
        assert patched["chart_spec"]["title"]["text"] == "token用量占比图"
        values = patched["chart_spec"]["data"]["values"]
        assert {row["category"] for row in values} == {"token用量"}
        assert "token总量" not in json.dumps(patched, ensure_ascii=False)

    @pytest.mark.asyncio
    async def test_usage_events_accumulate_for_one_streaming_task(self):
        ch = _make_channel()
        buf = _FeishuStreamBuf(
            text="Partial answer",
            card_id="card_1",
            sequence=1,
            last_edit=time.monotonic(),
        )
        ch._stream_bufs["oc_chat1"] = buf
        ch._client.cardkit.v1.card_element.content.return_value = _mock_content_response()

        await ch.send_delta(
            "oc_chat1",
            "",
            metadata={
                "_tool_events": [{
                    "phase": "usage",
                    "usage": {
                        "prompt_tokens": 10,
                        "cached_tokens": 2,
                        "completion_tokens": 4,
                        "total_tokens": 14,
                    },
                }]
            },
        )
        await ch.send_delta(
            "oc_chat1",
            "",
            metadata={
                "_tool_events": [{
                    "phase": "usage",
                    "usage": {
                        "prompt_tokens": 20,
                        "cache_tokens": 3,
                        "completion_tokens": 6,
                        "total_tokens": 26,
                    },
                }]
            },
        )

        assert buf.token_usage == {
            "prompt_tokens": 30,
            "cached_tokens": 5,
            "completion_tokens": 10,
            "total_tokens": 40,
        }
        patch_request = ch._client.cardkit.v1.card_element.patch.call_args[0][0]
        patched = json.loads(patch_request.body.partial_element)
        values = patched["chart_spec"]["data"]["values"]
        assert values == [
            {"category": "token用量", "item": "缓存输入", "value": 5},
            {"category": "token用量", "item": "非缓存输入", "value": 25},
            {"category": "token用量", "item": "输出", "value": 10},
        ]

    def test_token_chart_uses_single_usage_category(self):
        spec = FeishuChannel._token_chart_spec({
            "prompt_tokens": 10,
            "cached_tokens": 2,
            "completion_tokens": 4,
            "total_tokens": 14,
        })

        values = spec["data"]["values"]
        assert [row["category"] for row in values] == ["token用量", "token用量", "token用量"]
        assert [row["item"] for row in values] == ["缓存输入", "非缓存输入", "输出"]
        assert "token总量" not in json.dumps(spec, ensure_ascii=False)

    def test_tool_result_preview_uses_truncated_code_block(self):
        result = "x" * 950
        md = FeishuChannel._format_observability_markdown(
            [{
                "phase": "end",
                "name": "web_fetch",
                "arguments": {"url": "https://example.com"},
                "result": result,
            }],
            {},
        )

        assert "   - 结果:\n```\n" in md
        assert "...（已截断）" in md
        assert "Token 统计" not in md

    def test_tool_json_result_preview_uses_json_code_block(self):
        md = FeishuChannel._format_observability_markdown(
            [{
                "phase": "end",
                "name": "web_search",
                "arguments": {"query": "nanobot"},
                "result": {"ok": True, "count": 2},
            }],
            {},
        )

        assert "   - 结果:\n```json\n" in md
        assert '"ok": true' in md


class TestToolHintInlineStreaming:
    """Tool hint messages should be kept in the structured tool panel."""

    @pytest.mark.asyncio
    async def test_tool_hint_updates_panel_when_stream_active(self):
        """With an active streaming buffer, raw hint text stays out of the answer body."""
        ch = _make_channel()
        ch._stream_bufs["oc_chat1"] = _FeishuStreamBuf(
            text="Partial answer", card_id="card_1", sequence=2, last_edit=0.0,
        )
        ch._client.cardkit.v1.card_element.content.return_value = _mock_content_response()

        msg = OutboundMessage(
            channel="feishu", chat_id="oc_chat1",
            content='web_fetch("https://example.com")',
            metadata={"_tool_hint": True},
        )
        await ch.send(msg)

        buf = ch._stream_bufs["oc_chat1"]
        assert 'web_fetch("https://example.com")' not in buf.text
        assert buf.sequence == 3
        ch._client.cardkit.v1.card_element.content.assert_called_once()
        request = ch._client.cardkit.v1.card_element.content.call_args[0][0]
        assert request.element_id == "tool_logs_markdown"
        assert "web_fetch" in request.body.content
        assert "https://example.com" in request.body.content
        ch._client.im.v1.message.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_tool_hint_before_answer_waits_for_first_answer_delta(self):
        """Tool progress before the first answer token is cached, not sent as an empty card."""
        ch = _make_channel()
        ch._client.cardkit.v1.card.create.return_value = _mock_create_card_response("card_tool")
        ch._client.im.v1.message.create.return_value = _mock_send_response("om_tool")
        ch._client.cardkit.v1.card_element.content.return_value = _mock_content_response()

        msg = OutboundMessage(
            channel="feishu",
            chat_id="oc_chat1",
            content='web_search("nanobot")',
            metadata={
                "_progress": True,
                "_tool_hint": True,
                "_wants_stream": True,
                "_tool_events": [
                    {"phase": "start", "name": "web_search", "arguments": {"query": "nanobot"}},
                ],
            },
        )
        await ch.send(msg)

        buf = ch._stream_bufs["oc_chat1"]
        assert buf.card_id is None
        assert buf.text == ""
        ch._client.cardkit.v1.card.create.assert_not_called()
        ch._client.im.v1.message.create.assert_not_called()

        await ch.send_delta(
            "oc_chat1",
            "Answer",
            metadata={"_stream_delta": True, "_wants_stream": True, "_stream_id": "seg-1"},
        )

        assert buf.card_id == "card_tool"
        assert buf.text == "Answer"
        ch._client.im.v1.message.create.assert_called_once()
        request = ch._client.cardkit.v1.card.create.call_args[0][0]
        payload = json.loads(request.body.data)
        panel = payload["body"]["elements"][0]
        assert panel["tag"] == "collapsible_panel"
        assert panel["background_color"] == "grey"
        assert "网络搜索 (`web_search`)" in panel["elements"][0]["content"]

    @pytest.mark.asyncio
    async def test_tool_hint_preserved_on_next_delta(self):
        """When new delta arrives, cached tool hints stay out of the answer body."""
        ch = _make_channel()
        ch._stream_bufs["oc_chat1"] = _FeishuStreamBuf(
            text="Partial answer",
            card_id="card_1",
            sequence=3,
            last_edit=0.0,
            tool_events=[{
                "phase": "start",
                "name": "web_fetch",
                "arguments": {"url": "https://example.com"},
            }],
        )
        ch._client.cardkit.v1.card_element.content.return_value = _mock_content_response()

        await ch.send_delta("oc_chat1", " continued")

        buf = ch._stream_bufs["oc_chat1"]
        assert "Partial answer" in buf.text
        assert "🔧 web_fetch" not in buf.text
        assert buf.text.endswith(" continued")

    @pytest.mark.asyncio
    async def test_tool_hint_fallback_when_no_stream(self):
        """Without an active buffer, tool hint falls back to a standalone card."""
        ch = _make_channel()
        ch._client.im.v1.message.create.return_value = _mock_send_response("om_hint")

        msg = OutboundMessage(
            channel="feishu", chat_id="oc_chat1",
            content='read_file("path")',
            metadata={"_tool_hint": True},
        )
        await ch.send(msg)

        assert "oc_chat1" not in ch._stream_bufs
        ch._client.im.v1.message.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_tool_hint_group_uses_create_when_reply_disabled(self):
        ch = _make_channel(reply_to_message=False)
        ch._client.im.v1.message.create.return_value = _mock_send_response("om_hint")

        msg = OutboundMessage(
            channel="feishu", chat_id="oc_chat1",
            content='read_file("path")',
            metadata={"_tool_hint": True, "message_id": "om_001", "chat_type": "group"},
        )
        await ch.send(msg)

        ch._client.im.v1.message.create.assert_called_once()
        ch._client.im.v1.message.reply.assert_not_called()

    @pytest.mark.asyncio
    async def test_tool_hint_keeps_existing_topic_when_reply_disabled(self):
        ch = _make_channel(reply_to_message=False)
        reply_resp = MagicMock()
        reply_resp.success.return_value = True
        ch._client.im.v1.message.reply.return_value = reply_resp

        msg = OutboundMessage(
            channel="feishu", chat_id="oc_chat1",
            content='read_file("path")',
            metadata={
                "_tool_hint": True,
                "message_id": "om_001",
                "chat_type": "group",
                "thread_id": "ot_001",
            },
        )
        await ch.send(msg)

        ch._client.im.v1.message.reply.assert_called_once()
        ch._client.im.v1.message.create.assert_not_called()
        request = ch._client.im.v1.message.reply.call_args[0][0]
        assert request.request_body.reply_in_thread is not True

    @pytest.mark.asyncio
    async def test_tool_hint_group_replies_when_reply_enabled(self):
        ch = _make_channel(reply_to_message=True)
        reply_resp = MagicMock()
        reply_resp.success.return_value = True
        ch._client.im.v1.message.reply.return_value = reply_resp

        msg = OutboundMessage(
            channel="feishu", chat_id="oc_chat1",
            content='read_file("path")',
            metadata={"_tool_hint": True, "message_id": "om_001", "chat_type": "group"},
        )
        await ch.send(msg)

        ch._client.im.v1.message.reply.assert_called_once()
        ch._client.im.v1.message.create.assert_not_called()
        request = ch._client.im.v1.message.reply.call_args[0][0]
        assert request.request_body.reply_in_thread is True

    @pytest.mark.asyncio
    async def test_consecutive_tool_hints_update_panel_not_answer(self):
        """Raw tool hints should stay in the tool panel, not pollute answer text."""
        ch = _make_channel()
        ch._stream_bufs["oc_chat1"] = _FeishuStreamBuf(
            text="Partial answer", card_id="card_1", sequence=2, last_edit=0.0,
        )
        ch._client.cardkit.v1.card_element.content.return_value = _mock_content_response()

        msg1 = OutboundMessage(
            channel="feishu", chat_id="oc_chat1",
            content='$ cd /project', metadata={"_tool_hint": True},
        )
        await ch.send(msg1)

        msg2 = OutboundMessage(
            channel="feishu", chat_id="oc_chat1",
            content='$ git status', metadata={"_tool_hint": True},
        )
        await ch.send(msg2)

        buf = ch._stream_bufs["oc_chat1"]
        assert "$ cd /project" not in buf.text
        assert "$ git status" not in buf.text
        assert buf.text.startswith("Partial answer")
        assert buf.sequence == 4
        assert ch._client.cardkit.v1.card_element.content.call_count == 2
        last_request = ch._client.cardkit.v1.card_element.content.call_args[0][0]
        assert last_request.element_id == "tool_logs_markdown"
        assert "$ cd /project" in last_request.body.content
        assert "$ git status" in last_request.body.content

    @pytest.mark.asyncio
    async def test_tool_hint_preserved_on_final_stream_end(self):
        """When final _stream_end closes the card, raw tool hints are not copied into final text."""
        ch = _make_channel()
        ch._stream_bufs["oc_chat1"] = _FeishuStreamBuf(
            text="Final content",
            card_id="card_1",
            sequence=3,
            last_edit=0.0,
            tool_events=[{
                "phase": "start",
                "name": "web_fetch",
                "arguments": {"url": "https://example.com"},
            }],
        )
        ch._client.cardkit.v1.card_element.content.return_value = _mock_content_response()
        ch._client.cardkit.v1.card.settings.return_value = _mock_content_response()

        await ch.send_delta("oc_chat1", "", metadata={"_stream_end": True})

        assert "oc_chat1" not in ch._stream_bufs
        update_call = ch._client.cardkit.v1.card_element.content.call_args[0][0]
        assert update_call.element_id == "streaming_md"
        assert "🔧" not in update_call.body.content

    @pytest.mark.asyncio
    async def test_empty_tool_hint_is_noop(self):
        """Empty or whitespace-only tool hint content is silently ignored."""
        ch = _make_channel()
        ch._stream_bufs["oc_chat1"] = _FeishuStreamBuf(
            text="Partial answer", card_id="card_1", sequence=2, last_edit=0.0,
        )

        for content in ("", "   ", "\t\n"):
            msg = OutboundMessage(
                channel="feishu", chat_id="oc_chat1",
                content=content, metadata={"_tool_hint": True},
            )
            await ch.send(msg)

        buf = ch._stream_bufs["oc_chat1"]
        assert buf.text == "Partial answer"
        assert buf.sequence == 2
        ch._client.cardkit.v1.card_element.content.assert_not_called()


class TestSendMessageReturnsId:
    def test_returns_message_id_on_success(self):
        ch = _make_channel()
        ch._client.im.v1.message.create.return_value = _mock_send_response("om_abc")
        result = ch._send_message_sync("chat_id", "oc_chat1", "text", '{"text":"hi"}')
        assert result == "om_abc"

    def test_returns_none_on_failure(self):
        ch = _make_channel()
        resp = MagicMock()
        resp.success.return_value = False
        resp.code = 99999
        resp.msg = "error"
        resp.get_log_id.return_value = "log1"
        ch._client.im.v1.message.create.return_value = resp
        result = ch._send_message_sync("chat_id", "oc_chat1", "text", '{"text":"hi"}')
        assert result is None
