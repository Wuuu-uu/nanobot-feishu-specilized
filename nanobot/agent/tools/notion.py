"""Notion tool for database ingestion and management."""

from __future__ import annotations

import json
import logging
import mimetypes
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from nanobot.agent.tools.base import Tool
from nanobot.config.schema import NotionToolConfig

logger = logging.getLogger(__name__)


_TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".json", ".jsonl", ".csv", ".tsv",
    ".yaml", ".yml", ".xml", ".html", ".htm", ".py", ".js", ".ts",
    ".java", ".c", ".cc", ".cpp", ".h", ".hpp", ".go", ".rs", ".sql",
    ".log", ".ini", ".toml", ".cfg", ".conf", ".sh", ".bash", ".zsh",
    ".tex", ".rst",
}

_MAX_RICH_TEXT_CHARS = 1900
_MAX_APPEND_BLOCKS = 80
_INLINE_PATTERN = re.compile(
    r"(?<!\$)\$(?!\$)(?P<math>.+?)(?<!\$)\$(?!\$)"  # inline math $...$
    r"|(?P<code>`[^`]+`)"                                 # inline code `...`
    r"|(?P<link>\[[^\]]+\]\([^)]+\))"                 # markdown link [text](url)
    r"|(?P<autolink><https?://[^>\s]+>)"                  # autolink <https://...>
    r"|(?P<bolditalic>\*\*\*[^*]+\*\*\*)"            # bold+italic ***...***
    r"|(?P<bold>\*\*[^*]+\*\*)"                        # bold **...**
    r"|(?P<strike>~~[^~]+~~)"                              # strikethrough ~~...~~
    r"|(?P<italic>\*[^*]+\*)"                            # italic *...*
)
_IMAGE_PATTERN = re.compile(r"^!\[([^\]]*)\]\(([^)]+)\)\s*$")
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".tif", ".tiff", ".bmp", ".ico", ".heic"}

# Notion code block languages (as accepted by Notion API)
_NOTION_CODE_LANGUAGES = {
    "abap", "abc", "agda", "arduino", "ascii art", "assembly", "bash", "basic", "bnf",
    "c", "c#", "c++", "clojure", "coffeescript", "coq", "css", "dart", "dhall", "diff",
    "docker", "ebnf", "elixir", "elm", "erlang", "f#", "flow", "fortran", "gherkin",
    "glsl", "go", "graphql", "groovy", "haskell", "hcl", "html", "idris", "java",
    "javascript", "json", "julia", "kotlin", "latex", "less", "lisp", "livescript",
    "llvm ir", "lua", "makefile", "markdown", "markup", "matlab", "mathematica", "mermaid",
    "nix", "notion formula", "objective-c", "ocaml", "pascal", "perl", "php", "plain text",
    "powershell", "prolog", "protobuf", "purescript", "python", "r", "racket", "reason",
    "ruby", "rust", "sass", "scala", "scheme", "scss", "shell", "smalltalk", "solidity",
    "sql", "swift", "toml", "typescript", "vb.net", "verilog", "vhdl", "visual basic",
    "webassembly", "xml", "yaml", "java/c/c++/c#",
}

_NOTION_CODE_LANGUAGE_ALIASES = {
    "text": "plain text",
    "plaintext": "plain text",
    "plain": "plain text",
    "txt": "plain text",
    "console": "shell",
    "shellscript": "shell",
    "sh": "shell",
    "zsh": "shell",
    "bash script": "bash",
    "js": "javascript",
    "ts": "typescript",
    "py": "python",
    "yml": "yaml",
    "md": "markdown",
    "objc": "objective-c",
    "objective c": "objective-c",
    "objective_c": "objective-c",
    "csharp": "c#",
    "cs": "c#",
    "cpp": "c++",
    "cxx": "c++",
    "golang": "go",
}


class NotionTool(Tool):
    """Manage authorized Notion database(s) and ingest local files as dataset entries."""

    def __init__(self, config: NotionToolConfig, allowed_dir: Path | None = None):
        self.config = config
        self._allowed_dir = allowed_dir

    @property
    def name(self) -> str:
        return "notion"

    @property
    def description(self) -> str:
        return (
            "Manage Notion dataset database(s): inspect schema/content, upload local files, "
            "render Markdown to Notion blocks, and classify entries into notes/reports partitions."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "inspect_database",
                        "ensure_partitions",
                        "upload_file",
                        "list_items",
                        "reclassify_item",
                    ],
                    "description": "Tool action to execute",
                },
                "path": {
                    "type": "string",
                    "description": "Local file path to upload when action=upload_file",
                },
                "doc_type": {
                    "type": "string",
                    "description": "Dataset type label for upload/reclassify/inspect/list. Supports custom types (e.g. log). Use 'auto' for inference.",
                    "enum":[
                        "auto",
                        "reports",
                        "notes",
                    ],
                    "default": "auto",
                },
                "title": {
                    "type": "string",
                    "description": "Optional title override for upload_file",
                },
                "page_id": {
                    "type": "string",
                    "description": "Target Notion page ID when action=reclassify_item",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "description": "Number of items for list_items",
                    "default": 10,
                },
                "include_content": {
                    "type": "boolean",
                    "description": "Whether to include extracted text snippet on upload_file",
                    "default": True,
                },
            },
            "required": ["action"],
        }

    async def execute(
        self,
        action: str,
        path: str | None = None,
        doc_type: str = "auto",
        title: str | None = None,
        page_id: str | None = None,
        limit: int = 10,
        include_content: bool = True,
        **kwargs: Any,
    ) -> str:
        if not self.config.enabled:
            return "Error: Notion tool is disabled."
        if not self.config.api_key:
            return "Error: Notion config missing api_key."
        if not self._has_any_database_configured():
            return "Error: Notion config missing database_id or type_database_map."

        try:
            if action == "inspect_database":
                return await self._inspect_database(doc_type)
            if action == "ensure_partitions":
                return await self._ensure_partitions()
            if action == "upload_file":
                if not path:
                    return "Error: path is required for upload_file"
                return await self._upload_file(path, doc_type, title, include_content)
            if action == "list_items":
                return await self._list_items(doc_type=doc_type, limit=limit)
            if action == "reclassify_item":
                if not page_id:
                    return "Error: page_id is required for reclassify_item"
                return await self._reclassify_item(page_id, doc_type)
            return f"Error: unsupported action '{action}'"
        except Exception as e:
            return f"Error executing notion action '{action}': {str(e)}"

    async def _inspect_database(self, doc_type: str = "auto") -> str:
        targets = self._get_target_databases(doc_type)
        if not targets:
            return "Error: no target database resolved for inspect_database. Check tools.notion.typeDatabaseMap/databaseId settings."
        inspected: list[dict[str, Any]] = []

        for alias, database_id in targets.items():
            db = await self._request("GET", f"databases/{database_id}")
            sample_items = await self._request(
                "POST",
                f"databases/{database_id}/query",
                json_body={"page_size": 5},
            )
            properties = {
                name: schema.get("type")
                for name, schema in db.get("properties", {}).items()
            }
            inspected.append({
                "alias": alias,
                "database_id": db.get("id"),
                "title": self._plain_title(db.get("title", [])),
                "properties": properties,
                "sample_count": len(sample_items.get("results", [])),
                "sample_items": [self._page_brief(p) for p in sample_items.get("results", [])],
            })

        return json.dumps(
            {
                "status": "ok",
                "action": "inspect_database",
                "targets": inspected,
            },
            ensure_ascii=False,
            indent=2,
        )

    async def _ensure_partitions(self) -> str:
        targets = self._get_target_databases("auto")
        results: list[str] = []
        required_labels = self._get_partition_labels()
        if not required_labels:
            return "No partition labels configured. Add keys in tools.notion.typeDatabaseMap first."

        for alias, database_id in targets.items():
            db = await self._request("GET", f"databases/{database_id}")
            type_name = self.config.type_property
            prop = db.get("properties", {}).get(type_name)
            if not prop:
                results.append(
                    f"[{alias}] Type property '{type_name}' not found (skip)."
                )
                continue

            prop_type = prop.get("type")
            if prop_type == "select":
                options = prop.get("select", {}).get("options", [])
                names = {opt.get("name") for opt in options}
                for required in required_labels:
                    if required not in names:
                        options.append({"name": required})
                await self._request(
                    "PATCH",
                    f"databases/{database_id}",
                    json_body={"properties": {type_name: {"select": {"options": options}}}},
                )
                results.append(
                    f"[{alias}] ensured in select '{type_name}': {', '.join(required_labels)}"
                )
                continue

            if prop_type == "multi_select":
                options = prop.get("multi_select", {}).get("options", [])
                names = {opt.get("name") for opt in options}
                for required in required_labels:
                    if required not in names:
                        options.append({"name": required})
                await self._request(
                    "PATCH",
                    f"databases/{database_id}",
                    json_body={"properties": {type_name: {"multi_select": {"options": options}}}},
                )
                results.append(
                    f"[{alias}] ensured in multi_select '{type_name}': {', '.join(required_labels)}"
                )
                continue

            results.append(
                f"[{alias}] property '{type_name}' type '{prop_type}' unsupported; use select/multi_select/rich_text"
            )

        return "\n".join(results)

    async def _upload_file(
        self,
        raw_path: str,
        doc_type: str,
        title: str | None,
        include_content: bool,
    ) -> str:
        file_path = self._resolve_path(raw_path)
        if not file_path.exists() or not file_path.is_file():
            return f"Error: file not found: {raw_path}"

        actual_type = self._normalize_doc_type(doc_type, file_path)
        database_id = self._resolve_target_database_id(actual_type)
        if not database_id:
            return "Error: no target database configured for this doc_type"

        db = await self._request("GET", f"databases/{database_id}")
        properties_schema = db.get("properties", {})

        page_title = title or file_path.stem
        source_path = str(file_path)
        file_name = file_path.name

        page_properties: dict[str, Any] = {}
        title_property = self._find_title_property(properties_schema)
        if not title_property:
            return "Error: target database has no title property"

        page_properties[title_property] = self._build_title_value(page_title)
        self._set_property_value(properties_schema, page_properties, self.config.type_property, actual_type)
        self._set_property_value(properties_schema, page_properties, self.config.source_path_property, source_path)
        self._set_property_value(properties_schema, page_properties, self.config.file_name_property, file_name)

        full_text = ""
        content_for_property = ""
        if include_content:
            full_text = self._read_text_content(file_path)
            if full_text:
                max_chars = max(200, self.config.max_content_chars)
                content_for_property = full_text[:max_chars]
                self._set_property_value(properties_schema, page_properties, self.config.content_property, content_for_property)

        children = self._build_children(file_path=file_path, doc_type=actual_type, full_text=full_text)

        payload = {
            "parent": {"database_id": database_id},
            "properties": page_properties,
        }

        created = await self._request("POST", "pages", json_body=payload)
        page_id = created.get("id")
        if page_id and children:
            await self._append_children(page_id, children)

        res = {
            "status": "ok",
            "action": "upload_file",
            "page_id": page_id,
            "url": created.get("url"),
            "file": source_path,
            "doc_type": actual_type,
            "title": page_title,
            "database_id": database_id,
            "note": "Stored as a database page with metadata and rendered Notion blocks.",
        }
        return json.dumps(res, ensure_ascii=False, indent=2)

    async def _list_items(self, doc_type: str = "auto", limit: int = 10) -> str:
        limit = max(1, min(limit, 100))
        if doc_type and doc_type.strip().lower() != "auto":
            database_id = self._resolve_target_database_id(doc_type)
            if not database_id:
                return "Error: no target database configured for this doc_type"

            query_body: dict[str, Any] = {"page_size": limit}
            db = await self._request("GET", f"databases/{database_id}")
            filter_obj = self._build_type_filter(
                db.get("properties", {}),
                self._normalize_doc_type(doc_type, None),
            )
            if filter_obj:
                query_body["filter"] = filter_obj

            data = await self._request(
                "POST",
                f"databases/{database_id}/query",
                json_body=query_body,
            )

            items = [self._page_brief(p) for p in data.get("results", [])]
            return json.dumps(
                {
                    "status": "ok",
                    "action": "list_items",
                    "count": len(items),
                    "database_id": database_id,
                    "items": items,
                },
                ensure_ascii=False,
                indent=2,
            )

        targets = self._get_target_databases("auto")
        if not targets:
            return "Error: no configured target databases"

        all_items: list[dict[str, Any]] = []
        per_target: list[dict[str, Any]] = []
        for alias, database_id in targets.items():
            data = await self._request(
                "POST",
                f"databases/{database_id}/query",
                json_body={"page_size": limit},
            )
            items = [self._page_brief(p) for p in data.get("results", [])]
            all_items.extend(items)
            per_target.append(
                {
                    "alias": alias,
                    "database_id": database_id,
                    "count": len(items),
                    "items": items,
                }
            )

        return json.dumps(
            {
                "status": "ok",
                "action": "list_items",
                "count": len(all_items),
                "targets": per_target,
            },
            ensure_ascii=False,
            indent=2,
        )

    async def _reclassify_item(self, page_id: str, doc_type: str) -> str:
        target_type = self._normalize_doc_type(doc_type, None)
        target_database_id = self._resolve_target_database_id(target_type)
        if not target_database_id:
            return "Error: no target database configured for this doc_type"

        page_data = await self._request("GET", f"pages/{self._normalize_page_id(page_id)}")
        parent_db = (page_data.get("parent") or {}).get("database_id", "")
        if parent_db and parent_db != target_database_id:
            return (
                "Error: reclassify across different databases is not supported by Notion page update. "
                "Use upload_file to create a new page in target database, then archive the original page manually."
            )

        db = await self._request("GET", f"databases/{target_database_id}")
        properties_schema = db.get("properties", {})

        patch_properties: dict[str, Any] = {}
        self._set_property_value(properties_schema, patch_properties, self.config.type_property, target_type)
        if not patch_properties:
            return (
                f"Error: cannot set type. Property '{self.config.type_property}' missing or unsupported "
                "(supported: select/multi_select/rich_text)."
            )

        updated = await self._request(
            "PATCH",
            f"pages/{self._normalize_page_id(page_id)}",
            json_body={"properties": patch_properties},
        )

        return json.dumps(
            {
                "status": "ok",
                "action": "reclassify_item",
                "page_id": updated.get("id"),
                "doc_type": target_type,
                "database_id": target_database_id,
                "url": updated.get("url"),
            },
            ensure_ascii=False,
            indent=2,
        )

    def _read_text_content(self, file_path: Path) -> str:
        mime, _ = mimetypes.guess_type(str(file_path))
        is_text = file_path.suffix.lower() in _TEXT_EXTENSIONS or (mime and mime.startswith("text/"))
        if not is_text:
            return ""
        try:
            content = file_path.read_text(encoding="utf-8", errors="ignore").strip()
        except Exception:
            return ""
        return content

    def _build_children(self, file_path: Path, doc_type: str, full_text: str) -> list[dict[str, Any]]:
        info = (
            f"Ingested by nanobot notion tool\\n"
            f"File: {file_path.name}\\n"
            f"Path: {file_path}\\n"
            f"Partition: {doc_type}"
        )
        children = [
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": self._inline_to_rich_text(info)},
            }
        ]

        if not full_text:
            return children

        if file_path.suffix.lower() in {".md", ".markdown"}:
            children.extend(self._markdown_to_blocks(full_text, base_dir=file_path.parent))
        else:
            children.extend(self._code_blocks_from_text(full_text, language="plain text"))

        return children

    async def _append_children(self, page_id: str, children: list[dict[str, Any]]) -> None:
        normalized_page_id = self._normalize_page_id(page_id)
        for idx in range(0, len(children), _MAX_APPEND_BLOCKS):
            chunk = children[idx:idx + _MAX_APPEND_BLOCKS]
            await self._request(
                "PATCH",
                f"blocks/{normalized_page_id}/children",
                json_body={"children": chunk},
            )

    def _markdown_to_blocks(self, markdown_text: str, base_dir: Path | None = None) -> list[dict[str, Any]]:
        lines = markdown_text.splitlines()
        blocks: list[dict[str, Any]] = []
        paragraph_buffer: list[str] = []

        in_code = False
        code_lang = "plain text"
        code_lines: list[str] = []
        code_fence = "```"

        i = 0
        while i < len(lines):
            line = lines[i]
            stripped = line.strip()

            code_start = self._parse_code_fence_open(stripped)
            if in_code:
                if self._is_code_fence_close(stripped, code_fence):
                    blocks.extend(self._code_blocks_from_text("\n".join(code_lines), code_lang))
                    in_code = False
                    code_lang = "plain text"
                    code_lines = []
                    code_fence = "```"
                else:
                    code_lines.append(line)
                i += 1
                continue

            if code_start:
                self._flush_paragraph_buffer(paragraph_buffer, blocks)
                in_code = True
                code_fence, code_info = code_start
                code_lang = self._normalize_code_language(code_info)
                code_lines = []
                i += 1
                continue

            # Block equation: $$ ... $$
            if stripped == "$$":
                self._flush_paragraph_buffer(paragraph_buffer, blocks)
                eq_lines: list[str] = []
                j = i + 1
                while j < len(lines) and lines[j].strip() != "$$":
                    eq_lines.append(lines[j])
                    j += 1
                expression = "\n".join(eq_lines).strip()
                if expression:
                    blocks.append({
                        "object": "block",
                        "type": "equation",
                        "equation": {"expression": expression},
                    })
                i = j + 1  # skip closing $$
                continue

            # Single-line block equation: $$ expression $$
            single_block_eq = re.match(r"^\$\$(.+)\$\$$", stripped)
            if single_block_eq:
                self._flush_paragraph_buffer(paragraph_buffer, blocks)
                expression = single_block_eq.group(1).strip()
                if expression:
                    blocks.append({
                        "object": "block",
                        "type": "equation",
                        "equation": {"expression": expression},
                    })
                i += 1
                continue

            if not stripped:
                self._flush_paragraph_buffer(paragraph_buffer, blocks)
                i += 1
                continue

            # Image: ![alt](src)
            img_match = _IMAGE_PATTERN.match(stripped)
            if img_match:
                self._flush_paragraph_buffer(paragraph_buffer, blocks)
                alt_text = img_match.group(1)
                img_src = img_match.group(2)
                img_url = self._resolve_image_url(img_src, base_dir)
                if img_url:
                    blocks.append(self._image_block(img_url, alt_text))
                else:
                    # Fallback: render as text link if image can't be resolved
                    fallback = f"[Image: {alt_text or img_src}]({img_src})"
                    blocks.extend(self._text_blocks("paragraph", fallback))
                i += 1
                continue

            # Table block (requires Markdown alignment row to reduce false positives)
            if self._is_table_line(stripped):
                maybe_table_lines = [line]
                j = i + 1
                while j < len(lines) and self._is_table_line(lines[j].strip()):
                    maybe_table_lines.append(lines[j])
                    j += 1
                table_block = self._build_table_block(maybe_table_lines)
                if table_block:
                    self._flush_paragraph_buffer(paragraph_buffer, blocks)
                    blocks.append(table_block)
                    i = j
                    continue

            heading = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", stripped)
            if heading:
                self._flush_paragraph_buffer(paragraph_buffer, blocks)
                level = len(heading.group(1))
                text = heading.group(2).strip()
                if level <= 3:
                    # H1/H2/H3 → native Notion headings
                    block_type = f"heading_{level}"
                    blocks.extend(self._text_blocks(block_type, text))
                elif level == 4:
                    # H4 → heading_3 (reuse deepest native heading)
                    blocks.extend(self._text_blocks("heading_3", text))
                else:
                    # H5/H6 → bold paragraph as visual sub-heading
                    blocks.extend(self._bold_paragraph(text))
                i += 1
                continue

            if re.match(r"^([-*_])\1{2,}\s*$", stripped):
                self._flush_paragraph_buffer(paragraph_buffer, blocks)
                blocks.append({"object": "block", "type": "divider", "divider": {}})
                i += 1
                continue

            # --- List items (bullet / numbered / todo) with nesting support ---
            list_parsed = self._parse_list_item(line)
            if list_parsed is not None:
                self._flush_paragraph_buffer(paragraph_buffer, blocks)
                # Collect the full contiguous run of list lines starting at i
                run_lines: list[str] = [line]
                j = i + 1
                while j < len(lines):
                    nxt = lines[j]
                    # Continue the run if the next line is also a list item
                    if self._parse_list_item(nxt) is not None:
                        run_lines.append(nxt)
                        j += 1
                    else:
                        break
                blocks.extend(self._build_nested_list_blocks(run_lines))
                i = j
                continue

            quote = re.match(r"^\s*>\s?(.*)$", line)
            if quote:
                self._flush_paragraph_buffer(paragraph_buffer, blocks)
                blocks.extend(self._text_blocks("quote", quote.group(1).strip() or " "))
                i += 1
                continue

            paragraph_buffer.append(line)
            i += 1

        if in_code:
            blocks.extend(self._code_blocks_from_text("\n".join(code_lines), code_lang))

        self._flush_paragraph_buffer(paragraph_buffer, blocks)
        return blocks

    def _flush_paragraph_buffer(self, buffer: list[str], blocks: list[dict[str, Any]]) -> None:
        if not buffer:
            return
        text = "\n".join(buffer).strip()
        if text:
            blocks.extend(self._text_blocks("paragraph", text))
        buffer.clear()

    # ------------------------------------------------------------------
    # List nesting helpers
    # ------------------------------------------------------------------

    _RE_TODO = re.compile(r"^(\s*)[-*]\s+\[( |x|X)\]\s+(.+)$")
    _RE_BULLET = re.compile(r"^(\s*)[-*]\s+(.+)$")
    _RE_NUMBERED = re.compile(r"^(\s*)\d+[.)]\s+(.+)$")

    @staticmethod
    def _parse_list_item(line: str):
        """Return (indent: int, item_type: str, text: str, checked: bool|None) or None.

        item_type is one of: 'bulleted_list_item', 'numbered_list_item', 'to_do'.
        """
        m = NotionTool._RE_TODO.match(line)
        if m:
            return (len(m.group(1)), "to_do", m.group(3).strip(), m.group(2).lower() == "x")
        m = NotionTool._RE_NUMBERED.match(line)
        if m:
            return (len(m.group(1)), "numbered_list_item", m.group(2).strip(), None)
        m = NotionTool._RE_BULLET.match(line)
        if m:
            return (len(m.group(1)), "bulleted_list_item", m.group(2).strip(), None)
        return None

    def _make_list_block(self, item_type: str, text: str, checked=None,
                         children: list | None = None) -> dict[str, Any]:
        """Create a single list-item block (optionally with children)."""
        rich_text = self._inline_to_rich_text(text)
        if not rich_text:
            rich_text = self._inline_to_rich_text(" ")
        body: dict[str, Any] = {"rich_text": rich_text}
        if item_type == "to_do":
            body["checked"] = bool(checked)
        if children:
            body["children"] = children
        return {
            "object": "block",
            "type": item_type,
            item_type: body,
        }

    def _build_nested_list_blocks(self, run_lines: list[str]) -> list[dict[str, Any]]:
        """Parse a contiguous run of list lines and return nested Notion blocks.

        Notion allows at most **2 levels of nesting** (children of children).
        Deeper items are clamped to the deepest allowed level.
        """
        # Parse all items: (indent, type, text, checked)
        items: list[tuple[int, str, str, bool | None]] = []
        for ln in run_lines:
            parsed = self._parse_list_item(ln)
            if parsed is not None:
                items.append(parsed)

        if not items:
            return []

        # Determine indent levels: map raw indent values to 0, 1, 2 ...
        unique_indents = sorted(set(it[0] for it in items))
        indent_map = {raw: idx for idx, raw in enumerate(unique_indents)}

        # Clamp to max 3 levels (0, 1, 2) because Notion allows top + 2 nesting
        _MAX_DEPTH = 3  # levels 0, 1, 2
        normalised: list[tuple[int, str, str, bool | None]] = [
            (min(indent_map[it[0]], _MAX_DEPTH - 1), it[1], it[2], it[3])
            for it in items
        ]

        # Build tree iteratively using a stack.
        # Stack entries: (level, block_dict, children_list_ref)
        # We build top-level results and attach children in-place.
        top_blocks: list[dict[str, Any]] = []

        # Stack tracks the "path" of ancestors.  Each entry is
        # (level, children_list) where children_list is the list that
        # the item was appended to (so siblings go into the same list).
        stack: list[tuple[int, list[dict[str, Any]]]] = []

        for level, itype, itext, ichecked in normalised:
            block = self._make_list_block(itype, itext, checked=ichecked)

            if level == 0:
                # Top-level item
                top_blocks.append(block)
                stack = [(0, top_blocks)]
            else:
                # Pop stack until we find the parent level (< current level)
                while stack and stack[-1][0] >= level:
                    stack.pop()

                if not stack:
                    # Safety fallback: treat as top-level
                    top_blocks.append(block)
                    stack = [(0, top_blocks)]
                else:
                    # Attach as child of the last block in the parent's list
                    parent_list = stack[-1][1]
                    parent_block = parent_list[-1]
                    parent_type = parent_block["type"]
                    if "children" not in parent_block[parent_type]:
                        parent_block[parent_type]["children"] = []
                    children_ref = parent_block[parent_type]["children"]
                    children_ref.append(block)
                    stack.append((level, children_ref))

        return top_blocks

    # ------------------------------------------------------------------

    def _text_blocks(self, block_type: str, text: str) -> list[dict[str, Any]]:
        rich_text = self._inline_to_rich_text(text)
        if not rich_text:
            rich_text = self._inline_to_rich_text(" ")
        return [{
            "object": "block",
            "type": block_type,
            block_type: {"rich_text": rich_text},
        }]

    def _to_do_block(self, text: str, checked: bool = False) -> dict[str, Any]:
        rich_text = self._inline_to_rich_text(text)
        if not rich_text:
            rich_text = self._inline_to_rich_text(" ")
        return {
            "object": "block",
            "type": "to_do",
            "to_do": {
                "rich_text": rich_text,
                "checked": checked,
            },
        }

    def _bold_paragraph(self, text: str) -> list[dict[str, Any]]:
        """Render H5/H6 headings as bold paragraphs (Notion has no heading_4+)."""
        rich_text: list[dict[str, Any]] = []
        for chunk in self._split_chunks(text, _MAX_RICH_TEXT_CHARS):
            rich_text.append({
                "type": "text",
                "text": {"content": chunk},
                "annotations": {
                    "bold": True,
                    "italic": False,
                    "strikethrough": False,
                    "underline": False,
                    "code": False,
                    "color": "default",
                },
            })
        return [{
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": rich_text},
        }]

    def _code_blocks_from_text(self, text: str, language: str = "plain text") -> list[dict[str, Any]]:
        if not text:
            return []
        notion_language = self._to_notion_code_language(language)
        blocks: list[dict[str, Any]] = []
        for chunk in self._split_chunks(text, _MAX_RICH_TEXT_CHARS):
            blocks.append({
                "object": "block",
                "type": "code",
                "code": {
                    "language": notion_language,
                    "rich_text": [{"type": "text", "text": {"content": chunk}}],
                },
            })
        return blocks

    def _inline_to_rich_text(self, text: str) -> list[dict[str, Any]]:
        rich_text: list[dict[str, Any]] = []
        cursor = 0
        for match in _INLINE_PATTERN.finditer(text):
            start, end = match.span()
            if start > cursor:
                self._append_text_segment(rich_text, text[cursor:start], {})

            if match.group("math") is not None:
                expression = match.group("math").strip()
                if expression:
                    rich_text.append({
                        "type": "equation",
                        "equation": {"expression": expression},
                        "annotations": self._default_annotations(),
                    })
            elif match.group("code") is not None:
                token = match.group("code")
                self._append_text_segment(rich_text, token[1:-1], {"code": True})
            elif match.group("link") is not None:
                label, url = self._parse_markdown_link(match.group("link"))
                if url and self._is_valid_notion_link_url(url):
                    self._append_text_segment(rich_text, label or url, {}, link_url=url)
                else:
                    # Notion text links require valid URL. For in-page anchors or local paths,
                    # gracefully degrade to plain text to avoid API validation errors.
                    self._append_text_segment(rich_text, label or match.group("link"), {})
            elif match.group("autolink") is not None:
                url = match.group("autolink")[1:-1].strip()
                if url:
                    self._append_text_segment(rich_text, url, {}, link_url=url)
            elif match.group("bolditalic") is not None:
                token = match.group("bolditalic")
                inner = self._inline_to_rich_text(token[3:-3])
                rich_text.extend(self._apply_annotation_to_nodes(inner, {"bold": True, "italic": True}))
            elif match.group("bold") is not None:
                token = match.group("bold")
                inner = self._inline_to_rich_text(token[2:-2])
                rich_text.extend(self._apply_annotation_to_nodes(inner, {"bold": True}))
            elif match.group("strike") is not None:
                token = match.group("strike")
                inner = self._inline_to_rich_text(token[2:-2])
                rich_text.extend(self._apply_annotation_to_nodes(inner, {"strikethrough": True}))
            elif match.group("italic") is not None:
                token = match.group("italic")
                inner = self._inline_to_rich_text(token[1:-1])
                rich_text.extend(self._apply_annotation_to_nodes(inner, {"italic": True}))
            else:
                self._append_text_segment(rich_text, match.group(0), {})
            cursor = end

        if cursor < len(text):
            self._append_text_segment(rich_text, text[cursor:], {})
        return rich_text

    def _apply_annotation_to_nodes(
        self,
        nodes: list[dict[str, Any]],
        extra: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Overlay extra annotation flags onto a list of rich-text nodes.

        Equation nodes don't carry annotations, so they are left unchanged.
        For text nodes the extra flags are merged into the existing annotations.
        This lets bold/italic/strike wrappers compose correctly with nested links
        and inline code (e.g. **[text](url)** → bold link node).
        """
        result: list[dict[str, Any]] = []
        for node in nodes:
            if node.get("type") == "equation":
                result.append(node)
                continue
            # Deep-copy the node to avoid mutating cached objects
            new_node = {
                "type": node["type"],
                "text": dict(node["text"]),
            }
            if "link" in node["text"]:
                new_node["text"]["link"] = node["text"]["link"]
            existing = node.get("annotations", {})
            merged = dict(existing)
            for k, v in extra.items():
                merged[k] = v
            new_node["annotations"] = self._default_annotations(merged)
            result.append(new_node)
        return result

    def _default_annotations(self, annotations: dict[str, Any] | None = None) -> dict[str, Any]:
        merged = {
            "bold": False,
            "italic": False,
            "strikethrough": False,
            "underline": False,
            "code": False,
            "color": "default",
        }
        if annotations:
            for key, value in annotations.items():
                if key == "color":
                    merged[key] = str(value) if value else "default"
                elif key in merged:
                    merged[key] = bool(value)
        return merged

    def _append_text_segment(
        self,
        rich_text: list[dict[str, Any]],
        content: str,
        annotations: dict[str, Any],
        link_url: str | None = None,
    ) -> None:
        if not content:
            return
        for chunk in self._split_chunks(content, _MAX_RICH_TEXT_CHARS):
            item: dict[str, Any] = {
                "type": "text",
                "text": {"content": chunk},
            }
            if link_url:
                item["text"]["link"] = {"url": link_url}
            if annotations or link_url:
                item["annotations"] = self._default_annotations(annotations)
            rich_text.append(item)

    def _build_table_block(self, lines: list[str]) -> dict[str, Any] | None:
        rows: list[list[str]] = []
        for raw in lines:
            row = raw.strip()
            if not row:
                continue
            cells = self._split_table_cells(row)
            if len(cells) < 2:
                return None
            rows.append(cells)

        # Require at least header + alignment row for Markdown table.
        # This avoids false positives for normal paragraphs containing '|'.
        if len(rows) < 2:
            return None
        if not self._is_alignment_row(rows[1]):
            return None

        has_column_header = True
        rows.pop(1)  # remove alignment row

        if not rows:
            return None

        width = max(len(r) for r in rows)
        normalized_rows: list[dict[str, Any]] = []
        for row in rows:
            normalized = row + [""] * (width - len(row))
            normalized_rows.append({
                "object": "block",
                "type": "table_row",
                "table_row": {
                    "cells": [self._inline_to_rich_text(cell or " ") for cell in normalized]
                },
            })

        return {
            "object": "block",
            "type": "table",
            "table": {
                "table_width": width,
                "has_column_header": has_column_header,
                "has_row_header": False,
                "children": normalized_rows,
            },
        }

    def _is_alignment_row(self, cells: list[str]) -> bool:
        if not cells:
            return False
        return all(bool(re.match(r"^:?-{3,}:?$", c.strip())) for c in cells)

    def _is_table_line(self, line: str) -> bool:
        if "|" not in line:
            return False
        cells = self._split_table_cells(line)
        return len(cells) >= 2

    def _split_table_cells(self, line: str) -> list[str]:
        row = line.strip()
        if row.startswith("|"):
            row = row[1:]
        if row.endswith("|"):
            row = row[:-1]

        cells: list[str] = []
        buf: list[str] = []

        in_code = False
        in_math = False
        depth_round = 0
        depth_square = 0
        depth_curly = 0

        i = 0
        while i < len(row):
            ch = row[i]

            if ch == "\\" and i + 1 < len(row):
                next_ch = row[i + 1]
                if next_ch == "|":
                    # Only consume backslash for escaped pipe '\|' → '|'
                    buf.append("|")
                    i += 2
                else:
                    # Preserve backslash as-is (critical for LaTeX: \frac, \sum, \mathcal, etc.)
                    buf.append(ch)
                    i += 1
                continue

            if ch == "`":
                in_code = not in_code
                buf.append(ch)
                i += 1
                continue

            if not in_code and ch == "$":
                in_math = not in_math
                buf.append(ch)
                i += 1
                continue

            if not in_code and not in_math:
                if ch == "(":
                    depth_round += 1
                elif ch == ")" and depth_round > 0:
                    depth_round -= 1
                elif ch == "[":
                    depth_square += 1
                elif ch == "]" and depth_square > 0:
                    depth_square -= 1
                elif ch == "{":
                    depth_curly += 1
                elif ch == "}" and depth_curly > 0:
                    depth_curly -= 1

                if ch == "|" and depth_round == 0 and depth_square == 0 and depth_curly == 0:
                    cells.append("".join(buf).strip())
                    buf = []
                    i += 1
                    continue

            buf.append(ch)
            i += 1

        cells.append("".join(buf).strip())
        return cells

    def _parse_markdown_link(self, token: str) -> tuple[str, str]:
        match = re.match(r"^\[([^\]]+)\]\((.+)\)$", token)
        if not match:
            return token, ""

        label = match.group(1).strip()
        url = match.group(2).strip()
        if url.startswith("<") and url.endswith(">"):
            url = url[1:-1].strip()
        return label, url

    def _is_valid_notion_link_url(self, url: str) -> bool:
        if not url:
            return False
        parsed = urlparse(url)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)

    def _split_chunks(self, text: str, max_chars: int) -> list[str]:
        if not text:
            return []
        chunks: list[str] = []
        start = 0
        while start < len(text):
            end = min(start + max_chars, len(text))
            chunks.append(text[start:end])
            start = end
        return chunks

    def _parse_code_fence_open(self, stripped_line: str) -> tuple[str, str] | None:
        """Parse Markdown code-fence opener.

        Supports:
        - ```
        - ```python
        - ```plain text
        - ~~~ts
        - ```python title="x.py"   (captures full info string)
        """
        if not stripped_line:
            return None

        match = re.match(r"^(`{3,}|~{3,})(.*)$", stripped_line)
        if not match:
            return None

        fence = match.group(1)
        info = match.group(2).strip()
        return fence, info

    def _is_code_fence_close(self, stripped_line: str, opening_fence: str) -> bool:
        if not stripped_line or not opening_fence:
            return False

        # Closing fence must use same marker type and at least opening length.
        marker = opening_fence[0]
        if marker not in {"`", "~"}:
            return False

        required_len = len(opening_fence)
        match = re.match(r"^([`~]+)\s*$", stripped_line)
        if not match:
            return False

        run = match.group(1)
        return run[0] == marker and len(run) >= required_len

    def _normalize_code_language(self, info: str) -> str:
        if not info:
            return "plain text"

        # Keep whole info-string if it does not look like key=value metadata.
        # This preserves common names like "plain text" and "objective c".
        lowered = info.strip().lower()
        if not lowered:
            return "plain text"

        # If metadata style exists (e.g., "python title=..."), use first token as language.
        if re.search(r"\b\w+\s*=", info):
            lang = info.split()[0]
            return lang or "plain text"

        return info

    def _to_notion_code_language(self, language: str | None) -> str:
        """Normalize language token to Notion accepted enum, fallback to plain text."""
        lang = (language or "").strip().lower()
        if not lang:
            return "plain text"

        # Apply explicit aliases first.
        lang = _NOTION_CODE_LANGUAGE_ALIASES.get(lang, lang)

        # Normalize common separators for matching.
        compact = re.sub(r"[\s_\-]+", " ", lang).strip()
        compact = _NOTION_CODE_LANGUAGE_ALIASES.get(compact, compact)

        if compact in _NOTION_CODE_LANGUAGES:
            return compact

        # Try exact no-space forms for aliases like "objectivec".
        nospace = compact.replace(" ", "")
        mapped = _NOTION_CODE_LANGUAGE_ALIASES.get(nospace)
        if mapped and mapped in _NOTION_CODE_LANGUAGES:
            return mapped

        return "plain text"

    def _build_type_filter(self, schema: dict[str, Any], doc_type: str) -> dict[str, Any] | None:
        prop_name = self.config.type_property
        prop = schema.get(prop_name)
        if not prop:
            return None

        prop_type = prop.get("type")
        if prop_type == "select":
            return {"property": prop_name, "select": {"equals": doc_type}}
        if prop_type == "multi_select":
            return {"property": prop_name, "multi_select": {"contains": doc_type}}
        if prop_type == "rich_text":
            return {"property": prop_name, "rich_text": {"contains": doc_type}}
        return None

    def _set_property_value(
        self,
        schema: dict[str, Any],
        container: dict[str, Any],
        property_name: str,
        value: str,
    ) -> None:
        prop = schema.get(property_name)
        if not prop or not value:
            return

        prop_type = prop.get("type")
        text_value = value[:1800]

        if prop_type == "rich_text":
            container[property_name] = {
                "rich_text": [{"type": "text", "text": {"content": text_value}}]
            }
            return

        if prop_type == "select":
            container[property_name] = {"select": {"name": value}}
            return

        if prop_type == "multi_select":
            container[property_name] = {"multi_select": [{"name": value}]}
            return

        if prop_type == "url":
            # Local path is not an URL, skip.
            return

    def _normalize_doc_type(self, doc_type: str, file_path: Path | None) -> str:
        candidate = (doc_type or "").strip()
        configured_map = self._configured_type_map()
        if candidate and candidate.lower() != "auto":
            return candidate

        source = (str(file_path).lower() if file_path else "")

        if configured_map:
            if "log" in configured_map and any(kw in source for kw in ["log", "日志", "trace"]):
                return "log"
            if "reports" in configured_map and any(
                kw in source for kw in ["report", "日报", "周报", "月报", "总结", "analysis", "分析"]
            ):
                return "reports"
            if "notes" in configured_map:
                return "notes"
            return next(iter(configured_map.keys()))

        return "general"

    def _resolve_target_database_id(self, doc_type: str) -> str:
        normalized = self._normalize_doc_type(doc_type, None)
        configured_map = self._configured_type_map()
        if normalized in configured_map:
            return configured_map[normalized]
        if self.config.database_id:
            return self.config.database_id
        for _, db_id in self._get_target_databases("auto").items():
            if db_id:
                return db_id
        return ""

    def _get_target_databases(self, doc_type: str = "auto") -> dict[str, str]:
        targets: dict[str, str] = {}
        normalized = self._normalize_doc_type(doc_type, None)
        if normalized and normalized.lower() != "auto" and (doc_type or "").strip().lower() != "auto":
            db_id = self._resolve_target_database_id(normalized)
            if db_id:
                targets[normalized] = db_id
            return targets

        configured_map = self._configured_type_map()
        for type_name, db_id in configured_map.items():
            if db_id:
                targets[type_name] = db_id

        if self.config.database_id:
            targets["default"] = self.config.database_id

        unique_targets: dict[str, str] = {}
        seen_ids: set[str] = set()
        for alias, db_id in targets.items():
            if db_id and db_id not in seen_ids:
                unique_targets[alias] = db_id
                seen_ids.add(db_id)
        return unique_targets

    def _has_any_database_configured(self) -> bool:
        if self.config.database_id:
            return True
        return any(bool(v.strip()) for v in self.config.type_database_map.values())

    def _configured_type_map(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for key, value in self.config.type_database_map.items():
            type_key = (key or "").strip()
            db_id = (value or "").strip()
            if type_key and db_id:
                result[type_key] = db_id
        return result

    def _get_partition_labels(self) -> list[str]:
        labels: list[str] = []
        configured_map = self._configured_type_map()
        for key in configured_map.keys():
            if key not in labels:
                labels.append(key)
        return labels

    def _find_title_property(self, schema: dict[str, Any]) -> str | None:
        preferred = self.config.title_property
        if preferred in schema and schema[preferred].get("type") == "title":
            return preferred
        for name, spec in schema.items():
            if spec.get("type") == "title":
                return name
        return None

    def _build_title_value(self, value: str) -> dict[str, Any]:
        text = (value or "Untitled")[:200]
        return {"title": [{"type": "text", "text": {"content": text}}]}

    def _page_brief(self, page: dict[str, Any]) -> dict[str, Any]:
        title = "Untitled"
        for _, prop in page.get("properties", {}).items():
            if prop.get("type") == "title":
                title = self._plain_title(prop.get("title", [])) or "Untitled"
                break

        return {
            "page_id": page.get("id"),
            "title": title,
            "doc_type": self._extract_type(page.get("properties", {})),
            "last_edited_time": page.get("last_edited_time"),
            "url": page.get("url"),
        }

    def _extract_type(self, properties: dict[str, Any]) -> str:
        prop = properties.get(self.config.type_property)
        if not prop:
            return ""
        prop_type = prop.get("type")
        if prop_type == "select":
            return (prop.get("select") or {}).get("name") or ""
        if prop_type == "multi_select":
            items = prop.get("multi_select") or []
            return ",".join([x.get("name", "") for x in items if x.get("name")])
        if prop_type == "rich_text":
            return self._plain_title(prop.get("rich_text", []))
        return ""

    def _plain_title(self, rich_text: list[dict[str, Any]]) -> str:
        return "".join([(rt.get("plain_text") or "") for rt in rich_text]).strip()

    def _normalize_page_id(self, page_id: str) -> str:
        raw = page_id.strip().replace("-", "")
        if len(raw) == 32:
            return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"
        return page_id.strip()

    def _resolve_path(self, path: str) -> Path:
        resolved = Path(path).expanduser().resolve()
        if self._allowed_dir and not str(resolved).startswith(str(self._allowed_dir.resolve())):
            raise PermissionError(f"Path {path} is outside allowed directory {self._allowed_dir}")
        return resolved

    async def _request(
        self,
        method: str,
        endpoint: str,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"https://api.notion.com/v1/{endpoint.lstrip('/')}"
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Notion-Version": self.config.notion_version,
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=self.config.timeout) as client:
            response = await client.request(method.upper(), url, headers=headers, json=json_body)

        if response.status_code >= 400:
            detail = response.text
            raise RuntimeError(f"Notion API error {response.status_code}: {detail}")

        if response.text:
            return response.json()
        return {}

    # ── Cloudinary image upload ──────────────────────────────────────

    def _upload_image_to_cloudinary(self, file_path: Path) -> str | None:
        """Upload a local image to Cloudinary and return its public URL.

        Returns None if Cloudinary is not configured or upload fails.
        """
        cfg = self.config.cloudinary
        if not cfg.enabled:
            logger.warning("Cloudinary not configured; skipping image upload for %s", file_path)
            return None

        import requests  # sync is fine here; called during block building

        upload_url = f"https://api.cloudinary.com/v1_1/{cfg.cloud_name}/image/upload"

        try:
            with open(file_path, "rb") as f:
                resp = requests.post(
                    upload_url,
                    data={"api_key": cfg.api_key},
                    files={"file": (file_path.name, f)},
                    auth=(cfg.api_key, cfg.api_secret),
                    timeout=60,
                )
            if resp.status_code == 200:
                url = resp.json().get("secure_url")
                logger.info("Uploaded image to Cloudinary: %s → %s", file_path.name, url)
                return url
            else:
                logger.error("Cloudinary upload failed (%s): %s", resp.status_code, resp.text[:300])
                return None
        except Exception as e:
            logger.error("Cloudinary upload error: %s", e)
            return None

    def _resolve_image_url(self, src: str, base_dir: Path | None = None) -> str | None:
        """Resolve an image src to a public URL.

        - If src is already an http(s) URL, return as-is.
        - If src is a local path, upload to Cloudinary and return URL.
        - Returns None if resolution fails.
        """
        if src.startswith("http://") or src.startswith("https://"):
            return src

        # Resolve relative path against base_dir
        if base_dir:
            img_path = (base_dir / src).resolve()
        else:
            img_path = Path(src).resolve()

        if not img_path.exists() or not img_path.is_file():
            logger.warning("Image file not found: %s", img_path)
            return None

        if img_path.suffix.lower() not in _IMAGE_EXTENSIONS:
            logger.warning("Unsupported image format: %s", img_path.suffix)
            return None

        return self._upload_image_to_cloudinary(img_path)

    def _image_block(self, url: str, caption: str = "") -> dict[str, Any]:
        """Create a Notion image block from an external URL."""
        block: dict[str, Any] = {
            "object": "block",
            "type": "image",
            "image": {
                "type": "external",
                "external": {"url": url},
            },
        }
        if caption:
            block["image"]["caption"] = [{
                "type": "text",
                "text": {"content": caption[:_MAX_RICH_TEXT_CHARS]},
            }]
        return block
