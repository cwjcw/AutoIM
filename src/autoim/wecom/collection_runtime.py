"""Private structured collection events, report and export."""
from __future__ import annotations

import json
import logging
import platform
import re
import subprocess
import zipfile
from datetime import date, datetime, timedelta
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from pathlib import Path
from queue import SimpleQueue
from threading import RLock
from tempfile import TemporaryFile
from uuid import uuid4

from autoim import __version__
from autoim.wecom.collection_evidence import EvidenceArchive, EvidenceJob, write_evidence
from autoim.wecom.collection_test import PHASES
from autoim.runtime_paths import project_root, runtime_paths

from autoim.version import PC_VERSION

STAGES = {
    "WINDOW": "恢复、最大化并验证企业微信前台",
    "A1": "可靠识别登录姓名及头像",
    "MENU": "点击 A1 并验证个人菜单",
    "CARD": "点击菜单头像并验证个人资料卡",
    "EMAIL": "两次独立截图取得一致的有效邮箱",
    "RESTORE": "资料层消失且消息页恢复",
    "BIND": "确认正确的本地账号归属",
    "A2": "双击消息入口定位当前视口以外的未读",
    "B2": "扫描联系人及未读",
    "SCOPE": "应用单聊、群聊、静音和折叠规则",
    "NAVIGATION": "点击目标未读联系人",
    "CONVERSATION": "验证会话身份",
    "C2": "识别并核对可见消息",
    "SAVE": "SQLite 去重及提交",
    "NOTIFY": "向 Windows 提交通知",
    "CONTINUE": "处理后续联系人或继续巡检",
    "STOP": "撤销授权并停止 Worker",
    "TEST_C1": "独立读取当前测试会话标题",
    "TEST_C2": "独立识别可见消息",
    "TEST_NAVIGATION": "仅导航到明确授权的隔离测试目标",
    "TEST_CONVERSATION": "独立核实授权测试会话",
    "TEST_SAVE": "隔离数据库提交与去重",
    "TEST_NOTIFY": "提交 TEST 通知及人工可见确认",
    "CORE": "按当前采集范围发现、导航、读取并展示测试消息",
    **{f"TEST_STAGE_{number}": name for number, (name, _) in PHASES.items()},
}
SUBSTEPS = {
    "WINDOW_LOCATED": ("WINDOW_LOCATE", "定位主窗口"),
    "WINDOW_RESTORE": ("WINDOW_RESTORE", "恢复最小化窗口"),
    "WINDOW_MAXIMIZE": ("WINDOW_MAXIMIZE", "最大化窗口"),
    "WINDOW_ACTIVATE": ("WINDOW_ACTIVATE", "设置前台"),
    "WINDOW_PREPARED": ("WINDOW_FINAL", "最终 Win32 状态核验"),
    "A1_NAME_OBSERVED": ("A1_NAME", "识别 A1 姓名"),
    "A1_AVATAR_OBSERVED": ("A1_AVATAR", "识别 A1 头像"),
    "A1_CLICKED": ("A1_CLICK", "第一次头像点击"),
    "POPUP_CAPTURED": ("MENU_SCREENSHOT", "截取个人菜单"),
    "CARD_POPUP_CAPTURED": ("CARD_SCREENSHOT", "截取详细资料卡"),
    "TEST_NOTIFICATION_API_CALL": ("TEST_NOTICE_API", "调用 TEST 通知 API"),
    "TEST_WINDOWS_NOTIFICATION": ("TEST_NOTICE_RESULT", "取得 TEST 通知 API 结果"),
    "TEST_NOTIFICATION_SEEN": ("TEST_NOTICE_SEEN", "人工确认看到 TEST 通知"),
    "MENU_VERIFIED": ("MENU_VERIFY", "菜单视觉验证"),
    "MENU_AVATAR_LOCATED": ("AVATAR_LOCATE", "定位菜单头像"),
    "MENU_AVATAR_CLICKED": ("MENU_CLICK", "第二次头像点击"),
    "CARD_WINDOW_ADMITTED": ("CARD_RELATION", "发现资料卡并验证窗口关系"),
    "PROFILE_CARD_VERIFIED": ("CARD_VERIFY", "资料卡视觉确认"),
    "FIRST_EMAIL_READ": ("EMAIL_FIRST", "第一次邮箱读取"),
    "SECOND_EMAIL_READ": ("EMAIL_SECOND", "第二次邮箱读取"),
    "EMAIL_COMPARE": ("EMAIL_COMPARE", "邮箱一致性核验"),
    "CARD_CLOSE_CLICKED": ("CARD_CLOSE", "资料卡关闭动作"),
    "MENU_CLOSE_CLICKED": ("MENU_CLOSE", "菜单关闭动作"),
    "PROFILE_LAYERS_GONE": ("LAYERS_GONE", "确认资料层消失"),
    "CHAT_VIEW_RESTORED": ("CHAT_RESTORE", "消息布局及 A1 恢复"),
    "ACCOUNT_BOUND": ("ACCOUNT_BOUND", "本地账号绑定"),
    "CONTACT_SCOPE_OBSERVED": ("CONTACT_CLASSIFY", "逐行会话分类观察"),
    "B2_SCANNED": ("B2_SCAN", "识别 B2 和未读"),
    "SCOPE_APPLIED": ("SCOPE_FILTER", "范围过滤"),
    "TARGET_PRECHECK_PASSED": ("NAV_PRECHECK", "点击前复核"),
    "TARGET_CLICKED": ("NAV_CLICK", "实际点击"),
    "CONVERSATION_VERIFIED": ("C1_VERIFY", "点击后会话确认"),
    "C2_RECOGNIZED": ("C2_OCR", "消息 OCR 与解析"),
    "SQLITE_COMMIT": ("SQLITE_COMMIT", "事务提交与去重"),
    "WINDOWS_NOTIFICATION": ("NOTICE_SUBMIT", "系统通知 API 结果"),
    "POLL_RESUMED": ("POLL_CONTINUE", "持续巡检"),
    "TARGET_IN_SCOPE": ("CORE_SCOPE", "核对当前采集范围"),
    "NAVIGATION_PRECHECK": ("CORE_PRECHECK", "导航前安全校验"),
    "CONTACT_CLICKED": ("CORE_CLICK", "实际点击授权目标"),
    "LATEST_POSITION_CHECKED": ("CORE_LATEST", "核查 C2 最新位置"),
    "MESSAGE_OCR_COMPLETED": ("CORE_OCR", "C2 逐条 OCR"),
    "UNREAD_RANGE_CHECKED": ("CORE_UNREAD_RANGE", "核查未读范围"),
    "RESULT_DISPLAYED": ("CORE_DISPLAY", "结果进入 GUI"),
    "NEXT_TARGET": ("CORE_CONTINUE", "处理后续授权目标"),
    "STOP_REQUESTED": ("STOP_REQUEST", "收到停止指令"),
    "STOP_PERMISSION_REVOKED": ("STOP_PERMIT", "撤销许可"),
    "STOP_SCHEDULING": ("STOP_SCHEDULE", "停止调度"),
    "WORKER_DRAINED": ("STOP_WORKER", "Worker 退出"),
    "STOP_NO_NEW_INPUT": ("STOP_INPUT", "已审核停止后的输入事件"),
}
RESULTS = {"AUTO_PASS", "MANUAL_ASSISTED", "PARTIAL", "FAILED", "SKIPPED",
           "BLOCKED_BY_DEPENDENCY", "NOT_RUN"}
_EMAIL = re.compile(r"[^\s<>]+@[^\s<>]+\.[^\s<>]+")
_SECRET = re.compile(r"(?i)\b(token|api[_-]?key|password|secret)\s*[:=]\s*\S+")
_PHONE = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")
_MANAGED = re.compile(r"(?:autoim\.log|events\.jsonl)(?:\.\d+)?$")


def safe_reason(value):
    return _PHONE.sub("[号码已隐去]", _SECRET.sub(r"\1=[隐去]", _EMAIL.sub("[邮箱已隐去]", str(value))))[:1500]


def repository_state(root):
    def git(*args):
        return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=2, check=True,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()

    try:
        head = git("rev-parse", "HEAD")
        dirty = bool(git("status", "--porcelain", "--untracked-files=normal"))
        source_dirty = bool(git("status", "--porcelain", "--untracked-files=normal", "--",
                                "src", "pyproject.toml", "uv.lock"))
        if not re.fullmatch(r"[0-9a-f]{40}", head) or git("rev-parse", "HEAD") != head:
            return {"head": None, "dirty": None, "source_dirty": None, "source": "git", "reason": "HEAD_UNSTABLE"}
        return {"head": head, "dirty": dirty, "source_dirty": source_dirty, "source": "git", "reason": ""}
    except (OSError, subprocess.SubprocessError) as error:
        return {"head": None, "dirty": None, "source_dirty": None, "source": "git", "reason": type(error).__name__}


def git_version():
    return repository_state(project_root())["head"] or "unknown"


def _human(event):
    correlation = " ".join(str(event[k]) for k in ("detection", "task") if event[k])
    context = dict(event["details"])
    if event.get("exception"):
        context["exception"] = event["exception"]
    details = json.dumps(context, ensure_ascii=False, sort_keys=True)
    return (f"{event['time']} {event['level']} {event['id']} {correlation} {event['stage']} "
            f"{event['code']} {event['result']} {details}").strip()


class _EventFiles(logging.Handler):
    def __init__(self, root, max_bytes, backups, retention):
        super().__init__()
        self.root = root
        self.max_bytes = max_bytes
        self.backups = backups
        self.retention = retention
        self.day = ""
        self.files = ()
        self.io_lock = RLock()

    def emit(self, record):
        with self.io_lock:
            self._write(record.event, record.levelno)

    def _write(self, event, level):
        today = event["time"][:10]
        if today != self.day:
            self.close_files()
            directory = self.root / today
            directory.mkdir(parents=True, exist_ok=True)
            self.files = tuple(RotatingFileHandler(directory / name, maxBytes=self.max_bytes,
                                                  backupCount=self.backups, encoding="utf-8")
                               for name in ("autoim.log", "events.jsonl"))
            self.day = today
            self.prune(date.fromisoformat(today))
        for handler, line in zip(self.files, (_human(event), json.dumps(event, ensure_ascii=False))):
            handler.handle(logging.LogRecord("collection", level, "", 0, "%s", (line,), None))

    def prune(self, today):
        for folder in self.root.iterdir():
            if folder.is_symlink() or folder.is_junction() or not folder.is_dir():
                continue
            try:
                expired = date.fromisoformat(folder.name) <= today - timedelta(days=self.retention)
            except ValueError:
                continue
            if expired:
                for path in folder.iterdir():
                    if (_MANAGED.fullmatch(path.name) and path.is_file() and not path.is_symlink()
                            and path.resolve().is_relative_to(self.root.resolve())):
                        try:
                            path.unlink()
                        except OSError:
                            pass

    def close_files(self):
        for handler in self.files:
            handler.close()
        self.files = ()


class _Listener(QueueListener):
    def handle(self, record):
        if isinstance(record, EvidenceJob):
            try:
                record.callback(*record.args)
            except Exception:
                self.failure = "私有视觉证据写入失败"
            finally:
                record.slot.release()
            return
        if isinstance(record, tuple):
            record[0].set()
            return
        try:
            super().handle(record)
        except OSError:
            # A full/unavailable filesystem must not terminate the worker.
            self.failure = "运行日志目录不可写"


class CollectionRuntime:
    def __init__(self, root=None, *, max_bytes=20 * 1024 * 1024, backups=5, retention=30, revision=None):
        paths = runtime_paths()
        self.root = Path(root) if root is not None else paths.logs
        self.session = uuid4().hex
        git = repository_state(paths.project) if revision is None else {
            "head": revision, "dirty": None, "source_dirty": None, "source": "injected", "reason": ""}
        self.revision = git["head"] or "unknown"
        self.execution = {"app_version": __version__, "pc_version": PC_VERSION,
                          "started_at": datetime.now().astimezone().isoformat(),
                          "source_path": str(Path(__file__).resolve()), "project": str(paths.project),
                          "runtime": str(paths.root), "startup_git": git}
        self.queue = SimpleQueue()
        self.handler = QueueHandler(self.queue)
        self.files = _EventFiles(self.root, max_bytes, backups, retention)
        self.evidence = EvidenceArchive(self.root / "visual-evidence" if root is not None else paths.evidence, retention)
        self.listener = _Listener(self.queue, self.files)
        self.listener.failure = ""
        self.listener.start()
        self.lock = RLock()
        self.closed = False
        self.reports = {}
        self.steps = {}
        self.scopes = {}
        self.stop_audit = {}

    def keep_visual(self, detection, task, scan, kind, raw, data, regions, *, source_event=""):
        with self.lock:
            if self.closed:
                return ""
            identifier = uuid4().hex
            start = self.evidence.slots.acquire(blocking=False)
            event = self.emit("test", detection, "VISUAL_DIAGNOSTIC", "VISUAL_EVIDENCE", task=task,
                              details={"evidence_id": identifier, "kind": kind, "scan": scan,
                                       "source_event": source_event,
                                       "reason": "" if start else "LOCAL_EVIDENCE_QUEUE_FULL"})
            if start:
                frame = raw.frame
                record = {"id": identifier, "event": event, "source_event": source_event,
                          "detection": detection, "task": task, "scan": scan, "kind": kind,
                          "captured_at": frame.captured_at.isoformat(), "dpi": frame.state.dpi,
                          "size": [frame.capture.width, frame.capture.height],
                          "coordinates": "screen_physical_crop" if kind == "hover" else "original_window_pixels", "data": data}
                self.queue.put(EvidenceJob(self.evidence.keep, (record, frame.pixels, regions), self.evidence.slots))
            return identifier

    def emit(self, mode, detection, stage, code, result="", *, level="INFO", task="",
             elapsed_ms=None, details=None, exception=None):
        from traceback import extract_tb
        with self.lock:
            if self.closed or (mode == "production" and level == "DEBUG" and result not in RESULTS):
                return ""
            details = dict(details or {})
            for key in ("reason", "last_issue"):
                if key in details:
                    details[key] = safe_reason(details[key])
            event = {"id": uuid4().hex, "time": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                     "app_version": __version__, "pc_version": PC_VERSION, "git": self.revision, "execution": self.execution,
                     "session": self.session, "detection": detection, "task": task, "mode": mode,
                     "stage": stage, "code": code, "result": result, "level": level, "elapsed_ms": elapsed_ms,
                     "details": details}
            if exception is not None:
                event["exception"] = {"type": type(exception).__name__,
                                      "frames": [(Path(frame.filename).name, frame.lineno, frame.name)
                                                 for frame in extract_tb(exception.__traceback__)]}
            if detection:
                self._track(event, self.reports, self.steps, self.scopes, self.stop_audit)
            record = logging.LogRecord("autoim.collection", getattr(logging, level), "", 0, code, (), None)
            record.event = event
            self.handler.handle(record)
            return event["id"]

    @classmethod
    def _track(cls, event, reports, steps, scopes, stop_audit):
        detection, stage, code, result = (event[key] for key in ("detection", "stage", "code", "result"))
        scope = event["details"].get("evidence_scope")
        if scope:
            scopes.setdefault(detection, set()).add(scope)
        if stage in STAGES and result in RESULTS:
            cls._record_stage(reports, detection, stage, event, result)
        if code in SUBSTEPS:
            name, _ = SUBSTEPS[code]
            assertion = result if result in RESULTS else "PARTIAL"
            cls._record_stage(steps, detection, name, event, assertion)
        audit = stop_audit.setdefault(detection, {"requested": False, "drained": False, "late_input": 0})
        if code == "STOP_PERMISSION_REVOKED":
            audit["requested"] = True
        elif code == "WORKER_DRAINED":
            audit["drained"] = True
        elif code in ("INPUT_EXECUTED", "CURSOR_MOVED") and audit["requested"]:
            audit["late_input"] += 1

    @staticmethod
    def _record_stage(index, detection, key, event, result):
        stage = index.setdefault(detection, {}).setdefault(key, {
            "attempts": 0, "duration_ms": 0, "counts": {}, "events": [],
            "recovered_failures": 0, "unresolved_blockers": 0})
        stage["attempts"] += 1
        stage["duration_ms"] += event.get("elapsed_ms") or 0
        counts = stage["counts"]
        counts[result] = counts.get(result, 0) + 1
        stage["events"] = (stage["events"] + [event["id"]])[-40:]
        stage["actual"] = event["code"]
        stage["last_attempt_status"] = result
        if result in {"FAILED", "BLOCKED_BY_DEPENDENCY"}:
            stage["unresolved_blockers"] += 1
            stage["reason"] = event["details"].get("reason", event["code"])
        elif result in {"AUTO_PASS", "MANUAL_ASSISTED"}:
            stage["recovered_failures"] += stage["unresolved_blockers"]
            stage["unresolved_blockers"] = 0
            stage["reason"] = ""
            stage["completed_at"] = event["time"]
        elif result == "PARTIAL":
            stage["reason"] = event["details"].get("reason", event["code"])

    @staticmethod
    def _summaries(expected, source):
        formatted = {}
        for key, expectation in expected.items():
            data = source.get(key, {})
            counts = data.get("counts", {})
            historical = next((status for status in ("MANUAL_ASSISTED", "AUTO_PASS", "PARTIAL", "FAILED",
                                                     "BLOCKED_BY_DEPENDENCY", "SKIPPED") if status in counts),
                              "NOT_RUN")
            final = data.get("last_attempt_status", "NOT_RUN")
            formatted[key] = {"attempts": 0, "duration_ms": 0, "actual": "", "reason": "", "completed_at": None,
                              "last_attempt_status": "NOT_RUN", "recovered_failures": 0, "unresolved_blockers": 0,
                              **data, "expected": expectation, "status": final, "final_status": final,
                              "verification_status": historical, "manual_assisted": bool(counts.get("MANUAL_ASSISTED")),
                              "completed": final in {"AUTO_PASS", "MANUAL_ASSISTED"},
                              "waiting": final == "BLOCKED_BY_DEPENDENCY",
                              "counts": dict(counts), "events": list(data.get("events", ()))}
        return formatted

    def report(self, detection, *, recorded=None):
        with self.lock:
            reports, steps, scopes, audits = recorded if recorded is not None else (
                self.reports, self.steps, self.scopes, self.stop_audit)
            stages = self._summaries(STAGES, reports.get(detection, {}))
            substeps = self._summaries(dict(SUBSTEPS.values()), steps.get(detection, {}))
            blockers = {
                "MENU": ("WINDOW", "A1"), "CARD": ("MENU", "A1"), "EMAIL": ("CARD",),
                "RESTORE": ("CARD",), "BIND": ("EMAIL", "RESTORE"), "B2": ("WINDOW",),
                "SCOPE": ("B2",), "NAVIGATION": ("BIND", "B2"), "CONVERSATION": ("NAVIGATION",),
                "C2": ("CONVERSATION",), "SAVE": ("BIND", "C2"), "NOTIFY": ("SAVE",),
            }
            for key, dependencies in blockers.items():
                upstream = next((name for name in dependencies if stages[name]["status"] in
                                 ("FAILED", "BLOCKED_BY_DEPENDENCY")), "")
                if upstream and stages[key]["status"] == "NOT_RUN":
                    stages[key].update(status="BLOCKED_BY_DEPENDENCY", final_status="BLOCKED_BY_DEPENDENCY",
                                       waiting=True, unresolved_blockers=1, reason=f"前置阶段 {upstream} 未通过")
            audit = audits.get(detection, {})
            stop = stages["STOP"]
            if audit.get("requested") and audit.get("drained"):
                outcome = "FAILED" if audit["late_input"] else "AUTO_PASS"
                substeps["STOP_INPUT"].update(status=outcome, final_status=outcome, completed=outcome == "AUTO_PASS",
                                              attempts=1, actual="STOP_INPUT_AUDIT",
                                              events=substeps["STOP_PERMIT"]["events"] + substeps["STOP_WORKER"]["events"])
            if audit.get("late_input", 0):
                stop.update(status="FAILED", final_status="FAILED", completed=False,
                            reason="撤销许可后出现输入事件")
            elif audit.get("requested") and not audit.get("drained"):
                stop.update(status="PARTIAL", final_status="PARTIAL", completed=False, waiting=True,
                            reason="已撤销许可，等待 Worker 退出")
            elif audit.get("requested") and audit.get("drained"):
                stop_steps = ("STOP_REQUEST", "STOP_PERMIT", "STOP_SCHEDULE", "STOP_WORKER", "STOP_INPUT")
                passed = all(substeps[name]["status"] == "AUTO_PASS" for name in stop_steps)
                outcome = "AUTO_PASS" if passed else "PARTIAL"
                stop.update(status=outcome, final_status=outcome, completed=passed, waiting=False,
                            reason="" if passed else "停止审计缺少必要子步骤证据")
            return {"version": PC_VERSION, "git": self.revision, "session": self.session,
                    "execution": self.execution, "detection": detection, "evidence_scopes": sorted(scopes.get(detection, ())),
                    "stop_audit": dict(audit), "stages": stages, "substeps": substeps}

    def sync(self):
        from threading import Event
        with self.lock:
            if self.closed:
                return
            done = Event()
            self.queue.put((done,))
        if not done.wait(5):
            raise OSError("运行日志写入尚未完成")

    def events(self, scope="session", detection=""):
        self.sync()
        with self.files.io_lock:
            yield from self._read_events(scope, detection)

    def _read_events(self, scope, detection):
        today = datetime.now().astimezone().date().isoformat()
        for directory in sorted(self.root.glob("????-??-??")):
            if directory.is_symlink() or directory.is_junction() or (scope == "today" and directory.name != today):
                continue
            def sequence(path):
                return int(path.suffix[1:]) if path.suffix[1:].isdigit() else 0
            for path in sorted(directory.glob("events.jsonl*"), key=sequence, reverse=True):
                if path.is_symlink() or not path.is_file() or not _MANAGED.fullmatch(path.name):
                    continue
                with path.open(encoding="utf-8") as stream:
                    for line in stream:
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        required = {"session", "detection", "time", "id", "level", "stage", "code", "result", "task", "details"}
                        if not isinstance(event, dict) or not required <= event.keys() or not isinstance(event["details"], dict):
                            continue
                        if scope in {"session", "detection"} and event["session"] != self.session:
                            continue
                        if scope == "detection" and event["detection"] != detection:
                            continue
                        yield event

    def export(self, path, scope, detection, snapshot, *, full_visual=False):
        from shutil import copyfileobj
        path = Path(path)
        created = False
        try:
            with TemporaryFile() as readable, TemporaryFile() as structured:
                selected = []
                event_count = 0
                for event in self.events(scope, detection):
                    event_count += 1
                    if not full_visual and "config_path" in event["details"]:
                        event = {**event, "details": {**event["details"], "config_path": "本机 AutoIM 默认账号配置"}}
                    if event["detection"]:
                        selected.append(event)
                    readable.write((_human(event) + "\n").encode("utf-8"))
                    structured.write((json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8"))
                exported = repository_state(Path(self.execution["project"]))
                with path.open("xb") as destination:
                    created = True
                    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
                        for name, stream in (("autoim.log", readable), ("events.jsonl", structured)):
                            stream.seek(0)
                            with archive.open(name, "w", force_zip64=True) as entry:
                                copyfileobj(stream, entry)
                        archive.writestr("environment.json", json.dumps({"platform": platform.system(),
                                          "release": platform.release(), "python": platform.python_version(),
                                          "app_version": __version__, "pc_version": PC_VERSION, "git": self.revision,
                                          "execution": self.execution, "export_git": exported}))
                        recorded = ({}, {}, {}, {})
                        for event in selected:
                            self._track(event, *recorded)
                        report = self.report(detection, recorded=recorded)
                        versions = {event["detection"]: event for event in selected}
                        report["export_git"] = exported
                        report["detections"] = {
                            run: {**self.report(run, recorded=recorded), "version": event.get("pc_version", "unknown"),
                                  "git": event.get("git", "unknown"), "execution": event.get("execution")}
                            for run, event in versions.items()}
                        archive.writestr("flow-report.json", json.dumps(report, ensure_ascii=False, indent=2))
                        visual_count = write_evidence(archive, self.evidence, selected, report, full=full_visual)
                        summary = {"scope": scope, "includes_detection": bool(versions),
                                   "detection_count": len(versions),
                                   "latest_detection_at": max((event["time"] for event in versions.values()), default=None),
                                   "version": PC_VERSION, "event_count": event_count,
                                   "visual_evidence_count": visual_count, "full_visual": full_visual}
                        archive.writestr("diagnostic.json", json.dumps({**snapshot, "execution": self.execution,
                                         "export_git": exported, "export": summary}, ensure_ascii=False, indent=2))
                return summary
        except BaseException:
            if created:
                path.unlink(missing_ok=True)
            raise

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
        self.listener.stop()
        self.files.close_files()
