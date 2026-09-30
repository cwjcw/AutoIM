package com.autoim.agent

/** Pure parsing of immutable saved snapshots. No platform nodes or action APIs. */
data class SnapshotBounds(val left: Int, val top: Int, val right: Int, val bottom: Int) {
    val centerX get() = (left.toDouble() + right) / 2

    companion object {
        private val pattern = Regex("\\[(-?\\d+),(-?\\d+)]\\[(-?\\d+),(-?\\d+)]")
        fun parse(value: String): SnapshotBounds? {
            val match = pattern.matchEntire(value) ?: return null
            val coordinates = match.groupValues.drop(1).map { it.toIntOrNull() ?: return null }
            return SnapshotBounds(coordinates[0], coordinates[1], coordinates[2], coordinates[3])
                .takeIf { it.right > it.left && it.bottom > it.top }
        }
    }
}

enum class ScreenPosition { LEFT, CENTER, RIGHT }

data class NodeDiagnostic(
    val node: NodeSnapshot,
    val screenPosition: ScreenPosition?,
    val parent: NodeSnapshot?,
    val grandparent: NodeSnapshot?,
    val previousSibling: NodeSnapshot?,
    val nextSibling: NodeSnapshot?,
)

class StructureDiagnostics(val snapshot: AccessibilitySnapshot) {
    private val byIndex = snapshot.nodes.associateBy { it.nodeIndex }
    private val children = snapshot.nodes.filter { it.parentIndex != null }
        .groupBy { it.parentIndex }.mapValues { (_, nodes) -> nodes.associateBy { it.childIndex } }

    private fun parentOf(node: NodeSnapshot): NodeSnapshot? = node.parentIndex?.let(byIndex::get)
        ?.takeIf { it.nodeIndex >= 0 && it.nodeIndex < node.nodeIndex && it.depth == node.depth - 1 }

    fun screenPosition(node: NodeSnapshot): ScreenPosition? {
        if (snapshot.screenWidth <= 0) return null
        val bounds = SnapshotBounds.parse(node.boundsInScreen) ?: return null
        if (bounds.left < 0 || bounds.right > snapshot.screenWidth) return null
        return when {
            bounds.centerX < snapshot.screenWidth * 0.40 -> ScreenPosition.LEFT
            bounds.centerX > snapshot.screenWidth * 0.60 -> ScreenPosition.RIGHT
            else -> ScreenPosition.CENTER
        }
    }

    fun describe(node: NodeSnapshot): NodeDiagnostic {
        val parent = parentOf(node)
        val siblings = parent?.let { children[it.nodeIndex] }
        // An inaccessible/missing sibling slot is null, never replaced by a nearby node.
        return NodeDiagnostic(node, screenPosition(node), parent, parent?.let(::parentOf),
            siblings?.get(node.childIndex - 1), siblings?.get(node.childIndex + 1))
    }

    val keyTextNodes get() = snapshot.nodes.filter { it.text.isNotBlank() || it.contentDescription.isNotBlank() }
    val inputCandidates get() = snapshot.nodes.filter { it.editable || it.className.contains("EditText") }
    val sendCandidates get() = snapshot.nodes.filter { it.text.contains("发送") || it.contentDescription.contains("发送") }
    val scrollableCandidates get() = snapshot.nodes.filter { it.scrollable }
    val topTextCandidates get() = snapshot.nodes.filter { node ->
        val bounds = SnapshotBounds.parse(node.boundsInScreen)
        node.text.isNotBlank() && snapshot.screenHeight > 0 && bounds != null &&
            bounds.top >= 0 && bounds.bottom <= snapshot.screenHeight * 0.20
    }

    fun search(query: String): List<NodeDiagnostic> {
        val term = query.trim()
        if (term.isEmpty()) return emptyList()
        return snapshot.nodes.filter { it.text.contains(term, ignoreCase = true) ||
            it.contentDescription.contains(term, ignoreCase = true) }.map(::describe)
    }
}

/** Shared renderer for the on-device sections and clipboard export. */
object StructureReport {
    fun summary(snapshot: AccessibilitySnapshot) =
        "时间：${snapshot.captureTime}\npackage：${snapshot.packageName}\nrootClass：${snapshot.rootClass}\n" +
            "节点：${snapshot.nodeCount} / 深度：${snapshot.maxDepth} / 含文本：${snapshot.textNodeCount}\n" +
            "clickable：${snapshot.clickableNodeCount} / editable：${snapshot.editableNodeCount} / scrollable：${snapshot.scrollableNodeCount}\n" +
            "截断：${snapshot.truncated}\n屏幕：${snapshot.screenWidth} × ${snapshot.screenHeight} (${snapshot.screenMetricsSource})\n" +
            "LEFT/CENTER/RIGHT 仅表示几何位置；标题、输入、发送、滚动区域均为待人工确认的候选。" +
            if (snapshot.screenWidth <= 0 || snapshot.screenHeight <= 0)
                "\n旧快照或屏幕尺寸不可用：几何位置显示 null，顶部候选不计算。请重新采集。" else ""

    private fun relative(node: NodeSnapshot?): String = node?.let {
        "index=${it.nodeIndex}\nclass=${it.className}\nbounds=${it.boundsInScreen}\nclickable=${it.clickable}\nchildCount=${it.childCount}"
    } ?: "null"

    private fun sibling(node: NodeSnapshot?): String = node?.let {
        "index=${it.nodeIndex}\nclass=${it.className}\ntext=${it.text}\ndesc=${it.contentDescription}\nbounds=${it.boundsInScreen}"
    } ?: "null"

    fun detail(record: NodeDiagnostic): String = with(record) {
        "TEXT:\n${node.text}\nDESC:\n${node.contentDescription}\n" +
            "nodeIndex=${node.nodeIndex}\nparentIndex=${node.parentIndex}\ndepth=${node.depth}\nchildIndex=${node.childIndex}\nchildCount=${node.childCount}\n" +
            "class=${node.className}\nbounds=${node.boundsInScreen}\nscreenPosition=${screenPosition ?: "null"}\n" +
            "clickable=${node.clickable}\nlongClickable=${node.longClickable}\nfocusable=${node.focusable}\nfocused=${node.focused}\n" +
            "editable=${node.editable}\nscrollable=${node.scrollable}\nenabled=${node.enabled}\nselected=${node.selected}\npassword=${node.password}\nviewId=${node.viewIdResourceName}\n\n" +
            "PARENT:\n${relative(parent)}\n\nGRANDPARENT:\n${relative(grandparent)}\n\n" +
            "PREVIOUS_SIBLING:\n${sibling(previousSibling)}\n\nNEXT_SIBLING:\n${sibling(nextSibling)}"
    }

    fun details(records: List<NodeDiagnostic>, emptyMessage: String = "未发现候选") =
        records.joinToString("\n\n--------------------------------\n\n", transform = ::detail).ifBlank { emptyMessage }

    fun sections(diagnostics: StructureDiagnostics): List<Pair<String, String>> = with(diagnostics) {
        listOf(
            "可见文本" to DiagnosticLogic.visibleText(snapshot.nodes).joinToString("\n").ifBlank { "未发现可见文本" },
            "关键文本节点" to details(keyTextNodes.map(::describe), "未发现关键文本节点"),
            "输入节点候选" to details(inputCandidates.map(::describe), "未发现输入节点候选"),
            "发送节点候选" to details(sendCandidates.map(::describe), "未发现发送节点候选"),
            "Scrollable候选" to scrollableCandidates.joinToString("\n\n") {
                "index=${it.nodeIndex}\nclass=${it.className}\nbounds=${it.boundsInScreen}\nchildCount=${it.childCount}\ndepth=${it.depth}"
            }.ifBlank { "未发现 Scrollable 候选" },
            "顶部文本候选" to details(topTextCandidates.map(::describe), "未发现顶部文本候选（须有快照屏幕尺寸）"),
        )
    }

    fun export(diagnostics: StructureDiagnostics, deviceInfo: String, wecomInfo: String): String =
        "AutoIM Android Agent 0.1.2 结构诊断\n\n设备信息：\n$deviceInfo\n\n企业微信版本：\n$wecomInfo\n\n" +
            "Snapshot摘要：\n${summary(diagnostics.snapshot)}\n\n" +
            sections(diagnostics).filter { it.first != "可见文本" }
                .joinToString("\n\n") { (title, body) -> "【$title】\n$body" }

    fun fullTree(snapshot: AccessibilitySnapshot) = snapshot.nodes.joinToString("\n") {
        "${"  ".repeat(it.depth.coerceIn(0, DiagnosticLogic.MAX_DEPTH))}[${it.nodeIndex}] parentIndex=${it.parentIndex} " +
            "depth=${it.depth} childIndex=${it.childIndex} childCount=${it.childCount} class=${it.className} " +
            "text=${it.text} desc=${it.contentDescription} id=${it.viewIdResourceName} bounds=${it.boundsInScreen} " +
            "clickable=${it.clickable} longClickable=${it.longClickable} focusable=${it.focusable} focused=${it.focused} " +
            "editable=${it.editable} scrollable=${it.scrollable} enabled=${it.enabled} selected=${it.selected} password=${it.password}"
    }
}
