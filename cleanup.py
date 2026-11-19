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
    directory.mkdir(parents=True, exist_ok=True)
    emit(directory, state, publish=args.command in ("scan", "deep"))
    print(json.dumps({"run": str(directory / "run.json"),
                      "html": str(directory / "disk_cleanup_decision.html"),
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["drives", "scan", "basic", "deep", "drill", "recommend", "apply", "verify"])
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

