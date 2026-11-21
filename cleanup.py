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
# 英文目录/文件名 → 中文含义。用于页面直接解释"这是什么"，避免用户只看到一堆英文。
# 键统一小写匹配；同一名字在不同语境下含义相近，取最通用的解释。
NAME_GLOSSARY = {
    # 系统与用户目录
    "windows": "Windows 系统目录（系统核心文件，不能搬）",
    "users": "用户账户目录（每个账户的桌面/文档/应用数据都在这里）",
    "program files": "64 位软件安装目录（搬走软件会打不开）",
    "program files (x86)": "32 位软件安装目录（搬走软件会打不开）",
    "programdata": "程序公共数据目录（多个软件共用的配置与缓存）",
    "system volume information": "系统卷信息（还原点与索引，系统专用）",
    "$recycle.bin": "回收站（删除的文件先放这里）",
    "$winreagent": "Windows 更新残留（更新后遗留的临时文件）",
    "recovery": "系统恢复分区数据",
    "perflogs": "性能日志（系统性能记录）",
    "pagefile.sys": "虚拟内存文件（内存不够时系统用它顶替）",
    "swapfile.sys": "交换文件（配合虚拟内存的辅助文件）",
    "hiberfil.sys": "休眠文件（保存休眠时的内存状态）",
    "appdata": "应用数据（软件保存的配置、缓存和用户数据）",
    "local": "本地应用数据（只在本机用，通常最大）",
    "roaming": "漫游应用数据（可随账户同步）",
    "locallow": "低权限应用数据（旧程序常用）",
    "temp": "临时文件（可安全清理）",
    "tmp": "临时文件（可安全清理）",
    "cache": "缓存（应用可重建，通常可清）",
    "caches": "缓存（应用可重建，通常可清）",
    "logs": "日志文件（运行记录，一般可清）",
    "log": "日志文件（运行记录，一般可清）",
    "backup": "备份数据",
    "backups": "备份数据",
    "downloads": "下载（浏览器/软件下载的文件）",
    "documents": "文档",
    "desktop": "桌面",
    "pictures": "图片",
    "videos": "视频",
    "music": "音乐",
    "onedrive": "OneDrive 云同步目录",
    "public": "公共账户目录（所有用户共享）",
    "default": "系统默认用户模板",
    # 开发相关
    "node_modules": "Node.js 依赖包（可删，重新 npm install 会重建）",
    ".git": "Git 版本库（删了会丢失提交历史）",
    ".svn": "SVN 版本库",
    "dist": "构建产物（可删，重新构建会生成）",
    "build": "构建产物（可删，重新构建会生成）",
    "out": "输出目录（编译/构建结果）",
    "outputs": "输出目录（生成的结果文件）",
    "target": "编译产物（Rust/Java 等构建输出）",
    "bin": "可执行程序目录",
    "obj": "编译中间文件（可删，会重新生成）",
    "src": "源代码目录",
    "source": "源代码目录",
    "lib": "依赖库目录",
    "vendor": "第三方依赖（项目自带的外部库）",
    "assets": "静态资源（图片/字体/样式等）",
    "static": "静态资源（不随程序变化的前端文件）",
    "frontend": "前端代码（页面与交互）",
    "backend": "后端代码（服务端逻辑）",
    "fixtures": "测试固定数据（单元测试用的样本）",
    "tests": "测试代码",
    "test": "测试代码/测试数据",
    "spec": "测试用例",
    "docs": "文档",
    "scripts": "脚本（自动化命令）",
    "config": "配置文件",
    "settings": "设置文件",
    "data": "数据文件（程序运行的数据）",
    "db": "数据库文件",
    "database": "数据库文件",
    "session": "会话数据（登录状态等临时信息）",
    "pending": "待处理数据（尚未完成的队列）",
    "installer.exe": "安装程序（装完可删）",
    "installer": "安装程序（装完可删）",
    "msi": "Windows 安装包（装完可删）",
    "exe": "可执行程序",
    "python": "Python 运行环境",
    "pip": "Python 包管理器缓存（可清，会重新下载）",
    "venv": "Python 虚拟环境（可删重建）",
    ".venv": "Python 虚拟环境（可删重建）",
    "mingw64": "MinGW 编译工具链（C/C++ 编译器）",
    "cmake": "CMake 构建工具文件",
    "gradle": "Gradle 构建缓存（可清，会重新下载）",
    "maven": "Maven 依赖缓存（可清，会重新下载）",
    "npm": "npm 包缓存（可清，会重新下载）",
    "npm-cache": "npm 包缓存（可清，会重新下载）",
    "yarn": "Yarn 包缓存（可清，会重新下载）",
    "pnpm": "pnpm 包缓存（可清，会重新下载）",
    "cargo": "Rust 包缓存（可清，会重新下载）",
    "rustup": "Rust 工具链",
    "nuget": "NuGet 包缓存（.NET）",
    # 常见应用/厂商
    "microsoft": "微软（Office/Edge/VS 等微软软件的数据）",
    "nvidia": "NVIDIA 显卡驱动与缓存",
    "tencent": "腾讯（QQ/微信/游戏等数据）",
    "steam": "Steam 游戏平台（游戏本体与存档）",
    "epic games": "Epic 游戏平台",
    "doubao": "豆包（字节跳动 AI 助手）",
    "kingsoft": "金山（WPS 等办公软件）",
    "quark": "夸克（浏览器/网盘）",
    "quarkclouddrive": "夸克网盘",
    "codex": "Codex（AI 编程工具）",
    "zcode": "ZCode（AI 编程工具）",
    "trae cn": "Trae 国内版（AI 编程工具）",
    "wemeet": "腾讯会议",
    "edge": "Microsoft Edge 浏览器",
    "docker": "Docker 容器数据（镜像与容器，可清理无用镜像）",
    "user data": "用户数据（软件保存的个人数据）",
    "msi center": "微星 MSI Center（主板/笔记本控制软件）",
    "soft": "软件目录",
    "program": "程序目录",
    "programs": "程序目录",
    "app": "应用目录",
    "apps": "应用目录",
    "tools": "工具软件目录",
    "utils": "工具软件目录",
    "plugins": "插件目录",
    "extensions": "扩展插件",
    "skins": "皮肤/主题",
    "themes": "主题",
    "fonts": "字体文件",
    "driver": "驱动程序",
    "drivers": "驱动程序",
    "update": "更新包（安装后一般可删）",
    "updates": "更新包（安装后一般可删）",
    "installer_download": "安装包下载目录",
    "temp files": "临时文件（可安全清理）",
    "inetpub": "IIS 网站根目录（Windows 自带的 Web 服务）",
    "onedrivetemp": "OneDrive 临时文件（同步中的缓存）",
    "swdist": "Windows 分发包数据",
    "assembly": ".NET 程序集缓存",
    "catroot": "驱动签名目录（系统专用的数字签名库）",
    "driverstore": "驱动仓库（系统安装过的所有驱动）",
    "winsxs": "组件存储（系统更新留下的旧版本组件）",
    "syswow64": "32 位兼容层（让 32 位程序能在 64 位系统跑）",
    "system32": "系统核心程序（不能动）",
    "servicing": "系统维护组件",
    "winevt": "Windows 事件日志",
    "softwaredistribution": "Windows 更新缓存（可清理）",
    "deliveryoptimization": "更新分发缓存（可清理）",
    "prefetch": "预读文件（加速程序启动，可清）",
    "crashdumps": "崩溃转储（程序崩溃时的记录，可清）",
    "wer": "错误报告缓存（可清）",
    "explorer": "资源管理器缓存（缩略图/图标）",
    "d3dscache": "DirectX 着色器缓存（可清，会重建）",
    # 常见软件（英文名 → 中文说明）
    "qq": "QQ（腾讯即时通讯）",
    "qqnt": "QQ NT 版（新版 QQ 客户端）",
    "weixin": "微信（腾讯）",
    "xwechat": "微信新版客户端数据",
    "xwechat_files": "微信聊天文件与图片",
    "wps": "WPS Office（金山办公）",
    "wps office": "WPS Office（金山办公）",
    "chrome": "Google Chrome 浏览器",
    "google": "Google（Chrome/更新器等）",
    "googleupdater": "Chrome 更新程序缓存",
    "edgecore": "Edge 浏览器内核",
    "edgeupdate": "Edge 更新程序缓存",
    "edgewebview": "Edge WebView（内嵌网页组件）",
    "obsidian": "Obsidian 笔记软件",
    "workbuddy": "WorkBuddy（当前这个 AI 助手）",
    "codebuddy cn": "CodeBuddy 国内版（AI 编程工具）",
    "qoder cn": "Qoder 国内版（AI 编程工具）",
    "qodercn": "Qoder 国内版（AI 编程工具）",
    "comate": "Comate（百度 AI 编程助手）",
    "dify": "Dify（AI 应用开发平台）",
    "openai": "OpenAI（Codex 等工具）",
    "qianwen": "通义千问",
    "qianwenapp": "通义千问客户端",
    "minimax code": "MiniMax Code（AI 编程工具）",
    "baidunetdisk": "百度网盘",
    "telegram desktop": "Telegram 桌面版",
    "sharex": "ShareX（截图/录屏工具）",
    "dockerdesktop": "Docker Desktop（容器工具）",
    "epicgameslauncher": "Epic 游戏启动器",
    "epic online services": "Epic 在线服务组件",
    "steamapps": "Steam 游戏安装目录",
    "launcher": "启动器",
    "saved": "存档（游戏/软件保存的进度）",
    "game": "游戏数据",
    "patch": "补丁/更新文件",
    "tool": "工具",
    "package": "软件包",
    "wsl": "Windows 子系统 Linux（WSL）",
    "wslsettings": "WSL 设置",
    "ollama": "Ollama（本地大模型运行工具）",
    "comfyui": "ComfyUI（AI 绘图工作流）",
    "comfyui_windows_portable_nvidia_cu126.7z": "ComfyUI 便携版压缩包（可删或移走）",
    "ffmpeg": "FFmpeg（音视频处理工具）",
    "node": "Node.js 运行环境",
    "git": "Git 版本控制工具",
    "powershell": "PowerShell（命令行工具）",
    "openssl-win64": "OpenSSL 加密库",
    "libreoffice": "LibreOffice 办公套件",
    "dotnet": ".NET 运行环境",
    "microsoft visual studio": "Visual Studio 开发工具",
    "visualstudio": "Visual Studio 开发工具",
    "windows kits": "Windows SDK 开发工具包",
    "windows defender": "Windows Defender 杀毒组件",
    "windows defender advanced threat protection": "Windows Defender 高级威胁防护",
    "systemapps": "系统应用（Windows 自带）",
    "systemresources": "系统资源文件（不能删）",
    "package cache": "安装包缓存（VS/系统安装器留下，可清）",
    "administrator": "管理员账户目录",
    "dnf": "DNF 游戏（地下城与勇士）",
    "naraka": "永劫无间（游戏）",
    "qqspeed_launcher": "QQ飞车启动器",
    "shaders": "着色器（游戏图形渲染缓存）",
    "sounds": "音效文件",
    "userdata": "用户数据",
    "releasephysx27": "PhysX 物理引擎运行库",
    "releasephysx27_64": "PhysX 物理引擎运行库（64 位）",
    "archives": "归档（备份/历史版本）",
    "sub": "字幕文件",
    "coldstart": "冷启动数据（首次启动的初始化文件）",
    "evidence": "证据/记录数据",
    "rc1-delivery": "候选发布交付物",
    "test-deploy": "测试部署",
    "sandbox": "沙盒（隔离测试环境）",
    "hub": "中心仓库",
    "shared": "共享数据",
    "share": "共享数据",
    "accounts": "账户信息",
    "toolchain": "工具链",
    "webstorage": "网页存储数据",
    "cacheddata": "缓存数据",
    "modulardata": "模块数据",
    "partitions": "分区数据",
    "htmlcache": "网页缓存",
    "screenshots": "截图",
    "binaries": "二进制程序文件",
    "blobs": "二进制大对象（缓存/存储块）",
    "projects": "项目目录",
    "traces": "追踪记录（调试/性能日志）",
    "huggingface": "HuggingFace 模型缓存",
    "pypoetry": "Poetry（Python 依赖管理）缓存",
    "uv": "uv（Python 包管理器）缓存",
    ".pnpm-store": "pnpm 包缓存（可清，会重新下载）",
    "packages": "软件包目录",
    ".qq_dl_tmp": "QQ 下载临时文件（可清）",
    "system.vhd": "系统虚拟硬盘文件",
    "data1.vfs": "游戏数据包",
    "res.vfs": "游戏资源包",
}
# 文件后缀的通用中文解释（目录名里带后缀时兜底说明）
EXT_GLOSSARY = {
    ".dll": "动态链接库（程序运行需要的组件）",
    ".exe": "可执行程序",
    ".sys": "系统驱动文件",
    ".log": "日志文件（运行记录）",
    ".tmp": "临时文件（一般可删）",
    ".dat": "数据文件（程序私有格式）",
    ".db": "数据库文件",
    ".bak": "备份文件",
    ".dmp": "崩溃转储（可删）",
    ".iso": "光盘镜像文件（可删除或移走）",
    ".zip": "压缩包",
    ".rar": "压缩包",
    ".msi": "Windows 安装包",
    ".marker": "分区标记文件（系统用于识别分区）",
}


# 「为什么不在这里直接清」：按应用给出具体原因，避免用户以为工具偷懒。
# 键为 APP_TABLE/用途标签里出现的中文名片段，取不到时用 DEFAULT_WHY。
WHY_NOT_DIRECT = {
    "微信": "微信的图片、视频和文件由应用独占管理，直接删目录会让聊天记录里的内容显示损坏。请用微信「设置 → 存储空间」清理。",
    "WeChat": "微信的图片、视频和文件由应用独占管理，直接删目录会让聊天记录损坏。请在微信内清理。",
    "QQ": "QQ 的聊天记录与接收文件同理，请在 QQ 设置里清理，否则历史消息里的图片会打不开。",
    "WPS": "自动备份和云文档缓存删掉后，未保存的文档无法恢复。请用 WPS 内的清理入口。",
    "豆包": "清缓存不影响账号；但要保留聊天记录，请先在应用内确认已云同步。",
    "Doubao": "清缓存不影响账号；但要保留聊天记录，请先在应用内确认已云同步。",
    "HuggingFace": "模型权重删掉后，下次运行会重新下载，需要网络和等待时间。",
    "huggingface": "模型权重删掉后，下次运行会重新下载，需要网络和等待时间。",
    "Docker": "容器和镜像由 Docker 自己管理，直接删会让容器无法启动。请用 docker system prune 清理。",
    "Edge": "浏览器配置与历史记录混在同一目录，直接删会丢书签。请在浏览器内清理缓存。",
    "Chrome": "浏览器配置与历史记录混在同一目录，直接删会丢书签。请在浏览器内清理缓存。",
    "Steam": "游戏本体与存档由 Steam 管理，请在客户端里卸载或移动安装目录。",
    "NVIDIA": "显卡驱动的着色器缓存由驱动管理，删了会重建；不建议手工处理。",
    "腾讯会议": "会议录制与本地缓存混合存放，请在应用内清理以免丢录制。",
    "企业微信": "企业微信的聊天文件与审批附件由服务端同步，请在应用内清理。",
    "通义千问": "清缓存不影响账号对话；如要保留本地记录请先在应用内确认同步。",
    "Qianwen": "清缓存不影响账号对话；如要保留本地记录请先在应用内确认同步。",
}
DEFAULT_WHY = ("该目录由应用自己管理，工具直接删可能让应用找不到数据或丢历史记录。"
               "建议在应用设置里清理缓存，或先备份再处理。")


def is_container_path(path):
    """是否为"容器目录"：盘根、或 C:\\Users\\<用户名> 这类应下钻而非整体处理的目录。"""
    parts = [p for p in str(path or "").replace("/", "\\").split("\\") if p]
    if len(parts) < 2:
        return True
    if len(parts) == 3 and parts[1].casefold() == "users":
        return True
    return False


def why_not_direct(label, name=""):
    """给出"为什么不在这里直接清理"的具体原因。"""
    for key, text in WHY_NOT_DIRECT.items():
        hay = f"{label or ''} {name or ''}"
        if key and key.casefold() in hay.casefold():
            return text
    return DEFAULT_WHY


def zh_name(name):
    """给英文目录/文件名一个中文解释；中文名或未收录的返回空串。"""
    key = str(name or "").strip().casefold()
    if not key:
        return ""
    if key in NAME_GLOSSARY:
        return NAME_GLOSSARY[key]
    # 去掉扩展名再试一次（installer.exe → installer）
    stem, dot, ext = key.rpartition(".")
    if dot and stem and stem in NAME_GLOSSARY:
        return NAME_GLOSSARY[stem]
    # 仍未收录：按文件后缀给通用解释，至少让人知道这是什么东西
    if dot:
        suffix = "." + ext
        if suffix in EXT_GLOSSARY:
            return EXT_GLOSSARY[suffix]
    return ""


USER_MIN = 256 * 1024 ** 2
FOLDER_MIN = 200 * 1024 ** 2
# 生成"待你决定"建议的最小体积：深度下钻能看到所有 ≥200MB 的目录，
# 但小于 1GB 的零碎项不值得单独搬迁，只在"全部发现"展示，避免决策页被几百条小项淹没。
PROPOSAL_MIN = 1024 ** 3
# 应用识别表：键为路径中出现的目录名片段（小写），值为展示名。
# 命中后该类目录判定为 migrate-app，提示搬迁后需在应用内改存储位置。
APP_TABLE = {
    # 腾讯系
    "wegame": "WeGame", "tencent": "腾讯", "qq": "QQ", "tim": "TIM",
    "qq飞车": "QQ飞车", "qq游戏": "QQ游戏", "wechat": "微信", "微信": "微信",
    "xwechat": "微信", "wechat files": "微信文件", "xwechat_files": "微信文件",
    "wechatfiles": "微信文件", "tencent files": "QQ/微信文件", "mobilefile": "手机文件",
    "qqmusic": "QQ音乐", "qqlive": "腾讯视频", "tencent meeting": "腾讯会议",
    "wemeet": "腾讯会议", "企业微信": "企业微信", "wecom": "企业微信", "wxwork": "企业微信",
    # 游戏平台与游戏
    "steam": "Steam", "steamapps": "Steam", "epic": "Epic Games", "origin": "Origin",
    "battle.net": "暴雪战网", "blizzard": "暴雪战网", "riot": "拳头游戏", "gog galaxy": "GOG",
    "ubisoft": "育碧", "honkai": "米哈游（崩坏/星穹铁道）", "genshin": "原神",
    "yuanshen": "原神", "mihoyo": "米哈游", "hoyoverse": "米哈游", "star rail": "星穹铁道",
    "naraka": "永劫无间", "netease": "网易游戏", "网易": "网易游戏",
    "bilibili": "哔哩哔哩", "bili": "哔哩哔哩", "mihoyo launcher": "米哈游启动器",
    # 办公 / 通讯 / AI
    "dingtalk": "钉钉", "钉钉": "钉钉", "feishu": "飞书", "lark": "飞书", "飞书": "飞书",
    "workbuddy": "WorkBuddy", ".workbuddy": "WorkBuddy", "slack": "Slack",
    "discord": "Discord", "telegram": "Telegram", "outlook": "Outlook",
    "doubao": "豆包", "豆包": "豆包", "qianwen": "通义千问", "通义千问": "通义千问",
    "tongyi": "通义千问", "huggingface": "Hugging Face", "cursor": "Cursor",
    "windsurf": "Windsurf", "trae": "Trae", "claude": "Claude",
    "microsoft office": "Office", "office": "Office", "wps": "WPS Office", "kingsoft": "WPS Office",
    "adobe": "Adobe", "autodesk": "Autodesk", "notion": "Notion", "obsidian": "Obsidian",
    # 浏览器
    "chrome": "Chrome", "google": "Google", "microsoft edge": "Edge", "edge": "Edge",
    "firefox": "Firefox", "chromium": "Chromium", "opera": "Opera", "360browser": "360浏览器",
    # 开发工具 / 运行时
    "jetbrains": "JetBrains", "intellij": "IntelliJ IDEA", "pycharm": "PyCharm",
    "webstorm": "WebStorm", "goland": "GoLand", "vscode": "VS Code",
    "visual studio code": "VS Code", "visual studio": "Visual Studio",
    "nodejs": "Node.js", "npm": "npm", "yarn": "Yarn", "pnpm": "pnpm",
    "cargo": "Rust", "rustup": "Rust", "rust": "Rust", "python": "Python",
    "pip": "pip", "conda": "Conda", "anaconda": "Anaconda", "docker": "Docker",
    "vmware": "VMware", "virtualbox": "VirtualBox", "android": "Android SDK",
    "gradle": "Gradle", "maven": "Maven", "xcode": "Xcode", "git": "Git",
    # 影音 / 云盘 / 下载
    "cloudmusic": "网易云音乐", "网易云": "网易云音乐", "spotify": "Spotify",
    "iqiyi": "爱奇艺", "youku": "优酷", "kugou": "酷狗音乐", "kuwo": "酷我音乐",
    "baidunetdisk": "百度网盘", "baidu": "百度网盘", "aliyun": "阿里云盘",
    "dropbox": "Dropbox", "onedrive": "OneDrive", "google drive": "Google Drive",
    "xunlei": "迅雷", "thunder": "迅雷", "qbittorrent": "qBittorrent",
}
# 缓存目录名片段：命中判定为 cache，只给"用应用内清理"建议，绝不自动删除。
CACHE_HINTS = {
    "temp": "临时文件", "tmp": "临时文件", "cache": "缓存", "caches": "缓存",
    "cachestorage": "缓存", "code cache": "编译缓存", "gpucache": "GPU 缓存",
    "shadercache": "着色器缓存", "crashpad": "崩溃报告", "crashes": "崩溃报告",
    "npm-cache": "npm 缓存", "_cacache": "npm 缓存", "pip-cache": "pip 缓存",
    "__pycache__": "Python 缓存", "thumbcache": "缩略图缓存", "derivatedata": "派生数据",
    "local storage": "本地存储", "service worker": "服务缓存", "gpu cache": "GPU 缓存",
}
# 用户主目录下需重点下钻的应用数据目录（这些通常不在 AppData 内）。
USER_APP_DIRS = ("wechat files", "xwechat_files", "tencent files", "downloads",
                 "documents", "desktop", "pictures", "videos", "music", "onedrive",
                 ".workbuddy", ".vscode", ".gradle", ".m2", ".npm", ".cache")
MIGRATION_ROOT = "磁盘清理迁移"

# ── 目录用途识别：靠子目录特征标记与目录自身命名，判断一个大目录"大概是做啥的" ──
# 每项：(标记名, 展示用途)。命中任一即认定用途；可多重命中。
PURPOSE_MARKERS = {
    ".git": "Git 代码仓库", ".obsidian": "Obsidian 笔记库", ".workbuddy": "WorkBuddy 工作区",
    ".vscode": "VS Code 工作区", ".idea": "JetBrains 项目",
    "package.json": "Node 项目", "node_modules": "Node 依赖",
    "venv": "Python 虚拟环境", ".venv": "Python 虚拟环境",
    "pyproject.toml": "Python 项目", "requirements.txt": "Python 项目",
    "Cargo.toml": "Rust 项目", "go.mod": "Go 项目",
    "dist": "构建产物", "build": "构建产物", "target": "编译产物",
    "backup": "备份数据", "backup-040-data": "备份数据",
}

# 目录名本身能透露的用途（子串匹配，小写）。
PURPOSE_NAME_HINTS = (
    ("下载", "下载/暂存"), ("download", "下载/暂存"), ("卡拉米", "下载/暂存"),
    ("备份", "备份数据"), ("backup", "备份数据"), ("history", "历史归档"),
    ("视频", "视频工程"), ("video", "视频工程"), ("vedio", "视频工程"),
    ("wiki", "知识库"), ("知识", "知识库"), ("obsidian", "笔记库"),
    ("workspace", "开发工作区"), ("lab", "实验项目"), ("test", "测试项目"),
    ("candidate", "测试项目"), ("总库", "资料总库"),
)


def _dir_mtime(path):
    """目录最后修改时间（YYYY-MM-DD），读取失败返回空串。"""
    try:
        import datetime as _dt
        return _dt.datetime.fromtimestamp(Path(path).stat().st_mtime).strftime("%Y-%m-%d")
    except OSError:
        return ""


def purpose_of(path, *, deep=True):
    """识别一个目录的用途。返回 (用途标签, 依据)。只读顶层与少量子层，不递归全树。"""
    p = Path(path)
    tags, evidence = [], []
    name = p.name.casefold()
    for hint, label in PURPOSE_NAME_HINTS:
        if hint in name and label not in tags:
            tags.append(label)
    # 时间戳命名的目录（如 2026-08-20-23-12-04）视为会话/历史产物
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}[-_]\d{2}-\d{2}-\d{2}", p.name):
        if "会话工作区" not in tags:
            tags.append("会话工作区")
    # 系统级容器（Users/ProgramData/Program Files/Windows）本身不是"某个项目"，
    # 其内部碰巧有 .workbuddy / package.json 也不代表整个目录是该用途，必须跳过标记识别，
    # 否则会把整个用户目录误判成可搬迁的应用工作区。
    container = p.name.casefold() in {"users", "programdata", "program files",
                                      "program files (x86)", "windows"}
    if deep and p.is_dir() and not container:
        try:
            direct = {e.name for e in p.iterdir()}
        except OSError:
            direct = set()
        for marker, label in PURPOSE_MARKERS.items():
            if marker in direct and label not in tags:
                tags.append(label)
                evidence.append(marker)
        # 再下钻一层非隐藏子目录，捕捉"项目容器"里某个子项目的技术栈。
        if not tags:
            try:
                subs = [e for e in p.iterdir() if e.is_dir() and not e.name.startswith(".")]
            except OSError:
                subs = []
            for sub in subs[:10]:
                try:
                    subnames = {e.name for e in sub.iterdir()}
                except OSError:
                    continue
                for marker, label in PURPOSE_MARKERS.items():
                    if marker in subnames and label not in tags:
                        tags.append(label)
                        evidence.append(marker)
    if not tags:
        return "一般数据目录", "未发现明确特征"
    return " + ".join(tags[:3]), ("特征：" + ", ".join(evidence[:4])) if evidence else "目录名"


def deep_inspect_topdirs(state, seconds):
    """对所有目标盘的顶层大目录做用途识别，生成可搬迁/需确认的候选项。

    注意：inspect_volume 已递归算出每个顶层目录的合计大小，但只输出顶层；
    这里对每个顶层目录补充用途识别与建议，不重复做全树遍历。
    """
    notes = []
    disks = {r["disk"]: r for r in disk_rows() if "free" in r}
    known = {r["path"].casefold(): r for r in state["found"]}
    for disk in state["targets"]:
        if disk not in disks:
            continue
        for row in list(state["found"]):
            if row["disk"] != disk or not row.get("directory"):
                continue
            size = row.get("bytes") or 0
            if size < PROPOSAL_MIN:
                continue
            top = Path(row["path"]).name.casefold()
            # 系统保留目录、标准用户目录、回收站等交给原有逻辑，不重复处理
            # "users"/"programdata" 等系统容器也不应作为可搬迁/可清理项
            skip_tops = SYSTEM_TOP | USER_DIRS | {"$recycle.bin", "users", "programdata"}
            if top in skip_tops:
                continue
            # 已经被识别为应用/用户/缓存并生成建议的，不重复
            if any(p["id"] == row["id"] for p in state["proposals"]):
                continue
            label, basis = purpose_of(row["path"])
            mtime = _dir_mtime(row["path"])
            row["kind"] = "migrate-user"
            row["kindName"] = label
            row["purpose"] = label
            row["purposeBasis"] = basis
            row["modified"] = mtime
            rid = row["id"]
            # 是否包含当前工作区/脚本自身：不禁止搬迁（用户可决定），但必须在提示里讲清
            # "需先在应用内切换工作区再搬"，否则搬走正在运行的目录会破坏会话。
            try:
                import os as _os
                src = Path(row["path"]).resolve()
                bases = [Path.cwd().resolve(), Path(__file__).resolve()]
                ws = _os.environ.get("DISK_CLEANUP_WORKSPACE", "")
                if ws:
                    bases.append(Path(ws).resolve())
                in_use = any(src == base or src in base.parents for base in bases)
            except OSError:
                in_use = False
            row["inUse"] = in_use
            # 用户主目录容器（C:\Users\<用户名>）与包含当前工作区的目录，
            # 都不是"可整体处理"的对象：前者应下钻到子目录，后者误删会破坏会话。
            if is_container_path(row["path"]) or in_use:
                row["kind"] = "observe"
                row["kindName"] = "容器目录（仅展示，建议逐项处理）"
                continue
            # 目标盘：排除源盘，优先非系统盘，再按可用空间（统一 pick_target_disk）
            target = pick_target_disk(list(disks.values()), disk)
            if target and target["free"] >= size:
                dst = make_target_path(disk, target["disk"], row["path"])
                rec = (f"[{label}] {row['name']}（{size/1024**3:.1f} GiB，{basis}）"
                       f"从 {disk} 盘迁到 {target['disk']} 盘；最后修改 {mtime or '未知'}。")
                impact = ("这是" + label + "。搬迁保留完整目录结构与路径名。"
                          + ("注意：该目录包含当前正在使用的工作区或工具自身，搬迁前必须先在应用内把工作区/存储位置切走，"
                             "否则会搬走正在运行的会话导致工具异常。" if in_use else "")
                          + "搬迁后须到该应用的设置里改存储位置或重新指定目录，否则应用可能找不到数据。")
                state["proposals"].append({
                    "id": rid, "action": "migrate", "estimate": size, "auto": True,
                    "recommendation": rec, "impact": impact,
                    "target": f"目标位置：{dst}（{target['disk']} 盘当前可用 {target['free']/1024**3:.1f} GiB）",
                    "targetPath": dst, "sourceDisk": disk, "targetDisk": target["disk"],
                    "needAppReset": True,
                })
            else:
                # 目标盘装不下或没有空盘：只给"需确认"的提示，纳入决策页但不提供搬迁路径
                state["proposals"].append({
                    "id": rid, "action": "app-clean", "estimate": size, "auto": True,
                    "recommendation": (f"[{label}] {row['name']}（{size/1024**3:.1f} GiB，{basis}）"
                                       f"最后修改 {mtime or '未知'}。没有足够空盘可承接整体搬迁，"
                                       "请在应用内清理或在盘内整理。"),
                    "impact": ("这是" + label + "，本工具不会自动删除。请确认内容可重建或可删除后再处理；"
                               "若含工作区/项目源码，建议改用应用内清理或先备份。"
                               + ("注意：该目录包含当前正在使用的工作区或工具自身，请先确认不会影响当前会话。"
                                  if in_use else "")),
                    "target": "在对应应用设置里清理缓存/数据，或手动整理该目录；也可先删除可重建的子目录（如 node_modules、dist）。",
                    "needAppReset": False,
                    "whyNotDirect": why_not_direct(label, row["name"]),
                })
        notes.append(f"{disk} 盘：已对顶层大目录做用途识别。")
    return notes


def match_app(text):
    low = str(text).casefold()
    # 规整化分隔符，令 "microsoft vs code" / "visual studio code" 等都能命中
    compact = re.sub(r"[\s._\-]+", "", low)
    for key, name in APP_TABLE.items():
        if key in low or re.sub(r"[\s._\-]+", "", key) in compact:
            return name
    return ""


def classify_path(path):
    """返回 (类别, 名称)。类别：system / migrate-app / migrate-user / cache / skip。

    判定顺序：先认具体应用/用户数据/缓存（层级可深），再判断是否处于系统保留顶层。
    缓存单独成类，只提示用应用内清理，绝不自动删除。
    """
    parts = [p.casefold() for p in Path(path).parts]
    head = parts[1] if len(parts) > 1 else ""
    tail = parts[-1] if parts else ""
    if tail in ("pagefile.sys", "hiberfil.sys", "swapfile.sys"):
        return "system", ""
    # 缓存：仅识别应用数据区内部的缓存子目录，避免误判系统 Temp 根。
    in_appdata = any(seg in ("appdata", "programdata", ".cache") for seg in parts)
    if tail in CACHE_HINTS or (in_appdata and any(seg in CACHE_HINTS for seg in parts)):
        cache_name = CACHE_HINTS.get(tail) or next(
            (v for seg in parts for k, v in CACHE_HINTS.items() if seg == k), "应用缓存")
        owner = match_app(" ".join(parts))
        return "cache", f"{owner + ' · ' if owner else ''}{cache_name}"
    app = match_app(" ".join(parts))
    if app and len(parts) > 2:
        return "migrate-app", app
    if in_appdata and len(parts) > 4:
        # 未收录的应用用其目录名展示，避免笼统写"应用数据"
        return "migrate-app", match_app(path) or (Path(path).name or "应用数据")
    if any(seg in USER_DIRS for seg in parts) or head == "users":
        return "migrate-user", ""
    if Path(path).suffix.casefold() in BIG_FILE_EXT:
        return "migrate-user", ""
    if head in SYSTEM_TOP:
        return "system", ""
    return "skip", ""


def make_target_path(source, target_disk, path):
    """按源盘与源路径生成唯一目标路径。目标绝对路径只由脚本生成，不信任页面传入值。"""
    rel = Path(path)
    parts = [p for p in rel.parts[1:]]
    name = ("_".join(parts) or rel.name or "data")[:100]
    base = Path(str(target_disk).upper() + ":\\") / MIGRATION_ROOT / (source.upper() + "_" + name)
    dst = base
    seq = 2
    while dst.exists():
        dst = Path(str(base) + f"_{seq}")
        seq += 1
        if seq > 50:
            raise ValueError("目标位置重复过多，无法生成唯一路径。")
    return str(dst)


def is_system_disk(disk):
    """该盘是否系统盘（存在 Windows 目录或为启动卷）。"""
    try:
        if (Path(disk + ":\\Windows")).is_dir():
            return True
        # 备用：系统目录所在盘符
        system_root = os.environ.get("SystemRoot", "")
        if system_root[:2].upper() == disk.upper() + ":":
            return True
    except OSError:
        pass
    return False


def pick_target_disk(candidates, source):
    """从候选盘里挑搬迁目标：排除源盘，优先非系统盘，再按可用空间降序。

    系统盘（通常是 C）即便可用空间很大也不作为默认目标——它承载系统与临时文件，
    搬入大目录会挤压系统余量。若除系统盘外别无选择，才退回系统盘。
    """
    rows = [r for r in candidates if "free" in r and r.get("disk") and r["disk"] != source]
    if not rows:
        return None
    non_system = [r for r in rows if not is_system_disk(r["disk"])]
    pool = non_system or rows
    return max(pool, key=lambda r: r["free"])


def _migration_target(state, source, path):
    """在合适目标盘上生成唯一目标路径（优先非系统盘）。"""
    target = pick_target_disk(disk_rows(), source)
    if not target:
        return None, None
    try:
        return make_target_path(source, target["disk"], path), target
    except ValueError:
        return None, None


def _can_delete(row):
    """该条目是否允许"直接清理（永久删除）"。

    严格限制：只对 migrate 类用户/应用数据目录开放；系统目录、软件安装目录、
    盘根、用户主目录容器、回收站等一律不允许。宁可少开，不可误删。
    """
    if not row or not row.get("directory"):
        return False
    if row.get("kind") not in ("migrate-app", "migrate-user"):
        return False
    path = str(row.get("path") or "").rstrip("\\")
    if not path:
        return False
    # 不允许删盘根本身；盘根下的目录（含 D:\某目录 这种 2 段）是合法对象，
    # 但盘根下的系统级容器会在下面按名字再排除。
    parts = [p for p in path.replace("/", "\\").split("\\") if p]
    if len(parts) < 2:
        return False
    leaf = parts[-1].casefold()
    if leaf in SYSTEM_TOP or leaf in {"users", "programdata"} or leaf in USER_DIRS:
        return False
    if leaf.startswith("$"):
        return False
    # 路径中只要出现系统/软件安装目录，就整条链拒绝（例如 E:\Program Files (x86)\某游戏）——
    # 这类目录属于"已安装软件"，正确做法是卸载或搬迁，而不是直接删。
    chain = [seg.casefold() for seg in parts]
    if any(seg in {"windows", "program files", "program files (x86)", "programdata",
                   "system volume information", "recovery"} for seg in chain[:-1]):
        return False
    # 盘符必须有效
    if not re.match(r"^[A-Za-z]:", path):
        return False
    # 当前工作区/脚本自身所在目录绝不允许删除
    try:
        src = Path(path).resolve()
        bases = [Path.cwd().resolve(), Path(__file__).resolve().parent]
        ws = os.environ.get("DISK_CLEANUP_WORKSPACE", "")
        if ws:
            bases.append(Path(ws).resolve())
        if any(src == base or src in base.parents or base in src.parents for base in bases):
            return False
    except OSError:
        return False
    return True


def finalize_proposals(state):
    """为每条建议生成后端权威的动作白名单与目标盘候选（幂等）。AI 只给建议，权限由此处决定。"""
    disks = sorted(r["disk"] for r in disk_rows() if "free" in r)
    rows = {r["id"]: r for r in state.get("found", [])}
    for p in state.get("proposals", []):
        action = p.get("action")
        row = rows.get(p.get("id"), {})
        if action == "migrate":
            src = (row.get("disk") or "").upper()
            targets = [d for d in disks if d != src]
            p["targetDisks"] = targets
            allowed = ["migrate"] if targets else []
            # 应用数据可选用应用内存储管理（不自动删除）作为替代方案
            if row.get("kind") == "migrate-app":
                allowed.append("app-clean")
            # 用户明确要求：可搬迁项也允许用户主动选择"直接清理"（永久删除）。
            # 仅对 migrate 类（用户数据/应用数据）且不在系统保护名单内的目录开放；
            # 默认动作仍是搬迁，必须用户主动切换——绝不默认选中删除。
            if _can_delete(row):
                allowed.append("delete")
        elif action in ("app-clean", "uninstall", "system", "read-space", "recycle", "recycle-review"):
            allowed = [action]
        else:
            allowed = []
        if "skip" not in allowed:
            allowed.append("skip")
        p["allowedActions"] = allowed
        p["recommendedAction"] = action
        # 推荐目标盘优先非系统盘（下拉仍列出全部盘，用户可自行决定）
        pref = next((d for d in (p.get("targetDisks") or []) if not is_system_disk(d)), None)
        p["recommendedTargetDisk"] = p.get("targetDisk") or pref or (
            p["targetDisks"][0] if p.get("targetDisks") else None)

    # 可删项只保留"最外层"：若某可删目录的祖先目录也可删，则收回子项的删除权限，
    # 避免父目录与子目录同时出现在可删列表里造成误删与重复计算。
    deletable = [(p["id"], str(rows.get(p["id"], {}).get("path") or "").rstrip("\\").casefold())
                 for p in state.get("proposals", []) if "delete" in (p.get("allowedActions") or [])]
    delete_paths = [path for _, path in deletable if path]
    for pid, path in deletable:
        if not path:
            continue
        if any(path != other and path.startswith(other + "\\") for other in delete_paths):
            prop = next(p for p in state["proposals"] if p["id"] == pid)
            prop["allowedActions"] = [a for a in prop["allowedActions"] if a != "delete"]


def decisions_digest(decisions):
    return hashlib.sha256(json.dumps(decisions, sort_keys=True).encode()).hexdigest()


def validate_decisions(state, decisions):
    """校验并规范化用户决策。返回非跳过项 [{id, action, targetDisk?, targetPath?}]。"""
    props = {p["id"]: p for p in state.get("proposals", [])}
    rows = {r["id"]: r for r in state.get("found", [])}
    free = {r["disk"]: r["free"] for r in disk_rows() if "free" in r}
    if not isinstance(decisions, list) or not decisions or len(decisions) > len(props):
        raise ValueError("决策列表无效。")
    out, seen, targets = [], set(), []
    need_by_disk = {}
    for item in decisions:
        if not isinstance(item, dict):
            raise ValueError("决策项格式无效。")
        pid = item.get("id")
        if pid in seen or pid not in props or pid not in rows:
            raise ValueError("决策项不属于本轮建议，或重复。")
        seen.add(pid)
        prop, row = props[pid], rows[pid]
        action = item.get("action")
        allowed = prop.get("allowedActions") or [prop.get("action"), "skip"]
        if action not in allowed:
            raise ValueError(f"{row.get('name')} 不支持该处理方式。")
        if action == "skip":
            continue
        normalized = {"id": pid, "action": action}
        if action == "delete":
            # 永久删除必须由用户二次确认（页面弹窗确认后回传 confirmDelete）。
            if not item.get("confirmDelete"):
                raise ValueError(f"{row.get('name')} 的删除未确认，已拒绝。")
            if not _can_delete(row):
                raise ValueError(f"{row.get('name')} 不允许直接删除。")
        if action == "migrate":
            src = (row.get("disk") or "").upper()
            target = str(item.get("targetDisk") or "").upper()
            if (target not in (prop.get("targetDisks") or []) or target == src
                    or target not in free):
                raise ValueError(f"{row.get('name')} 的目标盘无效。")
            need_by_disk[target] = need_by_disk.get(target, 0) + (row.get("bytes") or 0)
            # 目标绝对路径由脚本重新生成，绝不采用页面值
            destination = make_target_path(src, target, row["path"])
            src_path = Path(row["path"])
            dst_path = Path(destination)
            if (dst_path == src_path or dst_path in src_path.parents
                    or src_path in dst_path.parents):
                raise ValueError(f"{row.get('name')} 的源与目标重叠，未通过校验。")
            for other in targets:
                o = Path(other)
                if dst_path == o or dst_path in o.parents or o in dst_path.parents:
                    raise ValueError("本轮有多个迁移目标重叠，请调整目标盘后重试。")
            targets.append(destination)
            normalized["targetDisk"] = target
            normalized["targetPath"] = destination
        out.append(normalized)
    for disk, need in need_by_disk.items():
        if free.get(disk, 0) < need:
            raise ValueError(f"{disk} 盘可用空间不足以承接本轮全部迁移，请分散目标盘或减少勾选。")
    return out


def build_composition(state, src_disk, units):
    """把已扫描的目录按用途归类，回答「磁盘空间到底去哪了」。units 为 (path, bytes, kind, name)。"""
    labels = {"system": "系统与保留", "app": "应用与游戏", "user": "用户文档",
              "cache": "应用缓存与临时", "other": "其他未分类"}
    buckets = {k: {"bytes": 0, "items": 0, "samples": []} for k in labels}
    # 顶层大目录的用途标签（由 deep_inspect_topdirs 补写），给未分类项一个合理归属，
    # 避免 D/E 盘整盘落到"其他未分类"造成误解。
    purpose_by_path = {r["path"].casefold(): (r.get("purpose") or "") for r in state.get("found", [])}
    for path, size, kind, name in units:
        purpose = purpose_by_path.get(str(path).casefold(), "")
        if kind in ("system",):
            key = "system"
        elif kind == "cache" or "缓存" in purpose:
            key = "cache"
        elif kind in ("migrate-app",) or any(t in purpose for t in
                ("Git", "Node", "Python", "项目", "工作区", "笔记", "编译", "构建")):
            key = "app"
        elif kind in ("migrate-user",) or any(t in purpose for t in
                ("下载", "备份", "资料", "文档", "视频", "归档", "知识")):
            key = "user"
        else:
            key = "other"
        bucket = buckets[key]
        bucket["bytes"] += size
        bucket["items"] += 1
        if len(bucket["samples"]) < 6 and size >= FOLDER_MIN:
            bucket["samples"].append({"name": name or Path(path).name, "path": str(path), "bytes": size})
    total = sum(b["bytes"] for b in buckets.values())
    categories = [{"key": k, "label": labels[k], "bytes": b["bytes"], "items": b["items"],
                   "samples": sorted(b["samples"], key=lambda s: -s["bytes"])}
                  for k, b in buckets.items()]
    state["composition"] = {"disk": src_disk, "scannedBytes": total, "categories": categories,
                            "note": "按目录用途归类统计（逻辑大小，含硬链接去重）；仅列识别到的部分。"}


def auto_plan(state, seconds):
    """扫描完成后深度下钻应用/缓存目录，生成可迁移、可清理建议与占用构成。不依赖外部手写清单。"""
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
    candidates = {}       # 可迁移 / 可清理候选
    units = []            # 占用构成归类单元

    def put(path, size, kind, name):
        key = str(path).casefold()
        if key in known:
            row = known[key]
        elif key in candidates:
            return
        else:
            # 盘符从实际路径推导（取首字母，不带冒号），不能写死 src_disk——
            # 否则 C 盘下钻出的应用被记成 "C:" 或错记到 D 盘。
            m = re.match(r"^([A-Za-z]):", str(path))
            disk = m.group(1).upper() if m else src_disk
            row = {"id": uuid.uuid4().hex, "disk": disk, "path": str(path),
                   "name": Path(path).name, "bytes": size, "coverage": "自动识别",
                   "directory": Path(path).is_dir(), "links": 0, "errors": 0, "pending": False,
                   "discovered": True}
            # 登记到"全部发现"，让下钻结果在页面可见；同一路径只登记一次。
            if not any(r.get("path", "").casefold() == key for r in state["found"]):
                state["found"].append(row)
        row["kind"] = kind
        row["kindName"] = name
        candidates[key] = (row, size, kind, name)

    def register_discovered(path, size, kind, name):
        """登记一个够大但不可自动处理的子目录到"全部发现"，附用途说明，供用户查看。"""
        key = str(path).casefold().rstrip("\\")
        if key in known or any(r.get("path", "").casefold().rstrip("\\") == key
                               for r in state["found"]):
            return
        m = re.match(r"^([A-Za-z]):", str(path))
        disk = m.group(1).upper() if m else src_disk
        label, basis = purpose_of(path, deep=False)
        # 列出直接子目录名（最多5个），让"里面是什么"一眼可见
        children_hint = []
        try:
            if Path(path).is_dir():
                children_hint = [e.name for e in Path(path).iterdir()
                                 if e.is_dir() and not e.name.startswith(".")][:5]
        except OSError:
            pass
        state["found"].append({"id": uuid.uuid4().hex, "disk": disk, "path": str(path),
                               "name": Path(path).name, "bytes": size,
                               "coverage": "深度下钻", "directory": Path(path).is_dir(),
                               "links": 0, "errors": 0, "pending": False,
                               "discovered": True, "kind": kind, "kindName": name,
                               "purpose": label, "purposeBasis": basis,
                               "childrenHint": children_hint})

    def scan_folder(folder):
        """下钻一个目录，把直接子项作为归类单元；返回子项 (path, size, kind, name)。"""
        try:
            if not folder.is_dir() or is_link(folder.lstat()):
                return []
            disk = str(folder)[:2].upper() if re.match(r"^[A-Za-z]:", str(folder)) else src_disk
            children, _ = inspect_volume(disk, seconds, folder)
        except OSError:
            return []
        refined_done.add(str(folder).casefold().rstrip("\\") + "\\")
        out = []
        for child in children:
            size = child["bytes"]
            if not size:
                continue
            kind, name = classify_path(child["path"])
            out.append((child["path"], size, kind, name))
            if size < FOLDER_MIN:
                continue
            # 大项（≥1GB）且可处理的才生成建议；其余（含小项、系统/软件目录）
            # 都登记到"全部发现"并附用途说明——看得见，但不塞进决策页。
            if size >= PROPOSAL_MIN and kind in ("migrate-app", "migrate-user", "cache"):
                put(Path(child["path"]), size, kind, name)
            else:
                register_discovered(child["path"], size, kind, name)
        return out

    # 1) 顶层构成：卷根直接子项已含递归合计，先整体归类。
    #    应用/用户/缓存类顶层目录若下钻失败，需以整体兜底，避免这部分空间凭空消失。
    refined_prefixes = []
    refined_done = set()
    for row in list(state["found"]):
        if row["disk"] != src_disk:
            continue
        kind, name = classify_path(row["path"])
        if row.get("directory") and kind in ("migrate-app", "migrate-user", "cache"):
            refined_prefixes.append(str(row["path"]).casefold().rstrip("\\") + "\\")
            continue
        units.append((row["path"], row["bytes"], kind, name))

    # 2) 用户目录：对每个目标盘的每个用户账户下钻 AppData 三处 + 常用主目录。
    #    不能只扫最紧张盘——否则最紧张盘是 D 时，C 盘的微信/Docker/豆包等应用数据会全部漏掉。
    for drill_disk in state["targets"]:
        users_root = Path(drill_disk + ":\\Users")
        if not users_root.is_dir():
            continue
        try:
            for user in users_root.iterdir():
                if user.name.casefold() in ("public", "default", "default user", "all users"):
                    continue
                for sub in ("AppData/Local", "AppData/Roaming", "AppData/LocalLow"):
                    units.extend(scan_folder(user / sub))
                for sub in USER_APP_DIRS:
                    folder = user / sub
                    if folder.is_dir():
                        units.extend(scan_folder(folder))
        except OSError as exc:
            notes.append(f"{drill_disk} 盘用户目录枚举受限：{exc}")

    # 3) Program Files / ProgramData：逐应用下钻，识别安装目录与缓存。
    #    同样对所有目标盘下钻，避免非最紧张盘上的应用数据被漏掉。
    #    scan_folder 会把达到阈值的 migrate-app/cache 子目录登记为候选。
    for drill_disk in state["targets"]:
        for top in ("Program Files", "Program Files (x86)", "ProgramData"):
            root = Path(drill_disk + ":\\") / top
            if root.is_dir():
                scan_folder(root)

    # 4) 所有目标盘的所有顶层大目录都下钻细分——深度清理不是只针对某一盘。
    #    用户选了哪些盘，就对哪些盘逐一深入，避免 D/E 盘只剩顶层容器、看不到里面是什么。
    for row in list(state["found"]):
        if row["disk"] not in state["targets"] or not row.get("directory"):
            continue
        size = row.get("bytes") or 0
        if size < FOLDER_MIN:
            continue
        kind, name = classify_path(row["path"])
        prefix = str(row["path"]).casefold().rstrip("\\") + "\\"
        if prefix in refined_done:
            continue
        got = scan_folder(Path(row["path"]))
        if not got:
            # 下钻失败：以整体兜底计入，避免这部分空间消失
            if prefix not in refined_done:
                units.append((row["path"], row["bytes"], kind, name))
                refined_done.add(prefix)

    # 对第 1 步跳过的顶层目录，若其前缀从未成功细分，同样兜底。
    for prefix in refined_prefixes:
        if prefix in refined_done:
            continue
        for row in state["found"]:
            if str(row["path"]).casefold().rstrip("\\") + "\\" == prefix:
                kind, name = classify_path(row["path"])
                units.append((row["path"], row["bytes"], kind, name))
                break

    # 4.5) 先给顶层大目录打用途标签，再生成占用构成——否则用途标签晚于构成写入，
    #      会导致 D/E 盘整盘落进"其他未分类"。此步必须在 build_composition 之前。
    notes.extend(deep_inspect_topdirs(state, seconds))
    build_composition(state, src_disk, units)

    # 5) 缓存：只提示用应用内清理，绝不自动删除。
    cache_rows = [(r, s, n) for r, s, k, n in candidates.values() if k == "cache"]
    shown_cache = 0
    for row, size, name in sorted(cache_rows, key=lambda item: -item[1]):
        if shown_cache >= 8:
            break
        if row["id"] not in known:
            state["found"].append(row)
        state["proposals"].append({
            "id": row["id"], "action": "app-clean", "estimate": size, "auto": True,
            "recommendation": f"发现 {name}（约 {size / 1024**3:.2f} GiB）。建议在该应用内执行清理，或用系统磁盘清理处理。",
            "impact": "缓存删除后应用首次运行会重建，可能变慢；本工具不会自动删除，请在应用内确认后操作。",
            "target": "在对应应用的设置→存储/缓存中执行清理；浏览器可用其自带的清除浏览数据功能。",
            "needAppReset": False,
            "whyNotDirect": why_not_direct(name, row.get("kindName") or ""),
        })
        shown_cache += 1

    # 6) 可迁移项：需有空白盘承接，且空间足够。
    migrate_rows = [(r, s, k, n) for r, s, k, n in candidates.values() if k in ("migrate-app", "migrate-user")]
    for row, size, kind, name in sorted(migrate_rows, key=lambda item: -item[1])[:12]:
        dst, target = _migration_target(state, src_disk, row["path"])
        if not dst:
            notes.append(f"未找到可承接 {row['name']} 的空白盘，已跳过迁移建议。")
            break
        if target["free"] < size:
            notes.append(f"{target['disk']} 盘剩余不足，无法承接 {row['name']}。")
            continue
        if row["id"] not in known:
            state["found"].append(row)
        app = name if kind == "migrate-app" else ""
        if kind == "migrate-app" and app:
            note = (f"这是 {app} 的数据或安装目录。搬迁后必须在该应用的设置里修改存储位置"
                    f"（如 WeGame/Steam 的存储位置、微信的文件管理路径），否则应用可能找不到数据。")
        else:
            note = "这是用户数据，搬迁通常无需改应用设置；路径已整体保留到目标盘。"
        state["proposals"].append({
            "id": row["id"], "action": "migrate", "estimate": size,
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
    # 全局去重：下钻与路径识别可能把同一路径登记多次（大小写/来源不同），
    # 保留 bytes 最大的一条，并同步删掉指向被合并行的建议。
    dedup_found(state)
    state["proposals"].sort(key=lambda p: -(p.get("estimate") or 0))
    notes.append(f"已自动生成 {len(state['proposals'])} 条建议（含迁移、系统项、回收站与大目录用途识别）。")
    return notes


def dedup_found(state):
    """按规范化路径去重 state['found']，并清理指向被删行的建议。"""
    keep = {}
    for row in state.get("found", []):
        key = str(row.get("path", "")).casefold().rstrip("\\")
        cur = keep.get(key)
        # 保留信息更全的一条：优先字节数大的，其次字段多的
        if cur is None or (row.get("bytes") or 0) > (cur.get("bytes") or 0):
            keep[key] = row
    state["found"] = list(keep.values())
    valid = {r["id"] for r in state["found"]}
    # 同一 id 只保留一条建议：优先带 sourceDisk/targetDisk（新版用途识别）或 estimate 更大的
    by_id = {}
    for p in state.get("proposals", []):
        if p.get("id") not in valid:
            continue
        cur = by_id.get(p["id"])
        score = (1 if p.get("targetDisk") else 0, p.get("estimate") or 0)
        cur_score = (1 if cur.get("targetDisk") else 0, cur.get("estimate") or 0) if cur else (-1, -1)
        if cur is None or score > cur_score:
            by_id[p["id"]] = p
    state["proposals"] = list(by_id.values())


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
    if state.get("mode") == "deep" and "deep" not in state:
        finalize_proposals(state)
    # 给所有英文目录/文件名补中文解释，页面直接展示，避免满屏英文看不懂。
    for row in state.get("found", []):
        if not row.get("nameZh"):
            row["nameZh"] = zh_name(row.get("name"))
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
    if "deep" in state or state["mode"] != "deep":
        raise ValueError("该次深度选择已经提交，或任务类型不符。")
    if choice.get("runId") != state["runId"] or choice.get("revision") != state["revision"]:
        raise ValueError("所选方案版本或条目无效，请打开本轮最新页面。")

    proposals = {p["id"]: p for p in state["proposals"]}
    rows = {row["id"]: row for row in state["found"]}
    # 新协议：decisions 含用户最终动作/目标盘；旧协议：ids 沿用建议预置动作。
    if "decisions" in choice:
        decisions = choice["decisions"]
        if choice.get("decisionDigest") != decisions_digest(decisions):
            raise ValueError("决策校验值不匹配，请从原对话重新打开本轮工作台。")
        if not isinstance(decisions, list) or not decisions or len(decisions) != len(set(
                d.get("id") for d in decisions if isinstance(d, dict))):
            raise ValueError("决策条目无效或重复。")
        validated = validate_decisions(state, decisions)
        ids = [d["id"] for d in decisions if isinstance(d, dict) and d.get("action") != "skip"]
    else:
        ids = choice.get("ids")
        if (not isinstance(ids, list) or not ids or len(set(ids)) != len(ids)
                or not set(ids) <= proposals.keys()):
            raise ValueError("所选编号不在本轮建议中，请打开本轮最新页面。")
        # 旧协议用建议预置动作；同样经过白名单与目标盘校验，确保安全不降级。
        legacy = [{"id": key, "action": proposals[key]["action"],
                   **({"targetDisk": proposals[key]["targetDisk"]}
                      if proposals[key].get("targetDisk") else {})} for key in ids]
        validated = validate_decisions(state, legacy)

    planned = [(proposals[d["id"]], rows[d["id"]], d) for d in validated]
    plans = [(Path(rows[key]["path"]), rows[key].get("name") or rows[key]["path"]) for key in ids]
    for i, (a, name_a) in enumerate(plans):
        for b, name_b in plans[i + 1:]:
            if a == b or a in b.parents or b in a.parents:
                raise ValueError(
                    f"本轮选择含重叠目标：{name_a}（{a}）与 {name_b}（{b}）"
                    "是同一路径或父子目录，请返回页面只保留其中一项。"
                )
    before = sample(state["targets"])
    entries = []
    for proposal, row, decision in planned:
        action = decision["action"]
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
                effective = {**proposal, "targetPath": decision["targetPath"],
                             "targetDisk": decision["targetDisk"]}
                moved = migrate_target(state, row, effective)
                entry.update(status="完成", note=f"已迁移到 {moved}。" + proposal.get("impact", ""))
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                entry.update(status="未确认完成", note=str(exc))
        elif action == "delete":
            # 用户主动选择的永久删除：再次确认路径安全后执行，失败不强行继续。
            try:
                target = Path(row["path"])
                if not _can_delete(row):
                    raise ValueError("该目录不在允许直接清理的范围内。")
                if not target.exists():
                    raise ValueError("目录已不存在，可能已被移动或删除。")
                if is_link(target.lstat()):
                    raise ValueError("目标是链接，已拒绝删除。")
                freed = row.get("bytes") or 0
                shutil.rmtree(target)
                if target.exists():
                    raise OSError("删除后目录仍然存在，未确认完成。")
                entry.update(status="完成",
                             note=f"已永久删除该目录（原占用 {freed/1024**3:.2f} GiB），不可恢复。")
            except (OSError, ValueError) as exc:
                entry.update(status="未确认完成", note=str(exc))
        else:
            entry.update(status="待 WorkBuddy 打开指定入口", note=proposal["target"])
        entries.append(entry)
    deep = {"before": before, "selected": ids, "entries": entries,
            "after": sample(state["targets"])}
    state["deep"] = deep
    state["decisions"] = choice.get("decisions")
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

