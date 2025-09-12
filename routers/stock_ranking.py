# routers/stock_scoring.py
from fastapi import APIRouter, HTTPException, Depends, Query
from db import get_db_collections
from typing import Optional,List
from pydantic import BaseModel
from datetime import datetime, timedelta
from scores.stock_scoring_system import StockScoringSystem



# 添加tools目录到Python路径
# sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'scores'))

# try:
#     from stock_scoring_system import StockScoringSystem
# except ImportError as e:
#     print(f"Warning: Cannot import StockScoringSystem: {e}")
#     StockScoringSystem = None

router = APIRouter(tags=["股票排名"])

def get_scoring_system(date: Optional[str] = None):
    """依赖注入：获取评分系统实例"""
    if StockScoringSystem is None:
        raise HTTPException(status_code=500, detail="评分系统未正确安装")
    
    # ✅ 修复：动态计算日期而不是硬编码
    if date is None:
        yesterday = datetime.now() - timedelta(days=1)
        date = yesterday.strftime('%Y%m%d')
    
    return StockScoringSystem(base_date=date)

@router.get("/stock-rankings")
async def get_stock_rankings(
    limit: int = Query(100, ge=1, le=1000),
    min_score: Optional[float] = Query(None, description="最低分数过滤"),
    exclude_st: bool = Query(True, description="是否排除ST股票"),
    date: Optional[str] = Query(None, description="指定日期 YYYYMMDD,默认为昨天")
):
    """
    获取股票评分排行榜
    
    Args:
        limit: 返回数量限制,默认100
        min_score: 最低分数过滤，可选
        exclude_st: 是否排除ST股票,默认True
        date: 指定评分日期，格式 YYYYMMDD,默认为昨天
    """
    try:
        scoring_system = get_scoring_system(date)
        
        if exclude_st:
            top_stocks = scoring_system.get_top_stocks(limit=limit, score_date=date)
            
            # 应用最低分数过滤
            if min_score is not None:
                top_stocks = [stock for stock in top_stocks if stock.get('total_score', 0) >= min_score]
                top_stocks = top_stocks[:limit]
        else:
            # 不排除ST股票的逻辑
            query = {}
            if date:
                query["score_date"] = date
            elif scoring_system.base_date:
                query["score_date"] = scoring_system.base_date
            
            all_stocks = list(scoring_system.stock_scores.find(query).sort("total_score", -1).limit(limit * 2))
            
            # 添加股票名称和行业信息
            for stock in all_stocks:
                stock_info = scoring_system.stock_info.find_one({"symbol": stock['symbol']})
                if stock_info:
                    stock['name'] = stock_info.get('name', '')
                    stock['industry'] = stock_info.get('industry', '')
                else:
                    stock['name'] = ''
                    stock['industry'] = ''
            
            # 应用最低分数过滤
            if min_score is not None:
                all_stocks = [stock for stock in all_stocks if stock.get('total_score', 0) >= min_score]
            
            top_stocks = all_stocks[:limit]
        
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
                "total_score": round(stock.get("total_score", 0), 2),
                "cycle_score": round(stock.get("cycle_score", 0), 2),
                "fundamental_score": round(stock.get("fundamental_score", 0), 2),
                "technical_score": round(stock.get("technical_score", 0), 2),
                "money_flow_score": round(stock.get("money_flow_score", 0), 2),
                "score_date": stock.get("score_date", scoring_system.base_date)
            }
            formatted_stocks.append(formatted_stock)
        
        return {
            "success": True,
            "data": formatted_stocks,
            "total": len(formatted_stocks),
            "date": scoring_system.base_date,
            "message": f"成功获取Top {len(formatted_stocks)}股票评分{'(已排除ST股票)' if exclude_st else ''}"
        }
        
    except Exception as e:
        import traceback
        error_detail = f"获取股票排行失败: {str(e)}\n{traceback.format_exc()}"
        print(error_detail)
        raise HTTPException(status_code=500, detail=f"获取股票排行失败: {str(e)}")

# ✅ 修改现有的刷新端点路径
@router.post("/stock-rankings/refresh")  # 原来是 "/refresh"
async def refresh_stock_scores(
    symbols: Optional[list] = None,
    date: Optional[str] = Query(None, description="指定日期 YYYYMMDD,默认为昨天")
):
    """
    重新计算股票评分
    
    Args:
        symbols: 指定股票代码列表，为空则计算所有股票
        date: 指定评分日期，默认为昨天
    """
    try:
        scoring_system = get_scoring_system(date)
        
        if symbols:
            # 只计算指定股票
            results = []
            for symbol in symbols:
                score_data = scoring_system.calculate_stock_score(symbol, base_date=date)
                if score_data:
                    # 更新数据库
                    scoring_system.stock_scores.replace_one(
                        {"symbol": symbol, "score_date": score_data.get("score_date")},
                        score_data,
                        upsert=True
                    )
                    results.append(score_data)
            message = f"成功重新评分 {len(results)} 只指定股票"
        else:
            # 计算所有股票
            results = scoring_system.score_all_stocks(base_date=date)
            message = f"成功重新评分 {len(results)} 只股票"
        
        return {
            "success": True,
            "data": results,
            "total": len(results),
            "date": scoring_system.base_date,
            "message": message
        }
    except Exception as e:
        import traceback
        print(f"重新评分失败: {str(e)}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"重新评分失败: {str(e)}")

@router.get("/score/{symbol}")
async def get_stock_score(
    symbol: str,
    date: Optional[str] = Query(None, description="指定日期 YYYYMMDD,默认为昨天")
):
    """
    获取单个股票的详细评分
    """
    try:
        scoring_system = get_scoring_system(date)
        
        # 构建查询条件
        query = {"symbol": symbol}
        if scoring_system.base_date:
            query["score_date"] = scoring_system.base_date
        
        # 先从数据库查询已有评分
        existing_score = scoring_system.stock_scores.find_one(query)
        
        if existing_score:
            existing_score["_id"] = str(existing_score["_id"])
            return {
                "success": True,
                "data": existing_score,
                "message": "获取历史评分成功"
            }
        
        # 如果没有历史评分，实时计算
        score_data = scoring_system.calculate_stock_score(symbol, base_date=date)
        if score_data:
            # 保存到数据库
            scoring_system.stock_scores.replace_one(
                {"symbol": symbol, "score_date": score_data.get("score_date")},
                score_data,
                upsert=True
            )
            return {
                "success": True,
                "data": score_data,
                "message": "实时计算评分成功"
            }
        else:
            raise HTTPException(status_code=404, detail="股票未找到或数据不足")
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        print(f"获取股票评分失败: {str(e)}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"获取股票评分失败: {str(e)}")

@router.get("/score-history/{symbol}")
async def get_stock_score_history(
    symbol: str,
    days: int = Query(30, ge=1, le=365),
    date: Optional[str] = Query(None, description="截止日期 YYYYMMDD,默认为昨天")
):
    """获取股票评分历史记录"""
    try:
        scoring_system = get_scoring_system(date)
        
        # 使用评分系统的方法获取历史记录
        score_history = scoring_system.get_stock_score_history(symbol, days=days)
        
        # 转换ObjectId
        for score in score_history:
            if '_id' in score:
                score["_id"] = str(score["_id"])
        
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
    date: Optional[str] = Query(None, description="指定日期 YYYYMMDD, 默认为昨天")
):
    """获取评分统计信息"""
    try:
        scoring_system = get_scoring_system(date)
        
        # 构建查询条件
        match_query = {}
        if scoring_system.base_date:
            match_query["score_date"] = scoring_system.base_date
        
        # 统计各分数段的股票数量
        pipeline = [
            {"$match": match_query},
            {
                "$group": {
                    "_id": {
                        "$switch": {
                            "branches": [
                                {"case": {"$gte": ["$total_score", 80]}, "then": "优秀(80-100)"},
                                {"case": {"$gte": ["$total_score", 70]}, "then": "良好(70-79)"},
                                {"case": {"$gte": ["$total_score", 60]}, "then": "一般(60-69)"},
                                {"case": {"$gte": ["$total_score", 50]}, "then": "较差(50-59)"}
                            ],
                            "default": "很差(0-49)"
                        }
                    },
                    "count": {"$sum": 1},
                    "avg_score": {"$avg": "$total_score"}
                }
            },
            {"$sort": {"avg_score": -1}}
        ]
        
        score_distribution = list(scoring_system.stock_scores.aggregate(pipeline))
        
        # 总体统计
        total_stocks = scoring_system.stock_scores.count_documents(match_query)
        avg_pipeline = [{"$match": match_query}, {"$group": {"_id": None, "avg": {"$avg": "$total_score"}}}]
        avg_result = list(scoring_system.stock_scores.aggregate(avg_pipeline))
        avg_score = avg_result[0]["avg"] if avg_result else 0
        
        return {
            "success": True,
            "data": {
                "total_stocks": total_stocks,
                "average_score": round(avg_score, 2),
                "score_distribution": score_distribution,
                "query_date": scoring_system.base_date,
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

# ✅ 修复 selected 端点
@router.post("/stock-rankings/selected")
async def get_selected_stock_rankings(
    request: SelectedStocksRequest,  # ✅ 使用 Pydantic 模型接收请求体
    date: Optional[str] = Query(None, description="指定日期 YYYYMMDD,默认为昨天")
):
    """
    获取指定股票的评分
    
    Args:
        request: 包含股票代码列表的请求体
        date: 指定评分日期，默认为昨天
    """
    try:
        symbols = request.symbols  # ✅ 从请求体中获取 symbols
        print(f"✅ 接收到股票代码: {symbols}") 
        
        if not symbols:
            raise HTTPException(status_code=400, detail="股票代码列表不能为空")
        
        scoring_system = get_scoring_system(date)
        
        # 构建查询条件
        query = {"symbol": {"$in": symbols}}
        if scoring_system.base_date:
            query["score_date"] = scoring_system.base_date
        
        # 查询指定股票的评分
        selected_stocks = list(scoring_system.stock_scores.find(query))
        
        # 添加股票名称和行业信息
        for stock in selected_stocks:
            stock_info = scoring_system.stock_info.find_one({"symbol": stock['symbol']})
            if stock_info:
                stock['name'] = stock_info.get('name', '')
                stock['industry'] = stock_info.get('industry', '')
            else:
                stock['name'] = ''
                stock['industry'] = ''
        
        # 按总分降序排序
        selected_stocks.sort(key=lambda x: x.get('total_score', 0), reverse=True)
        
        # 格式化返回数据
        formatted_stocks = []
        for stock in selected_stocks:
            # 处理 ObjectId
            if '_id' in stock:
                stock['_id'] = str(stock['_id'])
            
            formatted_stock = {
                "symbol": stock.get("symbol", ""),
                "name": stock.get("name", ""),
                "industry": stock.get("industry", ""),
                "total_score": round(stock.get("total_score", 0), 2),
                "cycle_score": round(stock.get("cycle_score", 0), 2),
                "fundamental_score": round(stock.get("fundamental_score", 0), 2),
                "technical_score": round(stock.get("technical_score", 0), 2),
                "money_flow_score": round(stock.get("money_flow_score", 0), 2),
                "score_date": stock.get("score_date", scoring_system.base_date)
            }
            formatted_stocks.append(formatted_stock)
        
        return {
            "success": True,
            "data": formatted_stocks,
            "total": len(formatted_stocks),
            "date": scoring_system.base_date,
            "message": f"成功获取 {len(formatted_stocks)} 只指定股票评分"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        error_detail = f"获取指定股票评分失败: {str(e)}\n{traceback.format_exc()}"
        print(error_detail)
        raise HTTPException(status_code=500, detail=f"获取指定股票评分失败: {str(e)}")