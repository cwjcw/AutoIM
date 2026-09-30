package com.autoim.agent

import android.content.ContextWrapper
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import java.io.File

class StructureDiagnosticsTest {
    private fun snapshot(root: NodeInput, width: Int = 1000, height: Int = 2000) =
        DiagnosticLogic.buildSnapshot(root, DiagnosticLogic.WECOM_PACKAGE, 123L, width, height, "test")!!

    private fun family(): AccessibilitySnapshot = snapshot(NodeInput(className = "root", children = listOf(
        NodeInput(className = "parent", clickable = true, children = listOf(
            NodeInput(text = "previous"), NodeInput(text = "AutoIM测试001"), NodeInput(contentDescription = "next"))),
        NodeInput(text = "other branch"))))

    @Test fun parentIndicesUseUniqueFlattenedIndices() {
        val nodes = family().nodes
        assertEquals(listOf(0, 1, 2, 3, 4, 5), nodes.map { it.nodeIndex })
        assertEquals(listOf(null, 0, 1, 1, 1, 0), nodes.map { it.parentIndex })
        assertEquals(listOf(0, 0, 0, 1, 2, 1), nodes.map { it.childIndex })
    }

    @Test fun siblingsAreDirectChildrenNotFlatNeighbors() {
        val diagnostics = StructureDiagnostics(family())
        val message = diagnostics.search("AutoIM测试001").single()
        assertEquals("previous", message.previousSibling?.text)
        assertEquals("next", message.nextSibling?.contentDescription)
        val branch = diagnostics.search("other branch").single()
        assertEquals("parent", branch.previousSibling?.className)
        assertNull(branch.nextSibling)
    }

    @Test fun grandparentIsCorrect() {
        val record = StructureDiagnostics(family()).search("AutoIM测试001").single()
        assertEquals(1, record.parent?.nodeIndex)
        assertEquals(0, record.grandparent?.nodeIndex)
        assertEquals("root", record.grandparent?.className)
    }

    @Test fun rootHasNullRelativesAndExplicitNullReport() {
        val diagnostics = StructureDiagnostics(snapshot(NodeInput(text = "root")))
        val record = diagnostics.search("root").single()
        assertNull(record.parent); assertNull(record.grandparent)
        assertNull(record.previousSibling); assertNull(record.nextSibling)
        val report = StructureReport.detail(record)
        assertTrue(report.contains("PARENT:\nnull"))
        assertTrue(report.contains("GRANDPARENT:\nnull"))
        assertTrue(report.contains("PREVIOUS_SIBLING:\nnull"))
        assertTrue(report.contains("NEXT_SIBLING:\nnull"))
    }

    @Test fun geometryClassifiesLeftCenterRight() {
        val diagnostics = StructureDiagnostics(snapshot(NodeInput(children = listOf(
            NodeInput(boundsInScreen = "[0,0][200,100]"),
            NodeInput(boundsInScreen = "[400,0][600,100]"),
            NodeInput(boundsInScreen = "[800,0][1000,100]")))))
        assertEquals(listOf(ScreenPosition.LEFT, ScreenPosition.CENTER, ScreenPosition.RIGHT),
            diagnostics.snapshot.nodes.drop(1).map(diagnostics::screenPosition))
    }

    @Test fun geometryThresholdsAreCenterAtExactlyFortyAndSixtyPercent() {
        val diagnostics = StructureDiagnostics(snapshot(NodeInput(children = listOf(
            NodeInput(boundsInScreen = "[300,0][500,100]"), NodeInput(boundsInScreen = "[500,0][700,100]")))))
        assertTrue(diagnostics.snapshot.nodes.drop(1).all { diagnostics.screenPosition(it) == ScreenPosition.CENTER })
    }

    @Test fun unavailableDimensionsNeverGuessGeometry() {
        val diagnostics = StructureDiagnostics(snapshot(NodeInput(text = "title", boundsInScreen = "[0,0][200,100]"), 0, 0))
        assertNull(diagnostics.screenPosition(diagnostics.snapshot.nodes.single()))
        assertTrue(diagnostics.topTextCandidates.isEmpty())
        assertTrue(StructureReport.summary(diagnostics.snapshot).contains("请重新采集"))
    }

    @Test fun malformedOrOffscreenBoundsNeverGuessPosition() {
        val diagnostics = StructureDiagnostics(snapshot(NodeInput(children = listOf(
            NodeInput(boundsInScreen = "garbage"), NodeInput(boundsInScreen = "[10,0][5,100]"),
            NodeInput(boundsInScreen = "[-100,0][20,100]"), NodeInput(boundsInScreen = "[900,0][1200,100]"),
            NodeInput(boundsInScreen = "[9999999999999999,0][100,200]")))))
        assertTrue(diagnostics.snapshot.nodes.drop(1).all { diagnostics.screenPosition(it) == null })
    }

    @Test fun editableIsInputCandidate() {
        val diagnostics = StructureDiagnostics(snapshot(NodeInput(children = listOf(NodeInput(editable = true, className = "custom")))))
        assertEquals("custom", diagnostics.inputCandidates.single().className)
    }

    @Test fun editTextClassIsInputCandidateEvenWhenNotEditable() {
        val diagnostics = StructureDiagnostics(snapshot(NodeInput(className = "android.widget.EditText")))
        assertEquals(1, diagnostics.inputCandidates.size)
    }

    @Test fun emptyInputCandidatesAreExplicit() {
        val sections = StructureReport.sections(StructureDiagnostics(snapshot(NodeInput())))
        assertEquals("未发现输入节点候选", sections.toMap()["输入节点候选"])
    }

    @Test fun sendTextMatchesExactAndContainedLabels() {
        val diagnostics = StructureDiagnostics(snapshot(NodeInput(children = listOf(
            NodeInput(text = "发送"), NodeInput(text = "点击发送消息"), NodeInput(text = "其他")))))
        assertEquals(listOf("发送", "点击发送消息"), diagnostics.sendCandidates.map { it.text })
    }

    @Test fun sendDescriptionMatchesAndShowsClickableParent() {
        val diagnostics = StructureDiagnostics(snapshot(NodeInput(clickable = true, children = listOf(
            NodeInput(contentDescription = "发送"), NodeInput(contentDescription = "发送消息")))))
        assertEquals(2, diagnostics.sendCandidates.size)
        val record = diagnostics.describe(diagnostics.sendCandidates.first())
        assertFalse(record.node.clickable)
        assertTrue(record.parent!!.clickable)
    }

    @Test fun scrollableCandidatesIncludeOnlyScrollableNodes() {
        val diagnostics = StructureDiagnostics(snapshot(NodeInput(children = listOf(
            NodeInput(className = "list", scrollable = true), NodeInput(className = "other")))))
        assertEquals("list", diagnostics.scrollableCandidates.single().className)
    }

    @Test fun topTwentyPercentUsesCapturedScreenHeightAndWholeBounds() {
        val diagnostics = StructureDiagnostics(snapshot(NodeInput(children = listOf(
            NodeInput(text = "title", boundsInScreen = "[100,50][800,150]"),
            NodeInput(text = "edge", boundsInScreen = "[100,350][800,400]"),
            NodeInput(text = "crossing", boundsInScreen = "[100,350][800,401]"),
            NodeInput(text = "body", boundsInScreen = "[100,900][800,1000]"),
            NodeInput(contentDescription = "desc only", boundsInScreen = "[100,0][800,100]"),
            NodeInput(text = "outside", boundsInScreen = "[100,-100][800,100]")))))
        assertEquals(listOf("title", "edge"), diagnostics.topTextCandidates.map { it.text })
    }

    @Test fun searchMatchesTextSubstringAndDescription() {
        val diagnostics = StructureDiagnostics(family())
        assertEquals(1, diagnostics.search("测试001").size)
        assertEquals("next", diagnostics.search(" NEXT ").single().node.contentDescription)
        assertEquals(1, diagnostics.search("previous").size)
    }

    @Test fun absentAndBlankSearchReturnEmpty() {
        val diagnostics = StructureDiagnostics(family())
        assertTrue(diagnostics.search("missing").isEmpty())
        assertTrue(diagnostics.search("   ").isEmpty())
    }

    @Test fun inaccessibleSiblingSlotRemainsNull() {
        val diagnostics = StructureDiagnostics(snapshot(NodeInput(children = listOf(
            NodeInput(text = "first"), null, NodeInput(text = "third")))))
        assertNull(diagnostics.search("first").single().nextSibling)
        assertNull(diagnostics.search("third").single().previousSibling)
        assertEquals(2, diagnostics.search("third").single().node.childIndex)
        assertEquals(3, diagnostics.snapshot.nodes.first().childCount)
    }

    @Test fun reportedChildCountSurvivesCappedTree() {
        val result = snapshot(NodeInput(children = listOf(NodeInput()), reportedChildCount = 9, capped = true))
        assertEquals(9, result.nodes.first().childCount)
        assertTrue(result.truncated)
    }

    @Test fun descriptionOnlyNodeIsKeyText() {
        val diagnostics = StructureDiagnostics(snapshot(NodeInput(contentDescription = "description")))
        assertEquals(1, diagnostics.keyTextNodes.size)
    }

    @Test fun structureExportExcludesFullTreeAndNonKeyDescendants() {
        val diagnostics = StructureDiagnostics(snapshot(NodeInput(children = listOf(
            NodeInput(text = "message"), NodeInput(children = listOf(NodeInput(className = "HIDDEN_TREE_ONLY")))))))
        val export = StructureReport.export(diagnostics, "test device", "test version")
        assertTrue(export.contains("test device")); assertTrue(export.contains("test version"))
        assertTrue(export.contains("TEXT:\nmessage")); assertFalse(export.contains("完整 Node Tree"))
        assertFalse(export.contains("HIDDEN_TREE_ONLY"))
        assertTrue(StructureReport.fullTree(diagnostics.snapshot).contains("HIDDEN_TREE_ONLY"))
    }

    @Test fun diagnosticsDoNotAssignMessageDirectionOrVerifiedTitle() {
        val export = StructureReport.export(StructureDiagnostics(family()), "device", "wecom")
        assertFalse(export.contains("INBOUND")); assertFalse(export.contains("OUTBOUND"))
        assertFalse(export.contains("VERIFIED"))
    }

    @Test fun newMetadataRoundTripsIncludingDimensionsAndRootNullParent() {
        withStore { store, _ ->
            val source = family()
            store.saveSnapshot(source)
            assertEquals(source, store.readSnapshot())
        }
    }

    @Test fun legacySnapshotsRestorePreorderRelationsWithoutInventingScreenDimensions() {
        withStore { store, directory ->
            store.saveSnapshot(family())
            val file = File(directory, "latest-accessibility-snapshot.json")
            val json = JSONObject(file.readText())
            listOf("schemaVersion", "screenWidth", "screenHeight", "screenMetricsSource").forEach(json::remove)
            val nodes = json.getJSONArray("nodes")
            for (i in 0 until nodes.length()) {
                listOf("nodeIndex", "parentIndex", "childIndex").forEach(nodes.getJSONObject(i)::remove)
            }
            file.writeText(json.toString())
            val restored = store.readSnapshot()!!
            assertEquals(family().nodes, restored.nodes)
            assertEquals(0, restored.screenWidth)
            assertEquals("root", StructureDiagnostics(restored).search("AutoIM测试001").single().grandparent?.className)
        }
    }

    @Test fun invalidStoredParentFailsSafely() {
        withStore { store, directory ->
            store.saveSnapshot(family())
            val file = File(directory, "latest-accessibility-snapshot.json")
            val json = JSONObject(file.readText())
            json.getJSONArray("nodes").getJSONObject(3).put("parentIndex", 5)
            file.writeText(json.toString())
            assertNull(store.readSnapshot())
        }
    }

    @Test fun invalidLegacyDepthJumpIsNotGuessed() {
        withStore { store, directory ->
            store.saveSnapshot(family())
            val file = File(directory, "latest-accessibility-snapshot.json")
            val json = JSONObject(file.readText()).put("schemaVersion", 1)
            json.getJSONArray("nodes").getJSONObject(1).put("depth", 8)
            file.writeText(json.toString())
            assertNull(store.readSnapshot())
        }
    }

    private fun withStore(action: (DiagnosticStore, File) -> Unit) {
        val directory = java.nio.file.Files.createTempDirectory("autoim-structure-").toFile()
        try {
            val context = object : ContextWrapper(null) { override fun getFilesDir() = directory }
            action(DiagnosticStore(context), directory)
        } finally { directory.deleteRecursively() }
    }
}
