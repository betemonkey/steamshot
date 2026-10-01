"""Command line: snapshot, serve, doctor, schedule, demo, init."""
import argparse
import os
import shlex
import shutil
import sys
import webbrowser
from datetime import datetime

from . import __version__, config, snapshot, steamfiles

TASK_NAME = "steam-snapshot"


def cmd_snapshot(cfg, args):
    try:
        s = snapshot.take(cfg)
    except snapshot.SnapshotError as e:
        snapshot.log_line(cfg["data_dir"], f"FAILED: {e}")
        print(f"snapshot failed: {e}", file=sys.stderr)
        return 1
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
        httpd = make_server(data_dir, host, port, {"images": bool(dash["images"])})
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
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


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
    games, src = steamfiles.read_library(root, chosen, cfg["snapshot"]["hide_appids"],
                                         cfg["snapshot"]["include_unowned"])
    print("  Sources")
    print(f"    {ok(src['localconfig'])} playtime         userdata/.../localconfig.vdf")
    print(f"    {ok(src['appinfo'])} names + types    appcache/appinfo.vdf")
    print(f"    {ok(src['packageinfo'])} licences         appcache/packageinfo.vdf"
          + ("" if src["packageinfo"] else "  (ownership not filtered)"))
    print(f"    {ok(src['collections'])} collections      userdata/.../cloudstorage")
    print(f"    {ok(src['installedApps'])} installed        {src['installedApps']} games in "
          f"{src['libraryFolders']} library folder(s)")
    hours = sum(g["minutes"] for g in games) / 60
    unnamed = sum(1 for g in games if not g["name"])
    print(f"  Library      {len(games)} games, {hours:,.0f} h"
          + (f" ({unnamed} names to look up online)" if unnamed else ""))
    online = cfg["online"]
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


def cmd_schedule(cfg, args):
    every = args.every
    if every < 5 or (every >= 60 and every % 60) or (every < 60 and 60 % every):
        print("--every must be 5-60 minutes dividing an hour (5, 10, 15, 20, 30, 60) "
              "or a whole number of hours (120, 180, ...)", file=sys.stderr)
        return 1
    project = config.PROJECT_DIR
    conf = cfg["_file"]
    if sys.platform == "win32":
        argument = "-m steam_snapshot" + (f' --config "{conf}"' if conf else "") + " snapshot"
        print("# Paste into PowerShell (no admin needed). Runs while you are logged in.")
        print(f"$action = New-ScheduledTaskAction -Execute '{pythonw()}' "
              f"-Argument '{argument}' -WorkingDirectory '{project}'")
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
        print(f"{when} {cmd} >/dev/null 2>&1")
        print()
        print("# Each run appends one line to the log, so you can check it is firing:")
        print(f"#   tail {shlex.quote(os.path.join(cfg['data_dir'], snapshot.LOG_NAME))}")
    return 0


def cmd_demo(cfg, args):
    from . import demo
    target = os.path.abspath(args.data_dir or os.path.join(config.PROJECT_DIR, "demo-data"))
    acct = demo.generate(target, days=args.days)
    print(f"demo data: {args.days} days of invented snapshots in {acct}")
    if args.no_serve:
        return 0
    args.data_dir, args.host, args.port = target, None, args.port
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
    sub.add_parser("doctor", help="show what the tool can see; changes nothing")
    s = sub.add_parser("schedule", help="print the command that schedules snapshots")
    s.add_argument("--every", type=int, default=30, help="minutes between runs (default 30)")
    s = sub.add_parser("demo", help="generate invented data and open the dashboard on it")
    s.add_argument("--days", type=int, default=60)
    s.add_argument("--data-dir", help="where to write it (default: ./demo-data)")
    s.add_argument("--port", type=int)
    s.add_argument("--open", action="store_true")
    s.add_argument("--no-serve", action="store_true", help="only write the data")
    sub.add_parser("init", help="create config.toml from the example")
    args = p.parse_args(argv)
    try:
        cfg = config.load(args.config)
    except config.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    return {"snapshot": cmd_snapshot, "serve": cmd_serve, "doctor": cmd_doctor,
            "schedule": cmd_schedule, "demo": cmd_demo, "init": cmd_init}[args.cmd](cfg, args)
