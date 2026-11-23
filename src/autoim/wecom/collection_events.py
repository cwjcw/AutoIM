"""Event times shared by collection, SQLite commits and diagnostics."""

from dataclasses import dataclass, fields
from datetime import datetime
from time import monotonic


@dataclass(frozen=True, slots=True)
class EventTime:
    wall: datetime
    monotonic: float

    @classmethod
    def now(cls):
        return cls(datetime.now().astimezone(), monotonic())

    def iso(self):
        return self.wall.isoformat(timespec="milliseconds")


@dataclass(frozen=True, slots=True)
class CollectionTrace:
    unread_detected_at: EventTime | None = None
    navigation_started_at: EventTime | None = None
    conversation_opened_at: EventTime | None = None
    ocr_completed_at: EventTime | None = None
    sqlite_committed_at: EventTime | None = None

    def durations(self):
        pairs = (
            ("unread_to_open_ms", self.unread_detected_at, self.conversation_opened_at),
            ("open_to_ocr_ms", self.conversation_opened_at, self.ocr_completed_at),
            ("ocr_to_commit_ms", self.ocr_completed_at, self.sqlite_committed_at),
            ("unread_to_commit_ms", self.unread_detected_at, self.sqlite_committed_at),
        )
        return {key: round((end.monotonic - start.monotonic) * 1000)
                for key, start, end in pairs if start and end and end.monotonic >= start.monotonic}

    def detail(self):
        stamps = {field.name: at.iso() for field in fields(self) if (at := getattr(self, field.name))}
        return {**stamps, **self.durations()}


@dataclass(frozen=True, slots=True)
class SaveNotice:
    id: str
    title: str
    count: int
    trace: CollectionTrace
    job_id: str | None = None
