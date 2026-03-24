import json
import asyncio

from nanobot.channels.feishu import FeishuChannel
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import FeishuConfig


def test_build_interactive_content_includes_token_monitor_variables() -> None:
    payload = FeishuChannel._build_interactive_content(
        text="hello",
        template_id="tid",
        template_version_name="1.0.0",
        token_monitor={
            "input_tokens": 50,
            "output_tokens": 30,
            "cache_tokens": 8,
            "task_total_tokens": 80,
            "context_budget_total_tokens": 100,
            "context_budget_used_tokens": 80,
            "context_budget_residue_tokens": 20,
            "context_budget_usage_ratio": 0.8,
            "context_budget_usage_percent": 80.0,
            "context_budget_exceeded": False,
            "chart": {
                "type": "bar",
                "data": {
                    "values": [
                        {"category": "token用量", "item": "input_cached", "value": 8},
                        {"category": "token用量", "item": "input_uncached", "value": 42},
                        {"category": "token用量", "item": "output", "value": 30},
                        {"category": "token用量", "item": "sum_tokens", "value": 80},
                    ]
                },
            },
        },
    )

    decoded = json.loads(payload)
    vars_obj = decoded["data"]["template_variable"]
    assert vars_obj["content"] == "hello"
    assert vars_obj["token_budget"]["type"] == "bar"
    assert vars_obj["token_budget"]["data"]["values"][0]["value"] == 8
    assert vars_obj["token_budget"]["data"]["values"][1]["value"] == 42
    assert vars_obj["token_budget"]["data"]["values"][2]["value"] == 30
    assert vars_obj["token_budget"]["data"]["values"][3]["value"] == 80


def test_build_interactive_content_without_token_monitor_is_compatible() -> None:
    payload = FeishuChannel._build_interactive_content(
        text="hello",
        template_id="tid",
        template_version_name="1.0.0",
    )

    decoded = json.loads(payload)
    vars_obj = decoded["data"]["template_variable"]
    assert vars_obj["content"] == "hello"
    assert vars_obj["token_budget"]["type"] == "bar"
    assert vars_obj["token_budget"]["data"]["values"][0]["value"] == 0
    assert vars_obj["token_budget"]["data"]["values"][1]["value"] == 0
    assert vars_obj["token_budget"]["data"]["values"][2]["value"] == 0
    assert vars_obj["token_budget"]["data"]["values"][3]["value"] == 0


def test_build_card_id_message_content_payload() -> None:
    payload = FeishuChannel._build_card_id_message_content("7355372766134157313")
    decoded = json.loads(payload)
    assert decoded == {
        "type": "card",
        "data": {"card_id": "7355372766134157313"},
    }


def test_build_streaming_card_json_contains_streaming_config() -> None:
    channel = FeishuChannel(FeishuConfig(streaming_enabled=True), MessageBus())
    card = channel._build_streaming_card_json("")

    assert card["schema"] == "2.0"
    assert "header" not in card
    assert card["config"]["streaming_mode"] is True
    assert card["config"]["update_multi"] is True
    assert card["config"]["streaming_config"]["print_frequency_ms"]["default"] == channel.config.streaming_print_frequency_ms_default
    assert card["config"]["streaming_config"]["print_step"]["default"] == channel.config.streaming_print_step_default
    assert card["config"]["streaming_config"]["print_strategy"] == channel.config.streaming_print_strategy
    assert card["body"]["elements"][0]["tag"] == "collapsible_panel"
    assert card["body"]["elements"][0]["expanded"] is False
    tool_elements = card["body"]["elements"][0]["elements"]
    assert len(tool_elements) == 1
    assert tool_elements[0]["element_id"] == "tool_logs_markdown"
    assert tool_elements[0]["text_size"] == "notation"
    assert "暂无工具调用" in tool_elements[0]["content"]
    assert card["body"]["elements"][1]["element_id"] == "answer_markdown"
    assert card["body"]["elements"][1]["content"] == "Generating..."


def test_build_streaming_card_json_includes_token_chart_when_provided() -> None:
    channel = FeishuChannel(FeishuConfig(streaming_enabled=True), MessageBus())
    card = channel._build_streaming_card_json(
        "hello",
        token_monitor={
            "chart": {
                "type": "bar",
                "data": {
                    "values": [
                        {"category": "token用量", "item": "output", "value": 30},
                    ]
                },
            }
        },
    )

    elements = card["body"]["elements"]
    assert len(elements) >= 3
    assert elements[0]["tag"] == "collapsible_panel"
    assert elements[1]["tag"] == "markdown"
    chart_elements = [el for el in elements if el.get("tag") == "chart"]
    assert len(chart_elements) == 1
    assert chart_elements[0]["element_id"] == "token_budget_chart"


def test_cardkit_patch_element_serializes_partial_element(monkeypatch) -> None:
    channel = FeishuChannel(FeishuConfig(streaming_enabled=True), MessageBus())
    captured: dict[str, object] = {}

    async def fake_request(method: str, path: str, payload: dict[str, object]) -> dict[str, object]:
        captured["method"] = method
        captured["path"] = path
        captured["payload"] = payload
        return {"code": 0, "msg": "success", "data": {}}

    monkeypatch.setattr(channel, "_cardkit_request", fake_request)

    asyncio.run(
        channel._cardkit_patch_element(
            card_id="c1",
            element_id="token_budget_chart",
            partial_element={"chart_spec": {"type": "bar", "data": {"values": []}}},
            sequence=3,
        )
    )

    assert captured["method"] == "PATCH"
    assert captured["path"] == "/open-apis/cardkit/v1/cards/c1/elements/token_budget_chart"
    payload = captured["payload"]
    assert isinstance(payload, dict)
    partial = payload.get("partial_element")
    assert isinstance(partial, str)
    decoded = json.loads(partial)
    assert decoded["chart_spec"]["type"] == "bar"
    assert payload.get("sequence") == 3


def test_replace_local_md_images_with_keys_sanitizes_unresolved_local_images() -> None:
    channel = FeishuChannel(FeishuConfig(streaming_enabled=True), MessageBus())
    text = "before ![arch](fig_esm2_arch.png) after"
    updated = asyncio.run(channel._replace_local_md_images_with_keys(text))
    assert "fig_esm2_arch.png" not in updated
    assert "[image omitted: arch]" in updated


def test_replace_local_md_images_with_keys_keeps_remote_images() -> None:
    channel = FeishuChannel(FeishuConfig(streaming_enabled=True), MessageBus())
    text = "![remote](https://example.com/a.png)"
    updated = asyncio.run(channel._replace_local_md_images_with_keys(text))
    assert updated == text
