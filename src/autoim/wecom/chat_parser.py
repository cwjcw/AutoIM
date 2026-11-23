"""Conservative parser for text copied from WeCom desktop chat history."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

PeerType = Literal["INTERNAL_WECOM", "EXTERNAL_WECOM", "PERSONAL_WECHAT", "UNKNOWN"]

_HEADER_RE = re.compile(
    r"^(?P<sender>.+)\s+(?P<time>\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2}:\d{2})\s*$"
)
_HEADER_SUFFIX_RE = re.compile(r"\s+\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2}:\d{2}\s*$")
_TIME_RE = re.compile(r"^(?P<month>\d{1,2})/(?P<day>\d{1,2})\s+(?P<hour>\d{1,2}):(?P<minute>\d{2}):(?P<second>\d{2})$")
_ORG_SUFFIX_RE = re.compile(r"(公司|有限公司|集团|企业|制品厂|工厂|中心|研究院|事务所|工作室)$")


@dataclass(frozen=True, slots=True)
class ParsedMessage:
    local_record_id: str
    sequence_number: int
    raw_header: str
    raw_sender: str
    display_name: str
    peer_type: PeerType
    organization_name: str | None
    raw_time: str
    parsed_datetime: datetime | None
    date_year_reliable: bool
    content: str
    raw_message_block: str
    parse_status: str
    parse_warnings: tuple[str, ...] = ()
    identity_evidence: tuple[str, ...] = ()
    contact_id: None = None
    conversation_id: None = None
    direction: Literal["UNKNOWN"] = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class ParseResult:
    raw_text: str
    messages: tuple[ParsedMessage, ...]
    parse_status: str
    warnings: tuple[str, ...] = ()

    @property
    def ambiguous_count(self) -> int:
        return sum(1 for message in self.messages
                   if message.parse_status != "PARSED" or message.peer_type == "UNKNOWN")


def _parse_sender(raw_sender: str) -> tuple[str, PeerType, str | None, tuple[str, ...], tuple[str, ...]]:
    normalized = raw_sender.replace("＠", "@")
    if "@" not in normalized:
        return normalized.strip(), "UNKNOWN", None, (), ("仅有发送人姓名，缺少可靠身份标记；无法确认是否为本公司员工。",)

    parts = [part.strip() for part in normalized.split("@")]
    display_name = parts[0]
    tags = [part for part in parts[1:] if part]
    personal_tags = {tag for tag in tags if tag in {"微信", "微信联系人"}}
    org_tags = [tag for tag in tags if tag not in {"微信", "微信联系人"}]
    evidence: list[str] = []
    warnings: list[str] = []
    if personal_tags and org_tags:
        warnings.append("同时出现个人微信与其他身份标记，身份冲突，分类为 UNKNOWN。")
        return display_name, "UNKNOWN", None, ("原始发送人包含冲突身份标记。",), tuple(warnings)
    if personal_tags:
        evidence.append("发送人包含明确的 @微信 或 @微信联系人 标记。")
        if len(personal_tags) > 1:
            evidence.append("保留并识别组合标签 @微信@微信联系人。")
        return display_name, "PERSONAL_WECHAT", None, tuple(evidence), ()
    if len(org_tags) == 1 and _ORG_SUFFIX_RE.search(org_tags[0]):
        evidence.append("发送人包含带有完整企业组织后缀的身份标记。")
        return display_name, "EXTERNAL_WECOM", org_tags[0], tuple(evidence), ()
    warnings.append("身份标签未知或企业名称不完整，分类为 UNKNOWN。")
    return display_name, "UNKNOWN", None, (), tuple(warnings)


def _valid_header(line: str) -> re.Match[str] | None:
    if not _HEADER_SUFFIX_RE.search(line):
        return None
    match = _HEADER_RE.fullmatch(line.strip())
    if not match or not match.group("sender").strip():
        return None
    time_match = _TIME_RE.fullmatch(match.group("time"))
    if not time_match:
        return None
    try:
        datetime(
            2000, int(time_match.group("month")), int(time_match.group("day")),
            int(time_match.group("hour")), int(time_match.group("minute")), int(time_match.group("second")),
        )
    except ValueError:
        return None
    return match


def _trim_separator_blank_lines(lines: list[str]) -> list[str]:
    start, end = 0, len(lines)
    while start < end and lines[start] == "":
        start += 1
    while end > start and lines[end - 1] == "":
        end -= 1
    return lines[start:end]


def _raw_line_spans(text: str) -> list[tuple[int, int]]:
    if not text:
        return []
    parts = re.split(r"(\r\n|\r|\n)", text)
    spans: list[tuple[int, int]] = []
    offset = 0
    for index in range(0, len(parts), 2):
        separator = parts[index + 1] if index + 1 < len(parts) else ""
        end = offset + len(parts[index]) + len(separator)
        spans.append((offset, end))
        offset = end
    return spans


def parse_wecom_clipboard(text: str) -> ParseResult:
    """Parse copied message blocks without inventing identity, year, or direction.

    Header-like lines are used as candidate boundaries only when the next line is
    blank (or the candidate is the first nonblank line). Such boundaries remain
    explicitly marked ambiguous because message text can imitate this syntax.
    """
    if not isinstance(text, str):
        raise TypeError("剪贴板内容必须是 str")
    if not text:
        return ParseResult(text, (), "EMPTY", ("剪贴板文本为空。",))

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    raw_spans = _raw_line_spans(text)
    candidates: list[int] = []
    for index, line in enumerate(lines):
        if not _valid_header(line):
            continue
        next_line_is_blank = index + 1 == len(lines) or lines[index + 1] == ""
        previous_line_is_blank = index == 0 or lines[index - 1] == ""
        if next_line_is_blank and previous_line_is_blank:
            candidates.append(index)

    if not candidates:
        has_header_like_line = any(_valid_header(line) for line in lines)
        warning = ("发现类似消息头，但缺少可确认的空行分隔；复制范围可能不完整，未猜测拆分。原始文本已保留。"
                   if has_header_like_line else "未发现符合企业微信复制格式的完整消息头；原始文本已保留。")
        return ParseResult(text, (), "UNRECOGNIZED", (warning,))

    leading = 0
    while leading < len(lines) and lines[leading] == "":
        leading += 1
    if candidates[0] > leading:
        prefix = "\n".join(lines[leading:candidates[0]]).strip()
        if prefix:
            return ParseResult(text, (), "UNRECOGNIZED", ("消息头之前存在无法归属的文本；未猜测其发送人，原始文本已保留。",))

    messages: list[ParsedMessage] = []
    overall_warnings: list[str] = []
    for sequence, line_start in enumerate(candidates, 1):
        line_end = candidates[sequence] if sequence < len(candidates) else len(lines)
        header_match = _valid_header(lines[line_start])
        assert header_match is not None
        raw_header = lines[line_start]
        raw_sender = header_match.group("sender").strip()
        raw_time = header_match.group("time")
        body_lines = lines[line_start + 1:line_end]
        content_lines = _trim_separator_blank_lines(body_lines)
        content = "\n".join(content_lines)
        raw_block_end = raw_spans[line_end][0] if line_end < len(raw_spans) else len(text)
        block = text[raw_spans[line_start][0]:raw_block_end] if line_start < len(raw_spans) else ""

        display_name, peer_type, organization, evidence, identity_warnings = _parse_sender(raw_sender)
        warnings = list(identity_warnings)
        status = "PARSED"
        overall_warnings.extend(f"第 {sequence} 条消息身份不确定：{warning}" for warning in identity_warnings)
        if sequence > 1:
            # Text and a copied message header share the same shape. Surface this
            # uncertainty instead of claiming the split is proven.
            warning = "此处由符合格式的候选消息头分隔；正文也可能包含同形文本，边界需人工核对。"
            warnings.append(warning)
            overall_warnings.append(f"第 {sequence} 条消息边界存在候选歧义。")
            status = "AMBIGUOUS_BOUNDARY"
        if not content:
            warnings.append("消息头后没有可见正文，复制范围可能不完整。")
            status = "PARTIAL"
        messages.append(ParsedMessage(
            local_record_id=str(uuid.uuid4()), sequence_number=sequence,
            raw_header=raw_header, raw_sender=raw_sender, display_name=display_name,
            peer_type=peer_type, organization_name=organization, raw_time=raw_time,
            parsed_datetime=None, date_year_reliable=False, content=content,
            raw_message_block=block, parse_status=status,
            parse_warnings=tuple(warnings), identity_evidence=evidence,
        ))

    # A syntactically plausible message header without a blank separator is not
    # consumed as a boundary; call out possible truncation rather than discarding it.
    if len(candidates) < sum(1 for line in lines if _valid_header(line)):
        overall_warnings.append("发现未满足消息分隔条件的类似消息头；文本可能截断或正文含类似消息头，原始文本已保留。")
    if len(text) > 1_000_000:
        overall_warnings.append("输入超过 1 MB；已按原文解析，界面展示可能较慢。")
    return ParseResult(text, tuple(messages), "PARSED_WITH_WARNINGS" if overall_warnings else "PARSED", tuple(overall_warnings))
