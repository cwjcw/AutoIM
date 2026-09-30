package com.autoim.agent

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

class DiagnosticStore(context: Context) {
    private val directory = context.filesDir
    private val snapshotFile = File(directory, "latest-accessibility-snapshot.json")
    private val notificationsFile = File(directory, "wecom-notifications.json")

    fun saveSnapshot(snapshot: AccessibilitySnapshot) = writeAtomically(snapshotFile, snapshot.toJson().toString())

    fun readSnapshot(): AccessibilitySnapshot? = runCatching {
        if (!snapshotFile.exists()) return null
        snapshotFile.readText().toSnapshot()
    }.getOrNull()

    fun saveNotifications(records: List<NotificationRecord>) {
        val json = JSONArray()
        DiagnosticLogic.boundedNotifications(records).forEach { record -> json.put(record.toJson()) }
        writeAtomically(notificationsFile, json.toString())
    }

    fun readNotifications(): List<NotificationRecord> = runCatching {
        if (!notificationsFile.exists()) return emptyList()
        val json = JSONArray(notificationsFile.readText())
        (0 until json.length()).mapNotNull { index -> runCatching { json.getJSONObject(index).toNotification() }.getOrNull() }
            .let(DiagnosticLogic::boundedNotifications)
    }.getOrDefault(emptyList())

    fun clear() {
        snapshotFile.delete()
        notificationsFile.delete()
    }

    private fun writeAtomically(target: File, content: String) {
        runCatching {
            val temp = File(directory, "${target.name}.tmp")
            temp.writeText(content)
            if (!temp.renameTo(target)) {
                target.writeText(content)
                temp.delete()
            }
        }
    }
}

private fun AccessibilitySnapshot.toJson() = JSONObject().apply {
    put("schemaVersion", 3)
    put("trigger", JSONObject().apply {
        put("eventType", trigger.eventType); put("eventTime", trigger.eventTime)
        put("eventPackage", trigger.eventPackage); put("eventClassName", trigger.eventClassName)
        put("coalescedEventTypes", JSONArray(trigger.coalescedEventTypes.toList()))
    })
    put("screenWidth", screenWidth); put("screenHeight", screenHeight); put("screenMetricsSource", screenMetricsSource)
    put("captureTime", captureTime); put("packageName", packageName); put("rootClass", rootClass)
    put("nodeCount", nodeCount); put("maxDepth", maxDepth); put("textNodeCount", textNodeCount)
    put("clickableNodeCount", clickableNodeCount); put("editableNodeCount", editableNodeCount)
    put("scrollableNodeCount", scrollableNodeCount); put("truncated", truncated)
    put("nodes", JSONArray().also { array -> nodes.forEach { array.put(it.toJson()) } })
}

private fun NodeSnapshot.toJson() = JSONObject().apply {
    put("nodeIndex", nodeIndex); put("parentIndex", parentIndex ?: JSONObject.NULL); put("childIndex", childIndex)
    put("depth", depth); put("index", index); put("className", className); put("text", text)
    put("contentDescription", contentDescription); put("viewIdResourceName", viewIdResourceName)
    put("boundsInScreen", boundsInScreen); put("clickable", clickable); put("longClickable", longClickable)
    put("visibleToUser", visibleToUser)
    put("focusable", focusable); put("focused", focused); put("editable", editable); put("scrollable", scrollable)
    put("enabled", enabled); put("selected", selected); put("password", password); put("childCount", childCount)
}

private fun String.toSnapshot(): AccessibilitySnapshot {
    val json = JSONObject(this)
    val nodes = json.getJSONArray("nodes")
    require(nodes.length() <= DiagnosticLogic.MAX_NODES)
    require(json.getString("packageName") == DiagnosticLogic.WECOM_PACKAGE)
    val ancestors = mutableListOf<Int>()
    val restored = (0 until nodes.length()).map { position ->
        val nodeJson = nodes.getJSONObject(position)
        val node = nodeJson.toNodeSnapshot()
        require(node.depth in 0..DiagnosticLogic.MAX_DEPTH && node.childIndex >= 0)
        // 0.1.0 stored preorder depth and sibling index, enough to restore its tree.
        require(if (position == 0) node.depth == 0 else node.depth > 0 && node.depth <= ancestors.size)
        while (ancestors.size > node.depth) ancestors.removeAt(ancestors.lastIndex)
        val parent = ancestors.lastOrNull()
        if (json.optInt("schemaVersion", 1) >= 2) {
            require(node.nodeIndex == position && node.parentIndex == parent)
        }
        ancestors.add(position)
        node.copy(nodeIndex = position, parentIndex = parent)
    }
    return AccessibilitySnapshot(json.getLong("captureTime"), json.getString("packageName"),
        json.optString("rootClass"), restored.size, json.optInt("maxDepth"), json.optInt("textNodeCount"),
        json.optInt("clickableNodeCount"), json.optInt("editableNodeCount"), json.optInt("scrollableNodeCount"),
        json.optBoolean("truncated"), restored, json.optInt("screenWidth").coerceAtLeast(0),
        json.optInt("screenHeight").coerceAtLeast(0), json.optString("screenMetricsSource", "unknown"),
        json.optJSONObject("trigger")?.let { trigger ->
            val types = trigger.optJSONArray("coalescedEventTypes") ?: JSONArray()
            require(types.length() <= 32)
            SnapshotTrigger(trigger.optInt("eventType"), trigger.optLong("eventTime"),
                trigger.optString("eventPackage"), trigger.optString("eventClassName"),
                (0 until types.length()).map { types.getInt(it) }.toSet())
        } ?: SnapshotTrigger())
}

private fun JSONObject.toNodeSnapshot() = NodeSnapshot(
    optInt("depth"), optInt("index"), optString("className"), optString("text"),
    optString("contentDescription"), optString("viewIdResourceName"), optString("boundsInScreen"),
    optBoolean("clickable"), optBoolean("longClickable"), optBoolean("focusable"), optBoolean("focused"),
    optBoolean("editable"), optBoolean("scrollable"), optBoolean("enabled"), optBoolean("selected"),
    optBoolean("password"), optInt("childCount"),
    optInt("nodeIndex", -1), if (isNull("parentIndex")) null else optInt("parentIndex"),
    optInt("childIndex", optInt("index")),
    optBoolean("visibleToUser", true),
)

private fun NotificationRecord.toJson() = JSONObject().apply {
    put("packageName", packageName); put("postTime", postTime); put("notificationId", notificationId)
    put("notificationKey", notificationKey); put("title", title); put("text", text)
    put("subText", subText); put("bigText", bigText); put("category", category)
}

private fun JSONObject.toNotification() = NotificationRecord(
    getString("packageName"), optLong("postTime"), optInt("notificationId"), optString("notificationKey"),
    optString("title"), optString("text"), optString("subText"), optString("bigText"), optString("category"),
)
