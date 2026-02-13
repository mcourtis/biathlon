"""Post-race analysis command handler."""

from __future__ import annotations

import argparse
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from ..api import (
    BiathlonError,
    get_all_results,
    get_analytic_results,
    get_cup_results,
    get_current_season_id,
    get_race_results,
)
from ..constants import (
    EVENT_TYPE_WC,
    INDIVIDUAL_DISCIPLINES,
    RELAY_DISCIPLINE,
    SINGLE_MIXED_RELAY_DISCIPLINE,
    SKI_LAPS,
)
from ..formatting import Color, is_pretty_output, render_table, rank_style
from ..utils import (
    format_race_header,
    get_first_time,
    parse_time_seconds,
    format_seconds,
)
from ._common import (
    DISCIPLINE_LEADER_MARKER,
    GENERAL_LEADER_MARKER,
    _format_section_title,
    _ordinal,
    _parse_rank,
    _row_ibu_id,
    detect_event_type,
    is_relay_discipline as _is_relay_discipline,
)
from .relay import _has_completed_results as _has_completed_relay_results
from .results import _find_latest_race_with_results_any, _has_completed_results
from .startlist import _get_cup_ids_for_race, _get_wc_points


MAJOR_LEVELS = {"WC", "WCH", "OWG"}
TOP_N = 6
RESULTS_TOP_N = 10
STANDINGS_TOP_N = 10
DISCIPLINE_LABELS = {
    "SP": "Sprint",
    "PU": "Pursuit",
    "IN": "Individual",
    "MS": "Mass Start",
}


def _parse_int(value: Any) -> int | None:
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _is_individual_like_discipline(discipline: str) -> bool:
    return discipline in INDIVIDUAL_DISCIPLINES or discipline == "SI"


def _is_team_level_result(result: dict) -> bool:
    disc = str(result.get("DisciplineId") or result.get("Comp") or "").upper()
    return bool(result.get("IsTeam")) or disc in {
        RELAY_DISCIPLINE,
        SINGLE_MIXED_RELAY_DISCIPLINE,
        "MR",
    }


def _sort_results_by_rank(results: list[dict]) -> list[dict]:
    def _key(res: dict) -> tuple[int, int]:
        rank_val = _parse_rank(
            res.get("Rank") or res.get("Standing") or res.get("ResultOrder")
        )
        order_val = _parse_rank(res.get("ResultOrder")) or 10**9
        return (rank_val if rank_val is not None else 10**9, order_val)

    return sorted(results, key=_key)


def _collect_flower_entries(
    results: list[dict], is_team: bool, limit: int
) -> list[dict]:
    filtered = [res for res in results if bool(res.get("IsTeam")) == is_team]
    sorted_results = _sort_results_by_rank(filtered)
    entries = []
    for res in sorted_results:
        rank_val = _parse_rank(
            res.get("Rank") or res.get("Standing") or res.get("ResultOrder")
        )
        if rank_val is None or rank_val > limit:
            continue
        name = res.get("Name") or res.get("ShortName") or ""
        nat = res.get("Nat") or ""
        if is_team and not name:
            name = nat
        entries.append(
            {
                "rank": rank_val,
                "name": name,
                "nat": nat,
                "ibu_id": str(res.get("IBUId") or ""),
            }
        )
        if len(entries) >= limit:
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


def _extract_rank_and_change(row: dict | None) -> tuple[int | None, str]:
    if not row:
        return None, "-"
    rank_val = _parse_rank(
        row.get("Rank") or row.get("Standing") or row.get("ResultOrder")
    )
    if rank_val is None:
        return None, "-"

    diff_val = None
    for key in ("RnkDiff", "RankDiff", "RankChange"):
        if key in row:
            diff_val = _parse_int(row.get(key))
            if diff_val is None:
                diff_val = 0
            break

    if diff_val is None:
        return rank_val, "-"
    if diff_val == 0:
        return rank_val, "="
    diff_val = -diff_val
    return rank_val, f"{diff_val:+d}"


def _build_race_points_map(results: list[dict], discipline: str) -> dict[str, int]:
    is_mass_start = discipline == "MS"
    points_by_id: dict[str, int] = {}
    for res in results:
        if res.get("IsTeam"):
            continue
        ibu_id = str(res.get("IBUId") or "")
        if not ibu_id:
            continue
        rank_val = _parse_rank(res.get("Rank") or res.get("ResultOrder"))
        if rank_val is None:
            continue
        points_by_id[ibu_id] = _get_wc_points(rank_val, mass_start=is_mass_start)
    return points_by_id


def _format_points(value: Any) -> str:
    points_val = _parse_int(value)
    if points_val is None:
        return str(value or 0)
    return str(points_val)


def _format_race_points(value: Any) -> str:
    points_val = _parse_int(value)
    if points_val is None:
        return str(value or 0)
    return f"{points_val:+d}"


def _apply_style(cell_str: str, style: str) -> str:
    if style == "dim":
        return Color.dim(cell_str)
    if style == "gold":
        return Color.gold(cell_str)
    if style == "silver":
        return Color.silver(cell_str)
    if style == "bronze":
        return Color.bronze(cell_str)
    if style == "flowers":
        return Color.flowers(cell_str)
    if style == "red":
        return Color.red(cell_str)
    return cell_str


def _make_row_style_formatter(row_styles: list[str]) -> Callable[[str, int], str]:
    def _formatter(cell_str: str, row_idx: int) -> str:
        if row_idx < len(row_styles):
            return _apply_style(cell_str, row_styles[row_idx])
        return cell_str

    return _formatter


def _leader_marker_suffix(
    ibu_id: str,
    name: str,
    nat: str,
    general_leader: dict[str, str],
    discipline_leader: dict[str, str],
    enabled: bool,
    mode: str = "any",
) -> str:
    if not enabled:
        return ""

    def matches(leader: dict[str, str]) -> bool:
        if not leader.get("id") and not leader.get("name"):
            return False
        if ibu_id and leader.get("id") and ibu_id == leader.get("id"):
            return True
        if name and nat and leader.get("name") == name and leader.get("nat") == nat:
            return True
        if name and leader.get("name") == name and not leader.get("nat"):
            return True
        return False

    markers = []
    if mode in {"any", "general"} and matches(general_leader):
        markers.append(GENERAL_LEADER_MARKER)
    if mode in {"any", "discipline"} and matches(discipline_leader):
        markers.append(DISCIPLINE_LEADER_MARKER)
    if not markers:
        return ""
    return " " + " ".join(markers)


def _make_leader_name_decorator(
    general_leader: dict[str, str],
    discipline_leader: dict[str, str],
    enabled: bool,
    mode: str,
) -> Callable[[str, str, str], str]:
    def _decorator(name: str, nat: str, ibu_id: str) -> str:
        suffix = _leader_marker_suffix(
            ibu_id,
            name,
            nat,
            general_leader,
            discipline_leader,
            enabled,
            mode,
        )
        return f"{name}{suffix}" if suffix else name

    return _decorator


def _make_name_formatter(
    row_styles: list[str] | None = None,
) -> Callable[[str, int], str]:
    def _formatter(cell_str: str, row_idx: int) -> str:
        text = cell_str.strip()
        tokens = text.split()
        markers = []
        while tokens and tokens[-1] in {
            GENERAL_LEADER_MARKER,
            DISCIPLINE_LEADER_MARKER,
        }:
            markers.insert(0, tokens.pop())
        base = " ".join(tokens)

        if row_styles and row_idx < len(row_styles) and row_styles[row_idx]:
            base = _apply_style(base, row_styles[row_idx])

        if markers:
            colored = []
            for marker in markers:
                if marker == GENERAL_LEADER_MARKER:
                    colored.append(Color.gold("●"))
                else:
                    colored.append(Color.red("●"))
            base = f"{base} {' '.join(colored)}" if base else " ".join(colored)

        return base

    return _formatter


def _format_change_cell(cell_str: str, _row_idx: int) -> str:
    text = cell_str.strip()
    if text.startswith("+"):
        return Color.green(cell_str)
    if text.startswith("-"):
        return Color.red(cell_str)
    return cell_str


def _format_race_points_cell(cell_str: str, _row_idx: int) -> str:
    text = cell_str.strip()
    if not text:
        return cell_str
    value = _parse_int(text)
    if value is None:
        return cell_str
    if value <= 0:
        return Color.red(cell_str)
    capped = min(value, 90)
    intensity = (capped - 1) / 89
    return Color.green(cell_str, intensity)


def _build_standings_rows(
    rows: list[dict],
    top_n: int,
    race_points_by_id: dict[str, int],
    name_decorator: Callable[[str, str, str], str] | None = None,
    participating_ids: set[str] | None = None,
) -> tuple[list[list[str]], list[str]]:
    entries = []
    for row in rows:
        rank_val, change = _extract_rank_and_change(row)
        if rank_val is None:
            continue
        name = row.get("Name") or row.get("ShortName") or ""
        nat = row.get("Nat") or ""
        total_points = row.get("Score") or row.get("Points") or 0
        ibu_id = _row_ibu_id(row)
        if name_decorator:
            name = name_decorator(name, nat, ibu_id)
        race_points = race_points_by_id.get(ibu_id, 0)
        participated = True
        if participating_ids is not None:
            participated = bool(ibu_id and ibu_id in participating_ids)
        entries.append(
            {
                "rank": rank_val,
                "name": name,
                "nat": nat,
                "race_points": race_points,
                "total_points": total_points,
                "change": change,
                "participated": participated,
            }
        )
    entries.sort(key=lambda e: e["rank"])
    entries = entries[:top_n]
    rows_out: list[list[str]] = []
    row_styles: list[str] = []
    for entry in entries:
        rows_out.append(
            [
                entry["rank"],
                entry["name"],
                entry["nat"],
                _format_race_points(entry["race_points"]),
                _format_points(entry["total_points"]),
                entry["change"],
            ]
        )
        row_styles.append("" if entry["participated"] else "dim")
    return rows_out, row_styles


def _make_key(
    ibu_id: str | None, bib: str | None, name: str | None, leg: int | None
) -> str:
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
    return _make_key(
        entry.get("IBUId"), entry.get("Bib"), entry.get("Name"), entry.get("Leg")
    )


def _analytic_key(entry: dict) -> str:
    return _make_key(
        entry.get("IBUId"), entry.get("Bib"), entry.get("Name"), entry.get("Leg")
    )


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


def _stage_miss_for_index(
    stage_misses: list[int], stage_idx: int, discipline: str
) -> int | None:
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
            lap_rows.append(
                {
                    "secs": secs,
                    "time": format_seconds(secs),
                    "name": res.get("Name") or res.get("ShortName") or "",
                    "nat": res.get("Nat") or "",
                    "lap": idx,
                    "leg": leg,
                }
            )
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
    sport_evt = payload.get("SportEvt") or {}
    event_type = detect_event_type(sport_evt)
    is_wc_race = event_type == EVENT_TYPE_WC
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
    flower_entries = _collect_flower_entries(results, is_relay, RESULTS_TOP_N)

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

    pretty = is_pretty_output(args)
    total_rows: list[dict] = []
    disc_rows: list[dict] = []
    race_points_by_id: dict[str, int] = {}
    general_leader = {"id": "", "name": "", "nat": ""}
    discipline_leader = {"id": "", "name": "", "nat": ""}
    cat_id = ""
    season_id = ""
    if is_wc_race and not is_relay and _is_individual_like_discipline(discipline):
        cat_id = str(comp.get("catId") or comp.get("CatId") or "").upper()
        season_id = str(sport_evt.get("SeasonId") or "") or get_current_season_id()
        total_cup_id, disc_cup_id = _get_cup_ids_for_race(season_id, cat_id, discipline)
        race_points_by_id = _build_race_points_map(results, discipline)
        with ThreadPoolExecutor(max_workers=2) as executor:
            total_future = executor.submit(_fetch_cup_rows, total_cup_id)
            disc_future = executor.submit(_fetch_cup_rows, disc_cup_id)
            total_rows = total_future.result()
            disc_rows = disc_future.result()
        if total_rows:
            general_leader = {
                "id": _row_ibu_id(total_rows[0]),
                "name": total_rows[0].get("Name")
                or total_rows[0].get("ShortName")
                or "",
                "nat": total_rows[0].get("Nat") or "",
            }
        if disc_rows:
            discipline_leader = {
                "id": _row_ibu_id(disc_rows[0]),
                "name": disc_rows[0].get("Name") or disc_rows[0].get("ShortName") or "",
                "nat": disc_rows[0].get("Nat") or "",
            }

    mark_leaders = pretty
    decorate_any = _make_leader_name_decorator(
        general_leader, discipline_leader, mark_leaders, "any"
    )
    name_formatter_plain = _make_name_formatter()
    name_nat_to_id = {
        (entry["name"], entry["nat"]): entry["ibu_id"]
        for entry in entries
        if entry.get("ibu_id")
    }
    participating_ids = {entry["ibu_id"] for entry in entries if entry.get("ibu_id")}

    print()
    print(_format_section_title(format_race_header(payload, race_id), args))
    print()

    sec = 0

    sec += 1
    if flower_entries:
        if is_wc_race:
            headers = (
                ["Rank", "Team", "Nat", "Points"]
                if is_relay
                else ["Rank", "Athlete", "Nat", "Points"]
            )
        else:
            headers = (
                ["Rank", "Team", "Nat"] if is_relay else ["Rank", "Athlete", "Nat"]
            )
        results_rows = []
        is_mass_start = discipline == "MS"
        for entry in flower_entries:
            row = [
                entry["rank"],
                decorate_any(entry["name"], entry["nat"], entry["ibu_id"]),
                entry["nat"],
            ]
            if is_wc_race:
                row.append(
                    _format_race_points(
                        _get_wc_points(entry["rank"], mass_start=is_mass_start)
                    )
                )
            results_rows.append(row)
        row_styles = [rank_style(entry["rank"]) for entry in flower_entries]
        rank_formatter = _make_row_style_formatter(row_styles)
        name_formatter = _make_name_formatter(row_styles)
        cell_fmts = [rank_formatter, name_formatter, None]
        if is_wc_race:
            cell_fmts.append(None)
        print(_format_section_title(f"{sec}. Results:", args))
        render_table(
            headers,
            results_rows,
            pretty=pretty,
            cell_formatters=cell_fmts,
        )
        print()
    else:
        print(_format_section_title(f"{sec}. Results: none", args))
        print()

    if (
        is_wc_race
        and flower_entries
        and not is_relay
        and _is_individual_like_discipline(discipline)
    ):
        disc_label = DISCIPLINE_LABELS.get(discipline, discipline)
        if total_rows or disc_rows:
            sec += 1
            if total_rows:
                total_standings_rows, total_row_styles = _build_standings_rows(
                    total_rows,
                    STANDINGS_TOP_N,
                    race_points_by_id,
                    decorate_any,
                    participating_ids=participating_ids,
                )
                total_name_formatter = _make_name_formatter(total_row_styles)
                print(
                    _format_section_title(
                        f"{sec}. World Cup standing changes (Total):", args
                    )
                )
                render_table(
                    ["Rank", "Athlete", "Nat", "Race Pts", "Total Pts", "Change"],
                    total_standings_rows,
                    pretty=pretty,
                    cell_formatters=[
                        None,
                        total_name_formatter,
                        None,
                        _format_race_points_cell,
                        None,
                        _format_change_cell,
                    ],
                    row_styles=total_row_styles,
                )
                print()
            else:
                print(
                    _format_section_title(
                        f"{sec}. World Cup standing changes (Total): no data available",
                        args,
                    )
                )
                print()

            sec += 1
            if disc_rows:
                disc_standings_rows, disc_row_styles = _build_standings_rows(
                    disc_rows,
                    STANDINGS_TOP_N,
                    race_points_by_id,
                    decorate_any,
                    participating_ids=participating_ids,
                )
                disc_name_formatter = _make_name_formatter(disc_row_styles)
                print(
                    _format_section_title(
                        f"{sec}. World Cup standing changes ({disc_label}):", args
                    )
                )
                render_table(
                    ["Rank", "Athlete", "Nat", "Race Pts", "Total Pts", "Change"],
                    disc_standings_rows,
                    pretty=pretty,
                    cell_formatters=[
                        None,
                        disc_name_formatter,
                        None,
                        _format_race_points_cell,
                        None,
                        _format_change_cell,
                    ],
                    row_styles=disc_row_styles,
                )
                print()
            else:
                print(
                    _format_section_title(
                        f"{sec}. World Cup standing changes ({disc_label}): no data available",
                        args,
                    )
                )
                print()
        else:
            sec += 1
            print(
                _format_section_title(
                    f"{sec}. World Cup standing changes: no data available", args
                )
            )
            print()

    use_major = bool(getattr(args, "major", False))
    level_set = MAJOR_LEVELS if use_major else {"WC"}

    race_milestones = []
    top_milestones: list[list[str]] = []
    race_milestone_ids: set[str] = set()
    processed_ids: set[str] = set()
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
        level_results = [
            r for r in all_results if str(r.get("Level") or "").upper() in level_set
        ]
        if race_row and race_row not in level_results:
            level_results.append(race_row)
        race_count = len(level_results)
        stats_by_category: dict[str, dict[str, dict[str, int]]] = {
            "Individual": {},
            "Team": {},
            "All": {},
        }

        def _init_stats() -> dict[str, int]:
            return {
                "win": 0,
                "podium": 0,
                "flower": 0,
                "win_prior": 0,
                "podium_prior": 0,
                "flower_prior": 0,
            }

        for res in level_results:
            rank_val = _parse_rank(res.get("Rank") or res.get("SO"))
            if rank_val is None:
                continue
            race_id_match = str(res.get("RaceId") or "")
            is_current_race = race_id_match == race_id
            category_label = "Team" if _is_team_level_result(res) else "Individual"

            # Update category-specific stats (use outer ibu_id, not from result)
            target_stats = stats_by_category[category_label]
            stat = target_stats.setdefault(ibu_id, _init_stats())

            # Also update combined "All" stats
            all_stats = stats_by_category["All"]
            all_stat = all_stats.setdefault(ibu_id, _init_stats())

            if rank_val == 1:
                stat["win"] += 1
                all_stat["win"] += 1
                if not is_current_race:
                    stat["win_prior"] += 1
                    all_stat["win_prior"] += 1
            if rank_val <= 3:
                stat["podium"] += 1
                all_stat["podium"] += 1
                if not is_current_race:
                    stat["podium_prior"] += 1
                    all_stat["podium_prior"] += 1
            if rank_val <= 6:
                stat["flower"] += 1
                all_stat["flower"] += 1
                if not is_current_race:
                    stat["flower_prior"] += 1
                    all_stat["flower_prior"] += 1
        if (
            race_count == 1 or race_count % 25 == 0
        ) and ibu_id not in race_milestone_ids:
            race_milestones.append(
                [
                    race_count,
                    decorate_any(entry["name"], entry["nat"], ibu_id),
                    entry["nat"],
                ]
            )
            race_milestone_ids.add(ibu_id)
        # Determine athlete's rank and build milestones for top 6
        if is_relay:
            # For relays, get rank from team result
            team_rank = None
            for team in team_results:
                if str(team.get("Bib") or "") == entry["bib"]:
                    team_rank = _parse_rank(team.get("Rank"))
                    break
            rank_val = team_rank
        else:
            rank_val = _parse_rank(entry["rank"])

        if rank_val is None or rank_val > 6:
            continue
        if ibu_id in processed_ids:
            continue
        processed_ids.add(ibu_id)

        category = "Team" if is_relay else "Individual"
        stat = stats_by_category[category].get(ibu_id) or _init_stats()
        all_stat = stats_by_category["All"].get(ibu_id) or _init_stats()
        decorated_name = decorate_any(entry["name"], entry["nat"], ibu_id)

        # Store: [count, type, decorated_name, nat, ibu_id, rank]
        # Winners get win counts
        if rank_val == 1:
            top_milestones.append(
                [
                    all_stat["win"],
                    "Win",
                    decorated_name,
                    entry["nat"],
                    ibu_id,
                    rank_val,
                ]
            )
            if not is_relay:
                top_milestones.append(
                    [
                        stat["win"],
                        "Individual Win",
                        decorated_name,
                        entry["nat"],
                        ibu_id,
                        rank_val,
                    ]
                )

        # Top 3 get podium counts
        if rank_val <= 3:
            top_milestones.append(
                [
                    all_stat["podium"],
                    "Podium",
                    decorated_name,
                    entry["nat"],
                    ibu_id,
                    rank_val,
                ]
            )
            if not is_relay:
                top_milestones.append(
                    [
                        stat["podium"],
                        "Individual Podium",
                        decorated_name,
                        entry["nat"],
                        ibu_id,
                        rank_val,
                    ]
                )

        # Top 6 get flower counts
        top_milestones.append(
            [
                all_stat["flower"],
                "Flower",
                decorated_name,
                entry["nat"],
                ibu_id,
                rank_val,
            ]
        )
        if not is_relay:
            top_milestones.append(
                [
                    stat["flower"],
                    "Individual Flower",
                    decorated_name,
                    entry["nat"],
                    ibu_id,
                    rank_val,
                ]
            )

    sec += 1
    if race_milestones:
        race_milestones.sort(key=lambda row: row[0], reverse=True)
        # Convert milestone numbers to ordinal
        race_milestones = [
            [_ordinal(row[0]), row[1], row[2]] for row in race_milestones
        ]
        label = (
            f"{sec}. World Cup + WCH + OWG race milestones:"
            if use_major
            else f"{sec}. World Cup race milestones:"
        )
        print(_format_section_title(label, args))
        render_table(
            ["Milestone", "Athlete", "Nat"],
            race_milestones,
            pretty=pretty,
            cell_formatters=[None, name_formatter_plain, None],
        )
        print()
    else:
        label = (
            f"{sec}. World Cup + WCH + OWG race milestones: none"
            if use_major
            else f"{sec}. World Cup race milestones: none"
        )
        print(_format_section_title(label, args))
        print()

    sec += 1
    if top_milestones:
        # Group by athlete (ibu_id), sort groups by race finish rank
        from itertools import groupby

        # Define sort order for milestone types
        type_order = {
            "Win": 0,
            "Individual Win": 1,
            "Podium": 2,
            "Individual Podium": 3,
            "Flower": 4,
            "Individual Flower": 5,
        }
        # Sort by ibu_id to group
        top_milestones.sort(key=lambda row: row[4])
        grouped = []
        for ibu_id, group in groupby(top_milestones, key=lambda row: row[4]):
            group_rows = list(group)
            # Sort within group by type order (Win, Podium, Flower)
            group_rows.sort(key=lambda row: type_order.get(row[1], 99))
            # Get athlete info from first row: decorated_name, nat, rank
            athlete_name = group_rows[0][2]
            athlete_nat = group_rows[0][3]
            athlete_rank = group_rows[0][5]
            grouped.append((athlete_rank, athlete_name, athlete_nat, group_rows))
        # Sort groups by race finish rank (ascending: 1st, 2nd, 3rd...)
        grouped.sort(key=lambda g: g[0])

        label = (
            f"{sec}. World Cup + WCH + OWG Top 6 Milestones:"
            if use_major
            else f"{sec}. World Cup Top 6 Milestones:"
        )
        print(_format_section_title(label, args))
        for i, (rank, athlete_name, athlete_nat, group_rows) in enumerate(grouped):
            # Print athlete header
            athlete_header = f"{rank}. {athlete_name} ({athlete_nat})"
            if pretty:
                print(
                    f"{Color.BOLD}{name_formatter_plain(athlete_header, 0)}{Color.RESET}"
                )
            else:
                print(athlete_header)
            # Convert milestone numbers to ordinal, only show Milestone and Type
            # Track which rows have multiples of 5 for highlighting
            display_rows = [[_ordinal(row[0]), row[1]] for row in group_rows]
            row_styles = [
                "highlight" if row[0] == 1 or row[0] % 5 == 0 else ""
                for row in group_rows
            ]
            render_table(
                ["Milestone", "Type"],
                display_rows,
                pretty=pretty,
                show_headers=False,
                row_styles=row_styles,
            )
            if i < len(grouped) - 1:
                print()
        print()
    else:
        label = (
            f"{sec}. World Cup + WCH + OWG Top 6 Milestones: none"
            if use_major
            else f"{sec}. World Cup Top 6 Milestones: none"
        )
        print(_format_section_title(label, args))
        print()

    sec += 1
    lap_rows = _fetch_lap_times(race_id, discipline)
    if lap_rows:
        print(_format_section_title(f"{sec}. Top 6 fastest laps:", args))
        headers = ["Time", "Athlete", "Nat", "Lap"]
        if is_relay:
            headers.insert(3, "Leg")
        rows = []
        for row in lap_rows:
            ibu_id = name_nat_to_id.get((row["name"], row["nat"]), "")
            name = decorate_any(row["name"], row["nat"], ibu_id)
            data = [row["time"], name, row["nat"], row["lap"]]
            if is_relay:
                data.insert(3, row["leg"] or "-")
            rows.append(data)
        cell_formatters = [None, name_formatter_plain, None, None]
        if is_relay:
            cell_formatters.insert(3, None)
        render_table(
            headers,
            rows,
            pretty=is_pretty_output(args),
            cell_formatters=cell_formatters,
        )
        print()
    else:
        print(_format_section_title(f"{sec}. Top 6 fastest laps: none", args))
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
                leg_times.append(
                    [
                        leg_secs,
                        entry["name"],
                        entry["nat"],
                        leg,
                        format_seconds(leg_secs),
                    ]
                )
                prev_secs = total_secs
        leg_times.sort(key=lambda row: row[0])
        leg_times = leg_times[:TOP_N]
        sec += 1
        if leg_times:
            print(
                _format_section_title(f"{sec}. Top 6 fastest legs (total time):", args)
            )
            rows = []
            for row in leg_times:
                ibu_id = name_nat_to_id.get((row[1], row[2]), "")
                name = decorate_any(row[1], row[2], ibu_id)
                rows.append([row[4], name, row[2], row[3]])
            render_table(
                ["Time", "Athlete", "Nat", "Leg"],
                rows,
                pretty=pretty,
                cell_formatters=[None, name_formatter_plain, None, None],
            )
            print()
        else:
            print(
                _format_section_title(
                    f"{sec}. Top 6 fastest legs (total time): none", args
                )
            )
            print()

        from .relay import _fetch_analytic_times

        crst_times = _fetch_analytic_times(race_id, "CRST")
        leg_info = {
            (entry["bib"], entry["leg"]): entry
            for entry in entries
            if entry["bib"] and entry["leg"]
        }
        leg_course_rows = []
        for (bib, leg), secs in crst_times.items():
            entry = leg_info.get((bib, leg))
            if not entry:
                continue
            leg_course_rows.append(
                [secs, entry["name"], entry["nat"], leg, format_seconds(secs)]
            )
        leg_course_rows.sort(key=lambda row: row[0])
        leg_course_rows = leg_course_rows[:TOP_N]
        sec += 1
        if leg_course_rows:
            print(
                _format_section_title(f"{sec}. Top 6 fastest legs (course time):", args)
            )
            rows = []
            for row in leg_course_rows:
                ibu_id = name_nat_to_id.get((row[1], row[2]), "")
                name = decorate_any(row[1], row[2], ibu_id)
                rows.append([row[4], name, row[2], row[3]])
            render_table(
                ["Time", "Athlete", "Nat", "Leg"],
                rows,
                pretty=pretty,
                cell_formatters=[None, name_formatter_plain, None, None],
            )
            print()
        else:
            print(
                _format_section_title(
                    f"{sec}. Top 6 fastest legs (course time): none", args
                )
            )
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
            misses = _stage_miss_for_index(
                stage_misses_map.get(key, []), stage_idx, discipline
            )
            if misses is None or misses != 0:
                continue
            stage_label = _stage_label(stage_idx, discipline, entry.get("leg"))
            zero_miss_rows.append(
                [secs, entry["name"], entry["nat"], format_seconds(secs), stage_label]
            )
    zero_miss_rows.sort(key=lambda row: row[0])
    zero_miss_rows = zero_miss_rows[:TOP_N]
    sec += 1
    if zero_miss_rows:
        print(_format_section_title(f"{sec}. Top 6 fastest shooters (0 miss):", args))
        headers = ["Time", "Athlete", "Nat", "Stage"]
        rows = []
        for row in zero_miss_rows:
            ibu_id = name_nat_to_id.get((row[1], row[2]), "")
            name = decorate_any(row[1], row[2], ibu_id)
            data = [row[3], name, row[2], row[4] or "-"]
            rows.append(data)
        render_table(
            headers,
            rows,
            pretty=pretty,
            cell_formatters=[None, name_formatter_plain, None, None],
        )
        print()
    else:
        print(
            _format_section_title(f"{sec}. Top 6 fastest shooters (0 miss): none", args)
        )
        print()

    return 0
