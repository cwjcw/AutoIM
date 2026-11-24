"""Visible-message history for the local default account in the isolated store."""

import hashlib
import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from autoim.wecom.collection_profile import normalize_email
from autoim.wecom.visual_store import database_path, name_key
from autoim.wecom.visual_time import normalized_time, parse_message_time

SOURCE = "CORE_VISIBLE_INCREMENTAL"
FULL_NAME_EVIDENCE = "VISUAL_FULL_DISPLAY_NAME"


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
        path = path or database_path().parent / "core-default-account.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            name, email = data["name"].strip(), normalize_email(data["email"])
            if name and len(name) <= 80 and email:
                return cls(name, email)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
        return None


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


def attach_times(messages, observed, *, recent):
    results = []
    prior = last_label = stamp = None
    order = 0
    for item in messages:
        raw = item.time
        key = normalized_time(raw), item.clock_source
        if last_label != key:
            stamp = parse_message_time(raw, observed, source=item.clock_source, prior=prior, recent=recent)
            last_label, order = key, 0
            if stamp.anchor:
                prior = datetime.fromisoformat(stamp.anchor)
        order += 1
        results.append(replace(item, clock=stamp, clock_sequence=order))
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
    if old["anchor"] and new.clock.anchor:
        return _stamp(old["anchor"]) == _stamp(new.clock.anchor)
    return bool(old["anchor"] or new.clock.anchor or not old["time_text"] or not new.time
                or normalized_time(old["time_text"]) == normalized_time(new.time))


def _stamp(value):
    return datetime.fromisoformat(value) if value else None


def compare_visible(history, messages, last_anchor=None, *, gap=False):
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
            history[-1]["anchor"] and messages[alignments[0]].clock.anchor):
        alignments = []
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
        floor = max((_stamp(row["anchor"]) for row in history if row["anchor"]), default=None)
        for i in range(end, len(messages)):
            item = messages[i]
            anchor = _stamp(item.clock.anchor)
            if item.quality != "RELIABLE" or (floor and anchor and anchor < floor):
                continue
            defaults[i] = "NEW"
            if anchor and (floor is None or anchor > floor):
                floor = anchor
        return defaults, overlap, "ORDERED_TAIL"
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
    messages = attach_times(messages, captured_at, recent=analysis.latest != "UNKNOWN")
    at = captured_at.isoformat(timespec="milliseconds")
    check()
    with store.transaction(timeout=0.7, before_commit=check) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS core_message_times (
            message_id TEXT PRIMARY KEY REFERENCES messages(id), anchor TEXT, time_source TEXT NOT NULL,
            precision TEXT NOT NULL, group_sequence INTEGER NOT NULL, run_id TEXT NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS core_conversation_tail (
            conversation_id TEXT PRIMARY KEY REFERENCES conversations(id), last_anchor TEXT, gap INTEGER NOT NULL)""")
        db.execute("""INSERT OR IGNORE INTO accounts(profile, identity_status, first_seen, login_name)
            VALUES (?, 'LOCAL_DEFAULT_UNVERIFIED', ?, ?)""", (account.profile, at, account.name))
        aid = db.execute("SELECT id FROM accounts WHERE profile=? AND identity_status='LOCAL_DEFAULT_UNVERIFIED'",
                         (account.profile,)).fetchone()
        if aid is None:
            raise ValueError("默认账号记录有冲突")
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
        history = list(reversed(db.execute("""SELECT m.position,m.direction,m.kind,m.sender_name,m.body,m.time_text,t.anchor
            FROM messages m JOIN core_message_times t ON m.id=t.message_id
            WHERE m.conversation_id=? AND m.source=? ORDER BY m.position DESC LIMIT 160""", (cid, SOURCE)).fetchall()))
        historical = db.execute("""SELECT COUNT(*) FROM messages m JOIN core_message_times t ON m.id=t.message_id
            WHERE m.conversation_id=? AND m.source=?""", (cid, SOURCE)).fetchone()[0]
        state = db.execute("SELECT last_anchor,gap FROM core_conversation_tail WHERE conversation_id=?", (cid,)).fetchone()
        last_anchor, had_gap = tuple(state) if state else (None, False)
        decisions, overlap, anchor_status = compare_visible(history, messages, last_anchor, gap=had_gap)
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
            db.execute("INSERT INTO core_message_times VALUES (?,?,?,?,?,?)",
                       (mid, item.clock.anchor, item.clock.time_source, item.clock.precision, item.clock_sequence, run_id))
            if item.file or item.kind == "IMAGE":
                file = item.file
                db.execute("INSERT INTO attachments VALUES (?,?,?,?,?,0)", (mid, file.file_name if file else None,
                           file.file_extension if file else None, file.file_size_text if file else None, item.kind))
            if item.clock.anchor:
                new_anchors.append(item.clock.anchor)
        gap = bool(had_gap or analysis.gaps or "REVIEW" in decisions)
        newest = max(filter(None, (last_anchor, *new_anchors)), key=_stamp, default=None)
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
