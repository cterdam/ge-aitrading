from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path

from execution.shadow_input import InvalidShadowInputError, load_shadow_input
from execution.shadow_pilot import run_one_shot_pilot
from execution.shadow_replay import run_shadow_replay
from execution.official_mcp_collector import (
    OfficialCollectorError,
    collect_official_raw_snapshot,
    collect_official_shadow_snapshot,
)
from execution.raw_data_vault import RawDataVault
from monitoring.daily_schedule import SESSION_TIMEZONE, expected_runs_for_date
from monitoring.kill_switch import AutomationHalt, KillSwitch
from monitoring.shadow_readiness import build_shadow_readiness, load_market_check_evidence
from monitoring.scheduler_health import evaluate_start_ack, write_start_ack
from monitoring.scheduler_watchdog import (
    check_expected_run,
    register_expected_run,
    scan_expected_runs,
)
from monitoring.shadow_activation import (
    evaluate_shadow_authorization,
    load_p0_qualification,
    load_shadow_authorization,
    persist_shadow_authorization,
)
from journal.shadow_review import InvalidShadowLogError, review_shadow_day
from journal.shadow_experiment import build_shadow_experiment_report
from research.parameter_audit import audit_parameters
from risk.startup_guard import load_safety_config, validate_safety_config


SAFETY_CONFIG_PATH = Path("config/safety.toml")
STRATEGY_VERSION = "strategy_v1.0"
AUTHORIZATION_PATH = Path("state/shadow_authorization.json")


def build_status() -> dict[str, object]:
    config = load_safety_config(SAFETY_CONFIG_PATH)
    validate_safety_config(config)
    kill_switch = KillSwitch().status()

    phase3_blockers = []
    if config.get("realtime_option_quote_verified") is not True:
        phase3_blockers.append("REALTIME_OPTION_QUOTE_NOT_VERIFIED")

    return {
        "system_mode": config["system_mode"],
        "live_trading_enabled": config["live_trading_enabled"],
        "order_tools_enabled": config["order_tools_enabled"],
        "kill_switch_engaged": kill_switch.engaged,
        "kill_switch_reason": kill_switch.reason,
        "automation_halted": AutomationHalt().active(),
        "phase3_blockers": phase3_blockers,
        "max_deployable_capital_usd": config["max_deployable_capital_usd"],
        "approved_trade_stage": config["approved_trade_stage"],
    }


def status_command() -> int:
    print(json.dumps(build_status(), indent=2, sort_keys=True))
    return 0


def kill_command() -> int:
    result = KillSwitch().engage()
    halt_path = AutomationHalt().engage()
    print(json.dumps({
        "engaged": result.engaged,
        "reason": result.reason,
        "automation_halted": True,
        "automation_halt_marker": str(halt_path),
        "resume_note": "Automation stays halted until the owner removes the marker file after review.",
    }, indent=2))
    return 0 if result.engaged else 1


def shadow_readiness_command(market_checks_path: str | None = None) -> int:
    market_checks = None
    if market_checks_path is not None:
        try:
            market_checks = load_market_check_evidence(market_checks_path)
        except ValueError as error:
            print(json.dumps({"status": "INVALID", "error": str(error)}, indent=2))
            return 1
    report = build_shadow_readiness(market_checks=market_checks)
    print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    return 0 if report.offline_ready else 1


def shadow_authorize_command(qualification: str, *, owner_approved: bool) -> int:
    strategy_version = "strategy_v1.0"
    try:
        checks = load_p0_qualification(qualification, strategy_version)
        authorization = evaluate_shadow_authorization(
            strategy_version=strategy_version,
            p0_checks=checks,
            owner_approved=owner_approved,
        )
        if not authorization.authorized:
            print(json.dumps({"status": "REFUSED", "reasons": authorization.reasons}, indent=2))
            return 2
        path = persist_shadow_authorization(authorization)
    except ValueError as error:
        print(json.dumps({"status": "INVALID", "error": str(error)}, indent=2))
        return 1
    print(json.dumps({
        "status": "SHADOW_AUTHORIZED",
        "strategy_version": strategy_version,
        "authorization_path": str(path),
        "live_trading_enabled": False,
        "order_tools_enabled": False,
    }, indent=2, sort_keys=True))
    return 0


def shadow_validate_command(path: str) -> int:
    try:
        sample_id, source_updated_at, snapshot = load_shadow_input(path)
    except InvalidShadowInputError as error:
        print(json.dumps({"status": "INVALID", "error": str(error)}, indent=2))
        return 1
    print(
        json.dumps(
            {
                "status": "VALID",
                "read_only": True,
                "sample_id": sample_id,
                "source_updated_at": source_updated_at.isoformat(),
                "symbol": snapshot.underlying.symbol,
                "option_type": snapshot.option.option_type.value,
                "market_bar_count": len(snapshot.market_bars),
                "account_identifiers_present": False,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _formal_shadow_authorized() -> bool:
    return load_shadow_authorization(STRATEGY_VERSION, AUTHORIZATION_PATH)


def shadow_evaluate_command(path: str, *, pilot: bool) -> int:
    if not pilot and not _formal_shadow_authorized():
        print(json.dumps({
            "status": "REFUSED",
            "error": "Formal mode requires a valid state/shadow_authorization.json; otherwise pass --pilot.",
        }, indent=2))
        return 2
    try:
        result = run_one_shot_pilot(
            path,
            pilot_mode=pilot,
            shadow_authorized=not pilot,
        )
    except (InvalidShadowInputError, ValueError) as error:
        print(json.dumps({"status": "INVALID", "error": str(error)}, indent=2))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] != "DATA_REJECTED" else 1


def shadow_review_command(day: str) -> int:
    try:
        parsed = date.fromisoformat(day)
        review = review_shadow_day(parsed)
    except (ValueError, InvalidShadowLogError) as error:
        print(json.dumps({"status": "INVALID", "error": str(error)}, indent=2))
        return 1
    print(json.dumps(review.to_dict(), indent=2, sort_keys=True))
    return 0


def shadow_replay_command(path: str, *, pilot: bool) -> int:
    if not pilot and not _formal_shadow_authorized():
        print(json.dumps({
            "status": "REFUSED",
            "error": "Formal mode requires a valid state/shadow_authorization.json; otherwise pass --pilot.",
        }, indent=2))
        return 2
    try:
        result = run_shadow_replay(
            path,
            pilot_mode=pilot,
            shadow_authorized=not pilot,
        )
    except (InvalidShadowInputError, ValueError, RuntimeError) as error:
        print(json.dumps({"status": "INVALID", "error": str(error)}, indent=2))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def shadow_experiment_command() -> int:
    try:
        report = build_shadow_experiment_report()
    except (ValueError, InvalidShadowLogError) as error:
        print(json.dumps({"status": "INVALID", "error": str(error)}, indent=2))
        return 1
    print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    return 0


def parameter_audit_command(path: str) -> int:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if raw.get("schema_version") != 1:
            raise ValueError("PARAMETER_EVIDENCE_SCHEMA_INVALID")
        audit = audit_parameters(raw.get("parameters", {}), raw.get("evidence_versions", {}))
    except (OSError, json.JSONDecodeError, ValueError, AttributeError) as error:
        print(json.dumps({"status": "INVALID", "error": str(error)}, indent=2))
        return 1
    status = str(raw.get("status") or "UNKNOWN")
    validated = audit.complete and status == "VALIDATED"
    print(json.dumps({
        "status": "VALIDATED" if validated else "REVIEW_REQUIRED",
        "inventory_complete": audit.complete,
        "evidence_status": status,
        "missing": list(audit.missing),
        "unversioned": list(audit.unversioned),
        "live_mode_blocked": not validated,
    }, indent=2, sort_keys=True))
    return 0 if validated else 2


def shadow_collect_command(symbol: str, output: str) -> int:
    try:
        path = collect_official_shadow_snapshot(symbol, output)
    except (OfficialCollectorError, InvalidShadowInputError, ValueError) as error:
        print(json.dumps({"status": "REJECTED", "error": str(error)}, indent=2))
        return 1
    print(
        json.dumps(
            {
                "status": "COLLECTED",
                "read_only": True,
                "symbol": symbol.upper(),
                "output": str(path),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def raw_collect_command(symbol: str) -> int:
    try:
        receipt = collect_official_raw_snapshot(symbol)
    except (OfficialCollectorError, ValueError) as error:
        print(json.dumps({"status": "REJECTED", "error": str(error)}, indent=2))
        return 1
    print(json.dumps({
        "status": "RAW_SNAPSHOT_STORED",
        "read_only": True,
        "snapshot_id": receipt.snapshot_id,
        "content_sha256": receipt.content_sha256,
        "path": str(receipt.path),
    }, indent=2, sort_keys=True))
    return 0


def market_check_verify_command(snapshot: str, output: str | None) -> int:
    from monitoring.market_checks import to_evidence_document, verify_market_checks

    try:
        results = verify_market_checks(snapshot)
    except (OSError, ValueError) as error:
        print(json.dumps({"status": "INVALID", "error": str(error)}, indent=2))
        return 1
    document = to_evidence_document(results)
    if output is not None:
        destination = Path(output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(destination)
        document["written_to"] = str(destination)
    print(json.dumps(document, indent=2, sort_keys=True))
    # Exit non-zero unless every check is a PASS, so scripts fail closed.
    all_pass = all(result.passed for result in results.values())
    return 0 if all_pass else 2


def raw_verify_command(path: str, sha256: str | None) -> int:
    try:
        receipt = RawDataVault.verify(path, sha256)
    except ValueError as error:
        print(json.dumps({"status": "INVALID", "error": str(error)}, indent=2))
        return 1
    print(json.dumps({
        "status": "VALID",
        "snapshot_id": receipt.snapshot_id,
        "content_sha256": receipt.content_sha256,
        "path": str(receipt.path),
    }, indent=2, sort_keys=True))
    return 0


def scheduler_ack_command(run_id: str, scheduled_for: str) -> int:
    try:
        scheduled = datetime.fromisoformat(scheduled_for)
        path = write_start_ack(
            run_id=run_id,
            scheduled_for=scheduled,
            acknowledged_at=datetime.now().astimezone(),
        )
    except ValueError as error:
        print(json.dumps({"status": "INVALID", "error": str(error)}, indent=2))
        return 1
    print(json.dumps({"status": "START_ACK_WRITTEN", "path": str(path)}, indent=2))
    return 0


def scheduler_check_command(path: str, scheduled_for: str, grace_seconds: int) -> int:
    try:
        report = evaluate_start_ack(
            path=path,
            scheduled_for=datetime.fromisoformat(scheduled_for),
            checked_at=datetime.now().astimezone(),
            grace_seconds=grace_seconds,
        )
    except ValueError as error:
        print(json.dumps({"status": "INVALID", "error": str(error)}, indent=2))
        return 1
    print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    return 0 if report.healthy else 2


def scheduler_watchdog_command(
    run_id: str, scheduled_for: str, grace_seconds: int
) -> int:
    try:
        report = check_expected_run(
            run_id=run_id,
            scheduled_for=datetime.fromisoformat(scheduled_for),
            checked_at=datetime.now().astimezone(),
            grace_seconds=grace_seconds,
        )
    except ValueError as error:
        print(json.dumps({"status": "INVALID", "error": str(error)}, indent=2))
        return 1
    print(json.dumps({
        "status": "HEALTHY" if report.health.healthy else report.health.reason,
        "health": report.health.to_dict(),
        "incident_path": str(report.incident_path) if report.incident_path else None,
        "new_entries_blocked": not report.health.healthy,
    }, indent=2, sort_keys=True))
    return 0 if report.health.healthy else (3 if report.health.reason == "START_ACK_PENDING" else 2)


def scheduler_expect_command(run_id: str, scheduled_for: str) -> int:
    try:
        path = register_expected_run(
            run_id=run_id,
            scheduled_for=datetime.fromisoformat(scheduled_for),
        )
    except ValueError as error:
        print(json.dumps({"status": "INVALID", "error": str(error)}, indent=2))
        return 1
    print(json.dumps({"status": "EXPECTED_RUN_REGISTERED", "path": str(path)}, indent=2))
    return 0


def scheduler_expect_day_command(day: str) -> int:
    """Register the full daily slot table so the watchdog is never fail-open."""

    try:
        parsed = date.fromisoformat(day)
        registered = []
        for run_id, scheduled_for in expected_runs_for_date(parsed, SESSION_TIMEZONE):
            path = register_expected_run(run_id=run_id, scheduled_for=scheduled_for)
            registered.append({"run_id": run_id, "scheduled_for": scheduled_for.isoformat(), "path": str(path)})
    except ValueError as error:
        print(json.dumps({"status": "INVALID", "error": str(error)}, indent=2))
        return 1
    print(json.dumps({
        "status": "EXPECTED_DAY_REGISTERED",
        "day": parsed.isoformat(),
        "count": len(registered),
        "registered": registered,
    }, indent=2, sort_keys=True))
    return 0


def scheduler_watchdog_scan_command(grace_seconds: int) -> int:
    results = scan_expected_runs(
        checked_at=datetime.now().astimezone(),
        grace_seconds=grace_seconds,
    )
    incidents = [result for result in results if not result.health.healthy and result.health.reason != "START_ACK_PENDING"]
    pending = [result for result in results if result.health.reason == "START_ACK_PENDING"]
    print(json.dumps({
        "status": "INCIDENT" if incidents else ("PENDING" if pending else "HEALTHY"),
        "expectations_checked": len(results),
        "incidents": [result.health.to_dict() for result in incidents],
        "pending": [result.health.to_dict() for result in pending],
        "new_entries_blocked": bool(incidents),
    }, indent=2, sort_keys=True))
    return 2 if incidents else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Robinhood AI trader safety control (READ_ONLY development)."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status", help="Show local safety status.")
    subparsers.add_parser("kill", help="Engage the local emergency stop.")
    readiness_parser = subparsers.add_parser(
        "shadow-readiness",
        help="Report offline readiness and Monday market-time blockers without activating Shadow.",
    )
    readiness_parser.add_argument(
        "--market-checks",
        help="Path to evidence-bearing market-check results (see config/market_checks.example.json).",
    )
    authorize_parser = subparsers.add_parser(
        "shadow-authorize",
        help="Authorize formal Shadow from complete P0 evidence plus explicit owner approval.",
    )
    authorize_parser.add_argument("qualification")
    authorize_parser.add_argument("--owner-approved", action="store_true")
    validate_parser = subparsers.add_parser(
        "shadow-validate", help="Validate a normalized read-only Shadow JSON input."
    )
    validate_parser.add_argument("path", help="Path to the Shadow JSON input file.")
    evaluate_parser = subparsers.add_parser(
        "shadow-evaluate", help="Run one explicit read-only Shadow pilot decision."
    )
    evaluate_parser.add_argument("path", help="Path to the validated Shadow JSON input file.")
    evaluate_parser.add_argument(
        "--pilot", action="store_true", help="Acknowledge this is a non-performance pilot."
    )
    review_parser = subparsers.add_parser(
        "shadow-review", help="Summarize and audit one day of Shadow logs."
    )
    review_parser.add_argument("date", help="Session date in YYYY-MM-DD format.")
    replay_parser = subparsers.add_parser(
        "shadow-replay", help="Replay a full read-only Shadow scenario."
    )
    replay_parser.add_argument("path", help="Path to the replay scenario JSON file.")
    replay_parser.add_argument("--pilot", action="store_true")
    subparsers.add_parser(
        "shadow-experiment-report", help="Evaluate accumulated Shadow evidence gates."
    )
    parameter_parser = subparsers.add_parser(
        "parameter-audit", help="Audit the mandatory human-selected parameter evidence inventory."
    )
    parameter_parser.add_argument("path")
    collect_parser = subparsers.add_parser(
        "shadow-collect", help="Collect one normalized snapshot through official read-only MCP."
    )
    collect_parser.add_argument("symbol", help="Underlying equity symbol.")
    collect_parser.add_argument("output", help="Destination JSON path.")
    raw_collect_parser = subparsers.add_parser(
        "raw-collect", help="Store one transport-only official MCP snapshot in the raw vault."
    )
    raw_collect_parser.add_argument("symbol", help="Underlying equity symbol.")
    raw_verify_parser = subparsers.add_parser(
        "raw-verify", help="Verify canonical encoding and optional SHA-256 of a raw snapshot."
    )
    raw_verify_parser.add_argument("path")
    raw_verify_parser.add_argument("--sha256")
    market_check_parser = subparsers.add_parser(
        "market-check-verify",
        help="Deterministically adjudicate the six market checks from a raw snapshot.",
    )
    market_check_parser.add_argument("snapshot", help="Path to an immutable raw vault snapshot.")
    market_check_parser.add_argument(
        "--out", help="Write the evidence document (feed to shadow-readiness --market-checks)."
    )
    ack_parser = subparsers.add_parser(
        "scheduler-ack", help="Atomically record proof that a scheduled task started."
    )
    ack_parser.add_argument("run_id")
    ack_parser.add_argument("scheduled_for", help="Timezone-aware ISO-8601 scheduled time.")
    check_parser = subparsers.add_parser(
        "scheduler-check", help="Fail closed when a scheduled-start ACK is absent or late."
    )
    check_parser.add_argument("path")
    check_parser.add_argument("scheduled_for", help="Timezone-aware ISO-8601 scheduled time.")
    check_parser.add_argument("--grace-seconds", type=int, default=120)
    watchdog_parser = subparsers.add_parser(
        "scheduler-watchdog",
        help="Independently audit an expected run and persist scheduler incidents.",
    )
    watchdog_parser.add_argument("run_id")
    watchdog_parser.add_argument("scheduled_for", help="Timezone-aware ISO-8601 scheduled time.")
    watchdog_parser.add_argument("--grace-seconds", type=int, default=120)
    expect_parser = subparsers.add_parser(
        "scheduler-expect", help="Pre-register one expected run for independent monitoring."
    )
    expect_parser.add_argument("run_id")
    expect_parser.add_argument("scheduled_for", help="Timezone-aware ISO-8601 scheduled time.")
    expect_day_parser = subparsers.add_parser(
        "scheduler-expect-day",
        help="Pre-register the complete daily slot table for one observation date.",
    )
    expect_day_parser.add_argument("day", help="Observation date in YYYY-MM-DD (America/Los_Angeles).")
    scan_parser = subparsers.add_parser(
        "scheduler-watchdog-scan", help="Audit all pre-registered scheduled runs."
    )
    scan_parser.add_argument("--grace-seconds", type=int, default=120)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "status":
        return status_command()
    if args.command == "kill":
        return kill_command()
    if args.command == "shadow-readiness":
        return shadow_readiness_command(args.market_checks)
    if args.command == "shadow-authorize":
        return shadow_authorize_command(args.qualification, owner_approved=args.owner_approved)
    if args.command == "shadow-validate":
        return shadow_validate_command(args.path)
    if args.command == "shadow-evaluate":
        return shadow_evaluate_command(args.path, pilot=args.pilot)
    if args.command == "shadow-review":
        return shadow_review_command(args.date)
    if args.command == "shadow-replay":
        return shadow_replay_command(args.path, pilot=args.pilot)
    if args.command == "shadow-experiment-report":
        return shadow_experiment_command()
    if args.command == "parameter-audit":
        return parameter_audit_command(args.path)
    if args.command == "shadow-collect":
        return shadow_collect_command(args.symbol, args.output)
    if args.command == "raw-collect":
        return raw_collect_command(args.symbol)
    if args.command == "raw-verify":
        return raw_verify_command(args.path, args.sha256)
    if args.command == "market-check-verify":
        return market_check_verify_command(args.snapshot, args.out)
    if args.command == "scheduler-ack":
        return scheduler_ack_command(args.run_id, args.scheduled_for)
    if args.command == "scheduler-check":
        return scheduler_check_command(args.path, args.scheduled_for, args.grace_seconds)
    if args.command == "scheduler-watchdog":
        return scheduler_watchdog_command(args.run_id, args.scheduled_for, args.grace_seconds)
    if args.command == "scheduler-expect":
        return scheduler_expect_command(args.run_id, args.scheduled_for)
    if args.command == "scheduler-expect-day":
        return scheduler_expect_day_command(args.day)
    if args.command == "scheduler-watchdog-scan":
        return scheduler_watchdog_scan_command(args.grace_seconds)
    raise AssertionError(f"Unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
