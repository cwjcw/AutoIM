"""Visible-message history for the local default account in the isolated store."""

import hashlib
import json
import sqlite3
import codecs
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from autoim.wecom.collection_profile import normalize_email
from autoim.wecom.visual_store import database_path, name_key
from autoim.wecom.visual_time import normalize_time_label, parse_message_time

SOURCE = "CORE_VISIBLE_INCREMENTAL"
FULL_NAME_EVIDENCE = "VISUAL_FULL_DISPLAY_NAME"


@dataclass(frozen=True, slots=True)
class DefaultAccountStatus:
    path: Path
    account: "CoreAccount | None"
    reason: str
    encoding: str = ""


class DefaultAccountConflict(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CoreAccount:
    name: str
    email: str

    @property
    def profile(self):
        digest = hashlib.sha256(self.email.encode()).hexdigest()[:24]
        return f"CORE-LOCAL-{digest}"

    @classmethod
    def load(cls, path: Path | None = None):
        return cls.inspect(path).account

    @classmethod
    def inspect(cls, path: Path | None = None):
        path = path or database_path().parent / "core-default-account.json"
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return DefaultAccountStatus(path, None, "DEFAULT_ACCOUNT_FILE_MISSING")
        except OSError:
            return DefaultAccountStatus(path, None, "DEFAULT_ACCOUNT_READ_FAILED")
        encoding = "utf-16" if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)) else "utf-8-sig"
        try:
            data = json.loads(raw.decode(encoding))
        except UnicodeError:
            return DefaultAccountStatus(path, None, "DEFAULT_ACCOUNT_ENCODING_INVALID", encoding)
        except ValueError:
            return DefaultAccountStatus(path, None, "DEFAULT_ACCOUNT_JSON_INVALID", encoding)
        if not isinstance(data, dict):
            return DefaultAccountStatus(path, None, "DEFAULT_ACCOUNT_JSON_INVALID", encoding)
        name = data.get("name")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80 or any(ord(char) < 32 for char in name):
            return DefaultAccountStatus(path, None, "DEFAULT_ACCOUNT_NAME_INVALID", encoding)
        email = data.get("email")
        email = normalize_email(email) if isinstance(email, str) else None
        if not email:
            return DefaultAccountStatus(path, None, "DEFAULT_ACCOUNT_EMAIL_INVALID", encoding)
        return DefaultAccountStatus(path, cls(name.strip(), email), "DEFAULT_ACCOUNT_LOADED", encoding)


@dataclass(frozen=True, slots=True)
class SaveVisible:
    messages: tuple
    first: int
    new: int
    existing: int
    uncertain: int
    historical: int
    overlap: int
    anchor_status: str
    gap: bool
    anchor_updated: bool
    had_anchor: bool
    previous_anchor: str | None


def attach_times(messages, observed):
    results = []
    prior = last_label = stamp = None
    order = 0
    for item in messages:
        raw = item.time
        key = normalize_time_label(raw)[0], item.clock_source
        if last_label != key:
            stamp = parse_message_time(raw, observed, source=item.clock_source, prior=prior)
            last_label, order = key, 0
            if stamp.anchor:
                prior = datetime.fromisoformat(stamp.anchor)
        order += 1
        results.append(replace(item, clock=replace(stamp, raw=raw), clock_sequence=order if raw else 0))
    return tuple(results)


def _body(value):
    # Keep exact text semantics. Ignore only OCR's line wrapping.
    return " ".join(value.split())


def _matches(old, new):
    if (new.kind == "IMAGE" and not new.text) or new.quality != "RELIABLE":
        return False
    if (old["direction"], old["kind"], old["sender_name"] or "", _body(old["body"])) != (
            new.direction, new.kind, new.sender, _body(new.text)):
        return False
    prior, current = _stamp(old["anchor"]), _stamp(new.clock.anchor)
    if prior and current:
        return prior == current
    return bool(prior or current or not old["time_text"] or not new.time
                or normalize_time_label(old["time_text"])[0] == normalize_time_label(new.time)[0])


def _stamp(value):
    if value:
        try:
            stamp = datetime.fromisoformat(value)
            return stamp if stamp.tzinfo is not None else None
        except ValueError:
            return None
    return None


def _compatible_history(row):
    item = dict(row)
    # Legacy time evidence lacked provenance. Interpret only labels that were
    # actually associated with a group or explicit timestamp at capture time.
    if item["normalized_text"] is None and item["time_text"] and item["time_source"] in ("GROUP_ANCHOR", "EXPLICIT"):
        try:
            observed = datetime.fromisoformat(item["first_seen"])
            if observed.tzinfo is not None:
                clock = parse_message_time(item["time_text"], observed, source=item["time_source"])
                if clock.anchor:
                    item["anchor"] = clock.anchor
        except ValueError:
            pass
    return item


def _time_columns(db):
    names = {row["name"] for row in db.execute("PRAGMA table_info(core_message_times)")}
    for column, sql_type in (("normalized_text", "TEXT"), ("correction", "TEXT"), ("year_inferred", "INTEGER"),
                             ("time_issue", "TEXT"), ("date_anchor", "TEXT")):
        if column not in names:
            db.execute(f"ALTER TABLE core_message_times ADD COLUMN {column} {sql_type}")


def _coverage_gap(gap, messages, decisions):
    points = gap.get("box")
    left = max((item for item in messages if points and item.box.bottom <= points[1] and item.clock.anchor),
               key=lambda item: item.box.bottom, default=None)
    right = min((item for item in messages if points and item.box.top >= points[3] and item.clock.anchor),
                key=lambda item: item.box.top, default=None)
    anchor = lambda item: {"time": item.clock.anchor, "sequence": item.sequence} if item else None
    return {"reason": gap["reason"], "box": points, "before": anchor(left), "after": anchor(right),
            "sequence": gap.get("sequence"), "blocks_time_only_append": True,
            "decisions": [decisions.count("FIRST"), decisions.count("NEW"), decisions.count("REVIEW")]}


def compare_visible(history, messages, last_anchor=None, *, gap=False, latest=False):
    """Return per-observation decisions plus the independently aligned overlap."""
    defaults = ["REVIEW"] * len(messages)
    if not history:
        return ["FIRST" if item.quality == "RELIABLE" else "REVIEW" for item in messages], 0, "FIRST_VISIBLE"
    alignments = []
    for overlap in range(min(len(history), len(messages)), 0, -1):
        suffix = history[-overlap:]
        alignments = [start for start in range(len(messages) - overlap + 1)
                      if all(_matches(old, current) for old, current in zip(suffix, messages[start:start + overlap]))]
        if alignments:
            break
    if len(alignments) == 1 and overlap == 1 and not (
            _stamp(history[-1]["anchor"]) and _stamp(messages[alignments[0]].clock.anchor)):
        alignments = []
    counted_equal_tail = False
    if len(alignments) > 1 and latest and not gap and _stamp(history[-1]["anchor"]):
        start, stop = alignments[0], alignments[-1] + overlap
        window = messages[start:stop]
        if (len(window) > overlap and all(item.clock.anchor and _matches(history[-1], item) for item in window)):
            # The verified end of the viewport contains more occurrences of
            # one equal anchored suffix than the complete history could cover.
            alignments = [start]
            counted_equal_tail = True
    if len(alignments) == 1:
        start = alignments[0]
        end = start + overlap
        for i in range(start, end):
            defaults[i] = "EXISTING"
        prefix = history[:-overlap]
        # Unmatched content above the overlap stays pending for review.
        for i, old in zip(range(start - 1, -1, -1), reversed(prefix)):
            if not _matches(old, messages[i]):
                break
            defaults[i] = "EXISTING"
        floor = max(filter(None, (_stamp(row["anchor"]) for row in history)), default=None)
        for i in range(end, len(messages)):
            item = messages[i]
            anchor = _stamp(item.clock.anchor)
            if item.quality != "RELIABLE" or (floor and anchor and anchor < floor):
                continue
            defaults[i] = "NEW"
            if anchor and (floor is None or anchor > floor):
                floor = anchor
        return defaults, overlap, "COUNTED_EQUAL_TAIL" if counted_equal_tail else "ORDERED_TAIL"
    if len(alignments) > 1:
        return defaults, 0, "AMBIGUOUS_TAIL"
    boundary = _stamp(last_anchor)
    anchors = [_stamp(item.clock.anchor) for item in messages]
    if (not gap and boundary and anchors and all(value and value > boundary for value in anchors)
            and all(first <= second for first, second in zip(anchors, anchors[1:]))):
        return ["NEW" if item.quality == "RELIABLE" else "REVIEW" for item in messages], 0, "AFTER_TIME_ANCHOR"
    return defaults, 0, "NO_RELIABLE_ALIGNMENT"


def save_visible(store, account, analysis, conversation_kind, captured_at, run_id, check):
    messages = analysis.messages
    if (not messages or not messages[0].contact or any(
            item.contact != messages[0].contact or item.unread in ("INDEPENDENT_OCR", "ATTRIBUTION_UNCERTAIN")
            for item in messages)):
        raise ValueError("会话归属没有得到验证")
    contact = messages[0].contact
    messages = attach_times(messages, captured_at)
    at = captured_at.isoformat(timespec="milliseconds")
    check()
    with store.transaction(timeout=0.7, before_commit=check) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS core_message_times (
            message_id TEXT PRIMARY KEY REFERENCES messages(id), anchor TEXT, time_source TEXT NOT NULL,
            precision TEXT NOT NULL, group_sequence INTEGER NOT NULL, run_id TEXT NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS core_conversation_tail (
            conversation_id TEXT PRIMARY KEY REFERENCES conversations(id), last_anchor TEXT, gap INTEGER NOT NULL)""")
        _time_columns(db)
        db.execute("""INSERT OR IGNORE INTO accounts(profile, identity_status, first_seen, login_name)
            VALUES (?, 'LOCAL_DEFAULT_UNVERIFIED', ?, ?)""", (account.profile, at, account.name))
        aid = db.execute("SELECT id FROM accounts WHERE profile=? AND identity_status='LOCAL_DEFAULT_UNVERIFIED'",
                         (account.profile,)).fetchone()
        if aid is None:
            raise DefaultAccountConflict("DEFAULT_ACCOUNT_DATABASE_CONFLICT")
        aid = aid[0]
        conversations = db.execute("""SELECT id FROM conversations WHERE account_id=? AND name_key=?
            AND association_status='CORE_NAME_CONFIRMED' AND evidence=?""",
                                   (aid, name_key(contact), FULL_NAME_EVIDENCE)).fetchall()
        if len(conversations) > 1:
            raise ValueError("本地会话记录有冲突")
        cid = conversations[0][0] if conversations else uuid4().hex
        if not conversations:
            db.execute("""INSERT INTO conversations
                (id,account_id,name_key,display_name,visual_title,kind,association_status,evidence,first_seen,last_seen,last_status)
                VALUES (?,?,?,?,?,?,'CORE_NAME_CONFIRMED',?,?,?,'VISIBLE')""",
                       (cid, aid, name_key(contact), contact, contact, conversation_kind, FULL_NAME_EVIDENCE, at, at))
        history = list(reversed([_compatible_history(row) for row in db.execute("""
            SELECT m.position,m.direction,m.kind,m.sender_name,m.body,m.time_text,m.first_seen,
                   t.anchor,t.time_source,t.normalized_text
            FROM messages m JOIN core_message_times t ON m.id=t.message_id
            WHERE m.conversation_id=? AND m.source=? ORDER BY m.position DESC LIMIT 160""", (cid, SOURCE))]))
        historical = db.execute("""SELECT COUNT(*) FROM messages m JOIN core_message_times t ON m.id=t.message_id
            WHERE m.conversation_id=? AND m.source=?""", (cid, SOURCE)).fetchone()[0]
        state = db.execute("SELECT last_anchor,gap FROM core_conversation_tail WHERE conversation_id=?", (cid,)).fetchone()
        last_anchor, had_gap = tuple(state) if state else (None, False)
        decisions, overlap, anchor_status = compare_visible(
            history, messages, last_anchor, gap=had_gap, latest=analysis.latest != "UNKNOWN" and not analysis.gaps)
        position = db.execute("SELECT COALESCE(MAX(position),0) FROM messages WHERE conversation_id=?", (cid,)).fetchone()[0]
        new_anchors = []
        for item, decision in zip(messages, decisions):
            check()
            if decision == "REVIEW":
                store._observation(db, aid, cid, None, None, run_id, at, item.kind, item.direction,
                                   {"text": item.text, "time_text": item.time, "source": SOURCE}, item.confidence,
                                   item.box, "UNCERTAIN", anchor_status, item.reason or "ORDER_OR_TIME_UNCERTAIN")
            if decision not in ("FIRST", "NEW"):
                continue
            position += 1
            mid = uuid4().hex
            db.execute("""INSERT INTO messages
                (id,conversation_id,position,direction,kind,sender_name,body,time_text,actual_time,
                 first_seen,last_seen,status,completeness,source,origin)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,'VISUAL_CONFIRMED',?,?,?)""",
                       (mid, cid, position, item.direction, item.kind, item.sender or None, item.text,
                        item.time or None, item.clock.anchor if item.clock.time_source == "EXPLICIT" else None,
                        at, at, analysis.completeness, SOURCE, decision))
            db.execute("""INSERT INTO core_message_times (message_id,anchor,time_source,precision,group_sequence,
                          run_id,normalized_text,correction,year_inferred,time_issue,date_anchor)
                          VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                       (mid, item.clock.anchor, item.clock.time_source, item.clock.precision, item.clock_sequence, run_id,
                        item.clock.normalized, json.dumps(item.clock.correction), int(item.clock.year_inferred),
                        item.clock.issue, item.clock.local_date))
            if item.file or item.kind == "IMAGE":
                file = item.file
                db.execute("INSERT INTO attachments VALUES (?,?,?,?,?,0)", (mid, file.file_name if file else None,
                           file.file_extension if file else None, file.file_size_text if file else None, item.kind))
            if item.clock.anchor:
                new_anchors.append(item.clock.anchor)
        for entry in analysis.gaps:
            check()
            evidence = _coverage_gap(entry, messages, decisions)
            store._observation(db, aid, cid, None, None, run_id, at, "C2_COVERAGE_GAP", "UNKNOWN",
                               evidence, entry.get("confidence"), None, "UNCERTAIN", anchor_status, entry["reason"])
        gap = bool(had_gap or analysis.gaps or "REVIEW" in decisions)
        newest = max((value for value in (last_anchor, *new_anchors) if _stamp(value)), key=_stamp, default=None)
        ordered = all(a <= b for a, b in zip(map(_stamp, new_anchors), map(_stamp, new_anchors[1:])))
        if gap or not ordered:
            newest = last_anchor
            gap = True
        db.execute("""INSERT INTO core_conversation_tail VALUES(?,?,?)
            ON CONFLICT(conversation_id) DO UPDATE SET last_anchor=excluded.last_anchor,gap=excluded.gap""", (cid, newest, gap))
        db.execute("UPDATE conversations SET last_seen=?,last_status=? WHERE id=?", (at, anchor_status, cid))
    result = tuple(replace(item, collection_status=decision,
                           save_status="SAVED" if decision in ("FIRST", "NEW") else
                           "EXISTING" if decision == "EXISTING" else "REVIEW")
                   for item, decision in zip(messages, decisions))
    return SaveVisible(result, decisions.count("FIRST"), decisions.count("NEW"), decisions.count("EXISTING"),
                       decisions.count("REVIEW"), historical, overlap, anchor_status, gap, newest != last_anchor,
                       bool(last_anchor), last_anchor)
