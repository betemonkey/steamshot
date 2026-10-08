"""Command line: snapshot, serve, open, shortcut, doctor, schedule, demo, init, export, import."""
import argparse
import os
import shlex
import shutil
import subprocess
import sys
import threading
import urllib.request
import webbrowser
import time
from datetime import datetime

from . import __version__, config, snapshot, steamfiles

TASK_NAME = "steam-snapshot"
WATCH_EVERY = 60  # seconds between the dashboard's looks for an update


def check_updates(cfg):
    """Look for (and maybe install) a new version. Never fails the caller."""
    from . import update
    try:
        return update.run(cfg)
    except Exception as e:
        snapshot.log_line(cfg["data_dir"], f"update check crashed: {e}")
        return {}


def cmd_snapshot(cfg, args):
    try:
        s = snapshot.take(cfg)
    except Exception as e:  # a scheduled run has no console: the log is all there is
        if not isinstance(e, snapshot.SnapshotError):
            e = f"{type(e).__name__}: {e}"
        snapshot.log_line(cfg["data_dir"], f"FAILED: {e}")
        print(f"snapshot failed: {e}", file=sys.stderr)
        check_updates(cfg)  # an update may be the fix
        return 1
    if check_updates(cfg).get("status") == "updated":
        print("  updated steam-snapshot; the next run uses the new version")
    print(f"snapshot saved: {s['games']} games, {s['hours']:,} h, "
          f"wishlist {s['wishlist']} ({s['wishlistStatus']})")
    print(f"  -> {s['path']}")
    for note in s["notes"]:
        print(f"  note: {note}")
    return 0


def cmd_serve(cfg, args):
    from .server import make_server
    data_dir = os.path.abspath(args.data_dir) if args.data_dir else cfg["data_dir"]
    dash = cfg["dashboard"]
    host = args.host or dash["host"]
    port = args.port or dash["port"]
    try:
        httpd = make_server(data_dir, host, port, {"images": bool(dash["images"])}, cfg=cfg)
    except OSError as e:
        print(f"cannot listen on {host}:{port}: {e}", file=sys.stderr)
        return 1
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}/"
    print(f"dashboard: {url}   (data: {data_dir})")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("  note: listening beyond this machine - anyone on your network can see it")
    print("  Ctrl+C to stop")
    if args.open:
        webbrowser.open(url)
    restart = threading.Event()
    if not getattr(args, "demo", False):  # the demo is a sandbox: no git, no update.json
        threading.Thread(target=watch_for_update, args=(cfg, httpd, restart), daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    if restart.is_set():
        return relaunch()
    return 0


def watch_for_update(cfg, httpd, restart):
    """Check for updates now and then; once new code is on disk (pulled here
    or by the scheduled snapshot), stop the server so it can restart on it."""
    from . import update
    while True:
        check_updates(cfg)
        on_disk = update.disk_version()
        if on_disk and on_disk != __version__:
            restart.set()
            say(f"steam-snapshot {on_disk} is installed - restarting the dashboard")
            httpd.shutdown()
            return
        time.sleep(WATCH_EVERY)


def say(text):
    """print() that cannot fail: a relaunched server may have inherited an
    output pipe that has since closed."""
    try:
        print(text, flush=True)
    except (OSError, ValueError):
        pass


def relaunch():
    """Start this same command again on the updated code, without --open (the
    browser tab is already there and reloads itself). It runs from the project
    folder, so `-m steam_snapshot` is found wherever the first one started."""
    argv = [sys.executable, "-m", "steam_snapshot", *absolute_paths(a for a in sys.argv[1:] if a != "--open")]
    if sys.platform != "win32":
        os.chdir(config.PROJECT_DIR)
        os.execv(sys.executable, argv)
    # os.execv is unreliable on Windows, so start a new process. It keeps the
    # terminal if there is one; a pipe would close when this process exits.
    try:
        console = bool(sys.stdout and sys.stdout.isatty())
    except (OSError, ValueError):
        console = False
    quiet = {} if console else {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                                "stderr": subprocess.DEVNULL}
    subprocess.Popen(argv, cwd=config.PROJECT_DIR, **quiet)
    return 0


def absolute_paths(args):
    """The command line with --config / --data-dir values made absolute, for
    a restart from another folder."""
    out, path_next = [], False
    for a in args:
        if path_next:
            a, path_next = os.path.abspath(a), False
        elif a in ("--config", "--data-dir"):
            path_next = True
        elif a.startswith(("--config=", "--data-dir=")):
            key, val = a.split("=", 1)
            a = f"{key}={os.path.abspath(val)}"
        out.append(a)
    return out


def cmd_doctor(cfg, args):
    ok = lambda b: "[ok]" if b else "[--]"  # noqa: E731
    print(f"steam-snapshot {__version__} - doctor (reads only, changes nothing)")
    print(f"  Python       {sys.version.split()[0]}  ({sys.executable})")
    print(f"  Config       {cfg['_file'] or 'none - using built-in defaults'}")
    print(f"  Data folder  {cfg['data_dir']}")
    root = steamfiles.find_steam_root(cfg["steam"]["path"])
    if not root:
        print("  Steam        NOT FOUND - set [steam] path in config.toml")
        return 1
    print(f"  Steam        {root}")
    accounts = steamfiles.list_accounts(root)
    chosen = steamfiles.pick_account(root, cfg["steam"]["account"])
    print(f"  Accounts     {len(accounts)} on this machine "
          f"(config: account = \"{cfg['steam']['account']}\")")
    for a in accounts:
        mark = "*" if chosen and a["accountId"] == chosen["accountId"] else " "
        used = datetime.fromtimestamp(a["lastUsed"]).strftime("%Y-%m-%d")
        print(f"    {mark} {a['steamid64']}  {a['persona'] or '(no name)'}  last used {used}")
    if not chosen:
        print("  The configured account has not logged in on this machine.")
        return 1
    print("    (* = the account a snapshot will read)")
    online = cfg["online"]
    owned_apps, owned_msg = None, 'not set (optional, see "Exact ownership" in the README)'
    if online["steam_api_key"]:
        if not online["enabled"]:
            owned_msg = "set, but unused: [online] enabled = false"
        else:
            from . import store
            try:
                owned_apps = store.owned_games(chosen["steamid64"], online["steam_api_key"])
                owned_msg = f"set, works: {len(owned_apps)} owned games"
            except store.StoreError as e:
                owned_msg = f"set, but the check failed: {e}"
    games, src = steamfiles.read_library(root, chosen, cfg["snapshot"]["hide_appids"],
                                         cfg["snapshot"]["include_unowned"], owned_apps)
    print("  Sources")
    print(f"    {ok(src['localconfig'])} playtime         userdata/.../localconfig.vdf")
    print(f"    {ok(src['appinfo'])} names + types    appcache/appinfo.vdf")
    print(f"    {ok(src['packageinfo'])} licences         appcache/packageinfo.vdf"
          + ("" if src["packageinfo"] else "  (ownership not filtered)"))
    print(f"    {ok(src['collections'])} collections      userdata/.../cloudstorage")
    print(f"  API key      {owned_msg}")
    print(f"  Ownership    {src['ownership']}")
    print(f"    {ok(src['installedApps'])} installed        {src['installedApps']} games in "
          f"{src['libraryFolders']} library folder(s)")
    hours = sum(g["minutes"] for g in games) / 60
    unnamed = sum(1 for g in games if not g["name"])
    print(f"  Library      {len(games)} games, {hours:,.0f} h"
          + (f" ({unnamed} names to look up online)" if unnamed else ""))
    if not online["enabled"]:
        print("  Online       off - no network calls, no wishlist")
    elif online["wishlist"]:
        from . import store
        try:
            wl = store.wishlist(chosen["steamid64"])
            print(f"  Wishlist     {'readable, %d games' % len(wl) if wl is not None else 'NOT readable'}")
            if wl is None:
                print("               Steam profile > Edit Profile > Privacy Settings >"
                      " Game details = Public")
        except store.StoreError as e:
            print(f"  Wishlist     check failed: {e}")
    dates = snapshot.snapshot_dates(snapshot.account_dir(cfg["data_dir"], chosen["steamid64"]))
    print(f"  Snapshots    {len(dates)} stored" + (f", latest {dates[-1]}" if dates else ""))
    from . import update
    u = update.status(cfg)
    if u["status"] == "off":
        print("  Updates      not checked ([updates] check = false or [online] enabled = false)")
    else:
        print(f"  Updates      {'installed automatically' if u['auto'] else 'banner only'}; "
              + (f"{u['latest']} available" + (f" ({u['reason']})" if u["reason"] else "")
                 if u["available"] else f"last check: {u['status']}"))
    return 0


def pythonw():
    """On Windows, the console-less interpreter, so a scheduled run does not
    flash a window every half hour."""
    exe = sys.executable
    if sys.platform == "win32":
        cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(cand):
            return cand
    return exe


def ps_quote(s):
    """A PowerShell single-quoted string: ' doubles, nothing else is special,
    so a folder name can never turn into a second command."""
    return "'" + str(s).replace("'", "''") + "'"


def dashboard_url(cfg):
    dash = cfg["dashboard"]
    host = dash["host"]
    return f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{dash['port']}/"


def dashboard_running(url, timeout=1.5):
    """True if this tool's dashboard answers at url (local only: never the network)."""
    # its own opener: a proxy set in Windows' settings must not see 127.0.0.1
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url + "api/version", timeout=timeout) as r:
            return r.headers.get("Server", "").startswith("steam-snapshot/")
    except (OSError, ValueError):
        return False


def start_dashboard(cfg):
    """`serve` in the background, with no window and detached from this process."""
    argv = [pythonw(), "-m", "steam_snapshot"] + (["--config", cfg["_file"]] if cfg["_file"] else []) + ["serve"]
    quiet = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        return subprocess.Popen(argv, cwd=config.PROJECT_DIR, creationflags=flags, **quiet)
    return subprocess.Popen(argv, cwd=config.PROJECT_DIR, start_new_session=True, **quiet)


def cmd_open(cfg, args, wait=15.0):
    """What the desktop shortcut runs: start the dashboard unless it already
    runs (in a terminal or from an earlier click), then open it."""
    url = dashboard_url(cfg)
    if not dashboard_running(url):
        start_dashboard(cfg)
        deadline = time.time() + wait
        while not dashboard_running(url):
            if time.time() > deadline:
                snapshot.log_line(cfg["data_dir"], f"open: the dashboard did not start on {url}")
                print(f"the dashboard did not start on {url}; try `serve` to see why", file=sys.stderr)
                return 1
            time.sleep(0.3)
    webbrowser.open(url)
    return 0


def shortcut_script(target, arguments, workdir, icon, name="Steamshot"):
    """PowerShell that writes <Desktop>\<name>.lnk. Every value is a quoted
    literal (ps_quote), so a folder name can never become a command."""
    return "\n".join([
        "$desk = [Environment]::GetFolderPath('Desktop')",
        f"$lnk = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $desk {ps_quote(name + '.lnk')}))",
        f"$lnk.TargetPath = {ps_quote(target)}",
        f"$lnk.Arguments = {ps_quote(arguments)}",
        f"$lnk.WorkingDirectory = {ps_quote(workdir)}",
        f"$lnk.IconLocation = {ps_quote(icon + ',0')}",
        "$lnk.Description = 'Open the Steamshot dashboard'",
        "$lnk.Save()",
        "Write-Output $lnk.FullName",
    ])


def cmd_shortcut(cfg, args):
    """A desktop icon that runs `open`: Windows only."""
    if sys.platform != "win32":
        print("the desktop shortcut is Windows only; elsewhere run `python -m steam_snapshot open`")
        return 1
    conf = cfg["_file"]
    arguments = "-m steam_snapshot" + (f' --config "{conf}"' if conf else "") + " open"
    icon = os.path.join(config.PROJECT_DIR, "assets", "steamshot.ico")
    script = shortcut_script(pythonw(), arguments, config.PROJECT_DIR, icon)
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                       capture_output=True, text=True)
    if r.returncode:
        print("could not create the shortcut: " + (r.stderr.strip() or "powershell failed"), file=sys.stderr)
        return 1
    print(f"created {r.stdout.strip()}: double-click it to start the dashboard and open it")
    print("an older Steamshot.url shortcut (it only opened the page) can be deleted")
    return 0


def cmd_schedule(cfg, args):
    every = args.every
    if every < 5 or (every >= 60 and every % 60) or (every < 60 and 60 % every):
        print("--every must be 5-60 minutes dividing an hour (5, 10, 15, 20, 30, 60) "
              "or a whole number of hours (120, 180, ...)", file=sys.stderr)
        return 1
    project = config.PROJECT_DIR
    conf = cfg["_file"]
    if sys.platform == "win32":
        # Windows paths cannot contain ", so "..." is safe inside the argument
        argument = "-m steam_snapshot" + (f' --config "{conf}"' if conf else "") + " snapshot"
        print("# Paste into PowerShell (no admin needed). Runs while you are logged in.")
        print(f"$action = New-ScheduledTaskAction -Execute {ps_quote(pythonw())} "
              f"-Argument {ps_quote(argument)} -WorkingDirectory {ps_quote(project)}")
        print("$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) "
              f"-RepetitionInterval (New-TimeSpan -Minutes {every})")
        print("$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable "
              "-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
              "-ExecutionTimeLimit (New-TimeSpan -Minutes 10)")
        print(f"Register-ScheduledTask -TaskName '{TASK_NAME}' -Action $action "
              "-Trigger $trigger -Settings $settings "
              "-Description 'Snapshot of the Steam library (steam-snapshot)'")
        print()
        print("# Run it once now / remove it later:")
        print(f"Start-ScheduledTask -TaskName '{TASK_NAME}'")
        print(f"Unregister-ScheduledTask -TaskName '{TASK_NAME}' -Confirm:$false")
    else:
        when = f"*/{every} * * * *" if every < 60 else (
            "0 * * * *" if every == 60 else f"0 */{every // 60} * * *")
        cmd = (f"cd {shlex.quote(project)} && {shlex.quote(sys.executable)} -m steam_snapshot"
               + (f" --config {shlex.quote(conf)}" if conf else "") + " snapshot")
        print("# Run `crontab -e` and add this line:")
        print(f"{when} {cmd.replace('%', chr(92) + '%')} >/dev/null 2>&1")  # cron reads % as a newline
        print()
        print("# Each run appends one line to the log, so you can check it is firing:")
        print(f"#   tail {shlex.quote(os.path.join(cfg['data_dir'], snapshot.LOG_NAME))}")
    return 0


def demo_days():
    """Enough demo history to reach back to 1 January of last year, so Year in
    review has a full previous year to compare with."""
    today = datetime.now().date()
    return (today - today.replace(year=today.year - 1, month=1, day=1)).days + 1


def cmd_demo(cfg, args):
    from . import demo
    target = os.path.abspath(args.data_dir or os.path.join(config.PROJECT_DIR, "demo-data"))
    acct = demo.generate(target, days=args.days)
    print(f"demo data: {args.days} days of invented snapshots in {acct}")
    if args.no_serve:
        return 0
    args.data_dir, args.host, args.port, args.demo = target, None, args.port, True
    return cmd_serve(cfg, args)


def cmd_init(cfg, args):
    src = os.path.join(config.PROJECT_DIR, "config.example.toml")
    dst = os.path.join(config.PROJECT_DIR, "config.toml")
    if os.path.exists(dst):
        print(f"{dst} already exists - leaving it alone")
        return 0
    shutil.copyfile(src, dst)
    print(f"created {dst} - edit it, then run `python -m steam_snapshot doctor`")
    return 0


def cmd_export(cfg, args):
    from . import transfer
    out = args.out or f"steamshot-export-{datetime.now():%Y-%m-%d}.zip"
    try:
        s = transfer.export(cfg["data_dir"], out, args.account)
    except (transfer.TransferError, OSError) as e:
        print(f"export failed: {e}", file=sys.stderr)
        return 1
    for acct, n in s["snapshots"].items():
        print(f"exported {acct}: {n} snapshot{'' if n == 1 else 's'} and the playtime history")
    print(f"  -> {s['path']}")
    return 0


def default_account(cfg):
    """For a bare history file: this data folder's only account, else the one
    a snapshot would read."""
    from . import transfer
    have = transfer.accounts(cfg["data_dir"])
    if len(have) == 1:
        return have[0]
    root = steamfiles.find_steam_root(cfg["steam"]["path"])
    acct = steamfiles.pick_account(root, cfg["steam"]["account"]) if root else None
    return acct["steamid64"] if acct else None


def cmd_import(cfg, args):
    from . import transfer
    try:
        account = args.account
        if not account and not transfer.zipfile.is_zipfile(args.file):
            account = default_account(cfg)
        results = transfer.import_file(cfg["data_dir"], args.file, account)
    except (transfer.TransferError, OSError) as e:
        print(f"import failed: {e}", file=sys.stderr)
        return 1
    for r in results:
        print(f"imported into {r['account']}: {r['days']} day{'' if r['days'] == 1 else 's'} of history, "
              f"{r['snapshots']} snapshot{'' if r['snapshots'] == 1 else 's'}"
              + (f" (history now starts {r['since']})" if r["since"] else ""))
        if not r["days"] and not r["snapshots"]:
            print("  nothing new: every day and snapshot in the file was already here")
    snapshot.log_line(cfg["data_dir"], "import " + os.path.basename(args.file) + ": "
                      + "; ".join(f"{r['account']} +{r['days']} days +{r['snapshots']} snapshots" for r in results))
    return 0


def main(argv=None):
    if sys.version_info < (3, 11):
        print("steam-snapshot needs Python 3.11 or newer", file=sys.stderr)
        return 1
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    p = argparse.ArgumentParser(prog="steam_snapshot",
                                description="Scheduled snapshots of your Steam library.")
    p.add_argument("--config", help="path to config.toml")
    p.add_argument("--version", action="version", version=f"steam-snapshot {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("snapshot", help="take a snapshot now (this is what the scheduler runs)")
    s = sub.add_parser("serve", help="start the dashboard")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.add_argument("--data-dir", help="serve a different data folder")
    s.add_argument("--open", action="store_true", help="open it in the browser")
    sub.add_parser("open", help="start the dashboard in the background if needed, then open it")
    sub.add_parser("shortcut", help="Windows: put a desktop icon that runs `open`")
    sub.add_parser("doctor", help="show what the tool can see; changes nothing")
    s = sub.add_parser("schedule", help="print the command that schedules snapshots")
    s.add_argument("--every", type=int, default=30, help="minutes between runs (default 30)")
    s = sub.add_parser("demo", help="generate invented data and open the dashboard on it")
    s.add_argument("--days", type=int, default=demo_days())
    s.add_argument("--data-dir", help="where to write it (default: ./demo-data)")
    s.add_argument("--port", type=int)
    s.add_argument("--open", action="store_true")
    s.add_argument("--no-serve", action="store_true", help="only write the data")
    sub.add_parser("init", help="create config.toml from the example")
    s = sub.add_parser("export", help="save snapshots and history to a zip (backup, or to move PCs)")
    s.add_argument("--out", help="the zip to write (default: steamshot-export-<date>.zip here)")
    s.add_argument("--account", help="only this SteamID64 (default: every account)")
    s = sub.add_parser("import", help="merge an export zip, or a history file, into this install")
    s.add_argument("file", help="a Steamshot export .zip, or a history .json")
    s.add_argument("--account", help="SteamID64 a history .json belongs to (default: this PC's account)")
    args = p.parse_args(argv)
    try:
        cfg = config.load(args.config)
    except config.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    return {"snapshot": cmd_snapshot, "serve": cmd_serve, "open": cmd_open, "shortcut": cmd_shortcut, "doctor": cmd_doctor,
            "schedule": cmd_schedule, "demo": cmd_demo, "init": cmd_init,
            "export": cmd_export, "import": cmd_import}[args.cmd](cfg, args)
