"""Take a snapshot and keep the on-disk history.

Layout under the data folder (one sub-folder per Steam account):

  steam-snapshot.log                 one line per run, for checking a scheduler
  <steamid64>/snapshots/YYYY-MM-DD.json
                                     the library as of that day; later runs on
                                     the same day replace it
  <steamid64>/history.json           playtime gained per day per game
  <steamid64>/names.json             names the store had to resolve (cache)

Steam keeps no session history at all - only a running total per game and one
last-played date. The per-day series is built by diffing totals between runs,
so it starts the day the tool is first run and cannot be backfilled.
"""
import json
import os
import re
from datetime import date, datetime, timedelta

from . import __version__, steamfiles

SCHEMA = 1
DATE_FILE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.json$")
LOG_NAME = "steam-snapshot.log"
LOG_MAX_BYTES = 512 * 1024


class SnapshotError(Exception):
    pass


def write_json(path, data):
    """Write via a temp file so a reader never sees half a file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def account_dir(data_dir, steamid64):
    return os.path.join(data_dir, str(steamid64))


def snapshot_dates(acct_dir):
    """Every stored snapshot day, oldest first."""
    try:
        names = os.listdir(os.path.join(acct_dir, "snapshots"))
    except OSError:
        return []
    return sorted(m.group(1) for m in map(DATE_FILE.match, names) if m)


def log_line(data_dir, text):
    """Append to the run log; trimmed to its newer half once it grows."""
    path = os.path.join(data_dir, LOG_NAME)
    try:
        os.makedirs(data_dir, exist_ok=True)
        if os.path.exists(path) and os.path.getsize(path) > LOG_MAX_BYTES:
            with open(path, encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
            with open(path, "w", encoding="utf-8") as fh:
                fh.writelines(lines[len(lines) // 2:])
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now().astimezone().isoformat(timespec='seconds')} {text}\n")
    except OSError:
        pass


# ---------- online enrichment ----------

def resolve_names(games, acct_dir, online, notes):
    """Fill in names the local cache did not have, from the store; cached."""
    from . import store
    cache_path = os.path.join(acct_dir, "names.json")
    cache = read_json(cache_path, {}) or {}
    missing = [g["appid"] for g in games if not g["name"] and str(g["appid"]) not in cache]
    if missing:
        try:
            items = store.get_items(missing, online["country"], online["language"])
            for appid, it in items.items():
                cache[str(appid)] = str(it.get("name") or "")
            # the batched call does not know delisted apps; try those one by
            # one, capped so a big gap cannot stall a run
            for appid in [a for a in missing if str(a) not in cache][:8]:
                cache[str(appid)] = store.appdetails_name(appid)
        except store.StoreError as e:
            notes.append(f"name lookup failed: {e}")
        write_json(cache_path, cache)
    for g in games:
        if not g["name"]:
            g["name"] = cache.get(str(g["appid"])) or ""


def build_wishlist(account, games, previous, online, notes):
    """(wishlist rows, status). Status is ok / private / error; on anything
    but ok the previous snapshot's list is carried over, so a privacy toggle
    or an outage never blanks it."""
    from . import store
    kept = (previous or {}).get("wishlist") or []
    try:
        items = store.wishlist(account["steamid64"])
    except store.StoreError as e:
        notes.append(f"wishlist fetch failed: {e}")
        return kept, "error"
    if items is None:
        notes.append("wishlist not readable - is the profile's Game details setting Public?")
        return kept, "private"
    owned = {g["appid"] for g in games}
    items = [w for w in items if w["appid"] not in owned]
    try:
        details = store.get_items([w["appid"] for w in items], online["country"],
                                  online["language"], release=True)
    except store.StoreError as e:
        notes.append(f"wishlist details failed: {e}")
        details = {}
    old = {w["appid"]: w for w in kept}
    rows = []
    for w in items:
        it = details.get(w["appid"])
        if it:
            iso, text, coming = store.release_of(it)
            name = str(it.get("name") or "")
        else:
            prev = old.get(w["appid"]) or {}
            iso, text, coming = (prev.get("releaseISO", ""), prev.get("release", ""),
                                 bool(prev.get("comingSoon")))
            name = prev.get("name", "")
        rows.append({"appid": w["appid"], "name": name or f"App {w['appid']}",
                     "added": w["added"], "priority": w["priority"],
                     "releaseISO": iso, "release": text, "comingSoon": coming})
    return rows, "ok"


# ---------- history ----------

def update_history(acct_dir, minutes_by_app, today, keep_days):
    """Add today's playtime gains. A game seen for the first time adds 0, so a
    new purchase or a first run never lands as a fake spike."""
    path = os.path.join(acct_dir, "history.json")
    hist = read_json(path, {}) or {}
    last = hist.get("last") or {}
    days = hist.get("days") or {}
    hist.setdefault("since", today)
    bucket = days.setdefault(today, {})
    for appid, mins in minutes_by_app.items():
        key = str(appid)
        prev = last.get(key)
        if prev is not None and mins > prev:
            bucket[key] = bucket.get(key, 0) + (mins - prev)
        last[key] = mins
    if not bucket:
        days.pop(today, None)
    if keep_days > 0:
        cutoff = (date.fromisoformat(today) - timedelta(days=keep_days)).isoformat()
        days = {d: v for d, v in days.items() if d >= cutoff}
    hist["days"] = dict(sorted(days.items()))
    hist["last"] = last
    write_json(path, hist)
    return hist


def prune_snapshots(acct_dir, today, keep_days):
    if keep_days <= 0:
        return 0
    cutoff = (date.fromisoformat(today) - timedelta(days=keep_days)).isoformat()
    removed = 0
    for d in snapshot_dates(acct_dir):
        if d < cutoff:
            try:
                os.remove(os.path.join(acct_dir, "snapshots", f"{d}.json"))
                removed += 1
            except OSError:
                pass
    return removed


# ---------- the run ----------

def take(cfg, now=None):
    """Read the library, enrich it, write today's snapshot. Returns a summary
    dict; raises SnapshotError when there is nothing trustworthy to write."""
    now = (now or datetime.now()).astimezone()
    today = now.date().isoformat()
    root = steamfiles.find_steam_root(cfg["steam"]["path"])
    if not root:
        raise SnapshotError("Steam install not found - set [steam] path in the config")
    account = steamfiles.pick_account(root, cfg["steam"]["account"])
    if not account:
        raise SnapshotError("no Steam account found (or the configured one has never "
                            "logged in on this machine) - run `doctor`")
    snap_cfg = cfg["snapshot"]
    games, sources = steamfiles.read_library(
        root, account, snap_cfg["hide_appids"], snap_cfg["include_unowned"])
    if not sources["localconfig"] or not games:
        raise SnapshotError("no games could be read - refusing to write an empty snapshot")

    data_dir = cfg["data_dir"]
    acct_dir = account_dir(data_dir, account["steamid64"])
    dates = snapshot_dates(acct_dir)
    previous = read_json(os.path.join(acct_dir, "snapshots", f"{dates[-1]}.json")) if dates else None

    notes, wishlist, wl_status = [], [], "off"
    online = cfg["online"]
    if online["enabled"]:
        resolve_names(games, acct_dir, online, notes)
        if online["wishlist"]:
            wishlist, wl_status = build_wishlist(account, games, previous, online, notes)
    for g in games:
        g["name"] = (g["name"] or f"App {g['appid']}").strip()

    snap = {
        "schema": SCHEMA,
        "tool": f"steam-snapshot {__version__}",
        "taken": now.isoformat(timespec="seconds"),
        "date": today,
        "account": {"steamid64": account["steamid64"], "accountId": account["accountId"],
                    "persona": account["persona"]},
        "sources": sources,
        "games": games,
        "wishlist": wishlist,
        "wishlistStatus": wl_status,
        "notes": notes,
    }
    path = os.path.join(acct_dir, "snapshots", f"{today}.json")
    write_json(path, snap)
    update_history(acct_dir, {g["appid"]: g["minutes"] for g in games},
                   today, snap_cfg["keep_days"])
    pruned = prune_snapshots(acct_dir, today, snap_cfg["keep_days"])

    summary = {
        "path": path,
        "games": len(games),
        "hours": round(sum(g["minutes"] for g in games) / 60),
        "wishlist": len(wishlist),
        "wishlistStatus": wl_status,
        "pruned": pruned,
        "notes": notes,
    }
    log_line(data_dir, f"ok {account['steamid64']} {today}: {len(games)} games, "
                       f"{summary['hours']} h, wishlist {len(wishlist)} ({wl_status})"
                       + (f"; {'; '.join(notes)}" if notes else ""))
    return summary
