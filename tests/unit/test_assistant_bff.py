"""Unit tests for Hermes BFF stream parsing (MVP)."""

from routers.assistant_bff import _parse_openai_sse_line


def test_parse_openai_delta_content():
    line = 'data: {"choices":[{"index":0,"delta":{"content":"Hi"},"finish_reason":null}]}'
    assert _parse_openai_sse_line(line) == "Hi"


def test_parse_openai_done():
    assert _parse_openai_sse_line("data: [DONE]") == "__DONE__"


def test_parse_ignores_non_data():
    assert _parse_openai_sse_line(": keepalive") is None
