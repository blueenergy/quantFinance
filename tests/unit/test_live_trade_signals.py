from datetime import datetime, timezone

from services.live_trade_signals import default_activate_after


def test_default_activate_after_anchors_pre_open_to_0925_cn_time():
    now = datetime(2026, 6, 19, 1, 0, tzinfo=timezone.utc)  # 09:00 Asia/Shanghai

    activate_after = default_activate_after(now)

    assert activate_after == datetime(2026, 6, 19, 1, 25)


def test_default_activate_after_after_close_uses_next_weekday_0925():
    now = datetime(2026, 6, 19, 8, 0, tzinfo=timezone.utc)  # Friday 16:00 Asia/Shanghai

    activate_after = default_activate_after(now)

    assert activate_after == datetime(2026, 6, 22, 1, 25)
