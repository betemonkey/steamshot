"""Take a snapshot and keep the on-disk history.

Layout under the data folder (one sub-folder per Steam account):

  steam-snapshot.log                 one line per run, for checking a scheduler
  <steamid64>/snapshots/YYYY-MM-DD.json
                                     the library as of that day; later runs on
                                     the same day replace it
  <steamid64>/history.json           playtime gained per day per game
  <steamid64>/names.json             names the store had to resolve (cache)
  <steamid64>/art.json               where each game's artwork lives (cache)

Steam keeps no session history at all - only a running total per game and one
last-played date. The per-day series is built by diffing totals between runs,
so it starts the day the tool is first run and cannot be backfilled.
"""
import json
import os
import re
import tempfile
import time
from datetime import date, datetime, timedelta

from . import __version__, steamfiles

SCHEMA = 1
DATE_FILE = re.compile(r"\A([0-9]{4}-[0-9]{2}-[0-9]{2})\.json\Z")
APPID_KEY = re.compile(r"\A[0-9]{1,10}\Z")
# the only artwork addresses the dashboard will put in its HTML
ART_URL = re.compile(r"\Ahttps://shared\.akamai\.steamstatic\.com/store_item_assets/steam/apps/[0-9]+/"
                     r"[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*\.(jpg|png|webp)(\?t=[0-9]+)?\Z")
LOG_NAME = "steam-snapshot.log"
LOG_MAX_BYTES = 512 * 1024
LOCK_NAME = "snapshot.lock"
LOCK_STALE = 15 * 60    # a lock older than this was left by a run that died
ART_REFRESH_DAYS = 30   # art paths change when a store page is updated
ART_MAX_PER_RUN = 400   # a first run with a huge library catches up over a few runs


class SnapshotError(Exception):
    pass


def write_json(path, data):
    """Write via a temp file so a reader never sees half a file. The temp
    name is unique, so two runs at once never write into the same one."""
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            # ASCII escapes, so a broken string (a lone surrogate) cannot fail mid-write
            json.dump(data, fh, separators=(",", ":"))
            fh.flush()
            os.fsync(fh.fileno())
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:  # Windows: the dashboard has the file open
                if attempt == 4:
                    raise
                time.sleep(0.2)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError, RecursionError):
        return default


def valid_day(s):
    """True for a real YYYY-MM-DD date."""
    if not (isinstance(s, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", s)):
        return False
    try:
        date.fromisoformat(s)
    except ValueError:
        return False
    return True


def clean_names(cache):
    """names.json with anything that is not {app id: name} dropped."""
    if not isinstance(cache, dict):
        return {}
    return {k: v for k, v in cache.items() if APPID_KEY.match(str(k)) and isinstance(v, str)}


def clean_art(cache):
    """art.json with anything that is not {app id: {"t": time, kind: Steam
    image URL}} dropped. These URLs go into the dashboard's HTML, and an
    imported file has not been through store.art_of's checks."""
    if not isinstance(cache, dict):
        return {}
    out = {}
    for k, rec in cache.items():
        if not (APPID_KEY.match(str(k)) and isinstance(rec, dict)):
            continue
        t = rec.get("t")
        ok = isinstance(t, (int, float)) and not isinstance(t, bool) and 0 <= t < 1e11
        row = {"t": int(t) if ok else 0}
        for kind, url in rec.items():
            if kind != "t" and isinstance(url, str) and ART_URL.match(url):
                row[str(kind)] = url
        out[str(k)] = row
    return out


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
    cache = clean_names(read_json(cache_path, {}))
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


def resolve_art(appids, acct_dir, online, notes, now=None):
    """Look up artwork URLs for games not looked up in the last
    ART_REFRESH_DAYS; cached in art.json. A game Steam has no art for is
    remembered too, so it isn't asked about again on every run."""
    from . import store
    now = time.time() if now is None else now
    path = os.path.join(acct_dir, "art.json")
    cache = clean_art(read_json(path, {}))
    due = [a for a in dict.fromkeys(str(a) for a in appids)
           if a not in cache or now - cache[a]["t"] > ART_REFRESH_DAYS * 86400]
    if not due:
        return
    due = due[:ART_MAX_PER_RUN]
    try:
        items = store.get_items(due, online["country"], online["language"], assets=True)
    except store.StoreError as e:
        notes.append(f"art lookup failed: {e}")
        return
    for a in due:
        cache[a] = {"t": int(now), **store.art_of(items.get(int(a)))}
    write_json(path, cache)


def build_wishlist(account, games, previous, online, notes):
    """(wishlist rows, status). Status is ok / private / error; on anything
    but ok the previous snapshot's list is carried over, so a privacy toggle
    or an outage never blanks it."""
    from . import store
    kept = (previous or {}).get("wishlist")
    kept = [w for w in kept if isinstance(w, dict) and isinstance(w.get("appid"), int)] \
        if isinstance(kept, list) else []
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

def load_history(path):
    """history.json, with "days" and "last" always dicts. A missing file is a
    fresh start. One that can't be read right now raises SnapshotError (the
    next run tries again); one that is damaged is moved aside, never
    overwritten, because Steam cannot give those days back."""
    if not os.path.exists(path):
        return {"days": {}, "last": {}}
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, ValueError) as e:
        if isinstance(e, OSError):
            raise SnapshotError(f"cannot read {path}: {e}") from e
        text = ""  # not UTF-8: damaged
    try:
        hist = json.loads(text)
        if not isinstance(hist, dict):
            raise ValueError("not a JSON object")
    except (ValueError, RecursionError) as e:
        aside = f"{path}.damaged-{datetime.now():%Y%m%d-%H%M%S}"
        try:
            os.replace(path, aside)
        except OSError as e2:
            raise SnapshotError(f"{path} is damaged and could not be moved aside: {e2}") from e2
        log_line(os.path.dirname(os.path.dirname(path)),
                 f"history.json was damaged ({e}); kept as {os.path.basename(aside)}, starting a new one")
        return {"days": {}, "last": {}}
    days, last = hist.get("days"), hist.get("last")
    hist["days"] = days if isinstance(days, dict) else {}
    hist["last"] = {k: v for k, v in last.items() if isinstance(v, int)} if isinstance(last, dict) else {}
    return hist


def update_history(acct_dir, minutes_by_app, today, keep_days):
    """Add today's playtime gains. A game seen for the first time adds 0, so a
    new purchase or a first run never lands as a fake spike."""
    path = os.path.join(acct_dir, "history.json")
    hist = load_history(path)
    last = hist["last"]
    days = hist["days"]
    hist.setdefault("since", today)
    bucket = days.get(today)
    bucket = days[today] = bucket if isinstance(bucket, dict) else {}
    for appid, mins in minutes_by_app.items():
        key = str(appid)
        prev = last.get(key)
        if prev is not None and mins > prev:
            had = bucket.get(key)
            bucket[key] = (had if isinstance(had, int) else 0) + (mins - prev)
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

def acquire_lock(data_dir):
    """A lock file per data folder, so a manual run and the scheduled one
    never write the same history at once. Returns its path, or None while
    another run holds it."""
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, LOCK_NAME)
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(path) > LOCK_STALE:
                    os.remove(path)
                    continue
            except OSError:
                continue
            return None
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return path
    return None


def release_lock(path):
    try:
        os.remove(path)
    except OSError:
        pass


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
    notes = []
    owned = fetch_owned(cfg, account, notes)
    owned_apps = set(owned) if owned is not None else None
    games, sources = steamfiles.read_library(
        root, account, snap_cfg["hide_appids"], snap_cfg["include_unowned"], owned_apps)
    if not sources["localconfig"] or not games:
        raise SnapshotError("no games could be read - refusing to write an empty snapshot")

    data_dir = cfg["data_dir"]
    lock = acquire_lock(data_dir)
    if not lock:
        raise SnapshotError("another snapshot run is in progress")
    try:
        summary = _write(cfg, now, today, account, games, sources, notes)
        if owned is not None:
            # the "From your Steam profile" block (key only); its failures
            # never cost the snapshot that was just written
            from . import steamapi
            try:
                summary["steamapi"] = steamapi.update(
                    account_dir(data_dir, account["steamid64"]), account["steamid64"],
                    cfg["online"]["steam_api_key"], owned, summary["notes"],
                    now.timestamp(), cfg["online"]["language"])
            except OSError as e:
                summary["notes"].append(f"profile block not written: {e}")
        return summary
    finally:
        release_lock(lock)


def fetch_owned(cfg, account, notes):
    """{owned app id: details} from the Steam Web API when the optional key is
    set and online calls are allowed; None otherwise or when the call fails
    (the licence cache decides then, as without a key)."""
    online = cfg["online"]
    if not (online["enabled"] and online["steam_api_key"]):
        return None
    from . import store
    try:
        return store.owned_details(account["steamid64"], online["steam_api_key"])
    except store.StoreError as e:
        notes.append(f"owned games: {e}; used the licence cache instead")
        return None


def _write(cfg, now, today, account, games, sources, notes=None):
    data_dir, snap_cfg = cfg["data_dir"], cfg["snapshot"]
    acct_dir = account_dir(data_dir, account["steamid64"])
    # a dated-in-the-future file (an odd import) is never "the previous run"
    dates = [d for d in snapshot_dates(acct_dir) if d <= today]
    previous = read_json(os.path.join(acct_dir, "snapshots", f"{dates[-1]}.json")) if dates else None
    if not isinstance(previous, dict):
        previous = None

    notes = list(notes or [])
    wishlist, wl_status = [], "off"
    online = cfg["online"]
    if online["enabled"]:
        resolve_names(games, acct_dir, online, notes)
        if online["wishlist"]:
            wishlist, wl_status = build_wishlist(account, games, previous, online, notes)
        resolve_art([g["appid"] for g in games] + [w["appid"] for w in wishlist],
                    acct_dir, online, notes)
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
