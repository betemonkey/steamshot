"""Configuration: a TOML file, every key optional.

Lookup order for the file: --config, then $STEAM_SNAPSHOT_CONFIG, then
config.toml in the project folder. With no file at all the defaults below
apply, which is a working setup for one Steam account on one machine.
"""
import os
import tomllib

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULTS = {
    "steam": {
        "path": "auto",
        "account": "auto",
    },
    "snapshot": {
        "data_dir": "data",
        "keep_days": 365,
        "include_unowned": False,
        "hide_appids": [],
    },
    "online": {
        "enabled": True,
        "wishlist": True,
        "country": "US",
        "language": "english",
    },
    "dashboard": {
        "host": "127.0.0.1",
        "port": 8765,
        "images": True,
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
        except (OSError, tomllib.TOMLDecodeError) as e:
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

    snap = cfg["snapshot"]
    try:
        snap["keep_days"] = int(snap["keep_days"])
        snap["hide_appids"] = [int(a) for a in snap["hide_appids"]]
        cfg["dashboard"]["port"] = int(cfg["dashboard"]["port"])
    except (TypeError, ValueError) as e:
        raise ConfigError(f"bad value: {e}") from e
    base = os.path.dirname(path) if path else PROJECT_DIR
    data_dir = os.path.expanduser(str(snap["data_dir"]))
    cfg["data_dir"] = os.path.normpath(data_dir if os.path.isabs(data_dir)
                                       else os.path.join(base, data_dir))
    cfg["_file"] = path
    return cfg
