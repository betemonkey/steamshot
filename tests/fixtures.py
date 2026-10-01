"""Build a fake Steam folder on disk: every file the reader touches, in the
real formats, with invented content."""
import json
import os
import struct

ACCOUNT = 12345
STEAMID64 = str(76561197960265728 + ACCOUNT)


def kv_v29(strings, pairs):
    """Binary KV for appinfo 0x29: keys are indexes into the string table."""
    out = b""
    for kind, key, value in pairs:
        if kind == "close":
            out += b"\x08"
            continue
        if key not in strings:
            strings.append(key)
        k = struct.pack("<i", strings.index(key))
        if kind == "open":
            out += b"\x00" + k
        elif kind == "str":
            out += b"\x01" + k + value.encode() + b"\x00"
        elif kind == "close":
            out += b"\x08"
    return out


def appinfo_v29(apps):
    """apps: {appid: (name, type)} -> bytes of a 0x29 appinfo.vdf."""
    strings, body = [], b""
    for appid, (name, kind) in apps.items():
        kv = kv_v29(strings, [
            ("open", "appinfo", None), ("str", "appid", str(appid)),
            # a "type" key in a deeper section first, to prove the reader
            # takes the one from "common"
            ("open", "config", None), ("str", "type", "default"), ("close", None, None),
            ("open", "common", None), ("str", "name", name), ("str", "type", kind),
            ("close", None, None), ("close", None, None), ("close", None, None)])
        entry = b"\x00" * 60 + kv
        body += struct.pack("<II", appid, len(entry)) + entry
    body += struct.pack("<I", 0)
    table_off = 16 + len(body)
    table = struct.pack("<i", len(strings)) + b"".join(s.encode() + b"\x00" for s in strings)
    header = struct.pack("<IIq", 0x07564429, 1, table_off)
    return header + body + table


def appinfo_v28(apps):
    """Older layout: keys inline."""
    body = b""
    for appid, (name, kind) in apps.items():
        kv = (b"\x00appinfo\x00\x00common\x00\x01name\x00" + name.encode() + b"\x00"
              + b"\x01type\x00" + kind.encode() + b"\x00\x08\x08\x08")
        entry = b"\x00" * 60 + kv
        body += struct.pack("<II", appid, len(entry)) + entry
    body += struct.pack("<I", 0)
    return struct.pack("<II", 0x07564428, 1) + body


def packageinfo(packages):
    """packages: {packageid: [appids]} -> bytes of a v0x28 packageinfo.vdf."""
    out = struct.pack("<II", 0x06565528, 1)
    for pid, appids in packages.items():
        out += struct.pack("<I", pid) + b"\x00" * 20 + struct.pack("<I", 1) + b"\x00" * 8
        kv = b"\x00" + str(pid).encode() + b"\x00"
        kv += b"\x02packageid\x00" + struct.pack("<i", pid)
        kv += b"\x00appids\x00"
        for i, a in enumerate(appids):
            kv += b"\x02" + str(i).encode() + b"\x00" + struct.pack("<i", a)
        kv += b"\x08"            # end appids
        kv += b"\x01name\x00pkg\x00"
        kv += b"\x08\x08"        # end package block, end of this record
        out += kv
    return out + struct.pack("<I", 0xFFFFFFFF)


LOCALCONFIG = """"UserLocalConfigStore"
{
\t"Software"
\t{
\t\t"Valve"
\t\t{
\t\t\t"Steam"
\t\t\t{
\t\t\t\t"apps"
\t\t\t\t{
\t\t\t\t\t"620"
\t\t\t\t\t{
\t\t\t\t\t\t"LastPlayed"\t\t"1700000000"
\t\t\t\t\t\t"Playtime"\t\t"600"
\t\t\t\t\t}
\t\t\t\t\t"130"
\t\t\t\t\t{
\t\t\t\t\t\t"LastPlayed"\t\t"1600000000"
\t\t\t\t\t\t"Playtime"\t\t"90"
\t\t\t\t\t}
\t\t\t\t\t"228980"
\t\t\t\t\t{
\t\t\t\t\t\t"Playtime"\t\t"5"
\t\t\t\t\t}
\t\t\t\t\t"480"
\t\t\t\t\t{
\t\t\t\t\t\t"Playtime"\t\t"300"
\t\t\t\t\t}
\t\t\t\t\t"777"
\t\t\t\t\t{
\t\t\t\t\t\t"Playtime"\t\t"45"
\t\t\t\t\t\t"LastPlayed"\t\t"1650000000"
\t\t\t\t\t}
\t\t\t\t\t"888"
\t\t\t\t\t{
\t\t\t\t\t\t"Playtime"\t\t"12"
\t\t\t\t\t}
\t\t\t\t}
\t\t\t}
\t\t}
\t}
\t"apps"
\t{
\t\t"620"
\t\t{
\t\t\t"cloud"
\t\t\t{
\t\t\t\t"last_sync_state"\t\t"synchronized"
\t\t\t}
\t\t}
\t\t"130"
\t\t{
\t\t\t"Playtime"\t\t"120"
\t\t}
\t}
}
"""

LOGINUSERS = f""""users"
{{
\t"{STEAMID64}"
\t{{
\t\t"AccountName"\t\t"secret_login"
\t\t"PersonaName"\t\t"Test \\"Player\\""
\t\t"RememberPassword"\t\t"1"
\t}}
}}
"""


def collections_file():
    def rec(key, name, added, deleted=False):
        value = json.dumps({"id": key.split(".", 1)[1], "name": name, "added": added, "removed": []})
        r = {"key": key, "timestamp": 1, "value": value, "version": "1",
             "conflictResolutionMethod": "custom", "strMethodId": "union-collections"}
        if deleted:
            r = {"key": key, "timestamp": 1, "is_deleted": True, "version": "1"}
        return [key, r]
    return [
        rec("user-collections.hidden", "Hidden", [888]),
        rec("user-collections.favorite", "Favoriten", [620]),
        rec("user-collections.uc-aaa", "Strategy", [620, 130, 999]),
        rec("user-collections.uc-bbb", "Racing", [620]),
        rec("user-collections.uc-old", "Old", [], deleted=True),
        ["showcases.1", {"key": "showcases.1", "value": "{}"}],
    ]


def make_steam(root, appinfo_bytes=None, packages=None):
    """Write a fake Steam install under root and return root."""
    cfg = os.path.join(root, "userdata", str(ACCOUNT), "config")
    os.makedirs(os.path.join(cfg, "cloudstorage"))
    os.makedirs(os.path.join(root, "appcache"))
    os.makedirs(os.path.join(root, "config"))
    lib2 = os.path.join(root, "library2")
    os.makedirs(os.path.join(root, "steamapps"))
    os.makedirs(os.path.join(lib2, "steamapps"))
    with open(os.path.join(cfg, "localconfig.vdf"), "w", encoding="utf-8") as fh:
        fh.write(LOCALCONFIG)
    with open(os.path.join(root, "config", "loginusers.vdf"), "w", encoding="utf-8") as fh:
        fh.write(LOGINUSERS)
    with open(os.path.join(cfg, "cloudstorage", "cloud-storage-namespace-1.json"), "w",
              encoding="utf-8") as fh:
        json.dump(collections_file(), fh)
    apps = {620: ("Portal 2", "Game"), 130: ("Half-Life: Blue Shift", "Game"), 228980: ("Steamworks Common Redistributables", "Tool"),
            777: ("Some Demo", "Demo"), 888: ("Hidden Game", "Game"), 999: ("Unplayed Game", "Game"),
            1000: ("Installed Only", "Game")}
    with open(os.path.join(root, "appcache", "appinfo.vdf"), "wb") as fh:
        fh.write(appinfo_bytes if appinfo_bytes is not None else appinfo_v29(apps))
    with open(os.path.join(root, "appcache", "packageinfo.vdf"), "wb") as fh:
        # 777 (the demo) is not licensed any more
        fh.write(packageinfo(packages or {1: [620, 130], 2: [888, 999], 3: [1000, 1001]}))
    with open(os.path.join(root, "steamapps", "libraryfolders.vdf"), "w", encoding="utf-8") as fh:
        fh.write('"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"%s"\n\t}\n\t"1"\n\t{\n\t\t"path"\t\t"%s"\n\t}\n}\n'
                 % (root.replace("\\", "\\\\"), lib2.replace("\\", "\\\\")))
    with open(os.path.join(root, "steamapps", "appmanifest_620.acf"), "w", encoding="utf-8") as fh:
        fh.write('"AppState"\n{\n\t"appid"\t\t"620"\n\t"SizeOnDisk"\t\t"1000000"\n}\n')
    with open(os.path.join(lib2, "steamapps", "appmanifest_1000.acf"), "w", encoding="utf-8") as fh:
        fh.write('"AppState"\n{\n\t"appid"\t\t"1000"\n\t"SizeOnDisk"\t\t"2000000"\n}\n')
    return root
