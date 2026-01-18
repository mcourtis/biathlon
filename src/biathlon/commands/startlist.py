"""Startlist analysis command handler."""

from __future__ import annotations

import argparse
import datetime
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

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
from ..constants import CAT_TO_GENDER
from ..formatting import Color, is_pretty_output, render_table
from ..utils import format_race_header, get_race_start_key, parse_start_datetime, parse_time_seconds
from .results import _get_wc_rows, _row_ibu_id


WC_RACE_MILESTONE_STEP = 25
WC_WIN_MILESTONE_STEP = 5
DISCIPLINES = {"SP", "PU", "IN", "MS"}
RELAY_DISCIPLINES = {"RL", "MR", "SR"}
MAJOR_EVENT_LEVELS = (1, 2, 3)

# IBU World Cup points distribution (positions 1-40)
# Source: IBU Rules 2025, Chapter 3
WC_POINTS = {
    1: 90, 2: 75, 3: 65, 4: 55, 5: 50, 6: 45, 7: 41, 8: 37, 9: 34, 10: 31,
    11: 30, 12: 29, 13: 28, 14: 27, 15: 26, 16: 25, 17: 24, 18: 23, 19: 22, 20: 21,
    21: 20, 22: 19, 23: 18, 24: 17, 25: 16, 26: 15, 27: 14, 28: 13, 29: 12, 30: 11,
    31: 10, 32: 9, 33: 8, 34: 7, 35: 6, 36: 5, 37: 4, 38: 3, 39: 2, 40: 1,
}

# Mapping from discipline code to cup suffix for discipline-specific cups
DISCIPLINE_CUP_SUFFIX = {
    "SP": "Sprint",
    "PU": "Pursuit",
    "IN": "Individual",
    "MS": "Mass Start",
}

MAX_FETCH_WORKERS = 15


def _max_workers(total: int) -> int:
    """Return a capped worker count for concurrent fetches."""
    return min(MAX_FETCH_WORKERS, max(1, total))


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


def _event_levels(use_major: bool) -> tuple[int, ...]:
    return MAJOR_EVENT_LEVELS if use_major else (1,)


def _is_relay_disc(discipline: str) -> bool:
    return discipline in RELAY_DISCIPLINES


def _parse_rank(value: Any) -> int | None:
    text = str(value).strip().rstrip(".")
    if text.isdigit():
        return int(text)
    return None


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


def _format_section_title(text: str, args: argparse.Namespace) -> str:
    if not is_pretty_output(args):
        return text
    return Color.section_title(text)


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
        "name": entry["name"],
        "age": entry.get("age", "-"),
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


def _get_alltime_venue_stats(venue_name: str, cat_id: str, use_major: bool) -> list[dict]:
    """Get all-time venue statistics for all athletes who raced there."""
    if not venue_name:
        return []

    venue_lower = venue_name.strip().lower()
    athlete_stats: dict[str, dict] = {}  # ibu_id -> aggregated stats

    # Get all seasons
    seasons = get_seasons()
    levels = _event_levels(use_major)

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
                venue_match = (
                    _venue_text_matches(venue_lower, organizer)
                    or _venue_text_matches(venue_lower, short_desc)
                )
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

                    disc = str(race.get("DisciplineId") or "").upper()
                    is_relay = _is_relay_disc(disc)

                    # Check category matches (allow mixed relays for gendered stats)
                    race_cat = str(race.get("catId") or race.get("CatId") or "").upper()
                    if cat_id and race_cat != cat_id:
                        if not (is_relay and race_cat == "MX"):
                            continue

                    try:
                        payload = get_race_results(race_id)
                    except BiathlonError:
                        continue

                    # Skip if it's a startlist (no results yet)
                    if _is_true(payload.get("IsStartList")):
                        continue

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

    # Calculate average rank and convert to list
    result = []
    for stats in athlete_stats.values():
        ranks = stats.pop("ranks")
        stats["avg_rank"] = sum(ranks) / len(ranks) if ranks else None
        result.append(stats)

    return result


def _get_team_venue_stats(venue_name: str, cat_id: str, discipline: str, use_major: bool) -> tuple[list[dict], int]:
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
                venue_match = (
                    _venue_text_matches(venue_lower, organizer)
                    or _venue_text_matches(venue_lower, short_desc)
                )
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
        elif disc_id == discipline:
            discipline_cup_id = cup_id

    return total_cup_id, discipline_cup_id


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


def _get_wc_points(position: int) -> int:
    """Look up World Cup points for a finish position."""
    return WC_POINTS.get(position, 0)


def _ordinal(n: int) -> str:
    """Format a number as an ordinal (1st, 2nd, 3rd, etc.)."""
    if 11 <= n % 100 <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _compute_what_if_scenarios(
    total_standings: list[dict],
    disc_standings: list[dict],
    startlist_ids: set[str],
    discipline: str,
) -> list[str]:
    """Generate standing change scenarios for startlist athletes who could take the lead.

    Returns a list of scenario strings describing potential standing changes.
    When both #1 and #2 are racing, calculates overtake scenarios.
    When #1 or #2 is missing, explains why and shows alternative scenarios.
    """
    scenarios = []

    def _ibu_id(row: dict) -> str:
        return row.get("IBUId") or row.get("IbuId") or ""

    def _points(row: dict) -> int:
        return int(row.get("Score") or row.get("Points") or 0)

    def _name(row: dict) -> str:
        return row.get("Name") or row.get("ShortName") or ""

    def _rank(row: dict) -> int:
        rank_val = row.get("Rank") or row.get("Standing") or 0
        return int(str(rank_val).rstrip(".")) if rank_val else 0

    def _find_position_for_points(target_pts: int) -> int | None:
        """Find the finishing position that gives exactly target_pts or less."""
        for pos in range(1, 41):
            if _get_wc_points(pos) <= target_pts:
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

        for finish_pos in range(1, 41):
            chaser_pts = _get_wc_points(finish_pos)
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
            if leader_must_finish is None:
                result.append(
                    f"{prefix}{chaser['name']} takes {_ordinal(leader['rank'])} from "
                    f"{leader['name']} {chaser_finish} if {leader['name']} finishes outside top 40"
                )
            else:
                result.append(
                    f"{prefix}{chaser['name']} takes {_ordinal(leader['rank'])} from "
                    f"{leader['name']} {chaser_finish} if {leader['name']} finishes "
                    f"{_ordinal(leader_must_finish)} or worse"
                )
        return result

    def _compute_gap_info(
        leader: dict, chaser: dict, label: str
    ) -> str:
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
        ranked = []
        for row in standings:
            rank = _rank(row)
            if rank > 0:
                ranked.append({
                    "ibu_id": _ibu_id(row),
                    "name": _name(row),
                    "points": _points(row),
                    "rank": rank,
                })
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
                a for a in ranked
                if a["ibu_id"] in startlist_ids and a["ibu_id"] not in exclude_ids
            ]

        # Case 1: Both racing - show direct scenarios
        if leader_racing and chaser_racing:
            other_contenders = _get_other_contenders({leader["ibu_id"], chaser["ibu_id"]})
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
            other_contenders = _get_other_contenders({leader["ibu_id"], alt_chaser["ibu_id"]})
            alt_scenarios = _compute_overtake_scenarios(
                leader, alt_chaser, label, other_contenders
            )
            if alt_scenarios:
                scenarios.append(f"{prefix}Alternative: {alt_chaser['name']} (#{alt_chaser['rank']}) vs leader:")
                scenarios.extend(alt_scenarios)
            else:
                # No guaranteed scenarios - show gap info
                scenarios.append(f"{prefix}Alternative: {alt_chaser['name']} (#{alt_chaser['rank']}) vs leader:")
                scenarios.append(_compute_gap_info(leader, alt_chaser, label))

    return scenarios


def _render_standings_section(
    title: str,
    standings: list[dict],
    args: argparse.Namespace,
) -> None:
    """Render a standings table."""
    if not standings:
        print(_format_section_title(f"{title}: no data available", args))
        print()
        return

    rows = []
    for idx, row in enumerate(standings):
        rank = row.get("Rank") or row.get("Standing") or idx + 1
        name = row.get("Name") or row.get("ShortName") or ""
        nat = row.get("Nat") or ""
        points = row.get("Score") or row.get("Points") or 0
        rows.append([str(rank).rstrip("."), name, nat, points])

    print(_format_section_title(title, args))
    render_table(
        ["Rank", "Athlete", "Nat", "Points"],
        rows,
        pretty=is_pretty_output(args),
    )
    print()


def _find_all_startlist_races() -> list[tuple[str, dict]]:
    """Find all races with startlists available.

    Returns list of (race_id, payload) tuples sorted by start time (nearest future first,
    then most recent past).
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
    with ThreadPoolExecutor(max_workers=_max_workers(len(active_event_ids))) as executor:
        futures = {executor.submit(get_races, eid): eid for eid in active_event_ids}
        for future in as_completed(futures):
            try:
                all_races.extend(future.result())
            except BiathlonError:
                continue

    # Collect race IDs
    race_ids = [r.get("RaceId") or r.get("Id") for r in all_races if r.get("RaceId") or r.get("Id")]

    if not race_ids:
        raise BiathlonError("No World Cup races with startlists found")

    # Fetch race results in parallel to check for startlists
    race_payloads: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=_max_workers(len(race_ids))) as executor:
        futures = {executor.submit(get_race_results, rid): rid for rid in race_ids}
        for future in as_completed(futures):
            rid = futures[future]
            try:
                race_payloads[rid] = future.result()
            except BiathlonError:
                continue

    # Filter to startlists and sort
    races: list[tuple[datetime.datetime | None, str, dict]] = []
    for rid, payload in race_payloads.items():
        if not _is_true(payload.get("IsStartList")):
            continue
        comp = payload.get("Competition") or {}
        start_raw = comp.get("StartTime") or comp.get("StartDate")
        start_dt = parse_start_datetime(start_raw if isinstance(start_raw, str) else None)
        races.append((start_dt, rid, payload))

    if not races:
        raise BiathlonError("No World Cup races with startlists found")

    # Sort: future races first (by soonest), then past races (by most recent)
    now = datetime.datetime.now(datetime.timezone.utc)
    future_races = [(dt, rid, p) for dt, rid, p in races if dt and dt >= now]
    past_races = [(dt, rid, p) for dt, rid, p in races if not dt or dt < now]

    future_races.sort(key=lambda entry: entry[0])
    past_races.sort(key=lambda entry: entry[0] or datetime.datetime.min, reverse=True)

    return [(rid, p) for _, rid, p in future_races + past_races]


def _select_race_interactive(candidates: list[tuple[str, dict]]) -> tuple[str, dict]:
    """Prompt user to select from multiple races.

    If only one candidate or not a TTY, auto-select the first.
    """
    if len(candidates) == 1:
        return candidates[0]

    # Check if we can prompt (is a TTY)
    if not sys.stdin.isatty():
        # Non-interactive: auto-select and inform user
        race_id, payload = candidates[0]
        print(f"Multiple startlists found, using: {race_id}", file=sys.stderr)
        return candidates[0]

    # Display options
    print("\nMultiple races with startlists found:\n", file=sys.stderr)
    for idx, (race_id, payload) in enumerate(candidates, 1):
        comp = payload.get("Competition") or {}
        sport_evt = payload.get("SportEvt") or {}
        cat = comp.get("catId") or comp.get("CatId") or "?"
        disc = comp.get("DisciplineId") or "?"
        venue = sport_evt.get("Organizer") or sport_evt.get("ShortDescription") or ""
        start = comp.get("StartTime") or ""

        cat_label = {"SW": "Women", "SM": "Men", "MX": "Mixed"}.get(cat, cat)
        print(f"  {idx}. {cat_label}'s {disc} - {venue}", file=sys.stderr)
        print(f"     Start: {start} | ID: {race_id}\n", file=sys.stderr)

    # Get user selection
    while True:
        try:
            choice = input(f"Enter selection (1-{len(candidates)}): ").strip()
            idx = int(choice) - 1
            if 0 <= idx < len(candidates):
                return candidates[idx]
            print("Invalid selection, try again.", file=sys.stderr)
        except ValueError:
            print("Please enter a number.", file=sys.stderr)
        except (EOFError, KeyboardInterrupt):
            raise BiathlonError("Selection cancelled")


def _build_startlist_entries(payload: dict) -> list[dict]:
    entries = []
    for res in payload.get("Results", []) or []:
        if res.get("IsTeam"):
            continue
        ibu_id = res.get("IBUId") or res.get("IbuId") or ""
        name = res.get("Name") or res.get("ShortName") or ""
        nat = res.get("Nat") or ""
        if not ibu_id and not name:
            continue
        entries.append({"ibu_id": str(ibu_id), "name": name, "nat": nat})
    return entries


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
        with ThreadPoolExecutor(max_workers=_max_workers(len(ibu_ids_for_age))) as executor:
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
    season_id = str((payload.get("SportEvt") or {}).get("SeasonId") or "") or get_current_season_id()
    venue_name = _extract_venue_name(payload)
    use_major = bool(getattr(args, "major", False))

    startlist_ids = {entry["ibu_id"] for entry in entries if entry["ibu_id"]}

    # Don't pre-fetch alltime stats - let render_venue_history() fetch on demand
    alltime_stats: list[dict] | None = None

    # Prefetch all athlete results in parallel
    ibu_ids_to_fetch = [e["ibu_id"] for e in entries if e.get("ibu_id")]
    prefetched_results: dict[str, dict | None] = {}
    if ibu_ids_to_fetch:
        with ThreadPoolExecutor(max_workers=_max_workers(len(ibu_ids_to_fetch))) as executor:
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
        "comp": comp,
        "race_disc": race_disc,
        "is_relay": is_relay,
        "discipline_set": discipline_set,
        "cat_id": cat_id,
        "season_id": season_id,
        "venue_name": venue_name,
        "use_major": use_major,
        "startlist_ids": startlist_ids,
        "alltime_stats": alltime_stats,
        "prefetched_results": prefetched_results,
    }


def render_startlist_analysis(ctx: dict, args: argparse.Namespace) -> None:
    """Render startlist analysis sections 1-13 (race-specific analysis)."""
    entries = ctx["entries"]
    age_cache = ctx["age_cache"]
    race_disc = ctx["race_disc"]
    is_relay = ctx["is_relay"]
    discipline_set = ctx["discipline_set"]
    cat_id = ctx["cat_id"]
    season_id = ctx["season_id"]
    venue_name = ctx["venue_name"]
    use_major = ctx["use_major"]
    startlist_ids = ctx["startlist_ids"]
    prefetched_results = ctx["prefetched_results"]

    # Section 1: Missing from top 25 World Cup standings
    missing_rows = []
    if cat_id in {"SW", "SM"}:
        wc_rows = _get_wc_rows(cat_id, season_id)
        top_rows = wc_rows[:25]
        missing = [row for row in top_rows if _row_ibu_id(row) not in startlist_ids]
        if missing:
            for row in missing:
                name = row.get("Name") or row.get("ShortName") or ""
                nat = row.get("Nat") or ""
                rank = row.get("Rank") or ""
                ibu_id = _row_ibu_id(row)
                age_val = _age_for_ibu(ibu_id, age_cache) if ibu_id else "-"
                missing_rows.append([rank, name, age_val, nat])

    if missing_rows:
        print(_format_section_title("1. Missing from top 25 World Cup standings:", args))
        render_table(["Rank", "Name", "Age", "Nat"], missing_rows, pretty=is_pretty_output(args))
        print()
    else:
        print(_format_section_title("1. Missing from top 25 World Cup standings: none", args))
        print()

    # World Cup standings sections (only for individual races, not relays)
    total_standings: list[dict] = []
    disc_standings: list[dict] = []
    if race_disc in DISCIPLINES and cat_id in {"SW", "SM"}:
        total_cup_id, disc_cup_id = _get_cup_ids_for_race(season_id, cat_id, race_disc)

        # Fetch standings (top 5 for display, top 10 for what-if scenarios)
        total_standings = _fetch_standings(total_cup_id, limit=10) if total_cup_id else []
        disc_standings = _fetch_standings(disc_cup_id, limit=10) if disc_cup_id else []

        # Section 2: World Cup Total Standings (Top 5)
        _render_standings_section(
            "2. World Cup Total Standings (Top 5):",
            total_standings[:5],
            args,
        )

        # Section 3: Discipline World Cup Standings (Top 5)
        disc_name = DISCIPLINE_CUP_SUFFIX.get(race_disc, race_disc)
        _render_standings_section(
            f"3. {disc_name} World Cup Standings (Top 5):",
            disc_standings[:5],
            args,
        )

        # Section 4: Standings Watch (what-if scenarios)
        scenarios = _compute_what_if_scenarios(
            total_standings, disc_standings, startlist_ids, disc_name
        )
        if scenarios:
            print(_format_section_title("4. Standings Watch:", args))
            for scenario in scenarios:
                print(f"  - {scenario}")
            print()
        else:
            print(_format_section_title("4. Standings Watch: no close battles", args))
            print()

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
                contenders.append([start_info, name, nat])

        if contenders:
            contenders.sort(key=lambda x: parse_time_seconds(x[0]) or 0)
            print(_format_section_title("4b. Pursuit contenders (start delay < 1 min):", args))
            render_table(["Delay", "Athlete", "Nat"], contenders, pretty=is_pretty_output(args))
            print()

    race_milestone_rows = []
    win_milestone_rows = []
    disc_race_rows = []
    disc_win_rows = []
    overall_stats_list: list[dict] = []
    venue_stats_list: list[dict] = []
    athlete_wc_stats: list[dict] = []

    for entry in entries:
        ibu_id = entry["ibu_id"]
        if not ibu_id:
            continue
        entry_age = entry.get("age", "-")
        all_payload = prefetched_results.get(ibu_id)
        if not all_payload:
            continue
        results = list(all_payload.get("Results") or [])
        wc_results = [res for res in results if str(res.get("Level") or "").upper() == "WC"]
        major_results = [
            res for res in results
            if str(res.get("Level") or "").upper() in {"WC", "WCH", "OWG"}
        ]
        source_results = major_results if use_major else wc_results

        # Collect venue stats for startlist athletes.
        if venue_name:
            venue_results = [res for res in source_results if _matches_venue(res, venue_name)]
            if venue_results:
                stats = _calculate_venue_stats(venue_results, entry)
                venue_stats_list.append(stats)
        if source_results:
            overall_stats_list.append(_calculate_venue_stats(source_results, entry))

        wc_races = len(wc_results)
        wc_wins = 0
        major_races = len(major_results)
        major_wins = 0
        major_disc_races = {disc: 0 for disc in discipline_set}
        major_disc_wins = {disc: 0 for disc in discipline_set}
        disc_races = {disc: 0 for disc in discipline_set}
        disc_wins = {disc: 0 for disc in discipline_set}
        for res in wc_results:
            rank_val = _parse_rank(res.get("Rank") or res.get("SO"))
            if rank_val == 1:
                wc_wins += 1
            disc = str(res.get("Comp") or "").upper()
            if is_relay and disc in RELAY_DISCIPLINES:
                disc = race_disc
            if disc in discipline_set:
                disc_races[disc] += 1
                if rank_val == 1:
                    disc_wins[disc] += 1
        athlete_wc_stats.append({
            "name": entry["name"],
            "age": entry_age,
            "nat": entry["nat"],
            "wc_wins": wc_wins,
            "wc_races": wc_races,
        })
        for res in major_results:
            rank_val = _parse_rank(res.get("Rank") or res.get("SO"))
            if rank_val == 1:
                major_wins += 1
            disc = str(res.get("Comp") or "").upper()
            if is_relay and disc in RELAY_DISCIPLINES:
                disc = race_disc
            if disc in discipline_set:
                major_disc_races[disc] += 1
                if rank_val == 1:
                    major_disc_wins[disc] += 1

        race_count = major_races if use_major else wc_races
        win_count = major_wins if use_major else wc_wins
        next_race = race_count + 1
        next_win = win_count + 1
        race_milestone = _next_race_milestone(next_race)
        win_milestone = _next_win_milestone(next_win)
        if race_milestone:
            race_milestone_rows.append([race_milestone, entry["name"], entry_age, entry["nat"], race_count])
        if win_milestone:
            win_milestone_rows.append([win_milestone, entry["name"], entry_age, entry["nat"], win_count])
        # Only check milestones for the current race's discipline
        if race_disc in discipline_set:
            disc_race_count = major_disc_races[race_disc] if use_major else disc_races[race_disc]
            disc_win_count = major_disc_wins[race_disc] if use_major else disc_wins[race_disc]
            disc_next_race = disc_race_count + 1
            disc_next_win = disc_win_count + 1
            disc_race_milestone = _next_race_milestone(disc_next_race)
            disc_win_milestone = _next_win_milestone(disc_next_win)
            if disc_race_milestone:
                disc_race_rows.append([disc_race_milestone, entry["name"], entry_age, entry["nat"], disc_race_count])
            if disc_win_milestone:
                disc_win_rows.append([disc_win_milestone, entry["name"], entry_age, entry["nat"], disc_win_count])

    if race_milestone_rows:
        race_milestone_rows.sort(key=lambda row: row[0], reverse=True)
        header_label = "5. World Cup + WCH + OWG race milestones:" if use_major else "5. World Cup race milestones:"
        print(_format_section_title(header_label, args))
        render_table(
            ["Milestone", "Athlete", "Age", "Nat", "CurrentRaces"],
            race_milestone_rows,
            pretty=is_pretty_output(args),
        )
        print()
    else:
        header_label = "5. World Cup + WCH + OWG race milestones: none" if use_major else "5. World Cup race milestones: none"
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
        render_table(
            ["Milestone", "Athlete", "Age", "Nat", "CurrentWins"],
            win_milestone_rows,
            pretty=is_pretty_output(args),
        )
        print()
    else:
        header_label = "6. World Cup + WCH + OWG win milestones: none" if use_major else "6. World Cup win milestones: none"
        print(_format_section_title(header_label, args))
        print()

    if disc_race_rows:
        disc_race_rows.sort(key=lambda row: row[0], reverse=True)
        header_label = (
            f"7. {race_disc} race milestones (WC + WCH + OWG):"
            if use_major
            else f"7. {race_disc} race milestones:"
        )
        print(_format_section_title(header_label, args))
        render_table(
            ["Milestone", "Athlete", "Age", "Nat", "CurrentRaces"],
            disc_race_rows,
            pretty=is_pretty_output(args),
        )
        print()
    else:
        header_label = (
            f"7. {race_disc} race milestones (WC + WCH + OWG): none"
            if use_major
            else f"7. {race_disc} race milestones: none"
        )
        print(_format_section_title(header_label, args))
        print()

    if disc_win_rows:
        disc_win_rows.sort(key=lambda row: row[0], reverse=True)
        header_label = (
            f"8. {race_disc} win milestones (WC + WCH + OWG, if they win this race):"
            if use_major
            else f"8. {race_disc} win milestones (if they win this race):"
        )
        print(_format_section_title(header_label, args))
        render_table(
            ["Milestone", "Athlete", "Age", "Nat", "CurrentWins"],
            disc_win_rows,
            pretty=is_pretty_output(args),
        )
        print()
    else:
        header_label = (
            f"8. {race_disc} win milestones (WC + WCH + OWG): none"
            if use_major
            else f"8. {race_disc} win milestones: none"
        )
        print(_format_section_title(header_label, args))
        print()

    # Top 6 athletes by WC wins
    top_by_wins = sorted(athlete_wc_stats, key=lambda x: x["wc_wins"], reverse=True)[:6]
    if top_by_wins and top_by_wins[0]["wc_wins"] > 0:
        print(_format_section_title("9. Top World Cup winners in startlist:", args))
        wins_rows = [[s["name"], s["age"], s["nat"], s["wc_wins"]] for s in top_by_wins if s["wc_wins"] > 0]
        render_table(["Athlete", "Age", "Nat", "WCWins"], wins_rows, pretty=is_pretty_output(args))
        print()

    # Top 6 athletes by WC races
    top_by_races = sorted(athlete_wc_stats, key=lambda x: x["wc_races"], reverse=True)[:6]
    if top_by_races and top_by_races[0]["wc_races"] > 0:
        print(_format_section_title("10. Most experienced in startlist (WC races):", args))
        races_rows = [[s["name"], s["age"], s["nat"], s["wc_races"]] for s in top_by_races if s["wc_races"] > 0]
        render_table(["Athlete", "Age", "Nat", "WCRaces"], races_rows, pretty=is_pretty_output(args))
        print()

    # Most experienced athletes at venue from startlist (races)
    if venue_name:
        top_venue_races = sorted(venue_stats_list, key=lambda s: s["races"], reverse=True)
        top_venue_races = [s for s in top_venue_races if s["races"] > 0][:6]
        races_label = (
            f"11. Most experienced at {venue_name} in startlist (WC + WCH + OWG races):"
            if use_major
            else f"11. Most experienced at {venue_name} in startlist (WC races):"
        )
        if top_venue_races:
            print(_format_section_title(races_label, args))
            races_rows = [[s["name"], s["age"], s["nat"], s["races"]] for s in top_venue_races]
            render_table(["Athlete", "Age", "Nat", "Races"], races_rows, pretty=is_pretty_output(args))
            print()
        else:
            print(_format_section_title(f"{races_label} none", args))
            print()

    # Most decorated athletes at venue from startlist (at least one win)
    if venue_name and venue_stats_list:
        # Filter to athletes with at least one win
        decorated = [s for s in venue_stats_list if s["wins"] > 0]
        # Sort by wins, podiums, flowers (desc), then races (asc - fewer races = better)
        decorated.sort(key=lambda s: (s["wins"], s["podiums"], s["flowers"], -s["races"]), reverse=True)
        decorated = decorated[:20]
        if decorated:
            venue_rows = []
            for idx, stats in enumerate(decorated, start=1):
                venue_rows.append([
                    idx,
                    stats["name"],
                    stats["age"],
                    stats["nat"],
                    stats["wins"],
                    stats["podiums"],
                    stats["flowers"],
                    stats["races"],
                ])
            print(_format_section_title(f"12. Most decorated athletes at {venue_name} from startlist:", args))
            render_table(
                ["#", "Athlete", "Age", "Nat", "Wins", "Podiums", "Flowers", "Races"],
                venue_rows,
                pretty=is_pretty_output(args),
            )
            print()
        else:
            print(_format_section_title(f"12. Most decorated athletes at {venue_name} from startlist: none", args))
            print()
    elif venue_name:
        print(_format_section_title(f"12. Most decorated athletes at {venue_name} from startlist: none", args))
        print()

    if overall_stats_list:
        alltime_decorated = [s for s in overall_stats_list if s["wins"] > 0]
        alltime_decorated.sort(key=lambda s: (s["wins"], s["podiums"], s["flowers"], -s["races"]), reverse=True)
        alltime_decorated = alltime_decorated[:20]
        if alltime_decorated:
            print(_format_section_title("13. Most decorated athletes from startlist (all venues):", args))
            overall_rows = []
            for idx, stats in enumerate(alltime_decorated, start=1):
                overall_rows.append([
                    idx,
                    stats["name"],
                    stats["age"],
                    stats["nat"],
                    stats["wins"],
                    stats["podiums"],
                    stats["flowers"],
                    stats["races"],
                ])
            render_table(
                ["#", "Athlete", "Age", "Nat", "Wins", "Podiums", "Flowers", "Races"],
                overall_rows,
                pretty=is_pretty_output(args),
            )
            print()
        else:
            print(_format_section_title("13. Most decorated athletes from startlist (all venues): none", args))
            print()
    else:
        print(_format_section_title("13. Most decorated athletes from startlist (all venues): none", args))
        print()


def render_venue_history(ctx: dict, args: argparse.Namespace) -> None:
    """Render venue history sections 14-20 (history & records)."""
    race_id = ctx["race_id"]
    age_cache = ctx["age_cache"]
    race_disc = ctx["race_disc"]
    cat_id = ctx["cat_id"]
    venue_name = ctx["venue_name"]
    use_major = ctx["use_major"]
    startlist_ids = ctx["startlist_ids"]
    alltime_stats = ctx["alltime_stats"]

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
                    location = event.get("Organizer") or event.get("ShortDescription") or ""
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
                        race_cat = str(race.get("catId") or race.get("CatId") or "").upper()
                        if cat_id and race_cat != cat_id:
                            continue
                        start_raw = race.get("StartTime") or race.get("StartDate") or ""
                        race_date = start_raw.split("T", 1)[0] if isinstance(start_raw, str) else ""
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
                        if winner_ibu in startlist_ids:
                            winner = Color.highlight(winner_text)
                        else:
                            winner = winner_text
                        break
                if winner:
                    recent_rows.append([race_date, location, winner])
        if recent_rows:
            print(_format_section_title(f"14. Last 5 {race_disc} winners:", args))
            render_table(["Date", "Location", "Winner"], recent_rows, pretty=is_pretty_output(args))
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
            season_races: list[tuple[str, str]] = []  # (date, race_id)
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
                        race_cat = str(race.get("catId") or race.get("CatId") or "").upper()
                        if cat_id and race_cat != cat_id:
                            continue
                        start_raw = race.get("StartTime") or race.get("StartDate") or ""
                        race_date = start_raw.split("T", 1)[0] if isinstance(start_raw, str) else ""
                        season_races.append((race_date, race_id_check))
            # Sort this season's races by date descending
            season_races.sort(key=lambda x: x[0], reverse=True)
            for race_date, past_race_id in season_races:
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
                        if winner_ibu in startlist_ids:
                            winner = Color.highlight(winner_text)
                        else:
                            winner = winner_text
                        break
                if winner:
                    venue_winner_rows.append([race_date, winner])
        if venue_winner_rows:
            print(_format_section_title(f"15. Last 5 {race_disc} winners at {venue_name}:", args))
            render_table(["Date", "Winner"], venue_winner_rows, pretty=is_pretty_output(args))
            print()

    # All-time venue stats (all athletes in history)
    if venue_name and cat_id in {"SW", "SM", "MX"}:
        if alltime_stats is None:
            alltime_stats = _get_alltime_venue_stats(venue_name, cat_id, use_major)
        if alltime_stats:
            # Top 5 winners at venue
            top_venue_winners = sorted(alltime_stats, key=lambda x: x["wins"], reverse=True)[:5]
            top_venue_winners = [s for s in top_venue_winners if s["wins"] > 0]
            if top_venue_winners:
                print(_format_section_title(f"16. Top 5 winners at {venue_name}:", args))
                venue_win_rows = []
                startlist_rows_11 = set()
                for idx, s in enumerate(top_venue_winners):
                    if s.get("ibu_id", "") in startlist_ids:
                        startlist_rows_11.add(idx)
                    venue_win_rows.append([s["name"], s["wins"]])

                def hl_11(cell_str: str, row_idx: int) -> str:
                    return Color.highlight(cell_str) if row_idx in startlist_rows_11 else cell_str

                render_table(["Athlete", "Wins"], venue_win_rows, pretty=is_pretty_output(args),
                             cell_formatters=[hl_11, None])
                print()

            # Top 5 by races at venue
            top_venue_races = sorted(alltime_stats, key=lambda x: x["races"], reverse=True)[:5]
            top_venue_races = [s for s in top_venue_races if s["races"] > 0]
            if top_venue_races:
                print(_format_section_title(f"17. Top 5 most races at {venue_name}:", args))
                venue_race_rows = []
                startlist_rows_12 = set()
                for idx, s in enumerate(top_venue_races):
                    if s.get("ibu_id", "") in startlist_ids:
                        startlist_rows_12.add(idx)
                    age_val = _age_for_ibu(s.get("ibu_id", ""), age_cache)
                    venue_race_rows.append([s["name"], age_val, s["nat"], s["races"]])

                def hl_12(cell_str: str, row_idx: int) -> str:
                    return Color.highlight(cell_str) if row_idx in startlist_rows_12 else cell_str

                render_table(
                    ["Athlete", "Age", "Nat", "Races"],
                    venue_race_rows,
                    pretty=is_pretty_output(args),
                    cell_formatters=[hl_12, None, None, None],
                )
                print()

            # Venue history for all athletes (like section 8 but not limited to startlist)
            # Filter to athletes with at least one win, sort by wins, podiums, flowers (desc), races (asc)
            alltime_decorated = [s for s in alltime_stats if s["wins"] > 0]
            alltime_decorated.sort(key=lambda s: (s["wins"], s["podiums"], s["flowers"], -s["races"]), reverse=True)
            alltime_decorated = alltime_decorated[:20]
            if alltime_decorated:
                print(_format_section_title(f"18. Venue history at {venue_name} (all athletes):", args))
                alltime_venue_rows = []
                startlist_row_indices = set()
                for idx, stats in enumerate(alltime_decorated):
                    if stats.get("ibu_id", "") in startlist_ids:
                        startlist_row_indices.add(idx)
                    alltime_venue_rows.append([
                        idx + 1,
                        stats["name"],
                        stats["wins"],
                        stats["podiums"],
                        stats["flowers"],
                        stats["races"],
                    ])

                def highlight_athlete(cell_str: str, row_idx: int) -> str:
                    if row_idx in startlist_row_indices:
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
        team_stats, total_races = _get_team_venue_stats(venue_name, cat_id, race_disc, use_major)
        if team_stats:
            # Team venue history
            team_stats.sort(key=lambda s: (s["wins"], s["podiums"], s["races"]), reverse=True)
            team_rows = []
            for stats in team_stats[:10]:
                team_rows.append([
                    stats["name"],
                    stats["races"],
                    stats["wins"],
                    stats["podiums"],
                    stats["flowers"],
                ])
            print(_format_section_title(f"19. Team venue history at {venue_name} ({total_races} races in history):", args))
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
                team_records_rows.append(["Most wins", team_records["wins"][1], team_records["wins"][0]])
            if team_records["podiums"][0] > 0:
                team_records_rows.append(["Most podiums", team_records["podiums"][1], team_records["podiums"][0]])
            if team_records["flowers"][0] > 0:
                team_records_rows.append(["Most flowers", team_records["flowers"][1], team_records["flowers"][0]])
            if team_records["races"][0] > 0:
                team_records_rows.append(["Most participations", team_records["races"][1], team_records["races"][0]])
            if team_records_rows:
                print(_format_section_title(f"20. Team venue records at {venue_name} (all teams in history):", args))
                render_table(["Category", "Team", "Count"], team_records_rows, pretty=is_pretty_output(args))
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
