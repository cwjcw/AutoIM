package com.autoim.agent

import java.security.MessageDigest

enum class MessageDirection { INBOUND, OUTBOUND, UNKNOWN }
enum class MessageType { TEXT }

data class ParsedMessage(
    val conversationTitle: String?,
    val direction: MessageDirection,
    val messageType: MessageType = MessageType.TEXT,
    val content: String,
    val sourceNodeIndex: Int,
    val textBounds: SnapshotBounds,
    val containerBounds: SnapshotBounds?,
    val observedAt: Long,
    val contentSignature: String,
    val evidence: List<String>,
) {
    val token get() = MessageToken(direction, normalizeMessageText(content))
}

data class MessageToken(val direction: MessageDirection, val normalizedText: String)

fun normalizeMessageText(text: String) = text.replace("\r\n", "\n").replace('\r', '\n').trim()

fun messageSignature(title: String?, direction: MessageDirection, text: String): String {
    // Length-prefix fields avoid separator collisions. This is NOT a message ID.
    val fields = listOf(title.orEmpty(), direction.name, normalizeMessageText(text))
    val bytes = fields.joinToString("") { "${it.length}:$it" }.toByteArray(Charsets.UTF_8)
    return MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }
}

data class ParsedConversationSnapshot(
    val conversationTitle: String?,
    val messages: List<ParsedMessage>,
    val messageRegion: SnapshotBounds?,
    val observedAt: Long,
    val trigger: SnapshotTrigger,
    val reliable: Boolean,
    val reasons: List<String>,
    val titleEvidence: List<String> = emptyList(),
)

/** Offline heuristics from structure + relative geometry; IDs are diagnostic hints only. */
class WeComSnapshotParser {
    fun parse(snapshot: AccessibilitySnapshot): ParsedConversationSnapshot {
        fun blocked(reason: String) = ParsedConversationSnapshot(null, emptyList(), null,
            snapshot.captureTime, snapshot.trigger, false, listOf(reason))
        if (snapshot.packageName != DiagnosticLogic.WECOM_PACKAGE) return blocked("非企业微信快照")
        if (snapshot.screenWidth <= 0 || snapshot.screenHeight <= 0) return blocked("屏幕尺寸缺失")
        if (!validStructure(snapshot)) return blocked("节点结构异常或不完整")
        val width = snapshot.screenWidth.toDouble()
        val height = snapshot.screenHeight.toDouble()
        val nodes = snapshot.nodes
        val byIndex = nodes.associateBy { it.nodeIndex }
        fun bounds(node: NodeSnapshot) = SnapshotBounds.parse(node.boundsInScreen)
            ?.takeIf { it.left >= 0 && it.top >= 0 && it.right <= width && it.bottom <= height }
        fun ancestors(node: NodeSnapshot): List<NodeSnapshot> {
            val result = mutableListOf<NodeSnapshot>()
            var current = node
            repeat(6) {
                current = current.parentIndex?.let(byIndex::get) ?: return result
                result += current
            }
            return result
        }
        fun isText(node: NodeSnapshot) = node.className.endsWith("TextView") &&
            node.text.isNotBlank() && !node.editable && !node.password && node.visibleToUser
        val inputs = nodes.filter { it.editable && it.className.endsWith("EditText") && !it.password && it.visibleToUser }
            .mapNotNull { node -> bounds(node)?.takeIf { it.top >= height * .40 }?.let { node to it } }
        if (inputs.size != 1) return blocked("无法唯一确定底部输入区")
        val input = inputs.single()
        val titles = nodes.filter(::isText).mapNotNull { node ->
            val rect = bounds(node) ?: return@mapNotNull null
            val parent = ancestors(node).firstOrNull { ancestor ->
                val box = bounds(ancestor)
                !ancestor.scrollable && !ancestor.editable && !ancestor.className.endsWith("TextView") &&
                    box != null && box.contains(rect) && box.top >= height * .025 &&
                    box.bottom <= height * .22 && box.right - box.left >= width * .65 &&
                    box.bottom - box.top <= height * .20
            } ?: return@mapNotNull null
            val parentRect = bounds(parent)!!
            if (rect.top < height * .025 || rect.bottom > height * .20 ||
                rect.centerX !in width * .25..width * .75 || node.scrollable ||
                parent.scrollable || parent.editable || parent.className.endsWith("TextView") ||
                !parentRect.contains(rect) || parentRect.top < height * .025 || parentRect.bottom > height * .22 ||
                parentRect.right - parentRect.left < width * .65 ||
                parentRect.bottom - parentRect.top > height * .20 ||
                nonMessageText(node.text) || node.text.trim() in navigationLabels) return@mapNotNull null
            // A title is in a dedicated top toolbar, never inside a scrolling message list.
            if (ancestors(node).any { it.scrollable }) return@mapNotNull null
            Triple(node, rect, parentRect)
        }
        if (titles.size != 1) return blocked("会话标题缺失或存在多个候选")
        val (titleNode, titleBounds, toolbar) = titles.single()
        val title = titleNode.text.trim()
        val inputToolbar = ancestors(input.first).mapNotNull(::bounds).firstOrNull {
            it.contains(input.second) && it.top >= height * .35 && it.bottom - it.top <= height * .20
        }
        val regionBottom = inputToolbar?.top ?: input.second.top
        if (regionBottom <= toolbar.bottom || regionBottom - toolbar.bottom < height * .20)
            return blocked("消息区域无法确定")
        val region = SnapshotBounds(0, maxOf(toolbar.bottom, titleBounds.bottom), snapshot.screenWidth, regionBottom)
        val candidates = nodes.filter(::isText).filter {
            val rect = bounds(it)
            rect != null && region.contains(rect) && !nonMessageText(it.text) &&
                ancestors(it).none { parent -> parent.editable || parent.password || parent.className.endsWith("Button") }
        }
        val reasons = mutableListOf<String>()
        if (snapshot.truncated) reasons += "快照已截断"
        val messages = candidates.map { node ->
            val rect = bounds(node)!!
            val parents = ancestors(node)
            val evidence = mutableListOf("TextView 正文位于标题下方、输入区上方", "textBounds=$rect")
            fun descendsFrom(child: NodeSnapshot, ancestor: NodeSnapshot) =
                child.nodeIndex == ancestor.nodeIndex || ancestors(child).any { it.nodeIndex == ancestor.nodeIndex }
            val container = parents.firstOrNull { parent ->
                val box = bounds(parent)
                box != null && box.contains(rect) && region.contains(box) && !parent.scrollable &&
                    !parent.editable && !parent.className.endsWith("TextView") &&
                    box.right - box.left < width * .94 &&
                    box.bottom - box.top <= (region.bottom - region.top) * .85
            }
            val containerBounds = container?.let(::bounds)
            val row = parents.lastOrNull { parent ->
                val box = bounds(parent)
                box != null && region.contains(box) && box.contains(rect) &&
                    box.bottom - box.top <= maxOf((rect.bottom - rect.top) * 1.8, height * .10) && !parent.scrollable
            }
            val avatars = row?.let { r -> nodes.filter { child ->
                val box = bounds(child)
                child.className.endsWith("ImageView") && child.visibleToUser && child.text.isBlank() && box != null &&
                    descendsFrom(child, r) && box.right - box.left in (width * .035).toInt()..(width * .15).toInt() &&
                    (box.bottom - box.top).toDouble() / (box.right - box.left) in .7..1.4 &&
                    box.bottom > rect.top && box.top < rect.bottom
            } } ?: emptyList()
            val avatarSides = avatars.mapNotNull { avatar ->
                val box = bounds(avatar)!!
                when {
                    box.right <= rect.left && box.left <= width * .12 -> MessageDirection.INBOUND
                    box.left >= rect.right && width - box.right <= width * .12 -> MessageDirection.OUTBOUND
                    else -> null
                }
            }.distinct()
            val boxSide = containerBounds?.let { side(it, width) }
            val textSide = side(rect, width)
            val avatarSide = avatarSides.singleOrNull()
            // Centered wide text may use its asymmetric bubble or a row's avatar anchor.
            val direction = when {
                avatarSides.size > 1 -> MessageDirection.UNKNOWN
                avatarSide != null && ((boxSide != null && boxSide != avatarSide) ||
                    (textSide != null && textSide != avatarSide)) -> MessageDirection.UNKNOWN
                avatarSide != null && (boxSide == null || boxSide == avatarSide) &&
                    (textSide == null || textSide == avatarSide) -> avatarSide
                boxSide != null && (textSide == null || textSide == boxSide) -> boxSide
                else -> MessageDirection.UNKNOWN
            }
            var finalDirection = direction
            if (container != null) {
                val bubbleTexts = candidates.count { descendsFrom(it, container) }
                val controls = nodes.any { descendsFrom(it, container) &&
                    (it.className.endsWith("Button") || it.className.endsWith("ImageView") || it.editable) }
                if (bubbleTexts != 1 || controls) {
                    finalDirection = MessageDirection.UNKNOWN
                    evidence += "非单一普通文本容器（引用/卡片/媒体等未支持）"
                }
            }
            if (row != null && (candidates.count { descendsFrom(it, row) } > 1 ||
                nodes.any { descendsFrom(it, row) && it.visibleToUser &&
                    (it.className.endsWith("Button") ||
                        (it.className.endsWith("ImageView") && it !in avatars)) })) {
                finalDirection = MessageDirection.UNKNOWN
                evidence += "行内有多段文本或非头像媒体控件；不作为普通一对一文本"
            }
            if (containerBounds != null) evidence += "containerBounds=$containerBounds; marginSide=$boxSide"
            evidence += "textMarginSide=$textSide; rowAvatarSide=$avatarSide; ancestorCount=${parents.size}"
            if (finalDirection == MessageDirection.UNKNOWN) reasons += "节点 ${node.nodeIndex} 方向/容器不可靠"
            ParsedMessage(title, finalDirection, content = node.text, sourceNodeIndex = node.nodeIndex,
                textBounds = rect, containerBounds = containerBounds, observedAt = snapshot.captureTime,
                contentSignature = messageSignature(title, finalDirection, node.text), evidence = evidence)
        }.sortedWith(compareBy<ParsedMessage> { it.textBounds.top }.thenBy { it.textBounds.left }.thenBy { it.sourceNodeIndex })
        return ParsedConversationSnapshot(title, messages, region, snapshot.captureTime, snapshot.trigger,
            reasons.isEmpty(), reasons.distinct(), listOf("顶部独立文本/工具栏结构", "titleBounds=$titleBounds",
                "toolbarBounds=$toolbar", "inputBounds=${input.second}", "viewId辅助证据=${titleNode.viewIdResourceName}"))
    }

    private fun validStructure(snapshot: AccessibilitySnapshot): Boolean {
        if (snapshot.nodes.isEmpty() || snapshot.nodeCount != snapshot.nodes.size) return false
        val stack = mutableListOf<Int>()
        val slots = hashSetOf<Pair<Int?, Int>>()
        for ((position, node) in snapshot.nodes.withIndex()) {
            if (node.nodeIndex != position || node.depth !in 0..DiagnosticLogic.MAX_DEPTH || node.childIndex < 0 ||
                node.childCount < 0 || !slots.add(node.parentIndex to node.childIndex)) return false
            if (position == 0) {
                if (node.depth != 0 || node.parentIndex != null) return false
            } else if (node.depth == 0 || node.depth > stack.size) return false
            while (stack.size > node.depth) stack.removeAt(stack.lastIndex)
            if (node.parentIndex != stack.lastOrNull()) return false
            node.parentIndex?.let { if (node.childIndex >= snapshot.nodes[it].childCount) return false }
            stack += position
        }
        return snapshot.nodes.all { node -> snapshot.nodes.count { it.parentIndex == node.nodeIndex } == node.childCount }
    }

    private fun side(box: SnapshotBounds, width: Double): MessageDirection? {
        val left = box.left.toDouble()
        val right = width - box.right
        return when {
            left <= width * .20 && right - left >= width * .08 -> MessageDirection.INBOUND
            right <= width * .20 && left - right >= width * .08 -> MessageDirection.OUTBOUND
            else -> null
        }
    }

    private fun nonMessageText(text: String): Boolean {
        val value = text.trim()
        return timePattern.matches(value) || value in systemLabels ||
            value.startsWith("以下为新消息") || value.contains("撤回了一条消息") ||
            value.contains("你已添加了") || value.contains("现在可以开始聊天")
    }

    companion object {
        private val timePattern = Regex("(?:(?:\\d{2,4}[-年/.]\\d{1,2}[-月/.]\\d{1,2}日?|今天|昨天|前天|星期[一二三四五六日天]|周[一二三四五六日天])\\s*)?(?:(?:上午|下午|晚上|凌晨)\\s*)?\\d{1,2}:\\d{2}")
        private val systemLabels = setOf("发送", "发消息...", "发消息…", "更多", "按住说话", "切换到键盘", "以下为新消息")
        private val navigationLabels = setOf("企业微信", "消息", "通讯录", "工作台", "我", "搜索", "返回")
    }
}

private fun SnapshotBounds.contains(other: SnapshotBounds) =
    other.left >= left && other.right <= right && other.top >= top && other.bottom <= bottom
