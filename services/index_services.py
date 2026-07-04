# app/services/index_service.py
from typing import List

from db import get_data_db
from models.index_models import IndexConstituent, HS300Constituent
from stock_data_access.index_constituents import load_snapshot


class IndexService:
    """指数数据服务。

    成分股只读 ``quant_data.index_constituents``（由 quant-data-engine 定时任务写入）。
    不再从 Finance 侧调用 Tushare 拉取或回写该集合。
    返回的 ``IndexConstituent.symbol`` 为带交易所后缀的 ts_code（如 ``600519.SH``）。
    """

    def __init__(self):
        self._index_constituents_col = None

    def _index_constituents_collection(self):
        if self._index_constituents_col is None:
            self._index_constituents_col = get_data_db()["index_constituents"]
            try:
                self._index_constituents_col.create_index(
                    [("index_code", 1), ("symbol", 1)]
                )
                self._index_constituents_col.create_index([("update_date", 1)])
            except Exception:
                pass
        return self._index_constituents_col

    @staticmethod
    def _store_key_for_index(index_code: str) -> str:
        """将路由别名或 Tushare 指数代码映射为 Mongo 中的 ``index_code``（与 engine ``store_key`` 一致）。"""
        c = (index_code or "").strip()
        low = c.lower()
        if low in ("hs300", "sse50", "csi500", "star50", "csi1000", "csi2000", "a500"):
            return low
        if low in ("000300.sh", "399300.sz"):
            return "hs300"
        if low == "000016.sh":
            return "sse50"
        if low == "000905.sh":
            return "csi500"
        if low == "000688.sh":
            return "star50"
        if low == "000852.sh":
            return "csi1000"
        if low in ("932000.csi", "932000.sh"):
            return "csi2000"
        # A500 常见代码；亦可通过 engine 的 INDEX_CONSTITUENTS_PROFILES 自定义 store_key
        if low in ("000510.sh", "000510.sz"):
            return "a500"
        return low

    @staticmethod
    def _infer_ts_suffix(bare_digits: str) -> str:
        """为无后缀的 6 位代码推断 Tushare 风格交易所后缀。"""
        c = "".join(ch for ch in bare_digits if ch.isdigit())
        if len(c) < 6:
            return ""
        c6 = c[:6]
        p3, p2 = c6[:3], c6[:2]
        if p3 in ("600", "601", "603", "605", "688", "689"):
            return ".SH"
        if p2 in ("00", "30") or c6.startswith(("001", "002", "003")):
            return ".SZ"
        if p2 in ("43", "83", "87", "88"):
            return ".BJ"
        if c6[0] == "6":
            return ".SH"
        if c6[0] in ("0", "3"):
            return ".SZ"
        return ".SH"

    @staticmethod
    def _ensure_ts_code(symbol: str) -> str:
        """统一为带交易所后缀的 ts_code（如 ``600519.SH``）。库内已有后缀则规范化大小写；无则按代码推断。"""
        s = (symbol or "").strip()
        if not s:
            return s
        u = s.upper()
        for suf in (".SH", ".SZ", ".BJ"):
            if u.endswith(suf):
                base = s[: len(s) - len(suf)].rstrip(".")
                return f"{base}{suf}"
        digits = "".join(ch for ch in s if ch.isdigit())
        suf = IndexService._infer_ts_suffix(digits)
        if suf:
            return f"{digits[:6]}{suf}"
        return s

    def _load_latest_constituents(self, store_key: str) -> List[IndexConstituent]:
        """按 ``index_code`` 取库中最新一批成分（单张快照，非历史并集）。"""
        try:
            coll = self._index_constituents_collection()
            snapshot = load_snapshot(coll, store_key)
            if snapshot is None:
                print(f"本地库未找到 {store_key} 成分股数据")
                return []
            records = list(
                coll.find(
                    {
                        "index_code": store_key,
                        "weight_trade_date": snapshot.weight_trade_date,
                        "update_date": snapshot.update_date,
                    }
                )
            )
            if not records:
                print(f"本地库未找到 {store_key} 成分股数据")
                return []
            out: List[IndexConstituent] = []
            for record in records:
                out.append(
                    IndexConstituent(
                        symbol=self._ensure_ts_code(record.get("symbol", "")),
                        name=record.get("name", ""),
                        industry=record.get("industry", ""),
                        market_cap=float(record.get("market_cap") or 0),
                        weight=float(record.get("weight") or 0),
                        update_time=record.get("update_time", ""),
                    )
                )
            print(
                f"从本地库读取 {len(out)} 条 {store_key} 成分股，"
                f"快照日期: {snapshot.update_date}"
            )
            return out
        except Exception as e:
            print(f"读取指数成分失败 ({store_key}): {e}")
            return []

    async def get_index_constituents(self, index_code: str) -> List[IndexConstituent]:
        """获取指数成分股（仅 Mongo）。"""
        store_key = self._store_key_for_index(index_code)
        rows = self._load_latest_constituents(store_key)
        if rows:
            return rows

        simple_code = self._alias_simple_code(index_code)
        upper_code = index_code.upper()
        fallback_map = {
            "399300.SZ": [
                {
                    "symbol": "000001.SZ",
                    "name": "平安银行",
                    "industry": "银行",
                    "market_cap": 280000000000,
                    "weight": 0.85,
                },
                {
                    "symbol": "000002.SZ",
                    "name": "万科A",
                    "industry": "房地产开发",
                    "market_cap": 250000000000,
                    "weight": 0.78,
                },
                {
                    "symbol": "600519.SH",
                    "name": "贵州茅台",
                    "industry": "食品饮料",
                    "market_cap": 2200000000000,
                    "weight": 6.89,
                },
            ],
            "hs300": [
                {
                    "symbol": "000001.SZ",
                    "name": "平安银行",
                    "industry": "银行",
                    "market_cap": 280000000000,
                    "weight": 0.85,
                },
                {
                    "symbol": "000002.SZ",
                    "name": "万科A",
                    "industry": "房地产开发",
                    "market_cap": 250000000000,
                    "weight": 0.78,
                },
                {
                    "symbol": "600519.SH",
                    "name": "贵州茅台",
                    "industry": "食品饮料",
                    "market_cap": 2200000000000,
                    "weight": 6.89,
                },
            ],
            "STAR50": [
                {
                    "symbol": "688001.SH",
                    "name": "华兴源创",
                    "industry": "半导体",
                    "market_cap": 45000000000,
                    "weight": 1.10,
                },
                {
                    "symbol": "688012.SH",
                    "name": "中微公司",
                    "industry": "半导体设备",
                    "market_cap": 120000000000,
                    "weight": 2.20,
                },
            ],
            "star50": [
                {
                    "symbol": "688001.SH",
                    "name": "华兴源创",
                    "industry": "半导体",
                    "market_cap": 45000000000,
                    "weight": 1.10,
                },
                {
                    "symbol": "688012.SH",
                    "name": "中微公司",
                    "industry": "半导体设备",
                    "market_cap": 120000000000,
                    "weight": 2.20,
                },
            ],
            "A500": [
                {
                    "symbol": "600519.SH",
                    "name": "贵州茅台",
                    "industry": "白酒",
                    "market_cap": 2500000000000,
                    "weight": 2.50,
                },
                {
                    "symbol": "000333.SZ",
                    "name": "美的集团",
                    "industry": "家电",
                    "market_cap": 450000000000,
                    "weight": 1.20,
                },
            ],
            "a500": [
                {
                    "symbol": "600519.SH",
                    "name": "贵州茅台",
                    "industry": "白酒",
                    "market_cap": 2500000000000,
                    "weight": 2.50,
                },
                {
                    "symbol": "000333.SZ",
                    "name": "美的集团",
                    "industry": "家电",
                    "market_cap": 450000000000,
                    "weight": 1.20,
                },
            ],
            "csi500": [
                {
                    "symbol": "600519.SH",
                    "name": "贵州茅台",
                    "industry": "白酒",
                    "market_cap": 2500000000000,
                    "weight": 2.50,
                },
                {
                    "symbol": "000333.SZ",
                    "name": "美的集团",
                    "industry": "家电",
                    "market_cap": 450000000000,
                    "weight": 1.20,
                },
            ],
        }
        data = (
            fallback_map.get(index_code)
            or fallback_map.get(simple_code)
            or fallback_map.get(upper_code)
            or fallback_map.get(simple_code.upper())
            or []
        )
        return [IndexConstituent(**item) for item in data]

    async def get_hs300_constituents(self) -> List[HS300Constituent]:
        data = await self.get_index_constituents("hs300")
        return [HS300Constituent(**c.dict()) for c in data]

    def _alias_simple_code(self, code: str) -> str:
        code_lower = code.lower()
        if code_lower in ("000300.sh", "399300.sz", "hs300"):
            return "hs300"
        if code_lower in ("000688.sh", "star50"):
            return "star50"
        if code_lower in ("000510.sh", "a500"):
            return "a500"
        if code_lower in ("000905.sh", "csi500"):
            return "csi500"
        if code_lower in ("932000.csi", "932000.sh", "csi2000"):
            return "csi2000"
        return code
