"""Synthetic demo data, so the dashboard can be tried before the first real
snapshot exists (and so screenshots never show anyone's real library).

The games are real Steam titles so the cover art loads; every number - hours,
dates, collections, the account - is invented. Upcoming wishlist entries are
fictional on purpose, so no real game gets a made-up release date.
"""
import os
import random
import shutil
from datetime import datetime, timedelta

from . import __version__
from .snapshot import SCHEMA, update_history, write_json

DEMO_ID = "76500000000000000"  # not a valid SteamID64 on purpose

# appid, name, starting hours, collection, installed GB, how often it is played
GAMES = [
    (289070, "Sid Meier's Civilization VI", 212, "Strategy", 20, 4),
    (294100, "RimWorld", 164, "Strategy", 0.5, 3),
    (427520, "Factorio", 205, "Strategy", 3, 2),
    (1158310, "Crusader Kings III", 58, "Strategy", 0, 0),
    (813780, "Age of Empires II: Definitive Edition", 77, "Strategy", 0, 1),
    (268500, "XCOM 2", 41, "Strategy", 0, 0),
    (292030, "The Witcher 3: Wild Hunt", 97, "Open world", 0, 0),
    (1091500, "Cyberpunk 2077", 58, "Open world", 70, 3),
    (990080, "Hogwarts Legacy", 36, "Open world", 0, 0),
    (2215430, "Ghost of Tsushima DIRECTOR'S CUT", 44, "Open world", 0, 1),
    (1151640, "Horizon Zero Dawn Complete Edition", 51, "Open world", 0, 0),
    (255710, "Cities: Skylines", 88, "Simulation", 0, 1),
    (227300, "Euro Truck Simulator 2", 131, "Simulation", 8, 5),
    (413150, "Stardew Valley", 89, "Simulation", 0.6, 2),
    (1290000, "PowerWash Simulator", 23, "Simulation", 0, 1),
    (526870, "Satisfactory", 71, "Simulation", 20, 2),
    (1250410, "Microsoft Flight Simulator", 17, "Simulation", 0, 0),
    (1551360, "Forza Horizon 5", 63, "Racing", 110, 8),
    (252950, "Rocket League", 77, "Racing", 0, 1),
    (244210, "Assetto Corsa", 29, "Racing", 0, 0),
    (892970, "Valheim", 52, "Survival", 1.5, 2),
    (105600, "Terraria", 140, "Survival", 0, 0),
    (264710, "Subnautica", 38, "Survival", 0, 1),
    (242760, "The Forest", 19, "Survival", 0, 0),
    (578080, "PUBG: BATTLEGROUNDS", 230, "Online", 35, 4),
    (1172470, "Apex Legends", 13, "Online", 0, 0),
    (2073850, "THE FINALS", 21, "Online", 0, 1),
    (1086940, "Baldur's Gate 3", 136, None, 122, 3),
    (1868140, "DAVE THE DIVER", 0, None, 0, 0),
    (391540, "Undertale", 0, None, 0, 0),
    (1174180, "Red Dead Redemption 2", 0, None, 120, 0),
]

WISHLIST = [
    # fictional upcoming entries: days from today, or None for "To be announced"
    (9900001, "Example: Starfall Tactics", 4),
    (9900002, "Example: Lantern Keeper", 26),
    (9900003, "Example: Neon Courier", 71),
    (9900004, "Example: Deep Orbit", None),
    # released games, real dates
    (1817070, "Marvel's Spider-Man Remastered", "2022-08-12"),
    (2050650, "Resident Evil 4", "2023-03-24"),
]


def generate(data_dir, days=60, seed=7):
    """Write `days` daily snapshots ending today into data_dir. Returns the
    account folder. Replaces any earlier demo data in the same place."""
    rng = random.Random(seed)
    acct_dir = os.path.join(data_dir, DEMO_ID)
    if os.path.isdir(acct_dir):
        shutil.rmtree(acct_dir)
    now = datetime.now().astimezone().replace(hour=21, minute=0, second=0, microsecond=0)
    minutes = {a: int(h * 60) for a, _, h, *_ in GAMES}
    last = {a: (int((now - timedelta(days=days + rng.randint(5, 400))).timestamp())
                if h else 0) for a, _, h, *_ in GAMES}
    weights = {a: w for a, *_, w in GAMES}
    late_buy = {1174180: days - 1}  # bought on the last day, so "changes" has one

    for i in range(days):
        day = now - timedelta(days=days - 1 - i)
        weekend = day.weekday() >= 5
        sessions = rng.choice([0, 1, 1, 2, 2, 3] + ([3, 4] if weekend else []))
        if i == days - 1:
            sessions = max(sessions, 3)  # the latest snapshot always has some play
        pool = [a for a, w in weights.items() if w]
        for _ in range(sessions):
            a = rng.choices(pool, weights=[weights[p] for p in pool])[0]
            minutes[a] += rng.randint(20, 150 if weekend else 90)
            last[a] = int(day.timestamp()) - rng.randint(0, 6 * 3600)
        games = []
        for appid, name, _, col, gb, _ in GAMES:
            if appid in late_buy and i < late_buy[appid]:
                continue
            g = {"appid": appid, "name": name, "type": "game",
                 "minutes": minutes[appid], "lastPlayed": last[appid], "owned": True}
            if gb:
                g["installed"] = True
                g["bytes"] = int(gb * 1024 ** 3)
            if col:
                g["collections"] = [col]
            if appid in (289070, 1551360):
                g["favourite"] = True
            games.append(g)
        wishlist = []
        for appid, name, when in WISHLIST:
            if isinstance(when, int):
                d = (now + timedelta(days=when)).date()
                iso, text, coming = d.isoformat(), f"{d.day} {d:%b} {d.year}", True
            elif when is None:
                iso, text, coming = "", "To be announced", True
            else:
                d = datetime.fromisoformat(when).date()
                iso, text, coming = d.isoformat(), f"{d.day} {d:%b} {d.year}", False
            wishlist.append({"appid": appid, "name": name, "added": "2025-11-02",
                             "priority": 0, "releaseISO": iso, "release": text,
                             "comingSoon": coming})
        date = day.date().isoformat()
        write_json(os.path.join(acct_dir, "snapshots", f"{date}.json"), {
            "schema": SCHEMA, "tool": f"steam-snapshot {__version__}", "demo": True,
            "taken": day.isoformat(timespec="seconds"), "date": date,
            "account": {"steamid64": DEMO_ID, "accountId": 0, "persona": "Demo Player"},
            "sources": {"localconfig": True, "appinfo": True, "packageinfo": True,
                        "collections": True, "installedApps": 9, "libraryFolders": 1},
            "games": games, "wishlist": wishlist, "wishlistStatus": "ok", "notes": [],
        })
        update_history(acct_dir, {g["appid"]: g["minutes"] for g in games}, date, 0)
    return acct_dir
