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
from autoim.wecom.visual_store import database_path

STAGES = {
    "WINDOW": "恢复、最大化并验证企业微信前台",
    "A1": "可靠识别登录姓名及头像",
    "MENU": "点击 A1 并验证个人菜单",
    "CARD": "点击菜单头像并验证个人资料卡",
    "EMAIL": "两次独立截图取得一致的有效邮箱",
    "RESTORE": "资料层消失且消息页恢复",
    "BIND": "确认正确的本地账号归属",
    "B2": "扫描联系人及未读",
    "SCOPE": "应用单聊、群聊、静音和折叠规则",
    "NAVIGATION": "点击目标未读联系人",
    "CONVERSATION": "验证会话身份",
    "C2": "识别并核对可见消息",
    "SAVE": "SQLite 去重及提交",
    "NOTIFY": "向 Windows 提交通知",
    "CONTINUE": "处理后续联系人或继续巡检",
    "STOP": "撤销授权并停止 Worker",
}
RESULTS = {"AUTO_PASS", "MANUAL_ASSISTED", "PARTIAL", "FAILED", "SKIPPED",
           "BLOCKED_BY_DEPENDENCY", "NOT_RUN"}
_EMAIL = re.compile(r"[^\s<>]+@[^\s<>]+\.[^\s<>]+")
_SECRET = re.compile(r"(?i)\b(token|api[_-]?key|password|secret)\s*[:=]\s*\S+")
_MANAGED = re.compile(r"(?:autoim\.log|events\.jsonl)(?:\.\d+)?$")


def safe_reason(value):
    return _SECRET.sub(r"\1=[隐去]", _EMAIL.sub("[邮箱已隐去]", str(value)))[:1500]


def git_version():
    try:
        root = Path(__file__).resolve().parents[3]
        result = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], cwd=root,
                                capture_output=True, text=True, timeout=2, check=True,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


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
        self.root = Path(root) if root is not None else database_path().parent / "logs"
        self.session = uuid4().hex
        self.revision = revision or git_version()
        self.queue = SimpleQueue()
        self.handler = QueueHandler(self.queue)
        self.files = _EventFiles(self.root, max_bytes, backups, retention)
        self.listener = _Listener(self.queue, self.files)
        self.listener.failure = ""
        self.listener.start()
        self.lock = RLock()
        self.closed = False
        self.reports = {}

    def emit(self, mode, detection, stage, code, result="", *, level="INFO", task="",
             elapsed_ms=None, details=None, exception=None):
        from traceback import extract_tb
        with self.lock:
            if self.closed or (mode == "production" and level == "DEBUG" and result not in RESULTS):
                return ""
            details = dict(details or {})
            if "reason" in details:
                details["reason"] = safe_reason(details["reason"])
            event = {"id": uuid4().hex, "time": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                     "app_version": __version__, "pc_version": "PC-V3.3", "git": self.revision,
                     "session": self.session, "detection": detection, "task": task, "mode": mode,
                     "stage": stage, "code": code, "result": result, "level": level, "elapsed_ms": elapsed_ms,
                     "details": details}
            if exception is not None:
                event["exception"] = {"type": type(exception).__name__,
                                      "frames": [(Path(frame.filename).name, frame.lineno, frame.name)
                                                 for frame in extract_tb(exception.__traceback__)]}
            if detection and stage in STAGES and result in RESULTS:
                self._record_stage(event)
            record = logging.LogRecord("autoim.collection", getattr(logging, level), "", 0, code, (), None)
            record.event = event
            self.handler.handle(record)
            return event["id"]

    def _record_stage(self, event):
        report = self.reports.setdefault(event["detection"], {})
        stage = report.setdefault(event["stage"], {"attempts": 0, "duration_ms": 0, "counts": {}, "events": []})
        stage["attempts"] += 1
        stage["duration_ms"] += event["elapsed_ms"] or 0
        counts = stage["counts"]
        result = event["result"]
        counts[result] = counts.get(result, 0) + 1
        stage["events"] = (stage["events"] + [event["id"]])[-40:]
        stage["actual"] = event["code"]
        if result in {"FAILED", "BLOCKED_BY_DEPENDENCY", "PARTIAL"}:
            stage["reason"] = event["details"].get("reason", event["code"])

    def report(self, detection):
        with self.lock:
            source = self.reports.get(detection, {})
            stages = {}
            for key, expected in STAGES.items():
                data = source.get(key, {})
                counts = data.get("counts", {})
                status = next((status for status in ("MANUAL_ASSISTED", "PARTIAL", "AUTO_PASS", "FAILED",
                                                      "BLOCKED_BY_DEPENDENCY", "SKIPPED") if status in counts),
                              "NOT_RUN")
                if counts.get("AUTO_PASS") and counts.get("FAILED") and status == "AUTO_PASS":
                    status = "PARTIAL"
                stages[key] = {"attempts": 0, "duration_ms": 0, "actual": "", "reason": "",
                               **data, "expected": expected, "status": status, "counts": dict(counts),
                               "events": list(data.get("events", ()))}
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
                    stages[key].update(status="BLOCKED_BY_DEPENDENCY", reason=f"前置阶段 {upstream} 未通过")
            return {"version": "PC-V3.3", "git": self.revision, "session": self.session,
                    "detection": detection, "stages": stages}

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

    def export(self, path, scope, detection, snapshot):
        from shutil import copyfileobj
        path = Path(path)
        created = False
        try:
            with TemporaryFile() as readable, TemporaryFile() as structured:
                for event in self.events(scope, detection):
                    readable.write((_human(event) + "\n").encode("utf-8"))
                    structured.write((json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8"))
                with path.open("xb") as destination:
                    created = True
                    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
                        for name, stream in (("autoim.log", readable), ("events.jsonl", structured)):
                            stream.seek(0)
                            with archive.open(name, "w", force_zip64=True) as entry:
                                copyfileobj(stream, entry)
                        archive.writestr("environment.json", json.dumps({"platform": platform.system(),
                                          "release": platform.release(), "python": platform.python_version(),
                                          "app_version": __version__, "pc_version": "PC-V3.3", "git": self.revision}))
                        archive.writestr("diagnostic.json", json.dumps(snapshot, ensure_ascii=False, indent=2))
                        archive.writestr("flow-report.json", json.dumps(self.report(detection), ensure_ascii=False, indent=2))
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
