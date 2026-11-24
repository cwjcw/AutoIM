"""Parse trusted visual time labels without inventing per-message timestamps."""

from dataclasses import dataclass
from datetime import datetime, time, timedelta
import re
import unicodedata

_CLOCK = r"(?:[01]?\d|2[0-3]):[0-5]\d"
TIME_LABEL = re.compile(rf"(?P<prefix>今天|昨天|星期[一二三四五六日天]|\d{{1,2}}月\d{{1,2}}日|"
                        rf"\d{{1,2}}/\d{{1,2}}|\d{{4}}-\d{{2}}-\d{{2}})?(?P<clock>{_CLOCK})|刚刚|\d{{1,4}}分钟前")


def normalized_time(text):
    return "".join(unicodedata.normalize("NFKC", text).split())


@dataclass(frozen=True, slots=True)
class MessageTime:
    raw: str = ""
    time_source: str = "UNKNOWN"
    precision: str = "UNKNOWN"
    anchor: str | None = None


def parse_message_time(raw, observed, *, source="GROUP_ANCHOR", prior=None, recent=False):
    value = normalized_time(raw)
    match = TIME_LABEL.fullmatch(value)
    if not raw or not match:
        return MessageTime(raw)
    if value.endswith("分钟前") or value == "刚刚":
        # Relative minute indicators are approximate and can drift between captures.
        return MessageTime(raw, source, "RELATIVE_MINUTE")
    hour, minute = map(int, match["clock"].split(":"))
    prefix = match["prefix"] or ""
    day = None
    try:
        if prefix in ("今天", "昨天"):
            day = observed.date() - timedelta(days=int(prefix == "昨天"))
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", prefix):
            day = datetime.strptime(prefix, "%Y-%m-%d").date()
        elif prefix.startswith("星期") and recent:
            weekday = "一二三四五六日".index(prefix[-1].replace("天", "日"))
            day = observed.date() - timedelta(days=(observed.weekday() - weekday) % 7)
        elif prefix and (prior or recent):
            month, date = map(int, re.findall(r"\d+", prefix))
            year = prior.year if prior else observed.year
            day = datetime(year, month, date).date()
            if not prior and day > observed.date():
                day = datetime(year - 1, month, date).date()
            if not prior and observed.date() - day > timedelta(days=31):
                day = None
        elif not prefix and prior:
            day = prior.date()
    except ValueError:
        return MessageTime(raw)
    if day is None:
        return MessageTime(raw, source, "CLOCK_WITHOUT_DATE")
    stamp = datetime.combine(day, time(hour, minute), observed.tzinfo)
    if stamp > observed or (not prefix and prior and stamp < prior):
        return MessageTime(raw, source, "CLOCK_WITHOUT_DATE")
    return MessageTime(raw, source, "MINUTE", stamp.isoformat(timespec="minutes"))
