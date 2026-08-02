"""Pydantic models for backtest batch creation (shared by API, Loop, MCP)."""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


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

    @field_validator("start_date", "end_date")
    @classmethod
    def validate_required_date(cls, v: str) -> str:
        value = str(v or "").strip()
        if not value:
            raise ValueError("start_date/end_date are required")
        return value.replace("-", "")

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
    only_statuses: List[str] = Field(default_factory=list)
