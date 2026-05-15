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
    doc = get_data_db()["market_spectrum_realtime_daily"].find_one(
        {"snapshot_trade_date": compact_trade_date(trade_date)},
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
    pnl_pct = to_float(features.get("pnl_pct"))
    imbalance = to_float(features.get("bid_ask_imbalance"))
    market_yang = to_float(features.get("market_yang_spectrum"))
    sector_positive = to_float(features.get("sector_positive_ratio"))
    pct_chg = to_float(features.get("pct_chg"))
    avg_trade_size = to_float(features.get("avg_trade_size_yuan"))

    if intraday_pos is not None and intraday_pos >= 0.78:
        score_sell += 22
        sell_reasons.append("near_intraday_high")
    if pnl_pct is not None and pnl_pct >= 1.0:
        score_sell += 16
        sell_reasons.append("position_has_profit")
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


def evaluate_signals_for_user(user_id: str, trade_date: Optional[str] = None) -> Dict[str, Any]:
    ensure_intraday_t0_indexes()
    td = compact_trade_date(trade_date)
    cols = get_db_collections()
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
            eval_coll.update_one(
                {"signal_id": doc["signal_id"], "horizon": doc["horizon"]},
                {"$set": doc},
                upsert=True,
            )
            written += 1
    return {"success": True, "trade_date": td, "signals_count": len(signals), "evaluations_written": written}


def performance_summary(user_id: str, trade_date: Optional[str] = None) -> Dict[str, Any]:
    ensure_intraday_t0_indexes()
    query: Dict[str, Any] = {"user_id": user_id}
    if trade_date:
        query["trade_date"] = compact_trade_date(trade_date)
    rows = list(get_db_collections()["intraday_t0_signal_evaluations"].find(query, {"_id": 0}))
    grouped: Dict[Tuple[str, str], List[float]] = {}
    for row in rows:
        key = (str(row.get("signal_type") or "UNKNOWN"), str(row.get("horizon") or "unknown"))
        ret = to_float(row.get("return_pct"))
        if ret is not None:
            grouped.setdefault(key, []).append(ret)
    summary = []
    for (signal_type, horizon), values in sorted(grouped.items()):
        wins = [v for v in values if (v > 0 if signal_type == "BUY_WATCH" else v < 0)]
        summary.append(
            {
                "signal_type": signal_type,
                "horizon": horizon,
                "count": len(values),
                "avg_return_pct": sum(values) / len(values),
                "win_rate": len(wins) / len(values) if values else 0,
            }
        )
    return {"success": True, "data": summary, "raw_count": len(rows)}
