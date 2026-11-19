"""Guard the POST /trader/signals/{order_id}/status allowlist.

quantTrader writes extra diagnostics on top of the status (e.g. the bid1/ask1
snapshot taken at submission). The endpoint filters unknown keys silently, so a
missing allowlist entry means the field disappears without any error — this test
pins the ones the trader relies on.
"""

from routers import trader as trader_mod

REQUIRED_FIELDS = {
    "status",
    "qmt_order_id",
    "effective_limit_price",
    "last_market_price",
    "book_snapshot",
}


def test_trader_signal_status_allowlist_covers_quant_trader_fields():
    missing = REQUIRED_FIELDS - trader_mod._TRADER_SIGNAL_STATUS_ALLOWED_FIELDS
    assert not missing, f"allowlist would silently drop: {sorted(missing)}"
