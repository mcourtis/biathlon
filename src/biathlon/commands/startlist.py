"""Startlist analysis command handler."""

from __future__ import annotations

import argparse
import datetime
import sys
from typing import Any

from ..api import (
    BiathlonError,
    get_all_results,
    get_athlete_bio,
    get_current_season_id,
    get_events,
    get_race_results,
    get_races,
    get_seasons,
)
from ..formatting import Color, is_pretty_output, render_table
from ..utils import format_race_header, get_race_start_key, parse_start_datetime
from .results import _get_wc_rows, _row_ibu_id


WC_RACE_MILESTONE_STEP = 25
WC_WIN_MILESTONE_STEP = 5
DISCIPLINES = {"SP", "PU", "IN", "MS"}
RELAY_DISCIPLINES = {"RL", "MR", "SR"}
MAJOR_EVENT_LEVELS = (1, 2, 3)


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


def _find_latest_startlist_race() -> tuple[str, dict]:
    season_id = get_current_season_id()
    events = get_events(season_id, level=1)
    races: list[tuple[datetime.datetime | None, str]] = []
    for event in events:
        event_id = event.get("EventId")
        if not event_id:
            continue
        for race in get_races(event_id):
            race_id = race.get("RaceId") or race.get("Id") or ""
            if not race_id:
                continue
            try:
                payload = get_race_results(race_id)
            except BiathlonError:
                continue
            if not _is_true(payload.get("IsStartList")):
                continue
            comp = payload.get("Competition") or {}
            start_raw = comp.get("StartTime") or comp.get("StartDate") or race.get("StartTime") or race.get("StartDate")
            start_dt = parse_start_datetime(start_raw if isinstance(start_raw, str) else None)
            races.append((start_dt, race_id))

    if not races:
        raise BiathlonError("No World Cup races with startlists found")

    now = datetime.datetime.utcnow()
    future_races = [entry for entry in races if entry[0] and entry[0] >= now]
    if future_races:
        future_races.sort(key=lambda entry: entry[0])
        race_id = future_races[0][1]
    else:
        races.sort(key=lambda entry: entry[0] or datetime.datetime.min, reverse=True)
        race_id = races[0][1]

    payload = get_race_results(race_id)
    return race_id, payload


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


def handle_startlist(args: argparse.Namespace) -> int:
    """Analyze a startlist for missing WC athletes and milestones."""
    try:
        if args.race:
            race_id = args.race
            payload = get_race_results(race_id)
        else:
            race_id, payload = _find_latest_startlist_race()
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

    age_cache: dict[str, str] = {}
    for entry in entries:
        ibu_id = entry.get("ibu_id", "")
        entry["age"] = _age_for_ibu(ibu_id, age_cache) if ibu_id else "-"

    comp = payload.get("Competition") or {}
    race_disc = str(comp.get("DisciplineId") or "").upper()
    is_relay_disc = _is_relay_disc(race_disc)
    discipline_set = {race_disc} if is_relay_disc else DISCIPLINES
    cat_id = str(comp.get("catId") or comp.get("CatId") or "").upper()
    season_id = str((payload.get("SportEvt") or {}).get("SeasonId") or "") or get_current_season_id()
    venue_name = _extract_venue_name(payload)

    print()
    print(_format_section_title(format_race_header(payload, race_id), args))
    print(f"Startlist entries: {len(entries)}")
    print()

    startlist_ids = {entry["ibu_id"] for entry in entries if entry["ibu_id"]}

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

    use_major = bool(getattr(args, "major", False))
    race_milestone_rows = []
    win_milestone_rows = []
    disc_race_rows = []
    disc_win_rows = []
    overall_stats_list: list[dict] = []
    venue_stats_list: list[dict] = []
    athlete_wc_stats: list[dict] = []  # For top wins/races sections
    alltime_stats: list[dict] | None = None
    if venue_name and cat_id in {"SW", "SM", "MX"}:
        alltime_stats = _get_alltime_venue_stats(venue_name, cat_id, use_major)
    for entry in entries:
        ibu_id = entry["ibu_id"]
        if not ibu_id:
            continue
        entry_age = entry.get("age", "-")
        try:
            all_payload = get_all_results(ibu_id)
        except BiathlonError:
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
            if is_relay_disc and disc in RELAY_DISCIPLINES:
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
            if is_relay_disc and disc in RELAY_DISCIPLINES:
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
        header_label = "2. World Cup + WCH + OWG race milestones:" if use_major else "2. World Cup race milestones:"
        print(_format_section_title(header_label, args))
        render_table(
            ["Milestone", "Athlete", "Age", "Nat", "CurrentRaces"],
            race_milestone_rows,
            pretty=is_pretty_output(args),
        )
        print()
    else:
        header_label = "2. World Cup + WCH + OWG race milestones: none" if use_major else "2. World Cup race milestones: none"
        print(_format_section_title(header_label, args))
        print()

    if win_milestone_rows:
        win_milestone_rows.sort(key=lambda row: row[0], reverse=True)
        header_label = (
            "3. World Cup + WCH + OWG win milestones (if they win this race):"
            if use_major
            else "3. World Cup win milestones (if they win this race):"
        )
        print(_format_section_title(header_label, args))
        render_table(
            ["Milestone", "Athlete", "Age", "Nat", "CurrentWins"],
            win_milestone_rows,
            pretty=is_pretty_output(args),
        )
        print()
    else:
        header_label = "3. World Cup + WCH + OWG win milestones: none" if use_major else "3. World Cup win milestones: none"
        print(_format_section_title(header_label, args))
        print()

    if disc_race_rows:
        disc_race_rows.sort(key=lambda row: row[0], reverse=True)
        header_label = (
            f"4. {race_disc} race milestones (WC + WCH + OWG):"
            if use_major
            else f"4. {race_disc} race milestones:"
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
            f"4. {race_disc} race milestones (WC + WCH + OWG): none"
            if use_major
            else f"4. {race_disc} race milestones: none"
        )
        print(_format_section_title(header_label, args))
        print()

    if disc_win_rows:
        disc_win_rows.sort(key=lambda row: row[0], reverse=True)
        header_label = (
            f"5. {race_disc} win milestones (WC + WCH + OWG, if they win this race):"
            if use_major
            else f"5. {race_disc} win milestones (if they win this race):"
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
            f"5. {race_disc} win milestones (WC + WCH + OWG): none"
            if use_major
            else f"5. {race_disc} win milestones: none"
        )
        print(_format_section_title(header_label, args))
        print()

    # Top 6 athletes by WC wins
    top_by_wins = sorted(athlete_wc_stats, key=lambda x: x["wc_wins"], reverse=True)[:6]
    if top_by_wins and top_by_wins[0]["wc_wins"] > 0:
        print(_format_section_title("6. Top World Cup winners in startlist:", args))
        wins_rows = [[s["name"], s["age"], s["nat"], s["wc_wins"]] for s in top_by_wins if s["wc_wins"] > 0]
        render_table(["Athlete", "Age", "Nat", "WCWins"], wins_rows, pretty=is_pretty_output(args))
        print()

    # Top 6 athletes by WC races
    top_by_races = sorted(athlete_wc_stats, key=lambda x: x["wc_races"], reverse=True)[:6]
    if top_by_races and top_by_races[0]["wc_races"] > 0:
        print(_format_section_title("7. Most experienced in startlist (WC races):", args))
        races_rows = [[s["name"], s["age"], s["nat"], s["wc_races"]] for s in top_by_races if s["wc_races"] > 0]
        render_table(["Athlete", "Age", "Nat", "WCRaces"], races_rows, pretty=is_pretty_output(args))
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
            print(_format_section_title(f"8. Most decorated athletes at {venue_name} from startlist:", args))
            render_table(
                ["#", "Athlete", "Age", "Nat", "Wins", "Podiums", "Flowers", "Races"],
                venue_rows,
                pretty=is_pretty_output(args),
            )
            print()
        else:
            print(_format_section_title(f"8. Most decorated athletes at {venue_name} from startlist: none", args))
            print()
    elif venue_name:
        print(_format_section_title(f"8. Most decorated athletes at {venue_name} from startlist: none", args))
        print()

    if venue_name:
        top_venue_races = sorted(venue_stats_list, key=lambda s: s["races"], reverse=True)
        top_venue_races = [s for s in top_venue_races if s["races"] > 0][:6]
        races_label = (
            f"9. Most experienced at {venue_name} in startlist (WC + WCH + OWG races):"
            if use_major
            else f"9. Most experienced at {venue_name} in startlist (WC races):"
        )
        if top_venue_races:
            print(_format_section_title(races_label, args))
            races_rows = [[s["name"], s["age"], s["nat"], s["races"]] for s in top_venue_races]
            render_table(["Athlete", "Age", "Nat", "Races"], races_rows, pretty=is_pretty_output(args))
            print()
        else:
            print(_format_section_title(f"{races_label} none", args))
            print()

    if overall_stats_list:
        alltime_decorated = [s for s in overall_stats_list if s["wins"] > 0]
        alltime_decorated.sort(key=lambda s: (s["wins"], s["podiums"], s["flowers"], -s["races"]), reverse=True)
        alltime_decorated = alltime_decorated[:20]
        if alltime_decorated:
            print(_format_section_title("10. Most decorated athletes from startlist (all venues):", args))
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
            print(_format_section_title("10. Most decorated athletes from startlist (all venues): none", args))
            print()
    else:
        print(_format_section_title("10. Most decorated athletes from startlist (all venues): none", args))
        print()

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
            print(_format_section_title(f"11. Last 5 {race_disc} winners:", args))
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
            print(_format_section_title(f"12. Last 5 {race_disc} winners at {venue_name}:", args))
            render_table(["Date", "Winner"], venue_winner_rows, pretty=is_pretty_output(args))
            print()

    # All-time venue stats (all athletes in history)
    if venue_name and cat_id in {"SW", "SM", "MX"}:
        if alltime_stats is None:
            alltime_stats = _get_alltime_venue_stats(venue_name, cat_id, use_major)
        if alltime_stats:
            # Helper to highlight name if in startlist
            def highlight_if_startlist(name: str, ibu_id: str) -> str:
                if ibu_id in startlist_ids:
                    return Color.highlight(name)
                return name

            # Top 5 winners at venue
            top_venue_winners = sorted(alltime_stats, key=lambda x: x["wins"], reverse=True)[:5]
            top_venue_winners = [s for s in top_venue_winners if s["wins"] > 0]
            if top_venue_winners:
                print(_format_section_title(f"13. Top 5 winners at {venue_name}:", args))
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
                print(_format_section_title(f"14. Top 5 most races at {venue_name}:", args))
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
                print(_format_section_title(f"15. Venue history at {venue_name} (all athletes):", args))
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
            print(_format_section_title(f"16. Team venue history at {venue_name} ({total_races} races in history):", args))
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
                print(_format_section_title(f"17. Team venue records at {venue_name} (all teams in history):", args))
                render_table(["Category", "Team", "Count"], team_records_rows, pretty=is_pretty_output(args))
                print()

    return 0
