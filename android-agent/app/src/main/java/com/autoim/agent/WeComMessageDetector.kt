package com.autoim.agent

import java.util.UUID

enum class MessageDetectionStatus {
    BASELINE_CREATED, NO_CHANGE, NEW_MESSAGE, DUPLICATE_EVENT, CONVERSATION_CHANGED,
    SCROLL_ONLY, RESYNC_REQUIRED, BLOCKED_LOW_CONFIDENCE,
}

data class DetectedMessageEvent(
    val localEventId: String,
    val conversationTitle: String,
    val direction: MessageDirection,
    val messageType: MessageType,
    val content: String,
    val observedAt: Long,
    val contentSignature: String,
)

data class MessageDetectionResult(
    val status: MessageDetectionStatus,
    val reason: String,
    val conversationTitle: String?,
    val messageCount: Int,
    val newMessages: List<DetectedMessageEvent>,
    val observedAt: Long,
)

data class MessageDetectionState(
    val conversationTitle: String,
    val previousVisibleMessages: List<MessageToken>,
    val lastUpdateTime: Long,
    val resyncPending: Boolean = false,
)

/** In-memory, occurrence-aware suffix/prefix sequence matching. No global signature set. */
class WeComMessageDetector {
    private val states = linkedMapOf<String, MessageDetectionState>()
    private var activeTitle: String? = null

    fun reset() { states.clear(); activeTitle = null }

    /** A damaged/restored state can only become a baseline; never emit historical events. */
    fun restoreState(state: MessageDetectionState) {
        states[state.conversationTitle] = state.copy(resyncPending = true)
        activeTitle = state.conversationTitle
    }

    fun detect(snapshot: ParsedConversationSnapshot): MessageDetectionResult {
        val title = snapshot.conversationTitle
        val tokens = snapshot.messages.map { it.token }
        fun result(status: MessageDetectionStatus, reason: String, additions: List<ParsedMessage> = emptyList()) =
            MessageDetectionResult(status, reason, title, snapshot.messages.size, additions.map {
                DetectedMessageEvent(UUID.randomUUID().toString(), title!!, it.direction, it.messageType,
                    it.content, it.observedAt, it.contentSignature)
            }, snapshot.observedAt)
        if (title.isNullOrBlank() || !snapshot.reliable || snapshot.messageRegion == null ||
            tokens.any { it.direction == MessageDirection.UNKNOWN } ||
            snapshot.messages.any { it.conversationTitle != title } ||
            snapshot.trigger.eventPackage != DiagnosticLogic.WECOM_PACKAGE) {
            // Invalidate continuity across a missing/ambiguous page, even if the title later returns.
            states.clear(); activeTitle = null
            return result(MessageDetectionStatus.BLOCKED_LOW_CONFIDENCE,
                snapshot.reasons.joinToString("；").ifBlank { "标题/方向/区域/触发包名不可靠" })
        }
        fun baseline(pending: Boolean = false) {
            states[title] = MessageDetectionState(title, tokens.toList(), snapshot.observedAt, pending)
            activeTitle = title
            while (states.size > 20) states.remove(states.keys.first())
        }
        val previous = states[title]
        val changed = activeTitle != null && activeTitle != title
        if (changed || previous == null) {
            baseline(snapshot.trigger.includesScroll)
            return result(if (changed) MessageDetectionStatus.CONVERSATION_CHANGED else MessageDetectionStatus.BASELINE_CREATED,
                if (changed) "会话切换；重新建立基线，不回放历史" else "首次观察；只建立基线")
        }
        if (snapshot.trigger.includesScroll) {
            baseline(true)
            return result(MessageDetectionStatus.SCROLL_ONLY, "滚动或合并事件含滚动；重新同步，禁止新增事件")
        }
        if (snapshot.trigger.includesWindowChange) {
            baseline()
            return result(MessageDetectionStatus.RESYNC_REQUIRED, "窗口切换；当前可靠快照重新建立基线")
        }
        if (previous.conversationTitle != title || previous.lastUpdateTime < 0 ||
            previous.previousVisibleMessages.any { it.direction == MessageDirection.UNKNOWN || it.normalizedText.isBlank() }) {
            baseline()
            return result(MessageDetectionStatus.RESYNC_REQUIRED, "检测状态损坏；安全重建基线")
        }
        if (snapshot.observedAt < previous.lastUpdateTime) {
            baseline(true)
            return result(MessageDetectionStatus.RESYNC_REQUIRED, "旧快照或时钟回退；安全重新同步")
        }
        val old = previous.previousVisibleMessages
        if (previous.resyncPending) {
            // Trailing content-changed events from scrolling cannot manufacture a new append.
            baseline(tokens != old)
            return result(MessageDetectionStatus.RESYNC_REQUIRED,
                if (tokens == old) "可见序列已稳定；基线就绪" else "滚动/窗口后的序列仍变化；继续重新同步")
        }
        if (tokens == old) {
            baseline()
            return result(MessageDetectionStatus.DUPLICATE_EVENT, "有序内容与出现次数相同（忽略布局变化）")
        }
        if (snapshot.trigger.eventType != SnapshotTrigger.WINDOW_CONTENT_CHANGED) {
            baseline()
            return result(MessageDetectionStatus.RESYNC_REQUIRED, "非内容变化触发；只同步基线")
        }
        if (old.isEmpty() || tokens.isEmpty()) {
            baseline()
            return result(MessageDetectionStatus.RESYNC_REQUIRED, "空窗口之间无法建立可靠重叠")
        }
        // Full previous prefix proves appending, including consecutive identical messages.
        val fullPrefix = tokens.size >= old.size && tokens.take(old.size) == old
        val overlaps = (1..minOf(old.size, tokens.size)).filter { old.takeLast(it) == tokens.take(it) }
        val overlap = if (fullPrefix) old.size else overlaps.singleOrNull()
        fun occurrences(sequence: List<MessageToken>, fragment: List<MessageToken>) =
            (0..sequence.size - fragment.size).count { start ->
                fragment.indices.all { sequence[start + it] == fragment[it] }
            }
        val ambiguousAnchor = !fullPrefix && overlap != null &&
            (occurrences(old, old.takeLast(overlap)) != 1 || occurrences(tokens, tokens.take(overlap)) != 1)
        if (overlap == null || ambiguousAnchor) {
            baseline()
            return result(MessageDetectionStatus.RESYNC_REQUIRED,
                if (overlaps.isEmpty()) "无可靠尾部/头部重叠；重建基线" else "重复序列有多种对齐；重建基线")
        }
        val additions = snapshot.messages.drop(overlap)
        baseline()
        return result(if (additions.isEmpty()) MessageDetectionStatus.NO_CHANGE else MessageDetectionStatus.NEW_MESSAGE,
            "有序序列重叠 $overlap 条；新增 ${additions.size} 个 occurrence", additions)
    }
}

/** Shared by service and UI, bounded memory only. Reading diagnostics never runs detection. */
object MessageDiagnostics {
    private val parser = WeComSnapshotParser()
    private val detector = WeComMessageDetector()
    private var parsed: ParsedConversationSnapshot? = null
    private val history = ArrayDeque<MessageDetectionResult>()

    @Synchronized fun startSession() { detector.reset(); parsed = null; history.clear() }
    @Synchronized fun invalidateContinuity() { detector.reset() }
    @Synchronized fun accept(snapshot: AccessibilitySnapshot) {
        parsed = parser.parse(snapshot)
        history.addLast(detector.detect(parsed!!))
        while (history.size > 100) history.removeFirst()
    }
    @Synchronized fun rebuildBaseline() {
        detector.reset()
        parsed?.let {
            history.addLast(detector.detect(it))
            // The next live snapshot must baseline again; saved UI data may have gone stale.
            detector.reset()
        }
        while (history.size > 100) history.removeFirst()
    }
    @Synchronized fun clearLog() { history.clear() }
    @Synchronized fun view(): MessageDiagnosticView = MessageDiagnosticView(parsed, history.toList())
}

data class MessageDiagnosticView(val parsed: ParsedConversationSnapshot?, val history: List<MessageDetectionResult>)

object MessageDiagnosticReport {
    fun parsed(snapshot: ParsedConversationSnapshot?): String = snapshot?.let {
        "当前会话：${it.conversationTitle ?: "无法可靠确定"}\n可见文本消息：${it.messages.size}\n" +
            "可靠：${it.reliable}\n原因：${it.reasons.joinToString("；")}\n区域：${it.messageRegion}\n" +
            "标题证据：${it.titleEvidence.joinToString("；")}\n触发事件：${it.trigger}\n\n" +
            it.messages.joinToString("\n\n") { m ->
                "[${m.direction}]\n${m.content}\nnode=${m.sourceNodeIndex}\ntextBounds=${m.textBounds}\n" +
                    "containerBounds=${m.containerBounds}\nobservedAt=${m.observedAt}\n" +
                    "contentSignature=${m.contentSignature}\nevidence=${m.evidence.joinToString("；")}"
            }
    } ?: "暂无本次运行解析结果。开启诊断后手工打开企微会话；重启后首次快照只建立基线。"

    fun detection(history: List<MessageDetectionResult>): String = history.asReversed().joinToString("\n\n") { r ->
        "${r.status} | 时间=${r.observedAt}\n原因=${r.reason}\nconversation=${r.conversationTitle}\n" +
            "messageCount=${r.messageCount}; newMessageCount=${r.newMessages.size}\n" +
            r.newMessages.joinToString("\n\n") { e ->
                "[${e.direction}] ${e.messageType}\n${e.content}\nlocalEventId=${e.localEventId}\n时间=${e.observedAt}"
            }
    }.ifBlank { "暂无检测日志（仅本次运行，最多 100 次）。" }

    fun export(view: MessageDiagnosticView) = "AutoIM Agent 0.1.2 消息解析诊断\n" +
        "localEventId 是本地 UUID，并非企业微信 messageId。\n\n【消息解析诊断】\n${parsed(view.parsed)}\n\n" +
        "【新消息检测】\n${detection(view.history)}"
}
