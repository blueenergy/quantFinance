"""Industry relative-strength (RS) detail builder for ranking score drill-down.

Mirrors ``RelativeStrengthFactor`` in stock-scoring-system so the API can
return per-window stock/index returns without waiting for a backfill rerun.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

LOOKBACK_DAYS = 540
MIN_ALIGNED_DAYS = 21
MIN_WINDOWS_FOR_RESONANCE = 2
RESONANCE_BONUS = 10.0
RS_WINDOWS: Tuple[Tuple[int, float, float], ...] = (
    (20, 0.05, 12.0),
    (60, 0.10, 16.0),
    (120, 0.20, 22.0),
)
SW_LEVEL_FIELDS = ("l1_code", "l1_name", "l2_code", "l2_name", "l3_code", "l3_name")


def _symbol_candidates(symbol: str) -> List[str]:
    s = (symbol or "").strip()
    if not s:
        return []
    u = s.upper()
    out: List[str] = []
    seen: set = set()

    def add(x: str) -> None:
        x = (x or "").strip()
        if x and x not in seen:
            seen.add(x)
            out.append(x)

    add(s)
    add(u)
    if u.endswith((".SH", ".SZ", ".BJ")):
        base = s.rsplit(".", 1)[0].strip()
        add(base)
        add(base.upper())
    else:
        digits = "".join(ch for ch in s if ch.isdigit())
        if digits:
            add(digits)
            if u.startswith("6") or u.startswith("5"):
                add(f"{digits}.SH")
            elif u.startswith(("0", "3")):
                add(f"{digits}.SZ")
            elif u.startswith(("4", "8")):
                add(f"{digits}.BJ")
    return out


def _load_close_series(rows: Optional[List[Dict[str, Any]]]) -> Optional[pd.Series]:
    if not rows:
        return None
    df = pd.DataFrame(rows)
    if "close" not in df.columns or "trade_date" not in df.columns:
        return None
    df = df[["trade_date", "close"]].copy()
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["trade_date"] = df["trade_date"].astype(str)
    df = df.dropna(subset=["close"]).drop_duplicates("trade_date", keep="last")
    df = df.sort_values("trade_date")
    series = df.set_index("trade_date")["close"]
    return series if not series.empty else None


def _adjust_volume_price_rows(
    rows: List[Dict[str, Any]],
    factors: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Apply the same hfq close convention used by stock-scoring-system."""
    if not rows or not factors:
        return rows
    price_df = pd.DataFrame(rows)
    factor_df = pd.DataFrame(factors)
    if "trade_date" not in price_df.columns or "trade_date" not in factor_df.columns:
        return rows
    if "close" not in price_df.columns or "adj_factor" not in factor_df.columns:
        return rows

    price_df["trade_date"] = price_df["trade_date"].astype(str)
    factor_df = factor_df[["trade_date", "adj_factor"]].copy()
    factor_df["trade_date"] = factor_df["trade_date"].astype(str)
    factor_df["adj_factor"] = pd.to_numeric(factor_df["adj_factor"], errors="coerce")
    merged = price_df.merge(factor_df, on="trade_date", how="left")
    merged["adj_factor"] = merged["adj_factor"].ffill().bfill()
    if merged["adj_factor"].notna().sum() == 0:
        return rows
    merged["close"] = pd.to_numeric(merged["close"], errors="coerce") * merged["adj_factor"]
    return merged.drop(columns=["adj_factor"]).to_dict("records")


def _find_sw_member(sw_member_col, symbol: str) -> Dict[str, str]:
    if sw_member_col is None:
        return {}
    for candidate in _symbol_candidates(symbol):
        doc = sw_member_col.find_one(
            {"src": "SW2021", "is_new": "Y", "con_code": candidate},
            {"_id": 0, **{field: 1 for field in SW_LEVEL_FIELDS}},
        )
        if doc:
            return {field: doc[field] for field in SW_LEVEL_FIELDS if doc.get(field)}
    return {}


def _fetch_volume_price(vp_col, symbol: str, start_date: str, end_date: str) -> List[Dict[str, Any]]:
    if vp_col is None:
        return []
    candidates = _symbol_candidates(symbol)
    if not candidates:
        return []
    rows = list(
        vp_col.find(
            {"symbol": {"$in": candidates}, "trade_date": {"$gte": start_date, "$lte": end_date}},
            {"_id": 0, "trade_date": 1, "close": 1},
        ).sort("trade_date", 1)
    )
    if rows:
        return rows
    return list(
        vp_col.find(
            {"ts_code": {"$in": candidates}, "trade_date": {"$gte": start_date, "$lte": end_date}},
            {"_id": 0, "trade_date": 1, "close": 1},
        ).sort("trade_date", 1)
    )


def _fetch_stock_adj_factors(adj_col, symbol: str, start_date: str, end_date: str) -> List[Dict[str, Any]]:
    if adj_col is None:
        return []
    candidates = _symbol_candidates(symbol)
    if not candidates:
        return []
    return list(
        adj_col.find(
            {"symbol": {"$in": candidates}, "trade_date": {"$gte": start_date, "$lte": end_date}},
            {"_id": 0, "trade_date": 1, "adj_factor": 1},
        ).sort("trade_date", 1)
    )


def _fetch_sw_daily(sw_daily_col, sw_code: str, start_date: str, end_date: str) -> List[Dict[str, Any]]:
    if sw_daily_col is None or not sw_code:
        return []
    return list(
        sw_daily_col.find(
            {"ts_code": sw_code, "trade_date": {"$gte": start_date, "$lte": end_date}},
            {"_id": 0, "trade_date": 1, "close": 1},
        ).sort("trade_date", 1)
    )


def _relative_strength_score(stock: pd.Series, index: pd.Series) -> Tuple[float, Dict[str, Any]]:
    details: Dict[str, Any] = {}
    sc = stock.to_numpy()
    ic = index.to_numpy()

    score = 50.0
    rs_values: List[float] = []
    windows: List[Dict[str, Any]] = []
    for window, ref, max_pts in RS_WINDOWS:
        if len(sc) < window + 1 or len(ic) < window + 1:
            windows.append({"window": window, "status": "insufficient_data"})
            continue
        stock_ret = sc[-1] / sc[-1 - window] - 1.0 if sc[-1 - window] > 0 else 0.0
        index_ret = ic[-1] / ic[-1 - window] - 1.0 if ic[-1 - window] > 0 else 0.0
        rs = stock_ret - index_ret
        pts = max(-1.0, min(1.0, rs / ref)) * max_pts
        score += pts
        rs_values.append(rs)
        windows.append({
            "window": window,
            "stock_return": round(stock_ret, 6),
            "index_return": round(index_ret, 6),
            "rs": round(rs, 6),
            "points": round(pts, 2),
            "status": "ok",
        })

    resonance = "可用窗口不足，未做共振判定"
    resonance_bonus = 0
    if len(rs_values) >= MIN_WINDOWS_FOR_RESONANCE:
        if all(v > 0 for v in rs_values):
            score += RESONANCE_BONUS
            resonance = "✅ 多窗口持续跑赢基准，相对强势"
            resonance_bonus = RESONANCE_BONUS
        elif all(v < 0 for v in rs_values):
            score -= RESONANCE_BONUS
            resonance = "❌ 多窗口持续跑输基准，相对弱势"
            resonance_bonus = -RESONANCE_BONUS
        else:
            resonance = "相对强度方向分歧"

    details["windows"] = windows
    details["resonance"] = resonance
    details["resonance_bonus"] = resonance_bonus
    return round(max(0.0, min(100.0, score)), 2), details


def build_industry_rs_details(
    *,
    data_db: Dict[str, Any],
    symbol: str,
    score_date: str,
    industry_level: str = "l1",
    stored_score: Optional[float] = None,
) -> Dict[str, Any]:
    """Compute structured industry RS breakdown for one symbol on one score date."""
    if not score_date:
        return {"error": "score_date 为空"}

    start_date = (
        datetime.strptime(score_date, "%Y%m%d") - timedelta(days=LOOKBACK_DAYS)
    ).strftime("%Y%m%d")

    stock_rows = _fetch_volume_price(data_db.get("volume_price"), symbol, start_date, score_date)
    stock_rows = _adjust_volume_price_rows(
        stock_rows,
        _fetch_stock_adj_factors(data_db.get("stock_adj_factor"), symbol, start_date, score_date),
    )
    stock_close = _load_close_series(stock_rows)
    if stock_close is None or len(stock_close) < MIN_ALIGNED_DAYS:
        return {"error": "价量数据不足，无法计算行业相对强度"}

    level = (industry_level or "l1").lower()
    sw_codes = _find_sw_member(data_db.get("sw_index_member"), symbol)
    industry_code = sw_codes.get(f"{level}_code")
    industry_name = sw_codes.get(f"{level}_name")
    if not industry_code:
        return {"error": f"无申万 {level.upper()} 行业映射"}

    index_close = _load_close_series(
        _fetch_sw_daily(data_db.get("sw_daily"), industry_code, start_date, score_date)
    )
    if index_close is None or len(index_close) < MIN_ALIGNED_DAYS:
        return {"error": "行业指数行情不足"}

    aligned = pd.concat({"stock": stock_close, "index": index_close}, axis=1, join="inner").dropna()
    if len(aligned) < MIN_ALIGNED_DAYS:
        return {"error": "个股与行业指数可对齐交易日不足"}

    computed_score, rs_details = _relative_strength_score(aligned["stock"], aligned["index"])
    window_points = round(
        sum(float(w.get("points", 0)) for w in rs_details.get("windows", []) if w.get("status") == "ok"),
        2,
    )
    resonance_bonus = rs_details.get("resonance_bonus", 0)

    return {
        # Keep the headline score consistent with the ranking table. When old
        # documents only have industry_rs_score, the window breakdown below is
        # recomputed live and exposed separately as computed_score.
        "score": stored_score if stored_score is not None else computed_score,
        "computed_score": computed_score,
        "stored_score": stored_score,
        "score_source": "stored" if stored_score is not None else "computed",
        "score_formula": {
            "base": 50,
            "window_points": window_points,
            "resonance_bonus": resonance_bonus,
            "raw_score": round(50 + window_points + resonance_bonus, 2),
        },
        "industry": {
            "code": industry_code,
            "name": industry_name,
            "level": level,
        },
        "windows": rs_details.get("windows", []),
        "resonance": rs_details.get("resonance"),
        "resonance_bonus": resonance_bonus,
        "score_date": score_date,
    }


def normalize_stored_industry_rs_details(doc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return stored ``industry_rs_details`` if it has per-window breakdown."""
    raw = doc.get("industry_rs_details")
    if not isinstance(raw, dict):
        return None
    windows = raw.get("windows")
    if not isinstance(windows, list) or not windows:
        return None
    if not any(isinstance(w, dict) and w.get("status") == "ok" for w in windows):
        return None
    payload = dict(raw)
    if payload.get("score") is None and doc.get("industry_rs_score") is not None:
        payload["score"] = doc.get("industry_rs_score")
    if not isinstance(payload.get("score_formula"), dict):
        window_points = round(
            sum(
                float(w.get("points", 0))
                for w in windows
                if isinstance(w, dict) and w.get("status") == "ok"
            ),
            2,
        )
        resonance_bonus = payload.get("resonance_bonus", 0) or 0
        payload["score_formula"] = {
            "base": 50,
            "window_points": window_points,
            "resonance_bonus": resonance_bonus,
            "raw_score": round(50 + window_points + resonance_bonus, 2),
        }
    if not payload.get("industry") and doc.get("industry_rs_level"):
        payload.setdefault("industry", {"level": doc.get("industry_rs_level")})
    payload["score_date"] = doc.get("score_date")
    return payload
