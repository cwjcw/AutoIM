package com.autoim.agent

import android.content.ContextWrapper
import org.junit.Assert.*
import org.junit.Test
import java.io.File

/** Synthetic coordinates and contacts only. No captured/customer data. */
class MessageParsingDetectionTest {
    private val parser = WeComSnapshotParser()
    private fun text(value: String, box: String, id: String = "") = NodeInput(
        className = "android.widget.TextView", text = value, boundsInScreen = box, viewIdResourceName = id)
    private fun group(box: String, children: List<NodeInput>, scroll: Boolean = false) = NodeInput(
        className = "android.widget.LinearLayout", boundsInScreen = box, children = children, scrollable = scroll)

    private fun fixture(contents: List<String> = listOf("FROM_CUSTOMER_001", "FROM_ME_001"),
        title: String = "测试联系人", directions: List<MessageDirection> = contents.indices.map {
            if (it % 2 == 0) MessageDirection.INBOUND else MessageDirection.OUTBOUND },
        wide: Boolean = false, event: Int = SnapshotTrigger.WINDOW_CONTENT_CHANGED, time: Long = 100,
        send: Boolean = true, id: String = "", wrappers: Int = 0, reverseTree: Boolean = false,
        extraTitle: Boolean = false, avatar: Boolean = false, media: Boolean = false): AccessibilitySnapshot {
        val rows = contents.mapIndexed { index, content ->
            val y = 260 + index * 130
            val (left, right) = when (directions[index]) {
                MessageDirection.INBOUND -> 70 to if (wide) 800 else 370
                MessageDirection.OUTBOUND -> (if (wide) 200 else 630) to 930
                MessageDirection.UNKNOWN -> 350 to 650
            }
            var bubble = group("[$left,$y][$right,${y + 90}]", listOf(text(content,
                "[${left + 10},${y + 10}][${right - 10},${y + 80}]")))
            repeat(wrappers) { bubble = group("[$left,$y][$right,${y + 90}]", listOf(bubble)) }
            group("[0,$y][1000,${y + 110}]", listOf(bubble) + if (avatar || media) listOf(NodeInput(
                className = "android.widget.ImageView", boundsInScreen = if (media)
                    "[400,${y + 10}][600,${y + 80}]" else "[20,${y + 10}][100,${y + 90}]")) else emptyList())
        }
        val header = group("[0,50][1000,180]", listOf(text(title, "[300,80][700,140]", id)) +
            if (extraTitle) listOf(text("另一测试标题", "[280,145][650,175]")) else emptyList())
        val input = NodeInput(className = "android.widget.EditText", text = "发消息...",
            editable = true, clickable = true, focusable = true, enabled = true, boundsInScreen = "[80,1650][820,1720]")
        val footer = group("[0,1600][1000,1800]", listOf(input) + if (send) listOf(NodeInput(
            className = "android.widget.Button", text = "发送", clickable = true, focusable = true,
            enabled = true, boundsInScreen = "[840,1650][980,1720]")) else emptyList())
        return DiagnosticLogic.buildSnapshot(group("[0,0][1000,1800]", listOf(header,
            group("[0,180][1000,1600]", if (reverseTree) rows.reversed() else rows, true), footer)),
            DiagnosticLogic.WECOM_PACKAGE, time, 1000, 1800, "fixture",
            SnapshotTrigger(event, time, DiagnosticLogic.WECOM_PACKAGE, "fixture"))!!
    }

    private fun parsed(vararg contents: String, title: String = "测试联系人", time: Long = 100,
        event: Int = SnapshotTrigger.WINDOW_CONTENT_CHANGED) = parser.parse(fixture(contents.toList(), title,
            List(contents.size) { MessageDirection.INBOUND }, event = event, time = time))
    private fun assertNoNew(result: MessageDetectionResult, status: MessageDetectionStatus? = null) {
        assertTrue(result.newMessages.isEmpty())
        if (status != null) assertEquals(status, result.status)
    }

    @Test fun identifiesTitleWithGeometryAndParentWithoutViewId() {
        val p = parser.parse(fixture())
        assertEquals("测试联系人", p.conversationTitle)
        assertTrue(p.reasons.toString(), p.reliable)
    }
    @Test fun titleIsNotAMessage() { assertFalse(parser.parse(fixture()).messages.any { it.content == "测试联系人" }) }
    @Test fun inputPlaceholderIsNotAMessage() { assertFalse(parser.parse(fixture()).messages.any { it.content == "发消息..." }) }
    @Test fun sendButtonIsNotAMessage() { assertFalse(parser.parse(fixture()).messages.any { it.content == "发送" }) }
    @Test fun missingSendIsNormal() { assertTrue(parser.parse(fixture(send = false)).reliable) }
    @Test fun inputRaisedByKeyboardStillDefinesMessageRegion() {
        val s = fixture()
        val p = parser.parse(s.copy(nodes = s.nodes.map { n ->
            when {
                n.editable -> n.copy(boundsInScreen = "[80,950][820,1020]")
                n.className.endsWith("Button") -> n.copy(boundsInScreen = "[840,950][980,1020]")
                n.boundsInScreen == "[0,1600][1000,1800]" -> n.copy(boundsInScreen = "[0,900][1000,1050]")
                else -> n
            }
        }))
        assertTrue(p.reasons.toString(), p.reliable)
        assertEquals(900, p.messageRegion!!.bottom)
        assertEquals(2, p.messages.size)
    }
    @Test fun ordinaryLeftIsInbound() { assertEquals(MessageDirection.INBOUND, parser.parse(fixture()).messages.first().direction) }
    @Test fun ordinaryRightIsOutbound() { assertEquals(MessageDirection.OUTBOUND, parser.parse(fixture()).messages.last().direction) }
    @Test fun centeredTextRemainsUnknown() {
        val p = parser.parse(fixture(listOf("居中状态"), directions = listOf(MessageDirection.UNKNOWN)))
        assertEquals(MessageDirection.UNKNOWN, p.messages.single().direction)
        assertFalse(p.reliable)
    }
    @Test fun timestampIsExcluded() { assertTrue(parser.parse(fixture(listOf("昨天 12:34"))).messages.isEmpty()) }
    @Test fun wideLeftUsesMarginsEvenWithCenteredText() {
        val m = parser.parse(fixture(wide = true)).messages.first()
        assertTrue(m.textBounds.centerX in 400.0..600.0)
        assertEquals(MessageDirection.INBOUND, m.direction)
    }
    @Test fun wideRightUsesMarginsEvenWithCenteredText() {
        val m = parser.parse(fixture(wide = true)).messages.last()
        assertTrue(m.textBounds.centerX in 400.0..600.0)
        assertEquals(MessageDirection.OUTBOUND, m.direction)
    }
    @Test fun visualOrderOverridesTraversalOrder() {
        assertEquals(listOf("A", "B"), parser.parse(fixture(listOf("A", "B"), reverseTree = true)).messages.map { it.content })
    }
    @Test fun extraHierarchyDoesNotChangeParsing() {
        assertEquals(listOf(MessageDirection.INBOUND, MessageDirection.OUTBOUND),
            parser.parse(fixture(wrappers = 4)).messages.map { it.direction })
    }
    @Test fun unfamiliarIdsDoNotChangeResults() {
        assertEquals(parser.parse(fixture()).messages.map { it.direction }, parser.parse(fixture(id = "some.new.id")).messages.map { it.direction })
    }
    @Test fun familiarTitleIdCannotRescueInvalidGeometry() {
        val s = fixture(id = "com.tencent.wework:id/nvw")
        val title = s.nodes.single { it.text == "测试联系人" }
        assertNull(parser.parse(s.copy(nodes = s.nodes.map { if (it == title) it.copy(boundsInScreen = "[300,600][700,660]") else it })).conversationTitle)
    }
    @Test fun missingTitleFailsClosed() {
        val p = parser.parse(fixture(title = ""))
        assertNoNew(WeComMessageDetector().detect(p), MessageDetectionStatus.BLOCKED_LOW_CONFIDENCE)
    }
    @Test fun multipleTopTitlesAreAmbiguous() {
        val p = parser.parse(fixture(extraTitle = true))
        assertFalse(p.reliable)
        assertNull(p.conversationTitle)
    }
    @Test fun missingScreenMetricsFailsClosed() { assertFalse(parser.parse(fixture().copy(screenWidth = 0)).reliable) }
    @Test fun corruptedParentStructureFailsClosed() {
        val s = fixture()
        assertFalse(parser.parse(s.copy(nodes = s.nodes.map { if (it.text == "FROM_ME_001") it.copy(parentIndex = 9999) else it })).reliable)
    }
    @Test fun truncatedSnapshotCannotEmitNewMessage() {
        val d = WeComMessageDetector()
        d.detect(parsed("A"))
        assertNoNew(d.detect(parser.parse(fixture(listOf("A", "B")).copy(truncated = true))), MessageDetectionStatus.BLOCKED_LOW_CONFIDENCE)
    }
    @Test fun unknownDirectionBlocksEntireDetection() {
        val d = WeComMessageDetector()
        d.detect(parsed("A"))
        assertNoNew(d.detect(parser.parse(fixture(listOf("A", "居中"),
            directions = listOf(MessageDirection.INBOUND, MessageDirection.UNKNOWN)))), MessageDetectionStatus.BLOCKED_LOW_CONFIDENCE)
    }
    @Test fun rawBodyIsPreservedButSignatureNormalizesOnlyEndsAndLineBreaks() {
        val body = "  产品 ABC-123 !@#\r\n数量  10  "
        val m = parser.parse(fixture(listOf(body))).messages.single()
        assertEquals(body, m.content)
        assertEquals("产品 ABC-123 !@#\n数量  10", m.token.normalizedText)
        assertEquals(m.contentSignature, messageSignature("测试联系人", m.direction, "产品 ABC-123 !@#\n数量  10"))
        assertNotEquals(m.contentSignature, messageSignature("测试联系人", m.direction, "产品 ABC-123 !@#\n数量 10"))
    }
    @Test fun firstSnapshotOnlyBaselines() { assertNoNew(WeComMessageDetector().detect(parsed("A", "B")), MessageDetectionStatus.BASELINE_CREATED) }
    @Test fun duplicateSnapshotDoesNotReplay() {
        val d = WeComMessageDetector(); d.detect(parsed("A", "B"))
        assertNoNew(d.detect(parsed("A", "B")), MessageDetectionStatus.DUPLICATE_EVENT)
    }
    @Test fun appendEmitsOnlyNewTail() {
        val d = WeComMessageDetector(); d.detect(parsed("A", "B"))
        val r = d.detect(parsed("A", "B", "C", time = 200))
        assertEquals(MessageDetectionStatus.NEW_MESSAGE, r.status)
        assertEquals(listOf("C"), r.newMessages.map { it.content })
    }
    @Test fun consecutiveIdenticalTextIsANewOccurrence() {
        val d = WeComMessageDetector(); d.detect(parsed("你好"))
        val r = d.detect(parsed("你好", "你好", time = 200))
        assertEquals(listOf("你好"), r.newMessages.map { it.content })
        assertNoNew(d.detect(parsed("你好", "你好", time = 300)))
    }
    @Test fun repeatedOccurrencesHaveDistinctLocalIds() {
        val d = WeComMessageDetector(); d.detect(parsed("你好"))
        val first = d.detect(parsed("你好", "你好", time = 200)).newMessages.single()
        val second = d.detect(parsed("你好", "你好", "你好", time = 300)).newMessages.single()
        assertNotEquals(first.localEventId, second.localEventId)
        assertEquals(first.contentSignature, second.contentSignature)
    }
    @Test fun multipleAppendsEmitInOrder() {
        val d = WeComMessageDetector(); d.detect(parsed("A"))
        assertEquals(listOf("B", "C"), d.detect(parsed("A", "B", "C")).newMessages.map { it.content })
    }
    @Test fun outboundAppendIsReportedLocally() {
        val d = WeComMessageDetector(); d.detect(parser.parse(fixture(listOf("A"))))
        assertEquals(MessageDirection.OUTBOUND, d.detect(parser.parse(fixture(listOf("A", "B")))).newMessages.single().direction)
    }
    @Test fun scrollNeverEmits() {
        val d = WeComMessageDetector(); d.detect(parsed("A", "B"))
        assertNoNew(d.detect(parsed("A", "B", "C", event = SnapshotTrigger.VIEW_SCROLLED)), MessageDetectionStatus.SCROLL_ONLY)
    }
    @Test fun scrollInCoalescedContentBurstNeverEmits() {
        val d = WeComMessageDetector(); d.detect(parsed("A"))
        val s = parsed("A", "B")
        assertNoNew(d.detect(s.copy(trigger = s.trigger.copy(coalescedEventTypes = setOf(SnapshotTrigger.VIEW_SCROLLED)))), MessageDetectionStatus.SCROLL_ONLY)
    }
    @Test fun trailingScrollEventsStaySuppressedUntilStable() {
        val d = WeComMessageDetector(); d.detect(parsed("A"))
        d.detect(parsed("A", "B", event = SnapshotTrigger.VIEW_SCROLLED))
        assertNoNew(d.detect(parsed("B", "C")), MessageDetectionStatus.RESYNC_REQUIRED)
        assertNoNew(d.detect(parsed("B", "C")), MessageDetectionStatus.RESYNC_REQUIRED)
        assertEquals(listOf("D"), d.detect(parsed("B", "C", "D")).newMessages.map { it.content })
    }
    @Test fun conversationSwitchBaselinesHistoryIncludingReturn() {
        val d = WeComMessageDetector(); d.detect(parsed("A"))
        assertNoNew(d.detect(parsed("X", "Y", title = "测试联系人B")), MessageDetectionStatus.CONVERSATION_CHANGED)
        assertNoNew(d.detect(parsed("A", "B")), MessageDetectionStatus.CONVERSATION_CHANGED)
    }
    @Test fun noOverlapResyncsWithoutGuessing() {
        val d = WeComMessageDetector(); d.detect(parsed("A", "B", "C"))
        assertNoNew(d.detect(parsed("X", "Y", "Z")), MessageDetectionStatus.RESYNC_REQUIRED)
        assertEquals(listOf("D"), d.detect(parsed("X", "Y", "Z", "D")).newMessages.map { it.content })
    }
    @Test fun uniqueSuffixPrefixOverlapSupportsWindowMovingWithAppend() {
        val d = WeComMessageDetector(); d.detect(parsed("A", "B", "C"))
        assertEquals(listOf("D"), d.detect(parsed("B", "C", "D")).newMessages.map { it.content })
    }
    @Test fun ambiguousRepeatedOverlapResyncs() {
        val d = WeComMessageDetector(); d.detect(parsed("X", "A", "A"))
        assertNoNew(d.detect(parsed("A", "A", "B")), MessageDetectionStatus.RESYNC_REQUIRED)
    }
    @Test fun repeatedAnchorElsewhereCannotReplayOldMessages() {
        val d = WeComMessageDetector(); d.detect(parsed("A", "B", "C"))
        assertNoNew(d.detect(parsed("C", "A", "B", "C", "D")), MessageDetectionStatus.RESYNC_REQUIRED)
    }
    @Test fun repeatedAnchorInOldWindowIsAmbiguous() {
        val d = WeComMessageDetector(); d.detect(parsed("A", "B", "A"))
        assertNoNew(d.detect(parsed("A", "C")), MessageDetectionStatus.RESYNC_REQUIRED)
    }
    @Test fun changedMessageInteriorCannotBeTreatedAsAppend() {
        val d = WeComMessageDetector(); d.detect(parsed("A", "B", "C"))
        assertNoNew(d.detect(parsed("A", "X", "B", "C", "D")), MessageDetectionStatus.RESYNC_REQUIRED)
    }
    @Test fun insertedOrPrependedHistoryResyncs() {
        val d = WeComMessageDetector(); d.detect(parsed("A", "B"))
        assertNoNew(d.detect(parsed("X", "A", "B")), MessageDetectionStatus.RESYNC_REQUIRED)
    }
    @Test fun damagedStateResyncsWithoutCrash() {
        val d = WeComMessageDetector()
        d.restoreState(MessageDetectionState("测试联系人", listOf(MessageToken(MessageDirection.UNKNOWN, "")), -1))
        assertNoNew(d.detect(parsed("A", "B")), MessageDetectionStatus.RESYNC_REQUIRED)
    }
    @Test fun restartStartsFreshBaseline() {
        val d = WeComMessageDetector(); d.detect(parsed("A")); d.reset()
        assertNoNew(d.detect(parsed("A", "B")), MessageDetectionStatus.BASELINE_CREATED)
    }
    @Test fun staleSnapshotResyncs() {
        val d = WeComMessageDetector(); d.detect(parsed("A", time = 500))
        assertNoNew(d.detect(parsed("A", "B", time = 100)), MessageDetectionStatus.RESYNC_REQUIRED)
    }
    @Test fun layoutChangeDoesNotReplay() {
        val d = WeComMessageDetector(); d.detect(parser.parse(fixture()))
        assertNoNew(d.detect(parser.parse(fixture(wide = true, time = 200))), MessageDetectionStatus.DUPLICATE_EVENT)
    }
    @Test fun missingPageInvalidatesOldContinuity() {
        val d = WeComMessageDetector(); d.detect(parsed("A"))
        d.detect(parser.parse(fixture(title = "")))
        assertNoNew(d.detect(parsed("A", "B")), MessageDetectionStatus.BASELINE_CREATED)
    }
    @Test fun emptyBaselineCannotProveAllMessagesNew() {
        val d = WeComMessageDetector(); d.detect(parsed())
        assertNoNew(d.detect(parsed("A")), MessageDetectionStatus.RESYNC_REQUIRED)
    }
    @Test fun firstWindowStateSnapshotStillOnlyBaselines() {
        val d = WeComMessageDetector(); assertNoNew(d.detect(parsed("A", event = SnapshotTrigger.WINDOW_STATE_CHANGED)))
        assertEquals(1, d.detect(parsed("A", "B")).newMessages.size)
    }
    @Test fun laterWindowStateChangeDoesNotEmit() {
        val d = WeComMessageDetector(); d.detect(parsed("A"))
        assertNoNew(d.detect(parsed("A", "B", event = SnapshotTrigger.WINDOW_STATE_CHANGED)), MessageDetectionStatus.RESYNC_REQUIRED)
        assertEquals(listOf("C"), d.detect(parsed("A", "B", "C")).newMessages.map { it.content })
    }
    @Test fun untrustedEventPackageFailsClosed() {
        val s = parsed("A")
        assertNoNew(WeComMessageDetector().detect(s.copy(trigger = s.trigger.copy(eventPackage = "other"))), MessageDetectionStatus.BLOCKED_LOW_CONFIDENCE)
    }
    @Test fun textCapMarksSnapshotTruncated() {
        assertTrue(fixture(listOf("A".repeat(1001))).truncated)
    }
    @Test fun differentScreenResolutionKeepsRelativeResults() {
        val s = fixture()
        val regex = Regex("-?\\d+")
        val scaled = s.copy(screenWidth = 1500, screenHeight = 2700, nodes = s.nodes.map { n ->
            n.copy(boundsInScreen = regex.replace(n.boundsInScreen) { (it.value.toInt() * 1.5).toInt().toString() })
        })
        assertEquals(parser.parse(s).messages.map { it.direction }, parser.parse(scaled).messages.map { it.direction })
    }
    @Test fun hiddenNodesAreNotVisibleMessages() {
        val s = fixture()
        val p = parser.parse(s.copy(nodes = s.nodes.map { if (it.text == "FROM_ME_001") it.copy(visibleToUser = false) else it }))
        assertEquals(listOf("FROM_CUSTOMER_001"), p.messages.map { it.content })
    }
    @Test fun hiddenTitleCannotIdentifyConversation() {
        val s = fixture()
        assertNull(parser.parse(s.copy(nodes = s.nodes.map { if (it.text == "测试联系人") it.copy(visibleToUser = false) else it })).conversationTitle)
    }
    @Test fun rowAvatarCanAnchorSymmetricBubbleInbound() {
        val p = parser.parse(fixture(listOf("正文"), directions = listOf(MessageDirection.UNKNOWN), avatar = true))
        assertTrue(p.reasons.toString(), p.reliable)
        assertEquals(MessageDirection.INBOUND, p.messages.single().direction)
    }
    @Test fun rowAvatarCanAnchorSymmetricBubbleOutbound() {
        val s = fixture(listOf("正文"), directions = listOf(MessageDirection.UNKNOWN), avatar = true)
        val p = parser.parse(s.copy(nodes = s.nodes.map { if (it.className.endsWith("ImageView"))
            it.copy(boundsInScreen = "[900,270][980,350]") else it }))
        assertEquals(MessageDirection.OUTBOUND, p.messages.single().direction)
    }
    @Test fun conflictingAvatarAndBubbleDirectionStaysUnknown() {
        val p = parser.parse(fixture(listOf("正文"), directions = listOf(MessageDirection.OUTBOUND), avatar = true))
        assertFalse(p.reliable)
        assertEquals(MessageDirection.UNKNOWN, p.messages.single().direction)
    }
    @Test fun mediaCardIsNotReliableOrdinaryText() {
        val p = parser.parse(fixture(listOf("文件说明"), media = true))
        assertFalse(p.reliable)
        assertNoNew(WeComMessageDetector().detect(p), MessageDetectionStatus.BLOCKED_LOW_CONFIDENCE)
    }
    @Test fun metadataSurvivesPrivateStorageRoundTrip() {
        val dir = java.nio.file.Files.createTempDirectory("autoim-message-fixture-").toFile()
        val store = DiagnosticStore(object : ContextWrapper(null) { override fun getFilesDir(): File = dir })
        val s = fixture().let { it.copy(trigger = it.trigger.copy(coalescedEventTypes = setOf(4096, 2048))) }
        store.saveSnapshot(s)
        assertEquals(s, store.readSnapshot())
    }
    @Test fun clearingLogDoesNotResetDeduplication() {
        MessageDiagnostics.startSession(); MessageDiagnostics.accept(fixture(listOf("A")))
        MessageDiagnostics.accept(fixture(listOf("A", "B")))
        MessageDiagnostics.clearLog(); MessageDiagnostics.accept(fixture(listOf("A", "B")))
        assertNoNew(MessageDiagnostics.view().history.single())
    }
    @Test fun manualRebuildCannotReplayStaleSavedMessages() {
        MessageDiagnostics.startSession(); MessageDiagnostics.accept(fixture(listOf("A")))
        MessageDiagnostics.rebuildBaseline(); MessageDiagnostics.accept(fixture(listOf("A", "B")))
        assertNoNew(MessageDiagnostics.view().history.last(), MessageDetectionStatus.BASELINE_CREATED)
    }
    @Test fun historyIsBoundedAndCanBeCleared() {
        MessageDiagnostics.startSession()
        repeat(110) { MessageDiagnostics.accept(fixture(listOf("A"), time = it.toLong())) }
        assertEquals(100, MessageDiagnostics.view().history.size)
        MessageDiagnostics.clearLog(); assertTrue(MessageDiagnostics.view().history.isEmpty())
    }
}
