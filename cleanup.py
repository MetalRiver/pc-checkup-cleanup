"""Local WorkBuddy disk cleanup runner. Requires Python 3.10+ on Windows."""
import argparse
import csv
import ctypes
from ctypes import wintypes
from collections import deque
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid
from datetime import datetime

BASE = Path(__file__).resolve().parent
DAY = 86400
REPARSE = 0x400
ACTIONS = {
    "recycle": "清空回收站",
    "recycle-review": "经 Windows 确认后清空回收站",
    "uninstall": "用应用的卸载入口处理",
    "migrate": "迁移到已核实的目标位置",
    "app-clean": "用应用的存储管理处理",
    "system": "打开相关 Windows 设置",
}


def timestamp():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def dump(path, value):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def load(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def is_link(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & REPARSE)


def disk_rows():
    drives = ctypes.windll.kernel32.GetLogicalDrives()
    rows = []
    for i in range(26):
        drive = chr(65 + i)
        root = drive + ":\\"
        if drives & (1 << i) and ctypes.windll.kernel32.GetDriveTypeW(root) == 3:
            try:
                usage = shutil.disk_usage(root)
                rows.append({"disk": drive, "total": usage.total, "free": usage.free})
            except OSError as exc:
                rows.append({"disk": drive, "error": str(exc)})
    return rows


def sample(disks):
    return {"time": timestamp(), "volumes": [row for row in disk_rows() if row["disk"] in disks]}


def file_identity(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_nlink]


def ordinary_path(path):
    for part in (*reversed(path.parents), path):
        if is_link(part.lstat()):
            raise OSError(f"路径含重解析点：{part}")


def walk_files(root):
    pending = deque([root])
    while pending:
        folder = pending.popleft()
        with os.scandir(folder) as entries:
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                if is_link(info):
                    continue
                if stat.S_ISDIR(info.st_mode):
                    pending.append(Path(entry.path))
                elif stat.S_ISREG(info.st_mode):
                    yield Path(entry.path), info


def basic_plan(disks):
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return [], ["当前用户的 LOCALAPPDATA 不可用。"]
    candidates, notes = [], []
    if "#!sandbox." in local.casefold():
        return [], ["当前运行环境重定向了本地应用数据。基础清理需由 WorkBuddy 的本机用户会话运行。"]
    rules = [
        ("DirectX 着色器缓存", Path(local) / "D3DSCache", True, re.compile(r".+")),
        ("资源管理器缩略图缓存", Path(local) / "Microsoft/Windows/Explorer", False,
         re.compile(r"thumbcache_[a-z0-9_]+\.db", re.I)),
    ]
    cutoff = time.time() - 7 * DAY
    for name, root, recursive, match in rules:
        if root.drive[:1].upper() not in disks:
            continue
        try:
            ordinary_path(root)
            if recursive:
                entries = walk_files(root)
            else:
                entries = ((path, path.lstat()) for path in root.iterdir())
            for path, info in entries:
                if (stat.S_ISREG(info.st_mode) and not is_link(info) and info.st_nlink == 1
                        and info.st_size and info.st_mtime < cutoff and match.fullmatch(path.name)):
                    candidates.append((name, path, file_identity(info)))
        except FileNotFoundError:
            continue
        except OSError as exc:
            notes.append(f"{name}：{exc}")
    return candidates, notes


def remove_known_file(path, expected):
    """Validate and mark the opened file for deletion using the same Windows handle."""
    import msvcrt

    ordinary_path(path)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    open_file = kernel.CreateFileW
    open_file.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                          wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    open_file.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    set_info = kernel.SetFileInformationByHandle
    set_info.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    set_info.restype = wintypes.BOOL
    handle = open_file(str(path), 0x10080, 0x1, None, 3, 0, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY)
    except Exception:
        close(handle)
        raise
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or is_link(info) or file_identity(info) != expected:
            raise OSError("文件已变化，留到下轮排查。")
        disposition = wintypes.BOOL(True)
        if not set_info(handle, 4, ctypes.byref(disposition), ctypes.sizeof(disposition)):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        os.close(fd)


def execute_basic(disks):
    planned, notes = basic_plan(disks)
    before = sample(disks)
    groups = {}
    for name, path, identity in planned:
        key = (path.drive[0].upper(), name)
        group = groups.setdefault(key, {"disk": key[0], "name": name, "done": 0,
                                        "removed": 0, "skipped": 0, "examples": []})
        try:
            remove_known_file(path, identity)
            group["done"] += 1
            group["removed"] += identity[2]
        except OSError as exc:
            group["skipped"] += 1
            if len(group["examples"]) < 5:
                group["examples"].append(f"{path.name}：{exc}")
    return {"before": before, "after": sample(disks), "groups": list(groups.values()), "notes": notes}


def add_entry(drive, path, info):
    directory = stat.S_ISDIR(info.st_mode)
    return {"id": uuid.uuid4().hex, "disk": drive, "path": str(path), "name": path.name,
            "bytes": 0 if directory else info.st_size,
            "coverage": "目录逻辑大小" if directory else "文件逻辑大小",
            "directory": directory, "links": 0, "errors": 0, "pending": directory}


def inspect_volume(drive, seconds, root=None):
    """扫描一个卷。seconds<=0 表示不限时，完整遍历到结束（真·全盘扫描）。"""
    rows, queue, warnings = [], deque(), []
    identities = set()
    deadline = time.monotonic() + seconds if seconds and seconds > 0 else None
    try:
        root = root or Path(drive + ":\\")
        ordinary_path(root)
        for path in root.iterdir():
            if deadline and time.monotonic() >= deadline:
                warnings.append("根目录项过多，已按扫描预算停止列举。")
                break
            try:
                info = path.lstat()
                if is_link(info) or path.name.casefold() == "$recycle.bin":
                    continue
                if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                    continue
                row = add_entry(drive, path, info)
                rows.append(row)
                if row["directory"]:
                    queue.append((row, deque([path])))
                elif info.st_nlink > 1:
                    key = (info.st_dev, info.st_ino)
                    if key in identities:
                        row["bytes"] = 0
                        row["links"] += 1
                    identities.add(key)
            except OSError as exc:
                warnings.append(f"{path}：{exc}")
    except OSError as exc:
        warnings.append(str(exc))
    while queue and (deadline is None or time.monotonic() < deadline):
        row, pending = queue.popleft()
        path = pending.popleft()
        try:
            ordinary_path(path)
            with os.scandir(path) as entries:
                for entry in entries:
                    if deadline and time.monotonic() >= deadline:
                        row["interrupted"] = True
                        break
                    try:
                        info = entry.stat(follow_symlinks=False)
                        if is_link(info):
                            row["links"] += 1
                        elif stat.S_ISDIR(info.st_mode):
                            pending.append(Path(entry.path))
                        elif stat.S_ISREG(info.st_mode):
                            if info.st_nlink > 1:
                                key = (info.st_dev, info.st_ino)
                                if key in identities:
                                    row["links"] += 1
                                    continue
                                identities.add(key)
                            row["bytes"] += info.st_size
                    except OSError:
                        row["errors"] += 1
        except OSError:
            row["errors"] += 1
        if pending:
            queue.append((row, pending))
        else:
            row["pending"] = bool(row.pop("interrupted", False))
    return rows, warnings


def query_native_recycle(drive):
    class QueryInfo(ctypes.Structure):
        _pack_ = 8
        _fields_ = [("cbSize", wintypes.DWORD), ("size", ctypes.c_longlong),
                    ("items", ctypes.c_longlong)]

    query = ctypes.windll.shell32.SHQueryRecycleBinW
    query.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(QueryInfo)]
    query.restype = ctypes.c_long
    info = QueryInfo()
    info.cbSize = ctypes.sizeof(info)
    status = query(drive + ":\\", ctypes.byref(info))
    if status < 0:
        raise OSError(f"Windows 回收站查询返回 0x{status & 0xffffffff:08x}")
    return {"bytes": info.size, "count": info.items}


def recycle_bin(drive):
    try:
        result = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()),
                                 "--native-recycle-query", drive], capture_output=True,
                                text=True, encoding="utf-8", timeout=8, check=False)
    except subprocess.TimeoutExpired as exc:
        raise OSError("Windows 回收站查询超时。") from exc
    if result.returncode:
        raise OSError(result.stderr.strip() or "Windows 回收站查询不可用。")
    return json.loads(result.stdout)


def empty_recycle(drive):
    empty = ctypes.windll.shell32.SHEmptyRecycleBinW
    empty.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.DWORD]
    empty.restype = ctypes.c_long
    status = empty(None, drive + ":\\", 7)
    if status < 0:
        raise OSError(f"Windows 回收站操作返回 0x{status & 0xffffffff:08x}")


def recycle_inventory(drive, sid, *, read_metadata=False):
    folder = Path(drive + ":\\$Recycle.Bin") / sid
    ordinary_path(folder)
    names = []
    item_count = 0
    recorded_bytes = 0
    deadline = time.monotonic() + (3 if read_metadata else 20)
    with os.scandir(folder) as entries:
        for path in entries:
            if time.monotonic() >= deadline:
                if read_metadata:
                    return None, item_count, recorded_bytes
                raise OSError("回收站明细核验未完成。")
            prefix = path.name[:2].upper()
            if prefix not in ("$I", "$R"):
                continue
            info = path.stat(follow_symlinks=False)
            if not read_metadata:
                if prefix == "$I":
                    item_count += 1
                names.append((path.name, *file_identity(info)))
            elif prefix == "$I" and stat.S_ISREG(info.st_mode) and not is_link(info):
                with open(path.path, "rb") as handle:
                    header = handle.read(16)
                if len(header) == 16 and int.from_bytes(header[:8], "little") in (1, 2):
                    item_count += 1
                    recorded_bytes += int.from_bytes(header[8:16], "little")
    content = json.dumps([sid, sorted(names)], ensure_ascii=False).encode()
    return hashlib.sha256(content).hexdigest(), item_count, recorded_bytes


def account_sid():
    output = subprocess.check_output(["whoami.exe", "/user", "/fo", "csv", "/nh"], text=True,
                                     errors="replace")
    sid = next(csv.reader(output.strip().splitlines()))[-1]
    if not re.fullmatch(r"S-\d+(?:-\d+)+", sid):
        raise OSError("无法识别当前用户的回收站范围。")
    return sid


def recycle_review(row):
    return {"id": row["id"], "action": "recycle-review", "estimate": None,
            "recommendation": "请在 Windows 回收站按原位置查看本盘内容。核实范围后处理。",
            "impact": "清空后，文件无法从回收站还原。",
            "target": f"在资源管理器的回收站里核实原位置为 {row['disk']} 盘的项目，仅处理该盘的内容。"}


def deep_scan(disks, seconds):
    found, proposed, warnings = [], [], []
    for disk in disks:
        rows, errors = inspect_volume(disk, seconds)
        found.extend(rows)
        warnings.extend(errors)
        try:
            info = recycle_bin(disk)
        except OSError as exc:
            warnings.append(f"{disk} 盘回收站：{exc}")
            try:
                fingerprint, item_count, recorded_bytes = recycle_inventory(disk, account_sid(), read_metadata=True)
                row = {"id": uuid.uuid4().hex, "disk": disk, "name": "回收站",
                       "path": disk + ":\\$Recycle.Bin", "bytes": recorded_bytes,
                       "coverage": f"当前用户的回收站元数据 · 已读 {item_count} 项 · 等待系统核实",
                       "errors": 0, "links": 0, "pending": fingerprint is None}
                found.append(row)
                if item_count:
                    proposed.append(recycle_review(row))
            except (OSError, subprocess.SubprocessError) as read_error:
                warnings.append(f"{disk} 盘回收站元数据不可读：{read_error}")
            continue
        row = {"id": uuid.uuid4().hex, "disk": disk, "name": "回收站", "path": disk + ":\\$Recycle.Bin",
               "bytes": info["bytes"], "coverage": f"Windows 读数 · {info['count']} 项",
               "errors": 0, "links": 0, "pending": False}
        found.append(row)
        if not info["count"]:
            continue
        try:
            fingerprint, count, _ = recycle_inventory(disk, account_sid())
            if count != info["count"]:
                raise OSError("系统条目数与当前用户条目数不一致，请在回收站里核实。")
            row["fingerprint"] = fingerprint
        except (OSError, subprocess.SubprocessError) as exc:
            warnings.append(f"{disk} 盘回收站待核实：{exc}")
            proposed.append(recycle_review(row))
            continue
        proposed.append({"id": row["id"], "action": "recycle", "estimate": info["bytes"],
                         "observed": info, "recommendation": "先查看有无需要还原的文件。确认都不再需要后清空。",
                         "impact": "清空此盘当前用户的回收站后，内容无法从回收站还原。"})
    proposed.sort(key=lambda item: -(item["estimate"] or 0))
    return sorted(found, key=lambda row: (row["disk"], -row["bytes"])), proposed, warnings


SYSTEM_TOP = {"windows", "program files", "program files (x86)", "programdata",
              "$recycle.bin", "system volume information", "$winreagent", "$sysreset",
              "recovery", "boot", "efi", "documents and settings", "$windows.~bt",
              "$windows.~ws", "perflogs"}
USER_DIRS = {"downloads", "documents", "desktop", "pictures", "videos", "music",
             "onedrive", "下载", "文档", "桌面", "图片", "视频", "音乐"}
BIG_FILE_EXT = {".iso", ".img", ".zip", ".rar", ".7z", ".tar", ".gz", ".xz",
                ".mp4", ".mkv", ".mov", ".avi", ".wmv", ".flv", ".vhd", ".vhdx",
                ".vmdk", ".vdi", ".mdf", ".bak", ".dmp"}
USER_MIN = 512 * 1024 ** 2
FOLDER_MIN = 1024 ** 3
APP_TABLE = {
    "wegame": "WeGame", "steam": "Steam", "steamapps": "Steam", "epic": "Epic Games",
    "origin": "Origin", "battle.net": "暴雪战网", "blizzard": "暴雪战网",
    "honkai": "米哈游（崩坏/星穹铁道）", "genshin": "原神", "mihoyo": "米哈游",
    "naraka": "永劫无间", "qq飞车": "QQ飞车", "tencent": "腾讯游戏",
    "netease": "网易游戏", "wechat": "微信", "微信": "微信", "dingtalk": "钉钉",
    "钉钉": "钉钉", "adobe": "Adobe", "autodesk": "Autodesk", "microsoft office": "Office",
}
MIGRATION_ROOT = "磁盘清理迁移"


def match_app(text):
    low = str(text).casefold()
    for key, name in APP_TABLE.items():
        if key in low:
            return name
    return ""


def classify_path(path):
    """返回 (类别, 应用名)。类别：system / migrate-app / migrate-user / skip。

    判定顺序：先认具体应用/用户数据（层级可深），再判断是否处于系统保留顶层。
    """
    parts = [p.casefold() for p in Path(path).parts]
    head = parts[1] if len(parts) > 1 else ""
    tail = parts[-1] if parts else ""
    if tail in ("pagefile.sys", "hiberfil.sys", "swapfile.sys"):
        return "system", ""
    app = match_app(" ".join(parts))
    if app and len(parts) > 2:
        return "migrate-app", app
    if "appdata" in parts and len(parts) > 4:
        return "migrate-app", match_app(path) or "该应用"
    if any(seg in USER_DIRS for seg in parts):
        return "migrate-user", ""
    if Path(path).suffix.casefold() in BIG_FILE_EXT:
        return "migrate-user", ""
    if head in SYSTEM_TOP:
        return "system", ""
    return "skip", ""


def _migration_target(state, source, path):
    """在可用空间最大的空白盘上生成唯一目标路径。"""
    rows = [r for r in disk_rows() if "free" in r and r["disk"] != source]
    if not rows:
        return None, None
    target = max(rows, key=lambda r: r["free"])
    rel = Path(path)
    parts = [p for p in rel.parts[1:]]
    name = ("_".join(parts) or rel.name or "data")[:120]
    base = Path(target["disk"] + ":\\") / MIGRATION_ROOT / (source + "_" + name)
    dst = base
    seq = 2
    while dst.exists():
        dst = Path(str(base) + f"_{seq}")
        seq += 1
        if seq > 50:
            return None, None
    return str(dst), target


def auto_plan(state, seconds):
    """扫描完成后自动识别可迁移内容与系统项，追加建议。不依赖外部手写清单。"""
    notes = []
    disks = [r for r in disk_rows() if "free" in r]
    if not disks:
        return notes
    src_disk = min((d["disk"] for d in disks if d["disk"] in state["targets"]),
                   key=lambda d: next(x["free"] / x["total"] for x in disks if x["disk"] == d),
                   default=None)
    if not src_disk:
        return notes
    known = {row["path"].casefold(): row for row in state["found"]}
    candidates = {}

    def put(path, size):
        key = str(path).casefold()
        if key in known:
            candidates[key] = (known[key], size)
        elif key not in candidates:
            row = {"id": uuid.uuid4().hex, "disk": src_disk, "path": str(path),
                   "name": Path(path).name, "bytes": size, "coverage": "自动识别的可迁移内容",
                   "directory": Path(path).is_dir(), "links": 0, "errors": 0, "pending": False}
            candidates[key] = (row, size)

    users_root = Path(src_disk + ":\\Users")
    if users_root.is_dir():
        try:
            for user in users_root.iterdir():
                if user.name.casefold() in ("public", "default", "default user", "all users"):
                    continue
                for sub in ("Downloads", "Documents", "Desktop", "Pictures", "Videos", "Music", "OneDrive"):
                    folder = user / sub
                    try:
                        if not folder.is_dir() or is_link(folder.lstat()):
                            continue
                        rows, _ = inspect_volume(src_disk, seconds, folder)
                        size = sum(r["bytes"] for r in rows)
                        if size >= USER_MIN:
                            put(folder, size)
                    except OSError:
                        continue
        except OSError as exc:
            notes.append(f"{src_disk} 盘用户目录枚举受限：{exc}")

    for row in list(state["found"]):
        if row["disk"] != src_disk or not row.get("directory") or row["bytes"] < FOLDER_MIN:
            continue
        kind, _ = classify_path(row["path"])
        if kind == "system" and not any(seg in ("program files", "program files (x86)")
                                        for seg in Path(row["path"]).parts):
            continue
        try:
            rows, _ = inspect_volume(src_disk, seconds, Path(row["path"]))
        except OSError:
            continue
        for child in rows:
            if child["bytes"] < FOLDER_MIN:
                continue
            child_kind, _ = classify_path(child["path"])
            if child_kind == "system":
                continue
            if kind == "system" and child_kind != "migrate-app":
                continue
            put(child["path"], child["bytes"])

    for row, size in sorted(candidates.values(), key=lambda item: -item[1])[:12]:
        dst, target = _migration_target(state, src_disk, row["path"])
        if not dst:
            notes.append(f"未找到可承接 {row['name']} 的空白盘，已跳过迁移建议。")
            break
        if target["free"] < size:
            notes.append(f"{target['disk']} 盘剩余不足，无法承接 {row['name']}。")
            continue
        if row["id"] not in known:
            state["found"].append(row)
        kind, app = classify_path(row["path"])
        if kind == "migrate-app" and app:
            note = (f"这是 {app} 的数据或安装目录。搬迁后必须在该应用的设置里修改存储位置"
                    f"（如 WeGame/Steam 的存储位置、微信的文件管理路径），否则应用可能找不到数据。")
            action = "migrate"
        else:
            note = "这是用户数据，搬迁通常无需改应用设置；路径已整体保留到目标盘。"
            action = "migrate"
        state["proposals"].append({
            "id": row["id"], "action": action, "estimate": size,
            "recommendation": f"把 {row['name']}（{size / 1024**3:.1f} GiB）从 {src_disk} 盘迁移到 {target['disk']} 盘的空白位置，腾出源盘空间。",
            "impact": note,
            "target": f"目标位置：{dst}（{target['disk']} 盘当前可用 {target['free'] / 1024**3:.1f} GiB）",
            "targetPath": dst, "sourceDisk": src_disk, "targetDisk": target["disk"], "auto": True,
            "needAppReset": bool(app),
        })

    for disk in state["targets"]:
        root = Path(disk + ":\\")
        for label, fname, advice in (
                ("虚拟内存文件 pagefile.sys", "pagefile.sys",
                 "在系统属性→高级→性能→高级→虚拟内存里改为固定较小值或由系统管理；改动需重启后生效。"),
                ("休眠文件 hiberfil.sys", "hiberfil.sys",
                 "若不用休眠，管理员命令行执行 powercfg /h off 可整文件释放；会同时关闭快速启动。")):
            f = root / fname
            try:
                size = f.lstat().st_size if f.exists() else 0
            except OSError:
                size = 0
            if not size:
                continue
            rid = uuid.uuid4().hex
            state["found"].append({"id": rid, "disk": disk, "path": str(f), "name": label,
                                   "bytes": size, "coverage": "系统文件", "directory": False,
                                   "links": 0, "errors": 0, "pending": False})
            state["proposals"].append({
                "id": rid, "action": "system", "estimate": size,
                "recommendation": f"{label} 占 {size / 1024**3:.1f} GiB，可按需回收。",
                "impact": "系统级改动，请确认使用场景后操作；改动前建议记录原设置。",
                "target": advice, "auto": True,
            })
    rid = uuid.uuid4().hex
    state["found"].append({"id": rid, "disk": state["targets"][0], "path": "C:\\Windows\\WinSxS",
                           "name": "Windows 组件存储", "bytes": 0, "coverage": "系统组件存储 · 大小待测",
                           "directory": True, "links": 0, "errors": 0, "pending": False})
    state["proposals"].append({
        "id": rid, "action": "system", "estimate": None,
        "recommendation": "组件存储保存了被更新替换的旧版本，通常可回收数 GB。",
        "impact": "清理后无法卸载此前已安装的旧更新；系统本身不受影响。",
        "target": "管理员命令行执行：Dism /Online /Cleanup-Image /StartComponentCleanup",
        "auto": True,
    })
    state["proposals"].sort(key=lambda p: -(p.get("estimate") or 0))
    notes.append(f"已自动生成 {len(state['proposals'])} 条建议（含迁移、系统项与回收站）。")
    return notes


def write_page(path, template, state, history_link):
    payload = json.dumps({**state, "historyLink": history_link}, ensure_ascii=False).replace("<", "\\u003c")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(template.replace("<!--RUN-DATA-->", payload, 1), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def emit(run_dir, state, *, publish=False):
    state["archived"] = (BASE / "disk_cleanup_decision.legacy.html").is_file()
    dump(run_dir / "run.json", state)
    template = (BASE / "disk_cleanup_template.html").read_text(encoding="utf-8-sig")
    write_page(run_dir / "disk_cleanup_decision.html", template, state,
               "../../disk_cleanup_decision.legacy.html")
    if publish:
        write_page(BASE / "disk_cleanup_decision.html", template, state,
                   "disk_cleanup_decision.legacy.html")


def create_run(args, disks):
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    directory = BASE / "runs" / run_id
    state = {"runId": run_id, "mode": args.command, "targets": disks, "created": sample(disks),
             "revision": uuid.uuid4().hex, "proposals": [], "found": [], "warnings": []}
    if args.command in ("scan", "deep"):
        found, proposals, warnings = deep_scan(disks, args.seconds)
        state.update(found=found, proposals=proposals if args.command == "deep" else [],
                     warnings=warnings, scanTime=timestamp(),
                     scanFull=(args.seconds <= 0))
        if args.command == "deep":
            warnings.extend(auto_plan(state, args.seconds))
    if args.command in ("basic", "deep"):
        state["basic"] = execute_basic(disks)
    record_baseline(state["created"])
    directory.mkdir(parents=True, exist_ok=True)
    emit(directory, state, publish=args.command in ("scan", "deep"))
    report = write_report(directory, state, publish=False) if args.command == "basic" else None
    print(json.dumps({"run": str(directory / "run.json"),
                      "html": str(directory / "disk_cleanup_decision.html"),
                      "report": (report or {}).get("html"),
                      "basic": state.get("basic"), "decisions": len(state["proposals"])},
                     ensure_ascii=False))


def add_review(state, source):
    if "deep" in state or state["mode"] != "deep":
        raise ValueError("只有尚未执行深度选择的深度任务可以更新建议。")
    found = {row["id"]: row for row in state["found"]}
    current = {p["id"]: p for p in state["proposals"]}
    for candidate in load(source):
        row = found[candidate["id"]]
        action = candidate["action"]
        if (action not in ACTIONS or action in ("recycle", "recycle-review") or row["id"] in current
                or row["path"].casefold().endswith("\\$recycle.bin")):
            raise ValueError("请使用未重复的扫描条目和支持的处理方式。")
        entry = {"id": row["id"], "action": action, "estimate": None}
        for field in ("recommendation", "impact", "target"):
            text = candidate[field]
            if not isinstance(text, str) or not text.strip() or len(text) > 600:
                raise ValueError(f"{field} 需要提供具体说明。")
            entry[field] = text.strip()
        current[row["id"]] = entry
    state["revision"] = uuid.uuid4().hex
    state["proposals"] = list(current.values())


def drill(state, item_id, seconds):
    if "deep" in state:
        raise ValueError("已提交的方案请在新一轮排查。")
    row = next(item for item in state["found"] if item["id"] == item_id)
    if not row.get("directory"):
        raise ValueError("只有目录支持进一步排查。")
    if row.get("drilled"):
        raise ValueError("该目录本轮已细分，可继续检查其子目录。")
    found, warnings = inspect_volume(row["disk"], seconds, Path(row["path"]))
    row["drilled"] = True
    state["found"].extend(found)
    state["warnings"].extend(warnings)
    state["found"].sort(key=lambda item: (item["disk"], -item["bytes"]))
    state["revision"] = uuid.uuid4().hex


def migrate_target(state, row, proposal):
    """把 row.path 真实迁移到 proposal.targetPath，robocopy /MOVE 后校验。"""
    source = Path(row["path"])
    raw_dst = proposal.get("targetPath")
    if not raw_dst or not isinstance(raw_dst, str):
        raise OSError("本条缺少有效的目标位置，未执行迁移。")
    destination = Path(raw_dst)
    allowed = {d["disk"] + ":\\" for d in disk_rows() if "free" in d}
    if destination.drive[:1].upper() + ":\\" not in allowed:
        raise OSError("目标盘不是本机固定盘，未执行迁移。")
    if row["disk"].upper() + ":\\" == destination.drive[:1].upper() + ":\\":
        raise OSError("目标与源在同一盘，无法腾出空间，未执行迁移。")
    if not source.exists():
        raise OSError("源路径已不存在，跳过迁移。")
    ordinary_path(source)
    if source.resolve() == destination.resolve() or source in destination.parents or destination in source.parents:
        raise OSError("源与目标路径重叠，未执行迁移。")
    free = next((d["free"] for d in disk_rows()
                 if "free" in d and d["disk"] + ":\\" == destination.drive[:1].upper() + ":\\"), 0)
    need = row.get("bytes") or 0
    if need and free < need:
        raise OSError(f"目标盘可用 {free / 1024**3:.1f} GiB 小于需迁移 {need / 1024**3:.1f} GiB，未执行。")
    destination.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["robocopy.exe", str(source), str(destination), "/MOVE", "/E", "/XJ", "/XJD",
           "/R:1", "/W:1", "/MT:16", "/NJH", "/NJS", "/NP", "/NFL", "/NDL"]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=7 * 24 * 3600)
    if result.returncode >= 8:
        raise OSError(f"robocopy 返回 {result.returncode}，迁移可能不完整，请核对源与目标。")
    if source.exists():
        raise OSError("复制后源目录仍存在，可能有未迁移的文件，请核对。")
    if not destination.exists():
        raise OSError("目标位置未生成，迁移失败。")
    return str(destination)


def apply_selection(directory, state, source):
    choice = load(source)
    fields = [state.get(k) for k in ("runId", "revision", "proposals", "found")]
    expected = hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()
    if choice.get("reviewDigest") != expected:
        raise ValueError("回执缺少或校验值不匹配，请从原对话重新打开本轮工作台。")
    choices = choice.get("ids")
    if "deep" in state or state["mode"] != "deep":
        raise ValueError("该次深度选择已经提交，或任务类型不符。")
    if (choice.get("runId") != state["runId"] or choice.get("revision") != state["revision"]
            or not isinstance(choices, list) or not choices or len(set(choices)) != len(choices)):
        raise ValueError("所选方案版本或条目无效，请打开本轮最新页面。")
    proposals = {p["id"]: p for p in state["proposals"]}
    rows = {row["id"]: row for row in state["found"]}
    if not set(choices) <= proposals.keys():
        raise ValueError("所选编号不在本轮建议中，请打开本轮最新页面。")
    planned = [(proposals[key], rows[key]) for key in choices]
    targets = [(Path(row["path"]), row.get("name") or row["path"]) for _, row in planned]
    for i, (a, name_a) in enumerate(targets):
        for b, name_b in targets[i + 1:]:
            if a == b or a in b.parents or b in a.parents:
                raise ValueError(
                    f"本轮选择含重叠目标：{name_a}（{a}）与 {name_b}（{b}）"
                    "是同一路径或父子目录，请返回页面只保留其中一项。"
                )
    before = sample(state["targets"])
    entries = []
    for proposal, row in planned:
        action = proposal["action"]
        entry = {"id": row["id"], "name": row["name"], "disk": row["disk"], "action": action}
        if action == "recycle":
            try:
                if (recycle_bin(row["disk"]) != proposal["observed"]
                        or recycle_inventory(row["disk"], account_sid())[0] != row["fingerprint"]):
                    raise OSError("回收站内容已变化，需要重新排查。")
                empty_recycle(row["disk"])
                remaining = recycle_bin(row["disk"])
                entry.update(status="完成" if not remaining["count"] else "有残留",
                             note=f"复核剩余 {remaining['count']} 项")
            except Exception as exc:
                entry.update(status="未确认完成", note=str(exc))
        elif action == "migrate":
            try:
                moved = migrate_target(state, row, proposal)
                entry.update(status="完成", note=f"已迁移到 {moved}。" + proposal.get("impact", ""))
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                entry.update(status="未确认完成", note=str(exc))
        else:
            entry.update(status="待 WorkBuddy 打开指定入口", note=proposal["target"])
        entries.append(entry)
    deep = {"before": before, "selected": choices, "entries": entries,
            "after": sample(state["targets"])}
    state["deep"] = deep
    emit(directory, state)


def verify(state, receipt):
    if "deep" not in state:
        raise ValueError("该任务尚未提交深度选择。")
    if receipt:
        entries = {row["id"]: row for row in state["deep"]["entries"]
                   if row["action"] != "recycle"}
        for result in load(receipt):
            if result.get("id") not in entries:
                raise ValueError("回填的执行项编号不在本轮可回填项目中。")
            row = entries[result["id"]]
            if result.get("status") not in ("完成", "部分完成", "跳过", "未完成"):
                raise ValueError("回填状态无效。")
            row.update(status=result["status"], note=str(result.get("note", ""))[:600])
    state["deep"].setdefault("reviews", []).append(sample(state["targets"]))


REPORT_SVG = "清理成果报告.svg"
REPORT_HTML = "清理成果报告.html"
BASELINE = BASE / "baseline.json"
ACTION_LABEL = {"recycle": "清空回收站", "recycle-review": "清空回收站", "migrate": "迁移目录",
                "uninstall": "卸载应用", "app-clean": "应用内清理", "system": "系统调整",
                "read-space": "读取空间"}
STATUS_COLOR = {"完成": "#0f766e", "有残留": "#b45309", "未确认完成": "#b91c1c",
                "待 WorkBuddy 打开指定入口": "#1d4ed8"}


def _gb(value):
    return f"{value / 1024 ** 3:.2f}"


def _signed_gb(value):
    return f"{value / 1024 ** 3:+.2f}"


def _cut(text, limit):
    text = str(text)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _volumes(sample_value):
    return {v["disk"]: v for v in (sample_value or {}).get("volumes", []) if "free" in v}


def _round_sample(state):
    """本轮收尾的实际读数：优先最后一次复核，其次执行后，最后基础清理后。"""
    deep = state.get("deep") or {}
    if deep.get("reviews"):
        return deep["reviews"][-1]
    if deep.get("after"):
        return deep["after"]
    if (state.get("basic") or {}).get("after"):
        return state["basic"]["after"]
    return state.get("created")


def record_baseline(created):
    """首次扫描时记录基线，之后所有报告都以它为累计起点。"""
    if BASELINE.is_file() or not (created or {}).get("volumes"):
        return
    dump(BASELINE, {"time": created.get("time"), "volumes": created["volumes"],
                    "note": "首次扫描自动记录的累计基线，请勿手工改写"})


def report_data(state, directory):
    final = _round_sample(state)
    start = state.get("created") or final
    before = (state.get("deep") or {}).get("before") or (state.get("basic") or {}).get("before") or start
    baseline, rounds, objects = start, 1, 0
    for path in sorted((BASE / "runs").glob("*/run.json")):
        try:
            other = load(path)
        except Exception:
            continue
        created = other.get("created") or {}
        if created.get("volumes") and str(created.get("time", "")) and \
                (not baseline.get("time") or created["time"] < baseline.get("time", "")):
            baseline = created
        deep = other.get("deep") or {}
        basic = other.get("basic") or {}
        if deep.get("entries") or basic.get("groups"):
            rounds += 1
        objects += sum(1 for e in deep.get("entries", []) if e.get("status") == "完成")
        objects += sum(int(g.get("done") or 0) for g in basic.get("groups", []))
    rounds = max(1, rounds)
    if BASELINE.is_file():
        try:
            recorded = load(BASELINE)
        except Exception:
            recorded = None
        if (recorded or {}).get("volumes"):
            baseline = recorded
    start_vol, base_vol, final_vol = _volumes(start), _volumes(baseline), _volumes(final)
    disks = [d for d in state.get("targets", []) if d in final_vol]
    this_round = sum(final_vol[d]["free"] - start_vol.get(d, final_vol[d])["free"] for d in disks)
    base_disks = [d for d in disks if d in base_vol]
    cumulative = sum(final_vol[d]["free"] - base_vol[d]["free"] for d in base_disks)
    entries = list((state.get("deep") or {}).get("entries") or [])
    selected = set((state.get("deep") or {}).get("selected") or [])
    found = {row["id"]: row for row in state.get("found", [])}
    pending = [{"name": found.get(p["id"], {}).get("name") or p.get("recommendation", "未命名项目"),
                "action": ACTION_LABEL.get(p["action"], p["action"])}
               for p in state.get("proposals", []) if p["id"] not in selected]
    basic_done = sum(int(g.get("done") or 0) for g in (state.get("basic") or {}).get("groups", []))
    basic_bytes = sum(int(g.get("removed") or 0) for g in (state.get("basic") or {}).get("groups", []))
    return {
        "runId": state.get("runId", ""), "mode": state.get("mode", ""),
        "start": str(start.get("time", ""))[:10], "end": str((final or {}).get("time", ""))[:16].replace("T", " "),
        "disks": disks, "start_vol": start_vol, "before_vol": _volumes(before),
        "final_vol": final_vol, "base_vol": base_vol, "this_round": this_round,
        "cumulative": cumulative, "has_baseline": bool(base_disks),
        "rounds": rounds, "objects": objects, "entries": entries, "pending": pending,
        "basic_done": basic_done, "basic_bytes": basic_bytes,
        "basic_groups": [g for g in (state.get("basic") or {}).get("groups", []) if g.get("done")],
        "done": sum(1 for e in entries if e.get("status") == "完成"),
        "warn": sum(1 for e in entries if e.get("status") in ("有残留", "未确认完成")),
        "manual": sum(1 for e in entries if str(e.get("status", "")).startswith("待 WorkBuddy")),
    }


def render_report(data):
    total_base = sum(data["base_vol"][d]["free"] for d in data["disks"] if d in data["base_vol"])
    total_now = sum(data["final_vol"][d]["free"] for d in data["disks"])
    big = _signed_gb(data["cumulative"] if data["has_baseline"] else data["this_round"])
    big_width = len(big) * 49
    scope = " + ".join(data["disks"]) or "本机"
    if data["has_baseline"]:
        sub = f"{scope} 合计可用：{_gb(total_base)} → {_gb(total_now)} GB"
        kind = "磁盘实际空出 · 累计"
    else:
        sub = f"{scope} 本轮执行前后：{_gb(sum(v['free'] for v in data['before_vol'].values()))} → {_gb(total_now)} GB"
        kind = "磁盘实际空出 · 本轮"
    title = "基础清理成果报告" if data["mode"] == "basic" else "磁盘清理成果报告"
    meta = f"{data['start']} 首次记录 → {data['end']} 实测　·　累计口径自首次扫描起算"

    cards = []
    for i, disk in enumerate(data["disks"][:3]):
        x = 88 + i * 482
        after = data["final_vol"][disk]
        base_free = data["base_vol"].get(disk, after)["free"]
        delta = after["free"] - base_free
        ratio = after["free"] / after["total"] if after.get("total") else 0
        color = "#0f766e" if delta >= 0 else "#b91c1c"
        bar = "#b91c1c" if ratio < 0.15 else ("#b45309" if ratio < 0.30 else "#14b8a6")
        cards.append(f'''  <rect x="{x}" y="470" width="458" height="110" rx="14" fill="#fbfdff" stroke="#e7edf4"/>
  <text x="{x + 22}" y="502" font-size="18" font-weight="800" fill="#16223a">{disk} 盘</text>
  <text x="{x + 436}" y="502" font-size="16" font-weight="800" fill="{color}" text-anchor="end">{_signed_gb(delta)} GB</text>
  <text x="{x + 22}" y="532" font-size="13" fill="#5b6b84">起始 {_gb(base_free)} → 当前 {_gb(after["free"])} GB</text>
  <rect x="{x + 22}" y="548" width="414" height="8" rx="4" fill="#e6edf5"/>
  <rect x="{x + 22}" y="548" width="{max(4, round(414 * ratio))}" height="8" rx="4" fill="{bar}"/>
  <text x="{x + 22}" y="574" font-size="11.5" fill="#8492a6">当前可用占比 {ratio * 100:.0f}%</text>''')

    rows = []
    for i, entry in enumerate(data["entries"][:6]):
        y = 692 + i * 26
        dot = STATUS_COLOR.get(entry.get("status"), "#64748b")
        rows.append(f'''  <circle cx="86" cy="{y - 5}" r="4" fill="{dot}"/>
  <text x="102" y="{y}" font-size="13.5" fill="#334155">{_cut(entry.get("name", "未命名项目"), 22)}</text>
  <text x="352" y="{y}" font-size="12.5" fill="#8492a6">{entry.get("disk", "")} 盘 · {ACTION_LABEL.get(entry.get("action"), entry.get("action", ""))}</text>
  <text x="556" y="{y}" font-size="12.5" font-weight="600" fill="{dot}">{_cut(entry.get("status", ""), 12)}</text>''')
    if len(data["entries"]) > 6:
        rows.append(f'  <text x="102" y="{692 + 6 * 26}" font-size="12" fill="#8492a6">'
                    f'另有 {len(data["entries"]) - 6} 项，见本轮工作台</text>')
    for group in data["basic_groups"][:max(0, 6 - len(data["entries"]))]:
        y = 692 + len(rows) * 26
        rows.append(f'''  <circle cx="86" cy="{y - 5}" r="4" fill="#0f766e"/>
  <text x="102" y="{y}" font-size="13.5" fill="#334155">基础清理 · {_cut(group.get("name", ""), 15)}</text>
  <text x="352" y="{y}" font-size="12.5" fill="#8492a6">{group.get("disk", "")} 盘 · {group.get("done", 0)} 个文件</text>
  <text x="556" y="{y}" font-size="12.5" font-weight="600" fill="#0f766e">{_gb(group.get("removed", 0))} GB</text>''')
    if not rows:
        rows.append('  <text x="86" y="700" font-size="13" fill="#8492a6">本轮没有产生需要执行的项目，'
                    '基础清理结果见上方统计。</text>')

    pending = []
    for i, item in enumerate(data["pending"][:5]):
        y = 692 + i * 26
        pending.append(f'''  <circle cx="1054" cy="{y - 5}" r="4" fill="#cbd5e1"/>
  <text x="1070" y="{y}" font-size="13" fill="#475569">{_cut(item["name"], 17)}</text>
  <text x="1330" y="{y}" font-size="12" fill="#8492a6">{item["action"]}</text>''')
    if len(data["pending"]) > 5:
        pending.append(f'  <text x="1070" y="{692 + 5 * 26}" font-size="12" fill="#8492a6">'
                       f'另有 {len(data["pending"]) - 5} 项未处理</text>')
    if not pending:
        pending.append('  <text x="1054" y="700" font-size="13" fill="#0f766e">本轮建议已全部处理，无遗留项。</text>')

    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="940" viewBox="0 0 1600 940" font-family="Microsoft YaHei, PingFang SC, Segoe UI, sans-serif">
  <defs>
    <linearGradient id="top" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0" stop-color="#1e3a8a"/><stop offset=".5" stop-color="#155e75"/><stop offset="1" stop-color="#0f766e"/>
    </linearGradient>
  </defs>
  <rect width="1600" height="940" fill="#eef2f8"/>
  <rect x="16" y="16" width="1568" height="908" rx="28" fill="#ffffff"/>
  <rect x="16" y="16" width="1568" height="8" rx="4" fill="url(#top)"/>

  <text x="56" y="80" font-size="30" font-weight="800" fill="#16223a">{title}</text>
  <text x="56" y="112" font-size="13" fill="#8492a6">{meta}</text>
  <rect x="1364" y="62" width="180" height="34" rx="17" fill="#f0fdfa" stroke="#99f6e4"/>
  <circle cx="1384" cy="79" r="5" fill="#0d9488"/>
  <text x="1398" y="84" font-size="12.5" font-weight="700" fill="#0f766e">本轮已完成</text>
  <line x1="56" y1="132" x2="1544" y2="132" stroke="#eef2f7" stroke-width="1.5"/>

  <rect x="56" y="152" width="700" height="240" rx="20" fill="#ecfdf5" stroke="#99f6e4" stroke-width="1.5"/>
  <text x="88" y="196" font-size="13.5" font-weight="700" fill="#0d9488">{kind}</text>
  <text x="86" y="292" font-size="82" font-weight="800" fill="#0f766e" letter-spacing="-3">{big}</text>
  <text x="{86 + big_width}" y="292" font-size="30" font-weight="700" fill="#0f766e">GB</text>
  <line x1="88" y1="316" x2="724" y2="316" stroke="#99f6e4" stroke-width="1.5" stroke-dasharray="4 4"/>
  <text x="88" y="352" font-size="14" font-weight="700" fill="#43536b">{sub}</text>

  <rect x="776" y="152" width="768" height="240" rx="20" fill="#f8fafc" stroke="#e7edf4"/>
  <text x="804" y="192" font-size="15" font-weight="800" fill="#16223a">本轮成果</text>
  <line x1="804" y1="206" x2="1516" y2="206" stroke="#eef2f7" stroke-width="1.5"/>
  <text x="804" y="268" font-size="34" font-weight="800" fill="#0f766e">{_signed_gb(data["this_round"])}</text>
  <text x="804" y="296" font-size="13" fill="#5b6b84">本轮实测释放 GB</text>
  <text x="1004" y="268" font-size="34" font-weight="800" fill="#16223a">{data["done"]}</text>
  <text x="1004" y="296" font-size="13" fill="#5b6b84">已完成项目</text>
  <text x="1204" y="268" font-size="34" font-weight="800" fill="#b45309">{data["warn"] + data["manual"]}</text>
  <text x="1204" y="296" font-size="13" fill="#5b6b84">待处理 / 需你操作</text>
  <text x="1404" y="268" font-size="34" font-weight="800" fill="#16223a">{len(data["pending"])}</text>
  <text x="1404" y="296" font-size="13" fill="#5b6b84">本轮未处理</text>
  <line x1="804" y1="322" x2="1516" y2="322" stroke="#eef2f7" stroke-width="1.5"/>
  <text x="804" y="352" font-size="13" fill="#5b6b84">基础清理：处理 {data["basic_done"]} 个缓存文件，释放 {_gb(data["basic_bytes"])} GB</text>

  <rect x="56" y="412" width="1488" height="200" rx="20" fill="#ffffff" stroke="#e7edf4"/>
  <text x="88" y="450" font-size="15" font-weight="800" fill="#16223a">各盘可用空间对比</text>
  <text x="1512" y="450" font-size="12" fill="#8492a6" text-anchor="end">灰底为容量，青色为当前可用</text>
{"".join(cards)}

  <rect x="56" y="632" width="940" height="200" rx="20" fill="#ffffff" stroke="#e7edf4"/>
  <text x="86" y="672" font-size="15" font-weight="800" fill="#16223a">本轮回执</text>
  <text x="966" y="672" font-size="12" fill="#8492a6" text-anchor="end">逐项执行结果</text>
{"".join(rows)}

  <rect x="1024" y="632" width="520" height="200" rx="20" fill="#ffffff" stroke="#e7edf4"/>
  <text x="1054" y="672" font-size="15" font-weight="800" fill="#16223a">本轮未处理</text>
  <text x="1514" y="672" font-size="12" fill="#8492a6" text-anchor="end">可下一轮继续</text>
{"".join(pending)}

  <line x1="56" y1="860" x2="1544" y2="860" stroke="#eef2f7" stroke-width="1.5"/>
  <text x="56" y="890" font-size="12" fill="#8492a6">口径：本轮数字为执行前后实测差值；累计以首次扫描基线为准，不含后台写入波动。</text>
  <text x="1544" y="890" font-size="12" fill="#8492a6" text-anchor="end">途有引力 · 磁盘体检与清理</text>
</svg>
'''


def write_report(directory, state, *, publish=True):
    data = report_data(state, directory)
    svg = render_report(data)
    (directory / REPORT_SVG).write_text(svg, encoding="utf-8")
    total_base = sum(data["base_vol"][d]["free"] for d in data["disks"] if d in data["base_vol"])
    total_now = sum(data["final_vol"][d]["free"] for d in data["disks"])
    hint = (f"累计口径：{data['start']} 首次记录 → {data['end']} 实测，合计 "
            f"{_signed_gb(data['cumulative'])} GB（{_gb(total_base)} → {_gb(total_now)} GB）"
            if data["has_baseline"] else
            f"本轮口径：{data['start']} → {data['end']}，实测 {_signed_gb(data['this_round'])} GB")
    html = f'''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>清理成果报告</title>
<style>html,body{{margin:0;padding:0}}body{{background:#eef2f8;min-height:100vh;display:flex;align-items:flex-start;justify-content:center;padding:24px 16px 40px;font-family:"Microsoft YaHei",sans-serif}}
.wrap{{width:1280px;max-width:100%}}.wrap img{{width:100%;height:auto;display:block;border-radius:26px;box-shadow:0 18px 50px rgba(20,32,56,.16)}}
.hint{{margin:14px 2px 0;font-size:12px;color:#8492a6;text-align:center}}</style></head>
<body><div class="wrap"><img src="{REPORT_SVG}" alt="磁盘清理成果报告"><p class="hint">{hint}</p></div></body></html>'''
    (directory / REPORT_HTML).write_text(html, encoding="utf-8")
    if publish:
        (BASE / REPORT_SVG).write_text(svg, encoding="utf-8")
        (BASE / REPORT_HTML).write_text(html, encoding="utf-8")
    return {"svg": str(directory / REPORT_SVG), "html": str(directory / REPORT_HTML)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["drives", "scan", "basic", "deep", "drill", "recommend",
                                            "apply", "verify", "report"])
    parser.add_argument("--drives", nargs="+", help="目标盘，例如 C D")
    parser.add_argument("--seconds", type=int, default=0,
                        help="每盘扫描预算秒数；0 表示不限时完整扫描（默认，真·全盘）")
    parser.add_argument("--run", type=Path, help="本轮 run.json 路径")
    parser.add_argument("--input", type=Path, help="建议、选择或复核回填 JSON")
    parser.add_argument("--id", help="需要细分的目录条目 ID")
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("该脚本运行于 Windows。")
    if args.command == "drives":
        print(json.dumps(disk_rows(), ensure_ascii=False))
        return
    if args.command in ("basic", "deep", "scan"):
        available = [r["disk"] for r in disk_rows() if "free" in r]
        disks = [v.upper().removesuffix(":") for v in (args.drives or available)]
        disks = list(dict.fromkeys(disks))
        if not disks or not set(disks).issubset(available) or args.seconds < 0:
            parser.error("请选择可用的本机固定盘，扫描预算须为非负整数（0 表示完整扫描）。")
        create_run(args, disks)
        return
    if not args.run or (args.command in ("apply", "recommend") and not args.input):
        parser.error("请提供 --run 与此操作要求的 --input。")
    if args.command == "drill" and (not args.id or args.seconds < 0):
        parser.error("细分排查请提供 --id 和非负扫描预算（0 表示完整细分）。")
    run_path = args.run.resolve()
    if run_path.name != "run.json" or run_path.parent.parent != BASE / "runs":
        parser.error("请选择此工作台生成的本轮 run.json。")
    lock = run_path.parent / ".lock"
    try:
        with lock.open("x", encoding="utf-8") as handle:
            handle.write(timestamp())
    except FileExistsError:
        parser.error("本轮正在处理或上次处理中断。请核对记录后再用新一轮继续。")
    try:
        state = load(run_path)
        if args.command == "report":
            if "deep" not in state and "basic" not in state:
                parser.error("该轮只做了扫描，没有可汇总的清理结果。")
            print(write_report(run_path.parent, state, publish=True)["html"])
            return
        if args.command == "apply":
            apply_selection(run_path.parent, state, args.input)
        elif args.command == "recommend":
            add_review(state, args.input)
        elif args.command == "drill":
            drill(state, args.id, args.seconds)
        else:
            verify(state, args.input)
        emit(run_path.parent, state, publish=True)
        print(run_path.parent / "disk_cleanup_decision.html")
        if args.command == "verify":
            print(write_report(run_path.parent, state, publish=True)["html"])
    finally:
        lock.unlink()


if __name__ == "__main__":
    args = sys.argv[1:]
    if len(args) == 2 and args[0] == "--native-recycle-query" and re.fullmatch("[A-Z]", args[1]):
        sys.stderr.reconfigure(encoding="utf-8")
        try:
            print(json.dumps(query_native_recycle(args[1])))
        except OSError as exc:
            sys.exit(str(exc))
    else:
        main()

