"""The "From your Steam profile" block (optional API key): fetch throttling,
the numbers it shows, the hostile-file cleaning, and the two endpoints.
No test touches the network; every Steam call is mocked."""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from steam_snapshot import config, demo, server, snapshot, steamapi, store  # noqa: E402
from steam_snapshot.server import make_server  # noqa: E402
from tests import fixtures  # noqa: E402

KEY = "0123456789abcdef0123456789ABCDEF"
NOW = 2_000_000_000
DAY = 86400
ICON = "https://steamcdn-a.akamaihd.net/steamcommunity/public/images/apps/620/" + "a" * 40 + ".jpg"
ICON_OUT = "https://cdn.akamai.steamstatic.com/steamcommunity/public/images/apps/620/" + "a" * 40 + ".jpg"


def game(minutes, last=1000, stats=True, name="", two=0, **plat):
    return {"name": name, "minutes": minutes, "lastPlayed": last, "minutes2w": two, "stats": stats,
            "platforms": {p: plat.get(p, 0) for p in ("windows", "mac", "linux", "deck")}}


class FetchPlanTests(unittest.TestCase):
    def test_first_runs_ask_at_most_25_most_played_first(self):
        owned = {a: game(a) for a in range(1, 41)}
        plan = steamapi.to_fetch(owned, {}, NOW)
        self.assertEqual(len(plan), steamapi.ACH_PER_RUN)
        self.assertEqual(plan[:3], [40, 39, 38])

    def test_only_played_games_with_stats(self):
        owned = {1: game(0), 2: game(50, stats=False), 3: game(50)}
        self.assertEqual(steamapi.to_fetch(owned, {}, NOW), [3])

    def test_played_since_is_asked_again_and_goes_first(self):
        owned = {1: game(10, last=500), 2: game(10, last=900), 3: game(99)}
        cache = {"1": {"t": NOW, "lp": 500}, "2": {"t": NOW, "lp": 800}}
        self.assertEqual(steamapi.to_fetch(owned, cache, NOW), [2, 3])

    def test_week_old_answers_are_refreshed(self):
        owned = {1: game(10, last=500), 2: game(10, last=500)}
        cache = {"1": {"t": NOW - 8 * DAY, "lp": 500}, "2": {"t": NOW - 2 * DAY, "lp": 500}}
        self.assertEqual(steamapi.to_fetch(owned, cache, NOW), [1])


class SummaryTests(unittest.TestCase):
    def test_untracked_is_what_no_device_counter_holds(self):
        owned = {1: game(100, windows=60, deck=10), 2: game(30, windows=40)}  # 2 over-counts: clamp
        self.assertEqual(steamapi.devices(owned),
                         {"windows": 100, "deck": 10, "linux": 0, "mac": 0, "untracked": 30})

    def test_closest_skips_small_and_complete_games(self):
        owned = {a: game(60, name=f"G{a}") for a in (1, 2, 3, 4, 5, 6)}
        cache = {"1": {"n": 1, "u": 1}, "2": {"n": 50, "u": 50}, "3": {"n": 40, "u": 38},
                 "4": {"n": 11, "u": 10}, "5": {"n": 9, "u": 8}, "6": {"n": 20, "u": 0}}
        out = steamapi.summarise(owned, cache, {}, NOW)
        self.assertEqual([r["appid"] for r in out["closest"]], [4, 3])
        self.assertEqual(out["achievements"], {"unlocked": 107, "total": 131, "complete": 1, "small": 2})

    def test_latest_three_newest_first_with_icons(self):
        owned = {620: game(60, name="Portal 2"), 7: game(5, name="Other")}
        cache = {"620": {"n": 20, "u": 3, "recent": [["A", 300, "Alpha"], ["B", 100, "Beta"]]},
                 "7": {"n": 10, "u": 2, "recent": [["C", 200, "Gamma"], ["D", 50, ""]]}}
        icons = {"620": {"t": NOW, "icons": {"A": ICON_OUT}}}
        latest = steamapi.summarise(owned, cache, icons, NOW)["latest"]
        self.assertEqual([(r["name"], r["game"]) for r in latest],
                         [("Alpha", "Portal 2"), ("Gamma", "Other"), ("Beta", "Portal 2")])
        self.assertEqual(latest[0]["icon"], ICON_OUT)
        self.assertEqual(latest[1]["icon"], "")

    def test_two_weeks_most_first(self):
        owned = {1: game(9, two=5), 2: game(9, two=40), 3: game(9)}
        self.assertEqual([r["appid"] for r in steamapi.summarise(owned, {}, {}, NOW)["twoWeeks"]], [2, 1])


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def run_update(self, owned, ach, icons=None, now=NOW):
        notes = []
        with mock.patch.object(store, "player_achievements", side_effect=ach) as pa, \
                mock.patch.object(store, "achievement_icons", return_value=icons or {}) as ai:
            steamapi.update(self.dir, fixtures.STEAMID64, KEY, owned, notes, now)
        return pa, ai, notes

    def read(self, name=steamapi.SUMMARY):
        return snapshot.read_json(os.path.join(self.dir, name))

    def test_no_stats_is_remembered_but_a_failure_is_not(self):
        owned = {1: game(10), 2: game(20)}

        def ach(sid, appid, key, lang):
            if appid == 1:
                return None
            raise store.StoreError("URLError: down")
        self.run_update(owned, ach)
        cache = self.read(steamapi.CACHE)["games"]
        self.assertIn("1", cache)
        self.assertNotIn("2", cache)
        pa, _, _ = self.run_update(owned, ach, now=NOW + 60)
        self.assertEqual([c.args[1] for c in pa.call_args_list], [2])

    def test_three_failures_stop_the_run(self):
        owned = {a: game(a) for a in range(1, 10)}
        pa, _, notes = self.run_update(owned, store.StoreError("HTTPError: 403"))
        self.assertEqual(pa.call_count, 3)
        self.assertTrue(any("retry next run" in n for n in notes))
        self.assertIsNotNone(self.read())  # the block is still written

    def test_icons_only_for_latest_and_cached(self):
        owned = {620: game(60, name="Portal 2")}
        res = {"total": 20, "unlocked": 1, "recent": [["A", 300, "Alpha"]]}
        _, ai, _ = self.run_update(owned, lambda *a: res, {"A": ICON_OUT})
        self.assertEqual(ai.call_count, 1)
        self.assertEqual(self.read()["latest"][0]["icon"], ICON_OUT)
        _, ai, _ = self.run_update(owned, lambda *a: res, {"A": ICON_OUT}, now=NOW + DAY)
        ai.assert_not_called()


class CleanTests(unittest.TestCase):
    def test_hostile_file_is_rebuilt(self):
        evil = {"schema": 1, "fetched": "x", "key": KEY,
                "devices": {"windows": -5, "deck": True, "linux": 3, "evil": 9},
                "achievements": {"unlocked": 5, "total": "9"},
                "twoWeeks": [{"appid": 0, "minutes": 9}, {"appid": 620, "minutes": 7, "name": 5}, "x"],
                "closest": [{"appid": 620, "unlocked": 99, "total": 10, "name": "<b>x</b>"}],
                "latest": [{"appid": 620, "name": "n", "t": 1, "icon": "javascript:alert(1)"},
                           {"appid": 620, "name": "m", "t": 2, "icon": ICON}]}
        out = steamapi.clean(evil)
        self.assertNotIn(KEY, json.dumps(out))
        self.assertEqual(out["devices"], {"windows": 0, "deck": 0, "linux": 3, "mac": 0, "untracked": 0})
        self.assertEqual(out["achievements"]["total"], 0)
        self.assertEqual(out["twoWeeks"], [{"appid": 620, "name": "", "minutes": 7}])
        self.assertEqual(out["closest"][0]["unlocked"], 10)  # never more than the total
        self.assertEqual([r["icon"] for r in out["latest"]], ["", ICON_OUT])
        self.assertIsNone(steamapi.clean({"schema": 2}))
        self.assertIsNone(steamapi.clean([1, 2]))


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        server.Handler._now_cache.clear()

    def tearDown(self):
        self._tmp.cleanup()

    def serve(self, cfg=None):
        httpd = make_server(self.tmp, "127.0.0.1", 0, {"images": False}, cfg=cfg)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return lambda p: urllib.request.urlopen(f"http://127.0.0.1:{port}{p}", timeout=5)

    def write_block(self, account):
        owned = {620: game(60, name="Portal 2", windows=40)}
        snapshot.write_json(os.path.join(self.tmp, account, steamapi.SUMMARY),
                            steamapi.summarise(owned, {}, {}, NOW))

    def test_no_block_is_404_and_no_steam_call(self):
        get = self.serve({"online": {"enabled": True, "steam_api_key": KEY}})
        with self.assertRaises(urllib.error.HTTPError) as e:
            get(f"/api/steamapi?account={fixtures.STEAMID64}")
        self.assertEqual(e.exception.code, 404)
        with mock.patch.object(store, "get_json") as gj:
            self.assertEqual(json.load(get(f"/api/nowplaying?account={fixtures.STEAMID64}")), {})
        gj.assert_not_called()  # not one of our accounts: never a proxy for any id

    def test_now_playing_asks_steam_once_a_minute_and_returns_two_fields(self):
        self.write_block(fixtures.STEAMID64)
        get = self.serve({"online": {"enabled": True, "steam_api_key": KEY}})
        answer = {"response": {"players": [{"steamid": fixtures.STEAMID64, "personaname": "Someone",
                                             "realname": "Private", "gameid": "620",
                                             "gameextrainfo": "Portal 2", "loccountrycode": "FR"}]}}
        with mock.patch.object(store, "get_json", return_value=answer) as gj:
            a = get(f"/api/nowplaying?account={fixtures.STEAMID64}").read()
            b = get(f"/api/nowplaying?account={fixtures.STEAMID64}").read()
        self.assertEqual(gj.call_count, 1)
        self.assertEqual(json.loads(a), {"appid": 620, "name": "Portal 2"})
        self.assertEqual(a, b)
        for body in (a, get(f"/api/steamapi?account={fixtures.STEAMID64}").read()):
            self.assertNotIn(KEY.encode(), body)

    def test_without_key_or_offline_nothing_is_asked(self):
        self.write_block(fixtures.STEAMID64)
        for cfg in (None, {"online": {"enabled": True, "steam_api_key": ""}},
                    {"online": {"enabled": False, "steam_api_key": KEY}}):
            server.Handler._now_cache.clear()
            get = self.serve(cfg)
            with mock.patch.object(store, "get_json") as gj:
                self.assertEqual(json.load(get(f"/api/nowplaying?account={fixtures.STEAMID64}")), {})
            gj.assert_not_called()

    def test_demo_never_reaches_steam(self):
        demo.generate(self.tmp, days=3)
        get = self.serve({"online": {"enabled": True, "steam_api_key": KEY}})
        with mock.patch.object(store, "get_json") as gj:
            self.assertEqual(json.load(get(f"/api/nowplaying?account={demo.DEMO_ID}")), demo.NOW_PLAYING)
            block = json.load(get(f"/api/steamapi?account={demo.DEMO_ID}"))
        gj.assert_not_called()
        self.assertEqual(block["achievements"]["small"], 1)  # THE FINALS: 1 achievement
        self.assertNotIn(2073850, [r["appid"] for r in block["closest"]])


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.root = fixtures.make_steam(os.path.join(self.tmp, "Steam"))
        env = mock.patch.dict(os.environ, {config.API_KEY_ENV: ""})
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def take(self, online=True, key=KEY):
        path = os.path.join(self.tmp, "config.toml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f'[steam]\npath = "{self.root.replace(chr(92), "/")}"\n[online]\n'
                     f'enabled = {"true" if online else "false"}\nwishlist = false\n'
                     f'steam_api_key = "{key}"\n')
        cfg = config.load(path)
        owned = {"response": {"games": [
            {"appid": 620, "playtime_forever": 100, "playtime_windows_forever": 70,
             "rtime_last_played": 5, "has_community_visible_stats": True, "name": "Portal 2"},
            {"appid": 130, "playtime_forever": 0}]}}
        ach = {"playerstats": {"success": True, "achievements": [
            {"apiname": "A", "achieved": 1, "unlocktime": 9, "name": "Alpha"},
            {"apiname": "B", "achieved": 0, "unlocktime": 0, "name": "Beta"}]}}

        def fake(url, timeout=20):
            return ach if "GetPlayerAchievements" in url else \
                {"game": {}} if "GetSchemaForGame" in url else owned
        with mock.patch.object(snapshot, "resolve_names"), mock.patch.object(snapshot, "resolve_art"), \
                mock.patch.object(store, "get_json", side_effect=fake) as gj:
            snapshot.take(cfg, now=datetime(2026, 3, 1, 12))
        acct = os.path.join(cfg["data_dir"], fixtures.STEAMID64)
        return acct, gj

    def test_snapshot_writes_the_block_without_the_key(self):
        acct, gj = self.take()
        block = snapshot.read_json(os.path.join(acct, steamapi.SUMMARY))
        self.assertEqual(block["devices"]["untracked"], 30)
        self.assertEqual(block["achievements"]["unlocked"], 1)
        self.assertEqual(block["latest"][0]["name"], "Alpha")
        self.assertEqual(sum("GetOwnedGames" in c.args[0] for c in gj.call_args_list), 1)  # one call, not two
        for fn in (steamapi.SUMMARY, steamapi.CACHE):
            with open(os.path.join(acct, fn), encoding="utf-8") as fh:
                self.assertNotIn(KEY, fh.read())

    def test_no_key_or_offline_writes_no_block(self):
        for online, key in ((True, ""), (False, KEY)):
            acct, gj = self.take(online, key)
            self.assertFalse(os.path.exists(os.path.join(acct, steamapi.SUMMARY)))
            gj.assert_not_called()


if __name__ == "__main__":
    unittest.main()
