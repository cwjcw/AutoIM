"""Assemble scoped screenshot observations without binding an official conversation ID."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path
from time import perf_counter
from typing import Mapping

import cv2
import numpy as np

from autoim.calibration import CalibrationStore
from autoim.vision import NormalizedRect
from autoim.wecom.visual_account import LoginEvidence
from autoim.wecom.visual_capture import VisualFrame, capture_visible_window
from autoim.wecom.visual_contacts import (ContactObservation, ContactTextDiagnostic, GlobalUnreadObservation,
                                          VisualName, detect_contacts, detect_global_unread, parse_visual_name)
from autoim.wecom.visual_layout import (Box, LayoutObservation, VisualThresholds, contours,
                                       detect_layout, detect_scroll_thumb, near_background)
from autoim.wecom.visual_messages import (Container, OcrDisposition, VisibleMessageObservation,
                                          analyze_messages, direction_evidence, find_containers)
from autoim.wecom.visual_ocr import LocalTextRecognizer, OcrUnavailable, TextRecognizer, TextSpan, text_lines


@dataclass(frozen=True, slots=True)
class ComposerObservation:
    toolbar: Box | None
    send_button: Box | None
    send_appearance: str
    has_visible_draft: bool | None


@dataclass(frozen=True, slots=True)
class ConversationObservation:
    title: str = ""
    visible_identity: tuple[str, ...] = ()
    selected_name: str = ""
    match_status: str = "UNKNOWN"
    conversation_type: str = "UNKNOWN"
    identity_unique: bool = False
    attribution_reliable: bool = False
    completeness: str = "VISIBLE_ONLY"
    raw_title_line: str = ""
    title_name_reliable: bool = False
    selected_name_reliable: bool = False
    comparison_evidence: str = "未定位可比较的姓名"
    selected_raw_name: str = ""
    selected_peer_type: str = "UNKNOWN"
    selected_identity_labels: tuple[str, ...] = ()
    title_peer_type: str = "UNKNOWN"
    title_identity_labels: tuple[str, ...] = ()
    selection_method: str = "UNKNOWN"
    selection_confidence: str = "UNKNOWN"
    selection_evidence: str = ""


@dataclass(frozen=True, slots=True)
class OcrAreaObservation:
    region: str
    box: Box
    status: str
    spans: tuple[TextSpan, ...] = ()
    reason: str = ""


class _ObservedReader:
    def __init__(self, reader: TextRecognizer | None) -> None:
        self.reader = reader
        self.region = ""
        self.observations: list[OcrAreaObservation] = []

    def read(self, pixels: np.ndarray, box: Box, *, single_line: bool = False) -> tuple[TextSpan, ...]:
        region = f"{self.region} 徽标数字" if single_line else self.region
        if self.reader is None:
            self.observations.append(OcrAreaObservation(region, box, "SKIPPED", reason="OCR 未初始化"))
            return ()
        try:
            spans = self.reader.read(pixels, box, single_line=True) if single_line else self.reader.read(pixels, box)
        except OcrUnavailable as exc:
            self.observations.append(OcrAreaObservation(region, box, "FAILED", reason=str(exc)))
            return ()
        self.observations.append(OcrAreaObservation(region, box, "TEXT_FOUND" if spans else "NO_TEXT", spans))
        return spans


@dataclass(frozen=True, slots=True)
class MessageCandidateObservation:
    container: Container
    direction: str
    left_distance: float | None
    right_distance: float | None
    parsed_sequence: int | None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class VisualObservation:
    frame: VisualFrame
    layout: LayoutObservation
    status: str
    page: str = "UNKNOWN"
    contacts: tuple[ContactObservation, ...] = ()
    conversation: ConversationObservation = ConversationObservation()
    messages: tuple[VisibleMessageObservation, ...] = ()
    context_text: tuple[TextSpan, ...] = ()
    search_box: Box | None = None
    add_button: Box | None = None
    composer: ComposerObservation | None = None
    list_scrollbar: Box | None = None
    history_scrollbar: Box | None = None
    hovered_region: str = ""
    warnings: tuple[str, ...] = ()
    global_unread: GlobalUnreadObservation = GlobalUnreadObservation()
    candidates: tuple[MessageCandidateObservation, ...] = ()
    ocr_status: str = "SKIPPED"
    ocr_reason: str = ""
    ocr_areas: tuple[OcrAreaObservation, ...] = ()
    recognition_ms: float = 0
    anchors: tuple[int | None, int | None] = (None, None)
    message_ocr: tuple[OcrDisposition, ...] = ()
    contact_text_diagnostics: tuple[ContactTextDiagnostic, ...] = ()
    message_completeness: str = "VISIBLE_MESSAGES_UNVERIFIED"
    board_scrollbar: Box | None = None
    login: LoginEvidence = LoginEvidence()
    verified_target: ContactObservation | None = None


def _group_senders(messages: tuple[VisibleMessageObservation, ...], dispositions: tuple[OcrDisposition, ...],
                   unit: float, minimum_score: float):
    ledger = list(dispositions)
    messages = list(messages)
    for index, message in enumerate(messages):
        if message.direction != "INBOUND":
            continue
        box = message.box
        previous_bottom = messages[index - 1].box.bottom if index else 0
        matches = [i for i, item in enumerate(ledger)
                   if item.kind == "UNASSIGNED" and item.span.score >= minimum_score
                   and re.fullmatch(r"[\w·・ .@-]{1,32}", item.span.text)
                   and any(char.isalpha() for char in item.span.text)
                   and item.span.box.top >= previous_bottom
                   and 1 * unit <= box.top - item.span.box.bottom <= 11 * unit
                   and abs(item.span.box.left - box.left) <= 9 * unit
                   and item.span.box.width <= 150 * unit
                   and not any(other.box.contains(item.span.box.center) for other in messages)]
        if len(matches) == 1:
            i = matches[0]
            item = ledger[i]
            ledger[i] = replace(item, kind="SENDER", possible_message=False, reason="同列消息紧邻上方的群发送人标签")
            messages[index] = replace(message, sender_name=item.span.text)
    return tuple(messages), tuple(ledger)


def _toolbar_controls(pixels: np.ndarray, bar: Box) -> tuple[Box | None, Box | None]:
    mask = ~near_background(bar.crop(pixels), 12)
    search = add = None
    for local in contours(mask):
        if not (bar.height * .21 <= local.height <= bar.height * .84
                and local.top > 0 and local.bottom < bar.height):
            continue
        item = local.offset(bar.left, bar.top)
        if local.left < bar.width * .25 and local.width >= bar.width * .40 and local.width / local.height > 3:
            search = item
        if local.left >= bar.width * .64 and .65 < local.width / local.height < 1.5:
            tile = item.crop(pixels)
            size = max(1, min(item.width, item.height) // 5)
            cx, cy = item.width // 2, item.height // 2
            contrast = ~near_background(tile, 20)
            horizontal = contrast[max(0, cy - 2):cy + 3, cx - size:cx + size + 1]
            vertical = contrast[cy - size:cy + size + 1, max(0, cx - 2):cx + 3]
            if np.mean(np.any(horizontal, axis=0)) >= .7 and np.mean(np.any(vertical, axis=1)) >= .7:
                add = item
    return search, add


def _composer(pixels: np.ndarray, body: Box, spans: tuple[TextSpan, ...], unit: float,
              thresholds: VisualThresholds) -> ComposerObservation:
    bar_height = min(body.height // 3, round(43 * unit))
    bar = Box(body.left, body.top, body.right, body.top + bar_height)
    icons = [item for item in contours(~near_background(bar.crop(pixels), 26))
             if 7 * unit <= item.width <= 36 * unit and 7 * unit <= item.height <= 31 * unit
             and item.top > 0 and item.bottom < bar.height]
    toolbar = (Box(bar.left + min(item.left for item in icons), bar.top + min(item.top for item in icons),
                   bar.left + max(item.right for item in icons), bar.top + max(item.bottom for item in icons))
               if len(icons) >= 3 else None)
    labels = [span for span in spans if re.fullmatch(r"发送\s*(?:[（(][Ss][)）])?", span.text.strip())
              and span.box.center[0] >= body.left + body.width * .72
              and span.box.center[1] >= body.top + body.height * .62
              and span.score >= thresholds.text_score]
    button = None
    appearance = "UNKNOWN"
    if len(labels) == 1:
        label = labels[0].box
        search = Box(max(body.left, label.left - round(32 * unit)),
                     max(body.top, label.top - round(12 * unit)),
                     min(body.right, label.right + round(32 * unit)),
                     min(body.bottom, label.bottom + round(12 * unit)))
        background = np.median(body.crop(pixels)[::4, ::4], axis=(0, 1))
        mask = np.max(np.abs(search.crop(pixels).astype(np.int16) - background), axis=2) >= 13
        candidates = [item.offset(search.left, search.top)
                      for item in contours(mask)
                      if item.width >= label.width + 7 * unit and item.height >= label.height + 3 * unit
                      and item.offset(search.left, search.top).contains(label.center)]
        button = candidates[0] if len(candidates) == 1 else label
        if button != label:
            tile = button.crop(pixels)
            background = np.median(tile, axis=(0, 1))
            appearance = "DARK" if np.mean(background) < 205 or np.ptp(background) >= 36 else "LIGHT"
    draft_spans = [span for span in spans if span.box.center[1] >= bar.bottom and span not in labels
                   and (not button or not button.contains(span.box.center))]
    has_draft = (True if any(span.score >= thresholds.text_score for span in draft_spans)
                 else False if not draft_spans and appearance == "LIGHT" else None)
    return ComposerObservation(toolbar, button, appearance, has_draft)


def _name_key(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).split()).casefold()


_PHONE_METADATA = re.compile(r"(?<![0-9])(?:(?:\+?86)[ \-]*)?1[3-9](?:[ \-]*[0-9]){9}(?![0-9])")
_ORG_METADATA = re.compile(r"(?:有限公司|公司|集团|部门|中心|事业部|工作室|Corporation|Company|Inc\.?|Ltd\.?)$", re.I)


def _title_identity(spans: tuple[TextSpan, ...], thresholds: VisualThresholds
                    ) -> tuple[str, VisualName, tuple[str, ...], bool, str]:
    if not spans:
        return "", VisualName(""), (), False, "C1 无标题 OCR"
    head = min(spans, key=lambda item: (item.box.center[1], item.box.left))
    first_line = tuple(sorted((item for item in spans
                               if abs(head.box.center[1] - item.box.center[1])
                               <= max(head.box.height, item.box.height) * .42),
                              key=lambda item: item.box.left))
    raw = " ".join(item.text.strip() for item in first_line)
    text = unicodedata.normalize("NFKC", raw)
    other = text_lines(tuple(item for item in spans if item not in first_line and item.score >= thresholds.text_score))
    note = "采用 C1 首行姓名"
    phone = _PHONE_METADATA.search(text)
    org_start = next((match.start() for match in re.finditer(r"\s+", text)
                      if re.fullmatch(r"[\u3400-\u9fff]{2,5}", text[:match.start()].strip())
                      and "@" not in text[match.end():]
                      and _ORG_METADATA.search(text[match.end():].strip())), None)
    cuts = [(phone.start(), "按手机号分离身份字段")] if phone else []
    if org_start is not None:
        cuts.append((org_start, "按组织名称后缀分离身份字段"))
    details = list(other)
    later_labels = tuple(tag.strip() for line in other if line.strip().startswith(("@", "＠"))
                         for tag in line.replace("＠", "@").strip().split("@")[1:])
    if cuts:
        index, note = min(cuts, key=lambda entry: entry[0])
        metadata = text[index:].strip()
        details.insert(0, metadata)
        later_labels += tuple(tag.strip() for tag in metadata.split("@")[1:])
        text = text[:index].strip().rstrip("·-—,，:：|").strip()
    parsed = parse_visual_name(text, labels_from_title=later_labels)
    inline_label = "@" in text
    if inline_label and not parsed.issue:
        details.insert(0, text[text.index("@"):])
        if not cuts:
            note = "按可识别的身份标签拆分标题"
    name = parsed.display_name
    truncated = "…" in raw or "..." in raw
    ambiguous = ("|" in name or bool(re.search(r"[\u3400-\u9fff]\s+[\u3400-\u9fff]|\d{5,}", name))
                 or (not cuts and not inline_label and len(first_line) > 1))
    reliable = (bool(name) and any(char.isalpha() for char in name)
                and not truncated and not ambiguous and not parsed.issue
                and min(item.score for item in first_line) >= thresholds.text_score)
    if not reliable:
        note = parsed.issue or "C1 主标题有截断、字段混合或置信不足"
    return raw, parsed, tuple(details), reliable, note


def compare_conversation(title_spans: tuple[TextSpan, ...], contacts: tuple[ContactObservation, ...],
                         thresholds: VisualThresholds) -> ConversationObservation:
    raw, title_identity, details, title_reliable, note = _title_identity(title_spans, thresholds)
    title = title_identity.display_name
    selected = [row for row in contacts if row.is_selected is True]
    row = selected[0] if len(selected) == 1 else None
    name = row.display_name if row else ""
    selected_reliable = bool(row and row.name_reliable)
    if (selected_reliable and title_reliable and _name_key(name) == _name_key(raw)
            and note == "按组织名称后缀分离身份字段"):
        # A group title can legitimately end with an organization name.
        title, details = raw, details[1:]
        note = "完整标题与选中行一致；末尾组织用词属于可见名称"
    state = "UNKNOWN"
    if selected_reliable and title_reliable:
        selected_type, title_type = row.peer_type, title_identity.peer_type
        both_tagged = selected_type != "UNKNOWN" and title_type != "UNKNOWN"
        if both_tagged and (selected_type != title_type or
                            (selected_type == "EXTERNAL_WECOM" and
                             tuple(map(_name_key, row.identity_labels)) != tuple(map(_name_key, title_identity.labels)))):
            note = "B2 与 C1 的明确身份标签冲突"
        else:
            state = "VISUAL_MATCH" if _name_key(name) == _name_key(title) else "CONVERSATION_MISMATCH"
    elif not selected_reliable:
        note += "；" + (row.name_issue if row and row.name_issue else "B2 缺少可靠的选中姓名")
    kind = "GROUP_CANDIDATE" if re.search(r"[（(]\d+\s*人?[）)]$", title) else "UNKNOWN"
    return ConversationObservation(title, details, name, state, kind,
                                   raw_title_line=raw, title_name_reliable=title_reliable,
                                   selected_name_reliable=selected_reliable, comparison_evidence=note,
                                   selected_raw_name=(row.raw_name or name) if row else "",
                                   selected_peer_type=row.peer_type if row else "UNKNOWN",
                                   selected_identity_labels=row.identity_labels if row else (),
                                   title_peer_type=title_identity.peer_type if title_reliable else "UNKNOWN",
                                   title_identity_labels=title_identity.labels,
                                   selection_method=row.selection_method if row else "UNKNOWN",
                                   selection_confidence=("CORROBORATED" if row.selection_method == "SUBTLE_FILL_AND_C1"
                                                         else "HIGH") if row else "UNKNOWN",
                                   selection_evidence=row.selection_evidence if row else "")


def _corroborate_selection(title_spans, contacts, thresholds, mouse):
    if any(row.is_selected is True for row in contacts):
        return contacts, ""
    if any(row.selection_method == "CONFLICTING_HIGHLIGHT" for row in contacts):
        return contacts, "B2 行底与高亮带定位冲突，或存在多个蓝色候选"
    raw, title, _, reliable, note = _title_identity(title_spans, thresholds)
    if not reliable:
        return contacts, "B2 无明确高亮，" + note
    names = {_name_key(title.display_name), _name_key(raw)}
    matches = [row for row in contacts if _name_key(row.display_name) in names]
    if len(matches) != 1 or not matches[0].name_reliable or matches[0].status != "CANDIDATE":
        return contacts, "C1 标题无法唯一核对到完整可靠的 B2 行"
    row = matches[0]
    if row.selection_method != "SUBTLE_ROW_FILL":
        return contacts, "C1 有唯一 B2 名称匹配，缺少独立行底证据 · " + row.selection_evidence
    if mouse and row.box.contains(mouse):
        return contacts, "浅色行与鼠标位置重合，无法排除悬停"
    reason = row.selection_evidence + " · C1 标题与唯一可靠 B2 行一致"
    corroborated = tuple(replace(item, is_selected=item is row, selection_method="SUBTLE_FILL_AND_C1",
                                 selection_evidence=reason) for item in contacts)
    if compare_conversation(title_spans, corroborated, thresholds).match_status != "VISUAL_MATCH":
        return contacts, "浅色行与 C1 身份核对冲突"
    return corroborated, ""


def observe_frame(frame: VisualFrame, calibration: Mapping[str, NormalizedRect],
                  reader: TextRecognizer | None = None,
                  thresholds: VisualThresholds = VisualThresholds(), *, on_conversation=None,
                  contacts_only=False, message_gate=None) -> VisualObservation:
    started = perf_counter()
    pixels = frame.pixels
    layout = detect_layout(pixels, calibration, thresholds)
    unit = max(.65, min(frame.state.dpi / 96, 3.0))
    a, b1, b2, c1, c2, c3 = (layout.get(name) for name in ("A", "B1", "B2", "C1", "C2", "C3"))
    search, add = _toolbar_controls(pixels, b1) if b1 else (None, None)
    hover = next((item.name for item in layout.regions if item.name not in ("B", "C")
                  and frame.mouse and item.box.contains(frame.mouse)), "")
    warnings = [layout.reason] if layout.reason else []
    ocr_status, ocr_reason = "SKIPPED", "缺少可用的文字识别区域"
    if any((a, b2, c1, c2, c3)):
        try:
            reader = reader if reader is not None else LocalTextRecognizer()
            ocr_status, ocr_reason = "INITIALIZED", ""
        except OcrUnavailable as exc:
            ocr_status, ocr_reason = "FAILED", str(exc)
            warnings.append(ocr_reason)
    trace = _ObservedReader(reader)
    title_spans = ()
    if c1 and not contacts_only:
        trace.region = "C1 标题"
        title_area = Box(c1.left, c1.top, c1.left + round(c1.width * .69), c1.bottom)
        title_spans = trace.read(pixels, title_area)
    trace.region = "A 消息入口"
    unread = detect_global_unread(pixels, a, trace, thresholds) if a else GlobalUnreadObservation()
    trace.region = "B2 联系人"
    contact_diagnostics: list[ContactTextDiagnostic] = []
    contacts = detect_contacts(pixels, b2, trace, unit, thresholds, contact_diagnostics) if b2 else ()
    if contacts_only:
        page = "MESSAGES_CANDIDATE" if b2 and (unread.entry or (search and add and contacts)) else "UNKNOWN"
        return VisualObservation(frame, layout, "CONTACTS_ONLY", page=page, contacts=contacts,
                                 search_box=search, add_button=add, global_unread=unread,
                                 ocr_areas=tuple(trace.observations), ocr_status=ocr_status,
                                 recognition_ms=(perf_counter() - started) * 1000)
    contacts, selection_issue = _corroborate_selection(title_spans, contacts, thresholds, frame.mouse)
    conversation = compare_conversation(title_spans, contacts, thresholds)
    if selection_issue:
        fills = [row.selection_evidence for row in contacts if row.selection_method == "SUBTLE_ROW_FILL"]
        conversation = replace(conversation, selection_evidence=" · ".join((selection_issue, *fills)))
    selected = next((row for row in contacts if row.is_selected), None)
    board = layout.get("D")
    if board or conversation.conversation_type == "GROUP_CANDIDATE" or (selected and selected.kind == "GROUP_CANDIDATE"):
        conversation = replace(conversation, conversation_type="GROUP")
    elif conversation.selected_peer_type in ("PERSONAL_WECHAT", "EXTERNAL_WECOM") or (
            conversation.title_peer_type in ("PERSONAL_WECHAT", "EXTERNAL_WECOM")):
        conversation = replace(conversation, conversation_type="ONE_TO_ONE")
    if on_conversation:
        on_conversation(VisualObservation(frame, layout, "VERIFYING_CONVERSATION",
                                           contacts=contacts, conversation=conversation))
    trace.region = "C3 输入区"
    composer = _composer(pixels, c3, () if message_gate is not None else trace.read(pixels, c3),
                         unit, thresholds) if c3 else None
    corroborated = bool(conversation.title and ((composer and composer.send_button)
                       or (search and add and contacts) or unread.entry or conversation.selected_name))
    if message_gate is not None:
        header = VisualObservation(frame, layout, "HEADER_ONLY",
                                   page="MESSAGES_CANDIDATE" if corroborated else "UNKNOWN",
                                   contacts=contacts, conversation=conversation, composer=composer,
                                   search_box=search, add_button=add, global_unread=unread,
                                   ocr_areas=tuple(trace.observations), ocr_status=ocr_status,
                                   recognition_ms=(perf_counter() - started) * 1000)
        if not message_gate(header):
            return header
    messages = context = candidates = ()
    anchors = (None, None)
    dispositions = ()
    completeness = "VISIBLE_MESSAGES_UNVERIFIED"
    if c2:
        containers = find_containers(pixels, c2, unit, thresholds)
        trace.region = "C2 历史消息"
        explicit = any(item.name == "C2" and "人工相对标定" in item.evidence for item in layout.regions)
        spans = trace.read(pixels, c2) if corroborated or explicit else ()
        if not (corroborated or explicit):
            trace.observations.append(OcrAreaObservation(trace.region, c2, "SKIPPED",
                                                        reason="缺少消息页证据"))
        analysis = analyze_messages(pixels, c2, containers, spans, unit, calibration, thresholds,
                                    page_verified=corroborated or explicit)
        messages, context, anchors, dispositions = (analysis.messages, analysis.context,
                                                     analysis.anchors, analysis.dispositions)
        if conversation.conversation_type == "GROUP":
            messages, dispositions = _group_senders(messages, dispositions, unit, thresholds.text_score)
            context = tuple(item.span for item in dispositions if item.kind in ("TIME", "SYSTEM", "UNASSIGNED"))
        by_box = {item.box: item for item in messages}
        observations = []
        for container in analysis.containers:
            _, left, right = direction_evidence(container.box, c2, anchors, thresholds)
            item = by_box.get(container.box)
            reason = (item.status if item else
                      "颜色轮廓近似头像，缺少可归属 OCR 证据" if container.needs_text else
                      "未通过消息分类或页面校验")
            observations.append(MessageCandidateObservation(
                container, item.direction if item else "UNKNOWN", left, right, item.sequence if item else None, reason))
        candidates = tuple(observations)
        if (any(item.possible_message for item in dispositions)
                or any(item.status != "CANDIDATE" for item in messages)
                or any(step.status == "FAILED" for step in trace.observations if step.region.startswith("C2"))):
            completeness = "VISIBLE_MESSAGES_INCOMPLETE"
            warnings.append("存在未能确认或归属的可见正文，详见 C2 文字归属。")
        elif any(item.kind in ("TEXT", "IMAGE", "FILE") for item in messages):
            completeness = "VISIBLE_MESSAGES_ACCOUNTED_FOR"
    if any(step.status == "FAILED" for step in trace.observations):
        warnings.append("部分 OCR 裁剪失败。其余区域结果已保留。")
    if conversation.match_status == "CONVERSATION_MISMATCH":
        warnings.append("选中联系人与右侧标题不一致。消息仅作为右侧区域观察，无法归属到该联系人。")
    elif conversation.match_status != "VISUAL_MATCH":
        warnings.append("缺少可核对的选中行或标题。本次消息没有可靠会话归属。")
    if c2 and not candidates:
        warnings.append("C2 没有满足容器判据的消息框。"
                        + ("已识别未归属正文候选。" if any(item.possible_message for item in dispositions) else ""))
    if not c2:
        warnings.append("C2 未定位，未对标题或草稿作历史消息解析。")
    if not corroborated:
        warnings.append("页面证据不足。独立区域结果仍可核对。")
    status = ("LAYOUT_UNCERTAIN" if layout.failures else
              "OCR_UNAVAILABLE" if ocr_status == "FAILED" else
              "CONVERSATION_MISMATCH" if conversation.match_status == "CONVERSATION_MISMATCH" else
              "CANDIDATE" if corroborated else "PAGE_UNCERTAIN")
    return VisualObservation(
        frame, layout, status, page="MESSAGES_CANDIDATE" if corroborated else "UNKNOWN",
        contacts=contacts, conversation=replace(conversation, completeness=completeness),
        messages=messages, context_text=context, search_box=search, add_button=add, composer=composer,
        list_scrollbar=detect_scroll_thumb(pixels, b2, unit) if b2 else None,
        history_scrollbar=detect_scroll_thumb(pixels, c2, unit) if c2 else None,
        hovered_region=hover, warnings=tuple(warnings), global_unread=unread, candidates=candidates,
        ocr_status=ocr_status, ocr_reason=ocr_reason, ocr_areas=tuple(trace.observations),
        recognition_ms=(perf_counter() - started) * 1000, anchors=anchors, message_ocr=dispositions,
        contact_text_diagnostics=tuple(contact_diagnostics), message_completeness=completeness,
        board_scrollbar=detect_scroll_thumb(pixels, board, unit) if board else None)


def read_visible_conversation(calibration_path: Path | None,
                              thresholds_path: Path | None = None) -> VisualObservation:
    thresholds = VisualThresholds.load(thresholds_path)
    frame = capture_visible_window()
    calibration = CalibrationStore(calibration_path).load() if calibration_path else {}
    cv2.setNumThreads(2)
    return observe_frame(frame, calibration, thresholds=thresholds)
