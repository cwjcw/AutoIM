package com.autoim.agent

import android.content.Context
import android.os.Build

object DiagnosticEnvironment {
    fun device(context: Context): String =
        "Manufacturer：${Build.MANUFACTURER}\nModel：${Build.MODEL}\nAndroid：${Build.VERSION.RELEASE}\n" +
            "SDK：${Build.VERSION.SDK_INT}\nAgent：${context.packageManager.getPackageInfo(context.packageName, 0).versionName}"

    fun wecom(context: Context): String = try {
        val info = context.packageManager.getPackageInfo(DiagnosticLogic.WECOM_PACKAGE, 0)
        val code = if (Build.VERSION.SDK_INT >= 28) info.longVersionCode
            else @Suppress("DEPRECATION") info.versionCode.toLong()
        "已安装：是\npackageName：${info.packageName}\n版本：${info.versionName.orEmpty()}\nversionCode：$code"
    } catch (_: Exception) {
        "已安装：否\npackageName：${DiagnosticLogic.WECOM_PACKAGE}\n版本：—\nversionCode：—"
    }
}
