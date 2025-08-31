import efinance as ef
import akshare as ak

class StockApiClient:
    """Wraps external API calls for stock data."""

    def get_base_info(self, symbol):
        """Get base info for a stock."""
        # Example: return ef.stock.get_base_info(symbol)
        pass

    def get_quote_history(self, symbol, beg=None, end=None):
        """Get historical quotes for a stock."""
        # Example: return ef.stock.get_quote_history(symbol, beg=beg, end=end)
        pass

    def get_all_stock_codes(self):
        """Get all stock codes."""
        # Example: return ef.stock.get_all_stock_codes()
        pass