# Live Trade Signals Contract

This document defines the minimum executable `trade_signals` contract used by:

```text
approved portfolio plan -> trade_signals -> quantTrader -> miniQMT -> execution/account sync
```

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
- `strategy_id`: strategy identifier.
- `symbol`: traded symbol.
- `action`: `buy` or `sell`.
- `size`: positive share quantity.
- `price`: limit price when available.
- `order_type`: `limit` or `market`.
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

`live_trading_enabled` and `risk_limits` are managed on the securities account settings page. Simulated accounts always publish with live trading disabled.

## Execution Status Lifecycle

`quantTrader` updates live signals through these statuses:

```text
pending -> submitted -> partial_filled -> filled
                         rejected | cancelled | failed
```

`trade_executions` writes are idempotent by `user_id + order_id + status + broker_order_id + filled_size`, so polling the same broker state does not create duplicate execution records.
