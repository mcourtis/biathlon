"""Relay command handler."""

from __future__ import annotations

import argparse
import datetime
import sys

from ..api import (
    BiathlonError,
    get_analytic_results,
    get_current_season_id,
    get_events,
    get_races,
    get_race_results,
)
from ..constants import (
    RELAY_DISCIPLINE,
    RELAY_MEN_CAT,
    RELAY_MIXED_CAT,
    RELAY_WOMEN_CAT,
    SINGLE_MIXED_RELAY_DISCIPLINE,
)
from ..formatting import format_seconds, is_pretty_output, rank_style, render_table
from ..utils import (
    add_relay_shootings,
    format_race_header,
    format_relay_shooting,
    get_first_time,
    get_race_start_key,
    parse_start_datetime,
    parse_relay_shooting,
    parse_relay_shootings,
    parse_time_seconds,
)


def _fetch_analytic_times(race_id: str, type_id: str) -> dict[tuple[str, int], float]:
    """Fetch analytic times and return dict keyed by (Bib, Leg) -> seconds."""
    times: dict[tuple[str, int], float] = {}
    try:
        analytic = get_analytic_results(race_id, type_id)
    except BiathlonError:
        return times
    for res in analytic.get("Results", []):
        if res.get("IsTeam"):
            continue
        bib = str(res.get("Bib") or "")
        leg = res.get("Leg")
        if not bib or leg is None:
            continue
        time_str = get_first_time(res, ["TotalTime", "Result"])
        if time_str:
            seconds = parse_time_seconds(time_str)
            if seconds is not None:
                times[(bib, leg)] = seconds
    return times


def _fetch_leg_lap_times(
    race_id: str,
    lap_prefix: str,
    lap_suffix: str,
    laps: int,
    laps_per_leg: int,
) -> dict[tuple[str, int], dict[str, str]]:
    """Fetch analytic lap times in parallel keyed by (Bib, Leg) -> {lapN: time_str}."""
    from concurrent.futures import ThreadPoolExecutor

    def fetch_one(idx: int) -> tuple[int, list]:
        type_id = f"{lap_prefix}{idx}{lap_suffix}"
        try:
            analytic = get_analytic_results(race_id, type_id)
        except BiathlonError:
            return idx, []
        return idx, analytic.get("Results", [])

    times: dict[tuple[str, int], dict[str, str]] = {}
    with ThreadPoolExecutor(max_workers=laps) as executor:
        results = list(executor.map(fetch_one, range(1, laps + 1)))

    # Process all results after parallel fetch
    for idx, res_list in results:
        for res in res_list:
            if res.get("IsTeam"):
                continue
            bib = str(res.get("Bib") or "")
            ibu_id = str(res.get("IBUId") or "")
            name = str(res.get("Name") or "")
            leg = res.get("Leg")
            if leg is None:
                leg_idx = (idx - 1) // laps_per_leg + 1
                local_idx = (idx - 1) % laps_per_leg + 1
            else:
                leg_idx = int(leg)
                local_idx = idx - (leg_idx - 1) * laps_per_leg
                if local_idx < 1 or local_idx > laps_per_leg:
                    local_idx = (idx - 1) % laps_per_leg + 1
            time_str = get_first_time(res, ["TotalTime", "Result"])
            if time_str:
                if bib:
                    times.setdefault((bib, leg_idx), {})[f"lap{local_idx}"] = time_str
                if ibu_id:
                    times.setdefault((ibu_id, leg_idx), {})[f"lap{local_idx}"] = (
                        time_str
                    )
                if name:
                    times.setdefault((name, leg_idx), {})[f"lap{local_idx}"] = time_str
    return times


def _has_completed_results(payload: dict) -> bool:
    """Return True when a race payload contains completed results."""
    results = payload.get("Results", [])
    if not results:
        return False
    for res in results:
        if not res.get("IsTeam"):
            continue
        rank = res.get("Rank")
        if rank is not None:
            rank_text = str(rank).strip()
            if rank_text and rank_text != "10000":
                return True
        result_val = res.get("Result") or res.get("TotalTime")
        if result_val:
            result_text = str(result_val).strip().upper()
            if result_text and result_text not in {"DNS", "-"}:
                return True
    return False


def _find_latest_relay_race(
    discipline: str,
    category: str,
) -> tuple[str, dict]:
    """Find the most recent completed relay race.

    Args:
        discipline: Discipline code (RL or SR)
        category: Category code (SW, SM, or MX)
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    season_id = get_current_season_id()
    events = get_events(season_id, level=1)

    races: list[tuple[str, str, str, str]] = []
    for event in events:
        event_id = event.get("EventId")
        if not event_id:
            continue
        for race in get_races(event_id):
            start_key = get_race_start_key(race)
            race_id = race.get("RaceId") or race.get("Id") or ""
            race_disc = str(race.get("DisciplineId") or "").upper()
            race_cat = str(race.get("catId") or race.get("CatId") or "").upper()
            if race_id:
                races.append((start_key, race_id, race_disc, race_cat))

    races.sort(reverse=True)

    for start_key, race_id, race_disc, race_cat in races:
        if race_disc != discipline or race_cat != category:
            continue
        try:
            payload = get_race_results(race_id)
        except BiathlonError:
            continue
        comp = payload.get("Competition") or {}
        start_raw = comp.get("StartTime") or start_key
        start_dt = parse_start_datetime(
            start_raw if isinstance(start_raw, str) else None
        )
        if start_dt and start_dt > now:
            continue
        if _has_completed_results(payload):
            return race_id, payload

    # Build descriptive label for error message
    if discipline == SINGLE_MIXED_RELAY_DISCIPLINE:
        relay_type = "single mixed relay"
    elif category == RELAY_MIXED_CAT:
        relay_type = "mixed relay"
    elif category == RELAY_MEN_CAT:
        relay_type = "men relay"
    else:
        relay_type = "women relay"

    raise BiathlonError(f"No completed {relay_type} races with results found")


def handle_relay(args: argparse.Namespace) -> int:
    """Show relay race results."""
    # Determine discipline and category based on flags
    if getattr(args, "singlemixed", False):
        discipline = SINGLE_MIXED_RELAY_DISCIPLINE
        category = RELAY_MIXED_CAT
    elif getattr(args, "mixed", False):
        discipline = RELAY_DISCIPLINE
        category = RELAY_MIXED_CAT
    elif getattr(args, "men", False):
        discipline = RELAY_DISCIPLINE
        category = RELAY_MEN_CAT
    else:
        # Default: women relay
        discipline = RELAY_DISCIPLINE
        category = RELAY_WOMEN_CAT

    # Find or use specified race
    if getattr(args, "race", ""):
        race_id = args.race
        payload = get_race_results(race_id)
    else:
        race_id, payload = _find_latest_relay_race(discipline, category)

    results = payload.get("Results", [])
    if not results:
        print(f"no results found for race {race_id}", file=sys.stderr)
        return 1

    # Fetch analytic times for Course, Range, Shooting
    crst_times = _fetch_analytic_times(race_id, "CRST")  # Course/ski time
    course_laps = _fetch_leg_lap_times(race_id, "CRS", "", 12, 3)
    range_laps = _fetch_leg_lap_times(race_id, "RNG", "", 8, 2)
    shooting_laps = _fetch_leg_lap_times(race_id, "S", "TM", 8, 2)
    rngt_times = _fetch_analytic_times(race_id, "RNGT")  # Range time
    sttm_times = _fetch_analytic_times(race_id, "STTM")  # Shooting time

    # Separate team results and leg results
    team_results = [r for r in results if r.get("IsTeam")]
    leg_results = [r for r in results if not r.get("IsTeam")]

    # Sort team results by rank
    def team_sort_key(r: dict) -> tuple:
        rank = r.get("Rank") or r.get("ResultOrder") or 9999
        try:
            return (0, int(rank))
        except (TypeError, ValueError):
            return (1, str(rank))

    team_results.sort(key=team_sort_key)

    # Apply --first filter (first N teams by race position)
    first_n = getattr(args, "first", 0)
    if first_n > 0:
        team_results = team_results[:first_n]

    # Group leg results by team (using Bib as team identifier)
    legs_by_bib: dict[str, list[dict]] = {}
    for leg in leg_results:
        bib = str(leg.get("Bib") or "")
        if bib:
            legs_by_bib.setdefault(bib, []).append(leg)

    # Sort legs within each team by leg number
    for bib in legs_by_bib:
        legs_by_bib[bib].sort(key=lambda x: x.get("Leg", 0))

    # Determine number of legs (usually 4, but single mixed is 4 too - 2 women + 2 men)
    num_legs = 4

    # Build output rows
    rows = []
    for team in team_results:
        bib = str(team.get("Bib") or "")
        team_rank = team.get("Rank") or team.get("ResultOrder") or ""
        team_name = team.get("Name") or team.get("ShortName") or ""
        nat = team.get("Nat") or ""
        total_time = team.get("TotalTime") or team.get("Result") or "-"
        behind = team.get("Behind") or ""

        # Parse total time for penalty calculation
        result_secs = parse_time_seconds(str(total_time)) if total_time != "-" else None

        # Sum analytic times across all legs for this team
        team_course_secs = 0.0
        team_range_secs = 0.0
        team_shooting_secs = 0.0
        has_course = False
        has_range = False
        has_shooting = False

        # Get leg data and sum shooting stats
        team_legs = legs_by_bib.get(bib, [])
        leg_prone: list[tuple[int, int] | None] = []
        leg_standing: list[tuple[int, int] | None] = []
        leg_names: list[str] = []
        leg_results: list[str] = []
        leg_total_times: list[str] = []
        leg_behinds: list[str] = []
        leg_misses: list[str] = []
        leg_prone_strs: list[str] = []
        leg_standing_strs: list[str] = []
        leg_course_laps: list[list[str]] = []
        leg_range_laps: list[list[str]] = []
        leg_shooting_laps: list[list[str]] = []

        for i in range(1, num_legs + 1):
            leg_key = (bib, i)
            # Sum course time
            if leg_key in crst_times:
                team_course_secs += crst_times[leg_key]
                has_course = True
            # Sum range time
            if leg_key in rngt_times:
                team_range_secs += rngt_times[leg_key]
                has_range = True
            # Sum shooting time
            if leg_key in sttm_times:
                team_shooting_secs += sttm_times[leg_key]
                has_shooting = True

            # Get shooting breakdown from leg results
            leg_data = next((lg for lg in team_legs if lg.get("Leg") == i), None)
            lap_values = []
            leg_lap_times = {}
            if leg_data:
                lookup_keys = []
                for ident in (
                    leg_data.get("IBUId"),
                    leg_data.get("Bib"),
                    bib,
                    leg_data.get("Name"),
                ):
                    if ident:
                        lookup_keys.append((str(ident), i))
                for key in lookup_keys:
                    if key in course_laps:
                        leg_lap_times = course_laps[key]
                        break
            if not leg_lap_times:
                leg_lap_times = course_laps.get(leg_key, {})
            for lap_idx in range(1, 4):
                lap_values.append(leg_lap_times.get(f"lap{lap_idx}", "-"))
            leg_course_laps.append(lap_values)

            range_values = []
            range_times = {}
            if leg_data:
                lookup_keys = []
                for ident in (
                    leg_data.get("IBUId"),
                    leg_data.get("Bib"),
                    bib,
                    leg_data.get("Name"),
                ):
                    if ident:
                        lookup_keys.append((str(ident), i))
                for key in lookup_keys:
                    if key in range_laps:
                        range_times = range_laps[key]
                        break
            if not range_times:
                range_times = range_laps.get(leg_key, {})
            for lap_idx in range(1, 3):
                range_values.append(range_times.get(f"lap{lap_idx}", "-"))
            leg_range_laps.append(range_values)

            shooting_values = []
            shooting_times = {}
            if leg_data:
                lookup_keys = []
                for ident in (
                    leg_data.get("IBUId"),
                    leg_data.get("Bib"),
                    bib,
                    leg_data.get("Name"),
                ):
                    if ident:
                        lookup_keys.append((str(ident), i))
                for key in lookup_keys:
                    if key in shooting_laps:
                        shooting_times = shooting_laps[key]
                        break
            if not shooting_times:
                shooting_times = shooting_laps.get(leg_key, {})
            for lap_idx in range(1, 3):
                shooting_values.append(shooting_times.get(f"lap{lap_idx}", "-"))
            leg_shooting_laps.append(shooting_values)

            if leg_data:
                shootings = parse_relay_shootings(leg_data.get("Shootings"))
                if shootings:
                    prone, standing = shootings
                    leg_prone.append(prone)
                    leg_standing.append(standing)
                else:
                    leg_prone.append(None)
                    leg_standing.append(None)

                leg_names.append(
                    leg_data.get("ShortName") or leg_data.get("Name") or "-"
                )
                leg_result = get_first_time(leg_data, ["TotalTime", "Result"]) or "-"
                leg_results.append(leg_result)
                leg_total_times.append(leg_result)
                leg_behinds.append(leg_data.get("Behind") or "-")
                if shootings:
                    leg_prone_strs.append(format_relay_shooting(*shootings[0]))
                    leg_standing_strs.append(format_relay_shooting(*shootings[1]))
                    total_p = shootings[0][0] + shootings[1][0]
                    total_s = shootings[0][1] + shootings[1][1]
                    leg_misses.append(format_relay_shooting(total_p, total_s))
                else:
                    leg_prone_strs.append("-")
                    leg_standing_strs.append("-")
                    total = parse_relay_shooting(leg_data.get("ShootingTotal"))
                    leg_misses.append(format_relay_shooting(*total) if total else "-")
            else:
                leg_prone.append(None)
                leg_standing.append(None)
                leg_names.append("-")
                leg_results.append("-")
                leg_total_times.append("-")
                leg_behinds.append("-")
                leg_misses.append("-")
                leg_prone_strs.append("-")
                leg_standing_strs.append("-")
                if not lap_values:
                    leg_course_laps.append(["-", "-", "-"])

        leg_times: list[str] = []
        prev_secs: float | None = None
        for i in range(num_legs):
            curr_text = leg_total_times[i]
            curr_secs = (
                parse_time_seconds(curr_text)
                if curr_text not in ("", None, "-")
                else None
            )
            if i == 0:
                leg_times.append(
                    format_seconds(curr_secs) if curr_secs is not None else curr_text
                )
            else:
                if curr_secs is not None and prev_secs is not None:
                    leg_times.append(format_seconds(curr_secs - prev_secs))
                else:
                    leg_times.append("-")
            if curr_secs is not None:
                prev_secs = curr_secs

        # Format Course, Range, Shooting times
        course_str = format_seconds(team_course_secs) if has_course else "-"
        range_str = format_seconds(team_range_secs) if has_range else "-"
        shooting_str = format_seconds(team_shooting_secs) if has_shooting else "-"

        # Calculate Penalty = Result - Course - Range
        penalty_str = "-"
        if result_secs is not None and has_course and has_range:
            penalty_secs = result_secs - team_course_secs - team_range_secs
            if penalty_secs >= 0:
                penalty_str = format_seconds(penalty_secs)

        # Sum prone and standing totals
        total_prone = add_relay_shootings(leg_prone)
        total_standing = add_relay_shootings(leg_standing)
        total_misses = (
            total_prone[0] + total_standing[0],
            total_prone[1] + total_standing[1],
        )

        prone_str = format_relay_shooting(*total_prone)
        standing_str = format_relay_shooting(*total_standing)
        misses_str = format_relay_shooting(*total_misses)

        row = {
            "rank": team_rank,
            "name": team_name,
            "nat": nat,
            "result": total_time,
            "behind": behind,
            "course": course_str,
            "range": range_str,
            "shooting": shooting_str,
            "penalty": penalty_str,
            "prone": prone_str,
            "standing": standing_str,
            "misses": misses_str,
        }
        for i in range(1, num_legs + 1):
            row[f"leg{i}_name"] = leg_names[i - 1]
            row[f"leg{i}_result"] = leg_results[i - 1]
            row[f"leg{i}_behind"] = leg_behinds[i - 1]
            row[f"leg{i}_time"] = leg_times[i - 1]
            row[f"leg{i}_miss"] = leg_misses[i - 1]
            row[f"leg{i}_prone"] = leg_prone_strs[i - 1]
            row[f"leg{i}_standing"] = leg_standing_strs[i - 1]
            row[f"leg{i}_courselap1"] = leg_course_laps[i - 1][0]
            row[f"leg{i}_courselap2"] = leg_course_laps[i - 1][1]
            row[f"leg{i}_courselap3"] = leg_course_laps[i - 1][2]
            row[f"leg{i}_range1"] = leg_range_laps[i - 1][0]
            row[f"leg{i}_range2"] = leg_range_laps[i - 1][1]
            row[f"leg{i}_shooting1"] = leg_shooting_laps[i - 1][0]
            row[f"leg{i}_shooting2"] = leg_shooting_laps[i - 1][1]
        rows.append(row)

    show_detail = getattr(args, "detail", False)

    # Column name mapping (header -> row key)
    col_map = {
        "result": "result",
        "behind": "behind",
        "course": "course",
        "range": "range",
        "shoot": "shooting",
        "penalty": "penalty",
        "prone": "prone",
        "stand": "standing",
        "misses": "misses",
        # Backwards compatibility
        "shooting": "shooting",
        "standing": "standing",
    }
    detail_sort_cols = [
        "team",
        "result",
        "behind",
        "misses",
        "course",
        "range",
        "shoot",
        "leg",
        "biathlete",
        "legresult",
        "legbehind",
        "legtime",
        "lap1",
        "lap2",
        "lap3",
        "range1",
        "range2",
        "shoot1",
        "shoot2",
        "prone",
        "stand",
        "miss",
    ]
    detail_col_map = {
        "team": "team",
        "result": "result",
        "behind": "behind",
        "misses": "misses",
        "course": "course",
        "range": "range",
        "shoot": "shooting",
        "leg": "leg",
        "biathlete": "biathlete",
        "legresult": "leg_result",
        "legbehind": "leg_behind",
        "legtime": "leg_time",
        "lap1": "leg_courselap1",
        "lap2": "leg_courselap2",
        "lap3": "leg_courselap3",
        "range1": "leg_range1",
        "range2": "leg_range2",
        "shoot1": "leg_shooting1",
        "shoot2": "leg_shooting2",
        "prone": "leg_prone",
        "stand": "leg_standing",
        "miss": "leg_miss",
        # Backwards compatibility
        "shooting": "shooting",
        "standing": "leg_standing",
    }
    detail_sort_headers = {
        "team": "Team",
        "result": "Result",
        "behind": "Behind",
        "misses": "Misses",
        "course": "Course",
        "range": "Range",
        "shoot": "Shoot",
        "leg": "Leg",
        "biathlete": "Biathlete",
        "legresult": "LegResult",
        "legbehind": "LegBehind",
        "legtime": "LegTime",
        "lap1": "Lap1",
        "lap2": "Lap2",
        "lap3": "Lap3",
        "range1": "Range1",
        "range2": "Range2",
        "shoot1": "Shoot1",
        "shoot2": "Shoot2",
        "prone": "Prone",
        "stand": "Stand",
        "miss": "Miss",
        "shooting": "Shoot",
        "standing": "Stand",
    }

    # Handle --sort
    sort_col = getattr(args, "sort", "").lower()
    show_sort_rank = False
    sort_col_map: dict[str, str] = {}
    detail_row_key = ""
    detail_sort_header = ""
    if sort_col:
        if show_detail:
            detail_row_key = detail_col_map.get(sort_col, "")
            if not detail_row_key:
                print(
                    f"error: sort must be one of {', '.join(detail_sort_cols)}",
                    file=sys.stderr,
                )
                return 1
            detail_sort_header = detail_sort_headers.get(
                sort_col, sort_col.capitalize()
            )
            show_sort_rank = True
        else:
            row_key = col_map.get(sort_col)
            if not row_key:
                valid_cols = [
                    "result",
                    "behind",
                    "course",
                    "range",
                    "shoot",
                    "penalty",
                    "prone",
                    "stand",
                    "misses",
                ]
                print(
                    f"error: sort must be one of {', '.join(valid_cols)}",
                    file=sys.stderr,
                )
                return 1

            # Determine sort rank header name
            sort_col_map = {
                "result": "Result",
                "behind": "Behind",
                "course": "Course",
                "range": "Range",
                "shoot": "Shoot",
                "penalty": "Penalty",
                "prone": "Prone",
                "stand": "Stand",
                "misses": "Misses",
                "shooting": "Shoot",
                "standing": "Stand",
            }
            # Sort by the specified column
            if row_key in ("prone", "standing", "misses"):
                # Sort by shooting (P+S format), by penalties first, then spares
                def sort_key(r: dict) -> tuple:
                    val = r.get(row_key, "-")
                    shooting = (
                        parse_relay_shooting(val)
                        if val not in ("", None, "-")
                        else None
                    )
                    if shooting:
                        return (0, shooting[0], shooting[1])  # penalties, then spares
                    return (1, 9999, 9999)

                rows = sorted(rows, key=sort_key)
            else:
                # Sort by time column
                def sort_key(r: dict) -> tuple:
                    val = r.get(row_key)
                    sec = (
                        parse_time_seconds(str(val))
                        if val not in ("", None, "-")
                        else None
                    )
                    if sec is None:
                        return (1, float("inf"))
                    return (0, sec)

                rows = sorted(rows, key=sort_key)

            # Assign sort rank
            for idx, row in enumerate(rows, start=1):
                row["sort_rank"] = idx
            show_sort_rank = True

    # Apply --limit
    limit_n = getattr(args, "limit", 25)
    if limit_n > 0:
        rows = rows[:limit_n]

    # Build headers and render
    print(format_race_header(payload, race_id))

    if show_detail:
        if show_sort_rank:
            headers = [
                "Rank",
                "FinalRank",
                "Team",
                "Result",
                "Behind",
                "Misses",
                "Course",
                "Range",
                "Shoot",
                "Leg",
                "Biathlete",
                "LegResult",
                "LegBehind",
                "LegTime",
                "Lap1",
                "Lap2",
                "Lap3",
                "Range1",
                "Range2",
                "Shoot1",
                "Shoot2",
                "Prone",
                "Stand",
                "Miss",
            ]
        else:
            headers = [
                "Rank",
                "Team",
                "Result",
                "Behind",
                "Misses",
                "Course",
                "Range",
                "Shoot",
                "Leg",
                "Biathlete",
                "LegResult",
                "LegBehind",
                "LegTime",
                "Lap1",
                "Lap2",
                "Lap3",
                "Range1",
                "Range2",
                "Shoot1",
                "Shoot2",
                "Prone",
                "Stand",
                "Miss",
            ]
    else:
        if show_sort_rank:
            headers = [
                "Rank",
                "FinalRank",
                "Team",
                "Country",
                "Result",
                "Behind",
                "Course",
                "Range",
                "Shooting",
                "Penalty",
                "Prone",
                "Standing",
                "Misses",
            ]
        else:
            headers = [
                "Rank",
                "Team",
                "Country",
                "Result",
                "Behind",
                "Course",
                "Range",
                "Shooting",
                "Penalty",
                "Prone",
                "Standing",
                "Misses",
            ]

    # Find index of sorted column header for highlighting
    highlight_headers = None
    if show_sort_rank:
        if show_detail:
            if detail_sort_header and detail_sort_header in headers:
                highlight_headers = [headers.index(detail_sort_header)]
        else:
            sort_header = sort_col_map.get(sort_col)
            if sort_header and sort_header in headers:
                highlight_headers = [headers.index(sort_header)]

    render_rows = []
    row_styles = []
    if show_detail:
        detail_rows = []
        for row in rows:
            for i in range(1, num_legs + 1):
                detail_rows.append(
                    {
                        "rank": row["rank"],
                        "team": row["name"],
                        "result": row["result"],
                        "behind": row["behind"],
                        "misses": row["misses"],
                        "course": row["course"],
                        "range": row["range"],
                        "shooting": row["shooting"],
                        "leg": i,
                        "biathlete": row[f"leg{i}_name"],
                        "leg_result": row[f"leg{i}_result"],
                        "leg_behind": row[f"leg{i}_behind"],
                        "leg_time": row[f"leg{i}_time"],
                        "leg_courselap1": row[f"leg{i}_courselap1"],
                        "leg_courselap2": row[f"leg{i}_courselap2"],
                        "leg_courselap3": row[f"leg{i}_courselap3"],
                        "leg_range1": row[f"leg{i}_range1"],
                        "leg_range2": row[f"leg{i}_range2"],
                        "leg_shooting1": row[f"leg{i}_shooting1"],
                        "leg_shooting2": row[f"leg{i}_shooting2"],
                        "leg_prone": row[f"leg{i}_prone"],
                        "leg_standing": row[f"leg{i}_standing"],
                        "leg_miss": row[f"leg{i}_miss"],
                    }
                )

        if show_sort_rank and detail_row_key:
            time_keys = {
                "result",
                "behind",
                "course",
                "range",
                "shooting",
                "penalty",
                "leg_result",
                "leg_behind",
                "leg_time",
                "leg_courselap1",
                "leg_courselap2",
                "leg_courselap3",
                "leg_range1",
                "leg_range2",
                "leg_shooting1",
                "leg_shooting2",
            }
            shooting_keys = {"misses", "leg_prone", "leg_standing", "leg_miss"}

            def detail_sort_key(entry: dict) -> tuple:
                val = entry.get(detail_row_key)
                if detail_row_key in shooting_keys:
                    shooting = (
                        parse_relay_shooting(val)
                        if val not in ("", None, "-")
                        else None
                    )
                    if shooting:
                        return (0, shooting[0], shooting[1])
                    return (1, 9999, 9999)
                if detail_row_key in time_keys:
                    sec = (
                        parse_time_seconds(str(val))
                        if val not in ("", None, "-")
                        else None
                    )
                    if sec is None:
                        return (1, float("inf"))
                    return (0, sec)
                if detail_row_key == "leg":
                    try:
                        return (0, int(val))
                    except (TypeError, ValueError):
                        return (1, 9999)
                text = str(val or "").strip().lower()
                return (text == "", text)

            detail_rows = sorted(detail_rows, key=detail_sort_key)
            for idx, entry in enumerate(detail_rows, start=1):
                entry["sort_rank"] = idx

        for entry in detail_rows:
            if show_sort_rank:
                render_row = [
                    entry.get("sort_rank", ""),
                    entry["rank"],
                    entry["team"],
                    entry["result"],
                    entry["behind"],
                    entry["misses"],
                    entry["course"],
                    entry["range"],
                    entry["shooting"],
                    entry["leg"],
                    entry["biathlete"],
                    entry["leg_result"],
                    entry["leg_behind"],
                    entry["leg_time"],
                    entry["leg_courselap1"],
                    entry["leg_courselap2"],
                    entry["leg_courselap3"],
                    entry["leg_range1"],
                    entry["leg_range2"],
                    entry["leg_shooting1"],
                    entry["leg_shooting2"],
                    entry["leg_prone"],
                    entry["leg_standing"],
                    entry["leg_miss"],
                ]
            else:
                render_row = [
                    entry["rank"],
                    entry["team"],
                    entry["result"],
                    entry["behind"],
                    entry["misses"],
                    entry["course"],
                    entry["range"],
                    entry["shooting"],
                    entry["leg"],
                    entry["biathlete"],
                    entry["leg_result"],
                    entry["leg_behind"],
                    entry["leg_time"],
                    entry["leg_courselap1"],
                    entry["leg_courselap2"],
                    entry["leg_courselap3"],
                    entry["leg_range1"],
                    entry["leg_range2"],
                    entry["leg_shooting1"],
                    entry["leg_shooting2"],
                    entry["leg_prone"],
                    entry["leg_standing"],
                    entry["leg_miss"],
                ]
            render_rows.append(render_row)
            row_styles.append(rank_style(entry["rank"]))
    else:
        for row in rows:
            if show_sort_rank:
                render_row = [
                    row["sort_rank"],
                    row["rank"],
                    row["name"],
                    row["nat"],
                    row["result"],
                    row["behind"],
                    row["course"],
                    row["range"],
                    row["shooting"],
                    row["penalty"],
                    row["prone"],
                    row["standing"],
                    row["misses"],
                ]
            else:
                render_row = [
                    row["rank"],
                    row["name"],
                    row["nat"],
                    row["result"],
                    row["behind"],
                    row["course"],
                    row["range"],
                    row["shooting"],
                    row["penalty"],
                    row["prone"],
                    row["standing"],
                    row["misses"],
                ]
            render_rows.append(render_row)
        row_styles = [rank_style(row["rank"]) for row in rows]
    render_table(
        headers,
        render_rows,
        pretty=is_pretty_output(args),
        row_styles=row_styles,
        highlight_headers=highlight_headers,
    )
    return 0
