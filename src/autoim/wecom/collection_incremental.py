"""Visible-message history for the local default account in the isolated store."""

import hashlib
import json
import sqlite3
import codecs
from contextlib import closing
from autoim.runtime_storage import backup_runtime
from dataclasses import dataclass, fields, replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from autoim.wecom.collection_profile import normalize_email
from autoim.runtime_paths import runtime_paths
from autoim.wecom.visual_store import name_key
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
        path = path or runtime_paths().account
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
    receipt_replayed: bool = False
    committed_at: str = ""

    def summary(self):
        return {field.name: getattr(self, field.name) for field in fields(self) if field.name != "messages"}


def attach_times(messages, observed):
    results = []
    prior = last_label = stamp = None
    order = 0
    for item in messages:
        raw = item.time
        captured = datetime.fromisoformat(item.observed_at) if item.observed_at else observed
        key = normalize_time_label(raw)[0], item.clock_source, captured.date()
        if last_label != key:
            stamp = parse_message_time(raw, captured, source=item.clock_source, prior=prior)
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
    if new.kind == 'IMAGE' and (not new.media_signature or new.media_signature != old.get('media_signature')):
        return False
    if new.quality != "RELIABLE":
        return False
    if (old["direction"], old["kind"], old["sender_name"] or "", _body(old["body"])) != (
            new.direction, new.kind, new.sender, _body(new.text)):
        return False
    exact, observed = _stamp(old.get("hover_anchor")), _stamp(new.hover.anchor) if new.hover and not new.hover.issue else None
    if exact and observed:
        return exact == observed
    prior, current = _stamp(old["anchor"]), _stamp(new.clock.anchor)
    if prior and current:
        return prior == current
    if new.kind in ("LINK_CARD", "CHAT_HISTORY_CARD", "UNKNOWN"):
        return False
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


def _has_table(db, table):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _history(db, cid):
    hover = _has_table(db, "core_hover_times")
    precise = "h.precise_time" if hover else "NULL"
    join = "LEFT JOIN core_hover_times h ON h.message_id=m.id" if hover else ""
    return list(reversed([_compatible_history(row) for row in db.execute(f"""
        SELECT m.id,m.position,m.direction,m.kind,m.sender_name,m.body,m.time_text,m.first_seen,
               t.anchor,t.time_source,t.normalized_text,{precise} AS hover_anchor,
               (SELECT json_extract(o.raw_ocr,'$.signature') FROM message_observations o WHERE
                o.message_id=m.id AND o.status='OBJECT_FINGERPRINT' LIMIT 1) AS media_signature
        FROM messages m JOIN core_message_times t ON m.id=t.message_id
        {join}
        WHERE m.conversation_id=? AND m.source=? ORDER BY m.position DESC LIMIT 160""", (cid, SOURCE))]))


def preview_visible(store, account, analysis, captured_at):
    messages = attach_times(analysis.messages, captured_at)
    if not store.path.is_file():
        return messages, ("FIRST",) * len(messages)
    with closing(sqlite3.connect(store.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=.25)) as db:
        db.row_factory = sqlite3.Row
        if not _has_table(db, "core_message_times"):
            return messages, ("FIRST",) * len(messages)
        rows = db.execute("""SELECT c.id FROM conversations c JOIN accounts a ON a.id=c.account_id
            WHERE a.profile=? AND c.name_key=? AND c.association_status='CORE_NAME_CONFIRMED' AND c.evidence=?""",
                          (account.profile, name_key(messages[0].contact), FULL_NAME_EVIDENCE)).fetchall()
        if len(rows) > 1:
            return messages, ("REVIEW",) * len(messages)
        if not rows:
            return messages, ("FIRST",) * len(messages)
        cid = rows[0]["id"]
        history = _history(db, cid)
        state = db.execute("SELECT last_anchor,gap FROM core_conversation_tail WHERE conversation_id=?", (cid,)).fetchone()
        anchor, gap = state if state else (None, False)
    decisions, _, _ = compare_visible(history, messages, anchor, gap=gap,
                                       latest=analysis.latest != "UNKNOWN" and not analysis.gaps,
                                       clear_tail=_clear_tail(analysis, messages))
    return messages, decisions


def _usable_hover(item):
    return item.hover and item.hover.anchor and not item.hover.issue


def _protect_hover_schema(store, check):
    paths = runtime_paths()
    if store.path != paths.isolated_db or not store.path.is_file() or not store.path.stat().st_size:
        return
    with closing(sqlite3.connect(store.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=.25)) as db:
        if _has_table(db, "core_hover_times") or not _has_table(db, "messages"):
            return
    check()
    backup_runtime(paths, "before-hover-time-schema", roles={"isolated"}, budget=3)
    check()


def _save_hover(db, message_id, item, observed):
    if _usable_hover(item) and message_id:
        db.execute("""INSERT OR IGNORE INTO core_hover_times
            (message_id,raw_time,precise_time,precision,source,year_inferred,captured_at)
            VALUES (?,?,?,?,'HOVER',?,?)""",
                   (message_id, item.hover.raw, item.hover.anchor, item.hover.precision,
                    int(item.hover.year_inferred), observed))


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


def _clear_tail(analysis, messages):
    if analysis.latest == "UNKNOWN" or any(not item.get("box") for item in analysis.gaps):
        return None
    floor = max((item["box"][3] for item in analysis.gaps), default=-1)
    return next((i for i, item in enumerate(messages) if item.box.top >= floor), None)


def _after_observed_history(history, messages, start):
    if start is None or not messages or messages[-1].unread not in ("CONFIRMED_UNREAD", "UNREAD_CANDIDATE"):
        return None
    observed = [_stamp(row.get("first_seen")) for row in history]
    if not all(observed):
        return None
    # A stored message already existed when it was observed. A trustworthy
    # current clock strictly after every stored observation independently
    # places the reliable visible suffix after those records.
    anchors = [_stamp(row.get("anchor")) for row in history]
    floor = max(stamp for stamp in (*observed, *anchors) if stamp)
    last = len(messages)
    upper = None
    while last > start:
        item = messages[last - 1]
        clock = _stamp(item.hover.anchor) if _usable_hover(item) else (
            _stamp(item.clock.anchor) if not item.clock.issue and not item.clock.year_inferred else None)
        if (item.quality != "RELIABLE" or not clock or clock <= floor or upper and clock > upper):
            break
        last -= 1
        upper = clock
    return last if last < len(messages) else None


def compare_visible(history, messages, last_anchor=None, *, gap=False, latest=False, matched=None,
                    clear_tail=None):
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
            (_stamp(history[-1]["anchor"]) and _stamp(messages[alignments[0]].clock.anchor))
            or (history[-1].get("hover_anchor") and _usable_hover(messages[alignments[0]]))):
        alignments = []
    if len(alignments) > 1:
        exact = [(sum(bool(old.get("hover_anchor") and _usable_hover(item)) for old, item in
                      zip(history[-overlap:], messages[start:start + overlap])), start) for start in alignments]
        best = max(score for score, _ in exact)
        if best:
            alignments = [start for score, start in exact if score == best]
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
        for i, old in zip(range(start, end), history[-overlap:]):
            defaults[i] = "EXISTING"
            if matched is not None:
                matched[i] = old.get("id")
        prefix = history[:-overlap]
        # Unmatched content above the overlap stays pending for review.
        for i, old in zip(range(start - 1, -1, -1), reversed(prefix)):
            if not _matches(old, messages[i]):
                break
            defaults[i] = "EXISTING"
            if matched is not None:
                matched[i] = old.get("id")
        fine_floor = max(filter(None, (_stamp(row.get("hover_anchor")) for row in history)), default=None)
        floor = max(filter(None, (_stamp(row["anchor"]) for row in history)), default=None)
        for i in range(end, len(messages)):
            item = messages[i]
            anchor = _stamp(item.clock.anchor)
            fine = _stamp(item.hover.anchor) if _usable_hover(item) else None
            if item.quality != "RELIABLE" or (floor and anchor and anchor < floor) or (fine_floor and fine and fine < fine_floor):
                continue
            defaults[i] = "NEW"
            if fine:
                fine_floor = fine
            if anchor and (floor is None or anchor > floor):
                floor = anchor
        return defaults, overlap, "COUNTED_EQUAL_TAIL" if counted_equal_tail else "ORDERED_TAIL"
    if len(alignments) > 1:
        return defaults, 0, "AMBIGUOUS_TAIL"
    fine_tail = _stamp(history[-1].get("hover_anchor"))
    fine_values = [_stamp(item.hover.anchor) if _usable_hover(item) else None for item in messages]
    if (latest and not gap and fine_tail and fine_values and all(item and item > fine_tail for item in fine_values)
            and all(a <= b for a, b in zip(fine_values, fine_values[1:]))):
        return ["NEW" if item.quality == "RELIABLE" else "REVIEW" for item in messages], 0, "AFTER_PRECISE_TAIL"
    boundary = _stamp(last_anchor)
    anchors = [_stamp(item.clock.anchor) for item in messages]
    if (not gap and boundary and anchors and all(value and value > boundary for value in anchors)
            and all(first <= second for first, second in zip(anchors, anchors[1:]))):
        return ["NEW" if item.quality == "RELIABLE" else "REVIEW" for item in messages], 0, "AFTER_TIME_ANCHOR"
    observed_tail = _after_observed_history(history, messages, clear_tail)
    if observed_tail is not None:
        defaults[observed_tail:] = ["NEW"] * (len(messages) - observed_tail)
        return defaults, 0, "AFTER_HISTORY_OBSERVATION"
    return defaults, 0, "NO_RELIABLE_ALIGNMENT"


def recovered_prefix(history, messages, decisions, complete_count):
    """Permit prepending only when the full known sequence has a unique position."""
    if not history or len(history)!=complete_count or len(messages)<=len(history):
        return 0
    starts=[i for i in range(len(messages)-len(history)+1)
            if all(_matches(old,new) for old,new in zip(history,messages[i:]))]
    if len(starts)!=1 or starts[0]==0:
        return 0
    n=starts[0]
    if (len(history)==1 and not (history[0].get('hover_anchor') and _usable_hover(messages[n]))
            or any(item.quality!='RELIABLE' for item in messages[:n])
            or any(d!='EXISTING' for d in decisions[n:n+len(history)])):
        return 0
    return n


def _decided(messages, decisions):
    return tuple(replace(item, collection_status=decision,
                         save_status="SAVED" if decision in ("FIRST", "NEW") else
                         "EXISTING" if decision == "EXISTING" else "REVIEW")
                 for item, decision in zip(messages, decisions))


def core_accounts(db):
    return db.execute("""SELECT id,profile,login_name,email,identity_status FROM accounts
        WHERE profile LIKE 'CORE-LOCAL-%' OR identity_status='LOCAL_DEFAULT_UNVERIFIED'""").fetchall()


def validate_core_account(db, account):
    rows = core_accounts(db)
    if not rows:
        return None
    row = rows[0]
    if (len(rows) != 1 or row["profile"] != account.profile or row["identity_status"] != "LOCAL_DEFAULT_UNVERIFIED"
            or name_key(row["login_name"] or "") != name_key(account.name)
            or (row["email"] and normalize_email(row["email"]) != account.email)):
        raise DefaultAccountConflict("DEFAULT_ACCOUNT_DATABASE_CONFLICT")
    return row


def bind_core_account(db, account, at):
    if normalize_email(account.email) != account.email:
        raise DefaultAccountConflict("DEFAULT_ACCOUNT_EMAIL_INVALID")
    row = validate_core_account(db, account)
    if row is not None:
        if row["email"] != account.email:
            db.execute("UPDATE accounts SET email=? WHERE id=?", (account.email, row["id"]))
        return row["id"]
    return db.execute("""INSERT INTO accounts(profile,identity_status,first_seen,login_name,email)
            VALUES (?, 'LOCAL_DEFAULT_UNVERIFIED', ?, ?, ?)""",
                      (account.profile, at, account.name, account.email)).lastrowid


def save_visible(store, account, analysis, conversation_kind, captured_at, run_id, check, *,
                 batch_id="", batch_digest="", historical_recovery=False, review_id=None):
    messages = analysis.messages
    if (not messages or not messages[0].contact or any(
            item.contact != messages[0].contact or item.unread in ("INDEPENDENT_OCR", "ATTRIBUTION_UNCERTAIN")
            for item in messages)):
        raise ValueError("会话归属没有得到验证")
    contact = messages[0].contact
    messages = attach_times(messages, captured_at)
    at = captured_at.isoformat(timespec="milliseconds")
    check()
    use_hover = any(_usable_hover(item) for item in messages)
    if use_hover:
        _protect_hover_schema(store, check)
    with store.transaction(timeout=0.7, before_commit=check, durable=bool(batch_id)) as db:
        if batch_id:
            db.execute("""CREATE TABLE IF NOT EXISTS core_commit_receipts (
                batch_id TEXT PRIMARY KEY, digest TEXT NOT NULL, account_key TEXT NOT NULL, captured_at TEXT NOT NULL,
                committed_at TEXT NOT NULL, decisions TEXT NOT NULL, summary TEXT NOT NULL)""")
            prior = db.execute("SELECT * FROM core_commit_receipts WHERE batch_id=?", (batch_id,)).fetchone()
            if prior:
                if prior["account_key"] != account.profile or prior["digest"] != batch_digest:
                    raise ValueError("PENDING_BATCH_RECEIPT_CONFLICT")
                decisions = json.loads(prior["decisions"])
                if len(decisions) != len(messages):
                    raise ValueError("PENDING_BATCH_RECEIPT_INVALID")
                summary = json.loads(prior["summary"])
                return SaveVisible(_decided(messages, decisions), **{**summary, "receipt_replayed": True})
        db.execute("""CREATE TABLE IF NOT EXISTS core_message_times (
            message_id TEXT PRIMARY KEY REFERENCES messages(id), anchor TEXT, time_source TEXT NOT NULL,
            precision TEXT NOT NULL, group_sequence INTEGER NOT NULL, run_id TEXT NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS core_conversation_tail (
            conversation_id TEXT PRIMARY KEY REFERENCES conversations(id), last_anchor TEXT, gap INTEGER NOT NULL)""")
        _time_columns(db)
        if use_hover:
            db.execute("""CREATE TABLE IF NOT EXISTS core_hover_times (
                message_id TEXT PRIMARY KEY REFERENCES messages(id), raw_time TEXT NOT NULL,
                precise_time TEXT NOT NULL, precision TEXT NOT NULL, source TEXT NOT NULL,
                year_inferred INTEGER NOT NULL, captured_at TEXT NOT NULL)""")
        aid = bind_core_account(db, account, at)
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
        history = _history(db, cid)
        historical = db.execute("""SELECT COUNT(*) FROM messages m JOIN core_message_times t ON m.id=t.message_id
            WHERE m.conversation_id=? AND m.source=?""", (cid, SOURCE)).fetchone()[0]
        state = db.execute("SELECT last_anchor,gap FROM core_conversation_tail WHERE conversation_id=?", (cid,)).fetchone()
        last_anchor, had_gap = tuple(state) if state else (None, False)
        matched = {}
        decisions, overlap, anchor_status = compare_visible(
            history, messages, last_anchor, gap=had_gap, latest=analysis.latest != "UNKNOWN" and not analysis.gaps,
            matched=matched, clear_tail=_clear_tail(analysis, messages))
        decisions = ["REVIEW" if item.hover and item.hover.issue else decision
                     for item, decision in zip(messages, decisions)]
        # A delayed observation can confirm already-saved content. It cannot
        # insert an older item after the latest position in an existing history.
        previous_seen = db.execute("SELECT last_seen FROM conversations WHERE id=?", (cid,)).fetchone()[0]
        delayed = bool((historical_recovery or batch_id) and _stamp(previous_seen)
                       and _stamp(previous_seen) > captured_at)
        if delayed:
            if "FIRST" in decisions or "NEW" in decisions:
                decisions = ["REVIEW" if item in ("FIRST", "NEW") else item for item in decisions]
                anchor_status = "OLDER_CAPTURE_REQUIRES_REVIEW"
        prefix_count=0
        if not delayed and analysis.completeness=='ACCESSIBLE_HISTORY_CONTIGUOUS' and not analysis.gaps:
            prefix_count=recovered_prefix(history,messages,decisions,historical)
            if prefix_count:
                decisions[:prefix_count]=['NEW']*prefix_count
        if review_id is not None:
            review = db.execute("""SELECT raw_ocr FROM message_observations
                WHERE id=? AND account_id=? AND conversation_id=? AND capture_id=? AND observed_at=?
                AND kind=? AND direction=? AND status='UNCERTAIN' AND message_id IS NULL
                AND dedup_evidence='NO_RELIABLE_ALIGNMENT'""",
                (review_id, aid, cid, run_id, at, messages[0].kind, messages[0].direction)).fetchone()
            if (not historical_recovery or len(messages) != 1 or not review
                    or json.loads(review[0]).get("text") != messages[0].text
                    or decisions[0] not in ("NEW", "EXISTING")):
                raise ValueError("HISTORICAL_REVIEW_NOT_RESOLVABLE")
        position = db.execute("SELECT COALESCE(MAX(position),0) FROM messages WHERE conversation_id=?", (cid,)).fetchone()[0]
        first_position = (db.execute('SELECT min(position) FROM messages WHERE conversation_id=?',(cid,)).fetchone()[0]
                          if prefix_count else 0)
        new_anchors = []
        for index, (item, decision) in enumerate(zip(messages, decisions)):
            check()
            item_at = datetime.fromisoformat(item.observed_at).isoformat(timespec='milliseconds') if item.observed_at else at
            if decision == "EXISTING" and use_hover and not delayed:
                _save_hover(db, matched.get(index), item, item_at)
            if review_id is not None and decision == "EXISTING":
                if not matched.get(index):
                    raise ValueError("HISTORICAL_REVIEW_NOT_RESOLVABLE")
                db.execute("UPDATE message_observations SET status='RESOLVED',message_id=? WHERE id=?",
                           (matched[index], review_id))
            if decision == "REVIEW":
                store._observation(db, aid, cid, None, None, run_id, at, item.kind, item.direction,
                                   {"text": item.text, "time_text": item.time, "source": SOURCE,
                                    "hover": {"raw": item.hover.raw, "candidate": item.hover.anchor,
                                              "issue": item.hover.issue} if item.hover else None}, item.confidence,
                                   item.box, "UNCERTAIN", anchor_status, (item.hover.issue if item.hover else "") or item.reason or "ORDER_OR_TIME_UNCERTAIN")
            if decision not in ("FIRST", "NEW"):
                continue
            if index<prefix_count:
                insert_position=first_position-prefix_count+index
            else:
                position+=1
                insert_position=position
            mid = uuid4().hex
            db.execute("""INSERT INTO messages
                (id,conversation_id,position,direction,kind,sender_name,body,time_text,actual_time,
                 first_seen,last_seen,status,completeness,source,origin)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,'VISUAL_CONFIRMED',?,?,?)""",
                       (mid, cid, insert_position, item.direction, item.kind, item.sender or None, item.text,
                        item.time or None, item.clock.anchor if item.clock.time_source == "EXPLICIT" else None,
                        item_at, item_at, analysis.completeness, SOURCE, decision))
            db.execute("""INSERT INTO core_message_times (message_id,anchor,time_source,precision,group_sequence,
                          run_id,normalized_text,correction,year_inferred,time_issue,date_anchor)
                          VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                       (mid, item.clock.anchor, item.clock.time_source, item.clock.precision, item.clock_sequence, run_id,
                        item.clock.normalized, json.dumps(item.clock.correction), int(item.clock.year_inferred),
                        item.clock.issue, item.clock.local_date))
            if review_id is not None:
                db.execute("UPDATE message_observations SET status='RESOLVED',message_id=? WHERE id=?", (mid, review_id))
            if use_hover:
                _save_hover(db, mid, item, item_at)
            if item.media_signature:
                store._observation(db,aid,cid,mid,None,run_id,item_at,item.kind,item.direction,
                                   {'signature':item.media_signature},item.confidence,item.box,
                                   'OBJECT_FINGERPRINT','VISIBLE_IMAGE','SCREENSHOT_ONLY')
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
        if not delayed:
            db.execute("""INSERT INTO core_conversation_tail VALUES(?,?,?)
                ON CONFLICT(conversation_id) DO UPDATE SET last_anchor=excluded.last_anchor,gap=excluded.gap""",
                       (cid, newest, gap))
            db.execute("UPDATE conversations SET last_seen=?,last_status=? WHERE id=?", (at, anchor_status, cid))
        committed_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        result = SaveVisible(_decided(messages, decisions), decisions.count("FIRST"), decisions.count("NEW"),
                             decisions.count("EXISTING"), decisions.count("REVIEW"), historical, overlap,
                             anchor_status, gap, newest != last_anchor, bool(last_anchor), last_anchor,
                             committed_at=committed_at)
        if batch_id:
            db.execute("INSERT INTO core_commit_receipts VALUES (?,?,?,?,?,?,?)",
                       (batch_id, batch_digest, account.profile, at, committed_at,
                        json.dumps(decisions), json.dumps(result.summary())))
    return result
