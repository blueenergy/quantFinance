"""
Symbol utilities for standardizing stock symbol formats.

Standard format: XXXXXX.XX (e.g., 000858.SZ, 600519.SH)
- SZ: Shenzhen Stock Exchange (深交所)
- SH: Shanghai Stock Exchange (上交所)
"""

def normalize_symbol(symbol: str) -> str:
    """
    Normalize symbol to standard format with exchange suffix.
    
    Rules:
    - Already has suffix (.SZ, .SH, or .BJ) -> return as-is (uppercased)
    - Starts with 6 -> Shanghai (.SH)
    - Starts with 0, 2, 3 -> Shenzhen (.SZ)
    - Starts with 4, 8, 9 -> Beijing (.BJ)
    
    Examples:
        "000858" -> "000858.SZ"
        "600519" -> "600519.SH"
        "430047" -> "430047.BJ"
        "000858.SZ" -> "000858.SZ" (unchanged)
    
    Args:
        symbol: Stock symbol (with or without suffix)
    
    Returns:
        Normalized symbol with exchange suffix
    """
    if not symbol:
        return symbol
    
    symbol = symbol.strip()
    
    # Already has suffix
    if '.' in symbol:
        return symbol.upper()
    
    # Add suffix based on first digit
    if symbol.startswith('6'):
        return f"{symbol}.SH"
    elif symbol.startswith(('0', '2', '3')):
        return f"{symbol}.SZ"
    elif symbol.startswith(('4', '8', '9')):
        return f"{symbol}.BJ"
    else:
        # Unknown pattern, return as-is
        return symbol


def strip_exchange_suffix(symbol: str) -> str:
    """
    Remove exchange suffix from symbol.
    
    Examples:
        "000858.SZ" -> "000858"
        "600519.SH" -> "600519"
        "000858" -> "000858" (unchanged)
    
    Args:
        symbol: Stock symbol (with or without suffix)
    
    Returns:
        Symbol without suffix
    """
    if not symbol:
        return symbol
    
    return symbol.split('.')[0]


def get_exchange_suffix(symbol: str) -> str:
    """
    Get exchange suffix from symbol.
    
    Examples:
        "000858.SZ" -> "SZ"
        "600519.SH" -> "SH"
        "000858" -> "" (no suffix)
    
    Args:
        symbol: Stock symbol
    
    Returns:
        Exchange suffix (SZ/SH) or empty string
    """
    if not symbol or '.' not in symbol:
        return ""
    
    parts = symbol.split('.')
    return parts[1].upper() if len(parts) > 1 else ""


def is_shanghai_stock(symbol: str) -> bool:
    """Check if symbol is Shanghai stock."""
    normalized = normalize_symbol(symbol)
    return normalized.endswith('.SH')


def is_shenzhen_stock(symbol: str) -> bool:
    """Check if symbol is Shenzhen stock."""
    normalized = normalize_symbol(symbol)
    return normalized.endswith('.SZ')


def is_beijing_stock(symbol: str) -> bool:
    """Check if symbol is Beijing stock."""
    normalized = normalize_symbol(symbol)
    return normalized.endswith('.BJ')


# Alias for backward compatibility with stock_utils.py
get_ts_code = normalize_symbol
