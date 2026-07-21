# Codex → Claude Code Runtime Migration

Status: code migration complete; **host activation requires the owner's manual
steps below**. Until the manual canary passes, every scheduled run fails
closed (`CLAUDE_CLI_NOT_FOUND` or an MCP/auth failure) and no market data is
collected. Missed samples are never backfilled.

## What changed in code

- `execution/official_mcp_collector.py` and `scripts/launchd_shadow_worker.py`
  now invoke the **Claude Code CLI** (`claude -p`) instead of `codex exec`.
- Read-only enforcement moved from the Codex sandbox/allowlist to Claude
  Code's print-mode permission model: only the thirteen
  `mcp__robinhood-trading__get_*` tools are allowed for collectors (plus
  workspace file tools and `python3` for the pilot agent); everything else is
  denied by default, and local mutation tools are explicitly disallowed again.
- Collector results are read from the agent's final JSON message
  (`--output-format json`); deterministic local parsing remains the security
  boundary and rejects prose, fences, errors, and schema mismatches.
- The preopen qualification gate now verifies the Claude CLI exists and that
  the `robinhood-trading` MCP server is registered; missing legacy Codex
  automation manifests count as passed (Codex is removed and cannot schedule
  duplicates).
- Pilot summaries report `agent_runtime: CLAUDE_CODE_CLI` and
  `agent_return_code`.

## Owner steps on the Mac (must be done before the next observation day)

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
   claude mcp add --transport http --scope user robinhood-trading <OFFICIAL_ROBINHOOD_MCP_URL>
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
