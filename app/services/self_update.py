"""
Orion's Belt — self-update service.

Orion's Belt ships three ways (see docs/architecture.md): a from-source
checkout run via install.py/run.sh/run.bat, a frozen PyInstaller bundle
(OrionsBelt.exe), and the setup.exe native bootstrapper. Only the first can
update itself in place — a frozen exe can't overwrite its own running
binary, and there's no .git repo to pull for either exe path. Those users
are pointed at the GitHub releases page instead of getting an "Update now"
button.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import requests

from config import Config

ROOT = Path(__file__).resolve().parent.parent.parent
GITHUB_REPO = "richard-callis/orions-belt"
GITHUB_API_TIMEOUT = 8
GIT_TIMEOUT = 30
PIP_INSTALL_TIMEOUT = 600


def is_source_install() -> bool:
    """True only for a from-source checkout — not a frozen PyInstaller
    build, and with a .git directory for `git pull` to act on."""
    return not getattr(sys, "frozen", False) and (ROOT / ".git").is_dir()


def get_current_version() -> str:
    """`git describe --tags` when running from a git checkout — accurate
    and self-maintaining, so nobody has to remember to bump
    Config.APP_VERSION on every release. Falls back to that static
    constant for frozen exe builds, which ship without a .git directory."""
    if is_source_install():
        try:
            result = subprocess.run(
                ["git", "describe", "--tags", "--abbrev=0"],
                cwd=ROOT, capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
    return Config.APP_VERSION


def _parse_version(v: str) -> tuple:
    """'v1.2.0' -> (1, 2, 0). Only the LEADING digits of each dot-separated
    segment are kept, so a prerelease suffix like 'v1.2.0-rc1' parses as
    (1, 2, 0) rather than pulling the '1' out of '-rc1' into the patch
    number."""
    v = (v or "").lstrip("vV")
    parts = []
    for p in v.split("."):
        digits = re.match(r"\d*", p).group(0)
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def is_newer(latest: str, current: str) -> bool:
    return _parse_version(latest) > _parse_version(current)


def check_for_update() -> dict:
    """Query GitHub's latest release. Network/parse failures are reported
    as a soft {"ok": False, ...} result rather than raised — this runs on a
    Settings-page load and shouldn't break the page if GitHub is
    unreachable."""
    current = get_current_version()
    try:
        resp = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
            headers={"Accept": "application/vnd.github+json"},
            timeout=GITHUB_API_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return {
            "ok": False,
            "error": f"Could not reach GitHub: {e}",
            "current_version": current,
            "source_install": is_source_install(),
        }

    latest = data.get("tag_name", "")
    return {
        "ok": True,
        "current_version": current,
        "latest_version": latest,
        "update_available": bool(latest) and is_newer(latest, current),
        "release_url": data.get("html_url"),
        "release_notes": data.get("body"),
        "published_at": data.get("published_at"),
        "source_install": is_source_install(),
    }


def _requirements_snapshot() -> str:
    path = ROOT / "requirements.txt"
    return path.read_text() if path.exists() else ""


def _schedule_restart(delay: float = 1.5) -> None:
    """Restart the whole process after `delay` seconds — long enough for
    the HTTP response carrying the apply_update() result to actually reach
    the browser before the process sending it disappears. Mirrors
    install.py's start_app(): os.execv replaces the process outright so
    there's exactly one process afterward, not an orphaned parent."""
    def _restart():
        time.sleep(delay)
        os.execv(sys.executable, [sys.executable] + sys.argv)

    threading.Thread(target=_restart, daemon=True).start()


def apply_update() -> dict:
    """git pull --ff-only, reinstall dependencies if requirements.txt
    changed, then restart so the new code takes effect.

    Refuses on anything but a clean, from-source checkout — a plain `git
    pull` over local edits would either fail outright or silently merge
    over work the user hasn't committed, and there's no repo to pull for a
    frozen exe build.
    """
    if not is_source_install():
        return {"ok": False, "error": "Not a source install — download the latest release instead."}

    try:
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=ROOT,
            capture_output=True, text=True, timeout=GIT_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "error": f"git status failed: {e}"}
    if status.returncode != 0:
        return {"ok": False, "error": status.stderr.strip() or "git status failed"}
    if status.stdout.strip():
        return {"ok": False, "error": "Local changes detected — commit, stash, or discard them before updating."}

    before = _requirements_snapshot()
    try:
        pull = subprocess.run(
            ["git", "pull", "--ff-only"], cwd=ROOT,
            capture_output=True, text=True, timeout=GIT_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "error": f"git pull failed: {e}"}
    if pull.returncode != 0:
        return {"ok": False, "error": pull.stderr.strip() or pull.stdout.strip() or "git pull failed"}

    deps_changed = _requirements_snapshot() != before
    if deps_changed:
        try:
            install = subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r", str(ROOT / "requirements.txt"), "--quiet"],
                cwd=ROOT, capture_output=True, text=True, timeout=PIP_INSTALL_TIMEOUT,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            return {"ok": False, "error": f"Pulled new code but dependency install failed to run: {e}"}
        if install.returncode != 0:
            return {
                "ok": False,
                "error": "Pulled new code but dependency install failed — run install.py manually: "
                         + (install.stderr.strip()[-500:] or "unknown error"),
            }

    _schedule_restart()
    return {"ok": True, "deps_reinstalled": deps_changed, "restarting": True}
