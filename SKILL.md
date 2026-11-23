---
name: disk-space-checkup-cleanup
version: 1.5.4
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

1. 把技能包**直接解压到用户级 skills 目录**：`%USERPROFILE%\.workbuddy\skills\`。压缩包已自带一层 `disk-space-checkup-cleanup\`，解压后应得到 `%USERPROFILE%\.workbuddy\skills\disk-space-checkup-cleanup\`，并确保 `SKILL.md`、`install.py`、`cleanup.py`、`review_bridge.py`、`disk_cleanup_template.html` 都直接在这个 `disk-space-checkup-cleanup` 目录里，不要变成 `disk-space-checkup-cleanup\disk-space-checkup-cleanup\...`。
2. 运行安装脚本：`python <技能目录>\install.py`。它会用**当前解释器**和**脚本实际所在目录**自动生成用户级 `%USERPROFILE%\.workbuddy\mcp.json`，无需手改任何绝对路径；换机器或换目录后重跑即可。已有其他连接器会保留。
3. 完全退出并重启 WorkBuddy。
4. 左下角连接器管理 → 右上角「自定义连接器」→ 找到 `disk-cleanup-review` → 点「信任」启用。
5. 验证：新开对话说「测试工作台回传」，能打开选择页并回传可用空间即成功。

若用户只是问“能不能清理/看看空间”而未安装，先按上面检查连接器状态；未配置就明确告知需要先执行第 1–4 步，不要直接调用 open_review。

## 运行环境

在本技能目录（与 `SKILL.md` 同层）运行脚本：这里有 `cleanup.py`、`review_bridge.py`、`disk_cleanup_template.html`，项目直接运行时写入项目的 `runs/`；用户级安装写入 `%USERPROFILE%\.workbuddy\disk-cleanup-data\runs`，旧位置的记录仍可读取。使用 Python 3.10+（以下用 python 表示）。

## 成果报告

每次任务收尾生成 SVG 报告与 HTML。今日累计按本机自然日统计同一数据目录内基础清理和各批复核的前后读数差，跨天从零重新累计。跨午夜的一批计入复核当天。报告另列本次任务净变化、各盘可用空间、各批回执和剩余项。

## 识别与选择

1. 运行 `python cleanup.py drives`，给出本机固定盘的容量、可用空间和紧张程度。用户只要求看看空间时，运行 `python cleanup.py scan --drives C D`，只做排查。
2. 用户要求清理时，给一次选择 **基础清理 · 自动完成** 或 **深度清理 · 基础项自动处理，需要您决定的项目在页面里选择**。明确指定模式时直接用指定模式。用户点名盘符时，将该盘作为清理目标；未点名则使用列出的可访问固定盘。把实际盘符传给脚本。
3. 基础运行 `python cleanup.py basic --drives C D`。脚本按已识别的可重建缓存规则筛选文件、跳过占用和变更对象并复核。用 `basic.before` 和 `basic.after` 按盘汇报实测变化。选模式后无需逐项确认、无需打开网页。

## 只读回传测试

收到“测试工作台回传”时，直接调用项目 MCP 的 open_review(test=true)。使用 WorkBuddy 内置浏览器工具在右侧栏导航到返回的 page，再立即调用 wait_review(handle)。若返回等待用户提交，继续用同一 handle 等待，直到收到、取消或超时。领取后先在对话中回复所选只读项，再调用 read_test_space(handle)，将盘符、GiB 和时间回复用户。本测试只使用工作台只读工具和 WorkBuddy 内置浏览器导航。如果 MCP 尚未连接，明确告知用户连接状态，不得在测试中调用 basic、deep 或 apply。

收到「测试分批清理」时，运行同一套源码的 `python -X utf8 cleanup.py demo`，确认返回 `simulation: true`，再用返回的 `runId` 从下文第 4 步开始。只接受这个演示轮次的 `runFile` 和 MCP 回执。演示的 `apply` 和 `verify` 使用虚拟数据，跳过实际应用和系统操作。请用户先选演示缓存 A，复核后在原对话选择继续，再选 B，最后选择结束。报告应含两批完成的 A、B 和未处理的 C。

## 深度清理

「应用盘点」可点击「打开 Windows 已安装应用」自行管理软件。能确认归属的 Chrome、Edge 和 Windows 临时文件清理项有打开对应设置的按钮。按钮只发起程序打开，处理结果仍以用户在应用内的实际操作和后续复核为准。

验收项目源码时使用项目级 MCP `disk-cleanup-review-project`。扫描前确认其 `batch_status` 和 `batch_decision` 已在当前对话中加载。若只看到用户级连接器，说明版本和连接器状态，由用户启用项目级连接器。

1. 运行 `python cleanup.py deep --drives C D`。**默认 `--seconds 0` 即不限时完整遍历每个目标盘**，扫出所有顶层目录、根目录大文件、回收站，基础项自动执行。全盘扫描在文件多的机器上可能耗时数分钟，需耐心等待、不要中途停止；无权读取或未扫完的条目会标注。用户明确要求快速看看时可用 `scan` 或 `--seconds 30` 限制预算，但那属于抽样、不代表全盘。只读查看其它盘用 `scan`，收益按盘报告。

   **深度下钻已内置**：脚本会对**用户选中的每一个盘**（不只最紧张盘）的每个用户账户自动下钻 `AppData\Local`、`AppData\Roaming`、`AppData\LocalLow`、用户主目录（下载/文档/桌面/微信文件/QQ 文件/.workbuddy 等）以及 `Program Files`、`Program Files (x86)`、`ProgramData`，并对所有盘的顶层大目录再下钻一层，逐应用识别，不再只停留在顶层目录。识别结果会写入 `run.json` 的 `compositions`（各盘占用构成），在页面顶部"空间去哪了"区块按「系统与保留 / 应用与游戏 / 用户文档 / 应用缓存与临时 / 其他」展示各占多少和代表目录——用它直接回答"C 盘一百多 G 到底去哪了"。当可迁移/可清理项合计明显小于磁盘已用空间时，要如实说明差额来自系统保留、被占用或权限受限目录，不要声称已经查全。
2. 深度扫描把自动建议写入 `proposals`，只适合检查的目录保留在「全部发现」。系统、安装软件和应用管理的完整数据目录只展示占用或建议用相应管理入口处理。Chrome、Edge 和常见应用中识别出的缓存达到 64 MiB 后可作为应用内清理建议。基础自动清理沿用已有规则。对证据不足的项目可用 `drill` 下钻、用 `recommend` 补充有证据的管理入口，保留原始扫描结果。「应用盘点」展示 Windows 登记的桌面软件，包括缺少图标路径或启动记录的应用。运行中的进程和近期记录可用于建议保留；旧 Explorer 启动记录只作参考，近期使用仍可能未知。它不生成卸载决定，只有另外核实了实际用途并由用户明确选择，才在 Windows 已安装应用中处理。
3. **应用数据目录的迁移提示是硬性要求**：若迁移对象属于已知应用（页面已标"迁移后需在应用设置里改存储位置"，impact 里写明应用名），在向用户说明时必须强调——搬迁完成后要到该应用的设置里修改存储/安装位置（如 WeGame/Steam 的存储位置、微信的文件管理路径），否则应用可能找不到数据。纯用户数据（下载、文档、视频）一般无需改设置。区分可自动处理与必须用户取舍的对象，核不清的如实标注，不臆测。
4. 有深度决策项时，调用 MCP 的 `open_review(run_id=本轮编号)`。用 WorkBuddy 右侧内置浏览器工具导航到返回的 `page`。若内置导航不可用，发可点击的链接并提示用右侧内置浏览器打开。保留链接后立即用私有 `handle` 调用 `wait_review`。每次工具返回「等待用户提交」就继续调用。发出链接后仍在这一轮工具执行中，只有领取、用户取消或过期才结束等待。它单次最多等 15 秒，工作台默认可提交 4 小时，用户可在页面点「延长 2 小时」。收到「已过期」才表示本次提交窗口结束。若连接中断或对话已结束，继续原任务时用原 `run_id` 重新打开，已保存的回执会按轮次和方案版本恢复。只有确定要更换未提交的工作台时才传 `recover=true`。页面显示「选择已保存」后，仍需拿到 `wait_review` 的领取结果。收到「已有执行记录」时继续复核。恢复历史回执后先核对任务是否尝试执行过，已有中断或部分操作应先核对执行结果和现场，再决定如何继续。

   尚未提交时，工作台右上角有「本轮不清理」。用户也可以在原对话说同样的话；先用 `batch_status` 获取当前版本，再调用 `batch_decision(run_id, revision, decision="skip")`。如果 `wait_review` 返回「本轮已结束」，直接交付汇总报告。已有基础清理和已完成批次计入报告。

   **工作台里的用户决策**：页面对每条建议展示「你的决定」下拉，用户可在脚本给定的白名单内改为搬迁 / 应用内清理 / 直接清理 / 跳过（「直接清理」仅对部分可搬迁项开放，默认不选中，选中后页面显示不可恢复警示、提交时二次确认）；选搬迁时还能指定目标盘（下拉只列非源盘），或用批量工具条按当前分类 / 已勾选 / 全部批量设置动作与目标盘。AI 只做预分类和推荐，最终动作以用户决策为准。不勾选即本轮跳过。收到选择后，先在同一对话逐项说明盘符、对象和**用户最终动作与目标盘**，再用返回的 runFile 和 choiceFile 执行 `python cleanup.py apply --run <runFile> --input <choiceFile>`。choiceFile 里的 decisions 已含用户最终动作与目标盘，并带 decisionDigest 防篡改；不要自行重建或增补勾选清单。交互中断且未收到时不执行。

apply 现在会**真实执行三类动作**：回收站按指纹校验后清空；`migrate` 迁移会用 robocopy /MOVE 把勾选的目录真正搬到目标盘并删除源（带同盘/重叠/空间不足校验，失败会标注"未确认完成"不强行继续）；`delete` 直接清理（永久删除）仅在用户主动选择、且 `_can_delete` 白名单通过、且回执带 `confirmDelete=true` 时执行，用 `shutil.rmtree` 删除后校验目录是否真的消失。**`delete` 严格受限**：只对 migrate-app / migrate-user 目录开放，盘根、Windows、Users、Program Files、ProgramData、回收站、系统标准用户目录、`$` 系统项、当前工作区自身及其父目录一律拒绝；父目录可删时子目录删除权限自动收回。执行后在对话里逐项回报每个迁移的源→目标、成功与否，并按要求提醒应用数据需在应用后台改存储位置。系统项（虚拟内存、休眠、组件存储）脚本不改动配置，只标记"待 WorkBuddy 打开指定入口"，由你按 target 里的官方入口操作后回填：在应用或系统设置项上，复核实际配置，已生效或已配置待重启的项目只记录和回报。
5. 对 WorkBuddy 接续处理的项目，写入形如 `[{"id":"本批条目 ID","status":"完成","note":"已验证的结果"}]` 的 JSON，可用状态为完成、部分完成、跳过、未完成。运行 `python cleanup.py verify --run <runFile> --input <复核回填.json>`。无人工动作回填时省略 `--input`。保留命令的原始退出状态。执行受宿主确认拦截、命令失败或尚无执行记录时，在原对话准确说明当前状态，先核对结果再继续。
6. **每批复核后直接在原对话询问用户**。调用 `batch_status(run_id)`，当 `phase=ready` 时汇报本批结果和剩余项数量，再用 WorkBuddy 原生选项询问「继续处理剩余项」或「结束本次任务」。当前入口若提供文本回复，请用户回复对应选项。没有剩余可选项时只给「结束本次任务」。用户选择前保持在确认阶段。
   - 用户选「继续处理剩余项」时，用上一步的 `runId` 与 `revision` 调用 `batch_decision(run_id, revision, decision="continue")`。用 WorkBuddy 内置浏览器打开返回的下一批链接，再调用 `wait_review`。下一批只开放尚未提交且与已处理路径不冲突的项目，沿用同一任务编号。用户也可在复核后的工作台点击「继续处理剩余项」。回到原对话时用 `batch_status` 找到当前批次，再打开当前工作台并领取。
   - 用户选「结束本次任务」时，调用 `batch_decision(run_id, revision, decision="finish")`。在原对话展示 `reportImage` 指向的 SVG 报告图，附上 `report` 的 HTML 链接，并简述全任务结果。报告汇总截至最后一批的结果、实测空间差和剩余未处理项。若用户在工作台点了结束，既有的 `wait_finish` 和 `batch_status` 也可领取同一份报告。
7. 对话中断后，先用 `batch_status` 查原任务。已结束则交付原报告，已复核则重新给出选项，待复核则继续复核；正在等待选择时用 `open_review` 恢复当前批次。历史任务的执行结果须先核对，再决定下一步。单独重出报告可运行 `python cleanup.py report --run <runFile>`。基础清理依旧生成只含基础项的同类报告。

结果使用任务开始和最后一批复核后的可用空间差，逐盘计算。技能目录的 `disk_cleanup_decision.html` 展示最近发布的排查、深度建议或复核结果。每次任务另保留 `runs/<轮次>/` 下的独立快照及各批次记录，演示报告保存在自己的轮次里。
