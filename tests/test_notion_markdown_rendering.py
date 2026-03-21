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


def test_inline_anchor_link_degrades_to_plain_text_for_notion() -> None:
    tool = _make_tool()
    rt = tool._inline_to_rich_text("Jump to [Section](#sec-1)")

    # Should keep readable label but without invalid Notion URL link attachment.
    section_tokens = [
        x for x in rt
        if x.get("type") == "text" and x.get("text", {}).get("content") == "Section"
    ]
    assert section_tokens
    assert section_tokens[0].get("text", {}).get("link") is None


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


def test_code_fence_with_space_language_parsed_correctly() -> None:
    tool = _make_tool()
    md = (
        "### 3.2 Planner Prompt\n\n"
        "```plain text\n"
        "You are the Planner.\n"
        "```\n\n"
        "---\n\n"
        "### 3.3 Coder Prompt\n"
    )

    blocks = tool._markdown_to_blocks(md)

    # Expect heading -> code -> divider -> heading
    types = [b["type"] for b in blocks]
    assert types == ["heading_3", "code", "divider", "heading_3"]

    code_block = blocks[1]["code"]
    assert code_block["language"] == "plain text"
    code_text = "".join(rt.get("text", {}).get("content", "") for rt in code_block["rich_text"])
    assert "You are the Planner." in code_text


def test_code_fence_close_not_confused_by_trailing_content() -> None:
    tool = _make_tool()
    md = (
        "```python title=demo.py\n"
        "print('ok')\n"
        "```\n"
        "### heading after code\n"
    )
    blocks = tool._markdown_to_blocks(md)
    types = [b["type"] for b in blocks]
    assert types == ["code", "heading_3"]
    assert blocks[0]["code"]["language"] == "python"


def test_code_language_alias_text_maps_to_plain_text() -> None:
    tool = _make_tool()
    blocks = tool._markdown_to_blocks("```text\nhello\n```\n")
    assert len(blocks) == 1
    assert blocks[0]["type"] == "code"
    assert blocks[0]["code"]["language"] == "plain text"


def test_code_language_alias_objective_c_maps_to_objective_c_dash() -> None:
    tool = _make_tool()
    blocks = tool._markdown_to_blocks("```objective c\nint main(){}\n```\n")
    assert len(blocks) == 1
    assert blocks[0]["type"] == "code"
    assert blocks[0]["code"]["language"] == "objective-c"



# ---- Nested list tests ----


def test_nested_list_numbered_with_bullet_children() -> None:
    """Numbered list items with bullet sub-items should nest via children."""
    tool = _make_tool()
    md = (
        "1. First\n"
        "   - Sub A\n"
        "   - Sub B\n"
        "2. Second\n"
        "   - Sub C\n"
        "3. Third\n"
    )
    blocks = tool._markdown_to_blocks(md)

    # Should produce exactly 3 top-level numbered_list_item blocks
    assert len(blocks) == 3
    for b in blocks:
        assert b["type"] == "numbered_list_item"

    # First item has 2 children
    c1 = blocks[0]["numbered_list_item"].get("children", [])
    assert len(c1) == 2
    assert c1[0]["type"] == "bulleted_list_item"
    assert c1[1]["type"] == "bulleted_list_item"

    # Second item has 1 child
    c2 = blocks[1]["numbered_list_item"].get("children", [])
    assert len(c2) == 1

    # Third item has no children
    c3 = blocks[2]["numbered_list_item"].get("children", [])
    assert len(c3) == 0


def test_nested_list_three_levels() -> None:
    """Three levels of nesting (max Notion allows)."""
    tool = _make_tool()
    md = (
        "- Level 0\n"
        "  - Level 1\n"
        "    - Level 2\n"
    )
    blocks = tool._markdown_to_blocks(md)

    assert len(blocks) == 1
    assert blocks[0]["type"] == "bulleted_list_item"

    children_1 = blocks[0]["bulleted_list_item"].get("children", [])
    assert len(children_1) == 1

    children_2 = children_1[0]["bulleted_list_item"].get("children", [])
    assert len(children_2) == 1


def test_nested_list_depth_clamped() -> None:
    """Four indent levels should be clamped to 3 (Notion max)."""
    tool = _make_tool()
    md = (
        "- L0\n"
        "  - L1\n"
        "    - L2\n"
        "      - L3 (should clamp to L2)\n"
    )
    blocks = tool._markdown_to_blocks(md)

    assert len(blocks) == 1
    c1 = blocks[0]["bulleted_list_item"]["children"]
    assert len(c1) == 1
    c2 = c1[0]["bulleted_list_item"]["children"]
    # L2 and L3 both at depth 2, so L2's sibling L3 should be here
    assert len(c2) == 2


def test_nested_list_todo_items() -> None:
    """To-do items should also nest properly."""
    tool = _make_tool()
    md = (
        "- Parent\n"
        "  - [x] Done task\n"
        "  - [ ] Open task\n"
    )
    blocks = tool._markdown_to_blocks(md)

    assert len(blocks) == 1
    children = blocks[0]["bulleted_list_item"]["children"]
    assert len(children) == 2
    assert children[0]["type"] == "to_do"
    assert children[0]["to_do"]["checked"] is True
    assert children[1]["type"] == "to_do"
    assert children[1]["to_do"]["checked"] is False


def test_nested_list_mixed_numbered_bullet() -> None:
    """Mixed numbered and bulleted items at same level."""
    tool = _make_tool()
    md = (
        "1. Numbered parent\n"
        "   - Bullet child\n"
        "   1. Numbered child\n"
    )
    blocks = tool._markdown_to_blocks(md)

    assert len(blocks) == 1
    children = blocks[0]["numbered_list_item"]["children"]
    assert len(children) == 2
    assert children[0]["type"] == "bulleted_list_item"
    assert children[1]["type"] == "numbered_list_item"


def test_flat_list_unchanged() -> None:
    """A flat list with no nesting should produce sibling blocks (no children)."""
    tool = _make_tool()
    md = (
        "- Alpha\n"
        "- Beta\n"
        "- Gamma\n"
    )
    blocks = tool._markdown_to_blocks(md)

    assert len(blocks) == 3
    for b in blocks:
        assert b["type"] == "bulleted_list_item"
        assert "children" not in b["bulleted_list_item"]


# ---------------------------------------------------------------------------
# Table cell: LaTeX backslash preservation
# ---------------------------------------------------------------------------

def test_table_cell_latex_backslash_preserved():
    """\\frac, \\sum, \\mathcal etc. inside table cells must NOT lose backslashes."""
    tool = _make_tool()
    md = (
        "| Name | Formula |\n"
        "| --- | --- |\n"
        "| Loss | $\\mathcal{L} = \\frac{1}{n}\\sum_{i=1}^{n}(y_i - \\hat{y}_i)^2$ |\n"
        "| Attention | $\\text{softmax}\\left(\\frac{QK^T}{\\sqrt{d_k}}\\right)V$ |\n"
    )
    blocks = tool._markdown_to_blocks(md)
    # Should produce a table block
    assert blocks[0]["type"] == "table"
    rows = blocks[0]["table"]["children"]
    # Row 0: header, Row 1: Loss, Row 2: Attention
    loss_cell = rows[1]["table_row"]["cells"][1]  # second cell of Loss row
    attn_cell = rows[2]["table_row"]["cells"][1]  # second cell of Attention row

    # Collect plain text and equation content from rich text segments
    def get_text_and_eq(cell):
        parts = []
        for seg in cell:
            if seg["type"] == "text":
                parts.append(("text", seg["text"]["content"]))
            elif seg["type"] == "equation":
                parts.append(("eq", seg["equation"]["expression"]))
        return parts

    loss_parts = get_text_and_eq(loss_cell)
    attn_parts = get_text_and_eq(attn_cell)

    # There should be exactly one equation segment per cell
    loss_eqs = [v for t, v in loss_parts if t == "eq"]
    attn_eqs = [v for t, v in attn_parts if t == "eq"]
    assert len(loss_eqs) == 1, f"Expected 1 equation in loss cell, got: {loss_parts}"
    assert len(attn_eqs) == 1, f"Expected 1 equation in attn cell, got: {attn_parts}"

    # Backslashes must be intact
    assert "\\mathcal" in loss_eqs[0], f"\\\\mathcal missing: {loss_eqs[0]}"
    assert "\\frac" in loss_eqs[0], f"\\\\frac missing: {loss_eqs[0]}"
    assert "\\sum" in loss_eqs[0], f"\\\\sum missing: {loss_eqs[0]}"
    assert "\\frac" in attn_eqs[0], f"\\\\frac missing in attn: {attn_eqs[0]}"
    assert "\\sqrt" in attn_eqs[0], f"\\\\sqrt missing in attn: {attn_eqs[0]}"


def test_table_cell_escaped_pipe_still_works():
    """A literal \\| inside a cell should be kept as '|' (not split the cell)."""
    tool = _make_tool()
    md = (
        "| Col A | Col B |\n"
        "| --- | --- |\n"
        r"| foo\|bar | baz |" + "\n"
    )
    blocks = tool._markdown_to_blocks(md)
    rows = blocks[0]["table"]["children"]
    # 'foo|bar' should be in a single cell, not split
    cell_a = rows[1]["table_row"]["cells"][0]
    text = "".join(
        seg["text"]["content"] for seg in cell_a if seg["type"] == "text"
    )
    assert "|" in text, f"Escaped pipe should remain as '|': got {text}"
    assert "foo" in text and "bar" in text, f"Cell content wrong: {text}"
