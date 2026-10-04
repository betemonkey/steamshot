# CLAUDE.md

Steamshot (`steam_snapshot` package) takes scheduled snapshots of a Steam library by reading the Steam client's own files on disk (no API key, no login, strictly read-only on the Steam folder). It writes one JSON snapshot per day plus a `history.json` of minutes gained per game per day, which it gets by diffing totals between runs. Steam keeps no history, so this can't be backfilled. A stdlib-only web server shows the data as a dashboard on 127.0.0.1. Python 3.11+, **standard library only, no dependencies, no pyproject**. It runs straight from the folder as `python -m steam_snapshot`.

## Commands (Windows dev box: use `py -3`, not `python`)

```powershell
py -3 -m unittest discover tests                          # full suite, offline, ~1s (verified: 35 tests)
py -3 -m unittest tests.test_steam_snapshot.ConfigTests   # one class (verified)
py -3 -m steam_snapshot demo --open                       # invented data in ./demo-data + dashboard on :8765
py -3 -m steam_snapshot demo --no-serve --data-dir <dir>  # just generate demo data (verified)
py -3 -m steam_snapshot doctor                            # read-only diagnostics against the real Steam install
py -3 -m steam_snapshot snapshot                          # real snapshot -> data/ (writes files, may call Steam store API)
py -3 -m steam_snapshot serve --open [--port N] [--data-dir D]
py -3 -m steam_snapshot open                              # start serve hidden (pythonw, detached) unless it answers, then open the page
py -3 -m steam_snapshot shortcut                          # Windows: writes Desktop\Steamshot.lnk (via PowerShell) that runs `open`
py -3 -m steam_snapshot schedule [--every 30]             # PRINTS a Task Scheduler / cron command; installs nothing
```
There's no linter or formatter config. The code has `# noqa: E402/E731` markers, so keep it flake8-clean in that style.

## Architecture

- `cli.py`: argparse subcommands (`snapshot serve open shortcut doctor schedule demo init export import`). `open` checks `/api/version` for a `Server: steam-snapshot/` header (proxy-free opener) before starting a second server. Each `cmd_*` takes `(cfg, args)`. Exit codes: 1 = runtime failure, 2 = config error.
- `config.py`: merges TOML over `DEFAULTS`. `PROJECT_DIR` is the repo root. Adds `cfg["data_dir"]` (absolute) and `cfg["_file"]`.
- `steamfiles.py`: every Steam parser: text VDF (`localconfig.vdf`, `loginusers.vdf`, `.acf`), binary VDF (`appinfo.vdf` v28/v29, `packageinfo.vdf`), and the collections JSON in cloudstorage. `read_library()` combines them into `(games, sources)`.
- `store.py`: keyless Steam web endpoints (`IStoreBrowseService/GetItems`, `IWishlistService/GetWishlist`, `appdetails`) via urllib. Raises `StoreError`. `art_of()` turns GetItems `include_assets` into artwork URLs: Steam files newer art under a per-image hash, so the plain `steam/apps/<id>/header.jpg` is a 404 for many games. URLs are pattern-checked because they go into the page's HTML.
- `snapshot.py`: `take(cfg, now=None)` is the scheduled run: read library, enrich (names/wishlist, plus `resolve_art()` into `art.json`: missing games now, every game again after 30 days), write `data/<steamid64>/snapshots/YYYY-MM-DD.json`, update `history.json`, prune past `keep_days`, append to `data/steam-snapshot.log`.
- `transfer.py`: `export` (zip of `<id>/history.json`, `names.json`, `snapshots/*.json` plus a manifest) and `import` (that zip, or a bare history JSON). Merge rule: days on/after the local `since` and existing snapshot files are never overwritten; `last` is never imported (no fake spike on the next run). Zip members outside that exact layout are ignored (path-traversal safe, tested). Import is CLI-only on purpose: the server stays GET-only.
- `update.py`: self-update. Throttled (6 h, state in `data/update.json`) check of the version on `origin/main` via `git fetch` (raw.githubusercontent.com fallback for non-checkouts), then `git pull --ff-only` only for a clean checkout of `main`. Finds Git for Windows even when it's not on PATH, never via the current folder (no `shutil.which` on Windows: binary planting), and never lets git or ssh prompt. Called from `cmd_snapshot` and from a watcher thread in `cmd_serve` that restarts the server (`relaunch()`) once the version on disk changes; the page polls `/api/version` and reloads when `running` changes.
- `server.py`: read-only GET server over the data folder (`/api/accounts|snapshots|snapshot|history|art|version`) with an mtime-keyed cache. It serves `web/index.html`, a single self-contained page (vanilla JS, light/dark CSS tokens, no external scripts). Colours come from CSS tokens; the header's colour button picks a palette (`ssPalette`: auto, black, steam, slate, forest, ember, light; `?palette=` forces one), and the dark palettes are `html:root[data-skin=...]` token overrides. The page has two layouts on the same data, Shelf (cover art first) and Tiles (bento grid), picked with a header switch and kept in `localStorage` (`ssView`; `?view=tiles` forces one, handy for screenshots). `facts()` computes the shared numbers once; `shelfView()`/`tilesView()` build the markup. Art comes from Steam's CDN with a fallback chain ending in a plain name tile, which is also what `images = false` shows. Design changes go through `mockups/` first (`mockups/redesign/` has the explored directions). The background is a "cover wall" of the library's art (`drawWall`). **Year in review** (`yir*` functions) is a full-screen slideshow built client-side from `history.json` and `/api/snapshots`: a year's window (1 Jan to the snapshot date, or the whole year) against the same dates a year earlier; `?yir=2026&slide=3` opens it paused on a slide for screenshots. `demo` now generates history back to 1 January of last year (`demo_days()`, with a taste shift in the last ~8 months) so the comparison has data.
- `demo.py`: deterministic fake data under `DEMO_ID = "76500000000000000"`, which is deliberately not a valid SteamID64. The server tests use it.
- `assets/`: the Steamshot icon (`steamshot.svg`, bold `steamshot-small.svg` for 16-24 px, `steamshot.ico` with 16-256 px frames, bold below 32). It is our own drawing in Steam's style; never add Valve's logo to the repo (local mockups keep a copy in the git-ignored `mockups/desktop-icon/_local/`). The server serves only the fixed `STATIC` table (`/`, `/favicon.ico`, `/icon.svg`), with explicit content types.
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
- With `[online] enabled = false`, nothing in Python touches the network (that includes the update check). The page still loads CDN art unless `[dashboard] images = false`. `store` is imported lazily inside functions, so keep it that way.
- If the licence list (`packageinfo.vdf`) looks like it belongs to another account, the ownership filter switches off instead of hiding the library.
- The snapshot JSON has `"schema": 1`, and the dashboard and old data files depend on its shape (documented in the README). Treat changes to it as a migration.
- The server must stay GET-only and keep validating `account`/`date` with regexes (`\A[0-9]...\Z`, not `^\d...$`: `$` allows a trailing newline and `\d` allows non-ASCII digits). Path-traversal requests return 400, and a test covers this. It also rejects a `Host` header that isn't an IP, `localhost` or the configured host (DNS rebinding), and sends a CSP, `X-Frame-Options: DENY` and `Referrer-Policy: no-referrer`.
- Treat imported files as hostile. `transfer.clean_snapshot`, `clean_history`, `snapshot.clean_names` and `clean_art` rebuild them field by field before anything is written, and the server filters `art.json` through `clean_art` again. Anything that reaches the page's HTML (app ids, art URLs) has to be checked this way; the page also re-checks app ids (`aid()`) and art URLs (`ART_OK`).
- JSON writes go through a unique temp file (`mkstemp`, fsync) plus `os.replace`, so the dashboard never reads a half-written file. `take()` holds `data/snapshot.lock`. A damaged `history.json` is moved aside (`load_history`), never treated as empty and overwritten.
- `.gitattributes` forces LF line endings.
