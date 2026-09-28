# AutoIM 开源项目研究

调研日期：2026-09-28。维护信息与许可证按当日 GitHub 仓库页面/API 可见内容记录；上游分支后续可能变化。

安全等级定义：

- **SAFE**：主要使用 Windows 标准桌面 API 或通用验证/状态管理思想。
- **CAUTION**：包含容易受版本、窗口状态、焦点或屏幕布局影响的正常桌面自动化。
- **DO_NOT_USE**：涉及破解、注入、反检测、协议绕过等，或把这些作为实现目标。

等级用于筛选所研究的技术部分，而不是对整个上游项目作全面代码审计。AutoIM 没有复制任何第三方源代码。

## A. wxbot-automation

仓库：[jxyk2007/wxbot-automation](https://github.com/jxyk2007/wxbot-automation)

- **项目定位**：基于 pyautogui、pywin32 和 psutil 的个人微信/企业微信自动化项目；仓库包含窗口检查、多版本 sender 和消息发送功能。
- **研究内容**：WXWork 进程枚举、按 PID 枚举顶层窗口、匹配 `WeWorkWindow`、每次重新查找 HWND、窗口有效性检查、ShowWindow/SetForegroundWindow、前台验证、窗口矩形与 pyperclip/键盘的使用。
- **License**：MIT。确认仓库根目录 LICENSE。
- **最近维护情况**：最新提交 `97832ee`，日期 2026-03-30；仓库 API 显示更新时间 2026-09-18。
- **安全等级**：窗口枚举/恢复/前台验证部分 **CAUTION**；反风控、人性化随机操作部分 **DO_NOT_USE**。
- **AutoIM 可以采用**：不缓存 HWND、按进程及窗口类动态查找、恢复最小化窗口、调用标准前台 API 后核验前台句柄、失败时停止并记录日志。
- **AutoIM 不采用**：以最大内存进程推断主进程（不够确定）、自动切换/回退到其他 IM、发送流程、随机鼠标轨迹、随机延时和规避平台检测的“人性化”行为。
- **原因**：AutoIM 需要精确识别单个企业微信主窗口并 fail closed；反检测目标与项目安全原则冲突。当前只借鉴设计思路，没有复制代码，因此无需将上游代码登记为 MIT 第三方代码。

## B. wx_work_auto

仓库：[yangyuexiong/wx_work_auto](https://github.com/yangyuexiong/wx_work_auto)

- **项目定位**：面向 Windows 企业微信 PC 客户端的自动化示例，文档覆盖 3.1.10.x 和 4.1.39.x。
- **研究内容**：顶层窗口/PID、恢复与聚焦、正常快捷键、pywinauto，以及仓库对 DirectUI、自定义 `WeWorkWindow`/`PerryShadowWnd` 和标准 UIA Patterns 可访问性的说明。
- **License**：MPL-2.0，仓库标记和 LICENSE 文件均如此。
- **最近维护情况**：仓库可见 3 个提交；最新提交日期 2025-08-03；仓库 API 显示更新时间 2026-02-12。
- **安全等级**：普通前台窗口/快捷键操作 **CAUTION**；基于屏幕图像和坐标的定位 **CAUTION**；为避免平台检测的点击或发送实现 **DO_NOT_USE**。
- **AutoIM 可以采用**：将焦点与窗口状态作为正常用户桌面操作的前置条件；通过实验报告解释当前 UIA 根窗口扫描结果可能与企业微信 4.1 的 DirectUI 情况一致。
- **AutoIM 不采用**：版本降级；图像识别、自动点击与发送；该仓库提到可能触发客户端退出/检测的 UI 点击方式；不直接复制 MPL 源码。
- **原因**：本阶段只实现动态窗口生命周期管理及受前台校验保护的标准键盘/剪贴板操作，不更换客户端版本、不做消息操作或视觉识别。

## C. wecom-automation-agent

仓库：[YUNTONGDONG/wecom-automation-agent](https://github.com/YUNTONGDONG/wecom-automation-agent)

- **项目定位**：带有 OCR 校验、计划调度、失败恢复和审计证据的有状态 WeCom GUI Agent；包含模型/发送链路，超出 AutoIM 当前范围。
- **研究内容**：执行前目标验证、clipboard/input/send 后验证、失败状态、人工批准和审计轨迹等架构设计。
- **License**：GitHub 仓库未检测到 license 文件或 SPDX license；视为没有确认可复用许可证。
- **最近维护情况**：最新提交 `a0abe48`，日期 2026-08-15；仓库 API 显示更新时间 2026-08-15。
- **安全等级**：经过授权、前置检查与失败状态管理的架构思想 **SAFE**；OCR/发送能力对当前阶段 **CAUTION / 不在范围内**；任何绕过机制均 **DO_NOT_USE**。
- **AutoIM 可以采用**：独立重新实现 foreground guard、前置目标检查、clipboard 校验、明确失败状态和审计日志等通用思想。
- **AutoIM 不采用**：任何该仓库源代码、依赖或 Agent/OCR/模型/发送实现。
- **原因**：没有已确认许可证；当前任务也不允许消息读取、AI、OCR 或发送。实现只按 AutoIM 需求独立编写，没有复制其代码。

## 研究结论与采用清单

本次实现采用的只有：动态解析窗口 HWND；校验进程、窗口类、标题及句柄；恢复最小化窗口；有限次数调用标准 `SetForegroundWindow` 并以 `GetForegroundWindow` 验证；焦点未验证时拒绝快捷键和剪贴板动作；失败写入日志；GUI 在重新发现 HWND/PID 后更新状态。

未采用反风控/随机化、客户或会话点击、消息发送、OCR、图像模板、版本降级、上游专有实现。没有复制任何项目代码，也没有新增第三方源码 License 义务。

基于 `wx_work_auto` 的文档和 AutoIM 已完成的 UIA 实测，只能说明 DirectUI 是可能解释之一；不能据此证明所有企业微信版本、界面和 UIA provider 的行为，也没有改变 AutoIM 对标准自动化接口的诊断结论。
