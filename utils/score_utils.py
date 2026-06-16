"""Local score utilities for the quantFinance package.

This mirrors the helper functionality used in stock-scoring-system but is kept
local to avoid cross-repo imports.
"""
from typing import Any, Dict, Iterable, Optional, Union


SCORE_DIMENSION_FIELDS: Dict[str, str] = {
    "cycle": "cycle_score",
    "growth": "growth_score",
    "fundamental": "fundamental_score",
    "value": "value_score",
    "technical": "technical_score",
    "money_flow": "money_flow_score",
}

DEFAULT_WEIGHTED_SCORE_WEIGHTS: Dict[str, float] = {
    "growth": 0.6,
    "cycle": 0.4,
}


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


def parse_score_weights(
    raw: Any,
    *,
    default: Optional[Dict[str, float]] = None,
    allowed_dimensions: Optional[Iterable[str]] = None,
) -> Dict[str, float]:
    """Parse runtime ranking weights such as ``growth:0.6,cycle:0.4``.

    The returned values are intentionally not normalized; callers can display
    the user's raw inputs while ``weighted_dimension_score`` normalizes at use.
    """
    allowed = set(allowed_dimensions or SCORE_DIMENSION_FIELDS.keys())
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return dict(default or DEFAULT_WEIGHTED_SCORE_WEIGHTS)

    if isinstance(raw, dict):
        items = raw.items()
    else:
        items = []
        for token in str(raw).replace(";", ",").split(","):
            part = token.strip()
            if not part:
                continue
            sep = ":" if ":" in part else "=" if "=" in part else None
            if not sep:
                raise ValueError(f"invalid weight token: {part}")
            k, v = part.split(sep, 1)
            items.append((k, v))

    weights: Dict[str, float] = {}
    for key, value in items:
        dim = str(key).strip().lower()
        if dim not in allowed:
            raise ValueError(f"unsupported weight dimension: {dim}")
        try:
            weight = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid weight for {dim}: {value}") from exc
        if weight < 0:
            raise ValueError(f"weight for {dim} must be non-negative")
        weights[dim] = weight

    if not weights or sum(weights.values()) <= 0:
        raise ValueError("at least one weight must be positive")
    return weights


def weighted_dimension_score(row: Dict[str, Any], weights: Dict[str, float]) -> float:
    total = sum(float(v or 0) for v in weights.values())
    if total <= 0:
        return 0.0
    score = 0.0
    for dim, weight in weights.items():
        field = SCORE_DIMENSION_FIELDS.get(dim)
        if not field:
            continue
        score += safe_float(row.get(field), 0.0) * (float(weight or 0) / total)
    return score
