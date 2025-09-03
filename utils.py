
def serialize_doc(doc):
    doc["_id"] = str(doc["_id"])
    return doc

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