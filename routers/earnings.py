from typing import Optional, Dict, Any, List
from fastapi import APIRouter, Depends, Query, HTTPException
from auth import get_current_active_user
from db import get_db_collections, get_data_db
from datetime import datetime, timedelta
from utils.stock_utils import serialize_doc
from services.analysis_tasks import build_task_doc
import pandas as pd

router = APIRouter()

@router.get("/hunter/signals")
async def get_earnings_hunter_signals(
    days: int = Query(3, description="查询最近N天的异动信号"),
    current_user: dict = Depends(get_current_active_user) # In a real scenario, use get_premium_user
):
    """
    财报猎手：扫描最近的业绩预告、快报和券商盈利预测，
    寻找可能发生“戴维斯双击”（盈利上修 + 估值扩张）的拐点公司。
    """
    # 模拟 VIP 权限校验
    # if not current_user.get("is_vip"):
    #     raise HTTPException(status_code=403, detail="此功能仅限 VIP 用户访问")

    data_collections = get_data_db()
    forecast_col = data_collections["earnings_forecast"]
    express_col = data_collections["earnings_express"]
    stock_basic_col = data_collections["stock_info"]
    
    # Calculate cutoff date
    cutoff_date = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    
    signals = []
    
    # 1. 扫描业绩预告 (Forecast) - 寻找“扭亏”或“大幅预增”
    forecasts = list(forecast_col.find(
        {"ann_date": {"$gte": cutoff_date}, "type": {"$in": ["预增", "扭亏"]}}
    ).sort("ann_date", -1))
    
    # Process forecasts
    # 同一报告期可能有首次预告 + 修正公告多条，已按 ann_date 倒序，
    # 每 (ts_code, end_date) 只保留最新一条，避免同一只票重复出现。
    seen_periods = set()
    for f in forecasts:
        ts_code = f.get("ts_code")
        period_key = (ts_code, f.get("end_date"))
        if period_key in seen_periods:
            continue
        seen_periods.add(period_key)
        stock = stock_basic_col.find_one({"ts_code": ts_code})
        if not stock: continue
        
        # 估算净利润增速中位数
        p_min = f.get("p_change_min")
        p_max = f.get("p_change_max")
        growth = 0
        if p_min is not None and p_max is not None:
            growth = (p_min + p_max) / 2
        elif p_min is not None:
            growth = p_min
        
        signals.append({
            "id": str(f["_id"]),
            "ts_code": ts_code,
            "name": stock.get("name", ""),
            "industry": stock.get("industry", ""),
            "signal_type": "业绩预告",
            "subtype": f.get("type"),
            "date": f.get("ann_date"),
            "report_period": f.get("end_date"),
            "core_metric": f"净利润增速约 {growth:.1f}%" if growth else "增速未知",
            "reason": f.get("change_reason", ""),
            "ai_analysis": f.get("ai_analysis")
        })
        
    # 2. 扫描业绩快报 (Express) - 寻找实际高增
    # yoy_net_profit from Tushare actually means the absolute value of last year's net profit!
    expresses_raw = list(express_col.find(
        {"ann_date": {"$gte": cutoff_date}}
    ).sort("ann_date", -1))
    
    for e in expresses_raw:
        ts_code = e.get("ts_code")
        # 避免和预告重复（如果同一天发）
        if any(s["ts_code"] == ts_code and s["report_period"] == e.get("end_date") for s in signals):
            continue
            
        n_income = e.get("n_income")
        last_year_n_income = e.get("yoy_net_profit")
        
        yoy_profit_pct = 0.0
        if n_income is not None and last_year_n_income is not None and last_year_n_income != 0:
            yoy_profit_pct = (n_income - last_year_n_income) / abs(last_year_n_income) * 100
        
        # 仅保留增长超过 30% 的公司
        if yoy_profit_pct < 30.0:
            continue
            
        stock = stock_basic_col.find_one({"ts_code": ts_code})
        if not stock: continue
        
        # 业绩快报一般不提供 yoy_sales（营收同比），这里展示绝对营收规模（亿）
        revenue = e.get("revenue")
        revenue_str = f"本期营收 {revenue / 100000000:.1f}亿" if revenue else "营收未知"
        
        signals.append({
            "id": str(e["_id"]),
            "ts_code": ts_code,
            "name": stock.get("name", ""),
            "industry": stock.get("industry", ""),
            "signal_type": "业绩快报",
            "subtype": "实际高增",
            "date": e.get("ann_date"),
            "report_period": e.get("end_date"),
            "core_metric": f"净利润同比 {yoy_profit_pct:.1f}% ({revenue_str})",
            "reason": "", # Express typically lacks reason, needs AI to infer from other data
            "ai_analysis": e.get("ai_analysis")
        })
        
    # 排序：按日期倒序
    signals.sort(key=lambda x: x["date"], reverse=True)
    
    return {
        "success": True,
        "count": len(signals),
        "data": signals
    }

@router.post("/hunter/analyze/{signal_id}")
async def analyze_hunter_signal(
    signal_id: str,
    signal_type: str = Query(..., description="信号类型：业绩预告/业绩快报"),
    current_user: dict = Depends(get_current_active_user)
):
    """
    提交对某个具体财报信号的AI深度挖掘任务。
    由 quantAnalyzer 接手执行具体的 LLM 分析，判断业务主线是否加速，剔除一次性损益，判断估值修复空间。
    """
    from bson import ObjectId
    
    # 获取需要的数据信息并验证
    collections = get_db_collections()
    data_collections = get_data_db()
    
    if signal_type == "业绩预告":
        col = data_collections["earnings_forecast"]
    elif signal_type == "业绩快报":
        col = data_collections["earnings_express"]
    else:
        raise HTTPException(status_code=400, detail="未知的信号类型")
        
    doc = col.find_one({"_id": ObjectId(signal_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="未找到该信号数据")
        
    tasks_col = collections["analysis_tasks"]
    
    # 检查是否已经在分析中
    existing_task = tasks_col.find_one({
        "type": "earnings_hunter",
        "params.signal_id": signal_id,
        "status": {"$in": ["pending", "processing"]}
    })
    
    if existing_task:
        return {
            "success": True,
            "task_id": str(existing_task["_id"]),
            "status": existing_task["status"],
            "message": "任务已经在处理中"
        }
        
    # 如果已经有分析结果而且是近期分析的，前端可以不需要再提。但这里允许复用接口触发重试或新建。
    
    # 创建异步任务给 quantAnalyzer
    task_doc = build_task_doc(
        current_user=current_user,
        task_type="earnings_hunter",
        priority=15,
        symbol=doc.get("ts_code", ""),
        stock_name=doc.get("name", "Unknown"),
        params={
            "signal_id": signal_id,
            "signal_type": signal_type,
            "data": serialize_doc(doc),
        },
        source="earnings_hunter",
    )
    
    result = tasks_col.insert_one(task_doc)
    
    return {
        "success": True,
        "task_id": str(result.inserted_id),
        "status": "pending",
        "message": "AI分析任务已放入队列，猎手正在解析..."
    }
