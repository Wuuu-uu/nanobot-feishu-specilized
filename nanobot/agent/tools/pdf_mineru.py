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
from nanobot.utils.helpers import expand_path, get_workspace_path

logger = logging.getLogger(__name__)

# Default persistent output directory (under workspace)
_DEFAULT_OUTPUT_DIR = get_workspace_path() / "mineru_outputs"


class MineruPdfParseTool(Tool):
    """Parse documents using MinerU batch APIs and return extracted text.

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
            self._output_root = expand_path(config.output_dir)
        else:
            self._output_root = _DEFAULT_OUTPUT_DIR

    @property
    def name(self) -> str:
        return "parse_pdf_mineru"

    @property
    def description(self) -> str:
        return (
            "Parse local files or URLs via MinerU batch APIs and return full.md content with metadata. "
            "Images extracted from source files are saved locally alongside full.md so "
            "that image references like ![](images/xxx.jpg) can be resolved."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "urls": {
                    "type": "array",
                    "description": "Batch public URLs (max 200)",
                    "items": {"type": "string"},
                },
                "paths": {
                    "type": "array",
                    "description": "Batch local file paths (max 200)",
                    "items": {"type": "string"},
                },
                "model_version": {
                    "type": "string",
                    "description": "MinerU model version: pipeline, vlm, MinerU-HTML",
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
            "required": [],
        }

    async def execute(
        self,
        urls: list[str] | None = None,
        paths: list[str] | None = None,
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

        effective_timeout = timeout or self.config.timeout
        effective_poll = poll_interval or self.config.poll_interval
        effective_model = model_version or self.config.model_version

        # Keep backward compatibility for legacy single-value calls.
        legacy_url = kwargs.get("url")
        legacy_path = kwargs.get("path")

        normalized_urls = [str(u).strip() for u in (urls or []) if str(u).strip()]
        normalized_paths = [str(p).strip() for p in (paths or []) if str(p).strip()]
        if legacy_url and str(legacy_url).strip():
            normalized_urls.append(str(legacy_url).strip())
        if legacy_path and str(legacy_path).strip():
            normalized_paths.append(str(legacy_path).strip())

        if not normalized_urls and not normalized_paths:
            return "Error: Provide urls or paths. For single file, pass a one-item list."
        if normalized_urls and normalized_paths:
            return "Error: URL batch and local file batch cannot be mixed in one call."

        batch_size = len(normalized_urls) or len(normalized_paths)
        if batch_size > 200:
            return "Error: MinerU batch size cannot exceed 200 files per request."

        api_root = _resolve_mineru_api_root(self.config.api_url)
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.token}",
        }

        common_payload: dict[str, Any] = {}
        if effective_model:
            common_payload["model_version"] = effective_model

        mode = "url" if normalized_urls else "local"

        # Step 1: Submit batch task (URL mode) or apply upload URLs + upload files (local mode).
        def _run() -> dict[str, Any]:
            if mode == "url":
                endpoint = f"{api_root}/extract/task/batch"
                files_payload: list[dict[str, Any]] = []
                for u in normalized_urls:
                    parsed = urlparse(u)
                    if not parsed.scheme or not parsed.netloc:
                        raise ValueError(f"Invalid URL provided: {u}")
                    files_payload.append({"url": u})

                payload = {"files": files_payload, **common_payload}

                resp = requests.post(endpoint, headers=headers, json=payload, timeout=30)
                resp.raise_for_status()
                data = resp.json()
                if data.get("code") != 0:
                    raise RuntimeError(f"MinerU submit failed: code={data.get('code')} msg={data.get('msg')}")
                batch_id = data.get("data", {}).get("batch_id")
                if not batch_id:
                    raise RuntimeError("MinerU did not return batch_id for URL batch")
            else:
                endpoint = f"{api_root}/file-urls/batch"
                resolved_files: list[Path] = []
                apply_files_payload: list[dict[str, Any]] = []
                for raw_path in normalized_paths:
                    resolved = _resolve_local_path(raw_path, self._allowed_dir)
                    if not resolved.exists() or not resolved.is_file():
                        raise FileNotFoundError(f"Local file not found: {raw_path}")
                    apply_files_payload.append({"name": resolved.name})
                    resolved_files.append(resolved)

                payload = {"files": apply_files_payload, **common_payload}
                resp = requests.post(endpoint, headers=headers, json=payload, timeout=30)
                resp.raise_for_status()
                data = resp.json()
                if data.get("code") != 0:
                    raise RuntimeError(
                        f"MinerU apply upload URLs failed: code={data.get('code')} msg={data.get('msg')}"
                    )

                batch_id = data.get("data", {}).get("batch_id")
                file_urls = data.get("data", {}).get("file_urls") or []
                if not batch_id:
                    raise RuntimeError("MinerU did not return batch_id for local batch")
                if len(file_urls) != len(resolved_files):
                    raise RuntimeError("MinerU returned mismatched file_urls count")

                for idx, upload_url in enumerate(file_urls):
                    file_path = resolved_files[idx]
                    with open(file_path, "rb") as f:
                        upload_resp = requests.put(upload_url, data=f, timeout=120)
                    upload_resp.raise_for_status()

            # Step 2: Poll batch results.
            result_endpoint = f"{api_root}/extract-results/batch/{batch_id}"
            start = time.monotonic()
            while True:
                poll_resp = requests.get(result_endpoint, headers=headers, timeout=30)
                poll_resp.raise_for_status()
                poll_data = poll_resp.json()
                if poll_data.get("code") != 0:
                    raise RuntimeError(
                        f"MinerU batch polling failed: code={poll_data.get('code')} msg={poll_data.get('msg')}"
                    )

                extract_result = poll_data.get("data", {}).get("extract_result") or []
                if extract_result:
                    states = {str(item.get("state", "")).lower() for item in extract_result}
                    unfinished = {"waiting-file", "pending", "running", "converting"}
                    if len(extract_result) >= batch_size and states.isdisjoint(unfinished):
                        return {
                            "batch_id": batch_id,
                            "extract_result": extract_result,
                        }

                if time.monotonic() - start >= effective_timeout:
                    raise TimeoutError("MinerU batch polling timed out")

                time.sleep(effective_poll)

        try:
            result = await asyncio.to_thread(_run)
        except Exception as e:
            return f"Error: MinerU request failed: {str(e)}"

        # Step 3: Download ZIP outputs for done files and build response.
        batch_id = str(result["batch_id"])
        extract_result = list(result.get("extract_result") or [])
        output_root = self._output_root / batch_id

        sections: list[str] = []
        done_count = 0
        failed_count = 0
        other_count = 0

        for idx, item in enumerate(extract_result, start=1):
            state = str(item.get("state", "unknown")).lower()
            file_name = str(item.get("file_name") or f"file_{idx}")
            data_id = item.get("data_id")

            if state != "done":
                if state == "failed":
                    failed_count += 1
                else:
                    other_count += 1
                err_msg = item.get("err_msg") or ""
                progress = item.get("extract_progress")
                meta = {
                    "batch_id": batch_id,
                    "index": idx,
                    "file_name": file_name,
                    "data_id": data_id,
                    "state": state,
                    "err_msg": err_msg,
                    "extract_progress": progress,
                }
                sections.append(f"Result {idx}:\n{_format_metadata(meta)}")
                continue

            full_zip_url = item.get("full_zip_url")
            if not full_zip_url:
                failed_count += 1
                sections.append(
                    f"Result {idx}:\n"
                    f"- batch_id: {batch_id}\n"
                    f"- file_name: {file_name}\n"
                    f"- state: failed\n"
                    f"- err_msg: done state but missing full_zip_url"
                )
                continue

            target_dir = output_root / _safe_output_name(idx, file_name, data_id)
            try:
                extract_info = await asyncio.to_thread(
                    _download_and_extract,
                    str(full_zip_url),
                    target_dir,
                )
            except Exception as e:
                failed_count += 1
                sections.append(
                    f"Result {idx}:\n"
                    f"- batch_id: {batch_id}\n"
                    f"- file_name: {file_name}\n"
                    f"- state: failed\n"
                    f"- err_msg: failed to download or extract result: {e}"
                )
                continue

            done_count += 1
            md_path: Path = extract_info["full_md_path"]
            images_dir: Path | None = extract_info.get("images_dir")
            image_count: int = extract_info.get("image_count", 0)
            text = md_path.read_text(encoding="utf-8", errors="replace").strip()

            metadata = {
                "batch_id": batch_id,
                "index": idx,
                "file_name": file_name,
                "data_id": data_id,
                "state": state,
                "full_zip_url": full_zip_url,
                "api_root": api_root,
                "model_version": effective_model,
                "timeout": effective_timeout,
                "poll_interval": effective_poll,
                "local_output_dir": str(target_dir),
                "local_full_md": str(md_path),
                "image_count": image_count,
            }
            if images_dir and images_dir.exists():
                metadata["local_images_dir"] = str(images_dir)

            section_parts = [f"Result {idx} Metadata:\n{_format_metadata(metadata)}"]
            if image_count > 0:
                section_parts.append(
                    f"\nImage notes:\n"
                    f"- images extracted to: {images_dir}\n"
                    f"- references use relative paths like ![](images/xxx.jpg)"
                )
            section_parts.append(f"\nText:\n{text if text else '<empty>'}")
            sections.append("\n".join(section_parts))

        summary = {
            "mode": mode,
            "batch_id": batch_id,
            "total": len(extract_result),
            "done": done_count,
            "failed": failed_count,
            "other": other_count,
            "output_root": str(output_root),
        }
        logger.info(
            "MinerU batch complete: mode=%s batch_id=%s total=%d done=%d failed=%d other=%d",
            mode,
            batch_id,
            len(extract_result),
            done_count,
            failed_count,
            other_count,
        )

        parts = [f"Summary:\n{_format_metadata(summary)}"]
        if sections:
            parts.append("\n\n".join(sections))
        return "\n\n".join(parts)


# ── Helpers ──────────────────────────────────────────────────────────

def _format_metadata(metadata: dict[str, Any]) -> str:
    lines = [f"- {key}: {value}" for key, value in metadata.items()]
    return "\n".join(lines)


def _resolve_mineru_api_root(api_url: str) -> str:
    """Normalize configured API URL to MinerU v4 root, e.g. https://mineru.net/api/v4."""
    parsed = urlparse(api_url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"Invalid MinerU api_url configured: {api_url}")

    path = parsed.path or ""
    marker = "/api/v4"
    if marker in path:
        prefix = path.split(marker, 1)[0]
        return f"{parsed.scheme}://{parsed.netloc}{prefix}{marker}"

    return api_url.rstrip("/")


def _resolve_local_path(path: str, allowed_dir: Path | None) -> Path:
    """Resolve a local path and enforce optional allowed directory restriction."""
    resolved = Path(path).expanduser().resolve()
    if allowed_dir and not str(resolved).startswith(str(allowed_dir.resolve())):
        raise PermissionError(f"Path {path} is outside allowed directory {allowed_dir}")
    return resolved


def _safe_output_name(index: int, file_name: str, data_id: Any) -> str:
    stem = Path(file_name).stem or f"file_{index}"
    safe_stem = "".join(ch for ch in stem if ch.isalnum() or ch in ("-", "_", ".")) or f"file_{index}"
    if data_id:
        safe_data = "".join(ch for ch in str(data_id) if ch.isalnum() or ch in ("-", "_", "."))
        if safe_data:
            return f"{index:03d}_{safe_stem}_{safe_data}"
    return f"{index:03d}_{safe_stem}"


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
