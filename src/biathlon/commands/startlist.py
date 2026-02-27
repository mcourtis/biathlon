"""Startlist analysis command handler."""

from __future__ import annotations

import argparse
import datetime
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from typing import Any, Callable

from ..api import (
    BiathlonError,
    get_all_results,
    get_athlete_bio,
    get_cup_results,
    get_cups,
    get_current_season_id,
    get_events,
    get_race_results,
    get_races,
    get_seasons,
)
from ..constants import (
    CAT_TO_GENDER,
    CATEGORY_DISPLAY_NAMES,
    DISCIPLINE_NAMES,
    EVENT_TYPE_LABELS,
    EVENT_TYPE_OWG,
    EVENT_TYPE_WC,
    EVENT_TYPE_WCH,
    RELAY_DISCIPLINES,
)
from ..formatting import Color, is_pretty_output, get_output_format, render_table
from ..utils import parse_start_datetime, parse_time_seconds
from ._common import (
    DISCIPLINE_LEADER_MARKER,
    GENERAL_LEADER_MARKER,
    _format_leader_markers,
    _format_section_title,
    _max_workers,
    _ordinal,
    _parse_rank,
    _row_ibu_id,
    counts_toward_wc_standings,
    detect_event_type,
    is_mixed_relay as _is_mixed_relay,
    is_relay_discipline as _is_relay_disc,
)
from .results import _get_wc_rows, _has_completed_results


WC_RACE_MILESTONE_STEP = 25
WC_WIN_MILESTONE_STEP = 5
DISCIPLINES = {"SP", "PU", "IN", "MS", "SI"}
INDIVIDUAL_EQUIVALENT_DISCIPLINES = {"IN", "SI"}
MAJOR_EVENT_LEVELS = (1, 2, 3)
RACE_SEASON_RE = re.compile(r"^BT(?P<season>\d{4})")
SEASON_TEXT_RE = re.compile(r"^(?P<s1>\d{2})\s*/\s*(?P<s2>\d{2})$")
# Winter Olympics cadence:
# - 4-year cycle through 1992 (e.g. 1984, 1988, 1992)
# - one-time 2-year gap to 1994 after IOC schedule change
# - then 4-year alternating cycle thereafter
OLYMPIC_SEASON_IDS = [
    "2526",
    "2122",
    "1718",
    "1314",
    "0910",
    "0506",
    "0102",
    "9798",
    "9394",
    "9192",
    "8788",
    "8384",
]

# Display names for 3-letter country/NOC codes used in IBU results.
COUNTRY_CODE_TO_NAME = {
    "AND": "Andorra",
    "ARG": "Argentina",
    "ARM": "Armenia",
    "AUS": "Australia",
    "AUT": "Austria",
    "AZE": "Azerbaijan",
    "BEL": "Belgium",
    "BIH": "Bosnia and Herzegovina",
    "BLR": "Belarus",
    "BRA": "Brazil",
    "BUL": "Bulgaria",
    "CAN": "Canada",
    "CHE": "Switzerland",
    "CHN": "China",
    "CRO": "Croatia",
    "CZE": "Czech Republic",
    "ESP": "Spain",
    "EST": "Estonia",
    "EUN": "Unified Team",
    "FIN": "Finland",
    "FRA": "France",
    "FRG": "West Germany",
    "GBR": "Great Britain",
    "GDR": "East Germany",
    "GER": "Germany",
    "GRE": "Greece",
    "HUN": "Hungary",
    "ITA": "Italy",
    "JPN": "Japan",
    "KAZ": "Kazakhstan",
    "KOR": "South Korea",
    "LAT": "Latvia",
    "LTU": "Lithuania",
    "MDA": "Moldova",
    "MGL": "Mongolia",
    "MKD": "North Macedonia",
    "NED": "Netherlands",
    "NOR": "Norway",
    "NZL": "New Zealand",
    "OAR": "Olympic Athletes from Russia",
    "POL": "Poland",
    "ROU": "Romania",
    "ROC": "Russia",
    "RUS": "Russia",
    "SCG": "Serbia and Montenegro",
    "SLO": "Slovenia",
    "SRB": "Serbia",
    "SVK": "Slovakia",
    "SUI": "Switzerland",
    "SWE": "Sweden",
    "TCH": "Czechoslovakia",
    "UKR": "Ukraine",
    "URS": "Soviet Union",
    "USA": "United States",
    "YUG": "Yugoslavia",
}


def _country_display(value: str) -> str:
    code = str(value or "").strip().upper()
    if not code:
        return ""
    return COUNTRY_CODE_TO_NAME.get(code, str(value))


@lru_cache(maxsize=None)
def _event_country_display(event_id: str, season_id: str) -> str:
    """Resolve event host country display name from Events metadata."""
    if not event_id or not season_id:
        return ""
    for level in (1, 2, 3, 4, 5, 6):
        try:
            events = get_events(season_id, level)
        except BiathlonError:
            continue
        for event in events:
            if str(event.get("EventId") or "") != event_id:
                continue
            raw = str(
                event.get("Nat")
                or event.get("Nation")
                or event.get("CountryId")
                or event.get("Country")
                or ""
            ).strip()
            if raw:
                return _country_display(raw)
    return ""


# IBU World Cup points distribution (positions 1-40)
# Source: IBU Rules 2025, Chapter 3
WC_POINTS = {
    1: 90,
    2: 75,
    3: 65,
    4: 55,
    5: 50,
    6: 45,
    7: 41,
    8: 37,
    9: 34,
    10: 31,
    11: 30,
    12: 29,
    13: 28,
    14: 27,
    15: 26,
    16: 25,
    17: 24,
    18: 23,
    19: 22,
    20: 21,
    21: 20,
    22: 19,
    23: 18,
    24: 17,
    25: 16,
    26: 15,
    27: 14,
    28: 13,
    29: 12,
    30: 11,
    31: 10,
    32: 9,
    33: 8,
    34: 7,
    35: 6,
    36: 5,
    37: 4,
    38: 3,
    39: 2,
    40: 1,
}

# Mass Start points (30 positions, steeper drops after 21st)
WC_POINTS_MS = {
    1: 90,
    2: 75,
    3: 65,
    4: 55,
    5: 50,
    6: 45,
    7: 41,
    8: 37,
    9: 34,
    10: 31,
    11: 30,
    12: 29,
    13: 28,
    14: 27,
    15: 26,
    16: 25,
    17: 24,
    18: 23,
    19: 22,
    20: 21,
    21: 20,
    22: 18,
    23: 16,
    24: 14,
    25: 12,
    26: 10,
    27: 8,
    28: 6,
    29: 4,
    30: 2,
}

# Mapping from discipline code to cup suffix for discipline-specific cups
DISCIPLINE_CUP_SUFFIX = {
    "SP": "Sprint",
    "PU": "Pursuit",
    "IN": "Individual",
    "MS": "Mass Start",
    "SI": "Individual",  # SI shares the Individual cup with IN
}

MAX_FETCH_WORKERS = 15

STARTLIST_CATEGORY_CODES = ("WC", "WCH", "OWG")
STARTLIST_DISCIPLINE_CODES = ("SI", "IN", "SP", "PU", "MS", "RL", "MR", "SR")

SECTION_MISSING_TOP25 = "missing_top25_wc"
SECTION_PARTICIPATING_TEAMS = "participating_teams"
SECTION_WC_TOTAL = "wc_total_top10"
SECTION_WC_DISCIPLINE = "wc_discipline_top10"
SECTION_NATIONS_CUP = "nations_cup_top10"
SECTION_RELAY_WC = "relay_wc_top10"
SECTION_STANDINGS_WATCH = "standings_watch"
SECTION_PURSUIT_CONTENDERS = "pursuit_contenders_lt_1min"
SECTION_RACE_MILESTONES = "race_milestones"
SECTION_WIN_MILESTONES = "win_milestones"
SECTION_PREVIOUS_PODIUMS = "previous_podiums_last_two_seasons"
SECTION_PREVIOUS_OWG_PODIUMS = "previous_owg_podiums_all_editions"
SECTION_PREVIOUS_WCH_PODIUMS = "previous_wch_podiums_last_10_editions"
SECTION_COUNTRY_OWG_DISC = "country_owg_medals_discipline"
SECTION_COUNTRY_WCH_DISC = "country_wch_medals_discipline"
SECTION_ATHLETE_OWG_DISC = "athlete_owg_medals_discipline"
SECTION_ATHLETE_WCH_DISC = "athlete_wch_medals_discipline"
SECTION_COUNTRY_OWG_ALL = "country_owg_medals_all_disciplines"
SECTION_COUNTRY_WCH_ALL = "country_wch_medals_all_disciplines"
SECTION_ATHLETE_OWG_ALL = "athlete_owg_medals_all_disciplines"
SECTION_ATHLETE_WCH_ALL = "athlete_wch_medals_all_disciplines"

STARTLIST_SECTION_ORDER = [
    SECTION_MISSING_TOP25,
    SECTION_PARTICIPATING_TEAMS,
    SECTION_WC_TOTAL,
    SECTION_WC_DISCIPLINE,
    SECTION_NATIONS_CUP,
    SECTION_RELAY_WC,
    SECTION_STANDINGS_WATCH,
    SECTION_PURSUIT_CONTENDERS,
    SECTION_RACE_MILESTONES,
    SECTION_WIN_MILESTONES,
    SECTION_PREVIOUS_PODIUMS,
    SECTION_PREVIOUS_OWG_PODIUMS,
    SECTION_PREVIOUS_WCH_PODIUMS,
    SECTION_COUNTRY_OWG_DISC,
    SECTION_COUNTRY_WCH_DISC,
    SECTION_ATHLETE_OWG_DISC,
    SECTION_ATHLETE_WCH_DISC,
    SECTION_COUNTRY_OWG_ALL,
    SECTION_COUNTRY_WCH_ALL,
    SECTION_ATHLETE_OWG_ALL,
    SECTION_ATHLETE_WCH_ALL,
]

STARTLIST_SECTION_TITLES = {
    SECTION_MISSING_TOP25: "Missing from top 25 WC Standing",
    SECTION_PARTICIPATING_TEAMS: "Participating Teams",
    SECTION_WC_TOTAL: "WC Total Standings (Top 10)",
    SECTION_WC_DISCIPLINE: "Discipline WC Standings (Top 10)",
    SECTION_NATIONS_CUP: "Nations Cup Standings (Top 10)",
    SECTION_RELAY_WC: "Relay WC Standings (Top 10)",
    SECTION_STANDINGS_WATCH: "Standings Watch (what-if)",
    SECTION_PURSUIT_CONTENDERS: "Pursuit contenders (<1 min)",
    SECTION_RACE_MILESTONES: "Race milestones",
    SECTION_WIN_MILESTONES: "Win milestones",
    SECTION_PREVIOUS_PODIUMS: "Previous podiums",
    SECTION_PREVIOUS_OWG_PODIUMS: "Previous Olympic Games podiums (available editions)",
    SECTION_PREVIOUS_WCH_PODIUMS: "Previous World Championship podiums (available editions)",
    SECTION_COUNTRY_OWG_DISC: "Country OWG medal table (discipline) (all editions)",
    SECTION_COUNTRY_WCH_DISC: "Country WCH medal table (discipline) (all editions)",
    SECTION_ATHLETE_OWG_DISC: "Athlete OWG medal table (discipline) (all editions)",
    SECTION_ATHLETE_WCH_DISC: "Athlete WCH medal table (discipline) (all editions)",
    SECTION_COUNTRY_OWG_ALL: "Country OWG medal table (all discipline) (all editions)",
    SECTION_COUNTRY_WCH_ALL: "Country WCH medal table (all discipline) (all editions)",
    SECTION_ATHLETE_OWG_ALL: "Athlete Olympic Games Medal Table - All Disciplines (all editions)",
    SECTION_ATHLETE_WCH_ALL: "Athlete WCH medal table (all discipline) (all editions)",
}


def _matrix_row(
    wc: tuple[int, ...], wch: tuple[int, ...], owg: tuple[int, ...]
) -> dict[str, dict[str, bool]]:
    return {
        "WC": {disc: bool(v) for disc, v in zip(STARTLIST_DISCIPLINE_CODES, wc)},
        "WCH": {disc: bool(v) for disc, v in zip(STARTLIST_DISCIPLINE_CODES, wch)},
        "OWG": {disc: bool(v) for disc, v in zip(STARTLIST_DISCIPLINE_CODES, owg)},
    }


STARTLIST_SECTION_MATRIX = {
    SECTION_MISSING_TOP25: _matrix_row(
        (1, 1, 1, 1, 1, 0, 0, 0),
        (1, 1, 1, 1, 1, 0, 0, 0),
        (1, 1, 1, 1, 1, 0, 0, 0),
    ),
    SECTION_PARTICIPATING_TEAMS: _matrix_row(
        (0, 0, 0, 0, 0, 1, 1, 1),
        (0, 0, 0, 0, 0, 1, 1, 1),
        (0, 0, 0, 0, 0, 1, 1, 1),
    ),
    SECTION_WC_TOTAL: _matrix_row(
        (1, 1, 1, 1, 1, 0, 0, 0),
        (1, 1, 1, 1, 1, 0, 0, 0),
        (1, 1, 1, 1, 1, 0, 0, 0),
    ),
    SECTION_WC_DISCIPLINE: _matrix_row(
        (1, 1, 1, 1, 1, 0, 0, 0),
        (1, 1, 1, 1, 1, 0, 0, 0),
        (1, 1, 1, 1, 1, 0, 0, 0),
    ),
    SECTION_NATIONS_CUP: _matrix_row(
        (1, 1, 1, 0, 0, 1, 1, 1),
        (1, 1, 1, 0, 0, 1, 1, 1),
        (0, 0, 0, 0, 0, 0, 0, 0),
    ),
    SECTION_RELAY_WC: _matrix_row(
        (0, 0, 0, 0, 0, 1, 1, 1),
        (0, 0, 0, 0, 0, 1, 1, 1),
        (0, 0, 0, 0, 0, 1, 1, 1),
    ),
    SECTION_STANDINGS_WATCH: _matrix_row(
        (1, 1, 1, 1, 1, 1, 1, 1),
        (0, 0, 0, 0, 0, 0, 0, 0),
        (0, 0, 0, 0, 0, 0, 0, 0),
    ),
    SECTION_PURSUIT_CONTENDERS: _matrix_row(
        (0, 0, 0, 1, 0, 0, 0, 0),
        (0, 0, 0, 1, 0, 0, 0, 0),
        (0, 0, 0, 1, 0, 0, 0, 0),
    ),
    SECTION_RACE_MILESTONES: _matrix_row(
        (1, 1, 1, 1, 1, 1, 1, 1), (1, 1, 1, 1, 1, 1, 1, 1), (1, 1, 1, 1, 1, 1, 1, 1)
    ),
    SECTION_WIN_MILESTONES: _matrix_row(
        (1, 1, 1, 1, 1, 1, 1, 1), (1, 1, 1, 1, 1, 1, 1, 1), (1, 1, 1, 1, 1, 1, 1, 1)
    ),
    SECTION_PREVIOUS_PODIUMS: _matrix_row(
        (1, 1, 1, 1, 1, 1, 1, 1), (1, 1, 1, 1, 1, 1, 1, 1), (1, 1, 1, 1, 1, 1, 1, 1)
    ),
    SECTION_PREVIOUS_OWG_PODIUMS: _matrix_row(
        (0, 0, 0, 0, 0, 0, 0, 0),
        (0, 0, 0, 0, 0, 0, 0, 0),
        (1, 1, 1, 1, 1, 1, 1, 1),
    ),
    SECTION_PREVIOUS_WCH_PODIUMS: _matrix_row(
        (0, 0, 0, 0, 0, 0, 0, 0),
        (1, 1, 1, 1, 1, 1, 1, 1),
        (0, 0, 0, 0, 0, 0, 0, 0),
    ),
    SECTION_COUNTRY_OWG_DISC: _matrix_row(
        (0, 0, 0, 0, 0, 0, 0, 0),
        (0, 0, 0, 0, 0, 0, 0, 0),
        (1, 1, 1, 1, 1, 1, 1, 1),
    ),
    SECTION_COUNTRY_WCH_DISC: _matrix_row(
        (0, 0, 0, 0, 0, 0, 0, 0),
        (1, 1, 1, 1, 1, 1, 1, 1),
        (0, 0, 0, 0, 0, 0, 0, 0),
    ),
    SECTION_ATHLETE_OWG_DISC: _matrix_row(
        (0, 0, 0, 0, 0, 0, 0, 0),
        (0, 0, 0, 0, 0, 0, 0, 0),
        (1, 1, 1, 1, 1, 1, 1, 1),
    ),
    SECTION_ATHLETE_WCH_DISC: _matrix_row(
        (0, 0, 0, 0, 0, 0, 0, 0),
        (1, 1, 1, 1, 1, 1, 1, 1),
        (0, 0, 0, 0, 0, 0, 0, 0),
    ),
    SECTION_COUNTRY_OWG_ALL: _matrix_row(
        (0, 0, 0, 0, 0, 0, 0, 0),
        (0, 0, 0, 0, 0, 0, 0, 0),
        (1, 1, 1, 1, 1, 1, 1, 1),
    ),
    SECTION_COUNTRY_WCH_ALL: _matrix_row(
        (0, 0, 0, 0, 0, 0, 0, 0),
        (1, 1, 1, 1, 1, 1, 1, 1),
        (0, 0, 0, 0, 0, 0, 0, 0),
    ),
    SECTION_ATHLETE_OWG_ALL: _matrix_row(
        (0, 0, 0, 0, 0, 0, 0, 0),
        (0, 0, 0, 0, 0, 0, 0, 0),
        (1, 1, 1, 1, 1, 1, 1, 1),
    ),
    SECTION_ATHLETE_WCH_ALL: _matrix_row(
        (0, 0, 0, 0, 0, 0, 0, 0),
        (1, 1, 1, 1, 1, 1, 1, 1),
        (0, 0, 0, 0, 0, 0, 0, 0),
    ),
}


def _validate_startlist_section_matrix() -> None:
    expected_sections = set(STARTLIST_SECTION_ORDER)
    if set(STARTLIST_SECTION_TITLES) != expected_sections:
        raise ValueError("startlist section titles do not match section order")
    if set(STARTLIST_SECTION_MATRIX) != expected_sections:
        raise ValueError("startlist section matrix does not match section order")
    for section_id in STARTLIST_SECTION_ORDER:
        row = STARTLIST_SECTION_MATRIX[section_id]
        if set(row) != set(STARTLIST_CATEGORY_CODES):
            raise ValueError(f"matrix categories mismatch for section {section_id}")
        for cat_code in STARTLIST_CATEGORY_CODES:
            col = row[cat_code]
            if set(col) != set(STARTLIST_DISCIPLINE_CODES):
                raise ValueError(
                    f"matrix disciplines mismatch for section {section_id}/{cat_code}"
                )


_validate_startlist_section_matrix()


def _race_category_code(event_type: str) -> str:
    if event_type == EVENT_TYPE_WCH:
        return "WCH"
    if event_type == EVENT_TYPE_OWG:
        return "OWG"
    return "WC"


def _matrix_discipline_code(race_disc: str, cat_id: str) -> str:
    disc = str(race_disc or "").upper()
    cat = str(cat_id or "").upper()
    if disc == "RL" and cat == "MX":
        return "MR"
    if disc in STARTLIST_DISCIPLINE_CODES:
        return disc
    return "RL" if disc in RELAY_DISCIPLINES else "SP"


def _section_enabled(section_id: str, category_code: str, discipline_code: str) -> bool:
    row = STARTLIST_SECTION_MATRIX.get(section_id, {})
    return bool(row.get(category_code, {}).get(discipline_code, False))


def _section_title(section_id: str) -> str:
    return STARTLIST_SECTION_TITLES.get(section_id, section_id)


def _discipline_display_label(discipline: str, category: str) -> str:
    disc = str(discipline or "").upper()
    cat = str(category or "").upper()
    if disc == "RL":
        if cat == "SM":
            return "Men Relay"
        if cat == "SW":
            return "Women Relay"
    return DISCIPLINE_NAMES.get(disc, disc)


def _print_spaced_section_title(
    title: str,
    args: argparse.Namespace,
    level: int = 2,
    blank_before: int = 1,
    blank_after: int = 1,
) -> None:
    text = str(title).strip()
    if not text.startswith("#"):
        prefix = "#" * max(1, level)
        text = f"{prefix} {text}"
    for _ in range(max(0, blank_before)):
        print()
    print(_format_section_title(text, args))
    for _ in range(max(0, blank_after)):
        print()


def _print_section_none(section_id: str, args: argparse.Namespace) -> None:
    _print_spaced_section_title(f"{_section_title(section_id)}: none", args)


def _fetch_athlete_results(ibu_id: str) -> tuple[str, dict | None]:
    """Fetch all results for an athlete, returning (ibu_id, results_payload)."""
    if not ibu_id:
        return ibu_id, None
    try:
        return ibu_id, get_all_results(ibu_id)
    except BiathlonError:
        return ibu_id, None


def _is_true(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return False


def _leader_marker_suffix(
    ibu_id: str,
    general_leader_id: str,
    discipline_leader_id: str,
    enabled: bool,
) -> str:
    if not enabled or not ibu_id:
        return ""
    markers = []
    if general_leader_id and ibu_id == general_leader_id:
        markers.append(GENERAL_LEADER_MARKER)
    if discipline_leader_id and ibu_id == discipline_leader_id:
        markers.append(DISCIPLINE_LEADER_MARKER)
    if not markers:
        return ""
    return " " + " ".join(markers)


def _event_levels(use_major: bool) -> tuple[int, ...]:
    return MAJOR_EVENT_LEVELS if use_major else (1,)


def _parse_leg(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _mixed_relay_leg_matches(cat_id: str, discipline: str, leg: int | None) -> bool:
    if cat_id not in {"SW", "SM"}:
        return True
    if leg is None:
        return False
    if discipline == "SR":
        return leg % 2 == 1 if cat_id == "SW" else leg % 2 == 0
    return leg <= 2 if cat_id == "SW" else leg > 2


def _extract_age(bio: dict) -> str:
    personal = {
        p.get("Description", "").lower(): p.get("Value")
        for p in bio.get("Personal", [])
        if p.get("Description")
    }
    age_val = bio.get("Age") or personal.get("age") or "-"
    if isinstance(age_val, str) and "," in age_val:
        age_val = age_val.split(",", 1)[0].strip()
    if age_val in (None, ""):
        return "-"
    return str(age_val)


def _age_for_ibu(ibu_id: str, cache: dict[str, str]) -> str:
    if not ibu_id:
        return "-"
    if ibu_id in cache:
        return cache[ibu_id]
    try:
        bio = get_athlete_bio(ibu_id)
    except BiathlonError:
        cache[ibu_id] = "-"
        return cache[ibu_id]
    age = _extract_age(bio)
    cache[ibu_id] = age
    return age


def _fetch_age(ibu_id: str) -> tuple[str, str]:
    """Fetch age for an athlete, returning (ibu_id, age)."""
    if not ibu_id:
        return ibu_id, "-"
    try:
        bio = get_athlete_bio(ibu_id)
        return ibu_id, _extract_age(bio)
    except BiathlonError:
        return ibu_id, "-"


def _gender_cat_from_value(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text in {"W", "WOMEN", "F", "FEMALE"}:
        return "SW"
    if text in {"M", "MEN", "MALE"}:
        return "SM"
    return ""


def _fetch_gender(ibu_id: str) -> tuple[str, str]:
    """Fetch gender for an athlete."""
    if not ibu_id:
        return ibu_id, ""
    try:
        bio = get_athlete_bio(ibu_id)
        gender_cat = _gender_cat_from_value(bio.get("GenderId") or bio.get("Gender"))
        return ibu_id, gender_cat
    except BiathlonError:
        return ibu_id, ""


def _display_gender(gender_cat: str) -> str:
    """Convert SW/SM to F/M for display."""
    return {"SW": "F", "SM": "M"}.get(gender_cat, "-")


def _gender_cat_for_ibu(ibu_id: str, cache: dict[str, str]) -> str:
    if not ibu_id:
        return ""
    if ibu_id in cache:
        return cache[ibu_id]
    try:
        bio = get_athlete_bio(ibu_id)
    except BiathlonError:
        cache[ibu_id] = ""
        return cache[ibu_id]
    gender_cat = _gender_cat_from_value(bio.get("GenderId") or bio.get("Gender"))
    cache[ibu_id] = gender_cat
    return gender_cat


def _next_race_milestone(next_count: int) -> int | None:
    if next_count == 1:
        return next_count
    if next_count % WC_RACE_MILESTONE_STEP == 0:
        return next_count
    return None


def _next_win_milestone(next_count: int) -> int | None:
    if next_count % WC_WIN_MILESTONE_STEP == 0:
        return next_count
    return None


def _extract_venue_name(payload: dict) -> str:
    """Extract venue name from race payload."""
    sport_evt = payload.get("SportEvt") or {}
    comp = payload.get("Competition") or {}
    return (
        sport_evt.get("Organizer")
        or sport_evt.get("ShortDescription")
        or comp.get("Place")
        or ""
    ).strip()


def _venue_text_matches(venue_lower: str, text: str) -> bool:
    text = text.strip().lower()
    if not venue_lower or not text:
        return False
    return venue_lower in text or text in venue_lower


def _matches_venue(result: dict, venue_name: str) -> bool:
    """Check if a result is from the specified venue."""
    place = str(result.get("Place") or "")
    venue_lower = venue_name.strip().lower()
    return _venue_text_matches(venue_lower, place)


def _calculate_venue_stats(results: list[dict], entry: dict) -> dict:
    """Calculate venue-specific statistics for an athlete."""
    races = len(results)
    wins = 0
    podiums = 0
    flowers = 0
    ranks = []
    for res in results:
        rank_val = _parse_rank(res.get("Rank") or res.get("SO"))
        if rank_val is not None:
            ranks.append(rank_val)
            if rank_val == 1:
                wins += 1
            if 2 <= rank_val <= 3:
                podiums += 1
            if 4 <= rank_val <= 6:
                flowers += 1
    avg_rank = sum(ranks) / len(ranks) if ranks else None
    return {
        "ibu_id": entry.get("ibu_id", ""),
        "name": entry["name"],
        "age": entry.get("age", "-"),
        "gender": entry.get("gender", ""),
        "nat": entry["nat"],
        "races": races,
        "wins": wins,
        "podiums": podiums,
        "flowers": flowers,
        "avg_rank": avg_rank,
    }


def _identify_venue_records(venue_stats: list[dict]) -> dict[str, tuple]:
    """Find venue record holders among startlist athletes."""
    records = {
        "wins": (0, "", ""),
        "podiums": (0, "", ""),
        "flowers": (0, "", ""),
        "races": (0, "", ""),
    }
    for stats in venue_stats:
        if stats["wins"] > records["wins"][0]:
            records["wins"] = (stats["wins"], stats["name"], stats["nat"])
        if stats["podiums"] > records["podiums"][0]:
            records["podiums"] = (stats["podiums"], stats["name"], stats["nat"])
        if stats["flowers"] > records["flowers"][0]:
            records["flowers"] = (stats["flowers"], stats["name"], stats["nat"])
        if stats["races"] > records["races"][0]:
            records["races"] = (stats["races"], stats["name"], stats["nat"])
    return records


def _fetch_season_events(args: tuple) -> list[dict]:
    """Fetch events for a season/level combination."""
    season_id, level = args
    try:
        return get_events(str(season_id), level=level)
    except BiathlonError:
        return []


def _fetch_venue_races(event_id: str) -> list[dict]:
    """Fetch races for an event."""
    try:
        return get_races(event_id)
    except BiathlonError:
        return []


def _fetch_race_payload(race_id: str) -> tuple[str, dict | None]:
    """Fetch race results payload."""
    try:
        payload = get_race_results(race_id)
        if _is_true(payload.get("IsStartList")):
            return race_id, None
        return race_id, payload
    except BiathlonError:
        return race_id, None


def _get_venue_events_only(venue_name: str, use_major: bool) -> list[dict]:
    """Get list of events at a venue (lightweight - no race results).

    This is a fast version that only fetches events, not athlete statistics.
    Used for Event Facts section where we only need the events list.

    Args:
        venue_name: Name of the venue to search for
        use_major: Whether to include WCH and OWG in addition to WC

    Returns:
        List of dicts with event info (season_id, start_date, event)
    """
    if not venue_name:
        return []

    venue_lower = venue_name.strip().lower()
    venue_events: list[dict] = []

    # Get all seasons
    seasons = get_seasons()
    levels = _event_levels(use_major)

    # Fetch all events for all seasons/levels in parallel
    season_level_pairs = [
        (season.get("SeasonId"), level)
        for season in seasons
        for level in levels
        if season.get("SeasonId")
    ]

    all_events: list[tuple[str, dict]] = []
    with ThreadPoolExecutor(
        max_workers=_max_workers(len(season_level_pairs))
    ) as executor:
        futures = {
            executor.submit(_fetch_season_events, pair): pair
            for pair in season_level_pairs
        }
        for future in as_completed(futures):
            season_id, _ = futures[future]
            for event in future.result():
                organizer = str(event.get("Organizer") or "")
                short_desc = str(event.get("ShortDescription") or "")
                venue_match = _venue_text_matches(
                    venue_lower, organizer
                ) or _venue_text_matches(venue_lower, short_desc)
                if venue_match and event.get("EventId"):
                    all_events.append((str(season_id), event))

    # Deduplicate events by EventId
    seen_event_ids: set[str] = set()
    for season_id, event in all_events:
        event_id = event.get("EventId")
        if event_id and event_id not in seen_event_ids:
            seen_event_ids.add(event_id)
            start_date = event.get("StartDate") or ""
            venue_events.append(
                {
                    "season_id": season_id,
                    "start_date": start_date.split("T")[0] if start_date else "",
                    "event": event,
                }
            )

    return venue_events


def _get_alltime_venue_stats(
    venue_name: str, cat_id: str, use_major: bool, show_progress: bool = False
) -> tuple[list[dict], set[str], list[dict]]:
    """Get all-time venue statistics for all athletes who raced there.

    Args:
        venue_name: Name of the venue to search for
        cat_id: Category ID (SW/SM/MX), or empty string for all categories
        use_major: Whether to include WCH and OWG in addition to WC
        show_progress: If True, print progress to stderr

    Returns:
        Tuple of (athlete_stats_list, current_season_ibu_ids, venue_events_list)
        - athlete_stats_list: List of athlete stat dicts
        - current_season_ibu_ids: Set of IBU IDs of athletes who raced this season
        - venue_events_list: List of dicts with event info (season_id, start_date)
    """
    if not venue_name:
        return [], set(), []

    venue_lower = venue_name.strip().lower()
    athlete_stats: dict[str, dict] = {}  # ibu_id -> aggregated stats
    current_season_ids: set[str] = set()
    venue_events: list[dict] = []
    gender_cache: dict[str, str] = {}

    # Get all seasons and current season
    seasons = get_seasons()
    current_season = get_current_season_id()
    levels = _event_levels(use_major)

    if show_progress:
        print(
            "\rFetching venue history... gathering events",
            file=sys.stderr,
            end="",
            flush=True,
        )

    # Step 1: Fetch all events for all seasons/levels in parallel
    season_level_pairs = [
        (season.get("SeasonId"), level)
        for season in seasons
        for level in levels
        if season.get("SeasonId")
    ]

    all_events: list[tuple[str, dict]] = []  # (season_id, event)
    with ThreadPoolExecutor(
        max_workers=_max_workers(len(season_level_pairs))
    ) as executor:
        futures = {
            executor.submit(_fetch_season_events, pair): pair
            for pair in season_level_pairs
        }
        for future in as_completed(futures):
            season_id, _ = futures[future]
            for event in future.result():
                organizer = str(event.get("Organizer") or "")
                short_desc = str(event.get("ShortDescription") or "")
                venue_match = _venue_text_matches(
                    venue_lower, organizer
                ) or _venue_text_matches(venue_lower, short_desc)
                if venue_match and event.get("EventId"):
                    all_events.append((str(season_id), event))

    # Deduplicate events by EventId
    seen_event_ids: set[str] = set()
    unique_events: list[tuple[str, dict]] = []
    for season_id, event in all_events:
        event_id = event.get("EventId")
        if event_id and event_id not in seen_event_ids:
            seen_event_ids.add(event_id)
            unique_events.append((season_id, event))
    all_events = unique_events

    if show_progress:
        print(
            f"\rFetching venue history... found {len(all_events)} events",
            file=sys.stderr,
            end="",
            flush=True,
        )

    # Track venue events for facts
    for season_id, event in all_events:
        start_date = event.get("StartDate") or ""
        venue_events.append(
            {
                "season_id": season_id,
                "start_date": start_date.split("T")[0] if start_date else "",
                "event": event,
            }
        )

    # Step 2: Fetch races for all venue events in parallel
    event_ids: list[str] = [
        eid for _, event in all_events if (eid := event.get("EventId"))
    ]
    event_to_season: dict[str, str] = {
        eid: season_id
        for season_id, event in all_events
        if (eid := event.get("EventId"))
    }

    all_races: list[tuple[str, str, dict]] = []  # (season_id, event_id, race)
    with ThreadPoolExecutor(max_workers=_max_workers(len(event_ids))) as executor:
        ev_futures = {
            executor.submit(_fetch_venue_races, eid): eid for eid in event_ids
        }
        for future in as_completed(ev_futures):
            event_id = ev_futures[future]
            season_id = event_to_season.get(event_id, "")
            for race in future.result():
                race_id = race.get("RaceId") or race.get("Id")
                if race_id:
                    # Filter by category if specified
                    race_cat = str(race.get("catId") or race.get("CatId") or "").upper()
                    disc = str(race.get("DisciplineId") or "").upper()
                    is_relay = _is_relay_disc(disc)
                    is_mixed_relay = _is_mixed_relay(disc, race_cat)
                    if cat_id and race_cat != cat_id:
                        if not (is_relay and is_mixed_relay):
                            continue
                    all_races.append((season_id, event_id, race))

    if show_progress:
        print(
            f"\rFetching venue history... fetching {len(all_races)} races   ",
            file=sys.stderr,
            end="",
            flush=True,
        )

    # Step 3: Fetch race results in parallel
    race_ids: list[str] = [
        rid for _, _, race in all_races if (rid := race.get("RaceId") or race.get("Id"))
    ]
    race_to_info: dict[str, tuple[str, dict]] = {
        rid: (season_id, race)
        for season_id, _, race in all_races
        if (rid := race.get("RaceId") or race.get("Id"))
    }

    with ThreadPoolExecutor(max_workers=_max_workers(len(race_ids))) as executor:
        race_futures = {
            executor.submit(_fetch_race_payload, rid): rid for rid in race_ids
        }
        completed = 0
        for rf in as_completed(race_futures):
            completed += 1
            if show_progress and completed % 10 == 0:
                print(
                    f"\rFetching venue history... {completed}/{len(race_ids)} races   ",
                    file=sys.stderr,
                    end="",
                    flush=True,
                )

            race_id, payload = rf.result()
            if not payload:
                continue

            season_id, race = race_to_info.get(race_id) or ("", {})
            is_current_season = str(season_id) == str(current_season)

            disc = str(race.get("DisciplineId") or "").upper()
            is_relay = _is_relay_disc(disc)
            is_mixed_relay = _is_mixed_relay(
                disc, str(race.get("catId") or race.get("CatId") or "").upper()
            )
            results = payload.get("Results") or []

            if is_relay:
                team_rank_by_key: dict[str, int] = {}
                for res in results:
                    if not res.get("IsTeam"):
                        continue
                    bib = str(res.get("Bib") or "")
                    nat = str(res.get("Nat") or "")
                    rank_val = _parse_rank(res.get("Rank"))
                    if rank_val is None:
                        continue
                    if bib:
                        team_rank_by_key[f"bib:{bib}"] = rank_val
                    if nat:
                        team_rank_by_key.setdefault(f"nat:{nat}", rank_val)

                seen_ibu_ids: set[str] = set()
                for res in results:
                    if res.get("IsTeam"):
                        continue
                    ibu_id = res.get("IBUId") or res.get("IbuId") or ""
                    if not ibu_id or ibu_id in seen_ibu_ids:
                        continue
                    if is_mixed_relay and cat_id in {"SW", "SM"}:
                        gender_cat = _gender_cat_for_ibu(ibu_id, gender_cache)
                        if gender_cat:
                            if gender_cat != cat_id:
                                continue
                        elif not _mixed_relay_leg_matches(
                            cat_id, disc, _parse_leg(res.get("Leg"))
                        ):
                            continue
                    seen_ibu_ids.add(ibu_id)
                    name = res.get("Name") or res.get("ShortName") or ""
                    nat = res.get("Nat") or ""
                    if not name:
                        name = nat
                    if not nat:
                        continue
                    bib = str(res.get("Bib") or "")
                    key = f"bib:{bib}" if bib else f"nat:{nat}"
                    rank_val = team_rank_by_key.get(key)
                    if rank_val is None:
                        rank_val = _parse_rank(res.get("Rank") or res.get("SO"))

                    if ibu_id not in athlete_stats:
                        athlete_stats[ibu_id] = {
                            "ibu_id": ibu_id,
                            "name": name,
                            "nat": nat,
                            "races": 0,
                            "wins": 0,
                            "podiums": 0,
                            "flowers": 0,
                            "ranks": [],
                        }

                    if is_current_season:
                        current_season_ids.add(ibu_id)

                    stats = athlete_stats[ibu_id]
                    stats["races"] += 1
                    if rank_val is not None:
                        stats["ranks"].append(rank_val)
                        if rank_val == 1:
                            stats["wins"] += 1
                        if 2 <= rank_val <= 3:
                            stats["podiums"] += 1
                        if 4 <= rank_val <= 6:
                            stats["flowers"] += 1
            else:
                for res in results:
                    if res.get("IsTeam"):
                        continue

                    ibu_id = res.get("IBUId") or res.get("IbuId") or ""
                    name = res.get("Name") or res.get("ShortName") or ""
                    nat = res.get("Nat") or ""
                    if not ibu_id:
                        continue
                    if not name:
                        name = nat

                    rank_val = _parse_rank(res.get("Rank"))

                    if ibu_id not in athlete_stats:
                        athlete_stats[ibu_id] = {
                            "ibu_id": ibu_id,
                            "name": name,
                            "nat": nat,
                            "races": 0,
                            "wins": 0,
                            "podiums": 0,
                            "flowers": 0,
                            "ranks": [],
                        }

                    if is_current_season:
                        current_season_ids.add(ibu_id)

                    stats = athlete_stats[ibu_id]
                    stats["races"] += 1
                    if rank_val is not None:
                        stats["ranks"].append(rank_val)
                        if rank_val == 1:
                            stats["wins"] += 1
                        if 2 <= rank_val <= 3:
                            stats["podiums"] += 1
                        if 4 <= rank_val <= 6:
                            stats["flowers"] += 1

    # Clear progress line if we were showing progress
    if show_progress:
        print("\r" + " " * 50 + "\r", file=sys.stderr, end="", flush=True)

    # Calculate average rank and convert to list
    result = []
    for stats in athlete_stats.values():
        ranks = stats.pop("ranks")
        stats["avg_rank"] = sum(ranks) / len(ranks) if ranks else None
        result.append(stats)

    return result, current_season_ids, venue_events


def _get_alltime_major_event_stats(
    event_type: str, cat_id: str, show_progress: bool = False
) -> tuple[list[dict], set[str], list[dict]]:
    """Get all-time statistics for a major event type (OWG or WCH) across all venues.

    Args:
        event_type: Event type to filter by ("OWG" for Olympic Games, "WCH" for World Championships)
        cat_id: Category ID (SW/SM/MX), or empty string for all categories
        show_progress: If True, print progress to stderr

    Returns:
        Tuple of (athlete_stats_list, current_season_ibu_ids, matched_events_list)
    """
    # Determine keyword to search for in event descriptions
    if event_type == "OWG":
        keyword = "olympic"
    elif event_type == "WCH":
        keyword = "world championships"
    else:
        return [], set(), []

    athlete_stats: dict[str, dict] = {}  # ibu_id -> aggregated stats
    current_season_ids: set[str] = set()
    matched_events: list[dict] = []
    gender_cache: dict[str, str] = {}

    # Get all seasons and current season
    seasons = get_seasons()
    current_season = get_current_season_id()

    if show_progress:
        print(
            f"\rFetching {event_type} history... gathering events",
            file=sys.stderr,
            end="",
            flush=True,
        )

    # Step 1: Fetch all events for all seasons at level 1 (World Cup level includes major events)
    season_level_pairs = [
        (season.get("SeasonId"), 1) for season in seasons if season.get("SeasonId")
    ]

    all_events: list[tuple[str, dict]] = []  # (season_id, event)
    with ThreadPoolExecutor(
        max_workers=_max_workers(len(season_level_pairs))
    ) as executor:
        futures = {
            executor.submit(_fetch_season_events, pair): pair
            for pair in season_level_pairs
        }
        for future in as_completed(futures):
            season_id, _ = futures[future]
            for event in future.result():
                desc = str(
                    event.get("Description") or event.get("ShortDescription") or ""
                ).lower()
                if keyword in desc and event.get("EventId"):
                    all_events.append((str(season_id), event))

    # Deduplicate events by EventId
    seen_event_ids: set[str] = set()
    unique_events: list[tuple[str, dict]] = []
    for season_id, event in all_events:
        event_id = event.get("EventId")
        if event_id and event_id not in seen_event_ids:
            seen_event_ids.add(event_id)
            unique_events.append((season_id, event))
    all_events = unique_events

    if show_progress:
        print(
            f"\rFetching {event_type} history... found {len(all_events)} events",
            file=sys.stderr,
            end="",
            flush=True,
        )

    # Track matched events for facts
    for season_id, event in all_events:
        start_date = event.get("StartDate") or ""
        matched_events.append(
            {
                "season_id": season_id,
                "start_date": start_date.split("T")[0] if start_date else "",
                "event": event,
            }
        )

    # Step 2: Fetch races for all matched events in parallel
    event_ids: list[str] = [
        eid for _, event in all_events if (eid := event.get("EventId"))
    ]
    event_to_season: dict[str, str] = {
        eid: season_id
        for season_id, event in all_events
        if (eid := event.get("EventId"))
    }

    all_races: list[tuple[str, str, dict]] = []  # (season_id, event_id, race)
    with ThreadPoolExecutor(max_workers=_max_workers(len(event_ids))) as executor:
        ev_futures = {
            executor.submit(_fetch_venue_races, eid): eid for eid in event_ids
        }
        for future in as_completed(ev_futures):
            event_id = ev_futures[future]
            season_id = event_to_season.get(event_id, "")
            for race in future.result():
                race_id = race.get("RaceId") or race.get("Id")
                if race_id:
                    # Filter by category if specified
                    race_cat = str(race.get("catId") or race.get("CatId") or "").upper()
                    disc = str(race.get("DisciplineId") or "").upper()
                    is_relay = _is_relay_disc(disc)
                    is_mixed_relay = _is_mixed_relay(disc, race_cat)
                    if cat_id and race_cat != cat_id:
                        if not (is_relay and is_mixed_relay):
                            continue
                    all_races.append((season_id, event_id, race))

    if show_progress:
        print(
            f"\rFetching {event_type} history... fetching {len(all_races)} races   ",
            file=sys.stderr,
            end="",
            flush=True,
        )

    # Step 3: Fetch race results in parallel
    race_ids: list[str] = [
        rid for _, _, race in all_races if (rid := race.get("RaceId") or race.get("Id"))
    ]
    race_to_info: dict[str, tuple[str, dict]] = {
        rid: (season_id, race)
        for season_id, _, race in all_races
        if (rid := race.get("RaceId") or race.get("Id"))
    }

    with ThreadPoolExecutor(max_workers=_max_workers(len(race_ids))) as executor:
        race_futures = {
            executor.submit(_fetch_race_payload, rid): rid for rid in race_ids
        }
        completed = 0
        for rf in as_completed(race_futures):
            completed += 1
            if show_progress and completed % 10 == 0:
                print(
                    f"\rFetching {event_type} history... {completed}/{len(race_ids)} races   ",
                    file=sys.stderr,
                    end="",
                    flush=True,
                )

            race_id, payload = rf.result()
            if not payload:
                continue

            season_id, race = race_to_info.get(race_id) or ("", {})
            is_current_season = str(season_id) == str(current_season)

            disc = str(race.get("DisciplineId") or "").upper()
            is_relay = _is_relay_disc(disc)
            is_mixed_relay = _is_mixed_relay(
                disc, str(race.get("catId") or race.get("CatId") or "").upper()
            )
            results = payload.get("Results") or []

            if is_relay:
                team_rank_by_key: dict[str, int] = {}
                for res in results:
                    if not res.get("IsTeam"):
                        continue
                    bib = str(res.get("Bib") or "")
                    nat = str(res.get("Nat") or "")
                    rank_val = _parse_rank(res.get("Rank"))
                    if rank_val is None:
                        continue
                    if bib:
                        team_rank_by_key[f"bib:{bib}"] = rank_val
                    if nat:
                        team_rank_by_key.setdefault(f"nat:{nat}", rank_val)

                seen_ibu_ids: set[str] = set()
                for res in results:
                    if res.get("IsTeam"):
                        continue
                    ibu_id = res.get("IBUId") or res.get("IbuId") or ""
                    if not ibu_id or ibu_id in seen_ibu_ids:
                        continue
                    if is_mixed_relay and cat_id in {"SW", "SM"}:
                        gender_cat = _gender_cat_for_ibu(ibu_id, gender_cache)
                        if gender_cat:
                            if gender_cat != cat_id:
                                continue
                        elif not _mixed_relay_leg_matches(
                            cat_id, disc, _parse_leg(res.get("Leg"))
                        ):
                            continue
                    seen_ibu_ids.add(ibu_id)
                    name = res.get("Name") or res.get("ShortName") or ""
                    nat = res.get("Nat") or ""
                    if not name:
                        name = nat
                    if not nat:
                        continue
                    bib = str(res.get("Bib") or "")
                    key = f"bib:{bib}" if bib else f"nat:{nat}"
                    rank_val = team_rank_by_key.get(key)
                    if rank_val is None:
                        rank_val = _parse_rank(res.get("Rank") or res.get("SO"))

                    if ibu_id not in athlete_stats:
                        athlete_stats[ibu_id] = {
                            "ibu_id": ibu_id,
                            "name": name,
                            "nat": nat,
                            "races": 0,
                            "gold": 0,
                            "silver": 0,
                            "bronze": 0,
                            "ranks": [],
                        }

                    if is_current_season:
                        current_season_ids.add(ibu_id)

                    stats = athlete_stats[ibu_id]
                    stats["races"] += 1
                    if rank_val is not None:
                        stats["ranks"].append(rank_val)
                        if rank_val == 1:
                            stats["gold"] += 1
                        elif rank_val == 2:
                            stats["silver"] += 1
                        elif rank_val == 3:
                            stats["bronze"] += 1
            else:
                for res in results:
                    if res.get("IsTeam"):
                        continue

                    ibu_id = res.get("IBUId") or res.get("IbuId") or ""
                    name = res.get("Name") or res.get("ShortName") or ""
                    nat = res.get("Nat") or ""
                    if not ibu_id:
                        continue
                    if not name:
                        name = nat

                    rank_val = _parse_rank(res.get("Rank"))

                    if ibu_id not in athlete_stats:
                        athlete_stats[ibu_id] = {
                            "ibu_id": ibu_id,
                            "name": name,
                            "nat": nat,
                            "races": 0,
                            "gold": 0,
                            "silver": 0,
                            "bronze": 0,
                            "ranks": [],
                        }

                    if is_current_season:
                        current_season_ids.add(ibu_id)

                    stats = athlete_stats[ibu_id]
                    stats["races"] += 1
                    if rank_val is not None:
                        stats["ranks"].append(rank_val)
                        if rank_val == 1:
                            stats["gold"] += 1
                        elif rank_val == 2:
                            stats["silver"] += 1
                        elif rank_val == 3:
                            stats["bronze"] += 1

    # Clear progress line if we were showing progress
    if show_progress:
        print("\r" + " " * 60 + "\r", file=sys.stderr, end="", flush=True)

    # Calculate average rank and convert to list
    result = []
    for stats in athlete_stats.values():
        ranks = stats.pop("ranks")
        stats["avg_rank"] = sum(ranks) / len(ranks) if ranks else None
        result.append(stats)

    return result, current_season_ids, matched_events


def _get_alltime_major_event_stats_both(
    event_type: str, show_progress: bool = False
) -> tuple[list[dict], list[dict], set[str], set[str]]:
    """Get all-time statistics for both genders in a single pass (optimized).

    This fetches all races once and splits results by gender, avoiding duplicate API calls.

    Args:
        event_type: Event type to filter by ("OWG" for Olympic Games, "WCH" for World Championships)
        show_progress: If True, print progress to stderr

    Returns:
        Tuple of (women_stats, men_stats, women_current_ids, men_current_ids)
    """
    if event_type == "OWG":
        keyword = "olympic"
    elif event_type == "WCH":
        keyword = "world championships"
    else:
        return [], [], set(), set()

    # Separate stats for women and men
    women_stats: dict[str, dict] = {}
    men_stats: dict[str, dict] = {}
    women_current_ids: set[str] = set()
    men_current_ids: set[str] = set()
    gender_cache: dict[str, str] = {}

    seasons = get_seasons()
    current_season = get_current_season_id()

    if show_progress:
        print(
            f"\rFetching {event_type} history... gathering events",
            file=sys.stderr,
            end="",
            flush=True,
        )

    # Step 1: Fetch all events
    season_level_pairs = [(s.get("SeasonId"), 1) for s in seasons if s.get("SeasonId")]
    all_events: list[tuple[str, dict]] = []

    with ThreadPoolExecutor(
        max_workers=_max_workers(len(season_level_pairs))
    ) as executor:
        futures = {
            executor.submit(_fetch_season_events, pair): pair
            for pair in season_level_pairs
        }
        for future in as_completed(futures):
            season_id, _ = futures[future]
            for event in future.result():
                desc = str(
                    event.get("Description") or event.get("ShortDescription") or ""
                ).lower()
                if keyword in desc and event.get("EventId"):
                    all_events.append((str(season_id), event))

    # Deduplicate
    seen = set()
    unique_events = []
    for sid, ev in all_events:
        eid = ev.get("EventId")
        if eid and eid not in seen:
            seen.add(eid)
            unique_events.append((sid, ev))
    all_events = unique_events

    if show_progress:
        print(
            f"\rFetching {event_type} history... found {len(all_events)} events",
            file=sys.stderr,
            end="",
            flush=True,
        )

    # Step 2: Fetch ALL races (both genders) for all events
    event_ids: list[str] = [eid for _, ev in all_events if (eid := ev.get("EventId"))]
    event_to_season: dict[str, str] = {
        eid: sid for sid, ev in all_events if (eid := ev.get("EventId"))
    }

    all_races: list[tuple[str, dict]] = []  # (season_id, race)
    with ThreadPoolExecutor(max_workers=_max_workers(len(event_ids))) as executor:
        ev_futures = {
            executor.submit(_fetch_venue_races, eid): eid for eid in event_ids
        }
        for future in as_completed(ev_futures):
            event_id = ev_futures[future]
            season_id = event_to_season.get(event_id, "")
            for race in future.result():
                if race.get("RaceId") or race.get("Id"):
                    all_races.append((season_id, race))

    if show_progress:
        print(
            f"\rFetching {event_type} history... fetching {len(all_races)} races   ",
            file=sys.stderr,
            end="",
            flush=True,
        )

    # Step 3: Fetch results for ALL races in parallel
    race_ids: list[str] = [
        rid for _, r in all_races if (rid := r.get("RaceId") or r.get("Id"))
    ]
    race_to_info: dict[str, tuple[str, dict]] = {
        rid: (sid, r) for sid, r in all_races if (rid := r.get("RaceId") or r.get("Id"))
    }

    with ThreadPoolExecutor(max_workers=_max_workers(len(race_ids))) as executor:
        race_futures = {
            executor.submit(_fetch_race_payload, rid): rid for rid in race_ids
        }
        completed = 0
        for rf in as_completed(race_futures):
            completed += 1
            if show_progress and completed % 10 == 0:
                print(
                    f"\rFetching {event_type} history... {completed}/{len(race_ids)} races   ",
                    file=sys.stderr,
                    end="",
                    flush=True,
                )

            race_id, payload = rf.result()
            if not payload:
                continue

            season_id, race = race_to_info.get(race_id) or ("", {})
            is_current = str(season_id) == str(current_season)
            race_cat = str(race.get("catId") or race.get("CatId") or "").upper()
            disc = str(race.get("DisciplineId") or "").upper()
            is_relay = _is_relay_disc(disc)
            results = payload.get("Results") or []

            def add_stat(
                ibu_id: str, name: str, nat: str, rank_val: int | None, cat: str
            ):
                """Add stats to the appropriate gender bucket."""
                stats_dict = women_stats if cat == "SW" else men_stats
                current_ids = women_current_ids if cat == "SW" else men_current_ids

                if ibu_id not in stats_dict:
                    stats_dict[ibu_id] = {
                        "ibu_id": ibu_id,
                        "name": name,
                        "nat": nat,
                        "races": 0,
                        "gold": 0,
                        "silver": 0,
                        "bronze": 0,
                        "ranks": [],
                    }
                if is_current:
                    current_ids.add(ibu_id)
                s = stats_dict[ibu_id]
                s["races"] += 1
                if rank_val is not None:
                    s["ranks"].append(rank_val)
                    if rank_val == 1:
                        s["gold"] += 1
                    elif rank_val == 2:
                        s["silver"] += 1
                    elif rank_val == 3:
                        s["bronze"] += 1

            if is_relay:
                # Handle relay results
                team_rank_by_key: dict[str, int] = {}
                for res in results:
                    if res.get("IsTeam"):
                        bib = str(res.get("Bib") or "")
                        nat = str(res.get("Nat") or "")
                        rank_val = _parse_rank(res.get("Rank"))
                        if rank_val is not None:
                            if bib:
                                team_rank_by_key[f"bib:{bib}"] = rank_val
                            if nat:
                                team_rank_by_key.setdefault(f"nat:{nat}", rank_val)

                seen_ibu: set[str] = set()
                for res in results:
                    if res.get("IsTeam"):
                        continue
                    ibu_id = res.get("IBUId") or res.get("IbuId") or ""
                    if not ibu_id or ibu_id in seen_ibu:
                        continue
                    seen_ibu.add(ibu_id)

                    # Determine gender for this athlete
                    athlete_cat = _gender_cat_for_ibu(ibu_id, gender_cache)
                    if not athlete_cat:
                        # Try to infer from race category or leg
                        if race_cat in ("SW", "SM"):
                            athlete_cat = race_cat
                        elif _is_mixed_relay(disc, race_cat):
                            leg = _parse_leg(res.get("Leg"))
                            if _mixed_relay_leg_matches("SW", disc, leg):
                                athlete_cat = "SW"
                            elif _mixed_relay_leg_matches("SM", disc, leg):
                                athlete_cat = "SM"
                            else:
                                continue
                        else:
                            continue
                    if athlete_cat not in ("SW", "SM"):
                        continue

                    name = res.get("Name") or res.get("ShortName") or ""
                    nat = res.get("Nat") or ""
                    if not name:
                        name = nat
                    if not nat:
                        continue

                    bib = str(res.get("Bib") or "")
                    key = f"bib:{bib}" if bib else f"nat:{nat}"
                    rank_val = team_rank_by_key.get(key)
                    if rank_val is None:
                        rank_val = _parse_rank(res.get("Rank") or res.get("SO"))

                    add_stat(ibu_id, name, nat, rank_val, athlete_cat)
            else:
                # Individual race
                if race_cat not in ("SW", "SM"):
                    continue
                for res in results:
                    if res.get("IsTeam"):
                        continue
                    ibu_id = res.get("IBUId") or res.get("IbuId") or ""
                    name = res.get("Name") or res.get("ShortName") or ""
                    nat = res.get("Nat") or ""
                    if not ibu_id:
                        continue
                    if not name:
                        name = nat
                    rank_val = _parse_rank(res.get("Rank"))
                    add_stat(ibu_id, name, nat, rank_val, race_cat)

    if show_progress:
        print("\r" + " " * 60 + "\r", file=sys.stderr, end="", flush=True)

    # Convert to lists
    def to_list(stats_dict: dict) -> list[dict]:
        result = []
        for s in stats_dict.values():
            ranks = s.pop("ranks")
            s["avg_rank"] = sum(ranks) / len(ranks) if ranks else None
            result.append(s)
        return result

    return to_list(women_stats), to_list(men_stats), women_current_ids, men_current_ids


def _get_team_venue_stats(
    venue_name: str, cat_id: str, discipline: str, use_major: bool
) -> tuple[list[dict], int]:
    """Get all-time venue statistics for teams in relay races.

    Returns a tuple of (team_stats_list, total_races_count).
    """
    if not venue_name:
        return [], 0

    venue_lower = venue_name.strip().lower()
    team_stats: dict[str, dict] = {}  # nat -> aggregated stats
    total_races = 0

    # Get all seasons
    seasons = get_seasons()
    levels = _event_levels(use_major)
    disciplines = RELAY_DISCIPLINES if _is_relay_disc(discipline) else {discipline}

    for season in seasons:
        season_id = season.get("SeasonId")
        if not season_id:
            continue

        for level in levels:
            # Get events for this season/level
            try:
                events = get_events(str(season_id), level=level)
            except BiathlonError:
                continue

            # Find events at this venue
            for event in events:
                organizer = str(event.get("Organizer") or "")
                short_desc = str(event.get("ShortDescription") or "")
                venue_match = _venue_text_matches(
                    venue_lower, organizer
                ) or _venue_text_matches(venue_lower, short_desc)
                if not venue_match:
                    continue

                event_id = event.get("EventId")
                if not event_id:
                    continue

                # Get races for this event
                try:
                    races = get_races(event_id)
                except BiathlonError:
                    continue

                for race in races:
                    race_id = race.get("RaceId") or race.get("Id")
                    if not race_id:
                        continue

                    # Only include matching relay disciplines
                    disc = str(race.get("DisciplineId") or "").upper()
                    if disc not in disciplines:
                        continue

                    # Check category matches (allow mixed relays for gendered stats)
                    race_cat = str(race.get("catId") or race.get("CatId") or "").upper()
                    if cat_id and race_cat != cat_id:
                        if not (_is_relay_disc(disc) and race_cat == "MX"):
                            continue

                    try:
                        payload = get_race_results(race_id)
                    except BiathlonError:
                        continue

                    # Skip if it's a startlist (no results yet)
                    if _is_true(payload.get("IsStartList")):
                        continue

                    total_races += 1

                    # Process team results only
                    for res in payload.get("Results") or []:
                        if not res.get("IsTeam"):
                            continue
                        nat = res.get("Nat") or ""
                        if not nat:
                            continue

                        team_name = res.get("Name") or res.get("ShortName") or nat
                        rank_val = _parse_rank(res.get("Rank"))

                        if nat not in team_stats:
                            team_stats[nat] = {
                                "name": team_name,
                                "nat": nat,
                                "races": 0,
                                "wins": 0,
                                "podiums": 0,
                                "flowers": 0,
                                "ranks": [],
                            }

                        stats = team_stats[nat]
                        stats["races"] += 1
                        if rank_val is not None:
                            stats["ranks"].append(rank_val)
                            if rank_val == 1:
                                stats["wins"] += 1
                            if 2 <= rank_val <= 3:
                                stats["podiums"] += 1
                            if 4 <= rank_val <= 6:
                                stats["flowers"] += 1

    # Calculate average rank and convert to list
    result = []
    for stats in team_stats.values():
        ranks = stats.pop("ranks")
        stats["avg_rank"] = sum(ranks) / len(ranks) if ranks else None
        result.append(stats)

    return result, total_races


def _get_cup_ids_for_race(
    season_id: str, cat_id: str, discipline: str
) -> tuple[str | None, str | None]:
    """Get total and discipline-specific cup IDs for a race.

    Returns (total_cup_id, discipline_cup_id).
    Uses Level=1 (World Cup), CatId, and DisciplineId to match cups.
    """
    if cat_id not in CAT_TO_GENDER:
        return None, None

    try:
        cups = get_cups(season_id)
    except BiathlonError:
        return None, None

    # SI (Short Individual) shares the Individual cup with IN
    cup_discipline = "IN" if discipline == "SI" else discipline

    total_cup_id = None
    discipline_cup_id = None

    for cup in cups:
        # Only match World Cup level (Level=1)
        if cup.get("Level") != 1:
            continue
        # Must match category (SW/SM)
        if cup.get("CatId") != cat_id:
            continue

        cup_id = cup.get("CupId") or ""
        disc_id = cup.get("DisciplineId") or ""

        # Total standings use "TS" discipline ID
        if disc_id == "TS":
            total_cup_id = cup_id
        # Match discipline standings
        elif disc_id == cup_discipline:
            discipline_cup_id = cup_id

    return total_cup_id, discipline_cup_id


def _find_mixed_relay_cup(season_id: str, discipline: str) -> str | None:
    """Find cup ID for mixed relay standings (SR or MR with MX category)."""
    try:
        cups = get_cups(season_id)
        for cup in cups:
            # Look for MX category with SR or MR discipline at Level 1
            if (
                cup.get("CatId") == "MX"
                and cup.get("Level") == 1
                and cup.get("DisciplineId") in {"SR", "MR", discipline}
            ):
                return str(cup.get("CupId"))
    except BiathlonError:
        pass
    return None


def _relay_wc_standings_label(category: str, discipline: str) -> str:
    """Return a display label for relay World Cup standings."""
    cat_id = str(category or "").upper()
    disc_id = str(discipline or "").upper()
    if _is_mixed_relay(disc_id, cat_id):
        if disc_id == "RL":
            return "Mixed Relay"
        return DISCIPLINE_NAMES.get(disc_id, "Mixed Relay")
    cat_name = CATEGORY_DISPLAY_NAMES.get(cat_id, cat_id)
    disc_name = DISCIPLINE_NAMES.get(disc_id, disc_id)
    return f"{cat_name} {disc_name}".strip() or "Relay"


def _fetch_relay_wc_standings(
    season_id: str,
    category: str,
    discipline: str,
    limit: int = 10,
) -> tuple[str, list[dict]]:
    """Fetch World Cup standings rows for the corresponding relay cup."""
    label = _relay_wc_standings_label(category, discipline)
    cat_id = str(category or "").upper()
    disc_id = str(discipline or "").upper()
    if not season_id or not _is_relay_disc(disc_id):
        return label, []

    cup_id: str | None = None
    if _is_mixed_relay(disc_id, cat_id):
        cup_id = _find_mixed_relay_cup(season_id, disc_id)
    elif cat_id in CAT_TO_GENDER:
        _, relay_cup_id = _get_cup_ids_for_race(season_id, cat_id, disc_id)
        cup_id = relay_cup_id

    if not cup_id:
        return label, []
    return label, _fetch_standings(cup_id, limit=limit)


def _fetch_standings(cup_id: str, limit: int = 5) -> list[dict]:
    """Fetch top N standings entries from a cup."""
    if not cup_id:
        return []
    try:
        data = get_cup_results(cup_id)
    except BiathlonError:
        return []

    rows = data.get("Rows") or []
    return rows[:limit]


def _find_nations_cup_id(season_id: str, cat_id: str) -> str | None:
    cat = str(cat_id or "").upper()
    if cat not in {"SW", "SM"}:
        return None
    try:
        cups = get_cups(season_id)
    except BiathlonError:
        return None
    for cup in cups:
        if cup.get("Level") != 1:
            continue
        if str(cup.get("CatId") or "").upper() != cat:
            continue
        if str(cup.get("DisciplineId") or "").upper() != "NC":
            continue
        cup_id = str(cup.get("CupId") or "")
        if cup_id:
            return cup_id
    return None


def _fetch_nations_cup_standings(
    season_id: str, cat_id: str, limit: int = 10
) -> list[dict]:
    cup_id = _find_nations_cup_id(season_id, cat_id)
    if not cup_id:
        return []
    return _fetch_standings(cup_id, limit=limit)


def _compute_nations_pre_race_standings(
    season_id: str,
    target_race_id: str,
    cutoff_dt: datetime.datetime | None,
    cat_id: str,
    limit: int = 10,
) -> list[dict]:
    """Approximate Nations Cup standings before a race start cutoff."""
    cat = str(cat_id or "").upper()
    if cutoff_dt is None or cat not in {"SW", "SM"}:
        return []

    points_by_nat: dict[str, float] = {}
    try:
        events = get_events(season_id, level=1)
    except BiathlonError:
        return []

    for event in events:
        event_type = detect_event_type(event)
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
            start_dt = parse_start_datetime(
                str(race.get("StartTime") or race.get("StartDate") or "")
            )
            if start_dt is None or start_dt > cutoff_dt:
                continue
            try:
                payload = get_race_results(race_id)
            except BiathlonError:
                continue
            if not _has_completed_results(payload):
                continue
            race_cat = str(race.get("catId") or race.get("CatId") or "").upper()
            race_disc = str(race.get("DisciplineId") or "").upper()
            if not counts_toward_wc_standings(
                event_type,
                season_id,
                discipline=race_disc,
                category=race_cat,
            ):
                continue
            is_team_race = race_disc in RELAY_DISCIPLINES
            for res in payload.get("Results", []) or []:
                if is_team_race and not res.get("IsTeam"):
                    continue
                if not is_team_race and res.get("IsTeam"):
                    continue
                rank_val = _parse_rank(res.get("Rank") or res.get("SO"))
                if rank_val is None:
                    continue
                points = _get_wc_points(rank_val, mass_start=race_disc == "MS")
                if points <= 0:
                    continue
                nat = str(res.get("Nat") or "").upper()
                if not nat:
                    continue
                if race_cat == cat:
                    points_by_nat[nat] = points_by_nat.get(nat, 0.0) + points
                elif race_cat == "MX":
                    points_by_nat[nat] = points_by_nat.get(nat, 0.0) + points / 2.0

    sorted_rows = sorted(points_by_nat.items(), key=lambda item: (-item[1], item[0]))
    out: list[dict] = []
    for idx, (nat, score) in enumerate(sorted_rows[:limit], start=1):
        score_text = str(int(score)) if float(score).is_integer() else f"{score:.1f}"
        out.append(
            {
                "Rank": idx,
                "Name": _country_display(nat),
                "Nat": nat,
                "Score": score_text,
            }
        )
    return out


def _get_wc_points(position: int, mass_start: bool = False) -> int:
    """Look up World Cup points for a finish position."""
    if mass_start:
        return WC_POINTS_MS.get(position, 0)
    return WC_POINTS.get(position, 0)


def _season_id_from_race_id(race_id: str) -> str:
    match = RACE_SEASON_RE.match(str(race_id or "").strip().upper())
    if not match:
        return ""
    return str(match.group("season") or "")


def _normalize_season_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) == 4 and text.isdigit():
        return text
    match = SEASON_TEXT_RE.match(text)
    if match:
        return f"{match.group('s1')}{match.group('s2')}"
    return ""


def _season_id_from_result(result: dict) -> str:
    season_id = _normalize_season_id(result.get("SeasonId"))
    if season_id:
        return season_id
    season = _normalize_season_id(result.get("Season"))
    if season:
        return season
    race_id = str(result.get("RaceId") or "")
    if race_id:
        return _season_id_from_race_id(race_id)
    return ""


def _season_sort_key(season_id: str) -> int | None:
    text = _normalize_season_id(season_id)
    if len(text) != 4 or not text.isdigit():
        return None
    start_yy = int(text[:2])
    century = 1900 if start_yy >= 90 else 2000
    return century + start_yy


def _start_dt_from_competition(comp: dict | None) -> datetime.datetime | None:
    if not isinstance(comp, dict):
        return None
    for key in ("StartTime", "StartDate", "Date"):
        raw = comp.get(key)
        if raw:
            dt = parse_start_datetime(str(raw))
            if dt is not None:
                return dt
    return None


def _start_dt_from_race_row(race: dict | None) -> datetime.datetime | None:
    if not isinstance(race, dict):
        return None
    for key in ("StartTime", "StartDate", "Date", "FirstStart"):
        raw = race.get(key)
        if raw:
            dt = parse_start_datetime(str(raw))
            if dt is not None:
                return dt
    return None


def _resolve_result_start_datetime(
    result: dict,
    race_start_cache: dict[str, datetime.datetime | None],
) -> datetime.datetime | None:
    for key in ("StartTime", "StartDate", "Date", "RaceDate"):
        raw = result.get(key)
        if raw:
            dt = parse_start_datetime(str(raw))
            if dt is not None:
                return dt
    race_id = str(result.get("RaceId") or "")
    if not race_id:
        return None
    if race_id in race_start_cache:
        return race_start_cache[race_id]
    try:
        payload = get_race_results(race_id)
    except BiathlonError:
        race_start_cache[race_id] = None
        return None
    start_dt = _start_dt_from_competition(payload.get("Competition") or {})
    race_start_cache[race_id] = start_dt
    return start_dt


def _is_result_before_cutoff(
    result: dict,
    target_race_id: str,
    cutoff_dt: datetime.datetime | None,
    race_start_cache: dict[str, datetime.datetime | None],
    target_season_key: int | None,
) -> bool:
    race_id = str(result.get("RaceId") or "")
    if race_id and race_id == target_race_id:
        return False
    if cutoff_dt is None:
        return True

    # Fast path for rows from clearly older/newer seasons.
    result_season_key = _season_sort_key(_season_id_from_result(result))
    if target_season_key is not None and result_season_key is not None:
        if result_season_key < target_season_key:
            return True
        if result_season_key > target_season_key:
            return False

    start_dt = _resolve_result_start_datetime(result, race_start_cache)
    if start_dt is None:
        return False
    return start_dt < cutoff_dt


def _filter_results_before_cutoff(
    rows: list[dict],
    target_race_id: str,
    cutoff_dt: datetime.datetime | None,
    race_start_cache: dict[str, datetime.datetime | None],
) -> list[dict]:
    target_season_key = _season_sort_key(_season_id_from_race_id(target_race_id))
    return [
        row
        for row in rows
        if _is_result_before_cutoff(
            row,
            target_race_id,
            cutoff_dt,
            race_start_cache,
            target_season_key,
        )
    ]


def _discipline_cup_key(discipline: str) -> str:
    disc = str(discipline or "").upper()
    return "IN" if disc == "SI" else disc


def _is_same_discipline_cup(race_discipline: str, target_discipline: str) -> bool:
    return _discipline_cup_key(race_discipline) == _discipline_cup_key(
        target_discipline
    )


def _race_meta_sort_key(
    entry: tuple[datetime.datetime | None, str, str],
) -> tuple[bool, datetime.datetime, str]:
    start_dt, race_id, _disc = entry
    fallback_dt = datetime.datetime.max.replace(tzinfo=datetime.timezone.utc)
    return (start_dt is None, start_dt or fallback_dt, race_id)


def _collect_wc_individual_races(
    season_id: str,
    cat_id: str,
) -> list[tuple[datetime.datetime | None, str, str]]:
    try:
        events = get_events(season_id, level=1)
    except BiathlonError:
        return []
    races_out: list[tuple[datetime.datetime | None, str, str]] = []
    for event in events:
        event_type = detect_event_type(event)
        event_id = str(event.get("EventId") or "")
        if not event_id:
            continue
        try:
            races = get_races(event_id)
        except BiathlonError:
            continue
        for race in races:
            race_id = str(race.get("RaceId") or race.get("Id") or "")
            if not race_id:
                continue
            race_cat = str(race.get("catId") or race.get("CatId") or "").upper()
            if cat_id and race_cat != cat_id:
                continue
            race_disc = str(race.get("DisciplineId") or "").upper()
            if race_disc not in DISCIPLINES:
                continue
            if not counts_toward_wc_standings(
                event_type,
                season_id,
                discipline=race_disc,
                category=race_cat,
            ):
                continue
            races_out.append((_start_dt_from_race_row(race), race_id, race_disc))

    races_out.sort(key=_race_meta_sort_key)
    return races_out


def _build_race_points_and_info(
    payload: dict,
    discipline: str,
) -> tuple[dict[str, int], dict[str, tuple[str, str]]]:
    points_by_id: dict[str, int] = {}
    athlete_info: dict[str, tuple[str, str]] = {}
    is_mass_start = str(discipline or "").upper() == "MS"
    for res in payload.get("Results") or []:
        if res.get("IsTeam"):
            continue
        ibu_id = str(res.get("IBUId") or "")
        if not ibu_id:
            continue
        rank_val = _parse_rank(res.get("Rank") or res.get("ResultOrder"))
        if rank_val is None:
            continue
        pts = _get_wc_points(rank_val, mass_start=is_mass_start)
        if pts <= 0:
            continue
        points_by_id[ibu_id] = pts
        athlete_info[ibu_id] = (
            str(res.get("Name") or res.get("ShortName") or ""),
            str(res.get("Nat") or ""),
        )
    return points_by_id, athlete_info


def _merge_points(total: dict[str, int], race_points: dict[str, int]) -> None:
    for ibu_id, points in race_points.items():
        total[ibu_id] = total.get(ibu_id, 0) + points


def _rank_points(points_by_id: dict[str, int]) -> dict[str, int]:
    ranked = sorted(points_by_id.items(), key=lambda item: (-item[1], item[0]))
    return {ibu_id: idx for idx, (ibu_id, _pts) in enumerate(ranked, 1)}


def _rows_from_points(
    points_by_id: dict[str, int],
    athlete_info: dict[str, tuple[str, str]],
) -> list[dict]:
    rank_map = _rank_points(points_by_id)
    ranked_ids = sorted(points_by_id, key=lambda ibu_id: rank_map[ibu_id])
    rows: list[dict] = []
    for ibu_id in ranked_ids:
        name, nat = athlete_info.get(ibu_id, ("", ""))
        rows.append(
            {
                "Rank": rank_map[ibu_id],
                "Name": name,
                "Nat": nat,
                "IBUId": ibu_id,
                "Score": points_by_id[ibu_id],
            }
        )
    return rows


def _compute_wc_pre_race_standings(
    season_id: str,
    cat_id: str,
    target_race_id: str,
    target_discipline: str,
    cutoff_dt: datetime.datetime | None,
) -> tuple[list[dict], list[dict]]:
    if cutoff_dt is None:
        return [], []
    races = _collect_wc_individual_races(season_id, cat_id)
    total_points: dict[str, int] = {}
    disc_points: dict[str, int] = {}
    athlete_info: dict[str, tuple[str, str]] = {}

    for start_dt, race_id, race_disc in races:
        if race_id == target_race_id:
            continue
        if start_dt is None or start_dt >= cutoff_dt:
            continue
        try:
            payload = get_race_results(race_id)
        except BiathlonError:
            continue
        if not _has_completed_results(payload):
            continue
        race_points, race_info = _build_race_points_and_info(payload, race_disc)
        _merge_points(total_points, race_points)
        if _is_same_discipline_cup(race_disc, target_discipline):
            _merge_points(disc_points, race_points)
        for ibu_id, info in race_info.items():
            athlete_info.setdefault(ibu_id, info)

    return _rows_from_points(total_points, athlete_info), _rows_from_points(
        disc_points, athlete_info
    )


def _compute_what_if_scenarios(
    total_standings: list[dict],
    disc_standings: list[dict],
    startlist_ids: set[str],
    discipline: str,
    name_formatter: Callable[[str, str], str] | None = None,
) -> list[str]:
    """Generate standing change scenarios for startlist athletes who could take the lead.

    Returns a list of scenario strings describing potential standing changes.
    When both #1 and #2 are racing, calculates overtake scenarios.
    When #1 or #2 is missing, explains why and shows alternative scenarios.
    """
    scenarios = []
    is_mass_start = discipline == "Mass Start"
    max_pos = 31 if is_mass_start else 41

    def _ibu_id(row: dict) -> str:
        return row.get("IBUId") or row.get("IbuId") or ""

    def _points(row: dict) -> int:
        return int(row.get("Score") or row.get("Points") or 0)

    def _name(row: dict) -> str:
        base_name = row.get("Name") or row.get("ShortName") or ""
        ibu_id = _ibu_id(row)
        return name_formatter(base_name, ibu_id) if name_formatter else base_name

    def _rank(row: dict) -> int:
        rank_val = row.get("Rank") or row.get("Standing") or 0
        return int(str(rank_val).rstrip(".")) if rank_val else 0

    def _find_position_for_points(target_pts: int) -> int | None:
        """Find the finishing position that gives exactly target_pts or less."""
        for pos in range(1, max_pos):
            if _get_wc_points(pos, is_mass_start) <= target_pts:
                return pos
        return None

    def _compute_overtake_scenarios(
        leader: dict, chaser: dict, label: str, other_contenders: list[dict]
    ) -> list[str]:
        """Compute scenarios where chaser can GUARANTEED overtake leader.

        A scenario is only shown if no other racing athlete could take 1st instead.
        Returns (scenarios, gap_info) where gap_info is shown if no guaranteed scenario exists.
        """
        result = []
        gap = leader["points"] - chaser["points"]

        for finish_pos in range(1, max_pos):
            chaser_pts = _get_wc_points(finish_pos, is_mass_start)
            chaser_new_total = chaser["points"] + chaser_pts
            max_leader_pts = chaser_pts - gap - 1
            if max_leader_pts < 0:
                continue

            leader_must_finish = _find_position_for_points(max_leader_pts)

            # Check if other contenders could interfere
            # If chaser wins (finish_pos=1), others get at most 75 pts (2nd place)
            # If chaser doesn't win, others could get 90 pts (1st place)
            max_other_pts = 75 if finish_pos == 1 else 90
            interference = False
            for other in other_contenders:
                other_best_total = other["points"] + max_other_pts
                if other_best_total >= chaser_new_total:
                    # This other athlete could potentially beat or tie chaser
                    interference = True
                    break

            if interference:
                # Skip this scenario - not guaranteed
                continue

            if finish_pos == 1:
                chaser_finish = "with a win"
            else:
                chaser_finish = f"with a {_ordinal(finish_pos)}-place finish"

            prefix = f"[{label}] "
            scoring_limit = max_pos - 1
            if leader_must_finish is None:
                result.append(
                    f"{prefix}{chaser['name']} takes {_ordinal(leader['rank'])} from "
                    f"{leader['name']} {chaser_finish} if {leader['name']} finishes outside top {scoring_limit}"
                )
            else:
                result.append(
                    f"{prefix}{chaser['name']} takes {_ordinal(leader['rank'])} from "
                    f"{leader['name']} {chaser_finish} if {leader['name']} finishes "
                    f"{_ordinal(leader_must_finish)} or worse"
                )
        return result

    def _compute_gap_info(leader: dict, chaser: dict, label: str) -> str:
        """Compute gap info when chaser cannot guarantee taking 1st."""
        gap = leader["points"] - chaser["points"]
        # Best case: chaser wins (90 pts), leader finishes outside top 40 (0 pts)
        best_case_gap = gap - 90
        prefix = f"[{label}] "
        if best_case_gap <= 0:
            return (
                f"{prefix}{chaser['name']} is {gap} pts behind {leader['name']}. "
                f"Best case: takes 1st (needs to outscore by {gap + 1}+ pts, others permitting)"
            )
        else:
            return (
                f"{prefix}{chaser['name']} is {gap} pts behind {leader['name']}. "
                f"Best case: gap reduces to {best_case_gap} pts"
            )

    # Process both total and discipline standings
    for standings, label in [(total_standings, "Total"), (disc_standings, discipline)]:
        if not standings:
            continue

        # Build ranked list of athletes
        ranked: list[dict[str, Any]] = []
        for row in standings:
            rank = _rank(row)
            if rank > 0:
                ranked.append(
                    {
                        "ibu_id": _ibu_id(row),
                        "name": _name(row),
                        "points": _points(row),
                        "rank": rank,
                    }
                )
        ranked.sort(key=lambda x: x["rank"])

        if len(ranked) < 2:
            continue

        leader = ranked[0] if ranked[0]["rank"] == 1 else None
        chaser = ranked[1] if len(ranked) > 1 and ranked[1]["rank"] == 2 else None

        if not leader:
            continue

        leader_racing = leader["ibu_id"] in startlist_ids
        chaser_racing = chaser["ibu_id"] in startlist_ids if chaser else False

        # Build list of other contenders (racing athletes who are neither leader nor chaser)
        def _get_other_contenders(exclude_ids: set[str]) -> list[dict]:
            return [
                a
                for a in ranked
                if a["ibu_id"] in startlist_ids and a["ibu_id"] not in exclude_ids
            ]

        # Case 1: Both racing - show direct scenarios
        if leader_racing and chaser_racing and chaser:
            other_contenders = _get_other_contenders(
                {leader["ibu_id"], chaser["ibu_id"]}
            )
            overtake_scenarios = _compute_overtake_scenarios(
                leader, chaser, label, other_contenders
            )
            if overtake_scenarios:
                scenarios.extend(overtake_scenarios)
            else:
                # No guaranteed scenarios - show gap info
                scenarios.append(_compute_gap_info(leader, chaser, label))
            continue

        # Case 2: Leader or chaser not racing - explain and find alternative
        prefix = f"[{label}] "
        missing_explanations = []

        if not leader_racing:
            missing_explanations.append(f"{leader['name']} (#1) not racing")
        if chaser and not chaser_racing:
            missing_explanations.append(f"{chaser['name']} (#2) not racing")

        if missing_explanations:
            scenarios.append(prefix + "; ".join(missing_explanations))

        # Find highest-ranked chaser who IS racing (skip #1 if not racing)
        alt_chaser = None
        for athlete in ranked[1:]:  # Skip leader
            if athlete["ibu_id"] in startlist_ids:
                alt_chaser = athlete
                break

        if leader_racing and alt_chaser and alt_chaser["rank"] > 2:
            # Leader racing, original #2 not racing, found alternative chaser
            other_contenders = _get_other_contenders(
                {leader["ibu_id"], alt_chaser["ibu_id"]}
            )
            alt_scenarios = _compute_overtake_scenarios(
                leader, alt_chaser, label, other_contenders
            )
            if alt_scenarios:
                scenarios.append(
                    f"{prefix}Alternative: {alt_chaser['name']} (#{alt_chaser['rank']}) vs leader:"
                )
                scenarios.extend(alt_scenarios)
            else:
                # No guaranteed scenarios - show gap info
                scenarios.append(
                    f"{prefix}Alternative: {alt_chaser['name']} (#{alt_chaser['rank']}) vs leader:"
                )
                scenarios.append(_compute_gap_info(leader, alt_chaser, label))

    return scenarios


def _render_standings_section(
    title: str,
    standings: list[dict],
    args: argparse.Namespace,
    startlist_ids: set[str] | None = None,
    name_formatter: Callable[[str, str], str] | None = None,
) -> None:
    """Render a standings table."""
    if not standings:
        _print_spaced_section_title(f"{title}: no data available", args)
        return

    rows = []
    missing_rows: set[int] = set()
    for idx, row in enumerate(standings):
        rank = row.get("Rank") or row.get("Standing") or idx + 1
        name = row.get("Name") or row.get("ShortName") or ""
        nat = row.get("Nat") or ""
        points = row.get("Score") or row.get("Points") or 0
        ibu_id = row.get("IBUId") or row.get("IbuId") or ""
        if name_formatter:
            name = name_formatter(name, str(ibu_id))
        if startlist_ids and ibu_id not in startlist_ids:
            missing_rows.add(idx)
        rows.append([str(rank).rstrip("."), name, nat, str(points)])

    def row_dimmer(cell_str: str, row_idx: int) -> str:
        return Color.dim(cell_str) if row_idx in missing_rows else cell_str

    def name_cell(cell_str: str, row_idx: int) -> str:
        return _format_leader_markers(cell_str, row_idx, row_dimmer)

    _print_spaced_section_title(title, args)
    render_table(
        ["Rank", "Athlete", "Nat", "Points"],
        rows,
        output_format=get_output_format(args),
        cell_formatters=[row_dimmer, name_cell, row_dimmer, row_dimmer],
        column_separators={3},
    )
    print()


def _render_wc_standings_sections(
    ctx: dict,
    args: argparse.Namespace,
    total_standings: list[dict],
    disc_standings: list[dict],
    wc_rows_for_missing: list[dict] | None,
    disc_name: str,
    format_leader_name: Callable[[str, str], str],
    leader_name_cell: Callable[[str, int], str],
) -> None:
    """Render World Cup standings sections (1-3)."""
    race_disc = ctx["race_disc"]
    cat_id = ctx["cat_id"]
    season_id = ctx["season_id"]
    startlist_ids = ctx["startlist_ids"]
    age_cache = ctx["age_cache"]
    is_mixed = ctx.get("is_mixed", False)

    # Section 1: Missing from top 25 World Cup standings (individual races only)
    if race_disc in DISCIPLINES and not is_mixed and cat_id in {"SW", "SM"}:
        missing_rows = []
        wc_rows = (
            wc_rows_for_missing
            if wc_rows_for_missing is not None
            else _get_wc_rows(cat_id, season_id)
        )
        top_rows = wc_rows[:25]
        missing = [row for row in top_rows if _row_ibu_id(row) not in startlist_ids]
        if missing:
            for row in missing:
                name = row.get("Name") or row.get("ShortName") or ""
                nat = row.get("Nat") or ""
                rank = row.get("Rank") or ""
                ibu_id = _row_ibu_id(row)
                name = format_leader_name(name, ibu_id)
                age_val = _age_for_ibu(ibu_id, age_cache) if ibu_id else "-"
                missing_rows.append([rank, name, age_val, nat])

        if missing_rows:
            print(
                _format_section_title(
                    "1. Missing from top 25 World Cup standings:", args
                )
            )
            render_table(
                ["Rank", "Name", "Age", "Nat"],
                missing_rows,
                output_format=get_output_format(args),
                cell_formatters=[None, leader_name_cell, None, None],
                column_separators={2},
            )
            print()
        else:
            print(
                _format_section_title(
                    "1. Missing from top 25 World Cup standings: none", args
                )
            )
            print()

    # World Cup standings sections (only for individual races, not relays)
    if race_disc in DISCIPLINES and cat_id in {"SW", "SM"}:
        # Section 2: World Cup Total Standings (Top 10)
        _render_standings_section(
            "2. World Cup Total Standings (Top 10):",
            total_standings[:10],
            args,
            startlist_ids,
            name_formatter=format_leader_name,
        )

        # Section 3: Discipline World Cup Standings (Top 10)
        _render_standings_section(
            f"3. {disc_name} World Cup Standings (Top 10):",
            disc_standings[:10],
            args,
            startlist_ids,
            name_formatter=format_leader_name,
        )


def _find_all_startlist_races() -> list[tuple[str, dict]]:
    """Find all races with startlists available.

    Returns list of (race_id, payload) tuples sorted by start time (chronological).
    """
    season_id = get_current_season_id()
    events = get_events(season_id, level=1)
    today = datetime.date.today()

    # Collect active event IDs (skip completed events)
    active_event_ids: list[str] = []
    for event in events:
        event_id = event.get("EventId")
        if not event_id:
            continue
        end_raw = event.get("EndDate") or event.get("StartDate") or ""
        if end_raw:
            end_str = end_raw.split("T", 1)[0] if isinstance(end_raw, str) else ""
            if end_str:
                try:
                    end_date = datetime.date.fromisoformat(end_str)
                    if end_date < today:
                        continue
                except ValueError:
                    pass
        active_event_ids.append(event_id)

    if not active_event_ids:
        raise BiathlonError("No World Cup races with startlists found")

    # Fetch races for all events in parallel
    all_races: list[dict] = []
    with ThreadPoolExecutor(
        max_workers=_max_workers(len(active_event_ids))
    ) as executor:
        futures = {executor.submit(get_races, eid): eid for eid in active_event_ids}
        for future in as_completed(futures):
            try:
                all_races.extend(future.result())
            except BiathlonError:
                continue

    # Collect race IDs
    race_ids: list[str] = [
        rid for r in all_races if (rid := r.get("RaceId") or r.get("Id"))
    ]

    if not race_ids:
        raise BiathlonError("No World Cup races with startlists found")

    # Fetch race results in parallel to check for startlists
    race_payloads: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=_max_workers(len(race_ids))) as executor:
        result_futures = {
            executor.submit(get_race_results, rid): rid for rid in race_ids
        }
        for res_f in as_completed(result_futures):
            rid = result_futures[res_f]
            try:
                race_payloads[rid] = res_f.result()
            except BiathlonError:
                continue

    # Filter to startlists and sort
    races: list[tuple[datetime.datetime | None, str, dict]] = []
    for rid, payload in race_payloads.items():
        if not _is_true(payload.get("IsStartList")):
            continue
        comp = payload.get("Competition") or {}
        start_raw = comp.get("StartTime") or comp.get("StartDate")
        start_dt = parse_start_datetime(
            start_raw if isinstance(start_raw, str) else None
        )
        races.append((start_dt, rid, payload))

    if not races:
        raise BiathlonError("No World Cup races with startlists found")

    # Sort by start datetime (chronological); unknown dates last
    races.sort(key=lambda entry: (entry[0] is None, entry[0]))

    return [(rid, p) for _, rid, p in races]


def _build_startlist_entries(payload: dict) -> list[dict]:
    entries = []
    seen: set[str] = set()
    for res in payload.get("Results", []) or []:
        if res.get("IsTeam"):
            continue
        ibu_id = res.get("IBUId") or res.get("IbuId") or ""
        name = res.get("Name") or res.get("ShortName") or ""
        nat = res.get("Nat") or ""
        if not ibu_id and not name:
            continue
        # Deduplicate by IBU ID (or by name+nat fallback for relay races)
        key = str(ibu_id) if ibu_id else f"{name}|{nat}"
        if key in seen:
            continue
        seen.add(key)
        entries.append({"ibu_id": str(ibu_id), "name": name, "nat": nat})
    return entries


def _get_startlist_family_names(payload: dict) -> set[str]:
    """Get family names of athletes from the startlist (individual entries only)."""
    athletes: set[str] = set()
    for res in payload.get("Results", []) or []:
        if res.get("IsTeam"):
            continue
        family_name = res.get("FamilyName") or ""
        if not family_name:
            name = res.get("Name") or res.get("ShortName") or ""
            family_name = name.split()[0] if name else ""
        if family_name:
            athletes.add(family_name)
    return athletes


def _build_team_entries(payload: dict) -> list[dict]:
    """Build team entries from a relay startlist (for provisional startlists with only teams)."""
    entries = []
    for res in payload.get("Results", []) or []:
        if not res.get("IsTeam"):
            continue
        ibu_id = res.get("IBUId") or res.get("IbuId") or ""
        name = res.get("Name") or res.get("ShortName") or ""
        nat = res.get("Nat") or ""
        bib = res.get("Bib") or ""
        start_row = res.get("StartRow") or ""
        if not name:
            continue
        entries.append(
            {
                "ibu_id": str(ibu_id),
                "name": name,
                "nat": nat,
                "bib": str(bib),
                "start_row": str(start_row),
            }
        )
    # Sort by bib number
    entries.sort(key=lambda e: int(e["bib"]) if e["bib"].isdigit() else 999)
    return entries


def _build_team_rosters(payload: dict) -> dict[str, list[str]]:
    """Build athlete name lists keyed by relay team bib or nation."""
    rosters: dict[str, list[tuple[int | None, str]]] = {}
    for res in payload.get("Results", []) or []:
        if res.get("IsTeam"):
            continue
        name = res.get("Name") or res.get("ShortName") or res.get("FamilyName") or ""
        if not name:
            continue
        bib = str(res.get("Bib") or "")
        nat = str(res.get("Nat") or "")
        key = f"bib:{bib}" if bib else (f"nat:{nat}" if nat else "")
        if not key:
            continue
        leg = _parse_leg(res.get("Leg"))
        rosters.setdefault(key, []).append((leg, name))

    out: dict[str, list[str]] = {}
    for key, values in rosters.items():
        values.sort(key=lambda item: (item[0] is None, item[0] or 0, item[1]))
        out[key] = [name for _, name in values]
    return out


def _season_to_olympic_year(season_id: str) -> str:
    """Convert season ID (e.g., '2122') to Olympic year (e.g., '2022')."""
    if len(season_id) != 4:
        return season_id
    first_part = season_id[0:2]
    second_part = season_id[2:4]
    if not first_part.isdigit() or not second_part.isdigit():
        return season_id
    try:
        start_suffix = int(first_part)
        year_suffix = int(second_part)
        # Biathlon season ids are two-digit year pairs (e.g. 93/94 -> "9394").
        # Seasons starting in 80-99 map to 19xx; 00-79 map to 20xx.
        # Handle century-crossing seasons like 99/00 by rolling to the next century.
        base_century = 1900 if start_suffix >= 80 else 2000
        if start_suffix > year_suffix:
            base_century += 100
        return str(base_century + year_suffix)
    except ValueError:
        return season_id


def _fetch_olympic_individual_podium(
    season_id: str,
    discipline: str,
    category: str,
    cutoff_dt: datetime.datetime | None = None,
    include_cutoff: bool = False,
) -> dict | None:
    """Fetch podium for a single Olympic individual race. Returns None if not found."""
    event_id = f"BT{season_id}SWRLOG__"
    try:
        races = get_races(event_id)
    except BiathlonError:
        return None
    candidates: list[tuple[datetime.datetime | None, str]] = []
    for race in races:
        race_disc = str(race.get("DisciplineId") or "").upper()
        race_cat = str(race.get("catId") or race.get("CatId") or "").upper()
        if race_disc == discipline and race_cat == category:
            race_id = str(race.get("RaceId") or "")
            if not race_id:
                continue
            start_dt = parse_start_datetime(
                str(race.get("StartTime") or race.get("StartDate") or "")
            )
            if cutoff_dt is not None:
                if start_dt is None:
                    continue
                if include_cutoff:
                    if start_dt > cutoff_dt:
                        continue
                elif start_dt >= cutoff_dt:
                    continue
            candidates.append((start_dt, race_id))
    if not candidates:
        return None
    fallback_dt = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
    candidates.sort(key=lambda item: (item[0] or fallback_dt, item[1]), reverse=True)
    race_id = candidates[0][1]
    if not race_id:
        return None
    try:
        payload = get_race_results(race_id)
    except BiathlonError:
        return None
    if not payload.get("IsResult"):
        return None

    medalists: dict[int, dict[str, str]] = {}
    for res in payload.get("Results", []) or []:
        if res.get("IsTeam"):
            continue
        rank_val = _parse_rank(
            res.get("Rank") or res.get("SO") or res.get("ResultOrder")
        )
        if rank_val not in (1, 2, 3):
            continue
        if rank_val in medalists:
            continue
        full_name = res.get("Name") or res.get("ShortName") or ""
        family_name = res.get("FamilyName") or ""
        if not family_name and full_name:
            family_name = full_name.split()[0]
        nat = str(res.get("Nat") or "")
        medalists[rank_val] = {
            "full_name": full_name or family_name,
            "family_name": family_name or full_name,
            "nat": nat,
        }

    if 1 not in medalists:
        return None

    def display_name(info: dict[str, str]) -> str:
        name = info.get("full_name") or info.get("family_name") or ""
        nat = info.get("nat") or ""
        return f"{name} ({nat})" if nat and nat not in name else name

    gender = "F" if category == "SW" else "M"

    def athlete_entry(info: dict[str, str]) -> dict[str, str]:
        return {
            "name": info.get("family_name") or info.get("full_name") or "",
            "full_name": info.get("full_name") or info.get("family_name") or "",
            "nat": info.get("nat") or "",
            "gender": gender,
        }

    gold_info = medalists.get(1)
    silver_info = medalists.get(2)
    bronze_info = medalists.get(3)

    venue = (payload.get("SportEvt") or {}).get("Organizer") or ""
    year = _season_to_olympic_year(season_id)
    return {
        "year": year,
        "venue": venue,
        "gold": display_name(gold_info) if gold_info else "",
        "silver": display_name(silver_info) if silver_info else "",
        "bronze": display_name(bronze_info) if bronze_info else "",
        "gold_athletes": [athlete_entry(gold_info)] if gold_info else [],
        "silver_athletes": [athlete_entry(silver_info)] if silver_info else [],
        "bronze_athletes": [athlete_entry(bronze_info)] if bronze_info else [],
        "gold_nat": gold_info.get("nat", "") if gold_info else "",
        "silver_nat": silver_info.get("nat", "") if silver_info else "",
        "bronze_nat": bronze_info.get("nat", "") if bronze_info else "",
    }


def _fetch_olympic_podium(
    season_id: str,
    discipline: str,
    category: str,
    cutoff_dt: datetime.datetime | None = None,
    include_cutoff: bool = False,
) -> dict | None:
    """Fetch podium for a single Olympic relay race. Returns None if not found."""
    event_id = f"BT{season_id}SWRLOG__"
    try:
        races = get_races(event_id)
    except BiathlonError:
        return None
    # Find the matching relay race
    candidates: list[tuple[datetime.datetime | None, str]] = []
    for race in races:
        race_disc = str(race.get("DisciplineId") or "").upper()
        race_cat = str(race.get("catId") or race.get("CatId") or "").upper()
        if race_disc == discipline and race_cat == category:
            race_id = str(race.get("RaceId") or "")
            if not race_id:
                continue
            start_dt = parse_start_datetime(
                str(race.get("StartTime") or race.get("StartDate") or "")
            )
            if cutoff_dt is not None:
                if start_dt is None:
                    continue
                if include_cutoff:
                    if start_dt > cutoff_dt:
                        continue
                elif start_dt >= cutoff_dt:
                    continue
            candidates.append((start_dt, race_id))
    if not candidates:
        return None
    fallback_dt = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
    candidates.sort(key=lambda item: (item[0] or fallback_dt, item[1]), reverse=True)
    race_id = candidates[0][1]
    if not race_id:
        return None
    try:
        payload = get_race_results(race_id)
    except BiathlonError:
        return None
    if not payload.get("IsResult"):
        return None
    # Extract podium teams and their nations
    gold, silver, bronze = "", "", ""
    gold_nat, silver_nat, bronze_nat = "", "", ""
    for res in payload.get("Results", []) or []:
        if not res.get("IsTeam"):
            continue
        rank_str = str(res.get("Rank") or "").rstrip(".")
        if not rank_str.isdigit():
            continue
        rank = int(rank_str)
        name = res.get("Name") or res.get("ShortName") or ""
        nat = res.get("Nat") or ""
        base = name or _country_display(nat)
        if nat and nat not in base:
            display = f"{base} ({nat})"
        else:
            display = base
        if rank == 1:
            gold = display
            gold_nat = nat
        elif rank == 2:
            silver = display
            silver_nat = nat
        elif rank == 3:
            bronze = display
            bronze_nat = nat
    if not gold:
        return None

    # Extract athlete names for each podium team
    def get_team_athletes(nation: str) -> list[dict]:
        """Get sorted list of athlete info dicts for athletes from a nation."""
        athletes = []
        for res in payload.get("Results", []) or []:
            if res.get("IsTeam"):
                continue
            if res.get("Nat") == nation:
                leg = res.get("Leg") or 0
                family_name = res.get("FamilyName") or ""
                full_name = res.get("Name") or res.get("ShortName") or family_name
                nat = res.get("Nat") or ""
                # Determine gender based on category and leg
                # Mixed relay (MX): legs 1-2 are women, legs 3-4 are men
                # Single mixed relay (SR): leg 1 is woman, leg 2 is man
                # Regular relay: use category (SW=women, SM=men)
                if category in ("MX", "MXRL"):
                    gender = "F" if leg <= 2 else "M"
                elif discipline == "SR" or category == "SR":
                    gender = "F" if leg == 1 else "M"
                else:
                    gender = "F" if category.startswith("SW") else "M"
                if family_name:
                    athletes.append(
                        {
                            "leg": leg,
                            "name": family_name,
                            "full_name": full_name,
                            "nat": nat,
                            "gender": gender,
                        }
                    )
        # Sort by leg number
        athletes.sort(key=lambda x: x["leg"])
        return athletes

    gold_athletes = get_team_athletes(gold_nat) if gold_nat else []
    silver_athletes = get_team_athletes(silver_nat) if silver_nat else []
    bronze_athletes = get_team_athletes(bronze_nat) if bronze_nat else []

    venue = (payload.get("SportEvt") or {}).get("Organizer") or ""
    country_raw = str(
        (payload.get("SportEvt") or {}).get("CountryId")
        or (payload.get("SportEvt") or {}).get("Country")
        or (payload.get("Competition") or {}).get("CountryId")
        or (payload.get("Competition") or {}).get("Country")
        or ""
    ).strip()
    country = _country_display(country_raw) if country_raw else ""
    if not country:
        country = _event_country_display(event_id, season_id)
    year = _season_to_olympic_year(season_id)
    return {
        "year": year,
        "venue": venue,
        "country": country,
        "gold": gold,
        "silver": silver,
        "bronze": bronze,
        "gold_athletes": gold_athletes,
        "silver_athletes": silver_athletes,
        "bronze_athletes": bronze_athletes,
        "gold_nat": gold_nat,
        "silver_nat": silver_nat,
        "bronze_nat": bronze_nat,
    }


def _get_past_olympic_relay_podiums(
    discipline: str,
    category: str,
    cutoff_dt: datetime.datetime | None = None,
    include_cutoff: bool = False,
) -> list[dict]:
    """Fetch podiums from past Olympic relay races.

    Returns list of dicts with keys: year, venue, gold, silver, bronze.
    """
    podiums: list[dict] = []
    with ThreadPoolExecutor(
        max_workers=_max_workers(len(OLYMPIC_SEASON_IDS))
    ) as executor:
        futures = {
            executor.submit(
                _fetch_olympic_podium,
                s_id,
                discipline,
                category,
                cutoff_dt,
                include_cutoff,
            ): s_id
            for s_id in OLYMPIC_SEASON_IDS
        }
        for future in as_completed(futures):
            result = future.result()
            if result:
                podiums.append(result)

    # Sort by year descending
    podiums.sort(key=lambda p: p["year"], reverse=True)
    return podiums


def _get_past_olympic_individual_podiums(
    discipline: str,
    category: str,
    cutoff_dt: datetime.datetime | None = None,
    include_cutoff: bool = False,
) -> list[dict]:
    """Fetch podiums from past Olympic individual races."""
    podiums: list[dict] = []
    with ThreadPoolExecutor(
        max_workers=_max_workers(len(OLYMPIC_SEASON_IDS))
    ) as executor:
        futures = {
            executor.submit(
                _fetch_olympic_individual_podium,
                s_id,
                discipline,
                category,
                cutoff_dt,
                include_cutoff,
            ): s_id
            for s_id in OLYMPIC_SEASON_IDS
        }
        for future in as_completed(futures):
            result = future.result()
            if result:
                podiums.append(result)

    podiums.sort(key=lambda p: p["year"], reverse=True)
    return podiums


def _fetch_olympic_season_medals(
    season_id: str,
    category: str,
    cutoff_dt: datetime.datetime | None = None,
    include_cutoff: bool = False,
) -> tuple[list[dict], dict[str, dict]]:
    """Fetch country and athlete medals for all disciplines in one Olympic season.

    Returns:
        country_medals: list of dicts with {discipline, gold, silver, bronze} (country codes)
        athlete_stats: dict mapping athlete key -> {name, nat, gender, gold, silver, bronze, races}
    """
    event_id = f"BT{season_id}SWRLOG__"
    try:
        races = get_races(event_id)
    except BiathlonError:
        return [], {}

    country_medals: list[dict] = []
    athlete_stats: dict[str, dict] = {}

    # Separate category-specific races from mixed races so we can process
    # category races first and learn which IBU IDs belong to the target
    # category (leg order in mixed relays varies between Olympics).
    cat_races: list[tuple[str, str, dict]] = []  # (disc, cat, payload)
    mixed_races: list[tuple[str, str, dict]] = []

    for race in races:
        race_disc = str(race.get("DisciplineId") or "").upper()
        race_cat = str(race.get("catId") or race.get("CatId") or "").upper()
        is_mixed = race_disc in {"MR", "SR"} or race_cat == "MX"
        if race_cat != category and not is_mixed:
            continue
        start_dt = parse_start_datetime(
            str(race.get("StartTime") or race.get("StartDate") or "")
        )
        if cutoff_dt is not None:
            if start_dt is None:
                continue
            if include_cutoff:
                if start_dt > cutoff_dt:
                    continue
            elif start_dt >= cutoff_dt:
                continue
        rid = race.get("RaceId")
        if not rid:
            continue
        try:
            payload = get_race_results(rid)
        except BiathlonError:
            continue
        if not payload.get("IsResult"):
            continue
        if is_mixed:
            mixed_races.append((race_disc, race_cat, payload))
        else:
            cat_races.append((race_disc, race_cat, payload))

    # IBU IDs seen in category-specific races (used to filter mixed relays)
    known_cat_ids: set[str] = set()
    gender = "F" if category == "SW" else "M"

    for race_disc, race_cat, payload in cat_races:
        results_list = payload.get("Results", []) or []
        is_relay = race_disc in RELAY_DISCIPLINES

        # --- Country medals ---
        gold_nat = silver_nat = bronze_nat = ""
        for res in results_list:
            if is_relay and not res.get("IsTeam"):
                continue
            if not is_relay and res.get("IsTeam"):
                continue
            rank_val = _parse_rank(res.get("Rank") or res.get("SO"))
            if rank_val is None:
                continue
            nat = str(res.get("Nat") or "")
            if rank_val == 1:
                gold_nat = nat
            elif rank_val == 2:
                silver_nat = nat
            elif rank_val == 3:
                bronze_nat = nat
        if gold_nat:
            country_medals.append(
                {
                    "discipline": race_disc,
                    "gold": gold_nat,
                    "silver": silver_nat,
                    "bronze": bronze_nat,
                }
            )

        # --- Athlete stats ---
        if is_relay:
            team_ranks: dict[str, int] = {}
            for res in results_list:
                if not res.get("IsTeam"):
                    continue
                rank_val = _parse_rank(res.get("Rank") or res.get("SO"))
                if rank_val is not None:
                    nat = str(res.get("Nat") or "")
                    if nat:
                        team_ranks[nat] = rank_val

            for res in results_list:
                if res.get("IsTeam"):
                    continue
                nat = str(res.get("Nat") or "")
                team_rank = team_ranks.get(nat)
                if team_rank is None:
                    continue
                ibu_id = _row_ibu_id(res)
                name = res.get("Name") or res.get("ShortName") or ""
                key = ibu_id or f"{name}|{nat}"
                if not key:
                    continue
                if ibu_id:
                    known_cat_ids.add(ibu_id)
                if key not in athlete_stats:
                    athlete_stats[key] = {
                        "name": name,
                        "nat": nat,
                        "gender": gender,
                        "gold": 0,
                        "silver": 0,
                        "bronze": 0,
                        "races": 0,
                        "gold_ind": 0,
                        "silver_ind": 0,
                        "bronze_ind": 0,
                        "races_ind": 0,
                        "gold_relay": 0,
                        "silver_relay": 0,
                        "bronze_relay": 0,
                        "races_relay": 0,
                    }
                athlete_stats[key]["races"] += 1
                athlete_stats[key]["races_relay"] += 1
                if team_rank == 1:
                    athlete_stats[key]["gold"] += 1
                    athlete_stats[key]["gold_relay"] += 1
                elif team_rank == 2:
                    athlete_stats[key]["silver"] += 1
                    athlete_stats[key]["silver_relay"] += 1
                elif team_rank == 3:
                    athlete_stats[key]["bronze"] += 1
                    athlete_stats[key]["bronze_relay"] += 1
        else:
            for res in results_list:
                if res.get("IsTeam"):
                    continue
                ibu_id = _row_ibu_id(res)
                name = res.get("Name") or res.get("ShortName") or ""
                nat = str(res.get("Nat") or "")
                key = ibu_id or f"{name}|{nat}"
                if not key:
                    continue
                if ibu_id:
                    known_cat_ids.add(ibu_id)
                rank_val = _parse_rank(
                    res.get("Rank") or res.get("SO") or res.get("ResultOrder")
                )
                if key not in athlete_stats:
                    athlete_stats[key] = {
                        "name": name,
                        "nat": nat,
                        "gender": gender,
                        "gold": 0,
                        "silver": 0,
                        "bronze": 0,
                        "races": 0,
                        "gold_ind": 0,
                        "silver_ind": 0,
                        "bronze_ind": 0,
                        "races_ind": 0,
                        "gold_relay": 0,
                        "silver_relay": 0,
                        "bronze_relay": 0,
                        "races_relay": 0,
                    }
                athlete_stats[key]["races"] += 1
                athlete_stats[key]["races_ind"] += 1
                if rank_val == 1:
                    athlete_stats[key]["gold"] += 1
                    athlete_stats[key]["gold_ind"] += 1
                elif rank_val == 2:
                    athlete_stats[key]["silver"] += 1
                    athlete_stats[key]["silver_ind"] += 1
                elif rank_val == 3:
                    athlete_stats[key]["bronze"] += 1
                    athlete_stats[key]["bronze_ind"] += 1

    # Resolve unknown mixed-relay athletes via CISBios gender lookup
    unknown_ids: set[str] = set()
    for _, _, payload in mixed_races:
        for res in payload.get("Results") or []:
            if res.get("IsTeam"):
                continue
            ibu_id = _row_ibu_id(res)
            if ibu_id and ibu_id not in known_cat_ids:
                unknown_ids.add(ibu_id)

    if unknown_ids:
        with ThreadPoolExecutor(max_workers=_max_workers(len(unknown_ids))) as ex:
            for ibu_id, gender_cat in ex.map(_fetch_gender, unknown_ids):
                if gender_cat == category:
                    known_cat_ids.add(ibu_id)

    # Second pass: mixed races — use known_cat_ids to identify athletes
    for race_disc, race_cat, payload in mixed_races:
        results_list = payload.get("Results", []) or []

        # --- Country medals (always counted) ---
        gold_nat = silver_nat = bronze_nat = ""
        for res in results_list:
            if not res.get("IsTeam"):
                continue
            rank_val = _parse_rank(res.get("Rank") or res.get("SO"))
            if rank_val is None:
                continue
            nat = str(res.get("Nat") or "")
            if rank_val == 1:
                gold_nat = nat
            elif rank_val == 2:
                silver_nat = nat
            elif rank_val == 3:
                bronze_nat = nat
        if gold_nat:
            country_medals.append(
                {
                    "discipline": race_disc,
                    "gold": gold_nat,
                    "silver": silver_nat,
                    "bronze": bronze_nat,
                }
            )

        # --- Athlete stats (only athletes from target category) ---
        team_ranks = {}
        for res in results_list:
            if not res.get("IsTeam"):
                continue
            rank_val = _parse_rank(res.get("Rank") or res.get("SO"))
            if rank_val is not None:
                nat = str(res.get("Nat") or "")
                if nat:
                    team_ranks[nat] = rank_val

        for res in results_list:
            if res.get("IsTeam"):
                continue
            ibu_id = _row_ibu_id(res)
            if not ibu_id or ibu_id not in known_cat_ids:
                continue
            nat = str(res.get("Nat") or "")
            team_rank = team_ranks.get(nat)
            if team_rank is None:
                continue
            name = res.get("Name") or res.get("ShortName") or ""
            key = ibu_id
            if key not in athlete_stats:
                athlete_stats[key] = {
                    "name": name,
                    "nat": nat,
                    "gender": gender,
                    "gold": 0,
                    "silver": 0,
                    "bronze": 0,
                    "races": 0,
                    "gold_ind": 0,
                    "silver_ind": 0,
                    "bronze_ind": 0,
                    "races_ind": 0,
                    "gold_relay": 0,
                    "silver_relay": 0,
                    "bronze_relay": 0,
                    "races_relay": 0,
                }
            athlete_stats[key]["races"] += 1
            athlete_stats[key]["races_relay"] += 1
            if team_rank == 1:
                athlete_stats[key]["gold"] += 1
                athlete_stats[key]["gold_relay"] += 1
            elif team_rank == 2:
                athlete_stats[key]["silver"] += 1
                athlete_stats[key]["silver_relay"] += 1
            elif team_rank == 3:
                athlete_stats[key]["bronze"] += 1
                athlete_stats[key]["bronze_relay"] += 1

    return country_medals, athlete_stats


def _get_all_olympic_medals(
    category: str,
    cutoff_dt: datetime.datetime | None = None,
    include_cutoff: bool = False,
) -> tuple[list[dict], dict[str, dict]]:
    """Fetch country and athlete medals across all Olympic seasons.

    Returns:
        country_medals: flat list of country medal dicts
        athlete_stats: merged dict of athlete stats across all seasons
    """
    all_country: list[dict] = []
    merged_athletes: dict[str, dict] = {}

    def _prefer_name(current: str, candidate: str) -> str:
        cur = str(current or "").strip()
        cand = str(candidate or "").strip()
        if not cand:
            return cur
        if not cur:
            return cand
        # Prefer longer names to avoid abbreviated variants like "J. DOE".
        if len(cand) > len(cur):
            return cand
        return cur

    def _merge(athlete_stats: dict[str, dict]) -> None:
        for key, data in athlete_stats.items():
            if key not in merged_athletes:
                merged_athletes[key] = {
                    "name": data["name"],
                    "nat": data["nat"],
                    "gender": data["gender"],
                    "gold": 0,
                    "silver": 0,
                    "bronze": 0,
                    "races": 0,
                    "gold_ind": 0,
                    "silver_ind": 0,
                    "bronze_ind": 0,
                    "races_ind": 0,
                    "gold_relay": 0,
                    "silver_relay": 0,
                    "bronze_relay": 0,
                    "races_relay": 0,
                }
            merged_athletes[key]["gold"] += data["gold"]
            merged_athletes[key]["silver"] += data["silver"]
            merged_athletes[key]["bronze"] += data["bronze"]
            merged_athletes[key]["races"] += data["races"]
            merged_athletes[key]["gold_ind"] += data.get("gold_ind", 0)
            merged_athletes[key]["silver_ind"] += data.get("silver_ind", 0)
            merged_athletes[key]["bronze_ind"] += data.get("bronze_ind", 0)
            merged_athletes[key]["races_ind"] += data.get("races_ind", 0)
            merged_athletes[key]["gold_relay"] += data.get("gold_relay", 0)
            merged_athletes[key]["silver_relay"] += data.get("silver_relay", 0)
            merged_athletes[key]["bronze_relay"] += data.get("bronze_relay", 0)
            merged_athletes[key]["races_relay"] += data.get("races_relay", 0)
            merged_athletes[key]["name"] = _prefer_name(
                merged_athletes[key]["name"], data["name"]
            )

    with ThreadPoolExecutor(
        max_workers=_max_workers(len(OLYMPIC_SEASON_IDS))
    ) as executor:
        futures = {
            executor.submit(
                _fetch_olympic_season_medals,
                s_id,
                category,
                cutoff_dt,
                include_cutoff,
            ): s_id
            for s_id in OLYMPIC_SEASON_IDS
        }
        for future in as_completed(futures):
            country_medals, athlete_stats = future.result()
            all_country.extend(country_medals)
            _merge(athlete_stats)

    return all_country, merged_athletes


def _season_ids_with_offset(season_id: str, count: int = 2) -> list[str]:
    current = _normalize_season_id(season_id)
    if not current:
        return []
    out = [current]
    prev_val = current
    while len(out) < count:
        try:
            prev_int = int(prev_val) - 101
        except ValueError:
            break
        if prev_int <= 0:
            break
        prev_val = f"{prev_int:04d}"
        out.append(prev_val)
    return out


def _extract_individual_podium_from_payload(payload: dict) -> dict | None:
    if not payload.get("IsResult"):
        return None
    medalists: dict[int, dict[str, str]] = {}
    for res in payload.get("Results", []) or []:
        if res.get("IsTeam"):
            continue
        rank_val = _parse_rank(
            res.get("Rank") or res.get("SO") or res.get("ResultOrder")
        )
        if rank_val not in (1, 2, 3) or rank_val in medalists:
            continue
        full_name = str(res.get("Name") or res.get("ShortName") or "").strip()
        family_name = str(res.get("FamilyName") or "").strip()
        if not family_name and full_name:
            family_name = full_name.split()[0]
        nat = str(res.get("Nat") or "").strip().upper()
        medalists[rank_val] = {
            "full_name": full_name or family_name,
            "family_name": family_name or full_name,
            "nat": nat,
        }
    if 1 not in medalists:
        return None

    comp = payload.get("Competition") or {}
    sport_evt = payload.get("SportEvt") or {}
    start_dt = _start_dt_from_competition(comp)
    event_type = detect_event_type(sport_evt)
    event_type_label = EVENT_TYPE_LABELS.get(
        event_type, EVENT_TYPE_LABELS.get(EVENT_TYPE_WC, "World Cup")
    )
    gender = (
        "F"
        if str(comp.get("catId") or comp.get("CatId") or "").upper() == "SW"
        else "M"
    )

    def _entry(data: dict[str, str] | None) -> dict[str, str]:
        if not data:
            return {}
        return {
            "name": data.get("family_name") or data.get("full_name") or "",
            "full_name": data.get("full_name") or data.get("family_name") or "",
            "nat": data.get("nat") or "",
            "gender": gender,
        }

    def _display(data: dict[str, str] | None) -> str:
        if not data:
            return ""
        name = data.get("full_name") or data.get("family_name") or ""
        nat = data.get("nat") or ""
        return f"{name} ({nat})" if nat and nat not in name else name

    gold = medalists.get(1)
    silver = medalists.get(2)
    bronze = medalists.get(3)
    country_raw = str(
        sport_evt.get("CountryId")
        or sport_evt.get("Country")
        or comp.get("CountryId")
        or comp.get("Country")
        or ""
    ).strip()
    country = _country_display(country_raw) if country_raw else ""
    return {
        "date": start_dt.date().isoformat() if start_dt else "",
        "year": str(start_dt.year) if start_dt else "",
        "race_type": event_type_label,
        "venue": str(
            sport_evt.get("Organizer") or sport_evt.get("ShortDescription") or ""
        ),
        "country": country,
        "gold": _display(gold),
        "silver": _display(silver),
        "bronze": _display(bronze),
        "gold_athletes": [_entry(gold)] if gold else [],
        "silver_athletes": [_entry(silver)] if silver else [],
        "bronze_athletes": [_entry(bronze)] if bronze else [],
        "gold_nat": gold.get("nat", "") if gold else "",
        "silver_nat": silver.get("nat", "") if silver else "",
        "bronze_nat": bronze.get("nat", "") if bronze else "",
    }


def _extract_relay_podium_from_payload(
    payload: dict, category: str, discipline: str
) -> dict | None:
    if not payload.get("IsResult"):
        return None

    def _team_display_name(team_name: str, nat_code: str) -> str:
        name = str(team_name or "").strip()
        nat = str(nat_code or "").strip().upper()
        if not name:
            return _country_display(nat) if nat else ""
        code_candidate = name.upper()
        if len(code_candidate) == 3 and code_candidate.isalpha():
            mapped = _country_display(code_candidate)
            if mapped and mapped != code_candidate:
                return mapped
        if nat and code_candidate == nat:
            mapped_nat = _country_display(nat)
            if mapped_nat:
                return mapped_nat
        return name

    results_list = payload.get("Results", []) or []
    team_medals: dict[int, tuple[str, str]] = {}
    for res in results_list:
        if not res.get("IsTeam"):
            continue
        rank_val = _parse_rank(
            res.get("Rank") or res.get("SO") or res.get("ResultOrder")
        )
        if rank_val not in (1, 2, 3) or rank_val in team_medals:
            continue
        name = str(res.get("Name") or res.get("ShortName") or "").strip()
        nat = str(res.get("Nat") or "").strip().upper()
        team_medals[rank_val] = (_team_display_name(name, nat), nat)
    if 1 not in team_medals:
        return None

    medal_nats = {nat for _, nat in team_medals.values() if nat}
    athletes_by_nat: dict[str, list[dict]] = {nat: [] for nat in medal_nats}
    for res in results_list:
        if res.get("IsTeam"):
            continue
        nat = str(res.get("Nat") or "").strip().upper()
        if nat not in medal_nats:
            continue
        leg = _parse_leg(res.get("Leg"))
        family_name = str(res.get("FamilyName") or "").strip()
        full_name = str(res.get("Name") or res.get("ShortName") or family_name).strip()
        if not full_name and not family_name:
            continue
        if category in ("MX", "MXRL"):
            gender = "F" if (leg or 0) <= 2 else "M"
        elif discipline == "SR" or category == "SR":
            gender = "F" if (leg or 0) == 1 else "M"
        else:
            gender = "F" if category.startswith("SW") else "M"
        athletes_by_nat.setdefault(nat, []).append(
            {
                "leg": leg,
                "name": family_name or full_name,
                "full_name": full_name or family_name,
                "nat": nat,
                "gender": gender,
            }
        )
    for nat in athletes_by_nat:
        athletes_by_nat[nat].sort(key=lambda x: (x["leg"] is None, x["leg"] or 0))

    comp = payload.get("Competition") or {}
    sport_evt = payload.get("SportEvt") or {}
    start_dt = _start_dt_from_competition(comp)
    event_type = detect_event_type(sport_evt)
    event_type_label = EVENT_TYPE_LABELS.get(
        event_type, EVENT_TYPE_LABELS.get(EVENT_TYPE_WC, "World Cup")
    )
    country_raw = str(
        sport_evt.get("CountryId")
        or sport_evt.get("Country")
        or comp.get("CountryId")
        or comp.get("Country")
        or ""
    ).strip()
    country = _country_display(country_raw) if country_raw else ""
    gold_name, gold_nat = team_medals.get(1, ("", ""))
    silver_name, silver_nat = team_medals.get(2, ("", ""))
    bronze_name, bronze_nat = team_medals.get(3, ("", ""))
    return {
        "date": start_dt.date().isoformat() if start_dt else "",
        "year": str(start_dt.year) if start_dt else "",
        "race_type": event_type_label,
        "venue": str(
            sport_evt.get("Organizer") or sport_evt.get("ShortDescription") or ""
        ),
        "country": country,
        "gold": gold_name,
        "silver": silver_name,
        "bronze": bronze_name,
        "gold_athletes": athletes_by_nat.get(gold_nat, []),
        "silver_athletes": athletes_by_nat.get(silver_nat, []),
        "bronze_athletes": athletes_by_nat.get(bronze_nat, []),
        "gold_nat": gold_nat,
        "silver_nat": silver_nat,
        "bronze_nat": bronze_nat,
    }


def _collect_podium_rows_for_seasons(
    season_ids: list[str],
    discipline: str,
    category: str,
    target_race_id: str = "",
    cutoff_dt: datetime.datetime | None = None,
    relay: bool = False,
) -> list[dict]:
    candidates: list[tuple[datetime.datetime | None, str, str, str, str]] = []
    for season in season_ids:
        try:
            events = get_events(season, level=1)
        except BiathlonError:
            continue
        for event in events:
            event_id = str(event.get("EventId") or "")
            if not event_id:
                continue
            event_type_label = EVENT_TYPE_LABELS.get(
                detect_event_type(event),
                EVENT_TYPE_LABELS.get(EVENT_TYPE_WC, "World Cup"),
            )
            try:
                races = get_races(event_id)
            except BiathlonError:
                continue
            for race in races:
                race_disc = str(race.get("DisciplineId") or "").upper()
                race_cat = str(race.get("catId") or race.get("CatId") or "").upper()
                if race_disc != discipline or race_cat != category:
                    continue
                race_id = str(race.get("RaceId") or race.get("Id") or "")
                if not race_id or race_id == target_race_id:
                    continue
                start_dt = parse_start_datetime(
                    str(race.get("StartTime") or race.get("StartDate") or "")
                )
                if cutoff_dt is not None and (start_dt is None or start_dt > cutoff_dt):
                    continue
                candidates.append(
                    (start_dt, race_id, season, event_id, event_type_label)
                )

    if not candidates:
        return []
    seen: set[str] = set()
    deduped: list[tuple[datetime.datetime | None, str, str, str, str]] = []
    for cand in candidates:
        if cand[1] in seen:
            continue
        seen.add(cand[1])
        deduped.append(cand)

    rows: list[tuple[datetime.datetime | None, dict]] = []
    with ThreadPoolExecutor(max_workers=_max_workers(len(deduped))) as executor:
        futures = {
            executor.submit(get_race_results, race_id): (start_dt, season, event_id)
            for start_dt, race_id, season, event_id, _label in deduped
        }
        labels_by_race = {
            race_id: event_label
            for _start_dt, race_id, _season, _event_id, event_label in deduped
        }
        for future in as_completed(futures):
            start_dt, season, event_id = futures[future]
            try:
                payload = future.result()
            except BiathlonError:
                continue
            row = (
                _extract_relay_podium_from_payload(payload, category, discipline)
                if relay
                else _extract_individual_podium_from_payload(payload)
            )
            if not row:
                continue
            if start_dt is not None:
                row["date"] = row.get("date") or start_dt.date().isoformat()
                row["year"] = row.get("year") or str(start_dt.year)
            row["season"] = row.get("season") or season
            race_id = str((payload.get("Competition") or {}).get("RaceId") or "")
            if not race_id:
                race_id = str((payload.get("Competition") or {}).get("Id") or "")
            if not race_id and payload.get("RaceId"):
                race_id = str(payload.get("RaceId"))
            if race_id:
                row["race_type"] = labels_by_race.get(race_id, row.get("race_type"))
            if not row.get("country"):
                row["country"] = _event_country_display(event_id, season)
            rows.append((start_dt, row))
    fallback_dt = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
    rows.sort(key=lambda item: item[0] or fallback_dt, reverse=True)
    return [row for _, row in rows]


def _get_previous_individual_podiums(
    race_id: str,
    season_id: str,
    discipline: str,
    category: str,
    cutoff_dt: datetime.datetime | None = None,
) -> list[dict]:
    seasons = _season_ids_with_offset(season_id, count=2)
    return _collect_podium_rows_for_seasons(
        seasons,
        discipline,
        category,
        target_race_id=race_id,
        cutoff_dt=cutoff_dt,
        relay=False,
    )


def _get_previous_relay_podiums(
    race_id: str,
    season_id: str,
    discipline: str,
    category: str,
    cutoff_dt: datetime.datetime | None = None,
) -> list[dict]:
    seasons = _season_ids_with_offset(season_id, count=2)
    return _collect_podium_rows_for_seasons(
        seasons,
        discipline,
        category,
        target_race_id=race_id,
        cutoff_dt=cutoff_dt,
        relay=True,
    )


def _get_recent_wch_season_ids(limit: int = 10) -> list[str]:
    seasons = get_seasons()
    candidates: list[tuple[int, str]] = []
    for season in seasons:
        season_id = _normalize_season_id(season.get("SeasonId") or season.get("Id"))
        if not season_id:
            continue
        sort_key = _season_sort_key(season_id)
        if sort_key is None:
            continue
        try:
            events = get_events(season_id, level=1)
        except BiathlonError:
            continue
        if any(detect_event_type(event) == EVENT_TYPE_WCH for event in events):
            candidates.append((sort_key, season_id))
    candidates.sort(reverse=True)
    return [season_id for _, season_id in candidates[:limit]]


def _get_past_wch_individual_podiums(
    discipline: str,
    category: str,
    n_editions: int = 10,
    cutoff_dt: datetime.datetime | None = None,
) -> list[dict]:
    seasons = _get_recent_wch_season_ids(limit=n_editions)
    return _collect_podium_rows_for_seasons(
        seasons,
        discipline,
        category,
        target_race_id="",
        cutoff_dt=cutoff_dt,
        relay=False,
    )


def _get_past_wch_relay_podiums(
    discipline: str,
    category: str,
    n_editions: int = 10,
    cutoff_dt: datetime.datetime | None = None,
) -> list[dict]:
    seasons = _get_recent_wch_season_ids(limit=n_editions)
    return _collect_podium_rows_for_seasons(
        seasons,
        discipline,
        category,
        target_race_id="",
        cutoff_dt=cutoff_dt,
        relay=True,
    )


def _fetch_wch_season_medals(
    season_id: str,
    category: str,
    cutoff_dt: datetime.datetime | None = None,
) -> tuple[list[dict], dict[str, dict]]:
    """Fetch country and athlete medals for all WCH races in one season."""
    try:
        events = get_events(season_id, level=1)
    except BiathlonError:
        return [], {}

    country_medals: list[dict] = []
    athlete_stats: dict[str, dict] = {}
    known_cat_ids: set[str] = set()
    gender = "F" if category == "SW" else "M"

    race_payloads: list[tuple[str, str, dict]] = []
    for event in events:
        if detect_event_type(event) != EVENT_TYPE_WCH:
            continue
        event_id = str(event.get("EventId") or "")
        if not event_id:
            continue
        try:
            races = get_races(event_id)
        except BiathlonError:
            continue
        for race in races:
            race_disc = str(race.get("DisciplineId") or "").upper()
            race_cat = str(race.get("catId") or race.get("CatId") or "").upper()
            is_mixed = race_disc in {"MR", "SR"} or race_cat == "MX"
            if race_cat != category and not is_mixed:
                continue
            start_dt = parse_start_datetime(
                str(race.get("StartTime") or race.get("StartDate") or "")
            )
            if cutoff_dt is not None and (start_dt is None or start_dt > cutoff_dt):
                continue
            race_id = str(race.get("RaceId") or "")
            if not race_id:
                continue
            try:
                payload = get_race_results(race_id)
            except BiathlonError:
                continue
            if not payload.get("IsResult"):
                continue
            race_payloads.append((race_disc, race_cat, payload))

    cat_races: list[tuple[str, str, dict]] = []
    mixed_races: list[tuple[str, str, dict]] = []
    for race_disc, race_cat, payload in race_payloads:
        if race_disc in {"MR", "SR"} or race_cat == "MX":
            mixed_races.append((race_disc, race_cat, payload))
        else:
            cat_races.append((race_disc, race_cat, payload))

    def _ensure_athlete(key: str, name: str, nat: str) -> None:
        if key in athlete_stats:
            return
        athlete_stats[key] = {
            "name": name,
            "nat": nat,
            "gender": gender,
            "gold": 0,
            "silver": 0,
            "bronze": 0,
            "races": 0,
            "gold_ind": 0,
            "silver_ind": 0,
            "bronze_ind": 0,
            "races_ind": 0,
            "gold_relay": 0,
            "silver_relay": 0,
            "bronze_relay": 0,
            "races_relay": 0,
        }

    def _update_athlete(key: str, medal: str, relay: bool) -> None:
        athlete_stats[key]["races"] += 1
        athlete_stats[key]["races_relay" if relay else "races_ind"] += 1
        if medal == "gold":
            athlete_stats[key]["gold"] += 1
            athlete_stats[key]["gold_relay" if relay else "gold_ind"] += 1
        elif medal == "silver":
            athlete_stats[key]["silver"] += 1
            athlete_stats[key]["silver_relay" if relay else "silver_ind"] += 1
        elif medal == "bronze":
            athlete_stats[key]["bronze"] += 1
            athlete_stats[key]["bronze_relay" if relay else "bronze_ind"] += 1

    for race_disc, _race_cat, payload in cat_races:
        results = payload.get("Results", []) or []
        is_relay = race_disc in RELAY_DISCIPLINES
        gold_nat = silver_nat = bronze_nat = ""
        for res in results:
            if is_relay and not res.get("IsTeam"):
                continue
            if not is_relay and res.get("IsTeam"):
                continue
            rank_val = _parse_rank(res.get("Rank") or res.get("SO"))
            if rank_val is None:
                continue
            nat = str(res.get("Nat") or "").upper()
            if rank_val == 1:
                gold_nat = nat
            elif rank_val == 2:
                silver_nat = nat
            elif rank_val == 3:
                bronze_nat = nat
        if gold_nat:
            country_medals.append(
                {
                    "discipline": race_disc,
                    "gold": gold_nat,
                    "silver": silver_nat,
                    "bronze": bronze_nat,
                }
            )

        if is_relay:
            team_ranks: dict[str, int] = {}
            for res in results:
                if not res.get("IsTeam"):
                    continue
                rank_val = _parse_rank(res.get("Rank") or res.get("SO"))
                if rank_val is None:
                    continue
                nat = str(res.get("Nat") or "").upper()
                if nat:
                    team_ranks[nat] = rank_val
            for res in results:
                if res.get("IsTeam"):
                    continue
                nat = str(res.get("Nat") or "").upper()
                rank_val = team_ranks.get(nat)
                if rank_val is None:
                    continue
                ibu_id = _row_ibu_id(res)
                name = str(res.get("Name") or res.get("ShortName") or "")
                key = ibu_id or f"{name}|{nat}"
                if not key:
                    continue
                if ibu_id:
                    known_cat_ids.add(ibu_id)
                _ensure_athlete(key, name, nat)
                medal = (
                    "gold"
                    if rank_val == 1
                    else "silver"
                    if rank_val == 2
                    else "bronze"
                    if rank_val == 3
                    else ""
                )
                _update_athlete(key, medal, relay=True)
        else:
            for res in results:
                if res.get("IsTeam"):
                    continue
                rank_val = _parse_rank(res.get("Rank") or res.get("SO"))
                if rank_val not in (1, 2, 3):
                    continue
                ibu_id = _row_ibu_id(res)
                name = str(res.get("Name") or res.get("ShortName") or "")
                nat = str(res.get("Nat") or "").upper()
                key = ibu_id or f"{name}|{nat}"
                if not key:
                    continue
                if ibu_id:
                    known_cat_ids.add(ibu_id)
                _ensure_athlete(key, name, nat)
                medal = (
                    "gold" if rank_val == 1 else "silver" if rank_val == 2 else "bronze"
                )
                _update_athlete(key, medal, relay=False)

    for race_disc, _race_cat, payload in mixed_races:
        results = payload.get("Results", []) or []
        mixed_team_ranks: dict[str, int] = {}
        for res in results:
            if not res.get("IsTeam"):
                continue
            rank_val = _parse_rank(res.get("Rank") or res.get("SO"))
            if rank_val is None:
                continue
            nat = str(res.get("Nat") or "").upper()
            if nat:
                mixed_team_ranks[nat] = rank_val
        for res in results:
            if res.get("IsTeam"):
                continue
            ibu_id = _row_ibu_id(res)
            if not ibu_id or ibu_id not in known_cat_ids:
                continue
            nat = str(res.get("Nat") or "").upper()
            rank_val = mixed_team_ranks.get(nat)
            if rank_val is None:
                continue
            name = str(res.get("Name") or res.get("ShortName") or "")
            key = ibu_id
            _ensure_athlete(key, name, nat)
            medal = (
                "gold"
                if rank_val == 1
                else "silver"
                if rank_val == 2
                else "bronze"
                if rank_val == 3
                else ""
            )
            _update_athlete(key, medal, relay=True)

    return country_medals, athlete_stats


def _get_all_wch_medals(
    category: str, cutoff_dt: datetime.datetime | None = None
) -> tuple[list[dict], dict[str, dict]]:
    season_ids = _get_recent_wch_season_ids(limit=999)
    all_country: list[dict] = []
    merged_athletes: dict[str, dict] = {}
    for season_id in season_ids:
        country, athletes = _fetch_wch_season_medals(season_id, category, cutoff_dt)
        all_country.extend(country)
        for key, data in athletes.items():
            if key not in merged_athletes:
                merged_athletes[key] = {
                    "name": data["name"],
                    "nat": data["nat"],
                    "gender": data["gender"],
                    "gold": 0,
                    "silver": 0,
                    "bronze": 0,
                    "races": 0,
                    "gold_ind": 0,
                    "silver_ind": 0,
                    "bronze_ind": 0,
                    "races_ind": 0,
                    "gold_relay": 0,
                    "silver_relay": 0,
                    "bronze_relay": 0,
                    "races_relay": 0,
                }
            merged = merged_athletes[key]
            for field in (
                "gold",
                "silver",
                "bronze",
                "races",
                "gold_ind",
                "silver_ind",
                "bronze_ind",
                "races_ind",
                "gold_relay",
                "silver_relay",
                "bronze_relay",
                "races_relay",
            ):
                merged[field] += data.get(field, 0)
            merged["name"] = (
                data.get("name")
                if len(data.get("name", "")) > len(merged["name"])
                else merged["name"]
            )
    return all_country, merged_athletes


def _prepare_startlist_context(
    payload: dict,
    race_id: str,
    args: argparse.Namespace,
    snapshot_target_race_id: str = "",
    snapshot_cutoff_dt: datetime.datetime | None = None,
) -> dict:
    """Prepare shared context for startlist analysis functions.

    Returns a dict with all the data needed by render_startlist_analysis.
    """
    entries = _build_startlist_entries(payload)
    age_cache: dict[str, str] = {}

    # Fetch all bio ages in parallel
    ibu_ids_for_age = [e.get("ibu_id", "") for e in entries if e.get("ibu_id")]
    if ibu_ids_for_age:
        with ThreadPoolExecutor(
            max_workers=_max_workers(len(ibu_ids_for_age))
        ) as executor:
            for ibu_id, age in executor.map(_fetch_age, ibu_ids_for_age):
                age_cache[ibu_id] = age

    for entry in entries:
        ibu_id = entry.get("ibu_id", "")
        entry["age"] = age_cache.get(ibu_id, "-") if ibu_id else "-"

    comp = payload.get("Competition") or {}
    race_disc = str(comp.get("DisciplineId") or "").upper()
    is_relay = _is_relay_disc(race_disc)
    discipline_set = {race_disc} if is_relay else DISCIPLINES
    cat_id = str(comp.get("catId") or comp.get("CatId") or "").upper()
    season_id = (
        str((payload.get("SportEvt") or {}).get("SeasonId") or "")
        or get_current_season_id()
    )
    venue_name = _extract_venue_name(payload)
    event_type = detect_event_type(payload.get("SportEvt") or {})
    team_entries = _build_team_entries(payload)

    # Detect mixed relay races
    is_mixed = _is_mixed_relay(race_disc, cat_id)

    # Fetch genders in parallel for mixed relay races
    gender_cache: dict[str, str] = {}
    if is_mixed and ibu_ids_for_age:
        with ThreadPoolExecutor(
            max_workers=_max_workers(len(ibu_ids_for_age))
        ) as executor:
            for ibu_id, gender in executor.map(_fetch_gender, ibu_ids_for_age):
                gender_cache[ibu_id] = gender

    for entry in entries:
        ibu_id = entry.get("ibu_id", "")
        entry["gender"] = gender_cache.get(ibu_id, "") if is_mixed else ""

    startlist_ids = {entry["ibu_id"] for entry in entries if entry["ibu_id"]}

    # Don't pre-fetch alltime stats - let render_venue_history() fetch on demand
    alltime_stats: list[dict] | None = None

    # Prefetch all athlete results in parallel
    ibu_ids_to_fetch = [e["ibu_id"] for e in entries if e.get("ibu_id")]
    prefetched_results: dict[str, dict | None] = {}
    if ibu_ids_to_fetch:
        with ThreadPoolExecutor(
            max_workers=_max_workers(len(ibu_ids_to_fetch))
        ) as executor:
            futures = {
                executor.submit(_fetch_athlete_results, ibu_id): ibu_id
                for ibu_id in ibu_ids_to_fetch
            }
            for future in as_completed(futures):
                ibu_id, result = future.result()
                prefetched_results[ibu_id] = result

    snapshot_race_start_cache: dict[str, datetime.datetime | None] = {}
    if snapshot_target_race_id:
        snapshot_race_start_cache[snapshot_target_race_id] = snapshot_cutoff_dt

    return {
        "payload": payload,
        "race_id": race_id,
        "entries": entries,
        "age_cache": age_cache,
        "gender_cache": gender_cache,
        "comp": comp,
        "race_disc": race_disc,
        "is_relay": is_relay,
        "is_mixed": is_mixed,
        "discipline_set": discipline_set,
        "cat_id": cat_id,
        "season_id": season_id,
        "venue_name": venue_name,
        "event_type": event_type,
        "team_entries": team_entries,
        "startlist_ids": startlist_ids,
        "alltime_stats": alltime_stats,
        "prefetched_results": prefetched_results,
        "is_snapshot": bool(snapshot_target_race_id),
        "snapshot_target_race_id": snapshot_target_race_id,
        "snapshot_cutoff_dt": snapshot_cutoff_dt,
        "snapshot_race_start_cache": snapshot_race_start_cache,
    }


def render_venue_history(
    ctx: dict, args: argparse.Namespace, section_offset: int = 13
) -> None:
    """Render venue history sections (history & records).

    Args:
        ctx: Context dict with venue/race info
        args: Command arguments
        section_offset: Starting section number offset (default 13 for startlist, 0 for brief preevent)
    """
    race_id = ctx["race_id"]
    age_cache = ctx["age_cache"]
    race_disc = ctx["race_disc"]
    cat_id = ctx["cat_id"]
    venue_name = ctx["venue_name"]
    use_major = bool(ctx.get("use_major", False))
    startlist_ids = ctx["startlist_ids"]
    alltime_stats = ctx["alltime_stats"]

    # Compute highlight_ids: startlist athletes OR recently active athletes
    highlight_ids = startlist_ids
    if not startlist_ids and alltime_stats:
        current_season = get_current_season_id()
        prev_int = int(current_season) - 101
        prev_season = str(prev_int)

        def to_slash_format(s: str) -> str:
            return f"{s[:2]}/{s[2:]}"

        recent_seasons = {to_slash_format(current_season), to_slash_format(prev_season)}

        active_ids: set[str] = set()
        for stats in alltime_stats:
            ibu_id = stats.get("ibu_id", "")
            if ibu_id:
                try:
                    payload = get_all_results(ibu_id)
                    results = payload.get("Results") or []
                    for res in results:
                        if (res.get("Season") or "") in recent_seasons:
                            active_ids.add(ibu_id)
                            break
                except BiathlonError:
                    pass
        highlight_ids = active_ids

    # Separator between startlist sections and history sections
    print(_format_section_title("--- History & Records ---", args))
    print()

    # Last 5 winners of the same discipline (across seasons)
    if race_disc:
        is_relay_race = _is_relay_disc(race_disc)
        recent_rows: list[list] = []
        seasons = get_seasons()
        for season in seasons:
            if len(recent_rows) >= 5:
                break
            s_id = season.get("SeasonId")
            if not s_id:
                continue
            season_races: list[tuple[str, str, str]] = []  # (date, race_id, location)
            for level in _event_levels(use_major):
                try:
                    events = get_events(str(s_id), level=level)
                except BiathlonError:
                    continue
                for event in events:
                    event_id = event.get("EventId")
                    if not event_id:
                        continue
                    location = (
                        event.get("Organizer") or event.get("ShortDescription") or ""
                    )
                    try:
                        races_list = get_races(event_id)
                    except BiathlonError:
                        continue
                    for race in races_list:
                        race_id_check = race.get("RaceId") or race.get("Id")
                        if not race_id_check or race_id_check == race_id:
                            continue
                        disc_check = str(race.get("DisciplineId") or "").upper()
                        if disc_check != race_disc:
                            continue
                        race_cat = str(
                            race.get("catId") or race.get("CatId") or ""
                        ).upper()
                        if cat_id and race_cat != cat_id:
                            continue
                        start_raw = race.get("StartTime") or race.get("StartDate") or ""
                        race_date = (
                            start_raw.split("T", 1)[0]
                            if isinstance(start_raw, str)
                            else ""
                        )
                        season_races.append((race_date, race_id_check, location))
            # Sort this season's races by date descending
            season_races.sort(key=lambda x: x[0], reverse=True)
            for race_date, past_race_id, location in season_races:
                if len(recent_rows) >= 5:
                    break
                try:
                    past_payload = get_race_results(past_race_id)
                except BiathlonError:
                    continue
                if _is_true(past_payload.get("IsStartList")):
                    continue
                results = past_payload.get("Results") or []
                winner = ""
                for res in results:
                    if is_relay_race:
                        if not res.get("IsTeam"):
                            continue
                    elif res.get("IsTeam"):
                        continue
                    rank_val = _parse_rank(res.get("Rank"))
                    if rank_val == 1:
                        name = res.get("Name") or res.get("ShortName") or ""
                        nat = res.get("Nat") or ""
                        winner_ibu = res.get("IBUId") or res.get("IbuId") or ""
                        winner_text = f"{name} ({nat})" if nat else name
                        if winner_ibu in highlight_ids:
                            winner = Color.highlight(winner_text)
                        else:
                            winner = winner_text
                        break
                if winner:
                    recent_rows.append([race_date, location, winner])
        if recent_rows:
            print(
                _format_section_title(
                    f"{section_offset + 1}. Last 5 {race_disc} winners:", args
                )
            )
            render_table(
                ["Date", "Location", "Winner"],
                recent_rows,
                output_format=get_output_format(args),
            )
            print()

    # Last 5 winners at this venue for this discipline (across seasons)
    if venue_name and race_disc:
        is_relay_race = _is_relay_disc(race_disc)
        venue_lower = venue_name.lower()
        venue_winner_rows: list[list] = []
        seasons = get_seasons()
        for season in seasons:
            if len(venue_winner_rows) >= 5:
                break
            s_id = season.get("SeasonId")
            if not s_id:
                continue
            venue_season_races: list[tuple[str, str]] = []  # (date, race_id)
            for level in _event_levels(use_major):
                try:
                    events = get_events(str(s_id), level=level)
                except BiathlonError:
                    continue
                for event in events:
                    organizer = str(event.get("Organizer") or "").lower()
                    if venue_lower not in organizer and organizer not in venue_lower:
                        continue
                    event_id = event.get("EventId")
                    if not event_id:
                        continue
                    try:
                        races_list = get_races(event_id)
                    except BiathlonError:
                        continue
                    for race in races_list:
                        race_id_check = race.get("RaceId") or race.get("Id")
                        if not race_id_check or race_id_check == race_id:
                            continue
                        disc_check = str(race.get("DisciplineId") or "").upper()
                        if disc_check != race_disc:
                            continue
                        race_cat = str(
                            race.get("catId") or race.get("CatId") or ""
                        ).upper()
                        if cat_id and race_cat != cat_id:
                            continue
                        start_raw = race.get("StartTime") or race.get("StartDate") or ""
                        race_date = (
                            start_raw.split("T", 1)[0]
                            if isinstance(start_raw, str)
                            else ""
                        )
                        venue_season_races.append((race_date, race_id_check))
            # Sort this season's races by date descending
            venue_season_races.sort(key=lambda x: x[0], reverse=True)
            for race_date, past_race_id in venue_season_races:
                if len(venue_winner_rows) >= 5:
                    break
                try:
                    past_payload = get_race_results(past_race_id)
                except BiathlonError:
                    continue
                if _is_true(past_payload.get("IsStartList")):
                    continue
                results = past_payload.get("Results") or []
                winner = ""
                for res in results:
                    if is_relay_race:
                        if not res.get("IsTeam"):
                            continue
                    elif res.get("IsTeam"):
                        continue
                    rank_val = _parse_rank(res.get("Rank"))
                    if rank_val == 1:
                        name = res.get("Name") or res.get("ShortName") or ""
                        nat = res.get("Nat") or ""
                        winner_ibu = res.get("IBUId") or res.get("IbuId") or ""
                        winner_text = f"{name} ({nat})" if nat else name
                        if winner_ibu in highlight_ids:
                            winner = Color.highlight(winner_text)
                        else:
                            winner = winner_text
                        break
                if winner:
                    venue_winner_rows.append([race_date, winner])
        if venue_winner_rows:
            print(
                _format_section_title(
                    f"{section_offset + 2}. Last 5 {race_disc} winners at {venue_name}:",
                    args,
                )
            )
            render_table(
                ["Date", "Winner"],
                venue_winner_rows,
                output_format=get_output_format(args),
            )
            print()

    # All-time venue stats (all athletes in history)
    if venue_name and cat_id in {"SW", "SM", "MX"}:
        if alltime_stats is None:
            alltime_stats, _, _ = _get_alltime_venue_stats(
                venue_name, cat_id, use_major
            )
        if alltime_stats:
            # Top 5 winners at venue
            top_venue_winners = sorted(
                alltime_stats, key=lambda x: x["wins"], reverse=True
            )[:5]
            top_venue_winners = [s for s in top_venue_winners if s["wins"] > 0]
            if top_venue_winners:
                print(
                    _format_section_title(
                        f"{section_offset + 3}. Top 5 winners at {venue_name}:", args
                    )
                )
                venue_win_rows = []
                highlight_rows_16 = set()
                for idx, s in enumerate(top_venue_winners):
                    if s.get("ibu_id", "") in highlight_ids:
                        highlight_rows_16.add(idx)
                    venue_win_rows.append([s["name"], s["wins"]])

                def hl_16(cell_str: str, row_idx: int) -> str:
                    return (
                        Color.highlight(cell_str)
                        if row_idx in highlight_rows_16
                        else cell_str
                    )

                render_table(
                    ["Athlete", "Wins"],
                    venue_win_rows,
                    output_format=get_output_format(args),
                    cell_formatters=[hl_16, None],
                )
                print()

            # Top 5 by races at venue
            top_venue_races = sorted(
                alltime_stats, key=lambda x: x["races"], reverse=True
            )[:5]
            top_venue_races = [s for s in top_venue_races if s["races"] > 0]
            if top_venue_races:
                print(
                    _format_section_title(
                        f"{section_offset + 4}. Top 5 most races at {venue_name}:", args
                    )
                )
                venue_race_rows = []
                highlight_rows_17 = set()
                for idx, s in enumerate(top_venue_races):
                    if s.get("ibu_id", "") in highlight_ids:
                        highlight_rows_17.add(idx)
                    age_val = _age_for_ibu(s.get("ibu_id", ""), age_cache)
                    venue_race_rows.append([s["name"], age_val, s["nat"], s["races"]])

                def hl_17(cell_str: str, row_idx: int) -> str:
                    return (
                        Color.highlight(cell_str)
                        if row_idx in highlight_rows_17
                        else cell_str
                    )

                render_table(
                    ["Athlete", "Age", "Nat", "Races"],
                    venue_race_rows,
                    output_format=get_output_format(args),
                    cell_formatters=[hl_17, None, None, None],
                )
                print()

            # Venue history for all athletes (like section 8 but not limited to startlist)
            # Filter to athletes with at least one win, sort by wins, podiums, flowers (desc), races (asc)
            alltime_decorated = [s for s in alltime_stats if s["wins"] > 0]
            alltime_decorated.sort(
                key=lambda s: (s["wins"], s["podiums"], s["flowers"], -s["races"]),
                reverse=True,
            )
            alltime_decorated = alltime_decorated[:20]
            if alltime_decorated:
                print(
                    _format_section_title(
                        f"{section_offset + 5}. Venue history at {venue_name} (all athletes):",
                        args,
                    )
                )
                alltime_venue_rows = []
                highlight_row_indices = set()
                for idx, stats in enumerate(alltime_decorated):
                    if stats.get("ibu_id", "") in highlight_ids:
                        highlight_row_indices.add(idx)
                    alltime_venue_rows.append(
                        [
                            idx + 1,
                            stats["name"],
                            stats["wins"],
                            stats["podiums"],
                            stats["flowers"],
                            stats["races"],
                        ]
                    )

                def highlight_athlete(cell_str: str, row_idx: int) -> str:
                    if row_idx in highlight_row_indices:
                        return Color.highlight(cell_str)
                    return cell_str

                render_table(
                    ["#", "Athlete", "Wins", "Podiums", "Flowers", "Races"],
                    alltime_venue_rows,
                    output_format=get_output_format(args),
                    cell_formatters=[None, highlight_athlete, None, None, None, None],
                )
                print()

    # Team venue history and records for relay races
    if venue_name and race_disc in RELAY_DISCIPLINES:
        team_stats, total_races = _get_team_venue_stats(
            venue_name, cat_id, race_disc, use_major
        )
        if team_stats:
            # Team venue history
            team_stats.sort(
                key=lambda s: (s["wins"], s["podiums"], s["races"]), reverse=True
            )
            team_rows = []
            for stats in team_stats[:10]:
                team_rows.append(
                    [
                        stats["name"],
                        stats["races"],
                        stats["wins"],
                        stats["podiums"],
                        stats["flowers"],
                    ]
                )
            print(
                _format_section_title(
                    f"{section_offset + 6}. Team venue history at {venue_name} ({total_races} races in history):",
                    args,
                )
            )
            render_table(
                ["Team", "Participations", "Wins", "Podiums", "Flowers"],
                team_rows,
                output_format=get_output_format(args),
            )
            print()

            # Team venue records
            team_records = _identify_venue_records(team_stats)
            team_records_rows = []
            if team_records["wins"][0] > 0:
                team_records_rows.append(
                    ["Most wins", team_records["wins"][1], team_records["wins"][0]]
                )
            if team_records["podiums"][0] > 0:
                team_records_rows.append(
                    [
                        "Most podiums",
                        team_records["podiums"][1],
                        team_records["podiums"][0],
                    ]
                )
            if team_records["flowers"][0] > 0:
                team_records_rows.append(
                    [
                        "Most flowers",
                        team_records["flowers"][1],
                        team_records["flowers"][0],
                    ]
                )
            if team_records["races"][0] > 0:
                team_records_rows.append(
                    [
                        "Most participations",
                        team_records["races"][1],
                        team_records["races"][0],
                    ]
                )
            if team_records_rows:
                print(
                    _format_section_title(
                        f"{section_offset + 7}. Team venue records at {venue_name} (all teams in history):",
                        args,
                    )
                )
                render_table(
                    ["Category", "Team", "Count"],
                    team_records_rows,
                    output_format=get_output_format(args),
                )
                print()


def _parse_points_number(value: Any) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def _collect_startlist_countries(ctx: dict) -> set[str]:
    countries: set[str] = set()
    for entry in ctx.get("entries", []) or []:
        nat = str(entry.get("nat") or "").upper()
        if nat:
            countries.add(nat)
    for team in ctx.get("team_entries", []) or []:
        nat = str(team.get("nat") or "").upper()
        if nat:
            countries.add(nat)
    return countries


def _compute_country_what_if_scenarios(
    standings: list[dict], racing_countries: set[str], label: str
) -> list[str]:
    ranked: list[dict[str, Any]] = []
    for idx, row in enumerate(standings):
        rank_raw = row.get("Rank") or row.get("Standing") or idx + 1
        rank = _parse_rank(rank_raw)
        if rank is None:
            continue
        nat = str(row.get("Nat") or row.get("Country") or row.get("Name") or "").upper()
        if not nat:
            continue
        name = str(row.get("Name") or _country_display(nat) or nat)
        ranked.append(
            {
                "rank": rank,
                "nat": nat,
                "name": name,
                "points": _parse_points_number(row.get("Score") or row.get("Points")),
            }
        )
    ranked.sort(key=lambda item: item["rank"])
    if len(ranked) < 2:
        return []
    leader = ranked[0]
    chaser = ranked[1]
    leader_racing = leader["nat"] in racing_countries
    chaser_racing = chaser["nat"] in racing_countries
    out: list[str] = []
    prefix = f"[{label}] "
    if not leader_racing or not chaser_racing:
        reasons = []
        if not leader_racing:
            reasons.append(f"{leader['name']} (#1) not racing")
        if not chaser_racing:
            reasons.append(f"{chaser['name']} (#2) not racing")
        if reasons:
            out.append(prefix + "; ".join(reasons))
        return out

    gap = leader["points"] - chaser["points"]
    if gap < 90:
        max_leader_points = 90 - gap - 1
        finish_pos: int | None = None
        for pos in range(1, 41):
            if _get_wc_points(pos) <= max_leader_points:
                finish_pos = pos
                break
        if finish_pos is None:
            out.append(
                f"{prefix}{chaser['name']} can overtake with a win if {leader['name']} finishes outside top 40"
            )
        else:
            out.append(
                f"{prefix}{chaser['name']} can overtake with a win if {leader['name']} finishes {_ordinal(finish_pos)} or worse"
            )
    else:
        out.append(
            f"{prefix}{chaser['name']} trails {leader['name']} by {int(gap)} pts (best case still behind)"
        )
    return out


def _render_individual_podium_table(
    section_id: str,
    rows: list[dict],
    args: argparse.Namespace,
    use_year: bool = False,
    highlight_names: set[str] | None = None,
    last_name_only: bool = False,
    include_nat: bool = True,
    title_override: str | None = None,
) -> None:
    if not rows:
        if title_override:
            _print_spaced_section_title(f"{title_override}: none", args)
        else:
            _print_section_none(section_id, args)
        return
    _print_spaced_section_title(title_override or _section_title(section_id), args)
    lead_col = "Year" if use_year else "Date"
    lead_key = "year" if use_year else "date"
    highlight_names = highlight_names or set()

    def _format_medalist(row: dict, medal_key: str, athletes_key: str) -> str:
        athletes = row.get(athletes_key, []) or []
        if not athletes:
            return str(row.get(medal_key) or "")
        athlete = athletes[0]
        family = str(athlete.get("name") or "")
        full_name = str(
            athlete.get("full_name") or athlete.get("name") or row.get(medal_key) or ""
        )
        if last_name_only:
            name = family or full_name.split(" ")[-1]
        else:
            name = full_name
        nat = str(athlete.get("nat") or "")
        display = f"{name} ({nat})" if include_nat and nat and nat not in name else name
        if family and family in highlight_names:
            return Color.highlight(display)
        return display

    table_rows: list[list[str]] = []
    for row in rows:
        table_rows.append(
            [
                str(row.get(lead_key) or ""),
                str(row.get("race_type") or ""),
                str(row.get("venue") or ""),
                _format_medalist(row, "gold", "gold_athletes"),
                _format_medalist(row, "silver", "silver_athletes"),
                _format_medalist(row, "bronze", "bronze_athletes"),
            ]
        )
    render_table(
        [
            lead_col,
            "Type",
            "Venue",
            Color.gold("Gold"),
            Color.silver("Silver"),
            Color.bronze("Bronze"),
        ],
        table_rows,
        output_format=get_output_format(args),
        column_separators={3},
    )
    print()


def _render_relay_podium_table(
    section_id: str,
    rows: list[dict],
    args: argparse.Namespace,
    use_year: bool = False,
    highlight_names: set[str] | None = None,
    last_name_only: bool = False,
    title_override: str | None = None,
) -> None:
    if not rows:
        if title_override:
            _print_spaced_section_title(f"{title_override}: none", args)
        else:
            _print_section_none(section_id, args)
        return
    _print_spaced_section_title(title_override or _section_title(section_id), args)
    lead_col = "Year" if use_year else "Date"
    lead_key = "year" if use_year else "date"
    highlight_names = highlight_names or set()

    def _format_athletes(values: list[dict]) -> str:
        names: list[str] = []
        for athlete in values:
            name = str(athlete.get("name") or "")
            full_name = str(athlete.get("full_name") or name)
            display_name = (
                (name or full_name.split(" ")[-1]) if last_name_only else full_name
            )
            if name and name in highlight_names:
                names.append(Color.highlight_plain(display_name))
            else:
                names.append(Color.dim(display_name) if display_name else "-")
        return "/".join(n for n in names if n) or "-"

    table_rows: list[list[str]] = []
    for row in rows:
        table_rows.append(
            [
                str(row.get(lead_key) or ""),
                str(row.get("race_type") or ""),
                str(row.get("venue") or ""),
                str(row.get("country") or "-"),
                str(row.get("gold") or ""),
                _format_athletes(row.get("gold_athletes", [])),
                str(row.get("silver") or ""),
                _format_athletes(row.get("silver_athletes", [])),
                str(row.get("bronze") or ""),
                _format_athletes(row.get("bronze_athletes", [])),
            ]
        )
    render_table(
        [lead_col, "Type", "Venue", "Country", "", "", "", "", "", ""],
        table_rows,
        output_format=get_output_format(args),
        column_separators={4, 6, 8},
        group_headers=[(4, 6, "GOLD"), (6, 8, "SILVER"), (8, 10, "BRONZE")],
        group_headers_position="inline",
    )
    print()


def _render_country_medal_table_from_podiums(
    section_id: str,
    rows: list[dict],
    args: argparse.Namespace,
    title_override: str | None = None,
) -> None:
    medal_counts: dict[str, dict[str, int]] = {}
    for row in rows:
        for medal, key in (
            ("gold", "gold_nat"),
            ("silver", "silver_nat"),
            ("bronze", "bronze_nat"),
        ):
            nat = str(row.get(key) or "")
            if not nat:
                continue
            medal_counts.setdefault(nat, {"gold": 0, "silver": 0, "bronze": 0})[
                medal
            ] += 1
    if not medal_counts:
        if title_override:
            _print_spaced_section_title(f"{title_override}: none", args)
        else:
            _print_section_none(section_id, args)
        return
    sorted_rows = sorted(
        medal_counts.items(),
        key=lambda item: (
            item[1]["gold"],
            item[1]["silver"],
            item[1]["bronze"],
            item[0],
        ),
        reverse=True,
    )
    _print_spaced_section_title(title_override or _section_title(section_id), args)
    table_rows: list[list[str]] = []
    for idx, (nat, counts) in enumerate(sorted_rows, start=1):
        total = counts["gold"] + counts["silver"] + counts["bronze"]
        table_rows.append(
            [
                str(idx),
                _country_display(nat),
                str(counts["gold"]),
                str(counts["silver"]),
                str(counts["bronze"]),
                str(total),
            ]
        )
    render_table(
        [
            "#",
            "Country",
            Color.gold("Gold"),
            Color.silver("Silver"),
            Color.bronze("Bronze"),
            "Total",
        ],
        table_rows,
        output_format=get_output_format(args),
        column_separators={2},
    )
    print()


def _render_athlete_medal_table_from_podiums(
    section_id: str,
    rows: list[dict],
    args: argparse.Namespace,
    highlight_names: set[str],
    min_gold: int = 0,
    include_highlight_medalists: bool = False,
    title_override: str | None = None,
) -> None:
    athlete_counts: dict[str, dict[str, Any]] = {}
    for row in rows:
        for medal, key in (
            ("gold", "gold_athletes"),
            ("silver", "silver_athletes"),
            ("bronze", "bronze_athletes"),
        ):
            for athlete in row.get(key, []) or []:
                full_name = str(athlete.get("full_name") or athlete.get("name") or "")
                family = str(athlete.get("name") or "")
                nat = str(athlete.get("nat") or "")
                gender = str(athlete.get("gender") or "")
                if not full_name:
                    continue
                if full_name not in athlete_counts:
                    athlete_counts[full_name] = {
                        "family": family,
                        "nat": nat,
                        "gender": gender,
                        "gold": 0,
                        "silver": 0,
                        "bronze": 0,
                        "races": 0,
                    }
                athlete_counts[full_name][medal] += 1
                athlete_counts[full_name]["races"] += 1
    if not athlete_counts:
        if title_override:
            _print_spaced_section_title(f"{title_override}: none", args)
        else:
            _print_section_none(section_id, args)
        return
    sorted_rows = sorted(
        athlete_counts.items(),
        key=lambda item: (
            item[1]["gold"],
            item[1]["silver"],
            item[1]["bronze"],
            item[1]["gold"] + item[1]["silver"] + item[1]["bronze"],
            item[1]["races"],
            item[0],
        ),
        reverse=True,
    )
    table_rows: list[list[str]] = []
    row_styles: list[str] = []
    for idx, (name, counts) in enumerate(sorted_rows, start=1):
        total = counts["gold"] + counts["silver"] + counts["bronze"]
        include_row = True
        if min_gold > 0 and counts["gold"] < min_gold:
            include_row = False
        if (
            include_highlight_medalists
            and counts["family"] in highlight_names
            and total > 0
        ):
            include_row = True
        if not include_row:
            continue
        table_rows.append(
            [
                str(idx),
                name,
                counts["nat"],
                counts["gender"],
                str(counts["gold"]),
                str(counts["silver"]),
                str(counts["bronze"]),
                str(total),
                str(counts["races"]),
            ]
        )
        row_styles.append("highlight" if counts["family"] in highlight_names else "dim")
    if not table_rows:
        if title_override:
            _print_spaced_section_title(f"{title_override}: none", args)
        else:
            _print_section_none(section_id, args)
        return
    _print_spaced_section_title(title_override or _section_title(section_id), args)
    render_table(
        [
            "#",
            "Athlete",
            "Nat",
            "Gender",
            Color.gold("Gold"),
            Color.silver("Silver"),
            Color.bronze("Bronze"),
            "Total",
            "Races",
        ],
        table_rows,
        output_format=get_output_format(args),
        row_styles=row_styles,
        column_separators={4},
    )
    print()


def _render_country_all_medal_table(
    section_id: str,
    all_country_medals: list[dict],
    args: argparse.Namespace,
    title_override: str | None = None,
    blank_after_title: int = 1,
) -> None:
    if not all_country_medals:
        if title_override:
            _print_spaced_section_title(
                f"{title_override}: none",
                args,
                blank_after=blank_after_title,
            )
        else:
            _print_section_none(section_id, args)
        return
    counts_by_country: dict[str, dict[str, int]] = {}
    for row in all_country_medals:
        disc = str(row.get("discipline") or "").upper()
        is_relay = disc in RELAY_DISCIPLINES
        for medal in ("gold", "silver", "bronze"):
            nat = str(row.get(medal) or "")
            if not nat:
                continue
            target = counts_by_country.setdefault(
                nat,
                {
                    "gold": 0,
                    "silver": 0,
                    "bronze": 0,
                    "gold_ind": 0,
                    "silver_ind": 0,
                    "bronze_ind": 0,
                    "gold_relay": 0,
                    "silver_relay": 0,
                    "bronze_relay": 0,
                },
            )
            target[medal] += 1
            target[f"{medal}_relay" if is_relay else f"{medal}_ind"] += 1
    sorted_rows = sorted(
        counts_by_country.items(),
        key=lambda item: (
            item[1]["gold"],
            item[1]["silver"],
            item[1]["bronze"],
            item[0],
        ),
        reverse=True,
    )
    _print_spaced_section_title(
        title_override or _section_title(section_id),
        args,
        blank_after=blank_after_title,
    )
    table_rows: list[list[str]] = []
    for idx, (nat, counts) in enumerate(sorted_rows, start=1):
        total = counts["gold"] + counts["silver"] + counts["bronze"]
        total_ind = counts["gold_ind"] + counts["silver_ind"] + counts["bronze_ind"]
        total_relay = (
            counts["gold_relay"] + counts["silver_relay"] + counts["bronze_relay"]
        )
        table_rows.append(
            [
                str(idx),
                _country_display(nat),
                str(counts["gold"]),
                str(counts["silver"]),
                str(counts["bronze"]),
                str(total),
                str(counts["gold_ind"]),
                str(counts["silver_ind"]),
                str(counts["bronze_ind"]),
                str(total_ind),
                str(counts["gold_relay"]),
                str(counts["silver_relay"]),
                str(counts["bronze_relay"]),
                str(total_relay),
            ]
        )
    render_table(
        [
            "#",
            "Country",
            Color.gold("Gold"),
            Color.silver("Silver"),
            Color.bronze("Bronze"),
            "Total",
            Color.gold("Gold"),
            Color.silver("Silver"),
            Color.bronze("Bronze"),
            "Total",
            Color.gold("Gold"),
            Color.silver("Silver"),
            Color.bronze("Bronze"),
            "Total",
        ],
        table_rows,
        output_format=get_output_format(args),
        column_separators={2, 6, 10},
        group_headers=[(2, 6, "All"), (6, 10, "Individual"), (10, 14, "Relay")],
    )
    print()


def _render_athlete_all_medal_table(
    section_id: str,
    all_athlete_stats: dict[str, dict],
    highlight_ids: set[str],
    args: argparse.Namespace,
    visible_ids: set[str] | None = None,
    title_override: str | None = None,
    blank_after_title: int = 1,
) -> None:
    if not all_athlete_stats:
        if title_override:
            _print_spaced_section_title(
                f"{title_override}: none",
                args,
                blank_after=blank_after_title,
            )
        else:
            _print_section_none(section_id, args)
        return
    sorted_rows = sorted(
        all_athlete_stats.items(),
        key=lambda item: (
            item[1].get("gold", 0),
            item[1].get("gold_ind", 0),
            item[1].get("gold_relay", 0),
            item[1].get("silver", 0),
            item[1].get("silver_ind", 0),
            item[1].get("silver_relay", 0),
            item[1].get("bronze", 0),
            item[1].get("bronze_ind", 0),
            item[1].get("bronze_relay", 0),
            item[1].get("races", 0),
        ),
        reverse=True,
    )
    if visible_ids is not None and not any(
        key in visible_ids for key, _stats in sorted_rows
    ):
        if title_override:
            _print_spaced_section_title(
                f"{title_override}: none",
                args,
                blank_after=blank_after_title,
            )
        else:
            _print_section_none(section_id, args)
        return
    _print_spaced_section_title(
        title_override or _section_title(section_id),
        args,
        blank_after=blank_after_title,
    )
    table_rows: list[list[str]] = []
    row_styles: list[str] = []
    for idx, (key, stats) in enumerate(sorted_rows, start=1):
        if visible_ids is not None and key not in visible_ids:
            continue
        gold = int(stats.get("gold", 0))
        silver = int(stats.get("silver", 0))
        bronze = int(stats.get("bronze", 0))
        total = gold + silver + bronze
        table_rows.append(
            [
                str(idx),
                str(stats.get("name") or ""),
                str(stats.get("nat") or ""),
                str(stats.get("gender") or ""),
                str(gold),
                str(silver),
                str(bronze),
                str(total),
                str(stats.get("races", 0)),
                str(stats.get("gold_ind", 0)),
                str(stats.get("silver_ind", 0)),
                str(stats.get("bronze_ind", 0)),
                str(
                    int(stats.get("gold_ind", 0))
                    + int(stats.get("silver_ind", 0))
                    + int(stats.get("bronze_ind", 0))
                ),
                str(stats.get("races_ind", 0)),
                str(stats.get("gold_relay", 0)),
                str(stats.get("silver_relay", 0)),
                str(stats.get("bronze_relay", 0)),
                str(
                    int(stats.get("gold_relay", 0))
                    + int(stats.get("silver_relay", 0))
                    + int(stats.get("bronze_relay", 0))
                ),
                str(stats.get("races_relay", 0)),
            ]
        )
        row_styles.append("highlight" if key in highlight_ids else "dim")
    render_table(
        [
            "#",
            "Athlete",
            "Nat",
            "Gender",
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
        ],
        table_rows,
        output_format=get_output_format(args),
        row_styles=row_styles,
        column_separators={4, 9, 14},
        group_headers=[(4, 9, "All"), (9, 14, "Individual"), (14, 19, "Relay")],
    )
    print()


def render_startlist_analysis(ctx: dict, args: argparse.Namespace) -> None:
    """Render brief-startlist sections via matrix-gated unified flow."""
    payload = ctx["payload"]
    race_id = ctx["race_id"]
    entries = ctx["entries"]
    race_disc = ctx["race_disc"]
    cat_id = ctx["cat_id"]
    season_id = ctx["season_id"]
    event_type = ctx.get("event_type", EVENT_TYPE_WC)
    startlist_ids = ctx["startlist_ids"]
    age_cache = ctx["age_cache"]
    prefetched_results = ctx["prefetched_results"]
    is_snapshot = bool(ctx.get("is_snapshot"))
    snapshot_target_race_id = str(ctx.get("snapshot_target_race_id") or "")
    snapshot_cutoff_dt = ctx.get("snapshot_cutoff_dt")
    snapshot_race_start_cache = ctx.get("snapshot_race_start_cache") or {}
    team_entries = list(ctx.get("team_entries") or [])

    category_code = _race_category_code(event_type)
    discipline_code = _matrix_discipline_code(race_disc, cat_id)
    output_format = get_output_format(args)
    is_mixed = ctx.get("is_mixed", False)
    startlist_countries = _collect_startlist_countries(ctx)
    startlist_athletes = _get_startlist_family_names(payload)
    mark_leaders = bool(getattr(args, "leader_markers", False)) and is_pretty_output(
        args
    )

    def enabled(section_id: str) -> bool:
        return _section_enabled(section_id, category_code, discipline_code)

    # Standings context (athlete cups)
    total_standings: list[dict] = []
    disc_standings: list[dict] = []
    wc_rows_for_missing: list[dict] | None = None
    disc_name = DISCIPLINE_CUP_SUFFIX.get(race_disc, race_disc)
    if race_disc in DISCIPLINES and cat_id in {"SW", "SM"}:
        if is_snapshot:
            total_standings, disc_standings = _compute_wc_pre_race_standings(
                season_id,
                cat_id,
                snapshot_target_race_id,
                race_disc,
                snapshot_cutoff_dt,
            )
            wc_rows_for_missing = total_standings
        else:
            total_cup_id, disc_cup_id = _get_cup_ids_for_race(
                season_id, cat_id, race_disc
            )
            total_standings = (
                _fetch_standings(total_cup_id, limit=10) if total_cup_id else []
            )
            disc_standings = (
                _fetch_standings(disc_cup_id, limit=10) if disc_cup_id else []
            )

    general_leader_id = _row_ibu_id(total_standings[0]) if total_standings else ""
    discipline_leader_id = _row_ibu_id(disc_standings[0]) if disc_standings else ""

    def format_leader_name(name: str, ibu_id: str) -> str:
        suffix = _leader_marker_suffix(
            ibu_id,
            general_leader_id,
            discipline_leader_id,
            mark_leaders,
        )
        return f"{name}{suffix}" if suffix else name

    def leader_name_cell(cell_str: str, row_idx: int) -> str:
        return _format_leader_markers(cell_str, row_idx)

    # Section 1
    if enabled(SECTION_MISSING_TOP25):
        wc_rows = (
            wc_rows_for_missing
            if wc_rows_for_missing is not None
            else _get_wc_rows(cat_id, season_id)
        )
        top_rows = wc_rows[:25]
        missing_rows: list[list[str]] = []
        for row in top_rows:
            ibu_id = _row_ibu_id(row)
            if ibu_id in startlist_ids:
                continue
            name = row.get("Name") or row.get("ShortName") or ""
            rank = str(row.get("Rank") or "")
            nat = str(row.get("Nat") or "")
            age_val = _age_for_ibu(ibu_id, age_cache) if ibu_id else "-"
            missing_rows.append(
                [rank, format_leader_name(str(name), ibu_id), age_val, nat]
            )
        if not missing_rows:
            _print_section_none(SECTION_MISSING_TOP25, args)
        else:
            _print_spaced_section_title(_section_title(SECTION_MISSING_TOP25), args)
            render_table(
                ["Rank", "Name", "Age", "Nat"],
                missing_rows,
                output_format=output_format,
                cell_formatters=[None, leader_name_cell, None, None],
                column_separators={2},
            )
            print()

    # Section 2
    if enabled(SECTION_PARTICIPATING_TEAMS):
        if not team_entries:
            _print_section_none(SECTION_PARTICIPATING_TEAMS, args)
        else:
            rosters = _build_team_rosters(payload)
            has_rosters = any(rosters.values())
            team_rows: list[list[str]] = []
            headers = ["Bib", "Team", "Nat"]
            if has_rosters:
                headers.extend(["Athlete 1", "Athlete 2", "Athlete 3", "Athlete 4"])
            for team in team_entries:
                team_row = [
                    str(team.get("bib") or ""),
                    str(team.get("name") or ""),
                    str(team.get("nat") or ""),
                ]
                if has_rosters:
                    bib = str(team.get("bib") or "")
                    nat = str(team.get("nat") or "")
                    roster = rosters.get(f"bib:{bib}") if bib else None
                    if not roster and nat:
                        roster = rosters.get(f"nat:{nat}")
                    team_row.extend(
                        (roster or [])[:4] + ["-"] * (4 - len((roster or [])[:4]))
                    )
                team_rows.append(team_row)
            _print_spaced_section_title(
                _section_title(SECTION_PARTICIPATING_TEAMS), args
            )
            render_table(
                headers,
                team_rows,
                output_format=output_format,
                column_separators={3} if has_rosters else {2},
            )
            print()

    # Sections 3 and 4
    for section_id, standings_rows in (
        (SECTION_WC_TOTAL, total_standings[:10]),
        (SECTION_WC_DISCIPLINE, disc_standings[:10]),
    ):
        if not enabled(section_id):
            continue
        if not standings_rows:
            _print_section_none(section_id, args)
            continue
        _render_standings_section(
            _section_title(section_id),
            standings_rows,
            args,
            startlist_ids,
            name_formatter=format_leader_name,
        )

    # Section 5 Nations Cup
    nations_rows_by_cat: dict[str, list[dict]] = {}
    if enabled(SECTION_NATIONS_CUP):
        mixed_like = discipline_code in {"MR", "SR"}
        target_cats = (
            ["SW", "SM"] if mixed_like else [cat_id] if cat_id in {"SW", "SM"} else []
        )
        for target_cat in target_cats:
            if is_snapshot:
                nation_rows = _compute_nations_pre_race_standings(
                    season_id,
                    snapshot_target_race_id,
                    snapshot_cutoff_dt,
                    target_cat,
                    limit=10,
                )
            else:
                nation_rows = _fetch_nations_cup_standings(
                    season_id, target_cat, limit=10
                )
            nations_rows_by_cat[target_cat] = nation_rows[:10]
        if not any(nations_rows_by_cat.values()):
            _print_section_none(SECTION_NATIONS_CUP, args)
        else:
            _print_spaced_section_title(_section_title(SECTION_NATIONS_CUP), args)
            for idx, target_cat in enumerate(target_cats):
                nation_rows = nations_rows_by_cat.get(target_cat, [])
                if not nation_rows:
                    continue
                if len(target_cats) > 1:
                    label = CATEGORY_DISPLAY_NAMES.get(target_cat, target_cat)
                    print(_format_section_title(label, args))
                table_rows = []
                for row_idx, standing_row in enumerate(nation_rows):
                    rank = str(
                        standing_row.get("Rank")
                        or standing_row.get("Standing")
                        or row_idx + 1
                    ).rstrip(".")
                    nat = str(standing_row.get("Nat") or "")
                    country = str(
                        standing_row.get("Name") or _country_display(nat) or nat
                    )
                    points = str(
                        standing_row.get("Score") or standing_row.get("Points") or "0"
                    )
                    table_rows.append([rank, country, points])
                render_table(
                    ["Rank", "Country", "Points"],
                    table_rows,
                    output_format=output_format,
                    column_separators={2},
                )
                if len(target_cats) > 1 and idx < len(target_cats) - 1:
                    print()
            print()

    # Section 6 Relay WC standings
    relay_rows: list[dict] = []
    if enabled(SECTION_RELAY_WC):
        _relay_label, relay_rows = _fetch_relay_wc_standings(
            season_id, cat_id, race_disc, limit=10
        )
        if not relay_rows:
            _print_section_none(SECTION_RELAY_WC, args)
        else:
            _print_spaced_section_title(_section_title(SECTION_RELAY_WC), args)
            table_rows = []
            for idx, row in enumerate(relay_rows[:10]):
                rank = str(row.get("Rank") or row.get("Standing") or idx + 1).rstrip(
                    "."
                )
                name = str(row.get("Name") or row.get("ShortName") or "")
                nat = str(row.get("Nat") or "")
                points = str(row.get("Score") or row.get("Points") or "0")
                table_rows.append([rank, name, nat, points])
            render_table(
                ["Rank", "Team", "Nat", "Points"],
                table_rows,
                output_format=output_format,
                column_separators={3},
            )
            print()

    # Section 7 Standings Watch
    if enabled(SECTION_STANDINGS_WATCH):
        scenarios: list[str] = []
        if race_disc in DISCIPLINES and cat_id in {"SW", "SM"}:
            scenarios.extend(
                _compute_what_if_scenarios(
                    total_standings,
                    disc_standings,
                    startlist_ids,
                    disc_name,
                    name_formatter=lambda n, i: _format_leader_markers(
                        format_leader_name(n, i), 0
                    ),
                )
            )
        if relay_rows:
            scenarios.extend(
                _compute_country_what_if_scenarios(
                    relay_rows,
                    startlist_countries,
                    "Relay WC",
                )
            )
        for target_cat, nation_rows in nations_rows_by_cat.items():
            if nation_rows:
                label = (
                    f"Nations Cup {CATEGORY_DISPLAY_NAMES.get(target_cat, target_cat)}"
                )
                scenarios.extend(
                    _compute_country_what_if_scenarios(
                        nation_rows, startlist_countries, label
                    )
                )
        if not scenarios:
            _print_section_none(SECTION_STANDINGS_WATCH, args)
        else:
            _print_spaced_section_title(_section_title(SECTION_STANDINGS_WATCH), args)
            for scenario in scenarios[:20]:
                print(f"  - {scenario}")
            print()

    # Section 8 Pursuit contenders
    if enabled(SECTION_PURSUIT_CONTENDERS):
        contenders: list[list[str]] = []
        for res in payload.get("Results", []) or []:
            if res.get("IsTeam"):
                continue
            delay = str(res.get("StartInfo") or "")
            delay_secs = parse_time_seconds(delay) if delay else None
            if delay_secs is None or delay_secs >= 60:
                continue
            ibu_id = str(res.get("IBUId") or res.get("IbuId") or "")
            contenders.append(
                [
                    delay,
                    format_leader_name(
                        str(res.get("Name") or res.get("ShortName") or ""), ibu_id
                    ),
                    str(res.get("Nat") or ""),
                ]
            )
        if not contenders:
            _print_section_none(SECTION_PURSUIT_CONTENDERS, args)
        else:
            contenders.sort(key=lambda row: parse_time_seconds(row[0]) or 0)
            _print_spaced_section_title(
                _section_title(SECTION_PURSUIT_CONTENDERS), args
            )
            render_table(
                ["Delay", "Athlete", "Nat"],
                contenders,
                output_format=output_format,
                cell_formatters=[None, leader_name_cell, None],
            )
            print()

    # Milestones sections
    race_rows: list[list[Any]] = []
    win_rows: list[list[Any]] = []

    event_levels = {"WC": {"WC"}, "WCH": {"WCH"}, "OWG": {"OWG"}}
    event_level_set = event_levels.get(category_code, {"WC"})
    event_race_step = 25 if category_code == "WC" else 5
    event_win_step = 5
    career_race_step = 25
    career_win_step = 5
    current_event_label = EVENT_TYPE_LABELS.get(category_code, category_code)
    career_event_label = "WC+WCH+OWG"
    disc_label = _discipline_display_label(race_disc, cat_id)
    class_label = "Team Race" if race_disc in RELAY_DISCIPLINES else "Indiv Race"

    def milestone(next_count: int, step: int, include_first: bool = True) -> int | None:
        if include_first and next_count == 1:
            return 1
        if step > 0 and next_count % step == 0:
            return next_count
        return None

    for entry in entries:
        ibu_id = entry.get("ibu_id") or ""
        if not ibu_id:
            continue
        result_payload = prefetched_results.get(ibu_id)
        if not result_payload:
            continue
        results = list(result_payload.get("Results") or [])
        if is_snapshot:
            results = _filter_results_before_cutoff(
                results,
                snapshot_target_race_id,
                snapshot_cutoff_dt,
                snapshot_race_start_cache,
            )
        major_results = [
            res
            for res in results
            if str(res.get("Level") or "").upper() in {"WC", "WCH", "OWG"}
        ]
        event_results = [
            res
            for res in results
            if str(res.get("Level") or "").upper() in event_level_set
        ]
        if not major_results and not event_results:
            continue

        def _counts(
            rows_in: list[dict],
        ) -> tuple[int, int, int, int, int, int, int, int]:
            all_races = len(rows_in)
            all_wins = 0
            ind_races = ind_wins = team_races = team_wins = 0
            disc_races = disc_wins = 0
            for res in rows_in:
                rank = _parse_rank(res.get("Rank") or res.get("SO"))
                disc = str(res.get("Comp") or "").upper()
                if rank == 1:
                    all_wins += 1
                is_team = disc in RELAY_DISCIPLINES
                if is_team:
                    team_races += 1
                    if rank == 1:
                        team_wins += 1
                else:
                    ind_races += 1
                    if rank == 1:
                        ind_wins += 1
                if disc == race_disc:
                    disc_races += 1
                    if rank == 1:
                        disc_wins += 1
            return (
                all_races,
                all_wins,
                ind_races,
                ind_wins,
                team_races,
                team_wins,
                disc_races,
                disc_wins,
            )

        (
            e_all,
            e_win,
            e_ind,
            e_ind_win,
            e_team,
            e_team_win,
            e_disc,
            e_disc_win,
        ) = _counts(event_results)
        (
            c_all,
            c_win,
            c_ind,
            c_ind_win,
            c_team,
            c_team_win,
            c_disc,
            c_disc_win,
        ) = _counts(major_results)
        name = format_leader_name(str(entry.get("name") or ""), ibu_id)
        age = str(entry.get("age") or "-")
        nat = str(entry.get("nat") or "")
        gender = _display_gender(str(entry.get("gender") or "")) if is_mixed else ""

        e_class_races = e_team if race_disc in RELAY_DISCIPLINES else e_ind
        c_class_races = c_team if race_disc in RELAY_DISCIPLINES else c_ind
        e_class_wins = e_team_win if race_disc in RELAY_DISCIPLINES else e_ind_win
        c_class_wins = c_team_win if race_disc in RELAY_DISCIPLINES else c_ind_win

        race_scopes = [
            (
                current_event_label,
                disc_label,
                e_disc,
                event_race_step,
                True,
                "event_discipline",
            ),
            (
                career_event_label,
                disc_label,
                c_disc,
                career_race_step,
                True,
                "career_discipline",
            ),
            (
                current_event_label,
                class_label,
                e_class_races,
                event_race_step,
                True,
                "event_class",
            ),
            (
                current_event_label,
                "Race",
                e_all,
                event_race_step,
                True,
                "event_all",
            ),
            (
                career_event_label,
                class_label,
                c_class_races,
                career_race_step,
                True,
                "career_class",
            ),
            (
                career_event_label,
                "Race",
                c_all,
                career_race_step,
                True,
                "career_all",
            ),
        ]
        win_scopes = [
            (
                current_event_label,
                disc_label,
                e_disc_win,
                event_win_step,
                False,
                "event_discipline",
            ),
            (
                career_event_label,
                disc_label,
                c_disc_win,
                career_win_step,
                False,
                "career_discipline",
            ),
            (
                current_event_label,
                class_label,
                e_class_wins,
                event_win_step,
                False,
                "event_class",
            ),
            (
                current_event_label,
                "Race",
                e_win,
                event_win_step,
                False,
                "event_all",
            ),
            (
                career_event_label,
                class_label,
                c_class_wins,
                career_win_step,
                False,
                "career_class",
            ),
            (
                career_event_label,
                "Race",
                c_win,
                career_win_step,
                False,
                "career_all",
            ),
        ]

        for (
            scope_event_label,
            type_label,
            current,
            step,
            include_first,
            scope_id,
        ) in race_scopes:
            hit = milestone(current + 1, step, include_first=include_first)
            if hit is None:
                continue
            milestone_row = [hit, scope_event_label, type_label, name]
            if is_mixed:
                milestone_row.extend([gender, age, nat, current])
            else:
                milestone_row.extend([age, nat, current])
            race_rows.append(milestone_row)

        for (
            scope_event_label,
            type_label,
            current,
            step,
            include_first,
            scope_id,
        ) in win_scopes:
            next_count = current + 1
            # For OWG/WCH current-event win milestones, use a less strict rule:
            # start at 2 and do not require multiples.
            if category_code in {"OWG", "WCH"} and scope_id.startswith("event_"):
                hit = next_count if next_count >= 2 else None
            else:
                hit = milestone(next_count, step, include_first=include_first)
            if hit is None:
                continue
            milestone_row = [hit, scope_event_label, type_label, name]
            if is_mixed:
                milestone_row.extend([gender, age, nat, current])
            else:
                milestone_row.extend([age, nat, current])
            win_rows.append(milestone_row)

    milestone_sections = [
        (SECTION_RACE_MILESTONES, race_rows, "CurrentRaces"),
        (SECTION_WIN_MILESTONES, win_rows, "CurrentWins"),
    ]

    for section_id, rows, count_header in milestone_sections:
        if not enabled(section_id):
            continue
        if not rows:
            _print_spaced_section_title(f"{_section_title(section_id)}: none", args)
            continue
        _print_spaced_section_title(_section_title(section_id), args)
        if is_mixed:
            headers = [
                "Milestone",
                "Event",
                "Type",
                "Athlete",
                "Gender",
                "Age",
                "Nat",
                count_header,
            ]
            formatters = [None, None, None, leader_name_cell, None, None, None, None]
        else:
            headers = [
                "Milestone",
                "Event",
                "Type",
                "Athlete",
                "Age",
                "Nat",
                count_header,
            ]
            formatters = [None, None, None, leader_name_cell, None, None, None]

        def _sort_for_subsection(sub_rows: list[list[Any]]) -> list[list[Any]]:
            athlete_max_milestone: dict[str, int] = {}
            athlete_age_sort: dict[str, int] = {}
            for row in sub_rows:
                athlete = str(row[3])
                milestone_value = int(row[0])
                previous = athlete_max_milestone.get(athlete, 0)
                if milestone_value > previous:
                    athlete_max_milestone[athlete] = milestone_value
                age_idx = 5 if is_mixed else 4
                age_raw = str(row[age_idx]).strip()
                age_val = int(age_raw) if age_raw.isdigit() else -1
                prev_age = athlete_age_sort.get(athlete, -1)
                if age_val > prev_age:
                    athlete_age_sort[athlete] = age_val
            sub_rows.sort(
                key=lambda row: (
                    -athlete_max_milestone.get(str(row[3]), 0),
                    -athlete_age_sort.get(str(row[3]), -1),
                    str(row[3]),
                    -int(row[0]),
                    str(row[1]),
                    str(row[2]),
                )
            )
            return sub_rows

        def _type_breadth(type_label: str) -> int:
            if type_label in {"Race", "Races"}:
                return 3
            if type_label in {"Indiv Race", "Team Race", "Indiv Races", "Team Races"}:
                return 2
            return 1

        def _dedupe_same_value_rows(sub_rows: list[list[Any]]) -> list[list[Any]]:
            deduped: dict[tuple[str, str, str, str, int], list[Any]] = {}
            for row in sub_rows:
                event_label = str(row[1])
                athlete = str(row[3])
                milestone_value = int(row[0])
                if is_mixed:
                    gender = str(row[4])
                    nat = str(row[6])
                else:
                    gender = ""
                    nat = str(row[5])
                # Keep same milestone value independently per event scope.
                # This allows one "1st" in Current Event and one "1st" in Career.
                key = (event_label, athlete, nat, gender, milestone_value)
                current = deduped.get(key)
                if current is None:
                    deduped[key] = row
                    continue
                row_breadth = _type_breadth(str(row[2]))
                current_breadth = _type_breadth(str(current[2]))
                if row_breadth > current_breadth:
                    deduped[key] = row
                    continue
                if row_breadth == current_breadth:
                    row_current = int(row[-1]) if str(row[-1]).isdigit() else -1
                    cur_current = int(current[-1]) if str(current[-1]).isdigit() else -1
                    if row_current > cur_current:
                        deduped[key] = row
            return list(deduped.values())

        sections_to_render = [
            (
                "Current Event",
                [row for row in rows if str(row[1]) == current_event_label],
            ),
            (
                "Career",
                [row for row in rows if str(row[1]) == career_event_label],
            ),
        ]
        for subsection_title, subsection_rows in sections_to_render:
            if not subsection_rows:
                continue
            deduped_rows = _dedupe_same_value_rows(subsection_rows)
            sorted_rows = _sort_for_subsection(deduped_rows)
            row_separators: set[int] = set()
            for idx in range(1, len(sorted_rows)):
                if str(sorted_rows[idx][3]) != str(sorted_rows[idx - 1][3]):
                    row_separators.add(idx)
            display_rows = [[_ordinal(int(row[0])), *row[1:]] for row in sorted_rows]
            if section_id in {SECTION_RACE_MILESTONES, SECTION_WIN_MILESTONES}:
                display_rows = [row[:-1] for row in display_rows]
                render_headers = headers[:-1]
                render_formatters = formatters[:-1]
            else:
                render_headers = headers
                render_formatters = formatters
            _print_spaced_section_title(
                subsection_title,
                args,
                level=3,
                blank_before=0,
            )
            render_table(
                render_headers,
                display_rows,
                output_format=output_format,
                cell_formatters=render_formatters,
                row_separators=row_separators,
            )
            print()

    # Sections 13-15 podium history
    cutoff = snapshot_cutoff_dt if is_snapshot else None
    previous_rows = (
        _get_previous_relay_podiums(race_id, season_id, race_disc, cat_id, cutoff)
        if discipline_code in {"RL", "MR", "SR"}
        else _get_previous_individual_podiums(
            race_id, season_id, race_disc, cat_id, cutoff
        )
    )
    discipline_title = _discipline_display_label(race_disc, cat_id)
    athlete_owg_disc_title = (
        f"Athlete Olympic Games Medal Table - {discipline_title} (all editions)"
    )
    country_owg_disc_title = (
        f"Country Olympic Games Medal Table - {discipline_title} (all editions)"
    )
    athlete_owg_all_title = (
        "Athlete Olympic Games Medal Table - All Disciplines (all editions)"
    )
    country_owg_all_title = (
        "Country Olympic Games Medal Table - All Disciplines (all editions)"
    )
    if enabled(SECTION_PREVIOUS_PODIUMS):
        if discipline_code in {"RL", "MR", "SR"}:
            _render_relay_podium_table(
                SECTION_PREVIOUS_PODIUMS,
                previous_rows,
                args,
                use_year=False,
                highlight_names=startlist_athletes,
                last_name_only=True,
            )
        else:
            _render_individual_podium_table(
                SECTION_PREVIOUS_PODIUMS,
                previous_rows,
                args,
                use_year=False,
                highlight_names=startlist_athletes,
                last_name_only=True,
            )

    owg_rows: list[dict] = []
    if enabled(SECTION_PREVIOUS_OWG_PODIUMS):
        if discipline_code in {"RL", "MR", "SR"}:
            owg_rows = _get_past_olympic_relay_podiums(race_disc, cat_id, cutoff)
            _render_relay_podium_table(
                SECTION_PREVIOUS_OWG_PODIUMS,
                owg_rows,
                args,
                use_year=True,
                highlight_names=startlist_athletes,
                last_name_only=True,
            )
        else:
            owg_rows = _get_past_olympic_individual_podiums(race_disc, cat_id, cutoff)
            _render_individual_podium_table(
                SECTION_PREVIOUS_OWG_PODIUMS,
                owg_rows,
                args,
                use_year=True,
                highlight_names=startlist_athletes,
                last_name_only=True,
                include_nat=False,
            )

    wch_rows: list[dict] = []
    if enabled(SECTION_PREVIOUS_WCH_PODIUMS):
        if discipline_code in {"RL", "MR", "SR"}:
            wch_rows = _get_past_wch_relay_podiums(
                race_disc, cat_id, n_editions=10, cutoff_dt=cutoff
            )
            _render_relay_podium_table(
                SECTION_PREVIOUS_WCH_PODIUMS,
                wch_rows,
                args,
                use_year=True,
                highlight_names=startlist_athletes,
            )
        else:
            wch_rows = _get_past_wch_individual_podiums(
                race_disc, cat_id, n_editions=10, cutoff_dt=cutoff
            )
            _render_individual_podium_table(
                SECTION_PREVIOUS_WCH_PODIUMS,
                wch_rows,
                args,
                use_year=True,
                highlight_names=startlist_athletes,
            )

    # Sections 16-23 medal tables
    if enabled(SECTION_COUNTRY_OWG_DISC):
        _render_country_medal_table_from_podiums(
            SECTION_COUNTRY_OWG_DISC,
            owg_rows,
            args,
            title_override=country_owg_disc_title,
        )
    if enabled(SECTION_COUNTRY_WCH_DISC):
        _render_country_medal_table_from_podiums(
            SECTION_COUNTRY_WCH_DISC, wch_rows, args
        )
    if enabled(SECTION_ATHLETE_OWG_DISC):
        _render_athlete_medal_table_from_podiums(
            SECTION_ATHLETE_OWG_DISC,
            owg_rows,
            args,
            startlist_athletes,
            min_gold=1,
            include_highlight_medalists=True,
            title_override=athlete_owg_disc_title,
        )
    if enabled(SECTION_ATHLETE_WCH_DISC):
        _render_athlete_medal_table_from_podiums(
            SECTION_ATHLETE_WCH_DISC, wch_rows, args, startlist_athletes
        )

    if any(
        enabled(section_id)
        for section_id in (
            SECTION_COUNTRY_OWG_ALL,
            SECTION_ATHLETE_OWG_ALL,
        )
    ):
        owg_country, owg_athletes = _get_all_olympic_medals(cat_id, cutoff)
    else:
        owg_country, owg_athletes = [], {}
    if any(
        enabled(section_id)
        for section_id in (
            SECTION_COUNTRY_WCH_ALL,
            SECTION_ATHLETE_WCH_ALL,
        )
    ):
        wch_country, wch_athletes = _get_all_wch_medals(cat_id, cutoff)
    else:
        wch_country, wch_athletes = [], {}

    if enabled(SECTION_COUNTRY_OWG_ALL):
        _render_country_all_medal_table(
            SECTION_COUNTRY_OWG_ALL,
            owg_country,
            args,
            title_override=country_owg_all_title,
            blank_after_title=0,
        )
    if enabled(SECTION_COUNTRY_WCH_ALL):
        _render_country_all_medal_table(SECTION_COUNTRY_WCH_ALL, wch_country, args)
    if enabled(SECTION_ATHLETE_OWG_ALL):
        visible_owg_athlete_ids: set[str] = set()
        for athlete_id, stats in owg_athletes.items():
            gold = int(stats.get("gold", 0))
            silver = int(stats.get("silver", 0))
            bronze = int(stats.get("bronze", 0))
            has_medal = (gold + silver + bronze) > 0
            if gold >= 2 or (athlete_id in startlist_ids and has_medal):
                visible_owg_athlete_ids.add(athlete_id)
        _render_athlete_all_medal_table(
            SECTION_ATHLETE_OWG_ALL,
            owg_athletes,
            startlist_ids,
            args,
            visible_ids=visible_owg_athlete_ids,
            title_override=athlete_owg_all_title,
            blank_after_title=0,
        )
    if enabled(SECTION_ATHLETE_WCH_ALL):
        _render_athlete_all_medal_table(
            SECTION_ATHLETE_WCH_ALL, wch_athletes, startlist_ids, args
        )
