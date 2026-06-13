from stock_data_access.indicators.nineturn import enrich_nineturn_signals, latest_signal_summary


def _bars(count=60):
    rows = []
    for idx in range(count):
        day = idx + 1
        close = 100 + idx
        rows.append(
            {
                "trade_date": f"202604{day:02d}",
                "open": close - 1,
                "high": close + 2,
                "low": close - 2,
                "close": close,
                "vol": 1000,
            }
        )
    rows[-4]["low"] = 150
    rows[-3]["low"] = 151
    rows[-2]["low"] = 149
    rows[-1]["low"] = 148
    rows[-1]["vol"] = 400
    return rows


def test_enrich_nineturn_signals_marks_strong_perfect_down_signal():
    bars = _bars()
    counts = [{"trade_date": bars[-1]["trade_date"], "down_count": 9, "nine_down_turn": "-9"}]

    signals = enrich_nineturn_signals(bars, counts)

    assert len(signals) == 1
    signal = signals[0]
    assert signal["direction"] == "down"
    assert signal["perfect"] is True
    assert signal["vol_filter_pass"] is True
    assert signal["trend_filter_pass"] is True
    assert signal["grade"] == "strong"
    assert signal["strength"] == 4
    assert latest_signal_summary(signals)["summary"] == "下九转·强"
