package com.autoim.agent

import android.content.ContextWrapper
import org.junit.Assert.*
import org.junit.Test
import java.io.File

class DiagnosticLogicTest {
    @Test fun otherPackageDoesNotEnterSnapshotPath() {
        assertFalse(DiagnosticLogic.shouldCapture(true, "com.example.other"))
        assertNull(DiagnosticLogic.buildSnapshot(NodeInput(), "com.example.other"))
    }

    @Test fun targetPackageCanEnterSnapshotPath() {
        assertTrue(DiagnosticLogic.shouldCapture(true, DiagnosticLogic.WECOM_PACKAGE))
        assertNotNull(DiagnosticLogic.buildSnapshot(NodeInput(), DiagnosticLogic.WECOM_PACKAGE))
    }

    @Test fun nullRootFailsSafely() { assertNull(DiagnosticLogic.buildSnapshot(null, DiagnosticLogic.WECOM_PACKAGE)) }

    @Test fun maximumDepthIsEnforced() {
        var node = NodeInput(className = "leaf")
        repeat(50) { node = NodeInput(className = "node", children = listOf(node)) }
        val result = DiagnosticLogic.buildSnapshot(node, DiagnosticLogic.WECOM_PACKAGE)!!
        assertTrue(result.truncated)
        assertTrue(result.maxDepth <= DiagnosticLogic.MAX_DEPTH)
    }

    @Test fun maximumNodeCountIsEnforced() {
        val root = NodeInput(children = List(DiagnosticLogic.MAX_NODES + 100) { NodeInput() })
        val result = DiagnosticLogic.buildSnapshot(root, DiagnosticLogic.WECOM_PACKAGE)!!
        assertEquals(DiagnosticLogic.MAX_NODES, result.nodeCount)
        assertTrue(result.truncated)
    }

    @Test fun textIsCopied() {
        val result = DiagnosticLogic.buildSnapshot(NodeInput(text = "message"), DiagnosticLogic.WECOM_PACKAGE)!!
        assertEquals("message", result.nodes.single().text)
        assertEquals(1, result.textNodeCount)
    }

    @Test fun contentDescriptionIsCopied() {
        val result = DiagnosticLogic.buildSnapshot(NodeInput(contentDescription = "描述"), DiagnosticLogic.WECOM_PACKAGE)!!
        assertEquals("描述", result.nodes.single().contentDescription)
    }

    @Test fun emptyTextDoesNotAppearInSummary() {
        val result = DiagnosticLogic.buildSnapshot(NodeInput(text = "  "), DiagnosticLogic.WECOM_PACKAGE)!!
        assertTrue(DiagnosticLogic.visibleText(result.nodes).isEmpty())
    }

    @Test fun visibleTextDeduplicatesTextAndDescriptionInOrder() {
        val result = DiagnosticLogic.buildSnapshot(NodeInput(text = "同一", contentDescription = "同一", children = listOf(NodeInput(text = "第二"))), DiagnosticLogic.WECOM_PACKAGE)!!
        assertEquals(listOf("同一", "第二"), DiagnosticLogic.visibleText(result.nodes))
    }

    @Test fun notificationsAcceptOnlyWeCom() {
        assertTrue(DiagnosticLogic.acceptsNotification(DiagnosticLogic.WECOM_PACKAGE))
        assertFalse(DiagnosticLogic.acceptsNotification("com.example.other"))
    }

    @Test fun otherAppNotificationBodyIsFiltered() {
        val other = notification("com.example.other", "private body")
        assertTrue(DiagnosticLogic.boundedNotifications(listOf(other)).isEmpty())
    }

    @Test fun notificationsAreLimitedToTwentyMostRecent() {
        val items = (1..25).map { notification(DiagnosticLogic.WECOM_PACKAGE, it.toString(), it.toLong()) }
        val result = DiagnosticLogic.boundedNotifications(items)
        assertEquals(20, result.size)
        assertEquals("6", result.first().text)
        assertEquals("25", result.last().text)
    }

    @Test fun clearRemovesSnapshotAndNotificationData() {
        val store = newStore()
        val snap = DiagnosticLogic.buildSnapshot(NodeInput(text = "test"), DiagnosticLogic.WECOM_PACKAGE)!!
        store.saveSnapshot(snap)
        store.saveNotifications(listOf(notification(DiagnosticLogic.WECOM_PACKAGE, "body")))
        store.clear()
        assertNull(store.readSnapshot())
        assertTrue(store.readNotifications().isEmpty())
    }

    @Test fun snapshotSerializationRoundTrips() {
        val store = newStore()
        val source = DiagnosticLogic.buildSnapshot(NodeInput(text = "hello", clickable = true, children = listOf(NodeInput(contentDescription = "desc"))), DiagnosticLogic.WECOM_PACKAGE, 123L)!!
        store.saveSnapshot(source)
        assertEquals(source, store.readSnapshot())
    }

    @Test fun corruptDiagnosticsAreIgnoredWithoutCrash() {
        val store = newStore()
        File(storeDir, "latest-accessibility-snapshot.json").writeText("not json")
        File(storeDir, "wecom-notifications.json").writeText("[")
        assertNull(store.readSnapshot())
        assertTrue(store.readNotifications().isEmpty())
    }

    private var storeDir: File = java.nio.file.Files.createTempDirectory("autoim-test-").toFile()
    private fun newStore(): DiagnosticStore {
        storeDir = java.nio.file.Files.createTempDirectory("autoim-test-").toFile()
        val context = object : ContextWrapper(null) { override fun getFilesDir(): File = storeDir }
        return DiagnosticStore(context)
    }

    private fun notification(pkg: String, text: String, time: Long = 1L) =
        NotificationRecord(pkg, time, 1, "key", "title", text, "", "", "")
}
