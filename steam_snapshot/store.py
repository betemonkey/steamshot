"""Keyless lookups against Steam's public web endpoints.

Used for four things the local files cannot answer: names for games missing
from Steam's local cache (new purchases, the whole wishlist), the wishlist
itself, release dates for wishlisted games, and where each game's artwork
lives on Steam's image server. None of these need an API key
or a login. Every call is optional - with `[online] enabled = false` this
module is never imported into a run's path and nothing leaves the machine.

Only app ids and your public SteamID64 are ever sent - plus, when the
optional key is set ([online] steam_api_key), the key itself to Steam's own
Web API for: the owned-games list (exact ownership, hours per device, last
two weeks), the account's achievements per game, achievement icons, and the
game being played right now. The key never appears in an error raised here.
"""
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from . import __version__

GET_ITEMS = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/"
GET_WISHLIST = "https://api.steampowered.com/IWishlistService/GetWishlist/v1/"
GET_OWNED = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"
GET_ACHIEVEMENTS = "https://api.steampowered.com/ISteamUserStats/GetPlayerAchievements/v1/"
GET_SCHEMA = "https://api.steampowered.com/ISteamUserStats/GetSchemaForGame/v2/"
GET_SUMMARIES = "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/"
PLATFORMS = ("windows", "mac", "linux", "deck")
# achievement icons: Steam serves the same path from several hosts; only this
# shape is accepted, and it is rebuilt on the steamstatic host the page's CSP allows
ICON_RE = re.compile(r"\Ahttps://[a-z0-9.-]+\.(?:steamstatic\.com|akamaihd\.net)"
                     r"/steamcommunity/public/images/apps/([0-9]{1,10})/([0-9a-f]{40})\.(jpg|png)\Z")
ICON_BASE = "https://cdn.akamai.steamstatic.com/steamcommunity/public/images/apps/"
APPDETAILS = "https://store.steampowered.com/api/appdetails"
USER_AGENT = f"steam-snapshot/{__version__}"
BATCH = 50
MAX_RESPONSE = 32 * 1024 * 1024  # a batch of 50 items is well under 1 MB
ART_BASE = "https://shared.akamai.steamstatic.com/store_item_assets/"
# dashboard name -> GetItems asset key
ART_KINDS = {"header": "header", "capsule": "main_capsule", "small": "small_capsule",
             "library": "library_capsule", "hero": "library_hero"}
ART_FORMAT_RE = re.compile(r"^steam/apps/\d+/\$\{FILENAME\}(\?t=\d+)?$")
ART_FILE_RE = re.compile(r"^[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*\.(jpg|png|webp)$")
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


class StoreError(Exception):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status  # the HTTP status when Steam answered with an error


def get_json(url, timeout=20):
    """The JSON object at url. Anything else - an error, a list, a huge or
    broken body - is a StoreError, so a change on Steam's side can only ever
    cost a lookup, never the snapshot."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(MAX_RESPONSE + 1)
        if len(body) > MAX_RESPONSE:
            raise ValueError("response too large")
        data = json.loads(body.decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError, RecursionError) as e:
        raise StoreError(f"{type(e).__name__}: {e}",
                         getattr(e, "code", None) if isinstance(e, urllib.error.HTTPError) else None) from e
    if not isinstance(data, dict):
        raise StoreError(f"unexpected answer from {url.split('?')[0]}")
    return data


def _dict(v):
    return v if isinstance(v, dict) else {}


def _date(ts, tz=None):
    """A datetime for a unix time, or None when it is out of range."""
    try:
        return datetime.fromtimestamp(ts, tz)
    except (OSError, OverflowError, ValueError):
        return None


def get_items(appids, country="US", language="english", release=False, assets=False):
    """{app id: store item} for a list of app ids, batched. Unknown or
    delisted ids are simply absent. Raises StoreError if Steam is unreachable."""
    ids = []
    for a in dict.fromkeys(appids):
        try:
            ids.append(int(a))
        except (TypeError, ValueError):
            continue
    out = {}
    for i in range(0, len(ids), BATCH):
        req = {"ids": [{"appid": a} for a in ids[i:i + BATCH]],
               "context": {"language": language, "country_code": country}}
        if release or assets:
            req["data_request"] = {k: True for k, on in (("include_release", release),
                                                         ("include_assets", assets)) if on}
        q = urllib.parse.urlencode({"input_json": json.dumps(req)})
        data = get_json(f"{GET_ITEMS}?{q}")
        items = _dict(data.get("response")).get("store_items")
        for it in items if isinstance(items, list) else []:
            if not isinstance(it, dict):
                continue
            try:
                appid = int(it.get("appid") or it.get("id") or 0)
            except (TypeError, ValueError):
                continue
            if appid:
                out[appid] = it
    return out


def art_of(item):
    """{kind: full URL} from a GetItems item fetched with assets=True.

    Steam now files most artwork under a per-image hash
    (steam/apps/<id>/<hash>/header.jpg), so the plain steam/apps/<id>/header.jpg
    the dashboard used to build is a 404 for newer and recently updated games.
    Everything is checked against a strict pattern: these URLs end up in the
    dashboard's HTML."""
    assets = _dict(_dict(item).get("assets"))
    fmt = assets.get("asset_url_format")
    if not isinstance(fmt, str) or not ART_FORMAT_RE.match(fmt):
        return {}
    out = {}
    for kind, key in ART_KINDS.items():
        name = assets.get(key)
        if isinstance(name, str) and ART_FILE_RE.match(name):
            out[kind] = ART_BASE + fmt.replace("${FILENAME}", name)
    return out


def appdetails_name(appid):
    """Fallback name lookup for ids the batched call did not know."""
    data = get_json(f"{APPDETAILS}?appids={int(appid)}&filters=basic")
    name = _dict(_dict(data.get(str(appid))).get("data")).get("name")
    return name if isinstance(name, str) else ""


def release_of(item):
    """(iso date, display string, coming soon) from a GetItems store item.

    Steam's appdetails only ships a display string rendered in US Pacific
    time, which can be a day off. GetItems carries the raw timestamp, so the
    date is computed in this machine's own time zone. A release *window*
    ("Q1 2027", "2027") also carries a timestamp - the window's last day - and
    is flagged by coming_soon_display; those keep no day, so a vague window
    never turns into an invented countdown.
    """
    item = _dict(item)
    rel = _dict(item.get("release"))
    coming = bool(item.get("is_coming_soon") or rel.get("is_coming_soon")
                  or rel.get("coming_soon_display"))
    try:
        ts = int(rel.get("steam_release_date") or 0)
    except (TypeError, ValueError, OverflowError):
        ts = 0
    d = _date(ts) if ts > 0 else None
    disp = rel.get("coming_soon_display")
    if d and (not disp or disp == "date_full"):
        return d.date().isoformat(), f"{d.day} {MONTHS[d.month - 1]} {d.year}", coming
    text = rel.get("custom_release_date_message")
    text = text.strip()[:80] if isinstance(text, str) else ""
    if not text and d:
        text = {"date_quarter": f"Q{(d.month - 1) // 3 + 1} {d.year}",
                "date_year": str(d.year),
                "date_month": f"{MONTHS[d.month - 1]} {d.year}"}.get(disp, "")
    return "", text or ("Coming soon" if coming else ""), coming


def keyed_json(label, url, params, api_key):
    """get_json for a call that carries the key. The key is a secret: no
    error raised here carries the request URL or the key."""
    q = urllib.parse.urlencode({"key": api_key, **params})
    try:
        return get_json(f"{url}?{q}")
    except StoreError as e:
        msg = str(e)
        if api_key in msg:  # belt and braces: never let the key into a log
            msg = msg.replace(api_key, "<key>")
        raise StoreError(f"{label}: {msg}", e.status) from None


def _int(v):
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0


def owned_details(steamid64, api_key):
    """{app id: {"name", "minutes", "lastPlayed", "minutes2w", "stats",
    "platforms": {windows, mac, linux, deck}}} from IPlayerService/GetOwnedGames.

    Needs the user's own Web API key (optional, [online] steam_api_key). Unlike
    the local licence cache it leaves out refunded games and games borrowed
    through Steam Family. include_played_free_games=1 keeps the free-to-play
    games that are really in the library (the ones that have been played);
    without it they would all drop out. One call gives ownership and the
    per-device / last-two-weeks hours the dashboard's profile block shows.
    """
    data = keyed_json("GetOwnedGames", GET_OWNED,
                      {"steamid": str(steamid64), "include_appinfo": 1,
                       "include_played_free_games": 1}, api_key)
    games = _dict(data.get("response")).get("games")
    if not isinstance(games, list):
        # an empty response is a private profile or a bad id, not "owns nothing"
        raise StoreError("GetOwnedGames returned no game list")
    out = {}
    for g in games:
        g = _dict(g)
        a = g.get("appid")
        if not (isinstance(a, int) and not isinstance(a, bool) and 0 < a < 2 ** 32):
            continue
        name = g.get("name")
        out[a] = {"name": name.strip()[:200] if isinstance(name, str) else "",
                  "minutes": _int(g.get("playtime_forever")),
                  "lastPlayed": _int(g.get("rtime_last_played")),
                  "minutes2w": _int(g.get("playtime_2weeks")),
                  "stats": g.get("has_community_visible_stats") is True,
                  "platforms": {p: _int(g.get(f"playtime_{p}_forever")) for p in PLATFORMS}}
    if not out:
        raise StoreError("GetOwnedGames returned an empty list")
    return out


def owned_games(steamid64, api_key):
    """Set of app ids the account owns (see owned_details)."""
    return set(owned_details(steamid64, api_key))


def player_achievements(steamid64, appid, api_key, language="english"):
    """{"total", "unlocked", "recent": [[api name, unlock time, display name]]
    (newest 3)} for one game, or None when the game has no achievements
    (Steam answers HTTP 400 "Requested app has no stats" then). Other failures
    raise StoreError, so a network hiccup is never cached as "no stats"."""
    try:
        data = keyed_json("GetPlayerAchievements", GET_ACHIEVEMENTS,
                          {"steamid": str(steamid64), "appid": int(appid), "l": language}, api_key)
    except StoreError as e:
        if e.status == 400:
            return None
        raise
    stats = _dict(data.get("playerstats"))
    rows = stats.get("achievements")
    if not isinstance(rows, list):
        if stats.get("success") is True:
            return None  # a game with stats but no achievements
        raise StoreError("GetPlayerAchievements returned no achievement list")
    total, unlocked, recent = 0, 0, []
    for r in rows:
        r = _dict(r)
        api = r.get("apiname")
        if not isinstance(api, str):
            continue
        total += 1
        if r.get("achieved") == 1:
            unlocked += 1
            t = _int(r.get("unlocktime"))
            name = r.get("name")
            recent.append([api[:200], t, name.strip()[:200] if isinstance(name, str) else ""])
    recent.sort(key=lambda x: -x[1])
    return {"total": total, "unlocked": unlocked, "recent": recent[:3]} if total else None


def achievement_icons(appid, api_key, language="english"):
    """{api name: icon URL} for a game from GetSchemaForGame. URLs that do not
    have the exact shape of a Steam achievement icon are dropped (they end up
    in the dashboard's HTML)."""
    data = keyed_json("GetSchemaForGame", GET_SCHEMA, {"appid": int(appid), "l": language}, api_key)
    rows = _dict(_dict(_dict(data.get("game")).get("availableGameStats"))).get("achievements")
    out = {}
    for r in rows if isinstance(rows, list) else []:
        r = _dict(r)
        api, url = r.get("name"), clean_icon(r.get("icon"))
        if isinstance(api, str) and url:
            out[api[:200]] = url
    return out


def clean_icon(url):
    """An achievement icon URL rebuilt on the steamstatic host, or ""."""
    m = ICON_RE.match(url) if isinstance(url, str) else None
    return f"{ICON_BASE}{m.group(1)}/{m.group(2)}.{m.group(3)}" if m else ""


def now_playing(steamid64, api_key):
    """{"appid", "name"} of the game the account is in right now, or {}.
    Only those two fields are read from the profile summary."""
    data = keyed_json("GetPlayerSummaries", GET_SUMMARIES, {"steamids": str(steamid64)}, api_key)
    players = _dict(data.get("response")).get("players")
    p = _dict(players[0]) if isinstance(players, list) and players else {}
    gid, name = p.get("gameid"), p.get("gameextrainfo")
    if isinstance(gid, str) and re.fullmatch(r"[0-9]{1,10}", gid) and 0 < int(gid) < 2 ** 32:
        return {"appid": int(gid), "name": name.strip()[:200] if isinstance(name, str) else ""}
    return {}


def wishlist(steamid64):
    """[{"appid", "added", "priority"}] or None when Steam would not say.

    The endpoint answers for any profile whose game details are public. A
    private profile gets {"response": {}} - no items key at all - which is
    "don't know", not "empty"; callers keep the last good list.
    """
    data = get_json(f"{GET_WISHLIST}?" + urllib.parse.urlencode({"steamid": str(steamid64)}))
    items = _dict(data.get("response")).get("items")
    if not isinstance(items, list):
        return None
    out = []
    for it in items:
        try:
            appid = int(it.get("appid") or 0)
            added = int(it.get("date_added") or 0)
            priority = int(it.get("priority") or 0)
        except (AttributeError, TypeError, ValueError, OverflowError):
            continue
        if 0 < appid < 2 ** 32:
            d = _date(added, timezone.utc) if added > 0 else None
            out.append({"appid": appid, "priority": priority,
                        "added": d.date().isoformat() if d else ""})
    return out
