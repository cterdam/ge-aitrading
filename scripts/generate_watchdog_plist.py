#!/usr/bin/env python3
"""Render the launchd watchdog plist for this host.

The independent watchdog scans pre-registered expectations every 60 seconds and
files fail-closed incidents for missed/late/invalid runs. Unlike the worker it
is NOT date-pinned: it runs continuously. Paths are derived from the actual
interpreter and repo location so the plist is correct wherever the repository
lives (the old hand-written template hardcoded a stale install path).

Usage:
    python3 scripts/generate_watchdog_plist.py > ~/Library/LaunchAgents/com.robinhood-ai-trader.watchdog.plist
    launchctl bootout gui/$(id -u)/com.robinhood-ai-trader.watchdog 2>/dev/null
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.robinhood-ai-trader.watchdog.plist
"""

from __future__ import annotations

import sys
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from monitoring.launchd_paths import launch_path

LABEL = "com.robinhood-ai-trader.watchdog"
INTERVAL_SECONDS = 60


def render(*, python: Path | None = None, workdir: Path | None = None) -> str:
    python_path = (python or Path(sys.executable)).resolve()
    work = (workdir or ROOT).resolve()
    tick = work / "scripts/watchdog_tick.py"
    py = escape(str(python_path))
    wk = escape(str(work))
    tk = escape(str(tick))
    path_value = escape(launch_path(python_path))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{py}</string>
    <string>{tk}</string>
  </array>
  <key>WorkingDirectory</key>
  <string>{wk}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>{path_value}</string>
  </dict>
  <key>StartInterval</key>
  <integer>{INTERVAL_SECONDS}</integer>
  <key>StandardOutPath</key>
  <string>{wk}/logs/watchdog.stdout.log</string>
  <key>StandardErrorPath</key>
  <string>{wk}/logs/watchdog.stderr.log</string>
</dict>
</plist>
"""


def main() -> int:
    sys.stdout.write(render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
