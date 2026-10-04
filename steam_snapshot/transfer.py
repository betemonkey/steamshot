"""Export and import: carry a library's history to another PC, keep a backup,
or bring in a history recorded by another tool.

An export is one zip:

  steamshot-export.json              what this is (tool, version, accounts)
  <steamid64>/history.json           playtime gained per day per game
  <steamid64>/names.json             cached store names, if any
  <steamid64>/art.json               cached artwork URLs, if any
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
import zlib
from datetime import date, datetime, timedelta

from . import __version__
from .snapshot import (DATE_FILE, SnapshotError, account_dir, clean_art, clean_names,
                       load_history, read_json, valid_day, write_json)

MANIFEST = "steamshot-export.json"
ACCOUNT_RE = re.compile(r"\A[0-9]{1,20}\Z")
MAX_MEMBER = 8 * 1024 * 1024      # a snapshot of a 10,000-game library is ~2 MB
MAX_TOTAL = 512 * 1024 * 1024     # all members together, uncompressed
MAX_MEMBERS = 20000               # fifty years of daily snapshots
MAX_MINUTES = 10 ** 7             # per game per day; anything bigger is not playtime
ZIP_ERRORS = (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, NotImplementedError,
              zlib.error, EOFError, OSError)


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
            for name in ("history.json", "names.json", "art.json"):
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

def latest_day():
    """The last date an import may carry: tomorrow, to allow for time zones.
    A snapshot dated further ahead would stay "latest" for good."""
    return (date.today() + timedelta(days=1)).isoformat()


def _int(v, lo=0, hi=2 ** 53):
    """v as an int in [lo, hi], or None. Accepts ints and digit strings,
    never bools, floats or anything that only looks like a number."""
    if isinstance(v, bool):
        return None
    if isinstance(v, str) and re.fullmatch(r"[0-9]{1,16}", v):
        v = int(v)
    return v if isinstance(v, int) and lo <= v <= hi else None


def _str(v, limit=500):
    return v[:limit] if isinstance(v, str) else ""


def clean_history(hist):
    """The usable part of a history document, or a TransferError."""
    if not isinstance(hist, dict) or not isinstance(hist.get("days"), dict):
        raise TransferError("not a history file: it needs a \"days\" object of YYYY-MM-DD dates")
    days, end = {}, latest_day()
    for day, apps in hist["days"].items():
        if not valid_day(day) or day > end or not isinstance(apps, dict):
            continue
        row = {}
        for appid, mins in apps.items():
            appid, mins = _int(appid, 1, 2 ** 32), _int(mins, 1, MAX_MINUTES)
            if appid and mins:
                row[str(appid)] = mins
        if row:
            days[day] = row
    since = hist.get("since")
    if not (valid_day(since) and since <= end):
        since = None
    if days and (since is None or since > min(days)):
        since = min(days)
    return {"since": since, "days": days}


GAME_INTS = ("minutes", "lastPlayed", "bytes")
GAME_FLAGS = ("installed", "owned", "hidden", "favourite")


def clean_snapshot(doc):
    """An imported snapshot rebuilt from the fields the dashboard uses, each
    of the right type, or None if it isn't one. Everything in it ends up on
    the dashboard, so nothing is taken on trust."""
    if not isinstance(doc, dict) or not isinstance(doc.get("games"), list):
        return None
    games = []
    for g in doc["games"]:
        appid = _int(g.get("appid"), 1, 2 ** 32) if isinstance(g, dict) else None
        if not appid:
            continue
        game = {"appid": appid, "name": _str(g.get("name")) or f"App {appid}", "type": _str(g.get("type"), 40)}
        for k in GAME_INTS:
            game[k] = _int(g.get(k)) or 0
        for k in GAME_FLAGS:
            if isinstance(g.get(k), bool):
                game[k] = g[k]
        cols = g.get("collections")
        if isinstance(cols, list):
            cols = [_str(c, 200) for c in cols if isinstance(c, str) and c.strip()]
            if cols:
                game["collections"] = cols
        games.append(game)
    wishlist = []
    for w in doc.get("wishlist") if isinstance(doc.get("wishlist"), list) else []:
        appid = _int(w.get("appid"), 1, 2 ** 32) if isinstance(w, dict) else None
        if appid:
            iso = w.get("releaseISO")
            wishlist.append({"appid": appid, "name": _str(w.get("name")) or f"App {appid}",
                             "added": w["added"] if valid_day(w.get("added")) else "",
                             "priority": _int(w.get("priority")) or 0,
                             "releaseISO": iso if valid_day(iso) else "",
                             "release": _str(w.get("release"), 80), "comingSoon": w.get("comingSoon") is True})
    acct = doc.get("account") if isinstance(doc.get("account"), dict) else {}
    sources = doc.get("sources") if isinstance(doc.get("sources"), dict) else {}
    out = {"schema": _int(doc.get("schema")) or 1, "tool": _str(doc.get("tool"), 80),
           "taken": _str(doc.get("taken"), 40), "date": _str(doc.get("date"), 10),
           "account": {"steamid64": _str(acct.get("steamid64"), 20),
                       "accountId": _int(acct.get("accountId")) or 0,
                       "persona": _str(acct.get("persona"), 200)},
           "sources": {str(k)[:40]: v for k, v in sources.items()
                       if isinstance(v, (bool, int)) and not isinstance(v, float)},
           "games": games, "wishlist": wishlist,
           "wishlistStatus": _str(doc.get("wishlistStatus"), 20),
           "notes": [_str(n) for n in doc.get("notes") or [] if isinstance(n, str)][:50]
           if isinstance(doc.get("notes"), list) else []}
    if doc.get("demo") is True:
        out["demo"] = True
    return out


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
    try:
        local = load_history(path)  # a damaged local file is moved aside, not merged over
    except SnapshotError as e:
        raise TransferError(str(e)) from e
    merged, added = merge_history(local, incoming)
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
        with open(path, encoding="utf-8-sig") as fh:  # -sig: Windows tools often add a BOM
            doc = json.load(fh)
    except (OSError, ValueError, RecursionError) as e:
        raise TransferError(f"{path} is neither a Steamshot export (.zip) nor a JSON history file: {e}") from e
    if not account:
        raise TransferError("a bare history file needs the account it belongs to (--account)")
    if not ACCOUNT_RE.match(account):
        raise TransferError(f"not a SteamID64: {account}")
    incoming = clean_history(doc)
    added, since = _merge_into(data_dir, account, incoming)
    return [{"account": account, "days": added, "snapshots": 0, "since": since}]


def _read_zip(path, only):
    """{account: {"history", "names", "art", "snapshots": {day: doc}}}, every
    document already cleaned. Nothing is written until the whole zip has
    been read, so a damaged one changes nothing."""
    found, total, end = {}, 0, latest_day()
    try:
        with zipfile.ZipFile(path) as z:
            infos = z.infolist()
            if len(infos) > MAX_MEMBERS:
                raise TransferError(f"{path} has {len(infos)} files - more than any export would")
            for info in infos:
                if info.is_dir() or info.filename == MANIFEST:
                    continue
                # accept exactly the layout export() writes; anything else (absolute
                # paths, "..", other files) is ignored, never written
                parts = info.filename.split("/")
                day = DATE_FILE.match(parts[2]) if len(parts) == 3 else None
                ok = (len(parts) == 2 and parts[1] in ("history.json", "names.json", "art.json")) or \
                     (day and parts[1] == "snapshots" and valid_day(day.group(1)) and day.group(1) <= end)
                if not ok or not ACCOUNT_RE.match(parts[0]) or info.file_size > MAX_MEMBER:
                    continue
                if only and parts[0] != only:
                    continue
                total += info.file_size
                if total > MAX_TOTAL:
                    raise TransferError(f"{path} unpacks to more than {MAX_TOTAL // 2 ** 20} MB")
                try:
                    doc = json.loads(z.read(info).decode("utf-8"))
                except (ValueError, UnicodeDecodeError, RecursionError):
                    continue
                acct = found.setdefault(parts[0], {"history": None, "names": {}, "art": {}, "snapshots": {}})
                if parts[1] == "history.json":
                    try:
                        acct["history"] = clean_history(doc)
                    except TransferError:
                        pass
                elif parts[1] == "names.json":
                    acct["names"] = clean_names(doc)
                elif parts[1] == "art.json":
                    acct["art"] = clean_art(doc)
                else:
                    snap = clean_snapshot(doc)
                    if snap is not None:
                        acct["snapshots"].setdefault(day.group(1), snap)
    except ZIP_ERRORS as e:
        raise TransferError(f"{path} could not be read as a Steamshot export: {e}") from e
    return found


def _import_zip(data_dir, path, only=None):
    found = _read_zip(path, only)
    if not found:
        raise TransferError(f"{path} has no Steamshot data" + (f" for account {only}" if only else ""))
    out = []
    for acct, got in sorted(found.items()):
        base = account_dir(data_dir, acct)
        snaps = 0
        for day, doc in sorted(got["snapshots"].items()):
            target = os.path.join(base, "snapshots", f"{day}.json")
            if not os.path.exists(target):
                write_json(target, doc)
                snaps += 1
        added, since = (0, None)
        if got["history"] is not None:
            added, since = _merge_into(data_dir, acct, got["history"])
        for fname, doc, clean in (("names.json", got["names"], clean_names), ("art.json", got["art"], clean_art)):
            if doc:  # caches: what is already here wins
                cpath = os.path.join(base, fname)
                write_json(cpath, {**doc, **clean(read_json(cpath, {}))})
        out.append({"account": acct, "days": added, "snapshots": snaps, "since": since})
    return out
