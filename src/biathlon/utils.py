"""Common utilities for the Biathlon CLI."""

from __future__ import annotations

import datetime
import re

from .api import BiathlonError, get_analytic_results, get_race_results
from .formatting import format_seconds


def parse_date(value: str | None) -> datetime.date | None:
    """Parse an ISO date string (with optional time) to a date object."""
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(value.split("T", 1)[0])
    except ValueError:
        return None


def parse_start_datetime(value: str | None) -> datetime.datetime | None:
    """Parse an ISO datetime string (allowing Z suffix) into a timezone-aware datetime."""
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    # Replace Z with +00:00 for proper timezone parsing
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        if "T" in text:
            dt = datetime.datetime.fromisoformat(text)
        else:
            dt = datetime.datetime.fromisoformat(f"{text}T00:00:00")
        # Ensure timezone-aware (assume UTC if naive)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt
    except ValueError:
        return None


def parse_time_seconds(value: str | None) -> float | None:
    """Convert a time string to seconds (supports +diff, mm:ss.d, hh:mm:ss.d)."""
    if not value:
        return None
    text = value.strip()
    if text.startswith("+"):
        text = text[1:]
    parts = text.split(":")
    try:
        if len(parts) == 1:
            return float(parts[0])
        if len(parts) == 2:
            minutes = int(parts[0])
            seconds = float(parts[1])
            return minutes * 60 + seconds
        if len(parts) == 3:
            hours = int(parts[0])
            minutes = int(parts[1])
            seconds = float(parts[2])
            return hours * 3600 + minutes * 60 + seconds
    except ValueError:
        return None
    return None


def parse_csv_values(value: str) -> list[str]:
    """Split a comma-separated string into trimmed parts."""
    parts = [part.strip() for part in value.split(",")]
    return [part for part in parts if part]


def parse_misses(value: str | None) -> int | None:
    """Parse miss count from ShootingTotal-like strings."""
    if value is None:
        return None
    text = str(value)
    if not text:
        return None
    digits = re.findall(r"\d+", text)
    if not digits:
        return None
    return sum(int(d) for d in digits)


def parse_relay_shooting(value: str | None) -> tuple[int, int] | None:
    """Parse relay shooting format 'P+S' into (penalties, spares) tuple.

    In relays, format is 'penalties+spare_rounds' e.g. '2+3' means
    2 penalty loops and 3 spare rounds used.
    """
    if not value:
        return None
    text = str(value).strip()
    if "+" not in text:
        return None
    parts = text.split("+")
    if len(parts) != 2:
        return None
    try:
        return (int(parts[0]), int(parts[1]))
    except ValueError:
        return None


def parse_relay_shootings(
    value: str | None,
) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """Parse relay Shootings format 'P+S P+S' into ((prone_p, prone_s), (stand_p, stand_s)).

    Returns tuple of (prone, standing) where each is (penalties, spares).
    """
    if not value:
        return None
    text = str(value).strip()
    parts = text.split()
    if len(parts) != 2:
        return None
    prone = parse_relay_shooting(parts[0])
    standing = parse_relay_shooting(parts[1])
    if prone is None or standing is None:
        return None
    return (prone, standing)


def format_relay_shooting(penalties: int, spares: int) -> str:
    """Format penalties and spares as 'P+S' string."""
    return f"{penalties}+{spares}"


def add_relay_shootings(
    shootings: list[tuple[int, int] | None],
) -> tuple[int, int]:
    """Sum multiple relay shooting tuples into total (penalties, spares)."""
    total_p, total_s = 0, 0
    for s in shootings:
        if s:
            total_p += s[0]
            total_s += s[1]
    return (total_p, total_s)


def is_dns(result: dict) -> bool:
    """Return True if result represents DNS (did not start) or similar."""
    irm = str(result.get("IRM") or "").upper()
    res_val = str(result.get("Result") or result.get("TotalTime") or "").upper()
    return irm == "DNS" or res_val == "DNS"


def get_first_time(result: dict, keys: list[str]) -> str:
    """Return the first non-empty time-like field from the result."""
    for key in keys:
        value = result.get(key)
        if value:
            return str(value)
    return ""


def extract_results(payload: dict) -> list[dict]:
    """Return sorted, non-team results list from a race payload."""
    candidates = [
        payload.get("Results"),
        payload.get("ResultList"),
        payload.get("Competitors"),
        payload.get("Data"),
    ]
    results = next((r for r in candidates if isinstance(r, list)), [])
    filtered = [res for res in results if not res.get("IsTeam")]
    filtered.sort(
        key=lambda res: (
            int(res.get("Rank")) if str(res.get("Rank", "")).isdigit() else 10**9,
            res.get("ResultOrder", 10**9),
        )
    )
    return filtered


def base_time_seconds(results: list[dict]) -> float | None:
    """Return the winner's absolute time in seconds if available."""
    for res in results:
        candidate = res.get("TotalTime") or res.get("Result")
        if candidate and not str(candidate).startswith("+"):
            parsed = parse_time_seconds(str(candidate))
            if parsed is not None:
                return parsed
    return None


def normalize_result_time(result: dict, base_seconds: float | None) -> str:
    """Convert result/total time to absolute clock if diff-based and base is known."""
    raw = get_first_time(result, ["Result", "TotalTime"]) or "-"
    if raw.startswith("+") and base_seconds is not None:
        diff = parse_time_seconds(raw)
        if diff is not None:
            return format_seconds(base_seconds + diff)
        return raw
    parsed = parse_time_seconds(raw)
    if parsed is not None:
        return format_seconds(parsed)
    return raw


def result_seconds(result: dict, base_seconds: float | None) -> float | None:
    """Return absolute time in seconds for a result row."""
    raw = get_first_time(result, ["Result", "TotalTime"])
    if not raw:
        return None
    if str(raw).upper() == "DNS":
        return None
    if str(raw).startswith("+") and base_seconds is not None:
        diff = parse_time_seconds(str(raw))
        if diff is None:
            return None
        return base_seconds + diff
    return parse_time_seconds(str(raw))


def sort_rows(rows: list[dict], column: str | None) -> list[dict]:
    """Sort rows by a given time column, pushing DNS/non-times to bottom."""
    if not column:
        return rows

    def key(row: dict) -> tuple:
        val = row.get(column)
        dns_flag = row.get("dns")
        if dns_flag:
            return (2, float("inf"), val)
        sec = parse_time_seconds(str(val)) if val not in ("", None, "-") else None
        if sec is None:
            return (1, float("inf"), val)
        return (0, sec, val)

    return sorted(rows, key=key)


def build_analytic_times(
    race_id: str,
    total_type: str,
    lap_prefix: str,
    lap_suffix: str,
    laps: int,
) -> dict:
    """Fetch analytic times per athlete keyed by IBUId/Bib."""
    times: dict = {}

    def merge(type_id: str, key: str) -> None:
        try:
            analytic = get_analytic_results(race_id, type_id)
        except BiathlonError:
            return
        for res in analytic.get("Results", []):
            if res.get("IsTeam"):
                continue
            ident = res.get("IBUId") or res.get("Bib") or res.get("Name")
            if not ident:
                continue
            times.setdefault(ident, {})[key] = get_first_time(
                res, ["TotalTime", "Result"]
            )

    merge(total_type, "total")
    for idx in range(1, laps + 1):
        type_id = f"{lap_prefix}{idx}{lap_suffix}"
        merge(type_id, f"lap{idx}")
    return times


def get_race_label(race: dict) -> str:
    """Return a display label for a race."""
    return (
        race.get("RaceName")
        or race.get("ShortDescription")
        or race.get("Description")
        or ""
    )


def get_event_label(event: dict) -> str:
    """Return a display label for an event."""
    return event.get("ShortDescription") or event.get("Organizer") or ""


def get_race_start_key(race: dict) -> str:
    """Return a sortable start key for a race."""
    return (
        race.get("StartTime") or race.get("StartDate") or race.get("FirstStart") or ""
    )


def format_race_header(payload: dict, race_id: str) -> str:
    """Return a descriptive header for a race including location when available."""
    comp = payload.get("Competition") or {}
    sport_evt = payload.get("SportEvt") or {}

    race_label = comp.get("ShortDescription") or comp.get("Description") or race_id
    event_label = sport_evt.get("ShortDescription") or sport_evt.get("Organizer") or ""

    start = comp.get("StartTime") or comp.get("StartDate") or ""
    date_part, time_part = "", ""
    start_dt = parse_start_datetime(str(start))
    if start_dt is not None:
        local_dt = start_dt.astimezone()
        date_part = local_dt.strftime("%Y-%m-%d")
        tz_name = local_dt.tzname() or ""
        time_part = f"{local_dt.strftime('%H:%M')} {tz_name}".strip()
    elif isinstance(start, str) and "T" in start:
        date_part, rest = start.split("T", 1)
        time_part = rest.rstrip("Z")
    location = f" — {event_label}" if event_label else ""
    when = " ".join(part for part in (date_part, time_part) if part)
    base = f"# {race_label}{location}".strip()
    if when:
        base = f"{base} {when}"
    return f"{base} ({race_id})".strip()


def format_result_row(
    result: dict, analytic_times: dict, base_secs: float | None
) -> str:
    """Format a single competitor result with rank and finish time details."""
    rank = result.get("Rank") or result.get("ResultOrder") or ""
    name = result.get("Name") or result.get("ShortName") or ""
    result_time = normalize_result_time(result, base_secs)
    identifier = result.get("IBUId") or result.get("Bib") or name
    times = analytic_times.get(identifier, {})
    course_time = (
        times.get("course")
        or get_first_time(result, ["TotalCourseTime", "CourseTime", "RunTime"])
        or "-"
    )
    range_time = (
        times.get("range")
        or get_first_time(result, ["TotalRangeTime", "RangeTime"])
        or "-"
    )
    shooting_time = (
        times.get("shooting")
        or get_first_time(result, ["TotalShootingTime", "ShootingTime"])
        or "-"
    )
    return (
        f"{rank}\t{name}\t{result_time}\t{course_time}\t{range_time}\t{shooting_time}"
    )


def resolve_race(race_arg: str, find_latest_func) -> tuple[str, dict]:
    """Return race id and payload, defaulting to latest race with results."""
    if race_arg:
        race_id = race_arg
        payload = get_race_results(race_id)
    else:
        race_id, payload = find_latest_func()
    return race_id, payload
