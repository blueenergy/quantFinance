from __future__ import annotations

import math
import os
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from bson import ObjectId
from pymongo import ASCENDING, DESCENDING

from db import data_db, get_data_db, get_db_collections
from utils.stock_utils import serialize_doc
from utils.symbol_utils import normalize_symbol


SIGNAL_TYPES = {"BUY_WATCH", "SELL_WATCH", "HOLD"}
WATCH_SIGNAL_TYPES = {"BUY_WATCH", "SELL_WATCH"}
DEFAULT_COOLDOWN_MINUTES = int(os.getenv("INTRADAY_T0_COOLDOWN_MINUTES", "15"))
DEFAULT_PRICE_RESET_PCT = float(os.getenv("INTRADAY_T0_COOLDOWN_PRICE_RESET_PCT", "1.0"))

# 验收口径（出处：llm-wiki projects/trading/t0-cross-day-improvement-plan.md）
ACCEPTANCE_LOOKBACK_DAYS = 10
ACCEPTANCE_MIN_CLUSTERS = 30
# t1_max 取的是**次日 high**，是 t1_open 的上包络、实盘不可成交，不进验收口径
ACCEPTANCE_EXCLUDED_HORIZONS = ("t1_max",)
# t1_open 隔夜持有已不是 T+0，单列一档，不与日内 horizon 混在一张表里
ACCEPTANCE_OVERNIGHT_HORIZONS = ("t1_open",)
Z_95 = 1.96

# 评估样本：(毛收益%, 净收益%, 股票, 交易日)。后两项是**聚类键** —— 同一
# (股票, 交易日) 会产生多条信号，行数远大于独立样本数，胜率的置信区间必须按簇算。
EvalSample = Tuple[float, Optional[float], str, str]

_INDEXES_READY = False


def ensure_intraday_t0_indexes() -> None:
    """Create the small set of indexes needed by the T+0 workflow."""
    global _INDEXES_READY
    if _INDEXES_READY:
        return

    collections = get_db_collections()
    collections["intraday_t0_mock_positions"].create_index(
        [("user_id", ASCENDING), ("symbol", ASCENDING)],
        unique=True,
        name="user_symbol_unique",
    )
    collections["intraday_t0_mock_positions"].create_index(
        [("user_id", ASCENDING), ("enabled", ASCENDING)],
        name="user_enabled_idx",
    )
    collections["intraday_t0_signals"].create_index(
        [("user_id", ASCENDING), ("trade_date", DESCENDING), ("snapshot_time", DESCENDING)],
        name="user_trade_snapshot_idx",
    )
    collections["intraday_t0_signals"].create_index(
        [("user_id", ASCENDING), ("symbol", ASCENDING), ("signal_type", ASCENDING), ("snapshot_time", DESCENDING)],
        name="user_symbol_signal_idx",
    )
    collections["intraday_t0_position_snapshots"].create_index(
        [("user_id", ASCENDING), ("symbol", ASCENDING), ("snapshot_time", ASCENDING)],
        unique=True,
        name="user_symbol_snapshot_unique",
    )
    collections["intraday_t0_signal_evaluations"].create_index(
        [("signal_id", ASCENDING), ("horizon", ASCENDING)],
        unique=True,
        name="signal_horizon_unique",
    )
    _INDEXES_READY = True


def now_ts() -> float:
    return time.time()


def compact_trade_date(raw: Optional[str] = None) -> str:
    if raw:
        td = str(raw).strip().replace("-", "")[:8]
        if len(td) == 8 and td.isdigit():
            return td
    return datetime.now().strftime("%Y%m%d")


def current_snapshot_time(trade_date: Optional[str] = None) -> str:
    td = compact_trade_date(trade_date)
    return f"{td}{datetime.now().strftime('%H%M')}"


def to_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None:
        return default
    try:
        n = float(value)
        if math.isnan(n) or math.isinf(n):
            return default
        return n
    except (TypeError, ValueError):
        return default


def to_int(value: Any, default: int = 0) -> int:
    n = to_float(value)
    return int(n) if n is not None else default


def pct(numerator: Optional[float], denominator: Optional[float]) -> Optional[float]:
    if numerator is None or denominator in (None, 0):
        return None
    return numerator / denominator * 100.0


def symbol_candidates(symbol: str) -> List[str]:
    normalized = normalize_symbol(symbol)
    bare = normalized.split(".")[0] if normalized else symbol
    candidates = [normalized, bare, str(symbol or "").strip().upper()]
    return list(dict.fromkeys([c for c in candidates if c]))


def resolve_stock_info(symbol: str) -> Dict[str, Any]:
    cols = get_data_db()
    for cand in symbol_candidates(symbol):
        doc = cols["stock_info"].find_one({"symbol": cand}, {"_id": 0, "symbol": 1, "name": 1, "industry": 1})
        if doc:
            return doc
    return {}


def lookup_sw_industry(symbol: str) -> Dict[str, str]:
    member = data_db["sw_index_member"]
    projection = {
        "_id": 0,
        "con_code": 1,
        "name": 1,
        "l1_code": 1,
        "l1_name": 1,
        "l2_code": 1,
        "l2_name": 1,
        "l3_code": 1,
        "l3_name": 1,
    }
    doc = None
    for cand in symbol_candidates(symbol):
        doc = member.find_one({"con_code": cand, "src": "SW2021", "is_new": "Y"}, projection)
        if doc:
            break
    if not doc:
        for cand in symbol_candidates(symbol):
            doc = member.find_one({"con_code": cand, "src": "SW2021"}, projection, sort=[("in_date", DESCENDING)])
            if doc:
                break
    if not doc:
        return {}
    return {
        "sw_l1_code": str(doc.get("l1_code") or ""),
        "sw_l1_name": str(doc.get("l1_name") or ""),
        "sw_l2_code": str(doc.get("l2_code") or ""),
        "sw_l2_name": str(doc.get("l2_name") or ""),
        "sw_l3_code": str(doc.get("l3_code") or ""),
        "sw_l3_name": str(doc.get("l3_name") or ""),
    }


def latest_daily_record(symbol: str, trade_date: Optional[str] = None) -> Optional[Dict[str, Any]]:
    cols = get_data_db()
    query: Dict[str, Any] = {"symbol": {"$in": symbol_candidates(symbol)}}
    if trade_date:
        query["trade_date"] = compact_trade_date(trade_date)
        realtime_doc = cols["volume_price_realtime_latest"].find_one(query, {"_id": 0})
        if realtime_doc:
            return realtime_doc
    return cols["volume_price"].find_one(query, {"_id": 0}, sort=[("trade_date", DESCENDING)])


def latest_market_spectrum(trade_date: str) -> Dict[str, Any]:
    # Two docs are written per snapshot_time (ma_period 5 and 30) with different
    # yang_spectrum values. Pinning ma_period keeps find_one deterministic;
    # legacy docs omit ma_period and are treated as MA5 (same convention as
    # market.py and MARKET_INSIGHT_PRIMARY_MA_PERIOD).
    doc = get_data_db()["market_spectrum_realtime_daily"].find_one(
        {
            "snapshot_trade_date": compact_trade_date(trade_date),
            "$or": [{"ma_period": 5}, {"ma_period": {"$exists": False}}],
        },
        {"_id": 0},
        sort=[("snapshot_time", DESCENDING)],
    )
    return doc or {}


def moving_average(symbol: str, trade_date: str, window: int = 5) -> Optional[float]:
    cursor = (
        get_data_db()["volume_price"]
        .find(
            {"symbol": {"$in": symbol_candidates(symbol)}, "trade_date": {"$lte": compact_trade_date(trade_date)}},
            {"_id": 0, "close": 1, "trade_date": 1},
        )
        .sort("trade_date", DESCENDING)
        .limit(window)
    )
    closes = [to_float(doc.get("close")) for doc in cursor]
    closes = [c for c in closes if c is not None]
    if not closes:
        return None
    return sum(closes) / len(closes)


def normalize_real_position(doc: Dict[str, Any]) -> Dict[str, Any]:
    symbol = normalize_symbol(str(doc.get("symbol") or doc.get("ts_code") or ""))
    quantity = to_int(doc.get("qty", doc.get("quantity", doc.get("volume", 0))))
    available = to_int(doc.get("can_use_volume", doc.get("available_quantity", doc.get("available_qty", quantity))))
    cost_price = to_float(doc.get("avg_price", doc.get("cost_price")), 0.0) or 0.0
    name = doc.get("stock_name") or doc.get("name") or resolve_stock_info(symbol).get("name") or symbol
    industry = {
        "sw_l1_code": doc.get("sw_l1_code", ""),
        "sw_l1_name": doc.get("sw_l1_name", ""),
        "sw_l2_code": doc.get("sw_l2_code", ""),
        "sw_l2_name": doc.get("sw_l2_name", ""),
        "sw_l3_code": doc.get("sw_l3_code", ""),
        "sw_l3_name": doc.get("sw_l3_name", ""),
    }
    if not any(industry.values()):
        industry = lookup_sw_industry(symbol)
    return {
        "id": str(doc.get("_id", "")),
        "user_id": str(doc.get("user_id") or ""),
        "symbol": symbol,
        "name": name,
        "quantity": quantity,
        "available_quantity": available,
        "cost_price": cost_price,
        "enabled": quantity > 0,
        "position_source": "real",
        **industry,
    }


def normalize_mock_position(doc: Dict[str, Any]) -> Dict[str, Any]:
    row = serialize_doc(doc)
    symbol = normalize_symbol(str(row.get("symbol") or ""))
    return {
        "id": str(row.get("_id") or row.get("id") or ""),
        "user_id": str(row.get("user_id") or ""),
        "symbol": symbol,
        "name": row.get("name") or resolve_stock_info(symbol).get("name") or symbol,
        "quantity": to_int(row.get("quantity")),
        "available_quantity": to_int(row.get("available_quantity", row.get("quantity"))),
        "cost_price": to_float(row.get("cost_price"), 0.0) or 0.0,
        "enabled": bool(row.get("enabled", True)),
        "position_source": row.get("position_source") or "mock",
        "sw_l1_code": row.get("sw_l1_code", ""),
        "sw_l1_name": row.get("sw_l1_name", ""),
        "sw_l2_code": row.get("sw_l2_code", ""),
        "sw_l2_name": row.get("sw_l2_name", ""),
        "sw_l3_code": row.get("sw_l3_code", ""),
        "sw_l3_name": row.get("sw_l3_name", ""),
    }


def enrich_position(position: Dict[str, Any], trade_date: Optional[str] = None) -> Dict[str, Any]:
    td = compact_trade_date(trade_date)
    symbol = position["symbol"]
    daily = latest_daily_record(symbol, td) or latest_daily_record(symbol) or {}
    close = to_float(daily.get("close"))
    cost_price = to_float(position.get("cost_price"), 0.0) or 0.0
    quantity = to_int(position.get("quantity"))
    pnl_pct = pct((close - cost_price) if close is not None else None, cost_price)
    market_value = close * quantity if close is not None else None
    position.update(
        {
            "trade_date": daily.get("trade_date") or td,
            "snapshot_time": daily.get("realtime_snapshot_time"),
            "current_price": close,
            "open": to_float(daily.get("open")),
            "high": to_float(daily.get("high")),
            "low": to_float(daily.get("low")),
            "pre_close": to_float(daily.get("pre_close")),
            "pct_chg": to_float(daily.get("pct_chg")),
            "volume": to_int(daily.get("volume")),
            "amount": to_float(daily.get("amount")),
            "realtime_num": to_int(daily.get("realtime_num")),
            "realtime_amount_yuan": to_float(daily.get("realtime_amount_yuan")),
            "realtime_bid_price1": to_float(daily.get("realtime_bid_price1")),
            "realtime_bid_volume1": to_int(daily.get("realtime_bid_volume1")),
            "realtime_ask_price1": to_float(daily.get("realtime_ask_price1")),
            "realtime_ask_volume1": to_int(daily.get("realtime_ask_volume1")),
            "realtime_trade_time": daily.get("realtime_trade_time"),
            "pnl_pct": pnl_pct,
            "market_value": market_value,
        }
    )
    return position


def get_user_positions(user_id: str, source: str = "auto", trade_date: Optional[str] = None) -> Tuple[str, List[Dict[str, Any]]]:
    ensure_intraday_t0_indexes()
    cols = get_db_collections()
    source = (source or "auto").lower()
    if source not in {"auto", "real", "mock", "watchlist"}:
        source = "auto"

    if source in {"auto", "real"}:
        real_docs = list(cols["trader_positions"].find({"user_id": user_id}))
        real_positions = [normalize_real_position(doc) for doc in real_docs]
        real_positions = [p for p in real_positions if p.get("symbol") and p.get("quantity", 0) > 0]
        if source == "real" or real_positions:
            return "real", [enrich_position(p, trade_date) for p in real_positions]

    if source in {"auto", "mock"}:
        mock_docs = list(cols["intraday_t0_mock_positions"].find({"user_id": user_id}).sort("updated_at", DESCENDING))
        mock_positions = [normalize_mock_position(doc) for doc in mock_docs]
        if source == "mock" or mock_positions:
            return "mock", [enrich_position(p, trade_date) for p in mock_positions]

    watch_doc = cols["user_watchlists"].find_one({"user_id": user_id}) or {}
    symbols = [normalize_symbol(s) for s in watch_doc.get("symbols", []) if s]
    positions = []
    for symbol in symbols[:100]:
        info = resolve_stock_info(symbol)
        daily = latest_daily_record(symbol, trade_date) or {}
        positions.append(
            enrich_position(
                {
                    "id": "",
                    "user_id": user_id,
                    "symbol": symbol,
                    "name": info.get("name") or symbol,
                    "quantity": 0,
                    "available_quantity": 0,
                    "cost_price": to_float(daily.get("close"), 0.0) or 0.0,
                    "enabled": False,
                    "position_source": "watchlist",
                    **lookup_sw_industry(symbol),
                },
                trade_date,
            )
        )
    return "watchlist", positions


def upsert_mock_position(user_id: str, payload: Dict[str, Any], position_id: Optional[str] = None) -> Dict[str, Any]:
    ensure_intraday_t0_indexes()
    symbol = normalize_symbol(str(payload.get("symbol") or "").strip())
    if not symbol:
        raise ValueError("symbol required")

    info = resolve_stock_info(symbol)
    industry = lookup_sw_industry(symbol)
    now = now_ts()
    doc = {
        "user_id": user_id,
        "symbol": symbol,
        "name": payload.get("name") or info.get("name") or symbol,
        "quantity": to_int(payload.get("quantity")),
        "available_quantity": to_int(payload.get("available_quantity", payload.get("quantity"))),
        "cost_price": to_float(payload.get("cost_price"), 0.0) or 0.0,
        "enabled": bool(payload.get("enabled", True)),
        "position_source": "mock",
        **industry,
        "updated_at": now,
    }
    coll = get_db_collections()["intraday_t0_mock_positions"]
    if position_id:
        query: Dict[str, Any] = {"_id": ObjectId(position_id), "user_id": user_id}
    else:
        query = {"user_id": user_id, "symbol": symbol}
        doc["created_at"] = now
    coll.update_one(query, {"$set": doc, "$setOnInsert": {"created_at": now}}, upsert=not bool(position_id))
    saved = coll.find_one({"user_id": user_id, "symbol": symbol})
    return enrich_position(normalize_mock_position(saved or doc))


def create_mock_positions_from_watchlist(user_id: str, default_quantity: int = 100) -> List[Dict[str, Any]]:
    cols = get_db_collections()
    watch_doc = cols["user_watchlists"].find_one({"user_id": user_id}) or {}
    out: List[Dict[str, Any]] = []
    for symbol in watch_doc.get("symbols", []):
        normalized = normalize_symbol(symbol)
        daily = latest_daily_record(normalized) or {}
        payload = {
            "symbol": normalized,
            "quantity": default_quantity,
            "available_quantity": default_quantity,
            "cost_price": to_float(daily.get("close"), 0.0) or 0.0,
            "enabled": True,
        }
        out.append(upsert_mock_position(user_id, payload))
    return out


def time_bucket(snapshot_time: str) -> str:
    hhmm = str(snapshot_time or "")[-4:]
    try:
        hour = int(hhmm[:2])
        minute = int(hhmm[2:])
    except ValueError:
        return "unknown"
    value = hour * 100 + minute
    if value < 1000:
        return "09:30-10:00"
    if value < 1130:
        return "10:00-11:30"
    if value < 1400:
        return "13:00-14:00"
    if value < 1430:
        return "14:00-14:30"
    return "14:30-15:00"


def calc_bid_ask_imbalance(position: Dict[str, Any]) -> Optional[float]:
    bid = to_float(position.get("realtime_bid_volume1"), 0.0) or 0.0
    ask = to_float(position.get("realtime_ask_volume1"), 0.0) or 0.0
    total = bid + ask
    if total <= 0:
        return None
    return (bid - ask) / total


def calc_spread_pct(position: Dict[str, Any]) -> Optional[float]:
    ask = to_float(position.get("realtime_ask_price1"))
    bid = to_float(position.get("realtime_bid_price1"))
    close = to_float(position.get("current_price"))
    if ask is None or bid is None or close in (None, 0):
        return None
    return (ask - bid) / close * 100.0


def calc_intraday_position(position: Dict[str, Any]) -> Optional[float]:
    close = to_float(position.get("current_price"))
    high = to_float(position.get("high"))
    low = to_float(position.get("low"))
    if close is None or high is None or low is None or high <= low:
        return None
    return (close - low) / (high - low)


def sector_strength_for_position(position: Dict[str, Any], trade_date: str) -> Dict[str, Any]:
    l1_code = str(position.get("sw_l1_code") or "")
    if not l1_code:
        return {}
    members = list(
        data_db["sw_index_member"].find(
            {"src": "SW2021", "is_new": "Y", "l1_code": l1_code},
            {"_id": 0, "con_code": 1},
        )
    )
    symbols = [m.get("con_code") for m in members if m.get("con_code")]
    if not symbols:
        return {}
    rows = list(
        get_data_db()["volume_price"].find(
            {"trade_date": compact_trade_date(trade_date), "symbol": {"$in": symbols}},
            {
                "_id": 0,
                "close": 1,
                "pct_chg": 1,
                "realtime_bid_volume1": 1,
                "realtime_ask_volume1": 1,
                "realtime_bid_price1": 1,
                "realtime_ask_price1": 1,
                "realtime_num": 1,
                "realtime_amount_yuan": 1,
            },
        )
    )
    if not rows:
        return {}
    imbalances: List[float] = []
    positives = 0
    narrow_spreads = 0
    for row in rows:
        pct_chg = to_float(row.get("pct_chg"))
        if pct_chg is not None and pct_chg > 0:
            positives += 1
        bid = to_float(row.get("realtime_bid_volume1"), 0.0) or 0.0
        ask = to_float(row.get("realtime_ask_volume1"), 0.0) or 0.0
        if bid + ask > 0:
            imbalances.append((bid - ask) / (bid + ask))
        close = to_float(row.get("close"))
        bid_price = to_float(row.get("realtime_bid_price1"))
        ask_price = to_float(row.get("realtime_ask_price1"))
        if close and bid_price and ask_price and (ask_price - bid_price) / close <= 0.003:
            narrow_spreads += 1
    return {
        "sector_l1_code": l1_code,
        "sector_l1_name": position.get("sw_l1_name", ""),
        "sector_stock_count": len(rows),
        "sector_positive_ratio": positives / len(rows),
        "sector_bid_ask_imbalance": sum(imbalances) / len(imbalances) if imbalances else None,
        "sector_narrow_spread_ratio": narrow_spreads / len(rows),
    }


def build_features(position: Dict[str, Any], trade_date: str, snapshot_time: str) -> Dict[str, Any]:
    close = to_float(position.get("current_price"))
    amount_yuan = to_float(position.get("realtime_amount_yuan"))
    num = to_float(position.get("realtime_num"))
    avg_trade_size = amount_yuan / num if amount_yuan is not None and num and num > 0 else None
    ma5 = moving_average(position["symbol"], trade_date, 5)
    market = latest_market_spectrum(trade_date)
    sector = sector_strength_for_position(position, trade_date)
    return {
        "pct_chg": to_float(position.get("pct_chg")),
        "pnl_pct": to_float(position.get("pnl_pct")),
        "bid_ask_imbalance": calc_bid_ask_imbalance(position),
        "spread_pct": calc_spread_pct(position),
        "intraday_position": calc_intraday_position(position),
        "avg_trade_size_yuan": avg_trade_size,
        "realtime_num": to_int(position.get("realtime_num")),
        "volume": to_int(position.get("volume")),
        "amount_yuan": amount_yuan,
        "ma5": ma5,
        "above_ma5": close is not None and ma5 is not None and close > ma5,
        "market_yang_spectrum": to_float(market.get("yang_spectrum")),
        "market_snapshot_time": market.get("snapshot_time"),
        "time_of_day": time_bucket(snapshot_time),
        **sector,
    }


def cooldown_suppressed(
    user_id: str,
    symbol: str,
    signal_type: str,
    snapshot_time: str,
    price: Optional[float],
    cooldown_minutes: int = DEFAULT_COOLDOWN_MINUTES,
    price_reset_pct: float = DEFAULT_PRICE_RESET_PCT,
) -> bool:
    if signal_type not in WATCH_SIGNAL_TYPES:
        return False
    last = get_db_collections()["intraday_t0_signals"].find_one(
        {"user_id": user_id, "symbol": symbol, "signal_type": signal_type},
        sort=[("snapshot_time", DESCENDING)],
    )
    if not last:
        return False
    try:
        current_dt = datetime.strptime(snapshot_time, "%Y%m%d%H%M")
        last_dt = datetime.strptime(str(last.get("snapshot_time")), "%Y%m%d%H%M")
    except ValueError:
        return False
    last_price = to_float(last.get("price"))
    price_moved = False
    if price is not None and last_price not in (None, 0):
        price_moved = abs(price - last_price) / last_price * 100.0 >= price_reset_pct
    return current_dt - last_dt < timedelta(minutes=cooldown_minutes) and not price_moved


def classify_signal(position: Dict[str, Any], features: Dict[str, Any], snapshot_time: str) -> Tuple[str, float, List[str]]:
    score_sell = 0.0
    score_buy = 0.0
    sell_reasons: List[str] = []
    buy_reasons: List[str] = []
    bucket = features.get("time_of_day")

    intraday_pos = to_float(features.get("intraday_position"))
    imbalance = to_float(features.get("bid_ask_imbalance"))
    market_yang = to_float(features.get("market_yang_spectrum"))
    sector_positive = to_float(features.get("sector_positive_ratio"))
    pct_chg = to_float(features.get("pct_chg"))
    avg_trade_size = to_float(features.get("avg_trade_size_yuan"))

    if intraday_pos is not None and intraday_pos >= 0.78:
        score_sell += 22
        sell_reasons.append("near_intraday_high")
    # 日内涨幅基准（pct_chg = (close - pre_close) / pre_close）。不能用持仓成本的 pnl_pct：
    # 浮盈票会恒 +16 卖压、套牢票永远拿不到分，形成成本锚定偏差（评审 P0-1）
    if pct_chg is not None and pct_chg >= 1.0:
        score_sell += 16
        sell_reasons.append("intraday_gain_1pct")
    if imbalance is not None and imbalance <= -0.18:
        score_sell += 18
        sell_reasons.append("ask_pressure_stronger")
    if sector_positive is not None and sector_positive < 0.45:
        score_sell += 14
        sell_reasons.append("sector_breadth_weak")
    if market_yang is not None and market_yang < 0.45:
        score_sell += 12
        sell_reasons.append("market_breadth_weak")
    if bucket in {"14:00-14:30", "14:30-15:00"}:
        score_sell += 10
        sell_reasons.append("afternoon_momentum_loss")
    if avg_trade_size is not None and avg_trade_size >= 20000 and pct_chg is not None and pct_chg < 0:
        score_sell += 10
        sell_reasons.append("large_trade_down_pressure")

    if intraday_pos is not None and intraday_pos <= 0.35:
        score_buy += 18
        buy_reasons.append("near_intraday_low")
    if imbalance is not None and imbalance >= 0.18:
        score_buy += 18
        buy_reasons.append("bid_pressure_stronger")
    if bool(features.get("above_ma5")):
        score_buy += 14
        buy_reasons.append("reclaim_ma5")
    if market_yang is not None and market_yang >= 0.45:
        score_buy += 12
        buy_reasons.append("market_breadth_recovering")
    if sector_positive is not None and sector_positive >= 0.50:
        score_buy += 14
        buy_reasons.append("sector_breadth_recovering")
    if bucket == "14:30-15:00":
        score_buy += 8
        buy_reasons.append("late_session_t1_premium_setup")
    if avg_trade_size is not None and avg_trade_size >= 20000 and pct_chg is not None and pct_chg > 0:
        score_buy += 10
        buy_reasons.append("large_trade_up_confirmation")

    if score_sell >= 58 and score_sell >= score_buy + 8:
        return "SELL_WATCH", min(score_sell, 100.0), sell_reasons
    if score_buy >= 58 and score_buy >= score_sell + 8:
        return "BUY_WATCH", min(score_buy, 100.0), buy_reasons
    reasons = ["no_high_quality_signal"]
    if score_sell > 0 or score_buy > 0:
        reasons.append("mixed_or_low_confidence")
    return "HOLD", max(score_sell, score_buy), reasons


def store_position_snapshot(
    user_id: str,
    position: Dict[str, Any],
    trade_date: str,
    snapshot_time: str,
    features: Dict[str, Any],
) -> None:
    doc = {
        "user_id": user_id,
        "symbol": position["symbol"],
        "name": position.get("name", ""),
        "trade_date": compact_trade_date(trade_date),
        "snapshot_time": snapshot_time,
        "price": to_float(position.get("current_price")),
        "pct_chg": to_float(position.get("pct_chg")),
        "position_source": position.get("position_source"),
        "features": features,
        "created_at": now_ts(),
    }
    get_db_collections()["intraday_t0_position_snapshots"].update_one(
        {"user_id": user_id, "symbol": position["symbol"], "snapshot_time": snapshot_time},
        {"$set": doc},
        upsert=True,
    )


def generate_signals_for_user(
    user_id: str,
    trade_date: Optional[str] = None,
    snapshot_time: Optional[str] = None,
    source: str = "auto",
) -> Dict[str, Any]:
    ensure_intraday_t0_indexes()
    td = compact_trade_date(trade_date)
    st = (snapshot_time or current_snapshot_time(td)).replace("-", "")[:12]
    actual_source, positions = get_user_positions(user_id, source=source, trade_date=td)
    enabled_positions = [p for p in positions if p.get("enabled", True) and p.get("symbol")]
    signals: List[Dict[str, Any]] = []
    coll = get_db_collections()["intraday_t0_signals"]
    for position in enabled_positions:
        features = build_features(position, td, st)
        store_position_snapshot(user_id, position, td, st, features)
        signal_type, score, reasons = classify_signal(position, features, st)
        if cooldown_suppressed(user_id, position["symbol"], signal_type, st, to_float(position.get("current_price"))):
            signal_type = "HOLD"
            score = min(score, 45.0)
            reasons = ["cooldown_suppressed"]
        doc = {
            "user_id": user_id,
            "symbol": position["symbol"],
            "name": position.get("name", ""),
            "trade_date": td,
            "snapshot_time": st,
            "signal_type": signal_type,
            "signal_score": round(float(score), 2),
            "price": to_float(position.get("current_price")),
            "position_source": position.get("position_source", actual_source),
            "reason_codes": reasons,
            "features": features,
            "created_at": now_ts(),
        }
        result = coll.insert_one(doc)
        doc["_id"] = result.inserted_id
        try:
            from services.intraday_t0_execution import maybe_auto_execute_after_signal

            maybe_auto_execute_after_signal(user_id, doc)
        except Exception:
            pass
        signals.append(serialize_doc(doc))
    return {
        "success": True,
        "user_id": user_id,
        "trade_date": td,
        "snapshot_time": st,
        "position_source": actual_source,
        "positions_count": len(enabled_positions),
        "signals_count": len(signals),
        "signals": signals,
    }


def iter_users_with_intraday_positions() -> Iterable[str]:
    cols = get_db_collections()
    user_ids = set()
    for doc in cols["trader_positions"].find({"qty": {"$gt": 0}}, {"user_id": 1}):
        if doc.get("user_id"):
            user_ids.add(str(doc["user_id"]))
    for doc in cols["intraday_t0_mock_positions"].find({"enabled": True}, {"user_id": 1}):
        if doc.get("user_id"):
            user_ids.add(str(doc["user_id"]))
    return sorted(user_ids)


def generate_signals_for_all_users(trade_date: Optional[str], snapshot_time: Optional[str]) -> Dict[str, Any]:
    results = []
    for user_id in iter_users_with_intraday_positions():
        results.append(generate_signals_for_user(user_id, trade_date=trade_date, snapshot_time=snapshot_time))
    return {
        "success": True,
        "trade_date": compact_trade_date(trade_date),
        "snapshot_time": snapshot_time,
        "users_count": len(results),
        "signals_count": sum(r.get("signals_count", 0) for r in results),
        "results": results,
    }


def _parse_snapshot_time(snapshot_time: str) -> Optional[datetime]:
    try:
        return datetime.strptime(str(snapshot_time), "%Y%m%d%H%M")
    except ValueError:
        return None


def _snapshot_after(user_id: str, symbol: str, target_time: datetime) -> Optional[Dict[str, Any]]:
    target = target_time.strftime("%Y%m%d%H%M")
    return get_db_collections()["intraday_t0_position_snapshots"].find_one(
        {"user_id": user_id, "symbol": symbol, "snapshot_time": {"$gte": target}},
        sort=[("snapshot_time", ASCENDING)],
    )


def _next_daily(symbol: str, trade_date: str) -> Optional[Dict[str, Any]]:
    return get_data_db()["volume_price"].find_one(
        {"symbol": {"$in": symbol_candidates(symbol)}, "trade_date": {"$gt": compact_trade_date(trade_date)}},
        {"_id": 0},
        sort=[("trade_date", ASCENDING)],
    )


REFERENCE_NOTIONAL_FALLBACK = 100_000.0


def round_trip_cost_pct(fee_model: Dict[str, Any], notional: float) -> float:
    """一次回转（买+卖各一腿）的费用百分比，复用交易侧同一套费用模型。"""
    from services.intraday_t0_trades import calc_trade_fees  # 模块级 import 会循环

    if not fee_model or notional <= 0:
        return 0.0
    fees = calc_trade_fees("buy", 1, notional, fee_model) + calc_trade_fees(
        "sell", 1, notional, fee_model
    )
    return fees / notional * 100.0


def _reference_notional(user_id: str, trade_date: str) -> float:
    """参考名义金额 = 当日各持仓市值的中位数（取不到则回落常量）。

    成本百分比**不是定值**：最低佣金 5 元在名义金额 5 万以下会咬（1 万 → 0.15%，
    ≥5 万 → 0.07%）。所以把假设显式化，并把用到的 ref_notional 写进每条评估。
    """
    values: List[float] = []
    try:
        for position in get_user_positions(user_id, trade_date=trade_date)[1]:
            qty = to_float(position.get("quantity"))
            price = to_float(position.get("current_price"))
            if qty and price:
                values.append(qty * price)
    except Exception:
        return REFERENCE_NOTIONAL_FALLBACK
    values = [v for v in values if v > 0]
    if not values:
        return REFERENCE_NOTIONAL_FALLBACK
    values.sort()
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2.0


def net_return_pct(
    return_pct: Optional[float],
    cost_pct: float,
    signal_type: Optional[str],
) -> float:
    """把往返成本折进**价格变动空间**（与 return_pct 同一坐标系）。

    - BUY_WATCH（先买后卖）：成本直接从收益里扣 → `return - cost`
    - SELL_WATCH（先卖后买）：要赚回来价格得**多跌**一个成本 → `return + cost`

    两种风格都保持「net 与 win 判据同向」（BUY 看 >0、SELL 看 <0）。
    若一律写 `return - cost`，SELL_WATCH 会把"价格几乎没跌"也判成赢，
    净胜率反而虚高 —— 实测确实出现过（59% → 78%）。
    """
    value = to_float(return_pct) or 0.0
    if str(signal_type or "") == "SELL_WATCH":
        return value + cost_pct
    return value - cost_pct


def evaluate_signals_for_user(user_id: str, trade_date: Optional[str] = None) -> Dict[str, Any]:
    ensure_intraday_t0_indexes()
    td = compact_trade_date(trade_date)
    cols = get_db_collections()
    from services.intraday_t0_trades import _t0_fee_model  # 同上，避免循环 import

    ref_notional = _reference_notional(user_id, td)
    cost_pct = round_trip_cost_pct(_t0_fee_model(), ref_notional)
    signals = list(cols["intraday_t0_signals"].find({"user_id": user_id, "trade_date": td}))
    eval_coll = cols["intraday_t0_signal_evaluations"]
    written = 0
    horizons = [5, 15, 30, 60]
    for signal in signals:
        base_price = to_float(signal.get("price"))
        if base_price in (None, 0):
            continue
        signal_dt = _parse_snapshot_time(str(signal.get("snapshot_time")))
        if not signal_dt:
            continue
        signal_id = str(signal["_id"])
        symbol = signal.get("symbol")
        docs: List[Dict[str, Any]] = []
        for minutes in horizons:
            snap = _snapshot_after(user_id, symbol, signal_dt + timedelta(minutes=minutes))
            if not snap:
                continue
            future_price = to_float(snap.get("price"))
            if future_price is None:
                continue
            docs.append(
                {
                    "signal_id": signal_id,
                    "user_id": user_id,
                    "symbol": symbol,
                    "trade_date": td,
                    "horizon": f"{minutes}m",
                    "base_price": base_price,
                    "future_price": future_price,
                    "future_snapshot_time": snap.get("snapshot_time"),
                    "return_pct": (future_price - base_price) / base_price * 100.0,
                    "signal_type": signal.get("signal_type"),
                    "evaluated_at": now_ts(),
                }
            )
        latest = latest_daily_record(symbol, td)
        if latest and to_float(latest.get("close")) is not None:
            close = to_float(latest.get("close"))
            docs.append(
                {
                    "signal_id": signal_id,
                    "user_id": user_id,
                    "symbol": symbol,
                    "trade_date": td,
                    "horizon": "close",
                    "base_price": base_price,
                    "future_price": close,
                    "return_pct": (close - base_price) / base_price * 100.0,
                    "signal_type": signal.get("signal_type"),
                    "evaluated_at": now_ts(),
                }
            )
        next_day = _next_daily(symbol, td)
        if next_day:
            open_price = to_float(next_day.get("open"))
            high_price = to_float(next_day.get("high"))
            if open_price is not None:
                docs.append(
                    {
                        "signal_id": signal_id,
                        "user_id": user_id,
                        "symbol": symbol,
                        "trade_date": td,
                        "horizon": "t1_open",
                        "base_price": base_price,
                        "future_price": open_price,
                        "future_trade_date": next_day.get("trade_date"),
                        "return_pct": (open_price - base_price) / base_price * 100.0,
                        "signal_type": signal.get("signal_type"),
                        "evaluated_at": now_ts(),
                    }
                )
            if high_price is not None:
                docs.append(
                    {
                        "signal_id": signal_id,
                        "user_id": user_id,
                        "symbol": symbol,
                        "trade_date": td,
                        "horizon": "t1_max",
                        "base_price": base_price,
                        "future_price": high_price,
                        "future_trade_date": next_day.get("trade_date"),
                        "return_pct": (high_price - base_price) / base_price * 100.0,
                        "signal_type": signal.get("signal_type"),
                        "evaluated_at": now_ts(),
                    }
                )
        for doc in docs:
            doc["ref_notional"] = ref_notional
            doc["cost_pct"] = cost_pct
            doc["net_return_pct"] = net_return_pct(
                doc.get("return_pct"), cost_pct, doc.get("signal_type")
            )
            eval_coll.update_one(
                {"signal_id": doc["signal_id"], "horizon": doc["horizon"]},
                {"$set": doc},
                upsert=True,
            )
            written += 1
    return {
        "success": True,
        "trade_date": td,
        "signals_count": len(signals),
        "evaluations_written": written,
        "ref_notional": ref_notional,
        "cost_pct": cost_pct,
    }


def _is_win(signal_type: str, value: float) -> bool:
    """方向感知的「赢」：BUY_WATCH 先买后卖（涨才算赢），SELL_WATCH 反之（跌才算赢）。

    毛口径、净口径、验收口径**共用这一份判据**。曾经另写过一份统一 `v>0` 的聚合，
    把 SELL_WATCH 方向算反、净胜率虚高到 78%。判据只留一处，谁都别复刻。
    """
    return value > 0 if signal_type == "BUY_WATCH" else value < 0


def _summarize_return_values(
    signal_type: str,
    values: List[Tuple[float, Optional[float], str, str]],
) -> Dict[str, Any]:
    """values 是 (gross_return_pct, net_return_pct, symbol, trade_date) 的列表。

    毛口径字段（avg_return_pct / win_rate）保持原义不变，便于与历史读数比较；
    净口径另给 avg_net_return_pct / net_win_rate —— 成本是收益的一阶项
    （本账户往返 0.07%），毛口径达标完全可能净亏，验收须读净口径。

    ``count`` 是**行数**，不是样本数：同一 (symbol, trade_date) 会产生多条信号，
    5m/15m/30m/60m 多个 horizon 也互相重叠。``clusters`` / ``days`` 才是真实规模
    —— 实测 BUY_WATCH 15m 的 349 行只落在 50 个簇上。读 win_rate 前先看它们。
    """
    if not values:
        return {
            "count": 0,
            "clusters": 0,
            "days": 0,
            "avg_return_pct": 0.0,
            "win_rate": 0.0,
        }
    gross = [v[0] for v in values]
    wins = [v for v in gross if _is_win(signal_type, v)]
    summary: Dict[str, Any] = {
        "count": len(gross),
        "clusters": len({(v[2], v[3]) for v in values}),
        "days": len({v[3] for v in values}),
        "avg_return_pct": sum(gross) / len(gross),
        "win_rate": len(wins) / len(gross),
    }
    net = [v[1] for v in values if v[1] is not None]
    if net:
        net_wins = [v for v in net if _is_win(signal_type, v)]
        summary["avg_net_return_pct"] = sum(net) / len(net)
        summary["net_win_rate"] = len(net_wins) / len(net)
    return summary


def _eval_sample(row: Dict[str, Any], ret: float) -> EvalSample:
    """把一条评估行压成 (毛收益%, 净收益%, 股票, 交易日)。后两项是聚类键。"""
    return (
        ret,
        to_float(row.get("net_return_pct")),
        str(row.get("symbol") or "").strip(),
        str(row.get("trade_date") or ""),
    )


def _group_evaluations_by_signal_horizon(
    rows: Iterable[Dict[str, Any]],
) -> Dict[Tuple[str, ...], List[EvalSample]]:
    grouped: Dict[Tuple[str, ...], List[EvalSample]] = {}
    for row in rows:
        ret = to_float(row.get("return_pct"))
        if ret is None:
            continue
        key = (
            str(row.get("signal_type") or "UNKNOWN"),
            str(row.get("horizon") or "unknown"),
        )
        grouped.setdefault(key, []).append(_eval_sample(row, ret))
    return grouped


def _group_evaluations_by_symbol_horizon(
    rows: Iterable[Dict[str, Any]],
) -> Dict[Tuple[str, ...], List[EvalSample]]:
    grouped: Dict[Tuple[str, ...], List[EvalSample]] = {}
    for row in rows:
        ret = to_float(row.get("return_pct"))
        symbol = str(row.get("symbol") or "").strip()
        if ret is None or not symbol:
            continue
        key = (
            symbol,
            str(row.get("signal_type") or "UNKNOWN"),
            str(row.get("horizon") or "unknown"),
        )
        grouped.setdefault(key, []).append(_eval_sample(row, ret))
    return grouped


def _group_evaluations_by_reason_horizon(
    user_id: str,
    trade_date: Optional[str],
    evaluation_rows: Iterable[Dict[str, Any]],
) -> Dict[Tuple[str, ...], List[EvalSample]]:
    signal_query: Dict[str, Any] = {
        "user_id": user_id,
        "signal_type": {"$in": sorted(WATCH_SIGNAL_TYPES)},
    }
    if trade_date:
        signal_query["trade_date"] = compact_trade_date(trade_date)
    signals = list(
        get_db_collections()["intraday_t0_signals"].find(
            signal_query,
            {"_id": 1, "reason_codes": 1, "signal_type": 1},
        )
    )
    reason_by_signal = {str(doc["_id"]): doc for doc in signals}
    grouped: Dict[Tuple[str, ...], List[EvalSample]] = {}
    for row in evaluation_rows:
        ret = to_float(row.get("return_pct"))
        signal_id = str(row.get("signal_id") or "")
        if ret is None or not signal_id:
            continue
        signal_doc = reason_by_signal.get(signal_id)
        if not signal_doc:
            continue
        signal_type = str(signal_doc.get("signal_type") or row.get("signal_type") or "UNKNOWN")
        if signal_type not in WATCH_SIGNAL_TYPES:
            continue
        horizon = str(row.get("horizon") or "unknown")
        for reason in signal_doc.get("reason_codes") or []:
            reason_code = str(reason or "").strip()
            if not reason_code:
                continue
            grouped.setdefault((reason_code, signal_type, horizon), []).append(
                _eval_sample(row, ret)
            )
    return grouped


def _rows_from_grouped(
    grouped: Dict[Tuple[str, ...], List[Tuple[float, Optional[float]]]],
    field_names: List[str],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for key, values in sorted(grouped.items()):
        signal_type = key[-2]
        stats = _summarize_return_values(signal_type, values)
        row = dict(zip(field_names, key))
        row.update(stats)
        if signal_type not in WATCH_SIGNAL_TYPES:
            # HOLD/UNKNOWN 没有方向：沿用 `v<0` 算赢会把"跌了"读成高胜率
            # （20260910 HOLD 5m avg=-1.029% 却显示 win=83%）。
            # 这里**删键**而不是置 None —— 前端 percent() 是 Number(v)，
            # Number(null)=0 会渲染成 "0.0%"（继续骗人），键缺失才是 "-"。
            # count / 均值仍有信息量，保留。
            row.pop("win_rate", None)
            row.pop("net_win_rate", None)
        rows.append(row)
    return rows


def performance_summary(user_id: str, trade_date: Optional[str] = None) -> Dict[str, Any]:
    ensure_intraday_t0_indexes()
    query: Dict[str, Any] = {"user_id": user_id}
    if trade_date:
        query["trade_date"] = compact_trade_date(trade_date)
    rows = list(get_db_collections()["intraday_t0_signal_evaluations"].find(query, {"_id": 0}))
    summary = _rows_from_grouped(
        _group_evaluations_by_signal_horizon(rows),
        ["signal_type", "horizon"],
    )
    by_symbol = _rows_from_grouped(
        _group_evaluations_by_symbol_horizon(rows),
        ["symbol", "signal_type", "horizon"],
    )
    by_reason = _rows_from_grouped(
        _group_evaluations_by_reason_horizon(user_id, trade_date, rows),
        ["reason_code", "signal_type", "horizon"],
    )
    return {
        "success": True,
        "data": summary,
        "by_symbol": by_symbol,
        "by_reason": by_reason,
        "raw_count": len(rows),
    }


def _clustered_mean_ci(cluster_rates: List[float]) -> Dict[str, Any]:
    """簇统计量的均值 + 近似 95% CI（簇被当作独立单位）。

    先算「簇内一个数」、再跨簇等权平均，簇内相关性就被这一步平均吸收，
    于是 ``se = sd/√n`` 即聚类稳健标准误，不是简化。

    ``ddof=1`` 有两个理由：与 factor 侧的 ``ic.std(ddof=1)`` 一致；且 n=1 时
    它天然未定义，正好逼出「不报 CI」—— 若用 ddof=0 会得到 sd=0 的**零宽区间**，
    那比宽区间更骗人（假装精确）。

    区间**不截断到 [0,1]**：下界为负的准确含义是「不能排除零胜率」，
    是该被看见的信息，截断会把它化妆成 0%。

    簇数 < 2 时**不返回 CI 键**（而非置 None）—— 前端 ``percent()`` 走
    ``Number(v)``，``Number(null)=0`` 会渲染成 "0.0%" 继续骗人，键缺失才是 "-"。
    """
    n = len(cluster_rates)
    if n < 2:
        return {"mean": cluster_rates[0] if n else None, "n": n}
    mean = sum(cluster_rates) / n
    var = sum((v - mean) ** 2 for v in cluster_rates) / (n - 1)
    sd = math.sqrt(max(var, 0.0))  # max 防浮点 -0.0
    half = Z_95 * sd / math.sqrt(n)
    return {
        "mean": mean,
        "n": n,
        "cluster_rate_sd": sd,
        "ci_low": mean - half,
        "ci_high": mean + half,
        "ci_half_width": half,
    }


def _acceptance_row(
    signal_type: str,
    horizon: str,
    samples: List[EvalSample],
    min_clusters: int,
) -> Optional[Dict[str, Any]]:
    """一个 (style, horizon) 的验收行：簇等权净胜率 + 95% CI + 真实样本规模。"""
    net_samples = [s for s in samples if s[1] is not None]
    if not net_samples:
        return None
    by_cluster: Dict[Tuple[str, str], List[float]] = {}
    for _gross, net, symbol, td in net_samples:
        by_cluster.setdefault((symbol, td), []).append(net)
    cluster_rates = [
        sum(1 for v in values if _is_win(signal_type, v)) / len(values)
        for values in by_cluster.values()
    ]
    stats = _clustered_mean_ci(cluster_rates)
    gross = [s[0] for s in samples]
    row: Dict[str, Any] = {
        "signal_type": signal_type,
        "horizon": horizon,
        "rows": len(samples),
        "clusters": len(by_cluster),
        "days": len({s[3] for s in samples}),
        "net_win_rate": stats["mean"],
        # 行级池化净胜率，仅供诊断：与簇等权口径差距大 = 结果被信号多的簇主导
        "pooled_net_win_rate": (
            sum(1 for _g, net, _s, _d in net_samples if _is_win(signal_type, net))
            / len(net_samples)
        ),
        "avg_net_return_pct": sum(s[1] for s in net_samples) / len(net_samples),
        "win_rate": (
            sum(1 for v in gross if _is_win(signal_type, v)) / len(gross) if gross else None
        ),
        # 门槛只是**标签**，不是闸门：不够格时数照报、CI 照给，够不够由人看
        "sufficient_sample": len(by_cluster) >= min_clusters,
    }
    for key in ("cluster_rate_sd", "ci_low", "ci_high", "ci_half_width"):
        if key in stats:
            row[key] = stats[key]
    return row


def acceptance_summary(
    user_id: str,
    lookback_days: int = ACCEPTANCE_LOOKBACK_DAYS,
    trade_date: Optional[str] = None,
    min_clusters: int = ACCEPTANCE_MIN_CLUSTERS,
) -> Dict[str, Any]:
    """Phase 1 验收口径：按 style 分离、按 (股票, 交易日) 聚类的净胜率 + 95% CI。

    **只报数，不判「通过/不通过」**：实测 32–50 个簇时 CI 半宽 **±8.5 ～ ±14pp**，
    且真实区间还要更宽（同日各票共享大盘 beta，簇之间不独立）。这种宽度上做
    自动结论是假精确；够不够格由人看着 CI 决定。

    与 ``performance_summary`` 的三点不同：

    1. **窗口是滚动的**（默认最近 10 个有评估的交易日），不是单日 —— 单日读数
       实测在 33%–67% 之间跳。``trade_date`` 是窗口**结束日**（含），传它可复现。
    2. **行数不是样本数**：``rows`` 是行数，``clusters`` 才是独立单位数
       （实测 BUY_WATCH 15m 的 349 行只落在 50 个簇上）。
    3. **排除 ``t1_max``**（次日 high，是 ``t1_open`` 的上包络、不可成交）；
       ``t1_open`` 隔夜持有已不是 T+0，单独放 ``overnight``。

    BUY / SELL 各自成行、绝不合并 —— 两条腿质量不对称（实测 BUY_WATCH 日内毛
    胜率 45–47% vs SELL_WATCH 54–58%），混算等于拿好腿补坏腿。
    """
    ensure_intraday_t0_indexes()
    # 刻意不用 distinct() / $lte：测试用的 FakeCollection 两者都不支持，
    # 且缺字段的行会在 `None <= str` 上抛 TypeError。取回后在 Python 里切窗口
    # （与 performance_summary 的无界查询同形，无新增性能风险）。
    rows = list(
        get_db_collections()["intraday_t0_signal_evaluations"].find(
            {"user_id": user_id}, {"_id": 0}
        )
    )
    end = compact_trade_date(trade_date) if trade_date else compact_trade_date()
    all_dates = sorted(
        {d for d in (str(r.get("trade_date") or "") for r in rows) if d and d <= end}
    )
    dates = all_dates[-max(int(lookback_days), 1):]
    window = set(dates)
    scoped = [r for r in rows if str(r.get("trade_date") or "") in window]

    intraday: Dict[Tuple[str, str], List[EvalSample]] = {}
    overnight: Dict[Tuple[str, str], List[EvalSample]] = {}
    horizons_seen = set()
    for row in scoped:
        signal_type = str(row.get("signal_type") or "")
        horizon = str(row.get("horizon") or "")
        if horizon:
            horizons_seen.add(horizon)
        # HOLD 没有方向：`v<0` 算赢会把「跌了」读成高胜率（20260910 事故），
        # 验收表里绝不能出现这种行。
        if signal_type not in WATCH_SIGNAL_TYPES:
            continue
        if horizon in ACCEPTANCE_EXCLUDED_HORIZONS:
            continue
        ret = to_float(row.get("return_pct"))
        if ret is None:
            continue
        bucket = overnight if horizon in ACCEPTANCE_OVERNIGHT_HORIZONS else intraday
        bucket.setdefault((signal_type, horizon), []).append(_eval_sample(row, ret))

    def _build(source: Dict[Tuple[str, str], List[EvalSample]]) -> List[Dict[str, Any]]:
        built = []
        for (signal_type, horizon), samples in sorted(source.items()):
            built_row = _acceptance_row(signal_type, horizon, samples, min_clusters)
            if built_row:
                built.append(built_row)
        return built

    return {
        "success": True,
        "data": _build(intraday),
        "overnight": _build(overnight),
        "lookback_days": int(lookback_days),
        "window_start": dates[0] if dates else None,
        "window_end": dates[-1] if dates else None,
        "dates": dates,
        "min_clusters": min_clusters,
        "excluded_horizons": list(ACCEPTANCE_EXCLUDED_HORIZONS),
        "horizons_seen": sorted(horizons_seen),
        "raw_count": len(rows),
        "window_count": len(scoped),
        "generated_at": now_ts(),
        "notes": [
            "`net_win_rate` = 簇内先算胜率、再跨簇等权平均；簇 = (股票, 交易日)。"
            "`pooled_net_win_rate` 是行级池化口径，**仅供诊断**：两者差距大 = "
            "结果被「信号多的簇」主导，此时期望值应以簇等权为准。",
            "CI 是簇稳健的近似 95% 区间，**不截断到 [0,1]**：下界为负的含义是"
            "「不能排除零胜率」，不是算错了。",
            "⚠️ 同一天各票共享大盘 beta，簇之间并不独立 → 有效自由度低于簇数，"
            "**真实区间比这里更宽**。故 `days` 必须与 `clusters` 一起读。",
            "BUY / SELL 分别成行、绝不合并；HOLD 无方向不进此表。",
            "t1_max（次日 high，t1_open 的上包络、不可成交）已排除；"
            "t1_open 隔夜持有已不是 T+0，单列 overnight。",
        ],
    }


def _csv_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    if any(ch in text for ch in [",", '"', "\n", "\r"]):
        return f'"{text.replace(chr(34), chr(34) + chr(34))}"'
    return text


def export_signals_csv(user_id: str, trade_date: Optional[str] = None) -> str:
    ensure_intraday_t0_indexes()
    query: Dict[str, Any] = {"user_id": user_id}
    if trade_date:
        query["trade_date"] = compact_trade_date(trade_date)
    rows = list(
        get_db_collections()["intraday_t0_signals"]
        .find(query, {"_id": 0})
        .sort([("snapshot_time", -1), ("_id", -1)])
    )
    headers = [
        "trade_date",
        "snapshot_time",
        "symbol",
        "name",
        "signal_type",
        "signal_score",
        "price",
        "position_source",
        "reason_codes",
        "market_yang_spectrum",
        "bid_ask_imbalance",
        "intraday_position",
        "pct_chg",
        "pnl_pct",
    ]
    lines = [",".join(headers)]
    for row in rows:
        features = row.get("features") or {}
        values = {
            "trade_date": row.get("trade_date"),
            "snapshot_time": row.get("snapshot_time"),
            "symbol": row.get("symbol"),
            "name": row.get("name"),
            "signal_type": row.get("signal_type"),
            "signal_score": row.get("signal_score"),
            "price": row.get("price"),
            "position_source": row.get("position_source"),
            "reason_codes": "|".join(row.get("reason_codes") or []),
            "market_yang_spectrum": features.get("market_yang_spectrum"),
            "bid_ask_imbalance": features.get("bid_ask_imbalance"),
            "intraday_position": features.get("intraday_position"),
            "pct_chg": features.get("pct_chg"),
            "pnl_pct": features.get("pnl_pct"),
        }
        lines.append(",".join(_csv_cell(values[header]) for header in headers))
    return "\n".join(lines) + ("\n" if lines else "")


def export_evaluations_csv(user_id: str, trade_date: Optional[str] = None) -> str:
    ensure_intraday_t0_indexes()
    query: Dict[str, Any] = {"user_id": user_id}
    if trade_date:
        query["trade_date"] = compact_trade_date(trade_date)
    rows = list(
        get_db_collections()["intraday_t0_signal_evaluations"]
        .find(query, {"_id": 0})
        .sort([("trade_date", -1), ("symbol", 1), ("horizon", 1)])
    )
    headers = [
        "signal_id",
        "user_id",
        "symbol",
        "trade_date",
        "signal_type",
        "horizon",
        "base_price",
        "future_price",
        "future_snapshot_time",
        "future_trade_date",
        "return_pct",
        "cost_pct",
        "net_return_pct",
        "ref_notional",
        "evaluated_at",
    ]
    lines = [",".join(headers)]
    for row in rows:
        lines.append(",".join(_csv_cell(row.get(header)) for header in headers))
    return "\n".join(lines) + ("\n" if lines else "")
