from celery_app import generate_market_analysis_task

# This runs synchronously in-process, no Redis, no worker
result = generate_market_analysis_task()
print(result)
