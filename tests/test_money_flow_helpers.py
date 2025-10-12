import types
import pandas as pd
import pytest

from quantFinance.tools import money_flow_collector as mfc


class DummyStateCollection:
    def __init__(self, doc=None):
        self._doc = doc
        self.updated = None

    def find_one(self, query):
        return self._doc

    def update_one(self, flt, upd, upsert=False):
        self.updated = (flt, upd, upsert)


class DummyPro:
    def __init__(self, df=None, raise_exc=False):
        self._df = df
        self._raise = raise_exc

    def trade_cal(self, start_date, end_date):
        if self._raise:
            raise RuntimeError("calendar fetch failed")
        return self._df


def make_cal_df(rows):
    """rows: list of tuples (cal_date, is_open)"""
    return pd.DataFrame(rows, columns=["cal_date", "is_open"])


def test_resolve_date_range_with_explicit_args():
    state = DummyStateCollection(doc={'last_date': '20250105'})
    pro = DummyPro(df=make_cal_df([('20250110', 1), ('20250109', 1)]))
    start, end = mfc.resolve_date_range('20250101', '20250110', state, pro)
    assert start == '20250101'
    assert end == '20250110'


def test_resolve_date_range_resume_from_state():
    # No start/end provided; state has last_date
    state = DummyStateCollection(doc={'last_date': '20250107'})
    pro = DummyPro(df=make_cal_df([('20250110', 1), ('20250109', 1), ('20250108', 0)]))
    start, end = mfc.resolve_date_range(None, None, state, pro)
    # end should pick first open day in df (20250110), start should be last_date + 1
    assert end == '20250110'
    assert start == '20250108'


def test_resolve_date_range_no_state_defaults():
    state = DummyStateCollection(doc=None)
    pro = DummyPro(df=make_cal_df([('20250110', 1), ('20250109', 1)]))
    start, end = mfc.resolve_date_range(None, None, state, pro)
    assert end == '20250110'
    assert start == '20250109'  # end - 1 day


def test_resolve_date_range_calendar_failure(monkeypatch):
    # Force calendar failure, control 'today'
    class FixedDateTime:
        @staticmethod
        def today():
            return mfc.datetime(2025, 1, 15)  # mimic datetime.date object via datetime replacement below

        @staticmethod
        def strptime(s, fmt):
            return mfc.datetime.strptime(s, fmt)

    # Monkeypatch datetime.today used inside resolve_date_range via module attribute
    # Simpler: patch datetime in module by injecting a proxy that has today() method
    # We'll patch only today() by assigning a lambda
    original_datetime = mfc.datetime
    class ProxyDateTime(original_datetime):
        @classmethod
        def today(cls):
            return original_datetime(2025, 1, 15)
    monkeypatch.setattr(mfc, 'datetime', ProxyDateTime)

    state = DummyStateCollection()
    pro = DummyPro(raise_exc=True)
    start, end = mfc.resolve_date_range(None, None, state, pro)
    assert end == '20250115'
    assert start == '20250114'

    # restore handled by monkeypatch teardown


def test_get_trade_days_success():
    pro = DummyPro(df=make_cal_df([('20250110', 1), ('20250109', 0), ('20250108', 1)]))
    days = mfc.get_trade_days('20250108', '20250110', pro)
    assert days == ['20250110', '20250108']  # preserves order from DataFrame rows filtered by is_open


def test_get_trade_days_failure():
    pro = DummyPro(raise_exc=True)
    days = mfc.get_trade_days('20250108', '20250110', pro)
    assert days == ['20250108', '20250109', '20250110']
