from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from execution.shadow_input import load_shadow_input
from execution.raw_data_vault import RawDataVault, RawSnapshotReceipt


class OfficialCollectorError(RuntimeError):
    pass


SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")

# The MCP server must be registered in Claude Code under this exact name
# (claude mcp add robinhood-trading ...), and its OAuth must be completed once
# interactively before unattended runs.
MCP_SERVER_NAME = "robinhood-trading"

# Defense in depth for unattended collection.  Claude Code print mode denies
# every tool that is not explicitly allowed, so the child agent can call only
# these read-only Robinhood tools; order/review/cancel/mutation tools are never
# allowed, and local file/shell/network tools are explicitly disallowed again.
READ_ONLY_ROBINHOOD_TOOLS = (
    "get_accounts",
    "get_portfolio",
    "get_equity_positions",
    "get_option_positions",
    "get_equity_orders",
    "get_option_orders",
    "get_equity_quotes",
    "get_equity_historicals",
    "get_equity_technical_indicators",
    "get_option_chains",
    "get_option_instruments",
    "get_option_quotes",
    "get_earnings_calendar",
)

EXPLICITLY_DISALLOWED_TOOLS = (
    "Bash",
    "Write",
    "Edit",
    "NotebookEdit",
    "WebFetch",
    "WebSearch",
    "Task",
)


def claude_binary() -> str:
    """Locate the Claude Code CLI; unattended collection fails closed without it."""

    found = shutil.which("claude")
    if found:
        return found
    for candidate in (
        Path.home() / ".claude/local/claude",
        Path("/opt/homebrew/bin/claude"),
        Path("/usr/local/bin/claude"),
        Path.home() / ".local/bin/claude",
    ):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    raise OfficialCollectorError("CLAUDE_CLI_NOT_FOUND")


def read_only_allowed_tools() -> str:
    return ",".join(f"mcp__{MCP_SERVER_NAME}__{name}" for name in READ_ONLY_ROBINHOOD_TOOLS)


def _read_only_collector_command() -> list[str]:
    return [
        claude_binary(),
        "-p",
        "--output-format", "json",
        "--allowedTools", read_only_allowed_tools(),
        "--disallowedTools", ",".join(EXPLICITLY_DISALLOWED_TOOLS),
    ]


def _final_json_payload(stdout: str) -> dict:
    """Extract the agent's final JSON object from claude -p --output-format json."""

    try:
        envelope = json.loads(stdout)
    except (TypeError, json.JSONDecodeError) as error:
        raise OfficialCollectorError("Claude runner output is not valid JSON.") from error
    if not isinstance(envelope, dict) or envelope.get("is_error") is not False:
        raise OfficialCollectorError("Claude runner reported an error result.")
    result = envelope.get("result")
    if not isinstance(result, str) or not result.strip():
        raise OfficialCollectorError("Claude runner returned no final message.")
    text = result.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise OfficialCollectorError("Final agent message is not a JSON object.") from error
    if not isinstance(payload, dict):
        raise OfficialCollectorError("Final agent message must be a JSON object.")
    return payload


def _safe_failure_detail(stderr: str | None) -> str:
    """Return a bounded diagnostic without leaking account-like identifiers."""
    if not stderr:
        return "no stderr"
    tail = "\n".join(stderr.splitlines()[-12:])
    tail = re.sub(r"\b\d{8,}\b", "[REDACTED_NUMBER]", tail)
    tail = re.sub(
        r"(?i)(bearer|token|authorization)([^\n]{0,120})",
        r"\1 [REDACTED]",
        tail,
    )
    return tail[-2000:]


def _aware_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise OfficialCollectorError("Raw source timestamp is missing.")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise OfficialCollectorError("Raw source timestamp is invalid.") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OfficialCollectorError("Raw source timestamp must include timezone.")
    return parsed


def collect_official_raw_snapshot(
    symbol: str,
    *,
    project_root: str | Path = ".",
    vault_root: str | Path = "logs/raw",
    timeout_seconds: int = 180,
) -> RawSnapshotReceipt:
    """Collect transport-only official MCP data; no model feature calculation."""

    normalized_symbol = symbol.strip().upper()
    if not SYMBOL_PATTERN.fullmatch(normalized_symbol):
        raise OfficialCollectorError("Invalid equity symbol.")
    root = Path(project_root).resolve()
    prompt = (root / "prompts/robinhood_raw_collector.md").read_text(encoding="utf-8").format(symbol=normalized_symbol)
    command = _read_only_collector_command()
    try:
        completed = subprocess.run(
            command, input=prompt, text=True, cwd=root, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=timeout_seconds, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise OfficialCollectorError("Official raw MCP collector failed or timed out.") from error
    if completed.returncode != 0:
        raise OfficialCollectorError(
            "Official raw MCP collector returned no valid result. "
            f"exit={completed.returncode}; "
            f"{_safe_failure_detail(getattr(completed, 'stderr', None))}"
        )
    envelope = _final_json_payload(completed.stdout)
    if envelope.get("schema_version") != 1:
        raise OfficialCollectorError("Unsupported raw snapshot schema.")
    try:
        request = json.loads(envelope.get("request", ""))
        response = json.loads(envelope.get("response", ""))
    except (TypeError, json.JSONDecodeError) as error:
        raise OfficialCollectorError(
            "Raw request/response strings must contain valid JSON."
        ) from error
    if not isinstance(request, dict) or not isinstance(response, dict):
        raise OfficialCollectorError("Raw request/response objects are required.")
    return RawDataVault(root / vault_root).store(
        source="ROBINHOOD_OFFICIAL_MCP",
        request=request,
        response=response,
        source_updated_at=_aware_datetime(envelope.get("source_updated_at")),
        received_at=datetime.now(timezone.utc),
    )


def collect_official_shadow_snapshot(
    symbol: str,
    output_path: str | Path,
    *,
    project_root: str | Path = ".",
    timeout_seconds: int = 180,
) -> Path:
    """Legacy normalized pilot collector.

    Formal Shadow evidence must use ``collect_official_raw_snapshot`` followed
    by deterministic local feature construction. This compatibility path is
    retained only for existing read-only pilot drills.
    """

    normalized_symbol = symbol.strip().upper()
    if not SYMBOL_PATTERN.fullmatch(normalized_symbol):
        raise OfficialCollectorError("Invalid equity symbol.")
    root = Path(project_root).resolve()
    prompt_path = root / "prompts/robinhood_shadow_collector.md"
    prompt = prompt_path.read_text(encoding="utf-8").format(symbol=normalized_symbol)

    command = _read_only_collector_command()
    try:
        completed = subprocess.run(
            command,
            input=prompt,
            text=True,
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise OfficialCollectorError("Official MCP collector failed to start or timed out.") from error
    if completed.returncode != 0:
        raise OfficialCollectorError(
            "Official MCP collector returned no valid result. "
            f"exit={completed.returncode}; "
            f"{_safe_failure_detail(getattr(completed, 'stderr', None))}"
        )

    normalized = _final_json_payload(completed.stdout)
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(
        json.dumps(normalized, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    # Parsing is the security boundary: secrets and malformed/unknown fields
    # reject here, before the temporary file can become the destination.
    try:
        load_shadow_input(temporary)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    temporary.replace(destination)
    return destination
