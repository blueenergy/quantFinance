from fastapi import APIRouter, HTTPException, Depends
from typing import List, Dict, Any
from models.all_models import BaseModel
from db import get_db_collections
import time
from auth import get_current_user

router = APIRouter(prefix="/strategy", tags=["strategy"])

# Fallback strategy map (used when database is not available)
STRATEGY_MAP_FALLBACK = {
	"grid": "网格策略",
	"buyhold": "买入持有",
	"boll": "布林带策略",
	"mean_reversion": "均值回归",
	"momentum_reversal": "动量反转",
	"atr_breakout": "ATR突破",
	"volume_mfi_breakout": "量价突破",
	"sma_cross": "双均线交叉",
	"triple_sma": "三均线交叉",
	"turtle": "海龟交易法",
	"quick": "快速测试",
	"index_enhanced_turtle": "指数增强海龟",
	"index_enhanced_grid": "指数增强网格",
	"hidden_dragon": "潜龙低吸",
	"single_yang": "单阳不破",
	"asymmetric_dragon": "非对称潜龙低吸",
	"asymmetric_yang": "非对称单阳不破",
	"exit_monitor": "只监控卖出",
}

@router.get("/strategies")
def list_strategies(current_user: Dict = Depends(get_current_user)) -> Dict[str, Any]:
	"""List available strategies from database (with fallback)."""
	collections = get_db_collections()
	col = collections.get("strategy_metadata")
	
	if col is None:
		# Fallback to hardcoded map if database not available
		return _list_strategies_fallback(current_user)
	
	try:
		# Read from database
		strats = list(col.find({}, {"_id": 0}))
		if not strats:
			# If no data in database, use fallback
			return _list_strategies_fallback(current_user)
			
		out = []
		user_service_level = current_user.get("service_level", "free") if current_user else "free"
		
		for s in strats:
			key = s.get("key")
				
			item = {
				"key": key,
				"name": s.get("name", key),
				"summary": s.get("summary", ""),
				"principle": s.get("principle", ""),
				"suitable_for": s.get("suitable_for", ""),
				"risk_warning": s.get("risk_warning", ""),
				"params_hint": s.get("params_hint", ""),
				"allow_live": s.get("allow_live", True),
				"allow_backtest": s.get("allow_backtest", True),
				"min_service_level": s.get("min_service_level", "free"),
				# Minute/daily variants share a family so the UI can show one
				# strategy with a cadence selector instead of two columns.
				"family": s.get("family", key),
				"family_name": s.get("family_name", s.get("name", key)),
				"cadence": s.get("cadence", "minute"),
			}
			# 标记 VIP 专属策略 based on min_service_level
			if item["min_service_level"] == "vip":
				item["vip_only"] = True
			
			# 检查用户是否有权限使用该策略
			user_level_index = ['free', 'basic', 'vip', 'premium'].index(user_service_level)
			required_level_index = ['free', 'basic', 'vip', 'premium'].index(item["min_service_level"])
			item["can_use"] = user_level_index >= required_level_index
			
			out.append(item)
		return {"ok": True, "strategies": out}
	except Exception as e:
		print(f"Error reading strategy_metadata: {e}")
		return _list_strategies_fallback(current_user)


def _list_strategies_fallback(current_user: Dict = None) -> Dict[str, Any]:
	"""Fallback to hardcoded strategy map."""
	out = []
	user_service_level = current_user.get("service_level", "free") if current_user else "free"
	
	for key, name in STRATEGY_MAP_FALLBACK.items():
		item = {
			"key": key,
			"name": name,
			"min_service_level": "vip" if key in ["hidden_dragon", "single_yang"] else "free",
		}
		# 标记 VIP 专属策略
		if item["min_service_level"] == "vip":
			item["vip_only"] = True
		
		# 检查用户是否有权限使用该策略
		user_level_index = ['free', 'basic', 'vip', 'premium'].index(user_service_level)
		required_level_index = ['free', 'basic', 'vip', 'premium'].index(item["min_service_level"])
		item["can_use"] = user_level_index >= required_level_index
		
		out.append(item)
	return {"ok": True, "strategies": out}


@router.get("/templates")
def list_strategy_templates(strategy: str = None) -> Dict[str, Any]:
	"""List parameter templates from strategy_template collection.
	
	Args:
		strategy: Optional filter by strategy type (e.g., 'grid', 'turtle')
	
	Returns:
		List of templates with preset name, description, and params
	"""
	collections = get_db_collections()
	col = collections.get("strategy_template")
	if col is None:
		return {"ok": False, "error": "strategy_template collection not found"}
	
	query = {}
	if strategy:
		query["strategy"] = strategy
	
	templates = list(col.find(query, {"_id": 0}))
	
	# Group by strategy
	grouped = {}
	for tpl in templates:
		strat = tpl.get("strategy", "unknown")
		if strat not in grouped:
			grouped[strat] = []
		grouped[strat].append({
			"preset": tpl.get("preset"),
			"description": tpl.get("description", ""),
			"params_with_desc": tpl.get("params_with_desc", {}),  # Only return params_with_desc
			"is_default": tpl.get("is_default", False),
		})
	
	return {"ok": True, "templates": grouped}

@router.get("/configs/{symbol}")
def get_config(symbol: str) -> Dict[str, Any]:
	collections = get_db_collections()
	col = collections["watch_configs"] if "watch_configs" in collections else collections["watchlist_strategies"]
	doc = col.find_one({"symbol": symbol})
	if not doc:
		return {"ok": False, "error": "not found"}
	doc["_id"] = str(doc.get("_id", ""))
	return {"ok": True, "config": doc}

@router.post("/configs/{symbol}")
def update_config(symbol: str, payload: Dict[str, Any]) -> Dict[str, Any]:
	"""更新策略配置，自动设置 engine="vnpy" """
	collections = get_db_collections()
	col = collections["watch_configs"] if "watch_configs" in collections else collections["watchlist_strategies"]
	update_doc = {
		"symbol": symbol,
		"enabled": bool(payload.get("enabled", True)),
		"strategy_key": payload.get("strategy_key", "grid"),
		"params": payload.get("params", {}),
		"refresh_interval_sec": float(payload.get("refresh_interval_sec", 0.5)),
		"updated_at": time.time(),
		"engine": "vnpy",  # 🔥 自动设置 engine 为 vnpy（实盘策略仅支持 vnpy）
	}
	col.update_one({"symbol": symbol}, {"$set": update_doc}, upsert=True)
	return {"ok": True}

@router.get("/configs")
def list_configs() -> List[Dict[str, Any]]:
	collections = get_db_collections()
	col = collections["watch_configs"] if "watch_configs" in collections else collections["watchlist_strategies"]
	return [
		{**doc, "_id": str(doc.get("_id", ""))}
		for doc in col.find({})
	]

@router.post("/configs")
def upsert_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
	"""批量更新策略配置，自动设置 engine="vnpy" """
	collections = get_db_collections()
	col = collections["watch_configs"] if "watch_configs" in collections else collections["watchlist_strategies"]
	payload = dict(cfg)
	payload["updated_at"] = time.time()
	payload["engine"] = "vnpy"  # 🔥 自动设置 engine 为 vnpy（实盘策略仅支持 vnpy）
	col.update_one({"symbol": payload["symbol"], "user_id": payload.get("user_id", "")}, {"$set": payload}, upsert=True)
	return {"ok": True, "symbol": payload["symbol"]}

@router.post("/configs/disable")
def disable_config(user_id: str, symbol: str) -> Dict[str, Any]:
	collections = get_db_collections()
	col = collections["watch_configs"] if "watch_configs" in collections else collections["watchlist_strategies"]
	col.update_one({"symbol": symbol, "user_id": user_id}, {"$set": {"enabled": False, "updated_at": time.time()}})
	return {"ok": True}
