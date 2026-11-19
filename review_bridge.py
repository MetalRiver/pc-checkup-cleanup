"""WorkBuddy project MCP. The browser submits decisions; only the waiting chat receives them."""
import concurrent.futures
import hashlib
import json
from pathlib import Path
import re
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import cleanup

BASE = Path(__file__).resolve().parent
TTL = 900
sessions = {}
guard = threading.RLock()
server = None
stdout_lock = threading.Lock()


def digest(plan):
    fields = [plan.get(k) for k in ("runId", "revision", "proposals", "found")]
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()


def read_run(run_id):
    if not isinstance(run_id, str) or not re.fullmatch(r"\d{8}-\d{6}-[a-f0-9]{8}", run_id):
        raise ValueError("请提供当前任务生成的轮次 ID。")
    path = BASE / "runs" / run_id / "run.json"
    cleanup.ordinary_path(path)
    data = cleanup.load(path)
    if data["runId"] != run_id or data["mode"] != "deep":
        raise ValueError("请打开本轮深度清理结果。")
    # 兼容旧轮次：补齐 v1.4 的决策白名单/目标盘字段（幂等）。
    if "deep" not in data:
        cleanup.finalize_proposals(data)
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
    return BASE / "runs" / run_id / ("choice-" + handle + ".json")


def session_for(handle, *, allow_submitted=False):
    s = sessions.get(handle)
    if s is None:
        raise ValueError("本轮交互连接已失效，请从当前 WorkBuddy 对话重新打开。")
    if time.monotonic() >= s["expires"]:
        # 已提交并已落盘的会话仍可领取一次，避免用户勾选因超时丢失。
        if allow_submitted and s["ids"] and not s["test"] \
                and choice_path(s["plan"]["runId"], handle).is_file():
            s["expires"] = time.monotonic() + TTL
        else:
            raise ValueError("本轮交互连接已过期，请从当前 WorkBuddy 对话重新打开。")
    return s


def open_review(run_id=None, test=False, recover=False):
    if test is not True and test is not False and test is not None:
        raise ValueError("test 必须是布尔值。")
    if recover is not True and recover is not False:
        raise ValueError("recover 必须是布尔值。")
    if (test is True) and run_id:
        raise ValueError("测试任务单独运行。")
    state = test_plan() if test else read_run(run_id)
    if "deep" in state or not state["proposals"]:
        raise ValueError("本轮已提交，或没有需要决定的项目。")
    if not test:
        # 把补齐后的白名单字段落盘，确保页面、桥接与应用端 reviewDigest 完全一致。
        cleanup.dump(BASE / "runs" / run_id / "run.json", state)
    handle, view = secrets.token_hex(16), secrets.token_urlsafe(24)
    global server
    with guard:
        for key in list(sessions):
            if sessions[key]["expires"] <= time.monotonic():
                del sessions[key]
        if not test:
            # 同一 runId 已有会话：默认幂等返回原页面；recover=true 时作废旧 handle 并新建。
            existing = [item for item in sessions.values() if item["plan"]["runId"] == run_id]
            if existing and not recover:
                old = existing[0]
                return {"page": f"http://127.0.0.1:{server.server_port}/{old['view']}/",
                        "handle": old["handle"], "expiresInSeconds": max(1, int(old["expires"] - time.monotonic())),
                        "recovered": True,
                        "next": "这是本轮已存在的工作台，直接展示此页面并用同一 handle 调用 wait_review。若需强制接管，传 recover=true。"}
            for item in existing:
                item["expires"] = time.monotonic() - 1
                item["superseded"] = True
        if server is None:
            server = ThreadingHTTPServer(("127.0.0.1", 0), Page)
            threading.Thread(target=server.serve_forever, daemon=True).start()
        sessions[handle] = {"view": view, "handle": handle, "plan": state,
                            "digest": digest(state), "test": test,
                            "event": threading.Event(), "ids": None, "decisions": None,
                            "received": False, "finish": threading.Event(), "finished": False,
                            "expires": time.monotonic() + TTL}
        url = f"http://127.0.0.1:{server.server_port}/{view}/"
        if not test:
            template = (BASE / "disk_cleanup_template.html").read_text(encoding="utf-8-sig")
            bound = {**state, "reviewUrl": url}
            cleanup.write_page(BASE / "runs" / run_id / "disk_cleanup_decision.html", template,
                               bound, "../../disk_cleanup_decision.legacy.html")
            root = BASE / "disk_cleanup_decision.html"
            if root.exists():
                match = re.search(r'<script type="application/json" id="run">(.*?)</script>',
                                  root.read_text(encoding="utf-8-sig"), re.S)
                if match and json.loads(match[1]).get("runId") == run_id:
                    cleanup.write_page(root, template, bound, "disk_cleanup_decision.legacy.html")
    return {"page": url, "handle": handle, "expiresInSeconds": TTL,
            "next": "立即在当前对话展示可点击的 page 链接，然后调用 wait_review 等待用户选择。不要结束这一轮。"}


def validate_selection(s, payload):
    plan = s["plan"]
    if not s["test"]:
        current = read_run(plan["runId"])
        if "deep" in current or digest(current) != s["digest"]:
            raise ValueError("本轮方案或执行状态已变化，请重新打开。")
    ids = payload.get("ids")
    by_id = {p["id"]: p for p in plan["proposals"]}
    if (payload.get("runId") != plan["runId"] or payload.get("revision") != plan["revision"]
            or not isinstance(ids, list) or not ids or len(ids) > len(by_id)
            or not all(isinstance(i, str) and i in by_id for i in ids) or len(ids) != len(set(ids))):
        raise ValueError("选择与本轮条目不匹配。")
    # 新协议：逐项动作/目标盘必须落在后端白名单内，目标盘有效、空间充足。
    if "decisions" in payload:
        decisions = payload["decisions"]
        # decisionDigest 由服务端依据 decisions 生成并写入回执，前端无需也无法伪造。
        if not isinstance(decisions, list) or len(decisions) < len(ids):
            raise ValueError("决策条目无效。")
        by_decision = {d.get("id"): d for d in decisions if isinstance(d, dict)}
        if set(by_decision) != set(ids):
            raise ValueError("勾选与决策条目不一致。")
        if not s["test"]:
            cleanup.validate_decisions(plan, decisions)
    rows = {r["id"]: r for r in plan["found"]}
    chosen = [(Path(rows[i]["path"]), rows[i].get("name") or rows[i]["path"]) for i in ids]
    for n, (a, name_a) in enumerate(chosen):
        for b, name_b in chosen[n + 1:]:
            if a == b or a in b.parents or b in a.parents:
                raise ValueError(
                    f"选择中含有重叠目标：{name_a}（{a}）与 {name_b}（{b}）是同一路径或父子目录，"
                    "请返回页面只保留其中一项后重新提交。"
                )
    return ids


def wait_review(handle):
    with guard:
        s = session_for(handle, allow_submitted=True)
    if not s["event"].wait(15):
        session_for(handle, allow_submitted=True)
        return {"state": "等待用户提交", "next": "在同一对话再次调用 wait_review。"}
    with guard:
        s = session_for(handle, allow_submitted=True)
        plan = s["plan"]
        choice = {"runId": plan["runId"], "revision": plan["revision"], "ids": s["ids"],
                  "reviewDigest": s["digest"]}
        if s.get("decisions") is not None:
            choice["decisions"] = s["decisions"]
            choice["decisionDigest"] = s.get("decisionDigest")
        if s.get("receipt"):
            return s["receipt"]
        validate_selection(s, choice)
        rows = {r["id"]: r for r in plan["found"]}
        by_id = {p["id"]: p for p in plan["proposals"]}
        decision_map = {d["id"]: d for d in (s.get("decisions") or [])}
        selected = []
        for pid in s["ids"]:
            base = dict(by_id[pid])
            d = decision_map.get(pid)
            if d and d.get("action"):
                base["action"] = d["action"]
                if d.get("targetDisk"):
                    base["targetDisk"] = d["targetDisk"]
            base["disk"] = rows[pid]["disk"]
            base["path"] = rows[pid]["path"]
            selected.append(base)
        result = {"state": "已收到", "runId": plan["runId"], "selected": selected}
        if s["test"]:
            result["next"] = "在对话回复已收到只读项目，再调用 read_test_space(handle)，将实测结果回复用户。"
        else:
            directory = BASE / "runs" / plan["runId"]
            receipt = directory / ("choice-" + handle + ".json")
            cleanup.dump(receipt, choice)
            result.update(runFile=str(directory / "run.json"), choiceFile=str(receipt),
                          next="先在本对话回复收到的盘和动作，再按技能用这份 choiceFile 调用 apply 并复核。")
        s["received"] = True
        s["receipt"] = result
        return result


def read_test_space(handle):
    with guard:
        s = session_for(handle)
        if not s["test"] or not s["received"] or s["ids"] != ["read-space"]:
            raise ValueError("该只读测试尚未收到选择。")
        if "result" not in s:
            sample = cleanup.sample(s["plan"]["targets"])
            if any("free" not in row for row in sample["volumes"]) or not sample["volumes"]:
                raise ValueError("读取失败，请在原对话告知用户。")
            s["result"] = sample
        return {"state": "只读测试已完成", "sample": s["result"], "next": "在当前对话回复盘符、可用 GiB 和采样时间。"}


def wait_finish(handle):
    """执行与复核完成后，等待用户在页面点「结束本轮清理」，再返回成果报告路径。"""
    with guard:
        s = session_for(handle, allow_submitted=True)
        if s["test"] or not s["received"]:
            raise ValueError("该任务尚未进入执行阶段，无需等待结束确认。")
    if not s["finish"].wait(15):
        with guard:
            session_for(handle, allow_submitted=True)
        return {"state": "等待用户确认结束", "next": "在同一对话再次调用 wait_finish；用户尚未点结束本轮。"}
    with guard:
        state = read_run(s["plan"]["runId"])
        run_id = s["plan"]["runId"]
        directory = BASE / "runs" / run_id
        report_html = directory / cleanup.REPORT_HTML
        latest = BASE / cleanup.REPORT_HTML
        result = {"state": "本轮已结束", "runId": run_id,
                  "report": str(report_html if report_html.is_file() else latest),
                  "next": "把成果报告链接发给用户，并用实测结果简短总结后结束本轮对话。"}
        s["finished"] = True
        return result


def status_detail(s):
    """返回页面轮询用的阶段状态、剩余未选项与最新磁盘可用空间。"""
    data = {"phase": "awaiting_selection", "message": "等待勾选", "submitted": s["ids"] is not None,
            "ids": s["ids"] or [], "finished": False, "reviewed": False,
            "unselected": [], "volumes": None}
    if s.get("finished"):
        data.update(phase="finished", message="本轮已结束，成果报告已生成。", finished=True, reviewed=True)
        return data
    if "result" in s:
        r = s["result"]["volumes"][0]
        data.update(phase="done", message=f"测试完成 · {r['disk']} 盘可用 {r['free']/1024**3:.2f} GiB。",
                    finished=True, volumes=s["result"]["volumes"])
        return data
    if s["ids"] is not None and not s["received"]:
        data["message"] = "已提交，等待当前对话领取。"
    if s["ids"] is not None and not s["received"]:
        data["phase"] = "submitted"
    if s["received"] and not s["test"]:
        data["phase"] = "applying"
        data["message"] = "当前对话已领取选择，等待执行结果。"
        state = read_run(s["plan"]["runId"])
        deep = state.get("deep") or {}
        samples = [x for x in (deep.get("reviews") or []) + [deep.get("after"), state.get("created")] if x]
        if samples:
            data["volumes"] = samples[-1].get("volumes")
        if deep:
            if set(deep.get("selected", [])) != set(s["ids"]):
                data["message"] = "本轮已由另一份选择处理。请查看原对话。"
                return data
            chosen = set(deep["selected"])
            data["unselected"] = [{"id": p["id"], "name": p.get("recommendation", "")[:40], "disk": ""}
                                  for p in s["plan"]["proposals"] if p["id"] not in chosen]
            done = all(not str(e.get("status", "")).startswith("待 WorkBuddy") for e in deep["entries"])
            data["phase"] = "verified" if done else "awaiting_manual"
            data["message"] = "已复核完成，可结束本轮。" if done else "部分项目需在应用/系统设置中处理，可结束本轮并查看说明。"
            data["reviewed"] = done
            data["canFinish"] = True
    elif s["received"]:
        data["phase"] = "done"
    return data


def status(s):
    return status_detail(s)["message"]


class Page(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self, code, data, kind="application/json"):
        raw = (data if kind.startswith("text/html") else json.dumps(data, ensure_ascii=False)).encode()
        self.send_response(code)
        for key, val in {"Cache-Control":"no-store", "Referrer-Policy":"no-referrer",
                         "X-Content-Type-Options":"nosniff", "Content-Type":kind + "; charset=utf-8",
                         "Content-Length":str(len(raw))}.items():
            self.send_header(key, val)
        self.end_headers()
        self.wfile.write(raw)

    def target(self):
        host = f"127.0.0.1:{self.server.server_port}"
        if self.headers.get("Host") != host:
            raise ValueError("连接目标不匹配。")
        match = re.fullmatch(r"/([A-Za-z0-9_-]{32})/(status|submit|finish)?", self.path)
        if not match:
            raise ValueError("链接无效。")
        with guard:
            for handle, s in sessions.items():
                if secrets.compare_digest(s["view"], match[1]):
                    return session_for(handle), match[2]
        raise ValueError("本轮链接已失效。")

    def do_GET(self):
        try:
            s, route = self.target()
            if route == "status":
                with guard:
                    self.respond(200, status_detail(s))
            elif route is None:
                source = read_run(s["plan"]["runId"]) if s["received"] and not s["test"] else s["plan"]
                plan = {**source, "liveReview": True, "archived": False}
                data = json.dumps(plan, ensure_ascii=False).replace("<", "\\u003c")
                html = (BASE/"disk_cleanup_template.html").read_text(encoding="utf-8-sig")
                self.respond(200, html.replace("<!--RUN-DATA-->", data, 1), "text/html")
            else:
                self.respond(404, {})
        except (OSError, ValueError) as exc:
            self.respond(409, {"error": str(exc)})

    def do_POST(self):
        try:
            s, route = self.target()
            origin = f"http://127.0.0.1:{self.server.server_port}"
            kind = self.headers.get_content_type()
            if not (self.headers.get("Origin") == origin and kind == "application/json"):
                raise ValueError("请在本轮工作台提交。")
            if route == "finish":
                with guard:
                    if s["ids"] is None or not s["received"]:
                        raise ValueError("本轮尚未提交或执行，无法结束。")
                    if not s["finished"]:
                        s["finished"] = True
                        s["finish"].set()
                self.respond(200, {"state": "finishing"})
                return
            if route != "submit":
                raise ValueError("请在本轮工作台提交。")
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length <= 32768:
                raise ValueError("选择无效。")
            body = self.rfile.read(length)
            payload = json.loads(body)
            with guard:
                if not isinstance(payload, dict):
                    raise ValueError("选择无效。")
                ids = validate_selection(s, payload)
                if s["ids"] is not None and ids != s["ids"]:
                    raise ValueError("当前会话已有提交。")
                s["ids"] = ids
                decisions = payload.get("decisions")
                if decisions is not None:
                    s["decisions"] = decisions
                    s["decisionDigest"] = cleanup.decisions_digest(decisions)
                if not s["test"]:
                    # 提交即落盘，避免对话端 15 分钟内未领取导致选择丢失。
                    choice = {"runId": s["plan"]["runId"], "revision": s["plan"]["revision"],
                              "ids": ids, "reviewDigest": s["digest"]}
                    if decisions is not None:
                        choice["decisions"] = decisions
                        choice["decisionDigest"] = s["decisionDigest"]
                    cleanup.dump(choice_path(s["plan"]["runId"], s["handle"]), choice)
                s["event"].set()
            self.respond(200, {"state": "submitted"})
        except (OSError, ValueError, KeyError) as exc:
            self.respond(409, {"error": str(exc)})


def tool(name, description, properties, required=()):
    return {"name": name, "description": description, "inputSchema": {
        "type":"object", "properties":properties, "required":list(required), "additionalProperties":False}}


HANDLE = {"handle": {"type":"string"}}
TOOLS = [
    tool("open_review", "打开当前任务的磁盘选择页。test=true 只做无删除链路测试；正式任务传 run_id；同一轮重复打开默认返回原页面，recover=true 强制接管。返回页面链接与对话等候凭据。",
         {"run_id":{"type":"string"}, "test":{"type":"boolean"}, "recover":{"type":"boolean"}}),
    tool("wait_review", "在发出页面链接的原对话领取用户勾选的清单（含最终动作与目标盘）；等待超时继续调用。", HANDLE, ["handle"]),
    tool("read_test_space", "完成已经领取的只读磁盘测试并返回实测值。", HANDLE, ["handle"]),
    tool("wait_finish", "apply 和 verify 完成后调用，等待用户在页面点「结束本轮清理」，返回成果报告路径；等待超时继续调用。", HANDLE, ["handle"]),
]


def dispatch(request):
    method = request.get("method")
    params = request.get("params", {})
    if method == "initialize":
        supported = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
        return {"protocolVersion": params.get("protocolVersion") if params.get("protocolVersion") in supported else supported[0],
                "capabilities":{"tools":{}}, "serverInfo":{"name":"disk-cleanup-review", "version":"1.4.1"}}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        funcs = {"open_review":open_review, "wait_review":wait_review,
                 "read_test_space":read_test_space, "wait_finish":wait_finish}
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
