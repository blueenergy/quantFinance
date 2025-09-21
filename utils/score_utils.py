"""Local score utilities for the quantFinance package.

This mirrors the helper functionality used in stock-scoring-system but is kept
local to avoid cross-repo imports.
"""
from typing import Any, Dict, Union


def safe_float(val: Any, default: float = 0.0) -> float:
    try:
        if isinstance(val, dict):
            if 'balanced' in val:
                raw = val.get('balanced')
            else:
                raw = next((v for v in val.values() if isinstance(v, (int, float))), None)
            return float(raw or default)
        return float(val)
    except Exception:
        return float(default)


def safe_round(val: Any, ndigits: int = 2) -> float:
    return round(safe_float(val, 0.0), ndigits)


def extract_strategy_score(cs: Union[Dict[str, float], float, int], strategy: str = 'balanced') -> float:
    if isinstance(cs, dict):
        if strategy in cs:
            return float(cs.get(strategy) or 0)
        vals = [v for v in cs.values() if isinstance(v, (int, float))]
        return float(max(vals)) if vals else 0.0
    try:
        return float(cs)
    except Exception:
        return 0.0
