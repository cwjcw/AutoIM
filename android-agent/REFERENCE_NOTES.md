# Reference Notes

Research notes for AutoIM Android Agent 0.1.0. No source code was copied.

## WorkTool (Apache-2.0)

- Adopt: check the event package before reading `rootInActiveWindow`, and check the root package again; traverse nodes defensively and copy bounds and properties into a snapshot.
- Do not adopt: auto-click, typing, sending, WebSocket/server connections, file observers, or reads from WeCom app data/cache directories. Those exceed this offline read-only PoC.

## FlowBot (restricted non-commercial terms)

- Adopt only the architectural idea of keeping Accessibility and notification listeners in separate Android services, each with explicit lifecycle handling.
- Do not copy its source or bring in its dependency graph. Its license is restrictive for this project.

## AutoJs6 (MPL-2.0)

- Adopt: distinguish service-connected from operational, react to events instead of polling, handle nullable/stale nodes, and use the platform notification-listener lifecycle.
- Do not adopt: scripting/runtime features, device-control actions, networking, OCR, root/Shizuku, or third-party modules.

## Android platform references

- Android requires the system binding permissions and service intent filters for both service types. Notification access is user-controlled; wait for `onListenerConnected()` before listener operations.
- Accessibility capture is enabled only by the explicit diagnostic-mode toggle and events from `com.tencent.wework`; no action APIs are called.

Sources: [WorkTool](https://github.com/gallonyin/worktool), [FlowBot](https://github.com/xlrpa/FlowBot), [AutoJs6](https://github.com/SuperMonster003/AutoJs6), [AccessibilityService API](https://developer.android.com/reference/android/accessibilityservice/AccessibilityService), [NotificationListenerService API](https://developer.android.com/reference/android/service/notification/NotificationListenerService).

## P-A1.1 screen geometry (0.1.1)

- Capture screen dimensions alongside the immutable snapshot using `WindowManager.getMaximumWindowMetrics()` on API 30+ and `Display.getRealMetrics()` on older supported versions. Do not use the root node bounds as screen dimensions, or apply today's display dimensions to a historical snapshot.
- Missing dimensions produce null geometry and no top-region candidates. Position is purely geometric; it does not establish the sender or verify the chat title.
- References: [WindowManager maximum metrics](https://developer.android.com/reference/android/view/WindowManager#getMaximumWindowMetrics()), [Display real metrics](https://developer.android.com/reference/android/view/Display#getRealMetrics(android.util.DisplayMetrics)). No external implementation was copied.

## P-A1.2 offline parsing (0.1.2)

- User-verified Redmi Note 11R evidence: top TextView title, left/right ordinary message text, editable EditText input, and a clickable/enabled standard send Button after manual input. Observed internal IDs are evidence only; parsing does not require them.
- New parsers operate only on copied Kotlin snapshots. Direction combines text/bubble margins and optional avatar anchors, never the earlier LEFT/CENTER/RIGHT label alone. Hidden nodes, ambiguous structures and unsupported media/card rows cannot generate reliable new-message events.
- Sequence comparison retains direction, minimally normalized body and occurrence count. A complete previous prefix allows repeated identical appends; shifted windows need one unambiguous suffix/prefix alignment. No overlap or ambiguous alignment resyncs without events.
- Debounced snapshots copy trigger metadata and preserve coalesced scroll/window event types. Scroll-triggered captures and subsequent unstable content changes only update the baseline. This intentionally favors missed events over historical replay.
- No additional libraries, copied external implementations, network clients, node actions or Android permissions were introduced. Notification text remains diagnostic evidence only.
