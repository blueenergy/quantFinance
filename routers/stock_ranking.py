# routers/stock_scoring.py
from fastapi import APIRouter, HTTPException, Depends, Query, Request
from db import get_db_collections
from utils.score_utils import safe_round, safe_float, extract_strategy_score
from typing import Optional, List, Any, cast
from pydantic import BaseModel
from datetime import datetime, timedelta
from bson import ObjectId

router = APIRouter(tags=["股票排名"])


# using helpers from stock-scoring-system/utils/score_utils.py


def get_db_collections_from_request(request: Request):
    # keep a small shim for compatibility with other routers
    return get_db_collections()


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

        if date:
            query = {"score_date": date}
            all_stocks = list(stock_scores.find(query).sort("composite_score", -1).limit(limit * 2))
        else:
            pipeline = [
                {"$group": {"_id": {"symbol": "$symbol", "date": "$score_date"}, "docs": {"$push": "$$ROOT"}}},
                {"$sort": {"_id.date": -1}},
            ]
            # attempt to get latest per symbol via aggregation
            try:
                pipeline = [
                    {"$group": {"_id": "$symbol", "latest_date": {"$max": "$score_date"}, "docs": {"$push": "$$ROOT"}}},
                    {"$unwind": "$docs"},
                    {"$match": {"$expr": {"$eq": ["$docs.score_date", "$latest_date"]}}},
                    {"$replaceRoot": {"newRoot": "$docs"}},
                    {"$sort": {"composite_score": -1}},
                    {"$limit": limit * 2}
                ]
                all_stocks = list(stock_scores.aggregate(pipeline))
            except Exception:
                all_stocks = list(stock_scores.find({}).sort("composite_score", -1).limit(limit * 2))

        # attach basic info
        for stock in all_stocks:
            info = stock_info.find_one({"symbol": stock.get('symbol')})
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
            for sym in symbols:
                per_date_scores = {}
                per_date_fields = {}
                latest_any = None
                for d in dates:
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
