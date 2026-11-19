# 全自动化系统电脑体检与清理

> PC Checkup & Cleanup · 一个给 WorkBuddy 用的 Windows 磁盘体检与清理技能
>
> 开源协议 MIT · 仅支持 Windows 10 / 11
>
> 仓库地址：https://github.com/MetalRiver/pc-checkup-cleanup

装好后，你只要对 WorkBuddy 说「清理电脑」「C 盘快满了」「看看空间去哪了」，它就会扫描磁盘、列出可清理项、在网页里让你勾选，然后只执行你勾选的部分，并复核各盘的实际变化。

**它能做什么**

- 全盘体检：识别各盘总量与剩余，按目录逐层下钻，找出真正占空间的东西；
- 分类建议：清理 / 搬迁 / 卸载 / 系统调整，每项标注用途、风险与预计释放量；
- 由你决定：网页里逐项勾选，或按分类批量设置，未勾选的一律不动；
- 真迁移：把大目录从紧张盘搬到空闲盘，而不是只给建议；
- 实测复核：执行后重新读取各盘可用空间，给出真实变化。

深度体检采用**全盘完整扫描**（不做时间截断，文件多的盘可能要几分钟），并**自动识别**可从紧张盘迁往空白盘的用户数据和游戏目录、虚拟内存/休眠文件等系统项，生成带"自动识别"标记的建议。勾选迁移后，脚本会用 robocopy 真正把文件搬到目标盘并删除源文件。

> **只支持 Windows 10 / 11**（清理引擎依赖 Windows 系统接口，macOS / Linux 无法使用）。

---

## 一、安装（一次性，约 3 分钟）

### 第 1 步：安装 Python

确认电脑能运行 Python（3.10 或更高）。打开「命令提示符」或「PowerShell」，输入：

```
python --version
```

- 能显示版本号（如 `Python 3.12.x`）→ 继续第 2 步。
- 提示"不是内部或外部命令"→ 先去 https://www.python.org/downloads/ 下载安装，**安装时务必勾选 "Add python.exe to PATH"**，装完重新打开命令行再试。
- 不想单独装：WorkBuddy 自带 Python 也可以，直接在 WorkBuddy 的对话里让它帮你运行安装脚本即可。

### 第 2 步：解压技能包

把 `disk-cleanup.zip` **直接解压到用户级 skills 目录**：

```
%USERPROFILE%\.workbuddy\skills\
```

（`%USERPROFILE%` 就是你的用户主目录，在资源管理器地址栏输入 `%USERPROFILE%\.workbuddy\skills` 回车可直达；如果 `skills` 文件夹不存在就新建一个。）

压缩包里已经带了一层 `disk-cleanup\` 目录。解压完成后应该是这样：

```
%USERPROFILE%\.workbuddy\skills\
  └─ disk-cleanup\
       ├─ SKILL.md
       ├─ README.md
       ├─ install.py
       ├─ cleanup.py
       ├─ review_bridge.py
       ├─ disk_cleanup_template.html
       └─ mcp.template.json
```

也就是说，进入 `skills\disk-cleanup\` 后应该**直接看到上面这些文件**。如果你解压后变成了 `skills\disk-cleanup\disk-cleanup\...`，说明解压目标选错了，把内层的 `disk-cleanup` 文件夹内容移到上一层即可。

### 第 3 步：运行安装脚本

在命令行进入上面的目录，运行：

```
python install.py
```

它会**自动**把当前 Python 路径和桥接脚本路径写进 WorkBuddy 的连接器配置（`%USERPROFILE%\.workbuddy\mcp.json`），不用你手改任何路径。你已经装的其他连接器不会被覆盖。看到 `[完成]` 即成功。

> 换电脑、换目录、或者以后挪动了位置，重新运行一次 `python install.py` 即可自动修正。

### 第 4 步：重启并信任连接器

1. **完全退出并重启 WorkBuddy**（配置改动要重启才会加载）。
2. 打开左下角「连接器管理」→ 右上角「自定义连接器」入口 → 找到 `disk-cleanup-review` → 点「**信任**」启用。

### 第 5 步：验证

新开一个对话，对 WorkBuddy 说：

```
测试工作台回传
```

如果它能打开一个选择页、并把你机器的可用空间回填出来，就装好了。这一步是只读的，不会删任何东西。

---

## 二、怎么用

安装完成后，正常跟 WorkBuddy 对话即可：

- 「**清理电脑**」→ 全盘扫描 + 基础清理（临时缓存等），并把需要你决定的大项放到网页里勾选。
- 「**看看 C 盘空间去哪了**」→ 只扫描、不删除，给出占用排行。
- 网页里勾选后**只有你选中的项会被执行**；涉及游戏卸载、虚拟内存等改动会明确标注影响，由你决定。

**关于"迁移"建议**（页面标有"自动识别"）：当某个盘紧张、另一个盘空闲时，技能会列出可搬过去的大目录，勾选后由脚本用 robocopy 真正搬移并删除源文件，全程校验、不会搬同盘或空间不足的目标。注意——

- 如果目录旁标了橙色"**迁移后需在应用设置里改存储位置**"（如游戏、微信等），搬完后**必须到该应用的设置里更改存储/安装位置**（例如 WeGame/Steam 的存储位置、微信的文件管理路径），否则应用可能找不到数据。
- 单纯的下载、文档、视频等用户数据搬迁一般无需改设置。

每项执行后都会重新读取各盘可用空间，告诉你实际回收了多少。

---

## 三、安全说明

- 技能只处理可重建的缓存、回收站，以及你**明确勾选**的项目；未勾选的一律不动。
- 回收站内容如果在扫描后发生变化，会被安全校验拦下、拒绝自动清空，不会误删你后来放进去的文件。遇到这种情况，重新扫描一次再清理即可。
- 扫描产生的记录和每轮快照保存在技能目录的 `runs\` 下，不上传、不外发。

---

## 四、卸载

1. 删除 `%USERPROFILE%\.workbuddy\skills\disk-cleanup\` 整个文件夹。
2. 编辑 `%USERPROFILE%\.workbuddy\mcp.json`，删掉 `mcpServers` 里的 `disk-cleanup-review` 一项（其他项保留）。
3. 重启 WorkBuddy。

---

## 五、常见问题

**Q：安装脚本提示找不到 Python？**
A：先按第 1 步装 Python 并勾选加入 PATH；或在 WorkBuddy 对话里让它用自带 Python 运行 `install.py`。

**Q：重启后连接器列表里没有 `disk-cleanup-review`？**
A：确认第 3 步看到 `[完成]`，且配置文件位置是 `%USERPROFILE%\.workbuddy\mcp.json`（不是项目目录下的 `.workbuddy`）。改完必须完全重启 WorkBuddy。

**Q：点「信任」后还是打不开选择页？**
A：在对话里说「测试工作台回传」，把报错原文发给 WorkBuddy，它会根据提示判断是连接器还是路径问题。

**Q：Mac 能用吗？**
A：不能。清理引擎依赖 Windows 系统接口，仅支持 Windows 10 / 11。

---

## 六、关于作者

这个技能是「**途有引力**」在做的一堆本地工具之一：原则是**数据留在自己机器上、不联网、不上传、不埋点**。

- 微信公众号：**途有引力**（工具更新、使用技巧、踩坑记录都发在这里）
- 同作者其他开源项目：[cabinet-drawer](https://github.com/MetalRiver/cabinet-drawer) · [pathorbit-board](https://github.com/MetalRiver/pathorbit-board)
- 问题反馈：[提交 Issue](https://github.com/MetalRiver/pc-checkup-cleanup/issues)

> 二维码：把公众号二维码图片放到仓库 `assets/wechat-qr.png`，README 顶部即可展示（当前为文字版，未放图）。

---

## 七、免责声明

本工具会**真实删除或移动文件**，请在执行前自行确认勾选内容：

- 所有清理与迁移动作都需要你在工作台页面明确勾选后才会执行，脚本不会自行决定删除你的文件；
- 迁移应用或游戏目录后，可能需要到对应软件的设置里更改存储位置，否则应用可能找不到数据；
- 请务必保留系统还原点或重要数据备份。作者不对因使用本工具造成的数据丢失、系统异常承担责任；
- 本工具不会上传任何扫描结果，`runs\` 目录下的记录仅保存在你本机。

---

## 八、许可证

本项目基于 [MIT License](LICENSE) 开源，可自由使用、修改与分发，请保留版权声明。

更新记录见 [CHANGELOG.md](CHANGELOG.md)。

