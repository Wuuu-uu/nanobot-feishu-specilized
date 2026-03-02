"""PDF parsing tool backed by MinerU KIE HTTP API.

Extracts full.md **and** images/ from the result ZIP so that downstream
tools (e.g. Notion uploader) can resolve local image paths.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from urllib.parse import urlparse
from zipfile import ZipFile

import requests

from nanobot.agent.tools.base import Tool
from nanobot.config.schema import MineruConfig

logger = logging.getLogger(__name__)

# Default persistent output directory (under workspace)
_DEFAULT_OUTPUT_DIR = Path.home() / ".nanobot" / "workspace" / "mineru_outputs"


class MineruPdfParseTool(Tool):
    """Parse a PDF using MinerU KIE and return extracted text with metadata.

    The result ZIP is extracted to a persistent local directory so that
    ``full.md`` and its companion ``images/`` folder are both preserved.
    The returned text includes a ``local_output_dir`` metadata field that
    points to the extraction directory — downstream tools can use this as
    ``base_dir`` when resolving ``![](images/…)`` references.
    """

    def __init__(self, config: MineruConfig, allowed_dir: Path | None = None):
        self.config = config
        self._allowed_dir = allowed_dir
        # Resolve output directory
        if config.output_dir:
            self._output_root = Path(config.output_dir)
        else:
            self._output_root = _DEFAULT_OUTPUT_DIR

    @property
    def name(self) -> str:
        return "parse_pdf_mineru"

    @property
    def description(self) -> str:
        return (
            "Parse a PDF via MinerU API and return full.md content with metadata. "
            "Images extracted from the PDF are saved locally alongside full.md so "
            "that image references like ![](images/xxx.jpg) can be resolved."
        )

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

        # ── Step 1: Submit task & poll ──────────────────────────────
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

        # ── Step 2: Download ZIP & extract to persistent dir ────────
        task_id = result["task_id"]
        output_dir = self._output_root / task_id

        try:
            extract_info = await asyncio.to_thread(
                _download_and_extract,
                result["full_zip_url"],
                output_dir,
            )
        except Exception as e:
            return f"Error: Failed to download or extract result: {str(e)}"

        # ── Step 3: Build response ──────────────────────────────────
        md_path: Path = extract_info["full_md_path"]
        images_dir: Path | None = extract_info.get("images_dir")
        image_count: int = extract_info.get("image_count", 0)

        text = md_path.read_text(encoding="utf-8", errors="replace").strip()

        metadata = {
            "url": url,
            "task_id": task_id,
            "full_zip_url": result["full_zip_url"],
            "api_url": self.config.api_url,
            "model_version": effective_model,
            "timeout": effective_timeout,
            "poll_interval": effective_poll,
            "local_output_dir": str(output_dir),
            "local_full_md": str(md_path),
            "image_count": image_count,
        }
        if images_dir and images_dir.exists():
            metadata["local_images_dir"] = str(images_dir)

        logger.info(
            "MinerU extraction complete: %s, %d images, output=%s",
            task_id, image_count, output_dir,
        )

        parts = [f"Metadata:\n{_format_metadata(metadata)}"]
        if image_count > 0:
            parts.append(
                f"\n📸 {image_count} images extracted and saved to: {images_dir}\n"
                f"Image references in full.md use relative paths like ![](images/xxx.jpg).\n"
                f"When uploading this file to Notion, use the full.md path directly — "
                f"images will be auto-uploaded to Cloudinary."
            )
        if text:
            parts.append(f"\nText:\n{text}")
        else:
            parts.append("\nText:\n<empty>")

        return "\n".join(parts)


# ── Helpers ──────────────────────────────────────────────────────────

def _format_metadata(metadata: dict[str, Any]) -> str:
    lines = [f"- {key}: {value}" for key, value in metadata.items()]
    return "\n".join(lines)


def _download_and_extract(zip_url: str, output_dir: Path) -> dict[str, Any]:
    """Download the MinerU result ZIP and extract to *output_dir*.

    Returns a dict with keys:
      - full_md_path: Path to full.md
      - images_dir: Path to images/ directory (may not exist if no images)
      - image_count: number of image files found
    """
    parsed = urlparse(zip_url)
    zip_name = Path(parsed.path).name or "result.zip"

    # Use a temp dir for download, then move to persistent location
    with TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        zip_path = tmp_path / zip_name

        # Download ZIP
        with requests.get(zip_url, stream=True, timeout=120) as resp:
            resp.raise_for_status()
            with open(zip_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)

        # Extract to temp first
        extract_dir = tmp_path / "extracted"
        extract_dir.mkdir(parents=True, exist_ok=True)
        with ZipFile(zip_path, "r") as zf:
            zf.extractall(extract_dir)

        # Find full.md — it might be nested inside a subdirectory
        full_md_path = next(extract_dir.rglob("full.md"), None)
        if not full_md_path:
            raise FileNotFoundError("full.md not found in extracted result")

        # The "content root" is the directory containing full.md
        content_root = full_md_path.parent

        # Move content root to persistent output_dir
        if output_dir.exists():
            shutil.rmtree(output_dir)
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(content_root, output_dir)

    # Verify
    final_md = output_dir / "full.md"
    if not final_md.exists():
        raise FileNotFoundError(f"full.md not found at {final_md}")

    images_dir = output_dir / "images"
    image_count = 0
    if images_dir.exists() and images_dir.is_dir():
        image_count = sum(1 for f in images_dir.iterdir() if f.is_file())

    logger.info(
        "Extracted MinerU result: %s (%d images)",
        output_dir, image_count,
    )

    return {
        "full_md_path": final_md,
        "images_dir": images_dir,
        "image_count": image_count,
    }
