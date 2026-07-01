"""Shared pinyin helpers for stock name search (initials + full spelling)."""

from __future__ import annotations

from typing import Any

try:
    from pypinyin import Style, load_phrases_dict, pinyin
except ImportError:  # pragma: no cover - optional at import time
    Style = None
    load_phrases_dict = None
    pinyin = None

if load_phrases_dict is not None:
    # pypinyin defaults "长" to zhang, but many A-share names use chang.
    load_phrases_dict(
        {
            "长海": [["chang"], ["hai"]],
            "长江": [["chang"], ["jiang"]],
            "长城": [["chang"], ["cheng"]],
            "长安": [["chang"], ["an"]],
            "长电": [["chang"], ["dian"]],
            "长春": [["chang"], ["chun"]],
            "长白": [["chang"], ["bai"]],
            "长源": [["chang"], ["yuan"]],
            "长虹": [["chang"], ["hong"]],
            "长亮": [["chang"], ["liang"]],
            "长信": [["chang"], ["xin"]],
            "长盛": [["chang"], ["sheng"]],
            "长青": [["chang"], ["qing"]],
        }
    )


def pinyin_initials(text: Any) -> str:
    """First-letter abbreviation, e.g. 平安银行 -> PAYH."""
    if not text or pinyin is None or Style is None:
        return ""
    try:
        return "".join(row[0] for row in pinyin(str(text), style=Style.FIRST_LETTER)).upper()
    except Exception:
        return ""


def pinyin_full(text: Any) -> str:
    """Full pinyin without separators, e.g. 平安银行 -> PINGANYINHANG."""
    if not text or pinyin is None or Style is None:
        return ""
    try:
        return "".join(row[0] for row in pinyin(str(text), style=Style.NORMAL)).upper()
    except Exception:
        return ""


def stock_name_pinyin_fields(name: Any) -> dict[str, str]:
    """Return persisted search fields for a stock display name."""
    label = str(name or "").strip()
    if not label:
        return {"pinyin": "", "pinyin_full": ""}
    return {
        "pinyin": pinyin_initials(label),
        "pinyin_full": pinyin_full(label),
    }
