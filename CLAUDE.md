# CLAUDE.md

Steamshot (`steam_snapshot` package) takes scheduled snapshots of a Steam library by reading the Steam client's own files on disk (no API key, no login, strictly read-only on the Steam folder). It writes one JSON snapshot per day plus a `history.json` of minutes gained per game per day, which it gets by diffing totals between runs. Steam keeps no history, so this can't be backfilled. A stdlib-only web server shows the data as a dashboard on 127.0.0.1. Python 3.11+, **standard library only, no dependencies, no pyproject**. It runs straight from the folder as `python -m steam_snapshot`.

## Commands (Windows dev box: use `py -3`, not `python`)

```powershell
py -3 -m unittest discover tests                          # full suite, offline, ~1s (verified: 27 tests)
py -3 -m unittest tests.test_steam_snapshot.ConfigTests   # one class (verified)
py -3 -m steam_snapshot demo --open                       # invented data in ./demo-data + dashboard on :8765
py -3 -m steam_snapshot demo --no-serve --data-dir <dir>  # just generate demo data (verified)
py -3 -m steam_snapshot doctor                            # read-only diagnostics against the real Steam install
py -3 -m steam_snapshot snapshot                          # real snapshot -> data/ (writes files, may call Steam store API)
py -3 -m steam_snapshot serve --open [--port N] [--data-dir D]
py -3 -m steam_snapshot schedule [--every 30]             # PRINTS a Task Scheduler / cron command; installs nothing
```
There's no linter or formatter config. The code has `# noqa: E402/E731` markers, so keep it flake8-clean in that style.

## Architecture

- `cli.py`: argparse subcommands (`snapshot serve doctor schedule demo init`). Each `cmd_*` takes `(cfg, args)`. Exit codes: 1 = runtime failure, 2 = config error.
- `config.py`: merges TOML over `DEFAULTS`. `PROJECT_DIR` is the repo root. Adds `cfg["data_dir"]` (absolute) and `cfg["_file"]`.
- `steamfiles.py`: every Steam parser: text VDF (`localconfig.vdf`, `loginusers.vdf`, `.acf`), binary VDF (`appinfo.vdf` v28/v29, `packageinfo.vdf`), and the collections JSON in cloudstorage. `read_library()` combines them into `(games, sources)`.
- `store.py`: keyless Steam web endpoints (`IStoreBrowseService/GetItems`, `IWishlistService/GetWishlist`, `appdetails`) via urllib. Raises `StoreError`.
- `snapshot.py`: `take(cfg, now=None)` is the scheduled run: read library, enrich (names/wishlist), write `data/<steamid64>/snapshots/YYYY-MM-DD.json`, update `history.json`, prune past `keep_days`, append to `data/steam-snapshot.log`.
- `update.py`: self-update. Throttled (6 h, state in `data/update.json`) check of the version on `origin/main` via `git fetch` (raw.githubusercontent.com fallback for non-checkouts; the repo is private, so that only works if it goes public), then `git pull --ff-only` only for a clean checkout of `main`. Finds Git for Windows even when it's not on PATH, and never lets git prompt. Called from `cmd_snapshot` and from a watcher thread in `cmd_serve` that restarts the server (`relaunch()`) once the version on disk changes; the page polls `/api/version` and reloads when `running` changes.
- `server.py`: read-only GET server over the data folder (`/api/accounts|snapshots|snapshot|history`) with an mtime-keyed cache. It serves `web/index.html`, a single self-contained page (vanilla JS, light/dark CSS tokens, no external scripts).
- `demo.py`: deterministic fake data under `DEMO_ID = "76500000000000000"`, which is deliberately not a valid SteamID64. The server tests use it.
- `tests/fixtures.py` builds a fake Steam folder in a temp dir, including binary VDF in the real formats. When you change a parser, extend the fixture rather than relying on a real Steam install.

## Config and secrets

- `config.example.toml` is committed and documents every key. `config.toml` is the real per-machine config and is **gitignored**, as are `data/` and `demo-data/`, because they hold the user's library and SteamID. Never commit them, and don't read or edit a user's `config.toml`/`data/` unless asked.
- There are no secrets or API keys anywhere. The only identifier sent over the network is the SteamID64 (for the wishlist).
- Lookup order: `--config`, then `$STEAM_SNAPSHOT_CONFIG`, then `<repo>/config.toml`, then built-in defaults. A relative `data_dir` resolves against the config file's folder.
- Unknown sections or keys are a hard `ConfigError`. **Adding a config key means updating `DEFAULTS`, `config.example.toml` and the README table together.** `test_example_config_is_valid` loads the example file.

## Invariants (easy to break)

- Never write anything under the Steam folder. `steamfiles` only reads.
- Parsers degrade, they don't raise. An unknown format returns "don't know" (empty result or `sources[x] = False`), and `take()` refuses to write an empty snapshot (`SnapshotError`) rather than recording a wiped library.
- A game seen for the first time adds 0 minutes to history, so purchases and first runs never show up as spikes. A later run on the same day replaces that day's snapshot.
- Wishlist: an `items`-less response means a private profile, which returns `None`, not `[]`. On a private or error result the previous snapshot's wishlist carries over. `wishlistStatus` is `ok|private|error|off`.
- Bump `__version__` in `steam_snapshot/__init__.py` for every change that should reach users: installs only update when the version on `main` is higher.
- With `[online] enabled = false`, nothing touches the network (that includes the update check). `store` is imported lazily inside functions, so keep it that way.
- If the licence list (`packageinfo.vdf`) looks like it belongs to another account, the ownership filter switches off instead of hiding the library.
- The snapshot JSON has `"schema": 1`, and the dashboard and old data files depend on its shape (documented in the README). Treat changes to it as a migration.
- The server must stay GET-only and keep validating `account`/`date` with regexes. Path-traversal requests return 400, and a test covers this.
- JSON writes go through a `.tmp` file plus `os.replace`, so the dashboard never reads a half-written file.
- `.gitattributes` forces LF line endings.
