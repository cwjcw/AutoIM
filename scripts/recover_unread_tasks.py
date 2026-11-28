"""Rebuild navigation intents from a selected private diagnostic run, without clicking."""

import argparse
from datetime import date
from pathlib import Path
from uuid import UUID

from autoim.runtime_paths import runtime_paths
from autoim.runtime_storage import backup_runtime
from autoim.wecom.collection_evidence import EvidenceArchive
from autoim.wecom.collection_incremental import CoreAccount
from autoim.wecom.collection_pending import PendingCollections, SavingBlocked
from autoim.wecom.collection_recovery import _events, _raster, _record
from autoim.wecom.collection_tasks import CaptureTasks


def recover(paths, day, run, *, write=False):
    events = list(_events(paths.logs / day / "events.jsonl", run))
    archive = EvidenceArchive(paths.evidence, 14)
    account = CoreAccount.load(paths.account)
    storage = next(
        (e for e in events if e["code"] == "CORE_DEFAULT_ACCOUNT_STATUS" and e["result"] == "AUTO_PASS"), None
    )
    if (
        not account
        or not storage
        or Path(storage["details"]["config_path"]).resolve() != paths.account.resolve()
    ):
        raise SavingBlocked("ARCHIVE_ACCOUNT_BINDING_UNVERIFIED")
    from datetime import datetime

    if paths.account.stat().st_mtime > datetime.fromisoformat(storage["time"]).timestamp():
        raise SavingBlocked("ARCHIVE_ACCOUNT_CHANGED")
    records = [
        _record(archive, run, e)
        for e in events
        if e["details"].get("evidence_id") and e["details"].get("kind") in ("before", "b2")
    ]
    intents = []
    for click in (e for e in events if e["code"] == "CONTACT_CLICKED" and e["result"] == "AUTO_PASS"):
        task = click["task"]
        views = [(v, f) for v, f in records if v["kind"] == "before" and v["task"] == task]
        if len(views) != 1:
            continue
        before, folder = views[0]
        _raster(before, folder, "b2")
        candidates = [
            (r, scan)
            for scan, _ in records
            if scan["kind"] == "b2" and scan["scan"] == before["scan"]
            for r in scan["data"]["rows"]
            if r["box"] == before["data"]["row_box"]
            and r["name_reliable"]
            and r["navigation_eligible"]
            and r["full_display_name"] == before["data"]["contact"]
        ]
        if len(candidates) != 1:
            continue
        row, scan = candidates[0]
        _raster(scan, folder, "b2")
        intents.append(
            (
                task,
                row["full_display_name"],
                dict(
                    name_reliable=True,
                    duplicate=bool(row["full_name_duplicate_rows"]),
                    kind=row["kind"],
                    peer=row["peer_type"],
                    count=row["unread_count"],
                    avatar_box=row["avatar"],
                    login=account.name.strip().casefold(),
                    original_click=click["id"],
                    before=before["id"],
                    scan=scan["id"],
                    originally_verified=any(
                        e["task"] == task and e["code"] == "CONVERSATION_VERIFIED" for e in events
                    ),
                ),
                before["captured_at"],
            )
        )
    if write and intents:
        backup_runtime(paths, "before-unread-task-recovery", roles={"pending", "isolated"})
        jobs = CaptureTasks(PendingCollections(paths.pending_db), paths.isolated_db, account)
        for key, name, proof, at in intents:
            jobs.discover(run, name, proof, identifier=key, at=at)
    return len(intents)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("day", type=date.fromisoformat)
    parser.add_argument("detection", type=UUID)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    print(
        "Verified recovery tasks:",
        recover(runtime_paths(), str(args.day), args.detection.hex, write=args.write),
    )
