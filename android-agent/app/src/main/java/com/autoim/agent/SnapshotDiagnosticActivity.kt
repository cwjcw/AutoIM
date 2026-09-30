package com.autoim.agent

import android.app.Activity
import android.content.ClipData
import android.content.ClipboardManager
import android.os.Bundle
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import java.util.concurrent.Executors

/** Queries one saved immutable snapshot; never accesses the Accessibility service. */
class SnapshotDiagnosticActivity : Activity() {
    private val worker = Executors.newSingleThreadExecutor()
    private lateinit var content: LinearLayout
    private var searchGeneration = 0

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        title = "最后企微快照 · 结构诊断"
        content = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            val padding = (16 * resources.displayMetrics.density).toInt()
            setPadding(padding, padding, padding, padding)
        }
        setContentView(ScrollView(this).apply { addView(content) })
        button("返回") { finish() }
        label("正在读取已保存快照…")
        worker.execute {
            val snapshot = DiagnosticStore(this).readSnapshot()
            val result = runCatching {
                snapshot?.let {
                    val diagnostics = StructureDiagnostics(it)
                    val parsing = WeComSnapshotParser().parse(it)
                    val messages = MessageDiagnostics.view()
                    Prepared(diagnostics, listOf("消息解析诊断" to MessageDiagnosticReport.parsed(parsing),
                        "新消息检测（本次运行）" to MessageDiagnosticReport.detection(messages.history)) +
                        StructureReport.sections(diagnostics), StructureReport.fullTree(it),
                        StructureReport.export(diagnostics, DiagnosticEnvironment.device(this), DiagnosticEnvironment.wecom(this)))
                }
            }
            runOnUiThread {
                if (isDestroyed || isFinishing) return@runOnUiThread
                content.removeAllViews()
                button("返回") { finish() }
                val prepared = result.getOrNull()
                if (prepared == null) {
                    label(if (result.isFailure) "结构诊断生成失败，请重新采集快照。" else "暂无有效快照，请开启诊断并手工打开企微聊天。")
                } else render(prepared)
            }
        }
    }

    private fun render(prepared: Prepared) {
        label(StructureReport.summary(prepared.diagnostics.snapshot))
        label("只查询打开本页时保存的 Snapshot。返回后重新进入可加载最新快照；不会重新访问企业微信。")
        button("复制结构诊断") { copy("AutoIM 结构诊断", prepared.export) }
        button("单独复制完整 Node Tree") { copy("AutoIM 完整 Node Tree", prepared.tree) }

        val query = EditText(this).apply {
            hint = "按文本查节点（text / contentDescription）"
            setSingleLine(true)
        }
        content.addView(query)
        val matches = TextView(this).apply { setTextIsSelectable(true); text = "输入文本后点击查找。" }
        button("按文本查节点") {
            val term = query.text.toString()
            val generation = ++searchGeneration
            matches.text = "正在查询已保存快照…"
            worker.execute {
                val records = prepared.diagnostics.search(term)
                val text = if (term.isBlank()) "请输入非空查询文本。" else
                    "匹配节点：${records.size}\n\n${StructureReport.details(records, "无匹配节点") }"
                runOnUiThread {
                    if (!isDestroyed && !isFinishing && generation == searchGeneration) matches.text = text
                }
            }
        }
        content.addView(matches)
        val defaults = setOf("消息解析诊断", "新消息检测（本次运行）")
        prepared.sections.forEach { (title, body) -> section(title, body, title in defaults) }
        section("完整 Node Tree", prepared.tree, false)
    }

    private fun section(title: String, body: String, initiallyOpen: Boolean) {
        var open = initiallyOpen
        val text = TextView(this).apply {
            textSize = 14f
            setTextIsSelectable(true)
            visibility = if (open) View.VISIBLE else View.GONE
            if (open) text = body
        }
        val toggle = button(getString(R.string.diagnostic_section_title, if (open) "▼" else "▶", title)) {}
        toggle.setOnClickListener {
            open = !open
            toggle.text = getString(R.string.diagnostic_section_title, if (open) "▼" else "▶", title)
            if (open && text.text.isEmpty()) text.text = body
            text.visibility = if (open) View.VISIBLE else View.GONE
        }
        content.addView(text)
    }

    private fun copy(title: String, text: String) {
        runCatching {
            (getSystemService(CLIPBOARD_SERVICE) as ClipboardManager).setPrimaryClip(ClipData.newPlainText(title, text))
        }.onSuccess {
            Toast.makeText(this, "已复制，请注意诊断包含聊天文本", Toast.LENGTH_SHORT).show()
        }.onFailure {
            Toast.makeText(this, "复制失败（内容可能过大），可搜索并手工复制单个节点。", Toast.LENGTH_LONG).show()
        }
    }

    private fun label(value: String) = content.addView(TextView(this).apply {
        text = value; textSize = 14f; setTextIsSelectable(true)
    })

    private fun button(value: String, action: () -> Unit): Button = Button(this).apply {
        text = value; setOnClickListener { action() }; content.addView(this)
    }

    override fun onDestroy() {
        worker.shutdownNow()
        super.onDestroy()
    }

    private data class Prepared(val diagnostics: StructureDiagnostics, val sections: List<Pair<String, String>>,
        val tree: String, val export: String)
}
