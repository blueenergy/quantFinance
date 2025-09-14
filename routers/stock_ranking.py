# routers/stock_scoring.py
from fastapi import APIRouter, HTTPException, Depends, Query
from db import get_db_collections
from typing import Optional,List
from pydantic import BaseModel
from datetime import datetime, timedelta
from fastapi import APIRouter

router = APIRouter(tags=["股票排名"])


# 添加tools目录到Python路径
# sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'scores'))

# try:
#     from stock_scoring_system import StockScoringSystem
# except ImportError as e:
#     print(f"Warning: Cannot import StockScoringSystem: {e}")
#     StockScoringSystem = None


from fastapi import Request

def get_db_collections_from_request(request: Request):
    # 假设 get_db_collections() 返回 {'stock_scores': ..., 'stock_info': ...}
    return get_db_collections()



@router.get("/stock-rankings")
async def get_stock_rankings(
    request: Request,
    limit: int = Query(100, ge=1, le=1000),
    min_score: Optional[float] = Query(None, description="最低分数过滤"),
    exclude_st: bool = Query(True, description="是否排除ST股票"),
    date: Optional[str] = Query(None, description="指定日期 YYYYMMDD,默认为最新日期")
):
    """
    获取股票评分排行榜 - 每只股票只显示最新一天的评分
    
    Args:
        limit: 返回数量限制,默认100
        min_score: 最低分数过滤，可选
        exclude_st: 是否排除ST股票,默认True
        date: 指定评分日期，格式 YYYYMMDD,默认为最新日期
    """
    try:
        db = get_db_collections_from_request(request)
        stock_scores = db['stock_scores']
        stock_info = db['stock_info']
        
        # ✅ 核心修改：确保每只股票只返回最新一天的评分
        if date:
            # 如果指定了日期，直接按该日期查询
            query = {"score_date": date}
        else:
            # 如果没有指定日期，使用聚合查询获取每只股票的最新评分
            pipeline = [
                # 按股票代码和评分日期分组，取最新日期
                {
                    "$group": {
                        "_id": "$symbol",
                        "latest_date": {"$max": "$score_date"},
                        "docs": {"$push": "$$ROOT"}
                    }
                },
                # 展开文档并只保留最新日期的记录
                {"$unwind": "$docs"},
                {
                    "$match": {
                        "$expr": {"$eq": ["$docs.score_date", "$latest_date"]}
                    }
                },
                # 替换根文档
                {"$replaceRoot": {"newRoot": "$docs"}},
                # 按总分降序排序
                {"$sort": {"composite_score": -1}},
                # 限制返回数量
                {"$limit": limit * 2}  # 多取一些，后面可能要过滤ST股票
            ]
            
            all_stocks = list(stock_scores.aggregate(pipeline))
            
            # 如果聚合查询失败，fallback到原逻辑
            if not all_stocks:
                query = {}
                if date:
                    query["score_date"] = date
                all_stocks = list(stock_scores.find(query).sort("composite_score", -1).limit(limit * 2))
        if exclude_st and not date:
            # 使用聚合查询结果
            top_stocks = all_stocks
        elif exclude_st:
            # 指定日期时，直接查找该日期的评分并过滤ST
            query = {"score_date": date}
            all_stocks = list(stock_scores.find(query).sort("composite_score", -1).limit(limit * 2))
            top_stocks = all_stocks
        else:
            # 不排除ST股票
            if date:
                query = {"score_date": date}
                all_stocks = list(stock_scores.find(query).sort("composite_score", -1).limit(limit * 2))
            # all_stocks 已经在上面定义了
            top_stocks = all_stocks
        # 添加股票名称和行业信息
        for stock in top_stocks:
            info = stock_info.find_one({"symbol": stock['symbol']})
            stock['name'] = info.get('name', '') if info else ''
            stock['industry'] = info.get('industry', '') if info else ''

        # Python再次按composite_score降序排序，确保顺序正确
        top_stocks.sort(key=lambda x: x.get('composite_score', 0), reverse=True)

        # Debug输出，方便排查排序问题
        print("[DEBUG] 排名预览：")
        for s in top_stocks[:10]:
            print(f"symbol={s.get('symbol')}, score_date={s.get('score_date')}, composite_score={s.get('composite_score')}")

        # 排除ST股票（如果启用）
        if exclude_st and not date:
            # 过滤ST股票
            top_stocks = [stock for stock in top_stocks 
                         if not (stock.get('name', '').startswith('ST') or 
                                stock.get('name', '').startswith('*ST'))]

        # 应用最低分数过滤
        if min_score is not None:
            top_stocks = [stock for stock in top_stocks if stock.get('composite_score', 0) >= min_score]

        # 最终限制数量
        top_stocks = top_stocks[:limit]
        
        # 格式化返回数据
        formatted_stocks = []
        for i, stock in enumerate(top_stocks, 1):
            # 处理 ObjectId
            if '_id' in stock:
                stock['_id'] = str(stock['_id'])
            
            formatted_stock = {
                "rank": i,
                "symbol": stock.get("symbol", ""),
                "name": stock.get("name", ""),
                "industry": stock.get("industry", ""),
                "composite_score": round(stock.get("composite_score", 0), 2),
                "cycle_score": round(stock.get("cycle_score", 0), 2),
                "growth_score": round(stock.get("growth_score", 0), 2),
                "fundamental_score": round(stock.get("fundamental_score", 0), 2),
                "value_score": round(stock.get("value_score", 0), 2),
                "technical_score": round(stock.get("technical_score", 0), 2),
                "money_flow_score": round(stock.get("money_flow_score", 0), 2),
                "score_date": stock.get("score_date", date if date else "")
            }
            formatted_stocks.append(formatted_stock)
        
        return {
            "success": True,
            "data": formatted_stocks,
            "total": len(formatted_stocks),
            "date": date if date else "",
            "message": f"成功获取Top {len(formatted_stocks)}股票评分{'(已排除ST股票)' if exclude_st else ''}"
        }
        
    except Exception as e:
        import traceback
        error_detail = f"获取股票排行失败: {str(e)}\n{traceback.format_exc()}"
        print(error_detail)
        raise HTTPException(status_code=500, detail=f"获取股票排行失败: {str(e)}")

@router.get("/score/{symbol}")
async def get_stock_score(
    request: Request,
    symbol: str,
    date: Optional[str] = Query(None, description="指定日期 YYYYMMDD,默认为昨天")
):
    """
    获取单个股票的详细评分（直接查数据库）
    """
    try:
        db = get_db_collections_from_request(request)
        stock_scores = db['stock_scores']
        stock_info = db['stock_info']
        # 构建查询条件
        query = {"symbol": symbol}
        if date:
            query["score_date"] = date
        # 查找评分
        score = stock_scores.find_one(query, sort=[("score_date", -1)])
        if not score:
            raise HTTPException(status_code=404, detail="未找到该股票评分记录")
        # 查找股票信息
        info = stock_info.find_one({"symbol": symbol})
        score["_id"] = str(score["_id"])
        score["name"] = info.get("name", "") if info else ""
        score["industry"] = info.get("industry", "") if info else ""
        return {
            "success": True,
            "data": score,
            "message": "获取历史评分成功"
        }
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        print(f"获取股票评分失败: {str(e)}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"获取股票评分失败: {str(e)}")

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
            query["score_date"] = {"$lte": date}
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

# ✅ 添加请求模型
class SelectedStocksRequest(BaseModel):
    symbols: List[str]

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
        # 如果没有指定日期，按总分降序排序
        if not date:
            selected_stocks.sort(key=lambda x: x.get('composite_score', 0), reverse=True)
        # 格式化返回数据
        formatted_stocks = []
        for stock in selected_stocks:
            if '_id' in stock:
                stock['_id'] = str(stock['_id'])
            formatted_stock = {
                "symbol": stock.get("symbol", ""),
                "name": stock.get("name", ""),
                "industry": stock.get("industry", ""),
                "composite_score": round(stock.get("composite_score", 0), 2),
                "cycle_score": round(stock.get("cycle_score", 0), 2),
                "value_score": round(stock.get("value_score", 0), 2),
                "fundamental_score": round(stock.get("fundamental_score", 0), 2),
                "growth_score": round(stock.get("growth_score", 0), 2),
                "technical_score": round(stock.get("technical_score", 0), 2),
                "money_flow_score": round(stock.get("money_flow_score", 0), 2),
                "score_date": stock.get("score_date", date if date else "")
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