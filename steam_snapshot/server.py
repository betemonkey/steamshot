"""A small read-only web server for the dashboard.

Standard library only. It serves one page plus a few JSON endpoints over the
data folder, answers GET only, and binds to 127.0.0.1 unless the config says
otherwise - your playtime stays on your machine.

  GET /                          the dashboard
  GET /api/accounts              accounts that have snapshots
  GET /api/snapshots?account=ID  stored days with headline numbers
  GET /api/snapshot?account=ID&date=YYYY-MM-DD   one snapshot (latest if no date)
  GET /api/history?account=ID    playtime gained per day
  GET /api/version               running version and update status
"""
import json
import mimetypes
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import __version__
from .snapshot import read_json, snapshot_dates

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
ACCOUNT_RE = re.compile(r"^\d{1,20}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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
            latest = self._load(os.path.join(acct, "snapshots", f"{dates[-1]}.json")) or {}
            out.append({"steamid64": name,
                        "persona": (latest.get("account") or {}).get("persona", ""),
                        "latest": dates[-1], "count": len(dates),
                        "demo": bool(latest.get("demo"))})
        out.sort(key=lambda a: a["latest"], reverse=True)
        return out

    def snapshots(self, account):
        acct = os.path.join(self.data_dir, account)
        out = []
        for d in snapshot_dates(acct):
            snap = self._load(os.path.join(acct, "snapshots", f"{d}.json")) or {}
            games = [g for g in snap.get("games") or [] if not g.get("hidden")]
            out.append({"date": d, "taken": snap.get("taken"), "games": len(games),
                        "minutes": sum(int(g.get("minutes") or 0) for g in games)})
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
        if snap is None:
            return None
        idx = dates.index(day)
        prev = None
        if idx > 0:
            p = self._load(os.path.join(acct, "snapshots", f"{dates[idx - 1]}.json")) or {}
            # the previous day's library, trimmed to what the "changes" card needs
            prev = {"date": dates[idx - 1],
                    "games": [{"appid": g.get("appid"), "name": g.get("name"),
                               "minutes": g.get("minutes", 0),
                               "hidden": bool(g.get("hidden"))}
                              for g in p.get("games") or []]}
        return {**snap, "previous": prev}

    def history(self, account):
        hist = self._load(os.path.join(self.data_dir, account, "history.json")) or {}
        return {"since": hist.get("since"), "days": hist.get("days") or {}}


class Handler(BaseHTTPRequestHandler):
    server_version = f"steam-snapshot/{__version__}"
    data = None  # set by make_server
    cfg = None   # the full config, for the update status; None in tests

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
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        account = q.get("account", "")
        day = q.get("date")
        if url.path in ("/", "/index.html"):
            return self.static("index.html")
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
        self.send(404, {"error": "not found"})

    def version(self):
        if not self.cfg:
            return {"running": __version__, "available": False, "status": "off"}
        from . import update
        return update.status(self.cfg)

    def static(self, name):
        path = os.path.join(WEB_DIR, name)
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except OSError:
            return self.send(404, {"error": "not found"})
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if ctype.startswith("text/"):
            ctype += "; charset=utf-8"
        self.send(200, body, ctype)


def make_server(data_dir, host, port, options, cfg=None):
    handler = type("BoundHandler", (Handler,), {"data": Data(data_dir, options), "cfg": cfg})
    return ThreadingHTTPServer((host, port), handler)
