"""Serial visible-unread collection using the existing capture/OCR pipeline."""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from threading import Event, Lock
from time import perf_counter
from uuid import uuid4

import numpy as np

from autoim.calibration import CalibrationStore
from autoim.wecom.collection_events import CollectionTrace, EventTime, SaveNotice
from autoim.wecom.collection_navigation import CollectionNavigation, CollectionPermit, NavigationPaused, _identity
from autoim.wecom.collection_profile import (ProfileAction, ProfileCheck, ProfileSurface, card_close,
                                              email_from_spans, is_profile_card, menu_action)
from autoim.wecom.visual_account import LoginEvidence, read_login
from autoim.wecom.visual_capture import CaptureError, capture_visible_window
from autoim.wecom.visual_contacts import ContactObservation
from autoim.wecom.visual_layout import VisualThresholds, detect_layout
from autoim.wecom.visual_monitor import _different, probe_frame, summarize
from autoim.wecom.visual_observation import VisualObservation, observe_frame
from autoim.wecom.visual_ocr import LocalTextRecognizer, OcrUnavailable
from autoim.wecom.visual_store import LocalAccount, SaveResult, VisualStore, avatar_key, name_key
from autoim.wecom.window_manager import WeComWindowManager
from autoim.wecom.window_state import WindowHealth, probe_window


class CollectionReader:
    def __init__(self, calibration: Path | None, thresholds: Path | None) -> None:
        self._calibration_path = calibration
        self._thresholds_path = thresholds
        self._settings = None
        self._ocr = None

    def set_calibration(self, path: Path | None) -> None:
        self._calibration_path = path
        self._settings = None

    def _prepare(self):
        if self._settings is None:
            self._settings = (
                CalibrationStore(self._calibration_path).load() if self._calibration_path else {},
                VisualThresholds.load(self._thresholds_path))
        if self._ocr is None:
            try:
                self._ocr = LocalTextRecognizer()
            except OcrUnavailable:
                pass  # The existing observation pipeline reports unavailable OCR.

    def identify(self, frame):
        self._prepare()
        layout = detect_layout(frame.pixels, *self._settings)
        login = (read_login(frame.pixels, layout.get("A"), self._ocr, frame.state.dpi) if self._ocr else
                 LoginEvidence(reason="本地 OCR 不可用"))
        return VisualObservation(frame, layout, "ACCOUNT_ONLY", login=login)

    def profile_text(self, surface):
        self._prepare()
        if self._ocr is None:
            raise OcrUnavailable("个人资料本地 OCR 不可用")
        return self._ocr.read(surface.pixels, surface.bounds)

    def __call__(self, frame, *, on_conversation=None):
        started = perf_counter()
        self._prepare()
        raw = observe_frame(frame, self._settings[0], self._ocr, self._settings[1], on_conversation=on_conversation)
        login = (read_login(frame.pixels, raw.layout.get("A"), self._ocr, frame.state.dpi) if self._ocr else
                 LoginEvidence(reason="本地 OCR 不可用"))
        return replace(raw, login=login, recognition_ms=(perf_counter() - started) * 1000)


@dataclass(frozen=True, slots=True)
class ScanStatus:
    status: str = "STOPPED"
    reason: str = ""
    target: str = ""
    candidates: int = 0
    queued: int = 0
    completed: int = 0
    errors: int = 0
    last_scan: datetime | None = None
    match: str = "UNKNOWN"
    saved: int = 0
    added: int = 0
    repeated: int = 0
    uncertain: int = 0
    pages: int = 1
    window_health: WindowHealth | None = None
    bound_pid: int | None = None
    bound_hwnd: int | None = None
    window_change_at: datetime | None = None
    window_change: str = ""
    rebind_at: datetime | None = None
    rebind_result: str = ""
    waiting_for_window: bool = False
    resumed_at: datetime | None = None
    account_name: str = ""
    account_email: str = ""
    account_state: str = "UNIDENTIFIED"
    account_reason: str = ""
    account_at: EventTime | None = None
    account_id: int | None = None
    account_choices: tuple[LocalAccount, ...] = ()
    recent: CollectionTrace = CollectionTrace()
    trace: CollectionTrace = CollectionTrace()
    last_success: CollectionTrace | None = None
    last_collected_at: EventTime | None = None
    navigation_result: str = ""
    ocr_result: str = ""
    sqlite_result: str = ""
    last_failure: str = ""
    notice: SaveNotice | None = None
    trace_job: str = ""
    screenshot_succeeded_at: datetime | None = None
    b2_scan_attempt_at: datetime | None = None
    b2_scan_completed_at: datetime | None = None
    contact_count: int = 0
    unread_marker_count: int = 0
    reliable_unread_count: int = 0
    unreliable_unread_count: int = 0
    unreliable_reasons: tuple[str, ...] = ()
    skipped_unread: int = 0
    skipped_reasons: tuple[str, ...] = ()
    awaiting_navigation: int = 0
    navigation_block_reason: str = ""
    foreground_changed_stage: str = ""
    scan_disposition: str = "B2_NOT_SCANNED"
    scan_id: str = ""
    scan_ocr_completed_at: datetime | None = None
    last_b2_summary: str = ""
    selection_method: str = "UNKNOWN"
    selection_name: str = ""
    selection_name_reliable: bool = False
    selection_confidence: str = "UNKNOWN"
    selection_evidence: str = ""
    navigation_allowed: bool | None = None
    input_diagnostic: str = ""


@dataclass(frozen=True, slots=True)
class CollectionScope:
    personal: bool = True
    group: bool = True
    muted: bool = False

    def exclusion(self, row: ContactObservation) -> str:
        if row.kind not in ("CONTACT", "GROUP_CANDIDATE"):
            return f"不支持的会话入口 · {row.kind}"
        if row.kind == "GROUP_CANDIDATE" and not self.group:
            return "采集范围未启用普通群聊"
        if row.kind == "CONTACT" and not self.personal:
            return "采集范围未启用普通单聊"
        if row.is_muted is True and not self.muted:
            return "采集范围未启用静音会话"
        return ""


@dataclass(frozen=True, slots=True)
class _Target:
    row: ContactObservation
    name: str
    avatar: str
    blockers: tuple[str, ...]
    skip_reason: str = ""

    @property
    def key(self) -> str:
        labels = "|".join(map(name_key, self.row.identity_labels))
        return f"{self.name}|{self.avatar}|{self.row.peer_type}|{labels}"

    @property
    def ambiguous(self) -> bool:
        return bool(self.blockers)

    @property
    def unread_signature(self) -> tuple:
        row = self.row
        return (row.unread_count if row.unread_count_reliable else None,
                name_key(row.preview) if row.preview_reliable else "")


def _new_unread(previous: tuple, current: tuple) -> bool:
    old_count, old_preview = previous
    count, preview = current
    return bool((old_count is not None and count is not None and count > old_count)
                or (old_preview and preview and preview != old_preview))


def _merge_unread(previous: tuple, current: tuple) -> tuple:
    count, preview = current
    return count if count is not None else previous[0], preview or previous[1]


def unread_targets(observation: VisualObservation, scope: CollectionScope | None = None) -> tuple[_Target, ...]:
    """B2 is the source, including muted rows and undifferentiated red dots."""
    if observation.page != "MESSAGES_CANDIDATE" or observation.layout.get("B2") is None:
        return ()
    names = Counter(name_key(row.display_name) for row in observation.contacts if row.display_name)
    targets = []
    for row in observation.contacts:
        if not row.has_unread_indicator:
            continue
        name = name_key(row.display_name)
        checks = (
            (names[name] != 1 or not row.name_reliable, "联系人身份重复或姓名不可靠"),
            (row.status != "CANDIDATE", "联系人行部分可见或状态不可靠"),
            (scope is not None and not scope.muted and row.is_muted is None, "静音状态无法确认"),
            (row.avatar is None and row.peer_type == "UNKNOWN", "缺少头像或身份标签的独立会话证据"),
            (scope is not None and row.kind == "CONTACT" and row.peer_type == "UNKNOWN"
             and not (row.preview or row.activity_time_reliable), "入口缺少普通会话的预览或活动证据"),
            (scope is not None and scope.personal != scope.group and row.kind == "CONTACT"
             and row.peer_type == "UNKNOWN", "采集范围区分单聊和群聊，当前类型无法独立确认"),
            (row.badge is None, "未读标记边界缺失"),
        )
        blockers = tuple(reason for failed, reason in checks if failed)
        skip = scope.exclusion(row) if scope else ""
        targets.append(_Target(row, name, avatar_key(observation, row), blockers, skip))
    return tuple(targets)


@dataclass(slots=True)
class _WaitingUnread:
    job: str
    signature: tuple


def _page_keys(observation: VisualObservation) -> tuple[str, ...]:
    return tuple(f"{name_key(row.display_name)}|{avatar_key(observation, row)}" for row in observation.contacts
                 if row.name_reliable and row.status == "CANDIDATE")


def page_transition(before: tuple[str, ...], after: tuple[str, ...]) -> str:
    if len(set(before)) != len(before) or len(set(after)) != len(after) or min(len(before), len(after)) < 3:
        return "SCROLL_UNCERTAIN"
    if before == after:
        return "NO_SCROLL_CHANGE"
    overlaps = [n for n in range(2, min(len(before), len(after)))
                if before[-n:] == after[:n] and not set(before[:-n]) & set(after[n:])]
    return "SCROLLED" if len(overlaps) == 1 else "SCROLL_UNCERTAIN"


def _matches_target(observation: VisualObservation, target: _Target) -> bool:
    rows = [row for row in observation.contacts if name_key(row.display_name) == target.name]
    conv = observation.conversation
    return (len(rows) == 1 and (rows[0].is_selected is True or observation.verified_target == rows[0])
            and rows[0].name_reliable
            and rows[0].status == "CANDIDATE" and conv.match_status == "VISUAL_MATCH"
            and conv.title_name_reliable and conv.selected_name_reliable and name_key(conv.title) == target.name
            and (not target.avatar or avatar_key(observation, rows[0]) == target.avatar)
            and rows[0].peer_type == target.row.peer_type
            and rows[0].identity_labels == target.row.identity_labels
            and (target.row.kind != "GROUP_CANDIDATE" or conv.conversation_type == "GROUP")
            and observation.layout.get("C2") is not None)


def _verify_transition(before: VisualObservation, after: VisualObservation, target: _Target) -> VisualObservation:
    """A click can verify an unhighlighted row only with independent transition evidence."""
    conv = after.conversation
    if (any(row.is_selected is True for row in after.contacts) or not before.conversation.title_name_reliable
            or name_key(before.conversation.title) == target.name or not conv.title_name_reliable
            or name_key(conv.title) != target.name or not target.avatar or not target.row.has_unread_indicator
            or after.page != "MESSAGES_CANDIDATE" or after.layout.get("C2") is None):
        return after
    matches = [row for row in after.contacts if name_key(row.display_name) == target.name]
    if len(matches) != 1:
        return after
    row = matches[0]
    if (row.has_unread_indicator or row.badge is not None or not row.name_reliable or row.status != "CANDIDATE"
            or avatar_key(after, row) != target.avatar
            or sum(avatar_key(after, item) == target.avatar for item in after.contacts) != 1
            or (conv.title_peer_type != "UNKNOWN" and row.peer_type != "UNKNOWN"
                and (conv.title_peer_type != row.peer_type or
                     (row.peer_type == "EXTERNAL_WECOM" and tuple(map(name_key, conv.title_identity_labels))
                      != tuple(map(name_key, row.identity_labels)))))):
        return after
    return replace(after, verified_target=row,
                   conversation=replace(conv, match_status="VISUAL_MATCH", selected_name=row.display_name,
                                        selected_name_reliable=True, selected_peer_type=row.peer_type,
                                        selected_identity_labels=row.identity_labels,
                                        comparison_evidence="已点击；C1 标题切换、唯一 B2 头像一致且未读标记消失",
                                        selection_method="VERIFIED_NAVIGATION_TRANSITION",
                                        selection_confidence="CORROBORATED"))


class AccountPaused(NavigationPaused):
    """The login evidence must be rechecked before storing messages."""


class CollectionService:
    def __init__(self, profile: str | None, store: VisualStore, reader: CollectionReader, *,
                 capture=None, navigator=None, permit: CollectionPermit | None = None, window_probe=None,
                 require_email: bool = False) -> None:
        self.profile = profile
        self._auto_account = profile is None
        self._require_email = require_email and self._auto_account
        self._profile_check: ProfileCheck | None = None
        self._pending_email = None
        self._verified_email = ""
        self._force_email = False
        self._failed_profile_lifetime = None
        self._bound_login: tuple[int, LoginEvidence, tuple] | None = None
        self._pending_login: tuple[LoginEvidence, tuple] | None = None
        self._requires_confirmation = False
        self.store = store
        self.read = reader
        manager = WeComWindowManager()
        self.capture = capture if capture is not None else lambda: capture_visible_window(manager)
        self._window_probe = window_probe
        if capture is None and window_probe is None:
            self._window_probe = lambda: probe_window(manager)
        self.permit = permit or CollectionPermit()
        self.navigator = navigator or CollectionNavigation(self.permit, manager=manager, capture=self.capture)
        self._lock = Lock()
        self._cancel = Event()
        self._cancel.set()
        self._run_id = ""
        self._status = ScanStatus()
        self._scan_history: tuple[ScanStatus, ...] = ()
        self._window = None
        self._window_plan_invalidated = False
        self._scroll = False
        self._queued: dict[str, str] = {}
        self._discovered: dict[str, EventTime] = {}
        self._handled: dict[str, tuple] = {}
        self._waiting_unread: dict[str, _WaitingUnread] = {}
        self.scope: CollectionScope | None = None
        self._deferred: dict[str, int] = {}
        self._queued_signatures: dict[str, tuple] = {}
        self._round = 0
        self._operations = 0

    def start(self, *, scroll: bool = False) -> None:
        if not self._cancel.is_set():
            raise NavigationPaused("自动采集已经启动")
        self.permit.grant(scroll=scroll)
        self._run_id = uuid4().hex
        self._recover = True
        self._scroll = scroll
        self._window = None
        self._window_plan_invalidated = False
        self._failed_profile_lifetime = None
        self._queued.clear()
        self._deferred.clear()
        self._queued_signatures.clear()
        self._discovered.clear()
        self._handled.clear()
        self._operations = 0
        previous = self._status
        self._scan_history = ()
        self._status = ScanStatus("RUNNING", "请手动切回最大化的企业微信，等待空闲后巡检",
                                  account_state=previous.account_state, account_name=previous.account_name, account_email=previous.account_email,
                                  account_at=previous.account_at, account_id=previous.account_id,
                                  account_choices=previous.account_choices, account_reason=previous.account_reason)
        self._cancel.clear()
        if isinstance(self.navigator, CollectionNavigation):
            self.navigator.mark_started()

    def stop(self) -> None:
        self._cancel.set()
        self.permit.revoke()
        self._set(status="STOPPED", reason="自动采集许可已撤销", waiting_for_window=False)

    @property
    def active(self) -> bool:
        return not self._cancel.is_set()

    def snapshot(self) -> ScanStatus:
        return self._status

    def recent_scans(self) -> tuple[ScanStatus, ...]:
        return self._scan_history

    def _set(self, **fields) -> ScanStatus:
        self._status = replace(self._status, **fields)
        return self._status

    def _read(self, *, on_conversation=None):
        frame = self.capture()
        if on_conversation and isinstance(self.read, CollectionReader):
            return self.read(frame, on_conversation=on_conversation)
        return self.read(frame)

    @staticmethod
    def _same_login(first: LoginEvidence, second: LoginEvidence) -> bool:
        return bool(first.key and first.key == second.key and first.avatar == second.avatar
                    and (first.avatar or (first.signature and first.signature == second.signature)))

    def _account_pause(self, reason, *, state="IDENTIFYING", choices=()):
        if self._status.account_id is not None or self._queued:
            self._invalidate_window_plan(reason)
        self.profile = None
        self._recover = True
        self._set(account_state=state, account_id=None, account_email="", account_reason=reason,
                  account_choices=choices, queued=0)
        raise AccountPaused(reason)

    def _check_login(self, raw, *, allow_binding=True):
        if not self._auto_account:
            return
        evidence, identity = raw.login, _identity(raw.frame)
        lifetime = (identity[0], identity[1], identity[-1])
        self._set(account_at=EventTime.now(), account_name=evidence.name)
        if not evidence.key or not evidence.signature:
            self._pending_login = None
            self._account_pause(evidence.reason or "A1 登录账号不可确认", state="IDENTIFICATION_FAILED")
        if self._require_email:
            self._check_email_login(evidence, lifetime, allow_binding=allow_binding)
            return
        if self._bound_login:
            account, prior, window = self._bound_login
            if not self._same_login(prior, evidence) or (not evidence.avatar and lifetime != window):
                self._requires_confirmation = True
            elif self.profile == account and not self._requires_confirmation:
                return
        if not allow_binding:
            self._pending_login = None
            self._account_pause("导航后登录账号未能延续绑定，已阻断写入")
        pending = self._pending_login
        self._pending_login = (evidence, identity)
        if not pending or pending[1] != identity or not self._same_login(pending[0], evidence):
            self._account_pause("等待第二次独立截图核对 A1 登录账号")
        account, choices = self.store.locate_login(evidence, create=not self._requires_confirmation)
        if account is None or self._requires_confirmation:
            compatible = tuple(row for row in choices if not row.avatar or row.avatar == evidence.avatar)
            reason = ("登录账号视觉证据改变，请确认归属" if self._requires_confirmation
                      else "历史账号无法无歧义匹配，请确认归属")
            self._account_pause(reason, state="CONFIRMATION_REQUIRED", choices=compatible)
        self._accept_login(account, evidence, lifetime)

    def _check_email_login(self, evidence, lifetime, *, allow_binding):
        if self._profile_check is not None:
            self._account_pause("资料窗口尚未恢复，等待核验流程结束", state="IDENTITY_CHECK_REQUIRED")
        if self._bound_login:
            account, prior, previous = self._bound_login
            if (self.profile == account and not self._force_email and self._verified_email
                    and lifetime == previous and self._same_login(prior, evidence)):
                return
        if not allow_binding:
            self._force_email = True
            self._account_pause("导航后 A1 或企业微信生命周期变化，禁止写入", state="IDENTITY_CHECK_REQUIRED")
        if self._pending_email:
            if self._pending_email[1] == lifetime and self._same_login(self._pending_email[0], evidence):
                self._account_pause("邮箱已核验，请确认是否沿用历史账号",
                                    state="CONFIRMATION_REQUIRED", choices=self._status.account_choices)
            self._pending_email = None
        cached = None if self._force_email else self.store.verified_login(evidence, lifetime)
        if cached:
            self._accept_login(cached[0], evidence, lifetime, email=cached[1])
            return
        self._account_pause("需完成邮箱核验" if self.active else "需授权后核验邮箱",
                            state="IDENTITY_CHECK_REQUIRED" if self.active else "WAITING_IDENTITY_PERMISSION")

    def request_identity_check(self):
        self._failed_profile_lifetime = None
        self._force_email = True
        self._pending_email = None
        return self.identify_account()

    @staticmethod
    def _lifetime(frame):
        identity = _identity(frame)
        return identity[0], identity[1], identity[-1]

    def _profile_status(self, stage, reason):
        return self._set(status="PAUSED" if self.active else "STOPPED", reason=reason,
                         account_state=stage, account_reason=reason, account_id=None,
                         notice=None, waiting_for_window=False)

    def _profile_tick(self, frame):
        if not self.active:
            return self._status
        session = self._profile_check
        if session and self._lifetime(frame) != self._lifetime(session.reference.frame):
            self._profile_check = None
            session = None
        if session is None:
            if self._failed_profile_lifetime == self._lifetime(frame):
                return self._profile_status("IDENTITY_VERIFICATION_FAILED", self._status.account_reason)
            identify = self.read.identify(frame)
            evidence = identify.login
            self._set(account_name=evidence.name, account_at=EventTime.now())
            if (not evidence.key or evidence.avatar_box is None or not evidence.avatar
                    or any(identify.layout.get(part) is None for part in ("A", "B1", "C1", "C2"))):
                return self._profile_status("IDENTITY_VERIFICATION_FAILED", "无法可靠定位 A1 登录姓名与头像")
            if not self.active:
                return self._status
            try:
                self._check_login(identify)
            except AccountPaused:
                pass
            else:
                return self._set(status="RUNNING", reason="当前企业微信账号邮箱已核验",
                                 saved=self.store.browse(self.profile)["message_count"])
            if self._status.account_state == "CONFIRMATION_REQUIRED":
                return self._profile_status("CONFIRMATION_REQUIRED", self._status.account_reason)
            try:
                known = self.navigator.profile_start(identify)
                surface = ProfileSurface(frame.capture.hwnd, frame.pixels, evidence.box, ())
                self._profile_status("OPENING_PROFILE_MENU", "正在复核 A1 个人菜单入口")
                self.navigator.profile_click(identify, ProfileAction("A1_OPEN", surface, evidence.avatar_box),
                                             known_windows=known)
            except NavigationPaused as exc:
                return self._profile_status("IDENTITY_CHECK_REQUIRED", str(exc))
            self._profile_check = ProfileCheck(identify, known)
            return self._profile_status("OPENING_PROFILE_MENU", "已打开头像入口，等待识别个人菜单")

        reference = session.reference
        try:
            current, surfaces, owned = self.navigator.profile_surfaces(reference.frame, session.known_windows)
            if not self.active:
                return self._status
            if session.stage == "VERIFY_RESTORED":
                return self._finish_identity(session, current, owned, surfaces)
            scanned = tuple((surface, self.read.profile_text(surface)) for surface in surfaces)
            dpi = current.state.dpi
            menus = [action for surface, spans in scanned
                     if (action := menu_action(surface, spans, reference.login.name, dpi))]
            if session.stage == "OPENING_PROFILE_MENU":
                if len(menus) != 1:
                    return self._wait_profile(session, "未取得唯一的个人菜单文字及头像证据")
                self._profile_status("OPENING_PROFILE_CARD", "正在复核个人菜单头像")
                self.navigator.profile_click(reference, menus[0], known_windows=session.known_windows)
                session.stage, session.polls = "READING_EMAIL", 0
                return self._profile_status(session.stage, "等待识别详细个人资料卡")
            cards = [(surface, spans) for surface, spans in scanned
                     if is_profile_card(spans, reference.login.name, surface)]
            if session.stage in ("READING_EMAIL", "VERIFYING_EMAIL"):
                if not surfaces and set(owned) <= set(session.known_windows):
                    session.failed = "资料层退出，完整邮箱核验未完成，请在高级工具中发起复核"
                    return self._finish_identity(session, current, owned, surfaces)
                if len(cards) != 1:
                    return self._wait_profile(session, "详细资料卡或邮箱标签无法唯一确认")
                surface, spans = cards[0]
                value = email_from_spans(surface, spans)
                key = (surface.hwnd, surface.geometry, surface.bounds)
                if session.stage == "READING_EMAIL":
                    session.email, session.email_surface, session.email_at = value, key, current.captured_at
                    session.stage, session.polls = "VERIFYING_EMAIL", 0
                    return self._profile_status(session.stage, "等待第二次独立截图核对邮箱")
                if current.captured_at <= session.email_at:
                    return self._wait_profile(session, "尚未取得独立资料截图")
                if not value or value != session.email or key != session.email_surface:
                    session.failed = "两次资料截图的邮箱无效、不一致或资料窗口发生变化"
                    session.email = ""
                action = card_close(surface, dpi)
                if action is None:
                    session.failed = "未取得资料关闭按钮证据，需手动关闭后重新核验"
                    return self._profile_status("CLOSING_PROFILE", session.failed)
                self._profile_status("CLOSING_PROFILE", "邮箱核对完成，正在复核本次打开的资料卡关闭位置")
                self.navigator.profile_click(reference, action, known_windows=session.known_windows)
                session.close_issued = True
                session.stage, session.polls = "CLOSING_PROFILE", 0
                return self._profile_status(session.stage, "验证资料卡已关闭")
            if cards:
                return self._wait_profile(session, "详细资料卡仍可见，等待其关闭")
            if len(menus) > 1:
                return self._wait_profile(session, "存在多个个人菜单候选，无法安全关闭")
            if menus:
                menu = menus[0]
                surface = ProfileSurface(reference.frame.capture.hwnd, reference.frame.pixels, reference.login.box, ())
                action = ProfileAction("A1_CLOSE", surface, reference.login.avatar_box)
                self.navigator.profile_click(reference, action, known_windows=session.known_windows, menu=menu)
                session.stage, session.polls = "VERIFY_RESTORED", 0
                return self._profile_status("CLOSING_PROFILE", "验证个人菜单已关闭")
            return self._finish_identity(session, current, owned, surfaces)
        except (NavigationPaused, OcrUnavailable, OSError) as exc:
            return self._wait_profile(session, str(exc) if isinstance(exc, NavigationPaused) else
                                      f"账号核验读取失败（{type(exc).__name__}）")

    def _wait_profile(self, session, reason):
        session.polls += 1
        state = session.stage if session.polls < 4 else "IDENTITY_VERIFICATION_FAILED"
        return self._profile_status(state, reason)

    def _finish_identity(self, session, frame, owned, surfaces):
        original = session.reference
        login = original.login
        b1, c1 = (original.layout.get(name) for name in ("B1", "C1"))
        if (any(hwnd not in session.known_windows for hwnd in owned) or surfaces
                or not login.unchanged(frame.pixels) or b1 is None or c1 is None
                or not all(np.array_equal(box.crop(original.frame.pixels), box.crop(frame.pixels)) for box in (b1, c1))):
            return self._wait_profile(session, "仍有资料层、窗口或消息页变化，等待恢复")
        self._profile_check = None
        if session.failed or not session.email or not session.close_issued:
            self._force_email = True
            self._failed_profile_lifetime = self._lifetime(frame)
            return self._profile_status("IDENTITY_VERIFICATION_FAILED", session.failed or "未取得有效邮箱")
        if not self.active:
            return self._status
        self._profile_status("BINDING_ACCOUNT", "正在核对本地邮箱绑定")
        lifetime = self._lifetime(frame)
        account, choices = self.store.bind_email(login, session.email, lifetime)
        if account is None:
            self._pending_email = (login, lifetime, session.email)
            self._set(account_email=session.email, account_choices=choices)
            return self._profile_status("CONFIRMATION_REQUIRED", "已核对邮箱，请确认沿用哪个历史账号或新建")
        self._accept_login(account, login, lifetime, email=session.email)
        return self._set(status="RUNNING", reason="邮箱已核验，下一轮重新识别消息主页面")

    def _accept_login(self, account, evidence, lifetime, *, email=""):
        if self._bound_login and self._bound_login[0] != account:
            for waiting in self._waiting_unread.values():
                self.store.job(waiting.job, "PAUSED", reason="账号已切换，等待重新核对")
                self._discovered.pop(waiting.job, None)
            self._waiting_unread.clear()
            self._scan_history = ()
            self._set(saved=0, added=0, repeated=0, uncertain=0, completed=0, errors=0, candidates=0,
                      pages=1, last_scan=None, recent=CollectionTrace(), trace=CollectionTrace(),
                      trace_job="", last_success=None, last_collected_at=None, target="", navigation_result="", ocr_result="",
                      sqlite_result="", notice=None, match="UNKNOWN", last_b2_summary="")
        self.profile = account
        self._verified_email = email
        self._failed_profile_lifetime = None
        self._force_email = False
        self._pending_email = None
        self._bound_login = (account, evidence, lifetime)
        self._requires_confirmation = False
        self._pending_login = None
        self._set(account_id=account, account_name=evidence.name, account_email=email,
                  account_state="IDENTITY_VERIFIED" if email else "BOUND",
                  account_reason="邮箱已核验并绑定本地账号" if email else "本地视觉绑定，官方账号 ID 未验证",
                  account_choices=())

    def confirm_account(self, account_id: int | None) -> ScanStatus:
        if self._require_email:
            if self._status.account_state != "CONFIRMATION_REQUIRED" or self._pending_email is None:
                return self._status
            evidence, lifetime, email = self._pending_email
            try:
                frame = self.capture()
                if self._lifetime(frame) != lifetime or not evidence.unchanged(frame.pixels):
                    self._pending_email = None
                    self._force_email = True
                    return self._profile_status("IDENTITY_CHECK_REQUIRED", "确认时 A1 或窗口变化，需重新核验")
                account, _ = self.store.bind_email(evidence, email, lifetime, confirmed=True, account_id=account_id)
                self.store.break_continuity()
                self._accept_login(account, evidence, lifetime, email=email)
                return self._set(status="RUNNING" if self.active else "READY", reason="邮箱账号绑定已确认",
                                 saved=self.store.browse(account)["message_count"])
            except CaptureError as exc:
                return self._window_error(exc)
            except (sqlite3.Error, OSError, ValueError):
                return self._profile_status("CONFIRMATION_REQUIRED", "无法确认该历史绑定，现有数据仍按原账号保存")
        pending = self._pending_login
        if self._status.account_state != "CONFIRMATION_REQUIRED" or pending is None:
            return self._status
        try:
            evidence, identity = pending
            frame = self.capture()
            if _identity(frame) != identity or not evidence.unchanged(frame.pixels):
                self._pending_login = None
                self._account_pause("确认期间 A1 或窗口已变化，请重新识别")
            if account_id is not None and account_id not in {row.id for row in self._status.account_choices}:
                raise ValueError("该历史账号已不在候选范围")
            account = self.store.confirm_login(evidence, account_id)
            self.store.break_continuity()
            self._handled.clear()
            self._accept_login(account, evidence, (identity[0], identity[1], identity[-1]))
            total = self.store.browse(account)["message_count"]
            return self._set(status="RUNNING" if self.active else "READY", reason="账号绑定已确认", saved=total)
        except AccountPaused as exc:
            return self._set(status="PAUSED", reason=str(exc), last_failure=str(exc))
        except CaptureError as exc:
            return self._window_error(exc)

    def _ensure_login_pixels(self, raw, fresh):
        if self._auto_account and (_identity(raw.frame) != _identity(fresh) or
                                   not raw.login.unchanged(fresh.pixels)):
            self._pending_login = None
            self._account_pause("保存前窗口或 A1 登录账号画面已变化，需重新识别")

    @staticmethod
    def _health_key(health: WindowHealth) -> tuple:
        sample = health.last
        return (health.status, health.reason, health.identity, health.geometry, health.foreground,
                sample.exists if sample else None, sample.iconic if sample else None,
                sample.show_command if sample else None, sample.owner if sample else None)

    def _record_window(self, health: WindowHealth | None) -> None:
        if health is None:
            return
        previous = self._status.window_health
        changes = {"window_health": health}
        if previous is None or self._health_key(previous) != self._health_key(health):
            old_status = previous.status if previous else "未检查"
            changes.update(window_change_at=health.checked_at, window_change=f"{old_status} → {health.status} · {health.reason}")
        self._set(**changes)

    def _invalidate_window_plan(self, reason: str) -> None:
        if self._window_plan_invalidated:
            return
        for job in self._queued.values():
            self.store.job(job, "PAUSED", navigation="REVALIDATE_WINDOW", reason=reason)
            self._discovered.pop(job, None)
        self._queued.clear()
        self._queued_signatures.clear()
        self._deferred.clear()
        self._handled.clear()
        self.store.break_continuity()
        self._window_plan_invalidated = True

    def _wait_for_window(self, reason: str, *, invalidate: bool = False) -> ScanStatus:
        if invalidate:
            self._invalidate_window_plan("窗口或坐标系失去连续性，旧未执行任务保留供核对")
        return self._set(status="PAUSED", reason=reason, waiting_for_window=True,
                         last_failure=reason, target="", queued=len(self._queued), match="UNKNOWN",
                         awaiting_navigation=len(self._queued), navigation_allowed=False, navigation_block_reason=reason,
                         scan_disposition="B2_NOT_SCANNED" if self._status.b2_scan_completed_at is None
                         else self._status.scan_disposition)

    def _window_error(self, exc: CaptureError | NavigationPaused) -> ScanStatus:
        if isinstance(exc, CaptureError):
            health = exc.diagnostics.window_health if exc.diagnostics else None
            self._record_window(health)
            return self._wait_for_window(str(exc).splitlines()[0], invalidate=True)
        self._record_window(exc.health)
        if exc.health is not None:
            return self._wait_for_window(str(exc), invalidate=bool(exc.health.capture_blocker)
                                         or str(exc).startswith("WINDOW_CHANGED"))
        return self._set(status="PAUSED", reason=str(exc), waiting_for_window=True)

    @staticmethod
    def _validate_read_frame(frame, expected=None):
        health = frame.diagnostics.window_health
        state, capture = frame.state, frame.capture
        pixels = frame.pixels
        if (state.minimized or not state.maximized or state.dpi < 1
                or pixels.dtype != np.uint8 or pixels.ndim != 3 or pixels.shape[2] != 3
                or pixels.shape[:2] != (state.height, state.width)
                or capture.width != state.width or capture.height != state.height
                or capture.rect is None or capture.pid < 1 or capture.hwnd < 1):
            raise NavigationPaused("截图尺寸、窗口状态或坐标证据无效")
        if health is not None and (
                health.status not in ("WINDOW_VALID", "WINDOW_NOT_FOREGROUND")
                or health.capture_blocker is not None
                or health.window is None or (health.window.pid, health.window.hwnd) != (capture.pid, capture.hwnd)
                or health.geometry != (capture.rect, (state.width, state.height), state.dpi)):
            raise NavigationPaused("截图窗口状态、PID、HWND 或坐标核对失败", health=health)
        if expected is not None and _identity(frame) != _identity(expected):
            raise NavigationPaused("预检与 OCR 截图窗口身份或坐标系不一致", health=health)

    def _record_b2_result(self, raw, attempted_at):
        observed = unread_targets(raw, self.scope)
        skipped = tuple(target for target in observed if target.skip_reason)
        targets = tuple(target for target in observed if not target.skip_reason)
        marker_count = len(observed)
        unreliable = tuple(target for target in targets if target.ambiguous)
        reliable_count = len(targets) - len(unreliable)
        reasons = tuple(dict.fromkeys(reason for target in unreliable for reason in target.blockers))
        completed = EventTime.now().wall
        conversation = raw.conversation
        self._set(b2_scan_attempt_at=attempted_at.wall, b2_scan_completed_at=completed, last_scan=completed,
                  contact_count=len(raw.contacts), unread_marker_count=marker_count,
                  reliable_unread_count=reliable_count, unreliable_unread_count=len(unreliable),
                  unreliable_reasons=reasons, candidates=reliable_count, navigation_block_reason="",
                  skipped_unread=len(skipped), skipped_reasons=tuple(dict.fromkeys(t.skip_reason for t in skipped)),
                  scan_disposition="B2_SCANNED_UNREAD" if reliable_count else
                  "B2_SCANNED_UNRELIABLE" if unreliable else
                  "SKIPPED_BY_POLICY" if skipped else "B2_SCANNED_NO_UNREAD",
                  awaiting_navigation=len(self._queued), selection_method=conversation.selection_method,
                  selection_name=conversation.selected_name, selection_name_reliable=conversation.selected_name_reliable,
                  selection_confidence=conversation.selection_confidence,
                  selection_evidence=conversation.selection_evidence,
                  last_b2_summary=f"{self._status.scan_id} · {completed.isoformat(timespec='milliseconds')} · "
                  f"联系人 {len(raw.contacts)} · 未读 {marker_count} · 可靠 {reliable_count} · 不可靠 {len(unreliable)}")
        return targets

    def _bind_frame(self, frame) -> None:
        key = _identity(frame)
        old = self._window
        if old == key and not self._window_plan_invalidated:
            return
        if old is not None and old != key:
            self._invalidate_window_plan("重新绑定窗口，旧坐标和未执行计划已作废")
        recreated = old is not None and (old[:2] != key[:2] or old[-1] != key[-1])
        code = "WINDOW_RECREATED" if recreated else "WINDOW_REBOUND" if old is not None else "WINDOW_BOUND"
        at = datetime.now().astimezone()
        self._set(bound_pid=key[0], bound_hwnd=key[1], rebind_at=at,
                  rebind_result=f"{code} · PID {key[0]} · HWND 0x{key[1]:X} · 已核对新截图与坐标",
                  window_change_at=at if old != key else self._status.window_change_at,
                  window_change=f"{code} · 窗口绑定或坐标系变化" if old != key else self._status.window_change,
                  waiting_for_window=self._status.waiting_for_window or (old is not None and old != key))
        self._window = key
        self._window_plan_invalidated = False

    def _numbers(self, result: SaveResult, **fields) -> ScanStatus:
        fields.update(added=self._status.added + result.new_candidates, repeated=self._status.repeated + result.repeated,
                      uncertain=self._status.uncertain + result.uncertain)
        return self._set(**fields)

    def _event(self, name: str, at: EventTime | None = None, **feedback):
        stamp = at or EventTime.now()
        self._set(trace=replace(self._status.trace, **{name: stamp}),
                  recent=replace(self._status.recent, **{name: stamp}), **feedback)
        return stamp

    def _ocr_completed(self, raw):
        return self._event("ocr_completed_at", ocr_result=f"{raw.ocr_status} · {raw.message_completeness} · "
                           f"{len(raw.messages)} 条可见观察 · {raw.recognition_ms:.0f} ms")

    def _committed(self, result: SaveResult, title: str, job=None):
        if result.committed_at is None:
            return
        self._event("sqlite_committed_at", result.committed_at,
                    sqlite_result=f"{result.outcome} · 保存 {result.inserted} · 新增 {result.new_candidates} · "
                                  f"重复 {result.repeated} · 未确认 {result.uncertain}")
        trace = self._status.trace
        if result.conversation_id and not result.uncertain:
            self._set(last_collected_at=result.committed_at)
        if result.new_candidates > 0:
            self._set(last_success=trace, notice=SaveNotice(result.capture_id, title, result.new_candidates, trace, job))
        if job:
            try:
                self.store.job_event(job, "SQLITE_COMMITTED", result.committed_at, {
                    **trace.detail(), "saved": result.inserted, "new": result.new_candidates,
                    "repeated": result.repeated, "uncertain": result.uncertain,
                })
            except (sqlite3.Error, OSError):
                self._set(last_failure="消息已保存，追加任务耗时证据失败")

    def identify_account(self) -> ScanStatus:
        if not self._auto_account:
            return self._status
        self._set(status="CAPTURING", reason="读取 A1 登录账号", account_state="IDENTIFYING",
                  account_choices=(), notice=None)
        identify = self.read.identify if isinstance(self.read, CollectionReader) else self.read
        try:
            for _ in range(2):
                frame = self.capture()
                self._record_window(frame.diagnostics.window_health)
                self._bind_frame(frame)
                try:
                    self._check_login(identify(frame))
                    saved = self.store.browse(self.profile)["message_count"] if self._require_email else self._status.saved
                    return self._set(status="READY", reason="当前账号已绑定", saved=saved)
                except AccountPaused:
                    if self._status.account_state != "IDENTIFYING":
                        raise
            return self._set(status="PAUSED", reason=self._status.account_reason)
        except AccountPaused as exc:
            return self._set(status="PAUSED", reason=str(exc), last_failure=str(exc))
        except CaptureError as exc:
            state = self._window_error(exc)
            self._pending_login = None
            self.profile = None
            return self._set(account_state="IDENTIFICATION_FAILED", account_reason=state.reason, account_id=None)

    def _remember(self, target: _Target):
        self._handled[target.key] = target.unread_signature
        if len(self._handled) > 128:
            self._handled.pop(next(iter(self._handled)))

    def save_current(self) -> ScanStatus:
        self._set(trace=CollectionTrace(), trace_job="", notice=None)
        if self._auto_account:
            state = self.identify_account()
            if state.status != "READY":
                return state
        try:
            raw = self._read()
            self._ocr_completed(raw)
            self._record_window(raw.frame.diagnostics.window_health)
            self._bind_frame(raw.frame)
            self._check_login(raw, allow_binding=False)
            if self._auto_account:
                self._ensure_login_pixels(raw, self.capture())
            result = self.store.save(self.profile, raw)
        except CaptureError as exc:
            if exc.diagnostics:
                self._record_window(exc.diagnostics.window_health)
            reason = str(exc).splitlines()[0]
            return self._set(status="PAUSED", reason=reason, last_failure=reason, waiting_for_window=False)
        except AccountPaused as exc:
            return self._set(status="PAUSED", reason=str(exc), last_failure=str(exc))
        except (sqlite3.Error, OSError, ValueError) as exc:
            reason = f"本地保存失败（{type(exc).__name__}）"
            return self._set(status="FAILED", reason=reason, last_failure=reason, sqlite_result=reason)
        self._committed(result, raw.conversation.title)
        try:
            total = self.store.browse(self.profile)["message_count"]
        except (sqlite3.Error, OSError):
            total = self._status.saved + result.inserted
        return self._numbers(result, status=result.outcome, reason=result.reason, target=raw.conversation.title,
                             match=raw.conversation.match_status, saved=total)

    def tick(self) -> ScanStatus:
        if not self._lock.acquire(blocking=False):
            return self._status
        scan_id = ""
        try:
            if not self.active:
                return self._set(status="STOPPED", reason="自动采集许可已撤销", waiting_for_window=False)
            self._round += 1
            scan_id = uuid4().hex[:12]
            self._set(notice=None, scan_id=scan_id, scan_disposition="B2_NOT_SCANNED",
                      screenshot_succeeded_at=None, b2_scan_attempt_at=None, b2_scan_completed_at=None,
                      scan_ocr_completed_at=None, candidates=0, contact_count=0, unread_marker_count=0,
                      reliable_unread_count=0, unreliable_unread_count=0, unreliable_reasons=(),
                      foreground_changed_stage="", navigation_block_reason="", navigation_allowed=None,
                      selection_name="", selection_name_reliable=False, selection_method="UNKNOWN", selection_confidence="UNKNOWN",
                      selection_evidence="", skipped_unread=0, skipped_reasons=())
            if self._operations >= 12:
                self.stop()
                return self._set(status="STOPPED", reason="连续 12 次导航未完成采集，已停止以避免反复操作")
            if self._window_probe is not None:
                health = self._window_probe()
                self._record_window(health)
                if not self.active:
                    return self._set(status="STOPPED", reason="自动采集许可已撤销", waiting_for_window=False)
                if health.capture_blocker:
                    message = "请手动最大化企业微信" if health.capture_blocker == "NOT_MAXIMIZED" else health.reason
                    return self._wait_for_window(f"{health.capture_blocker} · {message}", invalidate=True)
            else:
                health = None
            frame = self.capture()
            captured = frame.diagnostics.window_health
            self._record_window(captured)
            if not self.active:
                return self._set(status="STOPPED", reason="自动采集许可已撤销", waiting_for_window=False)
            if health and ((frame.capture.pid, frame.capture.hwnd) != (health.window.pid, health.window.hwnd)
                           or (captured and (captured.identity != health.identity or captured.geometry != health.geometry))):
                return self._wait_for_window("WINDOW_CHANGED · 预检与截图不一致，等待重新绑定", invalidate=True)
            try:
                self._validate_read_frame(frame)
            except NavigationPaused as exc:
                return self._wait_for_window(str(exc), invalidate=True)
            self._bind_frame(frame)
            if self._require_email and (self._profile_check is not None or self._force_email
                                        or self.profile is None or self._bound_login is None
                                        or self._bound_login[2] != self._lifetime(frame)):
                return self._profile_tick(frame)
            self._set(screenshot_succeeded_at=frame.captured_at,
                      foreground_changed_stage="截图" if (
                          (captured and captured.status == "WINDOW_NOT_FOREGROUND")
                          or (health and health.status == "WINDOW_NOT_FOREGROUND")) else "")
            if self._auto_account and self._status.account_state == "UNIDENTIFIED":
                self._set(account_state="IDENTIFYING", account_reason="正在读取 A1")
            self._set(trace=CollectionTrace(), trace_job="")
            b2_attempt = EventTime.now()
            self._set(status="RUNNING", reason="正在识别本轮 B2 联系人",
                      b2_scan_attempt_at=b2_attempt.wall, scan_disposition="B2_SCANNING")
            raw = self.read(frame)
            ocr_at = self._ocr_completed(raw)
            self._set(match=raw.conversation.match_status, scan_ocr_completed_at=ocr_at.wall)
            if not self.active:
                return self._set(status="STOPPED", reason="已停止，未执行导航")
            self._check_login(raw)
            self._validate_read_frame(raw.frame, frame)
            if raw.page != "MESSAGES_CANDIDATE":
                raise NavigationPaused("未确认当前企业微信为消息页面")
            if raw.layout.get("B2") is None or any(step.status in ("FAILED", "SKIPPED") for step in raw.ocr_areas
                                                    if step.region.startswith("B2")):
                self._set(scan_disposition="B2_SCAN_FAILED", navigation_block_reason="B2 OCR 或区域识别失败")
                raise NavigationPaused("B2 联系人扫描失败")
            navigation_window_reason = ""
            if captured and captured.status == "WINDOW_NOT_FOREGROUND":
                navigation_window_reason = "WINDOW_NOT_FOREGROUND · 需要前台新截图复核"
            if self._window_probe is not None:
                current = self._window_probe()
                self._record_window(current)
                if not self.active:
                    return self._set(status="STOPPED", reason="已停止，未执行导航")
                if current.capture_blocker:
                    return self._wait_for_window(f"{current.capture_blocker} · {current.reason}", invalidate=True)
                if health and (current.identity != health.identity or current.geometry != health.geometry):
                    return self._wait_for_window("WINDOW_CHANGED · OCR 后企业微信窗口身份或坐标变化", invalidate=True)
                elif current.status == "WINDOW_NOT_FOREGROUND":
                    stage = "截图" if health and health.status == "WINDOW_NOT_FOREGROUND" else "OCR"
                    self._set(foreground_changed_stage=self._status.foreground_changed_stage or stage)
                    navigation_window_reason = f"{current.status} · {current.reason}"
            targets = self._record_b2_result(raw, b2_attempt)
            if self._recover:
                self.store.recover_jobs(self.profile, self._run_id)
                self._recover = False
            if self._status.waiting_for_window:
                self._set(waiting_for_window=False, resumed_at=datetime.now().astimezone())
            return self._scan(raw, targets, navigation_window_reason)
        except AccountPaused as exc:
            self._set(scan_disposition="B2_SCAN_FAILED", navigation_allowed=False, navigation_block_reason=str(exc))
            return self._set(status="PAUSED" if self.active else "STOPPED", reason=str(exc), last_failure=str(exc))
        except (CaptureError, NavigationPaused) as exc:
            if not self.active:
                return self._set(status="STOPPED", reason="自动采集许可已撤销", waiting_for_window=False)
            state = self._window_error(exc)
            disposition = "B2_SCAN_FAILED" if state.scan_disposition == "B2_SCANNING" else state.scan_disposition
            return self._set(last_failure=state.reason, scan_disposition=disposition, navigation_allowed=False,
                             navigation_block_reason=state.reason)
        except (sqlite3.Error, OSError, ValueError) as exc:
            self.stop()
            reason = f"采集或存储失败（{type(exc).__name__}）"
            return self._set(status="FAILED", reason=reason, last_failure=reason)
        finally:
            if scan_id:
                self._scan_history = (*self._scan_history[-7:], self._status)
            self._lock.release()

    def _scan(self, raw, current, navigation_window_reason):
        still_visible = {_Target(row, name_key(row.display_name), avatar_key(raw, row), ()).key for row in raw.contacts
                         if row.name_reliable and row.status == "CANDIDATE" and not row.has_unread_indicator}
        unread_keys = {target.key for target in current}
        for key in still_visible - unread_keys:
            self._handled.pop(key, None)
            waiting = self._waiting_unread.pop(key, None)
            if waiting:
                self._discovered.pop(waiting.job, None)
                self.store.job(waiting.job, "PAUSED", reason="复扫已不见未读标记，保留历史观察")
        eligible = {target.key: target for target in current[:24] if not target.ambiguous}
        visible = {target.key: target for target in current[:24]}
        for key, job in tuple(self._queued.items()):
            target = visible.get(key)
            previous = self._queued_signatures.get(key, (None, ""))
            if target and _new_unread(previous, target.unread_signature):
                self.store.job(job, "PAUSED", reason="出现新的未读数量或消息预览，保留旧观察")
                self._queued.pop(key)
                self._queued_signatures.pop(key, None)
                self._deferred.pop(key, None)
                self._discovered.pop(job, None)
            elif target and target.ambiguous:
                self._waiting_unread[key] = _WaitingUnread(job, previous)
        for key in tuple(self._queued):
            if key not in eligible:
                job = self._queued.pop(key)
                self._queued_signatures.pop(key, None)
                self._deferred.pop(key, None)
                if key not in self._waiting_unread:
                    self._discovered.pop(job, None)
                    self.store.job(job, "PAUSED", reason="联系人行或未读证据已变化，等待新观察")
        for key, target in visible.items():
            if key in self._queued:
                self._queued_signatures[key] = _merge_unread(self._queued_signatures[key], target.unread_signature)
                reason = self._deferred.get(key)
                if reason:
                    self.store.refresh_waiting_job(self._queued[key], target.row, self._status.scan_id)
                continue
            if not self.active:
                return self._set(status="STOPPED", reason="已停止，未执行导航")
            signature = target.unread_signature
            handled = self._handled.get(key)
            if handled is not None and not _new_unread(handled, signature):
                self._handled[key] = _merge_unread(handled, signature)
                continue
            waiting = self._waiting_unread.get(key)
            if waiting and _new_unread(waiting.signature, signature):
                self.store.job(waiting.job, "PAUSED", reason="出现新的未读数量或消息预览，保留旧观察")
                self._discovered.pop(waiting.job, None)
                self._waiting_unread.pop(key)
                waiting = None
            if waiting is None:
                detected = self._event("unread_detected_at")
                job = self.store.create_job(self.profile, self._run_id, key, target.row, detected=detected)
                self._discovered[job] = detected
                waiting = _WaitingUnread(job, signature)
            if target.ambiguous:
                waiting.signature = _merge_unread(waiting.signature, signature)
                self._waiting_unread[key] = waiting
                self.store.observe_waiting_job(waiting.job, target.row, "；".join(target.blockers),
                                               self._status.scan_id)
            else:
                if self._waiting_unread.pop(key, None):
                    self.store.job(waiting.job, "QUEUED", navigation="NOT_STARTED", reason="本轮已复核未读目标")
                self._queued[key] = waiting.job
                self._queued_signatures[key] = _merge_unread(waiting.signature, signature)
        self._set(queued=len(self._queued), awaiting_navigation=len(self._queued),
                  saved=self.store.browse(self.profile)["message_count"])
        if navigation_window_reason and (self._queued or current):
            return self._set(status="PAUSED", waiting_for_window=True, reason=navigation_window_reason,
                             navigation_block_reason=navigation_window_reason, navigation_allowed=False,
                             scan_disposition="B2_SCANNED_WAITING_NAVIGATION" if self._queued else self._status.scan_disposition)
        for key, target in eligible.items():
            if key in self._queued and self._deferred.get(key, 0) <= self._round:
                return self._collect(raw, target, self._queued[key])
        if self._queued:
            return self._set(status="PAUSED", reason="等待重新复核局部阻断的未读目标",
                             navigation_allowed=False, scan_disposition="B2_SCANNED_WAITING_NAVIGATION")
        if self._status.unreliable_unread_count:
            reason = "检测到未读，等待重新验证 · " + "；".join(self._status.unreliable_reasons)
            return self._set(status="PAUSED", target="", reason=reason, navigation_allowed=False,
                             navigation_block_reason=reason)
        if (not navigation_window_reason and self._scroll and self._status.pages < 4
                and sum(row.is_selected is True for row in raw.contacts) == 1):
            return self._scroll_once(raw)
        if current:
            reason = "可见未读已记录，本轮没有新的导航任务"
        elif self._status.skipped_unread:
            reason = "发现未读，均已按采集范围或入口类型跳过"
        else:
            reason = "本轮 B2 扫描未发现未读"
        return self._set(status="RUNNING", target="", reason=reason, navigation_allowed=False)

    def _collect(self, raw: VisualObservation, target: _Target, job: str) -> ScanStatus:
        trace = CollectionTrace(unread_detected_at=self._discovered.get(job))
        self._set(status="VERIFYING_CONVERSATION", target=target.row.display_name,
                  reason="复核 B2 目标和当前窗口", trace=trace, trace_job=job)
        captured = None
        attempted = False
        clicked = False
        verified = False

        def record_opened(partial):
            if (_identity(partial.frame) != _identity(raw.frame) or not _matches_target(partial, target)
                    or (self._auto_account and not raw.login.unchanged(partial.frame.pixels))):
                return
            self.navigator.verify_view(partial)
            at = self._event("conversation_opened_at", navigation_result="已核对 B2 选中行及 C1 标题")
            self.store.job_event(job, "CONVERSATION_OPENED", at, {"conversation_opened_at": at.iso()})
            self._set(status="CAPTURING", reason="正在识别已核对会话的可见消息")

        def read_opened():
            self._set(trace=replace(self._status.trace, conversation_opened_at=None, ocr_completed_at=None))
            observed = self._read(on_conversation=record_opened)
            if clicked:
                observed = _verify_transition(raw, observed, target)
            at = self._ocr_completed(observed)
            self.store.job_event(job, "OCR_COMPLETED", at,
                                 {"ocr_completed_at": at.iso(), "summary": self._status.ocr_result})
            return observed

        try:
            if not self.active:
                raise NavigationPaused("已停止，未执行导航")
            if not _matches_target(raw, target):
                if target.row.is_selected is True:
                    raise NavigationPaused("B2 目标显示已选中，但 C1 或会话类型未核实", local=True)
                self.navigator.verify_view(raw, need_idle=True)
                fields = {"input_diagnostic": self.navigator.input_diagnostic} if isinstance(self.navigator, CollectionNavigation) else {}
                self._set(navigation_allowed=True, **fields)
                self._set(status="NAVIGATING", reason="提交未读证据并复核 B2 点击位置")
                started = self._event("navigation_started_at", navigation_result="正在验证并尝试进入未读会话")
                self.store.job(job, "NAVIGATING", risk=True, navigation="CLICK_INTENT",
                               detail={"navigation_started_at": started.iso(),
                                       "badge_before": target.row.has_unread_indicator, "count_before":
                                       target.row.unread_count if target.row.unread_count_reliable else None,
                                       "selected_before": raw.conversation.selected_name, "title_before": raw.conversation.title})
                attempted = True
                self._operations += 1
                self.navigator.select(raw, target.row)
                clicked = True
                self._set(status="VERIFYING_CONVERSATION", reason="重新截图核对标题、选中行和 C2",
                          navigation_result="点击已执行，等待视觉核对")
                self.store.job(job, "VERIFYING_CONVERSATION", navigation="CLICK_ISSUED")
                if self._cancel.wait(.30):
                    raise NavigationPaused("停止后点击结果待人工核对")
                captured = read_opened()
            else:
                self.store.job(job, "VERIFYING_CONVERSATION", navigation="ALREADY_SELECTED")
                captured = raw
                if self._status.recent.ocr_completed_at:
                    self._set(trace=replace(self._status.trace, ocr_completed_at=self._status.recent.ocr_completed_at),
                              navigation_result="目标原本已选中，无点击")
            reason = self._verify_capture(raw, captured, target)
            if reason and clicked:
                if self._cancel.wait(.30):
                    raise NavigationPaused("停止后点击结果待人工核对")
                captured = read_opened()
                reason = self._verify_capture(raw, captured, target)
            if reason:
                if self._auto_account:
                    self._ensure_login_pixels(captured, self.capture())
                if not self.active:
                    raise NavigationPaused("已停止，未保存本次采集")
                self._set(navigation_result=reason, last_failure=reason)
                result = self.store.save(self.profile, captured, job_id=job, allow_link=False, failure_reason=reason)
                return self._finish_task(target, result, captured, "NAVIGATION_UNCERTAIN", reason, job)
            self.navigator.verify_view(captured)
            if not self.active:
                raise NavigationPaused("已停止，未保存本次采集")
            if self.scope is not None and not self.scope.group and captured.conversation.conversation_type == "GROUP":
                raise NavigationPaused("打开后证实为已排除的群聊；保留可能已读证据")
            selected = next(row for row in captured.contacts if name_key(row.display_name) == target.name)
            if clicked and self._status.trace.conversation_opened_at is None:
                at = self._event("conversation_opened_at", navigation_result="已核对 B2 选中行及 C1 标题")
                self.store.job_event(job, "CONVERSATION_OPENED", at, {"conversation_opened_at": at.iso()})
            self._set(status="PERSISTING", reason="提交已核对会话和视觉观察")
            self.store.job(job, "CAPTURING", navigation="VERIFIED",
                           detail={"title_after": captured.conversation.title, "type": captured.conversation.conversation_type,
                                   "badge_after": selected.has_unread_indicator,
                                   "count_after": selected.unread_count if selected.unread_count_reliable else None})
            self.store.job(job, "PERSISTING", capture=captured.message_completeness)
            if self._auto_account:
                self._ensure_login_pixels(captured, self.capture())
            if not self.active:
                raise NavigationPaused("已停止，未保存本次采集")
            verified = True
            result = self.store.save(self.profile, captured, job_id=job)
            status = "COMPLETED" if result.conversation_id and not result.uncertain else "UNCERTAIN"
            reason = result.reason if status == "COMPLETED" else f"{result.reason}；如已点击，未读可能已清除"
            return self._finish_task(target, result, captured, status, reason, job)
        except (NavigationPaused, CaptureError) as exc:
            block_reason = str(exc).splitlines()[0]
            local = isinstance(exc, NavigationPaused) and exc.local and not clicked
            if local and self.active:
                self._deferred[target.key] = self._round + 4
                self.store.observe_waiting_job(job, target.row, block_reason, self._status.scan_id, no_click=True)
            else:
                self.store.job(job, "UNCERTAIN" if clicked else "PAUSED", no_click=not clicked,
                               navigation="NAVIGATION_UNCERTAIN" if clicked else "BLOCKED",
                               reason=block_reason)
            if isinstance(self.navigator, CollectionNavigation):
                self._set(input_diagnostic=self.navigator.input_diagnostic)
            if clicked or not self.active:
                self._queued.pop(target.key, None)
                self._queued_signatures.pop(target.key, None)
                self._deferred.pop(target.key, None)
                self._discovered.pop(job, None)
            if clicked:
                self._remember(target)
            if self.active and (isinstance(exc, CaptureError) or exc.health is not None):
                self._window_error(exc)
            if not self.active:
                return self._set(status="STOPPED", reason="自动采集许可已撤销", waiting_for_window=False,
                                 queued=len(self._queued), errors=self._status.errors + int(clicked))
            reason = str(exc).splitlines()[0] + ("；未读可能已清除" if clicked else "")
            waiting = not clicked and target.key in self._queued
            foreground_stage = self._status.foreground_changed_stage
            if "WINDOW_NOT_FOREGROUND" in reason and not foreground_stage:
                foreground_stage = "点击前"
            return self._set(status="PAUSED" if waiting else "UNCERTAIN",
                             reason=reason, navigation_result=reason, navigation_block_reason=reason,
                             navigation_allowed=False,
                             foreground_changed_stage=foreground_stage,
                             scan_disposition="B2_SCANNED_WAITING_NAVIGATION" if waiting else
                             self._status.scan_disposition, queued=len(self._queued),
                             awaiting_navigation=len(self._queued), waiting_for_window=waiting and not local,
                             errors=self._status.errors + int(clicked))
        except Exception as exc:
            self.stop()
            reason = f"采集或数据库写入失败（{type(exc).__name__}）"
            try:
                self.store.job(job, "FAILED", navigation="VERIFIED" if verified else "NAVIGATION_UNCERTAIN",
                               reason=reason)
            except (sqlite3.Error, OSError):
                pass  # The pre-action committed job remains available after restart.
            self._queued.pop(target.key, None)
            self._queued_signatures.pop(target.key, None)
            self._deferred.pop(target.key, None)
            self._discovered.pop(job, None)
            return self._set(status="FAILED", reason=reason + ("；未读可能已清除" if attempted else ""),
                             last_failure=reason, sqlite_result=reason if verified else self._status.sqlite_result,
                             queued=len(self._queued), errors=self._status.errors + 1)

    def _verify_capture(self, raw, observed, target):
        if not self.active:
            raise NavigationPaused("已停止，未保存本次采集")
        self._check_login(observed, allow_binding=False)
        self._validate_read_frame(observed.frame, raw.frame)
        if _identity(observed.frame) != _identity(raw.frame) or not _matches_target(observed, target):
            return "切换后 B2 选中行、C1 标题或会话类型与目标不符"
        summary = summarize(observed)
        stable_frame = self.capture()
        self._ensure_login_pixels(observed, stable_frame)
        if (summary.regions is None or _identity(stable_frame) != _identity(observed.frame)
                or probe_frame(stable_frame, summary.regions).differs(probe_frame(observed.frame, summary.regions))):
            return "切换后标题、选中行或聊天区仍在变化"
        return ""

    def _finish_task(self, target, result, raw, status, reason, job):
        self._queued.pop(target.key, None)
        self._queued_signatures.pop(target.key, None)
        self._discovered.pop(job, None)
        self._remember(target)
        self._deferred.pop(target.key, None)
        if status == "COMPLETED":
            self._operations = 0
        self._committed(result, raw.conversation.title, job)
        return self._numbers(result, status=status, reason=reason, match=raw.conversation.match_status,
                             saved=self._status.saved + result.inserted, queued=len(self._queued),
                             awaiting_navigation=len(self._queued),
                             completed=self._status.completed + int(status == "COMPLETED"),
                             errors=self._status.errors + int(status != "COMPLETED"))

    def _scroll_once(self, raw):
        area = raw.layout.get("B2")
        keys = _page_keys(raw)
        rows = [row.box for row in raw.contacts if row.name_reliable]
        if (area is None or len(keys) < 3 or len(keys) != len(set(keys)) or not rows
                or (raw.list_scrollbar is None and area.bottom - rows[-1].bottom > rows[-1].height * 1.5)):
            self._scroll = False
            return self._set(status="RUNNING", reason="B2 未取得继续翻页的可靠位置证据")
        if not self.active:
            return self._set(status="STOPPED", reason="已停止，未执行滚动")
        self._operations += 1
        try:
            self.navigator.scroll_visible_contacts(raw)
            if self._cancel.wait(.35):
                return self._set(status="STOPPED", reason="滚动后已停止")
            after = self._read()
            self._ocr_completed(after)
            self._check_login(after, allow_binding=False)
            self.navigator.verify_view(after)
        except (NavigationPaused, CaptureError) as exc:
            self._scroll = False
            if not self.active:
                return self._set(status="STOPPED", reason="自动采集许可已撤销", waiting_for_window=False)
            self._window_error(exc)
            return self._set(status="PAUSED", reason=f"B2 滚动复核失败 · {exc}")
        view = summarize(raw).regions
        board = raw.layout.get("D")
        changed = page_transition(keys, _page_keys(after))
        if (view is None or _identity(after.frame) != _identity(raw.frame)
                or raw.layout.get("B2") != after.layout.get("B2")
                or raw.layout.get("D") != after.layout.get("D")
                or (board is not None and not np.array_equal(board.crop(raw.frame.pixels), board.crop(after.frame.pixels)))
                or _different(probe_frame(raw.frame, view).history, probe_frame(after.frame, view).history, localized=True)
                or raw.conversation.title != after.conversation.title):
            changed = "SCROLL_UNCERTAIN"
        if changed != "SCROLLED":
            self._scroll = False
            return self._set(status=changed, reason="滚动无有效位移或前后列表关系不明，停止继续翻页")
        return self._set(status="SCROLLED", pages=self._status.pages + 1, reason="B2 已确认向下翻页")
