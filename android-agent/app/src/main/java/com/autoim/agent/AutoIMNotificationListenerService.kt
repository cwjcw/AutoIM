package com.autoim.agent

import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification

class AutoIMNotificationListenerService : NotificationListenerService() {
    override fun onNotificationPosted(sbn: StatusBarNotification?) {
        if (sbn == null || !DiagnosticLogic.acceptsNotification(sbn.packageName)) return
        val extras = runCatching { sbn.notification.extras }.getOrNull() ?: return
        fun value(key: String): String = runCatching { extras.getCharSequence(key)?.toString().orEmpty() }.getOrDefault("")
        val record = NotificationRecord(
            packageName = DiagnosticLogic.WECOM_PACKAGE,
            postTime = sbn.postTime,
            notificationId = sbn.id,
            notificationKey = sbn.key.orEmpty(),
            title = value("android.title"),
            text = value("android.text"),
            subText = value("android.subText"),
            bigText = value("android.bigText"),
            category = sbn.notification.category.orEmpty(),
        )
        val store = DiagnosticStore(this)
        store.saveNotifications(store.readNotifications() + record)
    }

    override fun onListenerConnected() {
        super.onListenerConnected()
        listenerConnected = true
    }

    override fun onListenerDisconnected() {
        listenerConnected = false
        super.onListenerDisconnected()
    }

    companion object { @Volatile var listenerConnected = false; private set }
}
