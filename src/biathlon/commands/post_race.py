"""Post-race analysis command handler."""

from __future__ import annotations

import argparse
import re
import sys
from typing import Any

from ..api import (
    BiathlonError,
    get_all_results,
    get_analytic_results,
    get_cup_results,
    get_current_season_id,
    get_race_results,
)
from ..constants import (
    INDIVIDUAL_DISCIPLINES,
    RELAY_DISCIPLINE,
    SINGLE_MIXED_RELAY_DISCIPLINE,
    SKI_LAPS,
)
from ..formatting import Color, is_pretty_output, render_table
from ..utils import format_race_header, get_first_time, parse_time_seconds, format_seconds
from .relay import _has_completed_results as _has_completed_relay_results
from .results import _find_latest_race_with_results_any, _has_completed_results
from .startlist import _get_cup_ids_for_race


MAJOR_LEVELS = {"WC", "WCH", "OWG"}
TOP_N = 6
DISCIPLINE_LABELS = {
    "SP": "Sprint",
    "PU": "Pursuit",
    "IN": "Individual",
    "MS": "Mass Start",
}


def _format_section_title(text: str, args: argparse.Namespace) -> str:
    if not is_pretty_output(args):
        return text
    return Color.section_title(text)


def _parse_rank(value: Any) -> int | None:
    text = str(value).strip().rstrip(".")
    if text.isdigit():
        return int(text)
    return None


def _parse_int(value: Any) -> int | None:
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _is_relay_discipline(discipline: str) -> bool:
    return discipline in {RELAY_DISCIPLINE, SINGLE_MIXED_RELAY_DISCIPLINE}


def _row_ibu_id(row: dict) -> str:
    for key in ("IBUId", "IbuId", "ibuId", "Id"):
        val = row.get(key)
        if val:
            return str(val)
    return ""


def _sort_results_by_rank(results: list[dict]) -> list[dict]:
    def _key(res: dict) -> tuple[int, int]:
        rank_val = _parse_rank(res.get("Rank") or res.get("Standing") or res.get("ResultOrder"))
        order_val = _parse_rank(res.get("ResultOrder")) or 10**9
        return (rank_val if rank_val is not None else 10**9, order_val)

    return sorted(results, key=_key)


def _collect_flower_entries(results: list[dict], is_team: bool) -> list[dict]:
    filtered = [res for res in results if bool(res.get("IsTeam")) == is_team]
    sorted_results = _sort_results_by_rank(filtered)
    entries = []
    for res in sorted_results:
        rank_val = _parse_rank(res.get("Rank") or res.get("Standing") or res.get("ResultOrder"))
        if rank_val is None or rank_val > TOP_N:
            continue
        name = res.get("Name") or res.get("ShortName") or ""
        nat = res.get("Nat") or ""
        if is_team and not name:
            name = nat
        entries.append({
            "rank": rank_val,
            "name": name,
            "nat": nat,
            "ibu_id": str(res.get("IBUId") or ""),
        })
        if len(entries) >= TOP_N:
            break
    return entries


def _fetch_cup_rows(cup_id: str | None) -> list[dict]:
    if not cup_id:
        return []
    try:
        payload = get_cup_results(cup_id)
    except BiathlonError:
        return []
    return payload.get("Rows") or payload.get("Results") or []


def _build_standings_lookup(rows: list[dict]) -> tuple[dict[str, dict], dict[str, dict]]:
    by_id: dict[str, dict] = {}
    by_name: dict[str, dict] = {}
    for row in rows:
        ibu_id = _row_ibu_id(row)
        name = row.get("Name") or row.get("ShortName") or ""
        if ibu_id:
            by_id[ibu_id] = row
        if name:
            by_name[name] = row
    return by_id, by_name


def _format_rank_change(row: dict | None) -> str:
    if not row:
        return "-"
    rank_val = _parse_rank(row.get("Rank") or row.get("Standing") or row.get("ResultOrder"))
    if rank_val is None:
        return "-"

    diff_val = None
    for key in ("RnkDiff", "RankDiff", "RankChange"):
        if key in row:
            diff_val = _parse_int(row.get(key))
            if diff_val is None:
                diff_val = 0
            break

    if diff_val is None:
        return str(rank_val)

    diff_text = f"{diff_val:+d}"
    return f"{rank_val} ({diff_text})"


def _make_key(ibu_id: str | None, bib: str | None, name: str | None, leg: int | None) -> str:
    if leg is not None:
        if ibu_id:
            return f"{ibu_id}:{leg}"
        if bib:
            return f"{bib}:{leg}"
    if ibu_id:
        return str(ibu_id)
    if bib:
        return f"bib:{bib}"
    return str(name or "")


def _entry_key(entry: dict) -> str:
    return _make_key(entry.get("IBUId"), entry.get("Bib"), entry.get("Name"), entry.get("Leg"))


def _analytic_key(entry: dict) -> str:
    return _make_key(entry.get("IBUId"), entry.get("Bib"), entry.get("Name"), entry.get("Leg"))


def _key_matches_ibu_id(key: str, ibu_id: str) -> bool:
    if key == ibu_id:
        return True
    return key.startswith(f"{ibu_id}:")


def _extract_leg_from_key(key: str) -> int | None:
    if ":" not in key:
        return None
    suffix = key.rsplit(":", 1)[-1]
    if suffix.isdigit():
        return int(suffix)
    return None


def _parse_stage_misses(shootings: str | None) -> list[int]:
    if not shootings:
        return []
    text = str(shootings).strip()
    if not text:
        return []
    if " " in text:
        misses = []
        for part in text.split():
            digits = re.findall(r"\d+", part)
            if digits:
                misses.append(sum(int(d) for d in digits))
        return misses
    misses = []
    for part in text.split("+"):
        part = part.strip()
        if not part:
            continue
        try:
            misses.append(int(part))
        except ValueError:
            digits = re.findall(r"\d+", part)
            if digits:
                misses.append(sum(int(d) for d in digits))
    return misses


def _stage_miss_for_index(stage_misses: list[int], stage_idx: int, discipline: str) -> int | None:
    if not stage_misses:
        return None
    if _is_relay_discipline(discipline):
        local_idx = (stage_idx - 1) % 2
    else:
        local_idx = stage_idx - 1
    if local_idx < 0 or local_idx >= len(stage_misses):
        return None
    return stage_misses[local_idx]


def _stage_label(stage_idx: int, discipline: str, leg: int | None) -> str:
    if _is_relay_discipline(discipline):
        local_idx = (stage_idx - 1) % 2
        stage = "Prone" if local_idx == 0 else "Standing"
        return f"Leg{leg} {stage}" if leg else stage
    stage = "Prone" if stage_idx % 2 == 1 else "Standing"
    if stage_idx <= 2:
        return stage
    stage_no = (stage_idx + 1) // 2
    return f"{stage}{stage_no}"


def _next_podium_milestone(count: int) -> int | None:
    if count == 1 or count % 5 == 0:
        return count
    return None


def _next_flower_milestone(count: int) -> int | None:
    if count == 1 or count % 5 == 0:
        return count
    return None


def _fetch_stage_times_by_stage(
    race_id: str,
    cache: dict[str, dict[int, dict[str, float]]],
) -> dict[int, dict[str, float]]:
    if race_id in cache:
        return cache[race_id]
    times: dict[int, dict[str, float]] = {}
    for idx in range(1, 9):
        type_id = f"S{idx}TM"
        try:
            analytic = get_analytic_results(race_id, type_id)
        except BiathlonError:
            continue
        for res in analytic.get("Results", []) or []:
            if res.get("IsTeam"):
                continue
            key = _analytic_key(res)
            if not key:
                continue
            time_str = get_first_time(res, ["TotalTime", "Result"])
            secs = parse_time_seconds(time_str) if time_str else None
            if secs is None:
                continue
            times.setdefault(idx, {})[key] = secs
    cache[race_id] = times
    return times


def _fetch_lap_times(race_id: str, discipline: str) -> list[dict]:
    if discipline == RELAY_DISCIPLINE:
        max_laps = 12
        laps_per_leg = 3
    elif discipline == SINGLE_MIXED_RELAY_DISCIPLINE:
        max_laps = 8
        laps_per_leg = 2
    else:
        max_laps = SKI_LAPS.get(discipline, 3)
        laps_per_leg = max_laps

    lap_rows: list[dict] = []
    for idx in range(1, max_laps + 1):
        type_id = f"CRS{idx}"
        try:
            analytic = get_analytic_results(race_id, type_id)
        except BiathlonError:
            continue
        for res in analytic.get("Results", []) or []:
            if res.get("IsTeam"):
                continue
            time_str = get_first_time(res, ["TotalTime", "Result"])
            secs = parse_time_seconds(time_str) if time_str else None
            if secs is None:
                continue
            leg = res.get("Leg")
            if leg is None and _is_relay_discipline(discipline):
                leg = (idx - 1) // laps_per_leg + 1
            lap_rows.append({
                "secs": secs,
                "time": format_seconds(secs),
                "name": res.get("Name") or res.get("ShortName") or "",
                "nat": res.get("Nat") or "",
                "lap": idx,
                "leg": leg,
            })
    lap_rows.sort(key=lambda row: row["secs"])
    return lap_rows[:TOP_N]


def handle_post_race(args: argparse.Namespace) -> int:
    """Show post-race highlights and milestones."""
    try:
        if args.race:
            race_id = args.race
            payload = get_race_results(race_id)
        else:
            race_id, payload = _find_latest_race_with_results_any()
    except BiathlonError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    comp = payload.get("Competition") or {}
    discipline = str(comp.get("DisciplineId") or "").upper()
    is_relay = _is_relay_discipline(discipline)
    if is_relay:
        if not _has_completed_relay_results(payload):
            print(f"race {race_id} does not have completed results", file=sys.stderr)
            return 1
    else:
        if not _has_completed_results(payload):
            print(f"race {race_id} does not have completed results", file=sys.stderr)
            return 1

    results = payload.get("Results", []) or []
    team_results = [r for r in results if r.get("IsTeam")]
    leg_results = [r for r in results if not r.get("IsTeam")]
    flower_entries = _collect_flower_entries(results, is_relay)

    entries = []
    key_to_entry: dict[str, dict] = {}
    for res in leg_results:
        key = _entry_key(res)
        entry = {
            "key": key,
            "ibu_id": str(res.get("IBUId") or ""),
            "name": res.get("Name") or res.get("ShortName") or "",
            "nat": res.get("Nat") or "",
            "bib": str(res.get("Bib") or ""),
            "leg": res.get("Leg"),
            "shootings": res.get("Shootings") or res.get("ShootingTotal") or "",
            "rank": res.get("Rank") or res.get("ResultOrder") or "",
            "time": res.get("TotalTime") or res.get("Result") or "",
        }
        if key:
            key_to_entry[key] = entry
        if entry["bib"] and entry["leg"] is not None:
            key_to_entry.setdefault(f"{entry['bib']}:{entry['leg']}", entry)
        entries.append(entry)

    winners: set[str] = set()
    podiumers: set[str] = set()
    flowers: set[str] = set()
    if is_relay:
        team_ranks: dict[str, int] = {}
        for team in team_results:
            bib = str(team.get("Bib") or "")
            rank_val = _parse_rank(team.get("Rank"))
            if bib and rank_val is not None:
                team_ranks[bib] = rank_val
        winning_bibs = {bib for bib, rank_val in team_ranks.items() if rank_val == 1}
        for entry in entries:
            if entry["bib"] and entry["bib"] in winning_bibs:
                if entry["ibu_id"]:
                    winners.add(entry["ibu_id"])
            rank_val = team_ranks.get(entry["bib"])
            if rank_val is not None and entry["ibu_id"]:
                if rank_val <= 3:
                    podiumers.add(entry["ibu_id"])
                elif 4 <= rank_val <= 6:
                    flowers.add(entry["ibu_id"])
    else:
        for entry in entries:
            rank_val = _parse_rank(entry["rank"])
            if rank_val == 1 and entry["ibu_id"]:
                winners.add(entry["ibu_id"])
            if rank_val is not None and entry["ibu_id"]:
                if rank_val <= 3:
                    podiumers.add(entry["ibu_id"])
                elif 4 <= rank_val <= 6:
                    flowers.add(entry["ibu_id"])

    print()
    print(_format_section_title(format_race_header(payload, race_id), args))
    print()

    if flower_entries:
        headers = ["Rank", "Team", "Nat"] if is_relay else ["Rank", "Athlete", "Nat"]
        rows = [[entry["rank"], entry["name"], entry["nat"]] for entry in flower_entries]
        print(_format_section_title("Winner + flower ceremony (top 6):", args))
        render_table(headers, rows, pretty=is_pretty_output(args))
        print()
    else:
        print(_format_section_title("Winner + flower ceremony (top 6): none", args))
        print()

    if flower_entries and not is_relay and discipline in INDIVIDUAL_DISCIPLINES:
        comp = payload.get("Competition") or {}
        cat_id = str(comp.get("catId") or comp.get("CatId") or "").upper()
        sport_evt = payload.get("SportEvt") or {}
        season_id = str(sport_evt.get("SeasonId") or "") or get_current_season_id()
        total_cup_id, disc_cup_id = _get_cup_ids_for_race(season_id, cat_id, discipline)
        total_rows = _fetch_cup_rows(total_cup_id)
        disc_rows = _fetch_cup_rows(disc_cup_id)
        if total_rows or disc_rows:
            total_by_id, total_by_name = _build_standings_lookup(total_rows)
            disc_by_id, disc_by_name = _build_standings_lookup(disc_rows)
            disc_label = DISCIPLINE_LABELS.get(discipline, discipline)
            standings_rows = []
            for entry in flower_entries:
                total_row = total_by_id.get(entry["ibu_id"]) or total_by_name.get(entry["name"])
                disc_row = disc_by_id.get(entry["ibu_id"]) or disc_by_name.get(entry["name"])
                standings_rows.append([
                    entry["name"],
                    entry["nat"],
                    _format_rank_change(total_row),
                    _format_rank_change(disc_row),
                ])
            print(_format_section_title(
                f"World Cup standing changes (Total + {disc_label}):",
                args,
            ))
            render_table(
                ["Athlete", "Nat", "WC Total", f"{disc_label} WC"],
                standings_rows,
                pretty=is_pretty_output(args),
            )
            print()
        else:
            print(_format_section_title("World Cup standing changes: no data available", args))
            print()

    use_major = bool(getattr(args, "major", False))
    level_set = MAJOR_LEVELS if use_major else {"WC"}

    race_milestones = []
    win_milestones = []
    podium_milestones = []
    flower_milestones = []
    race_milestone_ids: set[str] = set()
    win_milestone_ids: set[str] = set()
    podium_milestone_ids: set[str] = set()
    flower_milestone_ids: set[str] = set()
    all_results_cache: dict[str, list[dict]] = {}
    stage_cache: dict[str, dict[int, dict[str, float]]] = {}

    for entry in entries:
        ibu_id = entry["ibu_id"]
        if not ibu_id:
            continue
        if ibu_id not in all_results_cache:
            try:
                all_payload = get_all_results(ibu_id)
            except BiathlonError:
                all_payload = {}
            all_results_cache[ibu_id] = list(all_payload.get("Results") or [])
        all_results = all_results_cache[ibu_id]
        race_row = next((r for r in all_results if r.get("RaceId") == race_id), None)
        if not race_row:
            continue
        race_level = str(race_row.get("Level") or "").upper()
        if race_level not in level_set:
            continue
        level_results = [r for r in all_results if str(r.get("Level") or "").upper() in level_set]
        race_count = len(level_results)
        win_count = 0
        podium_count = 0
        flower_count = 0
        for res in level_results:
            rank_val = _parse_rank(res.get("Rank") or res.get("SO"))
            if rank_val is None:
                continue
            if rank_val == 1:
                win_count += 1
            if rank_val <= 3:
                podium_count += 1
            elif 4 <= rank_val <= 6:
                flower_count += 1
        if (race_count == 1 or race_count % 25 == 0) and ibu_id not in race_milestone_ids:
            race_milestones.append([race_count, entry["name"], entry["nat"]])
            race_milestone_ids.add(ibu_id)
        if ibu_id in winners and win_count % 5 == 0 and ibu_id not in win_milestone_ids:
            win_milestones.append([win_count, entry["name"], entry["nat"]])
            win_milestone_ids.add(ibu_id)
        if ibu_id in podiumers:
            milestone = _next_podium_milestone(podium_count)
            if milestone and ibu_id not in podium_milestone_ids:
                podium_milestones.append([milestone, entry["name"], entry["nat"]])
                podium_milestone_ids.add(ibu_id)
        if ibu_id in flowers:
            milestone = _next_flower_milestone(flower_count)
            if milestone and ibu_id not in flower_milestone_ids:
                flower_milestones.append([milestone, entry["name"], entry["nat"]])
                flower_milestone_ids.add(ibu_id)

    if race_milestones:
        race_milestones.sort(key=lambda row: row[0], reverse=True)
        label = "World Cup + WCH + OWG race milestones:" if use_major else "World Cup race milestones:"
        print(_format_section_title(label, args))
        render_table(["Milestone", "Athlete", "Nat"], race_milestones, pretty=is_pretty_output(args))
        print()
    else:
        label = "World Cup + WCH + OWG race milestones: none" if use_major else "World Cup race milestones: none"
        print(_format_section_title(label, args))
        print()

    if win_milestones:
        win_milestones.sort(key=lambda row: row[0], reverse=True)
        label = (
            "World Cup + WCH + OWG win milestones:"
            if use_major
            else "World Cup win milestones:"
        )
        print(_format_section_title(label, args))
        render_table(["Milestone", "Athlete", "Nat"], win_milestones, pretty=is_pretty_output(args))
        print()
    else:
        label = "World Cup + WCH + OWG win milestones: none" if use_major else "World Cup win milestones: none"
        print(_format_section_title(label, args))
        print()

    if podium_milestones:
        podium_milestones.sort(key=lambda row: row[0], reverse=True)
        label = (
            "World Cup + WCH + OWG podium milestones:"
            if use_major
            else "World Cup podium milestones:"
        )
        print(_format_section_title(label, args))
        render_table(["Milestone", "Athlete", "Nat"], podium_milestones, pretty=is_pretty_output(args))
        print()
    else:
        label = "World Cup + WCH + OWG podium milestones: none" if use_major else "World Cup podium milestones: none"
        print(_format_section_title(label, args))
        print()

    if flower_milestones:
        flower_milestones.sort(key=lambda row: row[0], reverse=True)
        label = (
            "World Cup + WCH + OWG flower ceremony milestones:"
            if use_major
            else "World Cup flower ceremony milestones:"
        )
        print(_format_section_title(label, args))
        render_table(["Milestone", "Athlete", "Nat"], flower_milestones, pretty=is_pretty_output(args))
        print()
    else:
        label = (
            "World Cup + WCH + OWG flower ceremony milestones: none"
            if use_major
            else "World Cup flower ceremony milestones: none"
        )
        print(_format_section_title(label, args))
        print()

    lap_rows = _fetch_lap_times(race_id, discipline)
    if lap_rows:
        print(_format_section_title("Top 6 fastest laps:", args))
        headers = ["Time", "Athlete", "Nat", "Lap"]
        if is_relay:
            headers.insert(3, "Leg")
        rows = []
        for row in lap_rows:
            data = [row["time"], row["name"], row["nat"], row["lap"]]
            if is_relay:
                data.insert(3, row["leg"] or "-")
            rows.append(data)
        render_table(headers, rows, pretty=is_pretty_output(args))
        print()
    else:
        print(_format_section_title("Top 6 fastest laps: none", args))
        print()

    if is_relay:
        leg_times = []
        legs_by_bib: dict[str, dict[int, float]] = {}
        entry_by_leg: dict[tuple[str, int], dict] = {}
        for entry in entries:
            if not entry["bib"] or entry["leg"] is None:
                continue
            time_str = entry["time"]
            if isinstance(time_str, str) and time_str.startswith("+"):
                continue
            secs = parse_time_seconds(time_str) if time_str else None
            if secs is None:
                continue
            bib = entry["bib"]
            leg = int(entry["leg"])
            legs_by_bib.setdefault(bib, {})[leg] = secs
            entry_by_leg[(bib, leg)] = entry
        for bib, leg_times_map in legs_by_bib.items():
            prev_secs = None
            for leg in sorted(leg_times_map.keys()):
                total_secs = leg_times_map[leg]
                leg_secs = total_secs if prev_secs is None else total_secs - prev_secs
                if leg_secs <= 0:
                    prev_secs = total_secs
                    continue
                entry = entry_by_leg.get((bib, leg))
                if not entry:
                    prev_secs = total_secs
                    continue
                leg_times.append([
                    leg_secs,
                    entry["name"],
                    entry["nat"],
                    leg,
                    format_seconds(leg_secs),
                ])
                prev_secs = total_secs
        leg_times.sort(key=lambda row: row[0])
        leg_times = leg_times[:TOP_N]
        if leg_times:
            print(_format_section_title("Top 6 fastest legs (total time):", args))
            rows = [[row[4], row[1], row[2], row[3]] for row in leg_times]
            render_table(["Time", "Athlete", "Nat", "Leg"], rows, pretty=is_pretty_output(args))
            print()
        else:
            print(_format_section_title("Top 6 fastest legs (total time): none", args))
            print()

        from .relay import _fetch_analytic_times
        crst_times = _fetch_analytic_times(race_id, "CRST")
        leg_info = {(entry["bib"], entry["leg"]): entry for entry in entries if entry["bib"] and entry["leg"]}
        leg_course_rows = []
        for (bib, leg), secs in crst_times.items():
            entry = leg_info.get((bib, leg))
            if not entry:
                continue
            leg_course_rows.append([secs, entry["name"], entry["nat"], leg, format_seconds(secs)])
        leg_course_rows.sort(key=lambda row: row[0])
        leg_course_rows = leg_course_rows[:TOP_N]
        if leg_course_rows:
            print(_format_section_title("Top 6 fastest legs (course time):", args))
            rows = [[row[4], row[1], row[2], row[3]] for row in leg_course_rows]
            render_table(["Time", "Athlete", "Nat", "Leg"], rows, pretty=is_pretty_output(args))
            print()
        else:
            print(_format_section_title("Top 6 fastest legs (course time): none", args))
            print()

    stage_times = _fetch_stage_times_by_stage(race_id, stage_cache)
    stage_misses_map: dict[str, list[int]] = {}
    for entry in entries:
        misses = _parse_stage_misses(entry["shootings"])
        if entry["key"]:
            stage_misses_map[entry["key"]] = misses
        if entry["bib"] and entry["leg"] is not None:
            stage_misses_map[f"{entry['bib']}:{entry['leg']}"] = misses
    zero_miss_rows = []
    for stage_idx, times in stage_times.items():
        for key, secs in times.items():
            entry = key_to_entry.get(key)
            if not entry:
                continue
            misses = _stage_miss_for_index(stage_misses_map.get(key, []), stage_idx, discipline)
            if misses is None or misses != 0:
                continue
            stage_label = _stage_label(stage_idx, discipline, entry.get("leg"))
            zero_miss_rows.append([secs, entry["name"], entry["nat"], format_seconds(secs), stage_label])
    zero_miss_rows.sort(key=lambda row: row[0])
    zero_miss_rows = zero_miss_rows[:TOP_N]
    if zero_miss_rows:
        print(_format_section_title("Top 6 fastest shooters (0 miss):", args))
        headers = ["Time", "Athlete", "Nat", "Stage"]
        rows = []
        for row in zero_miss_rows:
            data = [row[3], row[1], row[2], row[4] or "-"]
            rows.append(data)
        render_table(headers, rows, pretty=is_pretty_output(args))
        print()
    else:
        print(_format_section_title("Top 6 fastest shooters (0 miss): none", args))
        print()

    return 0
