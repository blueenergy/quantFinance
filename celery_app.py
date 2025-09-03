from celery import Celery

celery_app = Celery(
    'tasks',
    broker='redis://localhost:6379/0',
    backend='redis://localhost:6379/0'
)

@celery_app.task
def deep_analysis_task(symbol):
    import time
    time.sleep(10)  # 模拟耗时任务
    return f"{symbol} 深度分析完成"

@celery_app.task
def long_task(symbol):
    # 你的耗时逻辑
    return f"{symbol} 分析完成"