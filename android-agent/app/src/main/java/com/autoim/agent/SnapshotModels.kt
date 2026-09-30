package com.autoim.agent

data class NodeInput(
    val className: String = "",
    val text: String = "",
    val contentDescription: String = "",
    val viewIdResourceName: String = "",
    val boundsInScreen: String = "",
    val clickable: Boolean = false,
    val longClickable: Boolean = false,
    val focusable: Boolean = false,
    val focused: Boolean = false,
    val editable: Boolean = false,
    val scrollable: Boolean = false,
    val enabled: Boolean = false,
    val selected: Boolean = false,
    val password: Boolean = false,
    val children: List<NodeInput?> = emptyList(),
    val capped: Boolean = false,
    val reportedChildCount: Int? = null,
    val visibleToUser: Boolean = true,
)

data class NodeSnapshot(
    val depth: Int,
    val index: Int,
    val className: String,
    val text: String,
    val contentDescription: String,
    val viewIdResourceName: String,
    val boundsInScreen: String,
    val clickable: Boolean,
    val longClickable: Boolean,
    val focusable: Boolean,
    val focused: Boolean,
    val editable: Boolean,
    val scrollable: Boolean,
    val enabled: Boolean,
    val selected: Boolean,
    val password: Boolean,
    val childCount: Int,
    val nodeIndex: Int = -1,
    val parentIndex: Int? = null,
    val childIndex: Int = index,
    val visibleToUser: Boolean = true,
)

data class AccessibilitySnapshot(
    val captureTime: Long,
    val packageName: String,
    val rootClass: String,
    val nodeCount: Int,
    val maxDepth: Int,
    val textNodeCount: Int,
    val clickableNodeCount: Int,
    val editableNodeCount: Int,
    val scrollableNodeCount: Int,
    val truncated: Boolean,
    val nodes: List<NodeSnapshot>,
    val screenWidth: Int = 0,
    val screenHeight: Int = 0,
    val screenMetricsSource: String = "unknown",
    val trigger: SnapshotTrigger = SnapshotTrigger(),
)

/** Copied values only; eventTime is Android uptime, captureTime is wall-clock time. */
data class SnapshotTrigger(
    val eventType: Int = 0,
    val eventTime: Long = 0,
    val eventPackage: String = "",
    val eventClassName: String = "",
    val coalescedEventTypes: Set<Int> = emptySet(),
) {
    val includesScroll get() = eventType == VIEW_SCROLLED || VIEW_SCROLLED in coalescedEventTypes
    val includesWindowChange get() = eventType == WINDOW_STATE_CHANGED || WINDOW_STATE_CHANGED in coalescedEventTypes
    companion object {
        const val VIEW_SCROLLED = 4096
        const val WINDOW_STATE_CHANGED = 32
        const val WINDOW_CONTENT_CHANGED = 2048
    }
}

data class NotificationRecord(
    val packageName: String,
    val postTime: Long,
    val notificationId: Int,
    val notificationKey: String,
    val title: String,
    val text: String,
    val subText: String,
    val bigText: String,
    val category: String,
)

object DiagnosticLogic {
    const val WECOM_PACKAGE = "com.tencent.wework"
    const val MAX_DEPTH = 30
    const val MAX_NODES = 3000
    const val MAX_TEXT_LENGTH = 1000
    const val MAX_NOTIFICATIONS = 20

    fun shouldCapture(diagnosticEnabled: Boolean, eventPackage: String?): Boolean =
        diagnosticEnabled && eventPackage == WECOM_PACKAGE

    fun buildSnapshot(root: NodeInput?, rootPackage: String?, captureTime: Long = System.currentTimeMillis(),
        screenWidth: Int = 0, screenHeight: Int = 0, screenMetricsSource: String = "unknown",
        trigger: SnapshotTrigger = SnapshotTrigger()): AccessibilitySnapshot? {
        if (root == null || rootPackage != WECOM_PACKAGE) return null
        val nodes = mutableListOf<NodeSnapshot>()
        var truncated = false
        fun visit(node: NodeInput?, depth: Int, index: Int, parentIndex: Int?) {
            if (node == null) return
            if (node.capped) truncated = true
            if (nodes.size >= MAX_NODES || depth > MAX_DEPTH) { truncated = true; return }
            fun bounded(value: String): String {
                if (value.length > MAX_TEXT_LENGTH) truncated = true
                return value.take(MAX_TEXT_LENGTH)
            }
            val nodeIndex = nodes.size
            nodes += NodeSnapshot(depth, index, node.className, bounded(node.text), bounded(node.contentDescription),
                node.viewIdResourceName, node.boundsInScreen, node.clickable, node.longClickable, node.focusable,
                node.focused, node.editable, node.scrollable, node.enabled, node.selected, node.password,
                node.reportedChildCount ?: node.children.size, nodeIndex, parentIndex, index, node.visibleToUser)
            if (depth == MAX_DEPTH && node.children.any { it != null }) { truncated = true; return }
            node.children.forEachIndexed { childIndex, child ->
                if (nodes.size >= MAX_NODES) { truncated = true; return@forEachIndexed }
                visit(child, depth + 1, childIndex, nodeIndex)
            }
        }
        visit(root, 0, 0, null)
        return AccessibilitySnapshot(captureTime, WECOM_PACKAGE, root.className, nodes.size,
            nodes.maxOfOrNull { it.depth } ?: 0,
            nodes.count { it.text.isNotBlank() || it.contentDescription.isNotBlank() },
            nodes.count { it.clickable }, nodes.count { it.editable }, nodes.count { it.scrollable }, truncated, nodes.toList(),
            screenWidth.coerceAtLeast(0), screenHeight.coerceAtLeast(0), screenMetricsSource, trigger)
    }

    fun visibleText(nodes: List<NodeSnapshot>): List<String> {
        val seen = linkedSetOf<String>()
        nodes.forEach { node ->
            listOf(node.text, node.contentDescription).map(String::trim).filter(String::isNotEmpty).forEach(seen::add)
        }
        return seen.toList()
    }

    fun acceptsNotification(packageName: String?): Boolean = packageName == WECOM_PACKAGE

    fun boundedNotifications(records: List<NotificationRecord>): List<NotificationRecord> =
        records.filter { acceptsNotification(it.packageName) }.takeLast(MAX_NOTIFICATIONS)
}
