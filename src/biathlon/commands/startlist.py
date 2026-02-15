"""Startlist analysis command handler."""

from __future__ import annotations

import argparse
import datetime
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
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
from ..formatting import Color, is_pretty_output, render_table
from ..utils import format_race_header, parse_start_datetime, parse_time_seconds
from ._common import (
    DISCIPLINE_LEADER_MARKER,
    GENERAL_LEADER_MARKER,
    _format_leader_markers,
    _format_section_title,
    _max_workers,
    _ordinal,
    _parse_rank,
    _row_ibu_id,
    _select_race_interactive,
    detect_event_type,
    is_mixed_relay as _is_mixed_relay,
    is_relay_discipline as _is_relay_disc,
)
from .results import _get_wc_rows


WC_RACE_MILESTONE_STEP = 25
WC_WIN_MILESTONE_STEP = 5
DISCIPLINES = {"SP", "PU", "IN", "MS", "SI"}
INDIVIDUAL_EQUIVALENT_DISCIPLINES = {"IN", "SI"}
MAJOR_EVENT_LEVELS = (1, 2, 3)
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
    "8990",
    "8586",
    "8182",
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


def _get_wc_points(position: int, mass_start: bool = False) -> int:
    """Look up World Cup points for a finish position."""
    if mass_start:
        return WC_POINTS_MS.get(position, 0)
    return WC_POINTS.get(position, 0)


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
        print(_format_section_title(f"{title}: no data available", args))
        print()
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

    print(_format_section_title(title, args))
    render_table(
        ["Rank", "Athlete", "Nat", "Points"],
        rows,
        pretty=is_pretty_output(args),
        cell_formatters=[row_dimmer, name_cell, row_dimmer, row_dimmer],
        column_separators={3},
    )
    print()


def _render_wc_standings_sections(
    ctx: dict,
    args: argparse.Namespace,
    total_standings: list[dict],
    disc_standings: list[dict],
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
    if not is_mixed and cat_id in {"SW", "SM"}:
        missing_rows = []
        wc_rows = _get_wc_rows(cat_id, season_id)
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
                pretty=is_pretty_output(args),
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


def _season_to_olympic_year(season_id: str) -> str:
    """Convert season ID (e.g., '2122') to Olympic year (e.g., '2022')."""
    if len(season_id) != 4:
        return season_id
    second_part = season_id[2:4]
    try:
        year_suffix = int(second_part)
        # Determine century: 90+ is 1900s, otherwise 2000s
        if year_suffix >= 90:
            return str(1900 + year_suffix)
        return str(2000 + year_suffix)
    except ValueError:
        return season_id


def _fetch_olympic_individual_podium(
    season_id: str,
    discipline: str,
    category: str,
    cutoff_dt: datetime.datetime | None = None,
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
                if start_dt is None or start_dt > cutoff_dt:
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
                if start_dt is None or start_dt > cutoff_dt:
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
        display = f"{name} ({nat})" if nat and nat not in name else name
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
                full_name = res.get("ShortName") or res.get("Name") or family_name
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
    year = _season_to_olympic_year(season_id)
    return {
        "year": year,
        "venue": venue,
        "gold": gold,
        "silver": silver,
        "bronze": bronze,
        "gold_athletes": gold_athletes,
        "silver_athletes": silver_athletes,
        "bronze_athletes": bronze_athletes,
    }


def _get_past_olympic_relay_podiums(
    discipline: str,
    category: str,
    cutoff_dt: datetime.datetime | None = None,
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
            if start_dt is None or start_dt > cutoff_dt:
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
                _fetch_olympic_season_medals, s_id, category, cutoff_dt
            ): s_id
            for s_id in OLYMPIC_SEASON_IDS
        }
        for future in as_completed(futures):
            country_medals, athlete_stats = future.result()
            all_country.extend(country_medals)
            _merge(athlete_stats)

    return all_country, merged_athletes


def _prepare_startlist_context(
    payload: dict,
    race_id: str,
    args: argparse.Namespace,
) -> dict:
    """Prepare shared context for startlist analysis functions.

    Returns a dict with all the data needed by render_startlist_analysis and render_venue_history.
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
    use_major = bool(getattr(args, "major", False))
    event_type = detect_event_type(payload.get("SportEvt") or {})

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
        "use_major": use_major,
        "event_type": event_type,
        "startlist_ids": startlist_ids,
        "alltime_stats": alltime_stats,
        "prefetched_results": prefetched_results,
    }


def _render_olympic_individual_sections(
    ctx: dict,
    args: argparse.Namespace,
    section_offset: int = 0,
) -> None:
    """Render Olympic history sections for an individual startlist."""
    payload = ctx["payload"]
    race_disc = ctx["race_disc"]
    cat_id = ctx["cat_id"]
    startlist_ids = ctx["startlist_ids"]
    pretty = is_pretty_output(args)

    disc_name = DISCIPLINE_NAMES.get(race_disc, race_disc)
    cat_name = CATEGORY_DISPLAY_NAMES.get(cat_id, cat_id)

    highlight_athletes = _get_startlist_family_names(payload)

    podiums: list[dict] = []
    all_country_medals: list[dict] = []
    all_athlete_stats: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=2) as executor:
        podiums_future = executor.submit(
            _get_past_olympic_individual_podiums, race_disc, cat_id
        )
        medals_future = executor.submit(_get_all_olympic_medals, cat_id)
        podiums = podiums_future.result()
        all_country_medals, all_athlete_stats = medals_future.result()

    # Section 1: Past Olympic podiums
    if not podiums:
        print(
            _format_section_title(
                f"{section_offset + 1}. Past Olympic {cat_name} {disc_name} podiums: none",
                args,
            )
        )
        print()
    else:
        print(
            _format_section_title(
                f"{section_offset + 1}. Past Olympic {cat_name} {disc_name} podiums:",
                args,
            )
        )

        def format_medalist(row: dict, medal_key: str, athletes_key: str) -> str:
            athletes = row.get(athletes_key, [])
            if athletes:
                athlete = athletes[0]
                name = (
                    athlete.get("full_name")
                    or athlete.get("name")
                    or row.get(medal_key, "")
                )
                nat = athlete.get("nat") or ""
                display = name
                if nat and nat not in name:
                    display = f"{name} ({nat})"
                if athlete.get("name") in highlight_athletes:
                    if nat and nat not in name:
                        return f"{Color.highlight(name)} ({nat})"
                    return Color.highlight(display)
                return display
            return row.get(medal_key, "")

        podium_rows = []
        for p in podiums:
            podium_rows.append(
                [
                    p["year"],
                    p["venue"],
                    format_medalist(p, "gold", "gold_athletes"),
                    format_medalist(p, "silver", "silver_athletes"),
                    format_medalist(p, "bronze", "bronze_athletes"),
                ]
            )

        render_table(
            [
                "Year",
                "Venue",
                Color.gold("Gold"),
                Color.silver("Silver"),
                Color.bronze("Bronze"),
            ],
            podium_rows,
            pretty=pretty,
            column_separators={2},
        )
        print()

    # Section 2: Country medal table (discipline-specific)
    medal_counts: dict[str, dict[str, int]] = {}
    for p in podiums:
        for medal_type, key in [
            ("gold", "gold_nat"),
            ("silver", "silver_nat"),
            ("bronze", "bronze_nat"),
        ]:
            nat = p.get(key) or ""
            if not nat:
                continue
            if nat not in medal_counts:
                medal_counts[nat] = {"gold": 0, "silver": 0, "bronze": 0}
            medal_counts[nat][medal_type] += 1

    if not medal_counts:
        print(
            _format_section_title(
                f"{section_offset + 2}. Country medal table ({cat_name} {disc_name}): none",
                args,
            )
        )
        print()
    else:
        sorted_countries = sorted(
            medal_counts.items(),
            key=lambda x: (x[1]["gold"], x[1]["silver"], x[1]["bronze"]),
            reverse=True,
        )
        print(
            _format_section_title(
                f"{section_offset + 2}. Country medal table ({cat_name} {disc_name}):",
                args,
            )
        )
        medal_rows = []
        for idx, (country, counts) in enumerate(sorted_countries, 1):
            total = counts["gold"] + counts["silver"] + counts["bronze"]
            medal_rows.append(
                [
                    str(idx),
                    _country_display(country),
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
            medal_rows,
            pretty=pretty,
            column_separators={2},
        )
        print()

    # Section 3: Country medal table (all Olympic disciplines)
    if not all_country_medals:
        print(
            _format_section_title(
                f"{section_offset + 3}. Country medal table ({cat_name}, all Olympic disciplines): none",
                args,
            )
        )
        print()
    else:

        def _init_country() -> dict[str, int]:
            return {
                "gold": 0,
                "silver": 0,
                "bronze": 0,
                "gold_ind": 0,
                "silver_ind": 0,
                "bronze_ind": 0,
                "gold_relay": 0,
                "silver_relay": 0,
                "bronze_relay": 0,
            }

        all_country_counts: dict[str, dict[str, int]] = {}
        for m in all_country_medals:
            disc = str(m.get("discipline") or "").upper()
            is_relay_disc = disc in RELAY_DISCIPLINES
            for medal_type in ("gold", "silver", "bronze"):
                nat = m.get(medal_type, "")
                if not nat:
                    continue
                if nat not in all_country_counts:
                    all_country_counts[nat] = _init_country()
                all_country_counts[nat][medal_type] += 1
                suffix = "_relay" if is_relay_disc else "_ind"
                all_country_counts[nat][medal_type + suffix] += 1

        sorted_all_countries = sorted(
            all_country_counts.items(),
            key=lambda x: (x[1]["gold"], x[1]["silver"], x[1]["bronze"]),
            reverse=True,
        )

        print(
            _format_section_title(
                f"{section_offset + 3}. Country medal table ({cat_name}, all Olympic disciplines):",
                args,
            )
        )
        all_country_rows = []
        for idx, (country, counts) in enumerate(sorted_all_countries, 1):
            total = counts["gold"] + counts["silver"] + counts["bronze"]
            total_ind = counts["gold_ind"] + counts["silver_ind"] + counts["bronze_ind"]
            total_relay = (
                counts["gold_relay"] + counts["silver_relay"] + counts["bronze_relay"]
            )
            all_country_rows.append(
                [
                    str(idx),
                    _country_display(country),
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
            all_country_rows,
            pretty=pretty,
            column_separators={2, 6, 10},
            group_headers=[
                (2, 6, "All"),
                (6, 10, "Individual"),
                (10, 14, "Relay"),
            ],
        )
        print()

    # Section 4: Athlete medal table (discipline-specific)
    athlete_counts: dict[str, dict] = {}
    for p in podiums:
        for medal_type, athletes_key in [
            ("gold", "gold_athletes"),
            ("silver", "silver_athletes"),
            ("bronze", "bronze_athletes"),
        ]:
            for athlete in p.get(athletes_key, []):
                if not athlete:
                    continue
                full_name = athlete.get("full_name") or athlete.get("name", "")
                family_name = athlete.get("name", "")
                if not full_name:
                    continue
                if full_name not in athlete_counts:
                    athlete_counts[full_name] = {
                        "family_name": family_name,
                        "nat": athlete.get("nat", ""),
                        "gender": athlete.get("gender", ""),
                        "gold": 0,
                        "silver": 0,
                        "bronze": 0,
                        "races": 0,
                    }
                athlete_counts[full_name][medal_type] += 1
                athlete_counts[full_name]["races"] += 1

    sorted_athletes = sorted(
        (
            (k, v)
            for k, v in athlete_counts.items()
            if v["gold"] > 0
            or (
                v["family_name"] in highlight_athletes
                and (v["gold"] + v["silver"] + v["bronze"]) > 0
            )
        ),
        key=lambda x: (
            x[1]["gold"],
            x[1]["silver"],
            x[1]["bronze"],
            x[1]["gold"] + x[1]["silver"] + x[1]["bronze"],
            x[1]["races"],
        ),
        reverse=True,
    )

    if not sorted_athletes:
        print(
            _format_section_title(
                f"{section_offset + 4}. Athlete medal table ({cat_name} {disc_name}): none",
                args,
            )
        )
        print()
    else:
        print(
            _format_section_title(
                f"{section_offset + 4}. Athlete medal table ({cat_name} {disc_name}):",
                args,
            )
        )
        athlete_rows: list[list[str]] = []
        row_styles: list[str] = []
        for idx, (full_name, counts) in enumerate(sorted_athletes, 1):
            total = counts["gold"] + counts["silver"] + counts["bronze"]
            athlete_rows.append(
                [
                    str(idx),
                    full_name,
                    str(counts["nat"]),
                    str(counts["gender"]),
                    str(counts["gold"]),
                    str(counts["silver"]),
                    str(counts["bronze"]),
                    str(total),
                    str(counts["races"]),
                ]
            )
            if counts["family_name"] in highlight_athletes:
                row_styles.append("highlight")
            else:
                row_styles.append("dim")
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
            athlete_rows,
            pretty=pretty,
            row_styles=row_styles,
            column_separators={4},
        )
        print()

    # Section 5: Athlete medal table (all Olympic disciplines)
    medalists = [
        (key, stats)
        for key, stats in all_athlete_stats.items()
        if stats["gold"] >= 2
        or (
            key in startlist_ids
            and (stats["gold"] + stats["silver"] + stats["bronze"]) > 0
        )
    ]
    medalists.sort(
        key=lambda x: (
            -x[1]["gold"],
            -x[1].get("gold_ind", 0),
            -x[1].get("gold_relay", 0),
            -x[1]["silver"],
            -x[1].get("silver_ind", 0),
            -x[1].get("silver_relay", 0),
            -x[1]["bronze"],
            -x[1].get("bronze_ind", 0),
            -x[1].get("bronze_relay", 0),
            -(x[1]["gold"] + x[1]["silver"] + x[1]["bronze"]),
            -(
                x[1].get("gold_ind", 0)
                + x[1].get("silver_ind", 0)
                + x[1].get("bronze_ind", 0)
            ),
            -(
                x[1].get("gold_relay", 0)
                + x[1].get("silver_relay", 0)
                + x[1].get("bronze_relay", 0)
            ),
            x[1]["races"],
        ),
    )

    if not medalists:
        print(
            _format_section_title(
                f"{section_offset + 5}. Athlete medal table ({cat_name}, all Olympic disciplines): none",
                args,
            )
        )
        print()
    else:
        print(
            _format_section_title(
                f"{section_offset + 5}. Athlete medal table ({cat_name}, all Olympic disciplines):",
                args,
            )
        )
        highlight_ids = startlist_ids
        all_rows = []
        all_row_styles = []
        for idx, (key, stats) in enumerate(medalists, 1):
            gold = stats["gold"]
            silver = stats["silver"]
            bronze = stats["bronze"]
            total = gold + silver + bronze
            races = stats["races"]
            gold_ind = stats.get("gold_ind", 0)
            silver_ind = stats.get("silver_ind", 0)
            bronze_ind = stats.get("bronze_ind", 0)
            total_ind = gold_ind + silver_ind + bronze_ind
            races_ind = stats.get("races_ind", 0)
            gold_relay = stats.get("gold_relay", 0)
            silver_relay = stats.get("silver_relay", 0)
            bronze_relay = stats.get("bronze_relay", 0)
            total_relay = gold_relay + silver_relay + bronze_relay
            races_relay = stats.get("races_relay", 0)
            all_rows.append(
                [
                    str(idx),
                    stats["name"],
                    stats["nat"],
                    stats["gender"],
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
                    str(gold_relay),
                    str(silver_relay),
                    str(bronze_relay),
                    str(total_relay),
                    str(races_relay),
                ]
            )
            if key in highlight_ids:
                all_row_styles.append("highlight")
            else:
                all_row_styles.append("dim")
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
            all_rows,
            pretty=pretty,
            row_styles=all_row_styles,
            column_separators={4, 9, 14},
            group_headers=[(4, 9, "All"), (9, 14, "Individual"), (14, 19, "Relay")],
        )
        print()


def render_startlist_analysis(ctx: dict, args: argparse.Namespace) -> None:
    """Render startlist analysis sections 1-13 (race-specific analysis).

    Olympic individual races render Olympic history sections instead.
    """
    entries = ctx["entries"]
    race_disc = ctx["race_disc"]
    is_relay = ctx["is_relay"]
    discipline_set = ctx["discipline_set"]
    cat_id = ctx["cat_id"]
    season_id = ctx["season_id"]
    venue_name = ctx["venue_name"]
    use_major = ctx["use_major"]
    event_type = ctx.get("event_type", EVENT_TYPE_WC)
    is_major_type = event_type in (EVENT_TYPE_OWG, EVENT_TYPE_WCH)
    location_label = (
        EVENT_TYPE_LABELS.get(event_type, venue_name) if is_major_type else venue_name
    )
    startlist_ids = ctx["startlist_ids"]
    prefetched_results = ctx["prefetched_results"]
    is_mixed = ctx.get("is_mixed", False)
    pretty = is_pretty_output(args)
    milestone_disc_label = (
        "Individual" if race_disc in INDIVIDUAL_EQUIVALENT_DISCIPLINES else race_disc
    )
    total_standings: list[dict] = []
    disc_standings: list[dict] = []
    general_leader_id = ""
    discipline_leader_id = ""
    disc_name = DISCIPLINE_CUP_SUFFIX.get(race_disc, race_disc)
    if race_disc in DISCIPLINES and cat_id in {"SW", "SM"}:
        total_cup_id, disc_cup_id = _get_cup_ids_for_race(season_id, cat_id, race_disc)
        total_standings = (
            _fetch_standings(total_cup_id, limit=10) if total_cup_id else []
        )
        disc_standings = _fetch_standings(disc_cup_id, limit=10) if disc_cup_id else []
        if total_standings:
            general_leader_id = _row_ibu_id(total_standings[0])
        if disc_standings:
            discipline_leader_id = _row_ibu_id(disc_standings[0])

    mark_leaders = bool(getattr(args, "leader_markers", False)) and pretty

    def format_leader_name(name: str, ibu_id: str) -> str:
        suffix = _leader_marker_suffix(
            ibu_id,
            general_leader_id,
            discipline_leader_id,
            mark_leaders,
        )
        return f"{name}{suffix}" if suffix else name

    def format_leader_name_text(name: str, ibu_id: str) -> str:
        raw = format_leader_name(name, ibu_id)
        return _format_leader_markers(raw, 0)

    def leader_name_cell(cell_str: str, row_idx: int) -> str:
        return _format_leader_markers(cell_str, row_idx)

    is_olympic_individual = event_type == EVENT_TYPE_OWG and not is_relay

    _render_wc_standings_sections(
        ctx,
        args,
        total_standings,
        disc_standings,
        disc_name,
        format_leader_name,
        leader_name_cell,
    )

    if is_olympic_individual:
        _render_olympic_individual_sections(ctx, args, section_offset=3)
        return

    # World Cup standings sections (only for individual races, not relays)
    if race_disc in DISCIPLINES and cat_id in {"SW", "SM"}:
        # Section 4: Standings Watch (what-if scenarios)
        scenarios = _compute_what_if_scenarios(
            total_standings,
            disc_standings,
            startlist_ids,
            disc_name,
            name_formatter=format_leader_name_text,
        )
        if scenarios:
            print(_format_section_title("4. Standings Watch:", args))
            for scenario in scenarios[:20]:
                print(f"  - {scenario}")
            print()
        else:
            print(_format_section_title("4. Standings Watch: no close battles", args))
            print()

    elif is_mixed:
        # Section 2: Mixed Relay Cup Standings
        mixed_cup_id = _find_mixed_relay_cup(season_id, race_disc)
        if mixed_cup_id:
            try:
                cup_payload = get_cup_results(mixed_cup_id)
                rows = cup_payload.get("Rows") or cup_payload.get("Results") or []
                if rows:
                    print(
                        _format_section_title(
                            "2. Mixed Relay Cup Standings (Top 10):", args
                        )
                    )
                    table_rows = []
                    for row in rows[:10]:
                        rank = row.get("Rank") or ""
                        name = row.get("Name") or row.get("ShortName") or ""
                        nat = row.get("Nat") or ""
                        score = row.get("Score") or 0
                        table_rows.append(
                            [str(rank).rstrip("."), name, nat, str(score)]
                        )
                    render_table(
                        ["Rank", "Team/Athlete", "Nat", "Points"],
                        table_rows,
                        pretty=pretty,
                    )
                    print()
            except BiathlonError:
                pass

    # Section 4b: Pursuit contenders (start delay < 1 min) - only for pursuit races
    if race_disc == "PU":
        payload = ctx["payload"]
        contenders = []
        for res in payload.get("Results", []) or []:
            if res.get("IsTeam"):
                continue
            start_info = res.get("StartInfo")
            delay_secs = parse_time_seconds(start_info) if start_info else None
            if delay_secs is not None and delay_secs < 60:
                name = res.get("Name") or res.get("ShortName") or ""
                nat = res.get("Nat") or ""
                ibu_id = res.get("IBUId") or res.get("IbuId") or ""
                name = format_leader_name(name, str(ibu_id))
                contenders.append([start_info, name, nat])

        if contenders:
            contenders.sort(key=lambda x: parse_time_seconds(x[0]) or 0)
            print(
                _format_section_title(
                    "4b. Pursuit contenders (start delay < 1 min):", args
                )
            )
            render_table(
                ["Delay", "Athlete", "Nat"],
                contenders,
                pretty=pretty,
                cell_formatters=[None, leader_name_cell, None],
            )
            print()

    race_milestone_rows = []
    win_milestone_rows = []
    disc_race_rows = []
    disc_win_rows = []
    overall_stats_list: list[dict] = []
    individual_stats_list: list[dict] = []
    venue_stats_list: list[dict] = []
    athlete_wc_stats: list[dict] = []

    for entry in entries:
        ibu_id = entry["ibu_id"]
        if not ibu_id:
            continue
        entry_age = entry.get("age", "-")
        entry_gender = _display_gender(entry.get("gender", "")) if is_mixed else ""
        entry_name = format_leader_name(entry["name"], ibu_id)
        all_payload = prefetched_results.get(ibu_id)
        if not all_payload:
            continue
        results = list(all_payload.get("Results") or [])
        wc_results = [
            res for res in results if str(res.get("Level") or "").upper() == "WC"
        ]
        major_results = [
            res
            for res in results
            if str(res.get("Level") or "").upper() in {"WC", "WCH", "OWG"}
        ]
        source_results = major_results if use_major else wc_results

        # Collect venue stats for startlist athletes.
        if is_major_type:
            type_results = [
                res
                for res in source_results
                if str(res.get("Level") or "").upper() == event_type
            ]
            if type_results:
                venue_stats_list.append(_calculate_venue_stats(type_results, entry))
        elif venue_name:
            venue_results = [
                res for res in source_results if _matches_venue(res, venue_name)
            ]
            if venue_results:
                venue_stats_list.append(_calculate_venue_stats(venue_results, entry))
        if source_results:
            overall_stats_list.append(_calculate_venue_stats(source_results, entry))
            individual_results = [
                res
                for res in source_results
                if str(res.get("Comp") or "").upper()
                in INDIVIDUAL_EQUIVALENT_DISCIPLINES
            ]
            if individual_results:
                individual_stats_list.append(
                    _calculate_venue_stats(individual_results, entry)
                )

        wc_races = len(wc_results)
        wc_wins = 0
        wc_individual_races = 0
        wc_individual_wins = 0
        wc_team_races = 0
        wc_team_wins = 0
        major_races = len(major_results)
        major_wins = 0
        major_individual_races = 0
        major_individual_wins = 0
        major_team_races = 0
        major_team_wins = 0
        major_disc_races = {disc: 0 for disc in discipline_set}
        major_disc_wins = {disc: 0 for disc in discipline_set}
        disc_races = {disc: 0 for disc in discipline_set}
        disc_wins = {disc: 0 for disc in discipline_set}
        for res in wc_results:
            rank_val = _parse_rank(res.get("Rank") or res.get("SO"))
            if rank_val == 1:
                wc_wins += 1
            disc = str(res.get("Comp") or "").upper()
            is_team_result = disc in RELAY_DISCIPLINES
            if is_team_result:
                wc_team_races += 1
                if rank_val == 1:
                    wc_team_wins += 1
            else:
                wc_individual_races += 1
                if rank_val == 1:
                    wc_individual_wins += 1
            if is_relay and disc in RELAY_DISCIPLINES:
                disc = race_disc
            if disc in discipline_set:
                disc_races[disc] += 1
                if rank_val == 1:
                    disc_wins[disc] += 1
        athlete_wc_stats.append(
            {
                "name": entry["name"],
                "age": entry_age,
                "nat": entry["nat"],
                "wc_wins": wc_wins,
                "wc_races": wc_races,
            }
        )
        for res in major_results:
            rank_val = _parse_rank(res.get("Rank") or res.get("SO"))
            if rank_val == 1:
                major_wins += 1
            disc = str(res.get("Comp") or "").upper()
            is_team_result = disc in RELAY_DISCIPLINES
            if is_team_result:
                major_team_races += 1
                if rank_val == 1:
                    major_team_wins += 1
            else:
                major_individual_races += 1
                if rank_val == 1:
                    major_individual_wins += 1
            if is_relay and disc in RELAY_DISCIPLINES:
                disc = race_disc
            if disc in discipline_set:
                major_disc_races[disc] += 1
                if rank_val == 1:
                    major_disc_wins[disc] += 1

        race_count = major_races if use_major else wc_races
        win_count = major_wins if use_major else wc_wins
        individual_race_count = (
            major_individual_races if use_major else wc_individual_races
        )
        individual_win_count = (
            major_individual_wins if use_major else wc_individual_wins
        )
        team_race_count = major_team_races if use_major else wc_team_races
        team_win_count = major_team_wins if use_major else wc_team_wins

        # Check overall milestones
        next_race = race_count + 1
        next_win = win_count + 1
        race_milestone = _next_race_milestone(next_race)
        win_milestone = _next_win_milestone(next_win)
        if race_milestone:
            if is_mixed:
                race_milestone_rows.append(
                    [
                        race_milestone,
                        "Race",
                        entry_name,
                        entry_gender,
                        entry_age,
                        entry["nat"],
                        race_count,
                    ]
                )
            else:
                race_milestone_rows.append(
                    [
                        race_milestone,
                        "Race",
                        entry_name,
                        entry_age,
                        entry["nat"],
                        race_count,
                    ]
                )
        if win_milestone:
            if is_mixed:
                win_milestone_rows.append(
                    [
                        win_milestone,
                        "Win",
                        entry_name,
                        entry_gender,
                        entry_age,
                        entry["nat"],
                        win_count,
                    ]
                )
            else:
                win_milestone_rows.append(
                    [
                        win_milestone,
                        "Win",
                        entry_name,
                        entry_age,
                        entry["nat"],
                        win_count,
                    ]
                )

        # Check individual (non-relay) milestones - only for non-relay races
        # Skip if counts are equal (no relay experience, so Individual Race = Race)
        if not is_relay and individual_race_count != race_count:
            next_individual_race = individual_race_count + 1
            individual_race_milestone = _next_race_milestone(next_individual_race)
            if individual_race_milestone:
                if is_mixed:
                    race_milestone_rows.append(
                        [
                            individual_race_milestone,
                            "Individual Race",
                            entry_name,
                            entry_gender,
                            entry_age,
                            entry["nat"],
                            individual_race_count,
                        ]
                    )
                else:
                    race_milestone_rows.append(
                        [
                            individual_race_milestone,
                            "Individual Race",
                            entry_name,
                            entry_age,
                            entry["nat"],
                            individual_race_count,
                        ]
                    )
        if not is_relay and individual_win_count != win_count:
            next_individual_win = individual_win_count + 1
            individual_win_milestone = _next_win_milestone(next_individual_win)
            if individual_win_milestone:
                if is_mixed:
                    win_milestone_rows.append(
                        [
                            individual_win_milestone,
                            "Individual Win",
                            entry_name,
                            entry_gender,
                            entry_age,
                            entry["nat"],
                            individual_win_count,
                        ]
                    )
                else:
                    win_milestone_rows.append(
                        [
                            individual_win_milestone,
                            "Individual Win",
                            entry_name,
                            entry_age,
                            entry["nat"],
                            individual_win_count,
                        ]
                    )

        # Check team (relay) milestones - only for relay races
        # Skip if counts are equal (no individual experience, so Team Race = Race)
        if is_relay and team_race_count != race_count:
            next_team_race = team_race_count + 1
            team_race_milestone = _next_race_milestone(next_team_race)
            if team_race_milestone:
                if is_mixed:
                    race_milestone_rows.append(
                        [
                            team_race_milestone,
                            "Team Race",
                            entry_name,
                            entry_gender,
                            entry_age,
                            entry["nat"],
                            team_race_count,
                        ]
                    )
                else:
                    race_milestone_rows.append(
                        [
                            team_race_milestone,
                            "Team Race",
                            entry_name,
                            entry_age,
                            entry["nat"],
                            team_race_count,
                        ]
                    )
        if is_relay and team_win_count != win_count:
            next_team_win = team_win_count + 1
            team_win_milestone = _next_win_milestone(next_team_win)
            if team_win_milestone:
                if is_mixed:
                    win_milestone_rows.append(
                        [
                            team_win_milestone,
                            "Team Win",
                            entry_name,
                            entry_gender,
                            entry_age,
                            entry["nat"],
                            team_win_count,
                        ]
                    )
                else:
                    win_milestone_rows.append(
                        [
                            team_win_milestone,
                            "Team Win",
                            entry_name,
                            entry_age,
                            entry["nat"],
                            team_win_count,
                        ]
                    )
        # Only check milestones for the current race's discipline
        if race_disc in discipline_set:
            if race_disc in INDIVIDUAL_EQUIVALENT_DISCIPLINES:
                disc_race_count = sum(
                    (major_disc_races[disc] if use_major else disc_races[disc])
                    for disc in INDIVIDUAL_EQUIVALENT_DISCIPLINES
                )
                disc_win_count = sum(
                    (major_disc_wins[disc] if use_major else disc_wins[disc])
                    for disc in INDIVIDUAL_EQUIVALENT_DISCIPLINES
                )
            else:
                disc_race_count = (
                    major_disc_races[race_disc] if use_major else disc_races[race_disc]
                )
                disc_win_count = (
                    major_disc_wins[race_disc] if use_major else disc_wins[race_disc]
                )
            disc_next_race = disc_race_count + 1
            disc_next_win = disc_win_count + 1
            disc_race_milestone = _next_race_milestone(disc_next_race)
            disc_win_milestone = _next_win_milestone(disc_next_win)
            if disc_race_milestone:
                if is_mixed:
                    disc_race_rows.append(
                        [
                            disc_race_milestone,
                            entry_name,
                            entry_gender,
                            entry_age,
                            entry["nat"],
                            disc_race_count,
                        ]
                    )
                else:
                    disc_race_rows.append(
                        [
                            disc_race_milestone,
                            entry_name,
                            entry_age,
                            entry["nat"],
                            disc_race_count,
                        ]
                    )
            if disc_win_milestone:
                if is_mixed:
                    disc_win_rows.append(
                        [
                            disc_win_milestone,
                            entry_name,
                            entry_gender,
                            entry_age,
                            entry["nat"],
                            disc_win_count,
                        ]
                    )
                else:
                    disc_win_rows.append(
                        [
                            disc_win_milestone,
                            entry_name,
                            entry_age,
                            entry["nat"],
                            disc_win_count,
                        ]
                    )

    if race_milestone_rows:
        race_milestone_rows.sort(key=lambda row: row[0], reverse=True)
        header_label = (
            "5. World Cup + WCH + OWG race milestones:"
            if use_major
            else "5. World Cup race milestones:"
        )
        print(_format_section_title(header_label, args))
        if is_mixed:
            render_table(
                [
                    "Milestone",
                    "Type",
                    "Athlete",
                    "Gender",
                    "Age",
                    "Nat",
                    "CurrentRaces",
                ],
                race_milestone_rows,
                pretty=pretty,
                cell_formatters=[None, None, leader_name_cell, None, None, None, None],
            )
        else:
            render_table(
                ["Milestone", "Type", "Athlete", "Age", "Nat", "CurrentRaces"],
                race_milestone_rows,
                pretty=pretty,
                cell_formatters=[None, None, leader_name_cell, None, None, None],
            )
        print()
    else:
        header_label = (
            "5. World Cup + WCH + OWG race milestones: none"
            if use_major
            else "5. World Cup race milestones: none"
        )
        print(_format_section_title(header_label, args))
        print()

    if win_milestone_rows:
        win_milestone_rows.sort(key=lambda row: row[0], reverse=True)
        header_label = (
            "6. World Cup + WCH + OWG win milestones (if they win this race):"
            if use_major
            else "6. World Cup win milestones (if they win this race):"
        )
        print(_format_section_title(header_label, args))
        if is_mixed:
            render_table(
                ["Milestone", "Type", "Athlete", "Gender", "Age", "Nat", "CurrentWins"],
                win_milestone_rows,
                pretty=pretty,
                cell_formatters=[None, None, leader_name_cell, None, None, None, None],
            )
        else:
            render_table(
                ["Milestone", "Type", "Athlete", "Age", "Nat", "CurrentWins"],
                win_milestone_rows,
                pretty=pretty,
                cell_formatters=[None, None, leader_name_cell, None, None, None],
            )
        print()
    else:
        header_label = (
            "6. World Cup + WCH + OWG win milestones: none"
            if use_major
            else "6. World Cup win milestones: none"
        )
        print(_format_section_title(header_label, args))
        print()

    if disc_race_rows:
        disc_race_rows.sort(key=lambda row: row[0], reverse=True)
        header_label = (
            f"7. {milestone_disc_label} race milestones (WC + WCH + OWG):"
            if use_major
            else f"7. {milestone_disc_label} race milestones:"
        )
        print(_format_section_title(header_label, args))
        if is_mixed:
            render_table(
                ["Milestone", "Athlete", "Gender", "Age", "Nat", "CurrentRaces"],
                disc_race_rows,
                pretty=pretty,
                cell_formatters=[None, leader_name_cell, None, None, None, None],
            )
        else:
            render_table(
                ["Milestone", "Athlete", "Age", "Nat", "CurrentRaces"],
                disc_race_rows,
                pretty=pretty,
                cell_formatters=[None, leader_name_cell, None, None, None],
            )
        print()
    else:
        header_label = (
            f"7. {milestone_disc_label} race milestones (WC + WCH + OWG): none"
            if use_major
            else f"7. {milestone_disc_label} race milestones: none"
        )
        print(_format_section_title(header_label, args))
        print()

    if disc_win_rows:
        disc_win_rows.sort(key=lambda row: row[0], reverse=True)
        header_label = (
            f"8. {milestone_disc_label} win milestones (WC + WCH + OWG, if they win this race):"
            if use_major
            else f"8. {milestone_disc_label} win milestones (if they win this race):"
        )
        print(_format_section_title(header_label, args))
        if is_mixed:
            render_table(
                ["Milestone", "Athlete", "Gender", "Age", "Nat", "CurrentWins"],
                disc_win_rows,
                pretty=pretty,
                cell_formatters=[None, leader_name_cell, None, None, None, None],
            )
        else:
            render_table(
                ["Milestone", "Athlete", "Age", "Nat", "CurrentWins"],
                disc_win_rows,
                pretty=pretty,
                cell_formatters=[None, leader_name_cell, None, None, None],
            )
        print()
    else:
        header_label = (
            f"8. {milestone_disc_label} win milestones (WC + WCH + OWG): none"
            if use_major
            else f"8. {milestone_disc_label} win milestones: none"
        )
        print(_format_section_title(header_label, args))
        print()

    # Most decorated athletes from startlist (Individual races)
    if individual_stats_list:
        individual_decorated = [s for s in individual_stats_list if s["wins"] > 0]
        individual_decorated.sort(
            key=lambda s: (s["wins"], s["podiums"], s["flowers"], -s["races"]),
            reverse=True,
        )
        individual_decorated = individual_decorated[:10]
        if len(individual_decorated) < 10:
            podium_only = [
                s for s in individual_stats_list if s["wins"] == 0 and s["podiums"] > 0
            ]
            podium_only.sort(
                key=lambda s: (s["podiums"], s["flowers"], -s["races"]), reverse=True
            )
            individual_decorated.extend(podium_only[: 10 - len(individual_decorated)])
        if len(individual_decorated) < 10:
            flower_only = [
                s
                for s in individual_stats_list
                if s["wins"] == 0 and s["podiums"] == 0 and s["flowers"] > 0
            ]
            flower_only.sort(key=lambda s: (s["flowers"], -s["races"]), reverse=True)
            individual_decorated.extend(flower_only[: 10 - len(individual_decorated)])
        if individual_decorated:
            print(
                _format_section_title(
                    "9. Most decorated athletes from startlist (Individual races):",
                    args,
                )
            )
            individual_rows = []
            for idx, stats in enumerate(individual_decorated, start=1):
                name = format_leader_name(stats["name"], stats.get("ibu_id", ""))
                total = stats["wins"] + stats["podiums"] + stats["flowers"]
                if is_mixed:
                    individual_rows.append(
                        [
                            idx,
                            name,
                            _display_gender(stats.get("gender", "")),
                            stats["age"],
                            stats["nat"],
                            stats["wins"],
                            stats["podiums"],
                            stats["flowers"],
                            total,
                            stats["races"],
                        ]
                    )
                else:
                    individual_rows.append(
                        [
                            idx,
                            name,
                            stats["age"],
                            stats["nat"],
                            stats["wins"],
                            stats["podiums"],
                            stats["flowers"],
                            total,
                            stats["races"],
                        ]
                    )
            if is_mixed:
                render_table(
                    [
                        "#",
                        "Athlete",
                        "Gender",
                        "Age",
                        "Nat",
                        "Wins",
                        "Podiums",
                        "Flowers",
                        "Total",
                        "Races",
                    ],
                    individual_rows,
                    pretty=pretty,
                    cell_formatters=[
                        None,
                        leader_name_cell,
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
            else:
                render_table(
                    [
                        "#",
                        "Athlete",
                        "Age",
                        "Nat",
                        "Wins",
                        "Podiums",
                        "Flowers",
                        "Total",
                        "Races",
                    ],
                    individual_rows,
                    pretty=pretty,
                    cell_formatters=[
                        None,
                        leader_name_cell,
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
        else:
            print(
                _format_section_title(
                    "9. Most decorated athletes from startlist (Individual races): none",
                    args,
                )
            )
            print()
    else:
        print(
            _format_section_title(
                "9. Most decorated athletes from startlist (Individual races): none",
                args,
            )
        )
        print()

    # Most decorated athletes from startlist (all venues)
    if overall_stats_list:
        alltime_decorated = [s for s in overall_stats_list if s["wins"] > 0]
        alltime_decorated.sort(
            key=lambda s: (s["wins"], s["podiums"], s["flowers"], -s["races"]),
            reverse=True,
        )
        alltime_decorated = alltime_decorated[:10]
        if alltime_decorated:
            print(
                _format_section_title(
                    "10. Most decorated athletes from startlist (all venues):", args
                )
            )
            overall_rows = []
            for idx, stats in enumerate(alltime_decorated, start=1):
                name = format_leader_name(stats["name"], stats.get("ibu_id", ""))
                total = stats["wins"] + stats["podiums"] + stats["flowers"]
                if is_mixed:
                    overall_rows.append(
                        [
                            idx,
                            name,
                            _display_gender(stats.get("gender", "")),
                            stats["age"],
                            stats["nat"],
                            stats["wins"],
                            stats["podiums"],
                            stats["flowers"],
                            total,
                            stats["races"],
                        ]
                    )
                else:
                    overall_rows.append(
                        [
                            idx,
                            name,
                            stats["age"],
                            stats["nat"],
                            stats["wins"],
                            stats["podiums"],
                            stats["flowers"],
                            total,
                            stats["races"],
                        ]
                    )
            if is_mixed:
                render_table(
                    [
                        "#",
                        "Athlete",
                        "Gender",
                        "Age",
                        "Nat",
                        "Wins",
                        "Podiums",
                        "Flowers",
                        "Total",
                        "Races",
                    ],
                    overall_rows,
                    pretty=pretty,
                    cell_formatters=[
                        None,
                        leader_name_cell,
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
            else:
                render_table(
                    [
                        "#",
                        "Athlete",
                        "Age",
                        "Nat",
                        "Wins",
                        "Podiums",
                        "Flowers",
                        "Total",
                        "Races",
                    ],
                    overall_rows,
                    pretty=pretty,
                    cell_formatters=[
                        None,
                        leader_name_cell,
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
        else:
            print(
                _format_section_title(
                    "10. Most decorated athletes from startlist (all venues): none",
                    args,
                )
            )
            print()
    else:
        print(
            _format_section_title(
                "10. Most decorated athletes from startlist (all venues): none", args
            )
        )
        print()

    # Most decorated athletes at venue/event type from startlist (at least one win)
    if (venue_name or is_major_type) and venue_stats_list:
        # Filter to athletes with at least one win
        decorated = [s for s in venue_stats_list if s["wins"] > 0]
        # Sort by wins, podiums, flowers (desc), then races (asc - fewer races = better)
        decorated.sort(
            key=lambda s: (s["wins"], s["podiums"], s["flowers"], -s["races"]),
            reverse=True,
        )
        decorated = decorated[:10]
        if decorated:
            venue_rows = []
            for idx, stats in enumerate(decorated, start=1):
                name = format_leader_name(stats["name"], stats.get("ibu_id", ""))
                total = stats["wins"] + stats["podiums"] + stats["flowers"]
                if is_mixed:
                    venue_rows.append(
                        [
                            idx,
                            name,
                            _display_gender(stats.get("gender", "")),
                            stats["age"],
                            stats["nat"],
                            stats["wins"],
                            stats["podiums"],
                            stats["flowers"],
                            total,
                            stats["races"],
                        ]
                    )
                else:
                    venue_rows.append(
                        [
                            idx,
                            name,
                            stats["age"],
                            stats["nat"],
                            stats["wins"],
                            stats["podiums"],
                            stats["flowers"],
                            total,
                            stats["races"],
                        ]
                    )
            print(
                _format_section_title(
                    f"11. Most decorated athletes at {location_label} from startlist:",
                    args,
                )
            )
            if is_mixed:
                render_table(
                    [
                        "#",
                        "Athlete",
                        "Gender",
                        "Age",
                        "Nat",
                        "Wins",
                        "Podiums",
                        "Flowers",
                        "Total",
                        "Races",
                    ],
                    venue_rows,
                    pretty=pretty,
                    cell_formatters=[
                        None,
                        leader_name_cell,
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
            else:
                render_table(
                    [
                        "#",
                        "Athlete",
                        "Age",
                        "Nat",
                        "Wins",
                        "Podiums",
                        "Flowers",
                        "Total",
                        "Races",
                    ],
                    venue_rows,
                    pretty=pretty,
                    cell_formatters=[
                        None,
                        leader_name_cell,
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
        else:
            print(
                _format_section_title(
                    f"11. Most decorated athletes at {location_label} from startlist: none",
                    args,
                )
            )
            print()
    elif venue_name or is_major_type:
        print(
            _format_section_title(
                f"11. Most decorated athletes at {location_label} from startlist: none",
                args,
            )
        )
        print()

    # Most experienced athletes from startlist (all venues) - sorted by races
    if overall_stats_list:
        top_by_races = sorted(
            overall_stats_list, key=lambda s: s["races"], reverse=True
        )
        top_by_races = [s for s in top_by_races if s["races"] > 0][:10]
        if top_by_races:
            print(
                _format_section_title(
                    "12. Most experienced in startlist (all venues):", args
                )
            )
            exp_rows = []
            for idx, stats in enumerate(top_by_races, start=1):
                name = format_leader_name(stats["name"], stats.get("ibu_id", ""))
                if is_mixed:
                    exp_rows.append(
                        [
                            idx,
                            name,
                            _display_gender(stats.get("gender", "")),
                            stats["age"],
                            stats["nat"],
                            stats["races"],
                            stats["wins"],
                            stats["podiums"],
                            stats["flowers"],
                        ]
                    )
                else:
                    exp_rows.append(
                        [
                            idx,
                            name,
                            stats["age"],
                            stats["nat"],
                            stats["races"],
                            stats["wins"],
                            stats["podiums"],
                            stats["flowers"],
                        ]
                    )
            if is_mixed:
                render_table(
                    [
                        "#",
                        "Athlete",
                        "Gender",
                        "Age",
                        "Nat",
                        "Races",
                        "Wins",
                        "Podiums",
                        "Flowers",
                    ],
                    exp_rows,
                    pretty=pretty,
                    cell_formatters=[
                        None,
                        leader_name_cell,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                    ],
                )
            else:
                render_table(
                    [
                        "#",
                        "Athlete",
                        "Age",
                        "Nat",
                        "Races",
                        "Wins",
                        "Podiums",
                        "Flowers",
                    ],
                    exp_rows,
                    pretty=pretty,
                    cell_formatters=[
                        None,
                        leader_name_cell,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                    ],
                )
            print()

    # Most experienced athletes at venue/event type from startlist (races)
    if venue_name or is_major_type:
        top_venue_races = sorted(
            venue_stats_list, key=lambda s: s["races"], reverse=True
        )
        top_venue_races = [s for s in top_venue_races if s["races"] > 0][:10]
        if is_major_type:
            races_label = f"13. Most experienced at {location_label} in startlist:"
        elif use_major:
            races_label = f"13. Most experienced at {location_label} in startlist (WC + WCH + OWG races):"
        else:
            races_label = (
                f"13. Most experienced at {location_label} in startlist (WC races):"
            )
        if top_venue_races:
            print(_format_section_title(races_label, args))
            venue_exp_rows = []
            for idx, stats in enumerate(top_venue_races, start=1):
                name = format_leader_name(stats["name"], stats.get("ibu_id", ""))
                if is_mixed:
                    venue_exp_rows.append(
                        [
                            idx,
                            name,
                            _display_gender(stats.get("gender", "")),
                            stats["age"],
                            stats["nat"],
                            stats["races"],
                            stats["wins"],
                            stats["podiums"],
                            stats["flowers"],
                        ]
                    )
                else:
                    venue_exp_rows.append(
                        [
                            idx,
                            name,
                            stats["age"],
                            stats["nat"],
                            stats["races"],
                            stats["wins"],
                            stats["podiums"],
                            stats["flowers"],
                        ]
                    )
            if is_mixed:
                render_table(
                    [
                        "#",
                        "Athlete",
                        "Gender",
                        "Age",
                        "Nat",
                        "Races",
                        "Wins",
                        "Podiums",
                        "Flowers",
                    ],
                    venue_exp_rows,
                    pretty=pretty,
                    cell_formatters=[
                        None,
                        leader_name_cell,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                    ],
                )
            else:
                render_table(
                    [
                        "#",
                        "Athlete",
                        "Age",
                        "Nat",
                        "Races",
                        "Wins",
                        "Podiums",
                        "Flowers",
                    ],
                    venue_exp_rows,
                    pretty=pretty,
                    cell_formatters=[
                        None,
                        leader_name_cell,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                    ],
                )
            print()
        else:
            print(_format_section_title(f"{races_label} none", args))
            print()


def render_venue_history(
    ctx: dict, args: argparse.Namespace, section_offset: int = 13
) -> None:
    """Render venue history sections (history & records).

    Args:
        ctx: Context dict with venue/race info
        args: Command arguments
        section_offset: Starting section number offset (default 13 for startlist, 0 for brief event)
    """
    race_id = ctx["race_id"]
    age_cache = ctx["age_cache"]
    race_disc = ctx["race_disc"]
    cat_id = ctx["cat_id"]
    venue_name = ctx["venue_name"]
    use_major = ctx["use_major"]
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
                pretty=is_pretty_output(args),
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
                ["Date", "Winner"], venue_winner_rows, pretty=is_pretty_output(args)
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
                    pretty=is_pretty_output(args),
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
                    pretty=is_pretty_output(args),
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
                    pretty=is_pretty_output(args),
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
                pretty=is_pretty_output(args),
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
                    pretty=is_pretty_output(args),
                )
                print()


def handle_startlist(args: argparse.Namespace) -> int:
    """Analyze a startlist for missing WC athletes and milestones."""
    try:
        if args.race:
            race_id = args.race
            payload = get_race_results(race_id)
        else:
            candidates = _find_all_startlist_races()
            race_id, payload = _select_race_interactive(candidates)
    except BiathlonError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if not _is_true(payload.get("IsStartList")):
        print(f"race {race_id} does not have a startlist", file=sys.stderr)
        return 1

    entries = _build_startlist_entries(payload)
    if not entries:
        print(f"no startlist entries found for race {race_id}", file=sys.stderr)
        return 1

    ctx = _prepare_startlist_context(payload, race_id, args)

    print()
    print(_format_section_title(format_race_header(payload, race_id), args))
    print(f"Startlist entries: {len(ctx['entries'])}")
    print()

    render_startlist_analysis(ctx, args)
    render_venue_history(ctx, args)

    return 0
