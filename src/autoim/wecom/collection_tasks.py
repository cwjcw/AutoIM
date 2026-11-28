"""Durable navigation intents and page observations in the private pending database."""

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class CaptureTasks:
    def __init__(self, queue, destination, account):
        self.queue = queue
        self.destination = str(destination.resolve())
        self.account = account.profile

    @contextmanager
    def transaction(self):
        with self.queue._transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS capture_tasks (
                id TEXT PRIMARY KEY, account TEXT NOT NULL, destination TEXT NOT NULL, run TEXT NOT NULL,
                kind TEXT NOT NULL, name TEXT NOT NULL, state TEXT NOT NULL, evidence TEXT NOT NULL,
                created TEXT NOT NULL, updated TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                reason TEXT NOT NULL DEFAULT '', batch TEXT NOT NULL DEFAULT '')""")
            db.execute("""CREATE TABLE IF NOT EXISTS capture_pages (
                task TEXT NOT NULL, ordinal INTEGER NOT NULL, captured TEXT NOT NULL, evidence TEXT NOT NULL,
                PRIMARY KEY(task,ordinal))""")
            yield db

    def discover(self, run, name, evidence, *, kind="CONTACT", identifier=None, at=None):
        identifier = identifier or uuid4().hex
        with self.transaction() as db:
            db.execute(
                """INSERT INTO capture_tasks(id,account,destination,run,kind,name,state,evidence,created,updated)
                VALUES (?,?,?,?,?,?,'DISCOVERED',?,?,?) ON CONFLICT(id) DO NOTHING""",
                (
                    identifier,
                    self.account,
                    self.destination,
                    run,
                    kind,
                    name,
                    json.dumps(evidence, ensure_ascii=False),
                    at or stamp(),
                    stamp(),
                ),
            )
        return identifier

    def update(self, identifier, state, *, reason="", batch="", **fields):
        with self.transaction() as db:
            row = db.execute(
                "SELECT evidence FROM capture_tasks WHERE id=? AND account=? AND destination=?",
                (identifier, self.account, self.destination),
            ).fetchone()
            if row is None:
                raise ValueError("CAPTURE_TASK_NOT_FOUND")
            evidence = json.loads(row[0])
            evidence.update(fields)
            db.execute(
                """UPDATE capture_tasks SET state=?,reason=?,updated=?,evidence=?,
                attempts=attempts+?,batch=CASE WHEN ?='' THEN batch ELSE ? END WHERE id=?""",
                (
                    state,
                    reason,
                    stamp(),
                    json.dumps(evidence, ensure_ascii=False),
                    int(state == "NAVIGATION_PENDING"),
                    batch,
                    batch,
                    identifier,
                ),
            )

    def pause(self, identifiers):
        if not identifiers:
            return
        with self.transaction() as db:
            db.executemany(
                """UPDATE capture_tasks SET state='RETRY_PENDING', updated=?, reason='STOPPED_WITH_CHECKPOINT'
                   WHERE id=? AND account=? AND destination=?
                   AND state IN ('NAVIGATION_PENDING', 'CLICKED', 'VERIFIED', 'CAPTURING')""",
                ((stamp(), identifier, self.account, self.destination) for identifier in identifiers),
            )

    def outstanding(self):
        with self.transaction() as db:
            return [
                dict(row)
                for row in db.execute(
                    """SELECT * FROM capture_tasks
                WHERE account=? AND destination=? AND state!='COMPLETED' ORDER BY created,id""",
                    (self.account, self.destination),
                )
            ]

    def page(self, identifier, captured, data):
        payload = json.dumps(data, ensure_ascii=False)
        with self.transaction() as db:
            if not db.execute(
                "SELECT 1 FROM capture_tasks WHERE id=? AND account=? AND destination=?",
                (identifier, self.account, self.destination),
            ).fetchone():
                raise ValueError("CAPTURE_PAGE_OWNER_MISSING")
            ordinal = db.execute(
                "SELECT coalesce(max(ordinal),0)+1 FROM capture_pages WHERE task=?", (identifier,)
            ).fetchone()[0]
            db.execute("INSERT INTO capture_pages VALUES (?,?,?,?)", (identifier, ordinal, captured, payload))
        return ordinal

    def pages(self, identifier):
        with self.transaction() as db:
            return [
                json.loads(row[0])
                for row in db.execute(
                    """SELECT p.evidence FROM capture_pages p
                JOIN capture_tasks t ON t.id=p.task WHERE t.account=? AND t.destination=?
                AND t.id=? ORDER BY p.ordinal""",
                    (self.account, self.destination, identifier),
                )
            ]
