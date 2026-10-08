"""A small read-only web server for the dashboard.

Standard library only. It serves one page plus a few JSON endpoints over the
data folder, answers GET only, and binds to 127.0.0.1 unless the config says
otherwise - your playtime stays on your machine.

  GET /                          the dashboard
  GET /favicon.ico, /icon.svg    the Steamshot icon (from assets/)
  GET /api/accounts              accounts that have snapshots
  GET /api/snapshots?account=ID  stored days with headline numbers
  GET /api/snapshot?account=ID&date=YYYY-MM-DD   one snapshot (latest if no date)
  GET /api/history?account=ID    playtime gained per day
  GET /api/art?account=ID        artwork URLs per app id
  GET /api/steamapi?account=ID   the "From your Steam profile" block (optional
                                 API key only; 404 without it)
  GET /api/nowplaying?account=ID the game being played right now, {} if none
                                 (optional API key only; asks Steam at most
                                 once a minute per account)
  GET /api/version               running version and update status

Requests must name this server in their Host header (an IP address,
localhost or the configured host), so a web page that points its own domain
at 127.0.0.1 (DNS rebinding) cannot read the data.
"""
import ipaddress
import json
import os
import re
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import __version__
from .snapshot import clean_art, read_json, snapshot_dates
from .demo import DEMO_ID, NOW_PLAYING as DEMO_NOW_PLAYING
from .steamapi import SUMMARY as STEAMAPI_FILE, clean as clean_steamapi

NOW_PLAYING_TTL = 60  # seconds: page polling can never multiply calls to Steam

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
ASSETS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")
# the only files served: a fixed list, so no request path ever reaches the disk.
# Types are spelled out (mimetypes reads the Windows registry, which varies).
STATIC = {
    "/": (os.path.join(WEB_DIR, "index.html"), "text/html; charset=utf-8"),
    "/index.html": (os.path.join(WEB_DIR, "index.html"), "text/html; charset=utf-8"),
    "/favicon.ico": (os.path.join(ASSETS_DIR, "steamshot.ico"), "image/x-icon"),
    # the bold variant: a browser tab draws it at 16-32 px
    "/icon.svg": (os.path.join(ASSETS_DIR, "steamshot-small.svg"), "image/svg+xml"),
}
ACCOUNT_RE = re.compile(r"\A[0-9]{1,20}\Z")
DATE_RE = re.compile(r"\A[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
# the page is one self-contained file; images come from Steam's CDN only
CSP = ("default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data: blob: https://*.steamstatic.com; connect-src 'self'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def _num(v):
    return v if isinstance(v, int) and not isinstance(v, bool) else 0


def _dict(v):
    return v if isinstance(v, dict) else {}


def _games(snap):
    return [g for g in (snap.get("games") if isinstance(snap.get("games"), list) else []) if isinstance(g, dict)]


class Data:
    """Reads the data folder, caching parsed files by modification time so a
    year of daily snapshots is parsed once, not on every page load."""

    def __init__(self, data_dir, options):
        self.data_dir = data_dir
        self.options = options
        self._cache = {}
        self._lock = threading.Lock()

    def _load(self, path):
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return None
        with self._lock:
            hit = self._cache.get(path)
            if hit and hit[0] == mtime:
                return hit[1]
        data = read_json(path)
        with self._lock:
            self._cache[path] = (mtime, data)
        return data

    def accounts(self):
        out = []
        try:
            entries = os.listdir(self.data_dir)
        except OSError:
            return out
        for name in entries:
            acct = os.path.join(self.data_dir, name)
            if not ACCOUNT_RE.match(name) or not os.path.isdir(acct):
                continue
            dates = snapshot_dates(acct)
            if not dates:
                continue
            latest = _dict(self._load(os.path.join(acct, "snapshots", f"{dates[-1]}.json")))
            persona = _dict(latest.get("account")).get("persona")
            out.append({"steamid64": name,
                        "persona": persona if isinstance(persona, str) else "",
                        "latest": dates[-1], "count": len(dates),
                        "demo": bool(latest.get("demo"))})
        out.sort(key=lambda a: a["latest"], reverse=True)
        return out

    def snapshots(self, account):
        acct = os.path.join(self.data_dir, account)
        out = []
        for d in snapshot_dates(acct):
            snap = _dict(self._load(os.path.join(acct, "snapshots", f"{d}.json")))
            games = [g for g in _games(snap) if not g.get("hidden")]
            out.append({"date": d, "taken": snap.get("taken"), "games": len(games),
                        "minutes": sum(_num(g.get("minutes")) for g in games)})
        return out

    def snapshot(self, account, day=None):
        acct = os.path.join(self.data_dir, account)
        dates = snapshot_dates(acct)
        if not dates:
            return None
        if day is None:
            day = dates[-1]
        if day not in dates:
            return None
        snap = self._load(os.path.join(acct, "snapshots", f"{day}.json"))
        if not isinstance(snap, dict):
            return None
        idx = dates.index(day)
        prev = None
        if idx > 0:
            p = _dict(self._load(os.path.join(acct, "snapshots", f"{dates[idx - 1]}.json")))
            # the previous day's library, trimmed to what the "changes" card needs
            prev = {"date": dates[idx - 1],
                    "games": [{"appid": g.get("appid"), "name": g.get("name"),
                               "minutes": _num(g.get("minutes")),
                               "hidden": bool(g.get("hidden"))}
                              for g in _games(p)]}
        return {**snap, "previous": prev}

    def art(self, account):
        cache = clean_art(self._load(os.path.join(self.data_dir, account, "art.json")))
        return {a: {k: v for k, v in rec.items() if k != "t"}
                for a, rec in cache.items() if len(rec) > 1}

    def steamapi(self, account):
        return clean_steamapi(self._load(os.path.join(self.data_dir, account, STEAMAPI_FILE)))

    def history(self, account):
        hist = _dict(self._load(os.path.join(self.data_dir, account, "history.json")))
        return {"since": hist.get("since"), "days": _dict(hist.get("days"))}


class Handler(BaseHTTPRequestHandler):
    server_version = f"steam-snapshot/{__version__}"
    timeout = 30      # an idle or half-sent request does not hold a thread for ever
    data = None       # set by make_server
    cfg = None        # the full config, for the update status; None in tests
    host_name = None  # the configured host, also accepted in the Host header

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    def send(self, status, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)) or body is None:
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def host_ok(self):
        """The Host header names this server: an IP address (what a rebinding
        page cannot send), localhost, or the configured host, on our port."""
        m = re.fullmatch(r"(\[[0-9a-fA-F:.]+\]|[^:\[\]]+)(?::([0-9]{1,5}))?", self.headers.get("Host") or "")
        if not m or (m.group(2) and int(m.group(2)) != self.server.server_address[1]):
            return False
        name = m.group(1).strip("[]").lower()
        if name in ("localhost", (self.host_name or "").lower()):
            return True
        try:
            ipaddress.ip_address(name)
        except ValueError:
            return False
        return True

    def do_GET(self):
        if not self.host_ok():
            return self.send(421, {"error": "unknown Host header"})
        try:
            return self.route()
        except Exception:  # an odd file must not take the whole dashboard down
            return self.send(500, {"error": "could not read the data"})

    def route(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        account = q.get("account", "")
        day = q.get("date")
        if url.path in STATIC:
            return self.static(*STATIC[url.path])
        if url.path == "/api/accounts":
            return self.send(200, {"accounts": self.data.accounts(),
                                   "options": self.data.options,
                                   "version": __version__})
        if url.path == "/api/version":
            return self.send(200, self.version())
        if url.path.startswith("/api/"):
            if not ACCOUNT_RE.match(account) or (day and not DATE_RE.match(day)):
                return self.send(400, {"error": "bad account or date"})
        if url.path == "/api/snapshots":
            return self.send(200, self.data.snapshots(account))
        if url.path == "/api/snapshot":
            snap = self.data.snapshot(account, day)
            return self.send(200, snap) if snap else self.send(404, {"error": "no such snapshot"})
        if url.path == "/api/history":
            return self.send(200, self.data.history(account))
        if url.path == "/api/art":
            return self.send(200, self.data.art(account))
        if url.path == "/api/steamapi":
            block = self.data.steamapi(account)
            return self.send(200, block) if block else self.send(404, {"error": "no profile block"})
        if url.path == "/api/nowplaying":
            return self.send(200, self.now_playing(account))
        self.send(404, {"error": "not found"})

    # account -> (time asked, answer); shared by every request thread
    _now_cache = {}
    _now_lock = threading.Lock()

    def now_playing(self, account):
        """{"appid", "name"} or {}. Steam is asked only with the optional key,
        online calls on, and for an account whose profile block exists (one
        of ours, not any id a request names) - and at most once a minute."""
        if account == DEMO_ID:  # invented data never reaches Steam
            return dict(DEMO_NOW_PLAYING) if self.data.steamapi(account) else {}
        online = (self.cfg or {}).get("online") or {}
        key = online.get("steam_api_key")
        if not (key and online.get("enabled")) or not self.data.steamapi(account):
            return {}
        now = time.monotonic()
        with self._now_lock:
            hit = self._now_cache.get(account)
            if hit and now - hit[0] < NOW_PLAYING_TTL:
                return hit[1]
            # claim the slot before asking, so parallel requests don't all ask
            self._now_cache[account] = (now, hit[1] if hit else {})
        from . import store
        try:
            answer = store.now_playing(account, key)
        except store.StoreError:
            answer = {}
        with self._now_lock:
            self._now_cache[account] = (now, answer)
        return answer

    def version(self):
        if not self.cfg:
            return {"running": __version__, "available": False, "status": "off"}
        from . import update
        return update.status(self.cfg)

    def static(self, path, ctype):
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except OSError:
            return self.send(404, {"error": "not found"})
        self.send(200, body, ctype)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second process bind the same port and
    # take requests meant for this one; ask for the port exclusively instead.
    allow_reuse_address = sys.platform != "win32"

    def server_bind(self):
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class Server6(Server):
    address_family = socket.AF_INET6


def make_server(data_dir, host, port, options, cfg=None):
    handler = type("BoundHandler", (Handler,), {"data": Data(data_dir, options), "cfg": cfg,
                                                 "host_name": host})
    return (Server6 if ":" in host else Server)((host, port), handler)
