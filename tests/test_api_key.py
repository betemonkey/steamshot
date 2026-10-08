"""The optional Steam Web API key: exact ownership via GetOwnedGames.
No test touches the network; the HTTP call is mocked."""
import io
import os
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from steam_snapshot import cli, config, snapshot, steamfiles, store  # noqa: E402
from tests import fixtures  # noqa: E402

KEY = "0123456789abcdef0123456789ABCDEF"
# what Steam says this account owns: 1000 is installed but borrowed through
# Steam Family, so the licence cache counts it and the API doesn't
OWNED = {620, 130, 999}


class KeyCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.root = fixtures.make_steam(os.path.join(self.tmp, "Steam"))
        env = mock.patch.dict(os.environ, {config.API_KEY_ENV: ""})
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def load(self, extra="", online=True):
        path = os.path.join(self.tmp, "config.toml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f'[steam]\npath = "{self.root.replace(chr(92), "/")}"\n'
                     f'[online]\nenabled = {"true" if online else "false"}\nwishlist = false\n'
                     f'{extra}\n')
        return config.load(path)

    def take(self, cfg):
        # names and art are online lookups too; keep this test about ownership
        with mock.patch.object(snapshot, "resolve_names"), \
                mock.patch.object(snapshot, "resolve_art"):
            s = snapshot.take(cfg, now=datetime(2026, 3, 1, 12))
        return snapshot.read_json(s["path"])


class ConfigKeyTests(KeyCase):
    def test_default_is_off(self):
        self.assertEqual(config.DEFAULTS["online"]["steam_api_key"], "")
        self.assertEqual(self.load()["online"]["steam_api_key"], "")

    def test_valid_key_from_file_and_env(self):
        self.assertEqual(self.load(f'steam_api_key = "{KEY}"')["online"]["steam_api_key"], KEY)
        with mock.patch.dict(os.environ, {config.API_KEY_ENV: KEY.lower()}):
            self.assertEqual(self.load()["online"]["steam_api_key"], KEY.lower())

    def test_bad_key_is_a_config_error_that_does_not_echo_it(self):
        bad = "not-a-key-0123456789abcdef012345"
        with self.assertRaises(config.ConfigError) as cm:
            self.load(f'steam_api_key = "{bad}"')
        self.assertNotIn(bad, str(cm.exception))
        with self.assertRaises(config.ConfigError):
            self.load("steam_api_key = 12")

    def test_cli_exits_2_on_a_bad_key(self):
        path = os.path.join(self.tmp, "bad.toml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('[online]\nsteam_api_key = "short"\n')
        err = io.StringIO()
        with mock.patch("sys.stderr", err), redirect_stdout(io.StringIO()):
            code = cli.main(["--config", path, "doctor"])
        self.assertEqual(code, 2)


class OwnershipTests(KeyCase):
    def test_without_key_nothing_changes(self):
        with mock.patch.object(store, "get_json") as get:
            snap = self.take(self.load())
        get.assert_not_called()
        ids = {g["appid"] for g in snap["games"]}
        self.assertIn(1000, ids)  # the licence cache counts the family game
        self.assertEqual(snap["sources"]["ownership"], "licence-cache")

    def test_api_owned_list_drops_family_and_refunded_games(self):
        resp = {"response": {"game_count": 3, "games": [{"appid": a} for a in OWNED]}}
        with mock.patch.object(store, "get_json", return_value=resp) as get:
            snap = self.take(self.load(f'steam_api_key = "{KEY}"'))
        url = get.call_args[0][0]
        self.assertIn("include_played_free_games=1", url)
        ids = {g["appid"] for g in snap["games"]}
        self.assertNotIn(1000, ids)
        self.assertTrue({620, 130} <= ids)
        self.assertEqual(snap["sources"]["ownership"], "api")

    def test_offline_never_calls_even_with_a_key(self):
        with mock.patch.object(store, "get_json") as get:
            snap = self.take(self.load(f'steam_api_key = "{KEY}"', online=False))
        get.assert_not_called()
        self.assertEqual(snap["sources"]["ownership"], "licence-cache")

    def test_api_failure_falls_back_to_the_licence_cache(self):
        err = urllib.error.HTTPError(f"https://x/?key={KEY}", 403, "Forbidden", {}, None)

        def boom(url, timeout=20):
            raise store.StoreError(f"{type(err).__name__}: {err}")
        with mock.patch.object(store, "get_json", side_effect=boom):
            snap = self.take(self.load(f'steam_api_key = "{KEY}"'))
        self.assertEqual(snap["sources"]["ownership"], "licence-cache")
        self.assertIn(1000, {g["appid"] for g in snap["games"]})
        self.assertTrue(any("licence cache" in n for n in snap["notes"]))

    def test_private_profile_is_not_owns_nothing(self):
        with mock.patch.object(store, "get_json", return_value={"response": {}}):
            with self.assertRaises(store.StoreError):
                store.owned_games(fixtures.STEAMID64, KEY)

    def test_key_never_written_anywhere(self):
        def leaky(url, timeout=20):  # an error that quotes the URL, key and all
            raise store.StoreError(f"failed for {url}")
        cfg = self.load(f'steam_api_key = "{KEY}"')
        with mock.patch.object(store, "get_json", side_effect=leaky):
            self.take(cfg)
        for dirpath, _, files in os.walk(cfg["data_dir"]):
            for fn in files:
                with open(os.path.join(dirpath, fn), encoding="utf-8", errors="replace") as fh:
                    self.assertNotIn(KEY, fh.read(), fn)

    def test_doctor_says_set_but_never_prints_the_key(self):
        path = os.path.join(self.tmp, "config.toml")
        self.load(f'steam_api_key = "{KEY}"')
        resp = {"response": {"games": [{"appid": a} for a in OWNED]}}
        out = io.StringIO()
        with mock.patch.object(store, "get_json", return_value=resp), redirect_stdout(out):
            cli.main(["--config", path, "doctor"])
        text = out.getvalue()
        self.assertIn("set, works: 3 owned games", text)
        self.assertIn("Ownership    api", text)
        self.assertNotIn(KEY, text)

    def test_read_library_marks_installed_family_game_unowned(self):
        acct = steamfiles.pick_account(self.root)
        games, src = steamfiles.read_library(self.root, acct, include_unowned=True,
                                             owned_apps=OWNED)
        by = {g["appid"]: g for g in games}
        self.assertFalse(by[1000]["owned"])
        self.assertTrue(by[620]["owned"])


if __name__ == "__main__":
    unittest.main()
