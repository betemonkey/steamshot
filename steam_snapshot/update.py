"""Update check, and self-update of a git checkout.

At most every CHECK_EVERY seconds, read the version on the main branch of the
GitHub repo (`git fetch`, or one plain download for a copy that is not a git
checkout - nothing about your library is sent). If it is
newer and [updates] auto is on, `git pull --ff-only` the project folder. Only
a clean checkout of main is pulled; anything else (no git, a zip download,
local edits, another branch) is left alone and reported as "manual", so the
dashboard can show how to update by hand.

The result is kept in <data_dir>/update.json, shared by the scheduled
snapshot run and the dashboard server. Nothing here raises.
"""
import os
import re
import shutil
import subprocess
import sys
import time

from . import __version__, config
from .snapshot import log_line, read_json, write_json

REPO = "betemonkey/steamshot"
VERSION_URL = f"https://raw.githubusercontent.com/{REPO}/main/steam_snapshot/__init__.py"
STATE_NAME = "update.json"
CHECK_EVERY = 6 * 3600
VERSION_RE = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.M)
INIT_FILE = os.path.join(config.PROJECT_DIR, "steam_snapshot", "__init__.py")


def parse_version(v):
    """"1.10.0" -> (1, 10, 0); anything unparseable sorts lowest."""
    try:
        return tuple(int(p) for p in str(v).split("."))
    except ValueError:
        return ()


def newer(a, b):
    return parse_version(a) > parse_version(b)


def disk_version():
    """The version of the code on disk now, which differs from __version__
    once an update has been pulled under a running process."""
    try:
        with open(INIT_FILE, encoding="utf-8") as fh:
            m = VERSION_RE.search(fh.read())
    except OSError:
        return None
    return m.group(1) if m else None


def find_git():
    """git on PATH, or where Git for Windows installs it (its installer can
    leave it off PATH, and a scheduled task may see a shorter PATH).

    Not shutil.which on Windows: that looks in the current folder first, so a
    git.exe sitting in Downloads would run whenever the tool started there."""
    if sys.platform != "win32":
        return shutil.which("git")
    dirs = [d for d in os.environ.get("PATH", "").split(os.pathsep) if os.path.isabs(d)]
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
        if base and os.path.isabs(base):
            dirs.append(os.path.join(base, "Git", "cmd"))
    for d in dirs:
        cand = os.path.join(d, "git.exe")
        if os.path.isfile(cand):
            return cand
    return None


def is_checkout():
    return bool(find_git()) and os.path.isdir(os.path.join(config.PROJECT_DIR, ".git"))


def latest_version(timeout=10):
    """The version on main: via git when this is a checkout, otherwise a
    plain download from GitHub."""
    if is_checkout():
        r = _git(["fetch", "--quiet", "origin", "main"], timeout=60)
        if r.returncode:
            raise OSError("git fetch failed: " + (r.stderr.strip().splitlines() or ["?"])[-1])
        text = _git(["show", "FETCH_HEAD:steam_snapshot/__init__.py"]).stdout
    else:
        import urllib.request
        req = urllib.request.Request(VERSION_URL, headers={"User-Agent": f"steam-snapshot/{__version__}"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            text = r.read(64 * 1024).decode("utf-8", "replace")
    m = VERSION_RE.search(text)
    if not m:
        raise ValueError("no __version__ in the published file")
    return m.group(1)


def _git(args, timeout=30):
    # never prompt for a login: this runs unattended, often without a console
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never",
           "GIT_SSH_COMMAND": os.environ.get("GIT_SSH_COMMAND") or "ssh -o BatchMode=yes"}
    kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    return subprocess.run([find_git() or "git", *args], cwd=config.PROJECT_DIR, capture_output=True,
                          text=True, timeout=timeout, env=env, **kw)


def git_pull():
    """Fast-forward a clean checkout of main. Returns (ok, reason)."""
    if not find_git():
        return False, "git is not installed"
    if not is_checkout():
        return False, "the folder is not a git checkout"
    try:
        branch = _git(["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
        if branch != "main":
            return False, f"the checkout is on branch {branch or '?'}, not main"
        if _git(["status", "--porcelain", "--untracked-files=no"]).stdout.strip():
            return False, "the folder has local changes"
        r = _git(["pull", "--ff-only", "--quiet", "origin", "main"], timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"git failed: {e}"
    if r.returncode:
        return False, "git pull failed: " + (r.stderr.strip().splitlines() or ["?"])[-1]
    return True, ""


def status(cfg):
    """What the dashboard shows, without touching the network."""
    state = read_json(os.path.join(cfg["data_dir"], STATE_NAME)) or {}
    enabled = cfg["online"]["enabled"] and cfg["updates"]["check"]
    latest = state.get("latest")
    return {"running": __version__, "latest": latest if enabled else None,
            "available": bool(enabled and latest and newer(latest, disk_version() or __version__)),
            "auto": bool(enabled and cfg["updates"]["auto"]),
            "status": state.get("status", "unknown") if enabled else "off",
            "reason": state.get("reason", "")}


def run(cfg, now=None, fetch=latest_version, pull=git_pull):
    """Check (throttled) and update if allowed. Returns the stored state."""
    path = os.path.join(cfg["data_dir"], STATE_NAME)
    state = read_json(path) or {}
    if not (cfg["online"]["enabled"] and cfg["updates"]["check"]):
        return state
    now = time.time() if now is None else now
    if state.get("checked") is not None and now - float(state["checked"]) < CHECK_EVERY:
        return state
    current = disk_version() or __version__
    state = {"checked": now, "latest": state.get("latest"), "status": "current", "reason": ""}
    try:
        state["latest"] = fetch()
    except Exception as e:  # offline, GitHub down, ...: try again next time
        state.update(status="error", reason=f"update check failed: {e}")
        write_json(path, state)
        return state
    if newer(state["latest"], current):
        if cfg["updates"]["auto"]:
            ok, reason = pull()
            if ok:
                state["status"] = "updated"
                log_line(cfg["data_dir"], f"updated {current} -> {state['latest']}")
            else:
                state.update(status="manual", reason=reason)
                log_line(cfg["data_dir"], f"update {state['latest']} available, not applied: {reason}")
        else:
            state["status"] = "available"
    write_json(path, state)
    return state
