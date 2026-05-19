#!/usr/bin/env python3
"""Check model listing and lightweight availability for nanobot's provider.

The script intentionally never prints API keys. It reads the same config file
as nanobot, calls the provider's OpenAI-compatible `/models` endpoint, then
optionally sends tiny ping requests to selected models.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from nanobot.config.loader import load_config, resolve_config_env_vars, set_config_path


DEFAULT_CONFIG = Path("/root/.nanobot/config.json")
DEFAULT_COMMON_MODELS = [
    "gpt-5.5-2026-04-24",
    "gpt-5.4",
    "gpt-5.3-codex",
    "gpt-5.2",
    "gpt-4.1",
    "gpt-4o",
]


@dataclass(slots=True)
class ProbeResult:
    model: str
    endpoint: str
    ok: bool
    status_code: int | None
    detail: str


def _endpoint(base_url: str, suffix: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/v1"):
        return f"{base}{suffix}"
    return f"{base}/v1{suffix}"


def _redact_base(base_url: str) -> str:
    parts = urlsplit(base_url)
    if not parts.netloc:
        return base_url
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def _error_detail(payload: Any) -> str:
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            msg = err.get("message") or err.get("code") or err
            return str(msg)
        if "message" in payload:
            return str(payload.get("message"))
    text = str(payload)
    return text[:500]


def _model_sort_key(model: str) -> tuple[int, str]:
    lower = model.lower()
    if lower.startswith("gpt-"):
        return (0, lower)
    if lower.startswith(("o1", "o3", "o4")):
        return (1, lower)
    return (2, lower)


def _uses_responses(model: str, reasoning_effort: str | None) -> bool:
    lower = model.lower()
    return bool(reasoning_effort and reasoning_effort.lower() != "none") or any(
        token in lower for token in ("gpt-5", "o1", "o3", "o4")
    )


async def list_models(client: httpx.AsyncClient, base_url: str) -> tuple[list[str], str | None]:
    url = _endpoint(base_url, "/models")
    try:
        response = await client.get(url)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        return [], str(exc)

    models: list[str] = []
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item.get("id"):
                models.append(str(item["id"]))
            elif isinstance(item, str):
                models.append(item)
    elif isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict) and item.get("id"):
                models.append(str(item["id"]))
            elif isinstance(item, str):
                models.append(item)
    return sorted(set(models), key=_model_sort_key), None


async def probe_model(
    client: httpx.AsyncClient,
    base_url: str,
    model: str,
    *,
    reasoning_effort: str | None,
) -> ProbeResult:
    if _uses_responses(model, reasoning_effort):
        url = _endpoint(base_url, "/responses")
        body: dict[str, Any] = {
            "model": model,
            "input": [{"role": "user", "content": [{"type": "input_text", "text": "ping"}]}],
            "max_output_tokens": 16,
            "store": False,
        }
        if reasoning_effort and reasoning_effort.lower() != "none":
            body["reasoning"] = {"effort": reasoning_effort}
        endpoint = "responses"
    else:
        url = _endpoint(base_url, "/chat/completions")
        body = {
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 16,
            "temperature": 0,
        }
        endpoint = "chat"

    try:
        response = await client.post(url, json=body)
        if 200 <= response.status_code < 300:
            return ProbeResult(model, endpoint, True, response.status_code, "ok")
        try:
            payload = response.json()
        except Exception:
            payload = response.text
        return ProbeResult(model, endpoint, False, response.status_code, _error_detail(payload))
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}".rstrip()
        return ProbeResult(model, endpoint, False, None, detail)


def _print_models(models: list[str], *, limit: int) -> None:
    print(f"\n/models returned {len(models)} model(s)")
    shown = models[:limit] if limit > 0 else models
    for model in shown:
        print(f"  - {model}")
    if limit > 0 and len(models) > limit:
        print(f"  ... {len(models) - limit} more hidden by --list-limit")


def _print_probe_results(results: list[ProbeResult]) -> None:
    print("\nProbe results")
    if not results:
        print("  (no models probed)")
        return

    name_w = min(max(len("model"), *(len(r.model) for r in results)), 56)
    ep_w = max(len("endpoint"), *(len(r.endpoint) for r in results))
    print(f"  {'model':{name_w}}  {'endpoint':{ep_w}}  status  result")
    print(f"  {'-' * name_w}  {'-' * ep_w}  ------  ------")
    for result in results:
        status = str(result.status_code) if result.status_code is not None else "-"
        state = "OK" if result.ok else "FAIL"
        detail = result.detail.replace("\n", " ")[:180]
        print(f"  {result.model:{name_w}}  {result.endpoint:{ep_w}}  {status:>6}  {state}: {detail}")

    ok = [r.model for r in results if r.ok]
    print(f"\nAvailable by probe: {len(ok)}/{len(results)}")
    for model in ok:
        print(f"  - {model}")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="nanobot config path")
    parser.add_argument("--models", nargs="*", default=[], help="extra models to probe")
    parser.add_argument("--probe-listed", action="store_true", help="probe models returned by /models")
    parser.add_argument(
        "--listed-prefix",
        nargs="*",
        default=[],
        help="only probe listed models whose id starts with one of these prefixes",
    )
    parser.add_argument("--max-probes", type=int, default=20, help="max listed models to probe when --probe-listed is set")
    parser.add_argument("--list-limit", type=int, default=80, help="max models to print from /models, 0 means all")
    parser.add_argument("--timeout", type=float, default=30.0, help="request timeout in seconds")
    parser.add_argument("--reasoning-effort", default=None, help="override reasoning effort for response probes")
    args = parser.parse_args()

    config_path = Path(args.config).expanduser().resolve()
    set_config_path(config_path)
    config = resolve_config_env_vars(load_config(config_path))

    model = config.agents.defaults.model
    provider_name = config.get_provider_name(model)
    provider = config.get_provider(model)
    base_url = config.get_api_base(model)
    api_key = config.get_api_key(model)
    reasoning_effort = args.reasoning_effort
    if reasoning_effort is None:
        reasoning_effort = config.agents.defaults.reasoning_effort

    if not base_url or not api_key:
        raise SystemExit("Provider apiBase/apiKey is missing in config.")

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    if provider and provider.extra_headers:
        headers.update(provider.extra_headers)

    print(f"Config: {config_path}")
    print(f"Provider: {provider_name or '(unknown)'}")
    print(f"Base URL: {_redact_base(base_url)}")
    print(f"Configured model: {model}")
    print(f"Reasoning effort for probes: {reasoning_effort or '(none)'}")

    async with httpx.AsyncClient(headers=headers, timeout=args.timeout) as client:
        listed_models, list_error = await list_models(client, base_url)
        if list_error:
            print(f"\n/models failed: {list_error}")
        else:
            _print_models(listed_models, limit=args.list_limit)

        candidates: list[str] = []
        candidates.append(model)
        dream_model = config.agents.defaults.dream.model_override
        if dream_model:
            candidates.append(dream_model)
        candidates.extend(DEFAULT_COMMON_MODELS)
        candidates.extend(args.models)
        if args.probe_listed:
            listed_candidates = listed_models
            if args.listed_prefix:
                prefixes = tuple(args.listed_prefix)
                listed_candidates = [m for m in listed_candidates if m.startswith(prefixes)]
            candidates.extend(listed_candidates[: max(0, args.max_probes)])

        deduped = sorted(set(m for m in candidates if m), key=_model_sort_key)
        results = await asyncio.gather(*[
            probe_model(client, base_url, candidate, reasoning_effort=reasoning_effort)
            for candidate in deduped
        ])
        _print_probe_results(list(results))

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
