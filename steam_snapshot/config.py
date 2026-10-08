"""Configuration: a TOML file, every key optional.

Lookup order for the file: --config, then $STEAM_SNAPSHOT_CONFIG, then
config.toml in the project folder. With no file at all the defaults below
apply, which is a working setup for one Steam account on one machine.
"""
import os
import re
import tomllib

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API_KEY_ENV = "STEAM_SNAPSHOT_API_KEY"
API_KEY_RE = re.compile(r"\A[0-9A-Fa-f]{32}\Z")

DEFAULTS = {
    "steam": {
        "path": "auto",
        "account": "auto",
    },
    "snapshot": {
        "data_dir": "data",
        "keep_days": 0,
        "include_unowned": False,
        "hide_appids": [],
    },
    "online": {
        "enabled": True,
        "wishlist": True,
        "country": "US",
        "language": "english",
        "steam_api_key": "",
    },
    "dashboard": {
        "host": "127.0.0.1",
        "port": 8765,
        "images": True,
    },
    "updates": {
        "check": True,
        "auto": True,
    },
}


class ConfigError(Exception):
    pass


def find_config(explicit=None):
    if explicit:
        if not os.path.isfile(explicit):
            raise ConfigError(f"config file not found: {explicit}")
        return os.path.abspath(explicit)
    env = os.environ.get("STEAM_SNAPSHOT_CONFIG")
    if env:
        if not os.path.isfile(env):
            raise ConfigError(f"STEAM_SNAPSHOT_CONFIG points to a missing file: {env}")
        return os.path.abspath(env)
    local = os.path.join(PROJECT_DIR, "config.toml")
    return local if os.path.isfile(local) else None


def load(explicit=None):
    """The merged config as a dict, plus "_file" (path or None) and an
    absolute "data_dir"."""
    path = find_config(explicit)
    user = {}
    if path:
        try:
            with open(path, "rb") as fh:
                user = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError, RecursionError) as e:
            raise ConfigError(f"{path}: {e}") from e
    cfg = {}
    for section, values in DEFAULTS.items():
        given = user.get(section) or {}
        if not isinstance(given, dict):
            raise ConfigError(f"[{section}] must be a table")
        unknown = set(given) - set(values)
        if unknown:
            raise ConfigError(f"unknown key(s) in [{section}]: {', '.join(sorted(unknown))}")
        cfg[section] = {**values, **given}
    unknown = set(user) - set(DEFAULTS)
    if unknown:
        raise ConfigError(f"unknown section(s): {', '.join(sorted(unknown))}")

    account = cfg["steam"]["account"]
    if isinstance(account, int) and not isinstance(account, bool):
        cfg["steam"]["account"] = str(account)  # a SteamID64 written without quotes
    # a switch must be a real true/false: "false" in quotes would count as on
    for section, values in DEFAULTS.items():
        for key, default in values.items():
            if isinstance(default, (bool, str)) and type(cfg[section][key]) is not type(default):
                raise ConfigError(f"[{section}] {key} must be "
                                  + ("true or false" if isinstance(default, bool) else "a string in quotes"))
    snap = cfg["snapshot"]
    if not isinstance(snap["hide_appids"], list):
        raise ConfigError("[snapshot] hide_appids must be a list, like [480, 620]")
    try:
        snap["keep_days"] = int(snap["keep_days"])
        snap["hide_appids"] = [int(a) for a in snap["hide_appids"]]
        cfg["dashboard"]["port"] = int(cfg["dashboard"]["port"])
    except (TypeError, ValueError, OverflowError) as e:
        raise ConfigError(f"bad value: {e}") from e
    if not 0 <= snap["keep_days"] <= 36500:
        raise ConfigError("[snapshot] keep_days must be 0 (keep everything) to 36500")
    if not 1 <= cfg["dashboard"]["port"] <= 65535:
        raise ConfigError("[dashboard] port must be 1-65535")
    # optional Steam Web API key: the environment wins over the file. The
    # message never repeats the value - it is a secret.
    key = os.environ.get(API_KEY_ENV, "").strip() or cfg["online"]["steam_api_key"].strip()
    if key and not API_KEY_RE.match(key):
        raise ConfigError("[online] steam_api_key (or $" + API_KEY_ENV + ") is not a Steam Web "
                          "API key: it should be 32 letters and digits, from "
                          "https://steamcommunity.com/dev/apikey")
    cfg["online"]["steam_api_key"] = key
    account = cfg["steam"]["account"].strip()
    if account != "auto" and not (account.isascii() and account.isdigit() and len(account) <= 20):
        raise ConfigError('[steam] account must be "auto", a SteamID64 or an account id')
    base = os.path.dirname(path) if path else PROJECT_DIR
    data_dir = os.path.expanduser(str(snap["data_dir"]))
    cfg["data_dir"] = os.path.normpath(data_dir if os.path.isabs(data_dir)
                                       else os.path.join(base, data_dir))
    cfg["_file"] = path
    return cfg
