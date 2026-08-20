"""API keys must not ride in request URLs.

A key in the query string leaks into httpx error reprs (which embed the
full request URL) and into any request/access log. Regression for:
  - newsapi: ``apiKey`` was a query param -> now an X-Api-Key header.
  - web_search serpapi: ``api_key`` query param is required by the API,
    so the failure log is redacted instead.
"""
import logging


def test_serpapi_key_redacted_from_error_log(monkeypatch, caplog):
    monkeypatch.setenv("SERPAPI_API_KEY", "SECRET456")
    import httpx

    def boom(*a, **k):
        # Mimic httpx embedding the full request URL (with the key) in the
        # exception text.
        raise RuntimeError(
            "Server error '500' for url "
            "'https://serpapi.com/search.json?q=x&api_key=SECRET456'"
        )

    monkeypatch.setattr(httpx, "get", boom)
    from maverick.tools.web_search import _try_serpapi
    with caplog.at_level(logging.WARNING):
        out = _try_serpapi("hello", 5)

    assert out is None
    assert "SECRET456" not in caplog.text
    assert "***" in caplog.text
