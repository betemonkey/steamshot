"""Export and import: carry a library's history to another PC, keep a backup,
or bring in a history recorded by another tool.

An export is one zip:

  steamshot-export.json              what this is (tool, version, accounts)
  <steamid64>/history.json           playtime gained per day per game
  <steamid64>/names.json             cached store names, if any
  <steamid64>/snapshots/YYYY-MM-DD.json

`import` takes such a zip, or a bare history file in the same shape as
history.json ({"since": ..., "days": {"YYYY-MM-DD": {"<appid>": minutes}}}).
Merging never overwrites what this install recorded itself: a day on or after
this install's own first day is kept as it is, and a snapshot file that
already exists is left alone. Imported days only fill in what came before.
"last" (the totals the next snapshot diffs against) is never imported, so an
import can't create a fake spike on the next run.
"""
import json
import os
import re
import zipfile
from datetime import datetime

from . import __version__
from .snapshot import DATE_FILE, account_dir, read_json, write_json

MANIFEST = "steamshot-export.json"
ACCOUNT_RE = re.compile(r"^\d{1,20}$")
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_MEMBER = 50 * 1024 * 1024  # no single file in an export is anywhere near this


class TransferError(Exception):
    pass


def accounts(data_dir):
    try:
        names = os.listdir(data_dir)
    except OSError:
        return []
    return sorted(n for n in names if ACCOUNT_RE.match(n) and os.path.isdir(os.path.join(data_dir, n)))


# ---------- export ----------

def export(data_dir, out_path, account=None):
    """Write every account (or one) to a zip. Returns a summary dict."""
    chosen = [account] if account else accounts(data_dir)
    if account and account not in accounts(data_dir):
        raise TransferError(f"no data for account {account} in {data_dir}")
    if not chosen:
        raise TransferError(f"nothing to export: no snapshots in {data_dir}")
    out_path = os.path.abspath(out_path)
    tmp = out_path + ".tmp"
    counts = {}
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(MANIFEST, json.dumps({
            "tool": "steamshot", "version": __version__,
            "exported": datetime.now().astimezone().isoformat(timespec="seconds"),
            "accounts": chosen}, indent=1))
        for acct in chosen:
            base = account_dir(data_dir, acct)
            n = 0
            for name in ("history.json", "names.json"):
                if os.path.isfile(os.path.join(base, name)):
                    z.write(os.path.join(base, name), f"{acct}/{name}")
            snaps = os.path.join(base, "snapshots")
            for name in sorted(os.listdir(snaps)) if os.path.isdir(snaps) else []:
                if DATE_FILE.match(name):
                    z.write(os.path.join(snaps, name), f"{acct}/snapshots/{name}")
                    n += 1
            counts[acct] = n
    os.replace(tmp, out_path)
    return {"path": out_path, "snapshots": counts}


# ---------- import ----------

def clean_history(hist):
    """The usable part of a history document, or a TransferError."""
    if not isinstance(hist, dict) or not isinstance(hist.get("days"), dict):
        raise TransferError("not a history file: it needs a \"days\" object of YYYY-MM-DD dates")
    days = {}
    for day, apps in hist["days"].items():
        if not DAY_RE.match(str(day)) or not isinstance(apps, dict):
            continue
        row = {}
        for appid, mins in apps.items():
            try:
                appid, mins = int(appid), int(mins)
            except (TypeError, ValueError):
                continue
            if appid > 0 and mins > 0:
                row[str(appid)] = mins
        if row:
            days[day] = row
    since = hist.get("since")
    if not (isinstance(since, str) and DAY_RE.match(since)):
        since = min(days) if days else None
    return {"since": since, "days": days}


def merge_history(local, incoming):
    """(merged, days_added). Days this install recorded itself always win."""
    local = local or {}
    lsince = local.get("since")
    days = dict(local.get("days") or {})
    added = 0
    for day, row in incoming["days"].items():
        if (lsince and day >= lsince) or day in days:
            continue
        days[day] = row
        added += 1
    starts = [s for s in (lsince, incoming.get("since")) if s]
    merged = {"since": min(starts) if starts else None,
              "days": dict(sorted(days.items())),
              "last": local.get("last") or {}}
    return merged, added


def _merge_into(data_dir, acct, incoming):
    path = os.path.join(account_dir(data_dir, acct), "history.json")
    merged, added = merge_history(read_json(path, {}), incoming)
    if added or not os.path.exists(path):
        write_json(path, merged)
    return added, merged["since"]


def import_file(data_dir, path, account=None):
    """Import a Steamshot export zip, or a bare history file into `account`.
    Returns a list of per-account summaries."""
    if not os.path.isfile(path):
        raise TransferError(f"file not found: {path}")
    if zipfile.is_zipfile(path):
        return _import_zip(data_dir, path, account)
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as e:
        raise TransferError(f"{path} is neither a Steamshot export (.zip) nor a JSON history file: {e}") from e
    if not account:
        raise TransferError("a bare history file needs the account it belongs to (--account)")
    if not ACCOUNT_RE.match(account):
        raise TransferError(f"not a SteamID64: {account}")
    incoming = clean_history(doc)
    added, since = _merge_into(data_dir, account, incoming)
    return [{"account": account, "days": added, "snapshots": 0, "since": since}]


def _import_zip(data_dir, path, only=None):
    out = {}
    with zipfile.ZipFile(path) as z:
        members = {}
        for info in z.infolist():
            if info.is_dir() or info.filename == MANIFEST:
                continue
            # accept exactly the layout export() writes; anything else (absolute
            # paths, "..", other files) is ignored, never written
            parts = info.filename.split("/")
            ok = (len(parts) == 2 and parts[1] in ("history.json", "names.json")) or \
                 (len(parts) == 3 and parts[1] == "snapshots" and DATE_FILE.match(parts[2]))
            if not ok or not ACCOUNT_RE.match(parts[0]) or info.file_size > MAX_MEMBER:
                continue
            if only and parts[0] != only:
                continue
            members.setdefault(parts[0], []).append((parts, info))
        if not members:
            raise TransferError(f"{path} has no Steamshot data" + (f" for account {only}" if only else ""))

        def load(info):
            try:
                return json.loads(z.read(info).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return None

        for acct, items in sorted(members.items()):
            base = account_dir(data_dir, acct)
            snaps = 0
            history = names = None
            for parts, info in items:
                if parts[1] == "history.json":
                    history = load(info)
                elif parts[1] == "names.json":
                    names = load(info)
                else:
                    target = os.path.join(base, "snapshots", parts[2])
                    doc = load(info)
                    if os.path.exists(target) or not isinstance(doc, dict) or not isinstance(doc.get("games"), list):
                        continue
                    write_json(target, doc)
                    snaps += 1
            added, since = (0, None)
            if history is not None:
                added, since = _merge_into(data_dir, acct, clean_history(history))
            if isinstance(names, dict):
                npath = os.path.join(base, "names.json")
                current = read_json(npath, {}) or {}
                write_json(npath, {**names, **current})
            out[acct] = {"account": acct, "days": added, "snapshots": snaps, "since": since}
    return list(out.values())
