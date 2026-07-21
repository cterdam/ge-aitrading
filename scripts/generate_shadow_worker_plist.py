#!/usr/bin/env python3
"""Render the launchd shadow-worker plist for one observation date.

The StartCalendarInterval entries are pinned to a single calendar date on
purpose: automation never silently recurs onto a day the owner did not
explicitly prepare. This generator derives every entry from the shared
monitoring.daily_schedule table so the plist, the pre-registered expectations,
and the worker slot resolution can never diverge.

Usage:
    python3 scripts/generate_shadow_worker_plist.py 2026-07-22 > /tmp/shadow-worker.plist

Then review the file, install it with launchctl, and pre-register the same
day's expectations:
    python3 main.py scheduler-expect-day 2026-07-22

To do both plists plus expectations in one step, prefer:
    python3 scripts/prepare_observation_day.py 2026-07-22
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from monitoring.daily_schedule import DAILY_SLOTS
from monitoring.launchd_paths import launch_path

LABEL = "com.robinhood-ai-trader.shadow-worker-v2"


def render(day: date, *, python: Path | None = None, workdir: Path | None = None) -> str:
    # Derive every path from the actual interpreter and repo location so the
    # plist is correct wherever the repository lives.
    python_path = (python or Path(sys.executable)).resolve()
    work = (workdir or ROOT).resolve()
    worker = work / "scripts/launchd_shadow_worker.py"
    launch_path_value = launch_path(python_path)

    intervals = []
    for hour, minute in sorted(DAILY_SLOTS):
        intervals.append(
            "    <dict>\n"
            f"      <key>Month</key><integer>{day.month}</integer>\n"
            f"      <key>Day</key><integer>{day.day}</integer>\n"
            f"      <key>Hour</key><integer>{hour}</integer>\n"
            f"      <key>Minute</key><integer>{minute}</integer>\n"
            "    </dict>"
        )
    entries = "\n".join(intervals)
    py = escape(str(python_path))
    wk = escape(str(work))
    wkr = escape(str(worker))
    path_value = escape(launch_path_value)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{py}</string>
    <string>{wkr}</string>
  </array>
  <key>WorkingDirectory</key>
  <string>{wk}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>{path_value}</string>
  </dict>
  <key>StandardOutPath</key>
  <string>{wk}/logs/launchd-worker.stdout.log</string>
  <key>StandardErrorPath</key>
  <string>{wk}/logs/launchd-worker.stderr.log</string>
  <key>StartCalendarInterval</key>
  <array>
{entries}
  </array>
</dict>
</plist>
"""


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    day = date.fromisoformat(sys.argv[1])
    sys.stdout.write(render(day))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
