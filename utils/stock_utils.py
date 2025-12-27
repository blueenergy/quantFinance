
from datetime import datetime, date
from bson import ObjectId
from .symbol_utils import normalize_symbol

def serialize_doc(doc):
    def convert(obj):
        if isinstance(obj, ObjectId):
            return str(obj)
        elif isinstance(obj, (datetime, date)):
            return obj.isoformat()
        elif isinstance(obj, dict):
            return {k: convert(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [convert(i) for i in obj]
        else:
            return obj
    return convert(doc)

def get_ts_code(symbol):
    """
    Convert symbol to tushare/standard format with exchange suffix.
    
    Deprecated: Use normalize_symbol() from symbol_utils instead.
    This function is kept for backward compatibility.
    
    Examples:
        "000858" -> "000858.SZ"
        "600519" -> "600519.SH"
        "430047" -> "430047.BJ"
    """
    return normalize_symbol(symbol)

def format_trade_date(dt):
    if isinstance(dt, str):
        return dt
    elif isinstance(dt, (datetime, date)):
        return dt.strftime('%Y%m%d')
    return str(dt)