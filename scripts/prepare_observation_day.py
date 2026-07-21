#!/usr/bin/env python3
"""Prepare one observation day in a single step.

Combines the two error-prone daily steps so neither can be forgotten:

  1. render the date-pinned worker plist AND the watchdog plist into the
     LaunchAgents directory, and
  2. pre-register every expected run for that date so the watchdog has
     something to verify (an unregistered day leaves the watchdog fail-open).

It then prints the exact launchctl commands to (re)load both services. Loading
launchd services still happens on the Mac by the owner; this script never calls
launchctl itself.

Usage (on the Mac):
    python3 scripts/prepare_observation_day.py 2026-07-22
    # then run the printed launchctl bootout/bootstrap commands

Dry run without touching LaunchAgents or expectations:
    python3 scripts/prepare_observation_day.py 2026-07-22 --dry-run
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from monitoring.daily_schedule import expected_runs_for_date
from monitoring.scheduler_watchdog import register_expected_run
from scripts.generate_shadow_worker_plist import (
    LABEL as WORKER_LABEL,
    render as render_worker,
)
from scripts.generate_watchdog_plist import LABEL as WATCHDOG_LABEL, render as render_watchdog

DEFAULT_LAUNCH_AGENTS = Path.home() / "Library/LaunchAgents"


def prepare(
    day: date,
    *,
    launch_agents: Path,
    expected_directory: Path,
    write: bool = True,
) -> dict[str, object]:
    worker_path = launch_agents / f"{WORKER_LABEL}.plist"
    watchdog_path = launch_agents / f"{WATCHDOG_LABEL}.plist"
    expectations = expected_runs_for_date(day)

    if write:
        launch_agents.mkdir(parents=True, exist_ok=True)
        worker_path.write_text(render_worker(day), encoding="utf-8")
        watchdog_path.write_text(render_watchdog(), encoding="utf-8")
        registered = [
            str(register_expected_run(run_id=run_id, scheduled_for=scheduled, directory=expected_directory))
            for run_id, scheduled in expectations
        ]
    else:
        registered = [run_id for run_id, _ in expectations]

    return {
        "day": day.isoformat(),
        "worker_plist": str(worker_path),
        "watchdog_plist": str(watchdog_path),
        "expected_run_count": len(expectations),
        "registered": registered,
    }


def _launchctl_block(label: str, plist: str) -> str:
    return (
        f"launchctl bootout gui/$(id -u)/{label} 2>/dev/null\n"
        f"launchctl bootstrap gui/$(id -u) {plist}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare one observation day.")
    parser.add_argument("day", help="Observation date YYYY-MM-DD (America/Los_Angeles).")
    parser.add_argument(
        "--launch-agents",
        default=str(DEFAULT_LAUNCH_AGENTS),
        help="Directory to write the plists into (default: ~/Library/LaunchAgents).",
    )
    parser.add_argument(
        "--expected-dir",
        default="logs/scheduler/expected",
        help="Directory for pre-registered expectations.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen; write nothing.")
    args = parser.parse_args()

    day = date.fromisoformat(args.day)
    result = prepare(
        day,
        launch_agents=Path(args.launch_agents),
        expected_directory=Path(args.expected_dir),
        write=not args.dry_run,
    )

    mode = "DRY RUN — nothing written" if args.dry_run else "prepared"
    print(f"[{mode}] observation day {result['day']}")
    print(f"  worker plist   : {result['worker_plist']}")
    print(f"  watchdog plist : {result['watchdog_plist']}")
    print(f"  expectations   : {result['expected_run_count']} registered")
    if args.dry_run:
        return 0
    print("\nNow (re)load both services on the Mac:\n")
    print(_launchctl_block(WORKER_LABEL, result["worker_plist"]))
    print(_launchctl_block(WATCHDOG_LABEL, result["watchdog_plist"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
