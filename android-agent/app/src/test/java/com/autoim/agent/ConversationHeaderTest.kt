package com.autoim.agent

import org.junit.Assert.*
import org.junit.Test

/** Synthetic tree using only the title/geometry and sample strings explicitly supplied for this regression. */
class ConversationHeaderTest {
    private val parser = WeComSnapshotParser()
    private fun text(value: String, box: String, id: String = "") = NodeInput(
        className = "android.widget.TextView", text = value, boundsInScreen = box, viewIdResourceName = id)
    private fun group(box: String, children: List<NodeInput>, scrollable: Boolean = false) = NodeInput(
        className = "android.widget.LinearLayout", boundsInScreen = box, children = children, scrollable = scrollable)

    private fun fixture(
        title: String = "崔玮杰", personal: Boolean = false, icons: Int = 0,
        contents: List<String> = listOf("CUSTOMER_NEW_001"), width: Int = 1080, height: Int = 2408,
        id: String = "", titleBounds: String = "[448,110][589,174]", headerBounds: String = "[400,85][860,205]",
        extraHeader: List<NodeInput> = emptyList(), extraTop: List<NodeInput> = emptyList(),
        wideToolbar: Boolean = false, time: Long = 100,
    ): AccessibilitySnapshot {
        val headerItems = listOf(text(title, titleBounds, id)) +
            (if (personal) listOf(text("@微信", "[596,115][690,169]")) else emptyList()) +
            (0 until icons).map { i -> NodeInput(className = if (i % 2 == 0) "android.widget.ImageView" else "android.view.View",
                contentDescription = "状态图标$i", boundsInScreen = "[${700 + i * 45},120][${735 + i * 45},160]") } + extraHeader
        val narrow = group(headerBounds, headerItems)
        val header = if (wideToolbar) group("[0,0][1080,220]", listOf(narrow) + extraTop) else narrow
        val rows = contents.mapIndexed { i, content ->
            val y = 400 + i * 180
            group("[0,$y][1080,${y + 120}]", listOf(group("[70,$y][650,${y + 100}]",
                listOf(text(content, "[90,${y + 10}][630,${y + 90}]")))))
        }
        val input = NodeInput(className = "android.widget.EditText", text = "发消息或按住...", editable = true,
            clickable = true, focusable = true, enabled = true, boundsInScreen = "[80,2250][900,2350]")
        var page = group("[0,0][1080,2408]", listOf(header,
            group("[0,230][1080,2180]", listOf(text("下午 7:17", "[430,260][650,310]")) + rows, true),
            group("[0,2190][1080,2408]", listOf(input))) + if (wideToolbar) emptyList() else extraTop)
        repeat(12) { page = group("[0,0][1080,2408]", listOf(page)) }
        fun count(node: NodeInput): Int = 1 + node.children.filterNotNull().sumOf(::count)
        val padding = List((80 - 1 - count(page)).coerceAtLeast(0)) {
            NodeInput(className = "android.view.View", boundsInScreen = "[0,2390][1,2408]", visibleToUser = false)
        }
        val snapshot = DiagnosticLogic.buildSnapshot(group("[0,0][1080,2408]", listOf(page) + padding),
            DiagnosticLogic.WECOM_PACKAGE, time, 1080, 2408, "synthetic-redmi-regression",
            SnapshotTrigger(SnapshotTrigger.WINDOW_CONTENT_CHANGED, time, DiagnosticLogic.WECOM_PACKAGE, "synthetic"))!!
        return snapshot.copy(screenWidth = width, screenHeight = height, nodes = snapshot.nodes.map { n ->
            val b = SnapshotBounds.parse(n.boundsInScreen)!!
            n.copy(boundsInScreen = "[${b.left * width / 1080},${b.top * height / 2408}]" +
                "[${b.right * width / 1080},${b.bottom * height / 2408}]")
        })
    }

    private fun assertHeader(s: AccessibilitySnapshot, title: String, peer: PeerType): ParsedConversationSnapshot {
        val p = parser.parse(s)
        assertTrue(MessageDiagnosticReport.parsed(p), p.header?.titleReliable == true)
        assertEquals(title, p.conversationTitle)
        assertEquals(peer, p.peerType)
        assertTrue(p.header!!.reliable)
        return p
    }
    private fun assertBlocked(s: AccessibilitySnapshot) {
        val p = parser.parse(s)
        assertFalse(p.reliable)
        assertNull(p.conversationTitle)
        assertEquals(PeerType.UNKNOWN, p.peerType)
        val r = WeComMessageDetector().detect(p)
        assertEquals(MessageDetectionStatus.BLOCKED_LOW_CONFIDENCE, r.status)
        assertTrue(r.newMessages.isEmpty())
    }

    @Test fun ordinaryEnterpriseContact() { assertHeader(fixture(title = "张三"), "张三", PeerType.UNKNOWN) }
    @Test fun personalWechatIdentityIsMetadata() {
        val p = assertHeader(fixture(personal = true), "崔玮杰", PeerType.PERSONAL_WECHAT)
        assertTrue(p.header!!.metadata.any { it.type == HeaderMetadataType.IDENTITY_LABEL && it.text == "@微信" })
        assertEquals(1, p.header.evidence.candidates.count { it.eligible })
    }
    @Test fun enterpriseStatusImageDoesNotCompete() {
        // Adjacent to the title in this enterprise layout, without the identity label's slot.
        assertHeader(fixture(title = "张三", extraHeader = listOf(NodeInput(className = "android.widget.ImageView",
            contentDescription = "忙碌", boundsInScreen = "[605,120][645,160]"))), "张三", PeerType.UNKNOWN)
    }
    @Test fun severalStatusIconsDoNotChangeTitleOrScore() {
        val plain = assertHeader(fixture(title = "张三"), "张三", PeerType.UNKNOWN)
        val decorated = assertHeader(fixture(title = "张三", extraHeader = listOf(
            NodeInput(className = "android.widget.ImageView", contentDescription = "状态一", boundsInScreen = "[605,120][645,160]"),
            NodeInput(className = "android.view.View", contentDescription = "状态二", boundsInScreen = "[650,120][690,160]"),
            NodeInput(className = "android.view.View", boundsInScreen = "[695,120][735,160]"))), "张三", PeerType.UNKNOWN)
        assertEquals(plain.header!!.evidence.selectedScore, decorated.header!!.evidence.selectedScore)
        assertEquals(3, decorated.header.metadata.size)
    }
    @Test fun personalIdentityAndMultipleIconsRemainOneGroup() {
        val p = assertHeader(fixture(personal = true, icons = 3), "崔玮杰", PeerType.PERSONAL_WECHAT)
        assertEquals(1, p.header!!.evidence.groups.size)
        assertEquals(4, p.header.metadata.size)
    }
    @Test fun statusBarClockIsNotTitle() {
        val p = assertHeader(fixture(extraTop = listOf(text("7:20", "[440,5][580,50]"))), "崔玮杰", PeerType.UNKNOWN)
        assertTrue(p.header!!.evidence.candidates.single { it.text == "7:20" }.negativeEvidence.contains("time-like-text"))
    }
    @Test fun chatTimestampIsNotTitleOrMessage() {
        val p = assertHeader(fixture(), "崔玮杰", PeerType.UNKNOWN)
        assertFalse(p.messages.any { it.content == "下午 7:17" })
        assertFalse(p.header!!.evidence.candidates.single { it.text == "下午 7:17" }.eligible)
    }
    @Test fun inputHintCannotBecomeTitleEvenWhenInHeader() {
        assertHeader(fixture(extraHeader = listOf(text("发消息或按住...", "[600,115][850,175]"))), "崔玮杰", PeerType.UNKNOWN)
        assertBlocked(fixture(title = "发消息或按住..."))
    }
    @Test fun sendButtonCannotBecomeTitle() {
        val p = assertHeader(fixture(extraHeader = listOf(NodeInput(className = "android.widget.Button", text = "发送",
            boundsInScreen = "[600,115][740,175]", clickable = true))), "崔玮杰", PeerType.UNKNOWN)
        assertFalse(p.header!!.evidence.candidates.single { it.text == "发送" }.eligible)
        assertFalse(p.messages.any { it.content == "发送" })
    }
    @Test fun changedViewIdStillWorks() { assertHeader(fixture(id = "com.tencent.wework:id/updated"), "崔玮杰", PeerType.UNKNOWN) }
    @Test fun absentViewIdStillWorks() { assertHeader(fixture(id = ""), "崔玮杰", PeerType.UNKNOWN) }
    @Test fun observedViewIdOnlyAddsSecondaryEvidence() {
        val p = assertHeader(fixture(id = "com.tencent.wework:id/nvw"), "崔玮杰", PeerType.UNKNOWN)
        assertEquals(3, p.header!!.evidence.candidates.single { it.eligible }.score.viewIdHintScore)
    }
    @Test fun scalesTo720By1600() { assertHeader(fixture(width = 720, height = 1600, personal = true), "崔玮杰", PeerType.PERSONAL_WECHAT) }
    @Test fun observed1080By2408SnapshotRegressesEndToEnd() {
        val s = fixture()
        assertEquals(80, s.nodeCount)
        assertEquals(17, s.maxDepth)
        assertEquals(4, s.textNodeCount)
        assertFalse(s.truncated)
        val p = assertHeader(s, "崔玮杰", PeerType.UNKNOWN)
        assertEquals(listOf("CUSTOMER_NEW_001"), p.messages.map { it.content })
        assertEquals(MessageDirection.INBOUND, p.messages.single().direction)
    }
    @Test fun scalesTo1440By3200() { assertHeader(fixture(width = 1440, height = 3200, personal = true), "崔玮杰", PeerType.PERSONAL_WECHAT) }
    @Test fun longContactNameIsNotFilteredByLength() {
        val title = "测试联系人名称可以很长且包含123号项目"
        assertHeader(fixture(title = title, titleBounds = "[200,110][850,174]", headerBounds = "[180,85][920,205]"), title, PeerType.UNKNOWN)
    }
    @Test fun longGroupTitleCanKeepUnknownPeerType() {
        val title = "很长的跨部门产品项目协作讨论群（123）"
        val p = assertHeader(fixture(title = title, titleBounds = "[200,110][850,174]", headerBounds = "[180,85][920,205]"), title, PeerType.UNKNOWN)
        assertTrue(p.header!!.reliable)
    }
    @Test fun independentEqualPrimaryTitlesFailClosed() {
        assertBlocked(fixture(extraTop = listOf(group("[400,85][860,205]", listOf(text("另一测试联系人", "[448,110][589,174]"))))))
    }
    @Test fun noReliableTitleFailsClosed() { assertBlocked(fixture(title = "")) }
    @Test fun identityWithoutPrimaryTitleFailsClosed() { assertBlocked(fixture(title = "", personal = true)) }
    @Test fun messageBodyNeverCompetesWithTitle() {
        val p = assertHeader(fixture(personal = true), "崔玮杰", PeerType.PERSONAL_WECHAT)
        assertEquals(listOf("CUSTOMER_NEW_001"), p.messages.map { it.content })
        assertTrue(p.header!!.evidence.candidates.single { it.text == "CUSTOMER_NEW_001" }.negativeEvidence.contains("message-region"))
    }
    @Test fun firstRepairedSnapshotOnlyCreatesBaseline() {
        for (personal in listOf(false, true)) {
            val r = WeComMessageDetector().detect(parser.parse(fixture(personal = personal)))
            assertEquals(MessageDetectionStatus.BASELINE_CREATED, r.status)
            assertEquals(1, r.messageCount)
            assertTrue(r.newMessages.isEmpty())
        }
    }
    @Test fun appendedCustomerMessageUsesExistingDetector() {
        for (personal in listOf(false, true)) {
            val d = WeComMessageDetector()
            d.detect(parser.parse(fixture(personal = personal)))
            val r = d.detect(parser.parse(fixture(personal = personal, time = 200,
                contents = listOf("CUSTOMER_NEW_001", "CUSTOMER_NEW_002"))))
            assertEquals(MessageDetectionStatus.NEW_MESSAGE, r.status)
            assertEquals("CUSTOMER_NEW_002", r.newMessages.single().content)
            assertEquals(MessageDirection.INBOUND, r.newMessages.single().direction)
        }
    }
    @Test fun narrowTitleParentDoesNotNeedScreenWideAncestor() { assertHeader(fixture(), "崔玮杰", PeerType.UNKNOWN) }
    @Test fun toolbarMayStartAtTopOfWindow() { assertHeader(fixture(wideToolbar = true, personal = true), "崔玮杰", PeerType.PERSONAL_WECHAT) }
    @Test fun unrelatedIdentityElsewhereInToolbarDoesNotChangePeer() {
        assertHeader(fixture(wideToolbar = true, extraTop = listOf(text("@微信", "[950,115][1060,170]"))), "崔玮杰", PeerType.UNKNOWN)
    }
    @Test fun iconDescriptionCannotBecomeNameOrIdentity() {
        val p = assertHeader(fixture(extraHeader = listOf(NodeInput(className = "android.widget.ImageView",
            contentDescription = "@微信", boundsInScreen = "[605,120][645,160]"))), "崔玮杰", PeerType.UNKNOWN)
        assertEquals(HeaderMetadataType.STATUS_ICON, p.header!!.metadata.single().type)
    }
    @Test fun iconWithTextStillIsNotPrimary() {
        assertHeader(fixture(extraHeader = listOf(NodeInput(className = "android.widget.ImageView", text = "工作中",
            boundsInScreen = "[605,120][645,160]"))), "崔玮杰", PeerType.UNKNOWN)
    }
    @Test fun symbolGlyphIsDecoration() {
        val p = assertHeader(fixture(extraHeader = listOf(text("★", "[605,120][645,160]"))), "崔玮杰", PeerType.UNKNOWN)
        assertEquals(HeaderMetadataType.UNKNOWN_DECORATION, p.header!!.metadata.single().type)
    }
    @Test fun wholeTimeAndDateLabelsCannotBePrimary() {
        for (value in listOf("6:37", "19:20", "下午 7:17", "上午 9:30", "昨天", "今天", "星期三", "周三", "9月30日", "2026年9月30日")) {
            assertTrue(value, isTimeLikeHeaderText(value))
            assertBlocked(fixture(title = value))
        }
    }
    @Test fun digitsInsideContactNameAreRetained() { assertHeader(fixture(title = "张三123号"), "张三123号", PeerType.UNKNOWN) }
    @Test fun emptyOrSymbolOnlyTitleFailsClosed() {
        assertBlocked(fixture(title = "  "))
        assertBlocked(fixture(title = "★ …"))
    }
    @Test fun knownViewIdCannotRescueMissingHeaderStructure() {
        assertBlocked(fixture(id = "com.tencent.wework:id/nvw", headerBounds = "[0,0][1080,2408]"))
    }
    @Test fun knownViewIdCannotBreakOtherwiseEqualTitleTie() {
        assertBlocked(fixture(id = "com.tencent.wework:id/nvw", extraHeader = listOf(text("同等可信标题", "[448,110][589,174]"))))
    }
    @Test fun scoreBelowMinimumFailsClosedEvenWithIdHint() {
        val s = fixture(titleBounds = "[170,370][280,430]", headerBounds = "[140,350][850,460]", id = "com.tencent.wework:id/nvw")
        assertBlocked(s)
        assertTrue(parser.parse(s).header!!.evidence.selectedScore!! < ConversationHeaderResolver.MINIMUM_SCORE)
    }
    @Test fun insufficientScoreMarginFailsClosed() {
        val s = fixture(wideToolbar = true, extraTop = listOf(text("稍偏左的另一个标题", "[180,110][290,174]")))
        assertBlocked(s)
        val e = parser.parse(s).header!!.evidence
        assertEquals(10, e.selectedScore!! - e.runnerUpScore!!)
    }
    @Test fun changingStatusIconsDoesNotCreateMessages() {
        val d = WeComMessageDetector()
        d.detect(parser.parse(fixture(personal = true)))
        val r = d.detect(parser.parse(fixture(personal = true, icons = 3, time = 200)))
        assertEquals(MessageDetectionStatus.DUPLICATE_EVENT, r.status)
        assertTrue(r.newMessages.isEmpty())
    }
    @Test fun ambiguousRepairStillBlocksThenFreshSuccessBaselines() {
        val d = WeComMessageDetector()
        val blocked = d.detect(parser.parse(fixture(title = "", personal = true)))
        assertEquals(MessageDetectionStatus.BLOCKED_LOW_CONFIDENCE, blocked.status)
        val r = d.detect(parser.parse(fixture(personal = true, time = 200)))
        assertEquals(MessageDetectionStatus.BASELINE_CREATED, r.status)
        assertTrue(r.newMessages.isEmpty())
    }
    @Test fun diagnosticsExposeCandidatesMetadataScoresAndRejectionReasons() {
        val p = parser.parse(fixture(personal = true, icons = 1, id = "com.tencent.wework:id/nvw"))
        val report = MessageDiagnosticReport.parsed(p)
        for (field in listOf("标题组数量=1", "Primary Title 候选数=1", "nodeIndex=", "parentIndex=", "grandparentIndex=",
            "bounds=", "className=", "viewId=", "score=", "positiveEvidence=", "negativeEvidence=", "@微信",
            "STATUS_ICON", "conversationTitle=崔玮杰", "organizationName=null", "peerType=PERSONAL_WECHAT", "titleReliable=true", "selectedScore=", "runnerUpScore=",
            "header.reliable=true", "confidenceReason=", "role-IDENTITY_LABEL", "message-region")) {
            assertTrue(field, report.contains(field))
        }
    }
    @Test fun failureDiagnosticsKeepBothPrimaryCandidates() {
        val p = parser.parse(fixture(extraHeader = listOf(text("同等可信标题", "[448,110][589,174]"))))
        val report = MessageDiagnosticReport.parsed(p)
        assertTrue(report.contains("Primary Title 候选数=2"))
        assertTrue(report.contains("主标题歧义"))
        assertTrue(report.contains("conversationTitle=null"))
        assertTrue(report.contains("同等可信标题"))
    }

    // Synthetic tree: the supplied device node indexes, ancestry, bounds, classes and IDs are
    // encoded below; surrounding ancestors/input/message rows are constructed for parser context.
    @Test fun observedNode22And24BecomeNameAndOrganization() {
        val s = fixture(title = "崔玮杰", id = "com.tencent.wework:id/nwv",
            titleBounds = "[470,89][611,153]", headerBounds = "[330,80][750,205]",
            extraHeader = listOf(text("＠厦门凯南展示制品有限公司", "[345,153][735,194]", "com.tencent.wework:id/nx5")))
        val p = parser.parse(s)
        assertTrue(MessageDiagnosticReport.parsed(p), p.header?.titleReliable == true)
        assertEquals("崔玮杰", p.conversationTitle)
        assertEquals("厦门凯南展示制品有限公司", p.organizationName)
        assertEquals(PeerType.EXTERNAL_WECOM, p.peerType)
        assertEquals(1, p.header!!.evidence.candidates.count { it.eligible })
    }

    @Test fun halfAndFullWidthWechatLabelsClassifyPersonal() {
        for (badge in listOf("@微信", "＠微信")) {
            val p = parser.parse(fixture(title = "Hello World", extraHeader = listOf(text(badge, "[596,153][690,194]"))))
            assertTrue(MessageDiagnosticReport.parsed(p), p.reliable)
            assertEquals("Hello World", p.conversationTitle)
            assertNull(p.organizationName)
            assertEquals(PeerType.PERSONAL_WECHAT, p.peerType)
        }
    }

    @Test fun noIdentityEvidenceRemainsUnknownAndConflictsStayUnknown() {
        assertEquals(PeerType.UNKNOWN, parser.parse(fixture(title = "王蕊")).peerType)
        val conflict = parser.parse(fixture(personal = true, extraHeader = listOf(text("＠厦门微信科技有限公司", "[596,153][850,194]"))))
        assertEquals(PeerType.UNKNOWN, conflict.peerType)
    }

    @Test fun rightAlignedRealDeviceGeometryJoinsSeparateTextParents() {
        // Synthetic surrounding tree; these bounds and node IDs mirror the supplied Redmi capture.
        val orgRow = group("[600,350][945,395]", listOf(text("＠厦门凯南展示制品有限公司",
            "[612,355][933,389]", "com.tencent.wework:id/nx5")))
        val p = parser.parse(fixture(title = "崔玮杰", id = "com.tencent.wework:id/nwv",
            titleBounds = "[713,302][829,355]", headerBounds = "[590,285][950,410]",
            extraHeader = listOf(orgRow)))
        assertTrue(MessageDiagnosticReport.parsed(p), p.header?.titleReliable == true)
        assertEquals("崔玮杰", p.conversationTitle)
        assertEquals("厦门凯南展示制品有限公司", p.organizationName)
        assertEquals(PeerType.EXTERNAL_WECOM, p.peerType)
        assertEquals(1, p.header!!.evidence.candidates.count { it.eligible })
        val titleCandidate = p.header.evidence.candidates.single { it.text == "崔玮杰" }
        val org = p.header.metadata.single { it.type == HeaderMetadataType.ORGANIZATION_SUBTITLE }
        assertEquals(titleCandidate.groupId, p.header.evidence.groups.single { it.nodeIndices.contains(org.nodeIndex) }.id)
    }
}
