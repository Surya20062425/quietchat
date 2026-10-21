"""web tools — web_search (Brave API or DuckDuckGo HTML fallback), web_fetch (HTTP GET)."""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request

from .registry import get_context, registry

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) void-cli/1.0"
MAX_BODY = 400_000


def _http_get(url: str, timeout: float = 20.0, headers: dict | None = None) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.status, resp.read(MAX_BODY).decode(charset, errors="replace")


def _strip_html(html: str) -> str:
    html = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<[^>]+>", " ", html)
    html = re.sub(r"\s+", " ", html)
    return html.strip()


# --------------------------------------------------------------------------- search
def _search_brave(query: str, count: int) -> list[dict]:
    ctx = get_context()
    keys = ctx.config.api_keys("openai") if ctx.config else []  # never: brave uses its own env
    del keys  # explicit: brave key comes from BRAVE_API_KEY below
    key = ""
    if ctx.config is not None:
        import os
        key = os.environ.get("BRAVE_API_KEY", "")
    if not key:
        raise RuntimeError("BRAVE_API_KEY not set in ~/.void/.env")
    status, body = _http_get(
        "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode(
            {"q": query, "count": count}),
        headers={"X-Subscription-Token": key, "Accept": "application/json"})
    data = json.loads(body or "{}")
    results = []
    for item in (data.get("web") or {}).get("results", [])[:count]:
        results.append({"title": item.get("title", ""), "url": item.get("url", ""),
                        "snippet": item.get("description", "")})
    return results


_ANCHOR_RE = re.compile(r"<a\s([^>]*?)>(.*?)</a>", re.S | re.I)
_ATTR_RE = re.compile(r"""([\w:.-]+)\s*=\s*(['"])(.*?)\2""", re.S)


def _attrs(fragment: str) -> dict:
    return {m.group(1).lower(): m.group(3) for m in _ATTR_RE.finditer(fragment)}


def _absolute(href: str) -> str:
    href = href.replace("&amp;", "&").strip()
    if href.startswith("//"):
        return "https:" + href
    return href


def _search_ddg(query: str, count: int) -> list[dict]:
    """Free fallback (no API key).

    DuckDuckGo's /html/ endpoint answers with a bot-check page, so prefer the Lite
    endpoint and parse anchors by attribute name rather than by a fixed attribute order.
    """
    endpoints = (
        ("https://lite.duckduckgo.com/lite/?", "result-link", "result-snippet"),
        ("https://html.duckduckgo.com/html/?", "result__a", "result__snippet"),
    )
    errors: list[str] = []
    for url, link_class, snippet_class in endpoints:
        try:
            status, body = _http_get(url + urllib.parse.urlencode({"q": query}))
        except Exception as exc:
            errors.append(f"{url}: {exc}")
            continue
        results: list[dict] = []
        for m in _ANCHOR_RE.finditer(body):
            attrs = _attrs(m.group(1))
            if link_class not in (attrs.get("class") or "").split():
                continue
            href = _absolute(attrs.get("href") or "")
            if "uddg=" in href:
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
                href = (qs.get("uddg") or [href])[0]
            title = _strip_html(m.group(2))
            if not title or not href.startswith("http"):
                continue
            results.append({"title": title, "url": href, "snippet": ""})
            if len(results) >= count:
                break
        if results:
            snippets = re.findall(r"class=['\"][^'\"]*" + re.escape(snippet_class) + r"[^'\"]*['\"][^>]*>(.*?)</a?>?",
                                  body, re.S | re.I)
            for i, snip in enumerate(snippets):
                if i < len(results):
                    results[i]["snippet"] = _strip_html(snip)[:300]
            return results
        errors.append(f"{url}: no results parsed (len={len(body)})")
    raise RuntimeError("; ".join(errors) or "duckduckgo returned nothing")


def web_search_handler(args: dict) -> dict:
    query = str(args.get("query") or "").strip()
    if not query:
        return {"ok": False, "error": "query is required"}
    count = min(int(args.get("count") or 6), 10)
    backend = str(args.get("backend") or "auto")
    errors: list[str] = []
    if backend in ("brave", "auto"):
        try:
            return {"ok": True, "backend": "brave", "results": _search_brave(query, count)}
        except Exception as exc:
            errors.append(f"brave: {exc}")
            if backend == "brave":
                return {"ok": False, "error": "; ".join(errors)}
    try:
        results = _search_ddg(query, count)
        if results:
            return {"ok": True, "backend": "duckduckgo", "results": results}
        errors.append("duckduckgo: no results parsed")
    except Exception as exc:
        errors.append(f"duckduckgo: {exc}")
    return {"ok": False, "error": "; ".join(errors) or "no results"}

def web_fetch_handler(args: dict) -> dict:
    url = str(args.get("url") or "").strip()
    if not url:
        return {"ok": False, "error": "url is required"}
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    max_chars = int(args.get("max_chars") or 20_000)
    try:
        status, body = _http_get(url, timeout=float(args.get("timeout") or 20))
    except Exception as exc:
        return {"ok": False, "error": f"fetch failed: {exc}", "url": url}
    content_type = ""
    if url.lower().endswith((".txt", ".json", ".xml", ".csv", ".md", ".yaml", ".yml")):
        text = body
        content_type = "text"
    else:
        m = re.search(r"<title[^>]*>(.*?)</title>", body, re.S | re.I)
        title = _strip_html(m.group(1)) if m else ""
        text = _strip_html(body)
        content_type = "html->text"
        if title:
            text = f"{title}\n\n{text}"
    truncated = len(text) > max_chars
    return {"ok": True, "url": url, "status": status, "content_type": content_type,
            "content": text[:max_chars], "truncated": truncated,
            "length": len(text)}


registry.register(
    "web_search",
    {"description": "Search the web. Returns a list of {title, url, snippet}. Uses Brave API when "
                    "BRAVE_API_KEY is set, otherwise a free DuckDuckGo fallback.",
     "parameters": {"type": "object",
                    "properties": {"query": {"type": "string"},
                                   "count": {"type": "integer", "description": "1-10 results (default 6)."},
                                   "backend": {"type": "string", "enum": ["auto", "brave", "duckduckgo"]}},
                    "required": ["query"]}},
    web_search_handler, toolset="web")

registry.register(
    "web_fetch",
    {"description": "Fetch a URL and return readable text (HTML stripped). Good for docs, "
                    "articles, APIs returning JSON/text.",
     "parameters": {"type": "object",
                    "properties": {"url": {"type": "string"},
                                   "max_chars": {"type": "integer", "description": "Default 20000."},
                                   "timeout": {"type": "number"}},
                    "required": ["url"]}},
    web_fetch_handler, toolset="web")


