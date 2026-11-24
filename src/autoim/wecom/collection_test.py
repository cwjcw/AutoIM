"""Monotonic deadlines and phase evidence for one core collection test."""

from dataclasses import dataclass, replace
from datetime import datetime
from threading import RLock
from time import perf_counter

PHASES = {
    1: ("企业微信窗口准备", 10),
    2: ("B2 多特征联系人扫描", 15),
    3: ("未读徽标定位及范围核对", 10),
    4: ("自动点击联系人", 10),
    5: ("C1 目标会话确认", 10),
    6: ("C2 可见消息及时间 OCR", 20),
    7: ("增量比对、隔离保存及展示", 5),
}
TEST_SECONDS = 180
TARGET_SECONDS = 60


class TestDeadline(Exception):
    def __init__(self, stage, limit):
        self.stage, self.limit = stage, limit
        super().__init__({"TEST": "本轮测试达到 180 秒", "TARGET": "联系人处理达到 60 秒",
                          "STAGE": f"{PHASES[stage][0]}超时" if stage else "阶段超时"}[limit])


@dataclass(frozen=True, slots=True)
class TestPhase:
    number: int
    started: float
    wall: str
    operation: str
    retries: int = 0


class TestClock:
    def __init__(self, emit, now=perf_counter):
        self.now = now
        self.emit = emit
        self.lock = RLock()
        self.started = now()
        self.end = self.started + TEST_SECONDS
        self.target_end = float("inf")
        self.phase = None
        self.last_phase = 0
        self.outcomes = {}
        self.finished = None
        self.reason = ""
        self.last_issue = ""
        emit("TEST_STARTED", "", {})
        emit("TEST_DEADLINE_SET", "", {"test_seconds": TEST_SECONDS, "target_seconds": TARGET_SECONDS,
                                      "stage_limits": {k: v[1] for k, v in PHASES.items()}})

    def begin_target(self):
        self.target_end = self.now() + TARGET_SECONDS

    def end_target(self):
        self.target_end = float("inf")

    def begin(self, number, operation):
        with self.lock:
            if self.phase:
                if self.phase.number == number:
                    return
                raise RuntimeError("active stage must be finished before transition")
            if self.finished is not None:
                return
            phase = TestPhase(number, self.now(), datetime.now().astimezone().isoformat(), operation)
            self.phase = phase
            self.reason = ""
            self.emit("STAGE_TRANSITION", "", {"previous_stage": self.last_phase, "next_stage": number})
            self._event("STAGE_STARTED")

    def _deadline(self):
        phase = self.phase
        stage_end = phase.started + PHASES[phase.number][1] if phase else float("inf")
        return min((self.end, "TEST"), (self.target_end, "TARGET"), (stage_end, "STAGE"), key=lambda item: item[0])

    def check(self):
        with self.lock:
            end, limit = self._deadline()
            if self.now() >= end:
                raise TestDeadline(self.phase.number if self.phase else 0, limit)
            return end

    def _event(self, code, result="", **details):
        phase = self.phase
        if phase is None:
            return
        now = self.now()
        deadline, limit = self._deadline()
        self.emit(code, result, {"stage_number": phase.number, "stage_name": PHASES[phase.number][0],
                                 "started_at": phase.wall, "elapsed_ms": round((now - phase.started) * 1000),
                                 "remaining_ms": max(0, round((deadline - now) * 1000)),
                                 "stage_limit_seconds": PHASES[phase.number][1], "active_limit": limit,
                                 "operation": phase.operation, "retries": phase.retries, **details})

    def progress(self, operation, reason="", *, retry=False):
        with self.lock:
            if self.phase:
                self.phase = replace(self.phase, operation=operation, retries=self.phase.retries + retry)
                self.reason = reason
                self._event("STAGE_RETRY" if retry else "STAGE_PROGRESS", reason=reason)

    def complete(self, status="AUTO_PASS", reason="", **details):
        with self.lock:
            if not self.phase:
                return
            code = {"AUTO_PASS": "STAGE_COMPLETED", "PARTIAL": "STAGE_PARTIAL", "FAILED": "STAGE_FAILED",
                    "TIMEOUT": "STAGE_TIMEOUT", "SKIPPED": "STAGE_SKIPPED",
                    "BLOCKED_BY_DEPENDENCY": "STAGE_BLOCKED_BY_DEPENDENCY"}[status]
            self._event(code, status, reason=reason, **details)
            self.last_phase = self.phase.number
            prior = self.outcomes.get(self.last_phase)
            if prior is None or prior == "SKIPPED":
                self.outcomes[self.last_phase] = status
            elif status not in (prior, "SKIPPED"):
                self.outcomes[self.last_phase] = "PARTIAL"
            self.reason = reason
            if reason and status in ("FAILED", "PARTIAL", "TIMEOUT", "BLOCKED_BY_DEPENDENCY"):
                self.last_issue = reason
            self.phase = None

    def finish(self, code, reason, **summary):
        with self.lock:
            if self.finished is not None:
                return
            self.complete("TIMEOUT" if code == "TEST_TIMEOUT" else "SKIPPED", reason)
            self.finished = self.now()
            self.reason = reason
            for number, (name, _) in PHASES.items():
                if number in self.outcomes:
                    continue
                status = "SKIPPED" if code in ("TEST_COMPLETED", "TEST_STOPPED") else "BLOCKED_BY_DEPENDENCY"
                self.outcomes[number] = status
                self.emit("STAGE_" + status, status, {"stage_number": number, "stage_name": name, "reason": reason})
            self.emit(code, "", {"reason": reason, "elapsed_ms": round((self.finished - self.started) * 1000)})
            self.emit("TEST_SUMMARY", "", {"reason": reason, "last_issue": self.last_issue,
                                           "stages": dict(self.outcomes), **summary})

    def snapshot(self):
        with self.lock:
            phase = self.phase
            now = self.now() if self.finished is None else self.finished
            deadline, limit = self._deadline()
            awaiting_timeout = self.finished is None and phase is not None and now >= deadline
            return {"number": phase.number if phase else self.last_phase, "operation": phase.operation if phase else "",
                    "elapsed": max(0, now - phase.started) if phase else 0,
                    "remaining": max(0, deadline - now) if phase else 0,
                    "duration": PHASES[phase.number][1] if phase else 0,
                    "total": now - self.started, "finished": self.finished is not None,
                    "reason": str(TestDeadline(phase.number, limit)) if awaiting_timeout else self.reason,
                    "awaiting_timeout": awaiting_timeout, "last_issue": self.last_issue,
                    "previous": self.last_phase, "outcomes": dict(self.outcomes)}
