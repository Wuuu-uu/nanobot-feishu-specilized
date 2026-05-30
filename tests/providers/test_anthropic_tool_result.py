"""Tests for AnthropicProvider._tool_result_block image_url conversion.

Regression for: tool results containing OpenAI-format image_url blocks
(e.g. from read_file on an image file, via build_image_content_blocks)
were passed to Anthropic unconverted, causing silent image drops with a
"Non-transient LLM error with image content, retrying without images"
warning.
"""

from nanobot.providers.anthropic_provider import AnthropicProvider


def test_tool_result_block_converts_image_url_in_list_content():
    """image_url blocks inside tool_result list content must be translated
    to Anthropic-native image blocks; sibling text blocks pass through."""
    msg = {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": [
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,AAAA"},
                "_meta": {"path": "/tmp/x.png"},
            },
            {"type": "text", "text": "(Image file: /tmp/x.png)"},
        ],
    }
    block = AnthropicProvider._tool_result_block(msg)

    assert block["type"] == "tool_result"
    assert block["tool_use_id"] == "call_1"
    content = block["content"]
    assert isinstance(content, list)
    assert content[0] == {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": "AAAA",
        },
    }
    assert content[1] == {"type": "text", "text": "(Image file: /tmp/x.png)"}


def test_tool_result_block_preserves_string_content():
    """String content must be passed through unchanged; the image-conversion
    path for lists must not affect the string path."""
    msg = {
        "role": "tool",
        "tool_call_id": "call_2",
        "content": "plain tool output",
    }
    block = AnthropicProvider._tool_result_block(msg)

    assert block["type"] == "tool_result"
    assert block["tool_use_id"] == "call_2"
    assert block["content"] == "plain tool output"


def test_convert_messages_normalizes_invalid_tool_ids_consistently():
    provider = AnthropicProvider(api_key="test-key")
    invalid_id = "call_abc|fc_123"

    _system, messages = provider._convert_messages([
        {
            "role": "user",
            "content": "run a tool",
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": invalid_id,
                "function": {
                    "name": "exec",
                    "arguments": '{"command": "pwd"}',
                },
            }],
        },
        {
            "role": "tool",
            "tool_call_id": invalid_id,
            "name": "exec",
            "content": "/home/wulin",
        },
        {
            "role": "user",
            "content": "continue",
        },
    ])

    tool_use = messages[1]["content"][0]
    tool_result = messages[2]["content"][0]
    assert tool_use["type"] == "tool_use"
    assert tool_result["type"] == "tool_result"
    assert tool_use["id"] == tool_result["tool_use_id"]
    assert "|" not in tool_use["id"]
    assert tool_use["id"].startswith("toolu_")


def test_convert_messages_preserves_valid_tool_ids():
    provider = AnthropicProvider(api_key="test-key")

    _system, messages = provider._convert_messages([
        {"role": "user", "content": "run a tool"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call_good-123",
                "function": {"name": "exec", "arguments": "{}"},
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "call_good-123",
            "name": "exec",
            "content": "ok",
        },
        {"role": "user", "content": "continue"},
    ])

    assert messages[1]["content"][0]["id"] == "call_good-123"
    assert messages[2]["content"][0]["tool_use_id"] == "call_good-123"


def test_convert_messages_normalizes_raw_anthropic_tool_use_blocks():
    provider = AnthropicProvider(api_key="test-key")
    invalid_id = "raw.tool.use"

    _system, messages = provider._convert_messages([
        {"role": "user", "content": "run a tool"},
        {
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "id": invalid_id,
                "name": "exec",
                "input": {},
            }],
        },
        {
            "role": "tool",
            "tool_call_id": invalid_id,
            "name": "exec",
            "content": "ok",
        },
        {"role": "user", "content": "continue"},
    ])

    assert messages[1]["content"][0]["id"] == messages[2]["content"][0]["tool_use_id"]
    assert "." not in messages[1]["content"][0]["id"]
