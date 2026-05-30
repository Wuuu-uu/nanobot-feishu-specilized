import json
import re

# Check optional Feishu dependencies before running tests
try:
    from nanobot.channels import feishu
    FEISHU_AVAILABLE = getattr(feishu, "FEISHU_AVAILABLE", False)
except ImportError:
    FEISHU_AVAILABLE = False

if not FEISHU_AVAILABLE:
    import pytest
    pytest.skip("Feishu dependencies not installed (lark-oapi)", allow_module_level=True)

from nanobot.channels.feishu import FeishuChannel


def test_parse_md_table_strips_markdown_formatting_in_headers_and_cells() -> None:
    table = FeishuChannel._parse_md_table(
        """
| **Name** | __Status__ | *Notes* | ~~State~~ |
| --- | --- | --- | --- |
| **Alice** | __Ready__ | *Fast* | ~~Old~~ |
"""
    )

    assert table is not None
    assert [col["display_name"] for col in table["columns"]] == [
        "Name",
        "Status",
        "Notes",
        "State",
    ]
    assert table["rows"] == [
        {"c0": "Alice", "c1": "Ready", "c2": "Fast", "c3": "Old"}
    ]


def test_split_headings_strips_embedded_markdown_before_bolding() -> None:
    channel = FeishuChannel.__new__(FeishuChannel)

    elements = channel._split_headings("# **Important** *status* ~~update~~")

    assert elements == [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "**Important status update**",
            },
        }
    ]


def test_split_headings_keeps_markdown_body_and_code_blocks_intact() -> None:
    channel = FeishuChannel.__new__(FeishuChannel)

    elements = channel._split_headings(
        "# **Heading**\n\nBody with **bold** text.\n\n```python\nprint('hi')\n```"
    )

    assert elements[0] == {
        "tag": "div",
        "text": {
            "tag": "lark_md",
            "content": "**Heading**",
        },
    }
    assert elements[1]["tag"] == "markdown"
    assert "Body with **bold** text." in elements[1]["content"]
    assert "```python\nprint('hi')\n```" in elements[1]["content"]


def test_normalize_card_markdown_lifts_indented_fenced_code_blocks() -> None:
    content = (
        "2. **Probe task**\n"
        "   重点解决：\n"
        "   ```text\n"
        "   official image namespace\n"
        "   instance branch\n"
        "   ```\n"
        "3. next\n"
    )

    normalized = FeishuChannel._normalize_card_markdown(content)

    assert "   ```text" not in normalized
    assert "```text\nofficial image namespace\ninstance branch\n```" in normalized
    assert "2. **Probe task**" in normalized
    assert "3. next" in normalized


def test_inline_code_uses_interactive_card_format() -> None:
    assert (
        FeishuChannel._detect_msg_format("Use `runtime/reasoning/trace.py` next")
        == "interactive"
    )


def test_inline_math_uses_interactive_card_format() -> None:
    assert FeishuChannel._detect_msg_format("Use $E = mc^2$ next") == "interactive"


def test_source_label_uses_card_header_not_markdown_body() -> None:
    channel = FeishuChannel.__new__(FeishuChannel)

    elements = channel._build_card_elements(
        "# Status\n\nUse `runtime/reasoning/trace.py` next",
        {"_source_label": "Cron · sessions/cron_research.jsonl"},
    )
    card = channel._build_card(
        elements,
        {"_source_label": "Cron · sessions/cron_research.jsonl"},
    )

    assert card["schema"] == "2.0"
    assert card["header"]["title"] == {
        "tag": "plain_text",
        "content": "Cron Agent 消息",
    }
    assert card["header"]["subtitle"] == {
        "tag": "plain_text",
        "content": "Cron · sessions/cron_research.jsonl",
    }
    body = card["body"]["elements"]
    assert body[0]["tag"] == "markdown"
    assert body[0]["element_id"] == "message_markdown"
    assert body[0]["content"].startswith("# Status")
    assert "`runtime/reasoning/trace.py`" in body[0]["content"]


def test_helper_source_title_uses_helper_card_header() -> None:
    channel = FeishuChannel.__new__(FeishuChannel)
    metadata = {
        "_source_title": "Helper Agent 消息",
        "_source_label": "Helper · sessions/helper_cron_research.jsonl",
    }

    elements = channel._build_card_elements(
        "decision: revise\n\n需要缩小实验面板。",
        metadata,
    )
    card = channel._build_card(elements, metadata)

    assert card["schema"] == "2.0"
    assert card["header"]["title"] == {
        "tag": "plain_text",
        "content": "Helper Agent 消息",
    }
    assert card["header"]["subtitle"] == {
        "tag": "plain_text",
        "content": "Helper · sessions/helper_cron_research.jsonl",
    }


def test_cardkit_v2_markdown_body_preserves_heading_inline_code_and_formula() -> None:
    channel = FeishuChannel.__new__(FeishuChannel)
    metadata = {"_source_label": "Cron · sessions/cron_research.jsonl"}

    elements = channel._build_markdown_card_elements(
        "# 指标复核\n\n`trajectory_steps[*].text` 与 $E = mc^2$ 应保持原始 Markdown。",
        tool_events=[{
            "phase": "start",
            "name": "web_search",
            "arguments": {"query": "CardKit markdown"},
        }],
        token_usage={
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
        },
    )
    card = channel._build_card(elements, metadata)

    assert card["schema"] == "2.0"
    assert card["header"]["title"]["content"] == "Cron Agent 消息"
    body = card["body"]["elements"]
    assert body[0]["tag"] == "collapsible_panel"
    assert body[1] == {
        "tag": "markdown",
        "content": "# 指标复核\n\n`trajectory_steps[*].text` 与 $E = mc^2$ 应保持原始 Markdown。",
        "element_id": "message_markdown",
    }
    assert body[2]["tag"] == "hr"
    assert body[3]["element_id"] == "token_budget_chart"


def test_tool_observability_uses_per_argument_truncation_inside_json_block() -> None:
    md = FeishuChannel._format_observability_markdown(
        [{
            "phase": "end",
            "name": "exec",
            "arguments": {
                "cmd": "x" * 700,
                "cwd": "/home/wulin/nanobot",
                "timeout": 30,
            },
            "result": "ok",
        }],
        {},
    )

    assert "1. **执行命令 (`exec`)** - 已完成" in md
    assert "### 1. 执行命令" not in md
    assert "**参数**" in md
    match = re.search(r"\*\*参数\*\*\n```json\n(.*?)\n```", md, re.DOTALL)
    assert match is not None
    args = json.loads(match.group(1))
    assert args["cmd"].endswith("...（已截断）")
    assert args["cwd"] == "/home/wulin/nanobot"
    assert args["timeout"] == 30
    assert md.count("...（已截断）") == 1
    assert "   - 参数:" not in md
    assert "- `cmd`:" not in md
    assert "**结果**\n```\nok\n```" in md


def test_helper_json_content_renders_as_markdown_sections() -> None:
    channel = FeishuChannel.__new__(FeishuChannel)
    metadata = {
        "_source_title": "Helper Agent 消息",
        "_source_label": "Helper · sessions/helper_cron_research.jsonl",
    }

    elements = channel._build_markdown_card_elements(
        """
{
  "decision": "revise",
  "reason": ["缺少基线对照", "需要补充失败样例"],
  "next_action": {
    "command": "pytest tests/channels/test_feishu*.py",
    "acceptance": ["全部通过", "无手工 JSON 块"]
  }
}
""",
        metadata=metadata,
    )

    md = elements[0]["content"]
    assert "### decision\n- revise" in md
    assert "### reason\n- 缺少基线对照\n- 需要补充失败样例" in md
    assert "### next_action" in md
    assert "#### command\n- pytest tests/channels/test_feishu*.py" in md
    assert "#### acceptance\n- 全部通过\n- 无手工 JSON 块" in md
    assert "```json" not in md


def test_stream_key_prefers_explicit_stream_id_for_helper_messages() -> None:
    assert (
        FeishuChannel._stream_key(
            "ou_123",
            {"message_id": "cron-message", "_stream_id": "helper:request-1"},
        )
        == "helper:request-1"
    )


def test_cron_bracket_title_keeps_body_on_main_session_markdown_path() -> None:
    channel = FeishuChannel.__new__(FeishuChannel)

    elements = channel._build_card_elements(
        "【complex reasoning workflow 自主迭代｜23:57 轮次】\n\n"
        "验证新 artifacts：\n"
        "`trajectory_steps[*].text` 是否非空；"
    )

    assert elements[0]["tag"] == "markdown"
    assert elements[0]["content"].startswith("【complex reasoning workflow 自主迭代｜23:57 轮次】")
    assert "`trajectory_steps[*].text` 是否非空；" in elements[0]["content"]


def test_fenced_code_blocks_stay_markdown_elements() -> None:
    channel = FeishuChannel.__new__(FeishuChannel)

    elements = channel._build_card_elements(
        "本轮是否启动 Codex：没有。检测到 Codex 进程仍残留：\n"
        "```text\n"
        "PID 146815 / 146822\n"
        "elapsed≈13m25s\n"
        "```\n"
        "但日志已完成。"
    )

    assert elements == [
        {
            "tag": "markdown",
            "element_id": "message_markdown",
            "content": (
                "本轮是否启动 Codex：没有。检测到 Codex 进程仍残留：\n"
                "```text\nPID 146815 / 146822\nelapsed≈13m25s\n```\n"
                "但日志已完成。"
            ),
        }
    ]
