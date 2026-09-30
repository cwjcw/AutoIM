package com.autoim.agent

import android.accessibilityservice.AccessibilityService
import android.graphics.Rect
import android.os.Handler
import android.os.HandlerThread
import android.os.Build
import android.util.DisplayMetrics
import android.view.WindowManager
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo

class AutoIMAccessibilityService : AccessibilityService() {
    private lateinit var workerThread: HandlerThread
    private lateinit var worker: Handler
    private val preferences by lazy { getSharedPreferences(PREFS, MODE_PRIVATE) }
    private val store by lazy { DiagnosticStore(this) }
    private var pendingCapture: Runnable? = null
    private val pendingEventTypes = mutableSetOf<Int>()

    override fun onServiceConnected() {
        super.onServiceConnected()
        MessageDiagnostics.invalidateContinuity()
        connected = true
    }

    override fun onCreate() {
        super.onCreate()
        workerThread = HandlerThread("AutoIM-Uia-Snapshot").apply { start() }
        worker = Handler(workerThread.looper)
        MessageDiagnostics.startSession()
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        if (event == null || !DiagnosticLogic.shouldCapture(preferences.getBoolean(KEY_DIAGNOSTIC, false), event.packageName?.toString())) return
        operational = true
        preferences.edit().putBoolean(KEY_OPERATIONAL, true).apply()
        val eventPackage = event.packageName?.toString() ?: return
        val trigger = SnapshotTrigger(event.eventType, event.eventTime, eventPackage, event.className?.toString().orEmpty())
        // Keep scroll/window evidence even when a later content event replaces the pending capture.
        worker.post {
            pendingCapture?.let(worker::removeCallbacks)
            pendingEventTypes += trigger.eventType
            pendingCapture = Runnable {
                val combined = trigger.copy(coalescedEventTypes = pendingEventTypes.toSet())
                pendingEventTypes.clear()
                capture(combined)
            }.also { worker.postDelayed(it, DEBOUNCE_MS) }
        }
    }

    private fun capture(trigger: SnapshotTrigger) {
        if (!preferences.getBoolean(KEY_DIAGNOSTIC, false) || trigger.eventPackage != DiagnosticLogic.WECOM_PACKAGE) {
            MessageDiagnostics.invalidateContinuity()
            return
        }
        val root = runCatching { super.getRootInActiveWindow() }.getOrNull() ?: run {
            MessageDiagnostics.invalidateContinuity()
            return
        }
        try {
            val rootPackage = runCatching { root.packageName?.toString() }.getOrNull()
            if (rootPackage != DiagnosticLogic.WECOM_PACKAGE) {
                MessageDiagnostics.invalidateContinuity()
                return
            }
            val copied = copyTree(root)
            val dimensions = runCatching {
                val manager = getSystemService(WINDOW_SERVICE) as WindowManager
                if (Build.VERSION.SDK_INT >= 30) {
                    val bounds = manager.maximumWindowMetrics.bounds
                    Triple(bounds.width(), bounds.height(), "maximumWindowMetrics")
                } else {
                    val metrics = DisplayMetrics()
                    @Suppress("DEPRECATION")
                    manager.defaultDisplay.getRealMetrics(metrics)
                    Triple(metrics.widthPixels, metrics.heightPixels, "getRealMetrics")
                }
            }.getOrDefault(Triple(0, 0, "unknown"))
            val snapshot = DiagnosticLogic.buildSnapshot(copied, rootPackage,
                screenWidth = dimensions.first, screenHeight = dimensions.second,
                screenMetricsSource = dimensions.third, trigger = trigger) ?: return
            store.saveSnapshot(snapshot)
            MessageDiagnostics.accept(snapshot)
            preferences.edit().putBoolean(KEY_OPERATIONAL, true).apply()
            operational = true
        } catch (_: RuntimeException) {
            // A stale platform tree must invalidate the old conversation's continuity.
            MessageDiagnostics.invalidateContinuity()
        } finally {
            runCatching { root.recycle() }
        }
    }

    private fun copyTree(root: AccessibilityNodeInfo): NodeInput {
        var copiedCount = 0
        fun copy(node: AccessibilityNodeInfo, depth: Int): NodeInput {
            copiedCount++
            val rect = Rect()
            val bounds = runCatching { node.getBoundsInScreen(rect); "[${rect.left},${rect.top}][${rect.right},${rect.bottom}]" }.getOrDefault("")
            var textCapped = false
            fun text(value: CharSequence?): String {
                val raw = runCatching { value?.toString().orEmpty() }.getOrDefault("")
                if (raw.length > DiagnosticLogic.MAX_TEXT_LENGTH) textCapped = true
                return raw.take(DiagnosticLogic.MAX_TEXT_LENGTH)
            }
            val childCount = runCatching { node.childCount }.getOrDefault(0).coerceAtLeast(0)
            val base = NodeInput(
                className = runCatching { node.className?.toString().orEmpty() }.getOrDefault(""),
                text = text(runCatching { node.text }.getOrNull()),
                contentDescription = text(runCatching { node.contentDescription }.getOrNull()),
                viewIdResourceName = runCatching { node.viewIdResourceName.orEmpty() }.getOrDefault(""),
                boundsInScreen = bounds,
                clickable = runCatching { node.isClickable }.getOrDefault(false),
                longClickable = runCatching { node.isLongClickable }.getOrDefault(false),
                focusable = runCatching { node.isFocusable }.getOrDefault(false),
                focused = runCatching { node.isFocused }.getOrDefault(false),
                editable = runCatching { node.isEditable }.getOrDefault(false),
                scrollable = runCatching { node.isScrollable }.getOrDefault(false),
                enabled = runCatching { node.isEnabled }.getOrDefault(false),
                selected = runCatching { node.isSelected }.getOrDefault(false),
                password = runCatching { node.isPassword }.getOrDefault(false),
                visibleToUser = runCatching { node.isVisibleToUser }.getOrDefault(false),
                reportedChildCount = childCount,
                capped = textCapped,
            )
            if (depth >= DiagnosticLogic.MAX_DEPTH || copiedCount >= DiagnosticLogic.MAX_NODES) {
                return base.copy(capped = base.capped || childCount > 0)
            }
            val children = ArrayList<NodeInput?>(minOf(childCount, DiagnosticLogic.MAX_NODES - copiedCount))
            for (index in 0 until childCount) {
                if (copiedCount >= DiagnosticLogic.MAX_NODES) break
                val child = runCatching { node.getChild(index) }.getOrNull()
                if (child == null) {
                    children += null
                    continue
                }
                try { children += copy(child, depth + 1) } finally { runCatching { child.recycle() } }
            }
            return base.copy(children = children, capped = base.capped || children.size < childCount || children.any { it == null })
        }
        return copy(root, 0)
    }

    override fun onInterrupt() { MessageDiagnostics.invalidateContinuity() }

    override fun onDestroy() {
        connected = false
        MessageDiagnostics.invalidateContinuity()
        if (::worker.isInitialized) worker.removeCallbacksAndMessages(null)
        if (::workerThread.isInitialized) workerThread.quitSafely()
        super.onDestroy()
    }

    companion object {
        const val PREFS = "autoim_diagnostics"
        const val KEY_DIAGNOSTIC = "diagnostic_enabled"
        const val KEY_OPERATIONAL = "accessibility_operational"
        private const val DEBOUNCE_MS = 700L
        @Volatile var connected: Boolean = false
            private set
        @Volatile var operational: Boolean = false
            private set
    }
}
