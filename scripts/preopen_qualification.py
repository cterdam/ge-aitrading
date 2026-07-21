from __future__ import annotations

import json
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from execution.official_mcp_collector import (
    MCP_SERVER_NAME,
    OfficialCollectorError,
    claude_binary,
)
from main import build_status
from monitoring.daily_schedule import SESSION_TIMEZONE, expected_runs_for_date
from monitoring.shadow_readiness import build_shadow_readiness


AUTOMATION_ROOT = Path.home() / ".codex/automations"
EXPECTED_ROOT = ROOT / "logs/scheduler/expected"
OUTPUT = ROOT / "logs/qualification/latest.preopen.json"


def _automation_text(automation_id: str) -> str | None:
    path = AUTOMATION_ROOT / automation_id / "automation.toml"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _check_automation(automation_id: str) -> tuple[bool, list[str]]:
    text = _automation_text(automation_id)
    if text is None:
        return False, ["AUTOMATION_MANIFEST_MISSING"]
    reasons: list[str] = []
    if 'status = "ACTIVE"' not in text:
        reasons.append("AUTOMATION_NOT_ACTIVE")
    if "scheduler-ack" not in text:
        reasons.append("ATOMIC_START_ACK_NOT_REQUIRED")
    if "COUNT=1" in text:
        reasons.append("DEFECTIVE_COUNT_ONE_RECURRENCE")
    if "READ_ONLY" not in text:
        reasons.append("READ_ONLY_REQUIREMENT_MISSING")
    return not reasons, reasons


def _check_legacy_automation_paused(automation_id: str) -> tuple[bool, list[str]]:
    text = _automation_text(automation_id)
    if text is None:
        # Codex has been removed from this host. A missing legacy manifest
        # cannot schedule duplicate runs, so absence is a pass, not a failure.
        return True, ["LEGACY_AUTOMATION_ABSENT"]
    reasons = [] if 'status = "PAUSED"' in text else ["DUPLICATE_SCHEDULER_NOT_PAUSED"]
    return not reasons, reasons


def _check_claude_runtime() -> tuple[bool, bool, list[str]]:
    """Verify the Claude Code CLI exists and the Robinhood MCP is registered."""

    reasons: list[str] = []
    try:
        binary = claude_binary()
        cli_available = True
    except OfficialCollectorError:
        return False, False, ["CLAUDE_CLI_NOT_FOUND"]
    try:
        result = subprocess.run(
            [binary, "mcp", "get", MCP_SERVER_NAME],
            capture_output=True,
            text=True,
            timeout=20,
        )
        mcp_configured = result.returncode == 0
        if not mcp_configured:
            reasons.append("ROBINHOOD_MCP_NOT_CONFIGURED")
    except (OSError, subprocess.SubprocessError):
        mcp_configured = False
        reasons.append("CLAUDE_MCP_QUERY_FAILED")
    return cli_available, mcp_configured, reasons


def _launchd_service_loaded(label: str, required_fragment: str) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["/bin/launchctl", "print", f"gui/{subprocess.check_output(['/usr/bin/id', '-u'], text=True).strip()}/{label}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        # Off-macOS or launchctl unavailable: fail closed with a reason instead
        # of crashing the whole qualification report.
        return False, "LAUNCHCTL_UNAVAILABLE"
    return result.returncode == 0 and required_fragment in result.stdout, result.stdout[-2000:]


def build_report(observation_day: date | None = None) -> dict[str, object]:
    day = observation_day or datetime.now(SESSION_TIMEZONE).date()
    status = build_status()
    readiness = build_shadow_readiness().to_dict()
    automation_results = {
        automation_id: dict(zip(("passed", "reasons"), _check_legacy_automation_paused(automation_id)))
        for automation_id in ("robinhood-canary", "robinhood", "robinhood-pilot", "robinhood-pilot-2")
    }
    expected = []
    for path in sorted(EXPECTED_ROOT.glob("*.expected.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if payload.get("status") == "EXPECTED":
            expected.append(path.name)
    # Every slot of the observation day must be pre-registered; otherwise the
    # independent watchdog has nothing to verify and is silently fail-open.
    daily_expected_files = {
        f"{run_id}.expected.json" for run_id, _scheduled in expected_runs_for_date(day)
    }
    expectation_checks = {
        filename: filename in expected for filename in sorted(daily_expected_files)
    }
    watchdog_ok, watchdog_detail = _launchd_service_loaded(
        "com.robinhood-ai-trader.watchdog", "run interval = 60 seconds"
    )
    shadow_worker_ok, shadow_worker_detail = _launchd_service_loaded(
        "com.robinhood-ai-trader.shadow-worker-v2", "calendarinterval"
    )
    claude_cli_ok, robinhood_mcp_ok, claude_reasons = _check_claude_runtime()
    safety_ok = (
        status["system_mode"] == "READ_ONLY"
        and status["live_trading_enabled"] is False
        and status["order_tools_enabled"] is False
        and status["kill_switch_engaged"] is True
        and status["automation_halted"] is False
    )
    passed = (
        safety_ok
        and readiness["offline_ready"] is True
        and all(item["passed"] for item in automation_results.values())
        and all(expectation_checks.values())
        and watchdog_ok
        and shadow_worker_ok
        and claude_cli_ok
        and robinhood_mcp_ok
    )
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "observation_day": day.isoformat(),
        "status": "PREOPEN_READY" if passed else "NO_GO",
        "safety_ok": safety_ok,
        "safety": status,
        "offline_ready": readiness["offline_ready"],
        "formal_shadow_authorized": readiness["formal_shadow_authorized"],
        "market_checks_pending": readiness["pending_market_checks"],
        "automations": automation_results,
        "expected_run_count": len(expected),
        "required_expectations": expectation_checks,
        "watchdog_loaded": watchdog_ok,
        "watchdog_detail_tail": watchdog_detail,
        "shadow_worker_loaded": shadow_worker_ok,
        "shadow_worker_detail_tail": shadow_worker_detail,
        "claude_cli_available": claude_cli_ok,
        "robinhood_mcp_configured": robinhood_mcp_ok,
        "claude_runtime_reasons": claude_reasons,
        "note": "launchd is the sole primary scheduler; the agent runtime is the Claude Code CLI with a read-only tool allowlist. PREOPEN_READY authorizes read-only Pilot preparation only; market gates and formal Shadow authorization remain separate.",
    }


def main() -> int:
    observation_day = None
    if len(sys.argv) > 1:
        observation_day = date.fromisoformat(sys.argv[1])
    report = build_report(observation_day)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUTPUT.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(OUTPUT)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "PREOPEN_READY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
