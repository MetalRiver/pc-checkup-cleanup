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
    rows, queue, warnings = [], deque(), []
    identities = set()
    deadline = time.monotonic() + seconds
    try:
        root = root or Path(drive + ":\\")
        ordinary_path(root)
        for path in root.iterdir():
            if time.monotonic() >= deadline:
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
    while queue and time.monotonic() < deadline:
        row, pending = queue.popleft()
        path = pending.popleft()
        try:
            ordinary_path(path)
            with os.scandir(path) as entries:
                for entry in entries:
                    if time.monotonic() >= deadline:
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
                     warnings=warnings, scanTime=timestamp())
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
    targets = [Path(row["path"]) for _, row in planned]
    if any(a == b or a in b.parents or b in a.parents
           for i, a in enumerate(targets) for b in targets[i + 1:]):
        raise ValueError("本轮选择包含重复或重叠的父子路径，请返回页面保留其中一项。")
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
    parser.add_argument("--seconds", type=int, default=30, help="深度扫描每盘预算")
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
        if not disks or not set(disks).issubset(available) or args.seconds < 1:
            parser.error("请选择可用的本机固定盘，并提供正数扫描预算。")
        create_run(args, disks)
        return
    if not args.run or (args.command in ("apply", "recommend") and not args.input):
        parser.error("请提供 --run 与此操作要求的 --input。")
    if args.command == "drill" and (not args.id or args.seconds < 1):
        parser.error("细分排查请提供 --id 和正数扫描预算。")
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

