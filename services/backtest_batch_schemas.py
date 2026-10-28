"""Pydantic models for backtest batch creation (shared by API, Loop, MCP)."""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

# Reference indices available for the Strategy Lab bull/bear gate. Keep this
# list aligned with quant_data.index_prices; the API pre-checks coverage before
# a batch is expanded into hundreds of child tasks.
REGIME_INDEX_BY_UNIVERSE = {
    "csi1000": "000852.SH",
    "csi500": "000905.SH",
    "hs300": "000300.SH",
    "csi2000": "932000.CSI",
    "star50": "000688.SH",
}
REGIME_INDEX_FALLBACK = "000300.SH"
REGIME_INDEX_CHOICES = {
    "000300.SH": "沪深300",
    "000905.SH": "中证500",
    "000852.SH": "中证1000",
    "932000.CSI": "中证2000",
    "000016.SH": "上证50",
    "000001.SH": "上证指数",
    "399006.SZ": "创业板指",
    "000688.SH": "科创50",
}
ALLOWED_REGIME_RULES = {
    "k_slow_anchor",
    "k_slow_anchor_with_volume",
    "k_slow_anchor_with_volatility",
    "k_slow_anchor_with_vol_and_atr",
    "k_fast_price",
    "k_fast_symmetric",
    "k_fast_only",
}
DEFAULT_REGIME_RULE = "k_slow_anchor"


def resolve_regime_index(
    *,
    regime_cash: bool,
    regime_index: Optional[str],
    universe_type: Optional[str] = None,
    universe_value: Optional[str] = None,
) -> Optional[str]:
    """Resolve the reference index for a regime-enabled batch.

    Explicit codes win. ``auto``/blank follows a single index universe, and
    falls back to HS300 for manual / ETF / strategy-pool universes.
    """
    if not regime_cash:
        return None
    explicit = str(regime_index or "").strip().upper()
    if explicit and explicit != "AUTO":
        return explicit
    universe = str(universe_value or "").strip().lower()
    is_index_universe = str(universe_type or "").strip().lower() == "index"
    if is_index_universe and universe == "a500":
        # 000510.SH has no rows in quant_data.index_prices; do not silently
        # substitute HS300 for a user who asked to follow an A500 pool.
        raise ValueError("中证A500 指数行情（000510.SH）缺失，请手动选择参照指数")
    if is_index_universe and universe in REGIME_INDEX_BY_UNIVERSE:
        return REGIME_INDEX_BY_UNIVERSE[universe]
    if is_index_universe and universe.upper() in REGIME_INDEX_CHOICES:
        return universe.upper()
    return REGIME_INDEX_FALLBACK


class BacktestCombo(BaseModel):
    strategy_key: str
    preset: Optional[str] = None
    strategy_params: Dict[str, Any] = Field(default_factory=dict)
    label: Optional[str] = None


class BacktestBatchCreate(BaseModel):
    """Request model for creating a batch backtest experiment."""

    name: Optional[str] = Field(default=None, description="Human-readable experiment name")
    symbols: List[str] = Field(default_factory=list, description="Symbols for manual universe")
    asset_type: str = Field(default="stock", description="Asset type for child tasks: stock or etf")
    universe_type: str = Field(default="manual", description="manual, index, watchlist, or strategy_pool")
    universe_value: Optional[str] = Field(default=None, description="Universe key, e.g. csi1000 or csi2000")
    strategy_key: str = Field(..., description="Strategy key from STRATEGY_MAP (legacy primary combo)")
    preset: Optional[str] = Field(default=None, description="Optional preset name")
    strategy_params: Dict[str, Any] = Field(default_factory=dict)
    start_date: str = Field(..., description="Backtest start date (YYYYMMDD)")
    end_date: str = Field(..., description="Backtest end date (YYYYMMDD)")
    initial_cash: float = Field(default=1000000.0)
    limit_symbols: int = Field(default=0, ge=0, description="Optional cap for testing. 0 means no limit.")
    combos: Optional[List[BacktestCombo]] = None
    experiment_type: Literal["scan", "compare"] = "scan"
    regime_cash: bool = Field(
        default=False,
        description="牛市开仓、非牛空仓的指数择时开关；默认关闭",
    )
    regime_index: Optional[str] = Field(
        default=None,
        description="参照指数代码，如 000300.SH；auto/空表示跟随股票池",
    )
    regime_rule: Optional[str] = Field(
        default=None,
        description="指数状态规则 preset，默认 k_slow_anchor",
    )

    @field_validator("start_date", "end_date")
    @classmethod
    def validate_required_date(cls, v: str) -> str:
        value = str(v or "").strip()
        if not value:
            raise ValueError("start_date/end_date are required")
        return value.replace("-", "")

    @field_validator("regime_index")
    @classmethod
    def validate_regime_index(cls, v: Any) -> Optional[str]:
        text = str(v or "").strip().upper()
        if not text or text == "AUTO":
            return None
        if text not in REGIME_INDEX_CHOICES:
            raise ValueError(
                f"unsupported regime_index: {text}; "
                f"use one of {', '.join(sorted(REGIME_INDEX_CHOICES))}"
            )
        return text

    @field_validator("regime_rule")
    @classmethod
    def validate_regime_rule(cls, v: Any) -> Optional[str]:
        text = str(v or "").strip()
        if not text:
            return None
        if text not in ALLOWED_REGIME_RULES:
            raise ValueError(
                f"unsupported regime_rule: {text}; "
                f"use one of {', '.join(sorted(ALLOWED_REGIME_RULES))}"
            )
        return text

    @model_validator(mode="after")
    def normalize_regime(self) -> "BacktestBatchCreate":
        if not self.regime_cash:
            self.regime_index = None
            self.regime_rule = None
        return self

    @model_validator(mode="after")
    def validate_compare_combos(self) -> "BacktestBatchCreate":
        if self.experiment_type == "compare":
            if not self.combos:
                raise ValueError("experiment_type compare requires non-empty combos")
        if self.combos is not None and self.experiment_type != "compare":
            raise ValueError("combos requires experiment_type compare")
        if self.combos is not None and len(self.combos) == 0:
            raise ValueError("combos cannot be empty")
        return self


class BacktestBatchRerunCreate(BaseModel):
    name: Optional[str] = None
    strategy_key: Optional[str] = None
    preset: Optional[str] = None
    strategy_params: Optional[Dict[str, Any]] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    initial_cash: Optional[float] = None
    regime_cash: Optional[bool] = None
    regime_index: Optional[str] = None
    regime_rule: Optional[str] = None

    @field_validator("regime_index")
    @classmethod
    def validate_rerun_regime_index(cls, v: Any) -> Optional[str]:
        return BacktestBatchCreate.validate_regime_index(v)

    @field_validator("regime_rule")
    @classmethod
    def validate_rerun_regime_rule(cls, v: Any) -> Optional[str]:
        return BacktestBatchCreate.validate_regime_rule(v)
    only_statuses: List[str] = Field(default_factory=list)
