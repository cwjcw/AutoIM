"""Local visual evidence and offline, explicitly scoped diagnostic exports."""

from datetime import datetime, timedelta
from hashlib import sha256
from html import escape
from pathlib import Path, PurePosixPath
from threading import BoundedSemaphore
from typing import NamedTuple
import json
import re
import shutil
import unicodedata

import cv2
import numpy as np

from autoim.wecom.visual_store import name_key

KINDS = {"b2", "before", "after", "c2", "database", "failure"}
REGIONS = {"b2", "b2-edge", "c1", "c2"}


class EvidenceExportError(OSError):
    pass


class EvidenceJob(NamedTuple):
    callback: object
    args: tuple
    slot: BoundedSemaphore


def rect(box):
    return [box.left, box.top, box.right, box.bottom] if box else None


def span_info(span, *, name_fragment=False):
    return {"box": rect(span.box), "score": round(span.score, 4), "text": span.text, "name_fragment": name_fragment}


def contact_scan(raw, targets, scan, blocked):
    by_row = {id(t.row): t for t in targets}
    rows = []
    boxes = []
    for row in raw.contacts:
        target = by_row.get(id(row))
        key = name_key(row.display_name) if row.display_name else ""
        base = row.base_name or row.display_name
        repeated = [other.sequence for other in raw.contacts if other is not row and key and other.name_reliable
                    and name_key(other.display_name) == key]
        siblings = [other.sequence for other in raw.contacts if other is not row and base and
                    name_key(other.base_name or other.display_name) == name_key(base)
                    and name_key(other.display_name) != key]
        overlapping = [other.sequence for other in raw.contacts if other is not row and
                       (other.box.contains(row.box.center) or row.box.contains(other.box.center)
                        or any(span in other.name_spans for span in row.name_spans)
                        or row.badge and other.badge and (other.badge.contains(row.badge.center)
                                                        or row.badge.contains(other.badge.center)))]
        decision = target.scope_decision if target else "NO_UNREAD"
        reason = (target.skip_reason or " / ".join(target.blockers)) if target else ""
        if decision == "ELIGIBLE" and blocked:
            decision, reason = "NAVIGATION_BLOCKED", blocked
        activity = row.activity_span
        rows.append({"row": row.sequence, "box": rect(row.box), "position_reliable": row.geometry_reliable,
                     "row_pitch": row.row_pitch, "boundary_source": row.boundary_source,
                     "raw_name": row.raw_name, "full_display_name": row.display_name, "base_name": base,
                     "identity_labels": row.identity_labels, "key": key, "name_reliable": row.name_reliable,
                     "name_issue": row.name_issue, "name_threshold": row.name_threshold,
                     "name_ocr": [span_info(span, name_fragment=True) for span in row.name_spans],
                     "rejected_name_ocr": [span_info(span, name_fragment=True) for span in row.name_rejected],
                     "normalization": {"nfkc": row.raw_name != unicodedata.normalize("NFKC", row.raw_name),
                                       "join": len(row.name_spans) > 1, "rejected_fragments": len(row.name_rejected),
                                       "identity_suffix_present": bool(row.identity_labels or "@" in row.display_name),
                                       "truncated": "..." in unicodedata.normalize("NFKC", row.raw_name),
                                       "key_matches_full_ocr": bool(key and key == name_key(row.raw_name))},
                     "avatar": rect(row.avatar), "activity": span_info(activity) if activity else None,
                     "divider_y": row.divider_y, "highlight": rect(row.highlight_box),
                     "selected": row.is_selected, "selection_method": row.selection_method,
                     "badge": rect(row.badge), "badge_style": row.badge_kind,
                     "unread_count": row.unread_count if row.unread_count_reliable else None,
                     "muted": row.is_muted, "mute_evidence": row.mute_evidence, "mute_score": row.mute_score,
                     "kind": row.kind, "peer_type": row.peer_type, "full_name_duplicate_rows": repeated,
                     "same_base_distinct_name_rows": siblings, "overlapping_rows": overlapping,
                     "repetition_status": ("OCR_ROW_OVERLAP" if overlapping else "FULL_NAME_DUPLICATE" if repeated
                                           else "FULL_NAME_UNVERIFIED" if not row.name_reliable
                                           else "SAME_BASE_DISTINCT_FULL_NAME" if siblings else "DISTINCT"),
                     "scope": decision, "reason": reason, "navigation_eligible": decision == "ELIGIBLE"})
        boxes.append((rect(row.box), f"R{row.sequence} {decision}"))
        boxes.extend((rect(span.box), f"R{row.sequence} {'TAG' if '@' in unicodedata.normalize('NFKC', span.text) else 'NAME'}")
                     for span in row.name_spans)
        boxes.extend((rect(span.box), f"R{row.sequence} REJECTED") for span in row.name_rejected)
        if row.badge:
            boxes.append((rect(row.badge), f"R{row.sequence} BADGE"))
        if row.highlight_box:
            boxes.append((rect(row.highlight_box), f"R{row.sequence} SELECTED"))
        if activity:
            boxes.append((rect(activity.box), f"R{row.sequence} TIME"))
        boxes.extend(([row.box.left, y, row.box.right, y + 1], "DIVIDER") for y in row.divider_y)
    unassigned = [{"box": rect(badge.box), "kind": badge.kind} for badge in raw.b2_badges
                  if not any(row.badge and row.badge.contains(badge.box.center) for row in raw.contacts)]
    boxes.extend((item["box"], "UNASSIGNED BADGE") for item in unassigned)
    unread = raw.global_unread
    return {"scan": scan, "rows": rows, "unassigned_badges": unassigned,
            "statistics": {"global_unread": unread.global_unread_count if unread.global_unread_count_reliable else None,
                           "contacts": len(rows), "b2_badges": sum(r["badge"] is not None for r in rows) + len(unassigned),
                           "ordinary_unread_rows": sum(r["badge"] is not None and r["muted"] is False
                                                       and r["kind"] in ("CONTACT", "GROUP_CANDIDATE") for r in rows),
                           "ordinary_unread_messages_confirmed": sum(r["unread_count"] or 0 for r in rows
                                      if r["badge"] and r["muted"] is False and r["kind"] in ("CONTACT", "GROUP_CANDIDATE")),
                           "ordinary_unread_unknown_count": sum(r["badge"] is not None and r["unread_count"] is None
                                  and r["muted"] is False and r["kind"] in ("CONTACT", "GROUP_CANDIDATE") for r in rows),
                           "muted_unread": sum(bool(r["badge"]) and r["muted"] is True for r in rows),
                           "folded_unread": sum(bool(r["badge"]) and r["kind"] == "FOLDED_CHAT_CONTAINER" for r in rows),
                           "unassigned_badges": len(unassigned),
                           "navigation_eligible": sum(r["navigation_eligible"] for r in rows)}}, boxes


def title_evidence(raw):
    conv = raw.conversation
    spans = [span for area in raw.ocr_areas if area.region.startswith("C1") for span in area.spans]
    return {"raw_title": conv.raw_title_line, "title": conv.title, "base_name": conv.title_base_name,
            "selected": conv.selected_name, "match": conv.match_status, "reason": conv.comparison_evidence,
            "identity_labels": conv.title_identity_labels, "peer_type": conv.title_peer_type,
            "title_reliable": conv.title_name_reliable, "selected_reliable": conv.selected_name_reliable,
            "title_ocr": [span_info(span) for span in spans]}, [(rect(span.box), "C1 OCR") for span in spans]


def message_evidence(raw, messages, *, body=True):
    records = [{**item.diagnostic(), "observed_box": rect(item.box), "contact": item.contact,
                "key": name_key(item.contact), "text": item.text if body else "", "time": item.time,
                "time_anchor": item.clock.anchor, "time_source": item.clock.time_source,
                "time_precision": item.clock.precision, "group_sequence": item.clock_sequence,
                "collection_status": item.collection_status, "save_status": item.save_status} for item in messages]
    boxes = [(rect(item.box), f"M{item.sequence} {item.direction} {item.quality}") for item in messages]
    boxes.extend((rect(item.span.box), item.kind) for item in raw.message_ocr)
    ocr = [{**span_info(item.span), "kind": item.kind, "container": item.container_number,
            "nearest": item.nearest_container, "reason": item.reason,
            "possible_message": item.possible_message} for item in raw.message_ocr]
    containers = [{ "box": rect(item.container.box), "direction": item.direction,
                    "clipped": item.container.clipped, "reason": item.reason} for item in raw.candidates]
    boxes.extend((item["box"], "CONTAINER " + item["direction"]) for item in containers)
    return {"messages": records, "ocr": ocr, "containers": containers,
            "completeness": raw.message_completeness}, boxes


def _dump(value):
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")


def _png(pixels):
    success, image = cv2.imencode(".png", pixels, [cv2.IMWRITE_PNG_COMPRESSION, 2])
    if not success:
        raise EvidenceExportError("图像编码失败")
    return image.tobytes()


def _draw(pixels, area, overlays, *, masked):
    image = np.full(pixels.shape, 242, dtype=np.uint8) if masked else pixels.copy()
    left, top, right, bottom = area
    for box, label in overlays:
        if not box or box[2] <= left or box[0] >= right or box[3] <= top or box[1] >= bottom:
            continue
        x, y = max(0, box[0] - left), max(0, box[1] - top)
        x2, y2 = min(image.shape[1] - 1, box[2] - left - 1), min(image.shape[0] - 1, box[3] - top - 1)
        color = (205, 75, 12) if "BADGE" not in label else (25, 40, 220)
        cv2.rectangle(image, (x, y), (x2, y2), color, 2)
        cv2.putText(image, label, (x + 3, max(15, y - 3)), cv2.FONT_HERSHEY_SIMPLEX, .43, color, 1, cv2.LINE_AA)
    return image


class EvidenceArchive:
    RUN_LIMIT = 64 * 1024 * 1024
    TOTAL_LIMIT = 256 * 1024 * 1024

    def __init__(self, root, retention):
        self.root = Path(root)
        self.retention = retention
        self.slots = BoundedSemaphore(6)
        self.sizes = {}

    @staticmethod
    def _id(value):
        if not re.fullmatch(r"[a-f0-9]{32}", value):
            raise EvidenceExportError("诊断证据 ID 无效")
        return value

    def _trim(self, current):
        cutoff = datetime.now().timestamp() - timedelta(days=self.retention).total_seconds()
        used = 0
        paths = [path for path in self.root.iterdir() if path.is_dir() and not path.is_symlink()
                 and not path.is_junction() and re.fullmatch(r"[a-f0-9]{32}", path.name)]
        for folder in sorted(paths, key=lambda path: path.stat().st_mtime, reverse=True):
            if folder == current:
                continue
            used += sum(path.stat().st_size for path in folder.iterdir() if path.is_file() and not path.is_symlink())
            if folder.stat().st_mtime < cutoff or used > self.TOTAL_LIMIT - self.RUN_LIMIT:
                shutil.rmtree(folder)

    def keep(self, record, pixels, regions):
        detection, identifier = self._id(record["detection"]), self._id(record["id"])
        folder = self.root / detection
        folder.mkdir(parents=True, exist_ok=True)
        if folder.is_symlink() or folder.is_junction():
            raise EvidenceExportError("私有证据目录无效")
        if detection not in self.sizes:
            self._trim(folder)
            self.sizes[detection] = 0
        record["regions"] = []
        for kind, box, marks in regions:
            if kind not in REGIONS:
                raise EvidenceExportError("未知证据图像区域")
            info = {"kind": kind, "box": rect(box), "files": {}, "missing": ""}
            record["regions"].append(info)
            if pixels is None or box is None or not (0 <= box.left < box.right <= pixels.shape[1]
                                                      and 0 <= box.top < box.bottom <= pixels.shape[0]):
                info["missing"] = "CAPTURE_OR_COORDINATES_UNAVAILABLE"
                continue
            try:
                crop = box.crop(pixels)
                files = {"original": _png(crop), "annotated": _png(_draw(crop, rect(box), marks, masked=False)),
                         "masked": _png(_draw(crop, rect(box), marks, masked=True))}
                size = sum(map(len, files.values()))
                if self.sizes[detection] + size > self.RUN_LIMIT:
                    info["missing"] = "LOCAL_EVIDENCE_LIMIT"
                    continue
                for role, data in files.items():
                    filename = f"{identifier}-{kind}-{role}.png"
                    with (folder / filename).open("xb") as stream:
                        stream.write(data)
                    info["files"][role] = {"file": filename, "sha256": sha256(data).hexdigest()}
                self.sizes[detection] += size
            except (OSError, cv2.error):
                info.update(files={}, missing="LOCAL_IMAGE_WRITE_FAILED")
        (folder / f"{identifier}.json").write_bytes(_dump(record))

    def read(self, detection, identifier):
        folder = self.root / self._id(detection)
        file = folder / (self._id(identifier) + ".json")
        if not file.is_file():
            return None, folder
        if file.is_symlink() or folder.is_symlink() or folder.is_junction():
            raise EvidenceExportError("私有证据文件无效")
        try:
            return json.loads(file.read_text(encoding="utf-8")), folder
        except (OSError, ValueError) as error:
            raise EvidenceExportError("私有证据索引无法读取") from error


class _Anonymize:
    def __init__(self):
        self.tokens = {}

    def token(self, value, category="联系人"):
        if not value:
            return ""
        key = category, name_key(value)
        if key not in self.tokens:
            self.tokens[key] = f"{category}{len(self.tokens) + 1}"
        return self.tokens[key]

    def name(self, text):
        if not text:
            return ""
        fragments = re.split(r"([@＠])", text)
        for i in range(0, len(fragments), 2):
            part = fragments[i]
            if not part.strip():
                continue
            start = part[:len(part) - len(part.lstrip())]
            end = part[len(part.rstrip()):]
            word = part.strip()
            suffix = "..." if "..." in unicodedata.normalize("NFKC", word) else ""
            fragments[i] = start + (word if i and word in ("微信", "微信联系人") else
                                    self.token(word, "企业" if i else "联系人")) + suffix + end
        return "".join(fragments)

    def clean(self, value, key=""):
        if isinstance(value, dict):
            masked = {name: self.clean(item, name) for name, item in value.items()
                      if name not in ("time_anchor", "previous_anchor")}
            if "box" in value and "text" in value:
                masked["characters"] = len(value["text"])
                if value.get("name_fragment"):
                    masked["text"] = self.name(value["text"])
            if "time_anchor" in value:
                masked["has_absolute_anchor"] = bool(value["time_anchor"])
            if "previous_anchor" in value:
                masked["has_previous_anchor"] = bool(value["previous_anchor"])
            return masked
        if isinstance(value, (list, tuple)):
            return [self.clean(item, key) for item in value]
        if key in ("raw_name", "full_display_name", "base_name", "contact", "raw_title", "title", "selected") and isinstance(value, str):
            return self.name(value)
        if key == "identity_labels":
            return value if value in ("微信", "微信联系人") else self.token(value, "企业")
        if key == "key":
            return self.token(value, "完整键")
        if key == "text":
            return ""
        if key == "time":
            return "已读取" if value else ""
        return value


def _html(report, contacts, records, manifest, events):
    def e(value):
        return escape(str(value if value is not None and value != "" else "缺失"))
    def table(columns, values, css=""):
        return f'<table class="{css}"><thead><tr>' + "".join(f"<th>{e(col)}</th>" for col in columns) + "</tr></thead><tbody>" + "".join(
            "<tr>" + "".join(f"<td>{e(value)}</td>" for value in row) + "</tr>" for row in values) + "</tbody></table>"
    counts = [event for event in events if event["code"] in ("TEST_STARTED", "TEST_COMPLETED", "TEST_SUMMARY",
                               "DETECTION_FINISHED", "STAGE_STARTED", "STAGE_COMPLETED", "STAGE_TIMEOUT",
                               "CONTACT_CLICKED", "RESULT_DISPLAYED", "CORE_DB_COMMITTED", "CORE_DB_NOT_SAVED", "CORE_DB_SAVE_FAILED")]
    issues = [event for event in events if event["result"] in ("FAILED", "BLOCKED_BY_DEPENDENCY")
              or event["code"] in ("STAGE_RETRY", "STAGE_TIMEOUT")]
    images = "".join(f'<figure><figcaption>{e(item["detection"])} / {e(item["kind"])} / {e(item["id"])}</figcaption>' +
                     "".join(f'<a href="{e(path)}">原始裁剪</a>' if role == "original" else
                             f'<a href="{e(path)}"><img loading="lazy" src="{e(path)}" alt="视觉证据"></a>'
                             for region in item["regions"] for role, path in region["included"].items())
                     + "</figure>" for item in manifest if item.get("regions"))
    body = "<h1>AutoIM 视觉识别诊断</h1>" + f'<p>{e(report["version"])} · {e(report["git"])} · {e(report["detection"])}</p>'
    body += "<h2>本轮摘要</h2>" + table(("时间", "阶段", "事件", "任务", "结果", "细节"),
         [(x["time"], x["stage"], x["code"], x["task"], x["result"], x["details"]) for x in counts])
    body += "<h2>未读统计</h2>" + table(("扫描", "截图时间", "统计"),
         [(x["scan"], x["captured_at"], x["data"]["statistics"]) for x in contacts])
    body += '<h2>联系人与名称转换</h2><label>筛选 <input id="filter" placeholder="例如 ELIGIBLE、FAILED、未读"></label>'
    body += table(("扫描", "行", "原始", "完整", "基础名", "标签", "完整键", "重复或重叠", "活动时间", "徽标", "静音",
                   "选中", "处理结果", "原因"),
                  [(scan["scan"], r["row"], r["raw_name"], r["full_display_name"], r["base_name"], r["identity_labels"],
                    r["key"], r["repetition_status"], r["activity"], "未读 " + str(r["unread_count"]) if r["badge"] else "",
                    r["muted"], r["selected"], r["scope"], r["reason"] or r["name_issue"])
                   for scan in contacts for r in scan["data"]["rows"]], "contacts")
    body += "<h2>视觉证据</h2>" + images
    body += "<h2>导航前后</h2>" + table(("任务", "阶段", "证据", "C1 原始", "C1 完整名称", "B2 选中",
                                        "会话核对", "徽标仍可见", "确认依据"),
                         [(x["task"], x["kind"], x["id"], x["data"].get("raw_title"), x["data"].get("title"),
                           x["data"].get("selected"), x["data"].get("match"), x["data"].get("badge_after"),
                           x["data"].get("confirmation") or x["data"].get("reason"))
                          for x in records if x["kind"] in ("before", "after", "failure")])
    body += "<h2>消息与数据库</h2>" + table(("任务", "阶段", "证据", "结果"),
                         [(x["task"], x["kind"], x["id"], x["data"].get("database") or
                           [{k:v for k,v in item.items() if k in ("sequence","kind","quality","time_source",
                                                                 "collection_status","save_status","character_count")}
                            for item in x["data"].get("messages", ())]) for x in records])
    body += "<h2>异常与缺失</h2>" + table(("时间", "阶段", "任务", "事件", "结果", "关联证据", "细节"),
                         [(x["time"], x["stage"], x["task"], x["id"], x["code"],
                           [record["id"] for record in manifest if record.get("task") == x["task"] and x["task"]],
                           x["details"]) for x in issues])
    body += table(("证据", "阶段", "状态", "原因"), [(x.get("id"), x["kind"], x["status"], x.get("reason"))
                      for x in manifest if x["status"] != "AVAILABLE"])
    return ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>AutoIM 视觉识别诊断</title>'
            '<style>body{font:15px system-ui,sans-serif;margin:24px;color:#17304a}table{border-collapse:collapse;'
            'margin-bottom:20px;width:100%}th,td{padding:6px;border:1px solid #ccd4db;text-align:left;overflow-wrap:anywhere}'
            'img{max-width:min(95%,800px);border:1px solid #b0bdc8}figure{margin:16px 0}input{margin:8px}</style>'
            + body + """<script>document.querySelector('#filter').addEventListener('input',event=>{
            for(const row of document.querySelectorAll('.contacts tbody tr'))
                row.hidden=!row.textContent.toLowerCase().includes(event.target.value.toLowerCase());
            });</script></html>""").encode("utf-8")


def write_evidence(archive, store, events, report, *, full):
    anonymous = _Anonymize()
    contacts, messages, manifest = [], [], []
    captured = set()
    event_ids = {event["id"] for event in events}
    for event in events:
        if event["code"] != "VISUAL_EVIDENCE":
            continue
        details = event["details"]
        identifier, kind = details["evidence_id"], details["kind"]
        if kind not in KINDS:
            raise EvidenceExportError("未知视觉证据阶段")
        item = {"id": identifier, "kind": kind, "detection": event["detection"], "task": event["task"],
                "scan": details.get("scan", ""), "event": event["id"], "source_event": details.get("source_event", ""),
                "redacted": not full, "contains_chat": full and kind in ("c2", "failure", "after"),
                "status": "AVAILABLE", "regions": []}
        manifest.append(item)
        record, folder = store.read(event["detection"], identifier)
        if record is None:
            item.update(status="DIAGNOSTIC_EVIDENCE_MISSING", reason=details.get("reason") or
                        "LOCAL_BUFFER_UNAVAILABLE_OR_RETENTION_EXPIRED")
            continue
        if item["source_event"] and item["source_event"] not in event_ids:
            item.update(status="DIAGNOSTIC_EVIDENCE_MISSING", reason="SOURCE_EVENT_NOT_IN_EXPORT_SCOPE")
        if any(record.get(field) != item[field] for field in ("id", "kind", "detection", "task", "scan", "event")):
            raise EvidenceExportError("证据索引与事件不一致")
        for region in record["regions"]:
            if region["kind"] not in REGIONS:
                raise EvidenceExportError("未知证据图像区域")
            data = {"kind": region["kind"], "box": region["box"], "included": {}, "sha256": {},
                    "missing": region["missing"]}
            item["regions"].append(data)
            if region["missing"]:
                item.update(status="DIAGNOSTIC_EVIDENCE_MISSING", reason=region["missing"])
                continue
            for role in (("original", "annotated") if full else ("masked",)):
                entry = region["files"].get(role)
                expected = f'{identifier}-{region["kind"]}-{role}.png'
                if not entry or entry["file"] != expected:
                    raise EvidenceExportError("证据图像索引不完整")
                image_file = folder / expected
                if image_file.is_symlink() or not image_file.is_file():
                    item.update(status="DIAGNOSTIC_EVIDENCE_MISSING", reason="IMAGE_FILE_MISSING")
                    data["missing"] = "IMAGE_FILE_MISSING"
                    continue
                blob = image_file.read_bytes()
                checksum = sha256(blob).hexdigest()
                if checksum != entry["sha256"]:
                    raise EvidenceExportError("证据图像完整性摘要不一致")
                pixels = cv2.imdecode(np.frombuffer(blob, dtype=np.uint8), cv2.IMREAD_COLOR)
                left, top, right, bottom = region["box"]
                width, height = record["size"]
                if (pixels is None or pixels.shape[:2] != (bottom - top, right - left)
                        or not (0 <= left < right <= width and 0 <= top < bottom <= height)):
                    raise EvidenceExportError("证据图像尺寸与原图坐标不一致")
                location = str(PurePosixPath("screenshots", kind, event["detection"], expected))
                archive.writestr(location, blob)
                data["included"][role], data["sha256"][role] = location, checksum
        if item["status"] == "AVAILABLE":
            captured.add((item["detection"], item["task"], kind))
        output = {k: v for k, v in record.items() if k != "regions"}
        output["data"] = record["data"] if full else anonymous.clean(record["data"])
        output["images"] = item["regions"]
        (contacts if kind == "b2" else messages).append(output)

    def require(event, kind, task=None):
        task = event["task"] if task is None else task
        ref = event["detection"], task, kind
        if ref not in captured:
            manifest.append({"id": "", "kind": kind, "detection": ref[0], "task": task, "event": event["id"],
                             "status": "DIAGNOSTIC_EVIDENCE_MISSING", "reason": "EXPECTED_STAGE_CAPTURE_NOT_AVAILABLE",
                             "redacted": not full, "contains_chat": False, "regions": []})
            captured.add(ref)

    for event in events:
        code = event["code"]
        if code == "TEST_STARTED":
            require(event, "b2", "")
        elif code == "CONTACT_CLICKED":
            require(event, "before")
            require(event, "after")
        elif code in ("MESSAGE_OCR_COMPLETED", "INDEPENDENT_C2_OBSERVED"):
            require(event, "c2")
        elif code in ("TARGET_FAILED", "STAGE_TIMEOUT"):
            require(event, "failure")
    docs = {"contacts.json": _dump(contacts), "message-observations.json": _dump(messages),
            "recognition-report.html": _html(report, contacts, messages, manifest, events)}
    for name, data in docs.items():
        archive.writestr(name, data)
    archive.writestr("evidence-manifest.json", _dump({"mode": "FULL_PRIVATE" if full else "REDACTED",
                     "documents": {file: sha256(data).hexdigest() for file, data in docs.items()}, "evidence": manifest}))
