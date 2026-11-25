"""Replay explicitly selected, locally retained core observations after a failed save."""

import argparse
import json
import sqlite3
from collections import Counter
from dataclasses import replace
from datetime import date, datetime
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid5

import cv2
import numpy as np

from autoim.wecom.collection_core import CoreAnalysis, CoreMessage, _read_message, normalized_text
from autoim.wecom.collection_evidence import EvidenceArchive, EvidenceExportError
from autoim.wecom.collection_incremental import CoreAccount
from autoim.wecom.collection_pending import CoreDurability, PendingBatch, SavingBlocked, saving_reason
from autoim.wecom.collection_service import isolated_database_path
from autoim.wecom.visual_layout import Box, VisualThresholds
from autoim.wecom.visual_messages import analyze_messages, find_containers
from autoim.wecom.visual_ocr import TextSpan
from autoim.wecom.visual_store import VisualStore, database_path, name_key


def _events(path, detection):
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            event = json.loads(line)
            if event["detection"] == detection and event["mode"] == "test":
                yield event


def _record(archive, detection, event):
    detail = event["details"]
    record, folder = archive.read(detection, detail["evidence_id"])
    if not record or any(record[key] != value for key, value in (
            ("id", detail["evidence_id"]), ("event", event["id"]), ("source_event", detail["source_event"]),
            ("detection", event["detection"]), ("task", event["task"]), ("kind", detail["kind"]))):
        raise SavingBlocked("HISTORICAL_EVIDENCE_LINK_INVALID")
    return record, folder


def _raster(record, folder, kind, *, decode=False):
    region = next(region for region in record["regions"] if region["kind"] == kind)
    file = region["files"]["original"]
    if file["file"] != f"{record['id']}-{kind}-original.png":
        raise SavingBlocked("HISTORICAL_IMAGE_PATH_INVALID")
    path = folder / file["file"]
    if path.is_symlink():
        raise SavingBlocked("HISTORICAL_IMAGE_PATH_INVALID")
    image = path.read_bytes()
    if sha256(image).hexdigest() != file["sha256"]:
        raise SavingBlocked("HISTORICAL_IMAGE_CHECKSUM_INVALID")
    if decode:
        box = Box(*region["box"])
        width, height = record["size"]
        pixels = cv2.imdecode(np.frombuffer(image, np.uint8), cv2.IMREAD_COLOR)
        if (not 0 <= box.left < box.right <= width <= 16384 or not 0 <= box.top < box.bottom <= height <= 16384
                or width * height > 40_000_000 or pixels is None or pixels.shape[:2] != (box.height, box.width)):
            raise SavingBlocked("HISTORICAL_IMAGE_GEOMETRY_INVALID")
        original = np.empty((height, width, 3), np.uint8)
        box.crop(original)[:] = pixels
        return original, box


def _observations(record, folder, contact):
    data, task = record["data"], record["task"]
    original, area = _raster(record, folder, "c2", decode=True)
    # This recovery input format carries the complete evidence for ordinary text.
    # Cards and group sender attribution would need their own archived metadata.
    if any(row["kind"] != "TEXT" or row.get("sender_evidence") for row in data["messages"]):
        raise SavingBlocked("HISTORICAL_MESSAGE_KIND_UNSUPPORTED")
    if not all(name_key(row["contact"]) == name_key(contact) for row in data["messages"]):
        raise SavingBlocked("HISTORICAL_CONVERSATION_UNVERIFIED")
    recorded = tuple(CoreMessage(
        row["observation_id"], task, contact, row["sequence"], row["direction"], row["kind"], row["text"],
        row["time"], "", row["confidence"], Box(*row["box"]), row["quality"], "UNKNOWN", row["reason"],
        row["parser_status"], clock_source="EXPLICIT" if row["time_source"] == "EXPLICIT" else "GROUP_ANCHOR",
        method=row.get("method", "BUBBLE_FIRST"), ocr_boxes=tuple(map(tuple, row.get("ocr_boxes", ()))))
        for row in data["messages"])
    if any(a.sequence >= b.sequence or a.box.top > b.box.top for a, b in zip(recorded, recorded[1:])):
        raise SavingBlocked("HISTORICAL_MESSAGE_SEQUENCE_INVALID")
    gaps, completeness = tuple(data["gaps"]), data["completeness"]
    if not gaps:
        return recorded, gaps, completeness
    # Older evidence includes raw OCR boxes that the earlier bubble detector
    # left unassigned. Reapply the current parser to the original crop.
    unit = max(.65, min(record["dpi"] / 96, 3.0))
    thresholds = VisualThresholds()
    spans = tuple(TextSpan(row["text"], Box(*row["box"]), row["score"], row.get("angle")) for row in data["ocr"])
    containers = find_containers(original, area, unit, thresholds)
    parsed = analyze_messages(original, area, containers, spans, unit, {}, thresholds)
    replayed = tuple(_read_message(item, task, contact) for item in parsed.messages)

    def key(item):
        return item.kind, item.direction, normalized_text(item.text)

    if (not replayed or any(row.kind != "TEXT" or row.quality != "RELIABLE" for row in replayed)
            or Counter(map(key, recorded)) - Counter(map(key, replayed))):
        return recorded, gaps, completeness
    new_gaps = tuple({"reason": "UNASSIGNED_MESSAGE_OCR",
                      "box": (entry.span.box.left, entry.span.box.top, entry.span.box.right, entry.span.box.bottom),
                      "confidence": entry.span.score, "character_count": len(entry.span.text)}
                     for entry in parsed.dispositions if entry.possible_message)
    completeness = "VISIBLE_MESSAGES_INCOMPLETE" if new_gaps else "VISIBLE_MESSAGES_ACCOUNTED_FOR"
    return replayed, new_gaps, completeness


def recover_observation(log_root, day, detection, identifier, account):
    archive = EvidenceArchive(log_root / "visual-evidence", retention=30)
    matches = tuple(_events(log_root / day.isoformat() / "events.jsonl", detection))
    selected = next(event for event in matches if event["code"] == "VISUAL_EVIDENCE"
                    and event["details"]["evidence_id"] == identifier and event["details"]["kind"] == "c2")
    record, folder = _record(archive, detection, selected)
    task = selected["task"]
    events = [event for event in matches if event["task"] == task]
    scoped = [event for event in events if event["details"].get("evidence_scope") == "AUTOMATED_REAL_FLOW"]
    clicked = next(evt for evt in scoped if evt["code"] == "CONTACT_CLICKED" and evt["result"] == "AUTO_PASS")
    verified = next(evt for evt in scoped if evt["code"] == "CONVERSATION_VERIFIED" and evt["result"] == "AUTO_PASS")
    body = next(evt for evt in scoped if evt["id"] == record["source_event"] and evt["code"] == "MESSAGE_OCR_COMPLETED"
                and evt["result"] in ("AUTO_PASS", "PARTIAL"))
    failed = next(evt for evt in scoped if evt["code"] == "CORE_DB_NOT_SAVED" and evt["details"].get("reason")
                  in ("DEFAULT_ACCOUNT_UNAVAILABLE", "DEFAULT_ACCOUNT_FILE_MISSING"))
    target = clicked["details"]["target_id"]
    if (not clicked["time"] <= verified["time"] <= body["time"] <= failed["time"]
            or not record["data"]["conversation_verified"]
            or any(evt["details"].get("target_id") != target or evt["session"] != selected["session"]
                   for evt in (clicked, verified, body, failed))):
        raise SavingBlocked("HISTORICAL_CONVERSATION_UNVERIFIED")
    before, _ = _record(archive, detection, next(evt for evt in events if evt["code"] == "VISUAL_EVIDENCE"
                       and evt["details"]["kind"] == "before"))
    capture = datetime.fromisoformat(record["captured_at"])
    contact = before["data"]["contact"]
    method = verified["details"]["match_evidence"]
    after_id = ""
    for evt in events:
        if evt["code"] != "VISUAL_EVIDENCE" or evt["details"]["kind"] != "after":
            continue
        after, after_folder = _record(archive, detection, evt)
        title = after["data"]
        if (datetime.fromisoformat(clicked["time"]) <= datetime.fromisoformat(after["captured_at"]) <= capture
                and name_key(title["title"]) == name_key(title["selected"]) == name_key(contact)
                and title["title_reliable"] and title["match"] == "VISUAL_MATCH" and title["confirmation"] == method):
            _raster(after, after_folder, "c1")
            after_id = after["id"]
            break
    if not after_id or record["coordinates"] != "original_window_pixels":
        raise SavingBlocked("HISTORICAL_CONVERSATION_UNVERIFIED")
    messages, gaps, completeness = _observations(record, folder, contact)
    if not messages:
        raise SavingBlocked("HISTORICAL_NO_MESSAGE")
    batch_id = uuid5(NAMESPACE_URL, f"autoim:core-recovery:{account.profile}:{detection}:{task}")
    messages = tuple(replace(item, id=uuid5(batch_id, str(index)).hex[:12])
                     for index, item in enumerate(messages, 1))
    latest = next((evt["details"]["method"] for evt in reversed(scoped)
                   if evt["code"] == "LATEST_POSITION_CHECKED" and evt["time"] <= body["time"]
                   and evt["details"].get("target_id") == target), "UNKNOWN")
    # No conversation kind is inferred from OCR text or the display name.
    batch = PendingBatch.from_observation(account, CoreAnalysis(messages, latest, completeness, gaps),
                                          "UNKNOWN", capture, method, detection, task,
                                          (before["id"], after_id, identifier), batch_id=batch_id.hex,
                                          historical_recovery=True)
    return replace(batch, created_at=capture)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", nargs="+", metavar="DATE:DETECTION:C2_ID")
    parser.add_argument("--write", action="store_true", help="write to the local isolated core database")
    args = parser.parse_args()
    root = database_path().parent
    durable = CoreDurability(VisualStore(isolated_database_path()), root / "core-default-account.json",
                             root / "pending-collections" / "observations.sqlite3")
    check = lambda: None
    try:
        configured = CoreAccount.inspect(durable.account.path)
        if not configured.account:
            raise SavingBlocked(configured.reason)
        account = configured.account
        batches = []
        for value in args.evidence:
            day, detection, c2_id = value.split(":")
            EvidenceArchive._id(detection)
            EvidenceArchive._id(c2_id)
            batches.append(recover_observation(root / "logs", date.fromisoformat(day), detection, c2_id, account))
        if args.write:
            durable.account.ensure(durable.store, check, supplied=account)
        for batch in sorted(batches, key=lambda item: item.captured_at):
            print(f"observation={batch.captured_at.isoformat()} messages={len(batch.analysis.messages)} "
                  f"gaps={len(batch.analysis.gaps)}")
            if args.write:
                durable.queue.stage(batch)
        if args.write:
            recovered = durable.queue.recover(durable.store, account, check)
            for _, result in recovered.results:
                print(f"first={result.first if not result.receipt_replayed else 0} "
                      f"new={result.new if not result.receipt_replayed else 0} "
                      f"existing={result.existing} review={result.uncertain} "
                      f"anchor={result.anchor_status} receipt_replayed={result.receipt_replayed}")
            print(f"pending={recovered.pending} invalid={recovered.invalid} blocked={recovered.blocked or '-'}")
            return int(bool(recovered.blocked))
    except (StopIteration, KeyError, SavingBlocked, OSError, ValueError, sqlite3.Error, EvidenceExportError) as error:
        print("Historical recovery stopped:", saving_reason(error))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
