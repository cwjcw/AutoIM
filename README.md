# AutoIM

AutoIM 是面向 Windows 的桌面 IM 自动化能力验证工具。当前阶段仅支持**企业微信 Windows 客户端**，提供可见未读巡检、本地 SQLite、登录账号核验及独立的开发诊断工具。

仓库新增独立的 `android-agent/`：AutoIM Android Agent 0.1.3 离线消息解析与检测。Redmi Note 11R 已完成 P-A1.1 的正文、标题、左右消息、输入框和发送按钮验证；P-A1.2 已实现普通一对一文本解析、方向、有序新消息检测、重复过滤与安全重同步。0.1.3 修复标题组、主标题和身份/状态装饰解析，仍为 IN_PROGRESS，等待 0.1.3 真机验证。保留原结构诊断、快照搜索与完整树。Android Agent 不改变 Windows Agent 代码，也不含网络、自动点击或自动发送能力。详见 [android-agent/README.md](android-agent/README.md)。

Windows **PC-V1.3 可见消息读取**和 **PC-V2.2 文件识别与诊断复制**已获用户真机确认。**PC-V2.3 本地持久化**、**PC-V3.0 多会话采集**及 **PC-V3.1 登录账号识别与采集反馈**均已实现，真实企业微信端到端验收为 `PENDING_DEVICE_VERIFICATION`。P-W1 手工复制解析及 Android P-A1.2 为独立路径。[会话读取可行性研究](docs/PC_AUTOMATION_FEASIBILITY.md)

## PC 企业微信视觉采集

1. 关闭旧版 AutoIM 实例。用 `uv sync` 安装环境，然后运行 `uv run autoim`。
2. 手动最大化企业微信，打开授权测试会话，准备左右两侧至少三条可见文字消息。需要核对未读和选中分离时，保留另一个未读会话。
3. 在 AutoIM 的“高级工具 → PC 企业微信视觉采集诊断”点击“读取当前企业微信可见界面”。先在“整图”确认最右侧和底部发送区，再核对“区域”“联系人”和“可见消息”。全局未读数字在摘要中显示，静音联系人的未读单独列在联系人页。“消息框”和“OCR”显示候选、置信分数和失败原因。
4. 自动分界不完整时，选中缺失区域并点击“在截图中标定”。已确认的其他区域仍可识别。V1.2 标定使用独立文件 `config/wecom-pc-visual-calibration-v12.json`，以完整的原图坐标为准。勾选“延时 3 秒”可以在采集前手动把鼠标移到 B2、C2、C1 或 C3，观察滚动条差异。

V1.3 回归验收时，核对联系人底部的机器人和系统会话、附带手机号或组织的会话标题，以及左右长短气泡。“C2 文字归属”显示每块 OCR 的去向、最近容器和失败原因。“整图”可勾选显示未归属或不完整候选框。疑似正文无法归属时，状态为 `VISIBLE_MESSAGES_INCOMPLETE`。姓名匹配只确认可见文字一致，唯一身份保持未知。

V2.2 的「可见消息」将文件名、大小、方向及原图卡片位置分开展示。选中卡片可核对文件名原始 OCR 行和图标证据。「C2 文字归属」保留逐块原文和归属。文件名只在同一卡片内按连续行合并，截断或歧义保留原始行，居中时间仍单列为非消息。文件识别仅覆盖卡片可见字段，附件内容保持未读取。

单次读取结果窗口和「当前会话监测」均提供「复制监测诊断报告」和「复制完整识别明细」。摘要只含状态、时间、统计和失败类别。完整明细可能含客户姓名、手机号、聊天正文和文件名，需确认后写入 Windows 剪贴板。来源标为 `WINDOW_VISUAL_OCR`。

视觉入口通过 Windows `PrintWindow(PW_CLIENTONLY)` 获取目标 HWND 的客户区，根据缓冲区未绘制边带测量实际绘制范围。识别在原图上进行，预览按窗口工作区等比适配。自动检测右、底裁切或黑边。所有图像和结果只在内存中临时显示。旧的“解析当前剪贴板”可继续处理已经复制的文本。

依赖包含 RapidOCR 3.4.2（Apache 2.0，随包的中文模型和字典）、ONNX Runtime（MIT，本地 CPU 推理）、OpenCV（Apache 2.0，几何与颜色处理）和 NumPy（BSD 3-Clause，像素缓冲）。使用 Windows 的 Python 3.12 环境安装，模型无需额外下载。OCR 只处理分区裁剪，推理线程限制为 2，单帧上限 2000 万像素。首次安装会下载二进制依赖。

默认阈值见 `VisualThresholds`。可用本地 `config/wecom-pc-visual-thresholds.json` 覆盖单项，例如 `{"foreground_delta": 11, "anchor_tolerance": 0.025}`。标定只保存归一化坐标。[界面识别与监测规范](docs/PC_UI_RECOGNITION_SPEC.md)

## PC-V2 当前会话监测

1. 在 Windows 11 最大化企业微信并手动打开授权测试会话，保留六条已有消息可见。在诊断面板选择“开始监测当前会话”。默认每 2 秒采样。核对“当前已观察到的消息”和基线类型，首轮无论建立 `BASELINE_CREATED` 还是 `PARTIAL_BASELINE`，新增候选均为 0。
2. 由另一测试账号发送 `AUTOIM_PC_V2_002`，再连续发送三条不同消息和两条相同正文。每组等待画面稳定，核对候选的方向、顺序和条数。证据充分时显示 `VISUAL_NEW_CANDIDATE`。追加区有缺失或视觉证据不足时显示 `UNCONFIRMED_VISUAL_CHANGE` 及原因。
3. 手工向上滚动历史并返回底部，再手工切换联系人。核对历史消息和另一个会话的已有消息均未列为新增候选。等待新基线后可继续验证新增。结束时点击“停止监测”。

监测仅跟踪已打开的会话。诊断面板显示会话匹配、采集与 OCR 时刻、采样与识别次数、耗时、基线、重同步原因和候选列表。截图和聊天正文仅在本地内存中处理。换窗、滚动或序列证据含糊时会暂停或重建基线。设备能否捕获连续发送的全部消息待真机验证。

首轮真机监测中，联系人列表的 `姓名@微信` 与标题的 `姓名` 被误报错配，基线未建立。修复后会保留 B2 和 C1 的原文，识别 `@微信`、`＠微信`、`@微信联系人` 并分离标签，再精确核对显示姓名。可靠视觉核对即可用于当前可见会话基线，唯一客户身份仍为未验证。复验时先检查原始选中名称、解析后的姓名、`PERSONAL_WECHAT` 与 `VISUAL_MATCH`，再确认基线条数、候选为 0，并用另一账号发送一条测试消息。真机基线与新增识别均待复验。

后续真机报告确认姓名为 `VISUAL_MATCH`，但 C2 标为 `VISIBLE_MESSAGES_INCOMPLETE`，旧门禁因此清空基线。V2.1 保留已读消息、未归属 OCR 和未读全的卡片，区分部分基线与可靠基线。部分基线只有在早段缺失可定位、可靠尾部位置和顺序可核对、追加区存在独立像素变化时才报告新增候选。原地 OCR 补读只更新基线。完全漏检的消息、媒体和复杂卡片、视口外及两帧之间离开的内容仍可能缺失。[部分基线与完整性规则](docs/PC_UI_RECOGNITION_SPEC.md#pc-v21-部分基线与新增判定)

用户已确认 V2.2 的 Windows 企业微信真机复验通过，含文件识别和一键复制诊断。[文件卡片规则](docs/PC_UI_RECOGNITION_SPEC.md#pc-v22-文件消息与诊断复制)

## 本地持久化与多会话采集

1. 启动 AutoIM。主页显示账号、邮箱、核验状态、采集范围和统计。默认采集普通单聊和普通群聊，静音会话默认关闭。“折叠的聊天”暂不可启用。范围及快捷键在本地配置保存。
2. 首次点击“开始检测”并授权本次 AutoIM 进程，手动切回最大化的企业微信。企业微信处于前台且用户输入空闲时，AutoIM 可核验登录使用者的邮箱。初次给旧账号补绑邮箱时，确认应保留的本地账号 ID。默认 F10 在同一进程中启停，按键停止立即撤销后续鼠标许可。
3. 核验后可停止，并在“高级工具”中对测试会话 B 执行“保存当前可见消息”建立基线。手动打开 A，再次启动，让另一授权测试账号向 B 发送一条测试消息。核对导航、OCR、SQLite 提交及系统通知。运行诊断与高级工具默认折叠，历史记录与采集任务可从主页直接查询。

数据保存到 `%LOCALAPPDATA%\AutoIM\wecom-visual.sqlite3`。当前会话监测和自动巡检互斥。持久化任务只保存可靠关联的业务记录以及不确定视觉观察，首次历史读取与新增候选分开。用户主动发送测试消息；AutoIM 的发送能力保持关闭。滚动条无显示也可以存在对应区域，群看板 D 与 C2 独立，非本人群消息可以保留可靠的上方发送人标签。[表结构与安全规则](docs/PC_UI_RECOGNITION_SPEC.md#pc-v23--pc-v30-本地数据库与多会话采集)

正常巡检每轮结束后等待 2 秒，实测扫描周期还包含截图、OCR 和任务耗时。暂停复检的等待间隔为 3 秒。保持授权开启，手动恢复并最大化企业微信，再切回前台，窗口和账号核对通过后续跑。复制诊断报告可核对账号绑定、关键事件、Win32 状态、耗时及通知提交结果。[PC-V3.1 账号与诊断规则](docs/PC_UI_RECOGNITION_SPEC.md#pc-v31-登录账号与采集反馈)

窗口在后台时，可信的完整截图仍可只读扫描 B2 联系人和未读标记。点击导航前仍要求企业微信当前位于前台，并通过空闲、坐标、遮挡和许可检查。面板区分未执行扫描、扫描后没有未读、发现未读但等待导航等状态。[B2 扫描和前台导航诊断](docs/PC_UI_RECOGNITION_SPEC.md#pc-v311-只读-b2-扫描与前台导航)

架构将 IM 客户端探测与界面分开，后续可以添加微信、钉钉、飞书或其他 IM 的适配器；当前版本不包含这些适配器。

## 当前功能

- 主界面展示账号、采集范围、启停、统计及历史查询。开发工具和日志在“高级工具”中。
- 高级工具检测进程、PID、可执行程序路径、主窗口标题和窗口句柄，并尝试判断 UIA 是否可访问。
- 初始进程检测和后续桌面动作使用同一企业微信主窗口解析规则，只接受唯一、可见、标题及窗口类匹配的主窗口；隐藏 helper 窗口不会被选为目标。
- UIA 扫描深度默认 6 层，可在界面修改。扫描结果在页面显示，并写入 `outputs/wecom-uia-tree.txt`。
- 高级工具提供 UI Automation 多后端诊断：`uiautomation`、`pywinauto` UIA、`pywinauto` Win32 和 Windows UIA Raw View。
- 多后端诊断默认最大深度 10、最多 5000 个控件、每个 backend 30 秒超时；三个限制均可在界面调整。每个 backend 独立运行，超时或异常不会阻止其余 backend。
- 诊断展示 PID、HWND、窗口标题、ClassName 和可读取的控件属性，并检查 AutoIM / 企业微信进程完整性级别；发现企业微信权限级别更高时只记录提示，不会提权。
- 多后端报告保存在 `outputs/uia-diagnostics/`：`uiautomation.txt`、`pywinauto-uia.txt`、`pywinauto-win32.txt`、`raw-view.txt` 和 `summary.txt`。
- “剪贴板诊断”提供企业微信激活、读取 / 清空剪贴板、Ctrl+C、Ctrl+A + Ctrl+C、Ctrl+V、Esc 和手工复制测试。测试开始前由用户手动打开聊天并聚焦目标消息/区域；AutoIM 只恢复企业微信前台并发送快捷键，不自动选择联系人或消息。
- 企业微信桌面动作经 `WeComWindowManager` 动态重新解析并校验 PID/HWND、`WeWorkWindow` 类名和窗口标题；恢复最小化窗口后，最多尝试 3 次激活并核对 `GetForegroundWindow()`。目标歧义或前台校验失败时，快捷键和剪贴板动作取消并写日志。窗口变化会刷新 GUI 的 PID/HWND 信息。
- 企业微信状态区显示连接状态、PID、HWND、标题、ClassName、Rectangle 和前台状态；后台每 2.5 秒被动刷新窗口身份，检测窗口重启和关闭。
- `读取当前剪贴板` 只直接读取 Windows Clipboard，不激活或抢占企业微信前台。清空剪贴板、键盘快捷键及后续点击继续要求企业微信身份/前台 guard。
- “批量聊天记录解析”只读取用户已经复制的 Windows Clipboard 文本，通过独立纯 Python 模块解析并在表格显示消息顺序、原始时间、发送人、联系人类型、企业名称、正文和解析警告；读取与解析在后台线程执行。年份未知时不补年份，方向保持 UNKNOWN，`contact_id` / `conversation_id` 保持 null。身份不足和潜在边界歧义会明确提示。该功能已通过代码、自动化测试和用户 Windows GUI 手工验收。
- 界面标定支持会话列表、聊天标题、聊天消息和输入框。GUI 展示企业微信窗口截图，用户可拖框选区域并预览、保存、重新标定或删除。标定文件 `config/wecom-calibration.json` 只保存窗口内 0..1 相对坐标；旧版坐标会被忽略，需重新标定。
- 截图选区统一使用 `DisplayTransform`，从 Qt 控件鼠标坐标减去图片居中 offset，再按图片实际显示宽高映射至 0..1。QPixmap 的 DPR 只显示在诊断信息，不参与固定补偿；留白区的点和框会拒绝。诊断窗显示源图、控件、实际显示图尺寸、offset、DPR、拖框、源图矩形、normalized 和回显矩形。保存后重新读取配置并立即将区域叠加到当前截图；往返误差大于 2 控件像素时拒绝保存。
- “测试当前消息定位（Dry Run）”只允许截图、点选、计算并显示计划位置，不调用真实鼠标/键盘 API。消息区未标定、窗口身份或 rectangle 改变、点位越出窗口/已标定消息区、前台验证失败时操作会停止。
- **真实复制测试安全闩保持关闭。** 企业微信旧版标定曾出现回显比例偏差。旧 GUI 的真实复制按钮和 `WeComDriver` 真点击/复制接口保持禁用。PC-V3.0 采用独立的 B2 操作许可。两条路径的坐标单元测试均需各自的企业微信真机验收。
- 复制/诊断正文默认不写入日志或报告。开发诊断正文保存复选框默认关闭，启用后才写到 `outputs/vision-diagnostics/copied-message.txt`。
- 窗口截图只抓企业微信主窗口，不默认保存截图；聊天标题只展示经标定的局部截图并由用户人工核对，不对整个界面 OCR。
- 剪贴板文本在 GUI 显示并写入 `outputs/clipboard-diagnostics.txt`；`outputs/clipboard-diagnostics-summary.txt` 记录是否有文本、长度、前 500 字符、剪贴板是否变化及联系人名匹配。聊天内容和无关界面文本由用户核对后在 GUI 标记。
- 通用 Windows 剪贴板与键盘封装位于 `src/autoim/automation/`，不依赖企业微信页面实现。
- `src/autoim/wecom/` 放置企业微信 `WeComWindowManager` 与 `WeComDriver`。
- 每个控件记录 Name、ControlType、AutomationId、ClassName 和 BoundingRectangle。
- 进程检测和 UIA 扫描运行于 Qt 线程池，不阻塞 GUI。
- 日志写入 `logs/autoim.log`，也实时显示在高级工具中的“日志”。
- 使用 Qt Layout，并在 Windows 上启用 DPI 感知。

## 当前范围之外

Windows 自动采集限于授权范围内可安全到达的 B2 联系人和当时可见的 C2 内容。完整历史、停机期间遗漏、同名唯一身份、官方 `messageId`、复杂引用、群成员管理和附件内容仍未覆盖。AI/LLM、自动回复、自动发送、Hook、DLL 注入、逆向工程、Web API 与服务端在范围之外。Android P-A1.2 仅解析 Android 当前可见普通文本并在本机检测变化。

## 环境

- Windows 11
- Python 3.12
- [uv](https://docs.astral.sh/uv/)

## 安装与启动

在仓库根目录运行：

```powershell
uv sync
uv run autoim
```

也可使用模块入口：

```powershell
uv run python -m autoim
```

进入“企业微信”页面后状态会自动刷新。使用“界面标定”时选择要标定的区域，在企业微信窗口截图中拖框并点击保存；建议先标定“聊天消息区域”。“测试标定”会显示该窗口相对区域对应的局部截图。

Dry Run 会在截图中点选测试位置，显示计划屏幕点并明确提示未执行输入。当前真实复制测试禁用。用户应先重做标定，检查“保存标定”后显示的绿色区域是否与拖框重合，并将诊断值交由维护者确认；收到重新验收确认前不要解除代码中的真实输入安全闩。会话标题可通过“聊天标题区域”的局部截图人工核对；当前不运行全界面 OCR。

注意：“开始手工复制测试”属于早期剪贴板诊断，会清空并覆盖当前剪贴板并依次发送 Ctrl+C 与 Ctrl+A + Ctrl+C。请先确认剪贴板中没有需要保留的内容。普通 UIA 扫描结果在 `outputs/wecom-uia-tree.txt`，UIA 多后端结果在 `outputs/uia-diagnostics/`，剪贴板结果在 `outputs/clipboard-diagnostics*.txt`，P1.7 诊断正文仅在用户主动启用保存时写入 `outputs/vision-diagnostics/`。日志路径为 `logs/autoim.log`。

## 项目结构

```text
.
├── src/autoim/
│   ├── app.py          # PySide6 界面、页面和日志配置
│   ├── diagnostics.py  # 多 backend 扫描、Raw View、权限检查和汇总报告
│   ├── backend_runner.py # 独立 backend 进程入口，用于硬超时隔离
│   ├── automation/     # 通用 Windows 剪贴板、键盘和鼠标模块
│   ├── calibration.py  # 仅保存窗口相对标定区域
│   ├── vision.py       # 相对坐标与点击边界验证
│   ├── vision_widgets.py # 截图区域 / 点选窗口
│   ├── wecom/          # 动态窗口管理与前台校验过的 Driver
│   ├── safety.py       # BLOCKED_BY_SAFETY 日志标记 helper
│   ├── clipboard_diagnostics.py # 用户手工选区复制测试与报告
│   ├── workers.py      # 后台任务、企业微信检测和 UIA 扫描
│   └── __main__.py     # python -m autoim 入口
├── outputs/            # 运行时生成 UIA 扫描结果
├── logs/               # 运行时生成应用日志
├── PROJECT.md          # 需求范围、技术决策和阶段进度总控
├── docs/OPEN_SOURCE_RESEARCH.md # 指定开源项目的安全筛选研究
├── scripts/check_security_surface.py # 源码与依赖危险能力标记检查
├── tests/              # 窗口管理、Driver guard、GUI 同步与安全表层测试
├── android-agent/      # 独立 Kotlin Android 诊断 APK 工程
├── pyproject.toml
└── README.md
```

## 测试与安全检查

在仓库根目录运行：

```powershell
uv run python -m unittest discover -s tests -v
uv run python scripts/check_security_surface.py
git diff --check
```

安全检查仅扫描源码和依赖声明中的预设标记；发现标记时只报告并要求人工审查，不会自动删除或修改内容。此检查不替代完整代码审计。

项目永久安全边界及 `BLOCKED_BY_SAFETY` 处理规则见 [PROJECT.md](PROJECT.md)。开源项目的许可证和技术筛选见 [docs/OPEN_SOURCE_RESEARCH.md](docs/OPEN_SOURCE_RESEARCH.md)。
