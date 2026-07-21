from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from execution.official_mcp_collector import (
    OfficialCollectorError,
    claude_binary,
    collect_official_raw_snapshot,
    read_only_allowed_tools,
)
from execution.raw_data_vault import RawDataVault
from main import build_status
from monitoring.daily_schedule import DAILY_SLOTS, SESSION_TIMEZONE, run_id_for
from monitoring.kill_switch import AutomationHalt
from monitoring.scheduler_health import write_start_ack
from monitoring.scheduler_watchdog import unresolved_incident_ids


LOCAL = SESSION_TIMEZONE
LOCK_PATH = ROOT / "logs/scheduler/launchd-shadow-worker.lock"
SLOTS = DAILY_SLOTS

# The pilot agent needs read-only Robinhood MCP tools plus the ability to run
# the project's deterministic CLI and write its own logs inside the workspace.
# Everything else stays denied by Claude Code's print-mode default.
PILOT_ALLOWED_TOOLS = ",".join((
    read_only_allowed_tools(),
    "Read",
    "Glob",
    "Grep",
    "Write",
    "Edit",
    "Bash(python3:*)",
    "Bash(/Library/Frameworks/Python.framework/Versions/3.13/bin/python3:*)",
))


def _log_root(now: datetime) -> Path:
    return ROOT / "logs/launchd_worker" / now.astimezone(LOCAL).date().isoformat()


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _resolve_slot(now: datetime) -> tuple[datetime, str, str]:
    candidates = []
    for (hour, minute), (kind, symbol) in SLOTS.items():
        scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        candidates.append((abs((now - scheduled).total_seconds()), scheduled, kind, symbol))
    distance, scheduled, kind, symbol = min(candidates, key=lambda row: row[0])
    if distance > 180:
        raise ValueError("NO_REGISTERED_SLOT_WITHIN_180_SECONDS")
    return scheduled, kind, symbol


def _run_id(scheduled: datetime, kind: str) -> str:
    return run_id_for(kind, scheduled)


def _safety_ok() -> tuple[bool, dict[str, object]]:
    status = build_status()
    halted = AutomationHalt(ROOT / "state/automation_halt.json").active()
    incidents = unresolved_incident_ids(ROOT / "logs/incidents")
    status["automation_halted"] = halted
    status["unresolved_scheduler_incidents"] = list(incidents)
    valid = (
        status["system_mode"] == "READ_ONLY"
        and status["live_trading_enabled"] is False
        and status["order_tools_enabled"] is False
        and status["kill_switch_engaged"] is True
        and not halted
        and not incidents
    )
    return valid, status


def _run_canary(run_id: str, symbol: str, ack_path: Path, log_root: Path) -> int:
    """Exercise launchd -> official read-only MCP -> immutable local evidence."""
    summary_path = log_root / f"{run_id}.json"
    started = datetime.now(timezone.utc)
    try:
        receipt = collect_official_raw_snapshot(symbol, project_root=ROOT)
        verified = RawDataVault.verify(receipt.path, receipt.content_sha256)
        result_status = "COMPLETED"
        failure_reason = None
    except (OfficialCollectorError, ValueError) as error:
        receipt = None
        verified = None
        result_status = "FAILED_CLOSED"
        failure_reason = f"{type(error).__name__}: {error}"
    ended = datetime.now(timezone.utc)
    _atomic_json(summary_path, {
        "schema_version": 1,
        "status": result_status,
        "run_id": run_id,
        "kind": "CANARY",
        "symbol": symbol,
        "ack_path": str(ack_path),
        "started_at": started.isoformat(),
        "ended_at": ended.isoformat(),
        "duration_seconds": (ended - started).total_seconds(),
        "snapshot_path": str(verified.path) if verified else None,
        "snapshot_sha256": verified.content_sha256 if verified else None,
        "failure_reason": failure_reason,
        "read_only": True,
        "live_trading_enabled": False,
        "order_tools_enabled": False,
        "evidence_class": "PILOT_EXCLUDED_FROM_PERFORMANCE",
    })
    subprocess.run(
        [sys.executable, str(ROOT / "scripts/build_shadow_dashboard.py")],
        cwd=ROOT,
        timeout=30,
        check=False,
    )
    return 0 if verified else 2


def main() -> int:
    now = datetime.now(LOCAL)
    log_root = _log_root(now)
    log_root.mkdir(parents=True, exist_ok=True)
    if os.environ.get("ROBINHOOD_SHADOW_CANARY") == "1":
        scheduled = now.replace(second=0, microsecond=0)
        kind, symbol = "CANARY", "SPY"
    else:
        try:
            scheduled, kind, symbol = _resolve_slot(now)
        except ValueError as error:
            _atomic_json(log_root / f"unscheduled-{now:%H%M%S}.json", {
                "status": "REFUSED",
                "reason": str(error),
                "observed_at": now.astimezone(timezone.utc).isoformat(),
            })
            return 2
    run_id = _run_id(scheduled, kind)
    summary_path = log_root / f"{run_id}.json"
    try:
        ack_path = write_start_ack(
            run_id=run_id,
            scheduled_for=scheduled,
            acknowledged_at=now,
        )
    except ValueError as error:
        _atomic_json(summary_path, {"status": "ACK_FAILED", "reason": str(error)})
        return 2

    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _atomic_json(summary_path, {
                "status": "OVERLAP_SKIPPED",
                "run_id": run_id,
                "ack_path": str(ack_path),
            })
            return 0

        safe, safety = _safety_ok()
        if not safe:
            _atomic_json(summary_path, {
                "status": "SAFETY_GATE_FAILED",
                "run_id": run_id,
                "safety": safety,
            })
            return 2

        if kind == "CANARY":
            return _run_canary(run_id, symbol, ack_path, log_root)

        prompt = (ROOT / "prompts/launchd_pilot_worker.md").read_text(encoding="utf-8").format(
            run_id=run_id,
            scheduled_for=scheduled.isoformat(),
            symbol=symbol,
            log_root=str(log_root),
            trajectory_root=str(ROOT / "logs/quote_trajectories" / now.date().isoformat()),
        )
        stdout_path = log_root / f"{run_id}.stdout.jsonl"
        stderr_path = log_root / f"{run_id}.stderr.log"
        try:
            command = [
                claude_binary(), "-p",
                "--output-format", "stream-json", "--verbose",
                "--allowedTools", PILOT_ALLOWED_TOOLS,
            ]
        except OfficialCollectorError as error:
            _atomic_json(summary_path, {
                "status": "CLAUDE_CLI_NOT_FOUND",
                "run_id": run_id,
                "reason": str(error),
                "ack_path": str(ack_path),
            })
            return 2
        started = datetime.now(timezone.utc)
        try:
            with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
                completed = subprocess.run(
                    command,
                    input=prompt,
                    text=True,
                    cwd=ROOT,
                    stdout=stdout,
                    stderr=stderr,
                    timeout=720,
                    check=False,
                )
            result_status = "COMPLETED" if completed.returncode == 0 else "AGENT_FAILED"
            return_code = completed.returncode
        except (OSError, subprocess.TimeoutExpired) as error:
            result_status = "AGENT_TIMEOUT_OR_START_FAILURE"
            return_code = 2
            stderr_path.write_text(type(error).__name__ + "\n", encoding="utf-8")
        ended = datetime.now(timezone.utc)
        _atomic_json(summary_path, {
            "schema_version": 1,
            "status": result_status,
            "run_id": run_id,
            "kind": kind,
            "symbol": symbol,
            "scheduled_for": scheduled.astimezone(timezone.utc).isoformat(),
            "ack_path": str(ack_path),
            "started_at": started.isoformat(),
            "ended_at": ended.isoformat(),
            "duration_seconds": (ended - started).total_seconds(),
            "agent_runtime": "CLAUDE_CODE_CLI",
            "agent_return_code": return_code,
            "read_only": True,
            "live_trading_enabled": False,
            "order_tools_enabled": False,
            "evidence_class": "PILOT_EXCLUDED_FROM_PERFORMANCE",
        })
        subprocess.run(
            [sys.executable, str(ROOT / "scripts/build_shadow_dashboard.py")],
            cwd=ROOT,
            timeout=30,
            check=False,
        )
        return 0 if return_code == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
