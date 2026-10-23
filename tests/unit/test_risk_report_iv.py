"""IV 历史接口的读取清洗：历史文档里的 <think> 不能外露。"""
from routers import risk_report


def test_strip_thinking_removes_closed_block():
    raw = "<think>English chain of thought.</think>\n1. 整体水位：正常。"
    assert risk_report._strip_thinking(raw) == "1. 整体水位：正常。"


def test_strip_thinking_drops_unclosed_block():
    assert risk_report._strip_thinking("<think>English chain of thought") is None


def test_strip_thinking_keeps_plain_chinese():
    raw = "1. 整体水位：中证1000 26.3%，偏高。"
    assert risk_report._strip_thinking(raw) == raw
