# Robinhood AI Trader Experiment

Experimental, AI-assisted options-trading system for a dedicated Robinhood
Agentic cash account. Risk control is the foundation; profitable risk-adjusted
operation is the objective. AI is used only where it can demonstrate measurable
incremental advantage, while deterministic code retains control of safety,
accounting, order validity, and execution state.

## Current status

- Phase 3 read-only validation: complete
- Market-hours option quote freshness verified on 2026-07-17
- Phase 4 local safety foundation: core controls complete
- Phase 5 Shadow infrastructure: deterministic pipeline, session controller,
  saved-scenario replay, audit, daily review, and experiment gates implemented
- Formal multi-day Shadow evidence collection: not started
- System mode: `READ_ONLY`
- Live trading: disabled
- Robinhood order tools: never allowlisted in the Claude Code CLI runtime
- Local kill switch: engaged by default
- Automated local safety/research tests: 230 passing as of 2026-07-20 post-close

This repository contains no Robinhood client and no order-submission code.

## Hard boundaries

- Initial and maximum deployable risk capital: $300
- Profit above $300 is reserved and not automatically reinvested
- No automatic transfers or additional funding
- Cash account only; no margin borrowing
- Long Call and Long Put only
- One contract and one concurrent position maximum
- Limit orders only
- No short options, multi-leg options, stock shorting, averaging down, or Martingale
- No 0DTE; allowed expiration range is 7–21 DTE
- No overnight positions
- First five live trades: maximum $75 premium each
- Stage 2 requires explicit manual approval and completed integrity checks
- Absolute premium ceiling: $120
- Stop new trading below $225 account equity
- Pause after three consecutive losses
- Any unknown required account, order, position, market, or quote state rejects the order

## Safety commands

Show the local status:

```bash
python3 main.py status
```

Engage the local emergency stop:

```bash
python3 main.py kill
```

`kill` both engages the kill switch and writes `state/automation_halt.json`,
which stops every scheduled Shadow/Pilot worker run from starting. Resuming
requires the owner to delete that marker manually after review; there is
intentionally no command to clear it, and no command to arm trading during
READ_ONLY development.

Run all local tests:

```bash
python3 -m unittest discover -s tests -v
```

Scheduled jobs must write a start acknowledgement before doing any other work:

```bash
python3 main.py scheduler-ack shadow-20260720-0615 2026-07-20T06:15:00-07:00
```

`ACTIVE` automation configuration is not proof of execution. A missing or late
ACK is treated as a scheduler incident and must block performance reporting.

Expected runs are pre-registered independently of the scheduled worker:

```bash
python3 main.py scheduler-expect shadow-20260721-0703 2026-07-21T07:03:00-07:00
```

Each observation day must be prepared explicitly before it can run. Register
the complete daily slot table and generate the matching single-date launchd
plist from the same shared schedule:

```bash
python3 main.py scheduler-expect-day 2026-07-22
python3 scripts/generate_shadow_worker_plist.py 2026-07-22 > /tmp/shadow-worker.plist
```

Without both steps the worker will not fire and the watchdog has nothing to
verify for that date. An unresolved scheduler incident (one without an owner
`resolution` block) now deterministically blocks every new worker run.

`scripts/watchdog_tick.py` scans these expectations every minute when the
macOS LaunchAgent template in `config/` is installed. A missed, malformed, or
late ACK creates an immutable incident record, keeps new entries blocked, and
emits one macOS notification.

Report Monday Shadow readiness without activating anything:

```bash
python3 main.py shadow-readiness
```

After completing the six market-time checks with concrete evidence (see
`config/market_checks.example.json`), the same report can consume them:

```bash
python3 main.py shadow-readiness --market-checks logs/qualification/market_checks_YYYYMMDD.json
```

`monday_go` becomes true only when offline readiness, all six evidenced market
checks, and the persisted owner authorization record are all present. Simulated
exits now record `simulated_gross_pnl_usd`, `friction_usd`, and a net
`simulated_pnl_usd` using the conservative `[friction_model]` in
`config/safety.toml`; experiment gates evaluate the net figure, and non-pilot
sessions without a verified `state/shadow_authorization.json` are quarantined
as `unauthorized_sessions`.

The operating sequence and automatic `NO_GO` conditions are documented in
[`MONDAY_SHADOW_RUNBOOK.md`](MONDAY_SHADOW_RUNBOOK.md).

Validate a normalized read-only Shadow snapshot without running the strategy:

```bash
python3 main.py shadow-validate config/shadow_input.example.json
```

Run one explicit read-only pilot decision. This never assumes a fill and does
not count as strategy evidence:

```bash
python3 main.py shadow-evaluate config/shadow_input.example.json --pilot
```

Audit and summarize one day of Shadow logs:

```bash
python3 main.py shadow-review 2026-07-17
```

Replay a complete saved Shadow scenario (never strategy evidence):

```bash
python3 main.py shadow-replay config/shadow_replay.example.json --pilot
```

Evaluate the accumulated Shadow experiment against activation gates:

```bash
python3 main.py shadow-experiment-report
```

Audit the complete inventory of human-selected thresholds. The included file
is intentionally marked unvalidated and therefore returns `REVIEW_REQUIRED`:

```bash
python3 main.py parameter-audit config/parameter_evidence.example.json
```

Run the local-only off-hours qualification summary:

```bash
python3 scripts/run_offhours_qualification.py
```

This exercises local safety/fault definitions and writes a sanitized report to
`logs/qualification/`. It is not a substitute for official market integration,
formal Shadow evidence, or the owner emergency-stop rehearsal.

Collect one normalized snapshot using the authorized Claude Code CLI and
official Robinhood read-only MCP. This command consumes Claude usage and requires the
existing OAuth session; it never enables order tools:

```bash
python3 main.py shadow-collect SOFI logs/inbox/sofi.json
```

## Live-mode gate

The exact implementation boundary is documented in
[`ARCHITECTURE_STATUS.md`](ARCHITECTURE_STATUS.md). The mandatory human-
parameter review and full Shadow experiment are tracked in [`TODO.md`](TODO.md).
The architecture review identified P0 corrections that
block formal Shadow evidence collection and P1 corrections that block Live
Mode. Current thresholds are baseline hypotheses for testing, not proven
optimal settings.

## Project layout

```text
config/       Non-secret safety configuration
strategy/     Versioned strategy, Shadow pipeline, session and exit logic
risk/         Hard-coded safety ceiling and deterministic validator
execution/    Execution boundary (no live implementation yet)
journal/      Structured trade journal with secret-field rejection
monitoring/   Kill switch and monitoring controls
research/     Chronological evaluation and AI-lift measurement primitives
prompts/      Versioned, bounded MCP collection contracts
tests/        Automated safety tests
logs/         Ignored runtime output
credentials/  Ignored; credentials must not be stored here by the model
tokens/       Ignored; OAuth tokens are managed outside this project
```

## Robinhood connection policy

Only Robinhood's official Trading MCP is permitted. Authentication, MFA, and
authorization are completed by the account owner on Robinhood's official pages.
Passwords, MFA codes, recovery codes, OAuth tokens, and account numbers must not
be placed in prompts, source files, logs, screenshots, or Git history.

## Strategy status

`strategy_v1.0` remains in `DESIGN` and has not been finalized. The implemented baseline is a narrow
five-minute trend continuation setup using market direction, VWAP, EMA 9/20,
and volume confirmation. Strategy parameters will be agreed separately and
must pass Shadow Mode before any consideration of live trading.
