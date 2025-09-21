
from datetime import datetime, date


from bson import ObjectId

def serialize_doc(doc):
    def convert(obj):
        if isinstance(obj, ObjectId):
            return str(obj)
        elif isinstance(obj, dict):
            return {k: convert(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [convert(i) for i in obj]
        else:
            return obj
    return convert(doc)

def get_ts_code(symbol):
    if symbol.startswith('6'):
        tu_symbol = symbol + '.SH'
    elif symbol.startswith('0') or symbol.startswith('3'):
        tu_symbol = symbol + '.SZ'
    elif symbol.startswith('9') or symbol.startswith('8') or symbol.startswith('4'):
        tu_symbol = symbol + '.BJ'
    else:
        raise Exception("Not supported symbols")
    return tu_symbol

def format_trade_date(dt):
    if isinstance(dt, str):
        return dt
    elif isinstance(dt, (datetime, date)):
        return dt.strftime('%Y%m%d')
    return str(dt)