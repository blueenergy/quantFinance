#!/usr/bin/env python3
"""日内 T+0 止损验证 CLI —— 在 1 分钟 bar 上回放固定止损 / 移动止损。

**只读**：只 `find`，不写任何集合、不下单。逻辑在
`services/intraday_t0_stop_replay.py`，本文件只是壳。

Usage (from quantFinance repo root):
  python scripts/replay_intrabar_stop.py --start 20260820 --end 20260918
  python scripts/replay_intrabar_stop.py --start 20260820 --end 20260918 \\
      --stops 0.5 1.0 2.0 --modes fixed --horizons 30m close
  python scripts/replay_intrabar_stop.py --start 20260820 --end 20260918 \\
      --json-file /tmp/stop_replay.json

读法：**Δ > 0 = 止损有帮助**（方向调整口径，BUY/SELL 同向）。先看 `trigger%`
——若多数交易都被触发，那它就不是「尾部修复」而是换了个策略。
本工具只报数不判定；区间宽就是宽。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _fmt(value, width: int = 8) -> str:
    if value is None:
        return "-".rjust(width)
    return f"{value:+.{2}f}".rjust(width)


def _print_cells(cells: list) -> None:
    header = (
        f"{'horizon':<8}{'mode':<10}{'stop%':>6}{'n':>6}{'clu':>5}{'trig%':>7}"
        f"{'base_net':>10}{'stop_net':>10}{'Δ':>9}  {'95% CI':<22}{'Δ@5/10/20bp':<22}"
    )
    by_style: dict = {}
    for row in cells:
        by_style.setdefault(row["signal_type"], []).append(row)

    for style in sorted(by_style):
        rows = by_style[style]
        print(f"\n{'=' * len(header)}")
        print(f"{style}   (n={rows[0]['n']} 上限, 簇数随 horizon 变化)")
        print("=" * len(header))
        print(header)
        print("-" * len(header))
        for r in rows:
            ci = (
                f"[{r['delta_ci_low']:+.3f}, {r['delta_ci_high']:+.3f}]".ljust(22)
                if "delta_ci_low" in r
                else "—（簇数 < 2）".ljust(22)
            )
            slip = r.get("delta_by_slippage") or {}
            slip_txt = " ".join(
                f"{_fmt(slip[k], 7)}" for k in ("5.0", "10.0", "20.0") if k in slip
            )
            trig = f"{r['trigger_rate'] * 100:.1f}%" if r["trigger_rate"] is not None else "-"
            print(
                f"{r['horizon']:<8}{r['mode']:<10}{r['stop_pct']:>6.2f}"
                f"{r['n']:>6}{r['clusters']:>5}{trig:>7}"
                f"{_fmt(r['base_net_pnl_pct'], 10)}{_fmt(r['stop_net_pnl_pct'], 10)}"
                f"{_fmt(r['delta_mean'], 9)}  {ci}{slip_txt}"
            )


def _print_bias(bias: dict) -> None:
    cells = bias.get("cells") or []
    if not cells:
        return
    dates = sorted(bias.get("dates") or [])
    span = f"{dates[0]}–{dates[-1]}（{len(dates)} 日）" if dates else "?"
    print(f"\n{'=' * 105}")
    print("⚠️ 样本选择偏差（代理：评估表 return_pct；正 = 赚，方向已折）")
    print(f"   本表窗口 = 评估表自身 {span}，与上方回放窗口无关")
    print("=" * 105)
    print(
        f"{'style':<12}{'h':<6}"
        f"{'─ replayable ─':>29}{'─ no-bars（真看不见）─':>29}"
        f"{'─ overflow（拒打分）─':>29}"
    )
    print(
        f"{'':<18}"
        + (f"{'n':>6}{'mean':>8}{'worst10%':>12}{'!':>3}") * 3
    )

    def _cell(st: dict) -> str:
        if not st["n"]:
            return f"{'-':>29}"
        return (
            f"{st['n']:>6}{st['mean_pct']:>+8.3f}"
            f"{st['tail_pct']:>+9.3f}[{st['tail_n']:>2}]{st['implausible_n']:>3}"
        )

    for c in cells:
        i, nb, ov = c["replayable"], c["no_bars"], c["overflow"]
        print(
            f"{c['signal_type']:<12}{c['horizon']:<6}{_cell(i)}{_cell(nb)}{_cell(ov)}"
        )
        if i["n"] and nb["n"]:
            print(
                f"{'':<18}Δ(回放 − 看不见)   mean {c['gap_pct']:>+7.3f}   "
                f"tail {c['tail_gap_pct']:>+7.3f}"
            )
    print(
        "\n  worst10% = 该组最差 ⌊n/10⌋ 条的均值，[n] 是它的样本数（**1 条不算尾巴，算一条**）；\n"
        "  ! = |收益| > 10.5% 的行数 —— 沪主板单日上限 ±10%，非零即证明该行不是真实收益。\n"
        "  **Δ 只对 no-bars 算**：那才是回放真看不见的样本。overflow 是回放**主动拒绝**\n"
        "  打分的行（多为尾盘快照、horizon 装不下），把它们算进选择偏差会把偏差说大。\n"
        "  Δ < 0 = 看不见的那批更差 → 回放样本偏乐观；Δ ≈ 0 = 无明显选择偏差；\n"
        "  若 no-bars 的 n 很小（十几行）或 [n]=1，则**两个方向都判不了**。"
    )


def main() -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    parser = argparse.ArgumentParser(description="日内 T+0 止损回放（只读）")
    parser.add_argument("--start", required=True, help="起 YYYYMMDD")
    parser.add_argument("--end", required=True, help="止 YYYYMMDD")
    parser.add_argument(
        "--styles", nargs="+", default=["SELL_WATCH", "BUY_WATCH"],
        help="默认 SELL 为主、BUY 作对照",
    )
    parser.add_argument(
        "--horizons", nargs="+", default=["5m", "15m", "30m", "60m", "close"],
    )
    parser.add_argument("--stops", nargs="+", type=float, default=[0.5, 0.75, 1.0, 1.5, 2.0, 3.0])
    parser.add_argument("--modes", nargs="+", default=["fixed", "trailing"])
    parser.add_argument("--slippage-bps", nargs="+", type=float, default=[0.0, 5.0, 10.0, 20.0])
    parser.add_argument("--min-bars-per-day", type=int, default=0)
    parser.add_argument("--cost-pct", type=float, default=0.07,
                        help="往返成本%%，只影响报出的水平值，不影响 Δ")
    parser.add_argument("--json", action="store_true", help="打印完整 JSON 而非表格")
    parser.add_argument("--json-file", help="额外把完整 JSON 落盘")
    args = parser.parse_args()

    from services.intraday_t0_stop_replay import run_replay

    payload = run_replay(
        start_date=args.start,
        end_date=args.end,
        styles=args.styles,
        horizons=args.horizons,
        stops=args.stops,
        modes=args.modes,
        slippage_bps=args.slippage_bps,
        min_bars_per_day=args.min_bars_per_day,
        cost_pct=args.cost_pct,
    )

    if args.json_file:
        Path(args.json_file).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(
        f"T+0 止损回放 {payload['window']['start']}–{payload['window']['end']}  "
        f"信号 {payload['signals_replayed']}/{payload['signals_total']} 可回放  "
        f"装载 {payload['symbol_days_loaded']} 个 symbol-day  "
        f"往返成本 {payload['cost_pct']}%"
    )
    if payload["excluded_symbols"]:
        excl = "  ".join(
            f"{e['symbol']}×{e['signals']}" for e in payload["excluded_symbols"][:8]
        )
        print(f"⚠️ 因缺分钟数据被排除：{excl}")
    if payload["degraded_symbol_days"]:
        worst = payload["degraded_symbol_days"][:5]
        print(
            f"⚠️ 残缺 bar 日 {len(payload['degraded_symbol_days'])} 个"
            f"（最少 {worst[0]['bars']} 根）："
            + "  ".join(f"{w['symbol']}@{w['trade_date']}({w['bars']})" for w in worst)
        )

    _print_cells(payload["cells"])
    _print_bias(payload.get("selection_bias") or {})

    print(f"\n{'=' * 70}\n读法\n{'=' * 70}")
    for note in payload["notes"]:
        print(f"  · {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
