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

`PC-V2.3 - SQLite 消息持久化；PC-V3.0 - 企业微信多会话自动采集（PENDING_DEVICE_VERIFICATION）`

Windows 当前交付路径是用户手动打开会话后，从目标窗口截图监测可见内容。PC-V1.3 的可见聊天读取已通过用户真机验收（`DONE / VERIFIED`）。P1.7 坐标点选与 P-W1 剪贴板解析保留各自的用途。

并行扩展路线：

`P-A1.2 - 企业微信文本消息解析、新消息识别与去重（IN_PROGRESS）`

P-A1 核心读取可行性和 P-A1.1 结构证据已通过 Redmi Note 11R 真机验证（298 nodes / depth 18 / 正文、标题、左右消息、输入框和发送按钮）。本轮只实现 P-A1.2 离线普通一对一文本解析与新消息诊断；不进入 P-A1.3。

Android Agent 是独立执行端，当前只验证 Android 官方 AccessibilityService 与 NotificationListenerService，不改变 Windows P1.7 的安全闩或实现。

P1.7 历史目标：用户手动打开会话后，通过窗口相对标定与用户截图点选，验证安全复制一条当前可见文字消息。

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

真机标定测试确认标定数据能写入配置，但“测试标定”回显区域与用户实际拖框存在明显比例偏差（示例左边界实际约 0.31、保存为 0.178）。代码根因是旧选区控件把 `QGraphicsView.mapToScene()` 的 scene 坐标当成原始截图像素：scene 矩形取 `QPixmap.width/height`（设备像素），而 `QGraphicsPixmapItem` 按 QPixmap 的 device-independent size 绘制；DPR 不为 1 时，两者单位不同，scene 坐标除以原始尺寸会系统性缩小（例如约 1/1.75）。此外旧逻辑会将图片外的留白点击 clamp 到 scene 边缘，而不是拒绝。`fitInView()` 的 resizeEvent 虽会重算视图缩放，但这不能修复像素与逻辑单位混用。

真机测试确认标定持久化正常，但 GUI 缩放截图与原图之间的坐标转换存在明显偏差，当前阻塞真实点击测试。

已改为单一 `DisplayTransform`：使用原始截图像素宽高和交互控件当前逻辑像素宽高计算 KeepAspectRatio 下的实际显示尺寸、offset；输入按“鼠标控件坐标 - offset → 除以图片显示尺寸 → normalized”计算。DPR 只作为诊断值显示，不使用固定补偿系数；绘制前将 QPixmap DPR 元数据归一为 1，并通过显式 target rect 画到计算出的显示矩形。正反变换使用同一组几何，留白点拒绝。

标定保存前做 normalized 往返回显检查，最大边误差须不超过 2 个控件像素；保存后立即重新读取文件，再反算并叠加到相同截图供用户查看。诊断窗显示原始截图/QPixmap/控件/显示图片尺寸、offset、DPR、拖框控件矩形、原图矩形、normalized 矩形、回显矩形及误差。旧版本 1 标定数据不再加载，必须重新标定。

**安全闩：**用户明确要求暂停真实点击，因此 GUI 的“真实复制测试”保持禁用，`WeComDriver` 的真实点击/复制接口默认也会 Fail Closed；Dry Run 和标定回显仍可运行。坐标自动测试通过不等于真机标定已验收。只有用户重新完成真机标定验收后，才能另行解除该安全闩。默认日志和报告不保存聊天正文。

当前阶段还需由用户在真实企业微信核对拖框、保存后绿色回显和 Dry Run 标记。坐标回显偏差通过、用户确认新的真机标定前，不允许真实复制，也不得开始后续阶段。

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
- 生产消息库的多用户权限、加密和服务端同步
- 多客服
- 服务端
- Web管理平台
- 微信
- 钉钉
- 飞书
- C2 视口之外的历史消息 OCR 与自动遍历
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
- P-W1 Windows PC 已复制聊天记录结构化解析：`DONE / 用户 Windows GUI 验收通过（2026-11-22）`
- P-W2 Windows PC 会话定位、多选复制完整性与安全发送可行性研究：`DONE（分析）/ 未实施自动操作`
- PC-V1 企业微信界面视觉识别与直接采集：`DONE / VERIFIED`（用户实测的可见聊天场景）
- PC-V2.2 文件识别与诊断复制：`DONE / USER_VERIFIED`（用户报告 Windows 企业微信复验通过）
- PC-V2.3 SQLite 消息持久化：`CODE_IMPLEMENTED / PENDING_DEVICE_VERIFICATION`
- PC-V3.0 多会话自动采集：`CODE_IMPLEMENTED / PENDING_DEVICE_VERIFICATION`
- P-A1 Android WeCom Readability PoC：`IN_PROGRESS`（Accessibility 核心读取可行性已真机验证；通知实际接收等剩余验收未宣称完成）
- P-A1.1 企业微信消息结构解析诊断：`DONE / 已验证`
- P-A1.2 企业微信文本消息解析、新消息识别与去重：`IN_PROGRESS`（0.1.2 真机标题解析失败；0.1.3 修复标题组与身份/装饰解析，等待真机验证，不标记 DONE）

## P-W1 Windows PC 已复制聊天记录结构化解析

状态：

`DONE / 用户 Windows GUI 验收通过（2026-11-22）`

本阶段只解析用户已经通过企业微信原生复制功能放入 Windows Clipboard 的文本。不会自动操作企业微信窗口，不自动选择联系人或消息，不发送 Ctrl+C，不自动发送消息。

### Windows PC 端已有真实验收记录（用户提供）

- Windows 11 上 AutoIM Windows GUI 正常启动。
- 企业微信主窗口标题 `企业微信`、ClassName `WeWorkWindow`、PID、HWND 和窗口位置已识别成功。
- UIA、pywinauto UIA / Win32 与 UIA Raw View 没有发现足够的聊天内部语义控件；本阶段不重复开发 UIA 扫描器。
- 用户在企业微信原生多选三条消息并复制，AutoIM 的“读取当前剪贴板”成功取得完整文本、发送人、消息时间与顺序。
- 用户提供的真实文本格式为 `发送人 日期时间`、空行、正文、空行、下一条消息头。

以上是本阶段开始前已提供的 Windows 真实复制/窗口检测记录；新增结构化解析 GUI 的验收状态单独记录如下。

用户于 2026-11-22 确认新增 GUI 手工验收无问题；P-W1 代码、自动化测试及 Windows GUI 手工验收完成。该验收仅覆盖已复制文本的结构化解析，不扩大自动定位、自动选择、完整性确认或发送的验证范围。

### 本阶段实现

- `src/autoim/wecom/chat_parser.py` 提供纯 Python `parse_wecom_clipboard(text)`；不依赖 PySide6。
- 支持多条复制消息、保留原顺序、重复正文/时间不去重、中文与多行正文、CRLF / LF、原始头部与正文块。
- 时间只保留 `raw_time`；复制格式不含年份时 `parsed_datetime=null`、`date_year_reliable=false`，不推断年份。
- 联系人分为 `INTERNAL_WECOM`、`EXTERNAL_WECOM`、`PERSONAL_WECHAT`、`UNKNOWN`。当前复制文本没有足够的本公司员工凭据，因此姓名或无标签不会被判为内部员工。完整且明确的外部企业身份标签才判为 `EXTERNAL_WECOM`；`@微信` / `@微信联系人` 判为 `PERSONAL_WECHAT`；冲突或未知标签为 `UNKNOWN`。
- `contact_id`、`conversation_id` 保持 null；消息方向保持 `UNKNOWN`。临时 `local_record_id` 仅是解析记录标识。
- 采用消息头候选识别；无法证明消息头样式行不是正文时，记录边界歧义，供人工核对，不把它静默标成可靠边界。解析异常保留原始剪贴板文本。
- 企业微信页新增“批量聊天记录解析”区域。按钮在后台线程读取当前剪贴板并解析，表格展示序号、原始时间、发送人、联系人类型、企业名称、正文和解析状态；详情面板显示身份证据、警告和完整原文。
- 聊天正文不进入普通日志；不清空剪贴板、不激活企业微信、不截图、不自动点击或发送。

### 自动化验证与待验收事项

- 新增 30 项以上解析与 GUI 自动化覆盖，包括用户三条真实样本、联系人分类、身份冲突、重复消息、边界歧义、部分/非聊天文本、超长文本与重复解析。
- 自动化测试和安全表层检查通过；用户已在 Windows GUI 完成手工验收并确认无问题。

### 未完成与安全边界

- 客户唯一身份及会话唯一身份仍待可靠企业微信标识方案；当前不创建官方 ID。
- 自动定位会话、自动选择多条消息、确认复制完整性、识别消息方向及安全发送均未由本阶段验证或实现。
- Windows P1.7 的现有真实点击/复制安全闩保持原状；Android Agent 及其阶段记录未修改。

## P-W2 Windows PC 自动化可行性研究

状态：

`DONE（分析）/ 未实施自动操作`

研究报告：`docs/PC_AUTOMATION_FEASIBILITY.md`

结论摘要：

- 剪贴板只是当前可见消息的手工复制通道，不是完整的会话读取方案。企业微信开发者中心存在官方“获取会话内容”文档入口；应先由管理员核实存档开通状态、人员/会话覆盖、授权告知、许可、保留及安全要求。
- 若官方存档适用，应优先单独评估官方 SDK/API 作为消息数据源；它与读取 Windows 当前 UI 是不同路线，可能要求独立受控采集服务/后端。本项目尚未确认这些条件，也未接入接口。
- 若官方存档不适用，PC 窗口路线受限于 UIA 缺少聊天语义、视觉方式仅覆盖当前可见页面、剪贴板依赖用户选择；不能单独保证完整会话读取。
- 主窗口 PID/HWND/Rectangle 可以被动态校验，但现有证据不能绑定唯一聊天会话；同名用户和会话切换仍为身份风险。
- 用户已验证企业微信原生多选复制三条消息；这没有验证 AutoIM 自动选择，也没有提供可独立核对的选区条数。
- 剪贴板 sequence number 只能证明剪贴板变化；解析出的条数和边界状态不能单独证明复制完整。
- `contact_id`、`conversation_id` 仍为空，方向仍为 UNKNOWN，项目没有受控统一 `send_message()` 实现；当前不具备安全自动发送条件。
- 自动发送和安全闩变更须另行授权并通过独立验收。

研究仅使用本地实现与项目已有 Windows 验收记录，并参考 Microsoft UI Automation / Clipboard 文档及企业微信官方“获取会话内容”文档入口。官方 API 页面正文无法由本次检索工具读取，开通与授权细节待企业管理员核实。公开检索未找到可确认当前企业微信 Windows 多选计数/全量复制语义的官方说明。没有修改 Driver、GUI、安全闩或 Android Agent。

## PC-V1 企业微信界面视觉识别与直接采集

状态 `DONE / VERIFIED`，仅限用户已验证的 Windows 11 最大化企业微信可见聊天场景。由用户手动打开测试会话。[AutoIM PC 端企业微信界面识别规范](docs/PC_UI_RECOGNITION_SPEC.md)

- 用户确认的结构为 A 功能区、B 最近联系人区、C 会话区。B 分为 B1 固定搜索工具栏和 B2 滚动联系人列表。C 分为 C1 标题、C2 历史消息、C3 输入区。
- B2 的未读标记、静音和选中状态分别识别。静音会话仍检查未读。红点无可靠数字时保留空值。B2、C2 的滚动条独立，出现时只作辅助证据。C1、C3 的悬停不触发 C2 滚动条。
- C2 先定位消息容器，再按容器边缘对齐标定或候选左右内容锚点。仅左侧可靠为 `INBOUND`，仅右侧可靠为 `OUTBOUND`。方向冲突、截断或证据不足为 `UNKNOWN`。颜色辅助找容器，草稿留在 C3。
- 已实现 HWND 客户区截图、窗口状态和 DPI 校验、区域和局部文字识别、联系人行及消息诊断。后台使用本地 RapidOCR / ONNX Runtime，结果通过 GUI 显示。错配报 `CONVERSATION_MISMATCH`，相同标题也不建立唯一会话身份。
- 初版 Windows 自动化验证共 112 项通过、0 项失败。新增 34 项，原 78 项回归通过。原生截图使用程序创建的 Win32 测试窗口，真实本地 OCR 从合成画面读出三条中文消息。安全表层检查与 `git diff --check` 通过。
- 视觉输出限于当前视口。未实现自动导航、切换会话、滚动读取历史、完整性认证和消息发送。

### 首次真机失败及 V1.2 修复

- 用户报告在 DPI 168（175%）预览缺右侧和底部，布局及消息识别失败。复测中，旧缓冲区 1925 × 1025 恰好截取了 2696 × 1436 真实绘制范围的左上部分，右下发送标签位于旧范围外。旧诊断窗口 1120 × 740 逻辑像素，也超过约 1097 × 583 的可用工作区。
- 保留 PrintWindow，使用未绘制边带确定实际绘制范围。V1.2 适配工作区并对完整原图等比预览。八区分别记录来源和失败原因，保留已识别的联系人、标题、OCR 文字和消息框候选。A 区全局未读独立于 B2，静音会话仍检测。
- 只读实机探测取得八区、9 个联系人名称、全局未读 3，并识别到与标题匹配的高亮行。同次探测 C2 有 18 个尚未归属容器的 OCR 文字块、2 个容器候选、0 条已解析消息。消息正文归属、容器和方向仍需在测试会话复验。
- V1.2 回归为 132 项通过、0 项失败，含 20 项新增用例。同一组新增用例在 Qt 100%、125%、150%、175% 下分别通过。安全表层检查与 `git diff --check` 通过。

### V1.3 联系人、标题与可见消息归属

- 用户后续实机验收确认截图、布局、选中行和 OCR 可用，但底部漏掉机器人和系统会话两行。C1 同行混合姓名、手机号和组织导致误报错配。两条蓝色消息 OCR 置信约 98%–99%，V1.2 仅提取至少六条消息中的四条。
- B2 补入名称列及联系人行节距证据，保留无标准头像、缺少预览和部分可见的候选。C1 拆分辅助身份字段并比较可靠姓名，唯一身份保持未验证。C2 补充蓝色轮廓路径及空间关联，方向仍由容器的左右锚点判断。
- C2 逐块记录正文、时间、系统、媒体及未归属原因。未归属候选可能为正文时标记 `VISIBLE_MESSAGES_INCOMPLETE`。截图、分区和 OCR 引擎沿用 V1.2。
- 自动化回归 154 项通过、0 项失败，含 22 项 V1.3 新增用例。六条合成消息按原顺序提取。安全表层检查和 `git diff --check` 通过。
- 用户的 V1.3 Windows 11 复验确认五项结果：完整窗口截图；A／B／C 及子区域定位；联系人列表含底部行；当前会话标题及选中联系人匹配；六条可见聊天消息完整读取，含此前遗漏的两条蓝色消息，普通消息方向可区分。验收范围为这次可见聊天场景。

## PC-V2 企业微信当前会话持续监测

状态 `IN_PROGRESS / PENDING_DEVICE_VERIFICATION`。后台复用 V1.3 截图、分区和本地 OCR。默认采样间隔 2 秒，C2 变化和 C1／B2 守卫通过连续两帧确认后才进入完整识别。单任务串行，异常退避，关闭 GUI 时取消后续采样。

首次可靠观察只建基线。后续比较有序可见消息的精确重叠及容器位置，需要稳定的尾部追加证据，重复正文保留各自的出现顺序。人工滚动、窗口或会话变化、视觉姓名或连续性证据不足、消息归属不完整会阻断新增判断。视觉候选仅用于诊断，未获得企业微信官方 `messageId`，唯一客户身份未验证。

仅监测用户已打开的会话。未接入自动发现或打开未读会话、自动滚动或发送。视口外、暂停期间和采样间离场消息的完整性未知。V2 的持续监测与连续消息捕获率待 Windows 11 企业微信独立验收。[监测规则和真机验收步骤](docs/PC_UI_RECOGNITION_SPEC.md#pc-v2-当前会话持续监测)

Windows 自动化回归 189 项通过，含原有 154 项与 PC-V2 新增 35 项。安全表层检查和 `git diff --check` 通过。

### PC-V2 首轮真机基线失败及修复

- 首轮 Windows 真机观察将 B2 的 `姓名@微信` 与 C1 的 `姓名` 判为 `CONVERSATION_MISMATCH`，监测显示 `OBSERVATION_UNRELIABLE`、基线 0。旧 B2 将整段名称 OCR 直接作为显示名，旧比较仅做字符规范化后精确比较。合成回归复现了页面和消息可读、单因姓名错配而无法建立基线的路径。
- B2、C1 保留原始文字和已识别标签。精确识别 ASCII 或全角 @ 后的 `微信`、`微信联系人` 及明确组合，外部企业标签需要有可信组织名称证据。含“微信”的公司名按企业标签处理。冲突、未知标签或低置信姓名阻断核对。
- 当前会话的只读基线允许用可靠的显示姓名核对，头像、标题附加字段和唯一客户身份均为非必需。已知会话变化或同名歧义仍阻断旧基线连续性。面板分别显示页面、选中行、原始名称、解析姓名、标签、消息可靠性、连续稳定采样和直接阻断原因。
- PC-V2 保持 `IN_PROGRESS`。真机复验需在原场景确认先建立基线且候选为 0，再由测试账号发送一条新消息核对候选。
- 本次 Windows 自动化回归 205 项通过，含原有 189 项和新增 16 项。安全表层检查与 `git diff --check` 通过。

### PC-V2.1 可见消息不完整导致基线失败

- 用户真机报告已取得 `VISUAL_MATCH`，且截图、图像变化检测和 OCR 正常。监测显示 `VISIBLE_MESSAGES_INCOMPLETE`、`OBSERVATION_UNRELIABLE`、基线 0，现有报告未定位具体失败的消息。
- 旧服务同时要求 C2 完整性通过和每条消息可比较。低置信旧消息、可能为正文的未归属 OCR、裁切、未分类卡片或区域 OCR 失败可阻断整帧。旧逻辑保留最近观察中的原始消息，但同时使比较基线失效，监测面板也只有汇总数字。正常的消息页与选中行候选状态、已分类时间和系统提示不构成阻断。
- V2.1 保留每条可靠消息及不确定证据，并区分部分基线和可靠基线。部分基线的可信候选要求可定位的早段缺失、旧尾部至少两个不同的可靠锚点、顺序与容器位置一致、稳定采样及独立的新增区域像素变化。尾部缺失、区域 OCR 失败和重叠关系不足只记未确认变化或重同步。原地 OCR 补读只更新基线。
- 面板分别展示当前观察和新增候选，记录基线类型、消息计数、局部或区域缺失原因及未确认变化。绕过分区、快速跃出视口、隐藏滚动条及未识别的复杂卡片仍影响连续性。PC-V2.1 保持真机待验收，视觉候选仅用于只读诊断。[界面识别规范](docs/PC_UI_RECOGNITION_SPEC.md#pc-v21-部分基线与新增判定)
- 本次 Windows 自动化回归 226 项通过、0 项失败，含原有 205 项和新增 21 项。安全表层检查和 `git diff --check` 通过。

### PC-V2.2 文件消息、跨行文件名与诊断复制

- 2026-11-23，用户的 Windows 11 企业微信记录已确认新增普通文本、方向与居中时间识别。左侧 MD 的文件名、`6.6K` 与图标文字独立落入未归属 OCR，右侧 PDF 的多行文件名未组合。当前附件与项目输出均未找到两张原图，自动化复现使用脱敏合成卡片。
- 旧轮廓可能漏掉浅色卡片，方向算法只对容器锚点有效。旧文件规则未覆盖 `.md` 和 `K` 后缀，且只有通用多行文本字段。V2.2 用闭合浅色轮廓恢复卡片、空间唯一归属确定 OCR，再在单一卡片内分离图标、大小和同列连续名称行。截断与冲突保留原始证据。文件方向仍取决于整个卡片与 C2 锚点。
- 文件记录保留独立文件名、扩展名、大小、卡片位置、OCR/锚点证据和字段状态，附件内容保持未读取。成功的文件卡片可以保留在部分基线，居中时间保持独立上下文。连续监测的尾部追加门禁沿用 V2.1。
- 单次视觉结果和监测面板提供摘要及完整明细复制。摘要省略客户身份、文件名和聊天正文。完整明细确认后由 AutoIM 写入 Windows 剪贴板，来源为 `WINDOW_VISUAL_OCR`。文件识别和复制仍待真机复验。[文件规则与验收](docs/PC_UI_RECOGNITION_SPEC.md#pc-v22-文件消息与诊断复制)
- Windows 自动化回归 249 项通过、0 项失败，保留原有 226 项，新增 23 项。安全表层检查和 `git diff --check` 通过。

### PC-V2.3 SQLite 消息持久化

代码已实现，Windows 企业微信独立待验收。数据库默认位于当前用户 `%LOCALAPPDATA%\AutoIM\wecom-visual.sqlite3`，用 `PRAGMA user_version` 顺序执行事务化迁移，目前版本 3。七张表为 `accounts`、`conversations`、`messages`、`attachments`、`message_observations`、`capture_checkpoints` 和 `scan_jobs`。SQLite 和 OCR 都在后台任务运行。数据库保存授权采集的内容，默认无专门的字段加密。

会话以 B2、C1 和可比较的头像证据暂时关联，证据不足只允许当前连续会话的临时关联，视觉 ID 保持本地。首次保存为历史基线，不发新增事件。活跃采集以有序重叠、容器几何、方向、可见时标及像素比较决定原消息关联或尾部插入。观察写入和业务记录分开，重复正文可有多个位置。重启保留消息与未完成任务，恢复时未知连续性写为不确定观察，不无条件插入旧消息。旧版手工别名可在 PC-V3.1 确认绑定时继续使用原本的账号 ID。

### PC-V3.0 多会话自动采集

代码已实现，真实企业微信切换、点击回显、连续多个可见未读与有限翻页待独立验收。默认关闭，用户授权后仅使用前台、未被遮挡且空闲的企业微信窗口。B2 红点和数字独立于 A 区全局数字，静音联系人和群聊参与。点击前固化联系人和未读证据，点击后重新识别选中行、标题、可能出现的 D 区和稳定 C2，事务提交后才标记完成。不确定或失败任务保留可能已读的副作用记录，旧 Driver 的真实复制安全闩独立保留。

同一时间由一个后台任务执行真实窗口采集，当前会话监测、单次识别和自动巡检互斥。可见阶段验收后才启用有限 B2 向下滚动，单次最多四页和 48 次导航尝试，要求稳定列表重叠。C2 和 D 未接入滚动。多会话补读、时标/系统事件、群看板、发送标签和引用字段见 [识别规范](docs/PC_UI_RECOGNITION_SPEC.md#pc-v23--pc-v30-本地数据库与多会话采集)。

Windows 自动化回归 296 项通过、0 项失败，含原有 249 项和新增 47 项。已验证事务、迁移、重启查询、队列与受限鼠标入口；测试中的导航使用合成窗口观察和输入替身。安全表层检查、`git diff --check` 通过。

#### PC-V3.0 窗口状态与自动恢复

旧截图路径用单次 `IsIconic` 早退，错误报告中的矩形来自前一阶段的枚举。两个字段没有同时复核，失焦独立于最小化。旧巡检在收到新的窗口或坐标系后撤销许可，导致窗口重建无法自动续跑。用户给出的两组信号来自不同时间；本次被动实机复查时同一窗口的两次 `IsIconic` 为真，`showCmd=2`，只能确认此次复查为正常最小化。

统一探测用于单次截图、当前监测、自动巡检、导航复核和 GUI 被动刷新。暂停时后台每 3 秒复检；身份或几何变化时作废旧计划、保留已落库未完成任务、清除视觉连续性，再从新截图重建。显式停止后不恢复。同一个授权仍有效时识别新 PID/HWND 并继续巡检，用户仍需手动恢复、最大化并切回前台。Windows 真机采集验收保持待验证。

Windows 回归 320 项通过、0 项失败，含 296 项既有测试和 24 项新增窗口状态/恢复测试。原有窗口更换用例改为验证重新绑定并保留未完成任务。安全表层检查与 `git diff --check` 通过。

### PC-V3.1 企业微信登录账号与采集反馈

`CODE_IMPLEMENTED / PENDING_DEVICE_VERIFICATION`。复用当前窗口截图和本地 OCR 读取 A1 登录显示姓名，连续两次独立截图一致后绑定本地账号。版本 3 为 `accounts` 增加三个视觉绑定字段。唯一的姓名加头像视觉证据可恢复原 ID；同名、改名或旧别名存在歧义时由用户确认。账号入口同时保留独立查看历史账号的能力。头像指纹与本地 ID 均属于视觉归属证据，企业微信官方身份尚未验证。

采集过程提供 B2 扫描、未读、开始导航、打开会话、OCR、SQLite 提交和通知结果。事件墙上时间带时区，耗时取同一次任务的单调时钟；缺失事件的分段留空。现有 `scan_jobs.transitions` 保留阶段证据。新增保存通过现有视觉尾部追加规则确认，事务成功后才调用 Qt 托盘通知。通知可合并且不含正文，系统压制通知时仍可核对本地采集状态。正常延迟 2 秒和暂停复检 3 秒均在 `collection_panel.py` 中配置。[判定及真机验收](docs/PC_UI_RECOGNITION_SPEC.md#pc-v31-登录账号与采集反馈)

Windows 自动化 343 项通过，含原有 320 项和新增 23 项。安全表层检查和 `git diff --check` 通过。真实 Windows 企业微信已验证宽侧栏 A1 姓名识别及临时 SQLite 中的本地绑定复用。真实 A→B 自动导航、消息保存及通知显示仍待验收。

#### PC-V3.1.1 OCR 后 B2 扫描和前台导航分离

首轮实测 OCR 已完成但未读扫描时间为空，原因是 `tick()` 在 B2 校验和 `_scan()` 前调用要求前台的 `verify_view()`。现在可信完整截图及 A1 核对后先完成 B2 OCR 和未读统计，再进入原有导航前台门禁。只读帧仍必须通过窗口状态、完整图像、PID/HWND 及坐标一致性校验。后台发现的未读保留为待导航任务，恢复前台后重新截图核对目标；导航失败时不会点击或清除可信扫描统计。面板区分未扫描、扫描为零、发现可靠未读、发现不可靠未读和等待前台导航，并记录截图、B2 扫描、计数及阻断阶段。全量回归和真机复验结果见识别规范，端到端仍未验收。

后续真机反馈显示两个红色标记因选中状态缺失而无法导航。修复保留独立蓝色高亮证据，并以唯一浅色行底结合可信 C1 与 B2 身份交叉验证。缺少独立证据时保留未知。连续观察使用待复核任务保存次数及原因，相对活动时间变化和列表位移无需新建任务。扫描诊断按轮次显示。真实 A→B 仍待验收。[B2 识别与待复核规则](docs/PC_UI_RECOGNITION_SPEC.md#b2-选中行与未读待复核)

本轮全量回归 361 项通过，含 350 项既有测试和 11 项新增测试。安全表层检查与 `git diff --check` 通过。

### PC-V3.2 主界面、巡检调度与邮箱身份

状态为 `PENDING_DEVICE_VERIFICATION`。主界面提供采集范围、运行统计、历史和任务查询。运行诊断与高级工具默认折叠。范围与 Windows 快捷键通过 QSettings 保存在本机。首次启动自动操作须经本次应用进程授权。F10 或用户配置的组合键可重复启停并撤销后续鼠标动作。

2026-11-24 的只读 Win32 取样在 DPI 168 下确认，企业微信主窗 0x509C6 同 PID 可见辅助窗 `TitleBarWindow` 0x309C2 与客户区上沿重叠，位于主窗上方。1×1 的 `Tencent.WXWork.WedocHostWindow` 0x170C00 也重叠，但在主窗下方。旧条件在该窗口组合下均会误判。另确认 EnumWindows 会透传回调异常。新代码按目标点实际命中的窗口判定，并记录具体 Win32 信息。空闲取样得到最近输入时标变化且无监测按键按住。旧运行中的持续空闲误报未复现。

普通单聊和群聊默认纳入，静音需要显式勾选，未知静音状态保守处理，折叠入口及其他已知特殊入口按策略跳过。按本轮 B2 的可见顺序取目标。单目标风险延后复核，全局阻断等待恢复。无选中行允许进入点击前复核，点击后归属须有选中或独立切换证据。保存前验证当前账号、窗口与 C2。成功采集重置连续未完成动作计数。

SQLite v4 增量保存归一化邮箱、核验时间及窗口生命周期证据，保留旧账号主键及所有关联数据。已确认邮箱是本地账号标识。名称、头像、进程或主窗变化时重新核验。历史无邮箱账号须人工确认。真实个人菜单、资料卡及 A→B 端到端均待授权真机验收。全局未读双击和折叠内部采集递延。

Windows 全量回归 380 项通过，含 361 项既有测试、19 项新增测试。安全表层检查与 Git 空白检查通过。[PC-V3.2 识别规范](docs/PC_UI_RECOGNITION_SPEC.md#pc-v32-日常采集)

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

0.1.2 实现记录（versionCode 3；0.1.3 保留以下消息与检测逻辑）：

- `WeComSnapshotParser` 使用顶部文本、有限祖先工具栏结构、可编辑输入区和相对几何定义消息区；viewId 仅写入 evidence，不参与唯一识别。键盘抬高输入区仍可解析；不固定节点索引、树深度、分辨率或设备型号。
- 可见普通 TextView 正文按 top / left / nodeIndex 排序。标题、输入、Button、时间、系统提示和区域外文字排除；隐藏节点排除。方向综合正文/容器边距与同一行头像位置，冲突或居中且无可靠锚点时为 UNKNOWN；卡片/媒体/多段文本行不作为可靠普通文本。
- ParsedMessage 保留原始正文、节点索引、文字/容器 bounds、观察时间、签名与 evidence；比较文本只统一 CRLF/CR 并 trim 首尾，保留中间空格、数字、标点、型号和特殊字符。
- `WeComMessageDetector` 按会话标题保存最多 20 份内存状态，初次、重启、切换及返回会话只建立基线。使用明确的尾部/头部有序序列匹配：完整旧序列为当前前缀时按 occurrence 检测追加；窗口移动时要求唯一重叠且锚点只在两份序列各出现一次。无重叠、插入历史或歧义时 RESYNC_REQUIRED，不猜测。
- 连续“你好 / 你好”保留出现次数；相同签名不用于全局去重。相同有序内容（含出现次数）忽略布局变化并记录 DUPLICATE_EVENT。可靠新增产生本地 UUID，明确不是企业微信官方 messageId。
- 快照 schema 3 保存 eventType/eventTime/eventPackage/eventClassName、合并事件类型和 visibleToUser；不持有平台 Event/Node 对象。700ms debounce 保留整个事件批次的滚动/窗口证据。
- 滚动批次不发 NEW_MESSAGE；后续内容变化继续重新同步，直到连续可见序列稳定。窗口改变、低置信度、截断、坏结构、UNKNOWN、空窗口无法重叠、坏检测状态和时钟回退安全阻断/重建。滚动保护期间可能漏报真实新增，这是主动 Fail Closed；可手工重建基线再继续测试。
- 主界面增加消息解析与最近检测状态/原因/conversation/messageCount/newMessageCount、正文/方向/localEventId/时间；支持重建当前基线、清空检测日志、复制消息解析诊断。重建后下一份实时快照仍只建立基线，避免 UI 已保存快照变旧。清空日志不破坏去重状态。
- 最近 100 次检测日志仅在内存，进程/服务重建清空；最新原始快照与最近 20 条企微通知继续只存 app private files。通知仅作诊断，不生成 ParsedMessage 或打开会话。原完整树与结构诊断保留并可折叠。

后续 0.1.2 真机反馈见下方修复记录；普通消息、同文重复、滚动和会话切换仍需真机验收。测试通过不代替真机解析验收。

0.1.2 最终构建记录（2026-09-30）：

- 实际运行 `gradlew.bat test`、`gradlew.bat lintDebug`，以及最终 `gradlew.bat test lintDebug assembleDebug`；最终全部任务 BUILD SUCCESSFUL。
- 单元测试共 103 项：既有 41 项 + 新增纯 Kotlin 合成快照/检测测试 62 项；Debug / Release 各 103 项，共 206 次执行，failure/error/skipped 均为 0。覆盖用户要求的全部 20 类场景，并补充键盘抬升、未知 ID、隐藏节点、头像冲突、媒体卡片和重叠歧义。
- `lintDebug` 报告 `No issues found.`。APK 实际存在：`E:\code\AutoIM\AutoIM\android-agent\app\build\outputs\apk\debug\app-debug.apk`，1,098,235 bytes。
- aapt 确认 `com.autoim.agent` / versionName 0.1.2 / versionCode 3 / minSdk 26 / targetSdk 36；apksigner 验证通过（v2）。SHA-256：`4F3F50712D633DF78D55F77BAFC97CAB67C3D05FFEA5AA8AC0FDCB8986DFD6DB`。
- APK permission dump 无 uses-permission；源码/Manifest 未增加 INTERNET、网络客户端、Accessibility 操作/手势、自动点击/输入/发送、Root 或 Hook。
- `adb devices` 无连接设备；未自动安装或宣称完成 0.1.2 真机验收。P-A1.2 保持 IN_PROGRESS；停止于本阶段，不开发 P-A1.3。

0.1.3 标题解析修复（2026-10-01 / versionCode 4）：

- 用户提供的 Redmi Note 11R 0.1.2 真机结果：Accessibility 已工作、Notification Access 已授权；`com.tencent.wework` 快照 80 nodes / depth 17 / textNodeCount 4 / truncated=false，正确读取“崔玮杰”、`下午 7:17`、`CUSTOMER_NEW_001` 和输入提示。解析却得到 title=null / visibleMessageCount=0 / reliable=false，检测器为 BLOCKED_LOW_CONFIDENCE。
- 用户反馈顶部存在多个候选导致 Fail Closed。旧 resolver 要求候选数量恰好为 1，没有先区分主标题、身份 metadata 和状态 decoration，且要求祖先宽度至少屏幕 65%、顶部不低于屏幕 2.5%。旧诊断“会话标题缺失或存在多个候选”合并了零候选和多候选分支，现有反馈不足以还原真机精确候选数；合成测试同时覆盖多候选、窄标题父容器和 top=0 工具栏。
- 已确认业务规则：同一可靠标题组中的 `@微信` 表示个人微信用户 PERSONAL_WECHAT；可靠一对一标题无 `@微信` 表示企业微信用户 ENTERPRISE_WECHAT。标题旁可能有状态图标；ImageView/View、contentDescription 和符号 decoration 不能作为 conversationTitle，也不能改变联系人身份。
- 新增纯 Kotlin `ConversationHeaderResolver`：按相对屏幕几何、父/祖父关系和邻近同行节点形成 TitleGroup，分别保存 Primary Title、Identity Metadata、Status Decoration。排除时间/日期、输入提示、按钮、正文列表和纯符号。候选逐项评分，主标题最低 90 分、与次高分至少相差 12 分；固定 viewId 仅加 3 分辅助证据，不能单独识别或打破同等标题歧义。真正同等主标题、缺少结构或低分仍 Fail Closed。
- 主界面和复制诊断展示标题组数量、主标题候选数、候选 text/index/parent/grandparent/bounds/class/viewId、评分与正负证据、metadata、selectedTitle/peerType/selectedScore/runnerUpScore/header.reliable/confidenceReason。正文/方向流程保留；检测器只修改诊断展示，没有重写 occurrence、去重、滚动、切换或重同步逻辑。
- 新增 40 项合成回归测试，使用用户明确指定的测试标题/正文与几何；不提交真实客户快照。1080×2408 / 80 nodes / depth 17 / 4 text nodes fixture 解析“崔玮杰”及唯一 INBOUND `CUSTOMER_NEW_001`，reliable=true；有/无 `@微信` 分别为 PERSONAL_WECHAT/ENTERPRISE_WECHAT。首次 BASELINE_CREATED、newMessageCount=0；追加 `CUSTOMER_NEW_002` 仅产生一条 INBOUND NEW_MESSAGE。
- 当前等待用户覆盖安装 0.1.3，并按 Android README 的 10 步在同一会话验证标题、身份、基线与新增消息。P-A1.1 保持 DONE / 已验证；P-A1.2 保持 IN_PROGRESS；不开发 P-A1.3。

0.1.3 最终构建记录（2026-10-01）：

- 实际分别运行 `gradlew.bat test`、`gradlew.bat lintDebug`、`gradlew.bat assembleDebug`，均 BUILD SUCCESSFUL。
- 共 143 项单元测试（既有 103 + 新增 40），Debug / Release 各 143 项，共 286 次执行；failure/error/skipped 均为 0。覆盖标题与身份/装饰、时间/日期排除、未知/空 ID、不同分辨率、长名称、歧义低分阻断、解析与检测器集成回归。
- lintDebug：`No issues found.`。APK 实际存在：`E:\code\AutoIM\AutoIM\android-agent\app\build\outputs\apk\debug\app-debug.apk`，1,026,823 bytes。
- aapt 确认 `com.autoim.agent` / versionName 0.1.3 / versionCode 4 / minSdk 26 / targetSdk 36；apksigner v2 验证通过。SHA-256：`7F09930F2B64B16CE1E6203F3F72C3EBAA8FBEC2C780C5FD242BC7E6D6DAC728`。
- APK permission dump 无 uses-permission；源码/Manifest 未增加 INTERNET、网络客户端、自动点击/输入/发送、Root 或 Hook。本机测试和构建通过，不代表已完成 0.1.3 真机验证；P-A1.2 保持 IN_PROGRESS。

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

Windows 当前先验收 SQLite 保存与重启查询，再验收 A 打开时自动采集 B，随后验证多个未读、静音和群聊。通过可见会话巡检后再开启可选 B2 有限滚动。P1.7 真实复制安全闩继续关闭。

Android 当前任务：P-A1.1 保持 DONE / 已验证。P-A1.2 的 0.1.2 真机标题解析失败已反馈；0.1.3 完成 TitleGroup、Primary Title、身份 metadata 和状态 decoration 修复，状态保持 IN_PROGRESS，等待 Redmi Note 11R 安装 0.1.3 验证标题/peerType、首次基线和 CUSTOMER_NEW_002 新消息。只允许离线只读诊断；不进入 P-A1.3，不接服务器或开发自动发送。

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

日期：2026-09-28
需求：修复真机确认的标定显示比例错误；统一 Widget/source/normalized 坐标变换并覆盖 DPR 和留白；在完成重新验收前禁用所有真实点击/复制。
状态：坐标修复开发中，真机回显验收待用户确认
所属模块：企业微信 / automation / vision / 安全
计划阶段：P1.7 安全定位与当前可见消息读取 PoC
备注：已将旧版标定格式作废；仅允许截图、标定回显和 Dry Run，不允许真实鼠标/键盘输入。

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

日期：2026-10-01
需求：根据 Redmi Note 11R 的 0.1.2 标题解析失败，修复 TitleGroup / Primary Title / @微信 identity metadata / status decoration，升级 Android Agent 至 0.1.3 / versionCode 4。
状态：IN_PROGRESS（修复与合成回归完成，等待 0.1.3 真机验证）
所属模块：Android Agent / Snapshot / 会话标题解析与诊断
计划阶段：P-A1.2 企业微信文本消息解析、新消息识别与去重
备注：仅使用用户指定的测试标题、正文和几何构造合成 fixture；不重写检测器序列/滚动/切换/occurrence 逻辑，不开发 P-A1.3；禁止 INTERNET、自动点击/输入/发送、Root、Hook。

日期：2026-11-22
需求：研究 Windows PC 端企业微信自动定位会话、自动选择多条消息、复制完整性核对与安全发送的可行性。
状态：DONE（分析报告完成；未实施自动操作）
所属模块：Windows Agent / WeCom / 安全
计划阶段：P-W2 Windows PC 自动化可行性研究
备注：按用户纠正后的重点，本阶段以“如何读取企业微信会话”为核心，先核实官方会话内容存档接口适用性，再比较 UIA、视觉/OCR 与剪贴板路线。官方 API 入口已记录，但企业开通和授权条件待管理员核实；本报告不实施接口或 PC 自动操作。安全发送依赖唯一会话身份、方向与结果校验，保留在后续独立阶段。报告见 `docs/PC_AUTOMATION_FEASIBILITY.md`。

日期：2026-11-22
需求：落实企业微信 Windows 最大化界面的区域、联系人未读、会话核对和可见消息视觉采集，并按 V1.2 修复首次实机验收中的截图裁切和识别诊断。
状态：DONE / VERIFIED（V1.3 用户验收范围内）
所属模块：Windows Agent / WeCom / 本地视觉采集
计划阶段：PC-V1 企业微信界面视觉识别与直接采集
备注：以直接读取客户区图像为当前主线。用户手动打开测试会话。自动导航、切换、滚动和发送仍待独立研究。规范见 [PC_UI_RECOGNITION_SPEC.md](docs/PC_UI_RECOGNITION_SPEC.md)。

- 日期：2026-11-22
- 需求：持续只读监测用户已打开的企业微信会话，以稳定可见序列识别尾部新增候选。
- 状态：IN_PROGRESS（待 Windows 11 企业微信服务端来信、同文多发和手动视口变化验收）
- 所属模块：Windows Agent / WeCom / 本地视觉监测
- 计划阶段：PC-V2 当前会话持续监测

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
