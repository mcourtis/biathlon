"""Brief command handlers for race analysis."""

from __future__ import annotations

import argparse
import datetime
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, cast

from ..api import (
    BiathlonError,
    get_all_results,
    get_cups,
    get_cup_results,
    get_events,
    get_race_results,
    get_races,
    get_current_season_id,
    get_seasons,
)
from ..constants import (
    CATEGORY_DISPLAY_NAMES,
    DISCIPLINE_NAMES,
    EVENT_TYPE_LABELS,
    EVENT_TYPE_OWG,
    EVENT_TYPE_WC,
    EVENT_TYPE_WCH,
    RELAY_DISCIPLINES,
)
from ..formatting import (
    is_pretty_output,
    get_output_format,
    Color,
    render_table,
    OutputFormat,
)
from ..utils import format_race_header, parse_date, parse_start_datetime
from .events import compute_event_styles, format_level

from ._common import (
    DISCIPLINE_LEADER_MARKER,
    GENERAL_LEADER_MARKER,
    _format_leader_markers,
    _format_section_title,
    _has_completed_relay_results,
    _max_workers,
    _ordinal,
    _parse_rank,
    _row_ibu_id,
    detect_event_type,
    is_relay_discipline,
    _select_race_interactive,
)
from .postrace import (
    handle_post_race,
    _is_result_at_or_before_target,
    _is_team_level_result,
    _result_discipline_id,
    _start_dt_from_competition,
)
from .results import _has_completed_results
from .standings import (
    ATHLETE_STANDINGS_COLUMN_SEPARATORS,
    ATHLETE_STANDINGS_HEADERS,
    _age_on_date,
    _extract_age_text,
    _extract_birth_date,
    _prefetch_bios,
)
from .startlist import (
    _build_startlist_entries,
    _build_team_entries,
    _country_display,
    _event_country_display,
    _extract_venue_name,
    _find_all_startlist_races,
    _get_wc_points,
    _is_true,
    _prepare_startlist_context,
    render_startlist_analysis,
)


def _find_current_event() -> dict | None:
    """Find the current or next upcoming level-1 event.

    Returns the event dict or None if no suitable event found.
    """
    season_id = get_current_season_id()
    events = get_events(season_id, level=1)
    today = datetime.date.today()

    # Sort events by start date
    dated_events = []
    for event in events:
        start_raw = event.get("StartDate") or ""
        if not start_raw:
            continue
        start_str = start_raw.split("T", 1)[0] if isinstance(start_raw, str) else ""
        if not start_str:
            continue
        try:
            start_date = datetime.date.fromisoformat(start_str)
        except ValueError:
            continue
        end_raw = event.get("EndDate") or start_raw
        end_str = end_raw.split("T", 1)[0] if isinstance(end_raw, str) else start_str
        try:
            end_date = datetime.date.fromisoformat(end_str)
        except ValueError:
            end_date = start_date
        dated_events.append((start_date, end_date, event))

    dated_events.sort(key=lambda x: x[0])

    # Find current event (today is between start and end)
    for start_date, end_date, event in dated_events:
        if start_date <= today <= end_date:
            return event

    # Find next upcoming event
    for start_date, end_date, event in dated_events:
        if start_date > today:
            return event

    return None


def _find_first_race_for_event(
    event_id: str, cat_id: str = "SW"
) -> tuple[str, dict] | None:
    """Find the first race for an event to use as reference.

    Returns (race_id, payload) or None if no race found.
    """
    try:
        races = get_races(event_id)
    except BiathlonError:
        return None

    # Filter by category and sort by start time
    matching_races = []
    for race in races:
        race_cat = str(race.get("catId") or race.get("CatId") or "").upper()
        if race_cat != cat_id:
            continue
        race_id = race.get("RaceId") or race.get("Id")
        if not race_id:
            continue
        start_raw = race.get("StartTime") or race.get("StartDate") or ""
        matching_races.append((start_raw, race_id))

    if not matching_races:
        return None

    matching_races.sort(key=lambda x: x[0])
    race_id = matching_races[0][1]

    try:
        payload = get_race_results(race_id)
        return race_id, payload
    except BiathlonError:
        return None


def _format_local_time(start_raw: str) -> tuple[str, str, str, str]:
    """Convert an ISO datetime string to local date/day/time/timezone.

    Returns (date_str, day_str, time_str, tz_str) in local timezone.
    """
    if not isinstance(start_raw, str) or not start_raw:
        return "", "", "", ""

    try:
        # Handle Z suffix for UTC
        iso_str = start_raw.replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(iso_str)
        # If no timezone info, assume UTC
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        # Convert to local timezone
        local_dt = dt.astimezone()
        tz_name = local_dt.tzname() or local_dt.strftime("%z") or ""
        return (
            local_dt.strftime("%Y-%m-%d"),
            local_dt.strftime("%a"),
            local_dt.strftime("%H:%M"),
            tz_name,
        )
    except ValueError:
        # Fallback: just extract date part
        if "T" in start_raw:
            date_part, time_part = start_raw.split("T", 1)
            tz_guess = "UTC" if time_part.endswith("Z") else ""
            return date_part, "", time_part[:5], tz_guess
        return start_raw.split(" ", 1)[0], "", "", ""


def _resolve_race_start_datetime(
    competition: dict | None,
) -> datetime.datetime | None:
    """Resolve race start datetime from a competition payload."""
    if not isinstance(competition, dict):
        return None
    for key in ("StartTime", "StartDate", "Date"):
        raw = competition.get(key)
        if not raw:
            continue
        start_dt = parse_start_datetime(str(raw))
        if start_dt is not None:
            return start_dt
    return None


def _season_end_year(season_id: str) -> int | None:
    """Return the ending calendar year for a season id like '2526'."""
    s = str(season_id or "")
    if len(s) < 4 or not s[:4].isdigit():
        return None

    start_two = int(s[:2])
    end_two = int(s[2:4])
    century = 2000 if start_two < 50 else 1900
    start_year = century + start_two
    end_year = century + end_two
    if end_year < start_year:
        end_year += 100
    return end_year


def _render_venue_history_table(
    alltime_stats: list[dict],
    current_season_ids: set[str],
    location_label: str,
    gender_label: str,
    args: argparse.Namespace,
    use_medal_columns: bool = False,
) -> None:
    """Render history table for one gender with current season highlighting.

    Args:
        alltime_stats: List of athlete stat dicts
        current_season_ids: Set of IBU IDs of athletes active in current season
        location_label: Label for the location (venue name, "Olympic Games", "World Championships")
        gender_label: "Women" or "Men"
        args: Command arguments
        use_medal_columns: If True, use Gold/Silver/Bronze/Total columns instead of Wins/Podiums/Flowers
    """
    if not alltime_stats:
        print(
            _format_section_title(
                f"Most decorated {gender_label.lower()} at {location_label}: no data",
                args,
            )
        )
        print()
        return

    # For medal columns, filter by gold > 0; for venue columns, filter by wins > 0
    if use_medal_columns:
        alltime_decorated = [s for s in alltime_stats if s.get("gold", 0) > 0]
        # Sort by total medals, then gold, silver, bronze
        alltime_decorated.sort(
            key=lambda s: (
                s.get("gold", 0) + s.get("silver", 0) + s.get("bronze", 0),
                s.get("gold", 0),
                s.get("silver", 0),
                s.get("bronze", 0),
            ),
            reverse=True,
        )
    else:
        alltime_decorated = [s for s in alltime_stats if s["wins"] > 0]
        alltime_decorated.sort(
            key=lambda s: (s["wins"], s["podiums"], s["flowers"], -s["races"]),
            reverse=True,
        )
    alltime_decorated = alltime_decorated[:10]

    if not alltime_decorated:
        print(
            _format_section_title(
                f"Most decorated {gender_label.lower()} at {location_label}: no winners found",
                args,
            )
        )
        print()
    else:
        print(
            _format_section_title(
                f"Most decorated {gender_label.lower()} at {location_label}:", args
            )
        )
        venue_rows = []
        highlight_rows: set[int] = set()
        for idx, stats in enumerate(alltime_decorated):
            if stats.get("ibu_id", "") in current_season_ids:
                highlight_rows.add(idx)
            if use_medal_columns:
                gold = stats.get("gold", 0)
                silver = stats.get("silver", 0)
                bronze = stats.get("bronze", 0)
                total = gold + silver + bronze
                venue_rows.append(
                    [
                        idx + 1,
                        stats["name"],
                        gold,
                        silver,
                        bronze,
                        total,
                        stats["races"],
                    ]
                )
            else:
                venue_rows.append(
                    [
                        idx + 1,
                        stats["name"],
                        stats["wins"],
                        stats["podiums"],
                        stats["flowers"],
                        stats["races"],
                    ]
                )

        def highlight_cell(cell_str: str, row_idx: int) -> str:
            return Color.highlight(cell_str) if row_idx in highlight_rows else cell_str

        if use_medal_columns:
            headers = ["#", "Athlete", "Gold", "Silver", "Bronze", "Total", "Races"]
            formatters = [None, highlight_cell, None, None, None, None, None]
        else:
            headers = ["#", "Athlete", "Wins", "Podiums", "Flowers", "Races"]
            formatters = [None, highlight_cell, None, None, None, None]

        render_table(
            headers,
            venue_rows,
            output_format=get_output_format(args),
            cell_formatters=formatters,
        )
        print()

    # Most experienced section
    alltime_experienced = [s for s in alltime_stats if s["races"] > 0]
    if use_medal_columns:
        alltime_experienced.sort(
            key=lambda s: (
                s["races"],
                s.get("gold", 0) + s.get("silver", 0) + s.get("bronze", 0),
                s.get("gold", 0),
            ),
            reverse=True,
        )
    else:
        alltime_experienced.sort(
            key=lambda s: (s["races"], s["wins"], s["podiums"], s["flowers"]),
            reverse=True,
        )
    alltime_experienced = alltime_experienced[:10]

    if not alltime_experienced:
        print(
            _format_section_title(
                f"Most experienced {gender_label.lower()} at {location_label}: no data",
                args,
            )
        )
        print()
        return

    print(
        _format_section_title(
            f"Most experienced {gender_label.lower()} at {location_label}:", args
        )
    )
    venue_rows = []
    highlight_rows = set()
    for idx, stats in enumerate(alltime_experienced):
        if stats.get("ibu_id", "") in current_season_ids:
            highlight_rows.add(idx)
        if use_medal_columns:
            gold = stats.get("gold", 0)
            silver = stats.get("silver", 0)
            bronze = stats.get("bronze", 0)
            total = gold + silver + bronze
            venue_rows.append(
                [
                    idx + 1,
                    stats["name"],
                    stats["races"],
                    gold,
                    silver,
                    bronze,
                    total,
                ]
            )
        else:
            venue_rows.append(
                [
                    idx + 1,
                    stats["name"],
                    stats["races"],
                    stats["wins"],
                    stats["podiums"],
                    stats["flowers"],
                ]
            )

    def highlight_cell_exp(cell_str: str, row_idx: int) -> str:
        return Color.highlight(cell_str) if row_idx in highlight_rows else cell_str

    if use_medal_columns:
        headers = ["#", "Athlete", "Races", "Gold", "Silver", "Bronze", "Total"]
        formatters = [None, highlight_cell_exp, None, None, None, None, None]
    else:
        headers = ["#", "Athlete", "Races", "Wins", "Podiums", "Flowers"]
        formatters = [None, highlight_cell_exp, None, None, None, None]

    render_table(
        headers,
        venue_rows,
        output_format=get_output_format(args),
        cell_formatters=formatters,
    )
    print()


PREEVENT_CATEGORY_CODES = ("WC", "WCH", "OWG")

PREEVENT_SECTION_EVENT_FACTS = "event_facts"
PREEVENT_SECTION_EVENT_AGENDA = "event_agenda"
PREEVENT_SECTION_LAST_10_EDITIONS = "last_10_editions_venue"
PREEVENT_SECTION_ATHLETE_STANDINGS = "athlete_standings"
PREEVENT_SECTION_RELAY_STANDINGS = "relay_standings"
PREEVENT_SECTION_NATIONS_CUP = "nations_cup_standings"
PREEVENT_SECTION_DECORATED_VENUE = "most_decorated_venue"
PREEVENT_SECTION_DECORATED_EVENT_TYPE = "most_decorated_event_type"

PREEVENT_SECTION_ORDER = [
    PREEVENT_SECTION_EVENT_FACTS,
    PREEVENT_SECTION_EVENT_AGENDA,
    PREEVENT_SECTION_LAST_10_EDITIONS,
    PREEVENT_SECTION_ATHLETE_STANDINGS,
    PREEVENT_SECTION_RELAY_STANDINGS,
    PREEVENT_SECTION_NATIONS_CUP,
    PREEVENT_SECTION_DECORATED_VENUE,
    PREEVENT_SECTION_DECORATED_EVENT_TYPE,
]

PREEVENT_SECTION_TITLES = {
    PREEVENT_SECTION_EVENT_FACTS: "Event Facts",
    PREEVENT_SECTION_EVENT_AGENDA: "Event Agenda",
    PREEVENT_SECTION_LAST_10_EDITIONS: "Last 10 Editions at <venue>",
    PREEVENT_SECTION_ATHLETE_STANDINGS: "Athlete Standings",
    PREEVENT_SECTION_RELAY_STANDINGS: "Relay Standings",
    PREEVENT_SECTION_NATIONS_CUP: "Nations Cup Standings",
    PREEVENT_SECTION_DECORATED_VENUE: "Most Decorated Athletes at <venue>",
    PREEVENT_SECTION_DECORATED_EVENT_TYPE: "Most Decorated Athletes at <event type>",
}


def _preevent_matrix_row(wc: bool, wch: bool, owg: bool) -> dict[str, bool]:
    return {"WC": bool(wc), "WCH": bool(wch), "OWG": bool(owg)}


PREEVENT_SECTION_MATRIX = {
    PREEVENT_SECTION_EVENT_FACTS: _preevent_matrix_row(True, True, True),
    PREEVENT_SECTION_EVENT_AGENDA: _preevent_matrix_row(True, True, True),
    PREEVENT_SECTION_LAST_10_EDITIONS: _preevent_matrix_row(True, True, True),
    PREEVENT_SECTION_ATHLETE_STANDINGS: _preevent_matrix_row(True, True, True),
    PREEVENT_SECTION_RELAY_STANDINGS: _preevent_matrix_row(True, True, True),
    PREEVENT_SECTION_NATIONS_CUP: _preevent_matrix_row(True, True, True),
    PREEVENT_SECTION_DECORATED_VENUE: _preevent_matrix_row(True, True, True),
    PREEVENT_SECTION_DECORATED_EVENT_TYPE: _preevent_matrix_row(True, True, True),
}


def _validate_preevent_section_matrix() -> None:
    expected_sections = set(PREEVENT_SECTION_ORDER)
    if set(PREEVENT_SECTION_TITLES) != expected_sections:
        raise ValueError("preevent section titles do not match section order")
    if set(PREEVENT_SECTION_MATRIX) != expected_sections:
        raise ValueError("preevent section matrix does not match section order")
    for section_id in PREEVENT_SECTION_ORDER:
        row = PREEVENT_SECTION_MATRIX[section_id]
        if set(row) != set(PREEVENT_CATEGORY_CODES):
            raise ValueError(
                f"preevent matrix categories mismatch for section {section_id}"
            )


_validate_preevent_section_matrix()


def _preevent_category_code(event_type: str) -> str:
    if event_type == EVENT_TYPE_WCH:
        return "WCH"
    if event_type == EVENT_TYPE_OWG:
        return "OWG"
    return "WC"


def _preevent_section_enabled(section_id: str, category_code: str) -> bool:
    row = PREEVENT_SECTION_MATRIX.get(section_id, {})
    return bool(row.get(category_code, False))


POSTEVENT_CATEGORY_CODES = PREEVENT_CATEGORY_CODES

POSTEVENT_SECTION_EVENT_FACTS = PREEVENT_SECTION_EVENT_FACTS
POSTEVENT_SECTION_EVENT_AGENDA = PREEVENT_SECTION_EVENT_AGENDA
POSTEVENT_SECTION_LAST_10_EDITIONS = PREEVENT_SECTION_LAST_10_EDITIONS
POSTEVENT_SECTION_BEST_PERFORMANCES = "best_performances"
POSTEVENT_SECTION_ATHLETE_STANDINGS = PREEVENT_SECTION_ATHLETE_STANDINGS
POSTEVENT_SECTION_RELAY_STANDINGS = PREEVENT_SECTION_RELAY_STANDINGS
POSTEVENT_SECTION_NATIONS_CUP = PREEVENT_SECTION_NATIONS_CUP
POSTEVENT_SECTION_DECORATED_VENUE = PREEVENT_SECTION_DECORATED_VENUE
POSTEVENT_SECTION_DECORATED_EVENT_TYPE = PREEVENT_SECTION_DECORATED_EVENT_TYPE

POSTEVENT_SECTION_ORDER = [
    POSTEVENT_SECTION_EVENT_FACTS,
    POSTEVENT_SECTION_EVENT_AGENDA,
    POSTEVENT_SECTION_LAST_10_EDITIONS,
    POSTEVENT_SECTION_ATHLETE_STANDINGS,
    POSTEVENT_SECTION_RELAY_STANDINGS,
    POSTEVENT_SECTION_NATIONS_CUP,
    POSTEVENT_SECTION_DECORATED_VENUE,
    POSTEVENT_SECTION_DECORATED_EVENT_TYPE,
    POSTEVENT_SECTION_BEST_PERFORMANCES,
]

POSTEVENT_SECTION_TITLES = {
    POSTEVENT_SECTION_EVENT_FACTS: PREEVENT_SECTION_TITLES[
        PREEVENT_SECTION_EVENT_FACTS
    ],
    POSTEVENT_SECTION_EVENT_AGENDA: PREEVENT_SECTION_TITLES[
        PREEVENT_SECTION_EVENT_AGENDA
    ],
    POSTEVENT_SECTION_LAST_10_EDITIONS: PREEVENT_SECTION_TITLES[
        PREEVENT_SECTION_LAST_10_EDITIONS
    ],
    POSTEVENT_SECTION_BEST_PERFORMANCES: "Best Performances",
    POSTEVENT_SECTION_ATHLETE_STANDINGS: PREEVENT_SECTION_TITLES[
        PREEVENT_SECTION_ATHLETE_STANDINGS
    ],
    POSTEVENT_SECTION_RELAY_STANDINGS: PREEVENT_SECTION_TITLES[
        PREEVENT_SECTION_RELAY_STANDINGS
    ],
    POSTEVENT_SECTION_NATIONS_CUP: PREEVENT_SECTION_TITLES[
        PREEVENT_SECTION_NATIONS_CUP
    ],
    POSTEVENT_SECTION_DECORATED_VENUE: PREEVENT_SECTION_TITLES[
        PREEVENT_SECTION_DECORATED_VENUE
    ],
    POSTEVENT_SECTION_DECORATED_EVENT_TYPE: PREEVENT_SECTION_TITLES[
        PREEVENT_SECTION_DECORATED_EVENT_TYPE
    ],
}

POSTEVENT_SECTION_MATRIX = {
    POSTEVENT_SECTION_EVENT_FACTS: _preevent_matrix_row(True, True, True),
    POSTEVENT_SECTION_EVENT_AGENDA: _preevent_matrix_row(True, True, True),
    POSTEVENT_SECTION_LAST_10_EDITIONS: _preevent_matrix_row(True, True, True),
    POSTEVENT_SECTION_BEST_PERFORMANCES: _preevent_matrix_row(True, True, True),
    POSTEVENT_SECTION_ATHLETE_STANDINGS: _preevent_matrix_row(True, False, False),
    POSTEVENT_SECTION_RELAY_STANDINGS: _preevent_matrix_row(True, False, False),
    POSTEVENT_SECTION_NATIONS_CUP: _preevent_matrix_row(True, False, False),
    POSTEVENT_SECTION_DECORATED_VENUE: _preevent_matrix_row(True, True, True),
    POSTEVENT_SECTION_DECORATED_EVENT_TYPE: _preevent_matrix_row(True, True, True),
}


def _validate_postevent_section_matrix() -> None:
    expected_sections = set(POSTEVENT_SECTION_ORDER)
    if set(POSTEVENT_SECTION_TITLES) != expected_sections:
        raise ValueError("postevent section titles do not match section order")
    if set(POSTEVENT_SECTION_MATRIX) != expected_sections:
        raise ValueError("postevent section matrix does not match section order")
    for section_id in POSTEVENT_SECTION_ORDER:
        row = POSTEVENT_SECTION_MATRIX[section_id]
        if set(row) != set(POSTEVENT_CATEGORY_CODES):
            raise ValueError(
                f"postevent matrix categories mismatch for section {section_id}"
            )


_validate_postevent_section_matrix()


def _postevent_section_enabled(section_id: str, category_code: str) -> bool:
    row = POSTEVENT_SECTION_MATRIX.get(section_id, {})
    return bool(row.get(category_code, False))


def _preevent_heading(level: int, text: str, args: argparse.Namespace) -> str:
    marks = "#" * max(1, int(level))
    return _format_section_title(f"{marks} {str(text).strip()}", args)


def _normalize_race_discipline_for_sequence(value: object) -> str:
    disc = str(value or "").strip().upper()
    return "IN" if disc == "SI" else disc


def _build_season_race_sequence_maps(
    season_id: str,
    event_type: str,
    level: int = 1,
    include_event_id: str = "",
) -> tuple[dict[tuple[str, str], str], dict[tuple[str, str], str]]:
    event_ids: list[str] = []
    seen_event_ids: set[str] = set()
    try:
        events = get_events(season_id, level=level)
    except BiathlonError:
        events = []
    for event in events:
        if detect_event_type(event) != event_type:
            continue
        event_id = str(event.get("EventId") or "").strip()
        if not event_id or event_id in seen_event_ids:
            continue
        seen_event_ids.add(event_id)
        event_ids.append(event_id)

    include_id = str(include_event_id or "").strip()
    if include_id and include_id not in seen_event_ids:
        event_ids.append(include_id)

    grouped_by_disc: dict[
        tuple[str, str], list[tuple[datetime.datetime | None, str]]
    ] = {}
    grouped_full: dict[tuple[str, str], list[tuple[datetime.datetime | None, str]]] = {}
    for event_id in event_ids:
        try:
            races = get_races(event_id)
        except BiathlonError:
            continue
        for race in races:
            race_cat = str(race.get("catId") or race.get("CatId") or "").strip().upper()
            if race_cat not in {"SW", "SM", "MX"}:
                continue
            race_disc = _normalize_race_discipline_for_sequence(
                race.get("DisciplineId")
            )
            if not race_disc:
                continue
            race_id = str(race.get("RaceId") or race.get("Id") or "").strip()
            if not race_id:
                continue
            start_raw = str(race.get("StartTime") or race.get("StartDate") or "")
            start_dt = parse_start_datetime(start_raw)
            grouped_by_disc.setdefault((race_cat, race_disc), []).append(
                (start_dt, race_id)
            )

            if race_cat in {"SW", "SM"} and race_disc in {"SP", "PU", "IN", "MS"}:
                grouped_full.setdefault((race_cat, "IND"), []).append(
                    (start_dt, race_id)
                )

            if race_disc in RELAY_DISCIPLINES:
                if race_cat in {"SW", "SM"}:
                    grouped_full.setdefault((race_cat, "TEAM"), []).append(
                        (start_dt, race_id)
                    )
                elif race_cat == "MX":
                    grouped_full.setdefault(("SW", "TEAM"), []).append(
                        (start_dt, race_id)
                    )
                    grouped_full.setdefault(("SM", "TEAM"), []).append(
                        (start_dt, race_id)
                    )
                    grouped_full.setdefault(("MX", "TEAM"), []).append(
                        (start_dt, race_id)
                    )

    fallback_dt = datetime.datetime.max.replace(tzinfo=datetime.timezone.utc)
    seq_by_race_id_disc: dict[tuple[str, str], str] = {}
    for (race_cat, _race_disc), entries in grouped_by_disc.items():
        entries.sort(
            key=lambda item: (item[0] is None, item[0] or fallback_dt, item[1])
        )
        total = len(entries)
        for idx, (_start_dt, race_id) in enumerate(entries, start=1):
            seq_by_race_id_disc[(race_cat, race_id)] = f"{idx}/{total}"

    seq_by_race_id_full: dict[tuple[str, str], str] = {}
    for (race_cat, _bucket), entries in grouped_full.items():
        entries.sort(
            key=lambda item: (item[0] is None, item[0] or fallback_dt, item[1])
        )
        total = len(entries)
        for idx, (_start_dt, race_id) in enumerate(entries, start=1):
            seq_by_race_id_full[(race_cat, race_id)] = f"{idx}/{total}"

    return seq_by_race_id_disc, seq_by_race_id_full


def _season_id_from_event_id(event_id: str) -> str:
    text = str(event_id or "").strip().upper()
    if len(text) >= 6 and text.startswith("BT") and text[2:6].isdigit():
        return text[2:6]
    return ""


def _find_event_by_id(event_id: str) -> dict | None:
    event_key = str(event_id or "").strip()
    if not event_key:
        return None
    season_candidates: list[str] = []
    parsed_season = _season_id_from_event_id(event_key)
    if parsed_season:
        season_candidates.append(parsed_season)
    current_season = get_current_season_id()
    if current_season not in season_candidates:
        season_candidates.append(current_season)

    for season_id in season_candidates:
        for level in (1, 2, 3, 4, 5, 6):
            try:
                events = get_events(season_id, level)
            except BiathlonError:
                continue
            for event in events:
                if str(event.get("EventId") or "").strip() == event_key:
                    found = dict(event)
                    found.setdefault("Level", level)
                    return found
    return None


def _first_event_race_with_start(
    races: list[dict],
) -> tuple[str, datetime.datetime] | None:
    candidates: list[tuple[datetime.datetime, str]] = []
    for race in races:
        race_id = str(race.get("RaceId") or race.get("Id") or "")
        if not race_id:
            continue
        race_cat = str(race.get("catId") or race.get("CatId") or "").upper()
        if race_cat not in {"SW", "SM", "MX"}:
            continue
        start_raw = str(race.get("StartTime") or race.get("StartDate") or "")
        start_dt = parse_start_datetime(start_raw)
        if start_dt is None:
            continue
        candidates.append((start_dt, race_id))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]))
    start_dt, race_id = candidates[0]
    return race_id, start_dt


def _rows_from_athlete_points(
    points_by_id: dict[str, int],
    athlete_info: dict[str, tuple[str, str]],
    limit: int | None = None,
) -> list[dict]:
    ranked = sorted(points_by_id.items(), key=lambda item: (-item[1], item[0]))
    if isinstance(limit, int) and limit > 0:
        ranked = ranked[:limit]
    rows: list[dict] = []
    for idx, (ibu_id, points) in enumerate(ranked, start=1):
        name, nat = athlete_info.get(ibu_id, ("", ""))
        rows.append(
            {
                "Rank": idx,
                "Name": name,
                "Nat": nat,
                "IBUId": ibu_id,
                "Score": points,
            }
        )
    return rows


def _format_score_value(score: float) -> str:
    if float(score).is_integer():
        return str(int(score))
    return f"{score:.1f}"


def _parse_points_value(value: object) -> float:
    text = str(value if value is not None else "").strip()
    if not text:
        return 0.0
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return 0.0


def _rows_from_country_points(
    points_by_nat: dict[str, float],
    names_by_nat: dict[str, str] | None,
    limit: int,
) -> list[dict]:
    ranked = sorted(points_by_nat.items(), key=lambda item: (-item[1], item[0]))
    rows: list[dict] = []
    for idx, (nat, points) in enumerate(ranked[:limit], start=1):
        display = ""
        if names_by_nat:
            display = str(names_by_nat.get(nat) or "")
        if not display:
            display = _country_display(nat) or nat
        rows.append(
            {
                "Rank": idx,
                "Name": display,
                "Nat": nat,
                "Score": _format_score_value(points),
            }
        )
    return rows


def _combine_country_rows(
    row_groups: list[list[dict]],
    limit: int = 10,
    one_decimal: bool = False,
) -> list[dict]:
    totals_by_nat: dict[str, float] = {}
    names_by_nat: dict[str, str] = {}
    for rows in row_groups:
        for row in rows:
            nat = str(row.get("Nat") or "").strip().upper()
            if not nat:
                continue
            totals_by_nat[nat] = totals_by_nat.get(nat, 0.0) + _parse_points_value(
                row.get("Score")
                if row.get("Score") not in (None, "")
                else row.get("Points")
            )
            if nat not in names_by_nat:
                team_name = _normalize_team_name(row)
                if team_name:
                    names_by_nat[nat] = team_name

    rows = _rows_from_country_points(totals_by_nat, names_by_nat, limit)
    if one_decimal:
        for row in rows:
            row["Score"] = _format_nations_points(
                row.get("Score") or row.get("Points") or "0"
            )
    return rows


def _resolve_event_type_and_venue(
    current_event: dict | None, first_race_id: str
) -> tuple[str, str, dict]:
    event_type = detect_event_type(current_event or {})
    venue_name = str(
        (current_event or {}).get("Organizer")
        or (current_event or {}).get("ShortDescription")
        or ""
    ).strip()

    payload: dict = {}
    try:
        payload = get_race_results(first_race_id)
    except BiathlonError:
        payload = {}

    if not current_event and payload:
        event_type = detect_event_type(payload.get("SportEvt") or {})

    if not venue_name:
        venue_name = _extract_venue_name(payload) if payload else ""

    if not venue_name:
        venue_name = "Unknown venue"
    return event_type, venue_name, payload


def _venue_text_matches(venue_lower: str, text: str) -> bool:
    text_lower = str(text or "").strip().lower()
    return bool(venue_lower and text_lower) and (
        venue_lower in text_lower or text_lower in venue_lower
    )


def _collect_venue_level1_events(venue_name: str) -> list[dict]:
    venue_lower = str(venue_name or "").strip().lower()
    if not venue_lower or venue_lower == "unknown venue":
        return []

    try:
        seasons = get_seasons()
    except BiathlonError:
        return []

    season_ids = [
        str(season.get("SeasonId") or "").strip()
        for season in seasons
        if str(season.get("SeasonId") or "").strip()
    ]
    if not season_ids:
        return []

    seen_event_ids: set[str] = set()
    venue_events: list[dict] = []

    with ThreadPoolExecutor(max_workers=_max_workers(len(season_ids))) as executor:
        futures = {
            executor.submit(get_events, season_id, 1): season_id
            for season_id in season_ids
        }
        for future in as_completed(futures):
            season_id = futures[future]
            try:
                events = future.result()
            except BiathlonError:
                continue
            for event in events:
                event_id = str(event.get("EventId") or "").strip()
                if not event_id or event_id in seen_event_ids:
                    continue

                organizer = str(event.get("Organizer") or "")
                short_desc = str(event.get("ShortDescription") or "")
                if not (
                    _venue_text_matches(venue_lower, organizer)
                    or _venue_text_matches(venue_lower, short_desc)
                ):
                    continue

                seen_event_ids.add(event_id)
                start_raw = str(
                    event.get("StartDate") or event.get("FirstCompetitionDate") or ""
                ).strip()
                start_date = start_raw.split("T", 1)[0] if start_raw else ""
                venue_events.append(
                    {
                        "season_id": season_id,
                        "event_id": event_id,
                        "start_date": start_date,
                        "event": event,
                    }
                )

    return venue_events


def _resolve_event_country(
    current_event: dict | None, payload: dict, event_id: str, season_id: str
) -> str:
    event_raw = str(
        (current_event or {}).get("Nat")
        or (current_event or {}).get("Nation")
        or (current_event or {}).get("CountryId")
        or (current_event or {}).get("Country")
        or ""
    ).strip()
    if event_raw:
        return _country_display(event_raw)

    sport_evt = payload.get("SportEvt") or {}
    comp = payload.get("Competition") or {}
    payload_raw = str(
        sport_evt.get("CountryId")
        or sport_evt.get("Country")
        or comp.get("CountryId")
        or comp.get("Country")
        or ""
    ).strip()
    if payload_raw:
        return _country_display(payload_raw)

    return _event_country_display(event_id, season_id)


def _count_venue_event_editions(
    venue_name: str,
    venue_events: list[dict] | None = None,
    reference_date: datetime.date | None = None,
) -> tuple[int, int, int]:
    if venue_events is None:
        venue_events = _collect_venue_level1_events(venue_name)
    if reference_date is not None:
        filtered_events: list[dict] = []
        for entry in venue_events:
            start_date = parse_date(entry.get("start_date") or "")
            if start_date is not None and start_date > reference_date:
                continue
            filtered_events.append(entry)
        venue_events = filtered_events
    wc_count = 0
    wch_count = 0
    owg_count = 0

    for entry in venue_events:
        event_type = detect_event_type(cast(dict, entry.get("event") or {}))
        if event_type == EVENT_TYPE_WCH:
            wch_count += 1
        elif event_type == EVENT_TYPE_OWG:
            owg_count += 1
        else:
            wc_count += 1

    return wc_count, wch_count, owg_count


VENUE_RACE_TYPE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("SP", "Sprint"),
    ("PU", "Pursuit"),
    ("IN", "Individual"),
    ("MS", "Mass Start"),
    ("RL", "Relay"),
    ("MR", "Mixed Relay"),
    ("SR", "Single Mixed Relay"),
)
VENUE_RACE_TYPE_CODES = {code for code, _label in VENUE_RACE_TYPE_COLUMNS}


def _race_type_bucket(cat_id: str, discipline_id: str) -> str:
    disc = _normalize_race_discipline_for_sequence(discipline_id)
    cat = str(cat_id or "").upper()
    if disc == "RL" and cat == "MX":
        return "MR"
    if disc in VENUE_RACE_TYPE_CODES:
        return disc
    return ""


def _count_event_race_types(event_id: str) -> dict[str, int]:
    counts = {code: 0 for code, _ in VENUE_RACE_TYPE_COLUMNS}
    if not event_id:
        return counts
    try:
        races = get_races(event_id)
    except BiathlonError:
        return counts

    for race in races:
        cat_id = str(race.get("catId") or race.get("CatId") or "").upper()
        if cat_id not in {"SW", "SM", "MX"}:
            continue
        bucket = _race_type_bucket(cat_id, str(race.get("DisciplineId") or "").upper())
        if bucket:
            counts[bucket] += 1

    return counts


def _build_recent_venue_edition_rows(
    venue_name: str,
    limit: int = 10,
    venue_events: list[dict] | None = None,
    reference_date: datetime.date | None = None,
) -> list[list[str | int]]:
    if venue_events is None:
        venue_events = _collect_venue_level1_events(venue_name)
    venue_events = list(venue_events)
    if reference_date is not None:
        venue_events = [
            entry
            for entry in venue_events
            if (
                (parsed := parse_date(entry.get("start_date") or "")) is None
                or parsed <= reference_date
            )
        ]
    if not venue_events:
        return []

    def _start_date(entry: dict) -> datetime.date:
        parsed = parse_date(entry.get("start_date") or "")
        return parsed if parsed is not None else datetime.date.min

    venue_events.sort(
        key=lambda entry: (_start_date(entry), str(entry.get("event_id") or "")),
        reverse=True,
    )
    selected = venue_events[: max(0, int(limit))]

    event_ids = [str(entry.get("event_id") or "") for entry in selected]
    race_counts_by_event: dict[str, dict[str, int]] = {}
    with ThreadPoolExecutor(max_workers=_max_workers(len(event_ids))) as executor:
        futures = {
            executor.submit(_count_event_race_types, event_id): event_id
            for event_id in event_ids
            if event_id
        }
        for future in as_completed(futures):
            event_id = futures[future]
            race_counts_by_event[event_id] = future.result()

    rows: list[list[str | int]] = []
    for entry in selected:
        event = cast(dict, entry.get("event") or {})
        event_type = detect_event_type(event)
        event_type_label = _preevent_category_code(event_type)
        event_id = str(entry.get("event_id") or "")
        counts = race_counts_by_event.get(event_id) or {
            code: 0 for code, _ in VENUE_RACE_TYPE_COLUMNS
        }
        edition = str(entry.get("start_date") or entry.get("season_id") or "-")
        row: list[str | int] = [edition, event_type_label]
        for code, _ in VENUE_RACE_TYPE_COLUMNS:
            row.append(counts.get(code, 0))
        rows.append(row)

    return rows


def _race_type_presence_mark(value: object) -> str:
    try:
        count = int(value)
    except (TypeError, ValueError):
        return "-"
    return "X" if count > 0 else "-"


def _parse_leg_value(value: object) -> int | None:
    if isinstance(value, int):
        return value
    text = str(value or "").strip()
    if text.isdigit():
        return int(text)
    return None


def _gender_from_race_context(cat_id: str, discipline_id: str, leg: object) -> str:
    cat = str(cat_id or "").upper()
    disc = str(discipline_id or "").upper()
    leg_val = _parse_leg_value(leg)
    if cat == "SW":
        return "F"
    if cat == "SM":
        return "M"
    if cat != "MX":
        return ""
    if leg_val is None:
        return ""
    if disc == "SR":
        return "F" if leg_val % 2 == 1 else "M"
    return "F" if leg_val <= 2 else "M"


def _filter_events_to_reference_date(
    events: list[dict],
    reference_date: datetime.date | None,
    exclude_event_ids: set[str] | None = None,
) -> list[dict]:
    excluded = {
        str(event_id).strip()
        for event_id in (exclude_event_ids or set())
        if str(event_id).strip()
    }
    if reference_date is None:
        return [
            entry
            for entry in events
            if str(entry.get("event_id") or "").strip() not in excluded
        ]
    filtered: list[dict] = []
    for entry in events:
        event_id = str(entry.get("event_id") or "").strip()
        if event_id in excluded:
            continue
        parsed = parse_date(entry.get("start_date") or "")
        if parsed is not None and parsed > reference_date:
            continue
        filtered.append(entry)
    return filtered


def _resolve_current_season_id_for_highlight() -> str:
    try:
        seasons = get_seasons()
    except BiathlonError:
        seasons = []
    if seasons:
        current_entry = next((s for s in seasons if bool(s.get("IsCurrent"))), None)
        if current_entry and current_entry.get("SeasonId"):
            return str(current_entry.get("SeasonId") or "").strip()
        newest = max(seasons, key=lambda s: s.get("SortOrder", 0))
        newest_id = str(newest.get("SeasonId") or "").strip()
        if newest_id:
            return newest_id

    try:
        season_id = str(get_current_season_id() or "").strip()
        if season_id:
            return season_id
    except BiathlonError:
        pass
    return ""


def _collect_level1_events_by_type(event_type: str) -> list[dict]:
    try:
        seasons = get_seasons()
    except BiathlonError:
        return []
    season_ids = [
        str(season.get("SeasonId") or "").strip()
        for season in seasons
        if str(season.get("SeasonId") or "").strip()
    ]
    if not season_ids:
        return []
    seen_event_ids: set[str] = set()
    matched_events: list[dict] = []
    with ThreadPoolExecutor(max_workers=_max_workers(len(season_ids))) as executor:
        futures = {
            executor.submit(get_events, season_id, 1): season_id
            for season_id in season_ids
        }
        for future in as_completed(futures):
            season_id = futures[future]
            try:
                events = future.result()
            except BiathlonError:
                continue
            for event in events:
                event_id = str(event.get("EventId") or "").strip()
                if not event_id or event_id in seen_event_ids:
                    continue
                if detect_event_type(event) != event_type:
                    continue
                seen_event_ids.add(event_id)
                start_raw = str(
                    event.get("StartDate") or event.get("FirstCompetitionDate") or ""
                ).strip()
                start_date = start_raw.split("T", 1)[0] if start_raw else ""
                matched_events.append(
                    {
                        "season_id": season_id,
                        "event_id": event_id,
                        "start_date": start_date,
                        "event": event,
                    }
                )
    return matched_events


def _collect_current_season_participant_keys(
    reference_date: datetime.date | None = None,
) -> set[str]:
    season_id = _resolve_current_season_id_for_highlight()
    if not season_id:
        return set()

    try:
        events = get_events(season_id, level=1)
    except BiathlonError:
        return set()

    event_ids: list[str] = []
    for event in events:
        event_id = str(event.get("EventId") or "").strip()
        if not event_id:
            continue
        if reference_date is not None:
            start_raw = str(
                event.get("StartDate") or event.get("FirstCompetitionDate") or ""
            ).strip()
            start_date = parse_date(start_raw.split("T", 1)[0] if start_raw else "")
            if start_date is not None and start_date > reference_date:
                continue
        event_ids.append(event_id)
    if not event_ids:
        return set()

    race_meta_by_id: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=_max_workers(len(event_ids))) as executor:
        futures = {
            executor.submit(get_races, event_id): event_id for event_id in event_ids
        }
        for future in as_completed(futures):
            try:
                races = future.result()
            except BiathlonError:
                continue
            for race in races:
                race_id = str(race.get("RaceId") or race.get("Id") or "").strip()
                if not race_id:
                    continue
                cat_id = str(race.get("catId") or race.get("CatId") or "").upper()
                if cat_id not in {"SW", "SM", "MX"}:
                    continue
                disc_id = str(race.get("DisciplineId") or "").upper()
                race_meta_by_id[race_id] = disc_id

    if not race_meta_by_id:
        return set()

    participant_keys: set[str] = set()
    with ThreadPoolExecutor(max_workers=_max_workers(len(race_meta_by_id))) as executor:
        futures = {
            executor.submit(get_race_results, race_id): race_id
            for race_id in race_meta_by_id
        }
        for future in as_completed(futures):
            race_id = futures[future]
            disc_id = race_meta_by_id[race_id]
            try:
                payload = future.result()
            except BiathlonError:
                continue

            results = payload.get("Results") or []
            if not results:
                continue
            is_team_race = disc_id in RELAY_DISCIPLINES
            if is_team_race:
                if not _has_completed_relay_results(payload):
                    continue
            elif not _has_completed_results(payload):
                continue

            for row in results:
                if row.get("IsTeam"):
                    continue
                nat = str(row.get("Nat") or "").strip().upper()
                name = str(
                    row.get("Name")
                    or row.get("ShortName")
                    or row.get("FamilyName")
                    or ""
                ).strip()
                ibu_id = _row_ibu_id(row)
                key = ibu_id or f"{name}|{nat}"
                if key and key != "|":
                    participant_keys.add(key)

    return participant_keys


def _build_decorated_athlete_rows_for_events(
    events: list[dict],
    current_season_id: str,
    highlight_keys: set[str] | None = None,
    limit: int = 20,
) -> tuple[list[list[str]], list[str]]:
    if not events:
        return [], []

    event_ids = [
        str(entry.get("event_id") or "").strip()
        for entry in events
        if str(entry.get("event_id") or "").strip()
    ]
    event_season_by_id = {
        str(entry.get("event_id") or "").strip(): str(
            entry.get("season_id") or ""
        ).strip()
        for entry in events
    }
    race_meta_by_id: dict[str, tuple[str, str, str]] = {}
    with ThreadPoolExecutor(max_workers=_max_workers(len(event_ids))) as executor:
        futures = {
            executor.submit(get_races, event_id): event_id for event_id in event_ids
        }
        for future in as_completed(futures):
            event_id = futures[future]
            try:
                races = future.result()
            except BiathlonError:
                continue
            for race in races:
                race_id = str(race.get("RaceId") or race.get("Id") or "").strip()
                if not race_id:
                    continue
                cat_id = str(race.get("catId") or race.get("CatId") or "").upper()
                if cat_id not in {"SW", "SM", "MX"}:
                    continue
                disc_id = str(race.get("DisciplineId") or "").upper()
                race_meta_by_id[race_id] = (
                    cat_id,
                    disc_id,
                    event_season_by_id.get(event_id, ""),
                )

    if not race_meta_by_id:
        return [], []

    athlete_stats: dict[str, dict[str, int | str]] = {}
    current_season_participants: set[str] = set()
    with ThreadPoolExecutor(max_workers=_max_workers(len(race_meta_by_id))) as executor:
        futures = {
            executor.submit(get_race_results, race_id): race_id
            for race_id in race_meta_by_id
        }
        for future in as_completed(futures):
            race_id = futures[future]
            cat_id, disc_id, race_season_id = race_meta_by_id[race_id]
            try:
                payload = future.result()
            except BiathlonError:
                continue

            results = payload.get("Results") or []
            if not results:
                continue

            is_team_race = disc_id in RELAY_DISCIPLINES
            if is_team_race:
                if not _has_completed_relay_results(payload):
                    continue
            elif not _has_completed_results(payload):
                continue

            team_rank_by_key: dict[str, int] = {}
            if is_team_race:
                for row in results:
                    if not row.get("IsTeam"):
                        continue
                    team_rank = _parse_rank(
                        row.get("Rank") or row.get("SO") or row.get("ResultOrder")
                    )
                    if team_rank is None:
                        continue
                    bib = str(row.get("Bib") or "").strip()
                    nat = str(row.get("Nat") or "").strip().upper()
                    if bib:
                        team_rank_by_key[f"bib:{bib}"] = team_rank
                    if nat:
                        team_rank_by_key.setdefault(f"nat:{nat}", team_rank)

            race_athlete_keys: set[str] = set()
            race_individual_keys: set[str] = set()
            race_team_keys: set[str] = set()
            for row in results:
                if row.get("IsTeam"):
                    continue
                nat = str(row.get("Nat") or "").strip().upper()
                name = str(
                    row.get("Name")
                    or row.get("ShortName")
                    or row.get("FamilyName")
                    or ""
                ).strip()
                ibu_id = _row_ibu_id(row)
                key = ibu_id or f"{name}|{nat}"
                if not key or key == "|":
                    continue
                if current_season_id and race_season_id == current_season_id:
                    current_season_participants.add(key)

                stats = athlete_stats.setdefault(
                    key,
                    {
                        "name": name or key,
                        "nat": nat,
                        "gender": _gender_from_race_context(
                            cat_id, disc_id, row.get("Leg")
                        ),
                        "gold": 0,
                        "silver": 0,
                        "bronze": 0,
                        "gold_ind": 0,
                        "silver_ind": 0,
                        "bronze_ind": 0,
                        "gold_team": 0,
                        "silver_team": 0,
                        "bronze_team": 0,
                        "races": 0,
                        "races_ind": 0,
                        "races_team": 0,
                    },
                )
                if name and not str(stats.get("name") or "").strip():
                    stats["name"] = name
                if nat and not str(stats.get("nat") or "").strip():
                    stats["nat"] = nat
                if not str(stats.get("gender") or "").strip():
                    stats["gender"] = _gender_from_race_context(
                        cat_id, disc_id, row.get("Leg")
                    )

                race_athlete_keys.add(key)
                if is_team_race:
                    race_team_keys.add(key)
                else:
                    race_individual_keys.add(key)

                rank_val = _parse_rank(
                    row.get("Rank") or row.get("SO") or row.get("ResultOrder")
                )
                if rank_val is None and is_team_race:
                    bib = str(row.get("Bib") or "").strip()
                    if bib:
                        rank_val = team_rank_by_key.get(f"bib:{bib}")
                    if rank_val is None and nat:
                        rank_val = team_rank_by_key.get(f"nat:{nat}")

                if rank_val == 1:
                    stats["gold"] = int(stats["gold"]) + 1
                    if is_team_race:
                        stats["gold_team"] = int(stats["gold_team"]) + 1
                    else:
                        stats["gold_ind"] = int(stats["gold_ind"]) + 1
                elif rank_val == 2:
                    stats["silver"] = int(stats["silver"]) + 1
                    if is_team_race:
                        stats["silver_team"] = int(stats["silver_team"]) + 1
                    else:
                        stats["silver_ind"] = int(stats["silver_ind"]) + 1
                elif rank_val == 3:
                    stats["bronze"] = int(stats["bronze"]) + 1
                    if is_team_race:
                        stats["bronze_team"] = int(stats["bronze_team"]) + 1
                    else:
                        stats["bronze_ind"] = int(stats["bronze_ind"]) + 1

            for athlete_key in race_athlete_keys:
                athlete_stats[athlete_key]["races"] = (
                    int(athlete_stats[athlete_key]["races"]) + 1
                )
            for athlete_key in race_individual_keys:
                athlete_stats[athlete_key]["races_ind"] = (
                    int(athlete_stats[athlete_key]["races_ind"]) + 1
                )
            for athlete_key in race_team_keys:
                athlete_stats[athlete_key]["races_team"] = (
                    int(athlete_stats[athlete_key]["races_team"]) + 1
                )

    decorated = [
        (key, stats)
        for key, stats in athlete_stats.items()
        if int(stats["gold"]) > 0
        or int(stats["silver"]) > 0
        or int(stats["bronze"]) > 0
    ]
    if not decorated:
        return [], []

    decorated.sort(
        key=lambda item: (
            -int(item[1]["gold"]),
            -int(item[1].get("gold_ind", 0)),
            -int(item[1].get("gold_team", 0)),
            -int(item[1]["silver"]),
            -int(item[1].get("silver_ind", 0)),
            -int(item[1].get("silver_team", 0)),
            -int(item[1]["bronze"]),
            -int(item[1].get("bronze_ind", 0)),
            -int(item[1].get("bronze_team", 0)),
            -(int(item[1]["gold"]) + int(item[1]["silver"]) + int(item[1]["bronze"])),
            -(
                int(item[1].get("gold_ind", 0))
                + int(item[1].get("silver_ind", 0))
                + int(item[1].get("bronze_ind", 0))
            ),
            -(
                int(item[1].get("gold_team", 0))
                + int(item[1].get("silver_team", 0))
                + int(item[1].get("bronze_team", 0))
            ),
            -int(item[1]["races"]),
            str(item[1]["name"]),
            str(item[1]["nat"]),
        )
    )
    if limit > 0:
        decorated = decorated[:limit]

    rows: list[list[str]] = []
    row_styles: list[str] = []
    effective_highlight_keys = (
        set(highlight_keys)
        if highlight_keys is not None
        else current_season_participants
    )
    for rank, (athlete_key, stats) in enumerate(decorated, start=1):
        gold = int(stats["gold"])
        silver = int(stats["silver"])
        bronze = int(stats["bronze"])
        total = gold + silver + bronze
        races = int(stats["races"])
        gold_ind = int(stats.get("gold_ind", 0))
        silver_ind = int(stats.get("silver_ind", 0))
        bronze_ind = int(stats.get("bronze_ind", 0))
        total_ind = gold_ind + silver_ind + bronze_ind
        races_ind = int(stats.get("races_ind", 0))
        gold_team = int(stats.get("gold_team", 0))
        silver_team = int(stats.get("silver_team", 0))
        bronze_team = int(stats.get("bronze_team", 0))
        total_team = gold_team + silver_team + bronze_team
        races_team = int(stats.get("races_team", 0))
        rows.append(
            [
                str(rank),
                str(stats["name"] or ""),
                str(stats["nat"] or ""),
                str(stats["gender"] or "-"),
                str(gold),
                str(silver),
                str(bronze),
                str(total),
                str(races),
                str(gold_ind),
                str(silver_ind),
                str(bronze_ind),
                str(total_ind),
                str(races_ind),
                str(gold_team),
                str(silver_team),
                str(bronze_team),
                str(total_team),
                str(races_team),
            ]
        )
        row_styles.append(
            "highlight_plain" if athlete_key in effective_highlight_keys else ""
        )
    return rows, row_styles


def _build_venue_decorated_athlete_rows(
    venue_name: str,
    venue_events: list[dict] | None = None,
    reference_date: datetime.date | None = None,
    highlight_keys: set[str] | None = None,
    limit: int = 20,
    exclude_event_ids: set[str] | None = None,
) -> tuple[list[list[str]], list[str]]:
    if venue_events is None:
        venue_events = _collect_venue_level1_events(venue_name)
    events = _filter_events_to_reference_date(
        list(venue_events), reference_date, exclude_event_ids=exclude_event_ids
    )
    current_season_id = _resolve_current_season_id_for_highlight()
    return _build_decorated_athlete_rows_for_events(
        events,
        current_season_id=current_season_id,
        highlight_keys=highlight_keys,
        limit=limit,
    )


def _build_event_type_decorated_athlete_rows(
    event_type: str,
    reference_date: datetime.date | None = None,
    highlight_keys: set[str] | None = None,
    limit: int = 20,
    exclude_event_ids: set[str] | None = None,
) -> tuple[list[list[str]], list[str]]:
    events = _collect_level1_events_by_type(event_type)
    events = _filter_events_to_reference_date(
        events, reference_date, exclude_event_ids=exclude_event_ids
    )
    current_season_id = _resolve_current_season_id_for_highlight()
    return _build_decorated_athlete_rows_for_events(
        events,
        current_season_id=current_season_id,
        highlight_keys=highlight_keys,
        limit=limit,
    )


def _split_decorated_rows_by_gender(
    rows: list[list[str]], row_styles: list[str]
) -> tuple[dict[str, list[list[str]]], dict[str, list[str]]]:
    rows_by_gender: dict[str, list[list[str]]] = {"F": [], "M": []}
    styles_by_gender: dict[str, list[str]] = {"F": [], "M": []}
    for idx, row in enumerate(rows):
        if len(row) < 5:
            continue
        gender = str(row[3] or "").upper()
        if gender not in {"F", "M"}:
            continue
        style = row_styles[idx] if idx < len(row_styles) else ""
        # Drop gender column when rendering split women/men tables.
        rows_by_gender[gender].append(row[:3] + row[4:])
        styles_by_gender[gender].append(style)
    return rows_by_gender, styles_by_gender


def _render_decorated_athletes_split_tables(
    title: str,
    rows: list[list[str]],
    row_styles: list[str],
    args: argparse.Namespace,
    per_gender_limit: int | None = None,
) -> None:
    print(_preevent_heading(2, title, args))
    if not rows:
        print("none")
        print()
        return

    rows_by_gender, styles_by_gender = _split_decorated_rows_by_gender(rows, row_styles)
    print()
    headers = [
        "#",
        "Athlete",
        "Nat",
        Color.gold("Gold"),
        Color.silver("Silver"),
        Color.bronze("Bronze"),
        "Total",
        "Races",
        Color.gold("Gold"),
        Color.silver("Silver"),
        Color.bronze("Bronze"),
        "Total",
        "Races",
        Color.gold("Gold"),
        Color.silver("Silver"),
        Color.bronze("Bronze"),
        "Total",
        "Races",
    ]
    for gender_label, gender_code in (("Women", "F"), ("Men", "M")):
        print(_preevent_heading(3, gender_label, args))
        gender_rows = rows_by_gender.get(gender_code, [])
        gender_styles = styles_by_gender.get(gender_code, [])
        if isinstance(per_gender_limit, int) and per_gender_limit > 0:
            gender_rows = gender_rows[:per_gender_limit]
            gender_styles = gender_styles[:per_gender_limit]
        if not gender_rows:
            print("none")
            print()
            continue
        # Split tables use local rank numbering per gender.
        renumbered_rows = [
            [str(idx)] + row[1:] for idx, row in enumerate(gender_rows, start=1)
        ]
        render_table(
            headers,
            renumbered_rows,
            output_format=get_output_format(args),
            column_separators={3, 8, 13},
            group_headers=[(3, 8, "All"), (8, 13, "Individual"), (13, 18, "Team")],
            row_styles=gender_styles,
        )
        print()


def _compute_preevent_snapshot_standings(
    season_id: str,
    target_race_id: str,
    cutoff_dt: datetime.datetime,
    limit: int = 10,
) -> dict[str, dict]:
    athlete_total: dict[str, dict[str, int]] = {"SW": {}, "SM": {}}
    athlete_disc: dict[str, dict[str, dict[str, int]]] = {
        "SW": {"SP": {}, "PU": {}, "IN": {}, "MS": {}},
        "SM": {"SP": {}, "PU": {}, "IN": {}, "MS": {}},
    }
    athlete_info: dict[str, dict[str, tuple[str, str]]] = {"SW": {}, "SM": {}}

    relay_points: dict[str, dict[str, float]] = {"SW": {}, "SM": {}, "MX": {}}
    relay_names: dict[str, dict[str, str]] = {"SW": {}, "SM": {}, "MX": {}}
    nations_points: dict[str, dict[str, float]] = {"SW": {}, "SM": {}}

    try:
        events = get_events(season_id, level=1)
    except BiathlonError:
        events = []

    race_meta_by_id: dict[str, tuple[str, str]] = {}
    for event in events:
        if detect_event_type(event) != EVENT_TYPE_WC:
            continue
        event_id = str(event.get("EventId") or "")
        if not event_id:
            continue
        try:
            races = get_races(event_id)
        except BiathlonError:
            continue
        for race in races:
            race_id = str(race.get("RaceId") or race.get("Id") or "")
            if not race_id or race_id == target_race_id:
                continue
            race_cat = str(race.get("catId") or race.get("CatId") or "").upper()
            race_disc = str(race.get("DisciplineId") or "").upper()
            if race_cat not in {"SW", "SM", "MX"}:
                continue
            start_dt = parse_start_datetime(
                str(race.get("StartTime") or race.get("StartDate") or "")
            )
            if start_dt is None or start_dt >= cutoff_dt:
                continue
            if (
                race_disc not in {"SP", "PU", "IN", "MS", "SI"}
                and race_disc not in RELAY_DISCIPLINES
            ):
                continue
            race_meta_by_id[race_id] = (race_cat, race_disc)

    if race_meta_by_id:
        with ThreadPoolExecutor(
            max_workers=_max_workers(len(race_meta_by_id))
        ) as executor:
            futures = {
                executor.submit(get_race_results, race_id): race_id
                for race_id in race_meta_by_id
            }
            for future in as_completed(futures):
                race_id = futures[future]
                race_cat, race_disc = race_meta_by_id[race_id]
                try:
                    payload = future.result()
                except BiathlonError:
                    continue

                is_relay_race = race_disc in RELAY_DISCIPLINES
                if is_relay_race:
                    if not _has_completed_relay_results(payload):
                        continue
                elif not _has_completed_results(payload):
                    continue

                for res in payload.get("Results") or []:
                    if is_relay_race and not res.get("IsTeam"):
                        continue
                    if not is_relay_race and res.get("IsTeam"):
                        continue

                    rank_val = _parse_rank(
                        res.get("Rank") or res.get("SO") or res.get("ResultOrder")
                    )
                    if rank_val is None:
                        continue

                    points = _get_wc_points(rank_val, mass_start=race_disc == "MS")
                    if points <= 0:
                        continue

                    nat = str(res.get("Nat") or "").upper()
                    if not nat:
                        continue

                    if not is_relay_race and race_cat in {"SW", "SM"}:
                        ibu_id = _row_ibu_id(res)
                        if ibu_id:
                            athlete_total[race_cat][ibu_id] = (
                                athlete_total[race_cat].get(ibu_id, 0) + points
                            )
                            disc_key = "IN" if race_disc == "SI" else race_disc
                            if disc_key in athlete_disc[race_cat]:
                                athlete_disc[race_cat][disc_key][ibu_id] = (
                                    athlete_disc[race_cat][disc_key].get(ibu_id, 0)
                                    + points
                                )
                            if ibu_id not in athlete_info[race_cat]:
                                athlete_info[race_cat][ibu_id] = (
                                    str(res.get("Name") or res.get("ShortName") or ""),
                                    nat,
                                )

                    if is_relay_race and race_cat in {"SW", "SM", "MX"}:
                        relay_points[race_cat][nat] = relay_points[race_cat].get(
                            nat, 0.0
                        ) + float(points)
                        relay_names[race_cat].setdefault(
                            nat,
                            str(
                                res.get("Name")
                                or res.get("ShortName")
                                or _country_display(nat)
                                or nat
                            ),
                        )

                    if race_cat in {"SW", "SM"}:
                        nations_points[race_cat][nat] = nations_points[race_cat].get(
                            nat, 0.0
                        ) + float(points)
                    elif race_cat == "MX":
                        nations_points["SW"][nat] = (
                            nations_points["SW"].get(nat, 0.0) + float(points) / 2.0
                        )
                        nations_points["SM"][nat] = (
                            nations_points["SM"].get(nat, 0.0) + float(points) / 2.0
                        )

    athlete_rows: dict[str, dict[str, list[dict]]] = {"SW": {}, "SM": {}}
    for cat in ("SW", "SM"):
        athlete_rows[cat]["TS"] = _rows_from_athlete_points(
            athlete_total[cat], athlete_info[cat]
        )
        for disc in ("SP", "PU", "IN", "MS"):
            athlete_rows[cat][disc] = _rows_from_athlete_points(
                athlete_disc[cat][disc], athlete_info[cat]
            )

    relay_all_points: dict[str, float] = {}
    relay_all_names: dict[str, str] = {}
    for cat in ("SW", "SM", "MX"):
        for nat, relay_pts in relay_points[cat].items():
            relay_all_points[nat] = relay_all_points.get(nat, 0.0) + relay_pts
        for nat, name in relay_names[cat].items():
            if nat not in relay_all_names and name:
                relay_all_names[nat] = name

    nations_all_points: dict[str, float] = {}
    for cat in ("SW", "SM"):
        for nat, nations_pts in nations_points[cat].items():
            nations_all_points[nat] = nations_all_points.get(nat, 0.0) + nations_pts

    relay_rows = {
        cat: _rows_from_country_points(relay_points[cat], relay_names[cat], limit)
        for cat in ("SW", "SM", "MX")
    }
    relay_rows["ALL"] = _rows_from_country_points(
        relay_all_points, relay_all_names, limit
    )

    nations_rows = {
        cat: _rows_from_country_points(nations_points[cat], None, limit)
        for cat in ("SW", "SM")
    }
    nations_rows["ALL"] = _rows_from_country_points(nations_all_points, None, limit)

    return {
        "athlete": athlete_rows,
        "relay": relay_rows,
        "nations": nations_rows,
    }


def _find_level1_cup_id(season_id: str, cat_id: str, discipline_id: str) -> str:
    target_cat = str(cat_id or "").upper()
    target_disc = str(discipline_id or "").upper()
    if target_disc == "SI":
        target_disc = "IN"
    try:
        cups = get_cups(season_id)
    except BiathlonError:
        return ""
    for cup in cups:
        if int(cup.get("Level") or 0) != 1:
            continue
        if str(cup.get("CatId") or "").upper() != target_cat:
            continue
        if str(cup.get("DisciplineId") or "").upper() != target_disc:
            continue
        cup_id = str(cup.get("CupId") or "")
        if cup_id:
            return cup_id
    return ""


def _fetch_cup_rows(cup_id: str) -> list[dict]:
    if not cup_id:
        return []
    try:
        payload = get_cup_results(cup_id)
    except BiathlonError:
        return []
    return payload.get("Rows") or payload.get("Results") or []


def _fetch_live_athlete_cup_rows(season_id: str) -> dict[str, dict[str, list[dict]]]:
    athlete_rows: dict[str, dict[str, list[dict]]] = {"SW": {}, "SM": {}}
    for cat_id in ("SW", "SM"):
        athlete_rows[cat_id]["TS"] = _fetch_cup_rows(
            _find_level1_cup_id(season_id, cat_id, "TS")
        )
        for disc in ("SP", "PU", "IN", "MS"):
            athlete_rows[cat_id][disc] = _fetch_cup_rows(
                _find_level1_cup_id(season_id, cat_id, disc)
            )
    return athlete_rows


def _render_ranked_table(
    title: str,
    rows: list[dict],
    args: argparse.Namespace,
    name_header: str,
) -> None:
    print(_preevent_heading(3, title, args))
    if not rows:
        print("none")
        print()
        return

    table_rows = []
    for idx, row in enumerate(rows):
        rank = str(row.get("Rank") or row.get("Standing") or idx + 1).rstrip(".")
        name = str(row.get("Name") or row.get("ShortName") or "")
        nat = str(row.get("Nat") or "")
        points = str(row.get("Score") or row.get("Points") or "0")
        table_rows.append([rank, name, nat, points])

    render_table(
        ["Rank", name_header, "Nat", "Points"],
        table_rows,
        output_format=get_output_format(args),
        alignments=["right", "left", "left", "right"],
        column_separators={3},
    )
    print()


def _relay_display_cells(row: dict | None, idx: int) -> list[str]:
    if not row:
        return ["-", "-", "-"]
    rank = str(row.get("Rank") or row.get("Standing") or idx + 1).rstrip(".")
    name = _normalize_team_name(row)
    points = str(row.get("Score") or row.get("Points") or "0")
    return [rank, name, points]


def _render_relay_tables(
    relay_rows: dict[str, list[dict]],
    args: argparse.Namespace,
) -> None:
    women_rows = relay_rows.get("SW", [])
    men_rows = relay_rows.get("SM", [])
    mixed_rows = relay_rows.get("MX", [])
    all_rows = relay_rows.get("ALL", [])
    if not all_rows:
        all_rows = _combine_country_rows([women_rows, men_rows, mixed_rows], limit=10)

    if not any((women_rows, men_rows, mixed_rows, all_rows)):
        print("none")
        print()
        return

    if not is_pretty_output(args):
        for title, rows in (
            ("Women Relay", women_rows),
            ("Men Relay", men_rows),
            ("Mixed Relay", mixed_rows),
            ("All Relay (unofficial)", all_rows),
        ):
            print(_preevent_heading(3, title, args))
            if not rows:
                print("none")
                print()
                continue
            table_rows = []
            for idx, row in enumerate(rows):
                rank = str(row.get("Rank") or row.get("Standing") or idx + 1).rstrip(
                    "."
                )
                name = _normalize_team_name(row)
                points = str(row.get("Score") or row.get("Points") or "0")
                table_rows.append([rank, name, points])
            render_table(
                ["Rank", "Team", "Points"],
                table_rows,
                output_format=get_output_format(args),
                alignments=["right", "left", "right"],
                column_separators={2},
            )
            print()
        return

    max_rows = max(len(women_rows), len(men_rows), len(mixed_rows), len(all_rows))
    combined_rows: list[list[str]] = []
    for idx in range(max_rows):
        combined_rows.append(
            _relay_display_cells(
                women_rows[idx] if idx < len(women_rows) else None, idx
            )
            + _relay_display_cells(men_rows[idx] if idx < len(men_rows) else None, idx)
            + _relay_display_cells(
                mixed_rows[idx] if idx < len(mixed_rows) else None, idx
            )
            + _relay_display_cells(all_rows[idx] if idx < len(all_rows) else None, idx)
        )

    render_table(
        [
            "Rank",
            "Team",
            "Points",
            "Rank",
            "Team",
            "Points",
            "Rank",
            "Team",
            "Points",
            "Rank",
            "Team          ",
            "Points",
        ],
        combined_rows,
        output_format=get_output_format(args),
        alignments=[
            "right",
            "left",
            "right",
            "right",
            "left",
            "right",
            "right",
            "left",
            "right",
            "right",
            "left",
            "right",
        ],
        column_separators={3, 6, 9},
        group_headers=[
            (0, 3, "Women Relay"),
            (3, 6, "Men Relay"),
            (6, 9, "Mixed Relay"),
            (9, 12, "All Relay (unofficial)"),
        ],
    )
    print()


def _capitalize_country_name(text: str) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    if len(value) <= 3 and value.isalpha():
        return value.upper()
    if value.isupper():
        return value.title()
    return value


def _normalize_team_name(row: dict | None) -> str:
    if not isinstance(row, dict):
        return ""
    nat = str(row.get("Nat") or "").strip().upper()
    if nat:
        mapped = _country_display(nat)
        if mapped:
            return mapped
    raw = str(row.get("Name") or row.get("ShortName") or "")
    return _capitalize_country_name(raw)


def _format_nations_points(value: object) -> str:
    text = str(value if value is not None else "").strip()
    if not text:
        return "0.0"
    try:
        number = float(text.replace(",", ""))
    except ValueError:
        return text
    return f"{number:.1f}"


def _all_rows_have_integer_points(rows: list[dict]) -> bool:
    if not rows:
        return False
    for row in rows:
        raw = row.get("Score")
        if raw in (None, ""):
            raw = row.get("Points")
        text = str(raw if raw is not None else "").strip()
        if not text:
            return False
        try:
            number = float(text.replace(",", ""))
        except ValueError:
            return False
        if not number.is_integer():
            return False
    return True


def _strip_decimal_zero_scores(rows: list[dict]) -> None:
    for row in rows:
        raw = row.get("Score")
        if raw in (None, ""):
            raw = row.get("Points")
        text = str(raw if raw is not None else "").strip()
        if not text:
            row["Score"] = "0"
            continue
        try:
            number = float(text.replace(",", ""))
        except ValueError:
            row["Score"] = text
            continue
        row["Score"] = _format_score_value(number)


def _nation_display_cells(row: dict | None, idx: int) -> list[str]:
    if not row:
        return ["-", "-", "-"]
    rank = str(row.get("Rank") or row.get("Standing") or idx + 1).rstrip(".")
    country = _normalize_team_name(row)
    points = str(row.get("Score") or row.get("Points") or "0")
    return [rank, country, points]


def _render_nations_tables(
    nations_rows: dict[str, list[dict]],
    args: argparse.Namespace,
) -> None:
    women_rows_raw = nations_rows.get("SW", [])
    men_rows_raw = nations_rows.get("SM", [])
    all_rows_raw = nations_rows.get("ALL", [])

    def _normalized(rows: list[dict]) -> list[dict]:
        normalized = []
        for row in rows:
            copy = dict(row)
            copy["Name"] = _normalize_team_name(copy)
            copy["Score"] = _format_nations_points(
                copy.get("Score") or copy.get("Points") or "0"
            )
            normalized.append(copy)
        return normalized

    women_rows = _normalized(women_rows_raw)
    men_rows = _normalized(men_rows_raw)
    all_rows = _normalized(all_rows_raw)
    if not all_rows:
        all_rows = _combine_country_rows(
            [women_rows, men_rows], limit=10, one_decimal=True
        )
        for row in all_rows:
            row["Score"] = _format_nations_points(
                row.get("Score") or row.get("Points") or "0"
            )

    if _all_rows_have_integer_points(all_rows):
        _strip_decimal_zero_scores(all_rows)

    if not any((women_rows, men_rows, all_rows)):
        print("none")
        print()
        return

    if not is_pretty_output(args):
        for title, rows in (
            ("Women", women_rows),
            ("Men", men_rows),
            ("Combined (unofficial)", all_rows),
        ):
            _render_ranked_table(title, rows, args, "Team")
        return

    max_rows = max(len(women_rows), len(men_rows), len(all_rows))
    combined_rows: list[list[str]] = []
    for idx in range(max_rows):
        combined_rows.append(
            _nation_display_cells(
                women_rows[idx] if idx < len(women_rows) else None, idx
            )
            + _nation_display_cells(men_rows[idx] if idx < len(men_rows) else None, idx)
            + _nation_display_cells(all_rows[idx] if idx < len(all_rows) else None, idx)
        )

    render_table(
        [
            "Rank",
            "Team",
            "Points",
            "Rank",
            "Team",
            "Points",
            "Rank",
            "Team          ",
            "Points",
        ],
        combined_rows,
        output_format=get_output_format(args),
        alignments=[
            "right",
            "left",
            "right",
            "right",
            "left",
            "right",
            "right",
            "left",
            "right",
        ],
        column_separators={3, 6},
        group_headers=[
            (0, 3, "Women"),
            (3, 6, "Men"),
            (6, 9, "Combined (unofficial)"),
        ],
    )
    print()


def _render_snapshot_athlete_standings_table(
    title: str,
    total_rows: list[dict],
    discipline_rows: dict[str, list[dict]],
    args: argparse.Namespace,
    reference_date: datetime.date | None = None,
) -> None:
    print(_preevent_heading(3, title, args))
    display_rows = list(total_rows[:10])
    if not display_rows:
        print("none")
        print()
        return

    pretty = is_pretty_output(args)

    disc_points_by_id: dict[str, dict[str, str]] = {
        disc: {} for disc in ("SP", "PU", "IN", "MS")
    }
    discipline_leader_by_disc: dict[str, str] = {}
    for disc in ("SP", "PU", "IN", "MS"):
        rows_for_disc = discipline_rows.get(disc, [])
        if rows_for_disc:
            leader_id = _row_ibu_id(rows_for_disc[0])
            if leader_id:
                discipline_leader_by_disc[disc] = leader_id
        for row in rows_for_disc:
            ibu_id = _row_ibu_id(row)
            if not ibu_id:
                continue
            value = str(row.get("Score") or row.get("Points") or "0").strip()
            disc_points_by_id[disc][ibu_id] = value

    total_leader_id = _row_ibu_id(total_rows[0]) if total_rows else ""

    athlete_ids = [_row_ibu_id(row) for row in display_rows if _row_ibu_id(row)]
    age_display_by_id: dict[str, str] = {}
    if athlete_ids:
        bios = _prefetch_bios(athlete_ids)
        for ibu_id in dict.fromkeys(athlete_ids):
            bio = bios.get(ibu_id, {})
            if reference_date is not None:
                birth_date = _extract_birth_date(bio)
                if birth_date is not None:
                    age_display_by_id[ibu_id] = str(
                        _age_on_date(birth_date, reference_date)
                    )
                    continue
            age_text = _extract_age_text(bio)
            age_display_by_id[ibu_id] = age_text or "-"

    def _score_or_dash(value: str) -> str:
        text = str(value or "").strip()
        if not text:
            return "-"
        try:
            if float(text) == 0.0:
                return "-"
        except ValueError:
            pass
        return text

    def _is_discipline_leader(ibu_id: str) -> bool:
        if not ibu_id:
            return False
        return any(
            leader_id == ibu_id for leader_id in discipline_leader_by_disc.values()
        )

    def _format_secondary_leader_cell(cell_str: str, row_idx: int) -> str:
        if not pretty or not Color.enabled():
            return cell_str
        row = display_rows[row_idx]
        ibu_id = _row_ibu_id(row)
        if ibu_id == total_leader_id:
            return Color.gold(cell_str)
        if _is_discipline_leader(ibu_id):
            return Color.rgb(cell_str, Color.LIGHT_GOLD, bold=False)
        return cell_str

    def _format_name_base(cell_str: str, row_idx: int) -> str:
        if not pretty or not Color.enabled():
            return cell_str
        row = display_rows[row_idx]
        ibu_id = _row_ibu_id(row)
        if ibu_id == total_leader_id:
            return Color.gold(cell_str)
        if _is_discipline_leader(ibu_id):
            return Color.rgb(cell_str, Color.LIGHT_GOLD, bold=True)
        return cell_str

    def _format_name_cell(cell_str: str, row_idx: int) -> str:
        return _format_leader_markers(cell_str, row_idx, _format_name_base)

    row_styles: list[str] = []
    table_rows: list[list[str]] = []
    for idx, row in enumerate(display_rows):
        rank = str(row.get("Rank") or row.get("Standing") or idx + 1).rstrip(".")
        ibu_id = _row_ibu_id(row)
        name = str(row.get("Name") or row.get("ShortName") or "")
        if pretty:
            markers: list[str] = []
            if ibu_id == total_leader_id:
                markers.append(GENERAL_LEADER_MARKER)
            for disc in ("SP", "PU", "IN", "MS"):
                if discipline_leader_by_disc.get(disc) == ibu_id:
                    markers.append(DISCIPLINE_LEADER_MARKER)
            if markers:
                name = name + " " + " ".join(markers)
        nat = str(row.get("Nat") or "")
        total = str(row.get("Score") or row.get("Points") or "0")
        age_display = age_display_by_id.get(ibu_id, "-")
        row_styles.append("gold" if ibu_id == total_leader_id else "")
        table_rows.append(
            [
                rank,
                name,
                nat,
                age_display,
                total,
                _score_or_dash(disc_points_by_id["SP"].get(ibu_id, "0")),
                _score_or_dash(disc_points_by_id["PU"].get(ibu_id, "0")),
                _score_or_dash(disc_points_by_id["IN"].get(ibu_id, "0")),
                _score_or_dash(disc_points_by_id["MS"].get(ibu_id, "0")),
            ]
        )

    def _make_disc_value_formatter(disc_key: str):
        def formatter(cell_str: str, row_idx: int) -> str:
            if not pretty or not Color.enabled():
                return cell_str
            row = display_rows[row_idx]
            ibu_id = _row_ibu_id(row)
            if discipline_leader_by_disc.get(disc_key) != ibu_id:
                return cell_str
            if ibu_id == total_leader_id:
                return Color.gold(cell_str)
            return Color.rgb(cell_str, Color.LIGHT_GOLD, bold=False)

        return formatter

    render_table(
        list(ATHLETE_STANDINGS_HEADERS),
        table_rows,
        output_format=get_output_format(args),
        alignments=[
            "right",
            "left",
            "left",
            "right",
            "right",
            "right",
            "right",
            "right",
            "right",
        ],
        row_styles=row_styles if pretty else None,
        cell_formatters=(
            [
                _format_secondary_leader_cell,
                _format_name_cell,
                _format_secondary_leader_cell,
                _format_secondary_leader_cell,
                None,
                _make_disc_value_formatter("SP"),
                _make_disc_value_formatter("PU"),
                _make_disc_value_formatter("IN"),
                _make_disc_value_formatter("MS"),
            ]
            if pretty
            else None
        ),
        column_separators=ATHLETE_STANDINGS_COLUMN_SEPARATORS,
    )
    print()


def _render_preevent_agenda(
    races: list[dict],
    args: argparse.Namespace,
    season_id: str,
    event_type: str,
    event_id: str,
    level: int = 1,
) -> None:
    print(
        _preevent_heading(
            2, PREEVENT_SECTION_TITLES[PREEVENT_SECTION_EVENT_AGENDA], args
        )
    )
    include_season_race_columns = event_type == EVENT_TYPE_WC
    sequence_by_race_id_disc: dict[tuple[str, str], str] = {}
    sequence_by_race_id_full: dict[tuple[str, str], str] = {}
    if include_season_race_columns:
        sequence_by_race_id_disc, sequence_by_race_id_full = (
            _build_season_race_sequence_maps(
                season_id, event_type, level=level, include_event_id=event_id
            )
        )
    schedule_rows: list[tuple[datetime.datetime | None, str, list[str]]] = []
    for race in races:
        race_cat = str(race.get("catId") or race.get("CatId") or "").upper()
        if race_cat not in {"SW", "SM", "MX"}:
            continue
        race_id = str(race.get("RaceId") or race.get("Id") or "")
        start_raw = str(race.get("StartTime") or race.get("StartDate") or "")
        start_dt = parse_start_datetime(start_raw)
        date_str, day_str, time_str, tz_str = _format_local_time(start_raw)
        time_with_tz = time_str
        if tz_str:
            time_with_tz = f"{time_str} {tz_str}".strip()
        cat_label = CATEGORY_DISPLAY_NAMES.get(race_cat, race_cat)
        disc_code = str(race.get("DisciplineId") or "").upper()
        disc_label = DISCIPLINE_NAMES.get(disc_code, disc_code)
        row = [date_str, day_str, time_with_tz, cat_label, disc_label]
        if include_season_race_columns:
            season_race = sequence_by_race_id_disc.get((race_cat, race_id), "-")
            season_race_full = sequence_by_race_id_full.get((race_cat, race_id), "-")
            row.extend([season_race, season_race_full])
        schedule_rows.append(
            (
                start_dt,
                race_id,
                row,
            )
        )

    if not schedule_rows:
        print("none")
        print()
        return

    fallback_dt = datetime.datetime.max.replace(tzinfo=datetime.timezone.utc)
    schedule_rows.sort(
        key=lambda item: (item[0] is None, item[0] or fallback_dt, item[1])
    )
    headers = ["Date", "Day", "Time", "Category", "Discipline"]
    if include_season_race_columns:
        headers.extend(["Season Race", "Season Race Full"])
    render_table(
        headers,
        [row for _dt, _race_id, row in schedule_rows],
        output_format=get_output_format(args),
    )
    print()


def handle_brief_preevent(args: argparse.Namespace) -> int:
    """Display a pre-event brief (agenda and standings snapshot)."""
    requested_event_id = str(getattr(args, "event", "") or "").strip()
    current_event: dict | None

    if requested_event_id:
        event_id = requested_event_id
        current_event = _find_event_by_id(event_id)
    else:
        current_event = _find_current_event()
        if not current_event:
            print("No current or upcoming level-1 event found", file=sys.stderr)
            return 1
        event_id = str(current_event.get("EventId") or "").strip()
        if not event_id:
            print("Event has no ID", file=sys.stderr)
            return 1

    try:
        races = get_races(event_id)
    except BiathlonError:
        races = []
    if not races:
        print(f"No races found for event {event_id}", file=sys.stderr)
        return 1

    first_race_meta = _first_event_race_with_start(races)
    if not first_race_meta:
        print(
            f"event {event_id} does not expose a race start datetime for snapshot mode",
            file=sys.stderr,
        )
        return 1

    first_race_id, cutoff_dt = first_race_meta
    season_id = str((current_event or {}).get("SeasonId") or "").strip()
    if not season_id:
        season_id = _season_id_from_event_id(event_id) or get_current_season_id()

    event_type, venue_name, first_race_payload = _resolve_event_type_and_venue(
        current_event, first_race_id
    )
    category_code = _preevent_category_code(event_type)
    event_country = (
        _resolve_event_country(current_event, first_race_payload, event_id, season_id)
        or "-"
    )
    venue_reference_date = cutoff_dt.date()
    venue_events = _collect_venue_level1_events(venue_name)
    wc_editions, wch_editions, owg_editions = _count_venue_event_editions(
        venue_name,
        venue_events=venue_events,
        reference_date=venue_reference_date,
    )
    standings = _compute_preevent_snapshot_standings(
        season_id, first_race_id, cutoff_dt, limit=10
    )
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    if cutoff_dt > now_utc:
        standings["athlete"] = _fetch_live_athlete_cup_rows(season_id)

    print()
    print(_preevent_heading(1, f"Event Brief - {venue_name}", args))
    print()
    if _preevent_section_enabled(PREEVENT_SECTION_EVENT_FACTS, category_code):
        print(
            _preevent_heading(
                2, PREEVENT_SECTION_TITLES[PREEVENT_SECTION_EVENT_FACTS], args
            )
        )
        render_table(
            ["Country", "WC Editions", "WCH Editions", "OWG Editions"],
            [[event_country, wc_editions, wch_editions, owg_editions]],
            output_format=get_output_format(args),
        )
        print()

    if _preevent_section_enabled(PREEVENT_SECTION_EVENT_AGENDA, category_code):
        level_raw = (current_event or {}).get("Level")
        level_text = str(level_raw).strip() if level_raw is not None else ""
        try:
            event_level = int(level_text) if level_text else 1
        except (TypeError, ValueError):
            event_level = 1
        _render_preevent_agenda(
            races, args, season_id, event_type, event_id, level=event_level
        )

    if _preevent_section_enabled(PREEVENT_SECTION_LAST_10_EDITIONS, category_code):
        print(_preevent_heading(2, f"Last 10 Editions at {venue_name}", args))
        recent_edition_rows = _build_recent_venue_edition_rows(
            venue_name,
            limit=10,
            venue_events=venue_events,
            reference_date=venue_reference_date,
        )
        if not recent_edition_rows:
            print("none")
            print()
        else:
            recent_edition_display_rows: list[list[str]] = []
            for row in recent_edition_rows:
                display_row: list[str] = [str(row[0]), str(row[1])]
                for value in row[2:]:
                    display_row.append(_race_type_presence_mark(value))
                recent_edition_display_rows.append(display_row)

            def _format_edition_mark(cell_str: str, _row_idx: int) -> str:
                return Color.highlight(cell_str) if cell_str == "X" else cell_str

            render_table(
                ["Edition", "Type"]
                + [label for _code, label in VENUE_RACE_TYPE_COLUMNS],
                recent_edition_display_rows,
                output_format=get_output_format(args),
                alignments=["left", "left"] + ["left" for _ in VENUE_RACE_TYPE_COLUMNS],
                column_separators={2},
                cell_formatters=[None, None]
                + [_format_edition_mark for _ in VENUE_RACE_TYPE_COLUMNS],
            )
            print()

    if _preevent_section_enabled(PREEVENT_SECTION_ATHLETE_STANDINGS, category_code):
        print(
            _preevent_heading(
                2, PREEVENT_SECTION_TITLES[PREEVENT_SECTION_ATHLETE_STANDINGS], args
            )
        )
        athlete_rows = cast(dict[str, dict[str, list[dict]]], standings["athlete"])
        athlete_tables = [
            ("Women", athlete_rows["SW"]),
            ("Men", athlete_rows["SM"]),
        ]
        if not any(rows_by_disc.get("TS") for _title, rows_by_disc in athlete_tables):
            print("none")
            print()
        else:
            print()
            for title, rows_by_disc in athlete_tables:
                _render_snapshot_athlete_standings_table(
                    title,
                    rows_by_disc.get("TS", []),
                    rows_by_disc,
                    args,
                    reference_date=cutoff_dt.date(),
                )

    if _preevent_section_enabled(PREEVENT_SECTION_RELAY_STANDINGS, category_code):
        print(
            _preevent_heading(
                2, PREEVENT_SECTION_TITLES[PREEVENT_SECTION_RELAY_STANDINGS], args
            )
        )
        relay_rows = cast(dict[str, list[dict]], standings["relay"])
        print()
        _render_relay_tables(relay_rows, args)

    if _preevent_section_enabled(PREEVENT_SECTION_NATIONS_CUP, category_code):
        print(
            _preevent_heading(
                2, PREEVENT_SECTION_TITLES[PREEVENT_SECTION_NATIONS_CUP], args
            )
        )
        nations_rows = cast(dict[str, list[dict]], standings["nations"])
        print()
        _render_nations_tables(nations_rows, args)

    show_decorated_venue = _preevent_section_enabled(
        PREEVENT_SECTION_DECORATED_VENUE, category_code
    )
    show_decorated_event_type = _preevent_section_enabled(
        PREEVENT_SECTION_DECORATED_EVENT_TYPE, category_code
    )
    if show_decorated_venue or show_decorated_event_type:
        current_season_highlight_keys = _collect_current_season_participant_keys(
            reference_date=venue_reference_date
        )

        if show_decorated_venue:
            decorated_rows, decorated_row_styles = _build_venue_decorated_athlete_rows(
                venue_name,
                venue_events=venue_events,
                reference_date=venue_reference_date,
                highlight_keys=current_season_highlight_keys,
                exclude_event_ids={event_id},
                limit=0,
            )
            _render_decorated_athletes_split_tables(
                f"Most Decorated Athletes at {venue_name}",
                decorated_rows,
                decorated_row_styles,
                args,
                per_gender_limit=15,
            )

        if show_decorated_event_type:
            event_type_label = EVENT_TYPE_LABELS.get(
                event_type, EVENT_TYPE_LABELS.get(EVENT_TYPE_WC, "World Cup")
            )
            decorated_scope_rows, decorated_scope_styles = (
                _build_event_type_decorated_athlete_rows(
                    event_type,
                    reference_date=venue_reference_date,
                    highlight_keys=current_season_highlight_keys,
                    exclude_event_ids={event_id},
                    limit=0,
                )
            )
            _render_decorated_athletes_split_tables(
                f"Most Decorated Athletes at {event_type_label}",
                decorated_scope_rows,
                decorated_scope_styles,
                args,
                per_gender_limit=15,
            )

    return 0


def handle_brief_postseason(args: argparse.Namespace) -> int:
    """Display a season summary (events and race breakdown)."""
    season_id = (getattr(args, "season", "") or "").strip() or get_current_season_id()
    level_raw = getattr(args, "level", "1")
    try:
        level_int = int(level_raw)
        if level_int < 1 or level_int > 6:
            raise ValueError
    except ValueError:
        print("error: level must be an integer between 1 and 6", file=sys.stderr)
        return 1

    try:
        seasons = get_seasons()
    except BiathlonError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    season_entry = next(
        (s for s in seasons if str(s.get("SeasonId")) == season_id), None
    )
    if not season_entry:
        print(f"season {season_id} not found", file=sys.stderr)
        return 1

    season_desc = str(season_entry.get("Description") or "").strip()
    is_current = bool(season_entry.get("IsCurrent"))

    try:
        events = get_events(season_id, level=level_int)
    except BiathlonError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    header = season_desc or season_id
    if season_desc and season_id and season_id not in season_desc:
        header = f"{season_desc} (season {season_id})"

    print()
    print(_format_section_title(f"Season Brief: {header}", args))
    print()

    print(_format_section_title("Season Facts:", args))
    print(f"  Season id: {season_id}")
    if season_desc:
        print(f"  Description: {season_desc}")
    print(f"  Current season: {'yes' if is_current else 'no'}")
    print(f"  Level: {format_level(level_int)}")

    event_count = len(events)
    print(f"  Events: {event_count}")

    if not events:
        print()
        return 0

    pretty = is_pretty_output(args)
    output_format = get_output_format(args)
    today = datetime.date.today()
    completed = 0
    upcoming = 0
    undated = 0
    start_dates: list[datetime.date] = []
    end_dates: list[datetime.date] = []
    total_races = 0
    discipline_counts: dict[str, int] = {}
    category_counts: dict[str, int] = {}
    races_by_event: dict[str, list[dict]] = {}

    for event in events:
        start = parse_date(event.get("StartDate") or event.get("FirstCompetitionDate"))
        end = parse_date(event.get("EndDate")) or start
        if start:
            start_dates.append(start)
        if end:
            end_dates.append(end)
        if end:
            if end < today:
                completed += 1
            else:
                upcoming += 1
        else:
            undated += 1

        event_id = event.get("EventId")
        races: list[dict] = []
        if event_id:
            try:
                races = get_races(event_id)
            except BiathlonError:
                races = []
            races_by_event[str(event_id)] = races

        total_races += len(races)
        for race in races:
            disc = str(race.get("DisciplineId") or "").upper()
            if disc:
                discipline_counts[disc] = discipline_counts.get(disc, 0) + 1
            cat = str(race.get("catId") or race.get("CatId") or "").upper()
            if cat:
                category_counts[cat] = category_counts.get(cat, 0) + 1

    suffix_parts = []
    if completed:
        suffix_parts.append(f"{completed} completed")
    if upcoming:
        suffix_parts.append(f"{upcoming} upcoming")
    if undated:
        suffix_parts.append(f"{undated} undated")
    if suffix_parts:
        print(f"  Event status: {', '.join(suffix_parts)}")

    if total_races:
        print(f"  Races: {total_races}")

    if start_dates or end_dates:
        start_label = min(start_dates).isoformat() if start_dates else ""
        end_label = max(end_dates).isoformat() if end_dates else ""
        if start_label and end_label:
            print(f"  Date range: {start_label} -> {end_label}")

    print()

    def date_only(value: str | None) -> str:
        return value.split("T", 1)[0] if isinstance(value, str) else ""

    def parse_rank(value: object) -> int | None:
        text = str(value or "").strip().rstrip(".")
        if text.isdigit():
            return int(text)
        return None

    agenda_events = sorted(
        events,
        key=lambda event: (
            event.get("StartDate") or event.get("FirstCompetitionDate") or ""
        ),
    )
    agenda_rows: list[list[str]] = []

    def mark(tags: set | bool) -> str:
        if isinstance(tags, bool):
            return "X" if tags else ""
        if not tags:
            return ""
        if "W+M" in tags or ("W" in tags and "M" in tags):
            return "W+M"
        return "+".join(sorted(tags))

    for event in agenda_events:
        event_id = event.get("EventId")
        race_list = races_by_event.get(str(event_id), []) if event_id else []
        flags: dict[str, set[str] | bool] = {
            "individual": set(),
            "sprint": set(),
            "pursuit": set(),
            "mass": set(),
            "relay": set(),
            "mixed_relay": False,
            "single_mixed": False,
        }

        for race in race_list:
            name = (
                race.get("RaceName")
                or race.get("ShortDescription")
                or race.get("Description")
                or ""
            ).lower()
            disc = (race.get("DisciplineId") or "").upper()
            gender_tag = ""
            if "women" in name or "women's" in name:
                gender_tag = "W"
            elif "men" in name:
                gender_tag = "M"

            if disc in {"IN", "SI"}:
                cast(set[str], flags["individual"]).add(gender_tag or "W+M")
            elif disc == "SP":
                cast(set[str], flags["sprint"]).add(gender_tag or "W+M")
            elif disc == "PU":
                cast(set[str], flags["pursuit"]).add(gender_tag or "W+M")
            elif disc == "MS":
                cast(set[str], flags["mass"]).add(gender_tag or "W+M")
            elif disc == "RL" or "relay" in name:
                if "single" in name and "mixed" in name:
                    flags["single_mixed"] = True
                elif "mixed" in name:
                    flags["mixed_relay"] = True
                else:
                    cast(set[str], flags["relay"]).add(gender_tag or "W+M")

        agenda_rows.append(
            [
                event.get("Description") or "",
                event.get("ShortDescription") or event.get("Organizer") or "",
                event.get("Nat")
                or event.get("Nation")
                or event.get("CountryId")
                or event.get("Country")
                or "",
                date_only(
                    event.get("StartDate") or event.get("FirstCompetitionDate") or ""
                ),
                str(len(race_list)),
                mark(flags["individual"]),
                mark(flags["sprint"]),
                mark(flags["pursuit"]),
                mark(flags["mass"]),
                mark(flags["relay"]),
                mark(flags["mixed_relay"]),
                mark(flags["single_mixed"]),
            ]
        )

    if agenda_rows:
        print(_format_section_title("Agenda:", args))
        agenda_styles = compute_event_styles(agenda_events) if pretty else None
        render_table(
            [
                "Event",
                "Location",
                "Country",
                "StartDate",
                "Races",
                "Individual",
                "Sprint",
                "Pursuit",
                "MassStart",
                "Relay",
                "MixedRelay",
                "SingleMixedRelay",
            ],
            agenda_rows,
            output_format=output_format,
            row_styles=agenda_styles,
            cell_formatters=[
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
            ],
        )
        print()

    decorated_stats: dict[str, dict[str, Any]] = {"SW": {}, "SM": {}}
    race_jobs: list[tuple[str, str]] = []
    for event in events:
        event_id = event.get("EventId")
        if not event_id:
            continue
        for race in races_by_event.get(str(event_id), []):
            cat_id = str(race.get("catId") or race.get("CatId") or "").upper()
            if cat_id not in {"SW", "SM"}:
                continue
            race_id = race.get("RaceId") or race.get("Id")
            if race_id:
                race_jobs.append((str(race_id), cat_id))

    def fetch_race_payload(race_id: str) -> tuple[str, dict | None]:
        try:
            return race_id, get_race_results(race_id)
        except BiathlonError:
            return race_id, None

    if race_jobs:
        with ThreadPoolExecutor(max_workers=_max_workers(len(race_jobs))) as executor:
            futures = {
                executor.submit(fetch_race_payload, race_id): (race_id, cat_id)
                for race_id, cat_id in race_jobs
            }
            for future in as_completed(futures):
                race_id, cat_id = futures[future]
                _, payload = future.result()
                if not payload or _is_true(payload.get("IsStartList")):
                    continue
                results = payload.get("Results") or []
                for res in results:
                    if res.get("IsTeam"):
                        continue
                    rank_val = parse_rank(
                        res.get("Rank") or res.get("SO") or res.get("ResultOrder")
                    )
                    if rank_val is None:
                        continue
                    ibu_id = _row_ibu_id(res)
                    name = res.get("Name") or res.get("ShortName") or ""
                    nat = res.get("Nat") or ""
                    key = ibu_id or f"{name}|{nat}"
                    stats = decorated_stats[cat_id].setdefault(
                        key,
                        {
                            "name": name,
                            "nat": nat,
                            "wins": 0,
                            "podiums": 0,
                            "flowers": 0,
                            "races": 0,
                        },
                    )
                    stats["races"] += 1
                    if rank_val == 1:
                        stats["wins"] += 1
                    elif 2 <= rank_val <= 3:
                        stats["podiums"] += 1
                    elif 4 <= rank_val <= 6:
                        stats["flowers"] += 1

    def render_decorated_section(label: str, stats_map: dict) -> None:
        decorated = [s for s in stats_map.values() if s["flowers"] > 0]
        decorated.sort(
            key=lambda s: (s["wins"], s["podiums"], s["flowers"], -s["races"]),
            reverse=True,
        )
        decorated = decorated[:10]
        if decorated:
            print(_format_section_title(f"Top Decorated {label}:", args))
            rows = []
            for idx, stats in enumerate(decorated, start=1):
                total = stats["wins"] + stats["podiums"] + stats["flowers"]
                rows.append(
                    [
                        idx,
                        stats["name"],
                        stats["nat"],
                        stats["wins"],
                        stats["podiums"],
                        stats["flowers"],
                        total,
                        stats["races"],
                    ]
                )

            render_table(
                [
                    "Rank",
                    "Athlete",
                    "Nat",
                    "Wins",
                    "Podiums",
                    "Flowers",
                    "Total",
                    "Races",
                ],
                rows,
                output_format=output_format,
                cell_formatters=[None, None, None, None, None, None, None, None],
            )
            print()
        else:
            print(_format_section_title(f"Top Decorated {label}: none", args))
            print()

    if category_counts:
        cat_labels = {"SW": "Women", "SM": "Men", "MX": "Mixed"}
        rows = []
        for code in ("SW", "SM", "MX"):
            if code in category_counts:
                rows.append([cat_labels.get(code, code), str(category_counts[code])])
        for code in sorted(k for k in category_counts if k not in {"SW", "SM", "MX"}):
            rows.append([cat_labels.get(code, code), str(category_counts[code])])
        print(_format_section_title("Race categories:", args))
        render_table(["Category", "Races"], rows, output_format=output_format)
        print()

    if discipline_counts:
        disc_labels = {
            "IN": "Individual",
            "SI": "Short Individual",
            "SP": "Sprint",
            "PU": "Pursuit",
            "MS": "Mass Start",
            "RL": "Relay",
            "MR": "Mixed Relay",
            "SR": "Single Mixed Relay",
        }
        order = ["IN", "SI", "SP", "PU", "MS", "RL", "MR", "SR"]
        rows = []
        for code in order:
            if code in discipline_counts:
                rows.append([disc_labels.get(code, code), str(discipline_counts[code])])
        for code in sorted(k for k in discipline_counts if k not in order):
            rows.append([disc_labels.get(code, code), str(discipline_counts[code])])
        print(_format_section_title("Race disciplines:", args))
        render_table(["Discipline", "Races"], rows, output_format=output_format)
        print()

    render_decorated_section("Women", decorated_stats["SW"])
    render_decorated_section("Men", decorated_stats["SM"])

    return 0


def handle_brief_startlist(args: argparse.Namespace) -> int:
    """Display startlist analysis (before a race).

    Shows sections 1-13 from the startlist analysis (World Cup races).
    Olympic races show Olympic history sections instead.
    """
    snapshot_mode = False
    snapshot_cutoff_dt: datetime.datetime | None = None
    explicit_race = bool(getattr(args, "race", ""))

    try:
        if explicit_race:
            race_id = args.race
            payload = get_race_results(race_id)
        else:
            candidates = _find_all_startlist_races()
            race_id, payload = _select_race_interactive(candidates)
    except BiathlonError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    comp = payload.get("Competition") or {}
    race_disc = str(comp.get("DisciplineId") or "").upper()
    is_startlist = _is_true(payload.get("IsStartList"))
    if not is_startlist:
        if not explicit_race:
            print(f"race {race_id} does not have a startlist", file=sys.stderr)
            return 1
        if is_relay_discipline(race_disc):
            has_completed_results = _has_completed_relay_results(payload)
        else:
            has_completed_results = _has_completed_results(payload)
        if not has_completed_results:
            print(
                f"race {race_id} does not have a startlist or completed results",
                file=sys.stderr,
            )
            return 1
        snapshot_mode = True
        snapshot_cutoff_dt = _resolve_race_start_datetime(comp)
        if snapshot_cutoff_dt is None:
            print(
                f"race {race_id} does not expose a race start datetime for snapshot mode",
                file=sys.stderr,
            )
            return 1

    entries = _build_startlist_entries(payload)
    team_entries = _build_team_entries(payload)
    if not entries and not team_entries:
        print(f"no startlist entries found for race {race_id}", file=sys.stderr)
        return 1

    args.leader_markers = True
    ctx = _prepare_startlist_context(
        payload,
        race_id,
        args,
        snapshot_target_race_id=race_id if snapshot_mode else "",
        snapshot_cutoff_dt=snapshot_cutoff_dt,
    )

    print()
    print(_format_section_title(format_race_header(payload, race_id), args))
    print(f"Startlist entries: {len(ctx['entries'])}")
    print()

    render_startlist_analysis(ctx, args)

    return 0


def _latest_completed_level1_event_id(season_id: str) -> str:
    if not season_id:
        return ""
    try:
        events = get_events(season_id, level=1)
    except BiathlonError:
        return ""

    today = datetime.date.today()
    completed: list[tuple[datetime.date, datetime.date, str]] = []
    for event in events:
        event_id = str(event.get("EventId") or "").strip()
        if not event_id:
            continue
        end_raw = str(event.get("EndDate") or event.get("StartDate") or "").strip()
        end_date = parse_date(end_raw.split("T", 1)[0] if end_raw else "")
        if end_date is None or end_date > today:
            continue
        start_raw = str(event.get("StartDate") or "").strip()
        start_date = parse_date(start_raw.split("T", 1)[0] if start_raw else "")
        completed.append((end_date, start_date or datetime.date.min, event_id))

    if not completed:
        return ""
    completed.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    return completed[0][2]


def _find_level1_mixed_relay_cup_id(season_id: str) -> str:
    if not season_id:
        return ""
    try:
        cups = get_cups(season_id)
    except BiathlonError:
        return ""
    for disc in ("MR", "SR", "RL"):
        for cup in cups:
            if cup.get("Level") != 1:
                continue
            if str(cup.get("CatId") or "").upper() != "MX":
                continue
            if str(cup.get("DisciplineId") or "").upper() != disc:
                continue
            cup_id = str(cup.get("CupId") or "").strip()
            if cup_id:
                return cup_id
    return ""


def _empty_standings_snapshot() -> dict[str, dict]:
    return {
        "athlete": {
            "SW": {"TS": [], "SP": [], "PU": [], "IN": [], "MS": []},
            "SM": {"TS": [], "SP": [], "PU": [], "IN": [], "MS": []},
        },
        "relay": {"SW": [], "SM": [], "MX": [], "ALL": []},
        "nations": {"SW": [], "SM": [], "ALL": []},
    }


def _fetch_live_postevent_standings(
    season_id: str,
    limit: int = 10,
) -> dict[str, dict]:
    snapshot = _empty_standings_snapshot()
    if not season_id:
        return snapshot

    athlete_rows = _fetch_live_athlete_cup_rows(season_id)
    snapshot["athlete"] = athlete_rows

    relay_sw = _fetch_cup_rows(_find_level1_cup_id(season_id, "SW", "RL"))[:limit]
    relay_sm = _fetch_cup_rows(_find_level1_cup_id(season_id, "SM", "RL"))[:limit]
    relay_mx = _fetch_cup_rows(_find_level1_mixed_relay_cup_id(season_id))[:limit]
    relay_all = _combine_country_rows([relay_sw, relay_sm, relay_mx], limit=limit)
    snapshot["relay"] = {
        "SW": relay_sw,
        "SM": relay_sm,
        "MX": relay_mx,
        "ALL": relay_all,
    }

    nations_sw = _fetch_cup_rows(_find_level1_cup_id(season_id, "SW", "NC"))[:limit]
    nations_sm = _fetch_cup_rows(_find_level1_cup_id(season_id, "SM", "NC"))[:limit]
    nations_all = _combine_country_rows(
        [nations_sw, nations_sm], limit=limit, one_decimal=True
    )
    snapshot["nations"] = {
        "SW": nations_sw,
        "SM": nations_sm,
        "ALL": nations_all,
    }
    return snapshot


def _athlete_delta_key(row: dict) -> str:
    ibu_id = _row_ibu_id(row)
    if ibu_id:
        return f"id:{ibu_id}"
    name = str(row.get("Name") or row.get("ShortName") or "").strip()
    nat = str(row.get("Nat") or "").strip().upper()
    return f"name:{name}|{nat}"


def _country_delta_key(row: dict) -> str:
    nat = str(row.get("Nat") or "").strip().upper()
    if nat:
        return f"nat:{nat}"
    team = _normalize_team_name(row)
    return f"team:{team}"


def _row_rank_value(row: dict, fallback_rank: int) -> int:
    parsed = _parse_rank(row.get("Rank") or row.get("Standing") or fallback_rank)
    return parsed if parsed is not None else fallback_rank


def _row_points_value(row: dict) -> float:
    return _parse_points_value(
        row.get("Score") if row.get("Score") not in (None, "") else row.get("Points")
    )


def _format_signed_delta(value: float) -> str:
    if abs(value) < 1e-9:
        return "0"
    text = _format_score_value(value)
    if value > 0:
        return f"+{text}"
    return text


def _rank_delta_text(previous_rank: int | None, current_rank: int) -> str:
    if previous_rank is None:
        return "new"
    delta = previous_rank - current_rank
    if delta > 0:
        return f"+{delta}"
    return str(delta)


def _build_postevent_athlete_delta_rows(
    after_rows: list[dict],
    before_rows: list[dict],
    limit: int = 10,
) -> tuple[list[list[str]], list[str]]:
    before_map: dict[str, tuple[int, float]] = {}
    for idx, row in enumerate(before_rows[:limit], start=1):
        key = _athlete_delta_key(row)
        if not key:
            continue
        before_map[key] = (_row_rank_value(row, idx), _row_points_value(row))

    table_rows: list[list[str]] = []
    row_styles: list[str] = []
    for idx, row in enumerate(after_rows[:limit], start=1):
        current_rank = _row_rank_value(row, idx)
        current_points = _row_points_value(row)
        key = _athlete_delta_key(row)
        previous = before_map.get(key)

        prev_rank_text = "-"
        rank_delta_text = "new"
        prev_points_text = "-"
        points_delta_text = "-"
        changed = True
        if previous is not None:
            previous_rank, previous_points = previous
            prev_rank_text = str(previous_rank)
            rank_delta_text = _rank_delta_text(previous_rank, current_rank)
            prev_points_text = _format_score_value(previous_points)
            points_delta_text = _format_signed_delta(current_points - previous_points)
            changed = (
                previous_rank != current_rank
                or abs(current_points - previous_points) > 1e-9
            )

        table_rows.append(
            [
                str(current_rank),
                str(row.get("Name") or row.get("ShortName") or ""),
                str(row.get("Nat") or ""),
                _format_score_value(current_points),
                prev_rank_text,
                rank_delta_text,
                prev_points_text,
                points_delta_text,
            ]
        )
        row_styles.append("highlight_plain" if changed else "")
    return table_rows, row_styles


def _build_postevent_country_delta_rows(
    after_rows: list[dict],
    before_rows: list[dict],
    limit: int = 10,
) -> tuple[list[list[str]], list[str]]:
    before_map: dict[str, tuple[int, float]] = {}
    for idx, row in enumerate(before_rows[:limit], start=1):
        key = _country_delta_key(row)
        if not key:
            continue
        before_map[key] = (_row_rank_value(row, idx), _row_points_value(row))

    table_rows: list[list[str]] = []
    row_styles: list[str] = []
    for idx, row in enumerate(after_rows[:limit], start=1):
        current_rank = _row_rank_value(row, idx)
        current_points = _row_points_value(row)
        key = _country_delta_key(row)
        previous = before_map.get(key)

        prev_rank_text = "-"
        rank_delta_text = "new"
        prev_points_text = "-"
        points_delta_text = "-"
        changed = True
        if previous is not None:
            previous_rank, previous_points = previous
            prev_rank_text = str(previous_rank)
            rank_delta_text = _rank_delta_text(previous_rank, current_rank)
            prev_points_text = _format_score_value(previous_points)
            points_delta_text = _format_signed_delta(current_points - previous_points)
            changed = (
                previous_rank != current_rank
                or abs(current_points - previous_points) > 1e-9
            )

        table_rows.append(
            [
                str(current_rank),
                _normalize_team_name(row),
                _format_score_value(current_points),
                prev_rank_text,
                rank_delta_text,
                prev_points_text,
                points_delta_text,
            ]
        )
        row_styles.append("highlight_plain" if changed else "")
    return table_rows, row_styles


def _parse_int_text(value: object) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _decorated_delta_key(row: list[str]) -> str:
    if len(row) < 4:
        return ""
    name = str(row[1]).strip()
    nat = str(row[2]).strip().upper()
    gender = str(row[3]).strip().upper()
    return f"{name}|{nat}|{gender}"


def _build_postevent_decorated_delta_rows(
    after_rows: list[list[str]],
    before_rows: list[list[str]],
    gender_code: str,
    limit: int = 15,
) -> tuple[list[list[str]], list[str]]:
    gender = str(gender_code).upper()
    before_filtered = [
        row
        for row in before_rows
        if len(row) >= 8 and str(row[3] or "").upper() == gender
    ][:limit]
    after_filtered = [
        row
        for row in after_rows
        if len(row) >= 8 and str(row[3] or "").upper() == gender
    ][:limit]

    before_map: dict[str, tuple[int, int, int, int, int]] = {}
    for row in before_filtered:
        key = _decorated_delta_key(row)
        if not key:
            continue
        before_map[key] = (
            _parse_int_text(row[0]),
            _parse_int_text(row[4]),
            _parse_int_text(row[5]),
            _parse_int_text(row[6]),
            _parse_int_text(row[7]),
        )

    table_rows: list[list[str]] = []
    row_styles: list[str] = []
    for row in after_filtered:
        key = _decorated_delta_key(row)
        current_rank = _parse_int_text(row[0])
        gold = _parse_int_text(row[4])
        silver = _parse_int_text(row[5])
        bronze = _parse_int_text(row[6])
        total = _parse_int_text(row[7])
        previous = before_map.get(key)

        prev_rank_text = "-"
        rank_delta = "new"
        gold_delta = "-"
        silver_delta = "-"
        bronze_delta = "-"
        total_delta = "-"
        changed = True
        if previous is not None:
            prev_rank, prev_gold, prev_silver, prev_bronze, prev_total = previous
            prev_rank_text = str(prev_rank)
            rank_delta = _rank_delta_text(prev_rank, current_rank)
            gold_delta = _format_signed_delta(float(gold - prev_gold))
            silver_delta = _format_signed_delta(float(silver - prev_silver))
            bronze_delta = _format_signed_delta(float(bronze - prev_bronze))
            total_delta = _format_signed_delta(float(total - prev_total))
            changed = (
                prev_rank != current_rank
                or gold != prev_gold
                or silver != prev_silver
                or bronze != prev_bronze
                or total != prev_total
            )

        table_rows.append(
            [
                str(current_rank),
                str(row[1]),
                str(row[2]),
                str(gold),
                str(silver),
                str(bronze),
                str(total),
                prev_rank_text,
                rank_delta,
                gold_delta,
                silver_delta,
                bronze_delta,
                total_delta,
            ]
        )
        row_styles.append("highlight_plain" if changed else "")
    return table_rows, row_styles


def _render_postevent_athlete_standings(
    args: argparse.Namespace,
    before_standings: dict[str, dict],
    after_standings: dict[str, dict],
    disciplines_raced: set[tuple[str, str]],
) -> None:
    before_athlete = cast(dict[str, dict[str, list[dict]]], before_standings["athlete"])
    after_athlete = cast(dict[str, dict[str, list[dict]]], after_standings["athlete"])
    disc_set = {disc for disc, _cat in disciplines_raced}
    disc_order = ["SP", "PU", "IN", "MS"]
    disc_labels = {
        "SP": "Sprint",
        "PU": "Pursuit",
        "IN": "Individual",
        "MS": "Mass Start",
    }

    specs: list[tuple[str, list[dict], list[dict]]] = [
        (
            "Overall — Women",
            after_athlete.get("SW", {}).get("TS", []),
            before_athlete.get("SW", {}).get("TS", []),
        ),
        (
            "Overall — Men",
            after_athlete.get("SM", {}).get("TS", []),
            before_athlete.get("SM", {}).get("TS", []),
        ),
    ]
    for disc in disc_order:
        if disc not in disc_set:
            continue
        label = disc_labels.get(disc, disc)
        specs.append(
            (
                f"{label} — Women",
                after_athlete.get("SW", {}).get(disc, []),
                before_athlete.get("SW", {}).get(disc, []),
            )
        )
        specs.append(
            (
                f"{label} — Men",
                after_athlete.get("SM", {}).get(disc, []),
                before_athlete.get("SM", {}).get(disc, []),
            )
        )

    if not any(rows for _title, rows, _before in specs):
        print("none")
        print()
        return

    print()
    for title, after_rows, before_rows in specs:
        print(_preevent_heading(3, title, args))
        delta_rows, row_styles = _build_postevent_athlete_delta_rows(
            after_rows, before_rows, limit=10
        )
        if not delta_rows:
            print("none")
            print()
            continue
        render_table(
            ["Rank", "Athlete", "Nat", "Points", "Prev", "Rank Δ", "Prev Pts", "Pts Δ"],
            delta_rows,
            output_format=get_output_format(args),
            alignments=[
                "right",
                "left",
                "left",
                "right",
                "right",
                "right",
                "right",
                "right",
            ],
            column_separators={4, 6},
            row_styles=row_styles if is_pretty_output(args) else None,
        )
        print()


def _render_postevent_relay_standings(
    args: argparse.Namespace,
    before_standings: dict[str, dict],
    after_standings: dict[str, dict],
) -> None:
    before_relay = cast(dict[str, list[dict]], before_standings["relay"])
    after_relay = cast(dict[str, list[dict]], after_standings["relay"])

    before_all = before_relay.get("ALL", []) or _combine_country_rows(
        [
            before_relay.get("SW", []),
            before_relay.get("SM", []),
            before_relay.get("MX", []),
        ],
        limit=10,
    )
    after_all = after_relay.get("ALL", []) or _combine_country_rows(
        [
            after_relay.get("SW", []),
            after_relay.get("SM", []),
            after_relay.get("MX", []),
        ],
        limit=10,
    )

    specs: list[tuple[str, list[dict], list[dict]]] = [
        ("Women Relay", after_relay.get("SW", []), before_relay.get("SW", [])),
        ("Men Relay", after_relay.get("SM", []), before_relay.get("SM", [])),
        ("Mixed Relay", after_relay.get("MX", []), before_relay.get("MX", [])),
        ("All Relay (unofficial)", after_all, before_all),
    ]
    if not any(rows for _title, rows, _before in specs):
        print("none")
        print()
        return

    print()
    for title, after_rows, before_rows in specs:
        print(_preevent_heading(3, title, args))
        delta_rows, row_styles = _build_postevent_country_delta_rows(
            after_rows, before_rows, limit=10
        )
        if not delta_rows:
            print("none")
            print()
            continue
        render_table(
            ["Rank", "Team", "Points", "Prev", "Rank Δ", "Prev Pts", "Pts Δ"],
            delta_rows,
            output_format=get_output_format(args),
            alignments=["right", "left", "right", "right", "right", "right", "right"],
            column_separators={3, 5},
            row_styles=row_styles if is_pretty_output(args) else None,
        )
        print()


def _render_postevent_nations_standings(
    args: argparse.Namespace,
    before_standings: dict[str, dict],
    after_standings: dict[str, dict],
) -> None:
    before_nations = cast(dict[str, list[dict]], before_standings["nations"])
    after_nations = cast(dict[str, list[dict]], after_standings["nations"])

    before_all = before_nations.get("ALL", []) or _combine_country_rows(
        [before_nations.get("SW", []), before_nations.get("SM", [])],
        limit=10,
        one_decimal=True,
    )
    after_all = after_nations.get("ALL", []) or _combine_country_rows(
        [after_nations.get("SW", []), after_nations.get("SM", [])],
        limit=10,
        one_decimal=True,
    )

    specs: list[tuple[str, list[dict], list[dict]]] = [
        ("Women", after_nations.get("SW", []), before_nations.get("SW", [])),
        ("Men", after_nations.get("SM", []), before_nations.get("SM", [])),
        ("Combined (unofficial)", after_all, before_all),
    ]
    if not any(rows for _title, rows, _before in specs):
        print("none")
        print()
        return

    print()
    for title, after_rows, before_rows in specs:
        print(_preevent_heading(3, title, args))
        delta_rows, row_styles = _build_postevent_country_delta_rows(
            after_rows, before_rows, limit=10
        )
        if not delta_rows:
            print("none")
            print()
            continue
        render_table(
            ["Rank", "Team", "Points", "Prev", "Rank Δ", "Prev Pts", "Pts Δ"],
            delta_rows,
            output_format=get_output_format(args),
            alignments=["right", "left", "right", "right", "right", "right", "right"],
            column_separators={3, 5},
            row_styles=row_styles if is_pretty_output(args) else None,
        )
        print()


def _render_postevent_decorated_delta_split_tables(
    title: str,
    before_rows: list[list[str]],
    after_rows: list[list[str]],
    args: argparse.Namespace,
    per_gender_limit: int = 15,
) -> None:
    print(_preevent_heading(2, title, args))
    print()

    headers = [
        "#",
        "Athlete",
        "Nat",
        "Gold",
        "Silver",
        "Bronze",
        "Total",
        "Prev",
        "Rank Δ",
        "Gold Δ",
        "Silver Δ",
        "Bronze Δ",
        "Total Δ",
    ]
    had_rows = False
    for gender_label, gender_code in (("Women", "F"), ("Men", "M")):
        print(_preevent_heading(3, gender_label, args))
        rows, row_styles = _build_postevent_decorated_delta_rows(
            after_rows, before_rows, gender_code, limit=per_gender_limit
        )
        if not rows:
            print("none")
            print()
            continue
        had_rows = True
        render_table(
            headers,
            rows,
            output_format=get_output_format(args),
            alignments=[
                "right",
                "left",
                "left",
                "right",
                "right",
                "right",
                "right",
                "right",
                "right",
                "right",
                "right",
                "right",
                "right",
            ],
            column_separators={7, 9},
            row_styles=row_styles if is_pretty_output(args) else None,
        )
        print()

    if not had_rows:
        return


def _render_postevent_best_performances(
    args: argparse.Namespace,
    completed_races: list[tuple[str, dict]],
    all_results_cache: dict[str, list[dict]],
    race_start_cache: dict[str, datetime.datetime | None],
    output_format: OutputFormat,
) -> None:
    MAJOR_LEVELS = {"WC", "WCH", "OWG"}
    warning_keys: set[str] = set()
    any_race_had_results = False

    print(
        _preevent_heading(
            2, POSTEVENT_SECTION_TITLES[POSTEVENT_SECTION_BEST_PERFORMANCES], args
        )
    )
    print()

    def _prev_label(value: int | None, scope: str) -> str:
        if value is None:
            return f"none ({scope})"
        return f"{_ordinal(value)} ({scope})"

    for race_id, payload in completed_races:
        comp = payload.get("Competition") or {}
        disc = str(comp.get("DisciplineId") or "").upper()
        is_relay = is_relay_discipline(disc)
        target_start_dt = _start_dt_from_competition(comp)
        race_start_cache[race_id] = target_start_dt

        results = list(payload.get("Results") or [])
        team_results = [r for r in results if r.get("IsTeam")]
        leg_results = [r for r in results if not r.get("IsTeam")]
        entries = [
            {
                "ibu_id": str(r.get("IBUId") or ""),
                "name": r.get("Name") or r.get("ShortName") or "",
                "nat": r.get("Nat") or "",
                "bib": str(r.get("Bib") or ""),
                "rank": r.get("Rank") or r.get("ResultOrder") or "",
                "irm": str(r.get("IRM") or "").upper(),
                "time": str(r.get("TotalTime") or r.get("Result") or ""),
            }
            for r in leg_results
        ]

        team_rank_by_bib: dict[str, int] = {}
        if is_relay:
            for team in team_results:
                bib = str(team.get("Bib") or "")
                rv = _parse_rank(team.get("Rank") or team.get("SO"))
                if bib and rv is not None:
                    team_rank_by_bib[bib] = rv

        disc_label = DISCIPLINE_NAMES.get(disc, disc)
        disc_label_lc = disc_label.lower()
        if is_relay:
            all_label = "Best Relay Results (all discipline)"
            discipline_label = f"Best Relay Results ({disc_label_lc})"
        else:
            all_label = "Best Individual Result (all discipline)"
            discipline_label = f"Best Individual Results ({disc_label_lc})"

        seen_ids: set[str] = set()
        perf_rows: list[tuple[int, str, str, str, str]] = []
        for entry in entries:
            ibu_id = entry["ibu_id"]
            if not ibu_id or ibu_id in seen_ids:
                continue
            seen_ids.add(ibu_id)

            if is_relay:
                current_rank = team_rank_by_bib.get(entry["bib"])
            else:
                current_rank = _parse_rank(entry["rank"])
            if entry["irm"] == "LAP" or "LAP" in entry["time"].upper():
                continue
            if disc == "PU" and current_rank is not None and current_rank >= 10000:
                continue
            if current_rank is None:
                continue

            major_ranked: list[tuple[dict, int]] = []
            for res in all_results_cache.get(ibu_id, []):
                if str(res.get("Level") or "").upper() not in MAJOR_LEVELS:
                    continue
                if not _is_result_at_or_before_target(
                    res,
                    race_id,
                    target_start_dt,
                    race_start_cache,
                    warning_keys,
                    f"best performances for {ibu_id}",
                ):
                    continue
                rv = _parse_rank(
                    res.get("Rank") or res.get("SO") or res.get("ResultOrder")
                )
                if rv is None:
                    continue
                major_ranked.append((res, rv))

            prior_rows = [
                (res, rv)
                for res, rv in major_ranked
                if str(res.get("RaceId") or "") != race_id
            ]
            prior_same_type = [
                (res, rv)
                for res, rv in prior_rows
                if _is_team_level_result(res) == is_relay
            ]
            prior_best_all = min((rv for _, rv in prior_same_type), default=None)
            prior_best_disc = min(
                (rv for res, rv in prior_rows if _result_discipline_id(res) == disc),
                default=None,
            )

            is_best_all = prior_best_all is None or current_rank < prior_best_all
            is_best_disc = prior_best_disc is None or current_rank < prior_best_disc
            if not is_best_all and not is_best_disc:
                continue

            if is_best_all:
                perf_rows.append(
                    (
                        current_rank,
                        entry["name"],
                        entry["nat"],
                        all_label,
                        _prev_label(prior_best_all, "all discipline"),
                    )
                )
            else:
                perf_rows.append(
                    (
                        current_rank,
                        entry["name"],
                        entry["nat"],
                        discipline_label,
                        _prev_label(prior_best_disc, disc_label_lc),
                    )
                )

        if not perf_rows:
            continue

        any_race_had_results = True
        perf_rows.sort(key=lambda item: (item[0], item[1]))
        print(_preevent_heading(3, format_race_header(payload, race_id), args))
        print()

        table_rows: list[list[str]] = []
        row_styles: list[str] = []
        for rank_val, name, nat, milestone, previous_best in perf_rows:
            table_rows.append([str(rank_val), name, nat, milestone, previous_best])
            if rank_val == 1:
                row_styles.append("gold")
            elif rank_val == 2:
                row_styles.append("silver")
            elif rank_val == 3:
                row_styles.append("bronze")
            else:
                row_styles.append("dim")
        render_table(
            ["Rank", "Athlete", "Nat", "Milestone", "Previous Best"],
            table_rows,
            output_format=output_format,
            row_styles=row_styles,
        )
        print()

    if not any_race_had_results:
        print("none")
        print()


def handle_brief_postevent(args: argparse.Namespace) -> int:
    """Post-event recap with matrix-gated sections and before/after deltas."""

    event_id: str = str(getattr(args, "event", "") or "").strip()
    current_event: dict | None = None

    if event_id:
        current_event = _find_event_by_id(event_id)
    else:
        try:
            season_id_for_lookup = get_current_season_id()
            all_events = get_events(season_id_for_lookup, level=1)
        except BiathlonError as exc:
            print(str(exc), file=sys.stderr)
            return 1

        today = datetime.date.today()
        dated: list[tuple[datetime.date, dict]] = []
        for event in all_events:
            end_raw = str(event.get("EndDate") or event.get("StartDate") or "").strip()
            end_date = parse_date(end_raw.split("T", 1)[0] if end_raw else "")
            if end_date is None or end_date > today:
                continue
            dated.append((end_date, event))
        dated.sort(key=lambda item: item[0], reverse=True)
        candidates = [event for _end, event in dated[:5]]

        if not candidates:
            print("No completed World Cup events found this season", file=sys.stderr)
            return 1

        if len(candidates) == 1 or not sys.stdin.isatty():
            current_event = candidates[0]
            event_id = str(current_event.get("EventId") or "").strip()
        else:
            print("\nRecent events:\n", file=sys.stderr)
            for idx, event in enumerate(candidates, 1):
                eid = str(event.get("EventId") or "?")
                desc = (
                    event.get("ShortDescription")
                    or event.get("Description")
                    or event.get("Organizer")
                    or eid
                )
                ev_type = detect_event_type(event)
                ev_type_label = EVENT_TYPE_LABELS.get(ev_type, ev_type)
                start_str = str(event.get("StartDate") or "").split("T", 1)[0]
                end_str = str(event.get("EndDate") or "").split("T", 1)[0]
                date_range = f"{start_str} – {end_str}" if end_str else start_str
                print(
                    f"  {idx}. [{ev_type_label}] {desc}  ({date_range})  [ID: {eid}]",
                    file=sys.stderr,
                )
            print(file=sys.stderr)
            while True:
                try:
                    choice = input(f"Enter selection (1-{len(candidates)}): ").strip()
                    selected = int(choice) - 1
                    if 0 <= selected < len(candidates):
                        current_event = candidates[selected]
                        event_id = str(current_event.get("EventId") or "").strip()
                        break
                    print("Invalid selection, try again.", file=sys.stderr)
                except ValueError:
                    print("Please enter a number.", file=sys.stderr)
                except (EOFError, KeyboardInterrupt):
                    print("Selection cancelled", file=sys.stderr)
                    return 1

    if not event_id:
        print("Could not determine event ID", file=sys.stderr)
        return 1

    try:
        all_races = get_races(event_id)
    except BiathlonError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if not all_races:
        print(f"No races found for event {event_id}", file=sys.stderr)
        return 1

    all_races.sort(
        key=lambda race: race.get("StartTime") or race.get("StartDate") or ""
    )
    first_race_meta = _first_event_race_with_start(all_races)
    if not first_race_meta:
        print(
            f"event {event_id} does not expose a race start datetime for snapshot mode",
            file=sys.stderr,
        )
        return 1
    first_race_id, first_race_start_dt = first_race_meta

    race_ids = [
        str(race.get("RaceId") or race.get("Id") or "")
        for race in all_races
        if race.get("RaceId") or race.get("Id")
    ]
    race_payloads: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=_max_workers(len(race_ids))) as executor:
        futures: dict[Any, str] = {
            executor.submit(get_race_results, race_id): race_id for race_id in race_ids
        }
        for future in as_completed(futures):
            race_id = futures[future]
            try:
                race_payloads[race_id] = future.result()
            except BiathlonError:
                continue

    completed_races: list[tuple[str, dict]] = []
    for race_id in race_ids:
        payload = race_payloads.get(race_id)
        if not payload:
            continue
        comp = payload.get("Competition") or {}
        disc = str(comp.get("DisciplineId") or "").upper()
        if is_relay_discipline(disc):
            if _has_completed_relay_results(payload):
                completed_races.append((race_id, payload))
        elif _has_completed_results(payload):
            completed_races.append((race_id, payload))

    if not completed_races:
        print(f"No completed races found for event {event_id}", file=sys.stderr)
        return 1

    season_id = str((current_event or {}).get("SeasonId") or "").strip()
    race_start_cache: dict[str, datetime.datetime | None] = {}
    disciplines_raced: set[tuple[str, str]] = set()
    last_completed_start_dt: datetime.datetime | None = None
    for race_id, payload in completed_races:
        comp = payload.get("Competition") or {}
        start_dt = _start_dt_from_competition(comp)
        race_start_cache[race_id] = start_dt
        if start_dt is not None and (
            last_completed_start_dt is None or start_dt > last_completed_start_dt
        ):
            last_completed_start_dt = start_dt

        disc = str(comp.get("DisciplineId") or "").upper()
        cat_id = str(comp.get("catId") or comp.get("CatId") or "").upper()
        if not season_id:
            sport_evt = payload.get("SportEvt") or {}
            season_id = str(sport_evt.get("SeasonId") or "").strip()
        if not is_relay_discipline(disc) and cat_id in {"SW", "SM"}:
            disc_key = "IN" if disc == "SI" else disc
            if disc_key in {"SP", "PU", "IN", "MS"}:
                disciplines_raced.add((disc_key, cat_id))

    if not season_id:
        season_id = _season_id_from_event_id(event_id) or get_current_season_id()

    event_type, venue_name, first_race_payload = _resolve_event_type_and_venue(
        current_event, first_race_id
    )
    category_code = _preevent_category_code(event_type)
    event_country = (
        _resolve_event_country(current_event, first_race_payload, event_id, season_id)
        or "-"
    )
    reference_dt = last_completed_start_dt or first_race_start_dt
    reference_date = reference_dt.date()
    venue_events = _collect_venue_level1_events(venue_name)
    wc_editions, wch_editions, owg_editions = _count_venue_event_editions(
        venue_name,
        venue_events=venue_events,
        reference_date=reference_date,
    )

    before_standings = _empty_standings_snapshot()
    after_standings = _empty_standings_snapshot()
    if season_id:
        before_standings = _compute_preevent_snapshot_standings(
            season_id,
            first_race_id,
            first_race_start_dt,
            limit=10,
        )
        latest_completed_id = _latest_completed_level1_event_id(season_id)
        use_live_after = bool(latest_completed_id and latest_completed_id == event_id)
        if use_live_after:
            after_standings = _fetch_live_postevent_standings(season_id, limit=10)
        else:
            cutoff_dt = reference_dt + datetime.timedelta(seconds=1)
            after_standings = _compute_preevent_snapshot_standings(
                season_id,
                "",
                cutoff_dt,
                limit=10,
            )

    all_results_cache: dict[str, list[dict]] = {}
    if _postevent_section_enabled(POSTEVENT_SECTION_BEST_PERFORMANCES, category_code):
        all_ibu_ids: set[str] = set()
        for _race_id, payload in completed_races:
            for result in payload.get("Results") or []:
                if result.get("IsTeam"):
                    continue
                ibu_id = str(result.get("IBUId") or "").strip()
                if ibu_id:
                    all_ibu_ids.add(ibu_id)
        if all_ibu_ids:
            ibu_list = list(all_ibu_ids)
            with ThreadPoolExecutor(
                max_workers=_max_workers(len(ibu_list))
            ) as executor:
                futures: dict[Any, str] = {
                    executor.submit(get_all_results, ibu_id): ibu_id
                    for ibu_id in ibu_list
                }
                for future in as_completed(futures):
                    ibu_id = futures[future]
                    try:
                        payload = future.result()
                        all_results_cache[ibu_id] = list(payload.get("Results") or [])
                    except BiathlonError:
                        all_results_cache[ibu_id] = []

    output_format = get_output_format(args)

    print()
    print(_preevent_heading(1, f"Event Brief - {venue_name}", args))
    print()

    if _postevent_section_enabled(POSTEVENT_SECTION_EVENT_FACTS, category_code):
        print(
            _preevent_heading(
                2, POSTEVENT_SECTION_TITLES[POSTEVENT_SECTION_EVENT_FACTS], args
            )
        )
        render_table(
            ["Country", "WC Editions", "WCH Editions", "OWG Editions"],
            [[event_country, wc_editions, wch_editions, owg_editions]],
            output_format=output_format,
        )
        print()

    if _postevent_section_enabled(POSTEVENT_SECTION_EVENT_AGENDA, category_code):
        level_raw = (current_event or {}).get("Level")
        level_text = str(level_raw).strip() if level_raw is not None else ""
        try:
            event_level = int(level_text) if level_text else 1
        except (TypeError, ValueError):
            event_level = 1
        _render_preevent_agenda(
            all_races,
            args,
            season_id,
            event_type,
            event_id,
            level=event_level,
        )

    if _postevent_section_enabled(POSTEVENT_SECTION_LAST_10_EDITIONS, category_code):
        print(_preevent_heading(2, f"Last 10 Editions at {venue_name}", args))
        recent_edition_rows = _build_recent_venue_edition_rows(
            venue_name,
            limit=10,
            venue_events=venue_events,
            reference_date=reference_date,
        )
        if not recent_edition_rows:
            print("none")
            print()
        else:
            display_rows: list[list[str]] = []
            for row in recent_edition_rows:
                display_row: list[str] = [str(row[0]), str(row[1])]
                for value in row[2:]:
                    display_row.append(_race_type_presence_mark(value))
                display_rows.append(display_row)

            def _format_edition_mark(cell_str: str, _row_idx: int) -> str:
                return Color.highlight(cell_str) if cell_str == "X" else cell_str

            render_table(
                ["Edition", "Type"]
                + [label for _code, label in VENUE_RACE_TYPE_COLUMNS],
                display_rows,
                output_format=output_format,
                alignments=["left", "left"] + ["left" for _ in VENUE_RACE_TYPE_COLUMNS],
                column_separators={2},
                cell_formatters=[None, None]
                + [_format_edition_mark for _ in VENUE_RACE_TYPE_COLUMNS],
            )
            print()

    if _postevent_section_enabled(POSTEVENT_SECTION_ATHLETE_STANDINGS, category_code):
        print(
            _preevent_heading(
                2, POSTEVENT_SECTION_TITLES[POSTEVENT_SECTION_ATHLETE_STANDINGS], args
            )
        )
        _render_postevent_athlete_standings(
            args,
            before_standings=before_standings,
            after_standings=after_standings,
            disciplines_raced=disciplines_raced,
        )

    if _postevent_section_enabled(POSTEVENT_SECTION_RELAY_STANDINGS, category_code):
        print(
            _preevent_heading(
                2, POSTEVENT_SECTION_TITLES[POSTEVENT_SECTION_RELAY_STANDINGS], args
            )
        )
        _render_postevent_relay_standings(
            args,
            before_standings=before_standings,
            after_standings=after_standings,
        )

    if _postevent_section_enabled(POSTEVENT_SECTION_NATIONS_CUP, category_code):
        print(
            _preevent_heading(
                2, POSTEVENT_SECTION_TITLES[POSTEVENT_SECTION_NATIONS_CUP], args
            )
        )
        _render_postevent_nations_standings(
            args,
            before_standings=before_standings,
            after_standings=after_standings,
        )

    if _postevent_section_enabled(POSTEVENT_SECTION_DECORATED_VENUE, category_code):
        before_rows, _before_styles = _build_venue_decorated_athlete_rows(
            venue_name,
            venue_events=venue_events,
            reference_date=reference_date,
            exclude_event_ids={event_id},
            limit=0,
        )
        after_rows, _after_styles = _build_venue_decorated_athlete_rows(
            venue_name,
            venue_events=venue_events,
            reference_date=reference_date,
            limit=0,
        )
        _render_postevent_decorated_delta_split_tables(
            f"Most Decorated Athletes at {venue_name}",
            before_rows,
            after_rows,
            args,
            per_gender_limit=15,
        )

    if _postevent_section_enabled(
        POSTEVENT_SECTION_DECORATED_EVENT_TYPE, category_code
    ):
        before_scope_rows, _before_scope_styles = (
            _build_event_type_decorated_athlete_rows(
                event_type,
                reference_date=reference_date,
                exclude_event_ids={event_id},
                limit=0,
            )
        )
        after_scope_rows, _after_scope_styles = (
            _build_event_type_decorated_athlete_rows(
                event_type,
                reference_date=reference_date,
                limit=0,
            )
        )
        event_type_label = EVENT_TYPE_LABELS.get(
            event_type,
            EVENT_TYPE_LABELS.get(EVENT_TYPE_WC, "World Cup"),
        )
        _render_postevent_decorated_delta_split_tables(
            f"Most Decorated Athletes at {event_type_label}",
            before_scope_rows,
            after_scope_rows,
            args,
            per_gender_limit=15,
        )

    if _postevent_section_enabled(POSTEVENT_SECTION_BEST_PERFORMANCES, category_code):
        _render_postevent_best_performances(
            args,
            completed_races,
            all_results_cache,
            race_start_cache,
            output_format,
        )

    return 0


def handle_brief_preseason(args: argparse.Namespace) -> int:
    """Season preview (before the season starts). Work in progress."""
    print("brief preseason: work in progress", file=sys.stderr)
    return 1


def handle_brief_postrace(args: argparse.Namespace) -> int:
    """Display post-race analysis (after a race).

    Delegates to the existing postrace handler.
    """
    return handle_post_race(args)
