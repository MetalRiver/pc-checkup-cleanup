"""WorkBuddy project MCP. The browser submits decisions; only the waiting chat receives them."""
import concurrent.futures
from contextlib import contextmanager
import hashlib
import json
import msvcrt
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import winreg

import cleanup

BASE = Path(__file__).resolve().parent
SUBMISSION_SECONDS = 4 * 60 * 60
EXTENSION_SECONDS = 2 * 60 * 60
WAIT_SECONDS = 15
# 端口固定：原来用 0 让系统随机分配，WorkBuddy/MCP 一重启端口就变，
# 用户手里已打开的页面立刻失效（Failed to fetch）。改为固定起点 + 有限递增。
PORT_BASE = 8791
PORT_TRIES = 20
sessions = {}
guard = threading.RLock()
server = None
stdout_lock = threading.Lock()


class ReviewUpdated(ValueError):
    pass


def session_file(run_id):
    return cleanup.run_directory(run_id, BASE) / "bridge_session.json"


@contextmanager
def review_lock(run_id):
    """Serialize first submission and session changes across local MCP processes."""
    with session_file(run_id).with_name(".review.lock").open("a+b") as lock:
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def saved_meta(run_id):
    try:
        meta = cleanup.load(session_file(run_id))
    except FileNotFoundError:
        return {}
    if (not isinstance(meta, dict) or meta.get("runId") != run_id
            or not re.fullmatch(r"[a-f0-9]{32}", str(meta.get("handle")))
            or not re.fullmatch(r"[A-Za-z0-9_-]{32}", str(meta.get("view")))
            or not re.fullmatch(r"[a-f0-9]{32}", str(meta.get("choiceHandle", meta.get("handle"))))
            or not isinstance(meta.get("expiresAt"), (int, float))):
        raise ValueError("本轮工作台会话记录无效。")
    return meta


def save_session(s):
    if s["test"]:
        return
    run_id = s["plan"]["runId"]
    cleanup.dump(session_file(run_id), {
        "view": s["view"], "handle": s["handle"], "runId": run_id,
        "revision": s["plan"]["revision"], "reviewDigest": s["digest"],
        "expiresAt": s["expires_at"], "choiceHandle": s["choice_handle"],
        "received": s["received"], "finished": s["finished"],
    })


def session_state(plan, test, meta):
    handle = meta.get("handle") or secrets.token_hex(16)
    return {"view": meta.get("view") or secrets.token_urlsafe(24), "handle": handle,
            "plan": plan, "digest": digest(plan), "test": test, "event": threading.Event(),
            "choice": None, "choice_handle": meta.get("choiceHandle") or handle,
            "received": bool(meta.get("received")), "finish": threading.Event(),
            "finished": bool(meta.get("finished")),
            "expires_at": meta.get("expiresAt", time.time() + SUBMISSION_SECONDS)}


def load_choice(s, *, legacy=False):
    plan = s["plan"]
    path = choice_path(plan["runId"], s["choice_handle"])
    paths = sorted(path.parent.glob("choice-*.json")) if legacy else [path]
    matches = []
    for candidate in paths:
        match = re.fullmatch(r"choice-([a-f0-9]{32})", candidate.stem)
        if not match:
            continue
        try:
            choice = cleanup.load(candidate)
        except FileNotFoundError:
            continue
        if not isinstance(choice, dict):
            raise ValueError("选择回执格式无效。")
        if (choice.get("runId") != plan["runId"] or choice.get("revision") != plan["revision"]
                or choice.get("reviewDigest") != s["digest"]):
            if legacy:
                continue
            raise ValueError("选择回执与本轮方案不一致。")
        if "decisions" in choice and choice.get("decisionDigest") != cleanup.decisions_digest(choice["decisions"]):
            raise ValueError("选择回执的动作校验值不匹配。")
        validate_selection(s, choice, saved=True)
        matches.append((match[1], choice))
    if matches:
        handle, choice = next((entry for entry in matches if entry[0] == s["choice_handle"]), matches[0])
        if any(item != choice for _, item in matches):
            raise ValueError("本轮存在内容不同的历史提交，请回到原对话核对回执。")
        s["choice_handle"], s["choice"] = handle, choice
        s["event"].set()

def start_server():
    """固定端口优先；被占用时有限递增，避免随机端口导致旧页面失效。"""
    global server
    if server is not None:
        return server
    last = None
    for offset in range(PORT_TRIES):
        port = PORT_BASE + offset
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Page)
            break
        except OSError as exc:
            last = exc
            server = None
    if server is None:
        raise ValueError(f"无法占用 {PORT_BASE}-{PORT_BASE + PORT_TRIES - 1} 任一端口：{last}")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def digest(plan):
    fields = [plan.get(k) for k in ("runId", "revision", "proposals", "found")]
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()


def check_run_id(run_id):
    if not isinstance(run_id, str) or not re.fullmatch(r"\d{8}-\d{6}-[a-f0-9]{8}", run_id):
        raise ValueError("请提供当前任务生成的轮次 ID。")


def read_run(run_id):
    check_run_id(run_id)
    path = cleanup.run_directory(run_id, BASE) / "run.json"
    cleanup.ordinary_path(path)
    data = cleanup.load(path)
    if data["runId"] != run_id or data["mode"] != "deep":
        raise ValueError("请打开本轮深度清理结果。")
    return data


def test_plan():
    volumes = cleanup.disk_rows()
    available = [row["disk"] for row in volumes if "free" in row]
    if not available:
        raise ValueError("没有可用的固定盘，无法进行只读测试。")
    disk = available[0]
    return {"mode": "deep", "runId": "test-" + secrets.token_hex(6), "revision": secrets.token_hex(8),
            "targets": [disk], "created": cleanup.sample([disk]), "warnings": [],
            "found": [{"id": "read-space", "disk": disk, "name": "读取可用空间（测试）",
                       "path": disk + ":\\", "bytes": None, "coverage": "只读测试"}],
            "proposals": [{"id": "read-space", "action": "read-space", "estimate": None,
                           "recommendation": "由当前对话读取一次这个盘的可用空间，并回复实测值。",
                           "impact": "只读，不会清理文件或改变设置。"}]}


def choice_path(run_id, handle):
    return cleanup.run_directory(run_id, BASE) / ("choice-" + handle + ".json")


def session_for(handle):
    s = sessions.get(handle)
    if s is None:
        # The pending MCP call can be retried after the process exits, without opening a second session.
        paths = (path for root in cleanup.run_roots(BASE) for path in root.glob("*/bridge_session.json"))
        for path in paths:
            try:
                meta = saved_meta(path.parent.name)
            except (OSError, ValueError):
                continue
            if meta.get("handle") == handle:
                start_server()
                s = sessions[handle] = session_state(read_run(path.parent.name), False, meta)
                break
        else:
            raise ValueError("本轮交互连接已失效，请从当前 WorkBuddy 对话重新打开。")
    if not s["test"]:
        plan = s["plan"]
        meta = saved_meta(plan["runId"])
        if (meta.get("handle") != handle or meta.get("reviewDigest", s["digest"]) != s["digest"]
                or meta.get("revision", plan["revision"]) != plan["revision"]):
            raise ReviewUpdated("本批工作台已更新。请回原对话查看当前批次。")
        s["expires_at"] = meta["expiresAt"]
        s["choice_handle"] = meta.get("choiceHandle") or handle
        s["received"] = bool(meta.get("received"))
        s["finished"] = bool(meta.get("finished"))
        if s["choice"] is None:
            load_choice(s, legacy="revision" not in meta)
        if s["finished"]:
            s["finish"].set()
    return s


def expired(s):
    return s["choice"] is None and time.time() >= s["expires_at"]


def open_review(run_id=None, test=False, recover=False, open_browser=False):
    if test is not True and test is not False and test is not None:
        raise ValueError("test 必须是布尔值。")
    if recover is not True and recover is not False and recover is not None:
        raise ValueError("recover 必须是布尔值。")
    if type(open_browser) is not bool:
        raise ValueError("open_browser 必须是布尔值。")
    if test and run_id:
        raise ValueError("测试任务单独运行。")
    with guard:
        if test:
            s = session_state(test_plan(), True, {})
            recovered = False
        else:
            check_run_id(run_id)
            run_path = cleanup.run_directory(run_id, BASE) / "run.json"
            cleanup.ordinary_path(run_path)
            with review_lock(run_id):
                state = read_run(run_id)
                if state.get("finalizedAt"):
                    return report_result(state)
                if not state["proposals"]:
                    raise ValueError("本轮没有需要决定的项目。")
                meta = saved_meta(run_id)
                if meta.get("revision", state["revision"]) != state["revision"]                         or meta.get("reviewDigest", digest(state)) != digest(state):
                    meta = {}
                s = session_state(state, False, meta)
                load_choice(s, legacy="revision" not in meta)
                if "deep" in state and (not s["choice"] or not s["received"]):
                    raise ValueError("本轮已有执行记录，请查看本轮成果。")
                recovered = bool(s["choice"] or (meta and not expired(s) and not recover))
                if not recovered:
                    if any("allowedActions" not in proposal for proposal in state["proposals"]):
                        cleanup.finalize_proposals(state)
                        cleanup.dump(run_path, state)
                    s = session_state(state, False, {})
                if not recovered or "revision" not in meta:
                    save_session(s)
        start_server()
        sessions[s["handle"]] = s
        url = f"http://127.0.0.1:{server.server_port}/{s['view']}/"
    return {"page": url, "handle": s["handle"], "expiresAt": s["expires_at"], "openIn": "workbuddy",
            "expiresInSeconds": max(0, int(s["expires_at"] - time.time())),
            "recovered": recovered, "submitted": s["choice"] is not None,
            "next": "在 WorkBuddy 右侧内置浏览器导航到 page，原对话保留链接并立即调用 wait_review，保持本轮执行。"}

def validate_selection(s, payload, *, saved=False):
    plan = s["plan"]
    if not s["test"] and not saved:
        current = read_run(plan["runId"])
        if "deep" in current or digest(current) != s["digest"]:
            raise ValueError("本轮方案或执行状态已变化，请重新打开。")
    ids = payload.get("ids")
    by_id = {p["id"]: p for p in plan["proposals"]}
    if (payload.get("runId") != plan["runId"] or payload.get("revision") != plan["revision"]
            or not isinstance(ids, list) or not ids or len(ids) > len(by_id)
            or not all(isinstance(i, str) and i in by_id for i in ids) or len(ids) != len(set(ids))):
        raise ValueError("选择与本轮条目不匹配。")
    if "decisions" in payload:
        decisions = payload["decisions"]
        if (not isinstance(decisions, list) or len(decisions) != len(ids)
                or not all(isinstance(d, dict) and isinstance(d.get("id"), str)
                           and d.get("action") != "skip" for d in decisions)
                or {d["id"] for d in decisions} != set(ids)):
            raise ValueError("勾选与决策条目不一致。")
        if not s["test"] and not saved:
            cleanup.validate_decisions(plan, decisions)
        if s["test"] and any(d["action"] != "read-space" for d in decisions):
            raise ValueError("此工作台仅用于读取可用空间。")
    rows = {r["id"]: r for r in plan["found"]}
    chosen = [(Path(rows[i]["path"]), rows[i].get("name") or rows[i]["path"]) for i in ids]
    for n, (a, name_a) in enumerate(chosen):
        for b, name_b in chosen[n + 1:]:
            if a == b or a in b.parents or b in a.parents:
                raise ValueError(f"选择中含有重叠目标：{name_a}（{a}）与 {name_b}（{b}）。")
    return ids


def save_choice(s, payload):
    if not isinstance(payload, dict):
        raise ValueError("选择无效。")
    ids = validate_selection(s, payload, saved=s["choice"] is not None)
    choice = {"runId": s["plan"]["runId"], "revision": s["plan"]["revision"],
              "ids": ids, "reviewDigest": s["digest"]}
    if "decisions" in payload:
        choice["decisions"] = payload["decisions"]
        choice["decisionDigest"] = cleanup.decisions_digest(choice["decisions"])
    if s["choice"] is not None:
        if choice != s["choice"]:
            raise ValueError("本轮已保存一份选择。请回到原对话查看已提交的动作和目标盘。")
        return
    if expired(s):
        raise ValueError("本轮提交时间已到，请回到原对话重新打开工作台。")
    if not s["test"]:
        cleanup.dump(choice_path(choice["runId"], s["choice_handle"]), choice)
    s["choice"] = choice
    s["event"].set()


def wait_review(handle):
    with guard:
        s = session_for(handle)
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        with guard:
            s = session_for(handle)
            if not s["test"]:
                current = read_run(s["plan"]["runId"])
                if current["revision"] != s["plan"]["revision"]:
                    raise ReviewUpdated("已进入下一批，请打开当前工作台。")
                if current.get("finalizedAt"):
                    return report_result(current)
            if s["choice"] is not None:
                if s["test"]:
                    return review_result(s)
                run_id = s["plan"]["runId"]
                with review_lock(run_id):
                    return review_result(session_for(handle))
            if expired(s):
                return {"state": "已过期", "next": "如需继续，用同一 run_id 重新打开工作台。"}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"state": "等待用户提交", "expiresAt": s["expires_at"],
                        "next": "保持本轮对话并再次调用 wait_review；此状态只是单次等待结束。"}
        s["event"].wait(min(1, remaining))


def review_result(s):
    plan = s["plan"]
    choice = s["choice"]
    run_id = plan["runId"]
    result = {"state": "已收到", "runId": run_id, "selected": []}
    if not s["test"]:
        current = read_run(run_id)
        if current["revision"] != plan["revision"]:
            raise ReviewUpdated("该回执属于之前的批次，请回原对话查看当前批次。")
        if "deep" in current:
            return {"state": "已有执行记录", **cleanup.batch_progress(current),
                    "next": "调用 batch_status 查看本批状态，按原任务继续。"}
        if digest(current) != s["digest"]:
            raise ValueError("本轮方案已变化，已保存的选择需要重新核对。")
    rows = {r["id"]: r for r in plan["found"]}
    decisions = {d["id"]: d for d in choice.get("decisions", [])}
    proposals = {p["id"]: p for p in plan["proposals"]}
    for item_id in choice["ids"]:
        row = rows[item_id]
        selected = {**proposals[item_id], **decisions.get(item_id, {}),
                    "disk": row["disk"], "path": row["path"]}
        if selected["action"] == "migrate":
            selected["targetPath"] = cleanup.make_target_path(row["disk"], selected["targetDisk"], row["path"])
        else:
            selected.pop("targetDisk", None)
            selected.pop("targetPath", None)
        result["selected"].append(selected)
    if s["test"]:
        result["next"] = "在对话回复收到只读项目，再调用 read_test_space(handle)。"
    else:
        directory = cleanup.run_directory(run_id, BASE)
        result.update(runFile=str(directory / "run.json"),
                      choiceFile=str(choice_path(run_id, s["choice_handle"])),
                      next="在对话逐项核对用户选择。若恢复了旧任务，先核对是否已尝试执行，再按技能处理。")
    if not s["received"]:
        s["received"] = True
        save_session(s)
    return result

def read_test_space(handle):
    with guard:
        s = session_for(handle)
        if not s["test"] or not s["received"] or s["choice"]["ids"] != ["read-space"]:
            raise ValueError("该只读测试尚未收到选择。")
        if "result" not in s:
            sample = cleanup.sample(s["plan"]["targets"])
            if any("free" not in row for row in sample["volumes"]) or not sample["volumes"]:
                raise ValueError("读取失败，请在原对话告知用户。")
            s["result"] = sample
        return {"state": "只读测试已完成", "sample": s["result"], "next": "在当前对话回复盘符、可用 GiB 和采样时间。"}


def wait_finish(handle):
    """Compatibility tool for clients that use the workbench finish button."""
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        with guard:
            s = session_for(handle)
            if s["test"] or not s["received"]:
                raise ValueError("该任务尚未进入执行阶段，无需等待结束确认。")
            state = read_run(s["plan"]["runId"])
            if s["finished"] or state.get("finalizedAt"):
                return report_result(state)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"state": "等待用户确认结束", "next": "在原对话询问是否结束或处理剩余项。"}
        s["finish"].wait(min(1, remaining))


def report_result(state):
    directory = cleanup.run_directory(state["runId"], BASE)
    page, image = directory / cleanup.REPORT_HTML, directory / cleanup.REPORT_SVG
    if not page.is_file() or not image.is_file():
        raise ValueError("本次成果报告缺失，请重新生成。")
    return {**cleanup.batch_progress(state), "phase": "finished", "state": "本轮已结束",
            "finishReason": state.get("finishReason"),
            "batches": len(cleanup.batch_records(state)), "report": str(page), "reportImage": str(image),
            "next": "在原对话展示 reportImage 指向的报告图，并附上 report 的可打开版本。"}


def batch_status(run_id):
    state = read_run(run_id)
    if already_finished(state):
        return report_result(state)
    return {**cleanup.batch_progress(state), "runFile": str(cleanup.run_directory(run_id, BASE) / "run.json")}


def already_finished(state):
    if state.get("finalizedAt"):
        return True
    meta = saved_meta(state["runId"])
    return bool(state.get("deep") and meta.get("finished") and meta.get("revision") == state["revision"])


def apply_batch_decision(run_id, revision, decision):
    """Persist the confirmed choice while holding the cross-process review lock."""
    directory = cleanup.run_directory(run_id, BASE)
    with cleanup.run_lock(directory):
        state = read_run(run_id)
        # A retry after advancement can reopen the already-created next batch.
        history = state.get("batchHistory", [])
        if (decision == "continue" and "deep" not in state and history
                and revision == history[-1]["revision"]):
            return
        if state["revision"] != revision:
            raise ReviewUpdated("这是之前批次的选择，请核对当前批次后再决定。")
        if already_finished(state):
            if decision == "finish" or (decision == "skip" and state.get("finishReason") == "no-selection"):
                return report_result(state)
            raise ValueError("本次任务已结束。")
        if decision in ("finish", "skip"):
            meta = saved_meta(run_id)
            if decision == "skip":
                current = session_state(state, False, meta)
                load_choice(current, legacy="revision" not in meta)
                if current["choice"] is not None:
                    raise ValueError("当前批次已收到提交，请先核对处理状态。")
            cleanup.finalize_run(directory, state, leave_remaining=decision == "skip")
            return report_result(state)
        cleanup.advance_batch(directory, state)


def batch_decision(run_id, revision, decision, open_browser=False):
    check_run_id(run_id)
    if decision not in ("continue", "finish", "skip"):
        raise ValueError("请选择 continue、finish 或 skip。")
    if type(open_browser) is not bool:
        raise ValueError("open_browser 必须是布尔值。")
    with guard:
        cleanup.ordinary_path(cleanup.run_directory(run_id, BASE) / "run.json")
        with review_lock(run_id):
            result = apply_batch_decision(run_id, revision, decision)
        if decision in ("finish", "skip"):
            return result
        return {**open_review(run_id, open_browser=open_browser), **batch_status(run_id)}


SETTINGS_ENTRIES = {"installed-apps": "ms-settings:appsfeatures", "storage": "ms-settings:storagesense"}
BROWSER_ENTRIES = {
    "chrome": ("chrome.exe", "chrome://settings/clearBrowserData", "打开 Chrome 清理设置"),
    "edge": ("msedge.exe", "edge://settings/clearBrowserData", "打开 Edge 清理设置"),
}
BROWSER_INSTALL_DIRS = {"chrome.exe": "Google/Chrome/Application",
                        "msedge.exe": "Microsoft/Edge/Application"}


def launch_entries(plan):
    if plan.get("simulation"):
        return {}
    entries = {}
    if isinstance(plan.get("applications"), list):
        entries["installed-apps"] = ("installed-apps", "打开 Windows 已安装应用")
    rows = {row["id"]: row for row in plan["found"]}
    for proposal in plan["proposals"]:
        if proposal["action"] != "app-clean":
            continue
        row = rows.get(proposal["id"])
        if not row:
            continue
        parts = tuple(part.casefold() for part in Path(row["path"]).parts)
        browser = next((key for key, vendor in (("chrome", "google"), ("edge", "microsoft"))
                        if any(parts[i:i+3] == (vendor, key, "user data")
                               for i in range(1, len(parts) - 2))), None)
        if browser:
            entries[proposal["id"]] = (browser, BROWSER_ENTRIES[browser][2])
        elif len(parts) == 6 and parts[1] == "users" and parts[3:] == ("appdata", "local", "temp"):
            entries[proposal["id"]] = ("storage", "打开 Windows 存储设置")
    return entries


def registered_browser(filename):
    from app_usage import executable

    key_name = r"Software\Microsoft\Windows\CurrentVersion\App Paths" + "\\" + filename
    views = ((winreg.HKEY_CURRENT_USER, 0), (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
             (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY))
    for hive, view in views:
        try:
            with winreg.OpenKey(hive, key_name, 0, winreg.KEY_READ | view) as key:
                candidate = executable(str(winreg.QueryValueEx(key, "")[0]))
            if candidate is not None and candidate.name.casefold() == filename and candidate.is_file():
                cleanup.ordinary_path(candidate)
                return candidate
        except OSError:
            continue
    for variable in ("ProgramFiles", "ProgramFiles(x86)", "LocalAppData"):
        folder = os.environ.get(variable)
        if not folder:
            continue
        candidate = Path(folder) / BROWSER_INSTALL_DIRS[filename] / filename
        try:
            if candidate.is_file():
                cleanup.ordinary_path(candidate)
                return candidate
        except OSError:
            continue
    raise ValueError("找不到该浏览器的安装入口，请从开始菜单打开。")


def launch_entry(s, payload):
    if (s["test"] or expired(s) or not isinstance(payload, dict) or set(payload) != {"id"}
            or not isinstance(payload["id"], str)):
        raise ValueError("请从当前工作台打开已支持的入口。")
    entry = launch_entries(s["plan"]).get(payload["id"])
    if entry is None:
        raise ValueError("该项目没有可打开的入口。")
    key, label = entry
    if key in SETTINGS_ENTRIES:
        os.startfile(SETTINGS_ENTRIES[key])
    else:
        filename, url, _ = BROWSER_ENTRIES[key]
        path = registered_browser(filename)
        subprocess.Popen([str(path), url], cwd=path.parent)
    return {"state": "opened", "label": label}


def status_detail(s):
    """返回页面轮询用的阶段状态、剩余未选项与最新磁盘可用空间。"""
    choice = s["choice"]
    now = time.time()
    data = {"phase": "awaiting_selection", "message": "等待勾选", "submitted": choice is not None,
            "ids": choice["ids"] if choice else [], "finished": False, "reviewed": False,
            "unselected": [], "volumes": None, "serverNow": now, "expiresAt": s["expires_at"],
            "canExtend": choice is None and now < s["expires_at"],
            "batch": len(s["plan"].get("batchHistory", [])) + 1}
    if choice:
        data["decisions"] = choice.get("decisions", [])
    if not s["test"]:
        current = read_run(s["plan"]["runId"])
        if current["revision"] != s["plan"]["revision"]:
            raise ReviewUpdated("已进入下一批。请从原对话打开下一批工作台。")
        if current.get("finalizedAt"):
            data.update(phase="finished", message="本次任务已结束，汇总报告已生成。",
                        canExtend=False, finished=True)
            return data
        data["canSkip"] = choice is None and "deep" not in current
    if choice is None and now >= s["expires_at"]:
        data.update(phase="expired", message="提交时间已到。请回到原对话重新打开工作台。")
        return data
    if s.get("finished"):
        data.update(phase="finished", message="本轮已结束，成果报告已生成。", finished=True, reviewed=True)
        return data
    if "result" in s:
        r = s["result"]["volumes"][0]
        data.update(phase="done", message=f"测试完成 · {r['disk']} 盘可用 {r['free']/1024**3:.2f} GiB。",
                    finished=True, volumes=s["result"]["volumes"])
        return data
    if choice and not s["received"]:
        data.update(phase="submitted", message="选择已保存，等待 WorkBuddy 领取。")
    if s["received"] and not s["test"]:
        data.update(phase="received", message="WorkBuddy 已领取，等待执行结果。")
        state = current
        progress = cleanup.batch_progress(state)
        data["batch"] = progress["batch"]
        if progress["phase"] == "finished":
            data.update(phase="finished", message="本次任务已结束，汇总报告已生成。", finished=True, reviewed=True)
            return data
        deep = state.get("deep") or {}
        samples = [x for x in (deep.get("reviews") or []) + [deep.get("after"), state.get("created")] if x]
        if samples:
            data["volumes"] = samples[-1].get("volumes")
        if deep:
            if set(deep.get("selected", [])) != set(choice["ids"]):
                data["message"] = "本轮已由另一份选择处理。请查看原对话。"
                return data
            rows = {row["id"]: row for row in state["found"]}
            data["unselected"] = [{"id": p["id"], "name": rows[p["id"]]["name"], "disk": rows[p["id"]]["disk"]}
                                  for p in cleanup.available_proposals(state)]
            data["reviewed"] = data["canFinish"] = progress["phase"] == "ready"
            data["canContinue"] = data["reviewed"] and progress["remainingCount"] > 0
            if data["reviewed"]:
                data.update(phase="verified", message=("本批已复核，可继续处理剩余项或结束任务。"
                                                         if data["canContinue"] else
                                                         "本批已复核，所有可选项已处理。"))
            elif progress["phase"] == "awaiting_manual":
                data.update(phase="awaiting_manual", message="本批仍有需要在应用或系统设置中处理的项目。")
            else:
                data["message"] = "已记录处理结果，等待本批复核。"
    elif s["received"]:
        data.update(phase="received", message="WorkBuddy 已领取，等待读取可用空间。")
    return data


class InactiveReview(ValueError):
    pass


class Page(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self, code, data, kind="application/json"):
        raw = (data if kind.startswith("text/html") else json.dumps(data, ensure_ascii=False)).encode()
        self.send_response(code)
        headers = {"Cache-Control":"no-store", "Referrer-Policy":"no-referrer",
                   "X-Content-Type-Options":"nosniff", "Content-Type":kind + "; charset=utf-8",
                   "Content-Length":str(len(raw))}
        # 允许本机回环的任意端口跨源：bridge 端口递增后，旧页面仍可自动找回并提交。
        origin = self.headers.get("Origin") or ""
        if re.fullmatch(r"http://127\.0\.0\.1:\d{2,5}", origin):
            headers["Access-Control-Allow-Origin"] = origin
            headers["Vary"] = "Origin"
            headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            headers["Access-Control-Allow-Headers"] = "Content-Type"
        for key, val in headers.items():
            self.send_header(key, val)
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self):
        # CORS 预检：仅在来源是本机回环时放行。
        origin = self.headers.get("Origin") or ""
        if re.fullmatch(r"http://127\.0\.0\.1:\d{2,5}", origin):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Max-Age", "600")
            self.end_headers()
            return
        self.send_response(204)
        self.end_headers()

    def target(self):
        # 只接受本机回环来源（防 DNS rebinding）；端口不再写死，
        # 这样 bridge 因端口递增重启后，用户手里已打开的页面仍能提交。
        host = self.headers.get("Host") or ""
        if not re.fullmatch(r"127\.0\.0\.1:\d{2,5}", host):
            raise ValueError("连接目标不匹配。")
        match = re.fullmatch(r"/([A-Za-z0-9_-]{32})/(status|submit|extend|continue|finish|launch)?", self.path)
        if not match:
            raise ValueError("链接无效。")
        with guard:
            for handle, s in sessions.items():
                if secrets.compare_digest(s["view"], match[1]):
                    return session_for(handle), match[2]
        raise InactiveReview("本轮链接已失效。")

    def do_GET(self):
        try:
            s, route = self.target()
            if route == "status":
                with guard:
                    data = status_detail(session_for(s["handle"]))
                self.respond(200, data)
            elif route is None:
                source = read_run(s["plan"]["runId"]) if s["received"] and not s["test"] else s["plan"]
                plan = {**source, "liveReview": True, "archived": False}
                if not s["test"]:
                    plan["launchOptions"] = {item: label for item, (_, label) in launch_entries(source).items()}
                data = json.dumps(plan, ensure_ascii=False).replace("<", "\\u003c")
                html = (BASE/"disk_cleanup_template.html").read_text(encoding="utf-8-sig")
                self.respond(200, html.replace("<!--RUN-DATA-->", data, 1), "text/html")
            else:
                self.respond(404, {})
        except ReviewUpdated as exc:
            self.respond(410, {"error": str(exc)})
        except InactiveReview as exc:
            self.respond(404, {"error": str(exc)})
        except (OSError, ValueError) as exc:
            self.respond(409, {"error": str(exc)})

    def do_POST(self):
        try:
            s, route = self.target()
            origin = self.headers.get("Origin") or ""
            kind = self.headers.get_content_type()
            # 同样只校验"来自本机某个端口"，不绑定具体端口号
            if not (re.fullmatch(r"http://127\.0\.0\.1:\d{2,5}", origin) and kind == "application/json"):
                raise ValueError("请在本轮工作台提交。")
            if route == "continue":
                if s["test"] or not s["received"]:
                    raise ValueError("本批复核完成后才能继续。")
                plan = s["plan"]
                result = batch_decision(plan["runId"], plan["revision"], "continue")
                self.respond(200, {"page": result["page"], "batch": result["batch"]})
                return
            if route not in ("submit", "extend", "finish", "launch"):
                raise ValueError("请在本轮工作台提交。")
            payload = None
            if route != "finish":
                length = int(self.headers.get("Content-Length", 0))
                if not 0 < length <= 32768:
                    raise ValueError("选择无效。")
                payload = json.loads(self.rfile.read(length))
            with guard:
                if s["test"]:
                    result = update_review(session_for(s["handle"]), route, payload)
                else:
                    with review_lock(s["plan"]["runId"]):
                        result = update_review(session_for(s["handle"]), route, payload)
            self.respond(200, result)
        except ReviewUpdated as exc:
            self.respond(410, {"error": str(exc)})
        except InactiveReview as exc:
            self.respond(404, {"error": str(exc)})
        except (OSError, ValueError, KeyError) as exc:
            self.respond(409, {"error": str(exc)})


def update_review(s, route, payload):
    plan = s["plan"]
    if not s["test"] and read_run(plan["runId"])["revision"] != plan["revision"]:
        raise ReviewUpdated("已进入下一批。请从原对话打开下一批工作台。")
    if route == "launch":
        return launch_entry(s, payload)
    if route == "submit":
        if not s["test"] and read_run(plan["runId"]).get("finalizedAt"):
            raise ValueError("本次任务已经结束。")
        save_choice(s, payload)
        return {"state": "submitted"}
    if route == "extend":
        if payload != {"expiresAt": s["expires_at"]}:
            raise ValueError("提交期限已更新，请查看工作台上的剩余时间。")
        if s["choice"] is not None or expired(s):
            raise ValueError("只有仍可提交的工作台能延长时间。")
        s["expires_at"] += EXTENSION_SECONDS
        save_session(s)
        return status_detail(s)
    if s["finished"]:
        return {"state": "finished"}
    status = status_detail(s)
    if status["finished"]:
        return {"state": "finished"}
    if status.get("canSkip"):
        apply_batch_decision(plan["runId"], plan["revision"], "skip")
        s["event"].set()
        return {"state": "finished"}
    if not status.get("canFinish"):
        raise ValueError("本批复核完成后可以结束本次任务。")
    apply_batch_decision(plan["runId"], plan["revision"], "finish")
    s["finished"] = True
    save_session(s)
    s["finish"].set()
    return {"state": "finished"}



def tool(name, description, properties, required=()):
    return {"name": name, "description": description, "inputSchema": {
        "type":"object", "properties":properties, "required":list(required), "additionalProperties":False}}


HANDLE = {"handle": {"type":"string"}}
TOOLS = [
    tool("open_review", "打开当前任务的磁盘选择页。test=true 只做无删除链路测试；正式任务传 run_id；同一有效轮次复用原页面和已保存回执；recover=true 仅能换掉未提交的页面。返回页面链接与对话等候凭据。",
         {"run_id":{"type":"string"}, "test":{"type":"boolean"}, "recover":{"type":"boolean"}}),
    tool("wait_review", "在发出页面链接的原对话领取用户勾选的清单（含最终动作与目标盘）；单次等待结束请继续调用，直到收到回执或工作台过期。", HANDLE, ["handle"]),
    tool("read_test_space", "完成已经领取的只读磁盘测试并返回实测值。", HANDLE, ["handle"]),
    tool("wait_finish", "apply 和 verify 完成后调用，等待用户在页面点「结束本轮清理」，返回成果报告路径；等待超时继续调用。", HANDLE, ["handle"]),
    tool("batch_status", "查看当前批次、是否已复核、剩余可选数量或最终报告。用于原对话继续任务。",
         {"run_id": {"type": "string"}}, ["run_id"]),
    tool("batch_decision", "根据用户明确选择继续、复核后结束，或未提交时选 skip 结束并生成报告。",
         {"run_id": {"type": "string"}, "revision": {"type": "string"},
          "decision": {"type": "string", "enum": ["continue", "finish", "skip"]}},
         ["run_id", "revision", "decision"]),
]


def dispatch(request):
    method = request.get("method")
    params = request.get("params", {})
    if method == "initialize":
        supported = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
        return {"protocolVersion": params.get("protocolVersion") if params.get("protocolVersion") in supported else supported[0],
                "capabilities":{"tools":{}}, "serverInfo":{"name":"disk-cleanup-review", "version":"1.5.4"}}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        funcs = {"open_review":open_review, "wait_review":wait_review,
                 "read_test_space":read_test_space, "wait_finish":wait_finish,
                 "batch_status":batch_status, "batch_decision":batch_decision}
        try:
            value = funcs[params["name"]](**params.get("arguments", {}))
            return {"content":[{"type":"text", "text":json.dumps(value, ensure_ascii=False)}]}
        except (KeyError, TypeError, OSError, ValueError) as exc:
            return {"isError":True, "content":[{"type":"text","text":str(exc)}]}
    raise ValueError("未知方法")


def respond(request):
    try:
        reply = {"result": dispatch(request)}
    except Exception as exc:
        reply = {"error":{"code":-32603, "message":str(exc)}}
    with stdout_lock:
        print(json.dumps({"jsonrpc":"2.0", "id":request["id"], **reply}), flush=True)


if __name__ == "__main__":
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        for line in sys.stdin.buffer:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if isinstance(message, dict) and "id" in message and "method" in message:
                pool.submit(respond, message)
