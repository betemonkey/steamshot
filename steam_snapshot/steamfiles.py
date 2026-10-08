"""Read a Steam account's library straight from the Steam client's own files.

No Steam Web API key and no login are needed: everything here is a file the
Steam client already keeps on disk. Nothing is ever written to the Steam
folder - every function in this module only reads.

Sources, all relative to the Steam install folder:

  userdata/<id>/config/localconfig.vdf      playtime + last played, per app
  appcache/appinfo.vdf                      app id -> name and type (binary VDF)
  steamapps/appmanifest_*.acf               what is installed, and how big
  appcache/packageinfo.vdf                  the licence list (what is owned)
  userdata/<id>/config/cloudstorage/...     your library collections
  config/loginusers.vdf                     profile names of the accounts

Every parser degrades instead of failing: a file Steam changed the format of
yields "don't know" (an empty result), never an exception and never an empty
library presented as fact.
"""
import json
import os
import re
import struct
import sys

STEAM_ID64_BASE = 76561197960265728

# Steam's own plumbing shows up in localconfig next to real games. Most of it
# is caught by its app type (see SKIP_TYPES); this list is the backstop for
# when the type is missing from the cache: Steam Client (7), Steam Screenshots
# (760), Remote Play Client (202355), Steamworks Common Redistributables
# (228980), Steam Input Configs (241100), SteamVR (250820), Steam Game Notes
# (2371090), Proton Experimental (1493710) and the Steam Linux Runtimes
# (1070560, 1391110, 1628350). 480 ("Spacewar") is Valve's test app that
# Remote Play Together runs through - hours there are not a game.
SKIP_APPS = {7, 480, 760, 202355, 228980, 241100, 250820, 1070560, 1391110,
             1493710, 1628350, 2371090}
SKIP_TYPES = {"tool", "config"}
# str.isdigit() also accepts "²" and other digits int() refuses, and a long
# enough digit string trips Python's int conversion limit: only ASCII, capped
NUMBER = re.compile(r"[0-9]{1,19}")


def number(s):
    """int(s) for a plain non-negative number in a Steam file, else None."""
    return int(s) if isinstance(s, str) and NUMBER.fullmatch(s) else None


def read_text(path):
    """A Steam text file, closed straight away (Steam may want to rewrite it)."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


# ---------- locating Steam ----------

def find_steam_root(configured="auto"):
    """The Steam install folder, or None.

    A configured path wins. Otherwise: the registry on Windows, then the usual
    install locations for Windows, Linux (native, Flatpak, Snap) and macOS.
    A candidate only counts if it has a userdata folder, i.e. someone has
    actually logged in with it.
    """
    if configured and configured != "auto":
        path = os.path.expanduser(configured)
        return os.path.normpath(path) if os.path.isdir(path) else None
    candidates = []
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"SOFTWARE\Valve\Steam") as k:
                candidates.append(winreg.QueryValueEx(k, "SteamPath")[0])
        except OSError:
            pass
        candidates += [r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam"]
    elif sys.platform == "darwin":
        candidates.append("~/Library/Application Support/Steam")
    else:
        candidates += ["~/.steam/steam", "~/.local/share/Steam", "~/.steam/root",
                       "~/.var/app/com.valvesoftware.Steam/.local/share/Steam",
                       "~/snap/steam/common/.local/share/Steam"]
    for c in candidates:
        path = os.path.realpath(os.path.expanduser(c))
        if os.path.isdir(os.path.join(path, "userdata")):
            return os.path.normpath(path)
    return None


def library_dirs(root):
    """Every steamapps folder Steam knows about, not just the default one."""
    dirs = [os.path.join(root, "steamapps")]
    for vdf in (os.path.join(root, "steamapps", "libraryfolders.vdf"),
                os.path.join(root, "config", "libraryfolders.vdf")):
        try:
            text = read_text(vdf)
        except OSError:
            continue
        for path in re.findall(r'"path"\s+"([^"]+)"', text):
            d = os.path.join(path.replace("\\\\", "\\"), "steamapps")
            if os.path.isdir(d) and d not in dirs:
                dirs.append(d)
    return dirs


# ---------- accounts ----------

def to_account_id(value):
    """32-bit account id from either form: the account id itself or a SteamID64."""
    n = number(str(value).strip())
    if n is None:
        raise ValueError(f"not a Steam account id: {value!r}")
    return n - STEAM_ID64_BASE if n >= STEAM_ID64_BASE else n


def list_accounts(root):
    """[{accountId, steamid64, persona, lastUsed}] for every account that has a
    localconfig.vdf on this machine, most recently used first."""
    personas = read_personas(root)
    base = os.path.join(root, "userdata")
    out = []
    try:
        entries = os.listdir(base)
    except OSError:
        return out
    for name in entries:
        cfg = os.path.join(base, name, "config", "localconfig.vdf")
        acct = number(name)
        if not acct or acct >= 2 ** 32 or not os.path.exists(cfg):
            continue
        sid = str(STEAM_ID64_BASE + acct)
        out.append({"accountId": acct, "steamid64": sid,
                    "persona": personas.get(sid, ""),
                    "lastUsed": os.path.getmtime(cfg)})
    out.sort(key=lambda a: -a["lastUsed"])
    return out


def pick_account(root, configured="auto"):
    """The account to snapshot: the configured one, or the most recently used."""
    accounts = list_accounts(root)
    if configured and configured != "auto":
        want = to_account_id(configured)
        return next((a for a in accounts if a["accountId"] == want), None)
    return accounts[0] if accounts else None


def read_personas(root):
    """SteamID64 -> profile name, from loginusers.vdf. Only the display name is
    read; the login name and the remember-password flags are left alone."""
    path = os.path.join(root, "config", "loginusers.vdf")
    try:
        text = read_text(path)
    except OSError:
        return {}
    out = {}
    for sid, body in re.findall(r'"(\d{17})"\s*\{([^}]*)\}', text):
        m = re.search(r'"PersonaName"\s+"((?:[^"\\]|\\.)*)"', body)
        if m:
            out[sid] = m.group(1).replace('\\"', '"').replace("\\\\", "\\")
    return out


# ---------- playtime ----------

def parse_localconfig(path):
    """app id -> {"minutes", "lastPlayed" (unix time)} from the text VDF.

    Hand-rolled rather than a full VDF parse: the apps block is found by name
    and the two counters are flat inside each app's block. An app id can sit
    in more than one apps block and only one of them carries the counters, so
    values are merged with max(), never reset - a plain dict parse would
    silently read 0 hours for those games.
    """
    apps, depth, in_apps, cur = {}, 0, None, None
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return {}
    with fh:
        for line in fh:
            s = line.strip()
            if s == "{":
                depth += 1
                continue
            if s == "}":
                depth -= 1
                if in_apps is not None and depth <= in_apps:
                    in_apps, cur = None, None
                continue
            m = re.match(r'"([^"]+)"\s*$', s)
            if m:  # a key that opens a block on the next line
                if m.group(1).lower() == "apps" and in_apps is None:
                    in_apps = depth
                elif in_apps is not None and depth == in_apps + 1:
                    cur = number(m.group(1))
                    if cur is not None:
                        apps.setdefault(cur, {"minutes": 0, "lastPlayed": 0})
                continue
            kv = re.match(r'"([^"]+)"\s+"([^"]*)"\s*$', s)
            if kv and cur is not None and depth == in_apps + 2:
                key, val = kv.group(1).lower(), number(kv.group(2))
                if key == "playtime" and val is not None:
                    apps[cur]["minutes"] = max(apps[cur]["minutes"], val)
                elif key == "lastplayed" and val is not None:
                    apps[cur]["lastPlayed"] = max(apps[cur]["lastPlayed"], val)
    return apps


# ---------- names and types ----------

def parse_appinfo(path):
    """app id -> {"name", "type"} from Steam's binary appinfo cache.

    Format 0x29 pools *keys* in a trailing string table (values stay inline),
    so "name" is a 4-byte index rather than the literal; 0x28 and older keep
    keys inline. Both are handled. A newer layout yields an empty dict, and
    the snapshot then falls back to the store API for names - so a format bump
    degrades rather than breaks.
    """
    try:
        blob = read_bytes(path)
    except OSError:
        return {}
    if len(blob) < 16:
        return {}
    magic = struct.unpack_from("<I", blob, 0)[0]
    version = magic & 0xFF
    if (magic >> 8) != 0x075644 or not 0x27 <= version <= 0x29:
        return {}
    try:
        if version == 0x29:
            table_off = struct.unpack_from("<q", blob, 8)[0]
            if not 0 < table_off < len(blob):
                return {}
            count = struct.unpack_from("<i", blob, table_off)[0]
            if not 0 < count <= 1_000_000:  # the real table has a few thousand keys
                return {}
            pos, strings = table_off + 4, []
            for _ in range(count):
                end = blob.index(b"\x00", pos)
                strings.append(blob[pos:end].decode("utf-8", "replace"))
                pos = end + 1
            if "name" not in strings:
                return {}
            def key(k):
                return b"\x01" + struct.pack("<i", strings.index(k)) if k in strings else None
            name_key, type_key = key("name"), key("type")
            common_key = (b"\x00" + struct.pack("<i", strings.index("common"))
                          if "common" in strings else None)
            limit, pos, skip = table_off, 16, 60
        else:
            name_key, type_key, common_key = b"\x01name\x00", b"\x01type\x00", b"\x00common\x00"
            limit, pos, skip = len(blob), 8, 40
    except (ValueError, struct.error):
        return {}

    def value_after(k, start, end):
        if not k:
            return ""
        at = blob.find(k, start, end)
        if at == -1:
            return ""
        vs = at + len(k)
        ve = blob.find(b"\x00", vs, end)
        return blob[vs:ve].decode("utf-8", "replace") if ve != -1 else ""

    out = {}
    while pos + 8 <= limit:
        appid, size = struct.unpack_from("<II", blob, pos)
        if appid == 0:
            break
        start, end = pos + 8, pos + 8 + size
        if end > limit:
            break
        body = start + skip
        # both keys are read from the "common" section: deeper sections
        # (launch options, depots) reuse the key "type" for other things
        common = blob.find(common_key, body, end) if common_key else -1
        section = common if common != -1 else body
        name = value_after(name_key, section, end)
        kind = value_after(type_key, section, end)
        if name or kind:
            out[appid] = {"name": name, "type": kind.lower()}
        pos = end
    return out


# ---------- ownership ----------

def parse_packageinfo(path):
    """App ids covered by the licences in Steam's package cache.

    Needed because localconfig.vdf keeps a playtime entry forever: a refunded
    game, an expired free trial or a delisted demo still shows up there with
    the hours played before it went away. The licence list is the only local
    source that knows what is still owned.

    Caveat: it answers "is there a licence in the cache", not "do you still
    own this". A Steam Family shared game reads as owned. Use hide_appids in
    the config for those.

    Returns an empty set on any parse failure; callers treat that as "don't
    know", never as "owns nothing".
    """
    try:
        blob = read_bytes(path)
    except OSError:
        return set()
    if len(blob) < 8:
        return set()
    version = struct.unpack_from("<I", blob, 0)[0] & 0xFF
    # packageid + sha1 + change number (+ pics token from v28)
    header = 4 + 20 + 4 + (8 if version >= 0x28 else 0)
    owned, pos = set(), 8
    try:
        while pos + 4 <= len(blob):
            if struct.unpack_from("<I", blob, pos)[0] == 0xFFFFFFFF:
                break
            pos += header
            depth, in_appids = 0, None
            while pos < len(blob):
                kind = blob[pos]
                if kind == 8:  # end of block
                    pos += 1
                    depth -= 1
                    if in_appids is not None and depth < in_appids:
                        in_appids = None
                    if depth < 0:
                        break
                    continue
                end = blob.index(b"\x00", pos + 1)
                key = blob[pos + 1:end].decode("utf-8", "replace")
                pos = end + 1
                if kind == 0:  # nested block
                    depth += 1
                    if key.lower() == "appids" and in_appids is None:
                        in_appids = depth
                elif kind == 1:  # string
                    pos = blob.index(b"\x00", pos) + 1
                elif kind == 2:  # int32
                    value = struct.unpack_from("<i", blob, pos)[0]
                    pos += 4
                    if in_appids is not None:
                        owned.add(value)
                elif kind == 7:  # int64
                    pos += 8
                else:
                    break
    except (ValueError, struct.error, IndexError):
        return set()
    return owned


# ---------- collections ----------

def parse_collections(root, account_id):
    """Your Steam library collections, as
    ({app id: [collection names]}, hidden app ids, favourite app ids).

    They sync through Steam Cloud and live in
    userdata/<id>/config/cloudstorage/cloud-storage-namespace-1.json. The two
    built-in ones - Hidden and Favourites - are recognised by their key, not
    their name, because the name is translated into the client's language.

    Dynamic collections (built from a filter) only contribute the games added
    to them by hand: filter membership cannot be computed offline.
    Returns empty results on any failure.
    """
    path = os.path.join(root, "userdata", str(account_id), "config", "cloudstorage",
                        "cloud-storage-namespace-1.json")
    try:
        entries = json.loads(read_text(path))
    except (OSError, ValueError, RecursionError):
        return {}, set(), set()
    cols, hidden, favourite = {}, set(), set()
    try:
        for key, rec in entries:
            key = str(key)
            if not key.startswith("user-collections.") or rec.get("is_deleted"):
                continue
            try:
                value = json.loads(rec.get("value") or "")
            except (ValueError, RecursionError):
                continue
            added = value.get("added")
            ids = set()
            # a list of numbers (or digit strings); a bare string would iterate as digits
            for a in added if isinstance(added, list) else []:
                a = number(a) if isinstance(a, str) else a
                if isinstance(a, int) and not isinstance(a, bool) and 0 < a < 2 ** 32:
                    ids.add(a)
            kind = key.split(".", 1)[1]
            if kind == "hidden":
                hidden |= ids
                continue
            if kind == "favorite":
                favourite |= ids
                continue
            name = value.get("name")
            name = name.strip() if isinstance(name, str) else ""
            if not name:
                continue
            for appid in ids:
                cols.setdefault(appid, []).append(name)
    except (TypeError, ValueError, AttributeError):
        return {}, set(), set()
    for names in cols.values():
        names.sort(key=str.lower)
    return cols, hidden, favourite


# ---------- installs ----------

def parse_installed(dirs):
    """app id -> bytes on disk, from the manifest Steam writes per install."""
    installed = {}
    for d in dirs:
        try:
            entries = os.listdir(d)
        except OSError:
            continue
        for fn in entries:
            if not (fn.startswith("appmanifest_") and fn.endswith(".acf")):
                continue
            try:
                text = read_text(os.path.join(d, fn))
            except OSError:
                continue
            m = re.search(r'"appid"\s+"([0-9]{1,10})"', text)
            if not m:
                continue
            size = re.search(r'"SizeOnDisk"\s+"([0-9]{1,19})"', text)
            installed[int(m.group(1))] = int(size.group(1)) if size else 0
    return installed


# ---------- the whole library ----------

def read_library(root, account, hide_appids=(), include_unowned=False, owned_apps=None):
    """Everything readable about one account's library, as plain dicts.

    owned_apps: the account's owned app ids from the Steam Web API (optional,
    see store.owned_games). When given it decides ownership instead of the
    licence cache, which can't tell refunds and Steam Family games apart.

    Returns (games, sources) where sources says which files were readable -
    shown by `doctor` and stored in the snapshot so a gap can be explained.
    """
    acct = account["accountId"]
    played = parse_localconfig(os.path.join(root, "userdata", str(acct), "config",
                                            "localconfig.vdf"))
    info = parse_appinfo(os.path.join(root, "appcache", "appinfo.vdf"))
    installed = parse_installed(library_dirs(root))
    owned = parse_packageinfo(os.path.join(root, "appcache", "packageinfo.vdf"))
    collections, hidden, favourite = parse_collections(root, acct)

    candidates = set(played) | set(installed) | set(collections)
    candidates -= SKIP_APPS
    candidates -= {int(a) for a in hide_appids}
    candidates = {a for a in candidates
                  if (info.get(a) or {}).get("type") not in SKIP_TYPES}

    # The licence cache belongs to whoever logged in last. When it covers less
    # than half of this account's played games it is someone else's list (or
    # half-written), and filtering on it would blank most of the library - so
    # it is treated as "don't know" instead.
    played_ids = [a for a in candidates if a in played and a not in installed]
    if owned and played_ids and sum(a in owned for a in played_ids) < len(played_ids) / 2:
        owned_usable = False
    else:
        owned_usable = bool(owned)
    from_api = bool(owned_apps)

    games = []
    for appid in sorted(candidates):
        rec = played.get(appid) or {"minutes": 0, "lastPlayed": 0}
        meta = info.get(appid) or {}
        game = {"appid": appid, "name": meta.get("name") or "",
                "type": meta.get("type") or "",
                "minutes": rec["minutes"], "lastPlayed": rec["lastPlayed"]}
        if appid in installed:
            game["installed"] = True
            game["bytes"] = installed[appid]
        # only assert ownership when the licence list parsed and looks like
        # this account's; an installed game is owned whatever the cache says
        if from_api:
            # exact: an installed family-shared game is not owned
            game["owned"] = appid in owned_apps
        elif owned_usable:
            game["owned"] = appid in owned or appid in installed
        if collections.get(appid):
            game["collections"] = collections[appid]
        if appid in hidden:
            game["hidden"] = True
        if appid in favourite:
            game["favourite"] = True
        if game.get("owned") is False and not include_unowned:
            continue
        games.append(game)

    sources = {
        "localconfig": bool(played),
        "appinfo": bool(info),
        "packageinfo": owned_usable,
        "ownership": "api" if from_api else ("licence-cache" if owned_usable else "unknown"),
        "collections": bool(collections or hidden or favourite),
        "installedApps": len(installed),
        "libraryFolders": len(library_dirs(root)),
    }
    return games, sources
