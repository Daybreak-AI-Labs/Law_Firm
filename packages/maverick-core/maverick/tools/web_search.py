"""Bounded web search for the firm runtime.

Secure execution requires one explicit canonical backend, selected with
``MAVERICK_SEARCH_BACKEND`` or ``[firm] search_backend``. The privileged query
is sent to that backend at most once and is never replayed to another vendor.
The historical credential-driven fallback chain exists only when secure
defaults are explicitly disabled and no matter is bound.

Each search returns up to ``num_results`` (default 10) entries with
title + URL + snippet.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

from . import Tool

log = logging.getLogger(__name__)


_SEARCH_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "What to search for. Plain text, no quoting needed.",
        },
        "num_results": {
            "type": "integer",
            "description": "Max results to return (default 10, capped at 25).",
        },
        "site": {
            "type": "string",
            "description": "Optional site: filter (e.g. 'github.com').",
        },
    },
    "required": ["query"],
}


def _augment_query(query: str, site: str | None) -> str:
    if site:
        return f"site:{site} {query}"
    return query


def _format_results(results: list[dict]) -> str:
    """Render search results as a compact text block."""
    if not results:
        return "no results"
    lines: list[str] = []
    for i, r in enumerate(results, 1):
        title = (r.get("title") or "").strip()
        url = (r.get("url") or "").strip()
        snippet = (r.get("snippet") or "").strip()
        snippet = re.sub(r"\s+", " ", snippet)[:400]
        lines.append(f"{i}. {title}\n   {url}\n   {snippet}")
    return "\n\n".join(lines)


def _try_tavily(query: str, num: int) -> list[dict] | None:
    key = os.environ.get("TAVILY_API_KEY")
    if not key:
        return None
    try:
        import httpx
    except ImportError:
        return None
    try:
        resp = httpx.post(
            "https://api.tavily.com/search",
            json={
                "api_key": key,
                "query": query,
                "max_results": num,
                "include_answer": False,
            },
            timeout=15.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning("tavily search failed: %s", e)
        return None
    out = []
    for r in (data.get("results") or [])[:num]:
        out.append({
            "title": r.get("title") or "",
            "url": r.get("url") or "",
            "snippet": r.get("content") or r.get("snippet") or "",
        })
    return out


def _try_brave(query: str, num: int) -> list[dict] | None:
    key = os.environ.get("BRAVE_API_KEY")
    if not key:
        return None
    try:
        import httpx
    except ImportError:
        return None
    try:
        resp = httpx.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": min(num, 20)},
            headers={"X-Subscription-Token": key, "Accept": "application/json"},
            timeout=15.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning("brave search failed: %s", e)
        return None
    out = []
    for r in (data.get("web", {}).get("results") or [])[:num]:
        out.append({
            "title": r.get("title") or "",
            "url": r.get("url") or "",
            "snippet": r.get("description") or "",
        })
    return out


def _try_serpapi(query: str, num: int) -> list[dict] | None:
    key = os.environ.get("SERPAPI_API_KEY")
    if not key:
        return None
    try:
        import httpx
    except ImportError:
        return None
    try:
        resp = httpx.get(
            "https://serpapi.com/search.json",
            params={"q": query, "num": num, "api_key": key, "engine": "google"},
            timeout=20.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        # Redact the key: httpx errors embed the full request URL, which
        # carries api_key in the query string for serpapi.
        log.warning("serpapi search failed: %s", str(e).replace(key, "***"))
        return None
    out = []
    for r in (data.get("organic_results") or [])[:num]:
        out.append({
            "title": r.get("title") or "",
            "url": r.get("link") or "",
            "snippet": r.get("snippet") or "",
        })
    return out


# Length-bounded result matcher. Every quantifier is capped so a hostile or
# malformed page can't drive catastrophic backtracking (ReDoS): the old
# unbounded `(.+?) ... .*? ... (.+?)` with DOTALL scanned to EOF for each of
# many result anchors -> quadratic blow-up on a large response. Titles/snippets
# longer than the cap are skipped rather than scanned, which real DDG lite
# output never hits.
_MAX_DDG_HTML = 1_000_000
_DDG_RESULT_RE = re.compile(
    r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.{0,500}?)</a>'
    r'.{0,4000}?<a[^>]+class="result__snippet"[^>]*>(.{0,4000}?)</a>',
    re.DOTALL,
)


def _parse_ddg_results(html: str, num: int) -> list[dict]:
    """Extract up to ``num`` (title, url, snippet) rows from DDG lite HTML.

    Pure + length-bounded (see ``_DDG_RESULT_RE``) so it can't hang on a
    pathological page. DDG wraps the real URL in a ``/l/?uddg=`` redirector,
    which is unwrapped here; non-http targets are dropped.
    """
    out: list[dict] = []
    for m in _DDG_RESULT_RE.finditer(html):
        url_redirect = m.group(1)
        title_html = re.sub(r"<.*?>", "", m.group(2)).strip()
        snippet_html = re.sub(r"<.*?>", "", m.group(3)).strip()
        real = url_redirect
        m_uddg = re.search(r"uddg=([^&]+)", url_redirect)
        if m_uddg:
            from urllib.parse import unquote
            real = unquote(m_uddg.group(1))
        if not real.startswith("http"):
            continue
        out.append({"title": title_html, "url": real, "snippet": snippet_html})
        if len(out) >= num:
            break
    return out


def _try_duckduckgo(query: str, num: int) -> list[dict] | None:
    """Last-resort: DuckDuckGo HTML scrape via the official lite endpoint."""
    try:
        import httpx
    except ImportError:
        return None
    try:
        resp = httpx.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers={"User-Agent": "Mozilla/5.0 (compatible; Maverick/1.0)"},
            timeout=15.0,
            follow_redirects=True,
        )
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        log.warning("duckduckgo search failed: %s", e)
        return None
    # DDG HTML lite returns <a class="result__a" href="...">...</a> with an
    # adjacent <a class="result__snippet">. Parse via a length-bounded regex
    # (capped input size as defense-in-depth) -- not a general HTML parser; the
    # lite endpoint output is stable enough.
    return _parse_ddg_results(html[:_MAX_DDG_HTML], num)


_BACKENDS = {
    "tavily":   _try_tavily,
    "brave":    _try_brave,
    "serpapi":  _try_serpapi,
    "ddg":      _try_duckduckgo,
}

# The third-party host each backend sends the query to (for the enterprise
# tool-egress gate -- the query leaves the boundary to whichever one runs).
_BACKEND_HOSTS = {
    "tavily":  "api.tavily.com",
    "brave":   "api.search.brave.com",
    "serpapi": "serpapi.com",
    "ddg":     "html.duckduckgo.com",
}


def _secure_search_boundary_active() -> bool:
    """Return True when vendor fallback would cross a firm security boundary."""
    try:
        from ..security_defaults import secure_by_default

        if secure_by_default():
            return True
    except Exception:
        # Failure to resolve the security posture cannot authorize query replay.
        return True
    try:
        from ..matter_context import current_matter_context

        return current_matter_context() is not None
    except Exception:
        return True


def _configured_backend() -> tuple[str | None, str | None]:
    """Resolve the single firm backend; malformed configuration fails closed."""
    raw = os.environ.get("MAVERICK_SEARCH_BACKEND")
    if raw is None:
        try:
            from ..config import load_config

            config = load_config() or {}
            firm = config.get("firm") or {}
            raw = firm.get("search_backend") if isinstance(firm, dict) else None
        except Exception as exc:
            return None, f"web_search backend policy is unavailable: {type(exc).__name__}"
    backend = str(raw or "").strip().lower()
    if not backend:
        return None, (
            "secure web_search requires one explicit backend in "
            "MAVERICK_SEARCH_BACKEND or [firm] search_backend"
        )
    if backend not in _BACKENDS:
        return None, (
            f"unsupported web_search backend {backend!r}; choose one of "
            + ", ".join(sorted(_BACKENDS))
        )
    return backend, None


def _run_search(args: dict[str, Any]) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        return "ERROR: query is required"
    num = max(1, min(int(args.get("num_results") or 10), 25))
    site = args.get("site")
    full_query = _augment_query(query, site)

    secure_boundary = _secure_search_boundary_active()
    if secure_boundary:
        selected, policy_error = _configured_backend()
        if policy_error is not None or selected is None:
            return f"ERROR: {policy_error}"
        backends = [(selected, _BACKENDS[selected])]
    else:
        forced = os.environ.get("MAVERICK_SEARCH_BACKEND", "").strip().lower()
        if forced:
            if forced not in _BACKENDS:
                return f"ERROR: unsupported web_search backend {forced!r}"
            backends = [(forced, _BACKENDS[forced])]
        else:
            # Compatibility-only behavior for an explicitly insecure legacy
            # runtime. Firm/matter execution never reaches this fallback chain.
            backends = [
                ("tavily", _try_tavily),
                ("brave", _try_brave),
                ("serpapi", _try_serpapi),
                ("ddg", _try_duckduckgo),
            ]

    # Enterprise mode: the query egresses to a third-party search API. Keep only
    # backends whose host the operator allow-listed; if none, the boundary wins.
    from ..enterprise import (
        egress_permitted,
        enterprise_egress_denial,
        enterprise_enabled,
    )
    if secure_boundary:
        name, _fn = backends[0]
        denial = enterprise_egress_denial(
            f"https://{_BACKEND_HOSTS[name]}", tool="web_search"
        )
        if denial is not None:
            return f"ERROR: {denial}"
    elif enterprise_enabled():
        backends = [
            (name, fn) for name, fn in backends
            if egress_permitted(f"https://{_BACKEND_HOSTS.get(name, '')}")
        ]
        if not backends:
            return (
                "ERROR: web_search is disabled in enterprise mode -- the query would "
                "leave your boundary. Allow-list a search host (e.g. api.tavily.com) "
                "in [enterprise] allowed_hosts to enable it."
            )

    last_err: str | None = None
    for name, fn in backends:
        try:
            results = fn(full_query, num)
        except Exception as e:
            last_err = f"{name}: {type(e).__name__}: {e}"
            log.warning("backend %s raised: %s", name, e)
            if secure_boundary:
                return f"ERROR: configured web_search backend {name} failed"
            continue
        if results is None:
            if secure_boundary:
                return (
                    f"ERROR: configured web_search backend {name} is unavailable; "
                    "no fallback was attempted"
                )
            continue  # compatibility fallback: no key or no httpx
        if not results:
            last_err = f"{name}: returned no results"
            if secure_boundary:
                return f"ERROR: {last_err}; no fallback was attempted"
            continue
        log.info("web_search backend=%s query=%r num=%d", name, query, len(results))
        return f"[backend: {name}]\n\n" + _format_results(results)
    return f"ERROR: no search backend succeeded. last_err={last_err}"


def web_search() -> Tool:
    """Factory: builds the web_search tool."""
    return Tool(
        name="web_search",
        description=(
            "Search the web through the firm's single configured backend. "
            "Returns up to num_results (default 10) entries with title, URL, "
            "and snippet. Use the 'site' arg to scope (e.g. site='github.com')."
        ),
        input_schema=_SEARCH_INPUT_SCHEMA,
        fn=_run_search,
    )
