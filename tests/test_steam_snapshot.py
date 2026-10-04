"""Run with:  python -m unittest discover tests

Everything runs against a fake Steam folder built in a temp dir, offline."""
import json
import os
import sys
import tempfile
import threading
import unittest
import zipfile
import urllib.request
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from steam_snapshot import __version__, config, demo, snapshot, steamfiles, store, transfer, update  # noqa: E402
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

    def test_default_keeps_everything(self):
        self.cfg["snapshot"]["keep_days"] = config.DEFAULTS["snapshot"]["keep_days"]
        lc = os.path.join(self.root, "userdata", str(fixtures.ACCOUNT), "config", "localconfig.vdf")
        snapshot.take(self.cfg, now=datetime(2024, 3, 1, 12))
        text = steamfiles.read_text(lc)
        with open(lc, "w", encoding="utf-8") as fh:
            fh.write(text.replace('"Playtime"\t\t"600"', '"Playtime"\t\t"660"'))
        snapshot.take(self.cfg, now=datetime(2024, 3, 2, 12))
        snapshot.take(self.cfg, now=datetime(2026, 3, 1, 12))
        self.assertEqual(snapshot.snapshot_dates(self.acct_dir()),
                         ["2024-03-01", "2024-03-02", "2026-03-01"])
        hist = snapshot.read_json(os.path.join(self.acct_dir(), "history.json"))
        self.assertIn("2024-03-02", hist["days"])

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


class UpdateTests(TempDir):
    def setUp(self):
        super().setUp()
        self.fetched, self.pulled = 0, 0

    def cfg(self, extra=""):
        path = os.path.join(self.tmp, "c.toml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("[snapshot]\ndata_dir = 'data'\n" + extra)
        return config.load(path)

    def fetch(self, version="99.0.0"):
        def f():
            self.fetched += 1
            return version
        return f

    def pull(self, ok=True, reason=""):
        def p():
            self.pulled += 1
            return ok, reason
        return p

    def test_version_order(self):
        self.assertTrue(update.newer("1.10.0", "1.9.2"))
        self.assertFalse(update.newer("1.1.0", "1.1.0"))
        self.assertFalse(update.newer("garbage", "1.0.0"))
        self.assertEqual(update.disk_version(), __version__)

    def test_offline_never_checks(self):
        for extra in ("[online]\nenabled = false\n", "[updates]\ncheck = false\n"):
            cfg = self.cfg(extra)
            update.run(cfg, fetch=self.fetch(), pull=self.pull())
            self.assertEqual(update.status(cfg)["status"], "off")
        self.assertEqual((self.fetched, self.pulled), (0, 0))

    def test_auto_update_is_throttled(self):
        cfg = self.cfg()
        self.assertEqual(update.run(cfg, now=1000, fetch=self.fetch(), pull=self.pull())["status"], "updated")
        update.run(cfg, now=2000, fetch=self.fetch(), pull=self.pull())
        self.assertEqual((self.fetched, self.pulled), (1, 1))
        update.run(cfg, now=1000 + update.CHECK_EVERY, fetch=self.fetch(), pull=self.pull())
        self.assertEqual(self.fetched, 2)
        self.assertIn("updated", steamfiles.read_text(os.path.join(cfg["data_dir"], snapshot.LOG_NAME)))

    def test_cannot_pull_shows_banner(self):
        cfg = self.cfg()
        update.run(cfg, now=1000, fetch=self.fetch(), pull=self.pull(False, "the folder has local changes"))
        st = update.status(cfg)
        self.assertEqual((st["available"], st["status"], st["latest"]), (True, "manual", "99.0.0"))
        self.assertIn("local changes", st["reason"])

    def test_auto_off_and_up_to_date_never_pull(self):
        cfg = self.cfg("[updates]\nauto = false\n")
        self.assertEqual(update.run(cfg, now=1000, fetch=self.fetch(), pull=self.pull())["status"], "available")
        self.assertTrue(update.status(cfg)["available"])
        cfg = self.cfg()
        os.remove(os.path.join(cfg["data_dir"], update.STATE_NAME))
        self.assertEqual(update.run(cfg, now=1000, fetch=self.fetch(__version__), pull=self.pull())["status"], "current")
        self.assertEqual(self.pulled, 0)

    def test_check_failure_is_recorded(self):
        def boom():
            raise OSError("offline")
        cfg = self.cfg()
        self.assertEqual(update.run(cfg, now=1000, fetch=boom, pull=self.pull())["status"], "error")
        self.assertFalse(update.status(cfg)["available"])


class TransferTests(TempDir):
    def acct(self, root, aid=demo.DEMO_ID):
        return snapshot.account_dir(root, aid)

    def test_export_import_round_trip(self):
        src, dst = os.path.join(self.tmp, "a"), os.path.join(self.tmp, "b")
        demo.generate(src, days=10)
        snapshot.write_json(os.path.join(self.acct(src), "art.json"), {"620": {"t": 1, "header": "https://x/h.jpg"}})
        out = transfer.export(src, os.path.join(self.tmp, "x.zip"))
        self.assertEqual(out["snapshots"], {demo.DEMO_ID: 10})
        res = transfer.import_file(dst, out["path"])
        self.assertEqual(res[0]["snapshots"], 10)
        self.assertEqual(snapshot.snapshot_dates(self.acct(dst)), snapshot.snapshot_dates(self.acct(src)))
        a = snapshot.read_json(os.path.join(self.acct(src), "history.json"))
        b = snapshot.read_json(os.path.join(self.acct(dst), "history.json"))
        self.assertEqual((a["since"], a["days"]), (b["since"], b["days"]))
        self.assertEqual(b["last"], {})  # never imported: the next run starts a fresh baseline
        self.assertEqual(snapshot.read_json(os.path.join(self.acct(dst), "art.json"))["620"]["header"], "https://x/h.jpg")
        # importing the same file again adds nothing
        again = transfer.import_file(dst, out["path"])
        self.assertEqual((again[0]["days"], again[0]["snapshots"]), (0, 0))

    def test_merge_keeps_what_this_install_recorded(self):
        local = {"since": "2026-03-05", "days": {"2026-03-05": {"10": 30}}, "last": {"10": 500}}
        incoming = transfer.clean_history({"since": "2026-03-01", "days": {
            "2026-03-01": {"10": 60, "20": "15", "bad": 5},
            "2026-03-05": {"10": 999}, "2026-03-06": {"10": 40}, "nonsense": {"10": 1}}})
        merged, added = transfer.merge_history(local, incoming)
        self.assertEqual(added, 1)
        self.assertEqual(merged["since"], "2026-03-01")
        self.assertEqual(merged["days"], {"2026-03-01": {"10": 60, "20": 15}, "2026-03-05": {"10": 30}})
        self.assertEqual(merged["last"], {"10": 500})

    def test_bare_history_needs_an_account(self):
        path = os.path.join(self.tmp, "h.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"since": "2026-01-01", "days": {"2026-01-02": {"620": 45}}}, fh)
        with self.assertRaises(transfer.TransferError):
            transfer.import_file(self.tmp, path)
        res = transfer.import_file(os.path.join(self.tmp, "d"), path, "76561198000000001")
        self.assertEqual(res[0]["days"], 1)
        h = snapshot.read_json(os.path.join(self.tmp, "d", "76561198000000001", "history.json"))
        self.assertEqual(h["days"], {"2026-01-02": {"620": 45}})
        # a BOM (common from Windows tools) is fine; the earlier day fills in before "since"
        bom = os.path.join(self.tmp, "bom.json")
        with open(bom, "w", encoding="utf-8-sig") as fh:
            json.dump({"days": {"2025-12-31": {"620": 10}}}, fh)
        self.assertEqual(transfer.import_file(os.path.join(self.tmp, "d"), bom, "76561198000000001")[0]["days"], 1)

    def test_zip_cannot_write_outside_the_data_folder(self):
        path = os.path.join(self.tmp, "evil.zip")
        good = json.dumps({"date": "2026-01-01", "games": []})
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("../escape.json", "{}")
            z.writestr("123/../../escape2.json", "{}")
            z.writestr("123/snapshots/../../escape3.json", "{}")
            z.writestr("123/other.txt", "x")
            z.writestr("123/snapshots/2026-01-01.json", good)
        dst = os.path.join(self.tmp, "data")
        res = transfer.import_file(dst, path)
        self.assertEqual(res[0]["snapshots"], 1)
        for name in ("escape.json", "escape2.json", "escape3.json"):
            self.assertFalse(os.path.exists(os.path.join(self.tmp, name)))
        self.assertEqual(sorted(os.listdir(os.path.join(dst, "123"))), ["snapshots"])

    def test_existing_snapshot_is_not_overwritten(self):
        src, dst = os.path.join(self.tmp, "a"), os.path.join(self.tmp, "b")
        demo.generate(src, days=3)
        out = transfer.export(src, os.path.join(self.tmp, "x.zip"))
        day = snapshot.snapshot_dates(self.acct(src))[-1]
        mine = os.path.join(self.acct(dst), "snapshots", f"{day}.json")
        snapshot.write_json(mine, {"date": day, "games": [], "mine": True})
        res = transfer.import_file(dst, out["path"])
        self.assertEqual(res[0]["snapshots"], 2)
        self.assertTrue(snapshot.read_json(mine)["mine"])


class ArtTests(TempDir):
    ITEM = {"appid": 499170, "assets": {
        "asset_url_format": "steam/apps/499170/${FILENAME}?t=1790865583",
        "header": "deda420d9e42288f4734a068c707f4c156203969/header.jpg",
        "library_capsule": "abc123/library_600x900.jpg", "main_capsule": "capsule_616x353.jpg"}}

    def test_art_urls_from_store_item(self):
        art = store.art_of(self.ITEM)
        self.assertEqual(art["header"], "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/499170/"
                                        "deda420d9e42288f4734a068c707f4c156203969/header.jpg?t=1790865583")
        self.assertEqual(set(art), {"header", "library", "capsule"})

    def test_art_rejects_anything_odd(self):
        for fmt in ('steam/apps/1/${FILENAME}"><script>', "https://evil/${FILENAME}", "steam/apps/1/x.jpg"):
            self.assertEqual(store.art_of({"assets": {"asset_url_format": fmt, "header": "h.jpg"}}), {})
        odd = {"asset_url_format": "steam/apps/1/${FILENAME}",
               "header": "../../x.jpg", "main_capsule": "a'b.jpg", "small_capsule": "ok/s.jpg"}
        self.assertEqual(set(store.art_of({"assets": odd})), {"small"})
        self.assertEqual(store.art_of(None), {})

    def test_art_cache_refreshes_monthly(self):
        calls = []

        def fake(ids, country, language, release=False, assets=False):
            calls.append(list(ids))
            return {499170: self.ITEM}
        real, store.get_items = store.get_items, fake
        try:
            online = {"country": "US", "language": "english"}
            notes = []
            snapshot.resolve_art([499170, 42], self.tmp, online, notes, now=1000)
            cache = snapshot.read_json(os.path.join(self.tmp, "art.json"))
            self.assertIn("header", cache["499170"])
            self.assertEqual(cache["42"], {"t": 1000})  # no art: remembered, not asked again
            snapshot.resolve_art([499170, 42], self.tmp, online, notes, now=1000 + 86400)
            self.assertEqual(len(calls), 1)
            snapshot.resolve_art([499170, 42, 7], self.tmp, online, notes, now=1000 + 86400)
            self.assertEqual(calls[-1], ["7"])  # only the new one
            snapshot.resolve_art([499170], self.tmp, online, notes, now=1000 + 31 * 86400)
            self.assertEqual(calls[-1], ["499170"])

            def down(*a, **k):
                raise store.StoreError("offline")
            store.get_items = down
            snapshot.resolve_art([99], self.tmp, online, notes, now=1000)
            self.assertIn("art lookup failed", notes[-1])
            self.assertNotIn("99", snapshot.read_json(os.path.join(self.tmp, "art.json")))
        finally:
            store.get_items = real


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
            self.assertEqual(json.load(get("/api/version"))["running"], __version__)
            self.assertEqual(json.load(get(f"/api/art?account={demo.DEMO_ID}")), {})
            snapshot.write_json(os.path.join(self.tmp, demo.DEMO_ID, "art.json"),
                                {"620": {"t": 1, "header": "https://x/h.jpg"}, "7": {"t": 1}})
            self.assertEqual(json.load(get(f"/api/art?account={demo.DEMO_ID}")), {"620": {"header": "https://x/h.jpg"}})
            for bad in ("/api/snapshot?account=../x", "/api/snapshot?account=1&date=../../etc"):
                with self.assertRaises(urllib.error.HTTPError) as e:
                    get(bad)
                self.assertEqual(e.exception.code, 400)
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
