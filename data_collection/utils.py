from datetime import date, timedelta, datetime

def get_sync_dates(latest_date):
    """Calculate start and end dates for sync, considering weekends."""
    today = date.today()
    if today.weekday() == 6:
        today -= timedelta(days=2)
    elif today.weekday() == 5:
        today -= timedelta(days=1)
    if latest_date == today or (isinstance(latest_date, datetime) and latest_date.date() == today):
        return None, None
    start_date = (latest_date + timedelta(days=1)).date() if isinstance(latest_date, datetime) else latest_date + timedelta(days=1)
    return start_date, today