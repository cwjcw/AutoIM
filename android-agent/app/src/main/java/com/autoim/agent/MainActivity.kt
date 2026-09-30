package com.autoim.agent

import android.app.Activity
import android.app.AlertDialog
import android.content.ComponentName
import android.content.Intent
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.view.ViewGroup
import android.widget.Button
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import android.content.ClipData
import android.content.ClipboardManager

class MainActivity : Activity() {
    private lateinit var content: LinearLayout
    private lateinit var status: TextView
    private val store by lazy { DiagnosticStore(this) }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        title = getString(R.string.app_name)
        val scroll = ScrollView(this)
        content = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(18), dp(18), dp(18), dp(28))
        }
        scroll.addView(content)
        setContentView(scroll)
        render()
    }

    override fun onResume() { super.onResume(); if (::content.isInitialized) render() }

    private fun render() {
        content.removeAllViews()
        addText("AutoIM Android Agent 0.1.2\n离线消息解析与检测：只读取企业微信无障碍节点与通知。", 20f)
        addText("设备", 18f, bold = true)
        addText("Manufacturer：${Build.MANUFACTURER}\nModel：${Build.MODEL}\nAndroid：${Build.VERSION.RELEASE}\nSDK：${Build.VERSION.SDK_INT}\nAgent：${packageManager.getPackageInfo(packageName, 0).versionName}")

        val wecom = wecomInfo()
        addText("企业微信", 18f, bold = true)
        addText(wecom)
        status = TextView(this).apply { textSize = 15f; setTextIsSelectable(true) }
        content.addView(status)
        updateStatuses()

        addButton("打开无障碍设置") { openSettings(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)) }
        addButton("打开通知访问设置") { openSettings(Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS)) }
        addButton(diagnosticButtonLabel()) {
            val prefs = getSharedPreferences(AutoIMAccessibilityService.PREFS, MODE_PRIVATE)
            val enabled = prefs.getBoolean(AutoIMAccessibilityService.KEY_DIAGNOSTIC, false)
            prefs.edit().putBoolean(AutoIMAccessibilityService.KEY_DIAGNOSTIC, !enabled).apply()
            MessageDiagnostics.invalidateContinuity()
            Toast.makeText(this, if (enabled) "已停止诊断" else "已开始企业微信诊断", Toast.LENGTH_SHORT).show()
            render()
        }
        addButton("刷新状态") { render() }
        addButton("查看最后企微快照") { showSnapshot() }
        addButton("清空诊断数据") {
            AlertDialog.Builder(this).setMessage("清空本机保存的最新快照和企业微信通知？")
                .setNegativeButton("取消", null).setPositiveButton("清空") { _, _ ->
                    store.clear()
                    MessageDiagnostics.startSession()
                    getSharedPreferences(AutoIMAccessibilityService.PREFS, MODE_PRIVATE).edit()
                        .putBoolean(AutoIMAccessibilityService.KEY_OPERATIONAL, false).apply()
                    render()
                }.show()
        }
        addButton("复制诊断结果") { copyReport() }

        val messages = MessageDiagnostics.view()
        addText("消息解析诊断", 18f, bold = true)
        addText(MessageDiagnosticReport.parsed(messages.parsed))
        addText("新消息检测", 18f, bold = true)
        addText(MessageDiagnosticReport.detection(messages.history))
        addButton("重建当前会话基线") {
            MessageDiagnostics.rebuildBaseline()
            Toast.makeText(this, "基线已重建；下次实时快照仍只建立基线", Toast.LENGTH_SHORT).show()
            render()
        }
        addButton("清空消息检测日志") { MessageDiagnostics.clearLog(); render() }
        addButton("复制消息解析诊断") {
            val clipboard = getSystemService(CLIPBOARD_SERVICE) as ClipboardManager
            clipboard.setPrimaryClip(ClipData.newPlainText("AutoIM 消息解析诊断", MessageDiagnosticReport.export(MessageDiagnostics.view())))
            Toast.makeText(this, "消息解析诊断已复制", Toast.LENGTH_SHORT).show()
        }

        val snapshot = store.readSnapshot()
        addText("最后快照", 18f, bold = true)
        addText(snapshot?.let { "时间：${it.captureTime}\n节点数：${it.nodeCount}，最大深度：${it.maxDepth}，含文本节点：${it.textNodeCount}，已截断：${it.truncated}\n\n可见文本摘要：\n${DiagnosticLogic.visibleText(it.nodes).joinToString("\n").ifBlank { "（无可见文字）" }}" }
            ?: "暂无快照。开启诊断模式后，手工切换到企业微信并操作页面。")
        addText("最近企业微信通知（最多 20 条）", 18f, bold = true)
        val records = store.readNotifications()
        addText(records.asReversed().joinToString("\n\n") { "${it.postTime} | ${it.title}\n${it.text}\n${it.bigText}" }.ifBlank { "暂无通知记录。" })
    }

    private fun updateStatuses() {
        val component = ComponentName(this, AutoIMAccessibilityService::class.java).flattenToString()
        val enabledRaw = Settings.Secure.getString(contentResolver, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES).orEmpty()
        val authorized = enabledRaw.split(':').any { it.equals(component, ignoreCase = true) }
        val prefs = getSharedPreferences(AutoIMAccessibilityService.PREFS, MODE_PRIVATE)
        val worked = prefs.getBoolean(AutoIMAccessibilityService.KEY_OPERATIONAL, false) || AutoIMAccessibilityService.operational
        val notificationComponent = ComponentName(this, AutoIMNotificationListenerService::class.java).flattenToString()
        val notificationRaw = Settings.Secure.getString(contentResolver, "enabled_notification_listeners").orEmpty()
        val notificationAuthorized = notificationRaw.split(':').any { it.equals(notificationComponent, ignoreCase = true) }
        val accessibilityState = when {
            !authorized -> getString(R.string.status_unauthorized)
            AutoIMAccessibilityService.connected -> getString(R.string.status_connected)
            else -> getString(R.string.status_waiting)
        }
        val notificationState = if (notificationAuthorized) "已授权" else getString(R.string.status_unauthorized)
        val modeState = getString(if (prefs.getBoolean(AutoIMAccessibilityService.KEY_DIAGNOSTIC, false)) R.string.status_enabled else R.string.status_disabled)
        status.text = listOf(
            getString(R.string.status_accessibility, accessibilityState, if (worked) getString(R.string.status_worked) else ""),
            getString(R.string.status_notification, notificationState, if (AutoIMNotificationListenerService.listenerConnected) getString(R.string.status_notification_connected) else ""),
            getString(R.string.status_diagnostic, modeState),
        ).joinToString("\n")
    }

    private fun wecomInfo(): String = DiagnosticEnvironment.wecom(this)

    private fun diagnosticButtonLabel() = if (getSharedPreferences(AutoIMAccessibilityService.PREFS, MODE_PRIVATE)
        .getBoolean(AutoIMAccessibilityService.KEY_DIAGNOSTIC, false)) "停止企业微信诊断" else "开始企业微信诊断"

    private fun openSettings(intent: Intent) {
        runCatching { startActivity(intent) }.onFailure {
            Toast.makeText(this, "无法打开设置：${it.message}", Toast.LENGTH_LONG).show()
        }
    }

    private fun showSnapshot() {
        startActivity(Intent(this, SnapshotDiagnosticActivity::class.java))
    }

    private fun copyReport() {
        val snapshot = store.readSnapshot()
        val report = "AutoIM Agent 0.1.2\n${wecomInfo()}\n诊断模式：${diagnosticButtonLabel()}\nAccessibility 已连接：${AutoIMAccessibilityService.connected}\nAccessibility 已工作：${AutoIMAccessibilityService.operational}\n通知监听已连接：${AutoIMNotificationListenerService.listenerConnected}\n\n快照：${snapshot?.let { "${it.nodeCount} nodes, depth=${it.maxDepth}, truncated=${it.truncated}" } ?: "none"}\n${snapshot?.let { DiagnosticLogic.visibleText(it.nodes).joinToString("\n") }.orEmpty()}\n\n企微通知数量：${store.readNotifications().size}"
        val clipboard = getSystemService(CLIPBOARD_SERVICE) as ClipboardManager
        clipboard.setPrimaryClip(ClipData.newPlainText("AutoIM diagnostics", report))
        Toast.makeText(this, "诊断结果已复制", Toast.LENGTH_SHORT).show()
    }

    private fun addText(text: String, size: Float = 14f, bold: Boolean = false) {
        content.addView(TextView(this).apply {
            this.text = text; textSize = size; setPadding(0, dp(7), 0, dp(7)); setTextIsSelectable(true)
            if (bold) setTypeface(typeface, android.graphics.Typeface.BOLD)
        })
    }

    private fun addButton(label: String, onClick: () -> Unit) {
        content.addView(Button(this).apply { text = label; setOnClickListener { onClick() } },
            LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT))
    }

    private fun dp(value: Int) = (value * resources.displayMetrics.density).toInt()
}
