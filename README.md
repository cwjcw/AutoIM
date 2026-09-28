# AutoIM

AutoIM 是面向 Windows 的桌面 IM 自动化能力验证工具。当前阶段仅支持**企业微信 Windows 客户端**，提供 PySide6 GUI、进程和主窗口检测、UI Automation 诊断，以及用户手动选择后的剪贴板复制可行性测试。

当前开发阶段为 **P1.6：安全企业微信窗口管理与 Driver 基础架构**。剪贴板可行性已由用户确认：正常复制可包含 Unicode 文本及企业微信格式数据。

架构将 IM 客户端探测与界面分开，后续可以添加微信、钉钉、飞书或其他 IM 的适配器；当前版本不包含这些适配器。

## 当前功能

- 左侧导航：首页、消息、企业微信、AI 设置、系统设置、日志。
- 首页展示企业微信连接状态、Agent 状态和消息数量。Agent 当前显示“未启动”，消息数量固定为 0。
- 企业微信页面检测进程、PID、可执行程序路径、主窗口标题和窗口句柄，并尝试判断 UIA 是否可访问。
- 初始进程检测和后续桌面动作使用同一企业微信主窗口解析规则，只接受唯一、可见、标题及窗口类匹配的主窗口；隐藏 helper 窗口不会被选为目标。
- UIA 扫描深度默认 6 层，可在界面修改。扫描结果在页面显示，并写入 `outputs/wecom-uia-tree.txt`。
- 企业微信页面提供 UI Automation 多后端诊断：`uiautomation`、`pywinauto` UIA、`pywinauto` Win32 和 Windows UIA Raw View。
- 多后端诊断默认最大深度 10、最多 5000 个控件、每个 backend 30 秒超时；三个限制均可在界面调整。每个 backend 独立运行，超时或异常不会阻止其余 backend。
- 诊断展示 PID、HWND、窗口标题、ClassName 和可读取的控件属性，并检查 AutoIM / 企业微信进程完整性级别；发现企业微信权限级别更高时只记录提示，不会提权。
- 多后端报告保存在 `outputs/uia-diagnostics/`：`uiautomation.txt`、`pywinauto-uia.txt`、`pywinauto-win32.txt`、`raw-view.txt` 和 `summary.txt`。
- “剪贴板诊断”提供企业微信激活、读取 / 清空剪贴板、Ctrl+C、Ctrl+A + Ctrl+C、Ctrl+V、Esc 和手工复制测试。测试开始前由用户手动打开聊天并聚焦目标消息/区域；AutoIM 只恢复企业微信前台并发送快捷键，不自动选择联系人或消息。
- 企业微信桌面动作经 `WeComWindowManager` 动态重新解析并校验 PID/HWND、`WeWorkWindow` 类名和窗口标题；恢复最小化窗口后，最多尝试 3 次激活并核对 `GetForegroundWindow()`。目标歧义或前台校验失败时，快捷键和剪贴板动作取消并写日志。窗口变化会刷新 GUI 的 PID/HWND 信息。
- `WeComDriver` 将键盘/剪贴板操作与 WeCom 窗口管理分开。剪贴板读取、清空、序列号检查都在前台校验之后进行；等待复制结果时会被动检查焦点，若用户切换到其他应用则立即取消，不再抢回焦点。窗口管理使用普通 Windows API，不设置置顶、不用屏幕坐标，不做隐蔽或规避检测操作。
- 剪贴板文本在 GUI 显示并写入 `outputs/clipboard-diagnostics.txt`；`outputs/clipboard-diagnostics-summary.txt` 记录是否有文本、长度、前 500 字符、剪贴板是否变化及联系人名匹配。聊天内容和无关界面文本由用户核对后在 GUI 标记。
- 通用 Windows 剪贴板与键盘封装位于 `src/autoim/automation/`，不依赖企业微信页面实现。
- `src/autoim/wecom/` 放置企业微信 `WeComWindowManager` 与 `WeComDriver`。
- 每个控件记录 Name、ControlType、AutomationId、ClassName 和 BoundingRectangle。
- 进程检测和 UIA 扫描运行于 Qt 线程池，不阻塞 GUI。
- 日志写入 `logs/autoim.log`，也实时显示在“日志”页面。
- 使用 Qt Layout，并在 Windows 上启用 DPI 感知。

## 当前范围之外

本阶段不实现 AI/LLM、自动回复、消息读取、未读消息监听、OCR、截图识别、Hook、DLL 注入、逆向工程、SQLite、Web API 或服务端。

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

进入“企业微信”页面点击“检测企业微信”。进入“剪贴板诊断”前，先在企业微信手动打开聊天并点击一条文字消息或聊天区域，再点击“开始手工复制测试”。测试会清空并覆盖当前剪贴板，然后依次发送 Ctrl+C 和 Ctrl+A + Ctrl+C；请先确认剪贴板中没有需要保留的内容。普通 UIA 扫描结果保存到 `outputs/wecom-uia-tree.txt`，UIA 多后端结果保存到 `outputs/uia-diagnostics/`，剪贴板结果保存到 `outputs/clipboard-diagnostics*.txt`。日志路径为 `logs/autoim.log`。

## 项目结构

```text
.
├── src/autoim/
│   ├── app.py          # PySide6 界面、页面和日志配置
│   ├── diagnostics.py  # 多 backend 扫描、Raw View、权限检查和汇总报告
│   ├── backend_runner.py # 独立 backend 进程入口，用于硬超时隔离
│   ├── automation/     # 通用 Windows 剪贴板与键盘模块
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
├── pyproject.toml
└── README.md
```

## 测试与安全检查

在仓库根目录运行：

```powershell
uv run python -m unittest discover -s tests -v
uv run python scripts/check_security_surface.py
```

安全检查仅扫描源码和依赖声明中的预设标记；发现标记时只报告并要求人工审查，不会自动删除或修改内容。此检查不替代完整代码审计。

项目永久安全边界及 `BLOCKED_BY_SAFETY` 处理规则见 [PROJECT.md](PROJECT.md)。开源项目的许可证和技术筛选见 [docs/OPEN_SOURCE_RESEARCH.md](docs/OPEN_SOURCE_RESEARCH.md)。
