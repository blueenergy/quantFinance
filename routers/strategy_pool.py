from fastapi import APIRouter, HTTPException, Depends, Query
from typing import List, Dict, Any, Optional
from db import get_db_collections
from auth import get_current_user
from datetime import datetime, timedelta
from services.strategy_pool_chart import (
    DEFAULT_BACKTEST_DAYS_BACK,
    DEFAULT_LOOKAHEAD_DAYS,
    DEFAULT_LOOKBACK_DAYS,
    DEFAULT_MAX_ROUNDS,
    MAX_BACKTEST_DAYS_BACK,
    MAX_CHART_LOOKBACK_DAYS,
    assemble_chart_context,
    symbol_candidates,
    yyyymmdd,
)

router = APIRouter(tags=["strategy-pool"])


def _sw_l1_name(doc: Optional[Dict[str, Any]]) -> str:
    if not isinstance(doc, dict):
        return ""
    return str(doc.get("sw_l1") or doc.get("sw_l1_name") or doc.get("industry") or "").strip()


def attach_sw_l1_industry(stocks: List[Dict[str, Any]], stock_info_coll) -> None:
    """Fill ``industry`` with Shenwan L1 name; keep an existing pool value."""
    if not stocks:
        return
    for stock in stocks:
        existing = str(stock.get("industry") or stock.get("sw_l1") or "").strip()
        if existing:
            stock["industry"] = existing
            continue
        if stock_info_coll is None:
            continue
        industry = ""
        for key in symbol_candidates(str(stock.get("symbol") or "")):
            doc = stock_info_coll.find_one({"symbol": key})
            if doc is None:
                doc = stock_info_coll.find_one({"ts_code": key})
            industry = _sw_l1_name(doc)
            if industry:
                break
        if industry:
            stock["industry"] = industry


def _require_yyyymmdd(value: Optional[str], field: str) -> str:
    if not value:
        return ""
    stamp = yyyymmdd(value)
    if not stamp:
        raise HTTPException(status_code=400, detail=f"{field} must be YYYYMMDD")
    return stamp


def _extract_params_from_template_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Extract plain params dict from a strategy_template document.

    Supports both:
    - params: {k: v}
    - params_with_desc: {k: {value: v, description: ...}} (or occasionally {k: v})
    """
    if not isinstance(doc, dict):
        return {}

    params = doc.get("params")
    if isinstance(params, dict) and params:
        return params

    params_with_desc = doc.get("params_with_desc")
    if not isinstance(params_with_desc, dict):
        return {}

    out: Dict[str, Any] = {}
    for key, val in params_with_desc.items():
        if isinstance(val, dict) and "value" in val:
            out[key] = val.get("value")
        else:
            out[key] = val
    return out


def _candidate_preset_names(strategy: str, preset: str) -> List[str]:
    """Generate candidate preset names to locate a template.

    The DB / workers often store prefixed presets:
    - hidden_dragon: dragon_conservative/dragon_default/dragon_aggressive
    - single_yang:   yang_conservative/yang_default/yang_aggressive
    - turtle:        turtle_conservative/turtle_standard/turtle_aggressive

    But some template sources might store unprefixed forms (conservative/default/standard/aggressive).
    This helper tries both.
    """
    if not preset:
        return []

    preset = preset.strip()
    if not preset:
        return []

    prefix_map = {
        "hidden_dragon": "dragon_",
        "single_yang": "yang_",
        "turtle": "turtle_",
    }
    expected_prefix = prefix_map.get(strategy, f"{strategy}_")

    # Normalize common synonyms
    def _normalize_tier(tier: str) -> str:
        tier = tier.strip().lower()
        if tier == "standard":
            return "standard"
        if tier == "default":
            return "default"
        return tier

    candidates: List[str] = []

    # as-is
    candidates.append(preset)

    # if prefixed, also try stripped
    if preset.startswith(expected_prefix):
        stripped = preset[len(expected_prefix):]
        candidates.append(stripped)
        # try standard/default interchange if applicable
        if stripped == "default":
            candidates.append("standard")
        if stripped == "standard":
            candidates.append("default")
    else:
        # if unprefixed, try prefixed
        tier = _normalize_tier(preset)
        candidates.append(expected_prefix + tier)
        if tier == "default":
            candidates.append(expected_prefix + "standard")
        if tier == "standard":
            candidates.append(expected_prefix + "default")

    # Also handle cross-prefix inputs (e.g. client sends dragon_default but strategy=hidden_dragon)
    for other_prefix in ("dragon_", "yang_", "turtle_"):
        if preset.startswith(other_prefix) and other_prefix != expected_prefix:
            candidates.append(preset[len(other_prefix):])

    # De-dup while preserving order
    seen = set()
    unique: List[str] = []
    for c in candidates:
        if c and c not in seen:
            unique.append(c)
            seen.add(c)
    return unique


def _normalize_symbol(symbol: str) -> str:
    s = (symbol or "").strip().upper()
    if not s:
        return s
    if "." in s:
        return s
    if s.startswith("6"):
        return s + ".SH"
    if s.startswith("0") or s.startswith("3"):
        return s + ".SZ"
    if s.startswith("8") or s.startswith("4"):
        return s + ".BJ"
    return s

@router.get("/dates")
def get_available_dates(
    strategy: Optional[str] = Query(None, description="Filter by strategy key"),
    preset: Optional[str] = Query(None, description="Filter by preset name"),
    current_user: Dict = Depends(get_current_user)
) -> Dict[str, Any]:
    """Get available dates in the strategy stock pool."""
    try:
        collections = get_db_collections()
        pool_coll = collections.get("strategy_stock_pool")
        if pool_coll is None:
            return {"success": True, "dates": []}
        
        query = {}
        if strategy:
            query["strategy"] = strategy
        if preset:
            query["preset"] = preset
            
        dates = pool_coll.distinct("date", query)
        # Sort dates descending (newest first)
        dates.sort(reverse=True)
        
        return {"success": True, "dates": dates}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/presets")
def get_available_presets(
    strategy: str = Query(..., description="Strategy key to get presets for"),
    current_user: Dict = Depends(get_current_user)
) -> Dict[str, Any]:
    """Get available presets for a specific strategy from database."""
    try:
        collections = get_db_collections()
        pool_coll = collections.get("strategy_stock_pool")
        if pool_coll is None:
            return {"success": True, "presets": []}
        
        query = {"strategy": strategy}
        all_presets = [p for p in pool_coll.distinct("preset", query) if isinstance(p, str) and p]

        # Preset naming conventions are strategy-specific.
        # Backtest workers use:
        # - hidden_dragon: dragon_conservative/dragon_default/dragon_aggressive
        # - single_yang:   yang_conservative/yang_default/yang_aggressive
        # - turtle:        turtle_conservative/turtle_standard/turtle_aggressive
        prefix_map = {
            "hidden_dragon": ["dragon_"],
            "single_yang": ["yang_"],
            "turtle": ["turtle_"],
            "k_regime": ["k_regime_"],
        }

        prefixes = prefix_map.get(strategy, [strategy + "_"])
        filtered_presets = [p for p in all_presets if any(p.startswith(pref) for pref in prefixes)]

        # If the DB uses a different convention, avoid hiding presets entirely.
        if not filtered_presets and all_presets:
            filtered_presets = all_presets

        # Sort presets in a stable, user-friendly order.
        # Prefer conservative -> standard/default -> aggressive -> others.
        def preset_sort_key(preset_name: str):
            if preset_name.endswith("_conservative"):
                tier = 0
            elif preset_name.endswith("_default") or preset_name.endswith("_standard"):
                tier = 1
            elif preset_name.endswith("_aggressive"):
                tier = 2
            else:
                tier = 9
            return (tier, preset_name)

        filtered_presets.sort(key=preset_sort_key)

        return {"success": True, "strategy": strategy, "presets": filtered_presets}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/stocks")
def get_pool_stocks(
    date: Optional[str] = Query(None, description="Single date YYYYMMDD"),
    start_date: Optional[str] = Query(None, description="Inclusive range start YYYYMMDD"),
    end_date: Optional[str] = Query(None, description="Inclusive range end YYYYMMDD"),
    strategy: Optional[str] = Query(None, description="Filter by strategy key"),
    preset: Optional[str] = Query(None, description="Filter by preset name (e.g., conservative, aggressive, default)"),
    limit: int = Query(500, ge=1, le=2000, description="Max rows after date filtering"),
    current_user: Dict = Depends(get_current_user)
) -> Dict[str, Any]:
    """Get stocks in the strategy pool for one date or an inclusive date range."""
    try:
        date_stamp = _require_yyyymmdd(date, "date")
        start_stamp = _require_yyyymmdd(start_date, "start_date")
        end_stamp = _require_yyyymmdd(end_date, "end_date")
        if not date_stamp and not start_stamp and not end_stamp:
            raise HTTPException(status_code=400, detail="date or start_date/end_date is required")
        if start_stamp and end_stamp and start_stamp > end_stamp:
            raise HTTPException(status_code=400, detail="start_date must be on or before end_date")

        collections = get_db_collections()
        pool_coll = collections.get("strategy_stock_pool")
        if pool_coll is None:
            return {"success": True, "stocks": [], "count": 0}

        query: Dict[str, Any] = {}
        if strategy:
            query["strategy"] = strategy
        if preset:
            query["preset"] = preset
        if date_stamp:
            query["date"] = date_stamp

        stocks = list(pool_coll.find(query, {"_id": 0}))
        if not date_stamp:
            lo = start_stamp or "00000000"
            hi = end_stamp or "99999999"
            stocks = [
                row for row in stocks
                if lo <= yyyymmdd(row.get("date")) <= hi
            ]
        stocks = stocks[: int(limit)]
        attach_sw_l1_industry(stocks, collections.get("stock_info"))

        return {
            "success": True,
            "date": date_stamp or None,
            "start_date": start_stamp or None,
            "end_date": end_stamp or None,
            "count": len(stocks),
            "stocks": stocks,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/trade-history")
def get_trade_history(
    symbol: str = Query(..., description="Stock symbol"),
    strategy: Optional[str] = Query(None, description="Strategy key (optional, if not provided, returns trades from all strategies)"),
    preset: Optional[str] = Query(None, description="Preset name"),
    current_user: Dict = Depends(get_current_user)
) -> Dict[str, Any]:
    """Get complete trade history (buy + sell) for a stock to display on K-line chart."""
    try:
        collections = get_db_collections()
        trade_history_coll = collections.get("strategy_trade_history")
        print(trade_history_coll)
        if trade_history_coll is None:
            print(f"[DEBUG] strategy_trade_history collection not found for symbol {symbol}")
            return {"success": True, "trades": []}
        
        query = {"symbol": symbol}
        if strategy:
            query["strategy"] = strategy
        if preset:
            query["preset"] = preset
        
        print(f"[DEBUG] Querying trade history with query: {query}")
        
        # Get all trades sorted by datetime
        cursor = trade_history_coll.find(query, {"_id": 0}).sort("datetime", 1)
        trades = list(cursor)
        
        print(f"[DEBUG] Found {len(trades)} trades for query {query}")
        if trades:
            print(f"[DEBUG] Sample trade: {trades[0] if trades else 'None'}")
        
        response = {
            "success": True,
            "data": {
                "symbol": symbol,
                "strategy": strategy,
                "preset": preset,
                "count": len(trades),
                "trades": trades
            }
        }
        
        print(f"[DEBUG] Returning response: {response}")
        
        return response
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/params")
def get_preset_params(
    strategy: str = Query(..., description="Strategy key"),
    preset: Optional[str] = Query(None, description="Preset name (e.g. dragon_default / default / standard)"),
    current_user: Dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Return the parameter dict for a given strategy + preset.

    Source of truth: strategy_template collection.
    """
    try:
        collections = get_db_collections()
        tpl_coll = collections.get("strategy_template")
        if tpl_coll is None:
            return {
                "success": True,
                "strategy": strategy,
                "preset": preset,
                "params": {},
                "found": False,
                "reason": "strategy_template collection not found",
            }

        template_doc: Optional[Dict[str, Any]] = None

        if preset:
            for cand in _candidate_preset_names(strategy, preset):
                template_doc = tpl_coll.find_one({"strategy": strategy, "preset": cand})
                if template_doc:
                    break
        else:
            template_doc = tpl_coll.find_one({"strategy": strategy, "is_default": True})
            if not template_doc:
                cursor = tpl_coll.find({"strategy": strategy}, {"_id": 0})
                try:
                    template_doc = list(cursor.limit(1))[0] if cursor is not None else None
                except Exception:
                    template_doc = None

        if not template_doc:
            return {
                "success": True,
                "strategy": strategy,
                "preset": preset,
                "params": {},
                "found": False,
                "reason": "template not found",
            }

        params = _extract_params_from_template_doc(template_doc)
        return {
            "success": True,
            "strategy": strategy,
            "requested_preset": preset,
            "preset": template_doc.get("preset"),
            "params": params,
            "found": True,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/chart-context")
def get_strategy_pool_chart_context(
    symbol: str = Query(..., description="Stock symbol"),
    strategy: str = Query(..., description="Strategy key"),
    preset: Optional[str] = Query(None, description="Preset name"),
    signal_date: str = Query(..., description="Pool signal date YYYYMMDD"),
    max_rounds: int = Query(DEFAULT_MAX_ROUNDS, ge=1, le=40),
    lookback_days: int = Query(DEFAULT_LOOKBACK_DAYS, ge=30, le=MAX_CHART_LOOKBACK_DAYS),
    lookahead_days: int = Query(DEFAULT_LOOKAHEAD_DAYS, ge=0, le=400),
    current_user: Dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """K-line overlay for the strategy pool: recent buy/sell rounds + forward returns.

    Hindsight by design: bars after ``signal_date`` are included so the user can
    judge whether that signal worked. Not an as-of reconstruction.
    """
    _ = current_user
    signal = yyyymmdd(signal_date)
    if not signal:
        raise HTTPException(status_code=400, detail="signal_date must be YYYYMMDD")

    collections = get_db_collections()
    trade_coll = collections.get("strategy_trade_history")
    if trade_coll is None:
        raise HTTPException(status_code=404, detail="strategy_trade_history collection not found")

    try:
        return assemble_chart_context(
            trade_coll=trade_coll,
            symbol=symbol,
            strategy=strategy,
            preset=preset,
            signal_date=signal,
            max_rounds=max_rounds,
            lookback_days=lookback_days,
            lookahead_days=lookahead_days,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/backtest-result")
def get_strategy_pool_backtest_result(
    symbol: str = Query(..., description="Stock symbol"),
    strategy: str = Query(..., description="Strategy key"),
    preset: Optional[str] = Query(None, description="Preset name"),
    end_date: Optional[str] = Query(None, description="Optional end date (YYYYMMDD)"),
    days_back: int = Query(
        DEFAULT_BACKTEST_DAYS_BACK,
        ge=30,
        le=MAX_BACKTEST_DAYS_BACK,
        description="Lookback days for the result window",
    ),
    initial_cash: float = Query(1_000_000.0, gt=0, description="Initial cash used to build equity curve"),
    current_user: Dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Instant backtest-like result for Strategy Stock Pool.

    This endpoint does NOT create a new backtest task.
    It reconstructs a result object from existing `strategy_trade_history` records
    written by the screening worker.

    Returns an object compatible with the frontend BacktestManager result viewer:
    {task_id, user_id, symbol, strategy_key, strategy_params?, metrics, trades, equity_curve, created_at}
    """
    try:
        collections = get_db_collections()
        trade_coll = collections.get("strategy_trade_history")
        if trade_coll is None:
            raise HTTPException(status_code=404, detail="strategy_trade_history collection not found")

        norm_symbol = _normalize_symbol(symbol)
        if not norm_symbol:
            raise HTTPException(status_code=400, detail="symbol is required")

        if end_date and (not isinstance(end_date, str) or len(end_date) != 8 or not end_date.isdigit()):
            raise HTTPException(status_code=400, detail="end_date must be YYYYMMDD")

        query: Dict[str, Any] = {
            "symbol": norm_symbol,
            "strategy": strategy,
        }
        if preset:
            query["preset"] = preset

        # Optional date range filter (fast for Mongo if indexed)
        if end_date:
            try:
                end_dt = datetime.strptime(end_date, "%Y%m%d")
                start_dt = end_dt.replace()  # copy
                # Calendar-based window; we only filter on YYYYMMDD string, so compute start_date string.
                start_dt = end_dt - timedelta(days=days_back)
                query["date"] = {"$gte": start_dt.strftime("%Y%m%d"), "$lte": end_date}
            except Exception:
                # If parsing fails, ignore range and rely on symbol+strategy+preset.
                pass

        # Pull trades sorted by datetime
        cursor = trade_coll.find(query, {"_id": 0}).sort("datetime", 1)
        docs = list(cursor)
        if not docs:
            raise HTTPException(status_code=404, detail="No trade history found for given symbol/strategy/preset")

        # Metrics are duplicated on each trade doc by the worker; use the last doc.
        last_doc = docs[-1]
        params_used = last_doc.get("params_used")
        strategy_params = params_used if isinstance(params_used, dict) and params_used else None
        metrics = {
            "total_return": float(last_doc.get("hist_return", 0) or 0),
            "sharpe_ratio": float(last_doc.get("hist_sharpe_ratio", 0) or 0),
            "max_drawdown": float(last_doc.get("hist_max_drawdown", 0) or 0),
            "win_rate": float(last_doc.get("hist_win_rate", 0) or 0),
            "total_trades": int(last_doc.get("hist_total_trades", 0) or 0),
        }
        if last_doc.get("hist_calmar_ratio") is not None:
            metrics["calmar_ratio"] = float(last_doc.get("hist_calmar_ratio") or 0)

        trades: List[Dict[str, Any]] = []
        equity_by_date: Dict[str, float] = {}

        for d in docs:
            dt_str = d.get("datetime")
            raw_action = (d.get("action") or "")
            action = raw_action.lower()
            if action == "buy" or raw_action == "BUY":
                norm_action = "buy"
            elif action == "sell" or raw_action == "SELL":
                norm_action = "sell"
            else:
                norm_action = action

            price = d.get("price")
            qty = d.get("quantity", 0)
            pnl = float(d.get("pnl", 0) or 0)
            cum = float(d.get("cumulative_pnl", 0) or 0)

            trades.append({
                "datetime": dt_str or "",
                "action": norm_action,
                "price": float(price) if price is not None else 0.0,
                "quantity": int(qty or 0),
                "commission": float(d.get("commission", 0) or 0),
                "pnl": pnl,
                "cumulative_pnl": cum,
            })

            # Build a step equity curve by trade date (use last trade of each day)
            date_part = d.get("date")
            if isinstance(date_part, str) and len(date_part) == 8 and date_part.isdigit():
                equity_by_date[date_part] = float(initial_cash) + cum

        equity_curve = [{"date": k, "value": equity_by_date[k]} for k in sorted(equity_by_date.keys())]

        task_id = f"pool_{strategy}_{preset or 'default'}_{norm_symbol}_{(end_date or '')}".replace(" ", "")
        return {
            "task_id": task_id,
            "user_id": str(current_user.get("id", "")),
            "symbol": norm_symbol,
            "strategy_key": strategy,
            "strategy_params": strategy_params,
            "metrics": metrics,
            "trades": trades,
            "equity_curve": equity_curve,
            "created_at": datetime.utcnow().isoformat(),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
