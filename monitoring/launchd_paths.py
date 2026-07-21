from __future__ import annotations

from pathlib import Path


def launch_path(python: Path) -> str:
    """PATH for a launchd session so agents can find python3/claude.

    launchd hands processes a minimal PATH, so we prepend the interpreter's own
    bin dir and the common CLI install locations (Homebrew, /usr/local, the
    Claude native installer, ~/.local/bin). Shared by every plist generator so
    the worker and watchdog can never diverge.
    """

    home = Path.home()
    candidates = [
        str(python.parent),
        "/opt/homebrew/bin",
        "/usr/local/bin",
        str(home / ".local/bin"),
        str(home / ".claude/local"),
        "/usr/bin",
        "/bin",
    ]
    ordered: list[str] = []
    for entry in candidates:
        if entry not in ordered:
            ordered.append(entry)
    return ":".join(ordered)
