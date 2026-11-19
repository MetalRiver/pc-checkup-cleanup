"""一键安装 disk-cleanup 技能的 MCP 连接器配置。

运行方式：python install.py
它会用「运行本脚本的解释器」和「本脚本所在目录」自动填写配置，
因此换机器、换目录后直接重新运行即可，无需手改任何路径。

注意：本安装脚本本身跨平台，但清理引擎 cleanup.py 依赖 Win32 API，
整个技能仅支持 Windows 10 / 11。需 Python 3.10+。
"""
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BRIDGE = HERE / "review_bridge.py"
USER_WB = Path.home() / ".workbuddy"
MCP = USER_WB / "mcp.json"
KEY = "disk-cleanup-review"


def fail(message):
    print("[失败] " + message)
    sys.exit(1)


def main():
    if not BRIDGE.is_file():
        fail(f"找不到桥接脚本：{BRIDGE}\n请确保 install.py 与 review_bridge.py 在同一目录。")

    python = Path(sys.executable).resolve()
    if not python.is_file():
        fail(f"当前 Python 解释器路径无效：{python}")

    USER_WB.mkdir(parents=True, exist_ok=True)
    config = {}
    if MCP.is_file():
        try:
            config = json.loads(MCP.read_text(encoding="utf-8-sig"))
            if not isinstance(config, dict):
                config = {}
        except (OSError, ValueError) as exc:
            fail(f"无法解析现有 {MCP}：{exc}\n请手动检查该文件后重试。")

    servers = config.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        servers = config["mcpServers"] = {}

    # 只覆盖本技能自己的条目，保留用户已有的其他连接器。
    servers[KEY] = {
        "command": str(python),
        "args": ["-B", str(BRIDGE)],
        "type": "stdio",
    }

    temporary = MCP.with_name(MCP.name + ".tmp")
    try:
        temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, MCP)
    finally:
        temporary.unlink(missing_ok=True)

    print("[完成] 已写入连接器配置：" + str(MCP))
    print("  · 解释器：" + str(python))
    print("  · 桥接脚本：" + str(BRIDGE))
    print()
    print("接下来请按以下步骤启用：")
    print("  1. 完全退出并重启 WorkBuddy（MCP 配置改动需重启才会加载）。")
    print("  2. 打开左下角连接器管理 → 右上角「自定义连接器」入口。")
    print(f"     找到 {KEY}，点「信任」以启用。")
    print("  3. 在新对话里说「测试工作台回传」验证；若选择页能打开并回传可用空间即成功。")
    print("  4. 之后即可正常使用「清理电脑」等磁盘清理功能。")


if __name__ == "__main__":
    main()
