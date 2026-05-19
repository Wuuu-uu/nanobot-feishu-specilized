#!/usr/bin/env python3
"""Probe OpenAI-compatible reasoning/tool parameter support.

This script reuses the ``providers.openai`` section from a Nanobot
``config.json`` and sends a few tiny requests to determine which endpoint and
parameter shape is accepted by the configured upstream gateway.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


DEFAULT_CONFIG = "/root/.nanobot/config.json"


@dataclass(frozen=True)
class ProbeCase:
    name: str
    endpoint: str
    body: dict[str, Any]


def load_config(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def pick_openai_config(config: dict[str, Any]) -> tuple[str, str, str]:
    defaults = config.get("agents", {}).get("defaults", {})
    provider = config.get("providers", {}).get("openai", {})
    model = defaults.get("model") or "gpt-5.5-2026-04-24"
    api_key = provider.get("apiKey") or provider.get("api_key")
    api_base = provider.get("apiBase") or provider.get("api_base") or "https://api.openai.com/v1/"
    if not api_key:
        raise SystemExit("providers.openai.apiKey is missing in config")
    return model, str(api_key), normalize_base(str(api_base))


def normalize_base(api_base: str) -> str:
    base = api_base.rstrip("/") + "/"
    if base.endswith("/v1/"):
        return base
    return urljoin(base, "v1/")


def tiny_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "probe_echo",
            "description": "Return a short echo string for compatibility probing.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                },
                "required": ["text"],
                "additionalProperties": False,
            },
        },
    }


def tiny_responses_tool() -> dict[str, Any]:
    fn = tiny_tool()["function"]
    return {
        "type": "function",
        "name": fn["name"],
        "description": fn["description"],
        "parameters": fn["parameters"],
    }


def build_cases(
    model: str,
    effort: str,
    prompt: str,
    *,
    max_output_tokens: int,
    prompt_cache_key: str | None = None,
    prompt_cache_retention: str | None = None,
) -> list[ProbeCase]:
    def with_cache(body: dict[str, Any]) -> dict[str, Any]:
        if prompt_cache_key:
            body["prompt_cache_key"] = prompt_cache_key
        if prompt_cache_retention:
            body["prompt_cache_retention"] = prompt_cache_retention
        return body

    return [
        ProbeCase(
            name="responses_reasoning_no_tools",
            endpoint="responses",
            body=with_cache({
                "model": model,
                "input": prompt,
                "reasoning": {"effort": effort},
                "max_output_tokens": max_output_tokens,
                "store": False,
            }),
        ),
        ProbeCase(
            name="responses_reasoning_with_function_tool",
            endpoint="responses",
            body=with_cache({
                "model": model,
                "input": prompt,
                "reasoning": {"effort": effort},
                "tools": [tiny_responses_tool()],
                "tool_choice": "auto",
                "max_output_tokens": max_output_tokens,
                "store": False,
            }),
        ),
        ProbeCase(
            name="responses_reasoning_required_tool",
            endpoint="responses",
            body=with_cache({
                "model": model,
                "input": prompt,
                "reasoning": {"effort": effort},
                "tools": [tiny_responses_tool()],
                "tool_choice": "required",
                "max_output_tokens": max_output_tokens,
                "store": False,
            }),
        ),
        ProbeCase(
            name="responses_reasoning_named_tool",
            endpoint="responses",
            body=with_cache({
                "model": model,
                "input": prompt,
                "reasoning": {"effort": effort},
                "tools": [tiny_responses_tool()],
                "tool_choice": {"type": "function", "name": "probe_echo"},
                "max_output_tokens": max_output_tokens,
                "store": False,
            }),
        ),
        ProbeCase(
            name="responses_stream_reasoning_no_tools",
            endpoint="responses",
            body=with_cache({
                "model": model,
                "input": prompt,
                "reasoning": {"effort": effort},
                "max_output_tokens": max_output_tokens,
                "store": False,
                "stream": True,
            }),
        ),
        ProbeCase(
            name="responses_stream_reasoning_with_function_tool",
            endpoint="responses",
            body=with_cache({
                "model": model,
                "input": prompt,
                "reasoning": {"effort": effort},
                "tools": [tiny_responses_tool()],
                "tool_choice": "auto",
                "max_output_tokens": max_output_tokens,
                "store": False,
                "stream": True,
            }),
        ),
        ProbeCase(
            name="chat_tools_no_reasoning",
            endpoint="chat/completions",
            body=with_cache({
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "tools": [tiny_tool()],
                "tool_choice": "auto",
                "max_tokens": max_output_tokens,
            }),
        ),
        ProbeCase(
            name="chat_tools_with_reasoning_effort",
            endpoint="chat/completions",
            body=with_cache({
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "tools": [tiny_tool()],
                "tool_choice": "auto",
                "reasoning_effort": effort,
                "max_tokens": max_output_tokens,
            }),
        ),
        ProbeCase(
            name="chat_no_tools_with_reasoning_effort",
            endpoint="chat/completions",
            body=with_cache({
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "reasoning_effort": effort,
                "max_tokens": max_output_tokens,
            }),
        ),
    ]


def post_json(url: str, api_key: str, body: dict[str, Any], timeout: float) -> tuple[int, str]:
    data = json.dumps(body).encode("utf-8")
    req = Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except URLError as e:
        return 0, f"{e.__class__.__name__}: {e.reason}"
    except TimeoutError as e:
        return 0, f"TimeoutError: {e}"


def summarize_response(raw: str) -> str:
    if raw.startswith("event:") or "\ndata:" in raw:
        event_types: list[str] = []
        final_status = None
        error = None
        text_parts: list[str] = []
        usage = None
        for line in raw.splitlines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                event = json.loads(payload)
            except Exception:
                continue
            event_type = event.get("type")
            if event_type:
                event_types.append(event_type)
            if event_type == "response.output_text.delta":
                text_parts.append(event.get("delta") or "")
            if event_type == "response.failed":
                resp = event.get("response") or {}
                error = resp.get("error") or event.get("error") or event
                final_status = resp.get("status")
            if event_type == "response.completed":
                resp = event.get("response") or {}
                final_status = resp.get("status")
                usage = resp.get("usage")
        out = {
            "events": event_types,
            "final_status": final_status,
            "output_text": "".join(text_parts) or None,
            "usage": usage,
            "error": error,
        }
        return json.dumps(out, ensure_ascii=False)[:1200]
    try:
        data = json.loads(raw)
    except Exception:
        return raw.replace("\n", " ")[:500]
    if isinstance(data, dict) and data.get("error"):
        return json.dumps(data["error"], ensure_ascii=False)[:700]
    if not isinstance(data, dict):
        return str(data)[:500]
    usage = data.get("usage") or {}
    output_text = data.get("output_text")
    if output_text is None and isinstance(data.get("output"), list):
        parts: list[str] = []
        for item in data.get("output") or []:
            if not isinstance(item, dict):
                continue
            for block in item.get("content") or []:
                if isinstance(block, dict) and block.get("text"):
                    parts.append(str(block["text"]))
        output_text = "".join(parts) or None
    out = {
        "id": data.get("id"),
        "object": data.get("object"),
        "status": data.get("status"),
        "output_text": output_text,
        "usage": usage,
    }
    if "choices" in data:
        choices = data.get("choices") or []
        out["finish_reason"] = choices[0].get("finish_reason") if choices else None
    return json.dumps(out, ensure_ascii=False)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--model", default=None, help="Override agents.defaults.model")
    parser.add_argument("--effort", default="low", help="Reasoning effort to probe")
    parser.add_argument("--prompt", default="Reply with exactly: ok")
    parser.add_argument("--max-output-tokens", type=int, default=32)
    parser.add_argument("--prompt-cache-key", default=None)
    parser.add_argument("--prompt-cache-retention", default=None, choices=(None, "in_memory", "24h"))
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--case", default=None, help="Run only one named case")
    parser.add_argument("--raw", action="store_true", help="Print the first 2000 raw response chars")
    args = parser.parse_args()

    config = load_config(args.config)
    model, api_key, api_base = pick_openai_config(config)
    if args.model:
        model = args.model

    print(f"config: {args.config}")
    print(f"api_base: {api_base}")
    print(f"model: {model}")
    print(f"effort: {args.effort}")
    print()

    cases = build_cases(
        model,
        args.effort,
        args.prompt,
        max_output_tokens=args.max_output_tokens,
        prompt_cache_key=args.prompt_cache_key,
        prompt_cache_retention=args.prompt_cache_retention,
    )
    if args.case:
        cases = [c for c in cases if c.name == args.case]
        if not cases:
            names = ", ".join(
                c.name
                for c in build_cases(
                    model,
                    args.effort,
                    args.prompt,
                    max_output_tokens=args.max_output_tokens,
                    prompt_cache_key=args.prompt_cache_key,
                    prompt_cache_retention=args.prompt_cache_retention,
                )
            )
            raise SystemExit(f"unknown case {args.case!r}; choices: {names}")

    for case in cases:
        url = urljoin(api_base, case.endpoint)
        started = time.time()
        status, raw = post_json(url, api_key, case.body, args.timeout)
        elapsed = time.time() - started
        outcome = "OK" if 200 <= status < 300 else "FAIL"
        print(f"[{outcome}] {case.name}")
        print(f"  url: {url}")
        print(f"  status: {status} elapsed: {elapsed:.2f}s")
        print(f"  summary: {summarize_response(raw)}")
        if args.raw:
            print(f"  raw: {raw[:2000]}")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
