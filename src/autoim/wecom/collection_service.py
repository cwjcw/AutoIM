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
    account_state: str = "UNIDENTIFIED"
    account_reason: str = ""
    account_at: EventTime | None = None
    account_id: int | None = None
    account_choices: tuple[LocalAccount, ...] = ()
    recent: CollectionTrace = CollectionTrace()
    trace: CollectionTrace = CollectionTrace()
    last_success: CollectionTrace | None = None
    navigation_result: str = ""
    ocr_result: str = ""
    sqlite_result: str = ""
    last_failure: str = ""
    notice: SaveNotice | None = None
    trace_job: str = ""


@dataclass(frozen=True, slots=True)
class _Target:
    row: ContactObservation
    name: str
    avatar: str
    ambiguous: bool

    @property
    def key(self) -> str:
        return f"{self.name}|{self.avatar}|{self.row.box.top}" if self.ambiguous else f"{self.name}|{self.avatar}"

    @property
    def unread_signature(self) -> tuple:
        row = self.row
        return (row.unread_count if row.unread_count_reliable else None, row.activity_text, row.preview)


def unread_targets(observation: VisualObservation) -> tuple[_Target, ...]:
    """B2 is the source, including muted rows and undifferentiated red dots."""
    if observation.page != "MESSAGES_CANDIDATE" or observation.layout.get("B2") is None:
        return ()
    names = Counter(name_key(row.display_name) for row in observation.contacts if row.display_name)
    return tuple(_Target(row, name_key(row.display_name), avatar_key(observation, row),
                         names[name_key(row.display_name)] != 1 or not row.name_reliable
                         or row.status != "CANDIDATE" or row.is_selected is None or row.badge is None)
                 for row in observation.contacts if row.has_unread_indicator)


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
    return (len(rows) == 1 and rows[0].is_selected is True and rows[0].name_reliable
            and rows[0].status == "CANDIDATE" and conv.match_status == "VISUAL_MATCH"
            and conv.title_name_reliable and conv.selected_name_reliable and name_key(conv.title) == target.name
            and (not target.avatar or avatar_key(observation, rows[0]) == target.avatar)
            and rows[0].peer_type == target.row.peer_type
            and rows[0].identity_labels == target.row.identity_labels
            and (target.row.kind != "GROUP_CANDIDATE" or conv.conversation_type == "GROUP")
            and observation.layout.get("C2") is not None)


class AccountPaused(NavigationPaused):
    """The login evidence must be rechecked before storing messages."""


class CollectionService:
    def __init__(self, profile: str | None, store: VisualStore, reader: CollectionReader, *,
                 capture=None, navigator=None, permit: CollectionPermit | None = None, window_probe=None) -> None:
        self.profile = profile
        self._auto_account = profile is None
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
        self._window = None
        self._window_plan_invalidated = False
        self._scroll = False
        self._queued: dict[str, str] = {}
        self._discovered: dict[str, EventTime] = {}
        self._handled: dict[str, tuple] = {}
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
        self._queued.clear()
        self._discovered.clear()
        self._handled.clear()
        self._operations = 0
        previous = self._status
        self._status = ScanStatus("RUNNING", "请手动切回最大化的企业微信，等待空闲后巡检",
                                  account_state=previous.account_state, account_name=previous.account_name,
                                  account_at=previous.account_at, account_id=previous.account_id,
                                  account_choices=previous.account_choices, account_reason=previous.account_reason)
        self._cancel.clear()

    def stop(self) -> None:
        self._cancel.set()
        self.permit.revoke()
        self._set(status="STOPPED", reason="自动采集许可已撤销", waiting_for_window=False)

    @property
    def active(self) -> bool:
        return not self._cancel.is_set()

    def snapshot(self) -> ScanStatus:
        return self._status

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
        self._set(account_state=state, account_id=None, account_reason=reason,
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

    def _accept_login(self, account, evidence, lifetime):
        if self._bound_login and self._bound_login[0] != account:
            self._set(saved=0, added=0, repeated=0, uncertain=0, completed=0, errors=0, candidates=0,
                      pages=1, last_scan=None, recent=CollectionTrace(), trace=CollectionTrace(),
                      trace_job="", last_success=None, target="", navigation_result="", ocr_result="",
                      sqlite_result="", notice=None, match="UNKNOWN")
        self.profile = account
        self._bound_login = (account, evidence, lifetime)
        self._requires_confirmation = False
        self._pending_login = None
        self._set(account_id=account, account_name=evidence.name, account_state="BOUND",
                  account_reason="本地视觉绑定，官方账号 ID 未验证", account_choices=())

    def confirm_account(self, account_id: int | None) -> ScanStatus:
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
        self._handled.clear()
        self.store.break_continuity()
        self._window_plan_invalidated = True

    def _wait_for_window(self, reason: str, *, invalidate: bool = False) -> ScanStatus:
        if invalidate:
            self._invalidate_window_plan("窗口或坐标系失去连续性，旧未执行任务保留供核对")
        return self._set(status="PAUSED", reason=reason, waiting_for_window=True,
                         last_failure=reason, target="", candidates=0, queued=len(self._queued), match="UNKNOWN")

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
                    return self._set(status="READY", reason="当前账号已绑定")
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
        try:
            if not self.active:
                return self._set(status="STOPPED", reason="自动采集许可已撤销", waiting_for_window=False)
            self._set(notice=None)
            if self._operations >= 48:
                self.stop()
                return self._set(status="STOPPED", reason="本轮已达到 48 次导航操作上限")
            if self._window_probe is not None:
                health = self._window_probe()
                self._record_window(health)
                if not self.active:
                    return self._set(status="STOPPED", reason="自动采集许可已撤销", waiting_for_window=False)
                if health.capture_blocker:
                    message = "请手动最大化企业微信" if health.capture_blocker == "NOT_MAXIMIZED" else health.reason
                    return self._wait_for_window(f"{health.capture_blocker} · {message}", invalidate=True)
                if health.status == "WINDOW_NOT_FOREGROUND":
                    return self._wait_for_window(f"{health.status} · {health.reason}，等待用户切回企业微信")
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
            if captured and captured.status == "WINDOW_NOT_FOREGROUND":
                return self._wait_for_window(f"{captured.status} · 截图期间失去前台，等待用户切回企业微信")
            self._bind_frame(frame)
            if self._auto_account and self._status.account_state == "UNIDENTIFIED":
                self._set(account_state="IDENTIFYING", account_reason="正在读取 A1")
            self._set(trace=CollectionTrace(), trace_job="")
            raw = self.read(frame)
            ocr_at = self._ocr_completed(raw)
            self._set(match=raw.conversation.match_status)
            if not self.active:
                return self._set(status="STOPPED", reason="已停止，未执行导航")
            self._check_login(raw)
            self.navigator.verify_view(raw, need_idle=True)
            if raw.page != "MESSAGES_CANDIDATE":
                raise NavigationPaused("未确认当前企业微信为消息页面")
            if raw.layout.get("B2") is None or any(step.status == "FAILED" for step in raw.ocr_areas
                                                    if step.region.startswith("B2")):
                raise NavigationPaused("B2 联系人扫描失败")
            self._set(last_scan=ocr_at.wall)
            if self._recover:
                self.store.recover_jobs(self.profile, self._run_id)
                self._recover = False
            if self._status.waiting_for_window:
                self._set(waiting_for_window=False, resumed_at=datetime.now().astimezone())
            return self._scan(raw)
        except AccountPaused as exc:
            return self._set(status="PAUSED" if self.active else "STOPPED", reason=str(exc), last_failure=str(exc))
        except (CaptureError, NavigationPaused) as exc:
            if not self.active:
                return self._set(status="STOPPED", reason="自动采集许可已撤销", waiting_for_window=False)
            state = self._window_error(exc)
            return self._set(last_failure=state.reason)
        except (sqlite3.Error, OSError, ValueError) as exc:
            self.stop()
            reason = f"采集或存储失败（{type(exc).__name__}）"
            return self._set(status="FAILED", reason=reason, last_failure=reason)
        finally:
            self._lock.release()

    def _scan(self, raw):
        current = unread_targets(raw)
        still_visible = {f"{name_key(row.display_name)}|{avatar_key(raw, row)}" for row in raw.contacts
                         if row.name_reliable and not row.has_unread_indicator}
        for key in still_visible:
            self._handled.pop(key, None)
        eligible = {target.key: target for target in current[:24]}
        for key in tuple(self._queued):
            if key not in eligible:
                job = self._queued.pop(key)
                self._discovered.pop(job, None)
                self.store.job(job, "PAUSED", reason="联系人行或未读证据已变化，等待新观察")
        for key, target in eligible.items():
            if key in self._queued or self._handled.get(key) == target.unread_signature:
                continue
            if not self.active:
                return self._set(status="STOPPED", reason="已停止，未执行导航")
            detected = self._event("unread_detected_at")
            job = self.store.create_job(self.profile, self._run_id, key, target.row, detected=detected)
            if target.ambiguous:
                self.store.job(job, "UNCERTAIN", reason="同名或未读联系人行无法可靠验证")
                self._remember(target)
                self._set(errors=self._status.errors + 1)
            else:
                self._queued[key] = job
                self._discovered[job] = detected
        self._set(candidates=len(current), queued=len(self._queued),
                  saved=self.store.browse(self.profile)["message_count"])
        if self._queued:
            key = next(iter(self._queued))
            return self._collect(raw, eligible[key], self._queued[key])
        if self._scroll and self._status.pages < 4:
            return self._scroll_once(raw)
        return self._set(status="RUNNING", target="", reason="当前可见 B2 中没有等待的未读任务")

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
            at = self._ocr_completed(observed)
            self.store.job_event(job, "OCR_COMPLETED", at,
                                 {"ocr_completed_at": at.iso(), "summary": self._status.ocr_result})
            return observed

        try:
            if not self.active:
                raise NavigationPaused("已停止，未执行导航")
            if not target.row.is_selected:
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
            selected = next(row for row in captured.contacts if row.is_selected is True)
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
            self.store.job(job, "UNCERTAIN" if clicked else "PAUSED", no_click=not clicked,
                           navigation="NAVIGATION_UNCERTAIN" if clicked else "BLOCKED", reason=str(exc))
            if clicked or not self.active:
                self._queued.pop(target.key, None)
                self._discovered.pop(job, None)
            if clicked:
                self._remember(target)
            if self.active and (isinstance(exc, CaptureError) or exc.health is not None):
                self._window_error(exc)
            if not self.active:
                return self._set(status="STOPPED", reason="自动采集许可已撤销", waiting_for_window=False,
                                 queued=len(self._queued), errors=self._status.errors + int(clicked))
            reason = str(exc).splitlines()[0] + ("；未读可能已清除" if clicked else "")
            return self._set(status="PAUSED", reason=reason, navigation_result=reason, last_failure=reason,
                             queued=len(self._queued), errors=self._status.errors + int(clicked))
        except Exception as exc:
            self.stop()
            reason = f"采集或数据库写入失败（{type(exc).__name__}）"
            try:
                self.store.job(job, "FAILED", navigation="VERIFIED" if verified else "NAVIGATION_UNCERTAIN",
                               reason=reason)
            except (sqlite3.Error, OSError):
                pass  # The pre-action committed job remains available after restart.
            self._queued.pop(target.key, None)
            self._discovered.pop(job, None)
            return self._set(status="FAILED", reason=reason + ("；未读可能已清除" if attempted else ""),
                             last_failure=reason, sqlite_result=reason if verified else self._status.sqlite_result,
                             queued=len(self._queued), errors=self._status.errors + 1)

    def _verify_capture(self, raw, observed, target):
        if not self.active:
            raise NavigationPaused("已停止，未保存本次采集")
        self._check_login(observed, allow_binding=False)
        self.navigator.verify_view(observed)
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
        self._discovered.pop(job, None)
        self._remember(target)
        self._committed(result, raw.conversation.title, job)
        return self._numbers(result, status=status, reason=reason, match=raw.conversation.match_status,
                             saved=self._status.saved + result.inserted, queued=len(self._queued),
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
