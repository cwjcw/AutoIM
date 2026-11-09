# AutoIM Android Agent 0.1.3

An offline diagnostic APK using Android `AccessibilityService` and `NotificationListenerService`. Redmi Note 11R has verified **298 nodes, depth 18, actual message text, title, left/right layouts, editable input, and a standard send Button**. P-A1.1 is DONE / verified. P-A1.2 adds ordinary one-to-one text parsing and local occurrence-aware new-message detection. After 0.1.2 failed title resolution on the phone, 0.1.3 separates title groups, primary titles, identity metadata and status decorations; it stays IN_PROGRESS pending 0.1.3 device verification. It never clicks, types, sends, or connects to a network service. No P-A1.3 implementation is included.

Current version: **0.1.3 / versionCode 4**, package `com.autoim.agent`. Upgrade using the normal APK installer or `adb install -r` with the same debug signature. No automatic installation or permission grants are performed.

## Build on Windows

Requirements: JDK 17 and Android SDK command-line tools with Android SDK Platform 36 and Build Tools 35.0.0 installed. The project uses AGP 8.13.2, Kotlin 2.4.20, Gradle 8.14.5, `minSdk 26`, and `targetSdk 36`. Android Studio is not required.

From the repository root:

```powershell
cd android-agent
.\gradlew.bat test
.\gradlew.bat lintDebug
.\gradlew.bat assembleDebug
```

The APK is generated at:

```text
android-agent\app\build\outputs\apk\debug\app-debug.apk
```

To use a phone connected by USB with developer debugging enabled:

```powershell
adb devices
adb install -r .\app\build\outputs\apk\debug\app-debug.apk
```

For logs during diagnosis:

```powershell
adb logcat
```

## Redmi / MIUI manual setup

1. Install the APK and open **AutoIM Agent**.
2. Tap **打开无障碍设置**, choose **AutoIM 企业微信诊断**, and enable it yourself.
3. Tap **打开通知访问设置** and enable **AutoIM 企业微信通知诊断** yourself.
4. Return to AutoIM Agent and tap **开始企业微信诊断**.
5. Open WeCom and navigate manually to a chat. Scroll or change the visible page to produce accessibility events, then return to AutoIM Agent and tap **查看最后企微快照**.
6. The main page now shows **消息解析诊断** and **新消息检测**, plus **重建当前会话基线**, **清空消息检测日志**, and **复制消息解析诊断**. The saved-snapshot page expands message parsing/detection by default; original **可见文本**, **关键文本节点**, **输入节点候选**, **发送节点候选**, **Scrollable候选**, **顶部文本候选**, and **完整 Node Tree** remain available and collapsed.
7. To test notifications, keep notification access enabled and cause a WeCom notification to arrive. Return to the app and inspect **最近企业微信通知**.
8. Use **清空诊断数据** when finished. Private files keep the latest snapshot and 20 latest WeCom notifications; detection logs keep at most 100 results in memory. Clearing detection logs preserves deduplication; clearing all diagnostics resets it. Process/service restart forgets detection baselines and starts fresh.

Some MIUI versions show **受限设置 / 允许受限设置** for sideloaded apps. Open the Android app-info page through normal system settings, use the system's **允许受限设置** control if it is offered, then enable the service in Android settings. Do not use ADB permission grants, root, or other authorization bypasses.

The diagnostic toggle only enables snapshot collection after a `com.tencent.wework` event. The Accessibility service filters to that package, checks the event package and active root package, waits 700 ms after events, and caps the copied tree at depth 30 and 3,000 nodes. Notification content is stored only for `com.tencent.wework`. Neither service acts on the WeCom UI.

## Private data files

- `files/latest-accessibility-snapshot.json`
- `files/wecom-notifications.json`

These paths are private to the app sandbox; no public storage permission or database is used. Malformed diagnostic files are ignored safely. Parsed messages, per-conversation detection state (up to 20 titles), local UUID events and the latest 100 detector results are memory only. Clipboard export occurs only when the user taps copy.

## P-A1.2 parsing and safe detection

- TitleGroup uses relative top geometry, ancestry and nearby row relationships; Primary Title scores structure and position without requiring a screen-wide toolbar. A fixed viewId is only a small secondary hint. `@微信` is personal-WeChat identity metadata; status icons/descriptions are decorations, never titles. A reliable one-to-one title without that badge is ENTERPRISE_WECHAT. A score below 90 or margin below 12 blocks detection. Detailed candidate/group evidence remains visible on success and failure. An editable input defines the lower boundary; a raised keyboard input is supported.
- Only visible text inside this region is parsed. Original bodies remain unchanged. Message tokens use direction and text with CRLF/CR unified to LF and only leading/trailing whitespace trimmed. Numbers, punctuation, models, special characters and internal whitespace remain significant.
- Direction combines text/bubble margins with optional avatar anchors in a bounded ancestor row. Conflicting or weak evidence gives UNKNOWN, which blocks NEW_MESSAGE. Media, cards, quotes and group sender parsing are outside this stage.
- Messages sort by visual top, then left and node index. Exact ordered tokens and occurrence counts suppress duplicate events despite geometry changes. A complete previous prefix detects appended occurrences, including `你好` followed by another `你好`. A shifted visible window requires a unique suffix/prefix overlap with unique anchors in both sequences. Ambiguity, interior changes and no overlap resync without emitting history.
- First observation, process/service restart, conversation switch (including returning to an earlier chat) and manual baseline rebuild emit no new messages. A local event has a UUID; it is not a WeCom official message ID. The content signature is not a unique message ID or a global deduplication key.
- Snapshot schema 3 copies event type/time/package/class, coalesced event types and node visibility. Debouncing retains scroll evidence even if content-change events follow it. Scroll-triggered batches emit no messages, and subsequent changing sequences stay in resync until an unchanged snapshot confirms stability. A reliable window-state capture rebuilds the current baseline without events. Detection requires WeCom content-change events thereafter.
- Fail Closed may miss a real message while scrolling/resyncing, when WeCom automatically scrolls, when the visible window contains unsupported/UNKNOWN text, or when a snapshot is truncated. It never guesses that newly exposed history is new. Manual baseline rebuild is available, and the next live capture also baselines to avoid stale UI data.
- Notification content remains auxiliary diagnostics only and never creates ParsedMessage, opens a chat or operates WeCom.

## P-A1.1 saved-snapshot structure diagnostics (retained)

Introduced in **0.1.1 / versionCode 2**, retained in 0.1.3. P-A1.1 is now **DONE / 已验证** from the user's Redmi Note 11R structural evidence; internal IDs are secondary hints only.

- Each node retains the original properties and now includes unique preorder `nodeIndex`, nullable `parentIndex`, `depth`, `childIndex`, and the original reported `childCount`. `index` remains the legacy sibling index. No platform node objects are persisted.
- Key text diagnostics show self, parent, grandparent, and adjacent siblings. Missing relatives/slots are explicitly `null`; no inferred sender roles or hardcoded internal View IDs.
- Screen dimensions are saved at capture in the same pixel coordinate system as `boundsInScreen`. `centerX < 40%` means LEFT, `> 60%` means RIGHT, otherwise CENTER. These are geometry only, never message direction. Invalid bounds/dimensions show `null`.
- Input candidates: editable or class containing EditText. Send candidates: text/description containing “发送”, with clickable properties of ancestors. Scrollable candidates: scrollable true. Top text candidates: nonempty text with the entire bounds inside the top 20% of the captured screen. All are candidates for human inspection.
- 0.1.0 snapshots remain readable: structure is restored from their preorder depth and sibling index. They did not store screen dimensions, so geometry is unavailable until a new snapshot is captured. Invalid structure is ignored safely.
- **按文本查节点** searches text and description (substring, case-insensitive) in the snapshot loaded when the page opened. It does not query WeCom. Return and reopen to load the latest saved snapshot.
- **复制结构诊断** includes device info, WeCom version, snapshot summary and candidate sections, excluding the full tree. **单独复制完整 Node Tree** exports the original full structure separately. Clipboard exports contain chat text; copy/share only when appropriate. No automatic export or network upload occurs.
- JSON reading, report generation and searches run on a worker thread. The tree display is created only when expanded. Notification retention remains unchanged; capture now additionally drives the offline parser/detector.

### 下一轮 Redmi Note 11R 真机测试（0.1.3）

本轮只验证标题修复、首次基线和新增消息。所有安装、进入会话和发送均由用户手工操作。

1. 覆盖安装 0.1.3。
2. 检查 Accessibility 已连接 / 已工作，Notification Access 已授权。
3. 开启企微诊断。
4. 进入同一个“崔玮杰”测试会话。
5. 等待约 2 秒。
6. 回 AutoIM 查看消息解析诊断，期望 conversationTitle=崔玮杰、reliable=true；有 `@微信` 时 peerType=PERSONAL_WECHAT，无时为 ENTERPRISE_WECHAT。
7. 第一次进入只允许 BASELINE_CREATED，newMessageCount=0。
8. 让对方发送 `CUSTOMER_NEW_002`。
9. 等待约 2 秒后回 AutoIM，期望 NEW_MESSAGE、direction=INBOUND、content=CUSTOMER_NEW_002、newMessageCount=1。
10. 不继续开发 P-A1.3，完成后停止。

P-A1.2 保持 **IN_PROGRESS**，等待本轮真机结果；同文重复、滚动和切换的既有验收要求仍保留在 PROJECT.md。失败时 **复制消息解析诊断** 可提供标题组、候选评分与排除原因。

## 0.1.1 local build checks (2026-09-30)

- `gradlew.bat test`: BUILD SUCCESSFUL; 41 tests per Debug/Release variant (82 executions), zero failures/errors/skips.
- `gradlew.bat lintDebug`: BUILD SUCCESSFUL; zero issues.
- `gradlew.bat assembleDebug`: BUILD SUCCESSFUL; debug APK 970,916 bytes; v2 signature verified.
- Source/manifest checks found no action/gesture/network APIs or uses-permission entries. No Android device was attached for this version's manual UI acceptance; use the A–E checklist above.

## 0.1.2 local build checks (2026-09-30)

- Actual `gradlew.bat test` and `gradlew.bat lintDebug` runs succeeded, followed by final `gradlew.bat test lintDebug assembleDebug`: BUILD SUCCESSFUL.
- 103 tests per Debug/Release variant (41 existing + 62 new synthetic parsing/detection tests), 206 executions; zero failures/errors/skips. Includes all 20 requested scenarios.
- lintDebug: `No issues found.`
- APK exists at `E:\code\AutoIM\AutoIM\android-agent\app\build\outputs\apk\debug\app-debug.apk`, **1,098,235 bytes**. aapt confirms versionName 0.1.2 / versionCode 3 / package com.autoim.agent / minSdk 26 / targetSdk 36. apksigner v2 verification passes.
- APK permission dump contains no uses-permission. Source checks found no action/gesture/network APIs. No connected Android device was available; 0.1.2 manual acceptance is still pending.
- SHA-256: `4F3F50712D633DF78D55F77BAFC97CAB67C3D05FFEA5AA8AC0FDCB8986DFD6DB`.

## 0.1.3 local build checks (2026-10-01)

- Separately ran `gradlew.bat test`, `gradlew.bat lintDebug`, and `gradlew.bat assembleDebug`: all BUILD SUCCESSFUL; lint reports `No issues found.`
- 143 tests per Debug/Release variant (103 existing + 40 new header/integration tests), 286 executions; zero failures/errors/skips. Synthetic fixture resolves 崔玮杰 / CUSTOMER_NEW_001 reliably, baselines with zero new messages, then emits only INBOUND CUSTOMER_NEW_002 on append.
- APK: `E:\code\AutoIM\AutoIM\android-agent\app\build\outputs\apk\debug\app-debug.apk`, **1,026,823 bytes**; aapt confirms 0.1.3 / versionCode 4, minSdk 26, targetSdk 36. apksigner v2 verification passes. No uses-permission entries or action/network APIs were added.
- SHA-256: `7F09930F2B64B16CE1E6203F3F72C3EBAA8FBEC2C780C5FD242BC7E6D6DAC728`. P-A1.2 remains IN_PROGRESS pending the 0.1.3 phone checklist above.
