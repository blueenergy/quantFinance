"""日内 T+0 止损验证：在 1 分钟 bar 上回放固定止损 / 移动止损。

**只读分析，不写库、不下单。** 设计出处见 llm-wiki
`projects/trading/t0-cross-day-improvement-plan.md` 与本次会话的计划文件。

为什么需要它：SELL_WATCH 的亏损高度集中（最差 10% 的交易贡献 75–82% 的全部亏损），
而先前那次「把亏损截断在 1%」的模拟只看 horizon 末价 —— 等于假设止损必然恰好在
止损价成交，是**乐观上界**。`minute_bars` 有分钟级 high/low，可以真的走一遍路径。

三个贯穿全模块的约定，写错任何一个都会得出反向结论：

1. **方向调整口径**。`directional_pnl_pct()` 把价格空间收益折成「正 = 赚」：
   BUY_WATCH 看涨（`v>0` 赚），SELL_WATCH 看跌（`v<0` 赚）。实测确认过：
   SELL_WATCH 30m 的 `win_rate = 57.4% = frac(v<0)` 而 `mean(net) = +0.359`
   —— **那个 +0.359 是亏 0.359%，不是赚**。用统一正负号之后，本模块里
   **Δ > 0 永远表示「止损有帮助」**，两种风格一致。
2. **按 bar 下标推进，不是墙上时钟**。`minute_bars` 的网格由 vendor 决定
   （实测 20260918 是 241 根：`0930`→`1130` + `1301`→`1500`），午休与首尾都不规则。
   `t0 + N 分钟` 的算术会造出不存在的分钟 —— 那正是评估表里「60m 有 47% 跨到次日」的成因。
3. **成交价不是止损位**。触发时若该 bar 跳空越过止损位，拿的是开盘价（更差）。
   止损腿再叠加滑点；baseline 在 horizon 末退出、不急，故不加。
"""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from db import get_data_db, get_db_collections

# 这两个「私有名」是刻意跨模块 import 的，不要本地复刻：
#   _clustered_mean_ci —— 它对「每簇一个数」是通用工具，不绑定胜率
#   _is_win            —— docstring 明写「判据只留一处，谁都别复刻」
# 今天早些时候手写聚合已因方向算反翻过一次车（SELL 净胜率虚高到 78%）。
from services.intraday_t0_signals import (
    WATCH_SIGNAL_TYPES,
    _clustered_mean_ci,
    net_return_pct,
    to_float,
)

# horizon 标签 → 向前推进的 bar 数。`close` 是「持有到当日最后一根 bar」，不是固定步长。
HORIZON_BARS: Dict[str, Optional[int]] = {
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "60m": 60,
    "close": None,
}
DEFAULT_HORIZONS = ("5m", "15m", "30m", "60m", "close")
STOP_MODES = ("fixed", "trailing")
DEFAULT_STOPS = (0.5, 0.75, 1.0, 1.5, 2.0, 3.0)
DEFAULT_SLIPPAGE_BPS = (0.0, 5.0, 10.0, 20.0)
DEFAULT_STYLES = ("SELL_WATCH", "BUY_WATCH")

# 一根正常的分钟 bar 日应有 241 根。低于此值的 symbol-day 视为降级，
# 只统计不静默过滤 —— 让人能看见样本里混进了多少残缺日。
FULL_DAY_BARS = 200

# 本账户实测往返成本（万1 双边 + 卖方印花税 0.05%，名义金额在最低佣金门槛之上）。
# **它只影响报出的水平值，不影响 Δ** —— 两条腿同一个往返，成本在相减时抵消。
DEFAULT_COST_PCT = 0.07


def directional_pnl_pct(return_pct: Any, signal_type: Any) -> float:
    """价格空间收益 → 「正 = 赚」的方向调整口径。

    SELL_WATCH 先卖后买，跌了才赚，故取负号。这是本模块唯一的符号转换点。
    """
    value = to_float(return_pct) or 0.0
    return value if str(signal_type) == "BUY_WATCH" else -value


def is_short(signal_type: Any) -> bool:
    """SELL_WATCH 先卖后买 = 做空，止损位在**上方**。"""
    return str(signal_type) == "SELL_WATCH"


def normalize_bars(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """`minute_bars` 文档 → 按时间升序、OHLC 齐全的 bar 列表。

    `trade_date` 是 12 位 `YYYYMMDDHHMM` 字符串，只留后 4 位做定位标签。
    缺 OHLC 或非正价格的 bar 丢掉 —— 止损判定依赖 high/low，缺了就不能用。
    """
    bars: List[Dict[str, Any]] = []
    for row in rows:
        td = str(row.get("trade_date") or "")
        if len(td) != 12:
            continue
        o = to_float(row.get("open"))
        h = to_float(row.get("high"))
        low = to_float(row.get("low"))
        c = to_float(row.get("close"))
        if o is None or h is None or low is None or c is None:
            continue
        if min(o, h, low, c) <= 0:
            continue
        bars.append({"label": td[8:], "open": o, "high": h, "low": low, "close": c})
    bars.sort(key=lambda b: b["label"])
    return bars


def locate_entry(bars: Sequence[Dict[str, Any]], snapshot_time: Any) -> Optional[int]:
    """snapshot 时刻「或之前最后一根」bar 的下标；早于当日首根则 None。

    用 bisect 而不是时间算术：vendor 网格不规则（见模块 docstring 第 2 条）。
    """
    if not bars:
        return None
    hhmm = str(snapshot_time or "")[8:12]
    if len(hhmm) != 4 or not hhmm.isdigit():
        return None
    idx = bisect_right([b["label"] for b in bars], hhmm) - 1
    return idx if idx >= 0 else None


def exit_index(
    bars: Sequence[Dict[str, Any]], entry_idx: int, horizon: str
) -> Optional[int]:
    """horizon 对应的退出 bar 下标；当日 bar 不够则 None（该格不可回放）。

    返回 None 而不是夹到最后一根 —— 否则「60m」会被静默算成「20m」。
    """
    if horizon not in HORIZON_BARS:
        raise ValueError(f"未知 horizon: {horizon!r}（可选 {sorted(HORIZON_BARS)}）")
    step = HORIZON_BARS[horizon]
    if step is None:  # `close` 档：持有到当日最后一根 bar
        return len(bars) - 1
    idx = entry_idx + step
    return idx if idx < len(bars) else None


def walk_stop(
    bars: Sequence[Dict[str, Any]],
    entry_idx: int,
    exit_idx: int,
    entry_price: float,
    short: bool,
    stop_pct: float,
    mode: str,
    slippage_bps: float = 0.0,
) -> Dict[str, Any]:
    """从 `entry_idx + 1` 走到 `exit_idx`，返回触发信息。

    `price` 是**成交价**（已含跳空与滑点），不是止损位本身。

    两个刻意的选择：

    - **入场那根 bar 不判定**。同一次 snapshot 内没有可交易的前视空间。
    - **移动止损用「上一根为止」的极值**。第 i 根 bar 判定时用的止损位只依赖
      第 i-1 根收盘前已知的信息 —— 真实挂单正是这样摆着的。若改成「先更新极值
      再判定」，止损位会在同一根 bar 内自我收紧，那是前视。

    `mode="fixed"` 时 peak 恒为入场价，于是 level 恒定 —— 与 trailing 共用一条路径。
    """
    if stop_pct is None or float(stop_pct) <= 0:
        return {"triggered": False, "idx": None, "price": None}

    frac = float(stop_pct) / 100.0
    slip = float(slippage_bps or 0.0) / 10000.0
    peak = float(entry_price)  # 有利方向极值；做空时是「最低价」

    for i in range(entry_idx + 1, exit_idx + 1):
        bar = bars[i]
        if short:
            level = peak * (1.0 + frac)
            if bar["high"] >= level:
                # 跳空高开越过止损位 → 拿开盘价（对做空是更差的价）
                fill = max(level, bar["open"])
                return {"triggered": True, "idx": i, "price": fill * (1.0 + slip)}
        else:
            level = peak * (1.0 - frac)
            if bar["low"] <= level:
                # 跳空低开跌穿止损位 → 拿开盘价（对做多是更差的价）
                fill = min(level, bar["open"])
                return {"triggered": True, "idx": i, "price": fill * (1.0 - slip)}
        if mode == "trailing":
            peak = min(peak, bar["low"]) if short else max(peak, bar["high"])

    return {"triggered": False, "idx": None, "price": None}


def build_case(
    bars: Sequence[Dict[str, Any]],
    entry_idx: int,
    horizon: str,
    signal_type: str,
    cost_pct: float = DEFAULT_COST_PCT,
) -> Optional[Dict[str, Any]]:
    """固定入场与 horizon 退出，返回该 (信号, horizon) 的**不变成分**。

    baseline 与所有止损档共用这一份入场价与退出价 —— 配对比较的前提。
    """
    exit_i = exit_index(bars, entry_idx, horizon)
    if exit_i is None:
        return None
    entry_price = bars[entry_idx]["close"]
    base_exit = bars[exit_i]["close"]
    base_ret = (base_exit - entry_price) / entry_price * 100.0
    return {
        "entry_idx": entry_idx,
        "exit_idx": exit_i,
        "entry_price": entry_price,
        "base_exit_price": base_exit,
        "base_return_pct": base_ret,
        "base_pnl_pct": directional_pnl_pct(base_ret, signal_type),
        "base_net_pnl_pct": _net_pnl(base_ret, signal_type, cost_pct),
    }


def _net_pnl(return_pct: float, signal_type: str, cost_pct: float) -> float:
    """净口径的「正 = 赚」。扣费走 `net_return_pct`（方向感知折成本），再折方向。

    `net_return_pct` 对 BUY 是 `ret − cost`、对 SELL 是 `ret + cost`；再取一次
    方向号之后，两种风格都恰好等于「毛利 − 成本」，可以并排读。
    """
    return directional_pnl_pct(
        net_return_pct(return_pct, cost_pct, signal_type), signal_type
    )


def apply_stop(
    case: Dict[str, Any],
    bars: Sequence[Dict[str, Any]],
    signal_type: str,
    stop_pct: float,
    mode: str = "fixed",
    slippage_bps: float = 0.0,
    cost_pct: float = DEFAULT_COST_PCT,
) -> Dict[str, Any]:
    """在 case 的路径上叠加止损，返回止损腿结果与对基线之差。

    `delta_pct = stop_pnl − base_pnl`，**正 = 止损有帮助**（方向调整口径）。
    成本在 Δ 中恰好相消：两条腿都是同一个往返，`net_return_pct` 的
    BUY `−cost` / SELL `+cost` 在相减时抵消 —— 所以 Δ 是免成本的纯价差效应。
    （滑点**不**抵消：它只作用在止损腿。）
    """
    hit = walk_stop(
        bars,
        case["entry_idx"],
        case["exit_idx"],
        case["entry_price"],
        is_short(signal_type),
        stop_pct,
        mode,
        slippage_bps,
    )
    exit_price = hit["price"] if hit["triggered"] else case["base_exit_price"]
    ret = (exit_price - case["entry_price"]) / case["entry_price"] * 100.0
    pnl = directional_pnl_pct(ret, signal_type)
    return {
        "triggered": bool(hit["triggered"]),
        "trigger_idx": hit["idx"],
        "stop_exit_price": exit_price,
        "stop_return_pct": ret,
        "stop_pnl_pct": pnl,
        "stop_net_pnl_pct": _net_pnl(ret, signal_type, cost_pct),
        "delta_pct": pnl - case["base_pnl_pct"],
    }


# --------------------------------------------------------------------------
# 数据装载与汇总
# --------------------------------------------------------------------------


def load_signals(
    collections: Dict[str, Any],
    start_date: str,
    end_date: str,
    styles: Sequence[str] = DEFAULT_STYLES,
) -> List[Dict[str, Any]]:
    """取窗口内的 WATCH 信号。只取回放需要的字段。"""
    return list(
        collections["intraday_t0_signals"].find(
            {
                "signal_type": {"$in": [s for s in styles if s in WATCH_SIGNAL_TYPES]},
                "trade_date": {"$gte": start_date, "$lte": end_date},
            },
            {
                "snapshot_time": 1,
                "symbol": 1,
                "trade_date": 1,
                "price": 1,
                "signal_type": 1,
            },
        )
    )


def load_day_bars(
    minute_bars: Any,
    symbol: str,
    trade_date: str,
    cache: Dict[Tuple[str, str], List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """取某票某日的全部 1 分钟 bar（带缓存）。

    `minute_bars.trade_date` 是 12 位串，故范围查询是**字符串字典序**比较 ——
    固定宽度下才是安全的。
    """
    key = (symbol, trade_date)
    if key not in cache:
        rows = minute_bars.find(
            {
                "symbol": symbol,
                "trade_date": {"$gte": trade_date + "0000", "$lte": trade_date + "2359"},
            },
            {"trade_date": 1, "open": 1, "high": 1, "low": 1, "close": 1},
        )
        cache[key] = normalize_bars(rows)
    return cache[key]


def bucket_stats(values: Sequence[float]) -> Dict[str, Any]:
    """一组收益的 `n` / 均值 / **最差 ⌊n/10⌋ 条的均值** / 伪影计数。

    为什么要报尾巴而不只报均值：本分析要回答的是「止损能不能治那条肥尾」，
    均值差只说明分布整体位移，**尾巴差才说明肥尾落在哪一侧**。

    尾巴取 ⌊n/10⌋ 且**至少 1 条**：n 小的时候 `tail_n` 会等于 1，那**不是尾巴**，
    就是一条。故 `tail_n` 必须与 `tail_pct` 一起读 —— 只报均值不报 n，
    一条孤例看起来会像「最差 10% 的平均亏损」。

    `implausible_n` 数 |收益| > 10.5% 的行：沪主板单日上限 ±10%，单场内不可达。
    创业板/科创板是 ±20%，故它只是**下界**；非零即证明该组至少有一部分不是真实收益。
    """
    if not values:
        return {"n": 0, "mean_pct": None, "tail_n": 0, "tail_pct": None,
                "implausible_n": 0}
    ordered = sorted(values)
    tail = ordered[: max(1, len(ordered) // 10)]
    return {
        "n": len(ordered),
        "mean_pct": sum(ordered) / len(ordered),
        "tail_n": len(tail),
        "tail_pct": sum(tail) / len(tail),
        "implausible_n": sum(1 for v in ordered if abs(v) > 10.5),
    }


def exclusion_bias(
    styles: Sequence[str] = DEFAULT_STYLES,
    horizons: Sequence[str] = DEFAULT_HORIZONS,
) -> Dict[str, Any]:
    """被排除的信号是不是**系统性地更差**？—— 用评估表的 `return_pct` 做代理。

    回放只能看到有分钟数据的票，若被排除那批的收益分布与可回放那批不同，回放样本就
    不代表总体。故按三种情形分桶，各报 `mean_pct`（方向已折成「正 = 赚」）与
    `tail_pct`（**最差 ⌊n/10⌋ 条**的均值）：

    - `replayable` —— 有分钟数据且 horizon 装得下，即回放样本
    - `no_bars`    —— 该 (票,日) 无分钟数据，**回放真的看不见**，选择偏差问题的本体
    - `overflow`   —— 有分钟数据但入场定位不到 / horizon 装不下（多为尾盘快照）。
      回放**主动拒绝**给这些行打分（`exit_index` 返回 None 而非夹到最后一根），
      它们不属于「看不见」，混进 `no_bars` 会把选择偏差说大。

    **为什么必须分三桶**：两桶版（只分「可回放 / 被排除」）实测会把 `overflow` 里
    的伪影算成选择偏差 —— 那些行是尾盘快照，评估表的名义 horizon 漂到数日后，
    `return_pct` 出现单场不可达的 +27%/+29%，把「被排除那批」的尾巴拉得极肥，
    从而得出「肥尾都在看不见的样本里」这种**方向相反**的结论。

    报尾巴而不只报均值：本分析要回答的是「止损能不能治那条肥尾」，
    均值差说明分布整体位移，**尾巴差才说明肥尾在哪**。两者都报，读的人自己判。

    代理的局限（只用于判**方向与量级**，不当精确值）：评估表的 `base_price` 与真实
    分钟 bar 只有 12.7% 精确吻合、32% 偏差超 0.2%，horizon 标签也是名义的
    （`_snapshot_after` + 15 分钟冷却使 5m/15m 常解析到同一快照）。每桶的
    `implausible_n` 统计 |收益| > 10.5% 的行数，是代理伪影的直接读数。
    本诊断走**评估表自身的窗口**，与 `--start/--end` 无关，`dates` 键给出实际日期。
    """
    collections = get_db_collections()
    minute_bars = get_data_db()["minute_bars"]
    signals = {
        str(r["_id"]): r
        for r in collections["intraday_t0_signals"].find(
            {"signal_type": {"$in": list(styles)}},
            {"snapshot_time": 1, "symbol": 1, "trade_date": 1, "signal_type": 1},
        )
    }
    rows = list(
        collections["intraday_t0_signal_evaluations"].find(
            {
                "signal_type": {"$in": list(styles)},
                "horizon": {"$in": [h for h in horizons if h != "close"]},
            },
            {
                "signal_id": 1,
                "horizon": 1,
                "return_pct": 1,
                "trade_date": 1,
                "symbol": 1,
            },
        )
    )

    bars_cache: Dict[Tuple[str, str], Optional[List[str]]] = {}

    def _labels(symbol: str, trade_date: str) -> Optional[List[str]]:
        key = (symbol, trade_date)
        if key not in bars_cache:
            found = list(
                minute_bars.find(
                    {
                        "symbol": symbol,
                        "trade_date": {
                            "$gte": trade_date + "0000",
                            "$lte": trade_date + "2359",
                        },
                    },
                    {"trade_date": 1},
                )
            )
            labels = sorted(str(b["trade_date"])[8:] for b in found)
            bars_cache[key] = labels or None
        return bars_cache[key]

    split: Dict[Tuple[str, str], Dict[str, List[float]]] = defaultdict(
        lambda: {"replayable": [], "no_bars": [], "overflow": []}
    )
    excluded_symbols: Dict[str, int] = defaultdict(int)
    dates: set = set()

    for row in rows:
        signal = signals.get(str(row.get("signal_id") or ""))
        if not signal or row.get("return_pct") is None:
            continue
        style = str(signal.get("signal_type") or "")
        horizon = str(row.get("horizon") or "")
        if style not in WATCH_SIGNAL_TYPES or horizon not in HORIZON_BARS:
            continue
        symbol = str(row.get("symbol") or "")
        trade_date = str(row.get("trade_date") or "")
        dates.add(trade_date)

        labels = _labels(symbol, trade_date)
        if not labels:
            bucket = "no_bars"
        else:
            entry = locate_entry(
                [{"label": x} for x in labels], signal.get("snapshot_time")
            )
            if entry is None:
                bucket = "overflow"
            elif exit_index([{"label": x} for x in labels], entry, horizon) is None:
                bucket = "overflow"
            else:
                bucket = "replayable"

        # 正 = 赚（方向调整），与其余输出同向
        split[(style, horizon)][bucket].append(
            directional_pnl_pct(row["return_pct"], style)
        )
        if bucket != "replayable":
            excluded_symbols[symbol] += 1

    def _gap(a: Dict[str, Any], b: Dict[str, Any], key: str) -> Optional[float]:
        x, y = a.get(key), b.get(key)
        return None if x is None or y is None else x - y

    cells = []
    for (style, horizon), bucket in sorted(split.items()):
        inside = bucket_stats(bucket["replayable"])
        no_bars = bucket_stats(bucket["no_bars"])
        overflow = bucket_stats(bucket["overflow"])
        cells.append(
            {
                "signal_type": style,
                "horizon": horizon,
                "replayable": inside,
                "no_bars": no_bars,
                "overflow": overflow,
                # 关键披露：回放样本 vs 真·看不见的那批
                "gap_pct": _gap(inside, no_bars, "mean_pct"),
                "tail_gap_pct": _gap(inside, no_bars, "tail_pct"),
            }
        )

    return {
        "window": "评估表自身窗口（仅用于本诊断，与 --start/--end 无关）",
        "dates": sorted(dates),
        "proxy": "intraday_t0_signal_evaluations.return_pct",
        "buckets": {
            "replayable": "有分钟数据且 horizon 装得下 —— 回放样本",
            "no_bars": "该 (票,日) 无分钟数据 —— 回放**真的看不见**，这才是选择偏差问题所在",
            "overflow": "有分钟数据、但入场定位不到或 horizon 装不下（多为尾盘快照）"
            " —— 回放主动拒绝打分，**不属于**「看不见」",
        },
        "cells": cells,
        "excluded_symbols": [
            {"symbol": s, "signals": n}
            for s, n in sorted(excluded_symbols.items(), key=lambda kv: -kv[1])
        ],
    }


def summarize(samples: List[Dict[str, Any]]) -> Dict[str, Any]:
    """逐条样本 → 簇等权汇总 + Δ 的 95% CI。

    先算「簇内一个数」再跨簇等权平均，簇内相关性被这一步平均吸收 ——
    复用 `_clustered_mean_ci`，与验收口径是同一套估计量。
    """
    buckets: Dict[str, Dict[str, List[float]]] = defaultdict(
        lambda: {"base": [], "stop": [], "delta": [], "base_net": [], "stop_net": [], "trig": []}
    )
    for s in samples:
        b = buckets[s["cluster"]]
        b["base"].append(s["base_pnl_pct"])
        b["stop"].append(s["stop_pnl_pct"])
        b["delta"].append(s["delta_pct"])
        b["base_net"].append(s["base_net_pnl_pct"])
        b["stop_net"].append(s["stop_net_pnl_pct"])
        b["trig"].append(1.0 if s["triggered"] else 0.0)

    def _means(field: str) -> List[float]:
        return [sum(b[field]) / len(b[field]) for b in buckets.values() if b[field]]

    def _avg(rates: List[float]) -> Optional[float]:
        return (sum(rates) / len(rates)) if rates else None

    n_all = sum(len(b["delta"]) for b in buckets.values())
    n_trig = sum(1 for b in buckets.values() for t in b["trig"] if t)

    out: Dict[str, Any] = {
        "n": n_all,
        "clusters": len(buckets),
        "trigger_rate": (n_trig / n_all) if n_all else None,
        "base_pnl_pct": _avg(_means("base")),
        "stop_pnl_pct": _avg(_means("stop")),
        "base_net_pnl_pct": _avg(_means("base_net")),
        "stop_net_pnl_pct": _avg(_means("stop_net")),
    }
    out.update({"delta_" + k: v for k, v in _clustered_mean_ci(_means("delta")).items()})
    return out


def run_replay(
    start_date: str,
    end_date: str,
    styles: Sequence[str] = DEFAULT_STYLES,
    horizons: Sequence[str] = DEFAULT_HORIZONS,
    stops: Sequence[float] = DEFAULT_STOPS,
    modes: Sequence[str] = STOP_MODES,
    slippage_bps: Sequence[float] = DEFAULT_SLIPPAGE_BPS,
    min_bars_per_day: int = 0,
    cost_pct: float = DEFAULT_COST_PCT,
) -> Dict[str, Any]:
    """跑一遍回放，返回按 `(style, horizon, mode, stop)` 分行的结果。

    纯读：只 `find`，不写任何集合。
    """
    for h in horizons:
        if h not in HORIZON_BARS:
            raise ValueError(f"未知 horizon: {h!r}（可选 {sorted(HORIZON_BARS)}）")
    for m in modes:
        if m not in STOP_MODES:
            raise ValueError(f"未知止损模式: {m!r}（可选 {list(STOP_MODES)}）")
    slips = sorted({0.0, *[float(s) for s in slippage_bps]})  # 0 档必在，作主表

    collections = get_db_collections()
    minute_bars = get_data_db()["minute_bars"]
    signals = load_signals(collections, start_date, end_date, styles)

    cache: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    excluded: Dict[str, int] = defaultdict(int)
    degraded_days: Dict[Tuple[str, str], int] = {}
    # (style, horizon, mode, stop, slippage) → samples
    cells: Dict[Tuple[str, str, str, float, float], List[Dict[str, Any]]] = defaultdict(list)

    replayed = 0
    for signal in signals:
        style = str(signal.get("signal_type") or "")
        symbol = str(signal.get("symbol") or "")
        trade_date = str(signal.get("trade_date") or "")
        if style not in WATCH_SIGNAL_TYPES or not symbol or not trade_date:
            excluded[symbol or "?"] += 1
            continue

        bars = load_day_bars(minute_bars, symbol, trade_date, cache)
        if len(bars) < max(min_bars_per_day, 1):
            excluded[symbol] += 1
            continue
        if len(bars) < FULL_DAY_BARS:
            degraded_days[(symbol, trade_date)] = len(bars)

        entry_idx = locate_entry(bars, signal.get("snapshot_time"))
        if entry_idx is None:
            excluded[symbol] += 1
            continue
        replayed += 1
        cluster = f"{symbol}|{trade_date}"

        for horizon in horizons:
            case = build_case(bars, entry_idx, horizon, style, cost_pct)
            if case is None:
                continue
            for mode in modes:
                for stop_pct in stops:
                    for slip in slips:
                        res = apply_stop(
                            case, bars, style, stop_pct, mode, slip, cost_pct
                        )
                        cells[(style, horizon, mode, float(stop_pct), slip)].append(
                            {
                                "cluster": cluster,
                                "base_pnl_pct": case["base_pnl_pct"],
                                "stop_pnl_pct": res["stop_pnl_pct"],
                                "delta_pct": res["delta_pct"],
                                "triggered": res["triggered"],
                                "base_net_pnl_pct": case["base_net_pnl_pct"],
                                "stop_net_pnl_pct": res["stop_net_pnl_pct"],
                            }
                        )

    rows: List[Dict[str, Any]] = []
    for (style, horizon, mode, stop_pct, slip), samples in cells.items():
        if slip != 0.0:
            continue  # 主表只出 0 档；滑点另开 delta_by_slippage
        row = {"signal_type": style, "horizon": horizon, "mode": mode, "stop_pct": stop_pct}
        row.update(summarize(samples))
        row["delta_by_slippage"] = {
            str(s): summarize(cells[(style, horizon, mode, stop_pct, s)])["delta_mean"]
            for s in slips
            if (style, horizon, mode, stop_pct, s) in cells
        }
        rows.append(row)

    rows.sort(key=lambda r: (r["signal_type"], r["horizon"], r["mode"], r["stop_pct"]))

    return {
        "success": True,
        "window": {"start": start_date, "end": end_date},
        "signals_total": len(signals),
        "signals_replayed": replayed,
        "symbol_days_loaded": len(cache),
        "excluded_symbols": [
            {"symbol": sym, "signals": cnt}
            for sym, cnt in sorted(excluded.items(), key=lambda kv: -kv[1])
        ],
        "degraded_symbol_days": [
            {"symbol": sym, "trade_date": d, "bars": n}
            for (sym, d), n in sorted(degraded_days.items(), key=lambda kv: kv[1])
        ],
        "horizons": list(horizons),
        "modes": list(modes),
        "stops": [float(s) for s in stops],
        "slippage_bps": slips,
        "cost_pct": cost_pct,
        "selection_bias": exclusion_bias(styles, horizons),
        "cells": rows,
        "notes": [
            "Δ 用**方向调整口径**（正 = 赚），故 **Δ > 0 一律表示止损有帮助**，"
            "BUY/SELL 同向可比。原始价格空间里 SELL 是反的，别直接比。",
            "Δ 是**配对差**：两条腿同入场价、同路径，大盘 beta 与个股当日漂移在相减时抵消。"
            "往返成本也恰好相消，所以 Δ 是免成本的纯价差效应。",
            "水平值 `*_net_pnl_pct` 已扣往返成本（方向感知折费，两种风格都是「毛利 − 成本」），"
            "可与验收表的净口径并排读；`*_pnl_pct`（不带 net）是毛口径。",
            "30 簇门槛当初是为**水平量**（胜率）定的，本处报的是**配对差**，是不同的估计量 —— "
            "故此处只报区间，不下「达标/不达标」。区间宽就是宽。",
            "`trigger_rate` 是一等诊断量：若止损在多数交易里被触发，它就不是「尾部修复」"
            "而是换了个策略，读法完全不同。",
            "成交价含跳空（`max(level, open)` / `min(level, open)`），滑点只作用在止损腿。"
            "本系统**不存在滑点模型**，故滑点档位是敏感性分析，不是标定值。",
            "1 分钟 bar 看不见分钟内路径，无法建模「瞬时插针后反转」；"
            "本系统没有 A 股分钟以下数据，1m 已是下限。",
            "baseline 与止损腿都从 `minute_bars` 现算，**不用** `intraday_t0_signal_evaluations` "
            "的 `base_price` —— 实测它与分钟 bar 精确相等的只有 12.7%，超 0.2% 偏差的有 32%。",
            "⚠️ **样本选择偏差**见 `selection_bias`，按 `replayable / no-bars / overflow` "
            "三桶分开报 —— **不要**把它们并成一个「被排除」：`overflow`（尾盘快照、horizon "
            "装不下）是回放**主动拒绝**打分的行，混进来会把偏差说大。"
            "实测：BUY_WATCH 的 `no-bars`（n=103/档）与回放样本几乎同分布"
            "（Δmean ≤ +0.07pp、Δtail ≤ 0.52pp）→ **无明显选择偏差**；"
            "SELL_WATCH 的 `no-bars` 只有 16–19 行、且其「尾巴」`[n]=1` 是一条孤例"
            "（该收益在四个 horizon 上完全相同 ⇒ 评估表 horizon 漂移的产物，不是真实收益）"
            "→ **两个方向都判不了**。故本回放对 BUY 的样本代表性可接受，"
            "对 SELL 只能说「在可见样本上，止损的配对效应是多少」，"
            "**不能**外推到全体 SELL 信号。",
            "⚠️ `selection_bias` 的 `!` 列非零 = 代理自身有伪影：尾盘快照的名义 30m/60m "
            "在 `_snapshot_after` 下漂到数日后，出现单场不可达的 +27%/+29%。"
            "这正是本回放改用「下标推进、装不下就返回 None」的理由 —— "
            "但伪影只影响该诊断，**不影响**回放主表（主表两条腿都从 `minute_bars` 现算）。",
        ],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
