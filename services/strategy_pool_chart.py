"""Strategy-pool K-line chart context: trades, rounds, and forward returns.

Used by GET /api/strategy-pool/chart-context. Pure helpers are unit-tested
without Mongo; kline loading is injected so router tests can stub prices.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

FORWARD_HORIZONS: Tuple[int, ...] = (1, 5, 20)
DEFAULT_MAX_ROUNDS = 8
DEFAULT_LOOKBACK_DAYS = 240
DEFAULT_LOOKAHEAD_DAYS = 90
HINDSIGHT_DISCLAIMER = "含信号日之后走势，仅供事后验证，不是当时可见盘面。"


def yyyymmdd(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y%m%d")
    text = str(value).strip()
    if not text:
        return ""
    digits = text.replace("-", "").replace("/", "").replace(" ", "")[:8]
    if len(digits) == 8 and digits.isdigit():
        return digits
    return ""


def shift_yyyymmdd(date_str: str, days: int) -> str:
    stamp = yyyymmdd(date_str)
    if not stamp:
        return ""
    dt = datetime.strptime(stamp, "%Y%m%d")
    return (dt + timedelta(days=days)).strftime("%Y%m%d")


def normalize_action(raw: Any) -> str:
    action = str(raw or "").strip().lower()
    if action in {"buy", "sell"}:
        return action
    return action


def symbol_candidates(symbol: str) -> List[str]:
    raw = (symbol or "").strip().upper()
    if not raw:
        return []
    out: List[str] = []

    def add(value: str) -> None:
        if value and value not in out:
            out.append(value)

    add(raw)
    if "." in raw:
        add(raw.split(".", 1)[0])
    else:
        if raw.startswith("6"):
            add(raw + ".SH")
        elif raw.startswith("0") or raw.startswith("3"):
            add(raw + ".SZ")
        elif raw.startswith("8") or raw.startswith("4"):
            add(raw + ".BJ")
    return out


def normalize_trade(doc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    action = normalize_action(doc.get("action"))
    if action not in {"buy", "sell"}:
        return None
    date = yyyymmdd(doc.get("date")) or yyyymmdd(doc.get("datetime"))
    if not date:
        return None
    price = doc.get("price")
    return {
        "date": date,
        "datetime": str(doc.get("datetime") or ""),
        "action": action,
        "price": float(price) if price is not None else None,
        "quantity": int(doc.get("quantity") or 0),
        "pnl": float(doc.get("pnl") or 0),
        "cumulative_pnl": float(doc.get("cumulative_pnl") or 0),
        "symbol": doc.get("symbol"),
        "name": doc.get("name"),
    }


def pair_rounds(trades: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Pair sequential buy→sell into rounds; leftover buys stay open."""
    rounds: List[Dict[str, Any]] = []
    open_buy: Optional[Dict[str, Any]] = None
    for trade in trades:
        action = trade.get("action")
        if action == "buy":
            if open_buy is not None:
                rounds.append({"buy": open_buy, "sell": None})
            open_buy = trade
        elif action == "sell":
            if open_buy is not None:
                rounds.append({"buy": open_buy, "sell": trade})
                open_buy = None
    if open_buy is not None:
        rounds.append({"buy": open_buy, "sell": None})
    return rounds


def select_recent_rounds(
    rounds: Sequence[Dict[str, Any]],
    signal_date: str,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
) -> List[Dict[str, Any]]:
    """Keep the last ``max_rounds`` whose buy date is on or before the signal."""
    if max_rounds <= 0:
        return []
    signal = yyyymmdd(signal_date)
    eligible: List[Dict[str, Any]] = []
    for round_ in rounds:
        buy = round_.get("buy") or {}
        buy_date = yyyymmdd(buy.get("date"))
        if not signal or not buy_date or buy_date <= signal:
            eligible.append(round_)
    if not eligible:
        return list(rounds[:max_rounds])
    return list(eligible[-max_rounds:])


def bars_by_date(records: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for row in records:
        date = yyyymmdd(row.get("trade_date"))
        if date:
            out[date] = row
    return out


def build_markers(
    rounds: Sequence[Dict[str, Any]],
    records: Sequence[Dict[str, Any]],
    signal_date: str,
) -> List[Dict[str, Any]]:
    index = bars_by_date(records)
    signal = yyyymmdd(signal_date)
    markers: List[Dict[str, Any]] = []
    for round_ in rounds:
        for kind in ("buy", "sell"):
            trade = round_.get(kind)
            if not trade:
                continue
            date = yyyymmdd(trade.get("date"))
            bar = index.get(date) or {}
            markers.append(
                {
                    "kind": kind,
                    "trade_date": date,
                    "price": trade.get("price"),
                    "high": bar.get("high"),
                    "low": bar.get("low"),
                    "close": bar.get("close"),
                    "pnl": trade.get("pnl"),
                    "highlighted": bool(kind == "buy" and date == signal),
                    "label": "买" if kind == "buy" else "卖",
                }
            )
    return markers


def forward_returns(
    records: Sequence[Dict[str, Any]],
    signal_date: str,
    entry_price: Optional[float] = None,
    horizons: Sequence[int] = FORWARD_HORIZONS,
) -> Dict[str, Any]:
    signal = yyyymmdd(signal_date)
    empty = {
        "entry_date": signal or None,
        "entry_close": None,
        "entry_price": entry_price,
        "base_price": entry_price,
        "horizons": [
            {"days": int(h), "trade_date": None, "close": None, "return": None, "available": False}
            for h in horizons
        ],
    }
    if not records or not signal:
        return empty

    ordered = sorted(records, key=lambda row: yyyymmdd(row.get("trade_date")))
    dates = [yyyymmdd(row.get("trade_date")) for row in ordered]
    index = next((i for i, date in enumerate(dates) if date >= signal), None)
    if index is None:
        return empty

    entry_close = ordered[index].get("close")
    try:
        entry_close_f = float(entry_close) if entry_close is not None else None
    except (TypeError, ValueError):
        entry_close_f = None
    try:
        base = float(entry_price) if entry_price is not None else entry_close_f
    except (TypeError, ValueError):
        base = entry_close_f

    rows: List[Dict[str, Any]] = []
    for horizon in horizons:
        j = index + int(horizon)
        if j >= len(ordered) or base in (None, 0):
            rows.append(
                {
                    "days": int(horizon),
                    "trade_date": None,
                    "close": None,
                    "return": None,
                    "available": False,
                }
            )
            continue
        close = ordered[j].get("close")
        try:
            close_f = float(close)
        except (TypeError, ValueError):
            rows.append(
                {
                    "days": int(horizon),
                    "trade_date": yyyymmdd(ordered[j].get("trade_date")),
                    "close": None,
                    "return": None,
                    "available": False,
                }
            )
            continue
        rows.append(
            {
                "days": int(horizon),
                "trade_date": yyyymmdd(ordered[j].get("trade_date")),
                "close": close_f,
                "return": close_f / float(base) - 1.0,
                "available": True,
            }
        )

    return {
        "entry_date": dates[index],
        "entry_close": entry_close_f,
        "entry_price": entry_price,
        "base_price": base,
        "horizons": rows,
    }


def kline_window(
    signal_date: str,
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    lookahead_days: int = DEFAULT_LOOKAHEAD_DAYS,
    as_of: Optional[str] = None,
) -> Tuple[str, str]:
    signal = yyyymmdd(signal_date)
    if not signal:
        today = yyyymmdd(as_of) or datetime.now(timezone.utc).strftime("%Y%m%d")
        return shift_yyyymmdd(today, -lookback_days), today
    start = shift_yyyymmdd(signal, -abs(int(lookback_days)))
    end = shift_yyyymmdd(signal, abs(int(lookahead_days)))
    today = yyyymmdd(as_of) or datetime.now(timezone.utc).strftime("%Y%m%d")
    if end > today:
        end = today
    if end < signal:
        end = signal
    return start, end


def load_adjusted_kline(
    symbol: str,
    start_date: str,
    end_date: str,
    adjust: str = "qfq",
) -> List[Dict[str, Any]]:
    from db import data_db
    from stock_data_access.prices import VALID_ADJUST, AdjustedPriceDataAccess

    if adjust not in VALID_ADJUST:
        adjust = "qfq"
    dao = AdjustedPriceDataAccess(db=data_db)
    frame = dao.load_adjusted_ohlc(symbol, start_date, end_date, adjust=adjust)
    if frame is None or getattr(frame, "empty", True):
        return []
    frame = frame.reset_index(drop=True)
    records: List[Dict[str, Any]] = []
    for row in frame.to_dict("records"):
        close = row.get("close")
        if close is None:
            continue
        volume = row.get("volume")
        if volume is None:
            volume = row.get("vol")
        records.append(
            {
                "trade_date": yyyymmdd(row.get("trade_date")),
                "open": row.get("open"),
                "high": row.get("high"),
                "low": row.get("low"),
                "close": close,
                "volume": volume,
            }
        )
    return records


def serialize_round(round_: Dict[str, Any]) -> Dict[str, Any]:
    buy = round_.get("buy")
    sell = round_.get("sell")
    pnl = None
    if sell is not None:
        pnl = sell.get("pnl")
    return {
        "buy": buy,
        "sell": sell,
        "open": sell is None,
        "pnl": pnl,
    }


def build_chart_context(
    *,
    trades: Iterable[Dict[str, Any]],
    records: Sequence[Dict[str, Any]],
    signal_date: str,
    symbol: str,
    strategy: str,
    preset: Optional[str],
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    kline_start: Optional[str] = None,
    kline_end: Optional[str] = None,
    adjust: str = "qfq",
) -> Dict[str, Any]:
    normalized = [item for item in (normalize_trade(doc) for doc in trades) if item]
    normalized.sort(key=lambda item: (item["date"], item["datetime"], item["action"]))
    signal = yyyymmdd(signal_date)
    rounds = select_recent_rounds(pair_rounds(normalized), signal, max_rounds=max_rounds)
    markers = build_markers(rounds, records, signal)
    signal_buy = next(
        (round_.get("buy") for round_ in rounds if (round_.get("buy") or {}).get("date") == signal),
        None,
    )
    entry_price = (signal_buy or {}).get("price") if signal_buy else None
    returns = forward_returns(records, signal, entry_price=entry_price)

    return {
        "success": True,
        "symbol": symbol,
        "strategy": strategy,
        "preset": preset,
        "signal_date": signal,
        "disclaimer": HINDSIGHT_DISCLAIMER,
        "kline": {
            "records": list(records),
            "adjust": adjust,
            "start_date": kline_start,
            "end_date": kline_end,
            "count": len(records),
        },
        "markers": markers,
        "rounds": [serialize_round(round_) for round_ in rounds],
        "forward_returns": returns,
        "signal_buy": signal_buy,
    }


def fetch_trade_docs(
    trade_coll: Any,
    *,
    symbol: str,
    strategy: str,
    preset: Optional[str],
) -> Tuple[List[Dict[str, Any]], str]:
    last_symbol = symbol_candidates(symbol)[0] if symbol_candidates(symbol) else (symbol or "")
    if trade_coll is None:
        return [], last_symbol
    for candidate in symbol_candidates(symbol):
        query: Dict[str, Any] = {"symbol": candidate, "strategy": strategy}
        if preset:
            query["preset"] = preset
        cursor = trade_coll.find(query, {"_id": 0})
        if hasattr(cursor, "sort"):
            cursor = cursor.sort("datetime", 1)
        docs = list(cursor)
        if docs:
            return docs, candidate
        last_symbol = candidate
    return [], last_symbol


def assemble_chart_context(
    *,
    trade_coll: Any,
    symbol: str,
    strategy: str,
    preset: Optional[str],
    signal_date: str,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    lookahead_days: int = DEFAULT_LOOKAHEAD_DAYS,
    adjust: str = "qfq",
    load_kline: Optional[Callable[[str, str, str, str], List[Dict[str, Any]]]] = None,
    as_of: Optional[str] = None,
) -> Dict[str, Any]:
    start, end = kline_window(
        signal_date,
        lookback_days=lookback_days,
        lookahead_days=lookahead_days,
        as_of=as_of,
    )
    docs, matched_symbol = fetch_trade_docs(
        trade_coll,
        symbol=symbol,
        strategy=strategy,
        preset=preset,
    )
    loader = load_kline or load_adjusted_kline
    records = loader(matched_symbol, start, end, adjust)
    return build_chart_context(
        trades=docs,
        records=records,
        signal_date=signal_date,
        symbol=matched_symbol,
        strategy=strategy,
        preset=preset,
        max_rounds=max_rounds,
        kline_start=start,
        kline_end=end,
        adjust=adjust,
    )
