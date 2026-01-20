#!/bin/bash
# 脚本用途：量化任务每日执行脚本
# 维护人：shuyolin
# 执行时间：周一到周五，按不同时段执行对应任务

# 定义通用变量（方便后续修改路径）
LOG_DIR="/home/shuyolin"
DATE=$(date +%Y%m%d)  # 日期格式，无需转义（脚本内执行不用\%）

# ==================== 任务1：17:30 批量日线采集 ====================
run_bulk_daily_collector() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] 开始执行批量日线采集任务..."
    # 激活对应虚拟环境并执行脚本
    source /home/shuyolin/quantFinance/.venv/bin/activate && \
    python /home/shuyolin/quantFinance/tools/bulk_daily_collector.py >> ${LOG_DIR}/volumeprice_${DATE}.log 2>&1
    # 检查任务是否执行成功
    if [ $? -eq 0 ]; then
        echo "[$(date +'%Y-%m-%d %H:%M:%S')] 批量日线采集任务执行成功！"
    else
        echo "[$(date +'%Y-%m-%d %H:%M:%S')] 批量日线采集任务执行失败！查看日志：${LOG_DIR}/volumeprice_${DATE}.log"
    fi
    deactivate  # 退出虚拟环境
}

# ==================== 任务2：18:00 日线基础数据同步 ====================
run_bulk_daily_basic_sync() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] 开始执行日线基础数据同步任务..."
    source /home/shuyolin/quantFinance/.venv/bin/activate && \
    python /home/shuyolin/quantFinance/tools/bulk_daily_basic_sync.py >> ${LOG_DIR}/daily_basic_${DATE}.log 2>&1
    if [ $? -eq 0 ]; then
        echo "[$(date +'%Y-%m-%d %H:%M:%S')] 日线基础数据同步任务执行成功！"
    else
        echo "[$(date +'%Y-%m-%d %H:%M:%S')] 日线基础数据同步任务执行失败！查看日志：${LOG_DIR}/daily_basic_${DATE}.log"
    fi
    deactivate
}

# ==================== 任务3：18:00 资金流采集 ====================
run_money_flow_collector() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] 开始执行资金流采集任务..."
    source /home/shuyolin/quantFinance/.venv/bin/activate && \
    python /home/shuyolin/quantFinance/tools/money_flow_collector.py >> ${LOG_DIR}/money_flow_${DATE}.log 2>&1
    if [ $? -eq 0 ]; then
        echo "[$(date +'%Y-%m-%d %H:%M:%S')] 资金流采集任务执行成功！"
    else
        echo "[$(date +'%Y-%m-%d %H:%M:%S')] 资金流采集任务执行失败！查看日志：${LOG_DIR}/money_flow_${DATE}.log"
    fi
    deactivate
}

# ==================== 任务4：18:30 股票评分 ====================
run_stock_score() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] 开始执行股票评分任务..."
    source /home/shuyolin/stock-scoring-system/.venv/bin/activate && \
    python /home/shuyolin/stock-scoring-system/score_util.py -a all --max-workers 4 >> ${LOG_DIR}/score_${DATE}.log 2>&1
    if [ $? -eq 0 ]; then
        echo "[$(date +'%Y-%m-%d %H:%M:%S')] 股票评分任务执行成功！"
    else
        echo "[$(date +'%Y-%m-%d %H:%M:%S')] 股票评分任务执行失败！查看日志：${LOG_DIR}/score_${DATE}.log"
    fi
    deactivate
}

# ==================== 任务5：19:00 市场情绪分析 ====================
run_market_sentiment() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] 开始执行市场情绪分析任务..."
    source /home/shuyolin/stock-scoring-system/.venv/bin/activate && \
    python /home/shuyolin/stock-scoring-system/tools/analyze_market_sentiment.py
    if [ $? -eq 0 ]; then
        echo "[$(date +'%Y-%m-%d %H:%M:%S')] 市场情绪分析任务执行成功！"
    else
        echo "[$(date +'%Y-%m-%d %H:%M:%S')] 市场情绪分析任务执行失败！"
    fi
    deactivate
}

# ==================== 任务调用入口（按参数执行对应任务） ====================
case "$1" in
    collector)
        run_bulk_daily_collector
        ;;
    basic)
        run_bulk_daily_basic_sync
        run_money_flow_collector  # 18:00两个任务一起执行
        ;;
    score)
        run_stock_score
        ;;
    sentiment)
        run_market_sentiment
        ;;
    all)
        # 一次性执行所有任务（测试用）
        run_bulk_daily_collector
        sleep 30  # 间隔30秒，避免资源冲突
        run_bulk_daily_basic_sync
        run_money_flow_collector
        sleep 30
        run_stock_score
        sleep 30
        run_market_sentiment
        ;;
    *)
        echo "用法：$0 {collector|basic|score|sentiment|all}"
        echo "  collector - 执行17:30日线采集任务"
        echo "  basic     - 执行18:00基础数据+资金流任务"
        echo "  score     - 执行18:30股票评分任务"
        echo "  sentiment - 执行19:00市场情绪分析任务"
        echo "  all       - 执行所有任务（测试用）"
        exit 1
        ;;
esac

exit 0