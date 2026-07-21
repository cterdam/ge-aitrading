# Monday Shadow Runbook

Purpose: begin a controlled read-only paper-trading observation on Monday. This
runbook does not authorize live trading and does not enable Robinhood order,
review, cancel, transfer, or account-mutation tools.

All times are America/New_York. If Robinhood's official session differs from a
normal 09:30–16:00 session, the official instrument session controls.

## Definition of success

Monday is successful if the system safely produces any of these outcomes:

- `NO_TRADE` because the complete strategy conditions were not satisfied;
- `REJECTED` with a correct deterministic reason;
- one simulated entry/monitor/exit lifecycle using real read-only quotes;
- an intentional halt caused by stale, missing, contradictory, or disconnected
  data.

A simulated trade is not required. A forced trade is a test failure.

## Before 09:15 ET — offline gate

Run:

```bash
python3 main.py status
```

Expected: `READ_ONLY`, live and order tools false, kill switch engaged.

Run:

```bash
python3 main.py shadow-readiness
```

Expected before market checks: `offline_ready: true`, `monday_go: false`, with
only formal-authorization and market-time checks outstanding.

Run:

```bash
python3 -m unittest discover -s tests
```

Any failure means `NO_GO`.

## 09:15–09:30 ET — connection observation

1. Open the Claude Code CLI in this repository.
2. Confirm Robinhood MCP authentication is healthy.
3. Confirm its enabled tool list remains read-only. Any order/review/cancel or
   mutation tool becoming available is `NO_GO` until the allowlist is audited.
4. Do not remove the local kill-switch protection.

## 09:30–10:00 ET — raw data qualification

Do not evaluate an entry during the first 30 minutes. Collect one raw snapshot:

```bash
python3 main.py raw-collect SPY
```

Record the returned path and SHA-256. Verify it:

```bash
python3 main.py raw-verify PATH_FROM_PREVIOUS_COMMAND --sha256 SHA256_FROM_PREVIOUS_COMMAND
```

Required checks:

- no account/person identifiers or secrets are present;
- source and local receipt timestamps exist;
- official account aggregates, orders, positions, session, OHLCV bars, option
  chain/instruments/quotes, and earnings data are present or explicitly null;
- a second local feature calculation from the identical snapshot is byte-for-
  byte/logically identical to the first;
- settled cash, orders, positions, and official instrument session are known;
- option quote age is within policy.

Any unknown required field means `NO_GO` for formal Shadow. A read-only
diagnostic session may continue but cannot count as evidence.

## At or after 10:00 ET — formal Shadow decision gate

Formal Shadow can start only after:

1. every P0 check is documented as passed;
2. the exact strategy version is recorded;
3. the owner explicitly approves that exact version;
4. a controlled Shadow authorization record is created;
5. the readiness report returns `monday_go: true`.

After attaching concrete evidence to every entry in a copy of
`config/shadow_p0_qualification.example.json`, the owner-only authorization
command is:

```bash
python3 main.py shadow-authorize PATH_TO_COMPLETED_QUALIFICATION.json --owner-approved
```

This authorizes read-only Shadow for `strategy_v1.0`; it does not enable live
trading or order tools. Never run it with incomplete or placeholder evidence.

Until then, only `--pilot` evaluation is permitted and it must not count as
strategy performance.

## During the session

- Maximum one simulated entry candidate for the day.
- A valid `NO_TRADE` is preferred to an incomplete or marginal setup.
- Every sample passes duplicate, order, future-time, and staleness checks.
- Entry simulation requires an observed ask at or below the limit; no assumed
  fill.
- Exit simulation uses executable bid and reports base/stress friction.
- Stop considering new entries 90 minutes before official close.
- Simulated positions must begin forced exit 30 minutes before official close.
- Unknown quote/position/order state terminates the session as an error.

## Emergency stop drill

Engage the stop:

```bash
python3 main.py kill
```

Then verify:

```bash
python3 main.py status
```

Expected: kill switch engaged. In Shadow this stops the session controller; in
any future live architecture it must block new broker mutations independently.

## After close

Audit the day:

```bash
python3 main.py shadow-review YYYY-MM-DD
```

Then evaluate the experiment population:

```bash
python3 main.py shadow-experiment-report
```

Review separately:

- `NO_TRADE` decisions;
- deterministic rejections;
- unfilled simulated entries;
- simulated fills/exits under base and stress friction;
- stale/missing data and system errors;
- rule violations, which must equal zero;
- pilot/drill records, which must remain excluded from strategy evidence.

## Automatic NO_GO conditions

- Safety or strategy validation failure.
- MCP endpoint, OAuth state, market status, or applicable session unknown.
- Any write-capable Robinhood tool is needed for the exercise.
- Raw response contains secrets or identifiers.
- Snapshot hash or deterministic replay mismatch.
- Missing/old/future/out-of-order data.
- Account cash, positions, or open orders cannot be reconciled.
- Kill switch is unreadable or unexpectedly disengaged.
- Strategy remains unapproved, or formal Shadow authorization is absent.
- A simulated position cannot be proven closed before session end.
