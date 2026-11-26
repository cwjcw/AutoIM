"""Serial visible-unread collection using the existing capture/OCR pipeline."""

from __future__ import annotations

import sqlite3
from concurrent.futures import CancelledError
from hashlib import blake2s
from collections import Counter, OrderedDict
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from threading import Event, Lock
from time import perf_counter
from uuid import uuid4

import cv2
import numpy as np

from autoim.calibration import CalibrationStore
from autoim.runtime_paths import runtime_paths
from autoim.wecom.collection_hover import HoverBudget
from autoim.wecom.collection_core import (MAX_READ_POLLS, CoreRun, CoreTask, CoreView, analyze_read, avatar_distance, bounds,
                                         failure_crop_directory, history_changed, history_sample)
from autoim.wecom.collection_events import CollectionTrace, EventTime, SaveNotice
from autoim.wecom.collection_evidence import c2_evidence_regions, contact_scan, message_evidence, rect, title_evidence
from autoim.wecom.collection_incremental import CoreAccount, DefaultAccountConflict, attach_times, preview_visible, save_visible
from autoim.wecom.collection_pending import CoreDurability, PendingBatch, SavingBlocked, saving_reason
from autoim.wecom.collection_navigation import CollectionNavigation, CollectionPermit, NavigationPaused, _identity, isolated_row, row_target
from autoim.wecom.collection_profile import (ProfileAction, ProfileCheck, ProfileSurface, ProfileWindows, card_close,
                                              email_from_spans, is_profile_card, menu_action, unique_cards, unique_menu)
from autoim.wecom.collection_runtime import safe_reason
from autoim.wecom.collection_test import TestClock, TestDeadline
from autoim.wecom.collection_worker import CoreReadWorker
from autoim.wecom.visual_account import LoginEvidence, read_login
from autoim.wecom.visual_capture import CaptureError, capture_visible_window
from autoim.wecom.visual_b2 import mute_region
from autoim.wecom.visual_contacts import ContactObservation, muted_icon_region
from autoim.wecom.visual_layout import Box, LayoutObservation, VisualThresholds, detect_layout
from autoim.wecom.visual_monitor import _different, probe_frame, summarize
from autoim.wecom.visual_observation import VisualObservation, observe_frame
from autoim.wecom.visual_ocr import LocalTextRecognizer, OcrUnavailable
from autoim.wecom.visual_store import LocalAccount, SaveResult, VisualStore, avatar_key, name_key
from autoim.wecom.window_manager import WeComWindowManager, WindowResolutionError
from autoim.wecom.window_state import WindowHealth, probe_window


class CollectionReader:
    def __init__(self, calibration: Path | None, thresholds: Path | None) -> None:
        self._calibration_path = calibration
        self._thresholds_path = thresholds
        self._settings = None
        self._ocr = None
        self._core_cache = OrderedDict()

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

    def read_b2(self, frame):
        self._prepare()
        return observe_frame(frame, self._settings[0], self._ocr, self._settings[1], contacts_only=True)

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

    def read_core(self, frame, message_gate, *, history_only=False):
        started = perf_counter()
        own = self.identify(frame).login
        raw = observe_frame(frame, self._settings[0], self if self._ocr is not None else None, self._settings[1],
                            message_gate=lambda header: message_gate(replace(header, login=own)),
                            history_only=history_only, multi_feature=True)
        return replace(raw, login=own, recognition_ms=(perf_counter() - started) * 1000)

    def read(self, pixels, box, *, single_line=False):
        # A new capture is still required. Reuse OCR only for byte-identical crops.
        image = np.ascontiguousarray(box.crop(pixels))
        key = box, image.shape, single_line, blake2s(image, digest_size=16).digest()
        if key not in self._core_cache:
            self._core_cache[key] = self._ocr.read(pixels, box, single_line=single_line)
            if len(self._core_cache) > 48:
                self._core_cache.popitem(last=False)
        else:
            self._core_cache.move_to_end(key)
        return self._core_cache[key]


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
    storage_state: str = ""
    storage_pending: int = 0
    storage_invalid: int = 0
    storage_recovered: int = 0
    last_failure: str = ""
    failure_stage: str = ""
    failure_code: str = ""
    failure_at: str = ""
    failure_level: str = ""
    stop_reason: str = ""
    assist: str = ""
    notice: SaveNotice | None = None
    isolated_choices: tuple[IsolatedChoice, ...] = ()
    trace_job: str = ""
    screenshot_succeeded_at: datetime | None = None
    b2_scan_attempt_at: datetime | None = None
    b2_scan_completed_at: datetime | None = None
    contact_count: int = 0
    unread_marker_count: int = 0
    unowned_unread_count: int = 0
    muted_excluded_count: int = 0
    folded_excluded_count: int = 0
    reliable_unread_count: int = 0
    unreliable_unread_count: int = 0
    unreliable_reasons: tuple[str, ...] = ()
    skipped_unread: int = 0
    policy_skipped_unread: int = 0
    unsupported_unread: int = 0
    unknown_type_unread: int = 0
    unknown_muted_unread: int = 0
    other_unreliable_unread: int = 0
    navigation_blocked_unread: int = 0
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
    core: CoreView | None = None


@dataclass(frozen=True, slots=True)
class CollectionScope:
    personal: bool = True
    group: bool = True
    muted: bool = False

    def exclusion(self, row: ContactObservation) -> str:
        if row.kind in ("FOLDED_CHAT_CONTAINER", "SPECIAL_ENTRY", "BOT_CANDIDATE", "SYSTEM_CANDIDATE") and row.name_reliable:
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
    duplicate_name: bool = False

    @property
    def scope_decision(self):
        if self.skip_reason.startswith("不支持的会话入口"):
            return "UNSUPPORTED_ENTRY"
        if self.skip_reason:
            return "SKIPPED_BY_POLICY"
        return "WAITING_REVALIDATION" if self.blockers else "ELIGIBLE"

    @property
    def uncertainty(self):
        if any("入口" in item or "类型" in item for item in self.blockers):
            return "type"
        if "静音状态无法确认" in self.blockers:
            return "muted"
        return "other"

    @property
    def key(self) -> str:
        labels = "|".join(map(name_key, self.row.identity_labels))
        if self.duplicate_name or not self.row.name_reliable and self.row.geometry_reliable:
            return f"GEOMETRY|{self.row.box.left}|{self.row.box.top}|{self.avatar}|{self.name}"
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


def _core_read_rank(analysis):
    return (sum(item.quality == "RELIABLE" for item in analysis.messages),
            -len(analysis.gaps), analysis.latest != "UNKNOWN", len(analysis.messages))


def unread_targets(observation: VisualObservation, scope: CollectionScope | None = None, *,
                   allow_duplicate=False) -> tuple[_Target, ...]:
    """B2 is the source, including muted rows and undifferentiated red dots."""
    if observation.page != "MESSAGES_CANDIDATE" or observation.layout.get("B2") is None:
        return ()
    names = Counter(name_key(row.display_name) for row in observation.contacts if row.name_reliable and row.display_name)
    targets = []
    for row in observation.contacts:
        if not row.has_unread_indicator:
            continue
        name = name_key(row.display_name)
        duplicate = names[name] > 1
        physical = isolated_row(row, observation.contacts)
        duplicate_read = allow_duplicate and duplicate and row.geometry_reliable and physical
        geometric_target = (row.geometry_reliable and row.status == "NAME_UNCERTAIN" and
                            (row.activity_time_reliable or row.boundary_source == "GRAY_DIVIDERS") and
                            row.badge_kind in ("NORMAL_NUMBER", "SMALL_RED") and
                            not name.startswith(("折叠", "新的联系", "消息管理")))
        checks = (
            (row.kind not in ("CONTACT", "GROUP_CANDIDATE"), "未知或未核实的会话入口类型"),
            (duplicate and not duplicate_read or (not row.name_reliable or not name) and not geometric_target,
             "完整名称重复且行不可独立定位" if duplicate else "完整名称未确认"),
            (allow_duplicate and not physical, "未读徽标与联系人行重复分割或归属不唯一"),
            (row.status != "CANDIDATE" and not geometric_target, "联系人行部分可见或状态不可靠"),
            (scope is not None and not scope.muted and row.is_muted is None, "静音状态无法确认"),
            (not geometric_target and row.avatar is None and row.peer_type == "UNKNOWN"
             and not (row.geometry_reliable and row.activity_time_reliable), "缺少头像或身份标签的独立会话证据"),
            (scope is not None and row.kind == "CONTACT" and row.peer_type == "UNKNOWN"
             and not (row.preview or row.activity_time_reliable), "入口缺少普通会话的预览或活动证据"),
            (scope is not None and scope.personal != scope.group and row.kind == "CONTACT"
             and row.peer_type == "UNKNOWN", "采集范围区分单聊和群聊，当前类型无法独立确认"),
            (row.badge is None, "未读标记边界缺失"),
        )
        blockers = tuple(reason for failed, reason in checks if failed)
        skip = (scope or CollectionScope(muted=True)).exclusion(row)
        targets.append(_Target(row, name, avatar_key(observation, row), blockers, skip, duplicate))
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


def _matches_target(observation: VisualObservation, target: _Target, *, avatar_corroborated=False) -> bool:
    rows = [row for row in observation.contacts if name_key(row.display_name) == target.name]
    conv = observation.conversation
    return (len(rows) == 1 and (rows[0].is_selected is True or observation.verified_target == rows[0])
            and rows[0].name_reliable
            and rows[0].status == "CANDIDATE" and conv.match_status == "VISUAL_MATCH"
            and conv.title_name_reliable and conv.selected_name_reliable and name_key(conv.title) == target.name
            and (avatar_corroborated or not target.avatar or avatar_key(observation, rows[0]) == target.avatar)
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


def _core_selection_evidence(before, after, target, refreshed):
    if target.duplicate_name:
        selected = [row for row in after.contacts if row.is_selected is True]
        if (len(selected) != 1 or not refreshed or after.conversation.match_status != "VISUAL_MATCH"
                or name_key(after.conversation.title) != target.name or not after.conversation.title_name_reliable
                or not selected[0].name_reliable or name_key(selected[0].display_name) != target.name
                or not selected[0].geometry_reliable or not selected[0].box.contains(target.row.box.center)
                or selected[0].has_unread_indicator or selected[0].badge is not None):
            return "REPEATED_NAME_ROW_UNVERIFIED", None
        distance = avatar_distance(before, target.row, after, selected[0])
        if distance is not None and distance > 22:
            return "AVATAR_CONFLICT", distance
        return "CLICK_REPEATED_FULL_NAME_ROW", distance
    if _matches_target(after, target):
        row = next(row for row in after.contacts if name_key(row.display_name) == target.name)
        distance = avatar_distance(before, target.row, after, row)
        if distance is not None and distance > 22:
            return "AVATAR_CONFLICT", distance
        if target.avatar:
            return "EXACT_VISUAL_IDENTITY", distance
    if not target.row.name_reliable and target.row.geometry_reliable:
        return _core_geometry_continuity(before, after, target, refreshed)
    rows = [row for row in after.contacts if name_key(row.display_name) == target.name]
    if (len(rows) != 1 or not refreshed or not before.conversation.title_name_reliable
            or name_key(before.conversation.title) == target.name
            or not target.row.has_unread_indicator or target.row.badge is None
            or rows[0].is_selected is not True or rows[0].has_unread_indicator or rows[0].badge is not None):
        return "TRANSITION_EVIDENCE_MISSING", None
    row = rows[0]
    if not _matches_target(after, target, avatar_corroborated=True):
        return "NAME_ROW_OR_PEER_EVIDENCE_CONFLICT", None
    distance = avatar_distance(before, target.row, after, row)
    if distance is None:
        return "CLICK_TITLE_ROW_BADGE_REFRESH_AVATAR_UNAVAILABLE", None
    if distance > 22:
        return "AVATAR_CONFLICT", distance
    return "CLICK_TITLE_ROW_BADGE_REFRESH_AND_AVATAR", distance


def _core_geometry_continuity(before, after, target, refreshed):
    selected = [row for row in after.contacts if row.is_selected is True]
    conv = after.conversation
    title = name_key(conv.title)
    if (len(selected) != 1 or not refreshed or not conv.title_name_reliable or not title
            or not before.conversation.title_name_reliable or title == name_key(before.conversation.title)
            or not target.row.badge or after.layout.get("C2") is None):
        return "GEOMETRY_TRANSITION_UNVERIFIED", None
    row = selected[0]
    previous = target.row
    partial = name_key(previous.display_name).rstrip(".…")
    if (not row.geometry_reliable or row.has_unread_indicator or row.badge
            or row.kind not in ("CONTACT", "GROUP_CANDIDATE")
            or row.name_reliable and title != name_key(row.display_name)
            or any(other is not row and other.name_reliable and title == name_key(other.display_name)
                   for other in after.contacts)
            or partial and not title.startswith(partial)
            or target.row.peer_type != "UNKNOWN" and row.peer_type != "UNKNOWN"
            and target.row.peer_type != row.peer_type
            or conv.title_peer_type != "UNKNOWN" and row.peer_type != "UNKNOWN"
            and conv.title_peer_type != row.peer_type
            or not row.box.contains(previous.box.center)
            or abs(row.box.center[1] - previous.box.center[1]) > previous.box.height * .20):
        return "GEOMETRY_TITLE_OR_ROW_CONFLICT", None
    # A common neighboring named row anchors the list against reordering.
    neighbors = {name_key(r.display_name): r.box.center[1] for r in before.contacts if r.name_reliable
                 and r != previous and abs(r.box.center[1] - previous.box.center[1]) <= previous.box.height * 1.55}
    if not any(r is not row and r.name_reliable and name_key(r.display_name) in neighbors
               and abs(neighbors[name_key(r.display_name)] - r.box.center[1]) <= previous.box.height * .20
               for r in after.contacts):
        return "GEOMETRY_NEIGHBORS_CHANGED", None
    distance = avatar_distance(before, previous, after, row)
    return ("AVATAR_CONFLICT", distance) if distance is not None and distance > 22 else (
        "CLICK_GEOMETRY_TITLE_BADGE_REFRESH", distance)


@dataclass(frozen=True, slots=True)
class IsolatedChoice:
    target: _Target
    lifetime: tuple
    login: LoginEvidence
    scan_id: str
    captured_at: datetime


@dataclass(slots=True)
class IsolatedRun:
    choice: IsolatedChoice
    id: str
    clicked: bool = False
    click_reference: VisualObservation | None = None
    verified: bool = False
    failures: int = 0


def isolated_database_path():
    return runtime_paths().isolated_db


class _ProbeRollback(Exception):
    """Internal rollback marker for the isolated database probe."""


class AccountPaused(NavigationPaused):
    """The login evidence must be rechecked before storing messages."""


class CollectionService:
    def __init__(self, profile: str | None, store: VisualStore, reader: CollectionReader, *,
                 capture=None, navigator=None, permit: CollectionPermit | None = None, window_probe=None,
                 require_email: bool = False, isolated_store=None, core_account=None, durability=None) -> None:
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
        self._isolate_core_reads = capture is None and isinstance(reader, CollectionReader)
        self._core_account = core_account
        self._core_account_status = None
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
        self.runtime = None
        self.test_store = isolated_store or VisualStore(isolated_database_path())
        self._verify_isolated_database()
        if durability and durability.store.path.resolve() != self.test_store.path.resolve():
            raise ValueError("CORE_STORAGE_PATH_CONFLICT")
        paths = runtime_paths() if self._isolate_core_reads else None
        self._durability = durability or (CoreDurability(self.test_store, paths.account, paths.pending_db)
                                           if paths else None)
        self._isolated_run: IsolatedRun | None = None
        self.mode = "test"
        self._prepare_pending = False
        self._identity_skipped = False
        self._identity_retries = 0
        self._opening_polls = 0
        self._navigation_suspended = False
        self._manual_hold = False
        self._target_attempts = {}
        self._detection_finished = True
        self._worker_in_flight = False
        self._last_window_notice = None
        self._window_gate = ""
        self._core: CoreRun | None = None
        self._email_test_only = False

    def _verify_isolated_database(self):
        if isinstance(self.store, VisualStore) and isinstance(self.test_store, VisualStore):
            formal, isolated = self.store.path, self.test_store.path
            if (formal.resolve() == isolated.resolve() or
                    formal.exists() and isolated.exists() and formal.samefile(isolated)):
                raise ValueError("隔离测试库与正式数据库不能使用同一文件")

    def configure_runtime(self, runtime, mode):
        self.runtime = runtime
        self.mode = mode
        if isinstance(self.navigator, CollectionNavigation):
            self.navigator.report = self._navigation_evidence

    def _navigation_evidence(self, stage, code, *, details, result=""):
        # Window substeps have their own evidence without replacing the overall window result.
        if result and self.runtime:
            group = "WINDOW_ACTION" if code.startswith("WINDOW_") else "PROFILE_EVIDENCE"
            self.runtime.emit(self.mode, self._run_id, group, code, result,
                              details={**details, "evidence_scope": "AUTOMATED_REAL_FLOW"})
        else:
            level = "INFO" if code in ("INPUT_EXECUTED", "CURSOR_MOVED") else "DEBUG"
            self._record(stage, code, level=level, details=details)
        if self._core and self._core.task and details.get("action") == "B2":
            if code == "CURSOR_MOVED":
                self._core.task.cursor_moved = True
            elif code == "INPUT_EXECUTED":
                self._core.task.clicked_at = EventTime.now().wall
                self._core.task.applied_point = details.get("screen_point")
        if ((self._window_gate == "foreground" and code == "PROFILE_FOREGROUND" and details.get("allowed"))
                or (self._window_gate == "input" and code == "INPUT_IDLE_SAMPLE"
                    and details.get("idle_ms", 0) >= 1800 and not details.get("held"))):
            self._window_gate = ""
            self._record("WINDOW", "PROFILE_INPUT_GATE_RECOVERED", "AUTO_PASS")

    @property
    def detection_id(self):
        return self._run_id

    def _record(self, stage, code, result="", *, level="INFO", details=None, elapsed_ms=None, exception=None, task_id=None):
        if self.runtime:
            evidence = ("MANUAL_ASSISTED_FLOW" if result == "MANUAL_ASSISTED" else
                        "AUTOMATED_REAL_FLOW" if isinstance(self.navigator, CollectionNavigation) else "SIMULATED_TEST")
            return self.runtime.emit(self.mode, self._run_id, stage, code, result, level=level,
                                     task=self._status.trace_job if task_id is None else task_id,
                                     details={"evidence_scope": evidence, **(details or {})},
                                     elapsed_ms=elapsed_ms,
                                     exception=exception)
        return ""

    def _failure(self, stage, code, reason, *, level="WARNING", result="FAILED", exception=None):
        self._record(stage, code, result, level=level, details={"reason": safe_reason(reason)}, exception=exception)
        self._set(last_failure=reason, failure_stage=stage, failure_code=code, failure_at=EventTime.now().iso(), failure_level=level)

    def start(self, *, scroll: bool = False, core_test: bool = False, failure_crops=False, email_test_only=False) -> None:
        if not self._cancel.is_set() or self._lock.locked() or self._worker_in_flight:
            raise NavigationPaused("自动采集已经启动或上轮仍在清理")
        core = None
        if core_test:
            if self.mode != "test" or email_test_only:
                raise NavigationPaused("核心采集仅在测试模式下独立运行")
            core = CoreRun(failure_crops=failure_crops)
            if self.scope is None:
                self.scope = CollectionScope()
            scroll = False
        self._core, self._email_test_only = core, email_test_only
        if email_test_only:
            self._force_email = True
            self._pending_email = None
        self.permit.grant(scroll=scroll)
        self._run_id = uuid4().hex
        self._detection_finished = False
        self._isolated_run = None
        self._prepare_pending = isinstance(self.navigator, CollectionNavigation) and self.runtime is not None
        self._identity_skipped = self._manual_hold = self._navigation_suspended = False
        self._identity_retries = 0
        self._opening_polls = 0
        self._target_attempts.clear()
        self._last_window_notice = None
        self._window_gate = ""
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
        if core:
            previous = ScanStatus()
        self._status = ScanStatus("RUNNING", "正在准备企业微信窗口",
                                  account_state=previous.account_state, account_name=previous.account_name, account_email=previous.account_email,
                                  account_at=previous.account_at, account_id=previous.account_id,
                                  account_choices=previous.account_choices, account_reason=previous.account_reason,
                                  core=core.view if core else None)
        self._cancel.clear()
        if isinstance(self.navigator, CollectionNavigation):
            self.navigator.mark_started()
        self._record("RUN", "DETECTION_STARTED", details={"scope": repr(self.scope),
                     "flow": "CORE_ISOLATED_INCREMENTAL" if core else "EMAIL_DIAGNOSTIC" if email_test_only else "FORMAL_OR_LEGACY"})
        if core:
            core.clock = TestClock(self._timed_core_event)
            core.clock.begin(1, "检查本机保存环境" if self._durability else "定位、恢复、最大化并激活企业微信")
            if self._isolate_core_reads:
                core.reader = CoreReadWorker(self.read._calibration_path, self.read._thresholds_path)

    def stop(self, *, source="control", final_reason="") -> None:
        was_active = self.active
        self._cancel.set()
        if self._core and self._lock.locked() and isinstance(self.permit, CollectionPermit):
            self.permit.cancel_input()
        else:
            self.permit.revoke()
        self._isolated_run = None
        if self._core:
            run = self._core
            run.invalidated = True
            missing_read = bool(was_active and run.task and run.task.clicked_at)
            if missing_read:
                self._core_event("TARGET_PARTIAL", run.task, "PARTIAL", reason="STOPPED_AFTER_CLICK", read_risk=True)
            self._core_state(phase="TEST_FINISHED" if source in ("automatic", "timeout", "failure") else "STOPPED", waiting=0,
                             risk="BEFORE_UNREAD_NOT_READ" if missing_read else run.view.risk,
                             partial=run.view.partial + missing_read,
                             read_unfinished=run.view.read_unfinished + missing_read)
            if was_active and run.clock:
                code = {"automatic": "TEST_COMPLETED", "timeout": "TEST_TIMEOUT", "failure": "TEST_FAILED"}.get(source, "TEST_STOPPED")
                run.clock.finish(code, final_reason or "用户停止检测", clicks=run.view.clicks,
                                 discovered=run.view.discovered, verified=run.view.verified, messages=len(run.view.messages),
                                 failures=run.view.failures, partial=run.view.partial)
            if not self._lock.locked() and run.reader:
                run.reader.close()
        if self._profile_check and isinstance(self._profile_check.known_windows, ProfileWindows):
            self._profile_check.known_windows.valid = False
        self._set(status="STOPPED", reason=final_reason or "自动采集许可已撤销", waiting_for_window=False,
                  stop_reason=source, assist="", isolated_choices=())
        if was_active:
            self._record("STOP", "STOP_REQUESTED", "AUTO_PASS", details={"source": source})
            self._record("STOP", "STOP_PERMISSION_REVOKED", "AUTO_PASS", details={"source": source})
            self._record("STOP_CONTROL", "STOP_SCHEDULING", "AUTO_PASS", details={"source": source})
        if not self._lock.locked():
            self.finish_run()

    def begin_worker(self):
        self._worker_in_flight = True

    def end_worker(self):
        self._worker_in_flight = False
        self.finish_run()

    def finish_run(self):
        if not self.runtime or self.active or self._lock.locked() or self._worker_in_flight or self._detection_finished:
            return
        self._detection_finished = True
        self._record("STOP", "WORKER_DRAINED", "AUTO_PASS",
                     details={"source": self._status.stop_reason})
        self._record("RUN", "DETECTION_FINISHED", details={"report": self.runtime.report(self._run_id)})


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
        health = frame.diagnostics.window_health
        return frame.capture.pid, frame.capture.hwnd, health.identity if health else None

    def assist_identity(self, action):
        """UI updates this only between workers. Any future action needs a fresh frame."""
        if not self.active or self.mode != "test":
            return
        session = self._profile_check
        if action == "skip":
            self._identity_skipped = True
            self._manual_hold = False
            self._set(assist="", status="RUNNING", reason="已跳过邮箱模块；请关闭资料层后继续独立测试")
            for stage in ("EMAIL", "BIND", "NAVIGATION", "CONVERSATION", "C2", "SAVE", "NOTIFY"):
                self._record(stage, "IDENTITY_DEPENDENCY", "BLOCKED_BY_DEPENDENCY",
                             details={"reason": "本轮跳过邮箱核验"})
        elif action == "retry":
            if (session.retries if session else self._identity_retries) >= 2:
                self._set(reason="本阶段自动重试已达上限，可手动完成或跳过")
                return
            if session:
                session.retries += 1
                session.polls = 0
            else:
                self._identity_retries += 1
            self._failed_profile_lifetime = None
            self._opening_polls = 0
            self._manual_hold = False
            self._set(assist="", reason="将重新截图核对资料层")
        elif action == "manual" and session:
            self._manual_hold = False
            session.polls = 0
            if session.awaiting == "CLOSE":
                session.manual_close = True
                session.stage = "VERIFY_RESTORED"
            else:
                session.manual_card = True
                session.stage = "READING_EMAIL"
                if isinstance(session.known_windows, ProfileWindows) and not session.known_windows.card_click:
                    session.known_windows.card_click = self._record(
                        "CARD", "MANUAL_CARD_REQUEST", details={"profile_session": session.known_windows.id}) or "MANUAL_CARD"

            self._set(assist="", reason="等待企业微信前台及人工步骤的新画面")
        self._record("RUN", "IDENTITY_ASSIST_CHOICE", details={"action": action})

    def _profile_status(self, stage, reason):
        return self._set(status="PAUSED" if self.active else "STOPPED", reason=reason,
                         account_state=stage, account_reason=reason, account_id=None,
                         notice=None, waiting_for_window=False)

    @staticmethod
    def _profile_transient(exc):
        if not isinstance(exc, NavigationPaused):
            return False
        reason = str(exc)
        return ("空闲" in reason or "输入桌面" in reason or "前台" in reason or
                bool(exc.health and exc.health.status in {"WINDOW_MINIMIZED", "WINDOW_NOT_FOREGROUND"}))

    def _profile_tick(self, frame):
        profile_started = perf_counter()
        if not self.active:
            return self._status
        session = self._profile_check
        if session and (self._lifetime(frame) != self._lifetime(session.reference.frame)
                        or isinstance(session.known_windows, ProfileWindows) and not session.known_windows.valid):
            if isinstance(session.known_windows, ProfileWindows):
                session.known_windows.valid = False
            self._profile_check = None
            session = None
        if session is None:
            if self._failed_profile_lifetime == self._lifetime(frame):
                return self._profile_status("IDENTITY_VERIFICATION_FAILED", self._status.account_reason)
            started = perf_counter()
            identify = self.read.identify(frame)
            evidence = identify.login
            self._record("A1", "LOGIN_VISUAL_CHECK", "AUTO_PASS" if evidence.key and evidence.avatar else "FAILED",
                         elapsed_ms=(perf_counter() - started) * 1000,
                         details={"name_seen": bool(evidence.key), "avatar_seen": bool(evidence.avatar),
                                  "avatar_box": (evidence.avatar_box.left, evidence.avatar_box.top,
                                                 evidence.avatar_box.right, evidence.avatar_box.bottom)
                                  if evidence.avatar_box else None})
            self._record("IDENTITY_EVIDENCE", "A1_NAME_OBSERVED", "AUTO_PASS" if evidence.key else "FAILED")
            self._record("IDENTITY_EVIDENCE", "A1_AVATAR_OBSERVED", "AUTO_PASS" if evidence.avatar else "FAILED")
            self._set(account_name=evidence.name, account_at=EventTime.now())
            if (not evidence.key or evidence.avatar_box is None or not evidence.avatar
                    or any(identify.layout.get(part) is None for part in ("A", "B1", "C1", "C2"))):
                reason = "无法可靠定位 A1 登录姓名与头像"
                if self.runtime:
                    self._failed_profile_lifetime = self._lifetime(frame)
                    self._manual_hold = True
                    self._failure("A1", "A1_NOT_CONFIRMED", reason, level="ERROR")
                    self._set(assist="IDENTITY" if self.mode == "test" else "")
                return self._profile_status("IDENTITY_VERIFICATION_FAILED", reason)
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
                if not self.active:
                    return self._status
                if self.runtime and not self._profile_transient(exc):
                    self._opening_polls += 1
                    if self._opening_polls >= 4:
                        self._manual_hold = True
                        self._failure("MENU", "A1_OPEN_RETRIES_EXHAUSTED", str(exc), level="ERROR", exception=exc)
                        self._set(assist="IDENTITY" if self.mode == "test" else "")
                        return self._profile_status("IDENTITY_VERIFICATION_FAILED", str(exc))
                self._failure("MENU", "A1_OPEN_WAITING", str(exc), result="BLOCKED_BY_DEPENDENCY")
                return self._profile_status("IDENTITY_CHECK_REQUIRED", str(exc))
            self._opening_polls = 0
            self._profile_check = ProfileCheck(identify, known)
            event = self._record("IDENTITY_EVIDENCE", "A1_CLICKED", "AUTO_PASS", details={"point": evidence.avatar_box.center,
                                 "profile_session": known.id if isinstance(known, ProfileWindows) else ""})
            if isinstance(known, ProfileWindows):
                known.menu_click = event or "A1_CLICKED"
            return self._profile_status("OPENING_PROFILE_MENU", "已打开头像入口，等待识别个人菜单")

        reference = session.reference
        try:
            current, surfaces, owned = self.navigator.profile_surfaces(reference.frame, session.known_windows)
            if not self.active:
                return self._status
            context = session.known_windows if isinstance(session.known_windows, ProfileWindows) else None
            if session.stage == "VERIFY_RESTORED":
                return self._finish_identity(session, current, owned, surfaces)
            scanned = []
            for surface in surfaces:
                try:
                    scanned.append((surface, self.read.profile_text(surface)))
                except OcrUnavailable as exc:
                    self._record("MENU", "SURFACE_OCR_FAILED", level="WARNING",
                                 details={"hwnd": surface.hwnd, "embedded": surface.embedded}, exception=exc)
            dpi = current.state.dpi
            avatar_box = reference.login.avatar_box
            avatar = avatar_box.crop(reference.frame.pixels) if avatar_box else None
            menus = []
            for surface, spans in scanned:
                evidence = {"hwnd": surface.hwnd, "embedded": surface.embedded, "dpi": dpi}
                action = menu_action(surface, spans, reference.login.name, dpi, avatar=avatar, evidence=evidence)
                self._record("MENU", "MENU_CANDIDATE", level="DEBUG", details=evidence)
                if action:
                    menus.append(action)
            total = len(menus)
            if isinstance(self.navigator, CollectionNavigation):
                menus = unique_menu(menus, lambda action: self.navigator.profile_physical(current, action), dpi)
            self._record("MENU", "MENU_CANDIDATE_COUNTS", level="DEBUG",
                         details={"surfaces": len(surfaces), "before_dedup": total, "after_dedup": len(menus)})
            if session.stage == "OPENING_PROFILE_MENU":
                if len(menus) != 1:
                    return self._wait_profile(session, "未取得唯一的个人菜单文字及头像证据")
                self._record("MENU", "MENU_VERIFIED", "AUTO_PASS", elapsed_ms=(perf_counter() - profile_started) * 1000)
                self._record("IDENTITY_EVIDENCE", "MENU_AVATAR_LOCATED", "AUTO_PASS",
                             details={"point": menus[0].point})
                self._profile_status("OPENING_PROFILE_CARD", "正在复核个人菜单头像")
                if context:
                    self.navigator.confirm_profile_window(reference.frame, menus[0].surface, context, stage="MENU")
                    if menus[0].surface.embedded:
                        session.embedded_layers.append(menus[0].surface.bounds)
                self.navigator.profile_click(reference, menus[0], known_windows=session.known_windows)
                session.stage, session.polls, session.retries = "READING_EMAIL", 0, 0
                event = self._record("IDENTITY_EVIDENCE", "MENU_AVATAR_CLICKED", "AUTO_PASS",
                                     details={"profile_session": context.id if context else "",
                                              "menu_hwnd": menus[0].surface.hwnd})
                if context:
                    context.card_click = event or "MENU_AVATAR_CLICKED"
                return self._profile_status(session.stage, "等待识别详细个人资料卡")
            cards = []
            for surface, spans in scanned:
                facts = {"hwnd": surface.hwnd, "source": surface.source, "class_name": surface.window_class,
                         "dpi": dpi, "shape": surface.pixels.shape, "client": surface.geometry,
                         "profile_session": context.id if context else "", "ocr_span_count": len(spans)}
                if is_profile_card(spans, reference.login.name, surface, evidence=facts):
                    cards.append((surface, spans))
                self._record("CARD", "CARD_CANDIDATE", level="DEBUG", details=facts)
            if context:
                before = len(cards)
                cards = unique_cards(cards, lambda surface: self.navigator.profile_extent(current, surface))
                self._record("CARD", "CARD_CANDIDATE_COUNTS", level="DEBUG",
                             details={"profile_session": context.id, "before": before, "after": len(cards)})
            if session.stage in ("READING_EMAIL", "VERIFYING_EMAIL"):
                baseline = context.baseline if context else session.known_windows
                if not surfaces and set(owned) <= set(baseline):
                    session.failed = "资料层退出，完整邮箱核验未完成，请在高级工具中发起复核"
                    return self._finish_identity(session, current, owned, surfaces)
                if len(cards) != 1:
                    return self._wait_profile(session, "详细资料卡或邮箱标签无法唯一确认")
                surface, spans = cards[0]
                if context:
                    self.navigator.confirm_profile_window(reference.frame, surface, context, stage="CARD")
                    if surface.embedded and surface.bounds not in session.embedded_layers:
                        session.embedded_layers.append(surface.bounds)
                facts = {"hwnd": surface.hwnd, "profile_session": context.id if context else ""}
                value = email_from_spans(surface, spans, evidence=facts)
                self._record("EMAIL", "EMAIL_FIELD_EVIDENCE", level="DEBUG", details=facts)
                key = (surface.hwnd, surface.geometry, surface.bounds)
                if session.stage == "READING_EMAIL":
                    self._record("CARD", "PROFILE_CARD_VERIFIED", "MANUAL_ASSISTED" if session.manual_card else "AUTO_PASS",
                                 elapsed_ms=(perf_counter() - profile_started) * 1000)
                    self._record("IDENTITY_EVIDENCE", "FIRST_EMAIL_READ", "AUTO_PASS" if value else "FAILED", details={"valid": bool(value)})
                    session.email, session.email_surface, session.email_at = value, key, current.captured_at
                    session.stage, session.polls = "VERIFYING_EMAIL", 0
                    return self._profile_status(session.stage, "等待第二次独立截图核对邮箱")
                if current.captured_at <= session.email_at:
                    return self._wait_profile(session, "尚未取得独立资料截图")
                self._record("IDENTITY_EVIDENCE", "SECOND_EMAIL_READ", "AUTO_PASS" if value else "FAILED", details={"valid": bool(value)})
                if not value or value != session.email or key != session.email_surface:
                    reason = "两次资料截图的邮箱无效、不一致或资料窗口发生变化"
                    if context and session.email_retries < 2:
                        session.email_retries += 1
                        session.email = ""
                        session.stage = "READING_EMAIL"
                        self._record("EMAIL", "EMAIL_PAIR_RETRY", "FAILED",
                                     details={"reason": reason, "retry": session.email_retries})
                        return self._profile_status(session.stage, reason + "，重新读取两张截图")
                    session.failed = reason
                    session.email = ""
                self._record("EMAIL", "EMAIL_COMPARE", "FAILED" if session.failed else "AUTO_PASS",
                             elapsed_ms=(perf_counter() - profile_started) * 1000,
                             details={"reason": session.failed, "consistent": bool(session.email)})
                action = card_close(surface, dpi)
                if action is None:
                    reason = "未识别到可靠的资料卡关闭控件，请手动关闭资料卡及个人菜单"
                    if not self.runtime:
                        session.failed = reason
                        return self._profile_status("CLOSING_PROFILE", reason)
                    session.awaiting = "CLOSE"
                    self._manual_hold = True
                    self._failure("RESTORE", "CLOSE_CONTROL_UNCONFIRMED", reason, level="ERROR")
                    self._set(assist="CLOSE" if self.mode == "test" else "")
                    return self._profile_status("IDENTITY_VERIFICATION_FAILED", reason)
                self._profile_status("CLOSING_PROFILE", "邮箱核对完成，正在复核本次打开的资料卡关闭位置")
                self.navigator.profile_click(reference, action, known_windows=session.known_windows)
                session.close_issued = True
                self._record("IDENTITY_EVIDENCE", "CARD_CLOSE_CLICKED", "AUTO_PASS")
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
                self._record("IDENTITY_EVIDENCE", "MENU_CLOSE_CLICKED", "AUTO_PASS")
                return self._profile_status("CLOSING_PROFILE", "验证个人菜单已关闭")
            return self._finish_identity(session, current, owned, surfaces)
        except (NavigationPaused, OcrUnavailable, OSError) as exc:
            if not self.active:
                return self._status
            if self._profile_transient(exc):
                self._window_gate = "input" if "空闲" in str(exc) else "foreground"
                self._failure("WINDOW", "PROFILE_WAITING_FOR_WINDOW_OR_INPUT", str(exc),
                              result="BLOCKED_BY_DEPENDENCY")
                return self._profile_status(session.stage, str(exc))
            return self._wait_profile(session, str(exc) if isinstance(exc, NavigationPaused) else
                                      f"账号核验读取失败（{type(exc).__name__}）")

    def _wait_profile(self, session, reason):
        session.polls += 1
        state = session.stage if session.polls < 4 else "IDENTITY_VERIFICATION_FAILED"
        stage = ("MENU" if session.stage == "OPENING_PROFILE_MENU" else
                 "CARD" if session.stage == "READING_EMAIL" else
                 "EMAIL" if session.stage == "VERIFYING_EMAIL" else "RESTORE")
        if self.runtime:
            self._record(stage, "PROFILE_POLL", level="DEBUG", details={"attempt": session.polls, "reason": safe_reason(reason)})
            if session.polls >= 4:
                session.awaiting = "CLOSE" if stage == "RESTORE" else "CARD"
                self._manual_hold = True
                self._failure(stage, "PROFILE_RETRIES_EXHAUSTED", reason, level="ERROR")
                self._set(assist=session.awaiting if self.mode == "test" else "")
        return self._profile_status(state, reason)

    def _restored_message_view(self, session, frame, owned, surfaces):
        original = session.reference
        if isinstance(session.known_windows, ProfileWindows):
            context = session.known_windows
            if not context.valid or owned or self._lifetime(frame) != self._lifetime(original.frame):
                return None
            # Previously observed embedded popup extents need fresh evidence of
            # disappearance. Large new changes over the rail/header are unsafe.
            for surface in surfaces:
                if not surface.embedded:
                    return None
                for layer in session.embedded_layers:
                    box = surface.bounds
                    overlap = max(0, min(box.right, layer.right)-max(box.left, layer.left)) * max(
                        0, min(box.bottom, layer.bottom)-max(box.top, layer.top))
                    if overlap >= .45 * layer.width * layer.height:
                        return None
                if any((box := original.layout.get(part)) and surface.bounds.contains(box.center)
                       for part in ("A", "B1", "C1")):
                    return None
            raw = self.read(frame)
            if (raw.page != "MESSAGES_CANDIDATE" or
                    any(raw.layout.get(part) is None for part in ("B2", "C1", "C2")) or
                    not raw.conversation.title_name_reliable or not self._same_login(original.login, raw.login)):
                return None
            try:
                if isinstance(self.navigator, CollectionNavigation):
                    self.navigator.verify_normal_message(raw)
                else:
                    self.navigator.verify_view(raw)
            except NavigationPaused:
                return None
            return raw
        # Legacy synthetic callers supply bare HWND tuples.
        b1, c1 = (original.layout.get(name) for name in ("B1", "C1"))
        if (any(hwnd not in session.known_windows for hwnd in owned) or surfaces
                or not original.login.unchanged(frame.pixels) or b1 is None or c1 is None
                or not all(np.array_equal(box.crop(original.frame.pixels), box.crop(frame.pixels)) for box in (b1, c1))):
            return None
        return original

    def _finish_identity(self, session, frame, owned, surfaces):
        restored = self._restored_message_view(session, frame, owned, surfaces)
        if restored is None:
            return self._wait_profile(session, "资料层仍存在，或账号与 B2、C1、C2 未恢复")
        login = restored.login
        if isinstance(session.known_windows, ProfileWindows):
            session.known_windows.valid = False
        self._profile_check = None
        self._record("IDENTITY_EVIDENCE", "PROFILE_LAYERS_GONE", "MANUAL_ASSISTED" if session.manual_close else "AUTO_PASS")
        self._record("RESTORE", "CHAT_VIEW_RESTORED", "MANUAL_ASSISTED" if session.manual_close else "AUTO_PASS")
        if session.failed or not session.email or not (session.close_issued or session.manual_close):
            self._force_email = True
            self._failed_profile_lifetime = self._lifetime(frame)
            reason = session.failed or "未取得有效邮箱"
            if self.runtime:
                self._manual_hold = True
                self._failure("EMAIL", "EMAIL_NOT_VERIFIED", reason, level="ERROR")
                self._set(assist="IDENTITY" if self.mode == "test" else "")
            return self._profile_status("IDENTITY_VERIFICATION_FAILED", reason)
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

    def _accept_login(self, account, evidence, lifetime, *, email="", assisted=False):
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
        self._record("BIND", "ACCOUNT_BOUND", "MANUAL_ASSISTED" if assisted else "AUTO_PASS" if email else "PARTIAL",
                     details={"account_id": account})
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
                self._accept_login(account, evidence, lifetime, email=email, assisted=True)
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
        return (health.status, health.reason, health.identity, health.geometry_key, health.foreground,
                sample.exists if sample else None, sample.iconic if sample else None,
                sample.show_command if sample else None, sample.owner if sample else None)

    def _record_window(self, health: WindowHealth | None) -> None:
        if health is None:
            return
        sample, window = health.last, health.window
        if (self._status.failure_code in {"WINDOW_PREPARATION_FAILED", "WINDOW_PREPARATION_BLOCKED"}
                and health.foreground and health.capture_blocker is None and self._window_gate != "prepared"):
            self._window_gate = "prepared"
            self._record("WINDOW", "WINDOW_PREPARATION_RECOVERED", "AUTO_PASS")
        self._record("WINDOW", "WINDOW_PROBE", level="DEBUG", elapsed_ms=health.elapsed_ms,
                     details={"status": health.status, "pid": window.pid if window else None,
                              "hwnd": window.hwnd if window else None, "class_name": sample.class_name if sample else None,
                              "rect": sample.rect if sample else None, "dpi": sample.dpi if sample else None,
                              "client": sample.client_size if sample else None, "iconic": sample.iconic if sample else None,
                              "zoomed": sample.zoomed if sample else None, "foreground": health.foreground})
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
        if self._core and self._core.lifetime and reason.startswith("WINDOW_CLOSED"):
            return self._invalidate_core("BOUND_WINDOW_CLOSED")
        if self.runtime and self._status.last_failure != reason:
            if self._status.failure_stage != "WINDOW" or self._status.failure_level != "ERROR":
                self._failure("WINDOW", "WINDOW_WAITING", reason, result="BLOCKED_BY_DEPENDENCY")
        if invalidate and not self._core:
            self._invalidate_window_plan("窗口或坐标系失去连续性，旧未执行任务保留供核对")
        if self._core:
            self._core_state(waiting=0)
        return self._set(status="PAUSED", reason=reason, waiting_for_window=True,
                         last_failure=self._status.last_failure if self.runtime else reason,
                         target="", queued=len(self._queued), match="UNKNOWN",
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
        mapping = frame.coordinates
        if (state.minimized or (not state.maximized and not mapping) or state.dpi < 1
                or pixels.dtype != np.uint8 or pixels.ndim != 3 or pixels.shape[2] != 3
                or pixels.shape[:2] != (capture.height, capture.width)
                or (not mapping and (capture.width, capture.height) != (state.width, state.height))
                or capture.rect is None or capture.pid < 1 or capture.hwnd < 1):
            raise NavigationPaused("截图尺寸、窗口状态或坐标证据无效")
        if mapping and ((mapping.pid, mapping.hwnd, mapping.class_name, mapping.dpi) !=
                        (capture.pid, capture.hwnd, capture.class_name, state.dpi)
                        or mapping.client_size != (state.width, state.height)
                        or mapping.image_size != (capture.width, capture.height)
                        or not health or not health.last or not health.last.desktop
                        or health.last.desktop.identity != mapping.desktop.identity):
            raise NavigationPaused("COORDINATE_MAPPING_UNVERIFIED · 截图与几何快照不一致")
        if health and health.last and health.last.desktop and not mapping:
            raise NavigationPaused("COORDINATE_MAPPING_UNVERIFIED · 物理映射缺失")
        if health is not None and (
                health.status not in ("WINDOW_VALID", "WINDOW_NOT_FOREGROUND")
                or health.capture_blocker is not None
                or health.window is None or (health.window.pid, health.window.hwnd) != (capture.pid, capture.hwnd)
                or health.geometry != (capture.rect, (state.width, state.height), state.dpi)):
            raise NavigationPaused("截图窗口状态、PID、HWND 或坐标核对失败", health=health)
        if expected is not None and _identity(frame) != _identity(expected):
            raise NavigationPaused("预检与 OCR 截图窗口身份或坐标系不一致", health=health)

    def _record_b2_result(self, raw, attempted_at, *, blocked_reason=""):
        observed = unread_targets(raw, self.scope, allow_duplicate=self._core is not None)
        skipped = tuple(target for target in observed if target.skip_reason)
        targets = tuple(target for target in observed if not target.skip_reason)
        unowned = tuple(item for item in raw.b2_badges if not any(
            target.row.badge and target.row.badge.contains(item.box.center) for target in observed))
        marker_count = len(observed) + len(unowned)
        unreliable = tuple(target for target in targets if target.ambiguous)
        reliable_count = len(targets) - len(unreliable)
        categories = Counter(item.uncertainty for item in unreliable)
        policy_count = sum(item.scope_decision == "SKIPPED_BY_POLICY" for item in skipped)
        unsupported = len(skipped) - policy_count
        self._record_contact_decisions(raw, observed, blocked_reason)
        for item in unowned:
            self._record("SCOPE", "B2_BADGE_UNASSIGNED", details={"box": bounds(item.box), "kind": item.kind})
        reasons = tuple(dict.fromkeys(reason for target in unreliable for reason in target.blockers))
        completed = EventTime.now().wall
        conversation = raw.conversation
        self._set(b2_scan_attempt_at=attempted_at.wall, b2_scan_completed_at=completed, last_scan=completed,
                  contact_count=len(raw.contacts), unread_marker_count=marker_count,
                  unowned_unread_count=len(unowned),
                  muted_excluded_count=sum(item.row.is_muted is True and not self.scope.muted
                                           for item in skipped if self.scope),
                  folded_excluded_count=sum(item.row.kind == "FOLDED_CHAT_CONTAINER" for item in skipped),
                  reliable_unread_count=reliable_count, unreliable_unread_count=len(unreliable),
                  unreliable_reasons=reasons, candidates=reliable_count, navigation_block_reason="",
                  skipped_unread=len(skipped), policy_skipped_unread=policy_count, unsupported_unread=unsupported,
                  unknown_type_unread=categories["type"], unknown_muted_unread=categories["muted"],
                  other_unreliable_unread=categories["other"],
                  navigation_blocked_unread=reliable_count if blocked_reason else 0,
                  skipped_reasons=tuple(dict.fromkeys(t.skip_reason for t in skipped)),
                  scan_disposition="B2_SCANNED_UNREAD" if reliable_count else
                  "B2_SCANNED_UNRELIABLE" if unreliable or unowned else
                  "SKIPPED_BY_POLICY" if skipped else "B2_SCANNED_NO_UNREAD",
                  awaiting_navigation=len(self._queued), selection_method=conversation.selection_method,
                  selection_name=conversation.selected_name, selection_name_reliable=conversation.selected_name_reliable,
                  selection_confidence=conversation.selection_confidence,
                  selection_evidence=conversation.selection_evidence,
                  last_b2_summary=f"{self._status.scan_id} · {completed.isoformat(timespec='milliseconds')} · "
                  f"联系人 {len(raw.contacts)} · 未读 {marker_count} · 可靠 {reliable_count} · 不可靠 {len(unreliable)}")
        return targets

    def _record_contact_decisions(self, raw, observed, blocked_reason):
        if not self.runtime:
            return
        target_by_row = {id(target.row): target for target in observed}
        for row in raw.contacts:
            target = target_by_row.get(id(row))
            decision = target.scope_decision if target else "NO_UNREAD"
            core = self._core
            if (decision == "ELIGIBLE" and target and core and target.key in core.handled
                    and target.key not in core.cleared and not _new_unread(core.signatures[target.key], target.unread_signature)):
                decision = "UNREAD_ALREADY_HANDLED"
            elif decision == "ELIGIBLE" and blocked_reason:
                decision = "NAVIGATION_BLOCKED"
            identity = target.key if target else f"{name_key(row.display_name)}|{avatar_key(raw, row)}|{row.peer_type}|{'|'.join(map(name_key, row.identity_labels))}"
            alias = blake2s((self._run_id + identity).encode(), digest_size=8).hexdigest()
            evidence = ("RECOGNIZED_SPECIAL_LABEL" if decision == "UNSUPPORTED_ENTRY" else
                        "GROUP_NAME_OR_COUNT_MARKER" if row.kind == "GROUP_CANDIDATE" else
                        "PARSED_PEER_LABEL" if row.peer_type != "UNKNOWN" else
                        "VISIBLE_NAME_AND_ROW" if row.kind == "CONTACT" and row.name_reliable else "UNCONFIRMED")
            mute = "UNKNOWN" if row.is_muted is None else "TRUE" if row.is_muted else "FALSE"
            reason = (target.skip_reason or "；".join(target.blockers) or blocked_reason) if target else ""
            if decision == "UNREAD_ALREADY_HANDLED":
                reason = "本轮已处理相同的未读证据，等待新的未读标记或变化"
            count = row.unread_count if row.unread_count_reliable else None
            self._record("SCOPE", "CONTACT_SCOPE_OBSERVED", details={
                "observation_id": f"{self._status.scan_id}:{row.sequence}", "target_id": alias,
                "row_index": row.sequence, "row_box": (row.box.left, row.box.top, row.box.right, row.box.bottom),
                "conversation_kind": row.kind, "peer_type": row.peer_type, "kind_evidence": evidence,
                "muted_state": mute, "muted_evidence": row.mute_evidence or {"TRUE": "DIAGONAL_ICON_STROKE",
                    "FALSE": "NO_MARK_IN_ICON_REGION", "UNKNOWN": "INSUFFICIENT_ICON_EVIDENCE"}[mute],
                "muted_score": round(row.mute_score, 3) if row.mute_score is not None else None,
                "unread_present": row.has_unread_indicator,
                "unread_kind": ("DIGIT" if count is not None else "DOT_OR_UNREADABLE_DIGIT")
                if row.has_unread_indicator else "NONE",
                "unread_count": count, "name_reliable": row.name_reliable,
                "badge_box": bounds(row.badge), "badge_style": row.badge_kind,
                "position_reliable": row.status == "CANDIDATE" or row.geometry_reliable, "selected_state": row.is_selected,
                "time_anchor_reliable": row.activity_time_reliable, "avatar_detected": bool(row.avatar),
                "boundary_source": row.boundary_source, "estimated_pitch": row.row_pitch,
                "scope_decision": decision, "exclusion_reason": safe_reason(reason),
                "navigation_eligible": decision == "ELIGIBLE",
            })

    def _bind_frame(self, frame) -> None:
        key = _identity(frame)
        old = self._window
        if old == key and not self._window_plan_invalidated:
            return
        if old is not None and old != key:
            if self._isolated_run and (old[0], old[1], old[-1]) != (key[0], key[1], key[-1]):
                self._record("TEST_NAVIGATION", "TEST_AUTHORIZATION_INVALIDATED", "FAILED",
                             details={"reason": "窗口生命周期变化", "evidence_scope": "ISOLATED_NAVIGATION_TEST"})
                self._isolated_run = None
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
        self._record("SAVE", "SQLITE_COMMIT", "AUTO_PASS" if result.conversation_id and not result.uncertain else "PARTIAL",
                     elapsed_ms=trace.durations().get("ocr_to_commit_ms"),
                     details={"saved": result.inserted, "new": result.new_candidates,
                              "repeated": result.repeated, "uncertain": result.uncertain, "outcome": result.outcome})
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

    def identify_account(self, *, visual_only=False) -> ScanStatus:
        if not self._auto_account:
            return self._status
        self._set(status="CAPTURING", reason="读取 A1 登录账号", account_state="IDENTIFYING",
                  account_choices=(), notice=None)
        identify = self.read.identify if isinstance(self.read, CollectionReader) else self.read
        try:
            if visual_only:
                frame = self.capture()
                self._validate_read_frame(frame)
                self._record_window(frame.diagnostics.window_health)
                evidence = identify(frame).login
                return self._set(status="READY" if evidence.name else "PAUSED", account_name=evidence.name,
                                 account_state="TEST_A1_PREVIEW" if evidence.name else "TEST_A1_UNVERIFIED",
                                 account_id=None, account_email="", account_at=EventTime.now(),
                                 account_reason="A1 预览 · 启动核心测试后会重新验证" if evidence.name else evidence.reason,
                                 reason="等待授权测试范围")
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
        except (CaptureError, NavigationPaused) as exc:
            if visual_only:
                return self._set(status="PAUSED", reason=str(exc), account_state="TEST_A1_UNVERIFIED",
                                 account_reason="尚未取得有效 A1 截图")
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
            if self.runtime and not summarize(raw).messages_reliable:
                reason = "会话归属或 C2 完整性未核对，未写入聊天记录"
                self._failure("SAVE", "MANUAL_SAVE_BLOCKED", reason, result="BLOCKED_BY_DEPENDENCY")
                return self._set(status="PAUSED", reason=reason)
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

    def _prepare_start(self):
        started = perf_counter()
        try:
            kwargs = {"deadline_check": self._core_check} if self._core else {}
            health = self.navigator.prepare_window(self._cancel, **kwargs)
            self._record_window(health)
            if self._core and health.identity and health.window:
                self._core.lifetime = health.window.pid, health.window.hwnd, health.identity
            if health.capture_blocker or not health.foreground:
                reason = f"{health.status} · {health.reason}"
                self._failure("WINDOW", "WINDOW_PREPARATION_BLOCKED", reason,
                              level="ERROR" if health.status != "WINDOW_CLOSED" else "WARNING")
                return self._wait_for_window(reason)
        except CancelledError:
            return self._status
        except (OSError, WindowResolutionError, NavigationPaused) as exc:
            if not self.active:
                return self._status
            self._failure("WINDOW", "WINDOW_PREPARATION_FAILED", str(exc), level="ERROR", exception=exc)
            return self._wait_for_window(str(exc))
        self._record("WINDOW", "WINDOW_PREPARED", "AUTO_PASS", elapsed_ms=(perf_counter() - started) * 1000)
        return None

    def _readonly_test(self, frame, *, skipped):
        started = perf_counter()
        raw = self.read.read_b2(frame) if isinstance(self.read, CollectionReader) else self.read(frame)
        if not self.active:
            return self._status
        b2 = raw.layout.get("B2")
        failed = any(step.status in ("FAILED", "SKIPPED") for step in raw.ocr_areas if step.region.startswith("B2"))
        if not b2 or raw.page != "MESSAGES_CANDIDATE" or failed:
            self._record("B2", "READONLY_B2_UNCONFIRMED", "PARTIAL",
                         details={"reason": "画面或 B2 未核对", "evidence_scope": "ISOLATED_READONLY_TEST"})
        else:
            self._record_b2_result(raw, EventTime.now(), blocked_reason="正式身份或导航尚未验证")
            self._record("B2", "ISOLATED_TEST_OBSERVATION" if self.mode == "test" else "READONLY_OBSERVATION",
                         "AUTO_PASS", elapsed_ms=(perf_counter() - started) * 1000,
                         details={"contacts": len(raw.contacts), "unread": self._status.unread_marker_count,
                                  "reliable": self._status.reliable_unread_count, "unreliable": self._status.unreliable_unread_count,
                                  "excluded": self._status.skipped_unread, "scan": self._status.scan_id,
                                  "evidence_scope": "ISOLATED_READONLY_TEST" if self.mode == "test" else "READONLY"})
            self._record("SCOPE", "READONLY_SCOPE_APPLIED", "AUTO_PASS",
                         details={"excluded": self._status.skipped_unread, "evidence_scope": "ISOLATED_READONLY_TEST",
                                  "classification_human_confirmed": False})
        if self.mode != "test":
            return self._status
        if skipped:
            self._set(status="RUNNING", reason="仅执行独立测试，正式账号写入保持阻断")
        try:
            visible = self._isolated_view(frame, discard_closed_session=skipped)
        except (NavigationPaused, CaptureError, OcrUnavailable) as exc:
            self._record("TEST_C2", "TEST_MESSAGE_PAGE_WAIT", "BLOCKED_BY_DEPENDENCY",
                         details={"reason": safe_reason(exc), "evidence_scope": "ISOLATED_READONLY_TEST"})
            return self._set(reason=str(exc), isolated_choices=()) if skipped else self._status
        if visible is None or not self.active:
            return self._status
        self._record_independent_visual(visible)
        options = self._isolated_targets(visible) if self._identity_skipped and not self._navigation_suspended else ()
        self._set(isolated_choices=options)
        if self._isolated_run:
            return self._run_isolated_target(visible)
        return self._status

    def _isolated_view(self, frame, *, discard_closed_session=True):
        session = self._profile_check
        if session and isinstance(session.known_windows, ProfileWindows):
            if self._lifetime(frame) != self._lifetime(session.reference.frame):
                session.known_windows.valid = False
                self._profile_check = None
            else:
                current, surfaces, owned = self.navigator.profile_surfaces(session.reference.frame, session.known_windows)
                restored = self._restored_message_view(session, current, owned, surfaces)
                if restored is None:
                    raise NavigationPaused("请手动恢复正常消息页面后继续独立测试")
                if discard_closed_session:
                    session.known_windows.valid = False
                    self._profile_check = None
                return restored
        raw = self.read(frame)
        if (raw.page != "MESSAGES_CANDIDATE" or not raw.conversation.title_name_reliable or
                any(raw.layout.get(part) is None for part in ("B2", "C1", "C2"))):
            raise NavigationPaused("独立 C1/C2 测试等待可确认的消息页面")
        if isinstance(self.navigator, CollectionNavigation):
            self.navigator.verify_normal_message(raw)
        return raw

    def _record_independent_visual(self, raw):
        observation = summarize(raw)
        self._record("TEST_C1", "TEST_C1_OBSERVED", "AUTO_PASS" if raw.conversation.title_name_reliable else "PARTIAL",
                     details={"evidence_scope": "ISOLATED_READONLY_TEST", "match": raw.conversation.match_status,
                              "type": raw.conversation.conversation_type, "title_reliable": raw.conversation.title_name_reliable})
        self._record("TEST_C2", "TEST_C2_OBSERVED", "AUTO_PASS" if observation.messages_reliable else "PARTIAL",
                     elapsed_ms=raw.recognition_ms, details={"evidence_scope": "ISOLATED_READONLY_TEST",
                     "messages": len(raw.messages), "directions": dict(Counter(item.direction for item in raw.messages)),
                     "types": dict(Counter(item.kind for item in raw.messages)),
                     "timestamps": sum(bool(item.time_evidence) for item in raw.messages),
                     "confidence": [round(span.score, 3) for item in raw.messages for span in item.ocr_spans][:120],
                     "ocr_span_count": sum(len(area.spans) for area in raw.ocr_areas if area.region.startswith("C2")
                                           and hasattr(area, "spans")),
                     "completeness": raw.message_completeness, "status": raw.ocr_status})

    def _isolated_targets(self, raw):
        own = raw.login
        if not own.key or not own.avatar or not own.signature or self._lifetime(raw.frame)[-1] is None:
            return ()
        eligible = [target for target in unread_targets(raw, self.scope) if target.scope_decision == "ELIGIBLE"]
        for row in raw.contacts:
            if row.is_selected is True and not row.has_unread_indicator and not (
                    self.scope and self.scope.exclusion(row)):
                target = _Target(row, name_key(row.display_name), avatar_key(raw, row), ())
                if row.name_reliable and row.status == "CANDIDATE" and _matches_target(raw, target):
                    eligible.append(target)
        return tuple(IsolatedChoice(target, self._lifetime(raw.frame), own, self._status.scan_id, raw.frame.captured_at)
                     for target in eligible if target.avatar)

    def authorize_isolated(self, option):
        if (not self.active or self.mode != "test" or not self._identity_skipped or self._navigation_suspended
                or not any(item is option for item in self._status.isolated_choices)
                or (datetime.now(option.captured_at.tzinfo) - option.captured_at).total_seconds() > 90):
            return self._set(reason="隔离测试授权未生效，请等待新一轮可见会话并重新选择")
        self._isolated_run = IsolatedRun(option, uuid4().hex)
        self._record("TEST_NAVIGATION", "TEST_TARGET_AUTHORIZED", "MANUAL_ASSISTED",
                     details={"test_run": self._isolated_run.id, "scan": option.scan_id,
                              "evidence_scope": "ISOLATED_NAVIGATION_TEST",
                              "target_id": blake2s((self._run_id + option.target.key).encode(), digest_size=8).hexdigest()})
        return self._set(reason="已授权指定测试会话，等待新一轮窗口与目标复核")

    def test_database_probe(self):
        """Exercise the same transaction manager without reading any conversation."""
        if not self.active or self.mode != "test":
            return self._status
        marker = uuid4().hex
        detail = {"test_run": marker, "evidence_scope": "ISOLATED_DATABASE_TEST", "synthetic_record": True}
        try:
            self._verify_isolated_database()
            with self.test_store.transaction() as db:
                db.execute("CREATE TABLE IF NOT EXISTS synthetic_transaction_probe (id TEXT PRIMARY KEY)")
                db.execute("INSERT INTO synthetic_transaction_probe VALUES (?)", (marker,))
                duplicate = db.execute("INSERT OR IGNORE INTO synthetic_transaction_probe VALUES (?)", (marker,)).rowcount
            try:
                with self.test_store.transaction() as db:
                    db.execute("DELETE FROM synthetic_transaction_probe WHERE id = ?", (marker,))
                    raise _ProbeRollback()
            except _ProbeRollback:
                pass
            with self.test_store.transaction() as db:
                count = db.execute("SELECT count(*) FROM synthetic_transaction_probe WHERE id = ?", (marker,)).fetchone()[0]
            if count != 1 or duplicate:
                raise ValueError("事务或唯一约束验证失败")
        except (sqlite3.Error, OSError, ValueError) as exc:
            self._record("TEST_SAVE", "TEST_DATABASE_PROBE_FAILED", "FAILED", exception=exc, details=detail)
            return self._set(reason="隔离库事务自检失败")
        self._record("TEST_SAVE", "TEST_DATABASE_PROBE_PASSED", "AUTO_PASS",
                     details={**detail, "test": "COMMIT_UNIQUE_ROLLBACK"})
        return self._set(reason="隔离库合成事务自检通过")

    def _run_isolated_target(self, raw):
        run = self._isolated_run
        if run is None or not self.active:
            return self._status
        option = run.choice
        detail = {"test_run": run.id, "evidence_scope": "ISOLATED_NAVIGATION_TEST"}

        def invalidate(code, reason):
            self._isolated_run = None
            self._record("TEST_NAVIGATION", code, "FAILED", details={**detail, "reason": safe_reason(reason)})
            return self._set(reason=reason)

        if self._lifetime(raw.frame) != option.lifetime or not self._same_login(raw.login, option.login):
            return invalidate("TEST_AUTHORIZATION_INVALIDATED", "A1 或窗口身份已变化，隔离授权失效")
        target = option.target
        try:
            if run.click_reference is not None and not run.verified:
                raw = _verify_transition(run.click_reference, raw, target)
            if not _matches_target(raw, target):
                if run.clicked or run.verified:
                    return invalidate("TEST_TARGET_NOT_CURRENT", "已离开或未能核验授权测试会话")
                options = [item.target for item in self._status.isolated_choices if item.target.key == target.key]
                if len(options) != 1:
                    return invalidate("TEST_TARGET_NOT_REVALIDATED", "授权测试目标已不可见或不再可靠")
                target = options[0]
                self.navigator.verify_view(raw, need_idle=True)
                self.permit.ensure_current()
                if not self.active:
                    return self._status
                self.navigator.select(raw, target.row)
                run.clicked = True
                run.click_reference = raw
                self._record("TEST_NAVIGATION", "TEST_CONTACT_CLICKED", "AUTO_PASS", details=detail)
                return self._set(reason="已点击获授权测试会话，等待新截图确认")
            self.navigator.verify_view(raw)
            fresh = self.capture()
            if (not self.active or _identity(fresh) != _identity(raw.frame)
                    or not raw.login.unchanged(fresh.pixels)):
                return invalidate("TEST_SAVE_RECHECK", "隔离保存前 A1、窗口或许可复核失败")
            regions = summarize(raw).regions
            if regions is None or probe_frame(fresh, regions).differs(probe_frame(raw.frame, regions)):
                raise NavigationPaused("隔离测试会话当前仍在变化")
            run.verified = True
            run.click_reference = None
            run.failures = 0
            if not self.active:
                return self._status
            self._record("TEST_CONVERSATION", "TEST_CONVERSATION_VERIFIED", "AUTO_PASS", details=detail)
            test_profile = "TEST " + run.id
            reliable = summarize(raw).messages_reliable
            self._verify_isolated_database()
            result = self.test_store.save(test_profile, raw, allow_link=reliable)
            self._record("TEST_SAVE", "TEST_SQLITE_COMMIT", "AUTO_PASS" if reliable else "PARTIAL",
                         details={**detail, "evidence_scope": "ISOLATED_DATABASE_TEST", "saved": result.inserted,
                                  "new": result.new_candidates, "repeated": result.repeated,
                                  "uncertain": result.uncertain, "outcome": result.outcome})
            if result.new_candidates and result.committed_at is not None and self.active:
                notice = SaveNotice(result.capture_id, "隔离测试会话", result.new_candidates,
                                    CollectionTrace(sqlite_committed_at=result.committed_at), test_run=run.id)
                self._record("TEST_NOTIFY", "TEST_NOTIFICATION_GENERATED", details=detail)
                return self._set(reason="隔离测试数据库已提交新增消息", notice=notice)
            return self._set(reason=f"隔离测试已记录 · {result.outcome} · 保存 {result.inserted} · 重复 {result.repeated}")
        except (NavigationPaused, CaptureError) as exc:
            run.failures += 1
            if run.failures >= 3:
                return invalidate("TEST_NAVIGATION_RETRY_LIMIT", str(exc))
            self._record("TEST_NAVIGATION", "TEST_NAVIGATION_WAIT", "BLOCKED_BY_DEPENDENCY",
                         details={**detail, "reason": safe_reason(exc)})
            return self._set(reason=str(exc))
        except (sqlite3.Error, OSError, ValueError) as exc:
            self._isolated_run = None
            self._record("TEST_SAVE", "TEST_SQLITE_FAILED", "FAILED", exception=exc,
                         details={**detail, "evidence_scope": "ISOLATED_DATABASE_TEST", "reason": type(exc).__name__})
            return self._set(reason="隔离测试数据库操作失败，正式账号数据未参与")

    def _core_state(self, **changes):
        run = self._core
        run.view = replace(run.view, **changes)
        return self._set(core=run.view)

    def clear_core_results(self):
        if self._core:
            self._core.clear_results()
            self._set(core=self._core.view)
        return self._status

    def confirm_core_displayed(self, messages):
        run = self._core
        if not run:
            return
        for task_id in dict.fromkeys(item.task for item in messages):
            if task_id in run.displayed:
                continue
            run.displayed.add(task_id)
            self._record("CORE_EVIDENCE", "RESULT_DISPLAYED", "AUTO_PASS", task_id=task_id,
                         details={"core_task": task_id, "messages": sum(item.task == task_id for item in messages)})
        if run.presentation and any(item.task == run.presentation for item in messages) and run.clock.phase:
            try:
                run.clock.check()
            except TestDeadline:
                return
            run.clock.complete(displayed=True)
            run.presentation = ""
            run.clock.end_target()

    def _core_event(self, code, task=None, result="", **details):
        if task:
            details["core_task"] = task.id
            details["scan"] = task.scan_id
            details["target_id"] = blake2s((self._run_id + task.target.key).encode(), digest_size=8).hexdigest()
        stage = ("CORE" if code in ("TARGET_COMPLETED", "TARGET_PARTIAL", "TARGET_FAILED",
                                    "CORE_WINDOW_IDENTITY_CHANGED") else "CORE_EVIDENCE")
        return self._record(stage, code, result, details=details, task_id=task.id if task else "")

    def _keep_core_evidence(self, kind, raw, data, regions, *, task=None, task_id="", scan="", source_event=""):
        if not self.runtime or not self._core:
            return ""
        return self.runtime.keep_visual(self._run_id, task.id if task else task_id,
                                        task.scan_id if task else scan, kind, raw, data, regions,
                                        source_event=source_event)

    def _keep_core_failure(self, reason, task=None):
        run = self._core
        if not self.runtime:
            return
        raw, frame = run.evidence_raw, run.evidence_frame
        if frame is not None and (raw is None or raw.frame is not frame):
            raw = VisualObservation(frame, detect_layout(frame.pixels, {}, VisualThresholds()), "CAPTURE_ONLY")
        if raw is None and task:
            raw = task.before
        if raw is not None:
            data, marks = title_evidence(raw)
            data["failure_reason"] = safe_reason(reason)
            self._keep_core_evidence("failure", raw, data,
                                     [("b2", raw.layout.get("B2"), []), ("c1", raw.layout.get("C1"), marks),
                                      ("c2", raw.layout.get("C2"), [])] if task and task.clicked_at
                                     else [("b2", raw.layout.get("B2"), [])], task=task)

    def _invalidate_core(self, reason):
        run = self._core
        if not run.invalidated:
            if run.task and run.task.clicked_at:
                self._core_event("TARGET_FAILED", run.task, "FAILED", reason=reason, clicked=True,
                                 read_risk=True)
                self._core_state(failures=run.view.failures + 1, read_unfinished=run.view.read_unfinished + 1,
                                 risk="BEFORE_UNREAD_NOT_READ")
            run.invalidated = True
            run.task = None
            self._core_event("CORE_WINDOW_IDENTITY_CHANGED", result="FAILED", reason=reason)
            self.stop(source="failure", final_reason="企业微信窗口或登录身份已变化")
        self._core_state(phase="WINDOW_IDENTITY_CHANGED", waiting=0)
        return self._set(status="STOPPED", reason="企业微信窗口或登录身份已变化，可重新开始检测",
                         account_state="TEST_WINDOW_IDENTITY_CHANGED", account_reason=reason,
                         navigation_allowed=False)

    def _core_login_changed(self, own):
        previous = self._core.login
        return bool(previous and own.key and own.signature and
                    (own.key != previous.key or (previous.avatar and own.avatar and previous.avatar != own.avatar)))

    def _core_failure_crop(self, task, raw, analysis):
        if not self._core.failure_crops:
            return
        # Save only a failed region, not an entire window or current history viewport.
        related = analysis and name_key(raw.conversation.title) == task.target.name
        gap_box = next((gap["box"] for gap in analysis.gaps if gap["box"]), None) if related else None
        if gap_box:
            area, frame, label = raw.layout.get("C2"), raw.frame, "c2"
            box = Box(*gap_box)
            if not area or not (area.contains((box.left, box.top)) and area.contains((box.right-1, box.bottom-1))):
                return
            if box.width * box.height >= area.width * area.height / 3:
                return
        elif task.clicked_at and raw is not task.before:
            box, frame, label = raw.layout.get("C1"), raw.frame, "c1"
            if box:
                box = replace(box, right=box.left + round(box.width * .69))
        else:
            box, frame, label = task.target.row.box, task.before.frame, "b2"
        if not box:
            return
        self._save_core_crop(frame, box, label, task.id)

    def _save_core_crop(self, frame, box, label, identifier):
        try:
            folder = failure_crop_directory()
            ok, image = cv2.imencode(".png", box.crop(frame.pixels))
            if ok:
                folder.mkdir(parents=True, exist_ok=True)
                (folder / f"{self._run_id}-{identifier}-{label}.png").write_bytes(image.tobytes())
                self._core_event("PRIVATE_FAILURE_CROP_SAVED", region=label, box=bounds(box), observation_id=identifier)
        except (OSError, cv2.error) as exc:
            self._core_event("PRIVATE_FAILURE_CROP_FAILED", reason=type(exc).__name__)

    def _finish_core_target(self, task, raw, code, *, reason="", analysis=None, allow_persistence=True):
        run = self._core
        if not self.active and not (self._durability and task.verified and analysis and analysis.messages):
            return self._status
        target = task.target
        run.handled.add(target.key)
        run.task = None
        run.navigation_task = None
        missing_read = bool(task.clicked_at and code != "TARGET_COMPLETED")
        if code != "TARGET_COMPLETED":
            self._keep_core_failure(reason, task)
            self._core_failure_crop(task, raw, analysis)
        result = {"TARGET_COMPLETED": "AUTO_PASS", "TARGET_PARTIAL": "PARTIAL", "TARGET_FAILED": "FAILED"}[code]
        run.clock.complete(result, reason, clicked=bool(task.clicked_at), ocr_completed=bool(analysis))
        self._core_event(code, task, result, reason=reason, read_risk=missing_read)
        if analysis and analysis.messages:
            run.clock.begin(7, "解析消息时间、比对本机历史、保存并展示")
            if allow_persistence and self.active and task.verified and not task.target.duplicate_name:
                analysis = self._core_hover(task, raw, analysis)
            analysis = self._core_persist(task, raw, analysis, allow=allow_persistence)
            if self._core_account:
                run.view = replace(run.view, messages=(*run.view.messages, *analysis.messages)[-500:])
            else:
                run.accept_messages(task, analysis)
            if not self.active:
                self._core_state()
                return self._status
            if run.clock.phase and any(item.task == task.id for item in run.view.messages):
                run.presentation = task.id
            else:
                run.clock.complete("SKIPPED", "重复观察，界面已保留既有结果")
        elif self.active and not run.independent_done:
            run.independent_reason = reason or "目标采集未完成"
        if not run.presentation:
            run.clock.end_target()
        if not run.storage_blocked:
            self._core_event("NEXT_TARGET", task, result="AUTO_PASS", fresh_b2_required=True)
        self._core_state(phase=code, failures=run.view.failures + (code == "TARGET_FAILED"),
                         partial=run.view.partial + (code == "TARGET_PARTIAL"),
                         read_unfinished=run.view.read_unfinished + missing_read,
                         risk=reason, ocr_complete=bool(analysis and analysis.messages),
                         latest=analysis.latest if analysis else "UNKNOWN",
                         completeness=analysis.completeness if analysis else "")
        return self._set(status="RUNNING" if self.active else "STOPPED",
                         reason=run.storage_blocked or reason or "已读取目标消息，下一轮重新扫描 B2")

    def _apply_pending_recovery(self, recovery):
        returned = {item.id: item for _, result in recovery.results for item in result.messages}
        saved = sum(result.first + result.new for _, result in recovery.results if not result.receipt_replayed)
        for identifier, result in recovery.results:
            self._core_event("CORE_BATCH_RECOVERED", result="AUTO_PASS", batch_id=identifier,
                             age="ORIGINAL_CAPTURE_TIME", receipt_reused=result.receipt_replayed,
                             first=0 if result.receipt_replayed else result.first,
                             new=0 if result.receipt_replayed else result.new,
                             existing=result.existing, review=result.uncertain, historical=result.historical,
                             overlap=result.overlap, anchor_status=result.anchor_status, gap=result.gap)
        if self._core and returned:
            self._core_state(messages=tuple(returned.get(item.id, item) for item in self._core.view.messages))
        reason = recovery.blocked
        return self._set(storage_state=reason or ("PENDING_INVALID" if recovery.invalid else "READY"),
                         storage_pending=recovery.pending, storage_invalid=recovery.invalid,
                         storage_recovered=self._status.storage_recovered + saved)

    def recover_pending(self, cancelled=lambda: False):
        """Idle UI recovery has no capture or native input entry points."""
        if (self.mode != "test" or not self._durability or self.active or cancelled()
                or not self._lock.acquire(blocking=False)):
            return self._status
        def check():
            if cancelled() or self.active:
                raise CancelledError()
        try:
            _, _, recovery = self._durability.prepare(check)
            return self._apply_pending_recovery(recovery)
        except CancelledError:
            return self._status
        except (SavingBlocked, sqlite3.Error, OSError, ValueError) as error:
            reason = saving_reason(error)
            self._core_event("CORE_STORAGE_RECOVERY_BLOCKED", result="BLOCKED_BY_DEPENDENCY", reason=reason)
            return self._storage_blocked_state(reason)
        finally:
            self._lock.release()

    def _prepare_core_storage(self):
        run = self._core
        config_path = str(self._durability.account.path)
        try:
            account, source, recovery = self._durability.prepare(self._core_check)
            self._apply_pending_recovery(recovery)
            self._core_event("CORE_DEFAULT_ACCOUNT_STATUS", result="AUTO_PASS",
                             reason="DEFAULT_ACCOUNT_LOADED", source=source, config_path=config_path)
            if recovery.blocked:
                raise SavingBlocked(recovery.blocked)
            self._core_account = account
            run.storage_ready = True
            run.clock.progress("保存环境就绪，准备企业微信窗口")
            self._core_event("CORE_STORAGE_READY", result="AUTO_PASS", invalid=recovery.invalid)
            return True
        except (SavingBlocked, sqlite3.Error, OSError, ValueError) as error:
            reason = saving_reason(error)
            self._stop_core_storage(reason)
            return False

    def _storage_blocked_state(self, reason):
        try:
            pending, invalid = self._durability.queue.counts()
        except (sqlite3.Error, OSError):
            return self._set(storage_state=reason)
        return self._set(storage_state=reason, storage_pending=pending, storage_invalid=invalid)

    def _stop_core_storage(self, reason):
        self._core.storage_blocked = reason
        issues = self._core.view.blockers
        if ("STORAGE", reason) not in issues:
            self._core_state(blockers=(*issues, ("STORAGE", reason)))
        self._core.clock.complete("BLOCKED_BY_DEPENDENCY", reason)
        self._storage_blocked_state(reason)
        self._core_event("CORE_STORAGE_BLOCKED", result="BLOCKED_BY_DEPENDENCY", reason=reason,
                         config_path=str(self._durability.account.path), pending=self._status.storage_pending)
        self._set(sqlite_result=reason, navigation_allowed=False, navigation_block_reason=reason)
        self._core_state(phase="SAVE_ENVIRONMENT_BLOCKED", risk=reason, waiting=0)
        self.stop(source="failure", final_reason=f"本机保存暂停 · {reason}")
        return self._status

    def _stage_core_observation(self, task, analysis, at, allow):
        run, durable = self._core, self._durability
        staged = False
        try:
            batch = PendingBatch.from_observation(
                self._core_account, analysis, task.conversation_kind, at, task.match_evidence,
                self._run_id, task.id, (task.before_evidence, task.c2_evidence))
            durable.queue.stage(batch)
            staged = True
            self._set(storage_pending=self._status.storage_pending + 1, storage_state="STAGED")
            self._core_event("CORE_BATCH_STAGED", task, "AUTO_PASS", batch_id=batch.batch_id,
                             visible=len(analysis.messages))
            if not allow or not self.active:
                raise SavingBlocked("CAPTURE_STAGED_AFTER_STOP")

            def verify():
                self._core_check()
                durable.account.check(self._core_account)

            result, reason = durable.queue.process(durable.store, self._core_account, batch.batch_id, verify)
            if result is None:
                raise SavingBlocked(reason or "PENDING_COMMIT_NOT_CONFIRMED")
            self._set(storage_pending=max(0, self._status.storage_pending - 1), storage_state="READY")
            return result, analysis
        except (CancelledError, TestDeadline, SavingBlocked, sqlite3.Error, OSError, ValueError) as error:
            reason = saving_reason(error)
            run.storage_blocked = reason
            self._set(storage_state=reason, sqlite_result=reason)
            self._core_state(save_failures=run.view.save_failures + 1)
            status = "STAGED" if staged else "FAILED"
            event = self._core_event("CORE_DB_SAVE_FAILED", task, "FAILED", reason=reason, staged=staged)
            observations = tuple(replace(item, save_status=status) for item in attach_times(analysis.messages, at))
            self._keep_core_database(task, task.before, observations,
                                     {"committed": False, "reason": reason, "staged": staged}, event)
            return None, replace(analysis, messages=observations)

    def _core_hover(self, task, raw, analysis):
        run = self._core
        if (not run.reader or not self._core_account or not raw.frame.coordinates
                or not isinstance(self.navigator, CollectionNavigation)
                or (task and (raw is task.before or task.read_at != raw.frame.captured_at))):
            return analysis
        at = (task.read_at if task else None) or raw.frame.captured_at
        try:
            if task:
                messages, decisions = preview_visible(self.test_store, self._core_account, analysis, at)
            else:
                messages = attach_times(analysis.messages, at)
                decisions = ("OBSERVE",) * len(messages)
        except (OSError, sqlite3.Error, ValueError):
            return analysis
        chosen = [index for index, item in enumerate(messages) if item.kind == "TEXT"
                  and item.quality == "RELIABLE" and decisions[index] != "EXISTING"]
        if not chosen:
            return analysis
        elapsed_limit = perf_counter() + 3.6
        try:
            stage_end = run.clock.check()
        except TestDeadline:
            return analysis
        # Leave time for enqueue/commit and leave all already verified content
        # available even when the optional local OCR reaches its time budget.
        deadline = min(elapsed_limit, stage_end - .9)
        if deadline - perf_counter() < 1.9:
            return analysis

        def check():
            self._core_check()
            if perf_counter() >= deadline:
                raise HoverBudget("HOVER_BUDGET_EXHAUSTED")

        # Unresolved/repeated candidates and the newest reliable message take
        # priority. Previously verified history is left alone.
        chosen.sort(key=lambda index: (decisions[index] == "REVIEW",
                                       not messages[index].clock.anchor, index), reverse=True)
        if not task:
            chosen = list({messages[index].direction: index for index in reversed(chosen)}.values())
        updated = list(messages)
        run.clock.progress("按需读取 C2 悬停精确时间")
        for attempt, index in enumerate(sorted(chosen[:2])):
            if deadline - perf_counter() < .95:
                break
            item = updated[index]
            hover_started = EventTime.now().wall
            result = None
            try:
                check()
                raster = self.navigator.hover_message(raw, item, check)
                lower = datetime.fromisoformat(item.clock.anchor) if item.clock.anchor else None
                upper = next((datetime.fromisoformat(next_item.clock.anchor) for next_item in messages[index + 1:]
                              if next_item.clock.anchor and next_item.clock.anchor != item.clock.anchor), None)
                result, reason, box, ocr_ms = run.reader.request(
                    ("hover_ocr", raster, at, lower, upper), check, self._cancel)
                earlier = next((previous.hover for previous in reversed(updated[:index])
                                if previous.hover and previous.hover.anchor and not previous.hover.issue), None)
                if result and result.anchor and earlier and (
                        datetime.fromisoformat(result.anchor) < datetime.fromisoformat(earlier.anchor)):
                    result = replace(result, issue="HOVER_REVERSED_SEQUENCE")
                    reason = result.issue
                event = self._core_event("HOVER_TIME_OBSERVED", task, "AUTO_PASS" if result and not result.issue else "PARTIAL",
                    observation_id=item.id, reason=reason, bubble=bounds(item.box), screen_point=raster.point,
                    crop_screen=raster.region, tooltip=bounds(box), mapping=raw.frame.coordinates.snapshot,
                    started_at=hover_started.isoformat(), finished_at=EventTime.now().wall.isoformat(),
                    move_and_wait_ms=raster.move_ms, local_ocr_ms=ocr_ms,
                    time_precision=result.precision if result else "", fallback=not result or bool(result.issue))
                if result:
                    updated[index] = replace(item, hover=result)
                if self.runtime and (result or attempt == 0):
                    height, width = raster.after.shape[:2]
                    local = Box(0, 0, width, height)
                    capture = replace(raw.frame.capture, image=raster.after, width=width, height=height)
                    visual = replace(raw, frame=replace(raw.frame, capture=capture, coordinates=None))
                    self._keep_core_evidence("hover", visual, {"observation_id": item.id,
                        "hover_time": asdict(result) if result else None, "screen_region": raster.region, "screen_point": raster.point,
                        "associated_box": bounds(box), "ocr_ms": ocr_ms, "reason": reason},
                        [("hover-before", local, [], raster.before),
                         ("hover-after", local, [(rect(box), "TOOLTIP")] if box else [], raster.after)],
                        task=task, source_event=event)
                if not result or result.issue:
                    self._core_state(hover_failures=run.view.hover_failures + 1)
            except (NavigationPaused, CaptureError, HoverBudget, OcrUnavailable, CancelledError, TestDeadline,
                    OSError, ValueError) as exc:
                self._core_state(hover_failures=run.view.hover_failures + 1)
                self._core_event("HOVER_TIME_FALLBACK", task, "PARTIAL", observation_id=item.id,
                                 bubble=bounds(item.box), reason=safe_reason(exc), fallback=True)
                break
        return replace(analysis, messages=tuple(updated))

    def _core_persist(self, task, raw, analysis, *, allow):
        run = self._core
        at = task.read_at or raw.frame.captured_at
        if task.target.duplicate_name:
            reason = "DUPLICATE_FULL_NAME_HISTORY_AMBIGUOUS"
            event = self._core_event("CORE_DB_NOT_SAVED", task, "PARTIAL", reason=reason)
            review = tuple(replace(item, collection_status="REVIEW", save_status="REVIEW")
                           for item in attach_times(analysis.messages, at))
            self._keep_core_database(task, raw, review, {"committed": False, "reason": reason}, event)
            self._core_state(review_messages=run.view.review_messages + len(review))
            return replace(analysis, messages=review)
        durable = bool(self._durability and task.verified and self._core_account)
        if not durable and (not self._core_account or not task.verified or not allow):
            timed = attach_times(analysis.messages, at)
            if self._isolate_core_reads:
                status = self._core_account_status
                reason = (status.reason if status and not status.account else "DEFAULT_ACCOUNT_READ_FAILED"
                          ) if not self._core_account else "CAPTURE_EXPIRED_OR_UNVERIFIED"
                timed = tuple(replace(item, save_status="FAILED") for item in timed)
                self._core_state(save_failures=run.view.save_failures + 1)
                self._set(sqlite_result=reason)
                event = self._core_event("CORE_DB_NOT_SAVED", task, "PARTIAL", reason=reason)
                self._keep_core_database(task, raw, timed, {"committed": False, "reason": reason,
                                                            "config_path": str(status.path) if status else ""}, event)
            return replace(analysis, messages=timed)
        try:
            if durable:
                result, analysis = self._stage_core_observation(task, analysis, at, allow)
                if result is None:
                    return analysis
            else:
                result = save_visible(self.test_store, self._core_account, analysis, task.conversation_kind, at,
                                      self._run_id, self._core_check)
        except (TestDeadline, sqlite3.Error, OSError, ValueError) as exc:
            reason = str(exc) if isinstance(exc, DefaultAccountConflict) else type(exc).__name__
            event = self._core_event("CORE_DB_SAVE_FAILED", task, "FAILED", reason=reason)
            self._core_state(save_failures=run.view.save_failures + 1)
            if isinstance(exc, TestDeadline):
                run.clock.complete("TIMEOUT", str(exc))
            failed = tuple(replace(item, save_status="FAILED") for item in attach_times(analysis.messages, at))
            self._keep_core_database(task, raw, failed, {"committed": False, "reason": reason}, event)
            return replace(analysis, messages=failed)
        except CancelledError:
            return analysis
        details = dict(first=result.first, new=result.new, existing=result.existing, review=result.uncertain,
                       history=result.historical, visible=len(result.messages), overlap=result.overlap, gap=result.gap,
                       anchor_status=result.anchor_status, had_anchor=result.had_anchor, anchor_updated=result.anchor_updated,
                       receipt_reused=result.receipt_replayed, committed_at=result.committed_at)
        event = self._core_event("CORE_DB_COMMITTED", task, "AUTO_PASS", **details)
        self._keep_core_database(task, raw, result.messages, {**details, "committed": True,
                                                              "previous_anchor": result.previous_anchor}, event)
        self._core_state(first_saved=run.view.first_saved + result.first, new_saved=run.view.new_saved + result.new,
                         existing=run.view.existing + result.existing, review_messages=run.view.review_messages + result.uncertain)
        self._set(saved=self._status.saved + result.first + result.new, added=self._status.added + result.new,
                  repeated=self._status.repeated + result.existing, uncertain=self._status.uncertain + result.uncertain,
                  sqlite_result=f"隔离库已提交 · 首次 {result.first} · 新增 {result.new} · 待复核 {result.uncertain}",
                  last_collected_at=EventTime.now() if result.first or result.new else self._status.last_collected_at)
        return replace(analysis, messages=result.messages)

    def _keep_core_database(self, task, raw, messages, details, event):
        data, _ = message_evidence(raw, messages, body=False)
        data.update(database={**details, "key": name_key(messages[0].contact) if messages else ""},
                    c2_evidence=task.c2_evidence, conversation_verified=task.verified)
        data.pop("ocr")
        data.pop("containers")
        self._keep_core_evidence("database", raw, data, [], task=task, source_event=event)

    def _timed_core_event(self, code, result, details):
        stage = f"TEST_STAGE_{details['stage_number']}" if "stage_number" in details else "TEST_RUN"
        self._record(stage, code, "FAILED" if result == "TIMEOUT" else result, details=details)

    def core_progress(self):
        if not self._core or not self._core.clock:
            return None
        return self._core.clock.snapshot()

    def check_core_watchdog(self):
        run = self._core
        if self.active and run and run.clock.now() >= run.clock.end:
            self.stop(source="timeout", final_reason="本轮测试达到 180 秒")

    def _core_check(self):
        if not self.active:
            raise CancelledError()
        self._core.clock.check()

    def _core_capture(self):
        run = self._core
        self._core_check()
        frame = (run.reader.request("capture", self._core_check, self._cancel) if run.reader else self.capture())
        self._core_check()
        run.evidence_frame = frame
        try:
            self._validate_read_frame(frame)
        except NavigationPaused as exc:
            error = CaptureError("COORDINATE_FRAME_REJECTED", str(exc))
            error.frame, error.diagnostics = frame, frame.diagnostics
            raise error from exc
        if frame.coordinates:
            self._core_event("CAPTURE_MAPPING_VERIFIED", result="AUTO_PASS",
                             geometry=frame.coordinates.diagnostic(), capture_ms=round(frame.diagnostics.elapsed_ms))
        return frame

    def _core_read_frame(self, frame, operation):
        run = self._core
        self._core_check()
        keep_completed = self._durability is not None and operation == "body"
        if run.reader:
            raw = run.reader.request(operation, self._core_check, self._cancel, keep_completed=keep_completed)
        elif isinstance(self.read, CollectionReader):
            raw = self.read.read_core(frame, lambda _: operation != "header", history_only=operation == "independent")
        else:
            raw = self.read(frame)
        if not keep_completed:
            self._core_check()
        return raw

    def _core_independent_read(self, frame):
        run = self._core
        raw = self._core_read_frame(frame, "independent")
        self._validate_read_frame(raw.frame, frame)
        if (self._core_login_changed(raw.login) or (run.lifetime and self._lifetime(frame) != run.lifetime)):
            return self._invalidate_core("INDEPENDENT_WINDOW_IDENTITY_CHANGED")
        if raw.page != "MESSAGES_CANDIDATE" or not raw.layout.get("C2"):
            return self._core_blocked("当前窗口缺少可验证的 C2 消息页")
        identifier = uuid4().hex
        analysis = analyze_read(raw, identifier, raw.conversation.title if raw.conversation.title_name_reliable else "",
                                None, "", verified=False, badge_cleared=False, refreshed=False)
        # A manual F10 diagnostic can verify current text tooltips without
        # attributing or writing these existing messages as collected history.
        if raw.conversation.title_name_reliable and analysis.messages:
            # The diagnostic reads the currently visible, titled page. It
            # does not attach these observations to persistent history.
            analysis = self._core_hover(None, raw, analysis)
        displayed = tuple(replace(item, unread="INDEPENDENT_OCR") for item in attach_times(
            analysis.messages, frame.captured_at))
        run.independent_done = True
        run.independent_reason = ""
        run.clock.complete("PARTIAL" if analysis.gaps or not displayed else "AUTO_PASS",
                           "独立 OCR · 未确认目标未读归属", ocr_completed=True)
        event = self._record("CORE_EVIDENCE", "INDEPENDENT_C2_OBSERVED",
                             "PARTIAL" if analysis.gaps or not displayed else "AUTO_PASS", task_id=identifier,
                             details=dict(count=len(displayed), completeness=analysis.completeness,
                                          messages=[item.diagnostic() for item in displayed], gaps=analysis.gaps))
        data, marks = message_evidence(raw, displayed)
        self._keep_core_evidence("c2", raw, {**data, "conversation_verified": False},
                                 c2_evidence_regions(raw, marks), task_id=identifier, source_event=event)
        if displayed:
            run.clock.begin(7, "展示独立 OCR 正文及未确认归属")
            run.presentation = identifier
        self._core_state(phase="INDEPENDENT_OCR", ocr_complete=True, completeness=analysis.completeness,
                         messages=(*run.view.messages, *displayed)[-500:],
                         independent_messages=run.view.independent_messages + len(displayed),
                         risk="独立 OCR 测试 · 非已确认目标未读")
        return self._set(reason="已执行当前页面独立 OCR，未确认目标未读归属")

    def _core_blocked(self, reason, *, timeout=False, failure_type=""):
        phase = self._core.clock.phase
        if not failure_type and phase and phase.number in (1, 2, 6):
            failure_type = {1: "WINDOW_PREPARATION", 2: "B2_LAYOUT_OR_OCR", 6: "C2_READ"}[phase.number]
        if failure_type:
            issues = self._core.view.blockers
            if (failure_type, reason) not in issues:
                self._core_state(blockers=(*issues, (failure_type, reason)))
            self._core_event("CORE_STAGE_BLOCKED", result="BLOCKED_BY_DEPENDENCY",
                             failure_type=failure_type, reason=reason)
        run = self._core
        number = run.clock.phase.number if run.clock.phase else 0
        run.clock.complete("TIMEOUT" if timeout else "BLOCKED_BY_DEPENDENCY", reason)
        task = run.task or run.navigation_task
        if task:
            self._core_event("TARGET_STAGE_ENDED", task, reason=reason, read_risk=bool(task.clicked_at))
            run.clock.end_target()
            return self._finish_core_target(task, task.before,
                                            "TARGET_PARTIAL" if task.best else "TARGET_FAILED",
                                            reason=reason, analysis=task.best, allow_persistence=not timeout)
        if number == 7:
            run.presentation = ""
            run.clock.end_target()
        elif run.independent_reason:
            run.independent_done = True
            run.independent_reason = ""
        else:
            self._keep_core_failure(reason)
            run.independent_reason = reason
            run.finish_after_independent = True
        return self._set(status="RUNNING", reason=reason)

    def _tick_core(self):
        run = self._core
        clock = run.clock
        try:
            self._core_check()
            if run.storage_blocked and not run.presentation:
                return self._stop_core_storage(run.storage_blocked)
            if clock.phase and clock.phase.number == 1:
                if self._durability and not run.storage_ready and not self._prepare_core_storage():
                    return self._status
                blocked = None
                if self._prepare_pending:
                    self._prepare_pending = False
                    blocked = self._prepare_start()
                self._core_check()
                if blocked is not None:
                    return self._core_blocked(blocked.reason)
                clock.complete()
            if run.presentation:
                return self._status
            if run.finish_after_independent and run.independent_done:
                issue = run.view.blockers[-1][1] if run.view.blockers else ""
                self.stop(source="failure" if issue else "automatic",
                          final_reason=issue or "本轮可执行测试已结束")
                return self._status
            if run.independent_reason:
                clock.begin(6, "导航或列表任务未完成，独立读取当前 C2")
                self._core_state(phase="INDEPENDENT_OCR", risk=run.independent_reason)
                return self._core_independent_read(self._core_capture())
            if not clock.phase:
                clock.begin(2, "截图并识别 B2 可见联系人")
            self._round += 1
            self._set(scan_id=uuid4().hex[:12], b2_scan_completed_at=None, scan_disposition="B2_NOT_SCANNED")
            return self._core_poll(self._core_capture())
        except TestDeadline as exc:
            if exc.limit == "TEST":
                self.stop(source="timeout", final_reason=str(exc))
                return self._status
            return self._core_blocked(str(exc), timeout=True)
        except CancelledError:
            return self._status
        except (CaptureError, NavigationPaused, OcrUnavailable) as exc:
            if not self.active:
                return self._status
            if isinstance(exc, CaptureError):
                code = exc.code
                event = self._core_event("CORE_CAPTURE_REJECTED", result="BLOCKED_BY_DEPENDENCY",
                                         reason=safe_reason(exc), has_raw=exc.frame is not None, code_detail=code)
                if exc.frame is not None and self.runtime and not any(
                        item[0] == code for item in run.view.blockers):
                    frame = exc.frame
                    d = frame.diagnostics
                    sample = d.window_health.last if d.window_health else None
                    self.runtime.keep_visual(self._run_id, "", self._status.scan_id, "capture_failure",
                        VisualObservation(frame, LayoutObservation((), "UNVERIFIED"), "CAPTURE_ONLY"),
                        {"code": code, "reason": safe_reason(exc), "client": d.client_size,
                         "raw": d.raw_size, "probe": d.probe_size, "dpi": d.dpi, "window": d.window_rect,
                         "desktop": asdict(sample.desktop) if sample and sample.desktop else None},
                        [("capture", Box(0, 0, frame.capture.width, frame.capture.height), [])], source_event=event)
                return self._core_blocked(safe_reason(exc), failure_type=code)
            return self._core_blocked(safe_reason(exc))
        finally:
            if not self.active:
                if isinstance(self.permit, CollectionPermit):
                    self.permit.revoke()
                if run.reader:
                    run.reader.close()

    def _core_poll(self, frame):
        run = self._core
        if run.invalidated:
            return self._status
        lifetime = self._lifetime(frame)
        if run.lifetime and lifetime != run.lifetime:
            return self._invalidate_core("WINDOW_LIFETIME_CHANGED")
        if lifetime[-1] is None:
            return self._wait_for_window("WINDOW_STATE_UNCERTAIN · 未能验证企业微信主窗口进程身份")
        run.lifetime = lifetime
        task = run.task
        if task and frame.coordinates and task.before.frame.coordinates and (
                frame.coordinates.snapshot != task.before.frame.coordinates.snapshot):
            self._core_event("GEOMETRY_RESYNC", task, "PARTIAL", clicked=bool(task.clicked_at),
                             old_snapshot=task.before.frame.coordinates.snapshot,
                             new_snapshot=frame.coordinates.snapshot)
            return self._finish_core_target(task, task.before, "TARGET_PARTIAL" if task.best else "TARGET_FAILED",
                                            reason="WINDOW_GEOMETRY_CHANGED_AFTER_CLICK", analysis=task.best)
        attempted = EventTime.now()
        self._set(screenshot_succeeded_at=frame.captured_at, b2_scan_attempt_at=attempted.wall,
                  scan_disposition="B2_SCANNING", status="RUNNING", trace_job=task.id if task else "")
        ready = None
        phase = "WAITING_TARGET_TITLE"

        def allow_c2(header):
            nonlocal ready, phase
            if not task or not task.clicked_at or not self.active or self._core_login_changed(header.login):
                return False
            if _identity(header.frame) != _identity(task.before.frame):
                phase = "WINDOW_GEOMETRY_CHANGED_AFTER_CLICK"
                return False
            candidate = _verify_transition(task.before, header, task.target)
            sample = history_sample(candidate)
            refreshed = history_changed(task.before_sample, sample)
            if refreshed and not task.changed:
                task.changed = True
                self._core_event("CONVERSATION_CHANGED", task, c2_changed=True,
                                 title_changed=name_key(header.conversation.title) != name_key(task.before.conversation.title))
            evidence, distance = _core_selection_evidence(task.before, candidate, task.target, refreshed)
            task.match_evidence = evidence
            task.match_distance = round(distance, 2) if distance is not None else None
            if evidence not in ("EXACT_VISUAL_IDENTITY", "CLICK_TITLE_ROW_BADGE_REFRESH_AND_AVATAR",
                                "CLICK_TITLE_ROW_BADGE_REFRESH_AVATAR_UNAVAILABLE", "CLICK_GEOMETRY_TITLE_BADGE_REFRESH",
                                "CLICK_REPEATED_FULL_NAME_ROW"):
                task.previous = None
                return False
            phase = "WAITING_C2_REFRESH" if not refreshed else "WAITING_C2_STABILITY"
            if isinstance(self.navigator, CollectionNavigation):
                try:
                    self.navigator.verify_normal_message(candidate)
                except NavigationPaused:
                    phase = "C2_WINDOW_OR_OVERLAY_UNVERIFIED"
                    task.previous = None
                    return False
            row = next(row for row in candidate.contacts if row.is_selected is True) if evidence in (
                "CLICK_GEOMETRY_TITLE_BADGE_REFRESH", "CLICK_REPEATED_FULL_NAME_ROW") else (
                next(row for row in candidate.contacts if name_key(row.display_name) == task.target.name))
            cleared = not row.has_unread_indicator and row.badge is None
            if not task.verified and (refreshed or row.is_selected is True):
                task.verified = True
                run.clock.complete(title_verified=True)
                run.clock.begin(6, "定位最新消息并读取 C2")
                self._core_state(verified=run.view.verified + 1, title_match=True)
                self._core_event("CONVERSATION_VERIFIED", task, "AUTO_PASS", badge_cleared=cleared,
                                 selection=candidate.conversation.selection_method, c2_changed=refreshed,
                                 match_evidence=evidence, avatar_distance=task.match_distance,
                                 after_avatar_contour=bool(row.avatar))
            if not refreshed:
                task.previous = None
                return False
            # Treat the first verified sample as the reference. Subsequent samples must
            # independently repeat the title, account, geometry and refreshed C2 evidence.
            area = candidate.layout.get("C2")
            stable = task.previous is not None and area == task.previous_box and not history_changed(task.previous, sample)
            task.previous = sample
            task.previous_box = area
            if task.verified and stable:
                ready = candidate, cleared
            return ready is not None

        raw = self._core_read_frame(frame, "header")
        if allow_c2(raw) and (run.reader or isinstance(self.read, CollectionReader)):
            run.clock.progress("读取 C2 正文并复核遗漏气泡")
            raw = self._core_read_frame(frame, "body")
        ocr_at = EventTime.now()
        self._set(scan_ocr_completed_at=ocr_at.wall, match=raw.conversation.match_status,
                  account_name=raw.login.name, account_at=ocr_at)
        if not self.active and not (self._durability and ready and task and task.verified):
            return self._status
        self._validate_read_frame(raw.frame, frame)
        run.evidence_raw = raw
        run.evidence_frame = raw.frame
        if self._window_probe:
            current = self._window_probe()
            self._record_window(current)
            captured = frame.diagnostics.window_health
            if captured and current.identity != captured.identity and run.lifetime:
                return self._invalidate_core("WINDOW_LIFETIME_CHANGED_DURING_OCR")
            if current.capture_blocker or (captured and current.geometry_key != captured.geometry_key):
                return self._wait_for_window(f"{current.status} · OCR 后窗口状态或坐标变化")
        else:
            current = frame.diagnostics.window_health
        if self._core_login_changed(raw.login):
            return self._invalidate_core("A1_LOGIN_CHANGED")
        a1_observed = bool(raw.login.key and raw.login.signature)
        if a1_observed and (run.login is None or (not run.login.avatar and raw.login.avatar)):
            run.login = raw.login
            self._record("A1", "TEST_A1_OBSERVED", "AUTO_PASS",
                         details={"visual_only": True, "avatar_observed": bool(raw.login.avatar)})
        self._set(account_state="TEST_A1_OBSERVED" if a1_observed else "TEST_A1_UNAVAILABLE",
                  account_email="", account_id=None, account_choices=(),
                  account_reason="A1 视觉观察 · 确认会话使用本机隔离账号保存" if a1_observed else
                  "当前 A1 未读到可靠姓名 · 按已核对的企业微信窗口继续",
                  bound_pid=frame.capture.pid, bound_hwnd=frame.capture.hwnd, waiting_for_window=False)
        if task:
            return self._core_read_target(task, raw, ready, phase)
        if (raw.page != "MESSAGES_CANDIDATE" or raw.layout.get("B2") is None or raw.layout.get("C2") is None
                or any(step.status in ("FAILED", "SKIPPED") for step in raw.ocr_areas
                       if step.region == "B2 联系人")):
            self._set(scan_disposition="B2_SCAN_FAILED", navigation_allowed=False)
            return self._core_blocked("消息页面或 B2 联系人 OCR 未确认")
        return self._core_scan(raw, attempted, current)

    def _core_read_target(self, task, raw, ready, phase):
        run = self._core
        task.polls += 1
        matching_rows = [row for row in raw.contacts if name_key(row.display_name) == task.target.name]
        event = self._core_event("CONVERSATION_SAMPLE", task, poll=task.polls, phase=phase,
                         title_reliable=raw.conversation.title_name_reliable,
                         title_exact=name_key(raw.conversation.title) == task.target.name,
                         matching_rows=len(matching_rows), c2_changed=task.changed,
                         badge_after=matching_rows[0].has_unread_indicator if len(matching_rows) == 1 else None,
                         selection=raw.conversation.selection_method, match_evidence=task.match_evidence,
                         avatar_distance=task.match_distance,
                         after_avatar_contour=bool(len(matching_rows) == 1 and matching_rows[0].avatar),
                         ready_for_body=bool(ready))
        if self.runtime:
            data, title_marks = title_evidence(raw)
            selection = [(rect(row.box), "SELECTED") for row in raw.contacts if row.is_selected is True]
            self._keep_core_evidence("after", raw, {**data, "poll": task.polls, "phase": phase,
                                     "badge_after": any(row.has_unread_indicator for row in raw.contacts
                                                        if row.is_selected is True),
                                     "confirmation": task.match_evidence, "avatar_distance": task.match_distance,
                                     "before_evidence": task.before_evidence},
                                     [("b2", raw.layout.get("B2"), selection), ("c1", raw.layout.get("C1"), title_marks),
                                      ("c2", raw.layout.get("C2"), [])], task=task, source_event=event)
        if ready:
            task.body_reads += 1
            verified, cleared = ready
            if cleared:
                run.cleared.add(task.target.key)
            row = task.target.row
            analysis = analyze_read(raw, task.id, verified.conversation.title,
                                    row.unread_count if row.unread_count_reliable else None,
                                    row.preview if row.preview_reliable else "", verified=True,
                                    badge_cleared=cleared, refreshed=True)
            self._core_event("C2_CAPTURED", task, "AUTO_PASS", box=bounds(raw.layout.get("C2")),
                             captured_at=raw.frame.captured_at.isoformat(), poll=task.polls)
            self._core_event("LATEST_POSITION_CHECKED", task,
                             "PARTIAL" if analysis.latest == "UNKNOWN" else "AUTO_PASS", method=analysis.latest)
            timed = attach_times(analysis.messages, raw.frame.captured_at)
            event = self._core_event("MESSAGE_OCR_COMPLETED", task, "PARTIAL" if analysis.gaps or not analysis.messages else "AUTO_PASS",
                             messages=[{**item.diagnostic(), "time_format": item.clock.format,
                                        "time_issue": item.clock.issue, "time_precision": item.clock.precision,
                                        "time_correction": item.clock.correction, "year_inferred": item.clock.year_inferred}
                                       for item in timed],
                             completeness=analysis.completeness, gaps=analysis.gaps, viewport_limits=analysis.viewport_limits,
                             body_reads=task.body_reads, recognition_ms=round(raw.recognition_ms))
            data, marks = message_evidence(raw, timed)
            shot = self._keep_core_evidence("c2", raw, {**data, "conversation_verified": True, "gaps": analysis.gaps,
                                             "viewport_limits": analysis.viewport_limits},
                                            c2_evidence_regions(raw, marks), task=task, source_event=event)
            confirmed = sum(item.unread == "CONFIRMED_UNREAD" for item in analysis.messages)
            self._core_event("UNREAD_RANGE_CHECKED", task, "AUTO_PASS" if confirmed else "PARTIAL", unread_confirmed=confirmed,
                             badge_cleared=cleared, before_count=row.unread_count if row.unread_count_reliable else None)
            self._core_state(latest=analysis.latest, completeness=analysis.completeness,
                             ocr_complete=any(area.region.startswith("C2") and area.status in ("TEXT_FOUND", "NO_TEXT")
                                              for area in raw.ocr_areas) or bool(analysis.messages))
            if analysis.messages and (task.best is None or _core_read_rank(analysis) >= _core_read_rank(task.best)):
                task.best = analysis
                task.read_at, task.conversation_kind = raw.frame.captured_at, verified.conversation.conversation_type
                task.c2_evidence = shot
            if not self.active and task.best:
                return self._finish_core_target(task, raw, "TARGET_PARTIAL",
                                                reason="停止时已取得可靠会话观察", analysis=task.best,
                                                allow_persistence=False)
            if analysis.messages and not analysis.gaps:
                return self._finish_core_target(task, raw, "TARGET_COMPLETED", analysis=analysis)
            retryable = any(gap["reason"] in ("C2_OCR_FAILED", "C2_OCR_SKIPPED", "BODY_OCR_UNCERTAIN",
                                              "BODY_OCR_BELOW_CORE_THRESHOLD") for gap in analysis.gaps)
            if task.best and any(item.quality == "RELIABLE" for item in task.best.messages) and (
                    self._durability or not retryable or task.body_reads >= 2):
                return self._finish_core_target(task, raw, "TARGET_PARTIAL",
                                                reason="C2 可见消息存在覆盖缺口", analysis=task.best)
            phase = "C2_RECOVERABLE_GAP" if retryable else "C2_OCR_NO_RELIABLE_MESSAGES"
        if task.polls >= MAX_READ_POLLS:
            if task.best:
                return self._finish_core_target(task, raw, "TARGET_PARTIAL", reason=phase, analysis=task.best)
            return self._finish_core_target(task, raw, "TARGET_FAILED", reason=phase)
        self._core_state(phase=phase, risk="CLICKED_AWAITING_READ")
        run.clock.progress("重新截图复核会话和 C2", phase, retry=True)
        return self._set(reason="已点击目标，等待独立截图复核与读取")

    def _core_scan(self, raw, attempted, current):
        run = self._core
        clock = run.clock
        if clock.phase.number == 2:
            clock.complete()
            clock.begin(3, "按范围筛选可见未读候选")
            run.selection_waits = 0
        raw, mute_wait = self._core_review_muted(raw)
        names = Counter(name_key(row.display_name) for row in raw.contacts if row.display_name)
        for row in raw.contacts:
            name = name_key(row.display_name)
            if row.has_unread_indicator or names[name] != 1 or not row.name_reliable or row.status != "CANDIDATE":
                continue
            key = _Target(row, name, avatar_key(raw, row), ()).key
            if key in run.seen:
                run.cleared.add(key)
                run.precheck_failures.pop(key, None)
        foreground_block = "WINDOW_NOT_FOREGROUND" if current and not current.foreground else ""
        targets = self._record_b2_result(raw, attempted, blocked_reason=foreground_block)
        event = self._record("B2", "B2_SCANNED", "AUTO_PASS", elapsed_ms=raw.recognition_ms,
                     details={"scan": self._status.scan_id, "contacts": len(raw.contacts),
                              "unread": self._status.unread_marker_count})
        if self.runtime:
            data, marks = contact_scan(raw, unread_targets(raw, self.scope, allow_duplicate=True),
                                       self._status.scan_id, foreground_block)
            box, header = raw.layout.get("B2"), raw.layout.get("C1")
            regions = [("b2", box, marks)]
            if box and header and box.top - header.bottom >= round(18 * raw.frame.state.dpi / 96):
                start = max(header.bottom, box.top - round(100 * raw.frame.state.dpi / 96))
                regions.append(("b2-edge", Box(box.left, start, box.right, box.top + 2),
                                [([box.left, box.top, box.right, box.top + 1], "B2 START")]))
            self._keep_core_evidence("b2", raw, data, regions,
                                     scan=self._status.scan_id, source_event=event)
        eligible = []
        for target in sorted(targets, key=lambda item: item.row.box.top):
            key, signature = target.key, target.unread_signature
            if target.ambiguous:
                continue
            prior = run.signatures.get(key)
            changed = key in run.cleared or (prior is not None and _new_unread(prior, signature))
            if key not in run.seen or changed:
                run.discovered_at[key] = raw.frame.captured_at
                self._core_event("UNREAD_DETECTED", scan=self._status.scan_id, row=target.row.sequence,
                                 target_id=blake2s((self._run_id + key).encode(), digest_size=8).hexdigest(),
                                 unread_count=signature[0], preview_reliable=bool(signature[1]),
                                 captured_at=raw.frame.captured_at.isoformat())
            if changed:
                run.handled.discard(key)
                run.precheck_failures.pop(key, None)
            run.cleared.discard(key)
            run.seen.add(key)
            run.signatures[key] = signature if changed or prior is None else _merge_unread(prior, signature)
            if key not in run.handled:
                eligible.append(target)
        self._core_state(discovered=len(run.seen), waiting=len(eligible))
        self._set(queued=len(eligible), awaiting_navigation=len(eligible), navigation_allowed=bool(eligible and not foreground_block),
                  navigation_block_reason=foreground_block)
        if run.navigation_task:
            previous = run.navigation_task
            retry = next((item for item in eligible if item.key == previous.target.key), None)
            if retry is None:
                return self._finish_core_target(previous, raw, "TARGET_FAILED", reason="新截图无法复核原目标及未读")
            eligible = [retry, *(item for item in eligible if item.key != retry.key)]
        if foreground_block:
            self._core_state(phase="WAITING_FOREGROUND")
            run.selection_waits += 1
            clock.progress("等待企业微信恢复前台", foreground_block, retry=True)
            if run.selection_waits >= 2 and not run.navigation_task:
                return self._core_blocked(foreground_block)
            return self._set(status="PAUSED", reason="已完成 B2 扫描，等待企业微信恢复前台")
        if not eligible:
            self._core_state(phase="WAITING_UNREAD")
            if mute_wait:
                reason = "正在限次复核未知静音状态"
                clock.progress(reason, retry=True)
                return self._set(reason=reason)
            reason = ("当前可见范围没有未读" if not self._status.unread_marker_count
                      else "可见未读已处理或因范围、视觉证据不足而跳过")
            clock.complete("PARTIAL" if self._status.unreliable_unread_count else "SKIPPED", reason)
            if not run.independent_done and not run.view.messages:
                run.independent_reason = reason
                run.finish_after_independent = True
                return self._set(reason=reason)
            self.stop(source="automatic", final_reason=reason)
            return self._status
        target = eligible[0]
        task = run.navigation_task
        if task:
            task.target, task.before, task.before_sample = target, raw, history_sample(raw)
        else:
            task = CoreTask(target, raw, EventTime.now().wall, run.discovered_at[target.key],
                            before_sample=history_sample(raw))
            clock.begin_target()
            clock.complete()
            clock.begin(4, "核对 B2 实际操作点并点击联系人")
            run.navigation_task = task
        task.scan_id = self._status.scan_id
        self._set(trace_job=task.id)
        self._core_state(phase="NAVIGATION_PRECHECK", contact=target.row.display_name,
                         before_count=target.unread_signature[0], clicked=False, title_match=False,
                         latest="UNKNOWN", ocr_complete=False, completeness="", risk="")
        self._core_event("TARGET_IN_SCOPE", task, "AUTO_PASS", scoped=True, a1_observed=bool(raw.login.key),
                         pid=raw.frame.capture.pid, hwnd=raw.frame.capture.hwnd)
        self._core_event("TARGET_SELECTED", task, "AUTO_PASS", row=target.row.sequence, row_box=bounds(target.row.box),
                         unread_count=target.unread_signature[0], badge_box=bounds(target.row.badge),
                         badge_kind="DIGIT" if target.row.unread_count_reliable else "DOT_OR_UNKNOWN_DIGIT",
                         badge_style=target.row.badge_kind, name_reliable=target.row.name_reliable,
                         time_anchor=target.row.activity_time_reliable, boundary=target.row.boundary_source,
                         selection=target.row.selection_method, avatar_available=bool(target.row.avatar),
                         preview_reliable=target.row.preview_reliable, previous_title_reliable=raw.conversation.title_name_reliable,
                         discovered_at=task.discovered_at.isoformat(), selected_at=task.created_at.isoformat(),
                         foreground=current.foreground if current else None, window_box=raw.frame.capture.rect)
        try:
            self._core_check()
            if self._durability:
                try:
                    self._durability.before_click(self._core_account, self._core_check)
                except (SavingBlocked, sqlite3.Error, OSError, ValueError) as error:
                    return self._stop_core_storage(saving_reason(error))
            point = row_target(raw, target.row, allow_duplicate=True)
            if isinstance(self.navigator, CollectionNavigation):
                self.navigator.verify_normal_message(raw)
            else:
                self.navigator.verify_view(raw, need_idle=True)
            self.permit.ensure_current()
            if not self.active or run.invalidated:
                return self._status
            event = self._core_event("NAVIGATION_PRECHECK", task, "AUTO_PASS", point=point)
            data, markers = title_evidence(raw)
            x, y = point
            plan = {**data, "contact": target.row.display_name, "key": target.name,
                    "row": target.row.sequence, "row_box": rect(target.row.box), "badge": rect(target.row.badge),
                    "unread_count": target.unread_signature[0], "point": point}
            task.before_evidence = self._keep_core_evidence("before", raw, plan,
                [("b2", target.row.box, [(rect(target.row.box), "TARGET"), (rect(target.row.badge), "BADGE"),
                                        ([x-4, y-4, x+5, y+5], "POINT")]),
                 ("c1", raw.layout.get("C1"), markers)], task=task, source_event=event)
            # Keep the native action in the original gateway; capture runs in the read-only process.
            run.task = task
            if isinstance(self.navigator, CollectionNavigation):
                self.navigator.select(raw, target.row, fresh_capture=self._core_capture, deadline_check=self._core_check)
            else:
                self.navigator.select(raw, target.row)
        except (NavigationPaused, CaptureError, OSError) as exc:
            if not task.clicked_at:
                run.task = None
            self._core_event("NAVIGATION_PRECHECK", task, "BLOCKED_BY_DEPENDENCY", reason=safe_reason(exc),
                             cursor_moved=task.cursor_moved, actual_click=bool(task.clicked_at))
            if not self.active:
                return self._status
            if task.clicked_at:
                run.navigation_task = None
                clock.complete("PARTIAL", "已实际点击，等待复核", clicked=True)
                clock.begin(5, "重新截图确认目标会话")
                return self._set(reason="点击已经发生，等待重新截图核对会话")
            clock.progress("重新读取目标位置后复核", safe_reason(exc), retry=True)
            if isinstance(exc, NavigationPaused) and (
                    exc.health is not None or any(part in str(exc) for part in ("空闲", "输入桌面", "新的用户操作"))):
                self._core_state(phase="WAITING_NAVIGATION_SAFETY", risk="NAVIGATION_SAFETY_GATE")
                return self._set(status="PAUSED", reason=str(exc), navigation_allowed=False,
                                 navigation_block_reason=str(exc))
            run.precheck_failures[target.key] += 1
            if run.precheck_failures[target.key] >= 2:
                return self._finish_core_target(task, raw, "TARGET_FAILED", reason="NAVIGATION_RETRY_LIMIT")
            return self._set(status="PAUSED", reason=str(exc), navigation_allowed=False,
                             navigation_block_reason=str(exc))
        task.clicked_at = task.clicked_at or EventTime.now().wall
        if not self.active:
            self._core_state(phase="STOPPED", clicked=True, waiting=0,
                             risk="BEFORE_UNREAD_NOT_READ", clicks=run.view.clicks + 1)
            self._core_event("CONTACT_CLICKED", task, "AUTO_PASS", point=point,
                             screen_point=task.applied_point, cursor_moved=task.cursor_moved,
                             clicked_at=task.clicked_at.isoformat(), possibly_marked_read=True)
            return self._set(status="STOPPED", reason="许可已撤销；停止前点击已记录，读取未完成")
        self._core_state(phase="CLICKED_AWAITING_READ", clicked=True, clicks=run.view.clicks + 1, waiting=len(eligible) - 1,
                         risk="CLICKED_AWAITING_READ")
        self._core_event("CONTACT_CLICKED", task, "AUTO_PASS", point=point, screen_point=task.applied_point,
                         cursor_moved=task.cursor_moved, clicked_at=task.clicked_at.isoformat(),
                         possibly_marked_read=True)
        run.navigation_task = None
        clock.complete(clicked=True)
        clock.begin(5, "核对点击后 C1 标题及 B2 目标行")
        return self._set(reason="已点击，等待重新截图核对会话")

    def _core_review_muted(self, raw):
        run = self._core
        if self.scope.muted:
            return raw, False
        now = run.clock.now()
        rows = []
        retry = False
        names = Counter(name_key(row.display_name) for row in raw.contacts)
        for row in raw.contacts:
            name = name_key(row.display_name)
            named = row.name_reliable and names[name] == 1 and row.status == "CANDIDATE"
            geometric = row.geometry_reliable and row.status == "NAME_UNCERTAIN"
            if (not row.has_unread_indicator or not (named or geometric)
                    or row.kind not in ("CONTACT", "GROUP_CANDIDATE")
                    or self.scope.exclusion(replace(row, is_muted=None))):
                rows.append(row)
                continue
            target = _Target(row, name if named else f"row-{row.sequence}", avatar_key(raw, row) if named else "", ())
            locate_mute = mute_region if row.mute_evidence else muted_icon_region
            area = locate_mute(row.box, raw.frame.state.dpi / 96)
            tile = area.crop(raw.frame.pixels)
            if min(tile.shape[:2]) < 4:
                rows.append(row)
                continue
            thumb = cv2.resize(tile, (64, 32), interpolation=cv2.INTER_AREA).astype(np.float32)
            thumb -= np.median(thumb, axis=(0, 1))
            previous = run.mute_recent.get(target.key)
            method = "CURRENT_VISUAL_OBSERVATION"
            if row.is_muted is not None and target.avatar:
                run.mute_recent[target.key] = (now, area.width, area.height, raw.frame.state.dpi, thumb, row.is_muted)
                run.mute_trials.pop(target.key, None)
            elif row.is_muted is None:
                if (row.mute_evidence != "BELL_NUMBER_CONFLICT" and target.avatar and previous and now - previous[0] <= 20
                        and previous[1:4] == (area.width, area.height, raw.frame.state.dpi)
                        and np.max(np.abs(previous[4] - thumb)) <= 12):
                    row = replace(row, is_muted=previous[5])
                    method = "RECENT_SAME_IDENTITY_UNCHANGED_ICON_AREA"
                else:
                    attempts = min(2, run.mute_trials[target.key] + 1)
                    run.mute_trials[target.key] = attempts
                    retry |= attempts < 2
                    method = "MUTE_RECHECK_LIMIT" if attempts == 2 else "MUTE_RECHECK_PENDING"
                    if attempts == 2 and run.failure_crops and target.key not in run.muted_crops:
                        run.muted_crops.add(target.key)
                        self._save_core_crop(raw.frame, area, "mute", uuid4().hex)
            self._core_event("MUTE_OBSERVATION", target_id=blake2s((self._run_id + target.key).encode(), digest_size=8).hexdigest(),
                             row=row.sequence, dpi=raw.frame.state.dpi, box=bounds(area), method=method,
                             muted=row.is_muted, retries=run.mute_trials[target.key])
            rows.append(row)
        return replace(raw, contacts=tuple(rows)), retry

    def tick(self) -> ScanStatus:
        if not self._lock.acquire(blocking=False):
            return self._status
        scan_id = ""
        try:
            if not self.active:
                return self._set(status="STOPPED", reason="自动采集许可已撤销", waiting_for_window=False)
            if self._core:
                return self._status if self._core.invalidated else self._tick_core()
            if self._prepare_pending:
                self._prepare_pending = False
                blocked = self._prepare_start()
                if blocked is not None:
                    return blocked
            self._round += 1
            scan_id = uuid4().hex[:12]
            self._set(notice=None, scan_id=scan_id, scan_disposition="B2_NOT_SCANNED",
                      screenshot_succeeded_at=None, b2_scan_attempt_at=None, b2_scan_completed_at=None,
                      scan_ocr_completed_at=None, candidates=0, contact_count=0, unread_marker_count=0,
                      reliable_unread_count=0, unreliable_unread_count=0, unreliable_reasons=(),
                      foreground_changed_stage="", navigation_block_reason="", navigation_allowed=None,
                      selection_name="", selection_name_reliable=False, selection_method="UNKNOWN", selection_confidence="UNKNOWN",
                      selection_evidence="", skipped_unread=0, skipped_reasons=(), policy_skipped_unread=0,
                      unsupported_unread=0, unknown_type_unread=0, unknown_muted_unread=0,
                      other_unreliable_unread=0, navigation_blocked_unread=0)
            if self._operations >= 12 and not self._navigation_suspended:
                reason = "连续 12 次导航未完成，已暂停本轮导航"
                if not self.runtime:
                    self.stop()
                    return self._set(status="STOPPED", reason=reason)
                self._navigation_suspended = True
                self._failure("NAVIGATION", "NAVIGATION_NO_PROGRESS", reason, level="ERROR")
            if self._window_probe is not None:
                health = self._window_probe()
                self._record_window(health)
                if not self.active:
                    return self._set(status="STOPPED", reason="自动采集许可已撤销", waiting_for_window=False)
                if health.capture_blocker:
                    if self._core and self._core.lifetime and health.status == "WINDOW_CLOSED":
                        return self._invalidate_core("BOUND_WINDOW_CLOSED")
                    if self._last_window_notice != health.capture_blocker:
                        self._failure("WINDOW", health.capture_blocker, health.reason,
                                      result="BLOCKED_BY_DEPENDENCY")
                        self._last_window_notice = health.capture_blocker
                    message = "请手动最大化企业微信" if health.capture_blocker == "NOT_MAXIMIZED" else health.reason
                    return self._wait_for_window(f"{health.capture_blocker} · {message}", invalidate=True)
            else:
                health = None
            if self._last_window_notice:
                self._record("WINDOW", "WINDOW_RECOVERED", "AUTO_PASS")
                self._last_window_notice = None
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
            if not self._core:
                self._bind_frame(frame)
            self._record("WINDOW", "FRAME_CAPTURED", level="DEBUG",
                         details={"scan": scan_id, "hwnd": frame.capture.hwnd,
                                  "height": frame.state.height, "width": frame.state.width, "dpi": frame.state.dpi})
            if self._round > 1:
                self._record("CONTINUE", "POLL_RESUMED", "AUTO_PASS", details={"scan": scan_id})
            if self._email_test_only and not self._profile_check and self.profile is not None and not self._force_email:
                return self._set(status="PAUSED", reason="独立邮箱核验已完成，可停止测试")
            if self._identity_skipped or self._navigation_suspended:
                return self._readonly_test(frame, skipped=True)
            if self._manual_hold:
                session = self._profile_check
                if session and self._lifetime(frame) != self._lifetime(session.reference.frame):
                    self._profile_check = None
                    self._manual_hold = False
                    self._set(assist="")
                else:
                    return self._readonly_test(frame, skipped=False) if self.mode == "test" else self._status
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
            self._record("B2", "B2_STARTED", level="DEBUG", details={"scan": scan_id})
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
                if health and (current.identity != health.identity or current.geometry_key != health.geometry_key):
                    return self._wait_for_window("WINDOW_CHANGED · OCR 后企业微信窗口身份或坐标变化", invalidate=True)
                elif current.status == "WINDOW_NOT_FOREGROUND":
                    stage = "截图" if health and health.status == "WINDOW_NOT_FOREGROUND" else "OCR"
                    self._set(foreground_changed_stage=self._status.foreground_changed_stage or stage)
                    navigation_window_reason = f"{current.status} · {current.reason}"
            targets = self._record_b2_result(raw, b2_attempt, blocked_reason=navigation_window_reason)
            self._record("B2", "B2_SCANNED", "AUTO_PASS", elapsed_ms=raw.recognition_ms,
                         details={"scan": scan_id, "contacts": len(raw.contacts),
                                  "unread": self._status.unread_marker_count,
                                  "reliable": self._status.reliable_unread_count,
                                  "unreliable": self._status.unreliable_unread_count,
                                  "reasons": [safe_reason(reason) for reason in self._status.unreliable_reasons]})
            self._record("SCOPE", "SCOPE_APPLIED", "AUTO_PASS", details={"excluded": self._status.skipped_unread,
                         "reasons": [safe_reason(reason) for reason in self._status.skipped_reasons]})
            if self._recover:
                self.store.recover_jobs(self.profile, self._run_id)
                self._recover = False
            if self._status.waiting_for_window:
                self._set(waiting_for_window=False, resumed_at=datetime.now().astimezone())
            return self._scan(raw, targets, navigation_window_reason)
        except AccountPaused as exc:
            self._failure("BIND", "ACCOUNT_DEPENDENCY", str(exc), result="BLOCKED_BY_DEPENDENCY")
            self._set(scan_disposition="B2_SCAN_FAILED", navigation_allowed=False, navigation_block_reason=str(exc))
            return self._set(status="PAUSED" if self.active else "STOPPED", reason=str(exc), last_failure=str(exc))
        except (CaptureError, NavigationPaused, OcrUnavailable) as exc:
            if not self.active:
                return self._set(status="STOPPED", reason="自动采集许可已撤销", waiting_for_window=False)
            state = self._window_error(exc) if not isinstance(exc, OcrUnavailable) else self._set(status="PAUSED", reason="本地 OCR 不可用")
            disposition = "B2_SCAN_FAILED" if state.scan_disposition == "B2_SCANNING" else state.scan_disposition
            self._failure("B2" if disposition == "B2_SCAN_FAILED" else "WINDOW",
                          "POLL_BLOCKED", state.reason, exception=exc)
            return self._set(last_failure=state.reason, scan_disposition=disposition, navigation_allowed=False,
                             navigation_block_reason=state.reason)
        except (sqlite3.Error, OSError, ValueError) as exc:
            reason = f"采集或存储失败（{type(exc).__name__}）"
            self._failure("SAVE", "POLL_RUNTIME_FAILURE", reason, level="ERROR", exception=exc)
            self.stop(source="failure")
            return self._set(status="FAILED", reason=reason, last_failure=reason)
        finally:
            if scan_id:
                self._scan_history = (*self._scan_history[-7:], replace(self._status, core=None))
            self._lock.release()
            self.finish_run()

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
            self._failure("NAVIGATION", "NAVIGATION_WAITING_FOR_FOREGROUND", navigation_window_reason,
                          result="BLOCKED_BY_DEPENDENCY")
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
        target_alias = blake2s((self._run_id + target.key).encode(), digest_size=8).hexdigest()
        self._set(status="VERIFYING_CONVERSATION", target=target.row.display_name,
                  reason="复核 B2 目标和当前窗口", trace=trace, trace_job=job)
        self._record("NAVIGATION", "TARGET_SCHEDULED", level="DEBUG",
                     details={"target": target_alias, "scan": self._status.scan_id})
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
                self._record("NAV_EVIDENCE", "TARGET_PRECHECK_PASSED", "AUTO_PASS",
                             details={"target": target_alias})
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
                self._record("NAVIGATION", "TARGET_CLICKED", "AUTO_PASS", details={"target": target_alias})
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
                self._failure("CONVERSATION", "CONVERSATION_NOT_CONFIRMED", reason)
                if self.runtime:
                    self.store.job(job, "UNCERTAIN", no_click=not clicked, reason=reason,
                                   navigation="NAVIGATION_UNCERTAIN", persistence="BLOCKED_BY_DEPENDENCY")
                    self._record("SAVE", "UNLINKED_OBSERVATION", "BLOCKED_BY_DEPENDENCY",
                                 details={"message_count": len(captured.messages), "reason": safe_reason(reason)})
                    result = SaveResult(None, 0, 0, 1, 0, "NAVIGATION_UNCERTAIN", reason)
                else:
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
            self._record("CONVERSATION", "CONVERSATION_VERIFIED", "AUTO_PASS", details={"target": target_alias})
            full = summarize(captured).messages_reliable
            self._record("C2", "C2_RECOGNIZED", "AUTO_PASS" if full else "PARTIAL",
                         elapsed_ms=captured.recognition_ms, details={"message_count": len(captured.messages),
                         "completeness": captured.message_completeness, "ocr_status": captured.ocr_status})
            if self.runtime and not full:
                reason = "C2 消息完整性未核对，仅保留运行观察和任务证据"
                self._failure("SAVE", "INCOMPLETE_C2", reason, result="BLOCKED_BY_DEPENDENCY")
                self.store.job(job, "UNCERTAIN", no_click=not clicked, reason=reason,
                               navigation="VERIFIED", persistence="BLOCKED_BY_DEPENDENCY")
                return self._finish_task(target, SaveResult(None, 0, 0, 1, 0, "UNCERTAIN", reason),
                                         captured, "UNCERTAIN", reason, job)
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
            self._record("SAVE", "SQLITE_TRANSACTION_STARTED")
            result = self.store.save(self.profile, captured, job_id=job)
            status = "COMPLETED" if result.conversation_id and not result.uncertain else "UNCERTAIN"
            reason = result.reason if status == "COMPLETED" else f"{result.reason}；如已点击，未读可能已清除"
            return self._finish_task(target, result, captured, status, reason, job)
        except (NavigationPaused, CaptureError) as exc:
            block_reason = str(exc).splitlines()[0]
            local = isinstance(exc, NavigationPaused) and exc.local and not clicked
            if local and self.active:
                attempts = self._target_attempts.get(job, 0) + 1
                self._target_attempts[job] = attempts
                self._failure("NAVIGATION", "TARGET_DEFERRED", block_reason)
                if self.runtime and attempts >= 3:
                    self._remember(target)
                    self._queued.pop(target.key, None)
                    self._queued_signatures.pop(target.key, None)
                    self._deferred.pop(target.key, None)
                    self._discovered.pop(job, None)
                    self._target_attempts.pop(job, None)
                    self.store.job(job, "FAILED", reason=block_reason, no_click=True, navigation="LOCAL_RETRY_LIMIT")
                else:
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
            if not local:
                self._failure("CONVERSATION" if clicked else "NAVIGATION",
                              "AFTER_CLICK_UNCONFIRMED" if clicked else "NAVIGATION_PAUSED", reason, exception=exc)
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
            reason = f"采集或数据库写入失败（{type(exc).__name__}）"
            if self.runtime and not isinstance(exc, (sqlite3.Error, OSError)) and not verified:
                self._failure("C2", "LOCAL_CAPTURE_EXCEPTION", reason, exception=exc)
                self.store.job(job, "UNCERTAIN", no_click=not clicked, reason=reason, navigation="LOCAL_FAILURE")
                return self._finish_task(target, SaveResult(None, 0, 0, 1, 0, "UNCERTAIN", reason),
                                         captured or raw, "UNCERTAIN", reason, job)
            self._failure("SAVE", "SQLITE_TRANSACTION_FAILED" if verified else "DATABASE_OR_RUNTIME_FAILURE",
                          reason, level="ERROR", exception=exc)
            self.stop(source="failure")
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
        self._target_attempts.pop(job, None)
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
