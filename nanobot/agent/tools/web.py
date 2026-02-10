"""Web tools: web_search and web_fetch."""

import html
import json
import os
import re
from typing import Any
from urllib.parse import urlparse

import httpx

from nanobot.agent.tools.base import Tool

# Shared constants
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7_2) AppleWebKit/537.36"
MAX_REDIRECTS = 5  # Limit redirects to prevent DoS attacks


def _strip_tags(text: str) -> str:
    """Remove HTML tags and decode entities."""
    text = re.sub(r'<script[\s\S]*?</script>', '', text, flags=re.I)
    text = re.sub(r'<style[\s\S]*?</style>', '', text, flags=re.I)
    text = re.sub(r'<[^>]+>', '', text)
    return html.unescape(text).strip()


def _normalize(text: str) -> str:
    """Normalize whitespace."""
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def _validate_url(url: str) -> tuple[bool, str]:
    """Validate URL: must be http(s) with valid domain."""
    try:
        p = urlparse(url)
        if p.scheme not in ('http', 'https'):
            return False, f"Only http/https allowed, got '{p.scheme or 'none'}'"
        if not p.netloc:
            return False, "Missing domain"
        return True, ""
    except Exception as e:
        return False, str(e)


class WebSearchTool(Tool):
    """Search the web using Serper API."""
    
    name = "web_search"
    description = "Search the web. Returns titles, URLs, and snippets."
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query, e.g. 'What is the capital of France'"},
            "count": {"type": "integer", "description": "Results (1-10)", "minimum": 1, "maximum": 10},
            "country": {"type": "string", "description": "Country code (gl), e.g. us (for United States), cn (for China)"},
            "language": {"type": "string", "description": "Language code (hl), e.g. en"},
            "tbs": {"type": "string", "description": "Date range filter (tbs), e.g. qdr:h (for past hour), qdr:d (for past day)"},
            "page": {"type": "integer", "description": "Result page number", "minimum": 1},
            "autocorrect": {"type": "boolean", "description": "Enable autocorrect"},
            "searchType": {"type": "string", "description": "Serper search type, e.g. search (organic), news, scholar", "default": "search"}
        },
        "required": ["query"]
    }
    
    def __init__(
        self,
        api_key: str | None = None,
        max_results: int = 5,
        endpoint: str = "https://google.serper.dev/search",
        country: str | None = None,
        language: str | None = None,
        tbs: str | None = None,
        page: int | None = None,
        autocorrect: bool | None = None,
        search_type: str | None = None,
    ):
        self.api_key = api_key or os.environ.get("SERPER_API_KEY", "")
        self.max_results = max_results
        self.endpoint = endpoint
        self.default_country = country
        self.default_language = language
        self.default_tbs = tbs
        self.default_page = page
        self.default_autocorrect = autocorrect
        self.default_search_type = "search" if search_type is None else search_type
    
    async def execute(
        self,
        query: str,
        count: int | None = None,
        country: str | None = None,
        language: str | None = None,
        tbs: str | None = None,
        page: int | None = None,
        autocorrect: bool | None = None,
        searchType: str | None = None,
        **kwargs: Any,
    ) -> str:
        if not self.api_key:
            return "Error: SERPER_API_KEY not configured"
        
        try:
            n = min(max(count or self.max_results, 1), 10)
            payload: dict[str, Any] = {"q": query}
            effective_country = country or self.default_country
            effective_language = language or self.default_language
            effective_tbs = tbs or self.default_tbs
            effective_page = page or self.default_page
            effective_autocorrect = autocorrect if autocorrect is not None else self.default_autocorrect
            effective_search_type = searchType or self.default_search_type
            if effective_search_type and effective_search_type.startswith("/"):
                effective_search_type = effective_search_type.lstrip("/")

            request_url = self._build_request_url(effective_search_type)

            if effective_country:
                payload["gl"] = effective_country
            if effective_language:
                payload["hl"] = effective_language
            if effective_tbs:
                payload["tbs"] = effective_tbs
            if effective_page:
                payload["page"] = effective_page
            if effective_autocorrect is not None:
                payload["autocorrect"] = effective_autocorrect
            if effective_search_type:
                payload["type"] = effective_search_type

            async with httpx.AsyncClient() as client:
                r = await client.post(
                    request_url,
                    json=payload,
                    headers={
                        "Accept": "application/json",
                        "X-API-KEY": self.api_key,
                        "Content-Type": "application/json",
                    },
                    timeout=10.0
                )
                r.raise_for_status()

            data = r.json()
            results = self._extract_results(data, effective_search_type)
            if not results:
                return f"No results for: {query}"
            
            lines = [f"Results for: {query}\n"]
            for i, item in enumerate(results[:n], 1):
                lines.append(f"{i}. {item.get('title', '')}\n   {item.get('link', '')}")
                if desc := item.get("snippet"):
                    lines.append(f"   {desc}")
            return "\n".join(lines)
        except Exception as e:
            return f"Error: {e}"

    def _build_request_url(self, search_type: str | None) -> str:
        if not search_type or search_type == "search":
            return self.endpoint

        parsed = urlparse(self.endpoint)
        if parsed.scheme and parsed.netloc:
            base = f"{parsed.scheme}://{parsed.netloc}"
            return f"{base}/{search_type}"

        return self.endpoint

    def _extract_results(self, data: dict[str, Any], search_type: str | None) -> list[dict[str, Any]]:
        if search_type == "news":
            return data.get("news") or data.get("topStories") or []

        return data.get("organic", [])


class WebFetchTool(Tool):
    """Fetch and extract content from a URL using Readability."""
    
    name = "web_fetch"
    description = "Fetch URL and extract readable content (HTML → markdown/text)."
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to fetch"},
            "extractMode": {"type": "string", "enum": ["markdown", "text"], "default": "markdown"},
            "maxChars": {"type": "integer", "minimum": 100}
        },
        "required": ["url"]
    }
    
    def __init__(self, max_chars: int = 50000):
        self.max_chars = max_chars
    
    async def execute(self, url: str, extractMode: str = "markdown", maxChars: int | None = None, **kwargs: Any) -> str:
        from readability import Document

        max_chars = maxChars or self.max_chars

        # Validate URL before fetching
        is_valid, error_msg = _validate_url(url)
        if not is_valid:
            return json.dumps({"error": f"URL validation failed: {error_msg}", "url": url})

        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                max_redirects=MAX_REDIRECTS,
                timeout=30.0
            ) as client:
                r = await client.get(url, headers={"User-Agent": USER_AGENT})
                r.raise_for_status()
            
            ctype = r.headers.get("content-type", "")
            
            # JSON
            if "application/json" in ctype:
                text, extractor = json.dumps(r.json(), indent=2), "json"
            # HTML
            elif "text/html" in ctype or r.text[:256].lower().startswith(("<!doctype", "<html")):
                doc = Document(r.text)
                content = self._to_markdown(doc.summary()) if extractMode == "markdown" else _strip_tags(doc.summary())
                text = f"# {doc.title()}\n\n{content}" if doc.title() else content
                extractor = "readability"
            else:
                text, extractor = r.text, "raw"
            
            truncated = len(text) > max_chars
            if truncated:
                text = text[:max_chars]
            
            return json.dumps({"url": url, "finalUrl": str(r.url), "status": r.status_code,
                              "extractor": extractor, "truncated": truncated, "length": len(text), "text": text})
        except Exception as e:
            return json.dumps({"error": str(e), "url": url})
    
    def _to_markdown(self, html: str) -> str:
        """Convert HTML to markdown."""
        # Convert links, headings, lists before stripping tags
        text = re.sub(r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a>',
                      lambda m: f'[{_strip_tags(m[2])}]({m[1]})', html, flags=re.I)
        text = re.sub(r'<h([1-6])[^>]*>([\s\S]*?)</h\1>',
                      lambda m: f'\n{"#" * int(m[1])} {_strip_tags(m[2])}\n', text, flags=re.I)
        text = re.sub(r'<li[^>]*>([\s\S]*?)</li>', lambda m: f'\n- {_strip_tags(m[1])}', text, flags=re.I)
        text = re.sub(r'</(p|div|section|article)>', '\n\n', text, flags=re.I)
        text = re.sub(r'<(br|hr)\s*/?>', '\n', text, flags=re.I)
        return _normalize(_strip_tags(text))
