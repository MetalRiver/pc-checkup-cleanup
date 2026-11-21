---
name: disk-cleanup
version: 1.4.4
display_name: 全自动化系统电脑体检与清理
author: 途有引力
license: MIT
display_name_en: Fully Automated System PC Checkup and Cleanup
description: 在 Windows 上识别本机磁盘、排查空间占用、执行基础清理或生成可选择的深度清理 HTML，并复核各盘可用空间。用于“清理电脑”“C 盘快满了”“看看空间去哪了”等请求。
description_zh: 在 Windows 上识别本机磁盘、排查空间占用、执行基础清理，或生成可勾选的深度清理页面，只执行用户勾选的项目并复核各盘可用空间。适用于“清理电脑”“C 盘快满了”“看看空间去哪了”等请求。仅支持 Windows 10/11。
description_en: Identify local disks on Windows, analyze space usage, run basic cleanup, or generate an interactive deep-cleanup page that only applies user-selected items and verifies free space on each drive. Use for requests about cleaning up the PC, a full C drive, or finding where disk space went. Windows 10/11 only.
---

# 磁盘清理

## 首次安装（含连接器配置）

本技能的深度交互页依赖一个项目 MCP 连接器 `disk-cleanup-review`。**安装技能不会自动配置它，必须按下述步骤做完一次**；否则只能做只读扫描和基础清理，无法打开勾选页。

> 面向用户的完整安装说明见技能包内 `README.md`（含 Python 依赖、截图式步骤、验证与卸载）。本技能仅支持 Windows 10/11——清理引擎 `cleanup.py` 依赖 Win32 API。

1. 把技能包**直接解压到用户级 skills 目录**：`%USERPROFILE%\.workbuddy\skills\`。压缩包已自带一层 `disk-cleanup\`，解压后应得到 `%USERPROFILE%\.workbuddy\skills\disk-cleanup\`，并确保 `SKILL.md`、`install.py`、`cleanup.py`、`review_bridge.py`、`disk_cleanup_template.html` 都直接在这个 `disk-cleanup` 目录里，不要变成 `disk-cleanup\disk-cleanup\...`。
2. 运行安装脚本：`python <技能目录>\install.py`。它会用**当前解释器**和**脚本实际所在目录**自动生成用户级 `%USERPROFILE%\.workbuddy\mcp.json`，无需手改任何绝对路径；换机器或换目录后重跑即可。已有其他连接器会保留。
3. 完全退出并重启 WorkBuddy。
4. 左下角连接器管理 → 右上角「自定义连接器」→ 找到 `disk-cleanup-review` → 点「信任」启用。
5. 验证：新开对话说「测试工作台回传」，能打开选择页并回传可用空间即成功。

若用户只是问“能不能清理/看看空间”而未安装，先按上面检查连接器状态；未配置就明确告知需要先执行第 1–4 步，不要直接调用 open_review。

## 运行环境

在本技能目录（与 `SKILL.md` 同层）运行脚本：这里有 `cleanup.py`、`review_bridge.py`、`disk_cleanup_template.html`，各轮数据写入本目录的 `runs/`。使用 Python 3.10+（以下用 python 表示）。

## 成果报告

每轮收尾都会产出同一套横版报告 `清理成果报告.svg`（1600×940，另有同名 `.html` 外壳可直接打开或分享），固定包含五块：累计空出、本轮释放、各盘起始→当前、本轮回执、本轮未处理。累计口径以技能目录下 `baseline.json` 记录的最早一次扫描为基线；该文件在首次扫描时自动写入，不要手工改写，也不要随安装包分发。

## 识别与选择

1. 运行 `python cleanup.py drives`，给出本机固定盘的容量、可用空间和紧张程度。用户只要求看看空间时，运行 `python cleanup.py scan --drives C D`，只做排查。
2. 用户要求清理时，给一次选择 **基础清理 · 自动完成** 或 **深度清理 · 基础项自动处理，需要您决定的项目在页面里选择**。明确指定模式时直接用指定模式。用户点名盘符时，将该盘作为清理目标；未点名则使用列出的可访问固定盘。把实际盘符传给脚本。
3. 基础运行 `python cleanup.py basic --drives C D`。脚本按已识别的可重建缓存规则筛选文件、跳过占用和变更对象并复核。用 `basic.before` 和 `basic.after` 按盘汇报实测变化。选模式后无需逐项确认、无需打开网页。

## 只读回传测试

收到“测试工作台回传”时，直接调用项目 MCP 的 open_review(test=true)。立即在当前对话展示返回的可点击 page 链接，再调用 wait_review(handle)。若返回等待用户提交，继续用同一 handle 等待，直到收到、取消或超时。领取后先在对话中回复所选只读项，再调用 read_test_space(handle)，将盘符、GiB 和时间回复用户。本测试仅调用这三个工具。如果 MCP 尚未连接，明确告知用户连接状态，不得在测试中调用 basic、deep 或 apply。

## 深度清理

1. 运行 `python cleanup.py deep --drives C D`。**默认 `--seconds 0` 即不限时完整遍历每个目标盘**，扫出所有顶层目录、根目录大文件、回收站，基础项自动执行。全盘扫描在文件多的机器上可能耗时数分钟，需耐心等待、不要中途停止；无权读取或未扫完的条目会标注。用户明确要求快速看看时可用 `scan` 或 `--seconds 30` 限制预算，但那属于抽样、不代表全盘。只读查看其它盘用 `scan`，收益按盘报告。

   **深度下钻已内置**：脚本会对**用户选中的每一个盘**（不只最紧张盘）的每个用户账户自动下钻 `AppData\Local`、`AppData\Roaming`、`AppData\LocalLow`、用户主目录（下载/文档/桌面/微信文件/QQ 文件/.workbuddy 等）以及 `Program Files`、`Program Files (x86)`、`ProgramData`，并对所有盘的顶层大目录再下钻一层，逐应用识别，不再只停留在顶层目录。识别结果会写入 `run.json` 的 `composition`（占用构成），在页面顶部"空间去哪了"区块按「系统与保留 / 应用与游戏 / 用户文档 / 应用缓存与临时 / 其他」展示各占多少和代表目录——用它直接回答"C 盘一百多 G 到底去哪了"。当可迁移/可清理项合计明显小于磁盘已用空间时，要如实说明差额来自系统保留、被占用或权限受限目录，不要声称已经查全。
2. **建议已由脚本自动生成，不要留空页面。** deep 完成时脚本已识别并写入 `run.json` 的 `proposals`：把各盘用户数据/应用/游戏目录迁往空闲盘（排除源盘后优先非系统盘；≥1 GiB 才进入建议，更小的项仍在「全部发现」展示）（含 `targetPath`、`targetDisk`，已覆盖微信、QQ、WorkBuddy、Steam、浏览器、JetBrains、开发工具等常见应用，迁移后需在应用内改存储位置）、应用缓存与临时目录（`action=app-clean`，**只提示用应用内清理，脚本绝不自动删除**）、虚拟内存与休眠文件、组件存储、回收站。读取本轮 `run.json`，逐条核对建议的合理性与目标盘剩余空间；对证据不足的大项可再用 `python cleanup.py drill --run <run.json> --id <目录ID>`（默认全量细分）核实应用归属，再用 `python cleanup.py recommend --run <run.json> --input <建议.json>` 补充 uninstall / app-clean / system 等建议（字段 `id`、`action`、`recommendation`、`impact`、`target`）。不要重复脚本已生成的条目。脚本生成的迁移建议带 `auto:true`，页面会标"自动识别"。
3. **应用数据目录的迁移提示是硬性要求**：若迁移对象属于已知应用（页面已标"迁移后需在应用设置里改存储位置"，impact 里写明应用名），在向用户说明时必须强调——搬迁完成后要到该应用的设置里修改存储/安装位置（如 WeGame/Steam 的存储位置、微信的文件管理路径），否则应用可能找不到数据。纯用户数据（下载、文档、视频）一般无需改设置。区分可自动处理与必须用户取舍的对象，核不清的如实标注，不臆测。
4. 有深度决策项时，调用项目 MCP 的 open_review(run_id=本轮编号)，立即在原对话给出可点击的 page 链接，并调用 wait_review(handle)。每次返回等待状态时继续调用，保持当前任务；用户取消或超时后告知。handle 为任务私有参数，仅向用户展示 page。同一 runId 重复调用 open_review 会幂等返回原页面（标记 recovered）；只有明确需要强制接管时才传 `recover=true`，旧的 handle 会立即失效。只要用户正在使用已有的有效深度轮次，就复用该轮并核实状态，不重复执行基础清理。

   **工作台里的用户决策**：页面对每条建议展示「你的决定」下拉，用户可在脚本给定的白名单内改为搬迁 / 应用内清理 / 直接清理 / 跳过（「直接清理」仅对部分可搬迁项开放，默认不选中，选中后页面显示不可恢复警示、提交时二次确认）；选搬迁时还能指定目标盘（下拉只列非源盘），或用批量工具条按当前分类 / 已勾选 / 全部批量设置动作与目标盘。AI 只做预分类和推荐，最终动作以用户决策为准。不勾选即本轮跳过。收到选择后，先在同一对话逐项说明盘符、对象和**用户最终动作与目标盘**，再用返回的 runFile 和 choiceFile 执行 `python cleanup.py apply --run <runFile> --input <choiceFile>`。choiceFile 里的 decisions 已含用户最终动作与目标盘，并带 decisionDigest 防篡改；不要自行重建或增补勾选清单。交互中断且未收到时不执行。

apply 现在会**真实执行三类动作**：回收站按指纹校验后清空；`migrate` 迁移会用 robocopy /MOVE 把勾选的目录真正搬到目标盘并删除源（带同盘/重叠/空间不足校验，失败会标注"未确认完成"不强行继续）；`delete` 直接清理（永久删除）仅在用户主动选择、且 `_can_delete` 白名单通过、且回执带 `confirmDelete=true` 时执行，用 `shutil.rmtree` 删除后校验目录是否真的消失。**`delete` 严格受限**：只对 migrate-app / migrate-user 目录开放，盘根、Windows、Users、Program Files、ProgramData、回收站、系统标准用户目录、`$` 系统项、当前工作区自身及其父目录一律拒绝；父目录可删时子目录删除权限自动收回。执行后在对话里逐项回报每个迁移的源→目标、成功与否，并按要求提醒应用数据需在应用后台改存储位置。系统项（虚拟内存、休眠、组件存储）脚本不改动配置，只标记"待 WorkBuddy 打开指定入口"，由你按 target 里的官方入口操作后回填：在应用或系统设置项上，复核实际配置，已生效或已配置待重启的项目只记录和回报。
5. 对 WorkBuddy 接续处理的项目，写入形如 `[{"id":"本轮条目 ID","status":"完成","note":"已验证的结果"}]` 的 JSON，可用状态为完成、部分完成、跳过、未完成。运行 `python cleanup.py verify --run <run.json> --input <复核回填.json>` 重新读取磁盘。有新采样、无人工动作回填时可省略 `--input`。**verify 结束时会自动生成本轮横版成果报告**（`runs/<轮次>/清理成果报告.svg` 与 `.html`，并复制到技能目录根作为最新一份），命令会打印报告路径。**不要把「提交选择」或「verify 完成」当成对话结束**——此时工作台仍在线，正显示执行进度与最新磁盘空间。
6. **等待用户确认结束**：verify 后调用项目 MCP 的 `wait_finish(handle)`。工作台在复核完成后出现「结束本轮清理并生成报告」按钮；用户点击后页面保持在线并通知 `wait_finish` 返回报告路径。未点时 wait_finish 会返回等待状态，继续用同一 handle 调用即可，不要提前结束对话。收到「本轮已结束」后，把返回的成果报告 HTML 链接与简短实测结果发给用户，此时才是本轮的真正结束。若用户长时间不点结束，如实说明报告已生成、可随时在技能目录打开。
7. 需要单独重出报告时运行 `python cleanup.py report --run <run.json>`（只读生成，不改变本轮状态）。基础清理轮次（`basic`）结束时会自动生成只含基础项的同类报告。

结果使用本轮执行前和复核后的可用空间差，逐盘计算。技能目录的 `disk_cleanup_decision.html` 直接展示最近发布的排查、深度建议或复核结果。每轮另保留 `runs/<轮次>/` 下的独立快照。基础清理的结果在对话里汇报，本轮数字也保存在任务记录里。
