"""Image generation tool with optional Feishu delivery."""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx

from nanobot.agent.tools.base import Tool
from nanobot.config.schema import FeishuConfig, ImageGenConfig

try:
	import lark_oapi as lark
	from lark_oapi.api.im.v1 import (
		CreateImageRequest,
		CreateImageRequestBody,
		CreateMessageRequest,
		CreateMessageRequestBody,
	)
	FEISHU_AVAILABLE = True
except ImportError:
	FEISHU_AVAILABLE = False
	lark = None


class ImageGenerateTool(Tool):
	"""Generate images via a model API and optionally send via Feishu."""

	def __init__(
		self,
		config: ImageGenConfig,
		feishu_config: FeishuConfig | None = None,
		workspace: Path | None = None,
		allowed_dir: Path | None = None,
	) -> None:
		self.config = config
		self.feishu_config = feishu_config or FeishuConfig()
		self.workspace = workspace or Path.cwd()
		self._allowed_dir = allowed_dir
		self._default_channel = ""
		self._default_chat_id = ""

	def set_context(self, channel: str, chat_id: str) -> None:
		"""Set default channel and chat context for sending."""
		self._default_channel = channel
		self._default_chat_id = chat_id

	@property
	def name(self) -> str:
		return "image_generate"

	@property
	def description(self) -> str:
		return "Generate or edit an image via model API and optionally send it to Feishu."

	@property
	def parameters(self) -> dict[str, Any]:
		return {
			"type": "object",
			"properties": {
				"prompt": {
					"type": "string",
					"description": "Image generation prompt",
				},
				"image_path": {
					"type": "string",
					"description": "Optional single image path for editing",
				},
				"image_paths": {
					"type": "array",
					"description": "Optional list of image paths for editing",
					"items": {"type": "string"},
				},
				"aspect_ratio": {
					"type": "string",
					"description": "Aspect ratio like '1:1', '16:9', or 'original'",
					"default": "1:1",
				},
				"output_path": {
					"type": "string",
					"description": "Optional file path to save the image",
				},
				"send_to_user": {
					"type": "boolean",
					"description": "Send image to user via Feishu",
					"default": False,
				},
				"title": {
					"type": "string",
					"description": "Optional Feishu post title for image message",
				},
				"channel": {
					"type": "string",
					"description": "Optional target channel override",
				},
				"chat_id": {
					"type": "string",
					"description": "Optional target chat/user ID override",
				},
			},
			"required": ["prompt"],
		}

	async def execute(
		self,
		prompt: str,
		image_path: str | None = None,
		image_paths: list[str] | None = None,
		aspect_ratio: str = "1:1",
		output_path: str | None = None,
		send_to_user: bool = False,
		title: str | None = None,
		channel: str | None = None,
		chat_id: str | None = None,
		**kwargs: Any,
	) -> str:
		if not self.config.enabled:
			return "Error: image generation tool is disabled."
		if not self.config.api_base or not self.config.api_key or not self.config.model_name:
			return "Error: image generation config missing api_base/api_key/model_name."

		images = self._collect_images(image_path, image_paths)
		effective_ratio = self._resolve_aspect_ratio(aspect_ratio, images)
		full_prompt = self._build_prompt(prompt, effective_ratio)

		try:
			img_bytes, mime_type = await self._generate_image(full_prompt, images)
		except Exception as e:
			msg = str(e) or type(e).__name__
			return f"Error: image generation failed: {msg}"

		try:
			saved_path = await asyncio.to_thread(
				self._save_image,
				img_bytes,
				mime_type,
				output_path,
			)
		except Exception as e:
			return f"Error: failed to save image: {str(e)}"

		result_lines = [f"Saved image to: {saved_path}"]

		if send_to_user:
			target_channel = channel or self._default_channel
			target_chat = chat_id or self._default_chat_id
			if target_channel != "feishu":
				result_lines.append("Feishu send skipped: channel is not feishu")
				return "\n".join(result_lines)
			if not target_chat:
				result_lines.append("Feishu send skipped: chat_id missing")
				return "\n".join(result_lines)

			try:
				image_key = await asyncio.to_thread(
					self._upload_feishu_image,
					saved_path,
				)
				await asyncio.to_thread(
					self._send_feishu_image_post,
					image_key,
					target_chat,
					title or "Image",
				)
				result_lines.append(f"Feishu image sent: image_key={image_key}")
			except Exception as e:
				result_lines.append(f"Feishu send failed: {str(e)}")

		return "\n".join(result_lines)

	def _build_prompt(self, prompt: str, aspect_ratio: str) -> str:
		suffix = ""
		if aspect_ratio != "original":
			suffix = f" The image aspect ratio must be {aspect_ratio}."
		return f"Generate a high quality image based on the following description: {prompt}.{suffix}"

	async def _generate_image(
		self,
		full_prompt: str,
		images: list[dict[str, Any]] | None = None,
	) -> tuple[bytes, str]:
		url = urljoin(self.config.api_base.rstrip("/") + "/", "chat/completions")
		user_content: list[dict[str, Any]] = [{"type": "text", "text": full_prompt}]
		if images:
			user_content.extend(images)
		payload = {
			"model": self.config.model_name,
			"messages": [{"role": "user", "content": user_content}],
			"stream": False,
		}
		headers = {
			"Authorization": f"Bearer {self.config.api_key}",
			"Content-Type": "application/json",
		}

		async with httpx.AsyncClient(timeout=self.config.timeout) as client:
			response = await client.post(url, json=payload, headers=headers)
			response.raise_for_status()

		data = response.json()
		image_match = _extract_image_from_payload(data)
		if not image_match:
			detail = _describe_payload(data)
			raise RuntimeError(f"no base64 image data found in response ({detail})")

		mime_type, b64_data = image_match
		img_bytes = base64.b64decode(b64_data)
		return img_bytes, mime_type

	def _save_image(self, img_bytes: bytes, mime_type: str, output_path: str | None) -> str:
		extension = _mime_to_ext(mime_type)
		if output_path:
			target = _resolve_path(output_path, self._allowed_dir)
		else:
			stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
			target_dir = self.workspace / "outputs" / "images"
			target = target_dir / f"image_{stamp}.{extension}"
			if self._allowed_dir:
				target = _resolve_path(str(target), self._allowed_dir)

		target.parent.mkdir(parents=True, exist_ok=True)
		target.write_bytes(img_bytes)
		return str(target)

	def _collect_images(
		self,
		image_path: str | None,
		image_paths: list[str] | None,
	) -> list[dict[str, Any]]:
		paths: list[str] = []
		if image_path:
			paths.append(image_path)
		if image_paths:
			paths.extend(image_paths)

		images: list[dict[str, Any]] = []
		for raw in paths:
			path = _resolve_path(raw, self._allowed_dir)
			if not path.is_file():
				raise FileNotFoundError(f"Image not found: {raw}")
			mime = _guess_mime(path)
			if not mime or not mime.startswith("image/"):
				raise ValueError(f"Unsupported image type: {raw}")
			b64_data = base64.b64encode(path.read_bytes()).decode("ascii")
			images.append({
				"type": "image_url",
				"image_url": {"url": f"data:{mime};base64,{b64_data}"},
			})

		return images

	def _resolve_aspect_ratio(self, aspect_ratio: str, images: list[dict[str, Any]]) -> str:
		if images and (not aspect_ratio or aspect_ratio == "1:1"):
			return "original"
		return aspect_ratio or "1:1"

	def _upload_feishu_image(self, image_path: str) -> str:
		if not FEISHU_AVAILABLE:
			raise RuntimeError("Feishu SDK not installed")
		if not self.feishu_config.app_id or not self.feishu_config.app_secret:
			raise RuntimeError("Feishu app_id/app_secret not configured")

		client = lark.Client.builder() \
			.app_id(self.feishu_config.app_id) \
			.app_secret(self.feishu_config.app_secret) \
			.log_level(lark.LogLevel.INFO) \
			.build()

		with open(image_path, "rb") as f:
			request = CreateImageRequest.builder() \
				.request_body(
					CreateImageRequestBody.builder()
					.image_type("message")
					.image(f)
					.build()
				).build()

			response = client.im.v1.image.create(request)

		if not response.success():
			raise RuntimeError(f"image upload failed: code={response.code}, msg={response.msg}")

		return response.data.image_key

	def _send_feishu_image_post(self, image_key: str, chat_id: str, title: str) -> None:
		if not FEISHU_AVAILABLE:
			raise RuntimeError("Feishu SDK not installed")
		if not self.feishu_config.app_id or not self.feishu_config.app_secret:
			raise RuntimeError("Feishu app_id/app_secret not configured")

		client = lark.Client.builder() \
			.app_id(self.feishu_config.app_id) \
			.app_secret(self.feishu_config.app_secret) \
			.log_level(lark.LogLevel.INFO) \
			.build()

		if chat_id.startswith("oc_"):
			receive_id_type = "chat_id"
		else:
			receive_id_type = "open_id"

		post_content = {
			"zh_cn": {
				"title": title,
				"content": [[{"tag": "img", "image_key": image_key}]],
			}
		}
		content = json.dumps(post_content, ensure_ascii=False)

		request = CreateMessageRequest.builder() \
			.receive_id_type(receive_id_type) \
			.request_body(
				CreateMessageRequestBody.builder()
				.receive_id(chat_id)
				.msg_type("post")
				.content(content)
				.build()
			).build()

		response = client.im.v1.message.create(request)
		if not response.success():
			raise RuntimeError(f"image message send failed: code={response.code}, msg={response.msg}")


def _resolve_path(path: str, allowed_dir: Path | None = None) -> Path:
	resolved = Path(path).expanduser().resolve()
	if allowed_dir and not str(resolved).startswith(str(allowed_dir.resolve())):
		raise PermissionError(f"Path {path} is outside allowed directory {allowed_dir}")
	return resolved


def _extract_content(payload: dict[str, Any]) -> str | list[dict[str, Any]]:
	try:
		choices = payload.get("choices") or []
		if choices:
			message = choices[0].get("message") or {}
			return message.get("content") or ""
	except Exception:
		return ""
	return ""


def _extract_image_from_payload(payload: dict[str, Any]) -> tuple[str, str] | None:
	try:
		choices = payload.get("choices") or []
		if choices:
			message = choices[0].get("message") or {}
			content = message.get("content")
			if isinstance(content, list):
				text_parts: list[str] = []
				for part in content:
					if not isinstance(part, dict):
						continue
					if part.get("type") == "image_url":
						url = (part.get("image_url") or {}).get("url", "")
						match = _extract_image_data(url)
						if match:
							return match
					if part.get("type") == "text":
						text_parts.append(str(part.get("text") or ""))
				if text_parts:
					return _extract_image_data("\n".join(text_parts))
			elif isinstance(content, str):
				return _extract_image_data(content)
	except Exception:
		return None
	return None


def _describe_payload(payload: dict[str, Any]) -> str:
	try:
		choices = payload.get("choices") or []
		if choices:
			message = choices[0].get("message") or {}
			content = message.get("content")
			if isinstance(content, list):
				return "content=list"
			if isinstance(content, str):
				short = content[:120].replace("\n", " ")
				return f"content=str:{short}"
			return f"content={type(content).__name__}"
	except Exception:
		return "payload=unreadable"
	return "payload=empty"


def _extract_image_data(text: str) -> tuple[str, str] | None:
	if not text:
		return None

	match = re.search(r"data:image/([a-zA-Z0-9.+-]+);base64,([A-Za-z0-9+/=\n\r]+)", text)
	if match:
		mime = f"image/{match.group(1)}"
		b64_data = match.group(2).replace("\n", "").replace("\r", "")
		return mime, b64_data

	md_match = re.search(r"!\[image\]\(([^)]+)\)", text)
	if md_match and "data:image" in md_match.group(1):
		return _extract_image_data(md_match.group(1))

	return None


def _mime_to_ext(mime_type: str) -> str:
	if mime_type == "image/jpeg":
		return "jpg"
	if mime_type == "image/webp":
		return "webp"
	if mime_type == "image/gif":
		return "gif"
	return "png"


def _guess_mime(path: Path) -> str:
	mime, _ = mimetypes.guess_type(str(path))
	return mime or ""
