"""PDF parsing tool backed by MinerU KIE HTTP API."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from urllib.parse import urlparse
from zipfile import ZipFile

import requests

from nanobot.agent.tools.base import Tool
from nanobot.config.schema import MineruConfig


class MineruPdfParseTool(Tool):
    """Parse a PDF using MinerU KIE and return extracted text with metadata."""

    def __init__(self, config: MineruConfig, allowed_dir: Path | None = None):
        self.config = config
        self._allowed_dir = allowed_dir

    @property
    def name(self) -> str:
        return "parse_pdf_mineru"

    @property
    def description(self) -> str:
        return "Parse a PDF via MinerU API and return full.md content with metadata."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Public URL to a PDF file",
                },
                "model_version": {
                    "type": "string",
                    "description": "Override MinerU model version (e.g. 'vlm')",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Override timeout (seconds) for MinerU polling",
                    "minimum": 1,
                },
                "poll_interval": {
                    "type": "integer",
                    "description": "Override poll interval (seconds) for MinerU polling",
                    "minimum": 1,
                },
            },
            "required": ["url"],
        }

    async def execute(
        self,
        url: str,
        model_version: str | None = None,
        timeout: int | None = None,
        poll_interval: int | None = None,
        **kwargs: Any,
    ) -> str:
        if not self.config.enabled:
            return "Error: MinerU tool is disabled. Enable tools.mineru in config.json."
        if not self.config.api_url:
            return "Error: MinerU api_url is not configured."
        if not self.config.token:
            return "Error: MinerU token is not configured."

        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return "Error: Invalid URL provided."

        effective_timeout = timeout or self.config.timeout
        effective_poll = poll_interval or self.config.poll_interval
        effective_model = model_version or self.config.model_version

        def _run() -> dict[str, Any]:
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.config.token}",
            }
            payload: dict[str, Any] = {"url": url}
            if effective_model:
                payload["model_version"] = effective_model

            create_resp = requests.post(
                self.config.api_url,
                headers=headers,
                json=payload,
                timeout=30,
            )
            create_resp.raise_for_status()
            create_data = create_resp.json()
            task_id = create_data.get("data", {}).get("task_id")
            if not task_id:
                raise RuntimeError("MinerU did not return task_id")

            poll_url = f"{self.config.api_url}/{task_id}"
            start = time.monotonic()
            while True:
                poll_resp = requests.get(poll_url, headers=headers, timeout=30)
                poll_resp.raise_for_status()
                poll_data = poll_resp.json()
                data = poll_data.get("data", {})
                state = data.get("state")

                if state == "done":
                    full_zip_url = data.get("full_zip_url")
                    if not full_zip_url:
                        raise RuntimeError("MinerU did not return full_zip_url")
                    return {
                        "task_id": task_id,
                        "full_zip_url": full_zip_url,
                    }

                if state == "failed":
                    message = data.get("message") or poll_data.get("message")
                    raise RuntimeError(f"MinerU task failed: {message or 'unknown error'}")

                if time.monotonic() - start >= effective_timeout:
                    raise TimeoutError("MinerU polling timed out")

                time.sleep(effective_poll)

        try:
            result = await asyncio.to_thread(_run)
        except Exception as e:
            return f"Error: MinerU request failed: {str(e)}"

        try:
            text = await asyncio.to_thread(
                _download_and_extract_full_md,
                result["full_zip_url"],
            )
        except Exception as e:
            return f"Error: Failed to download or extract result: {str(e)}"

        metadata = {
            "url": url,
            "task_id": result.get("task_id"),
            "full_zip_url": result.get("full_zip_url"),
            "api_url": self.config.api_url,
            "model_version": effective_model,
            "timeout": effective_timeout,
            "poll_interval": effective_poll,
        }

        if text:
            return f"Metadata:\n{_format_metadata(metadata)}\n\nText:\n{text}"
        return f"Metadata:\n{_format_metadata(metadata)}\n\nText:\n<empty>"


def _format_metadata(metadata: dict[str, Any]) -> str:
    lines = [f"- {key}: {value}" for key, value in metadata.items()]
    return "\n".join(lines)


def _download_and_extract_full_md(zip_url: str) -> str:
    parsed = urlparse(zip_url)
    zip_name = Path(parsed.path).name or "result.zip"

    with TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        zip_path = tmp_path / zip_name

        with requests.get(zip_url, stream=True, timeout=60) as resp:
            resp.raise_for_status()
            with open(zip_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)

        extract_dir = tmp_path / "extracted"
        extract_dir.mkdir(parents=True, exist_ok=True)
        with ZipFile(zip_path, "r") as zf:
            zf.extractall(extract_dir)

        full_md_path = next(extract_dir.rglob("full.md"), None)
        if not full_md_path:
            raise FileNotFoundError("full.md not found in extracted result")

        text = full_md_path.read_text(encoding="utf-8", errors="replace").strip()
        return text
