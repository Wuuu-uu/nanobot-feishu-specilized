import base64

import httpx

from nanobot.agent.tools.image_generate import ImageGenerateTool
from nanobot.config.schema import ImageGenConfig


class FakeAsyncClient:
    def __init__(self, outcomes: list[Exception | httpx.Response], timeout: int) -> None:
        self.outcomes = outcomes
        self.timeout = timeout
        self.calls = 0
        self.requests: list[dict] = []

    async def __aenter__(self) -> "FakeAsyncClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def post(
        self,
        url: str,
        json: dict | None = None,
        headers: dict | None = None,
        data: dict | None = None,
        files: list | None = None,
    ) -> httpx.Response:
        self.calls += 1
        self.requests.append(
            {
                "url": url,
                "json": json,
                "headers": headers,
                "data": data,
                "files": files,
            }
        )
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _success_response() -> httpx.Response:
    image_data = base64.b64encode(b"fake-image-bytes").decode("ascii")
    payload = {
        "choices": [
            {
                "message": {
                    "content": f"data:image/png;base64,{image_data}",
                }
            }
        ]
    }
    request = httpx.Request("POST", "https://example.com/chat/completions")
    return httpx.Response(200, json=payload, request=request)


def _images_api_success_response(path: str) -> httpx.Response:
    image_data = base64.b64encode(b"fake-image-bytes").decode("ascii")
    payload = {
        "data": [
            {
                "b64_json": image_data,
            }
        ]
    }
    request = httpx.Request("POST", f"https://example.com/{path}")
    return httpx.Response(200, json=payload, request=request)


def _error_response(status_code: int, text: str) -> httpx.Response:
    request = httpx.Request("POST", "https://example.com/chat/completions")
    return httpx.Response(status_code, text=text, request=request)


async def test_generate_image_retries_on_timeout_then_succeeds(monkeypatch) -> None:
    config = ImageGenConfig(
        enabled=True,
        api_base="https://example.com",
        api_key="k",
        model_name="m",
        timeout=1,
        retry_attempts=3,
        retry_backoff_seconds=0,
    )
    tool = ImageGenerateTool(config=config)

    fake_client = FakeAsyncClient(
        outcomes=[httpx.ReadTimeout("timeout"), _success_response()],
        timeout=config.timeout,
    )
    monkeypatch.setattr(
        "nanobot.agent.tools.image_generate.httpx.AsyncClient",
        lambda timeout: fake_client,
    )

    img_bytes, mime = await tool._generate_image("draw a cat")

    assert fake_client.calls == 2
    assert fake_client.requests[-1]["url"] == "https://example.com/chat/completions"
    assert mime == "image/png"
    assert img_bytes == b"fake-image-bytes"


async def test_generate_image_retries_on_retryable_status_then_succeeds(monkeypatch) -> None:
    config = ImageGenConfig(
        enabled=True,
        api_base="https://example.com",
        api_key="k",
        model_name="m",
        timeout=1,
        retry_attempts=3,
        retry_backoff_seconds=0,
        retry_status_codes=[500],
    )
    tool = ImageGenerateTool(config=config)

    fake_client = FakeAsyncClient(
        outcomes=[_error_response(500, "server busy"), _success_response()],
        timeout=config.timeout,
    )
    monkeypatch.setattr(
        "nanobot.agent.tools.image_generate.httpx.AsyncClient",
        lambda timeout: fake_client,
    )

    img_bytes, mime = await tool._generate_image("draw a dog")

    assert fake_client.calls == 2
    assert fake_client.requests[-1]["url"] == "https://example.com/chat/completions"
    assert mime == "image/png"
    assert img_bytes == b"fake-image-bytes"


async def test_generate_image_fails_fast_on_non_retryable_status(monkeypatch) -> None:
    config = ImageGenConfig(
        enabled=True,
        api_base="https://example.com",
        api_key="k",
        model_name="m",
        timeout=1,
        retry_attempts=3,
        retry_backoff_seconds=0,
        retry_status_codes=[500],
    )
    tool = ImageGenerateTool(config=config)

    fake_client = FakeAsyncClient(
        outcomes=[_error_response(400, "bad request")],
        timeout=config.timeout,
    )
    monkeypatch.setattr(
        "nanobot.agent.tools.image_generate.httpx.AsyncClient",
        lambda timeout: fake_client,
    )

    try:
        await tool._generate_image("bad prompt")
    except RuntimeError as e:
        assert "http 400" in str(e)
    else:
        raise AssertionError("Expected RuntimeError")

    assert fake_client.calls == 1


async def test_gpt_image_models_use_chat_completions_by_default(monkeypatch) -> None:
    config = ImageGenConfig(
        enabled=True,
        api_base="https://example.com",
        api_key="k",
        model_name="gpt-image-2",
        timeout=1,
        retry_attempts=1,
        retry_backoff_seconds=0,
    )
    tool = ImageGenerateTool(config=config)

    fake_client = FakeAsyncClient(
        outcomes=[_success_response()],
        timeout=config.timeout,
    )
    monkeypatch.setattr(
        "nanobot.agent.tools.image_generate.httpx.AsyncClient",
        lambda timeout: fake_client,
    )

    img_bytes, mime = await tool._generate_image("draw a poster")

    assert fake_client.calls == 1
    assert fake_client.requests[0]["url"] == "https://example.com/chat/completions"
    assert fake_client.requests[0]["json"] == {
        "model": "gpt-image-2",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "draw a poster"}]}],
        "stream": False,
    }
    assert mime == "image/png"
    assert img_bytes == b"fake-image-bytes"


async def test_generate_image_uses_images_generations_when_images_port_enabled(
    monkeypatch,
) -> None:
    config = ImageGenConfig(
        enabled=True,
        api_base="https://example.com",
        api_key="k",
        model_name="gpt-image-2",
        images_port_enabled=True,
        timeout=1,
        retry_attempts=1,
        retry_backoff_seconds=0,
    )
    tool = ImageGenerateTool(config=config)

    fake_client = FakeAsyncClient(
        outcomes=[_images_api_success_response("images/generations")],
        timeout=config.timeout,
    )
    monkeypatch.setattr(
        "nanobot.agent.tools.image_generate.httpx.AsyncClient",
        lambda timeout: fake_client,
    )

    img_bytes, mime = await tool._generate_image("draw a poster", aspect_ratio="16:9")

    assert fake_client.calls == 1
    assert fake_client.requests[0]["url"] == "https://example.com/images/generations"
    assert fake_client.requests[0]["json"] == {
        "model": "gpt-image-2",
        "prompt": "draw a poster",
    }
    assert fake_client.requests[0]["data"] is None
    assert fake_client.requests[0]["files"] is None
    assert mime == "image/png"
    assert img_bytes == b"fake-image-bytes"


async def test_generate_image_uses_images_edits_for_multimodal_gpt_image_models(
    monkeypatch, tmp_path
) -> None:
    config = ImageGenConfig(
        enabled=True,
        api_base="https://example.com",
        api_key="k",
        model_name="gpt-image-2",
        images_port_enabled=True,
        timeout=1,
        retry_attempts=1,
        retry_backoff_seconds=0,
    )
    tool = ImageGenerateTool(config=config)

    ref_path = tmp_path / "ref.png"
    ref_path.write_bytes(b"fake-reference-image")
    images = tool._collect_images(str(ref_path), None)

    fake_client = FakeAsyncClient(
        outcomes=[_images_api_success_response("images/edits")],
        timeout=config.timeout,
    )
    monkeypatch.setattr(
        "nanobot.agent.tools.image_generate.httpx.AsyncClient",
        lambda timeout: fake_client,
    )

    img_bytes, mime = await tool._generate_image(
        "edit this image",
        images=images,
        aspect_ratio="original",
    )

    assert fake_client.calls == 1
    assert fake_client.requests[0]["url"] == "https://example.com/images/edits"
    assert fake_client.requests[0]["json"] is None
    assert fake_client.requests[0]["data"] == {
        "model": "gpt-image-2",
        "prompt": "edit this image",
    }
    assert fake_client.requests[0]["files"] == [
        ("image[]", ("ref.png", b"fake-reference-image", "image/png"))
    ]
    assert mime == "image/png"
    assert img_bytes == b"fake-image-bytes"


async def test_quality_parameter_is_hidden_until_enabled() -> None:
    disabled_config = ImageGenConfig(
        enabled=True,
        api_base="https://example.com",
        api_key="k",
        model_name="gpt-image-2",
        images_port_enabled=True,
    )
    enabled_config = ImageGenConfig(
        enabled=True,
        api_base="https://example.com",
        api_key="k",
        model_name="gpt-image-2",
        images_port_enabled=True,
        quality_enabled=True,
    )

    assert "quality" not in ImageGenerateTool(config=disabled_config).parameters["properties"]
    assert "quality" in ImageGenerateTool(config=enabled_config).parameters["properties"]


async def test_quality_enabled_derives_size_without_forwarding_quality(monkeypatch) -> None:
    config = ImageGenConfig(
        enabled=True,
        api_base="https://example.com",
        api_key="k",
        model_name="gpt-image-2",
        images_port_enabled=True,
        quality_enabled=True,
        timeout=1,
        retry_attempts=1,
        retry_backoff_seconds=0,
    )
    tool = ImageGenerateTool(config=config)

    fake_client = FakeAsyncClient(
        outcomes=[_images_api_success_response("images/generations")],
        timeout=config.timeout,
    )
    monkeypatch.setattr(
        "nanobot.agent.tools.image_generate.httpx.AsyncClient",
        lambda timeout: fake_client,
    )

    img_bytes, mime = await tool._generate_image(
        "draw a poster",
        aspect_ratio="16:9",
        quality="medium",
    )

    assert fake_client.calls == 1
    assert fake_client.requests[0]["json"] == {
        "model": "gpt-image-2",
        "prompt": "draw a poster",
        "size": "1536x864",
    }
    assert "quality" not in fake_client.requests[0]["json"]
    assert mime == "image/png"
    assert img_bytes == b"fake-image-bytes"


async def test_original_ratio_does_not_derive_size_even_with_quality(monkeypatch) -> None:
    config = ImageGenConfig(
        enabled=True,
        api_base="https://example.com",
        api_key="k",
        model_name="gpt-image-2",
        images_port_enabled=True,
        quality_enabled=True,
        timeout=1,
        retry_attempts=1,
        retry_backoff_seconds=0,
    )
    tool = ImageGenerateTool(config=config)

    fake_client = FakeAsyncClient(
        outcomes=[_images_api_success_response("images/generations")],
        timeout=config.timeout,
    )
    monkeypatch.setattr(
        "nanobot.agent.tools.image_generate.httpx.AsyncClient",
        lambda timeout: fake_client,
    )

    await tool._generate_image("draw a poster", aspect_ratio="原比例", quality="high")

    assert fake_client.requests[0]["json"] == {
        "model": "gpt-image-2",
        "prompt": "draw a poster",
    }


async def test_invalid_ratio_fails_before_request(monkeypatch) -> None:
    config = ImageGenConfig(
        enabled=True,
        api_base="https://example.com",
        api_key="k",
        model_name="gpt-image-2",
        images_port_enabled=True,
        quality_enabled=True,
        timeout=1,
        retry_attempts=1,
        retry_backoff_seconds=0,
    )
    tool = ImageGenerateTool(config=config)
    fake_client = FakeAsyncClient(
        outcomes=[_images_api_success_response("images/generations")],
        timeout=config.timeout,
    )
    monkeypatch.setattr(
        "nanobot.agent.tools.image_generate.httpx.AsyncClient",
        lambda timeout: fake_client,
    )

    try:
        await tool._generate_image("draw a poster", aspect_ratio="4:1", quality="low")
    except ValueError as e:
        assert "must not exceed 3:1" in str(e)
    else:
        raise AssertionError("Expected ValueError")

    assert fake_client.calls == 0
