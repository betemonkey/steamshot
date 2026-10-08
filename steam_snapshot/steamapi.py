"""The "From your Steam profile" block: only with the optional Web API key.

The scheduled snapshot run (not the page) fetches it and writes two files
into the account folder:

  steamapi.json        what the dashboard shows: hours per device, the last
                       two weeks, achievement totals, "closest to 100%" and
                       the latest unlocks. Served through clean() only.
  steamapi-cache.json  per-game achievement counts and icon maps, so a run
                       only asks Steam about games that changed.

Achievements cost one call per game, so they are fetched gently: a game is
asked again only when it was played since the last ask (its last-played time
moved) or the answer is older than ACH_MAX_AGE; at most ACH_PER_RUN games per
run, played-since first, then never asked (most played first), then stale.
Only played games with community stats are asked. A game Steam says has no
stats is remembered as such; a network failure is not cached at all.

Neither file holds the key, and neither is exported (transfer.py): they are
a cache of what Steam says, rebuilt by the next run.
"""
import os
import re
import time

from .snapshot import read_json, write_json

ACH_PER_RUN = 25
ACH_MAX_AGE = 7 * 86400
ICON_MAX_AGE = 30 * 86400
CLOSEST_MIN = 10      # fewer achievements than this can't top "closest to 100%"
CLOSEST_N = 4
LATEST_N = 3
SUMMARY = "steamapi.json"
CACHE = "steamapi-cache.json"
DEVICES = ("windows", "deck", "linux", "mac", "untracked")
APPID_KEY = re.compile(r"\A[0-9]{1,10}\Z")


def _int(v):
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0


def _str(v, n=200):
    return v.strip()[:n] if isinstance(v, str) else ""


def devices(owned):
    """Minutes per device across the library. "untracked" is the part of each
    game's total that no device counter holds - hours from before Steam
    counted per device - so the parts always add up to the total."""
    out = dict.fromkeys(DEVICES, 0)
    for g in owned.values():
        p = g.get("platforms") or {}
        split = sum(_int(p.get(k)) for k in ("windows", "mac", "linux", "deck"))
        for k in ("windows", "mac", "linux", "deck"):
            out[k] += _int(p.get(k))
        out["untracked"] += max(0, _int(g.get("minutes")) - split)
    return out


def to_fetch(owned, games_cache, now):
    """App ids to ask about this run, in priority order, at most ACH_PER_RUN."""
    moved, new, stale = [], [], []
    for a, g in owned.items():
        if not (g.get("stats") and g.get("minutes")):
            continue
        c = games_cache.get(str(a))
        if c is None:
            new.append(a)
        elif c.get("lp") != g.get("lastPlayed"):
            moved.append(a)
        elif now - _int(c.get("t")) > ACH_MAX_AGE:
            stale.append(a)
    moved.sort(key=lambda a: -_int(owned[a].get("lastPlayed")))
    new.sort(key=lambda a: -_int(owned[a].get("minutes")))
    stale.sort(key=lambda a: _int(games_cache[str(a)].get("t")))
    return (moved + new + stale)[:ACH_PER_RUN]


def summarise(owned, games_cache, icons, now):
    """The dashboard's view, from the owned list and the per-game cache."""
    played = {a: g for a, g in owned.items() if g.get("minutes")}
    unlocked = total = complete = small = 0
    rows, latest = [], []
    for a, g in played.items():
        c = games_cache.get(str(a)) or {}
        n, u = _int(c.get("n")), _int(c.get("u"))
        if not n:
            continue
        unlocked, total = unlocked + min(u, n), total + n
        if n < CLOSEST_MIN:
            small += 1
        elif u >= n:
            complete += 1
        elif u:
            rows.append({"appid": a, "name": g.get("name") or "", "unlocked": u, "total": n})
        for r in c.get("recent") or []:
            if isinstance(r, list) and len(r) == 3 and _int(r[1]):
                latest.append({"appid": a, "game": g.get("name") or "", "api": r[0],
                               "name": _str(r[2]) or _str(r[0]), "t": _int(r[1])})
    # closest to 100%: fewest missing first, then the higher share
    rows.sort(key=lambda r: (r["total"] - r["unlocked"], -r["unlocked"] / r["total"]))
    latest.sort(key=lambda r: -r["t"])
    latest = latest[:LATEST_N]
    for r in latest:
        r["icon"] = ((icons.get(str(r["appid"])) or {}).get("icons") or {}).get(r.pop("api"), "")
    two = sorted(({"appid": a, "name": g.get("name") or "", "minutes": _int(g.get("minutes2w"))}
                  for a, g in owned.items() if _int(g.get("minutes2w"))), key=lambda r: -r["minutes"])
    return {"schema": 1, "fetched": int(now), "devices": devices(owned), "twoWeeks": two,
            "achievements": {"unlocked": unlocked, "total": total, "complete": complete,
                             "small": small},
            "closest": rows[:CLOSEST_N], "latest": latest}


def update(acct_dir, steamid64, api_key, owned, notes, now=None, language="english"):
    """Refresh the cache a little and rewrite steamapi.json. Never raises for
    a Steam failure: what could not be asked waits for the next run."""
    from . import store
    now = now or time.time()
    cache = read_json(os.path.join(acct_dir, CACHE)) or {}
    if not isinstance(cache, dict):
        cache = {}
    games_cache = {k: v for k, v in (cache.get("games") or {}).items()
                   if APPID_KEY.match(str(k)) and isinstance(v, dict)}
    icons = {k: v for k, v in (cache.get("icons") or {}).items()
             if APPID_KEY.match(str(k)) and isinstance(v, dict)}
    asked = failed = 0
    for a in to_fetch(owned, games_cache, now):
        try:
            res = store.player_achievements(steamid64, a, api_key, language)
        except store.StoreError as e:
            failed += 1
            if failed >= 3:  # Steam is down or the profile went private: stop asking
                notes.append(f"achievements: {e}; will retry next run")
                break
            continue
        asked += 1
        rec = {"t": int(now), "lp": owned[a].get("lastPlayed", 0)}
        if res:
            rec.update(n=res["total"], u=res["unlocked"], recent=res["recent"])
        games_cache[str(a)] = rec
    out = summarise(owned, games_cache, icons, now)
    # icons only for the games in "latest unlocks"; re-asked after 30 days or
    # when an unlocked achievement is missing from the cached map
    keep = {}
    for r in out["latest"]:
        k = str(r["appid"])
        ic = icons.get(k)
        fresh = ic and now - _int(ic.get("t")) <= ICON_MAX_AGE and r["icon"]
        if not fresh:
            try:
                ic = {"t": int(now), "icons": store.achievement_icons(r["appid"], api_key, language)}
            except store.StoreError:
                ic = ic or {"t": 0, "icons": {}}
        keep[k] = ic
    if keep != {k: icons.get(k) for k in keep}:
        out = summarise(owned, games_cache, keep, now)
    write_json(os.path.join(acct_dir, CACHE), {"games": games_cache, "icons": keep})
    write_json(os.path.join(acct_dir, SUMMARY), out)
    return {"asked": asked, "games": len(games_cache)}


def clean(data):
    """steamapi.json rebuilt field by field: the file is treated as hostile
    (it is read straight into the page). None when there is nothing usable."""
    from .store import clean_icon
    if not isinstance(data, dict) or data.get("schema") != 1:
        return None

    def appid(v):
        return v if isinstance(v, int) and not isinstance(v, bool) and 0 < v < 2 ** 32 else 0

    dev = data.get("devices") if isinstance(data.get("devices"), dict) else {}
    ach = data.get("achievements") if isinstance(data.get("achievements"), dict) else {}
    out = {"fetched": _int(data.get("fetched")),
           "devices": {k: _int(dev.get(k)) for k in DEVICES},
           "achievements": {k: _int(ach.get(k)) for k in ("unlocked", "total", "complete", "small")},
           "twoWeeks": [], "closest": [], "latest": []}
    for r in (data.get("twoWeeks") if isinstance(data.get("twoWeeks"), list) else [])[:50]:
        if isinstance(r, dict) and appid(r.get("appid")) and _int(r.get("minutes")):
            out["twoWeeks"].append({"appid": r["appid"], "name": _str(r.get("name")),
                                    "minutes": _int(r.get("minutes"))})
    for r in (data.get("closest") if isinstance(data.get("closest"), list) else [])[:CLOSEST_N]:
        if isinstance(r, dict) and appid(r.get("appid")) and _int(r.get("total")):
            out["closest"].append({"appid": r["appid"], "name": _str(r.get("name")),
                                   "unlocked": min(_int(r.get("unlocked")), _int(r.get("total"))),
                                   "total": _int(r.get("total"))})
    for r in (data.get("latest") if isinstance(data.get("latest"), list) else [])[:LATEST_N]:
        if isinstance(r, dict) and appid(r.get("appid")):
            out["latest"].append({"appid": r["appid"], "game": _str(r.get("game")),
                                  "name": _str(r.get("name")), "t": _int(r.get("t")),
                                  "icon": clean_icon(r.get("icon"))})
    return out
