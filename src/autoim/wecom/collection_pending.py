"""Durability for verified core-mode collections, independent of visual capture."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone

from autoim.runtime_paths import runtime_paths
from autoim.runtime_storage import (RuntimeDataError, backup_runtime, config_backup_sources,
                                    database_role, mark_database, protect_database)
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import uuid4

from autoim.wecom.collection_core import CoreAnalysis, CoreMessage
from autoim.wecom.collection_incremental import (CoreAccount, DefaultAccountConflict, SaveVisible, attach_times,
                                                bind_core_account, core_accounts, save_visible, validate_core_account)
from autoim.wecom.visual_files import FileCardObservation
from autoim.wecom.visual_layout import Box
from autoim.wecom.visual_ocr import TextSpan
from autoim.wecom.visual_store import name_key
from autoim.wecom.visual_time import MessageTime


class SavingBlocked(RuntimeError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def saving_reason(error):
    if isinstance(error, SavingBlocked):
        return error.reason
    if isinstance(error, DefaultAccountConflict):
        return "DEFAULT_ACCOUNT_DATABASE_CONFLICT"
    if isinstance(error, sqlite3.Error):
        code = getattr(error, "sqlite_errorname", None)
        return code or "SQLITE_UNAVAILABLE"
    if isinstance(error, RuntimeDataError):
        return str(error)
    if isinstance(error, OSError):
        return "LOCAL_IO_FAILED"
    return type(error).__name__


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _atomic_config(path, account):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(dir=path.parent, prefix=".default-account-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(_json(asdict(account)).encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        if CoreAccount.load(temporary) != account:
            raise SavingBlocked("DEFAULT_ACCOUNT_WRITE_VALIDATION_FAILED")
        current = CoreAccount.load(path)
        if current and current != account:
            raise SavingBlocked("DEFAULT_ACCOUNT_LOCAL_COPY_CONFLICT")
        os.replace(temporary, path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


class _RollbackProbe(Exception):
    pass


def probe_store(store, account, check):
    """Exercise schema, identity and a real write, with no business side effects."""
    check()
    try:
        with store.transaction(timeout=.45) as db:
            validate_core_account(db, account)
            for table in ("conversations", "messages", "message_observations"):
                db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
            db.execute("CREATE TABLE core_pending_write_probe (id INTEGER)")
            db.execute("INSERT INTO core_pending_write_probe VALUES (1)")
            check()
            raise _RollbackProbe()
    except _RollbackProbe:
        pass


class PersistentAccount:
    def __init__(self, path):
        self.path = Path(path)
        self.backup = self.path.with_name("core-default-account.backup.json")

    def _database_account(self, store):
        if not store.path.is_file():
            return None
        with closing(sqlite3.connect(store.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=.45)) as db:
            db.row_factory = sqlite3.Row
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='accounts' AND type='table'").fetchone():
                return None
            rows = core_accounts(db)
        if len(rows) > 1:
            raise SavingBlocked("DEFAULT_ACCOUNT_DATABASE_CONFLICT")
        return dict(rows[0]) if rows else None

    def ensure(self, store, check, *, supplied=None):
        check()
        paths = runtime_paths()
        primary, backup = CoreAccount.inspect(self.path), CoreAccount.inspect(self.backup)
        candidates = [(primary.account, "PRIMARY"), (backup.account, "BACKUP"), (supplied, "SUPPLIED")]
        if self.path == paths.account and not any(account for account, _ in candidates):
            candidates.extend((CoreAccount.load(file), "HISTORICAL_BACKUP") for file in config_backup_sources(paths))
        if len({(candidate.profile, name_key(candidate.name)) for candidate, _ in candidates if candidate}) > 1:
            raise SavingBlocked("DEFAULT_ACCOUNT_LOCAL_COPY_CONFLICT")
        try:
            row = self._database_account(store)
        except sqlite3.Error:
            # A verified local configuration can be restored while the database
            # is unavailable. Readiness still independently blocks navigation.
            row = None
        if row and row["email"]:
            from autoim.wecom.collection_profile import normalize_email
            email = normalize_email(row["email"])
            if not email or not row["login_name"]:
                raise SavingBlocked("DEFAULT_ACCOUNT_DATABASE_CONFLICT")
            recovered = CoreAccount(row["login_name"], email)
            if row["profile"] != recovered.profile or row["identity_status"] != "LOCAL_DEFAULT_UNVERIFIED":
                raise SavingBlocked("DEFAULT_ACCOUNT_DATABASE_CONFLICT")
            candidates.append((recovered, "ISOLATED_DATABASE"))
        populated = [(account, source) for account, source in candidates if account]
        if not populated:
            raise SavingBlocked(primary.reason)
        account, source = populated[0]
        if len({(candidate.profile, name_key(candidate.name)) for candidate, _ in populated}) != 1:
            raise SavingBlocked("DEFAULT_ACCOUNT_DATABASE_CONFLICT")
        if row and (row["profile"] != account.profile or row["identity_status"] != "LOCAL_DEFAULT_UNVERIFIED"
                    or name_key(row["login_name"] or "") != name_key(account.name)):
            raise SavingBlocked("DEFAULT_ACCOUNT_DATABASE_CONFLICT")
        changed = primary.account is None or backup.account is None
        if backup.account is None:
            check()
            _atomic_config(self.backup, account)
        if primary.account is None:
            check()
            _atomic_config(self.path, account)
        if CoreAccount.load(self.path) != account or CoreAccount.load(self.backup) != account:
            raise SavingBlocked("DEFAULT_ACCOUNT_WRITE_VALIDATION_FAILED")
        if changed and self.path == paths.account:
            backup_runtime(paths, "config", roles=set(), budget=4)
        probe_store(store, account, check)
        if row and row["email"] != account.email:
            if self.path == paths.account:
                backup_runtime(paths, "before-account-backfill", roles={"isolated"}, budget=8)
            with store.transaction(timeout=.45, before_commit=check, durable=True) as db:
                bind_core_account(db, account, datetime.now(timezone.utc).isoformat())
        return account, source

    def check(self, expected):
        current = CoreAccount.inspect(self.path)
        if current.account is None:
            raise SavingBlocked(current.reason)
        if current.account != expected or CoreAccount.load(self.backup) != expected:
            raise SavingBlocked("DEFAULT_ACCOUNT_CHANGED")


@dataclass(frozen=True, slots=True)
class PendingBatch:
    batch_id: str
    run_id: str
    detection_id: str
    task_id: str
    account_key: str
    full_display_name: str
    conversation_kind: str
    captured_at: datetime
    conversation_verification: str
    analysis: CoreAnalysis
    evidence_ids: tuple[str, ...]
    created_at: datetime
    historical_recovery: bool = False
    schema_version: int = 1

    @classmethod
    def from_observation(cls, account, analysis, kind, captured_at, verification, run_id, task_id,
                         evidence_ids=(), *, batch_id=None, historical_recovery=False):
        messages = attach_times(analysis.messages, captured_at)
        return cls(batch_id or uuid4().hex, run_id, run_id, task_id, account.profile,
                   messages[0].contact if messages else "", kind, captured_at, verification,
                   replace(analysis, messages=messages), tuple(item for item in evidence_ids if item),
                   datetime.now(timezone.utc), historical_recovery)

    def encode(self):
        data = asdict(self)
        if not self.analysis.viewport_limits:
            data["analysis"].pop("viewport_limits")
        for field in ("captured_at", "created_at"):
            data[field] = data[field].isoformat()
        return _json(data)

    @classmethod
    def decode(cls, payload):
        data = json.loads(payload)
        if data["schema_version"] != 1:
            raise ValueError("PENDING_SCHEMA_VERSION")
        for field in ("captured_at", "created_at"):
            data[field] = datetime.fromisoformat(data[field])
        analysis = data["analysis"]
        analysis["messages"] = tuple(_decode_message(msg) for msg in analysis["messages"])
        analysis["gaps"] = tuple(analysis["gaps"])
        analysis["viewport_limits"] = tuple(analysis.get("viewport_limits", ()))
        data["analysis"], data["evidence_ids"] = CoreAnalysis(**analysis), tuple(data["evidence_ids"])
        batch = cls(**data)
        batch.validate()
        return batch

    def validate(self):
        if (not re.fullmatch(r"[a-f0-9]{32}", self.batch_id) or self.schema_version != 1
                or not re.fullmatch(r"CORE-LOCAL-[a-f0-9]{24}", self.account_key)
                or not self.task_id or not self.detection_id or not self.full_display_name
                or not self.conversation_verification or self.captured_at.utcoffset() is None
                or self.created_at.utcoffset() is None or not self.analysis.messages
                or any(item.task != self.task_id or item.contact != self.full_display_name
                       or item.unread in ("INDEPENDENT_OCR", "ATTRIBUTION_UNCERTAIN")
                       for item in self.analysis.messages)):
            raise ValueError("PENDING_OBSERVATION_UNVERIFIED")


def _decode_message(data):
    data["box"] = Box(**data["box"])
    data["clock"]["correction"] = tuple(data["clock"]["correction"])
    data["clock"] = MessageTime(**data["clock"])
    data["ocr_boxes"] = tuple(tuple(box) for box in data["ocr_boxes"])
    if data["file"] is not None:
        file = data["file"]
        file["card_bounds"] = Box(**file["card_bounds"])
        for key in ("name_ocr_blocks", "icon_evidence"):
            file[key] = tuple(TextSpan(span["text"], Box(**span["box"]), span["score"], span.get("angle"))
                              for span in file[key])
        for key in ("name_rows", "failure_reasons"):
            file[key] = tuple(file[key])
        data["file"] = FileCardObservation(**file)
    return CoreMessage(**data)


@dataclass(frozen=True, slots=True)
class Recovery:
    results: tuple[tuple[str, SaveVisible], ...] = ()
    pending: int = 0
    invalid: int = 0
    blocked: str = ""


class PendingCollections:
    """A bounded independent SQLite outbox. Target receipts make replay idempotent."""

    def __init__(self, path, *, capacity=64 * 1024 * 1024, retention_days=14):
        self.path = Path(path)
        self.capacity, self.retention_days = capacity, retention_days
        self._paths = runtime_paths()
        self._managed = database_role(self._paths, self.path) == "pending"

    @contextmanager
    def _transaction(self):
        if self._managed:
            protect_database(self._paths, "pending")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=.45, isolation_level=None)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA secure_delete=ON")
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("""CREATE TABLE IF NOT EXISTS batches (
                    id TEXT PRIMARY KEY, account_key TEXT NOT NULL, captured_at TEXT NOT NULL,
                    payload TEXT NOT NULL, digest TEXT NOT NULL, size INTEGER NOT NULL,
                    status TEXT NOT NULL, retry_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT, finished_at TEXT, retry_after TEXT)""")
                yield db
                db.commit()
                if self._managed:
                    mark_database(self._paths, "pending")
            except BaseException:
                db.rollback()
                raise

    def _limit(self, db, size):
        used = db.execute("SELECT COALESCE(SUM(size),0) FROM batches").fetchone()[0]
        if used + size > self.capacity:
            raise SavingBlocked("PENDING_CAPACITY_REACHED")

    def probe(self, *, require_empty=False):
        with self._transaction() as db:
            self._limit(db, max(1, min(256 * 1024, self.capacity // 4)))
            if require_empty and db.execute(
                    "SELECT 1 FROM batches WHERE status IN ('PENDING','RETRY_WAIT') LIMIT 1").fetchone():
                raise SavingBlocked("PENDING_BATCHES_REMAIN")
            # Execute a real, committed write even when the queue already exists.
            db.execute("CREATE TABLE IF NOT EXISTS write_probe (id INTEGER PRIMARY KEY, checked_at TEXT)")
            db.execute("""INSERT INTO write_probe VALUES(1,?) ON CONFLICT(id)
                DO UPDATE SET checked_at=excluded.checked_at""", (datetime.now(timezone.utc).isoformat(),))

    def stage(self, batch):
        batch.validate()
        payload = batch.encode()
        raw = payload.encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        with self._transaction() as db:
            existing = db.execute("SELECT digest FROM batches WHERE id=?", (batch.batch_id,)).fetchone()
            if existing:
                if existing[0] != digest:
                    raise SavingBlocked("PENDING_BATCH_CONFLICT")
                return
            self._limit(db, len(raw))
            db.execute("""INSERT INTO batches(id,account_key,captured_at,payload,digest,size,status)
                VALUES(?,?,?,?,?,?,'PENDING')""", (batch.batch_id, batch.account_key,
                       batch.captured_at.astimezone(timezone.utc).isoformat(), payload, digest, len(raw)))

    def _process(self, store, account, identifier, check, *, honor_backoff):
        with self._transaction() as db:
            row = db.execute("SELECT * FROM batches WHERE id=? AND status IN ('PENDING','RETRY_WAIT')",
                             (identifier,)).fetchone()
            if not row:
                return None, ""
            if row["account_key"] != account.profile:
                return None, "PENDING_ACCOUNT_MISMATCH"
            if honor_backoff and row["retry_after"] and datetime.fromisoformat(row["retry_after"]) > datetime.now(timezone.utc):
                return None, "PENDING_RETRY_WAIT"
            try:
                raw = row["payload"].encode("utf-8")
                if hashlib.sha256(raw).hexdigest() != row["digest"]:
                    raise ValueError("PENDING_INTEGRITY_ERROR")
                batch = PendingBatch.decode(row["payload"])
                if batch.batch_id != identifier or batch.account_key != account.profile:
                    raise ValueError("PENDING_OBSERVATION_UNVERIFIED")
            except (ValueError, TypeError, KeyError, AttributeError):
                db.execute("UPDATE batches SET status='INVALID',last_error='PENDING_INVALID' WHERE id=?", (identifier,))
                return None, "PENDING_INVALID"
            check()
            try:
                result = save_visible(store, account, batch.analysis, batch.conversation_kind, batch.captured_at,
                                      batch.run_id, check, batch_id=identifier, batch_digest=row["digest"],
                                      historical_recovery=batch.historical_recovery)
            except (sqlite3.Error, OSError, ValueError, SavingBlocked) as error:
                reason = saving_reason(error)
                retries = row["retry_count"] + 1
                retry_at = datetime.now(timezone.utc) + timedelta(seconds=min(60, 2 ** min(retries, 6)))
                db.execute("""UPDATE batches SET status='RETRY_WAIT', retry_count=?, last_error=?,
                    retry_after=? WHERE id=?""", (retries, reason, retry_at.isoformat(), identifier))
                return None, reason
            # The entire payload can be released after the target commit. Review
            # observations and exact per-record decisions are now in the target.
            db.execute("""UPDATE batches SET status=?,size=0,payload='',last_error=NULL,
                retry_after=NULL,finished_at=? WHERE id=?""",
                       ("REVIEW_REQUIRED" if result.uncertain or batch.analysis.gaps else "COMMITTED",
                        datetime.now(timezone.utc).isoformat(), identifier))
            return result, ""

    def process(self, store, account, identifier, check):
        return self._process(store, account, identifier, check, honor_backoff=False)

    @staticmethod
    def _counts(db):
        pending, invalid = db.execute("""SELECT
            count(CASE WHEN status IN ('PENDING','RETRY_WAIT') THEN 1 END),
            count(CASE WHEN status='INVALID' THEN 1 END) FROM batches""").fetchone()
        return pending, invalid

    def counts(self):
        if not self.path.is_file():
            return 0, 0
        with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=.45)) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='batches' AND type='table'").fetchone():
                return 0, 0
            return self._counts(db)

    def recover(self, store, account, check, *, limit=12):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self.retention_days)).isoformat()
        with self._transaction() as db:
            # Only lightweight metadata remains in finished entries.
            db.execute("""DELETE FROM batches WHERE status IN ('COMMITTED','REVIEW_REQUIRED')
                AND size=0 AND finished_at < ?""", (cutoff,))
            rows = db.execute("""SELECT id FROM batches WHERE status IN ('PENDING','RETRY_WAIT')
                               ORDER BY captured_at,id LIMIT ?""", (limit,)).fetchall()
        results = []
        blocked = ""
        for row in rows:
            check()
            result, reason = self._process(store, account, row["id"], check, honor_backoff=True)
            if result is not None:
                results.append((row["id"], result))
            if reason and reason != "PENDING_INVALID":
                blocked = reason
                break
        pending, invalid = self.counts()
        return Recovery(tuple(results), pending, invalid, blocked or ("PENDING_BATCHES_REMAIN" if pending else ""))


class CoreDurability:
    def __init__(self, store, account_path, pending_path):
        self.store = store
        self.account = PersistentAccount(account_path)
        self.queue = PendingCollections(pending_path)
        if len({self.store.path.resolve(), self.queue.path.resolve(), self.account.path.resolve(),
                self.account.backup.resolve()}) != 4:
            raise ValueError("CORE_STORAGE_PATH_CONFLICT")

    def prepare(self, check):
        account, source = self.account.ensure(self.store, check)
        check()
        self.queue.probe()
        check()

        def verify():
            check()
            self.account.check(account)

        return account, source, self.queue.recover(self.store, account, verify)

    def before_click(self, account, check):
        self.account.check(account)
        probe_store(self.store, account, check)
        self.queue.probe(require_empty=True)
        check()
