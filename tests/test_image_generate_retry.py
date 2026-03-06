import base64

import httpx

from nanobot.agent.tools.image_generate import ImageGenerateTool
from nanobot.config.schema import ImageGenConfig


class FakeAsyncClient:
    def __init__(self, outcomes: list[Exception | httpx.Response], timeout: int) -> None:
        self.outcomes = outcomes
        self.timeout = timeout
        self.calls = 0

    async def __aenter__(self) -> "FakeAsyncClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def post(self, url: str, json: dict, headers: dict) -> httpx.Response:
        self.calls += 1
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
