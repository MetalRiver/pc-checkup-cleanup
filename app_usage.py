"""Read installed desktop apps and the current user's Windows Explorer launch evidence."""
import codecs
import json
import os
from pathlib import Path
import re
import subprocess
import time
import winreg


UNINSTALL = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
USER_ASSIST = r"Software\Microsoft\Windows\CurrentVersion\Explorer\UserAssist"
EXE_COUNT = r"{CEBFF5CD-ACE2-4F4F-9178-9926F41749EA}\Count"
FOLDERS = {
    "{905e63b6-c1bf-494e-b29c-65b732d3d21a}": "ProgramFiles",
    "{6d809377-6af0-444b-8957-a3773f02200e}": "ProgramW6432",
    "{7c5a40ef-a0fb-4bfc-874a-c0f2e0b9fa8e}": "ProgramFiles(x86)",
    "{f1b32785-6fba-4fcf-9d55-7b8e7f157091}": "LOCALAPPDATA",
    "{3eb685db-65f9-4cf6-a03a-e3ef65729f3d}": "APPDATA",
}
REVIEW_AFTER_DAYS = 7
FILETIME_UNIX = 116444736000000000


def executable(value):
    match = re.fullmatch(r'\s*"?([^"]+?\.exe)"?\s*(?:,\s*-?\d+)?\s*', value, re.I)
    if not match:
        return None
    expanded = os.path.expandvars(match.group(1))
    lead, separator, rest = expanded.partition("\\")
    if lead.casefold() in FOLDERS:
        folder = os.environ.get(FOLDERS[lead.casefold()])
        if not folder:
            return None
        expanded = folder + separator + rest
    candidate = Path(expanded)
    return candidate if re.fullmatch(r"[A-Za-z]:", candidate.drive) and candidate.is_absolute() else None


def last_recorded_launches():
    launches = {}
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, USER_ASSIST + "\\" + EXE_COUNT)
    except OSError:
        return launches
    with key:
        for index in range(winreg.QueryInfoKey(key)[1]):
            try:
                name, data, kind = winreg.EnumValue(key, index)
            except OSError:
                continue
            if kind != winreg.REG_BINARY or len(data) < 68:
                continue
            path = executable(codecs.decode(name, "rot_13"))
            if path is not None:
                # FILETIME is in 100-nanosecond ticks since 1601-01-01 UTC.
                ticks = int.from_bytes(data[60:68], "little")
                if ticks > FILETIME_UNIX:
                    timestamp = (ticks - FILETIME_UNIX) / 10_000_000
                    launches[path] = max(timestamp, launches.get(path, 0))
    return launches


def installed_entries():
    views = ((winreg.HKEY_CURRENT_USER, 0),
             (winreg.HKEY_CURRENT_USER, winreg.KEY_WOW64_32KEY),
             (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
             (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY))
    for hive, view in views:
        try:
            key = winreg.OpenKey(hive, UNINSTALL, 0, winreg.KEY_READ | view)
            with key:
                for index in range(winreg.QueryInfoKey(key)[0]):
                    try:
                        with winreg.OpenKey(key, winreg.EnumKey(key, index)) as app:
                            values = dict(winreg.EnumValue(app, i)[:2] for i in range(winreg.QueryInfoKey(app)[1]))
                    except OSError:
                        continue
                    if (values.get("DisplayName")
                            and values.get("SystemComponent") not in (1, "1") and not values.get("ParentKeyName")
                            and not values.get("ReleaseType")):
                        yield values
        except OSError:
            continue


def usage_days(recorded, now):
    if recorded is None or not 0 < recorded <= now:
        return None
    return int((now - recorded) // (24 * 3600))


def running_executables():
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             "[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); "
             "ConvertTo-Json -Compress -InputObject @(Get-Process -ErrorAction SilentlyContinue | "
             "ForEach-Object { $_.Path } | Where-Object { $_ })"],
            check=True, capture_output=True, encoding="utf-8", timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return {Path(value) for value in json.loads(result.stdout) if value}
    except (OSError, ValueError, subprocess.SubprocessError):
        return set()


def usage_review():
    launches = last_recorded_launches()
    running = running_executables()
    seen = set()
    now = time.time()
    for app in installed_entries():
        name = str(app["DisplayName"]).strip()
        if not name:
            continue
        key = name.casefold(), str(app.get("Publisher") or "").casefold()
        if key in seen:
            continue
        seen.add(key)
        path = executable(str(app.get("DisplayIcon", "")))
        if path and re.match(r"^(unins|uninstall|setup|update|msiexec)", path.stem, re.I):
            path = None
        location = Path(os.path.expandvars(str(app.get("InstallLocation") or "").strip().strip('"')))
        if not location.is_absolute() or not re.fullmatch(r"[A-Za-z]:", location.drive) or len(location.parts) < 3:
            location = None
        # Only a dedicated matching installation folder may contribute alternate executable evidence.
        if path and location and location not in path.parents:
            location = None
        candidates = {item for item in launches.keys() | running
                      if item == path or (location and location in item.parents)}
        active = bool(candidates & running)
        recorded = max((launches.get(item, 0) for item in candidates), default=0)
        days = usage_days(recorded, now)
        yield {"name": name, "publisher": str(app.get("Publisher") or ""),
               "path": str(location or path or ""), "running": active, "recordedDays": days,
               "recordedAt": time.strftime("%Y-%m-%d %H:%M", time.localtime(recorded)) if days is not None else "",
               "review": "keep" if active or (days is not None and days < REVIEW_AFTER_DAYS) else "unknown"}
