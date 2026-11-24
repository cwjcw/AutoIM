"""Transactional storage for local visual observations and provisional WeCom messages."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections import OrderedDict
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from threading import RLock
from uuid import uuid4

import cv2
import numpy as np

from autoim.wecom.collection_profile import normalize_email
from autoim.wecom.collection_events import EventTime
from autoim.wecom.visual_account import LoginEvidence
from autoim.wecom.visual_contacts import ContactObservation, name_key
from autoim.wecom.visual_layout import Box
from autoim.wecom.visual_monitor import (FrameProbe, MonitorObservation, _history_content_box, _tail_pixels_changed,
                                         compare_sequences, probe_frame, summarize)
from autoim.wecom.visual_observation import VisualObservation

SOURCE = "WINDOW_VISUAL_OCR"
SCHEMA_VERSION = 4
_MIGRATIONS = {
    1: (
        """CREATE TABLE accounts (id INTEGER PRIMARY KEY, profile TEXT NOT NULL UNIQUE,
           identity_status TEXT NOT NULL DEFAULT 'USER_SELECTED_LOCAL_PROFILE', first_seen TEXT NOT NULL)""",
        """CREATE TABLE conversations (id TEXT PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id),
           name_key TEXT NOT NULL, display_name TEXT NOT NULL, visual_title TEXT NOT NULL, kind TEXT NOT NULL,
           association_status TEXT NOT NULL, evidence TEXT NOT NULL, homonym_risk INTEGER NOT NULL DEFAULT 1,
           first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, last_status TEXT NOT NULL)""",
        "CREATE INDEX conversation_lookup ON conversations(account_id, name_key)",
        """CREATE TABLE messages (id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id),
           position INTEGER NOT NULL, direction TEXT NOT NULL, kind TEXT NOT NULL, sender_name TEXT,
           body TEXT NOT NULL, time_text TEXT, actual_time TEXT, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
           status TEXT NOT NULL, completeness TEXT NOT NULL, source TEXT NOT NULL, origin TEXT NOT NULL,
           UNIQUE(conversation_id, position))""",
        """CREATE TABLE attachments (message_id TEXT PRIMARY KEY REFERENCES messages(id), filename TEXT,
           extension TEXT, size_text TEXT, kind TEXT NOT NULL, content_available INTEGER NOT NULL DEFAULT 0
           CHECK(content_available=0))""",
        """CREATE TABLE message_observations (id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id),
           conversation_id TEXT REFERENCES conversations(id), message_id TEXT REFERENCES messages(id),
           capture_id TEXT NOT NULL, observed_at TEXT NOT NULL, kind TEXT NOT NULL, direction TEXT NOT NULL,
           raw_ocr TEXT NOT NULL, confidence REAL, bounds TEXT, status TEXT NOT NULL,
           dedup_evidence TEXT NOT NULL, reason TEXT NOT NULL)""",
        "CREATE INDEX observation_lookup ON message_observations(conversation_id, observed_at)",
        """CREATE TABLE capture_checkpoints (conversation_id TEXT PRIMARY KEY REFERENCES conversations(id),
           monitor_status TEXT NOT NULL, reliable_baseline TEXT, partial_baseline TEXT,
           last_scan TEXT NOT NULL, continuity_trusted INTEGER NOT NULL, gap INTEGER NOT NULL,
           last_result TEXT NOT NULL)""",
    ),
    2: (
        "ALTER TABLE messages ADD COLUMN quoted_sender TEXT",
        "ALTER TABLE messages ADD COLUMN quoted_content TEXT",
        "ALTER TABLE messages ADD COLUMN quoted_type TEXT",
        "ALTER TABLE messages ADD COLUMN quoted_message_id TEXT REFERENCES messages(id)",
        "ALTER TABLE messages ADD COLUMN quote_parse_status TEXT NOT NULL DEFAULT 'NOT_OBSERVED'",
        """CREATE TABLE scan_jobs (id TEXT PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id),
           run_id TEXT NOT NULL, row_key TEXT NOT NULL, display_name TEXT NOT NULL,
           candidate TEXT NOT NULL, unread_evidence TEXT NOT NULL, created_at TEXT NOT NULL,
           attempted_at TEXT, attempts INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL,
           navigation_status TEXT NOT NULL, read_risk INTEGER NOT NULL DEFAULT 0,
           capture_result TEXT NOT NULL, persistence_result TEXT NOT NULL,
           failure_reason TEXT NOT NULL, transitions TEXT NOT NULL)""",
        "CREATE INDEX scan_job_lookup ON scan_jobs(account_id, created_at)",
        "ALTER TABLE message_observations ADD COLUMN job_id TEXT REFERENCES scan_jobs(id)",
    ),
    3: (
        "ALTER TABLE accounts ADD COLUMN login_name TEXT",
        "ALTER TABLE accounts ADD COLUMN login_key TEXT",
        "ALTER TABLE accounts ADD COLUMN login_avatar TEXT",
    ),
    4: (
        "ALTER TABLE accounts ADD COLUMN email TEXT",
        "ALTER TABLE accounts ADD COLUMN email_verified_at TEXT",
        "ALTER TABLE accounts ADD COLUMN email_window TEXT",
        "CREATE UNIQUE INDEX account_email_lookup ON accounts(email) WHERE email IS NOT NULL",
    ),
}


def database_path() -> Path:
    base = Path(os.environ["LOCALAPPDATA"]) if os.environ.get("LOCALAPPDATA") else Path.home() / "AppData" / "Local"
    return base / "AutoIM" / "wecom-visual.sqlite3"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def avatar_key(observation: VisualObservation, row: ContactObservation) -> str:
    box = row.avatar
    if box is None or min(box.width, box.height) < 14:
        return ""
    safe = Box(box.left + box.width // 5, box.top + box.height * 2 // 5,
               box.right - box.width // 4, box.bottom - box.height // 8)
    crop = safe.crop(observation.frame.pixels)
    if min(crop.shape[:2]) < 5:
        return ""
    tile = cv2.resize(crop, (16, 16), interpolation=cv2.INTER_AREA)
    if float(np.max(tile.std(axis=(0, 1)))) < 9:
        return ""
    return hashlib.sha256((tile // 16).tobytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class SaveResult:
    conversation_id: str | None
    inserted: int
    repeated: int
    uncertain: int
    new_candidates: int
    outcome: str
    reason: str = ""
    committed_at: EventTime | None = None
    capture_id: str = ""


@dataclass(frozen=True, slots=True)
class LocalAccount:
    id: int
    label: str
    key: str | None
    avatar: str | None
    email: str | None = None


@dataclass(slots=True)
class _LiveBaseline:
    observation: MonitorObservation
    probe: FrameProbe
    ids: dict[int, str]


class VisualStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or database_path()
        self._lock = RLock()
        self._ready = False
        self._live: OrderedDict[str, _LiveBaseline] = OrderedDict()
        self._weak_active: tuple[tuple, str] | None = None

    @contextmanager
    def transaction(self, *, timeout=4, before_commit=None):
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.path, timeout=timeout, isolation_level=None)) as db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA foreign_keys=ON")
                if not self._ready:
                    db.execute("PRAGMA journal_mode=WAL")
                db.execute("BEGIN IMMEDIATE")
                try:
                    if not self._ready:
                        self._migrate(db)
                    yield db
                    if before_commit:
                        before_commit()
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
                else:
                    self._ready = True

    @staticmethod
    def _migrate(db: sqlite3.Connection) -> None:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise sqlite3.DatabaseError("数据库版本高于当前 AutoIM 支持的版本")
        for target in range(version + 1, SCHEMA_VERSION + 1):
            for statement in _MIGRATIONS[target]:
                db.execute(statement)
            db.execute(f"PRAGMA user_version={target}")

    @staticmethod
    def _account(db, profile: str | int) -> int:
        if isinstance(profile, int):
            if db.execute("SELECT 1 FROM accounts WHERE id=?", (profile,)).fetchone():
                return profile
            raise ValueError("本地账号绑定不存在")
        if not isinstance(profile, str):
            raise ValueError("尚未绑定本地账号")
        profile = profile.strip()
        if not profile or len(profile) > 80:
            raise ValueError("请填写不超过 80 字的本地测试账号别名")
        db.execute("INSERT OR IGNORE INTO accounts(profile, first_seen) VALUES (?,?)", (profile, _now()))
        return db.execute("SELECT id FROM accounts WHERE profile=?", (profile,)).fetchone()[0]

    @staticmethod
    def _accounts(db) -> tuple[LocalAccount, ...]:
        return tuple(LocalAccount(*row) for row in db.execute(
            "SELECT id, COALESCE(login_name, profile), login_key, login_avatar, email FROM accounts ORDER BY id"))

    def accounts(self) -> tuple[LocalAccount, ...]:
        with self.transaction() as db:
            return self._accounts(db)

    @staticmethod
    def _insert_login(db, evidence: LoginEvidence) -> int:
        return db.execute("""INSERT INTO accounts(profile, first_seen, identity_status, login_name, login_key, login_avatar)
                          VALUES (?, ?, 'VISUAL_LOCAL_BINDING', ?, ?, ?)""",
                          (f"visual:{uuid4().hex}", _now(), evidence.name, evidence.key, evidence.avatar)).lastrowid

    def locate_login(self, evidence: LoginEvidence, *, create: bool) -> tuple[int | None, tuple[LocalAccount, ...]]:
        """Resolve under the same lock/transaction that creates a first binding."""
        with self.transaction() as db:
            possible = tuple(row for row in self._accounts(db) if row.key in (None, evidence.key)
                             or (evidence.avatar and row.avatar == evidence.avatar))
            matching = [row for row in possible if evidence.avatar and row.key == evidence.key
                        and row.avatar == evidence.avatar]
            unresolved = any(row.key == evidence.key and not row.avatar for row in possible)
            if len(matching) == 1 and not unresolved:
                return matching[0].id, possible
            if not possible and create:
                return self._insert_login(db, evidence), ()
            return None, possible

    def confirm_login(self, evidence: LoginEvidence, account_id: int | None) -> int:
        with self.transaction() as db:
            if account_id is None:
                return self._insert_login(db, evidence)
            account = next((row for row in self._accounts(db) if row.id == account_id), None)
            if not account or (account.avatar and evidence.avatar != account.avatar) or (
                    account.key not in (None, evidence.key) and not account.avatar):
                raise ValueError("选定账号与当前视觉证据不一致")
            db.execute("""UPDATE accounts SET login_name=?, login_key=?, login_avatar=?,
                          identity_status='VISUAL_LOCAL_BINDING' WHERE id=?""",
                       (evidence.name, evidence.key, evidence.avatar, account_id))
            return account_id

    def verified_login(self, evidence, lifetime):
        if not evidence.key or not evidence.avatar or not lifetime or lifetime[-1] is None:
            return None
        with self.transaction() as db:
            matches = db.execute("""SELECT id, email FROM accounts WHERE email IS NOT NULL
                     AND login_key=? AND login_avatar=? AND email_window=?""",
                     (evidence.key, evidence.avatar, _json(lifetime))).fetchall()
            return tuple(matches[0]) if len(matches) == 1 else None

    def bind_email(self, evidence, email, lifetime, *, confirmed=False, account_id=None):
        email = normalize_email(email)
        if not evidence.key or not evidence.avatar or not email or not lifetime or lifetime[-1] is None:
            raise ValueError("邮箱、A1 头像或窗口生命周期核验不完整")
        with self.transaction() as db:
            accounts = self._accounts(db)
            existing = next((row for row in accounts if row.email == email), None)
            if existing:
                if confirmed and account_id != existing.id:
                    raise ValueError("邮箱已关联另一账号，历史记录保持独立")
                account = existing.id
            else:
                legacy = tuple(row for row in accounts if row.email is None and
                               (row.key in (None, evidence.key) or row.avatar == evidence.avatar))
                if legacy and not confirmed:
                    return None, legacy
                if confirmed and account_id is not None:
                    if account_id not in {row.id for row in legacy}:
                        raise ValueError("所选历史账号不具备绑定资格")
                    account = account_id
                else:
                    account = self._insert_login(db, evidence)
            db.execute("""UPDATE accounts SET login_name=?, login_key=?, login_avatar=?, email=?,
                          email_verified_at=?, email_window=?, identity_status='VISUAL_PROFILE_EMAIL'
                          WHERE id=?""", (evidence.name, evidence.key, evidence.avatar, email,
                                         _now(), _json(lifetime), account))
            return account, ()

    def break_continuity(self) -> None:
        with self._lock:
            self._weak_active = None
            self._live.clear()

    def _conversation(self, db, account: int, raw: VisualObservation, observed: MonitorObservation,
                      selected: ContactObservation | None, at: str) -> tuple[str | None, bool, tuple | None]:
        if selected is None or observed.blocker_code not in ("", "MESSAGES_UNRELIABLE"):
            return None, False, None
        name = name_key(observed.selected_name)
        if not name:
            return None, False, None
        key = avatar_key(raw, selected)
        if key and any(row is not selected and avatar_key(raw, row) == key for row in raw.contacts):
            key = ""
        kind = observed.conversation.conversation_type
        weak = (account, observed.window_key, selected.box, name, kind)
        evidence = _json({"avatar_visual": key, "kind": kind, "peer_type": selected.peer_type,
                          "labels": tuple(name_key(label) for label in selected.identity_labels),
                          "official_id_verified": False})
        known = db.execute("SELECT id, evidence FROM conversations WHERE account_id=? AND name_key=?",
                           (account, name)).fetchall()
        if not known:
            conversation = uuid4().hex
            db.execute("""INSERT INTO conversations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (
                conversation, account, name, observed.selected_name, observed.title, kind,
                "VISUAL_PROVISIONAL" if key else "SESSION_PROVISIONAL", evidence, 1, at, at, "OBSERVED"))
            return conversation, True, weak if not key else None
        matches = [row["id"] for row in known if key and row["evidence"] == evidence]
        if len(matches) == 1:
            return matches[0], False, None
        if not key and self._weak_active and self._weak_active[0] == weak:
            return self._weak_active[1], False, weak
        return None, False, None

    def save(self, profile: str | int, raw: VisualObservation, *, job_id: str | None = None,
             allow_link: bool = True, failure_reason: str = "") -> SaveResult:
        observed = summarize(raw)
        at = observed.at.isoformat(timespec="milliseconds")
        capture_id = uuid4().hex
        selected = next((row for row in raw.contacts if row.is_selected is True), raw.verified_target)
        with self._lock:
            with self.transaction() as db:
                account = self._account(db, profile)
                if job_id and not db.execute("SELECT 1 FROM scan_jobs WHERE id=? AND account_id=?",
                                             (job_id, account)).fetchone():
                    raise ValueError("巡检任务与本地账号不一致")
                conversation, created, weak = self._conversation(db, account, raw, observed, selected, at) if allow_link else (
                    None, False, None)
                live = self._live.get(conversation) if conversation else None
                unseen = conversation and not db.execute(
                    "SELECT 1 FROM messages WHERE conversation_id=? LIMIT 1", (conversation,)).fetchone()
                ids, append, outcome = self._associate(observed, raw, live, bool(created or unseen), conversation)
                reason = failure_reason or (observed.blocker if observed.blocker_code else "") or outcome
                inserted, repeated, uncertain = self._save_messages(
                    db, account, conversation, job_id, capture_id, at, raw, observed, ids, append, outcome, reason)
                new_count = inserted if outcome == "VISUAL_APPEND" else 0
                baseline = (_LiveBaseline(observed, probe_frame(raw.frame, observed.regions), ids)
                            if conversation and observed.reliable_messages else None)
                if conversation:
                    self._checkpoint(db, conversation, observed, outcome, ids, at)
                if job_id:
                    job_state = "COMPLETED" if (conversation and not uncertain and
                                outcome in ("HISTORY_BASELINE", "SAME_VISIBLE_SEQUENCE", "VISUAL_APPEND")) else "UNCERTAIN"
                    self._update_job(db, job_id, job_state, navigation="VERIFIED" if allow_link else "NAVIGATION_UNCERTAIN",
                                     capture=observed.completeness, persistence=f"{outcome}: {inserted}/{repeated}/{uncertain}",
                                     reason="" if job_state == "COMPLETED" else reason)
            committed_at = EventTime.now()
            self._weak_active = (weak, conversation) if weak is not None else None
            if baseline:
                self._live[conversation] = baseline
                self._live.move_to_end(conversation)
                if len(self._live) > 32:
                    self._live.popitem(last=False)
            elif conversation:
                self._live.pop(conversation, None)
            return SaveResult(conversation, inserted, repeated, uncertain, new_count, outcome, reason,
                              committed_at, capture_id)

    @staticmethod
    def _associate(observed, raw, live, initial, conversation):
        ids: dict[int, str] = {}
        if not conversation:
            return ids, (), "CONVERSATION_UNCERTAIN"
        if not observed.reliable_messages:
            return ids, (), "OCR_UNRELIABLE"
        if initial:
            return ids, tuple(item.message for item in observed.reliable_messages), "HISTORY_BASELINE"
        if live is None:
            return ids, (), "CONTINUITY_UNVERIFIED"
        delta = compare_sequences(live.observation, observed, cursor_in_history=raw.hovered_region == "C2")
        old, new = live.observation.reliable_messages, observed.reliable_messages
        probe = probe_frame(raw.frame, observed.regions)
        if delta.status == "NO_CHANGE" and np.array_equal(live.probe.history, probe.history) and len(old) == len(new) and all(
                a.message.box == b.message.box and a.message.kind == b.message.kind
                and a.message.direction == b.message.direction and a.message.sender_name == b.message.sender_name
                and a.message.time_evidence == b.message.time_evidence
                and a.message.text == b.message.text for a, b in zip(old, new)):
            return {b.message.sequence: live.ids[a.message.sequence] for a, b in zip(old, new)
                    if a.message.sequence in live.ids}, (), "SAME_VISIBLE_SEQUENCE"
        if delta.status == "VISUAL_NEW_CANDIDATE" and observed.regions:
            count = len(delta.appended)
            overlap = len(new) - count
            content = _history_content_box(observed.regions.history, raw.frame.state.dpi)
            if 0 < overlap <= len(old) and all(_tail_pixels_changed(
                    live.probe, probe, entry.message.box, content) for entry in delta.appended):
                links = {}
                for a, b in zip(old[-overlap:], new[:overlap]):
                    if (a.message.sender_name != b.message.sender_name
                            or a.message.direction != b.message.direction or a.message.kind != b.message.kind
                            or a.message.time_evidence != b.message.time_evidence or a.message.text != b.message.text):
                        return ids, (), "ORDER_UNCERTAIN"
                    if a.message.sequence in live.ids:
                        links[b.message.sequence] = live.ids[a.message.sequence]
                return links, tuple(item.message for item in delta.appended), "VISUAL_APPEND"
        return ids, (), "ORDER_UNCERTAIN"

    def _save_messages(self, db, account, conversation, job_id, capture_id, at, raw, observed, ids, append, outcome, reason):
        accepted = {item.sequence for item in append}
        inserted = repeated = uncertain = 0
        position = db.execute("SELECT COALESCE(MAX(position),0) FROM messages WHERE conversation_id=?",
                              (conversation,)).fetchone()[0]
        for item in raw.messages:
            message_id = ids.get(item.sequence)
            if item.sequence in accepted:
                position += 1
                message_id = ids[item.sequence] = uuid4().hex
                db.execute("""INSERT INTO messages
                    (id,conversation_id,position,direction,kind,sender_name,body,time_text,first_seen,last_seen,
                     status,completeness,source,origin) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    message_id, conversation, position, item.direction, item.kind, item.sender_name or None,
                    item.text, item.time_evidence or None, at, at, "VISUAL_CONFIRMED", observed.completeness,
                    SOURCE, outcome))
                if item.file or item.kind == "IMAGE":
                    file = item.file
                    db.execute("INSERT INTO attachments VALUES (?,?,?,?,?,0)", (
                        message_id, file.file_name if file else None, file.file_extension if file else None,
                        file.file_size_text if file else None, item.kind))
                inserted += 1
            elif message_id:
                db.execute("UPDATE messages SET last_seen=? WHERE id=?", (at, message_id))
                repeated += 1
            else:
                uncertain += 1
            payload = {"text": item.text, "sender": item.sender_name or None, "ocr": [asdict(span) for span in item.ocr_spans],
                       "file": asdict(item.file) if item.file else None, "evidence": item.evidence,
                       "time_text": item.time_evidence, "source": SOURCE}
            if item.kind == "COMPLEX_CANDIDATE":
                payload["quote"] = {"quotedSender": None, "quotedContent": None, "quotedType": None,
                                    "quotedMessageId": None, "quoteParseStatus": "UNPARSED_COMPLEX"}
            self._observation(db, account, conversation, message_id, job_id, capture_id, at, item.kind,
                              item.direction, payload, min((s.score for s in item.ocr_spans), default=None),
                              item.box, item.status if message_id else "UNCERTAIN", outcome,
                              "" if message_id else reason)
        for entry in raw.message_ocr:
            kind = {"TIME": "TIME_SEPARATOR", "SYSTEM": "SYSTEM_EVENT", "SENDER": "SENDER_LABEL",
                    "UNASSIGNED": "UNASSIGNED_OCR"}.get(entry.kind)
            if kind:
                uncertain += int(kind == "UNASSIGNED_OCR" and entry.possible_message)
                self._observation(db, account, conversation, None, job_id, capture_id, at, kind, "UNKNOWN",
                                  {"ocr": asdict(entry.span), "source": SOURCE}, entry.span.score, entry.span.box,
                                  "UNCERTAIN" if entry.possible_message else "CONTEXT", outcome, entry.reason)
        if not raw.messages or any(gap.box is None for gap in observed.gaps):
            uncertain += 1
            self._observation(db, account, conversation, None, job_id, capture_id, at, "CAPTURE", "UNKNOWN",
                              {"source": SOURCE, "completeness": observed.completeness}, None, None,
                              "UNCERTAIN", outcome, reason)
        return inserted, repeated, uncertain

    @staticmethod
    def _observation(db, account, conversation, message_id, job_id, capture_id, at, kind,
                     direction, payload, confidence, bounds, status, outcome, reason):
        db.execute("""INSERT INTO message_observations (account_id,conversation_id,message_id,job_id,capture_id,
                   observed_at,kind,direction,raw_ocr,confidence,bounds,status,dedup_evidence,reason)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (account, conversation, message_id, job_id, capture_id, at, kind, direction, _json(payload),
                    confidence, _json(asdict(bounds)) if bounds else None, status, outcome, reason))

    @staticmethod
    def _checkpoint(db, cid, observed, outcome, ids, at):
        baseline = _json([{"sequence": item.message.sequence, "message_id": ids.get(item.message.sequence)}
                          for item in observed.reliable_messages])
        trusted = bool(ids and outcome in ("HISTORY_BASELINE", "SAME_VISIBLE_SEQUENCE", "VISUAL_APPEND"))
        reliable = baseline if trusted and observed.baseline_type == "RELIABLE" else None
        partial = baseline if trusted and observed.baseline_type == "PARTIAL" else None
        db.execute("""INSERT INTO capture_checkpoints VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(conversation_id) DO UPDATE SET monitor_status=excluded.monitor_status,
                   reliable_baseline=COALESCE(excluded.reliable_baseline,reliable_baseline),
                   partial_baseline=COALESCE(excluded.partial_baseline,partial_baseline),
                   last_scan=excluded.last_scan,continuity_trusted=excluded.continuity_trusted,
                   gap=MAX(gap,excluded.gap),last_result=excluded.last_result""",
                   (cid, observed.baseline_type, reliable, partial, at, int(trusted), int(not reliable), outcome))
        db.execute("UPDATE conversations SET last_seen=?, visual_title=?, last_status=? WHERE id=?",
                   (at, observed.title, outcome, cid))

    def create_job(self, profile, run_id, row_key, row: ContactObservation, *, detected: EventTime | None = None) -> str:
        with self.transaction() as db:
            account = self._account(db, profile)
            job_id, at = uuid4().hex, _now()
            candidate = {"name": row.display_name, "kind": row.kind, "bounds": asdict(row.box),
                         "activity": row.activity_text, "selected": row.is_selected}
            unread = {"hasUnreadIndicator": row.has_unread_indicator,
                      "unreadCount": row.unread_count if row.unread_count_reliable else None,
                      "badge": asdict(row.badge) if row.badge else None, "muted": row.is_muted}
            db.execute("INSERT INTO scan_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                job_id, account, run_id, row_key, row.display_name or "未识别联系人", _json(candidate), _json(unread), at,
                None, 0, "QUEUED", "NOT_STARTED", 0, "", "", "",
                _json([{"at": detected.iso() if detected else at, "status": "DISCOVERED",
                        "detail": {"unread_detected_at": detected.iso()} if detected else None},
                       {"at": at, "status": "QUEUED"}])))
            return job_id

    def job(self, job_id: str, state: str, **details) -> None:
        with self.transaction() as db:
            self._update_job(db, job_id, state, **details)

    def job_event(self, job_id: str, status: str, at: EventTime, detail: dict) -> None:
        with self.transaction() as db:
            row = db.execute("SELECT transitions FROM scan_jobs WHERE id=?", (job_id,)).fetchone()
            changes = json.loads(row[0])
            changes.append({"at": at.iso(), "status": status, "detail": detail})
            db.execute("UPDATE scan_jobs SET transitions=? WHERE id=?", (_json(changes), job_id))

    def observe_waiting_job(self, job_id: str, row: ContactObservation, reason: str | None, scan_id: str,
                            *, no_click: bool = False) -> None:
        with self.transaction() as db:
            job = db.execute("SELECT transitions, status, failure_reason FROM scan_jobs WHERE id=?", (job_id,)).fetchone()
            if reason is None:
                if job["status"] != "WAITING_REVALIDATION":
                    return
                reason = job["failure_reason"]
            changes = json.loads(job["transitions"])
            at = _now()
            detail = {"scan_id": scan_id, "last_observed_at": at, "observations": 1,
                      "activity": row.activity_text, "bounds": asdict(row.box),
                      "unread_count": row.unread_count if row.unread_count_reliable else None}
            if changes[-1]["status"] == "WAITING_REVALIDATION":
                previous = changes.pop()
                detail["observations"] += previous["detail"]["observations"] - int(previous["detail"]["scan_id"] == scan_id)
                first = previous["at"]
            else:
                first = at
            changes.append({"at": first, "status": "WAITING_REVALIDATION", "reason": reason, "detail": detail})
            db.execute("""UPDATE scan_jobs SET status='WAITING_REVALIDATION', failure_reason=?, transitions=?,
                          read_risk=CASE WHEN ? THEN 0 ELSE read_risk END,
                          navigation_status=CASE WHEN ? THEN 'BLOCKED' ELSE navigation_status END
                          WHERE id=?""", (reason, _json(changes), no_click, no_click, job_id))

    def refresh_waiting_job(self, job_id: str, row: ContactObservation, scan_id: str) -> None:
        self.observe_waiting_job(job_id, row, None, scan_id)

    @staticmethod
    def _update_job(db, job_id, state, *, navigation="", risk=False, no_click=False,
                    capture="", persistence="", reason="", detail=None):
        row = db.execute("SELECT * FROM scan_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise ValueError("未找到已持久化巡检任务")
        at = _now()
        changes = json.loads(row["transitions"])
        matching_pause = (state == "PAUSED" and changes[-1]["status"] == state
                          and changes[-1].get("reason") == reason and changes[-1].get("detail") == detail)
        if not matching_pause:
            changes.append({"at": at, "status": state, "reason": reason, "detail": detail})
        attempt = state == "NAVIGATING"
        db.execute("""UPDATE scan_jobs SET status=?, attempted_at=?, attempts=attempts+?, navigation_status=?,
                   read_risk=?, capture_result=?, persistence_result=?, failure_reason=?, transitions=? WHERE id=?""",
                   (state, at if attempt else row["attempted_at"], int(attempt), navigation or row["navigation_status"],
                    int(not no_click and (risk or row["read_risk"])),
                    capture or row["capture_result"], persistence or row["persistence_result"],
                    reason, _json(changes), job_id))

    def recover_jobs(self, profile, run_id) -> None:
        with self.transaction() as db:
            account = self._account(db, profile)
            rows = db.execute("""SELECT id FROM scan_jobs WHERE account_id=? AND run_id<>?
                              AND status IN ('QUEUED','NAVIGATING','VERIFYING_CONVERSATION','CAPTURING','PERSISTING')""",
                              (account, run_id)).fetchall()
            for row in rows:
                self._update_job(db, row["id"], "PAUSED", reason="上次巡检中断，保留未读及可能已读的证据")

    def browse(self, profile: str | int, conversation_id: str | None = None, *, before: int | None = None) -> dict:
        with self.transaction() as db:
            account = self._account(db, profile)
            def rows(sql, params):
                return [dict(row) for row in db.execute(sql, params)]
            jobs = rows("SELECT * FROM scan_jobs WHERE account_id=? ORDER BY created_at DESC LIMIT 100", (account,))
            conversations = rows("SELECT * FROM conversations WHERE account_id=? ORDER BY last_seen DESC", (account,))
            for conversation in conversations:
                if (conversation["association_status"] == "CORE_NAME_CONFIRMED"
                        and conversation["evidence"] == "VISUAL_MULTI_EVIDENCE"):
                    conversation["last_status"] = "旧版基础姓名 · 待复核"
            messages = rows("""SELECT m.*,a.filename,a.extension,a.size_text FROM messages m
                            JOIN conversations c ON c.id=m.conversation_id LEFT JOIN attachments a ON a.message_id=m.id
                            WHERE c.account_id=? AND c.id=? AND (? IS NULL OR m.position<?)
                            ORDER BY m.position DESC LIMIT 200""", (account, conversation_id, before, before))
            errors = rows("""SELECT observed_at,kind,status,dedup_evidence,reason,raw_ocr
                          FROM message_observations WHERE account_id=? AND status='UNCERTAIN'
                          ORDER BY id DESC LIMIT 100""", (account,))
            count = db.execute("""SELECT COUNT(*) FROM messages m JOIN conversations c ON m.conversation_id=c.id
                               WHERE c.account_id=?""", (account,)).fetchone()[0]
            return {"conversations": conversations, "messages": list(reversed(messages)),
                    "jobs": jobs, "errors": errors, "message_count": count}
