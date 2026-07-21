# Codex → Claude Code Runtime Migration

Status: **complete and validated live on 2026-07-21.** The Claude Code CLI ran
the full observation day (06:35 market gate + pilot samples 07:03 onward all
`COMPLETED`) with the read-only boundary intact and no order tool ever exposed.
The runtime activation steps below are retained as a reference for standing up a
new host; the current Mac host is already activated.

The first market day on the new runtime is treated as a fresh qualification
day, not evidence collection: its six official market checks must be
re-collected under Claude before any formal Shadow authorization is considered.

## What changed in code

- `execution/official_mcp_collector.py` and `scripts/launchd_shadow_worker.py`
  now invoke the **Claude Code CLI** (`claude -p`) instead of `codex exec`.
- Read-only enforcement moved from the Codex sandbox/allowlist to Claude
  Code's print-mode permission model: only `mcp__robinhood-trading__get_*`
  tools are allowed (the raw collector is narrowed further to market-data-only
  tools, excluding every account/order/position tool); everything else is
  denied by default and local mutation tools are explicitly disallowed again.
- **The raw collector is a deterministic stream-json harvest, not model
  transcription.** The agent only *invokes* the bounded read-only tools; local
  code reads each tool's request and byte-faithful response from the
  `--output-format stream-json` event stream, enforces completeness (every
  required tool must appear), rejects any non-JSON tool result (e.g. a
  truncation notice), derives `source_updated_at` only from timestamps literally
  present in the responses (clamped to collection time + 5 min so option
  expirations / future earnings dates can never masquerade as an observation),
  and stores the immutable vault snapshot. The model never touches the data, so
  it cannot fabricate or reshape it. The vault's forbidden-key scan is an
  independent second layer.
- Earnings uses the symbol-scoped `get_earnings_results` (trailing quarters,
  naturally bounded), **not** `get_earnings_calendar` — the calendar tool has
  no symbol parameter and returns a market-wide window that overflows the
  harness tool-output cap and fails the run closed (observed live on the
  2026-07-21 06:10 canary).
- The preopen qualification gate now verifies the Claude CLI exists and that
  the `robinhood-trading` MCP server is registered; missing legacy Codex
  automation manifests count as passed (Codex is removed and cannot schedule
  duplicates).
- Pilot summaries report `agent_runtime: CLAUDE_CODE_CLI` and
  `agent_return_code`.

## Preparing each observation day (required, every day)

launchd plists are pinned to a single calendar date on purpose, so automation
never silently recurs onto a day nobody prepared. Before each observation day,
regenerate the plists and register that day's expectations in one step:

```bash
python3 scripts/prepare_observation_day.py 2026-07-22   # writes both plists + registers expectations
# then run the launchctl bootout/bootstrap commands it prints
```

Never reload plists from anything other than the generators
(`scripts/generate_shadow_worker_plist.py`, `scripts/generate_watchdog_plist.py`,
or the combined `prepare_observation_day.py`). Paths are derived from the actual
interpreter and repo location at render time, so they are correct wherever the
repository lives.

## Runtime activation steps (reference; already done on the current Mac)

1. **Install and authenticate the Claude Code CLI** (if not already):

   ```bash
   curl -fsSL https://claude.ai/install.sh | bash
   claude --version
   ```

   Run `claude` once interactively and complete login.

2. **Register the Robinhood official MCP server under the exact name
   `robinhood-trading` at user scope**, using the same official server
   endpoint previously configured in Codex:

   ```bash
   claude mcp add --transport http --scope user robinhood-trading https://agent.robinhood.com/mcp/trading
   claude mcp get robinhood-trading
   ```

   The server name must match `MCP_SERVER_NAME` in
   `execution/official_mcp_collector.py`; the tool allowlist is derived from
   it.

3. **Complete OAuth once, interactively.** Open `claude` in this repository,
   run `/mcp`, select `robinhood-trading`, and finish the browser OAuth flow.
   Unattended `claude -p` runs reuse this stored authorization; they cannot
   perform interactive OAuth themselves.

4. **Verify the read-only boundary before trusting automation.** In the same
   interactive session, confirm the server's tool list shows only the expected
   tools and that order/cancel/transfer tools, if listed by the server, are
   NOT in the project allowlist (they are never allowed by
   `read_only_allowed_tools()`).

5. **Run the manual canary end to end:**

   ```bash
   ROBINHOOD_SHADOW_CANARY=1 python3 scripts/launchd_shadow_worker.py
   ```

   Success criteria: summary status `COMPLETED`, a new immutable snapshot under
   `logs/raw/`, and a verified SHA-256. Any failure is fail-closed; fix and
   rerun until green.

6. **Run the preopen gate for the intended observation day:**

   ```bash
   python3 scripts/preopen_qualification.py YYYY-MM-DD
   ```

   `PREOPEN_READY` now additionally requires `claude_cli_available` and
   `robinhood_mcp_configured`.

## Explicitly out of scope

This migration changes the agent runtime only. It does not enable order
tools, does not touch the kill switch or hard boundaries, does not authorize
formal Shadow, and does not alter strategy parameters. The first market
session on the new runtime should be treated as a fresh qualification day: its
six market checks must be re-collected under Claude before any formal Shadow
authorization is considered.
