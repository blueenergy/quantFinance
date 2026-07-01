from routers.portfolio_plans import (
    VOL_LIVE_BLOCK_ANNUAL,
    VOL_LIVE_BLOCK_ATR_PCT,
    _volatility_buy_blockers,
)


def test_volatility_buy_blockers_extreme_vol():
    blockers = _volatility_buy_blockers({"vol_20d_annual": VOL_LIVE_BLOCK_ANNUAL + 0.05})
    assert "extreme_volatility" in blockers


def test_volatility_buy_blockers_extreme_atr():
    blockers = _volatility_buy_blockers({"atr_pct_20d": VOL_LIVE_BLOCK_ATR_PCT + 0.01})
    assert "extreme_atr_volatility" in blockers


def test_volatility_buy_blockers_abnormal_range():
    blockers = _volatility_buy_blockers({"abnormal_range_flag": True})
    assert "abnormal_intraday_range" in blockers


def test_volatility_buy_blockers_none_when_normal():
    blockers = _volatility_buy_blockers(
        {"vol_20d_annual": 0.35, "atr_pct_20d": 0.05, "abnormal_range_flag": False}
    )
    assert blockers == []
