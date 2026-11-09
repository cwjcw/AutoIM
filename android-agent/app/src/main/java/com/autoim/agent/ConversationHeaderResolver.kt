package com.autoim.agent

import kotlin.math.max
import kotlin.math.min

enum class PeerType { INTERNAL_WECOM, EXTERNAL_WECOM, PERSONAL_WECHAT, UNKNOWN }
enum class HeaderMetadataType { ORGANIZATION_SUBTITLE, TEXT_METADATA, STATUS_ICON, IDENTITY_LABEL, UNKNOWN_DECORATION }
enum class HeaderNodeRole { PRIMARY_TITLE, ORGANIZATION_SUBTITLE, IDENTITY_LABEL, STATUS_DECORATION, UNKNOWN }

data class HeaderMetadata(
    val type: HeaderMetadataType,
    val nodeIndex: Int,
    val text: String,
    val contentDescription: String,
    val className: String,
    val bounds: SnapshotBounds?,
)

data class TitleCandidateScore(
    val topPositionScore: Int,
    val horizontalPositionScore: Int,
    val textNodeScore: Int,
    val parentStructureScore: Int,
    val titleGroupScore: Int,
    val viewIdHintScore: Int,
    val textShapeScore: Int,
    val penalties: Map<String, Int>,
) {
    val total get() = topPositionScore + horizontalPositionScore + textNodeScore + parentStructureScore +
        titleGroupScore + viewIdHintScore + textShapeScore - penalties.values.sum()
}

data class TitleCandidate(
    val text: String,
    val nodeIndex: Int,
    val parentIndex: Int?,
    val grandparentIndex: Int?,
    val bounds: SnapshotBounds?,
    val className: String,
    val viewId: String,
    val groupId: Int?,
    val score: TitleCandidateScore,
    val eligible: Boolean,
    val positiveEvidence: List<String>,
    val negativeEvidence: List<String>,
    val role: HeaderNodeRole = HeaderNodeRole.UNKNOWN,
)

data class TitleGroup(
    val id: Int,
    val containerNodeIndex: Int,
    val containerBounds: SnapshotBounds,
    val nodeIndices: List<Int>,
    val primaryNodeIndices: List<Int>,
    val metadata: List<HeaderMetadata>,
)

data class HeaderEvidence(
    val groups: List<TitleGroup>,
    val candidates: List<TitleCandidate>,
    val selectedScore: Int?,
    val runnerUpScore: Int?,
    val confidenceReason: String,
)

data class ConversationHeader(
    val title: String?,
    val organizationName: String?,
    val peerType: PeerType,
    val metadata: List<HeaderMetadata>,
    val reliable: Boolean,
    val titleReliable: Boolean,
    val identityConfidenceReason: String,
    val bounds: SnapshotBounds?,
    val evidence: HeaderEvidence,
)

/** Pure snapshot parsing. Parent geometry is local to a header, not a minimum screen-wide toolbar. */
class ConversationHeaderResolver {
    fun resolve(snapshot: AccessibilitySnapshot): ConversationHeader {
        val width = snapshot.screenWidth.toDouble()
        val height = snapshot.screenHeight.toDouble()
        if (width <= 0 || height <= 0) return ConversationHeader(null, null, PeerType.UNKNOWN, emptyList(), false, false, "缺少屏幕尺寸", null,
            HeaderEvidence(emptyList(), emptyList(), null, null, "标题解析缺少屏幕尺寸"))
        val byIndex = snapshot.nodes.associateBy { it.nodeIndex }
        fun bounds(node: NodeSnapshot) = SnapshotBounds.parse(node.boundsInScreen)?.takeIf {
            it.left >= 0 && it.top >= 0 && it.right <= width && it.bottom <= height
        }
        fun ancestors(node: NodeSnapshot): List<NodeSnapshot> {
            val result = mutableListOf<NodeSnapshot>()
            var current = node
            repeat(DiagnosticLogic.MAX_DEPTH) {
                current = current.parentIndex?.let(byIndex::get) ?: return result
                if (result.any { it.nodeIndex == current.nodeIndex }) return result
                result += current
            }
            return result
        }
        fun inMessageList(node: NodeSnapshot) = node.scrollable ||
            listOf("RecyclerView", "ListView", "ScrollView").any { node.className.contains(it) }
        val discovered = snapshot.nodes.filter { node ->
            node.visibleToUser && (node.text.isNotBlank() || node.contentDescription.isNotBlank() || node.childCount == 0) &&
                (bounds(node)?.top?.let { it < height * .22 } ?: node.text.isNotBlank())
        }.map { node ->
            val rect = bounds(node)
            val parents = ancestors(node)
            val insideMessageList = inMessageList(node) || parents.any(::inMessageList)
            val control = node.editable || node.password || node.className.endsWith("EditText") ||
                node.className.endsWith("Button") || parents.any {
                    it.editable || it.password || it.className.endsWith("Button")
                }
            val container = if (insideMessageList || control || rect == null) null else parents.take(6).lastOrNull { parent ->
                val box = bounds(parent)
                parent.parentIndex != null && parent.visibleToUser && !inMessageList(parent) && !parent.editable &&
                    !textClass(parent) && !parent.className.endsWith("Button") && box != null &&
                    box.left <= rect.left && box.right >= rect.right && box.top <= rect.top && box.bottom >= rect.bottom &&
                    box.bottom <= height * .20 && box.bottom - box.top <= height * .14
            }
            HeaderNode(node, rect, container, container?.let(::bounds), insideMessageList, control)
        }

        // Join sibling containers only through a bounded title-band common ancestor and
        // plausible title geometry. A shared page/root ancestor is deliberately insufficient.
        val eligibleNodes = discovered.filter { item ->
            item.container != null && item.bounds != null && !item.control && !item.insideMessageList &&
                !item.node.className.endsWith("Button") &&
                classify(item) in setOf(HeaderNodeRole.PRIMARY_TITLE, HeaderNodeRole.ORGANIZATION_SUBTITLE,
                    HeaderNodeRole.IDENTITY_LABEL, HeaderNodeRole.STATUS_DECORATION)
        }
        fun containerAncestors(item: HeaderNode): List<NodeSnapshot> =
            listOfNotNull(item.container) + ancestors(item.container!!)
        fun shareTitleAncestor(a: HeaderNode, b: HeaderNode): Boolean {
            val bIds = containerAncestors(b).map { it.nodeIndex }.toSet()
            return containerAncestors(a).asSequence().filter { it.nodeIndex in bIds }.any { ancestor ->
                val box = bounds(ancestor)
                box != null && box.top >= 0 && box.bottom <= height * .22 &&
                    box.bottom - box.top <= height * .20 && box.right - box.left <= width * .92
            }
        }
        val components = mutableListOf<List<HeaderNode>>()
        val remaining = eligibleNodes.toMutableList()
        while (remaining.isNotEmpty()) {
            val component = mutableListOf(remaining.removeAt(0))
            var offset = 0
            while (offset < component.size) {
                val anchor = component[offset++]
                val neighbours = remaining.filter { item ->
                    shareTitleAncestor(anchor, item) && nearInTitleGroup(anchor.bounds!!, item.bounds!!, width)
                }
                component += neighbours
                remaining.removeAll(neighbours.toSet())
            }
            components += component
        }
        // Keep independently supported title nodes addressable even if row/container grouping
        // cannot join the second line. The group's bounds remain the node's own bounded header container.
        val groupedNodes = components.flatten().map { it.node.nodeIndex }.toSet()
        val standalone = eligibleNodes.filter { it.node.nodeIndex !in groupedNodes && classify(it) == HeaderNodeRole.PRIMARY_TITLE }
        val allComponents = components + standalone.map { listOf(it) }
        val groupForNode = allComponents.flatMapIndexed { index, group -> group.map { it.node.nodeIndex to index + 1 } }.toMap()
        val metadataByNode = discovered.mapNotNull { item -> metadata(item)?.let { item.node.nodeIndex to it } }.toMap()
        val roles = discovered.associate { it.node.nodeIndex to classify(it) }
        val candidates = discovered.filter { it.node.text.isNotBlank() }.map { item ->
            val node = item.node
            val rect = item.bounds
            val text = node.text.trim()
            val positive = mutableListOf<String>()
            val penalties = linkedMapOf<String, Int>()
            fun points(value: Int, evidence: String): Int { if (value > 0) positive += evidence; return value }
            val top = points(when {
                rect == null || rect.top < height * .02 || rect.bottom > height * .20 -> 0
                rect.bottom <= height * .15 -> 20
                else -> 10
            }, "top-region")
            val horizontal = points(when {
                rect == null -> 0
                rect.centerX in width * .30..width * .70 -> 20
                rect.centerX in width * .20..width * .80 -> 10
                else -> 0
            }, "center-position")
            if (isTimeLikeHeaderText(text)) penalties["time-like-text"] = 100
            if (isInputHint(text)) penalties["input-hint"] = 100
            if (text in toolbarLabels) penalties["toolbar-or-button-text"] = 100
            val role = roles[node.nodeIndex] ?: HeaderNodeRole.UNKNOWN
            if (role != HeaderNodeRole.PRIMARY_TITLE) penalties["role-$role"] = 100
            if (item.control) penalties["input-or-button-node"] = 100
            if (item.insideMessageList) penalties["message-region"] = 100
            if (!textClass(node)) penalties["non-text-decoration"] = 100
            if (text.none { it.isLetterOrDigit() }) penalties["symbol-only"] = 100
            if (top == 0) penalties["outside-title-band-or-status-bar"] = 100
            if (horizontal == 0) penalties["outside-central-header"] = 100
            if (item.container == null) penalties["missing-header-parent"] = 100
            val groupId = groupForNode[node.nodeIndex]
            val score = TitleCandidateScore(top, horizontal,
                points(if (textClass(node)) 20 else 0, "text-node"),
                points(if (item.container != null) 25 else 0, "title-parent=${item.container?.nodeIndex}"),
                points(if (groupId != null) 10 else 0, "title-group=$groupId"),
                points(if (node.viewIdResourceName == "com.tencent.wework:id/nvw") 3 else 0, "viewId-secondary-hint"),
                points(if (text.any { it.isLetterOrDigit() }) 5 else 0, "nonempty-name-shape"), penalties)
            TitleCandidate(node.text, node.nodeIndex, node.parentIndex, node.parentIndex?.let(byIndex::get)?.parentIndex,
                rect, node.className, node.viewIdResourceName, groupId, score,
                penalties.isEmpty(), positive, penalties.keys.toList(), role)
        }
        val groups = allComponents.mapIndexed { index, members ->
            val indices = members.map { it.node.nodeIndex }
            TitleGroup(index + 1, members.first().container!!.nodeIndex, members.first().containerBounds!!, indices,
                candidates.filter { it.eligible && it.nodeIndex in indices }.map { it.nodeIndex },
                indices.mapNotNull(metadataByNode::get))
        }
        val ranked = candidates.filter { it.eligible }.sortedWith(
            compareByDescending<TitleCandidate> { it.score.total }.thenBy { it.nodeIndex })
        val best = ranked.firstOrNull()
        val runnerUp = ranked.getOrNull(1)
        val reliable = best != null && best.score.total >= MINIMUM_SCORE &&
            (runnerUp == null || best.score.total - runnerUp.score.total >= MINIMUM_MARGIN)
        val reason = when {
            best == null -> "无可靠主标题；请查看候选 negativeEvidence"
            best.score.total < MINIMUM_SCORE -> "主标题得分 ${best.score.total} 低于阈值 $MINIMUM_SCORE"
            !reliable -> "主标题歧义：最高 ${best.score.total} / 次高 ${runnerUp!!.score.total}，差值不足 $MINIMUM_MARGIN"
            else -> "主标题结构与位置可靠；得分 ${best.score.total}，阈值 $MINIMUM_SCORE，最小分差 $MINIMUM_MARGIN"
        }
        val selectedGroup = if (reliable) groups.first { it.id == best!!.groupId } else null
        val selectedMetadata = selectedGroup?.metadata.orEmpty().toMutableList()
        val title = if (reliable) best!!.text.trim() else null
        val titleNode = best?.let { byIndex[it.nodeIndex] }
        val titleAncestors = titleNode?.let(::ancestors).orEmpty().map { it.nodeIndex }.toSet()
        val secondLine = if (reliable && best != null) discovered.filter { item ->
            item.node.nodeIndex != best.nodeIndex && classify(item) in setOf(HeaderNodeRole.ORGANIZATION_SUBTITLE, HeaderNodeRole.IDENTITY_LABEL) &&
                item.bounds != null && best.bounds != null &&
                (nearInSameRow(item.bounds, best.bounds, width) ||
                    (item.bounds.top >= best.bounds.bottom - max(1.0, (best.bounds.bottom - best.bounds.top) * .12) &&
                        item.bounds.top - best.bounds.bottom <= height * .05 &&
                        min(item.bounds.right, best.bounds.right) - max(item.bounds.left, best.bounds.left) >=
                            min(item.bounds.right - item.bounds.left, best.bounds.right - best.bounds.left) * .25)) &&
                (ancestors(item.node).any { it.nodeIndex in titleAncestors } || item.node.parentIndex == best.nodeIndex)
        } else emptyList()
        secondLine.mapNotNull { metadata(it) }.forEach { if (it !in selectedMetadata) selectedMetadata += it }
        val organization = selectedMetadata.firstOrNull { it.type == HeaderMetadataType.ORGANIZATION_SUBTITLE }?.text?.trim()?.removePrefix("＠")?.removePrefix("@")?.trim()
        val identity = when {
            !reliable -> PeerType.UNKNOWN to "标题结构不可靠"
            secondLine.size > 1 -> PeerType.UNKNOWN to "姓名下方存在多个身份说明候选"
            selectedMetadata.any { it.type == HeaderMetadataType.IDENTITY_LABEL } && organization != null -> PeerType.UNKNOWN to "身份标签与组织副标题冲突"
            selectedMetadata.any { it.type == HeaderMetadataType.IDENTITY_LABEL } -> PeerType.PERSONAL_WECHAT to "识别到独立 @微信 身份标签"
            organization != null -> PeerType.EXTERNAL_WECOM to "识别到组织名称副标题"
            title != null && groupCountSuffix.containsMatchIn(title) -> PeerType.UNKNOWN to "疑似群聊，不能套用联系人分类"
            else -> PeerType.UNKNOWN to "未取得可证明内部身份的一对一结构证据"
        }
        return ConversationHeader(title, organization, identity.first, selectedMetadata, reliable, reliable, identity.second, selectedGroup?.containerBounds,
            HeaderEvidence(groups, candidates, best?.score?.total, runnerUp?.score?.total, reason))
    }

    private data class HeaderNode(val node: NodeSnapshot, val bounds: SnapshotBounds?, val container: NodeSnapshot?,
        val containerBounds: SnapshotBounds?, val insideMessageList: Boolean, val control: Boolean)

    private fun classify(item: HeaderNode): HeaderNodeRole {
        val node = item.node
        val value = node.text.trim()
        return when {
            isWechatLabel(value) -> HeaderNodeRole.IDENTITY_LABEL
            organizationPattern.matches(value) -> HeaderNodeRole.ORGANIZATION_SUBTITLE
            node.className.endsWith("ImageView") || value.isEmpty() || value.none { it.isLetterOrDigit() } -> HeaderNodeRole.STATUS_DECORATION
            isTimeLikeHeaderText(value) || isInputHint(value) || value in toolbarLabels || item.control -> HeaderNodeRole.UNKNOWN
            item.container != null && textClass(node) -> HeaderNodeRole.PRIMARY_TITLE
            else -> HeaderNodeRole.UNKNOWN
        }
    }

    private fun metadata(item: HeaderNode): HeaderMetadata? {
        val type = when (classify(item)) {
            HeaderNodeRole.ORGANIZATION_SUBTITLE -> HeaderMetadataType.ORGANIZATION_SUBTITLE
            HeaderNodeRole.IDENTITY_LABEL -> HeaderMetadataType.IDENTITY_LABEL
            HeaderNodeRole.STATUS_DECORATION -> if (item.node.className.endsWith("ImageView") || item.node.contentDescription.isNotBlank()) HeaderMetadataType.STATUS_ICON else HeaderMetadataType.UNKNOWN_DECORATION
            HeaderNodeRole.UNKNOWN -> HeaderMetadataType.TEXT_METADATA
            else -> return null
        }
        return HeaderMetadata(type, item.node.nodeIndex, item.node.text, item.node.contentDescription, item.node.className, item.bounds)
    }

    private fun nearInSameRow(a: SnapshotBounds, b: SnapshotBounds, width: Double): Boolean {
        val overlap = min(a.bottom, b.bottom) - max(a.top, b.top)
        val minHeight = min(a.bottom - a.top, b.bottom - b.top)
        val gap = max(0, max(a.left - b.right, b.left - a.right))
        return overlap >= minHeight * .4 && gap <= max(width * .06, minHeight * .8)
    }

    private fun nearInTitleGroup(a: SnapshotBounds, b: SnapshotBounds, width: Double): Boolean {
        if (nearInSameRow(a, b, width)) return true
        val verticalGap = max(0, max(a.top - b.bottom, b.top - a.bottom))
        val horizontalOverlap = min(a.right, b.right) - max(a.left, b.left)
        val verticallyAdjacent = verticalGap <= max((a.bottom-a.top), (b.bottom-b.top)) * .35
        return verticallyAdjacent && horizontalOverlap >= min(a.right-a.left, b.right-b.left) * .45
    }

    private fun isWechatLabel(text: String) = text.trim() in setOf("@微信", "＠微信")

    companion object {
        // ID hint is smaller than the margin; an ID alone cannot resolve otherwise equal titles.
        const val MINIMUM_SCORE = 90
        const val MINIMUM_MARGIN = 12
        private val toolbarLabels = setOf("发送", "返回", "更多", "搜索", "消息", "通讯录", "工作台", "我", "企业微信")
        private val groupCountSuffix = Regex("[（(]\\d+[）)]$")
        private val organizationPattern = Regex("[@＠].{2,}")
        private fun textClass(node: NodeSnapshot) = node.className.endsWith("TextView") || node.className.endsWith("Text")
        private fun isInputHint(text: String) = text.startsWith("发消息") || text == "按住说话" || text == "切换到键盘"
    }
}

/** Match whole time/date labels, not arbitrary names containing digits. */
fun isTimeLikeHeaderText(text: String): Boolean = headerTimePattern.matches(text.trim())

private val headerTimePattern = Regex(
    "(?:(?:今天|昨天|前天|星期[一二三四五六日天]|周[一二三四五六日天]|" +
        "(?:\\d{2,4}年)?\\d{1,2}月\\d{1,2}日|\\d{2,4}[-/.]\\d{1,2}[-/.]\\d{1,2})" +
        "(?:\\s*(?:上午|下午|晚上|凌晨|中午)?\\s*\\d{1,2}[:：]\\d{2})?|" +
        "(?:上午|下午|晚上|凌晨|中午)?\\s*\\d{1,2}[:：]\\d{2})"
)

object ConversationHeaderReport {
    fun render(header: ConversationHeader?): String {
        if (header == null) return "标题解析未运行（快照包名、尺寸或结构校验未通过）"
        val e = header.evidence
        return "标题组数量=${e.groups.size}\nPrimary Title 候选数=${e.candidates.count { it.eligible }}\n" +
            "conversationTitle=${header.title}\norganizationName=${header.organizationName}\npeerType=${header.peerType}\n" +
            "titleReliable=${header.titleReliable}\nidentityConfidenceReason=${header.identityConfidenceReason}\nselectedScore=${e.selectedScore}\n" +
            "runnerUpScore=${e.runnerUpScore}\nheader.reliable=${header.reliable}\nconfidenceReason=${e.confidenceReason}\n\n" +
            e.groups.joinToString("\n\n") { group ->
                "TitleGroup #${group.id}: container=${group.containerNodeIndex}, bounds=${group.containerBounds}\n" +
                    "Primary nodes=${group.primaryNodeIndices}\nMetadata:\n" + group.metadata.joinToString("\n") {
                        "${it.type}: text=${it.text}, desc=${it.contentDescription}, nodeIndex=${it.nodeIndex}, class=${it.className}, bounds=${it.bounds}"
                    }
            } + "\n\n标题候选评分（含排除项）：\n" + e.candidates.joinToString("\n\n") { c ->
                "text=${c.text}\nnodeIndex=${c.nodeIndex}; parentIndex=${c.parentIndex}; grandparentIndex=${c.grandparentIndex}\n" +
                    "bounds=${c.bounds}; className=${c.className}; viewId=${c.viewId}; group=${c.groupId}\n" +
                    "score=${c.score.total}; eligible=${c.eligible}\nscoreDetails=${c.score}\n" +
                    "positiveEvidence=${c.positiveEvidence.joinToString("; ")}\nnegativeEvidence=${c.negativeEvidence.joinToString("; ")}"
            }
    }
}

