class StockInfoManager:
    """Manages stock name and info caching/updating."""

    def __init__(self, db_manager):
        self.db = db_manager
        self._stock_name_cache = {}

    def get_stock_name(self, symbol):
        """Fetch stock name from cache, DB, or API."""
        if symbol in self._stock_name_cache:
            return self._stock_name_cache[symbol]
        doc = self.db.stock_info_collection.find_one({"symbol": symbol})
        if doc and "name" in doc:
            self._stock_name_cache[symbol] = doc["name"]
            return doc["name"]
        # TODO: fetch from API if not found
        return None

    def batch_update_stock_names(self, symbols):
        """Batch update stock names from API and store in DB."""
        # TODO: implement API fetch and DB update
        pass