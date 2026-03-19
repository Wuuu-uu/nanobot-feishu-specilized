from __future__ import annotations

from pathlib import Path

from nanobot.agent.tools.notion import NotionTool
from nanobot.config.schema import NotionToolConfig


def _make_tool() -> NotionTool:
    return NotionTool(NotionToolConfig())


def _extract_cell_plain_text(cell: list[dict]) -> str:
    parts: list[str] = []
    for token in cell:
        if token.get("type") == "text":
            parts.append(token.get("text", {}).get("content", ""))
        elif token.get("type") == "equation":
            parts.append(token.get("equation", {}).get("expression", ""))
    return "".join(parts)


def test_table_cell_with_pipe_in_formula_kept_in_single_cell() -> None:
    tool = _make_tool()
    lines = [
        "| Expr | Desc |",
        "| --- | --- |",
        "| p(x|y) | posterior probability |",
    ]
    table = tool._build_table_block(lines)
    assert table is not None

    # 2nd row (index 1): data row
    data_cells = table["table"]["children"][1]["table_row"]["cells"]
    assert len(data_cells) == 2
    assert _extract_cell_plain_text(data_cells[0]) == "p(x|y)"
    assert _extract_cell_plain_text(data_cells[1]) == "posterior probability"


def test_markdown_table_requires_alignment_row() -> None:
    tool = _make_tool()
    md = "Paragraph with A | B\nStill paragraph with x|y\n"
    blocks = tool._markdown_to_blocks(md)
    assert len(blocks) == 1
    assert blocks[0]["type"] == "paragraph"


def test_inline_markdown_link_mapped_to_notion_link() -> None:
    tool = _make_tool()
    rt = tool._inline_to_rich_text("See [paper](https://example.com) now")

    link_tokens = [
        x for x in rt
        if x.get("type") == "text" and x.get("text", {}).get("link", {}).get("url")
    ]
    assert len(link_tokens) == 1
    assert link_tokens[0]["text"]["content"] == "paper"
    assert link_tokens[0]["text"]["link"]["url"] == "https://example.com"


def test_inline_strike_and_bolditalic_annotations() -> None:
    tool = _make_tool()
    rt = tool._inline_to_rich_text("~~bad~~ then ***great***")

    strike = [x for x in rt if x.get("annotations", {}).get("strikethrough")]
    both = [
        x for x in rt
        if x.get("annotations", {}).get("bold") and x.get("annotations", {}).get("italic")
    ]
    assert strike and strike[0]["text"]["content"] == "bad"
    assert both and both[0]["text"]["content"] == "great"


def test_todo_list_maps_to_to_do_block() -> None:
    tool = _make_tool()
    md = "- [x] done\n- [ ] todo\n"
    blocks = tool._markdown_to_blocks(md)

    assert [b["type"] for b in blocks] == ["to_do", "to_do"]
    assert blocks[0]["to_do"]["checked"] is True
    assert blocks[1]["to_do"]["checked"] is False
