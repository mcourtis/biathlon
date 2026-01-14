"""Post-race analysis command handler."""

from __future__ import annotations

import argparse
import re
import sys
from typing import Any

from ..api import BiathlonError, get_all_results, get_analytic_results, get_race_results
from ..constants import RELAY_DISCIPLINE, SINGLE_MIXED_RELAY_DISCIPLINE, SKI_LAPS
from ..formatting import is_pretty_output, render_table
from ..utils import format_race_header, get_first_time, parse_time_seconds, format_seconds
from .relay import _has_completed_results as _has_completed_relay_results
from .results import _find_latest_race_with_results_any, _has_completed_results


MAJOR_LEVELS = {"WC", "WCH", "OWG"}
TOP_N = 6


def _parse_rank(value: Any) -> int | None:
    text = str(value).strip().rstrip(".")
    if text.isdigit():
        return int(text)
    return None


def _is_relay_discipline(discipline: str) -> bool:
    return discipline in {RELAY_DISCIPLINE, SINGLE_MIXED_RELAY_DISCIPLINE}


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


def _best_zero_miss_stage(
    ibu_id: str,
    all_results: list[dict],
    stage_cache: dict[str, dict[int, dict[str, float]]],
) -> tuple[float | None, str, str]:
    best_time: float | None = None
    best_race = ""
    best_stage = ""
    for res in all_results:
        race_id = res.get("RaceId") or ""
        if not race_id:
            continue
        discipline = str(res.get("Comp") or "").upper()
        stage_misses = _parse_stage_misses(res.get("Shootings") or res.get("ShootingTotal"))
        if not stage_misses:
            continue
        stage_times = _fetch_stage_times_by_stage(race_id, stage_cache)
        for stage_idx, times in stage_times.items():
            for key, secs in times.items():
                if not _key_matches_ibu_id(key, ibu_id):
                    continue
                miss_val = _stage_miss_for_index(stage_misses, stage_idx, discipline)
                if miss_val is None or miss_val != 0:
                    continue
                if best_time is None or secs < best_time:
                    best_time = secs
                    best_race = race_id
                    best_stage = _stage_label(stage_idx, discipline, _extract_leg_from_key(key))
    return best_time, best_race, best_stage


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
    print(format_race_header(payload, race_id))
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
        print(label)
        render_table(["Milestone", "Athlete", "Nat"], race_milestones, pretty=is_pretty_output(args))
        print()
    else:
        label = "World Cup + WCH + OWG race milestones: none" if use_major else "World Cup race milestones: none"
        print(label)
        print()

    if win_milestones:
        win_milestones.sort(key=lambda row: row[0], reverse=True)
        label = (
            "World Cup + WCH + OWG win milestones:"
            if use_major
            else "World Cup win milestones:"
        )
        print(label)
        render_table(["Milestone", "Athlete", "Nat"], win_milestones, pretty=is_pretty_output(args))
        print()
    else:
        label = "World Cup + WCH + OWG win milestones: none" if use_major else "World Cup win milestones: none"
        print(label)
        print()

    if podium_milestones:
        podium_milestones.sort(key=lambda row: row[0], reverse=True)
        label = (
            "World Cup + WCH + OWG podium milestones:"
            if use_major
            else "World Cup podium milestones:"
        )
        print(label)
        render_table(["Milestone", "Athlete", "Nat"], podium_milestones, pretty=is_pretty_output(args))
        print()
    else:
        label = "World Cup + WCH + OWG podium milestones: none" if use_major else "World Cup podium milestones: none"
        print(label)
        print()

    if flower_milestones:
        flower_milestones.sort(key=lambda row: row[0], reverse=True)
        label = (
            "World Cup + WCH + OWG flower ceremony milestones:"
            if use_major
            else "World Cup flower ceremony milestones:"
        )
        print(label)
        render_table(["Milestone", "Athlete", "Nat"], flower_milestones, pretty=is_pretty_output(args))
        print()
    else:
        label = (
            "World Cup + WCH + OWG flower ceremony milestones: none"
            if use_major
            else "World Cup flower ceremony milestones: none"
        )
        print(label)
        print()

    lap_rows = _fetch_lap_times(race_id, discipline)
    if lap_rows:
        print("Top 6 fastest laps:")
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
        print("Top 6 fastest laps: none")
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
            print("Top 6 fastest legs (total time):")
            rows = [[row[4], row[1], row[2], row[3]] for row in leg_times]
            render_table(["Time", "Athlete", "Nat", "Leg"], rows, pretty=is_pretty_output(args))
            print()
        else:
            print("Top 6 fastest legs (total time): none")
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
            print("Top 6 fastest legs (course time):")
            rows = [[row[4], row[1], row[2], row[3]] for row in leg_course_rows]
            render_table(["Time", "Athlete", "Nat", "Leg"], rows, pretty=is_pretty_output(args))
            print()
        else:
            print("Top 6 fastest legs (course time): none")
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
        print("Top 6 fastest shooters (0 miss):")
        headers = ["Time", "Athlete", "Nat", "Stage"]
        rows = []
        for row in zero_miss_rows:
            data = [row[3], row[1], row[2], row[4] or "-"]
            rows.append(data)
        render_table(headers, rows, pretty=is_pretty_output(args))
        print()
    else:
        print("Top 6 fastest shooters (0 miss): none")
        print()

    record_rows = []
    best_stage_map: dict[str, tuple[float | None, str, str]] = {}
    for entry in entries:
        ibu_id = entry["ibu_id"]
        if not ibu_id or ibu_id in best_stage_map:
            continue
        all_results = all_results_cache.get(ibu_id)
        if all_results is None:
            try:
                all_payload = get_all_results(ibu_id)
            except BiathlonError:
                continue
            all_results = list(all_payload.get("Results") or [])
            all_results_cache[ibu_id] = all_results
        best_stage_map[ibu_id] = _best_zero_miss_stage(ibu_id, all_results, stage_cache)

    for stage_idx, times in stage_times.items():
        for key, secs in times.items():
            entry = key_to_entry.get(key)
            if not entry or not entry["ibu_id"]:
                continue
            misses = _stage_miss_for_index(stage_misses_map.get(key, []), stage_idx, discipline)
            if misses is None or misses != 0:
                continue
            best_secs, best_race, best_stage = best_stage_map.get(entry["ibu_id"], (None, "", ""))
            if best_secs is None:
                continue
            if abs(secs - best_secs) <= 0.01:
                record_rows.append([
                    format_seconds(secs),
                    entry["name"],
                    entry["nat"],
                    best_race or "-",
                    best_stage or _stage_label(stage_idx, discipline, entry.get("leg")),
                ])

    if record_rows:
        print("Personal shooting records (0 miss):")
        render_table(["Time", "Athlete", "Nat", "RaceId", "Stage"], record_rows, pretty=is_pretty_output(args))
        print()
    else:
        print("Personal shooting records (0 miss): none")
        print()

    return 0
