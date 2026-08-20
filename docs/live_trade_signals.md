# Live Trade Signals Contract

This document defines the minimum executable `trade_signals` contract used by:

```text
approved portfolio plan -> trade_signals -> quantTrader -> miniQMT -> execution/account sync
```

## Sources of truth

Every state field has exactly one authority. Publish-time risk checks and sell
sizing must read only from the source below — no implicit third source (see
llm-wiki `projects/trading/execution-chain-roadmap.md` §权威来源). This closes the "three sets of
numbers" gap between UI, lineage, and the broker (risks R4/R5).

| Concern | Authoritative source | Used for |
|---------|----------------------|----------|
| Fill quantity / price | Broker deal reports (`trade_executions`) | Terminal state, fees/taxes |
| Sellable shares (T+1) | Broker `available_qty` | Hard cap on sell size |
| In-plan delta / sell sizing | Lineage snapshot (`LIVE_SELL_SIZING_BASIS=lineage`) | Multi-plan shared-account sizing |
| Plan capital budget | Plan `capital_basis` | Buy-side soft cap |
| UI overview totals | `portfolio_live_snapshots` | Read-only materialized view, **not** a decision input |

Notes:

- `portfolio_live_snapshots` is a read-time materialization for the dashboard;
  it is rebuilt asynchronously (single lease-guarded path, see
  `services/portfolio_live_snapshot.py::schedule_snapshot_rebuild`) and must
  never be used as a sizing or risk input.
- Sell sizing under `LIVE_SELL_SIZING_BASIS=lineage` is derived from the lineage
  snapshot but is still clamped by the broker `available_qty` hard cap.
- Board-lot / STAR minimum-order normalization is applied on top of the sized
  delta via `stock_data_access.board_lots`; it changes the *order quantity*, not
  the authority for the underlying delta.

## Minimum Fields

Executable live signals must include:

- `order_id`: stable idempotency key, unique per `user_id`.
- `user_id`: owner of the signal.
- `securities_account_id`: quantFinance securities account id.
- `account_id`: broker account id.
- `broker`: broker adapter name, for example `miniQMT`.
- `mode`: must be `live`.
- `is_executable`: must be `true`.
- `source`: signal source, for example `portfolio_plan` or `strategy_live`.
- `plan_id`: source portfolio plan id when applicable.
- `strategy_template_id`: portfolio strategy template id.
- `symbol`: traded symbol.
- `action`: `buy` or `sell`.
- `size`: positive share quantity.
- `price`: limit price when available.
- `order_type`: `limit` or `market`.
- `execution_phase`: `sell` or `buy`; sell signals are executed before buy signals.
- `execution_priority`: lower values run earlier inside the same phase.
- `plan_item_rank`: source plan item rank, kept for audit and execution ordering.
- `reference_price`: publish-time price anchor used for execution protection.
- `max_slippage_bps`: maximum tolerated slippage in basis points.
- `price_floor`: lowest acceptable sell limit price when available.
- `price_ceiling`: highest acceptable buy limit price when available.
- `effective_limit_price`: initial protected limit price sent to the trader.
- `activate_after`: earliest time the trader may submit the signal to the broker.
- `valid_until`: first execution window expiry, now anchored to `activate_after`
  by default so overnight-published signals do not expire before the next
  executable session; later iterations may cancel or reprice.
- `phase_barrier`: optional buy-signal hold condition, e.g.
  `{ "type": "plan_sells_terminal", "plan_id": "..." }`. Written in Phase 1 of the
  stable-rebalance design but not yet enforced by the trader (enforcement is
  Phase 2); see [stable_rebalance_execution.md](./stable_rebalance_execution.md).
- `fee_model`: optional account fee model used only when broker execution reports do not include real fees. It may include `buy_commission_rate`, `sell_commission_rate`, `min_commission`, `stamp_tax_rate`, and `transfer_fee_rate`; estimated execution fees must be marked with `estimated_fee=true`.
- `status`: initial value is `pending`.
- `created_at`: signal creation time.

The backend helper `services.live_trade_signals.LiveTradeSignal` is the canonical validator for portfolio-plan-published signals.

## Idempotency

Portfolio plan publishing derives `order_id` from:

```text
plan_id + symbol + action + size + target_shares + delta_shares
```

The index helper `tools/create_trade_signals_index.py` now creates a unique index on:

```text
user_id + order_id
```

Publishing an approved plan is therefore safe to retry. Existing active signals are returned in the publish response and are not inserted again.

## Controlled Publish Flow

`POST /portfolio-plans/plans/{plan_id}/publish-live-signals`

Request:

```json
{
  "securities_account_id": "...",
  "dry_run": true
}
```

Rules:

- Only the current user's `approved` plans can be published.
- `dry_run=true` returns generated signals and a risk report without writing `trade_signals`.
- `dry_run=false` writes only new signals after the risk report passes.
- Account live trading is default-off. The securities account must explicitly set one of `live_trading_enabled`, `live_enabled`, or `allow_live_trading`.
- Basic gates cover account binding, available cash, sellable quantity, order/day limits, single-symbol position percentage when configured, missing prices, ST, suspended, and limit-up buy blocks when data is available.
- Published signals include execution phase, priority, reference price, and slippage guard fields so `quantTrader` can execute sells first and avoid unbounded market orders.

`live_trading_enabled` and `risk_limits` are managed on the securities account settings page. Simulated accounts always publish with live trading disabled.

## Execution Status Lifecycle

`quantTrader` updates live signals through these statuses:

```text
pending -> submitted -> partial_filled -> filled
                         rejected | cancelled | failed
```

`trade_executions` writes are idempotent by `user_id + order_id + status + broker_order_id + filled_size`, so polling the same broker state does not create duplicate execution records.

## Ghost entrust ids (QMT list no longer shows the order)

When miniQMT's `query_stock_orders` omits this `broker_order_id`, `quantTrader`
treats a failed cancel API as **idempotent success** (no live entrust to cancel),
including when QMT returns **zero** today's orders. After `cancel_requested`, if
the broker snapshot **still** omits that id, the tracker marks the signal
**cancelled** with `last_error=order_absent_from_broker_query_after_cancel` so
Mongo can reach a terminal state.

While the signal is still **submitted** or **partial_filled**, if the same query
does **not** include this `broker_order_id`, `quantTrader` reconciles straight to
**cancelled** with `last_error=submitted_entrust_absent_from_broker_query` (no
`cancel_requested` step). A successful empty QMT entrust list is interpreted as
"no live orders".

**Trusted vs untrusted snapshot (critical invariant):** only a *successfully
returned* order snapshot may drive the "id absent → cancelled" reconcile. A
successful **empty** snapshot is trusted (means no live orders); a **failure** is
not. The broker layer (`MiniQMTBroker.query_orders` / `get_execution_status`)
therefore raises `BrokerQueryError` when the snapshot is unreliable — not
connected, the xtquant API raised, or it returned `None` — instead of collapsing
that into an empty dict. On `BrokerQueryError` the tracker **skips the whole poll
cycle** and leaves order state unchanged, so a disconnect is never mistaken for
"broker has no orders". For the same reason, a `cancel_order` returning `-1` is
only treated as idempotent success when the re-query **succeeds** and omits the
id; if the re-query itself is untrusted, cancel is reported as not accepted and
retried next cycle.

If `cancel_requested` is set but **QMT still lists** the same `broker_order_id` as an
active entrust, `quantTrader` **retries** `cancel_order` on a throttle (defaults:
`QUANT_TRADER_CANCEL_RETRY_GRACE_SECONDS=15`,
`QUANT_TRADER_CANCEL_RETRY_INTERVAL_SECONDS=25`; same keys under **`execution`** in
`config.json`, with env overriding JSON). When the entrust id **disappears** from the
query (or the query is empty), the tracker moves to **cancelled** as above — no
separate “force timer while row still exists” knob.

## Gap-fill remainder (`plan_topup`)

When a live plan under-executes (signals cancelled or only partially filled) and
deployed capital falls short of target, `POST /portfolio-plans/plans/{plan_id}/replan-remainder`
finishes **the same selection** without re-picking stocks. It is **preview-first**:
call with `dry_run=true` to get a per-item breakdown plus a risk report, then
`dry_run=false` after human confirmation to publish `source=plan_topup` signals.

Rules:

- **Baseline is the live account** (`trader_positions` actual holdings +
  `trader_accounts` available cash), not the paper lineage used at plan
  generation. If the account has no synced snapshot, every item is blocked with
  `account_not_synced` (we never guess holdings).
- Only items the plan **meant to trade** (original `delta_shares != 0`) are topped
  up, and only in the **same direction**. This avoids inventing new sells of
  pre-existing holdings the plan never intended to touch.
- **Buys are capped by remaining budget** (planned target cost − current position
  value) and then by **available cash**, so a price rise since plan time cannot
  overspend the original budget (`budget_capped` / `cash_capped` reasons).
- **Sells** fill the remaining `current − target` shares, limited by available
  (non-frozen) quantity (`available_position_below_gap`).
- A symbol with an **in-flight** (non-terminal) live signal for the plan is
  skipped (`live_signal_active`) so we never double-send. Top-up `order_id`s are
  `live-topup-*` (unique per round) and carry `replaces_order_ids`,
  `topup_reason`, and a `topup_baseline` audit snapshot. Each publish writes a
  `plan_remainder_topup` entry to `portfolio_plan_operation_logs`.

**Operational follow-up (new plan / re-approve):** cancelling the signal does **not**
regenerate `portfolio_trade_plan_items`. To align the portfolio with **latest**
positions and risk, use your normal plan pipeline (e.g. enqueue plan generation /
review, `POST /portfolio-plans/plans/{plan_id}/approve`, then
`POST /portfolio-plans/plans/{plan_id}/publish-live-signals` with a dry run
first). Published `order_id` values are derived from plan item fields, so a new
plan revision produces new signals rather than reviving the old `live-plan-*`
rows.

## Capital Basis (compounding / rolling equity)

Each rebalance sizes positions against a **capital baseline**. The basis is
recorded on the plan as `capital_basis` and audited in `plan.summary`
(`baseline_source`, `baseline_equity`, `baseline_cash`,
`baseline_securities_account_id`, `baseline_synced_at`).

Modes:

- `account_equity` (**default once the lineage has live signals**): the next
  period rolls on the lineage's **own** real equity. It is reconstructed
  combo-scoped from `initial_capital` + every live fill across the lineage's
  rebalance `plan_id`s (deduped per order) + current close prices, so other
  strategies or manual holdings sharing the same brokerage account never leak in.
  This matches the backtest report, whose equity curve is compounded
  (`(1 + period_return).cumprod()`).
- `rolling_paper` (**default for paper-only lineages**, legacy behavior): inherit
  the `portfolio_paper_positions` lineage snapshot (cash/equity/positions).
- `fixed_notional` (opt-in): size every period against a fixed `initial_capital`
  while still carrying lineage positions for sell/hold. Use for fixed-allocation
  or simulation runs.

`capital_basis` is intentionally **not** part of `params_hash` (like
`monitor_can_trade`), so switching it never forks the plan/position lineage.

**Freshness gate (block on stale):** when an `account_equity` lineage already has
carried live state, the brokerage account must have a fresh `trader_accounts`
snapshot. If the account is missing or its `synced_at` is older than
`PORTFOLIO_LIVE_BASELINE_STALE_SECONDS` (default 6h), plan generation **fails
with `account_not_synced`** instead of seeding a plan from stale state. The first
live period (no prior fills) starts from `initial_capital` and is not gated.

The cross-plan endpoint `GET /portfolio-plans/plans/{plan_id}/lineage-live-equity`
returns `current_book_equity`, a continuous one-book equity over all lineage live
executions, so the headline equity no longer resets to `initial_capital` each
rebalance `plan_id`.

## Execution Ordering And Price Guards

Portfolio-plan signals are published with this first-level execution policy:

```text
sell phase -> buy phase
```

Within the same phase, `execution_priority` and `plan_item_rank` keep the order deterministic. Protected limit prices are computed in `services/execution_policy.py`:

```text
buy ceiling  = reference_price * (1 + max_slippage_bps / 10000)   # rounded up to 0.01
sell floor   = reference_price * (1 - max_slippage_bps / 10000)   # rounded down to 0.01
```

Default `max_slippage_bps` depends on the price anchor:

| Context | `reference_price_source` examples | Default style | Default bps |
|---|---|---|---|
| Intraday / manual fast-action | `realtime`, `estimated_price` | `balanced` | 30 |
| Next-open / stale close anchor | `daily_close`, `latest_close` | `open_auction` | 100 |

`reference_price` is the publish-time anchor; `effective_limit_price` is the **protected limit** sent to the trader (buy ceiling or sell floor), not the expected fill price. Account `risk_limits.max_slippage_bps` overrides the style default when set.

`quantTrader` should use `effective_limit_price` or recompute the protected limit before sending the broker order. If no reference price is available for a live sell order, the trader should reject or retry the signal rather than submit an unprotected market sell.

## Stable rebalance execution (design)

For the proposed **two-phase sell → barrier → buy** flow, **pre-authorized buy replanning** after sells settle, new signal/plan fields, and `QUANT_TRADER_*` / backend configuration knobs, see [stable_rebalance_execution.md](./stable_rebalance_execution.md).
