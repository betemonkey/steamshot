"""Run with:  python -m unittest discover tests

Everything runs against a fake Steam folder built in a temp dir, offline."""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from steam_snapshot import config, demo, snapshot, steamfiles  # noqa: E402
from steam_snapshot.server import make_server  # noqa: E402
from tests import fixtures  # noqa: E402


class TempDir(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()


class ParserTests(TempDir):
    def setUp(self):
        super().setUp()
        self.root = fixtures.make_steam(os.path.join(self.tmp, "Steam"))

    def test_localconfig_merges_duplicate_app_blocks(self):
        apps = steamfiles.parse_localconfig(os.path.join(
            self.root, "userdata", str(fixtures.ACCOUNT), "config", "localconfig.vdf"))
        self.assertEqual(apps[620], {"minutes": 600, "lastPlayed": 1700000000})
        # 130 appears in two apps blocks; the larger counter wins
        self.assertEqual(apps[130]["minutes"], 120)
        self.assertEqual(apps[130]["lastPlayed"], 1600000000)

    def test_appinfo_v29_reads_name_and_type_from_common(self):
        info = steamfiles.parse_appinfo(os.path.join(self.root, "appcache", "appinfo.vdf"))
        self.assertEqual(info[620], {"name": "Portal 2", "type": "game"})
        self.assertEqual(info[228980]["type"], "tool")
        self.assertEqual(info[777]["type"], "demo")

    def test_appinfo_v28(self):
        path = os.path.join(self.tmp, "old.vdf")
        with open(path, "wb") as fh:
            fh.write(fixtures.appinfo_v28({10: ("Counter-Strike", "Game")}))
        self.assertEqual(steamfiles.parse_appinfo(path), {10: {"name": "Counter-Strike", "type": "game"}})

    def test_appinfo_unknown_format_degrades(self):
        path = os.path.join(self.tmp, "future.vdf")
        with open(path, "wb") as fh:
            fh.write(b"\x99\x99\x99\x99" + b"\x00" * 64)
        self.assertEqual(steamfiles.parse_appinfo(path), {})
        self.assertEqual(steamfiles.parse_appinfo(os.path.join(self.tmp, "missing.vdf")), {})

    def test_packageinfo(self):
        owned = steamfiles.parse_packageinfo(os.path.join(self.root, "appcache", "packageinfo.vdf"))
        self.assertEqual(owned, {620, 130, 888, 999, 1000, 1001})

    def test_packageinfo_garbage_is_unknown(self):
        path = os.path.join(self.tmp, "bad.vdf")
        with open(path, "wb") as fh:
            fh.write(b"\x28\x55\x56\x06\x01\x00\x00\x00" + b"\x05" * 50)
        self.assertEqual(steamfiles.parse_packageinfo(path), set())

    def test_collections_skip_builtins_by_key(self):
        cols, hidden, fav = steamfiles.parse_collections(self.root, fixtures.ACCOUNT)
        self.assertEqual(cols[620], ["Racing", "Strategy"])
        self.assertEqual(cols[999], ["Strategy"])
        self.assertEqual(hidden, {888})
        self.assertEqual(fav, {620})
        self.assertNotIn("Old", sum(cols.values(), []))

    def test_installed_across_library_folders(self):
        inst = steamfiles.parse_installed(steamfiles.library_dirs(self.root))
        self.assertEqual(inst, {620: 1000000, 1000: 2000000})

    def test_accounts_and_persona(self):
        accts = steamfiles.list_accounts(self.root)
        self.assertEqual(len(accts), 1)
        self.assertEqual(accts[0]["steamid64"], fixtures.STEAMID64)
        self.assertEqual(accts[0]["persona"], 'Test "Player"')
        self.assertEqual(steamfiles.pick_account(self.root, fixtures.STEAMID64)["accountId"], fixtures.ACCOUNT)
        self.assertEqual(steamfiles.pick_account(self.root, str(fixtures.ACCOUNT))["accountId"], fixtures.ACCOUNT)
        self.assertIsNone(steamfiles.pick_account(self.root, "1"))

    def test_read_library(self):
        acct = steamfiles.pick_account(self.root)
        games, src = steamfiles.read_library(self.root, acct)
        by = {g["appid"]: g for g in games}
        # tools, the Spacewar shim and the unlicensed demo are out
        self.assertNotIn(228980, by)
        self.assertNotIn(480, by)
        self.assertNotIn(777, by)
        self.assertEqual(set(by), {620, 130, 888, 999, 1000})
        self.assertTrue(by[620]["installed"] and by[620]["favourite"])
        self.assertEqual(by[620]["collections"], ["Racing", "Strategy"])
        self.assertTrue(by[888]["hidden"])
        self.assertEqual(by[999]["minutes"], 0)  # filed in a collection, never launched
        self.assertTrue(by[1000]["owned"])
        self.assertTrue(all(src[k] for k in ("localconfig", "appinfo", "packageinfo", "collections")))

    def test_read_library_options(self):
        acct = steamfiles.pick_account(self.root)
        games, _ = steamfiles.read_library(self.root, acct, hide_appids=[130], include_unowned=True)
        by = {g["appid"]: g for g in games}
        self.assertNotIn(130, by)
        self.assertIs(by[777]["owned"], False)

    def test_foreign_licence_list_is_ignored(self):
        """A licence cache that covers almost none of the played games belongs
        to another account; it must not empty this one's library."""
        root = fixtures.make_steam(os.path.join(self.tmp, "Steam2"), packages={9: [5, 6]})
        games, src = steamfiles.read_library(root, steamfiles.pick_account(root))
        self.assertFalse(src["packageinfo"])
        self.assertIn(777, {g["appid"] for g in games})
        self.assertTrue(all("owned" not in g for g in games))


class SnapshotTests(TempDir):
    def setUp(self):
        super().setUp()
        self.root = fixtures.make_steam(os.path.join(self.tmp, "Steam"))
        cfg_path = os.path.join(self.tmp, "config.toml")
        with open(cfg_path, "w", encoding="utf-8") as fh:
            fh.write(f'[steam]\npath = "{self.root.replace(chr(92), "/")}"\n'
                     '[online]\nenabled = false\n[snapshot]\nkeep_days = 3\n')
        self.cfg = config.load(cfg_path)

    def acct_dir(self):
        return snapshot.account_dir(self.cfg["data_dir"], fixtures.STEAMID64)

    def test_snapshot_writes_files(self):
        s = snapshot.take(self.cfg, now=datetime(2026, 3, 1, 12))
        self.assertEqual(s["games"], 5)
        snap = snapshot.read_json(s["path"])
        self.assertEqual(snap["date"], "2026-03-01")
        self.assertEqual(snap["wishlistStatus"], "off")
        self.assertEqual(snap["account"]["persona"], 'Test "Player"')
        self.assertTrue(all(g["name"] for g in snap["games"]))
        log = steamfiles.read_text(os.path.join(self.cfg["data_dir"], snapshot.LOG_NAME))
        self.assertIn("ok " + fixtures.STEAMID64, log)

    def test_history_deltas_and_retention(self):
        snapshot.take(self.cfg, now=datetime(2026, 3, 1, 12))
        lc = os.path.join(self.root, "userdata", str(fixtures.ACCOUNT), "config", "localconfig.vdf")
        text = steamfiles.read_text(lc)
        with open(lc, "w", encoding="utf-8") as fh:
            fh.write(text.replace('"Playtime"\t\t"600"', '"Playtime"\t\t"660"'))
        snapshot.take(self.cfg, now=datetime(2026, 3, 2, 12))
        hist = snapshot.read_json(os.path.join(self.acct_dir(), "history.json"))
        self.assertEqual(hist["since"], "2026-03-01")
        # the first run only records a baseline; the second gains 60 minutes
        self.assertEqual(hist["days"], {"2026-03-02": {"620": 60}})
        # five days later the 3-day retention drops the old snapshots
        snapshot.take(self.cfg, now=datetime(2026, 3, 7, 12))
        self.assertEqual(snapshot.snapshot_dates(self.acct_dir()), ["2026-03-07"])

    def test_same_day_replaces(self):
        snapshot.take(self.cfg, now=datetime(2026, 3, 1, 9))
        snapshot.take(self.cfg, now=datetime(2026, 3, 1, 21))
        self.assertEqual(snapshot.snapshot_dates(self.acct_dir()), ["2026-03-01"])

    def test_refuses_empty(self):
        os.remove(os.path.join(self.root, "userdata", str(fixtures.ACCOUNT), "config", "localconfig.vdf"))
        with self.assertRaises(snapshot.SnapshotError):
            snapshot.take(self.cfg)


class ConfigTests(TempDir):
    def write(self, text):
        path = os.path.join(self.tmp, "c.toml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def test_defaults_and_relative_data_dir(self):
        cfg = config.load(self.write("[snapshot]\ndata_dir = 'snaps'\n"))
        self.assertEqual(cfg["data_dir"], os.path.join(self.tmp, "snaps"))
        self.assertEqual(cfg["dashboard"]["port"], 8765)

    def test_unknown_key_is_an_error(self):
        with self.assertRaises(config.ConfigError):
            config.load(self.write("[snapshot]\nkeep_dayz = 3\n"))

    def test_example_config_is_valid(self):
        cfg = config.load(os.path.join(config.PROJECT_DIR, "config.example.toml"))
        self.assertEqual(cfg["steam"]["account"], "auto")


class ServerTests(TempDir):
    def test_endpoints_on_demo_data(self):
        demo.generate(self.tmp, days=10)
        httpd = make_server(self.tmp, "127.0.0.1", 0, {"images": False})
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        get = lambda p: urllib.request.urlopen(f"http://127.0.0.1:{port}{p}", timeout=5)  # noqa: E731
        try:
            accts = json.load(get("/api/accounts"))
            self.assertEqual(accts["accounts"][0]["steamid64"], demo.DEMO_ID)
            snaps = json.load(get(f"/api/snapshots?account={demo.DEMO_ID}"))
            self.assertEqual(len(snaps), 10)
            snap = json.load(get(f"/api/snapshot?account={demo.DEMO_ID}"))
            self.assertEqual(snap["previous"]["date"], snaps[-2]["date"])
            hist = json.load(get(f"/api/history?account={demo.DEMO_ID}"))
            self.assertTrue(hist["days"])
            self.assertIn(b"Steam snapshot", get("/").read())
            for bad in ("/api/snapshot?account=../x", "/api/snapshot?account=1&date=../../etc"):
                with self.assertRaises(urllib.error.HTTPError) as e:
                    get(bad)
                self.assertEqual(e.exception.code, 400)
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
