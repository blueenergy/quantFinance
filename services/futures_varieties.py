"""Futures variety metadata for dashboard APIs."""
from __future__ import annotations

from typing import Any, Dict, List

# Keep in sync with quant-data-engine constants/fut_codes.py (no cross-repo import).
FUT_INDEX: List[Dict[str, Any]] = [
    {"ts_code": "IF.CFX", "name": "沪深300期货", "group": "index", "spot_ts_code": "000300.SH"},
    {"ts_code": "IH.CFX", "name": "上证50期货", "group": "index", "spot_ts_code": "000016.SH"},
    {"ts_code": "IC.CFX", "name": "中证500期货", "group": "index", "spot_ts_code": "000905.SH"},
    {"ts_code": "IM.CFX", "name": "中证1000期货", "group": "index", "spot_ts_code": "000852.SH"},
]

FUT_COMMODITY: List[Dict[str, Any]] = [
    {"ts_code": "AU.SHF", "name": "黄金", "group": "commodity", "spot_ts_code": None},
    {"ts_code": "CU.SHF", "name": "沪铜", "group": "commodity", "spot_ts_code": None},
    {"ts_code": "AL.SHF", "name": "沪铝", "group": "commodity", "spot_ts_code": None},
    {"ts_code": "RB.SHF", "name": "螺纹钢", "group": "commodity", "spot_ts_code": None},
    {"ts_code": "I.DCE", "name": "铁矿石", "group": "commodity", "spot_ts_code": None},
    {"ts_code": "SC.INE", "name": "原油", "group": "commodity", "spot_ts_code": None},
    {"ts_code": "J.DCE", "name": "焦炭", "group": "commodity", "spot_ts_code": None},
    {"ts_code": "JM.DCE", "name": "焦煤", "group": "commodity", "spot_ts_code": None},
    {"ts_code": "M.DCE", "name": "豆粕", "group": "commodity", "spot_ts_code": None},
    {"ts_code": "CF.ZCE", "name": "棉花", "group": "commodity", "spot_ts_code": None},
    {"ts_code": "TA.ZCE", "name": "PTA", "group": "commodity", "spot_ts_code": None},
    {"ts_code": "LC.GFE", "name": "碳酸锂", "group": "commodity", "spot_ts_code": None},
]

FUT_VARIETIES: List[Dict[str, Any]] = FUT_INDEX + FUT_COMMODITY

FUT_WHITELIST_CODES = [v["ts_code"] for v in FUT_VARIETIES]


def list_futures_varieties() -> Dict[str, Any]:
    return {
        "index": FUT_INDEX,
        "commodity": FUT_COMMODITY,
        "all": FUT_VARIETIES,
    }
