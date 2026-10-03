import json

import httpx
import pytest

from harness.tools import ToolRegistry
from harness.tools.fetch_url import FetchUrl, html_to_text

PUBLIC, PRIVATE = "93.184.216.34", "127.0.0.1"


@pytest.fixture(autouse=True)
def dns(monkeypatch):
    """Everything resolves to a public address unless the host is named 'internal'."""
    def fake(host, port):
        return [(0, 0, 0, "", (PRIVATE if host == "internal" else PUBLIC, port))]

    monkeypatch.setattr("harness.tools.fetch_url.socket.getaddrinfo", fake)


def fetch(handler, url="https://example.com/", start=0, page_chars=12_000):
    http = httpx.Client(transport=httpx.MockTransport(handler))
    registry = ToolRegistry([FetchUrl(http=http, page_chars=page_chars)])
    return registry.run("fetch_url", json.dumps({"url": url, "start": start}))


def page(body, status=200, ctype="text/html; charset=utf-8"):
    return httpx.Response(status, text=body, headers={"content-type": ctype})


def test_html_is_reduced_to_readable_text():
    html = (
        "<html><head><title>T</title><style>p{}</style></head><body><script>evil()</script>"
        "<h1>Hi</h1><p>one  <b>two</b></p><ul><li>a</li><li>b</li></ul></body></html>"
    )
    assert html_to_text(html).splitlines() == ["T", "Hi", "one two", "a", "b"]


def test_result_states_which_characters_it_covers():
    r = fetch(lambda req: page("<p>hello world</p>"))
    assert r.ok and r.output == "[https://example.com/ | characters 0-11 of 11]\nhello world"


def test_long_pages_are_paged_with_a_hint():
    handler = lambda req: page("<p>" + "a" * 50 + "</p>")
    first = fetch(handler, page_chars=20).output
    assert "characters 0-20 of 50" in first and "start=20" in first
    last = fetch(handler, start=40, page_chars=20).output
    assert "characters 40-50 of 50" in last and "more available" not in last


def test_private_and_non_http_targets_are_refused():
    assert "non-public address 127.0.0.1" in fetch(lambda req: page("x"), url="http://internal/").output
    assert "only http(s)" in fetch(lambda req: page("x"), url="file:///etc/passwd").output


def test_redirect_to_a_private_host_is_refused():
    def handler(req):
        if req.url.host == "example.com":
            return httpx.Response(302, headers={"location": "http://internal/admin"})
        return page("secret")

    r = fetch(handler)
    assert not r.ok and "non-public" in r.output


def test_redirect_loop_http_errors_and_binary_types():
    assert "too many redirects" in fetch(lambda req: httpx.Response(302, headers={"location": "/again"})).output
    assert "HTTP 404" in fetch(lambda req: page("nope", status=404)).output
    pdf = fetch(lambda req: httpx.Response(200, content=b"%PDF", headers={"content-type": "application/pdf"}))
    assert not pdf.ok and "unsupported content type 'application/pdf'" in pdf.output


def test_json_passes_through_unparsed():
    r = fetch(lambda req: httpx.Response(200, text='{"a": 1}', headers={"content-type": "application/json"}))
    assert '{"a": 1}' in r.output


def test_bad_arguments_are_reported_to_the_model():
    assert "url: Field required" in ToolRegistry([FetchUrl()]).run("fetch_url", "{}").output
