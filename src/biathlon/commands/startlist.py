"""Startlist analysis command handler."""

from __future__ import annotations

import argparse
import datetime
import sys
from typing import Any

from ..api import BiathlonError, get_all_results, get_current_season_id, get_events, get_race_results, get_races
from ..formatting import is_pretty_output, render_table
from ..utils import format_race_header, get_race_start_key, parse_start_datetime
from .results import _get_wc_rows, _row_ibu_id


WC_RACE_MILESTONE_STEP = 25
WC_WIN_MILESTONE_STEP = 5
DISCIPLINES = {"SP", "PU", "IN", "MS"}
RELAY_DISCIPLINES = {"RL", "SR"}


def _is_true(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return False


def _parse_rank(value: Any) -> int | None:
    text = str(value).strip().rstrip(".")
    if text.isdigit():
        return int(text)
    return None


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

    comp = payload.get("Competition") or {}
    race_disc = str(comp.get("DisciplineId") or "").upper()
    if race_disc in RELAY_DISCIPLINES:
        discipline_set = {race_disc}
    else:
        discipline_set = DISCIPLINES
    cat_id = str(comp.get("catId") or comp.get("CatId") or "").upper()
    season_id = str((payload.get("SportEvt") or {}).get("SeasonId") or "") or get_current_season_id()

    print()
    print(format_race_header(payload, race_id))
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
                missing_rows.append([rank, name, nat])

    if missing_rows:
        print("Missing from top 25 World Cup standings:")
        render_table(["Rank", "Name", "Nat"], missing_rows, pretty=is_pretty_output(args))
        print()
    else:
        print("Missing from top 25 World Cup standings: none")
        print()

    use_major = bool(getattr(args, "major", False))
    race_milestone_rows = []
    win_milestone_rows = []
    disc_race_rows = []
    disc_win_rows = []
    for entry in entries:
        ibu_id = entry["ibu_id"]
        if not ibu_id:
            continue
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
            if disc in discipline_set:
                disc_races[disc] += 1
                if rank_val == 1:
                    disc_wins[disc] += 1
        for res in major_results:
            rank_val = _parse_rank(res.get("Rank") or res.get("SO"))
            if rank_val == 1:
                major_wins += 1
            disc = str(res.get("Comp") or "").upper()
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
            race_milestone_rows.append([race_milestone, entry["name"], entry["nat"], race_count])
        if win_milestone:
            win_milestone_rows.append([win_milestone, entry["name"], entry["nat"], win_count])
        for disc in discipline_set:
            disc_race_count = major_disc_races[disc] if use_major else disc_races[disc]
            disc_win_count = major_disc_wins[disc] if use_major else disc_wins[disc]
            disc_next_race = disc_race_count + 1
            disc_next_win = disc_win_count + 1
            disc_race_milestone = _next_race_milestone(disc_next_race)
            disc_win_milestone = _next_win_milestone(disc_next_win)
            if disc_race_milestone:
                disc_race_rows.append([disc_race_milestone, disc, entry["name"], entry["nat"], disc_race_count])
            if disc_win_milestone:
                disc_win_rows.append([disc_win_milestone, disc, entry["name"], entry["nat"], disc_win_count])

    if race_milestone_rows:
        race_milestone_rows.sort(key=lambda row: row[0], reverse=True)
        header_label = "World Cup + WCH + OWG race milestones:" if use_major else "World Cup race milestones:"
        print(header_label)
        render_table(["Milestone", "Athlete", "Nat", "CurrentRaces"], race_milestone_rows, pretty=is_pretty_output(args))
        print()
    else:
        header_label = "World Cup + WCH + OWG race milestones: none" if use_major else "World Cup race milestones: none"
        print(header_label)
        print()

    if win_milestone_rows:
        win_milestone_rows.sort(key=lambda row: row[0], reverse=True)
        header_label = (
            "World Cup + WCH + OWG win milestones (if they win this race):"
            if use_major
            else "World Cup win milestones (if they win this race):"
        )
        print(header_label)
        render_table(["Milestone", "Athlete", "Nat", "CurrentWins"], win_milestone_rows, pretty=is_pretty_output(args))
        print()
    else:
        header_label = "World Cup + WCH + OWG win milestones: none" if use_major else "World Cup win milestones: none"
        print(header_label)
        print()

    if disc_race_rows:
        disc_race_rows.sort(key=lambda row: row[0], reverse=True)
        header_label = "Discipline race milestones (WC + WCH + OWG):" if use_major else "Discipline race milestones:"
        print(header_label)
        render_table(["Milestone", "Discipline", "Athlete", "Nat", "CurrentRaces"], disc_race_rows, pretty=is_pretty_output(args))
        print()
    else:
        header_label = "Discipline race milestones (WC + WCH + OWG): none" if use_major else "Discipline race milestones: none"
        print(header_label)
        print()

    if disc_win_rows:
        disc_win_rows.sort(key=lambda row: row[0], reverse=True)
        header_label = (
            "Discipline win milestones (WC + WCH + OWG, if they win this race):"
            if use_major
            else "Discipline win milestones (if they win this race):"
        )
        print(header_label)
        render_table(["Milestone", "Discipline", "Athlete", "Nat", "CurrentWins"], disc_win_rows, pretty=is_pretty_output(args))
        print()
    else:
        header_label = "Discipline win milestones (WC + WCH + OWG): none" if use_major else "Discipline win milestones: none"
        print(header_label)
        print()

    return 0
