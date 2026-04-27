# routers/stock_scoring.py
from fastapi import APIRouter, HTTPException, Depends, Query, Request
from db import get_db_collections, get_data_db
from utils.score_utils import safe_round, safe_float, extract_strategy_score
from typing import Optional, List, Any, cast, Dict, Tuple, Set
from pydantic import BaseModel
from datetime import datetime, timedelta
from bson import ObjectId

router = APIRouter(tags=["股票排名"])


# using helpers from stock-scoring-system/utils/score_utils.py


def get_db_collections_from_request(request: Request):
    # keep a small shim for compatibility with other routers
    return get_db_collections()


RETURN_SINCE_EMPTY: Dict[str, Any] = {
    "return_since_score_pct": None,
    "price_base_trade_date": None,
    "price_latest_trade_date": None,
}


def _close_from_volume_price_doc(doc: Optional[dict]) -> Optional[float]:
    if not doc:
        return None
    try:
        c = float(doc.get("close"))
        return c if c > 0 else None
    except (TypeError, ValueError):
        return None


def _trade_date_str(doc: Optional[dict]) -> Optional[str]:
    if not doc:
        return None
    td = doc.get("trade_date")
    if td is None:
        return None
    if hasattr(td, "strftime"):
        return td.strftime("%Y%m%d")
    if isinstance(td, int):
        s = str(td)
        return s[:8] if len(s) >= 8 else s
    s = str(td)
    return s.replace("-", "")[:8] if s else None


def _fetch_base_doc_single(volume_price_col: Any, symbol: str, sd: str) -> Optional[dict]:
    """单只股票基准 K：字符串 / BSON date / int 三种 trade_date 存法依次尝试。"""
    base_doc = volume_price_col.find_one(
        {"symbol": symbol, "trade_date": {"$lte": sd}},
        sort=[("trade_date", -1)],
    )
    if not base_doc:
        try:
            y, mo, day = int(sd[:4]), int(sd[4:6]), int(sd[6:8])
            dt_end = datetime(y, mo, day)
            base_doc = volume_price_col.find_one(
                {"symbol": symbol, "trade_date": {"$lte": dt_end}},
                sort=[("trade_date", -1)],
            )
        except (ValueError, IndexError):
            base_doc = None
    if not base_doc:
        try:
            as_int = int(sd)
            base_doc = volume_price_col.find_one(
                {"symbol": symbol, "trade_date": {"$lte": as_int}},
                sort=[("trade_date", -1)],
            )
        except ValueError:
            base_doc = None
    return base_doc


def _return_since_from_docs(latest_doc: Optional[dict], base_doc: Optional[dict]) -> dict:
    out = dict(RETURN_SINCE_EMPTY)
    latest_close = _close_from_volume_price_doc(latest_doc)
    base_close = _close_from_volume_price_doc(base_doc)
    if latest_close is None or base_close is None:
        return out
    pct = (latest_close / base_close - 1.0) * 100.0
    out["return_since_score_pct"] = round(pct, 2)
    out["price_base_trade_date"] = _trade_date_str(base_doc)
    out["price_latest_trade_date"] = _trade_date_str(latest_doc)
    return out


def _bulk_latest_by_symbol(volume_price_col: Any, symbols: List[str]) -> Dict[str, Any]:
    """一次聚合：每只股票最新一根日线。"""
    if not symbols:
        return {}
    uniq = list(dict.fromkeys(symbols))
    try:
        cur = volume_price_col.aggregate(
            [
                {"$match": {"symbol": {"$in": uniq}}},
                {"$sort": {"trade_date": -1}},
                {"$group": {"_id": "$symbol", "doc": {"$first": "$$ROOT"}}},
            ]
        )
        return {row["_id"]: row["doc"] for row in cur if row.get("_id") and row.get("doc")}
    except Exception:
        return {}


def _bulk_base_by_symbol_string(
    volume_price_col: Any, symbols: List[str], sd: str
) -> Dict[str, Any]:
    """一次聚合：trade_date 为字符串时可比较的「不晚于 score_date 的最近一根」。"""
    if not symbols or len(sd) < 8:
        return {}
    uniq = list(dict.fromkeys(symbols))
    try:
        cur = volume_price_col.aggregate(
            [
                {"$match": {"symbol": {"$in": uniq}, "trade_date": {"$lte": sd}}},
                {"$sort": {"symbol": 1, "trade_date": -1}},
                {"$group": {"_id": "$symbol", "doc": {"$first": "$$ROOT"}}},
            ]
        )
        return {row["_id"]: row["doc"] for row in cur if row.get("_id") and row.get("doc")}
    except Exception:
        return {}


def compute_return_since_score(
    volume_price_col: Any,
    symbol: str,
    score_date: str,
    latest_by_symbol: Dict[str, Any],
    base_by_key: Dict[Tuple[str, str], Any],
) -> dict:
    """
    Baseline: last trading day's close on or before score_date.
    Latest: most recent close in volume_price for symbol.
    """
    if not symbol or not score_date or len(str(score_date).strip()) < 8:
        return dict(RETURN_SINCE_EMPTY)
    sd = str(score_date).strip()[:8]

    if symbol not in latest_by_symbol:
        latest_by_symbol[symbol] = volume_price_col.find_one(
            {"symbol": symbol},
            sort=[("trade_date", -1)],
        )
    latest_doc = latest_by_symbol[symbol]

    bkey = (symbol, sd)
    if bkey not in base_by_key:
        base_by_key[bkey] = _fetch_base_doc_single(volume_price_col, symbol, sd)
    base_doc = base_by_key[bkey]

    return _return_since_from_docs(latest_doc, base_doc)


def enrich_rows_return_since_score(rows: List[dict], volume_price_col: Any) -> None:
    """批量写涨跌字段：Mongo 往返约 1 + 不同 score_date 种数 + 少量回退查询。"""
    if not rows:
        return
    if not hasattr(volume_price_col, "aggregate"):
        latest_by_symbol: Dict[str, Any] = {}
        base_by_key: Dict[Tuple[str, str], Any] = {}
        for row in rows:
            row.update(
                compute_return_since_score(
                    volume_price_col,
                    row.get("symbol") or "",
                    str(row.get("score_date") or ""),
                    latest_by_symbol,
                    base_by_key,
                )
            )
        return

    symbols: List[str] = []
    seen: Set[str] = set()
    for row in rows:
        sym = row.get("symbol") or ""
        if sym and sym not in seen:
            seen.add(sym)
            symbols.append(sym)
    latest_map = _bulk_latest_by_symbol(volume_price_col, symbols)

    by_sd: Dict[str, List[str]] = {}
    for row in rows:
        sym = row.get("symbol") or ""
        sd = str(row.get("score_date") or "").strip()[:8]
        if len(sd) < 8 or not sym:
            continue
        by_sd.setdefault(sd, []).append(sym)

    base_map: Dict[Tuple[str, str], Any] = {}
    for sd, syms in by_sd.items():
        uniq_syms = list(dict.fromkeys(syms))
        bulk = _bulk_base_by_symbol_string(volume_price_col, uniq_syms, sd)
        for sym in uniq_syms:
            doc = bulk.get(sym)
            if doc and _close_from_volume_price_doc(doc) is not None:
                base_map[(sym, sd)] = doc
        for sym in uniq_syms:
            if (sym, sd) in base_map:
                continue
            base_map[(sym, sd)] = _fetch_base_doc_single(volume_price_col, sym, sd)

    for row in rows:
        sym = row.get("symbol") or ""
        sd = str(row.get("score_date") or "").strip()[:8]
        if not sym or len(sd) < 8:
            row.update(dict(RETURN_SINCE_EMPTY))
            continue
        latest_doc = latest_map.get(sym)
        base_doc = base_map.get((sym, sd))
        row.update(_return_since_from_docs(latest_doc, base_doc))


@router.get("/stock-rankings")
async def get_stock_rankings(
    request: Request,
    limit: int = Query(100, ge=1, le=1000),
    min_score: Optional[float] = Query(None, description="最低分数过滤"),
    exclude_st: bool = Query(True, description="是否排除ST股票"),
    date: Optional[str] = Query(None, description="指定日期 YYYYMMDD,默认为最新日期"),
    strategy: str = Query('balanced', description="指定排行榜策略，balanced/aggressive/conservative")
):
    """
    获取股票评分排行榜 - 每只股票只显示最新一天的评分（简洁实现）
    """
    try:
        db = get_db_collections_from_request(request)
        stock_scores = db['stock_scores']
        stock_info = db['stock_info']

        # Efficient aggregation: $sort -> $group (first doc) -> project -> sort
        if date:
            match_stage = {"$match": {"score_date": date}}
            if min_score is not None:
                match_stage["$match"].update({"composite_score": {"$gte": min_score}})
            pipeline = [
                match_stage,
                {"$project": {
                    "symbol": 1,
                    "composite_score": 1,
                    "cycle_score": 1,
                    "value_score": 1,
                    "fundamental_score": 1,
                    "growth_score": 1,
                    "technical_score": 1,
                    "money_flow_score": 1,
                    "score_date": 1
                }},
                {"$sort": {"composite_score": -1}},
                {"$limit": limit * 2}
            ]
        else:
            # sort by score_date desc, then group by symbol taking first
            pipeline = [
                {"$sort": {"score_date": -1}},
                {"$group": {"_id": "$symbol", "first": {"$first": "$$ROOT"}}},
                {"$replaceRoot": {"newRoot": "$first"}},
                {"$project": {
                    "symbol": 1,
                    "composite_score": 1,
                    "cycle_score": 1,
                    "value_score": 1,
                    "fundamental_score": 1,
                    "growth_score": 1,
                    "technical_score": 1,
                    "money_flow_score": 1,
                    "score_date": 1
                }},
            ]
            if min_score is not None:
                pipeline.append({"$match": {"composite_score": {"$gte": min_score}}})
            pipeline.extend([
                {"$sort": {"composite_score": -1}},
                {"$limit": limit * 2}
            ])

        try:
            all_stocks = list(stock_scores.aggregate(pipeline))
        except Exception:
            # Fallback: simple find with sorting if aggregation fails
            base_query = {"score_date": date} if date else {}
            if min_score is not None:
                base_query["composite_score"] = {"$gte": min_score}
            all_stocks = list(stock_scores.find(base_query).sort("composite_score", -1).limit(limit * 2))

        # Batch attach basic info to avoid N+1 queries
        symbols = [s.get('symbol') for s in all_stocks if s.get('symbol')]
        info_map = {}
        if symbols:
            # Use positional projection arg for broader compatibility with test fakes
            for info in stock_info.find({"symbol": {"$in": symbols}}, {"symbol": 1, "name": 1, "industry": 1}):
                info_map[info.get('symbol')] = info
        for stock in all_stocks:
            info = info_map.get(stock.get('symbol'))
            stock['name'] = info.get('name', '') if info else ''
            stock['industry'] = info.get('industry', '') if info else ''

        # filter ST if requested (simple heuristic: name contains 'ST')
        if exclude_st:
            top_stocks = [s for s in all_stocks if 'ST' not in (s.get('name') or '')]
        else:
            top_stocks = all_stocks

        # use centralized helper to extract strategy-specific score
        top_stocks.sort(key=lambda s: extract_strategy_score(s.get('composite_score', 0), strategy), reverse=True)

        formatted = []
        for stock in top_stocks[:limit]:
            cs = stock.get('composite_score', 0)
            # ensure composite_score is a dict for consistent responses
            if not isinstance(cs, dict):
                cs = {'balanced': cs}

            formatted.append({
                'symbol': stock.get('symbol', ''),
                'name': stock.get('name', ''),
                'industry': stock.get('industry', ''),
                'composite_score': cs,
                'cycle_score': safe_round(stock.get('cycle_score', 0), 2),
                'value_score': safe_round(stock.get('value_score', 0), 2),
                'fundamental_score': safe_round(stock.get('fundamental_score', 0), 2),
                'growth_score': safe_round(stock.get('growth_score', 0), 2),
                'technical_score': safe_round(stock.get('technical_score', 0), 2),
                'money_flow_score': safe_round(stock.get('money_flow_score', 0), 2),
                'score_date': stock.get('score_date', '')
            })

        try:
            enrich_rows_return_since_score(formatted, get_data_db()["volume_price"])
        except Exception as ex:
            print(f"[stock-rankings] enrich return_since_score failed: {ex}")

        return {
            "success": True,
            "data": formatted,
            "total": len(formatted),
            "message": "获取排行榜成功"
        }
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        print(f"获取股票评分失败: {str(e)}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"获取股票评分失败: {str(e)}")

@router.get("/stock-score-detail")
async def get_single_score_detail(
    request: Request,
    symbol: str = Query(..., description="股票代码"),
    category: str = Query(..., description="评分类别: composite|cycle|value|fundamental|growth|technical|money_flow"),
    score_date: Optional[str] = Query(None, description="评分日期 YYYYMMDD, 为空则取最新"),
    strategy: str = Query('balanced', description="策略: balanced|aggressive|conservative 等，与文档中 strategy_scores 对应")
):
    """返回指定股票某一评分类别的详情字段。

    category 到字段映射:
    composite -> 综合得分拆解（含各子项及策略权重）
    cycle -> cycle_details
    value -> value_details
    fundamental -> fundamental_details
    growth -> growth_details
    technical -> technical_details
    money_flow -> money_flow_details
    """
    try:
        cat_map = {
            'cycle': 'cycle_details',
            'value': 'value_details',
            'fundamental': 'fundamental_details',
            'growth': 'growth_details',
            'technical': 'technical_details',
            'money_flow': 'money_flow_details'
        }
        supported_categories = set(list(cat_map.keys()) + ['composite'])
        if category not in supported_categories:
            raise HTTPException(status_code=400, detail=f"不支持的评分类别: {category}")
        db = get_db_collections_from_request(request)
        stock_scores = db['stock_scores']
        query = {"symbol": symbol}
        if score_date:
            query["score_date"] = score_date
            doc = stock_scores.find_one(query)
        else:
            doc = stock_scores.find_one(query, sort=[('score_date', -1)])
        if not doc:
            raise HTTPException(status_code=404, detail="未找到对应评分记录")
        # 处理综合得分拆解
        if category == 'composite':
            composite_score = doc.get('composite_score') or {}
            # 确保 composite_score 统一为 dict
            if not isinstance(composite_score, dict):
                composite_score = {'balanced': composite_score}
            strategy_scores = doc.get('strategy_scores') or {}
            strat_obj = strategy_scores.get(strategy) or strategy_scores.get('balanced') or {}
            weight_map = strat_obj.get('weight') or strat_obj.get('weights') or {}
            # 如果 weight_map 为空，尝试 fallback 通用默认权重（以 balanced 为例）
            if not weight_map:
                weight_map = {'cycle': 0.25, 'growth': 0.25, 'fundamental': 0.35, 'value': 0.35, 'technical': 0.25, 'money_flow': 0.15}
            # 构造子项得分（直接使用文档中各独立字段）
            sub_scores = {
                'cycle': doc.get('cycle_score'),
                'growth': doc.get('growth_score'),
                'fundamental': doc.get('fundamental_score'),
                'value': doc.get('value_score'),
                'technical': doc.get('technical_score'),
                'money_flow': doc.get('money_flow_score')
            }
            # 组装列表展示友好字段
            details_payload = {}
            name_map = {
                'cycle': '周期评分',
                'growth': '成长评分',
                'fundamental': '基本面评分',
                'value': '价值评分',
                'technical': '技术面评分',
                'money_flow': '资金流评分'
            }
            for key, score_val in sub_scores.items():
                if score_val is None:
                    continue
                w = weight_map.get(key)
                w_display = f"{round(w*100, 2)}%" if isinstance(w, (int, float)) else '-' 
                details_payload[f"{name_map.get(key, key)} (权重: {w_display})"] = round(score_val, 2) if isinstance(score_val, (int, float)) else score_val
            details_payload['当前策略'] = strategy
            details_payload['综合得分(当前策略)'] = composite_score.get(strategy)
            return {
                'success': True,
                'data': {
                    'symbol': symbol,
                    'score_date': doc.get('score_date'),
                    'category': category,
                    'details': details_payload
                },
                'message': '获取综合得分拆解成功'
            }
        # 其它分类处理
        detail_field = cat_map[category]
        details = doc.get('details') or {}
        # 兼容旧结构: 直接平铺字段
        if not details and any(k.endswith('_details') for k in doc.keys()):
            details = {k: v for k, v in doc.items() if k.endswith('_details')}
        category_details = details.get(detail_field, doc.get(detail_field, {})) or {}
        return {
            'success': True,
            'data': {
                'symbol': symbol,
                'score_date': doc.get('score_date'),
                'category': category,
                'details': category_details
            },
            'message': '获取评分详情成功'
        }
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        print(f"获取评分详情失败: {str(e)}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"获取评分详情失败: {str(e)}")

@router.get("/score-history/{symbol}")
async def get_stock_score_history(
    request: Request,
    symbol: str,
    days: int = Query(30, ge=1, le=365),
    date: Optional[str] = Query(None, description="截止日期 YYYYMMDD,默认为昨天")
):
    """获取股票评分历史记录（直接查数据库）"""
    try:
        db = get_db_collections_from_request(request)
        stock_scores = db['stock_scores']
        # 构建查询条件
        query = {"symbol": symbol}
        if date:
            score_date_filter = {"$lte": date}
            query["score_date"] = cast(Any, score_date_filter)
        # 查询最近days天的评分，按score_date降序
        cursor = stock_scores.find(query).sort("score_date", -1).limit(days)
        score_history = []
        for score in cursor:
            if '_id' in score:
                score["_id"] = str(score["_id"])
            score_history.append(score)
        # 按日期升序返回
        score_history = sorted(score_history, key=lambda x: x.get("score_date", ""))
        return {
            "success": True,
            "data": score_history,
            "total": len(score_history),
            "message": f"获取 {symbol} 最近{days}天评分历史"
        }
    except Exception as e:
        import traceback
        print(f"获取评分历史失败: {str(e)}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"获取评分历史失败: {str(e)}")

@router.get("/categories")
async def get_score_categories():
    """
    获取评分维度说明
    """
    categories = {
        "cycle_score": {
            "name": "经济周期评分",
            "weight": 0.25,
            "description": "基于月K线走势,评估股票在经济周期中的位置",
            "factors": ["价格相对历史高低点位置", "近期价格趋势", "波动率分析"]
        },
        "fundamental_score": {
            "name": "基本面评分", 
            "weight": 0.35,
            "description": "基于公司基本面数据的评估",
            "factors": ["市值规模", "行业热度", "历史价格稳定性", "财务指标"]
        },
        "technical_score": {
            "name": "技术面评分",
            "weight": 0.25, 
            "description": "基于技术分析指标的评估",
            "factors": ["价格趋势", "成交量趋势", "技术指标(RSI, MACD等)"]
        },
        "money_flow_score": {
            "name": "资金流向评分",
            "weight": 0.15,
            "description": "基于大资金进出情况的评估", 
            "factors": ["大单净买入", "资金流入趋势", "连续净买入天数"]
        }
    }
    
    return {
        "success": True,
        "data": categories,
        "message": "获取评分维度说明成功"
    }

@router.get("/statistics")
async def get_scoring_statistics(
    request: Request,
    date: Optional[str] = Query(None, description="指定日期 YYYYMMDD, 默认为昨天")
):
    """获取评分统计信息（直接查数据库）"""
    try:
        db = get_db_collections_from_request(request)
        stock_scores = db['stock_scores']
        # 构建查询条件
        match_query = {}
        if date:
            match_query["score_date"] = date
        # 统计各分数段的股票数量
        pipeline = [
            {"$match": match_query},
            {
                "$group": {
                    "_id": {
                        "$switch": {
                            "branches": [
                                {"case": {"$gte": ["$composite_score", 80]}, "then": "优秀(80-100)"},
                                {"case": {"$gte": ["$composite_score", 70]}, "then": "良好(70-79)"},
                                {"case": {"$gte": ["$composite_score", 60]}, "then": "一般(60-69)"},
                                {"case": {"$gte": ["$composite_score", 50]}, "then": "较差(50-59)"}
                            ],
                            "default": "很差(0-49)"
                        }
                    },
                    "count": {"$sum": 1},
                    "avg_score": {"$avg": "$composite_score"}
                }
            },
            {"$sort": {"avg_score": -1}}
        ]
        score_distribution = list(stock_scores.aggregate(pipeline))
        # 总体统计
        total_stocks = stock_scores.count_documents(match_query)
        avg_pipeline = [{"$match": match_query}, {"$group": {"_id": None, "avg": {"$avg": "$composite_score"}}}]
        avg_result = list(stock_scores.aggregate(avg_pipeline))
        avg_score = avg_result[0]["avg"] if avg_result else 0
        return {
            "success": True,
            "data": {
                "total_stocks": total_stocks,
                "average_score": round(avg_score, 2),
                "score_distribution": score_distribution,
                "query_date": date if date else "",
                "last_update": datetime.now().isoformat()
            },
            "message": "获取评分统计成功"
        }
    except Exception as e:
        import traceback
        print(f"获取统计信息失败: {str(e)}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"获取统计信息失败: {str(e)}")

@router.get("/current-date")
async def get_current_date():
    """获取当前系统使用的默认日期（昨天）"""
    yesterday = datetime.now() - timedelta(days=1)
    return {
        "success": True,
        "data": {
            "default_date": yesterday.strftime('%Y%m%d'),
            "formatted_date": yesterday.strftime('%Y年%m月%d日'),
            "description": "系统默认使用前一天作为股票评分基准日期"
        },
        "message": "获取默认日期成功"
    }

@router.get("/stock-dates")
async def get_stock_dates(request: Request, symbol: str = Query(..., description="股票代码，例如 000001")):
    """
    返回指定股票历史存在评分的所有日期（降序）
    """
    try:
        db = get_db_collections_from_request(request)
        stock_scores = db['stock_scores']
        # 使用聚合获取该 symbol 的所有 distinct score_date 并按降序排序
        pipeline = [
            {"$match": {"symbol": symbol}},
            {"$group": {"_id": "$score_date"}},
            {"$sort": {"_id": -1}}
        ]
        results = list(stock_scores.aggregate(pipeline))
        dates = [r['_id'] for r in results if r and '_id' in r]
        return {"success": True, "data": dates, "total": len(dates), "message": f"找到 {len(dates)} 个评分日期"}
    except Exception as e:
        import traceback
        print(f"获取股票可用评分日期失败: {str(e)}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"获取股票可用评分日期失败: {str(e)}")

# ✅ 添加请求模型
class SelectedStocksRequest(BaseModel):
    symbols: List[str]
    dates: Optional[List[str]] = None

# ✅ 修复 selected 端点 - 确保每只股票只显示最新评分

@router.post("/stock-rankings/selected")
async def get_selected_stock_rankings(
    request: Request,
    body: SelectedStocksRequest,
    date: Optional[str] = Query(None, description="指定日期 YYYYMMDD,默认为每只股票的最新日期")
):
    """
    获取指定股票的评分 - 每只股票只显示最新一天的评分（直接查数据库）
    """
    try:
        symbols = body.symbols
        if not symbols:
            raise HTTPException(status_code=400, detail="股票代码列表不能为空")
        db = get_db_collections_from_request(request)
        stock_scores = db['stock_scores']
        stock_info = db['stock_info']
        # support optional multi-date per-symbol queries via body.dates
        dates = getattr(body, 'dates', None) or None
        if dates:
            # build a lightweight per-date structure for each requested symbol
            info_cache = {s['symbol']: s for s in stock_info.find({"symbol": {"$in": symbols}})}
            formatted_stocks = []
            try:
                vp_col = get_data_db()["volume_price"]
            except Exception:
                vp_col = None
            for sym in symbols:
                per_date_scores = {}
                per_date_fields = {}
                per_date_return_since: Dict[str, dict] = {}
                latest_by_symbol: Dict[str, Any] = {}
                base_by_key: Dict[Tuple[str, str], Any] = {}
                latest_any = None
                for d in dates:
                    if vp_col is not None:
                        per_date_return_since[d] = compute_return_since_score(
                            vp_col, sym, d, latest_by_symbol, base_by_key
                        )
                    else:
                        per_date_return_since[d] = {
                            "return_since_score_pct": None,
                            "price_base_trade_date": None,
                            "price_latest_trade_date": None,
                        }
                    doc = stock_scores.find_one({"symbol": sym, "score_date": d})
                    if doc:
                        latest_any = latest_any or doc
                        cs = doc.get('composite_score', 0)
                        if not isinstance(cs, dict):
                            cs = {'balanced': cs}
                        per_date_scores[d] = cs
                        per_date_fields[d] = {
                            'cycle_score': safe_round(doc.get('cycle_score', 0), 2),
                            'growth_score': safe_round(doc.get('growth_score', 0), 2),
                            'fundamental_score': safe_round(doc.get('fundamental_score', 0), 2),
                            'value_score': safe_round(doc.get('value_score', 0), 2),
                            'technical_score': safe_round(doc.get('technical_score', 0), 2),
                            'money_flow_score': safe_round(doc.get('money_flow_score', 0), 2)
                        }
                info = info_cache.get(sym) or stock_info.find_one({"symbol": sym})
                formatted_stocks.append({
                    'symbol': sym,
                    'name': info.get('name', '') if info else '',
                    'industry': info.get('industry', '') if info else '',
                    'composite_score': latest_any.get('composite_score', {}) if latest_any else {},
                    'per_date_scores': per_date_scores,
                    'per_date_fields': per_date_fields,
                    'per_date_return_since': per_date_return_since,
                    'score_date': latest_any.get('score_date') if latest_any else ''
                })
            return {
                'success': True,
                'data': formatted_stocks,
                'total': len(formatted_stocks),
                'dates': dates,
                'message': f'成功获取 {len(formatted_stocks)} 只指定股票的多日期评分'
            }

        else:
            if date:
                query = {"symbol": {"$in": symbols}, "score_date": date}
                selected_stocks = list(stock_scores.find(query))
            else:
                pipeline = [
                    {"$match": {"symbol": {"$in": symbols}}},
                    {"$group": {
                        "_id": "$symbol",
                        "latest_date": {"$max": "$score_date"},
                        "docs": {"$push": "$$ROOT"}
                    }},
                    {"$unwind": "$docs"},
                    {"$match": {"$expr": {"$eq": ["$docs.score_date", "$latest_date"]}}},
                    {"$replaceRoot": {"newRoot": "$docs"}},
                    {"$sort": {"composite_score": -1}}
                ]
                selected_stocks = list(stock_scores.aggregate(pipeline))
                if not selected_stocks:
                    base_query = {"symbol": {"$in": symbols}}
                    selected_stocks = list(stock_scores.find(base_query))
        # 添加股票名称和行业信息
        for stock in selected_stocks:
            info = stock_info.find_one({"symbol": stock['symbol']})
            stock['name'] = info.get('name', '') if info else ''
            stock['industry'] = info.get('industry', '') if info else ''
        # 如果没有指定日期，按总分降序排序（兼容 composite_score 为 dict 或数值）
        if not date:
            # default to 'balanced' strategy when sorting selected stocks
            selected_stocks.sort(key=lambda x: extract_strategy_score(x.get('composite_score', 0), 'balanced'), reverse=True)
        # 格式化返回数据
        formatted_stocks = []
        for stock in selected_stocks:
            if '_id' in stock:
                stock['_id'] = str(stock['_id'])
            cs = stock.get('composite_score', 0)
            if not isinstance(cs, dict):
                cs = {'balanced': cs}
            formatted_stock = {
                'symbol': stock.get('symbol', ''),
                'name': stock.get('name', ''),
                'industry': stock.get('industry', ''),
                'composite_score': cs,
                'cycle_score': safe_round(stock.get('cycle_score', 0), 2),
                'value_score': safe_round(stock.get('value_score', 0), 2),
                'fundamental_score': safe_round(stock.get('fundamental_score', 0), 2),
                'growth_score': safe_round(stock.get('growth_score', 0), 2),
                'technical_score': safe_round(stock.get('technical_score', 0), 2),
                'money_flow_score': safe_round(stock.get('money_flow_score', 0), 2),
                'score_date': stock.get('score_date', date if date else '')
            }
            formatted_stocks.append(formatted_stock)
        try:
            enrich_rows_return_since_score(formatted_stocks, get_data_db()["volume_price"])
        except Exception as ex:
            print(f"[stock-rankings/selected] enrich return_since_score failed: {ex}")
        return {
            "success": True,
            "data": formatted_stocks,
            "total": len(formatted_stocks),
            "date": date if date else "",
            "message": f"成功获取 {len(formatted_stocks)} 只指定股票评分 (每只股票显示最新评分)"
        }
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        error_detail = f"获取指定股票评分失败: {str(e)}\n{traceback.format_exc()}"
        print(error_detail)
        raise HTTPException(status_code=500, detail=f"获取指定股票评分失败: {str(e)}")
