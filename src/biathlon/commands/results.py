"""Results command handler (replacement for results + relay)."""

from __future__ import annotations

import argparse
import datetime
import sys

from ..api import (
    BiathlonError,
    get_analytic_results,
    get_cup_results,
    get_current_season_id,
    get_events,
    get_race_results,
    get_races,
)
from ..constants import (
    CAT_TO_GENDER,
    RELAY_DISCIPLINE,
    RELAY_MEN_CAT,
    RELAY_MIXED_CAT,
    RELAY_WOMEN_CAT,
    SINGLE_MIXED_RELAY_DISCIPLINE,
    SKI_LAPS,
    SHOOTING_STAGES,
)
from ..formatting import format_seconds, is_pretty_output, rank_style, render_table
from ..utils import (
    add_relay_shootings,
    base_time_seconds,
    extract_results,
    format_race_header,
    format_relay_shooting,
    get_first_time,
    get_race_start_key,
    is_dns,
    normalize_result_time,
    parse_relay_shooting,
    parse_relay_shootings,
    parse_start_datetime,
    parse_time_seconds,
)
from .relay import _has_completed_results as _has_completed_relay_results
from .scores import find_cup_id


def _row_ibu_id(row: dict) -> str:
    """Return the IBU id from a standings row."""
    for key in ("IBUId", "IbuId", "ibuId"):
        val = row.get(key)
        if val:
            return str(val)
    return ""


def _get_wc_rows(cat_id: str, season_id: str) -> list[dict]:
    """Fetch World Cup standings rows for a category/season."""
    gender = CAT_TO_GENDER.get(cat_id.upper())
    if not gender:
        return []
    try:
        cup_id = find_cup_id(season_id, gender, level=1, cup_type="total")
        payload = get_cup_results(cup_id)
        return payload.get("Rows") or payload.get("Results") or []
    except BiathlonError:
        return []


def _get_wc_rank_map(cat_id: str, top_n: int, season_id: str | None = None) -> dict[str, int]:
    """Return IBU id -> WC rank mapping for the top N."""
    if top_n <= 0:
        return {}
    season = season_id or get_current_season_id()
    rows = _get_wc_rows(cat_id, season)
    rank_map: dict[str, int] = {}
    for row in rows:
        ibu_id = _row_ibu_id(row)
        if not ibu_id:
            continue
        try:
            rank = int(row.get("Rank") or row.get("rank") or 0)
        except (TypeError, ValueError):
            continue
        if rank <= 0:
            continue
        rank_map[ibu_id] = rank
        if len(rank_map) >= top_n:
            break
    return rank_map


def _get_top_n_ibu_ids(cat_id: str, top_n: int, season_id: str | None = None) -> list[str]:
    """Return IBU ids for the top N WC standings."""
    if top_n <= 0:
        return []
    season = season_id or get_current_season_id()
    rows = _get_wc_rows(cat_id, season)
    ids: list[str] = []
    for row in rows:
        ibu_id = _row_ibu_id(row)
        if not ibu_id:
            continue
        ids.append(ibu_id)
        if len(ids) >= top_n:
            break
    return ids


def _has_completed_results(payload: dict) -> bool:
    """Return True when a race payload contains completed results."""
    results = payload.get("Results", [])
    if not results:
        return False
    for res in results:
        if res.get("IsTeam"):
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


def _calculate_pursuit_time(result_time: str, start_delay: str) -> str:
    """Return pursuit time excluding start delay."""
    result_secs = parse_time_seconds(result_time)
    if result_secs is None:
        return "-"
    delay_secs = parse_time_seconds(start_delay)
    if delay_secs is None:
        delay_secs = 0
    pursuit_secs = result_secs - delay_secs
    if pursuit_secs < 0:
        return "-"
    return format_seconds(pursuit_secs)


def _fetch_analytic_map(race_id: str, type_id: str) -> dict[str, str]:
    """Fetch analytic times keyed by IBUId/Bib/Name."""
    try:
        analytic = get_analytic_results(race_id, type_id)
    except BiathlonError:
        return {}
    times: dict[str, str] = {}
    for res in analytic.get("Results", []):
        if res.get("IsTeam"):
            continue
        ident = res.get("IBUId") or res.get("Bib") or res.get("Name")
        if not ident:
            continue
        times[str(ident)] = get_first_time(res, ["TotalTime", "Result"]) or "-"
    return times


def _fetch_stage_times(race_id: str, prefix: str, suffix: str, count: int) -> dict[int, dict[str, str]]:
    """Fetch analytic stage times in parallel."""
    from concurrent.futures import ThreadPoolExecutor

    def fetch_one(idx: int) -> tuple[int, dict[str, str]]:
        type_id = f"{prefix}{idx}{suffix}"
        return idx, _fetch_analytic_map(race_id, type_id)

    stages: dict[int, dict[str, str]] = {}
    with ThreadPoolExecutor(max_workers=count) as executor:
        for idx, times in executor.map(fetch_one, range(1, count + 1)):
            stages[idx] = times
    return stages


def _lookup_analytic_time(times: dict[str, str], res: dict) -> str:
    """Return analytic time for a result using multiple identifier keys."""
    for key in (res.get("IBUId"), res.get("Bib"), res.get("Name"), res.get("ShortName")):
        if key and str(key) in times:
            return times[str(key)]
    return ""


def _parse_shootings(value: str | None) -> list[int]:
    """Parse shootings string like '0+1+0+2' into list of ints."""
    if not value:
        return []
    parts = [p.strip() for p in str(value).split("+") if p.strip()]
    misses: list[int] = []
    for part in parts:
        try:
            misses.append(int(part))
        except ValueError:
            misses.append(0)
    return misses


def _shooting_totals(misses: list[int]) -> tuple[int, int, int]:
    """Return (misses_total, prone_total, standing_total)."""
    misses_total = sum(misses) if misses else 0
    prone = 0
    standing = 0
    if len(misses) >= 4:
        prone = misses[0] + misses[1]
        standing = misses[2] + misses[3]
    elif len(misses) == 2:
        prone = misses[0]
        standing = misses[1]
    elif len(misses) == 1:
        prone = misses[0]
    return misses_total, prone, standing


def _shooting_stages(misses: list[int], stages: int) -> tuple[str, str, str, str]:
    """Return stage misses for Prone1/Prone2/Standing1/Standing2."""
    stage_vals = ["-", "-", "-", "-"]
    if not misses or stages <= 0:
        return stage_vals[0], stage_vals[1], stage_vals[2], stage_vals[3]
    if stages <= 2:
        stage_vals[0] = str(misses[0])
        if len(misses) >= 2:
            stage_vals[2] = str(misses[1])
        return stage_vals[0], stage_vals[1], stage_vals[2], stage_vals[3]
    for idx, val in enumerate(misses[:4]):
        stage_vals[idx] = str(val)
    return stage_vals[0], stage_vals[1], stage_vals[2], stage_vals[3]


def _find_latest_race_with_results_any() -> tuple[str, dict]:
    """Return the most recent race id with completed results (incl. relay)."""
    now = datetime.datetime.now(datetime.timezone.utc)
    season_id = get_current_season_id()
    events = get_events(season_id, level=1)

    races: list[tuple[str, str, str]] = []
    for event in events:
        event_id = event.get("EventId")
        if not event_id:
            continue
        for race in get_races(event_id):
            start_key = get_race_start_key(race)
            race_id = race.get("RaceId") or race.get("Id") or ""
            discipline = str(race.get("DisciplineId") or "").upper()
            if race_id:
                races.append((start_key, race_id, discipline))

    races.sort(reverse=True)

    for start_key, race_id, discipline in races:
        try:
            payload = get_race_results(race_id)
        except BiathlonError:
            continue
        comp = payload.get("Competition") or {}
        start_raw = comp.get("StartTime") or start_key
        start_dt = parse_start_datetime(start_raw if isinstance(start_raw, str) else None)
        if start_dt and start_dt > now:
            continue
        if discipline in (RELAY_DISCIPLINE, SINGLE_MIXED_RELAY_DISCIPLINE):
            if _has_completed_relay_results(payload):
                return race_id, payload
        else:
            if _has_completed_results(payload):
                return race_id, payload

    raise BiathlonError("No completed races with results found")


def _find_latest_race_by_discipline(
    discipline: str, mixed_mode: str, cat_filter: str | None
) -> tuple[str, dict]:
    """Return the most recent race id with completed results for a discipline.

    mixed_mode: "any", "mixed-only", or "non-mixed".
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
        if race_disc != discipline:
            continue
        try:
            payload = get_race_results(race_id)
        except BiathlonError:
            continue
        comp = payload.get("Competition") or {}
        start_raw = comp.get("StartTime") or start_key
        start_dt = parse_start_datetime(start_raw if isinstance(start_raw, str) else None)
        if start_dt and start_dt > now:
            continue
        comp_cat = str(comp.get("catId") or comp.get("CatId") or race_cat or "").upper()
        if mixed_mode == "mixed-only" and comp_cat and comp_cat != RELAY_MIXED_CAT:
            continue
        if mixed_mode == "non-mixed" and comp_cat == RELAY_MIXED_CAT:
            continue
        if cat_filter and comp_cat and comp_cat != cat_filter:
            continue

        if discipline in (RELAY_DISCIPLINE, SINGLE_MIXED_RELAY_DISCIPLINE):
            if _has_completed_relay_results(payload):
                return race_id, payload
        else:
            if _has_completed_results(payload):
                return race_id, payload

    raise BiathlonError(f"No completed races with results found for discipline {discipline}")


def _get_discipline(payload: dict) -> str:
    """Return the discipline id for a race payload."""
    comp = payload.get("Competition") or {}
    return str(comp.get("DisciplineId") or "").upper()


def _sort_rows(rows: list[dict], key: str, relay: bool) -> list[dict]:
    """Sort rows by a given key."""
    def time_key(value: object) -> tuple:
        sec = parse_time_seconds(str(value)) if value not in ("", None, "-") else None
        if sec is None:
            return (1, float("inf"))
        return (0, sec)

    def int_key(value: object) -> tuple:
        try:
            return (0, int(value))
        except (TypeError, ValueError):
            return (1, 10**9)

    def shooting_key(value: object) -> tuple:
        shooting = parse_relay_shooting(value) if value not in ("", None, "-") else None
        if shooting:
            return (0, shooting[0], shooting[1])
        return (1, 9999, 9999)

    def row_key(row: dict) -> tuple:
        dns_flag = row.get("dns", False)
        value = row.get(key, "")
        if key in {"rank", "startrank", "misses"} and not relay:
            return (1, 0, 0) if dns_flag else (0, *int_key(value))
        if key == "gain" and not relay:
            if dns_flag:
                return (1, 0, 0)
            try:
                return (0, -int(value))
            except (TypeError, ValueError):
                return (0, 10**9)
        if relay and key in {"misses"}:
            return (1, 0, 0) if dns_flag else (0, *shooting_key(value))
        return (1, 0, 0) if dns_flag else (0, *time_key(value))

    return sorted(rows, key=row_key)


def _build_relay_rows(payload: dict, race_id: str, first_n: int, discipline: str) -> list[dict]:
    """Build relay team rows with leg details."""
    from .relay import _fetch_analytic_times, _fetch_leg_lap_times

    results = payload.get("Results", [])
    if not results:
        return []

    crst_times = _fetch_analytic_times(race_id, "CRST")
    course_laps = _fetch_leg_lap_times(race_id, "CRS", "", 12, 3)
    range_laps = _fetch_leg_lap_times(race_id, "RNG", "", 8, 2)
    shooting_laps = _fetch_leg_lap_times(race_id, "S", "TM", 8, 2)
    rngt_times = _fetch_analytic_times(race_id, "RNGT")
    sttm_times = _fetch_analytic_times(race_id, "STTM")

    team_results = [r for r in results if r.get("IsTeam")]
    leg_results = [r for r in results if not r.get("IsTeam")]

    def team_sort_key(r: dict) -> tuple:
        rank = r.get("Rank") or r.get("ResultOrder") or 9999
        try:
            return (0, int(rank))
        except (TypeError, ValueError):
            return (1, str(rank))

    team_results.sort(key=team_sort_key)
    if first_n > 0:
        team_results = team_results[:first_n]

    legs_by_bib: dict[str, list[dict]] = {}
    for leg in leg_results:
        bib = str(leg.get("Bib") or "")
        if bib:
            legs_by_bib.setdefault(bib, []).append(leg)
    for bib in legs_by_bib:
        legs_by_bib[bib].sort(key=lambda x: x.get("Leg", 0))

    num_legs = 4
    is_single_mixed = discipline == SINGLE_MIXED_RELAY_DISCIPLINE
    rows = []
    for team in team_results:
        bib = str(team.get("Bib") or "")
        team_rank = team.get("Rank") or team.get("ResultOrder") or ""
        team_name = team.get("Name") or team.get("ShortName") or ""
        nat = team.get("Nat") or ""
        total_time = team.get("TotalTime") or team.get("Result") or "-"
        behind = team.get("Behind") or ""

        result_secs = parse_time_seconds(str(total_time)) if total_time != "-" else None
        team_course_secs = 0.0
        team_range_secs = 0.0
        team_shooting_secs = 0.0
        has_course = False
        has_range = False
        has_shooting = False

        team_legs = legs_by_bib.get(bib, [])
        leg_prone: list[tuple[int, int] | None] = []
        leg_standing: list[tuple[int, int] | None] = []
        leg_names: list[str] = []
        leg_results_list: list[str] = []
        leg_total_times: list[str] = []
        leg_behinds: list[str] = []
        leg_misses: list[str] = []
        leg_prone_strs: list[str] = []
        leg_standing_strs: list[str] = []
        leg_course_laps: list[list[str]] = []
        leg_range_laps: list[list[str]] = []
        leg_shooting_laps: list[list[str]] = []
        leg_course_totals: list[str] = []

        for i in range(1, num_legs + 1):
            leg_key = (bib, i)
            if leg_key in crst_times:
                team_course_secs += crst_times[leg_key]
                has_course = True
            if leg_key in rngt_times:
                team_range_secs += rngt_times[leg_key]
                has_range = True
            if leg_key in sttm_times:
                team_shooting_secs += sttm_times[leg_key]
                has_shooting = True

            leg_data = next((lg for lg in team_legs if lg.get("Leg") == i), None)
            lap_values = []
            leg_lap_times = {}
            if leg_data:
                lookup_keys = []
                for ident in (leg_data.get("IBUId"), leg_data.get("Bib"), bib, leg_data.get("Name")):
                    if ident:
                        lookup_keys.append((str(ident), i))
                        if is_single_mixed and i > 2:
                            lookup_keys.append((str(ident), i - 2))
                for key in lookup_keys:
                    if key in course_laps:
                        leg_lap_times = course_laps[key]
                        break
            if not leg_lap_times:
                leg_lap_times = course_laps.get(leg_key, {})
                if is_single_mixed and i > 2 and not leg_lap_times:
                    leg_lap_times = course_laps.get((bib, i - 2), {})
            for lap_idx in range(1, 4):
                lap_values.append(leg_lap_times.get(f"lap{lap_idx}", "-"))
            leg_course_laps.append(lap_values)

            range_values = []
            range_times = {}
            if leg_data:
                lookup_keys = []
                for ident in (leg_data.get("IBUId"), leg_data.get("Bib"), bib, leg_data.get("Name")):
                    if ident:
                        lookup_keys.append((str(ident), i))
                        if is_single_mixed and i > 2:
                            lookup_keys.append((str(ident), i - 2))
                for key in lookup_keys:
                    if key in range_laps:
                        range_times = range_laps[key]
                        break
            if not range_times:
                range_times = range_laps.get(leg_key, {})
                if is_single_mixed and i > 2 and not range_times:
                    range_times = range_laps.get((bib, i - 2), {})
            for lap_idx in range(1, 3):
                range_values.append(range_times.get(f"lap{lap_idx}", "-"))
            leg_range_laps.append(range_values)

            shooting_values = []
            shooting_times = {}
            if leg_data:
                lookup_keys = []
                for ident in (leg_data.get("IBUId"), leg_data.get("Bib"), bib, leg_data.get("Name")):
                    if ident:
                        lookup_keys.append((str(ident), i))
                        if is_single_mixed and i > 2:
                            lookup_keys.append((str(ident), i - 2))
                for key in lookup_keys:
                    if key in shooting_laps:
                        shooting_times = shooting_laps[key]
                        break
            if not shooting_times:
                shooting_times = shooting_laps.get(leg_key, {})
                if is_single_mixed and i > 2 and not shooting_times:
                    shooting_times = shooting_laps.get((bib, i - 2), {})
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

                leg_names.append(leg_data.get("ShortName") or leg_data.get("Name") or "-")
                leg_result = get_first_time(leg_data, ["TotalTime", "Result"]) or "-"
                leg_results_list.append(leg_result)
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
                leg_results_list.append("-")
                leg_total_times.append("-")
                leg_behinds.append("-")
                leg_misses.append("-")
                leg_prone_strs.append("-")
                leg_standing_strs.append("-")
                if not lap_values:
                    leg_course_laps.append(["-", "-", "-"])

            leg_course = "-"
            if leg_key in crst_times:
                leg_course = format_seconds(crst_times[leg_key])
            leg_course_totals.append(leg_course)

        leg_times: list[str] = []
        prev_secs: float | None = None
        for i in range(num_legs):
            curr_text = leg_total_times[i]
            curr_secs = parse_time_seconds(curr_text) if curr_text not in ("", None, "-") else None
            if i == 0:
                leg_times.append(format_seconds(curr_secs) if curr_secs is not None else curr_text)
            else:
                if curr_secs is not None and prev_secs is not None:
                    leg_times.append(format_seconds(curr_secs - prev_secs))
                else:
                    leg_times.append("-")
            if curr_secs is not None:
                prev_secs = curr_secs

        course_str = format_seconds(team_course_secs) if has_course else "-"
        range_str = format_seconds(team_range_secs) if has_range else "-"
        shooting_str = format_seconds(team_shooting_secs) if has_shooting else "-"

        penalty_str = "-"
        if result_secs is not None and has_course and has_range:
            penalty_secs = result_secs - team_course_secs - team_range_secs
            if penalty_secs >= 0:
                penalty_str = format_seconds(penalty_secs)

        total_prone = add_relay_shootings(leg_prone)
        total_standing = add_relay_shootings(leg_standing)
        total_misses = (total_prone[0] + total_standing[0], total_prone[1] + total_standing[1])

        prone_str = format_relay_shooting(*total_prone)
        standing_str = format_relay_shooting(*total_standing)
        misses_str = format_relay_shooting(*total_misses)

        row = {
            "rank": team_rank,
            "team": team_name,
            "nat": nat,
            "result": total_time,
            "behind": behind,
            "course": course_str,
            "range": range_str,
            "shooting": shooting_str,
            "penalty": penalty_str,
            "misses": misses_str,
            "dns": is_dns(team),
        }
        for i in range(1, num_legs + 1):
            row[f"leg{i}_name"] = leg_names[i - 1]
            row[f"leg{i}_result"] = leg_results_list[i - 1]
            row[f"leg{i}_behind"] = leg_behinds[i - 1]
            row[f"leg{i}_course"] = leg_course_totals[i - 1]
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
            row[f"leg{i}_time"] = leg_times[i - 1]
        rows.append(row)

    return rows


def handle_results(args: argparse.Namespace) -> int:
    """List results for a race (unified output)."""
    if getattr(args, "race", "") and getattr(args, "discipline", ""):
        print("error: --race and --discipline cannot be used together", file=sys.stderr)
        return 1
    if getattr(args, "race", "") and getattr(args, "men", False):
        print("error: --race and --men cannot be used together", file=sys.stderr)
        return 1

    if getattr(args, "race", ""):
        race_id = args.race
        payload = get_race_results(race_id)
    elif getattr(args, "discipline", ""):
        disc_arg = args.discipline
        is_men = getattr(args, "men", False)
        disc_map = {
            "individual": ("IN", "any"),
            "sprint": ("SP", "any"),
            "pursuit": ("PU", "any"),
            "mass-start": ("MS", "any"),
            "relay": (RELAY_DISCIPLINE, "non-mixed"),
            "mixed-relay": (RELAY_DISCIPLINE, "mixed-only"),
            "single-mixed-relay": (SINGLE_MIXED_RELAY_DISCIPLINE, "mixed-only"),
        }
        disc_code, mixed_mode = disc_map.get(disc_arg, ("", "any"))
        if not disc_code:
            print("error: unknown discipline", file=sys.stderr)
            return 1
        if disc_arg in {"mixed-relay", "single-mixed-relay"} and is_men:
            print("error: --men is not supported for mixed relay races", file=sys.stderr)
            return 1
        cat_filter = RELAY_MEN_CAT if is_men else RELAY_WOMEN_CAT
        if disc_arg in {"mixed-relay", "single-mixed-relay"}:
            cat_filter = None
        race_id, payload = _find_latest_race_by_discipline(disc_code, mixed_mode, cat_filter)
    else:
        race_id, payload = _find_latest_race_with_results_any()

    discipline = _get_discipline(payload)
    show_detail = getattr(args, "detail", False)

    if discipline in (RELAY_DISCIPLINE, SINGLE_MIXED_RELAY_DISCIPLINE):
        rows = _build_relay_rows(payload, race_id, getattr(args, "first", 0), discipline)
        if not rows:
            print(f"no results found for race {race_id}", file=sys.stderr)
            return 1
        if getattr(args, "country", ""):
            country_filter = args.country.upper()
            rows = [r for r in rows if r.get("nat", "").upper() == country_filter]

        sort_col = getattr(args, "sort", "").lower().replace(" ", "")
        show_sort_rank = False
        sort_header = ""
        detail_sort_key = ""
        if sort_col:
            sort_map = {
                "results": "result",
                "result": "result",
                "behind": "behind",
                "course": "course",
                "range": "range",
                "shooting": "shooting",
                "shoot": "shooting",
                "penalty": "penalty",
                "misses": "misses",
                "miss": "misses",
            }
            detail_sort_map = {
                "leg": "leg",
                "biathlete": "biathlete",
                "legresult": "leg_result",
                "legbehind": "leg_behind",
                "legcourse": "leg_course",
                "lap1": "lap1",
                "lap2": "lap2",
                "lap3": "lap3",
                "r1": "range1",
                "r2": "range2",
                "s1": "shoot1",
                "s2": "shoot2",
                "prone": "leg_prone",
                "standing": "leg_standing",
                "miss": "leg_miss",
                "legpenalty": "leg_penalty",
            }
            sort_key = sort_map.get(sort_col)
            detail_sort_key = detail_sort_map.get(sort_col, "")
            if not sort_key and not detail_sort_key:
                valid = ", ".join(list(sort_map.keys()) + list(detail_sort_map.keys()))
                print(f"error: sort must be one of {valid}", file=sys.stderr)
                return 1
            if sort_key:
                rows = _sort_rows(rows, sort_key, relay=True)
                show_sort_rank = True
                sort_header = {
                    "result": "Results",
                    "behind": "Behind",
                    "course": "Course",
                    "range": "Range",
                    "shooting": "Shoot",
                    "penalty": "Penalty",
                    "misses": "Miss",
                }.get(sort_key, "")
            elif detail_sort_key:
                show_sort_rank = True
                sort_header = {
                    "leg": "Leg",
                    "biathlete": "Biathlete",
                    "leg_result": "LegResult",
                    "leg_behind": "LegBehind",
                    "leg_course": "LegCourse",
                    "lap1": "Lap1",
                    "lap2": "Lap2",
                    "lap3": "Lap3",
                    "range1": "R1",
                    "range2": "R2",
                    "shoot1": "S1",
                    "shoot2": "S2",
                    "leg_prone": "Prone",
                    "leg_standing": "Stand",
                    "leg_miss": "Miss",
                    "leg_penalty": "LegPenalty",
                }.get(detail_sort_key, "")

        limit_n = getattr(args, "limit", 25)
        if limit_n > 0:
            rows = rows[:limit_n]

        print(format_race_header(payload, race_id))
        if show_detail:
            headers = [
                "Rank", "Team", "Results", "Behind", "Course", "Range", "Shoot", "Penalty",
                "Miss",
                "Leg", "Biathlete", "LegResult", "LegBehind", "LegCourse",
                "Lap1", "Lap2", "Lap3",
                "R1", "R2", "S1", "S2",
                "Prone", "Stand",
                "Miss", "LegPenalty",
            ]
        else:
            headers = [
                "Rank", "Team", "Results", "Behind",
                "Course", "Range", "Shoot", "Penalty", "Miss",
            ]
        if show_sort_rank:
            headers.insert(0, "Sort")

        render_rows: list[list[str]] = []
        row_styles: list[str] = []
        if show_detail:
            detail_rows: list[dict] = []
            for row in rows:
                for leg in range(1, 5):
                    detail_rows.append(
                        {
                            "rank": row["rank"],
                            "team": row["team"],
                            "result": row["result"],
                            "behind": row["behind"],
                            "course": row["course"],
                            "range": row["range"],
                            "shooting": row["shooting"],
                            "penalty": row["penalty"],
                            "misses": row["misses"],
                            "leg": leg,
                            "biathlete": row[f"leg{leg}_name"],
                            "leg_result": row[f"leg{leg}_result"],
                            "leg_behind": row[f"leg{leg}_behind"],
                            "leg_course": row[f"leg{leg}_course"],
                            "lap1": row[f"leg{leg}_courselap1"],
                            "lap2": row[f"leg{leg}_courselap2"],
                            "lap3": row[f"leg{leg}_courselap3"],
                            "range1": row[f"leg{leg}_range1"],
                            "range2": row[f"leg{leg}_range2"],
                            "shoot1": row[f"leg{leg}_shooting1"],
                            "shoot2": row[f"leg{leg}_shooting2"],
                            "leg_prone": row[f"leg{leg}_prone"],
                            "leg_standing": row[f"leg{leg}_standing"],
                            "leg_miss": row[f"leg{leg}_miss"],
                            "leg_time": row[f"leg{leg}_time"],
                            "dns": row.get("dns", False),
                        }
                    )
            def detail_sort(entry: dict) -> tuple:
                dns_flag = entry.get("dns", False)
                val = entry.get(detail_sort_key)
                if detail_sort_key in {"leg"}:
                    try:
                        return (1, 0) if dns_flag else (0, int(val))
                    except (TypeError, ValueError):
                        return (0, 10**9)
                if detail_sort_key in {"leg_miss", "leg_prone", "leg_standing"}:
                    shooting = parse_relay_shooting(val) if val not in ("", None, "-") else None
                    if shooting:
                        return (1, 0, 0) if dns_flag else (0, shooting[0], shooting[1])
                    return (0, 9999, 9999)
                if detail_sort_key in {"leg_result", "leg_behind", "leg_time", "leg_course", "lap1", "lap2", "lap3", "range1", "range2", "shoot1", "shoot2", "leg_penalty"}:
                    sec = parse_time_seconds(str(val)) if val not in ("", None, "-") else None
                    if sec is None:
                        return (1, float("inf"))
                    return (1, float("inf")) if dns_flag else (0, sec)
                text = str(val or "").strip().lower()
                return (text == "", text)

            if detail_sort_key:
                detail_rows = sorted(detail_rows, key=detail_sort)

            for entry in detail_rows:
                leg_time_secs = parse_time_seconds(entry.get("leg_time"))
                leg_course_secs = parse_time_seconds(entry.get("leg_course"))
                range1_secs = parse_time_seconds(entry.get("range1"))
                range2_secs = parse_time_seconds(entry.get("range2"))
                if None in (leg_time_secs, leg_course_secs, range1_secs, range2_secs):
                    entry["leg_penalty"] = "-"
                else:
                    penalty_secs = leg_time_secs - leg_course_secs - range1_secs - range2_secs
                    entry["leg_penalty"] = format_seconds(penalty_secs) if penalty_secs >= 0 else "-"

            for idx, entry in enumerate(detail_rows, start=1):
                render_rows.append(
                    [
                        entry["rank"],
                        entry["team"],
                        entry["result"],
                        entry["behind"],
                        entry["course"],
                        entry["range"],
                        entry["shooting"],
                        entry["penalty"],
                        entry["misses"],
                        entry["leg"],
                        entry["biathlete"],
                        entry["leg_result"],
                        entry["leg_behind"],
                        entry["leg_course"],
                        entry["lap1"],
                        entry["lap2"],
                        entry["lap3"],
                        entry["range1"],
                        entry["range2"],
                        entry["shoot1"],
                        entry["shoot2"],
                        entry["leg_prone"],
                        entry["leg_standing"],
                        entry["leg_miss"],
                        entry["leg_penalty"],
                    ]
                )
                if show_sort_rank:
                    render_rows[-1].insert(0, idx)
                row_styles.append(rank_style(entry.get("rank")))
        else:
            for idx, row in enumerate(rows, start=1):
                render_rows.append(
                    [
                        row["rank"],
                        row["team"],
                        row["result"],
                        row["behind"],
                        row["course"],
                        row["range"],
                        row["shooting"],
                        row["penalty"],
                        row["misses"],
                    ]
                )
                if show_sort_rank:
                    render_rows[-1].insert(0, idx)
                row_styles.append(rank_style(row.get("rank")))
        highlight_headers = None
        if show_sort_rank and sort_header and sort_header in headers:
            highlight_headers = [headers.index(sort_header)]
        render_table(
            headers,
            render_rows,
            pretty=is_pretty_output(args),
            row_styles=row_styles if is_pretty_output(args) else None,
            highlight_headers=highlight_headers,
        )
        return 0

    results = extract_results(payload)
    if not results:
        print(f"no results found for race {race_id}", file=sys.stderr)
        return 1

    top_n = getattr(args, "top", 0)
    if top_n > 0:
        cat_id = (payload.get("Competition") or {}).get("catId", "").upper()
        if cat_id in ("SM", "SW"):
            top_ibu_ids = _get_top_n_ibu_ids(cat_id, top_n)
            if top_ibu_ids:
                results = [r for r in results if r.get("IBUId") in top_ibu_ids]

    first_n = getattr(args, "first", 0)
    if first_n > 0:
        results = results[:first_n]

    base_secs = base_time_seconds(results)
    laps = SKI_LAPS.get(discipline, 0)
    stages = SHOOTING_STAGES.get(discipline, 0)

    # Fetch all analytic data in parallel
    from concurrent.futures import ThreadPoolExecutor, as_completed

    course_times: dict[str, str] = {}
    ski_times: dict[str, str] = {}
    range_times: dict[str, str] = {}
    shooting_times: dict[str, str] = {}
    course_laps: dict[int, dict[str, str]] = {}
    range_laps: dict[int, dict[str, str]] = {}
    shooting_laps: dict[int, dict[str, str]] = {}

    # Build list of all fetches needed
    fetches: list[tuple[str, str, int | None]] = [
        ("map", "CRST", None),
        ("map", "SKIT", None),
        ("map", "RNGT", None),
        ("map", "STTM", None),
    ]
    if show_detail and laps:
        for i in range(1, laps + 1):
            fetches.append(("course_lap", f"CRS{i}", i))
    if show_detail and stages:
        for i in range(1, stages + 1):
            fetches.append(("range_lap", f"RNG{i}", i))
            fetches.append(("shooting_lap", f"S{i}TM", i))

    def fetch_one(fetch_type: str, type_id: str, idx: int | None) -> tuple[str, str, int | None, dict[str, str]]:
        return fetch_type, type_id, idx, _fetch_analytic_map(race_id, type_id)

    with ThreadPoolExecutor(max_workers=len(fetches)) as executor:
        futures = [executor.submit(fetch_one, ft, tid, idx) for ft, tid, idx in fetches]
        for future in as_completed(futures):
            fetch_type, type_id, idx, data = future.result()
            if fetch_type == "map":
                if type_id == "CRST":
                    course_times = data
                elif type_id == "SKIT":
                    ski_times = data
                elif type_id == "RNGT":
                    range_times = data
                elif type_id == "STTM":
                    shooting_times = data
            elif fetch_type == "course_lap" and idx is not None:
                course_laps[idx] = data
            elif fetch_type == "range_lap" and idx is not None:
                range_laps[idx] = data
            elif fetch_type == "shooting_lap" and idx is not None:
                shooting_laps[idx] = data

    rows = []
    for res in results:
        ident = str(res.get("IBUId") or res.get("Bib") or res.get("Name") or "")
        rank = res.get("Rank") or res.get("ResultOrder") or ""
        name = res.get("Name") or res.get("ShortName") or ""
        nat = res.get("Nat") or ""
        result_time = normalize_result_time(res, base_secs)
        course_time = _lookup_analytic_time(course_times, res) or get_first_time(
            res, ["TotalCourseTime", "CourseTime", "RunTime"]
        ) or "-"
        range_time = _lookup_analytic_time(range_times, res) or get_first_time(
            res, ["TotalRangeTime", "RangeTime"]
        ) or "-"
        shooting_time = _lookup_analytic_time(shooting_times, res) or get_first_time(
            res, ["TotalShootingTime", "ShootingTime"]
        ) or "-"
        ski_time = _lookup_analytic_time(ski_times, res) or get_first_time(res, [
            "TotalSkiTime", "SkiTime", "SkiTimeTotal", "SKITime", "Ski",
        ]) or "-"

        misses_list = _parse_shootings(res.get("Shootings") or res.get("ShootingTotal"))
        misses_total, prone_total, standing_total = _shooting_totals(misses_list)
        prone1, prone2, standing1, standing2 = _shooting_stages(misses_list, stages)

        start_rank = res.get("StartOrder") or res.get("StartPosition") or "-"
        start_delay = res.get("StartInfo") or "-"
        gain = "-"
        try:
            gain = int(start_rank) - int(rank)
        except (TypeError, ValueError):
            gain = "-"
        pursuit_time = "-"
        if discipline == "PU":
            pursuit_time = _calculate_pursuit_time(result_time, str(start_delay))

        if discipline == "IN":
            penalty = format_seconds(misses_total * 60) if misses_list else "-"
        else:
            base_time = pursuit_time if discipline == "PU" else result_time
            result_secs = parse_time_seconds(base_time)
            course_secs = parse_time_seconds(course_time)
            range_secs = parse_time_seconds(range_time)
            if result_secs is None or course_secs is None or range_secs is None:
                penalty = "-"
            else:
                penalty_secs = result_secs - course_secs - range_secs
                penalty = format_seconds(penalty_secs) if penalty_secs >= 0 else "-"

        row = {
            "rank": rank,
            "biathlete": name,
            "country": nat,
            "startrank": start_rank,
            "gain": gain,
            "startdelay": start_delay,
            "result": result_time,
            "pursuittime": pursuit_time,
            "ski": ski_time,
            "course": course_time,
            "range": range_time,
            "shooting": shooting_time,
            "penalty": penalty,
            "misses": str(misses_total) if misses_list else "-",
            "prone1": prone1,
            "prone2": prone2,
            "standing1": standing1,
            "standing2": standing2,
            "dns": is_dns(res),
            "ibu_id": res.get("IBUId") or "",
        }
        if show_detail:
            for lap in range(1, laps + 1):
                row[f"courselap{lap}"] = _lookup_analytic_time(course_laps.get(lap, {}), res) or "-"
            for stage in range(1, stages + 1):
                row[f"range{stage}"] = _lookup_analytic_time(range_laps.get(stage, {}), res) or "-"
                row[f"shoot{stage}"] = _lookup_analytic_time(shooting_laps.get(stage, {}), res) or "-"
        rows.append(row)

    country_filter = getattr(args, "country", "").upper()
    if country_filter:
        rows = [r for r in rows if r.get("country", "").upper() == country_filter]

    sort_col = getattr(args, "sort", "").lower().replace(" ", "")
    show_sort_rank = False
    sort_header = ""
    if sort_col:
        sort_map = {
            "results": "result",
            "result": "result",
            "pursuittime": "pursuittime",
            "pursuit": "pursuittime",
            "startrank": "startrank",
            "start": "startrank",
            "startdelay": "startdelay",
            "delay": "startdelay",
            "gain": "gain",
            "ski": "ski",
            "course": "course",
            "range": "range",
            "shooting": "shooting",
            "shoot": "shooting",
            "penalty": "penalty",
            "misses": "misses",
            "miss": "misses",
        }
        if show_detail:
            detail_map = {
                "lap1": "courselap1",
                "lap2": "courselap2",
                "lap3": "courselap3",
                "lap4": "courselap4",
                "lap5": "courselap5",
                "r1": "range1",
                "r2": "range2",
                "r3": "range3",
                "r4": "range4",
                "s1": "shoot1",
                "s2": "shoot2",
                "s3": "shoot3",
                "s4": "shoot4",
                "pr1": "prone1",
                "pr2": "prone2",
                "st1": "standing1",
                "st2": "standing2",
            }
            if discipline == "SP":
                detail_map.update({"pr": "prone1", "st": "standing1"})
            sort_map.update(detail_map)
        sort_key = sort_map.get(sort_col)
        if not sort_key:
            valid = ", ".join(sort_map.keys())
            print(f"error: sort must be one of {valid}", file=sys.stderr)
            return 1
        rows = _sort_rows(rows, sort_key, relay=False)
        show_sort_rank = True
        sort_header = {
            "result": "Results",
            "pursuittime": "Pursuit",
            "startrank": "Start",
            "startdelay": "Delay",
            "gain": "Gain",
            "ski": "Ski",
            "course": "Course",
            "range": "Range",
            "shooting": "Shoot",
            "penalty": "Penalty",
            "misses": "Miss",
            "courselap1": "Lap1",
            "courselap2": "Lap2",
            "courselap3": "Lap3",
            "courselap4": "Lap4",
            "courselap5": "Lap5",
            "range1": "R1",
            "range2": "R2",
            "range3": "R3",
            "range4": "R4",
            "shoot1": "S1",
            "shoot2": "S2",
            "shoot3": "S3",
            "shoot4": "S4",
            "prone1": "Pr1",
            "prone2": "Pr2",
            "standing1": "St1",
            "standing2": "St2",
        }.get(sort_key, "")

    limit_n = getattr(args, "limit", 25)
    if limit_n > 0:
        rows = rows[:limit_n]

    print(format_race_header(payload, race_id))

    headers: list[str] = []
    if discipline == "PU":
        headers = [
            "Rank", "Biathlete", "Nat", "Start", "Gain", "Delay",
            "Results", "Pursuit", "Course", "Range", "Shoot", "Penalty",
            "Miss",
        ]
    else:
        headers = [
            "Rank", "Biathlete", "Nat", "Results",
            "Ski" if discipline == "IN" else "",
            "Course", "Range", "Shoot", "Penalty", "Miss",
        ]
        headers = [h for h in headers if h]

    if show_detail:
        if discipline == "SP":
            detail_headers = [
                "Lap1", "Lap2", "Lap3",
                "R1", "R2", "S1", "S2", "Pr", "St",
            ]
        else:
            detail_headers = [
                "Lap1", "Lap2", "Lap3",
            ]
            if laps >= 4:
                detail_headers.append("Lap4")
            if laps >= 5:
                detail_headers.append("Lap5")
            detail_headers.extend([
                "R1", "R2", "R3", "R4",
                "S1", "S2", "S3", "S4",
                "Pr1", "Pr2", "St1", "St2",
            ])
        headers.extend(detail_headers)
    if show_sort_rank:
        headers.insert(0, "Sort")

    render_rows: list[list[str]] = []
    for idx, row in enumerate(rows, start=1):
        base = []
        if discipline == "PU":
            gain_val = row["gain"]
            if isinstance(gain_val, int) and gain_val > 0:
                gain_val = f"+{gain_val}"
            base = [
                row["rank"],
                row["biathlete"],
                row["country"],
                row["startrank"],
                gain_val,
                row["startdelay"],
                row["result"],
                row["pursuittime"],
                row["course"],
                row["range"],
                row["shooting"],
                row["penalty"],
                row["misses"],
            ]
        else:
            base = [
                row["rank"],
                row["biathlete"],
                row["country"],
                row["result"],
            ]
            if discipline == "IN":
                base.append(row["ski"])
            base.extend([
                row["course"],
                row["range"],
                row["shooting"],
                row["penalty"],
                row["misses"],
            ])

        if show_detail:
            if discipline == "SP":
                base.extend([
                    row.get("courselap1", "-"),
                    row.get("courselap2", "-"),
                    row.get("courselap3", "-"),
                    row.get("range1", "-"),
                    row.get("range2", "-"),
                    row.get("shoot1", "-"),
                    row.get("shoot2", "-"),
                    row.get("prone1", "-"),
                    row.get("standing1", "-"),
                ])
            else:
                base.extend([
                    row.get("courselap1", "-"),
                    row.get("courselap2", "-"),
                    row.get("courselap3", "-"),
                ])
                if laps >= 4:
                    base.append(row.get("courselap4", "-"))
                if laps >= 5:
                    base.append(row.get("courselap5", "-"))
                base.extend([
                    row.get("range1", "-"),
                    row.get("range2", "-"),
                    row.get("range3", "-"),
                    row.get("range4", "-"),
                    row.get("shoot1", "-"),
                    row.get("shoot2", "-"),
                    row.get("shoot3", "-"),
                    row.get("shoot4", "-"),
                    row.get("prone1", "-"),
                    row.get("prone2", "-"),
                    row.get("standing1", "-"),
                    row.get("standing2", "-"),
                ])

        if show_sort_rank:
            base.insert(0, idx)
        render_rows.append(base)

    row_styles = None
    if is_pretty_output(args):
        if getattr(args, "highlight_wc", False):
            cat_id = (payload.get("Competition") or {}).get("catId", "").upper()
            if cat_id in ("SM", "SW"):
                wc_rank_map = _get_wc_rank_map(cat_id, 6)
                row_styles = [rank_style(wc_rank_map.get(row.get("ibu_id"))) for row in rows]
        if row_styles is None:
            row_styles = [rank_style(row.get("rank")) for row in rows]
    highlight_headers = None
    if show_sort_rank and sort_header and sort_header in headers:
        highlight_headers = [headers.index(sort_header)]
    render_table(
        headers,
        render_rows,
        pretty=is_pretty_output(args),
        row_styles=row_styles,
        highlight_headers=highlight_headers,
    )
    return 0
