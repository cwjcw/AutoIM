# AutoIM 项目总控文档

> 本文件是 AutoIM 项目的需求、范围、技术决策和开发进度的唯一总控文件。
>
> 所有 AI Agent / Codex 在开始开发前必须先阅读本文件。
>
> 每完成一个开发阶段后必须更新本文件。
>
> 用户已确认的核心需求不得擅自删除、修改或弱化。

---

# 1. 项目基本信息

项目名称：

`AutoIM`

GitHub：

`https://github.com/cwjcw/AutoIM`

运行环境：

- Windows 11
- Python 3.12
- PySide6
- uv
- Git

当前开发方式：

- Codex Desktop
- Luna + High

当前第一优先支持平台：

- 企业微信 Windows 客户端

未来可能扩展：

- 微信
- 钉钉
- 飞书
- 其他 IM

当前阶段不要提前开发其他 IM。

---

# 2. 项目最终目标

AutoIM 是一个运行在 Windows 上的 AI 客服系统。

基本流程：

```text
客户
↓
企业微信
↓
AutoIM
↓
读取客户消息
↓
识别客户和会话
↓
读取历史上下文
↓
查询知识库
↓
AI 判断是否能够可靠回答
↓
┌────────────────┬────────────────┐
│ 可以回答       │ 不能可靠回答   │
↓                ↓
AI生成回复       转人工处理
↓                ↓
发送给客户       人工接管
                 ↓
                 人工回复
                 ↓
                 AutoIM代为发送
```

核心目标不是让 AI 回答所有问题。

核心原则：

> AI 只回答能够基于知识库和明确规则可靠回答的问题。

如果 AI 不能可靠回答：

> 必须交给人工处理，禁止猜测、编造或强行回复。

---

# 3. 核心业务原则

以下属于已经确认的产品原则。

未经用户明确要求，不得修改。

## R001 AI回答必须基于知识库

AI回答客户问题时，应优先根据：

- 企业知识库
- 产品资料
- FAQ
- 业务规则
- 已确认的标准回复
- 必要的上下文

生成答案。

不能仅依赖模型自身知识自由回答。

状态：

`已确认`

---

## R002 AI必须判断自己是否可以回答

AI收到客户问题以后必须进行判断：

```text
CAN_ANSWER
```

或：

```text
NEED_HUMAN
```

不能仅生成一个答案然后直接发送。

判断至少考虑：

- 知识库是否存在相关依据
- 信息是否足够
- 是否存在歧义
- 是否涉及需要授权的业务
- AI置信度
- 是否命中必须人工处理规则

状态：

`已确认`

---

## R003 无法回答必须转人工

以下情况必须转人工：

- 知识库没有答案
- 信息不足
- AI无法确认答案真实性
- 客户问题存在较大歧义
- 涉及特殊报价
- 涉及价格优惠
- 涉及重大交期承诺
- 涉及投诉
- 涉及合同
- 涉及付款异常
- 涉及重要业务承诺
- 系统规则明确要求人工处理
- AI判断风险过高

处理方式：

```text
AI_AUTO
↓
发现不能回答
↓
HUMAN_PENDING
↓
通知人工
↓
HUMAN_ACTIVE
↓
人工回复
↓
人工选择是否恢复 AI
```

状态：

`已确认`

---

## R004 人工不需要登录企业微信客服账号

企业微信客服账号始终登录在专用 AI 客服 Windows 电脑。

人工处理人员原则上不重新登录该企业微信账号。

人工通过 AutoIM 的人工接管界面：

- 查看客户
- 查看聊天上下文
- 查看 AI 判断原因
- 查看 AI 建议
- 输入人工回复
- 点击发送

实际企业微信消息发送动作仍由：

`AutoIM Agent`

在 AI 客服专用电脑执行。

状态：

`已确认`

---

## R005 AI与人工共用统一发送通道

无论消息来自：

- AI
- 人工

都不能分别开发两套企业微信发送逻辑。

统一通过：

```text
send_message()
```

执行。

目的：

- 避免冲突
- 统一日志
- 统一消息状态
- 统一异常处理
- 以后方便扩展其他 IM

状态：

`已确认`

---

## R006 会话必须具有处理状态

每个客户会话至少支持：

### AI_AUTO

AI正常自动处理。

### HUMAN_PENDING

AI无法处理，等待人工。

### HUMAN_ACTIVE

人工正在处理。

### AI_RESUME

人工结束处理，准备恢复 AI。

后续如有必要可以扩展状态，但不得删除以上核心状态。

状态：

`已确认`

---

## R007 人工接管期间 AI 不得发送消息

当状态为：

```text
HUMAN_PENDING
```

或：

```text
HUMAN_ACTIVE
```

AI不得自动向客户发送任何业务回复。

防止出现：

```text
人工正在回复
+
AI同时回复
```

状态：

`已确认`

---

# 4. 第一阶段技术策略

企业微信自动化优先级：

```text
1. Windows UI Automation
↓
2. 剪贴板 / 键盘操作
↓
3. 局部视觉识别
↓
4. OCR
```

当前禁止优先采用：

- Hook
- DLL注入
- 内存读取
- 企业微信逆向工程
- 修改企业微信程序

除非以后用户明确批准。

---

# 5. GUI要求

AutoIM 从第一版开始必须有 GUI。

技术：

`PySide6`

主界面暂定包含：

- 首页
- 消息
- 企业微信
- AI 设置
- 知识库
- 人工接管
- 系统设置
- 日志

当前没有开发的页面允许显示：

`功能开发中`

但架构应允许后续加入。

---

# 6. 企业微信模块需求

最终企业微信模块需要实现：

- 检测企业微信是否运行
- 获取进程
- 获取窗口
- 识别会话列表
- 发现未读消息
- 识别客户
- 读取消息内容
- 识别消息方向
- 保存消息记录
- 填写回复
- 发送消息
- 防止重复读取
- 防止重复发送
- 异常恢复

当前不是所有功能都需要立即实现。

必须按照开发阶段逐步推进。

---

# 7. AI模块规划

未来 AI 模块至少包括：

```text
客户消息
↓
上下文组装
↓
知识库检索
↓
问题分类
↓
风险判断
↓
可回答判断
↓
生成回答 / 转人工
```

AI输出未来建议采用结构化结果，例如：

```json
{
  "decision": "CAN_ANSWER",
  "category": "product_question",
  "confidence": 0.91,
  "knowledge_used": [],
  "reply": "……",
  "reason": "知识库存在明确答案"
}
```

不能回答时：

```json
{
  "decision": "NEED_HUMAN",
  "category": "price_discount",
  "confidence": 0.32,
  "reply": null,
  "reason": "客户要求特殊价格优惠，需要人工授权"
}
```

具体字段后续开发 AI 模块时再正式确定。

---

# 8. 人工接管模块规划

未来人工处理页面至少显示：

```text
待人工处理会话
```

每条显示：

- 客户名称
- 收到时间
- 问题内容
- AI判断的分类
- 转人工原因
- 等待时间
- 所属客服账号

进入会话后显示：

- 历史消息
- 最新问题
- AI检索到的知识
- AI不能回答的原因
- AI建议回复（如有）
- 人工回复输入框

操作至少包括：

- 接管
- 发送
- 暂不处理
- 结束人工接管
- 恢复 AI

---

# 9. 多客服规划

未来支持：

```text
客服电脑01
客服电脑02
客服电脑03
```

每台电脑可以运行独立 AutoIM Agent。

人工接管层不应要求人工知道具体客户在哪一台电脑。

系统负责路由：

```text
人工回复
↓
找到对应 Agent
↓
对应电脑
↓
对应企业微信
↓
对应客户会话
↓
发送
```

当前阶段暂时按照单电脑开发。

但架构不得明显阻碍未来多 Agent 扩展。

---

# 10. 当前开发阶段

当前阶段：

`P1.7 - 安全定位与当前可见消息读取 PoC`

并行扩展路线：

`P-A1.2 - 企业微信文本消息解析、新消息识别与去重（IN_PROGRESS）`

P-A1 核心读取可行性和 P-A1.1 结构证据已通过 Redmi Note 11R 真机验证（298 nodes / depth 18 / 正文、标题、左右消息、输入框和发送按钮）。本轮只实现 P-A1.2 离线普通一对一文本解析与新消息诊断；不进入 P-A1.3。

Android Agent 是独立执行端，当前只验证 Android 官方 AccessibilityService 与 NotificationListenerService，不改变 Windows P1.7 的安全闩或实现。

当前目标：用户手动打开会话后，通过窗口相对标定与用户截图点选，验证安全复制一条当前可见文字消息。

---

# 11. 当前已完成

## P1.1 创建 PySide6 GUI

状态：

`已完成 / 已验证`

内容：

- GUI基础框架
- 企业微信页面
- 日志
- UIA扫描入口

---

## P1.2 企业微信主窗口检测

状态：

`已完成`

当前已确认能够检测：

```text
Name='企业微信'
ControlType=WindowControl
ClassName='WeWorkWindow'
```

并能够获得：

- HWND
- BoundingRectangle

---

## P1.3 第一版 UIA 扫描

状态：

`已完成`

当前结果：

最大扫描深度：

`6`

但仅扫描到企业微信根窗口，没有发现内部子控件。

当前不能确认：

- 企业微信是否使用自绘
- 当前扫描方式是否不正确
- 是否存在其他可访问 UI Tree

---

# 12. 阶段记录与当前正在进行

## P1.5 剪贴板可行性诊断（已完成）

状态：

`DONE / 已验证`

当前范围：

- 用户手动打开会话并选择聊天内容；AutoIM 不自动点击联系人或消息。
- 实现 Windows 剪贴板读写、清空、变化检测和超时等待。
- 在企业微信页增加剪贴板诊断，测试 Ctrl+C 和 Ctrl+A + Ctrl+C。
- 保存剪贴板文本与诊断汇总；语义内容需用户核对，不能假装自动识别。
- 不开发 OCR、AI、自动回复、Hook、注入、内存读取或逆向。

已实现：独立 Windows 剪贴板与键盘模块、企业微信激活和剪贴板诊断 GUI、Ctrl+C / Ctrl+A + Ctrl+C 手工测试、联系人名称文本匹配、人工内容分类、完整复制记录和前 500 字符汇总报告。

真实验证结论：用户确认正常企业微信复制操作可向 Windows Clipboard 写入 `Unicode Text`、`WeWork Message`、`WeChat_RichEdit_Format`。AutoIM 只读取正常 Copy 操作提供的剪贴板数据，不读取客户端内部存储。详细历史诊断输出仍由用户在本机 GUI 实测生成。

输出：`outputs/clipboard-diagnostics.txt`、`outputs/clipboard-diagnostics-summary.txt`。报告含文本内容，已加入 `.gitignore`，仅保存在本机工作目录。

---

## P1.6 安全企业微信窗口管理与 Driver 基础架构

状态：

`DONE / 已验证`

当前范围：

- 新建 `WeComWindowManager`，每次关键动作动态重新解析企业微信 PID/HWND 和主窗口身份。
- 提供窗口有效性检查、最小化恢复、前台激活/验证、窗口矩形查询。
- 前台激活最多尝试 3 次；目标无法唯一确认或前台验证失败时 fail closed。
- 新建 `WeComDriver`，让快捷键及剪贴板诊断先通过窗口 guard；窗口 PID/HWND 变化时同步 GUI。
- 研究三个指定开源项目，仅采用安全的标准 Windows 自动化设计思想，不复制源代码。
- 加入自动化测试与依赖/源代码危险能力标记检查。
- 用户真实 Windows 11 / 企业微信 5.1+ 验收：正常激活、最小化恢复、重启后 PID/HWND 自动更新、关闭后安全停止、Windows Clipboard 读取、AutoIM 激活后 Ctrl+C、选中文字保持并可正常复制，全部通过。
- 先前从非 GUI 子进程调用 `SetForegroundWindow` 被 Windows 拒绝时，程序按设计重试最多 3 次后安全停止；用户在正常 GUI 工作流中已验证激活通过。未尝试提权、置顶或其他绕过方式。
- 自动化验证通过；安全表层检查通过；本机只读 HWND/PID/ClassName/Rectangle 解析通过。

---

## P1.7 安全定位与当前可见消息读取 PoC

状态：

`IN_PROGRESS`

目标：用户手动打开当前会话后，使用窗口相对标定和截图点选定位当前屏幕可见的一条文字消息，并通过普通企业微信 GUI 点击、Ctrl+C 与 Windows Clipboard 读取正文。不自动发现/遍历会话，不读取未读消息，不自动发送。

已确认的基础能力：企业微信 Windows 5.1+ 实测确认，在用户已选中文字的情况下，AutoIM 可安全激活企业微信并通过 Ctrl+C 将文字复制到 Windows Clipboard。

当前实现：企业微信状态区展示连接、PID、HWND、标题、ClassName、Rectangle 和前台状态；后台每 2.5 秒被动刷新，重新启动后更新窗口身份。单纯读取剪贴板不激活企业微信，快捷键和真实点击仍要求新鲜窗口身份与前台校验。

标定区包括会话列表、聊天标题、聊天消息、输入框，JSON 只保存企业微信窗口内 0..1 相对坐标，不保存屏幕绝对坐标。截图仅抓企业微信窗口，默认不落盘；可以在 GUI 截图上拖框、预览、保存、重新标定、删除和测试区域。

Dry Run 通过截图点选并计算计划屏幕点，显示窗口/区域/动作，代码路径不调用鼠标、键盘输入接口。真实复制要求聊天区域标定，重新验证 PID/HWND/Rectangle、前台状态、窗口内点位及聊天标定边界后才正常点击一次并发出 Ctrl+C；前台失效、窗口变化或越界立即停止。复制结果默认不写入日志；日志只记长度、Clipboard 方法、SHA256 和 UNVERIFIED 状态。GUI 提供默认关闭的诊断正文保存选项。

当前阶段还需在真实企业微信上由用户验证标定准确性、Dry Run 标记位置及“真实复制测试”文本是否和点选的可见消息一致。通过前不得开始后续阶段。

阶段已按用户提供的真机验收结果关闭。安全窗口管理器和 Driver 作为 P1.7 的基础继续使用。

---

## P1.4 多后端 UI Automation 诊断

状态：

`DONE / 已验证`

需要测试：

- uiautomation
- pywinauto backend=uia
- pywinauto backend=win32
- Windows UIA Raw View

目标：

确认哪种方式能够发现企业微信内部控件。

已验证结果（2026-09-28）：

| Backend | 状态 | 控件数量 | 最大实际深度 | 耗时 |
| --- | --- | ---: | ---: | ---: |
| uiautomation | 成功 | 1 | 0 | 289 ms |
| pywinauto backend=uia | 成功 | 1 | 0 | 425 ms |
| pywinauto backend=win32 | 成功 | 1 | 0 | 410 ms |
| Windows UIA Raw View | 成功 | 2 | 1 | 332 ms |

Raw View 额外遍历到一个根窗口后代节点，但该节点的 Name、ControlType、AutomationId、ClassName、句柄、BoundingRectangle 和 ProcessId 等属性为空。其他三个 backend 仅发现根窗口。因此当前没有证据确认企业微信向标准 UIA 暴露了可用的内部控件结构；Raw View 的空属性节点也不足以判断主体界面是否自绘。

权限诊断：AutoIM 与企业微信均为中完整性级别，未发现权限级别差异。

GUI 多后端诊断期间事件循环保持响应；每个 backend 独立运行，异常保护已通过无效 HWND 验证。结果文件位于 `outputs/uia-diagnostics/`。

实现包括可配置的最大深度（默认 10）、最大控件数（默认 5000）、单 backend 超时（默认 30 秒），并输出四份 backend 结果和 `summary.txt`。

本阶段范围外，仍禁止开发：

- 消息读取
- AI
- 自动回复

---

# 13. 下一阶段候选

如果 UIA 可以获得企业微信内部控件：

进入：

`P2 - 企业微信消息结构识别`

包括：

1. 会话列表
2. 当前联系人
3. 聊天区域
4. 输入框
5. 未读状态

如果 UIA 无法获得内部控件：

进入：

`P1.5 - 替代读取方式验证`

依次研究：

1. 键盘操作
2. 剪贴板
3. 局部视觉定位
4. OCR

不得直接跳到复杂逆向方案。

---

# 14. 当前明确不开发

以下功能当前属于未来阶段：

- AI自动回答
- LLM
- 知识库
- 人工接管
- 消息数据库
- 多客服
- 服务端
- Web管理平台
- 微信
- 钉钉
- 飞书
- OCR
- 自动发送

这些属于已确认的项目需求或规划。

“当前不开发”不代表删除需求。

---

# 15. 需求状态定义

所有需求使用以下状态：

```text
想法
待确认
已确认
开发中
已完成
已验证
暂缓
废弃
```

只有用户明确要求，需求才能标记：

`废弃`

AI Agent 不得自行废弃需求。

---

# 16. 开发进度状态

开发任务统一使用：

```text
TODO
IN_PROGRESS
DONE
BLOCKED
DEFERRED
```

当前任务状态：

- P1.4 多后端 UI Automation 诊断：`DONE`
- P1.5 剪贴板可行性诊断：`DONE`（用户确认真实复制产生 Unicode Text、WeWork Message、WeChat_RichEdit_Format）
- P1.6 安全企业微信窗口管理与 Driver 基础架构：`DONE / 已验证`
- P1.7 安全定位与当前可见消息读取 PoC：`IN_PROGRESS`
- P-A1 Android WeCom Readability PoC：`IN_PROGRESS`（Accessibility 核心读取可行性已真机验证；通知实际接收等剩余验收未宣称完成）
- P-A1.1 企业微信消息结构解析诊断：`DONE / 已验证`
- P-A1.2 企业微信文本消息解析、新消息识别与去重：`IN_PROGRESS`（0.1.2 实现与本机构建已完成，等待真机验收，不标记 DONE）

## P-A1 Android WeCom Readability PoC

状态：

`IN_PROGRESS`

目标：生成可安装的 AutoIM Android Agent 0.1.0 诊断 APK，验证企业微信 Android 是否向系统 Accessibility Tree 和 Notification Access 暴露当前页面节点及通知。用户手工打开企业微信；AutoIM 只在诊断模式开启且收到 `com.tencent.wework` 事件时采集快照，并在回调中再次验证活动窗口包名。通知只保留企业微信来源。不会点击、输入、发送或联网。

实现约束：只保存最新一份 Accessibility Snapshot 和最近 20 条企微通知到应用私有 JSON 文件；树深度最多 30、节点最多 3000、每节点文本最多 1000 字符。用户可以查看、复制和清空诊断结果。数据损坏时安全忽略。不开启公共存储、SQLite、`INTERNET`、`ACCESS_NETWORK_STATE` 或第三方统计/崩溃 SDK。

参考研究记录：

- WorkTool（Apache-2.0）：仅参考企业微信包名守卫、root 包名复核、节点遍历及属性快照；不采用其自动操作、网络、文件监听和企微缓存读取。
- FlowBot：仅参考无障碍与通知服务分离的架构和生命周期；受限非商业许可，不复制代码。
- AutoJs6（MPL-2.0）：参考连接与实际工作状态区分、事件驱动、异常处理及通知监听生命周期；不采用脚本运行时、联网、Root 或操作能力。
- 详细笔记：`android-agent/REFERENCE_NOTES.md`。

P-A1 验收条件：

1. Windows Gradle Wrapper 能运行单元测试并成功生成真实 APK。
2. 用户可手工开启 Android Accessibility 与 Notification Access。
3. 用户手工打开企业微信会话时能生成最新 Accessibility Snapshot，并检查消息正文、联系人、输入框和发送按钮是否暴露于 Tree。
4. 只记录企业微信通知，最多 20 条，用户可清空。
5. 全程无 Root、Hook、注入、企业微信数据库读取、私有协议逆向和网络权限。

本机开发构建记录（2026-09-29）：

- 环境：Temurin JDK 17.0.20.1、Android SDK Platform 36、Build Tools 35.0.0、Gradle 8.14.5、AGP 8.13.2、Kotlin 2.4.20。
- `gradlew.bat test`：成功；15 个 Android 单元测试全部通过（Debug 与 Release 变体均执行）。
- `gradlew.bat assembleDebug`：BUILD SUCCESSFUL；已生成 `android-agent/app/build/outputs/apk/debug/app-debug.apk`（947,600 bytes，package `com.autoim.agent`，version `0.1.0`，minSdk 26 / targetSdk 36）。
- `gradlew.bat lint`：BUILD SUCCESSFUL；未发现 lint issue。
- APK Manifest 未申请 INTERNET 或 ACCESS_NETWORK_STATE；运行时依赖无第三方统计/崩溃 SDK。
- 当前无连接的 Android 设备（`adb devices` 列表为空），未做 Redmi Note 11R 安装或真机读取验收；P-A1 继续保持 IN_PROGRESS，等待用户按 `android-agent/README.md` 执行真机步骤。

真机验收更新（2026-09-30，用户提供）：

- Redmi Note 11R：Accessibility 已连接；Notification Access 已授权；企业微信已安装。
- 企业微信聊天页面 Accessibility Snapshot：298 nodes / 最大深度 18。
- 可见文本包含真实聊天消息正文；完整 Node Tree 可正常读取。
- Android 官方 AccessibilityService 读取当前企业微信聊天页面内容的核心假设已经通过，不再重复研究“能不能读取”。
- 通知授权不等于实际通知接收验证；当前未宣称通知内容捕获已完成真机验收。
- 此次更新最初仍待标题/左右布局/输入框/发送按钮证据；用户随后已提供这些结果，见 P-A1.1 验收更新。当前不进入自动发送或 P-A2。

## P-A1.1 企业微信消息结构解析诊断

状态：`DONE / 已验证`

范围：在保留现有 AccessibilitySnapshot、NodeSnapshot、DiagnosticStore 和完整 Node Tree 的基础上，从已保存快照派生结构诊断，不调用操作 API，不重新访问企业微信完成搜索。

0.1.1 实现记录（Android Agent 0.1.1 / versionCode 2）：

- 新快照记录唯一 preorder nodeIndex、parentIndex、childIndex、depth 和原始 childCount；不保存 AccessibilityNodeInfo 对象。旧 index 字段保留为同级序号以兼容 0.1.0。
- 保存采集时的屏幕宽高和尺寸来源；按中心 X 的 40% / 60% 边界仅输出 LEFT / CENTER / RIGHT。尺寸缺失或矩形无效时显示 null，不猜测，不赋予消息方向语义。
- 旧版 0.1.0 快照从已知先序 depth/index 恢复结构；非法层级/父索引安全拒绝。旧快照没有采集时屏幕尺寸，提示重新采集，禁止套用当前屏幕尺寸冒充历史尺寸。
- 对每个非空 text/contentDescription 节点记录自身、父/祖父及前后相邻兄弟；缺失关系明确 null，无法读取的兄弟槽位不由旁边节点代替。
- 输入候选：editable 或类名含 EditText；发送候选：text/description 含“发送”，同时显示父/祖父 clickable。只记录候选，不点击。
- Scrollable 候选列出 scrollable 节点；顶部候选采用非空 text 且整个矩形位于快照屏幕顶部 20% 内的规则，只供人工判断标题，不宣布 VERIFIED。
- 独立快照诊断页面包含 7 个折叠区，默认展开关键文本、输入、发送；完整树默认折叠。快照读取、报告派生和搜索在工作线程完成，页面查询固定在打开时的已保存快照。
- “复制结构诊断”仅含设备、企业微信版本、快照摘要及关键候选；完整树单独复制。导出包含聊天文本，仅用户主动复制；不增加联网、公共存储或操作权限。
- 单元测试：原有 15 项 + 新增 26 项 = 41 项；Debug / Release 均通过，共 82 次执行。lintDebug / assembleDebug 和 APK 信息见下方最终构建记录。

最终构建记录（2026-09-30）：

- `gradlew.bat test`、`gradlew.bat lintDebug`、`gradlew.bat assembleDebug` 分别 BUILD SUCCESSFUL；修复一处折叠标题 SetTextI18n 提示后再次联合执行全部任务 BUILD SUCCESSFUL。
- 41 项单元测试在 Debug / Release 中各通过一次，共 82 次执行，failure/error/skipped 均为 0；lintDebug 最终 0 issue。
- APK：`android-agent/app/build/outputs/apk/debug/app-debug.apk`，970,916 bytes，`com.autoim.agent` / versionName 0.1.1 / versionCode 2 / minSdk 26 / targetSdk 36；apksigner 验证通过（v2 签名）。
- 源码与 Manifest 检查：无 Accessibility 操作/手势、自动点击/输入/滚动、网络客户端 API 或 uses-permission；完整树保留、精简导出分离已测试。
- 0.1.1 构建时 `adb devices` 无设备，开发端未执行安装验收；后续 A–E 的 Redmi Note 11R 结构验收结果由用户提供，见下方更新。
- 构建工具仍提示 SDK XML v4/parser v3 兼容警告；不影响上述成功构建与 lint 结果。

真机验收更新（2026-09-30，用户提供，Redmi Note 11R）：

- AccessibilityService 已连接且正常工作；Notification Access 已授权；企业微信 package 为 `com.tencent.wework`。
- 聊天页面快照约 298 nodes / depth 18，聊天正文与完整树可读取。
- 当前会话标题“崔玮杰”可读取：顶部 TextView、CENTER；观察到 `com.tencent.wework:id/nvw`，仅为辅助证据。
- 对方 `FROM_CUSTOMER_001` 为 TextView / LEFT；自己 `FROM_ME_001` 为 TextView / RIGHT；父/祖父结构相近，左右布局是方向证据之一。
- 输入框为可编辑 EditText：editable/clickable/focusable=true，空时为“发消息...”。
- 手工输入后出现标准 Button“发送”：clickable/focusable/enabled=true、RIGHT；观察到 `com.tencent.wework:id/im6`，仅为辅助证据。输入为空时发送节点可能不存在，这是正常状态。
- 以上为用户的真实采样结论，P-A1.1 关闭为 DONE / 已验证；未声称获得官方消息 ID 或完成自动操作。

## P-A1.2 企业微信文本消息解析、新消息识别与去重

状态：`IN_PROGRESS`

目标：基于现有 AccessibilitySnapshot 解析普通一对一可见文本，识别当前会话、INBOUND / OUTBOUND / UNKNOWN，检测新增 occurrence 并过滤重复事件。只产生本地诊断事件，不进入 P-A1.3。

当前实现（Android Agent 0.1.2 / versionCode 3）：

- `WeComSnapshotParser` 使用顶部文本、有限祖先工具栏结构、可编辑输入区和相对几何定义消息区；viewId 仅写入 evidence，不参与唯一识别。键盘抬高输入区仍可解析；不固定节点索引、树深度、分辨率或设备型号。
- 可见普通 TextView 正文按 top / left / nodeIndex 排序。标题、输入、Button、时间、系统提示和区域外文字排除；隐藏节点排除。方向综合正文/容器边距与同一行头像位置，冲突或居中且无可靠锚点时为 UNKNOWN；卡片/媒体/多段文本行不作为可靠普通文本。
- ParsedMessage 保留原始正文、节点索引、文字/容器 bounds、观察时间、签名与 evidence；比较文本只统一 CRLF/CR 并 trim 首尾，保留中间空格、数字、标点、型号和特殊字符。
- `WeComMessageDetector` 按会话标题保存最多 20 份内存状态，初次、重启、切换及返回会话只建立基线。使用明确的尾部/头部有序序列匹配：完整旧序列为当前前缀时按 occurrence 检测追加；窗口移动时要求唯一重叠且锚点只在两份序列各出现一次。无重叠、插入历史或歧义时 RESYNC_REQUIRED，不猜测。
- 连续“你好 / 你好”保留出现次数；相同签名不用于全局去重。相同有序内容（含出现次数）忽略布局变化并记录 DUPLICATE_EVENT。可靠新增产生本地 UUID，明确不是企业微信官方 messageId。
- 快照 schema 3 保存 eventType/eventTime/eventPackage/eventClassName、合并事件类型和 visibleToUser；不持有平台 Event/Node 对象。700ms debounce 保留整个事件批次的滚动/窗口证据。
- 滚动批次不发 NEW_MESSAGE；后续内容变化继续重新同步，直到连续可见序列稳定。窗口改变、低置信度、截断、坏结构、UNKNOWN、空窗口无法重叠、坏检测状态和时钟回退安全阻断/重建。滚动保护期间可能漏报真实新增，这是主动 Fail Closed；可手工重建基线再继续测试。
- 主界面增加消息解析与最近检测状态/原因/conversation/messageCount/newMessageCount、正文/方向/localEventId/时间；支持重建当前基线、清空检测日志、复制消息解析诊断。重建后下一份实时快照仍只建立基线，避免 UI 已保存快照变旧。清空日志不破坏去重状态。
- 最近 100 次检测日志仅在内存，进程/服务重建清空；最新原始快照与最近 20 条企微通知继续只存 app private files。通知仅作诊断，不生成 ParsedMessage 或打开会话。原完整树与结构诊断保留并可折叠。

本轮只做本机开发验证；0.1.2 仍等待用户安装到 Redmi Note 11R 按 `android-agent/README.md` 验收普通消息、同文重复、滚动和会话切换。测试通过不代替真机解析验收。

0.1.2 最终构建记录（2026-09-30）：

- 实际运行 `gradlew.bat test`、`gradlew.bat lintDebug`，以及最终 `gradlew.bat test lintDebug assembleDebug`；最终全部任务 BUILD SUCCESSFUL。
- 单元测试共 103 项：既有 41 项 + 新增纯 Kotlin 合成快照/检测测试 62 项；Debug / Release 各 103 项，共 206 次执行，failure/error/skipped 均为 0。覆盖用户要求的全部 20 类场景，并补充键盘抬升、未知 ID、隐藏节点、头像冲突、媒体卡片和重叠歧义。
- `lintDebug` 报告 `No issues found.`。APK 实际存在：`E:\code\AutoIM\AutoIM\android-agent\app\build\outputs\apk\debug\app-debug.apk`，1,098,235 bytes。
- aapt 确认 `com.autoim.agent` / versionName 0.1.2 / versionCode 3 / minSdk 26 / targetSdk 36；apksigner 验证通过（v2）。SHA-256：`4F3F50712D633DF78D55F77BAFC97CAB67C3D05FFEA5AA8AC0FDCB8986DFD6DB`。
- APK permission dump 无 uses-permission；源码/Manifest 未增加 INTERNET、网络客户端、Accessibility 操作/手势、自动点击/输入/发送、Root 或 Hook。
- `adb devices` 无连接设备；未自动安装或宣称完成 0.1.2 真机验收。P-A1.2 保持 IN_PROGRESS；停止于本阶段，不开发 P-A1.3。

---

# 17. 开发规则

每次 Codex 开始工作时必须：

1. 阅读 `PROJECT.md`
2. 阅读现有代码
3. 确认当前阶段
4. 只开发当前任务
5. 不主动扩展未来功能

每次完成任务后必须：

1. 运行测试
2. 检查错误
3. 更新 README（如需要）
4. 更新 `PROJECT.md`
5. 更新“已完成”
6. 更新“当前正在进行”
7. 更新“下一步”
8. 保留历史需求

---

# 18. 需求变更记录

## 2026-09-28

新增确认：

- 项目独立于其他现有系统
- Windows 环境运行
- 第一阶段只支持企业微信
- 从第一版开始必须有 GUI
- AI不能回答所有问题
- AI必须依赖知识库判断能否回答
- AI无法可靠回答的问题必须转人工
- 人工不重新登录 AI 客服企业微信账号
- 未来需要 AutoIM 人工接管机制
- AI和人工统一通过 Agent 向企业微信发送消息

---

# 19. 当前下一步

当前任务：完成 P1.7 标定与当前可见消息安全复制 PoC，并由用户在真实企业微信 GUI 验收。P1.7 完成后停止；不得自行进入 P1.8 未读消息检测或任何更后续阶段。

Android 当前任务：P-A1.1 已按 Redmi Note 11R 的真实结构结果标记 DONE / 已验证。P-A1.2 普通一对一文本解析、方向判断、新消息检测与去重已实现，状态保持 IN_PROGRESS，等待安装 0.1.2 后真机验收。只允许离线只读诊断；不进入 P-A1.3，不接服务器或开发自动发送。

---

# 20. 用户新增需求记录区

后续用户提出新的需求时：

必须先记录到本节，再判断应该放入哪个正式需求章节。

不得仅存在于聊天记录中。

格式：

```text
日期：
需求：
状态：
所属模块：
计划阶段：
备注：
```

日期：2026-09-28
需求：通过用户手动选择企业微信聊天区域，验证 Ctrl+C / Ctrl+A + Ctrl+C 与 Windows 剪贴板读取文本的可行性；生成逐项结果和汇总报告。
状态：已完成（用户确认已产生 Windows 剪贴板文本与企业微信格式）
所属模块：企业微信 / automation
计划阶段：P1.5 剪贴板可行性诊断
备注：不自动选择联系人或消息；不包含 OCR、AI、自动回复、Hook、DLL 注入、内存读取或逆向。

日期：2026-09-28
需求：研究三个指定开源项目并实现安全的企业微信动态窗口管理、前台 guard、WeComDriver 基础接口、剪贴板诊断接入、测试、自动安全表层检查及永久安全约束。
状态：已完成 / 已验证（用户提供企业微信 5.1+ 真机验收）
所属模块：企业微信 / automation / 安全
计划阶段：P1.6 安全企业微信窗口管理与 Driver 基础架构
备注：禁止复制未授权第三方源码；不实现消息读取/发送、OCR、AI、Hook、注入、内存访问、破解或规避检测。

日期：2026-09-28
需求：显示动态企业微信身份信息；直接读取剪贴板不抢焦点；提供窗口相对界面标定、Dry Run 和用户截图点选后一条可见消息的 Clipboard 复制 PoC。
状态：开发中
所属模块：企业微信 / automation / vision
计划阶段：P1.7 安全定位与当前可见消息读取 PoC
备注：窗口截图不默认落盘，区域只存归一化相对坐标；只对用户可见并手动点选的信息操作，禁止自动发现会话、读取未读、自动发送或绕过安全边界。

日期：2026-09-29
需求：新增可安装的 Android Agent 0.1.0 离线诊断 APK，验证企业微信 Android 的 Accessibility Tree 与 NotificationListenerService 可见性。
状态：开发中
所属模块：Android Agent / Accessibility / Notification
计划阶段：P-A1 Android WeCom Readability PoC
备注：Windows P1.7 保持 IN_PROGRESS；Android 仅保存企业微信最新 UI 快照与最近 20 条通知至应用私有文件。禁止联网、自动点击/发送、OCR、Root、Hook、注入、数据库读取和协议逆向。


日期：2026-09-30
需求：基于已验证的 Android 企业微信 Accessibility Snapshot，新增 P-A1.1 消息结构解析诊断、结构索引、节点亲缘/几何候选、折叠区、快照内搜索、精简复制导出，并升级 Android Agent 至 0.1.1。
状态：DONE / 已验证（Redmi Note 11R 的正文、标题、左右布局、输入框、发送 Button 证据已由用户确认）
所属模块：Android Agent / Snapshot / 结构诊断
计划阶段：P-A1.1 企业微信消息结构解析诊断
备注：Redmi Note 11R 已确认 298 nodes / depth 18 / 实际消息正文，核心读取假设通过。仅只读诊断；禁止自动点击、输入、发送、联网；不进入 P-A2。

日期：2026-09-30
需求：P-A1.1 按用户真机结果关闭；只开发 P-A1.2 的会话/普通文本解析、方向、有序新消息检测、重复事件过滤、滚动/切换安全重新同步与诊断界面，升级 Agent 至 0.1.2。
状态：IN_PROGRESS（本机自动测试与构建后，等待 Redmi Note 11R 安装验收，不标记 DONE）
所属模块：Android Agent / Snapshot / 消息解析与检测
计划阶段：P-A1.2 企业微信文本消息解析、新消息识别与去重
备注：纯 Kotlin 合成快照测试，无真实客户 fixture；签名不是 messageId，UUID 仅为本地事件 ID。禁止网络权限、自动点击/输入/发送、服务器、AI、OCR、Root、Hook、数据库或协议逆向；不进入 P-A1.3。

---

# AutoIM 安全与数据获取原则

本章节为永久项目约束。后续 Agent 不得自行修改、删除或弱化；变更必须由用户明确要求。

本次开源项目研究记录见 `docs/OPEN_SOURCE_RESEARCH.md`。

1. 安全优先于功能。
2. 不破解企业微信。
3. 不读取企业微信进程内存。
4. 不解密企业微信数据库。
5. 不向企业微信进程注入代码或模块。
6. 不修改企业微信客户端、EXE、DLL 或 Accessibility 行为。
7. 不逆向企业微信私有通信协议，不伪造客户端通信。
8. 不拦截、解密或重放企业微信网络流量。
9. 不绕过企业微信的权限、安全、风控或设备限制。
10. 不开发反检测、伪装真人或隐藏自动化行为。
11. 数据和操作仅使用官方能力、Windows 标准 API、正常用户可执行的键盘/鼠标操作、Windows Clipboard，以及用户当前屏幕上可见内容的截图/OCR；仅在对应阶段经用户授权后开发。
12. 若功能没有符合本安全原则的实现方式，停止该功能，在运行日志和 `PROJECT.md` 中标记 `BLOCKED_BY_SAFETY`，并说明“当前没有找到符合 AutoIM 安全原则的实现方式”。
13. Fail closed：目标窗口/会话/内容/输入区域无法可靠确认时，取消操作，不猜测后继续。
14. 宁可功能缺失，也不得违反以上原则。
