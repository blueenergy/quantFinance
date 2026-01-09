from fastapi import APIRouter, HTTPException, Depends, Query
from typing import List, Dict, Any, Optional
from db import get_db_collections
from auth import get_current_user
import time
from datetime import datetime

router = APIRouter(prefix="/strategy-pool", tags=["strategy-pool"])


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
    date: str = Query(..., description="Date in YYYYMMDD format"),
    strategy: Optional[str] = Query(None, description="Filter by strategy key"),
    preset: Optional[str] = Query(None, description="Filter by preset name (e.g., conservative, aggressive, default)"),
    current_user: Dict = Depends(get_current_user)
) -> Dict[str, Any]:
    """Get stocks in the strategy pool for a specific date, strategy, and preset."""
    try:
        collections = get_db_collections()
        pool_coll = collections.get("strategy_stock_pool")
        if pool_coll is None:
            return {"success": True, "stocks": []}
        
        query = {"date": date}
        if strategy:
            query["strategy"] = strategy
        if preset:
            query["preset"] = preset
            
        cursor = pool_coll.find(query, {"_id": 0})
        stocks = []
        for doc in cursor:
            # Add some derived fields if needed
            stocks.append(doc)
        
        return {
            "success": True, 
            "date": date,
            "count": len(stocks),
            "stocks": stocks
        }
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
