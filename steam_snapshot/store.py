"""Keyless lookups against Steam's public web endpoints.

Used for four things the local files cannot answer: names for games missing
from Steam's local cache (new purchases, the whole wishlist), the wishlist
itself, release dates for wishlisted games, and where each game's artwork
lives on Steam's image server. None of these need an API key
or a login. Every call is optional - with `[online] enabled = false` this
module is never imported into a run's path and nothing leaves the machine.

Only app ids and your public SteamID64 are ever sent.
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
    pass


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
        raise StoreError(f"{type(e).__name__}: {e}") from e
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
