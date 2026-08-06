"""Price-window metric provider (adjusted OHLC)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from stock_data_access.prices import AdjustedPriceDataAccess

from ..metrics import compute_price_window_metrics
from ..schemas import ScreenSpec


def _normalize_date(value: str) -> str:
    return str(value).replace("-", "")[:8]


class PriceMetricProvider:
    provider_id = "price"

    def __init__(
        self,
        data_db=None,
        price_dao: Optional[AdjustedPriceDataAccess] = None,
    ):
        self._dao = price_dao or AdjustedPriceDataAccess(db=data_db)

    def resolve_end_date(self, spec: ScreenSpec, volume_price_col) -> str:
        end = (spec.end or "latest").strip()
        if end.lower() != "latest":
            return _normalize_date(end)
        doc = volume_price_col.find_one(
            {}, sort=[("trade_date", -1)], projection={"trade_date": 1}
        )
        if not doc or not doc.get("trade_date"):
            raise ValueError("无法解析 latest 交易日：volume_price 无数据")
        return _normalize_date(doc["trade_date"])

    def enrich(
        self,
        symbols: List[str],
        spec: ScreenSpec,
        *,
        end_trade_date: str,
    ) -> Dict[str, Dict[str, Any]]:
        if not spec.start:
            raise ValueError("price provider 需要 start 日期 (YYYYMMDD)")

        start = _normalize_date(spec.start)
        end = _normalize_date(end_trade_date)
        if start > end:
            raise ValueError(f"start ({start}) 晚于 end ({end})")

        batch = self._dao.load_adjusted_ohlc_batch(
            symbols, start, end, adjust=spec.adjust or "hfq"
        )

        out: Dict[str, Dict[str, Any]] = {}
        for symbol, df in batch.items():
            metrics = compute_price_window_metrics(symbol, df)
            if metrics is None:
                continue
            if spec.exclude_adj_degraded and metrics.adj_degraded:
                continue
            out[symbol] = metrics.as_dict()
        return out
