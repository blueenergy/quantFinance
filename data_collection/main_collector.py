from db_manager import DatabaseManager
from stock_info import StockInfoManager
from api_client import StockApiClient
from data_sync import DataSyncManager

class VolumePriceCollector:
    def __init__(self):
        self.db = DatabaseManager()
        self.stock_info = StockInfoManager(self.db)
        self.api = StockApiClient()
        self.sync = DataSyncManager(self.db, self.api, self.stock_info)

    # Add orchestration logic here (CLI, batch update, etc.)

    def run_batch_update(self):
        self.sync.batch_update()

if __name__ == "__main__":
    # Example usage or CLI parsing
    collector = VolumePriceCollector()
    # collector.sync.batch_update(...)
    collector.run_batch_update()
    collector.db.close()