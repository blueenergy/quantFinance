import pandas as pd
from utils import get_sync_dates

class DataSyncManager:
    """Handles incremental and full data synchronization."""

    def __init__(self, db_manager, api_client, stock_info_manager):
        self.db = db_manager
        self.api = api_client
        self.stock_info = stock_info_manager

    def get_incremental_data(self, symbol, days_back=1):
        """Fetch and process incremental data for a symbol."""
        latest_date = self.db.find_latest_trade_date(symbol)
        start_date, today = get_sync_dates(latest_date)
        if start_date is None:
            print(f"{symbol} is up to date.")
            return pd.DataFrame()
        beg = start_date.strftime('%Y-%m-%d')
        end = today.strftime('%Y-%m-%d')
        df = self.api.get_quote_history(symbol, beg=beg, end=end)
        # TODO: process df, filter, rename columns, add metadata
        return df

    def batch_update(self, force_update=False, update_existing=False, incremental_days=None):
        """Batch update all stocks."""
        symbols = self.api.get_all_stock_codes()
        for symbol in symbols:
            df = self.get_incremental_data(symbol, days_back=incremental_days or 1)
            # TODO: insert/update records in DB
            pass

    def incremental_sync_all(self, days=None, per_stock_calculation=True):
        """Incrementally sync all stocks."""
        # TODO: implement per-stock missing days logic and call get_incremental_data
        pass