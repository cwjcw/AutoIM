"""C2 visual time labels interpreted relative to their original local capture."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import re
import unicodedata


_CLOCK = r"[0-9]{1,2}:[0-9]{2}"
_MONTH_DAY = r"(?:[0-9]{1,2}/[0-9]{1,2}|[0-9]{1,2}月[0-9]{1,2}日)"
_YEAR_DAY = r"[0-9]{4}-[0-9]{2}-[0-9]{2}"
_DATE = rf"(?:{_YEAR_DAY}|{_MONTH_DAY})"
TIME_LABEL = re.compile(rf"(?:(?P<relative>今天|昨天|星期[一二三四五六日天])(?P<weekday_clock>{_CLOCK})|"
                        rf"(?P<calendar>{_DATE})(?: (?P<calendar_clock>{_CLOCK}))?|(?P<clock>{_CLOCK})|"
                        r"(?P<approximate>刚刚|[0-9]{1,4}分钟前))")


def normalized_time(text):
    return "".join(unicodedata.normalize("NFKC", text).split())


def normalize_time_label(raw):
    """Correct only unambiguous OCR variants of a visually classified time label."""
    formatted = unicodedata.normalize("NFKC", raw).strip()
    pieces = re.fullmatch(r"([0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{1,2}\s*/\s*[0-9]{1,2}|"
                         r"[0-9]{1,2}\s*月\s*[0-9]{1,2}\s*日)\s+([0-9\s]+:\s*[0-9]{2})", formatted)
    value = " ".join(map(normalized_time, pieces.groups())) if pieces else normalized_time(raw)
    rules = ["NFKC_OR_WHITESPACE"] if value != raw else []
    if re.fullmatch(rf"星期[—–−-]{_CLOCK}", value):
        value = value[:2] + "一" + value[3:]
        rules.append("WEEKDAY_ONE_HORIZONTAL_STROKE")
    return value, tuple(rules)


def is_time_label(raw):
    return bool(TIME_LABEL.fullmatch(normalize_time_label(raw)[0]))


@dataclass(frozen=True, slots=True)
class MessageTime:
    raw: str = ""
    time_source: str = "UNKNOWN"
    precision: str = "UNKNOWN"
    anchor: str | None = None
    normalized: str = ""
    correction: tuple[str, ...] = ()
    year_inferred: bool = False
    issue: str = ""
    format: str = "UNKNOWN"
    local_date: str | None = None


def _infer_date(value, observed):
    if "-" in value:
        return date.fromisoformat(value), False
    month, day = map(int, re.findall(r"[0-9]+", value))
    for year in (observed.year, observed.year - 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if 0 <= (observed - candidate).days <= 366:
            return candidate, True
    raise ValueError("No supported calendar year")


def parse_message_time(raw, observed, *, source="GROUP_ANCHOR", prior=None):
    value, corrections = normalize_time_label(raw)
    match = TIME_LABEL.fullmatch(value)
    if not raw or not match:
        return MessageTime(raw, normalized=value, correction=corrections,
                           issue="INVALID_FORMAT" if raw else "")
    label = match["relative"] or ""
    calendar = match["calendar"]
    clock = match["clock"] or match["weekday_clock"] or match["calendar_clock"]
    family = ("MONTH_DAY" if calendar and "-" not in calendar else "FULL_DATE" if calendar else
              "WEEKDAY" if label.startswith("星期") else "YESTERDAY" if label == "昨天" else
              "RELATIVE" if match["approximate"] else "TODAY")
    evidence = {"normalized": value, "correction": corrections, "format": family}
    if match["approximate"]:
        return MessageTime(raw, source, "RELATIVE_MINUTE", **evidence)
    today = observed.date()
    inferred = False
    try:
        if calendar:
            day, inferred = _infer_date(calendar, today)
        elif label.startswith("星期"):
            weekday = "一二三四五六日".index(label[-1].replace("天", "日"))
            days = (today.weekday() - weekday) % 7
            if days < 2:
                return MessageTime(raw, source, issue="WEEKDAY_BOUNDARY_UNVERIFIED", **evidence)
            day = today - timedelta(days=days)
        else:
            day = today - timedelta(days=int(label == "昨天"))
        if clock:
            hour, minute = map(int, clock.split(":"))
            stamp = datetime.combine(day, time(hour, minute), observed.tzinfo)
        else:
            stamp = None
    except ValueError:
        return MessageTime(raw, source, issue="INVALID_DATE_OR_CLOCK", **evidence)
    evidence.update(year_inferred=inferred)
    if day > today or stamp and stamp > observed:
        return MessageTime(raw, source, issue="AFTER_CAPTURE", **evidence)
    if prior and (day < prior.date() or stamp and stamp < prior):
        return MessageTime(raw, source, issue="REVERSED_SEQUENCE", **evidence)
    if stamp is None:
        return MessageTime(raw, source, "DATE", local_date=day.isoformat(), **evidence)
    return MessageTime(raw, source, "MINUTE", stamp.isoformat(timespec="minutes"),
                       local_date=day.isoformat(), **evidence)
