"""Post-race analysis command handler."""

from __future__ import annotations

import argparse
import datetime
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from ..api import (
    BiathlonError,
    get_all_results,
    get_analytic_results,
    get_cup_results,
    get_events,
    get_race_results,
    get_races,
    get_seasons,
)
from ..constants import (
    CATEGORY_DISPLAY_NAMES,
    DISCIPLINE_NAMES,
    EVENT_TYPE_LABELS,
    EVENT_TYPE_OWG,
    EVENT_TYPE_WC,
    INDIVIDUAL_DISCIPLINES,
    RELAY_DISCIPLINE,
    RELAY_DISCIPLINES,
    SINGLE_MIXED_RELAY_DISCIPLINE,
    SKI_LAPS,
)
from ..formatting import (
    Color,
    is_pretty_output,
    get_output_format,
    render_table,
    rank_style,
)
from ..utils import (
    parse_start_datetime,
    format_race_header,
    get_first_time,
    parse_time_seconds,
    format_seconds,
)
from ._common import (
    DISCIPLINE_LEADER_MARKER,
    GENERAL_LEADER_MARKER,
    _format_section_title,
    _has_completed_relay_results,
    _ordinal,
    _parse_rank,
    _row_ibu_id,
    _select_race_interactive,
    detect_event_type,
    is_relay_discipline as _is_relay_discipline,
)
from .results import _find_recent_completed_races, _has_completed_results
from .startlist import (
    OLYMPIC_SEASON_IDS,
    _get_all_olympic_medals,
    _get_cup_ids_for_race,
    _get_past_olympic_individual_podiums,
    _get_past_olympic_relay_podiums,
    _get_wc_points,
)


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

RELAY_MILESTONE_ROWS_RANK_1 = [
    "Relay Win",
    "Relay Podium",
    "Relay Flower",
    "Win",
    "Podium",
    "Flower",
]
RELAY_MILESTONE_ROWS_RANK_2_3 = ["Relay Podium", "Relay Flower", "Podium", "Flower"]
RELAY_MILESTONE_ROWS_RANK_4_6 = ["Relay Flower", "Flower"]


MEDAL_RANK_MAP = {1: "gold", 2: "silver", 3: "bronze"}


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

COUNTRY_WITH_CODE_RE = re.compile(r"^(?P<name>.+?)\s*\((?P<code>[A-Z]{3})\)$")


def _country_key_and_display(value: str) -> tuple[str, str]:
    """Return internal key + display country name from code or 'Name (CODE)'."""
    text = str(value or "").strip()
    if not text:
        return "", ""

    match = COUNTRY_WITH_CODE_RE.match(text)
    if match:
        code = match.group("code")
        name = match.group("name").strip()
        return code, name or COUNTRY_CODE_TO_NAME.get(code, code)

    code = text.upper()
    if code in COUNTRY_CODE_TO_NAME:
        return code, COUNTRY_CODE_TO_NAME[code]
    return text, text


def _country_display(value: str) -> str:
    _, display = _country_key_and_display(value)
    return display


def _medal_dots(medals: set[str]) -> str:
    """Return colored medal dots for a set of medal types."""
    dots = []
    for medal_type in ("gold", "silver", "bronze"):
        if medal_type in medals:
            if medal_type == "gold":
                dots.append(Color.gold("●"))
            elif medal_type == "silver":
                dots.append(Color.silver("●"))
            else:
                dots.append(Color.bronze("●"))
    return " ".join(dots)


def _best_medal_style(medals: set[str]) -> str:
    """Return the row style for the best medal in the set."""
    if "gold" in medals:
        return "gold"
    if "silver" in medals:
        return "silver"
    if "bronze" in medals:
        return "bronze"
    return ""


def _make_medal_cell_formatter(
    row_styles: list[str],
    medal_map: dict[str, set[str]],
    keys: list[str],
) -> Callable[[str, int], str]:
    """Create a cell formatter that applies row style and appends medal dots."""

    def _formatter(cell_str: str, row_idx: int) -> str:
        base = cell_str
        if row_idx < len(row_styles) and row_styles[row_idx]:
            base = _apply_style(base, row_styles[row_idx])
        if row_idx < len(keys):
            medals = medal_map.get(keys[row_idx])
            if medals:
                dots = _medal_dots(medals)
                if dots:
                    base = f"{base} {dots}"
        return base

    return _formatter


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


RACE_SEASON_RE = re.compile(r"^BT(?P<season>\d{4})")
SEASON_TEXT_RE = re.compile(r"^(?P<s1>\d{2})\s*/\s*(?P<s2>\d{2})$")


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


def _warn_once(message: str, warning_keys: set[str], key: str) -> None:
    if key in warning_keys:
        return
    warning_keys.add(key)
    print(message, file=sys.stderr)


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
    comp = payload.get("Competition") or {}
    start_dt = _start_dt_from_competition(comp)
    race_start_cache[race_id] = start_dt
    return start_dt


def _is_result_at_or_before_target(
    result: dict,
    target_race_id: str,
    target_start_dt: datetime.datetime | None,
    race_start_cache: dict[str, datetime.datetime | None],
    warning_keys: set[str],
    warning_context: str,
) -> bool:
    race_id = str(result.get("RaceId") or "")
    if race_id and race_id == target_race_id:
        return True

    # Fast path: if season is strictly before/after target season, no per-race lookup.
    target_season_key = _season_sort_key(_season_id_from_race_id(target_race_id))
    result_season_key = _season_sort_key(_season_id_from_result(result))
    if target_season_key is not None and result_season_key is not None:
        if result_season_key < target_season_key:
            return True
        if result_season_key > target_season_key:
            return False

    if target_start_dt is None:
        return True
    start_dt = _resolve_result_start_datetime(result, race_start_cache)
    if start_dt is None:
        warn_key = f"{warning_context}:{race_id or id(result)}"
        _warn_once(
            (
                "warning: skipping row with unknown chronology in "
                f"{warning_context} (race {race_id or 'unknown'})"
            ),
            warning_keys,
            warn_key,
        )
        return False
    return start_dt <= target_start_dt


def _filter_results_to_snapshot(
    rows: list[dict],
    target_race_id: str,
    target_start_dt: datetime.datetime | None,
    race_start_cache: dict[str, datetime.datetime | None],
    warning_keys: set[str],
    warning_context: str,
) -> list[dict]:
    return [
        row
        for row in rows
        if _is_result_at_or_before_target(
            row,
            target_race_id,
            target_start_dt,
            race_start_cache,
            warning_keys,
            warning_context,
        )
    ]


def _discipline_cup_key(discipline: str) -> str:
    disc = str(discipline or "").upper()
    return "IN" if disc == "SI" else disc


def _is_same_discipline_cup(race_discipline: str, target_discipline: str) -> bool:
    return _discipline_cup_key(race_discipline) == _discipline_cup_key(
        target_discipline
    )


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
    if style == "highlight":
        return Color.highlight(cell_str)
    if style == "highlight_plain":
        return Color.highlight_plain(cell_str)
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


def _relay_milestone_types_for_rank(rank: int) -> list[str]:
    if rank == 1:
        return RELAY_MILESTONE_ROWS_RANK_1
    if rank <= 3:
        return RELAY_MILESTONE_ROWS_RANK_2_3
    return RELAY_MILESTONE_ROWS_RANK_4_6


def _is_race_milestone_count(count: int) -> bool:
    return count == 1 or count % 25 == 0


def _build_race_milestone_rows(
    *,
    race_count: int,
    team_race_count: int | None,
    is_relay: bool,
    decorated_name: str,
    nat: str,
) -> list[list[Any]]:
    rows: list[list[Any]] = []
    if _is_race_milestone_count(race_count):
        if is_relay:
            rows.append([race_count, "Race", decorated_name, nat])
        else:
            rows.append([race_count, decorated_name, nat])
    if (
        is_relay
        and team_race_count is not None
        and team_race_count != race_count
        and _is_race_milestone_count(team_race_count)
    ):
        rows.append([team_race_count, "Team Race", decorated_name, nat])
    return rows


def _build_relay_milestone_blocks(
    top_milestones: list[list[Any]],
    entries: list[dict],
    team_results: list[dict],
) -> list[dict[str, Any]]:
    milestone_counts: dict[str, dict[str, int]] = {}
    for row in top_milestones:
        if len(row) < 6:
            continue
        count = _parse_int(row[0])
        milestone_type = str(row[1] or "")
        ibu_id = str(row[4] or "")
        if count is None or count <= 0 or not milestone_type or not ibu_id:
            continue
        milestone_counts.setdefault(ibu_id, {})[milestone_type] = count

    athletes_by_bib_leg: dict[str, dict[int, dict]] = {}
    for entry in entries:
        bib = str(entry.get("bib") or "")
        leg_val = _parse_int(entry.get("leg"))
        if not bib or leg_val is None:
            continue
        athletes_by_bib_leg.setdefault(bib, {})[leg_val] = entry

    teams: list[dict[str, Any]] = []
    for team in team_results:
        rank_val = _parse_rank(team.get("Rank"))
        bib = str(team.get("Bib") or "")
        if rank_val is None or rank_val > TOP_N or not bib:
            continue
        team_name = str(
            team.get("Name") or team.get("ShortName") or team.get("Nat") or ""
        )
        team_nat = str(team.get("Nat") or "")
        teams.append(
            {
                "rank": rank_val,
                "bib": bib,
                "name": team_name or team_nat or bib,
                "nat": team_nat,
            }
        )
    teams.sort(key=lambda row: (row["rank"], row["bib"]))

    blocks: list[dict[str, Any]] = []
    for team in teams:
        rank_val = int(team["rank"])
        bib = str(team["bib"])
        legs = athletes_by_bib_leg.get(bib, {})
        headers = ["Milestone Type"]
        athlete_ids: list[str] = []
        for leg in (1, 2, 3, 4):
            leg_entry = legs.get(leg)
            athlete_name = str(leg_entry.get("name") or "-") if leg_entry else "-"
            headers.append(f"L{leg} {athlete_name}")
            athlete_ids.append(str(leg_entry.get("ibu_id") or "") if leg_entry else "")

        rows: list[list[str]] = []
        highlight_cells: set[tuple[int, int]] = set()
        row_types = _relay_milestone_types_for_rank(rank_val)
        for row_idx, milestone_type in enumerate(row_types):
            row_out = [milestone_type]
            for col_idx, athlete_id in enumerate(athlete_ids, start=1):
                cell = "-"
                if athlete_id:
                    count = milestone_counts.get(athlete_id, {}).get(milestone_type)
                    if count is not None:
                        cell = _ordinal(count)
                        if count == 1 or count % 5 == 0:
                            highlight_cells.add((row_idx, col_idx))
                row_out.append(cell)
            rows.append(row_out)
        blocks.append(
            {
                "rank": rank_val,
                "team_name": team["name"],
                "team_nat": team["nat"],
                "headers": headers,
                "rows": rows,
                "highlight_cells": highlight_cells,
            }
        )
    return blocks


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
        markers: list[str] = []
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
                str(entry["rank"]),
                str(entry["name"]),
                str(entry["nat"]),
                _format_race_points(entry["race_points"]),
                _format_points(entry["total_points"]),
                str(entry["change"]),
            ]
        )
        row_styles.append("" if entry["participated"] else "dim")
    return rows_out, row_styles


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
        if detect_event_type(event) != EVENT_TYPE_WC:
            continue
        event_id = event.get("EventId")
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
            if not _is_individual_like_discipline(race_disc):
                continue
            races_out.append((_start_dt_from_race_row(race), race_id, race_disc))

    races_out.sort(key=_race_meta_sort_key)
    return races_out


def _build_individual_race_points(
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
    prev_points_by_id: dict[str, int],
    athlete_info: dict[str, tuple[str, str]],
) -> list[dict]:
    curr_rank = _rank_points(points_by_id)
    prev_rank = _rank_points(prev_points_by_id)
    ranked_ids = sorted(points_by_id, key=lambda ibu_id: curr_rank[ibu_id])
    rows: list[dict] = []
    for ibu_id in ranked_ids:
        name, nat = athlete_info.get(ibu_id, ("", ""))
        row: dict[str, Any] = {
            "Rank": curr_rank[ibu_id],
            "Name": name,
            "Nat": nat,
            "IBUId": ibu_id,
            "Score": points_by_id[ibu_id],
        }
        prev = prev_rank.get(ibu_id)
        if prev is not None:
            row["RnkDiff"] = curr_rank[ibu_id] - prev
        rows.append(row)
    return rows


def _has_newer_relevant_wc_points_race(
    season_id: str,
    cat_id: str,
    target_race_id: str,
    target_start_dt: datetime.datetime | None,
) -> bool:
    races = _collect_wc_individual_races(season_id, cat_id)
    for start_dt, race_id, _disc in races:
        if race_id == target_race_id:
            continue
        if target_start_dt is None:
            if start_dt is None:
                continue
        elif start_dt is None:
            return True
        elif start_dt <= target_start_dt:
            continue
        try:
            payload = get_race_results(race_id)
        except BiathlonError:
            continue
        if _has_completed_results(payload):
            return True
    return False


def _compute_wc_snapshot_rows(
    season_id: str,
    cat_id: str,
    target_race_id: str,
    target_discipline: str,
    target_start_dt: datetime.datetime | None,
    warning_keys: set[str],
) -> tuple[list[dict], list[dict]]:
    races = _collect_wc_individual_races(season_id, cat_id)
    pre_total: dict[str, int] = {}
    post_total: dict[str, int] = {}
    pre_disc: dict[str, int] = {}
    post_disc: dict[str, int] = {}
    athlete_info: dict[str, tuple[str, str]] = {}

    for start_dt, race_id, race_disc in races:
        if race_id == target_race_id:
            pass
        elif target_start_dt is not None:
            if start_dt is None:
                _warn_once(
                    (
                        "warning: skipping race with unknown chronology while "
                        f"building standings snapshot ({race_id})"
                    ),
                    warning_keys,
                    f"snapshot:{race_id}",
                )
                continue
            if start_dt > target_start_dt:
                continue

        try:
            payload = get_race_results(race_id)
        except BiathlonError:
            continue
        if not _has_completed_results(payload):
            continue
        race_points, race_info = _build_individual_race_points(payload, race_disc)
        for ibu_id, info in race_info.items():
            athlete_info.setdefault(ibu_id, info)
        is_target = race_id == target_race_id
        same_disc = _is_same_discipline_cup(race_disc, target_discipline)

        if is_target:
            _merge_points(post_total, race_points)
            if same_disc:
                _merge_points(post_disc, race_points)
        else:
            _merge_points(pre_total, race_points)
            _merge_points(post_total, race_points)
            if same_disc:
                _merge_points(pre_disc, race_points)
                _merge_points(post_disc, race_points)

    total_rows = _rows_from_points(post_total, pre_total, athlete_info)
    disc_rows = _rows_from_points(post_disc, pre_disc, athlete_info)
    return total_rows, disc_rows


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


def _render_olympic_medal_sections(
    args: argparse.Namespace,
    sec: int,
    discipline: str,
    cat_id: str,
    is_relay: bool,
    participating_ids: set[str],
    gold_ids: set[str],
    silver_ids: set[str],
    bronze_ids: set[str],
    cutoff_dt: datetime.datetime | None = None,
    race_country_medals: dict[str, set[str]] | None = None,
    race_athlete_medals: dict[str, set[str]] | None = None,
) -> int:
    """Render Olympic/WCH medal table sections after the main postrace output.

    Returns the updated section counter.
    """
    output_format = get_output_format(args)
    disc_name = DISCIPLINE_NAMES.get(discipline, discipline)
    cat_name = CATEGORY_DISPLAY_NAMES.get(cat_id, cat_id)

    # Fetch podiums and all-medals in parallel
    podiums: list[dict] = []
    all_country_medals: list[dict] = []
    all_athlete_stats: dict[str, dict] = {}

    with ThreadPoolExecutor(max_workers=2) as executor:
        if is_relay:
            podiums_future = executor.submit(
                _get_past_olympic_relay_podiums,
                discipline,
                cat_id,
                cutoff_dt=cutoff_dt,
                include_cutoff=True,
            )
        else:
            podiums_future = executor.submit(
                _get_past_olympic_individual_podiums,
                discipline,
                cat_id,
                cutoff_dt=cutoff_dt,
                include_cutoff=True,
            )
        medals_future = executor.submit(
            _get_all_olympic_medals,
            cat_id,
            cutoff_dt=cutoff_dt,
            include_cutoff=True,
        )
        podiums = podiums_future.result()
        all_country_medals, all_athlete_stats = medals_future.result()

    # Section: Country medal table (discipline-specific)
    sec += 1
    medal_counts: dict[str, dict[str, int]] = {}
    country_labels: dict[str, str] = {}
    for p in podiums:
        if is_relay:
            # Relay podiums: parse country from display string "Name (NAT)"
            for medal_type in ("gold", "silver", "bronze"):
                key, display = _country_key_and_display(p.get(medal_type, ""))
                if key:
                    if key not in medal_counts:
                        medal_counts[key] = {"gold": 0, "silver": 0, "bronze": 0}
                    medal_counts[key][medal_type] += 1
                    country_labels.setdefault(key, display)
        else:
            # Individual podiums: use gold_nat/silver_nat/bronze_nat keys
            for medal_type, key in [
                ("gold", "gold_nat"),
                ("silver", "silver_nat"),
                ("bronze", "bronze_nat"),
            ]:
                nat = p.get(key) or ""
                country_key, display = _country_key_and_display(nat)
                if not country_key:
                    continue
                if country_key not in medal_counts:
                    medal_counts[country_key] = {"gold": 0, "silver": 0, "bronze": 0}
                medal_counts[country_key][medal_type] += 1
                country_labels.setdefault(country_key, display)

    if not medal_counts:
        print(
            _format_section_title(
                f"{sec}. Country medal table ({cat_name} {disc_name}): none", args
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
                f"{sec}. Country medal table ({cat_name} {disc_name}):", args
            )
        )
        rcm = race_country_medals or {}
        medal_rows = []
        disc_country_styles = []
        disc_country_keys = []
        for idx, (country, counts) in enumerate(sorted_countries, 1):
            total = counts["gold"] + counts["silver"] + counts["bronze"]
            medal_rows.append(
                [
                    str(idx),
                    country_labels.get(country, _country_display(country)),
                    str(counts["gold"]),
                    str(counts["silver"]),
                    str(counts["bronze"]),
                    str(total),
                ]
            )
            cm = rcm.get(country)
            disc_country_styles.append(_best_medal_style(cm) if cm else "dim")
            disc_country_keys.append(country)
        disc_country_fmt = _make_medal_cell_formatter(
            disc_country_styles, rcm, disc_country_keys
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
            output_format=output_format,
            row_styles=disc_country_styles,
            column_separators={2},
            cell_formatters=[None, disc_country_fmt, None, None, None, None],
        )
        print()

    # Section: Country medal table (all Olympic disciplines)
    sec += 1
    if not all_country_medals:
        print(
            _format_section_title(
                f"{sec}. Country medal table (all Olympic disciplines): none", args
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
                f"{sec}. Country medal table (all Olympic disciplines):", args
            )
        )
        rcm_all = race_country_medals or {}
        all_country_rows = []
        all_country_styles = []
        all_country_keys = []
        for idx, (country, c) in enumerate(sorted_all_countries, 1):
            total = c["gold"] + c["silver"] + c["bronze"]
            total_ind = c["gold_ind"] + c["silver_ind"] + c["bronze_ind"]
            total_relay = c["gold_relay"] + c["silver_relay"] + c["bronze_relay"]
            all_country_rows.append(
                [
                    str(idx),
                    _country_display(country),
                    str(c["gold"]),
                    str(c["silver"]),
                    str(c["bronze"]),
                    str(total),
                    str(c["gold_ind"]),
                    str(c["silver_ind"]),
                    str(c["bronze_ind"]),
                    str(total_ind),
                    str(c["gold_relay"]),
                    str(c["silver_relay"]),
                    str(c["bronze_relay"]),
                    str(total_relay),
                ]
            )
            cm = rcm_all.get(country)
            all_country_styles.append(_best_medal_style(cm) if cm else "dim")
            all_country_keys.append(country)
        all_country_fmt = _make_medal_cell_formatter(
            all_country_styles, rcm_all, all_country_keys
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
            output_format=output_format,
            row_styles=all_country_styles,
            column_separators={2, 6, 10},
            group_headers=[
                (2, 6, "All"),
                (6, 10, "Individual"),
                (10, 14, "Relay"),
            ],
            cell_formatters=[
                None,
                all_country_fmt,
                None,
                None,
                None,
                None,
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
        print()

    # Section: Athlete medal table (all Olympic disciplines)
    sec += 1
    race_medalist_ids = gold_ids | silver_ids | bronze_ids

    def _medal_sort_key(x: tuple[str, dict]) -> tuple:
        s = x[1]
        return (
            -s["gold"],
            -s.get("gold_ind", 0),
            -s.get("gold_relay", 0),
            -s["silver"],
            -s.get("silver_ind", 0),
            -s.get("silver_relay", 0),
            -s["bronze"],
            -s.get("bronze_ind", 0),
            -s.get("bronze_relay", 0),
            -(s["gold"] + s["silver"] + s["bronze"]),
            -(s.get("gold_ind", 0) + s.get("silver_ind", 0) + s.get("bronze_ind", 0)),
            -(
                s.get("gold_relay", 0)
                + s.get("silver_relay", 0)
                + s.get("bronze_relay", 0)
            ),
            s["races"],
        )

    # Sort ALL medalists to get correct ranks, then filter display list
    all_medalists = [
        (key, stats)
        for key, stats in all_athlete_stats.items()
        if stats["gold"] > 0 or stats["silver"] > 0 or stats["bronze"] > 0
    ]
    all_medalists.sort(key=_medal_sort_key)
    # Build ranked list: keep athletes with 2+ gold medals + race medalists
    medalists = [
        (rank, key, stats)
        for rank, (key, stats) in enumerate(all_medalists, 1)
        if stats["gold"] >= 2 or key in race_medalist_ids
    ]

    if not medalists:
        print(
            _format_section_title(
                f"{sec}. Athlete medal table (all Olympic disciplines): none", args
            )
        )
        print()
    else:
        print(
            _format_section_title(
                f"{sec}. Athlete medal table (all Olympic disciplines):", args
            )
        )
        ram = race_athlete_medals or {}
        all_rows = []
        all_row_styles = []
        all_ath_keys = []
        for rank, key, stats in medalists:
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
                    str(rank),
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
            # Row styling: gold/silver/bronze for race medalists, highlight for participants
            if key in gold_ids:
                all_row_styles.append("gold")
            elif key in silver_ids:
                all_row_styles.append("silver")
            elif key in bronze_ids:
                all_row_styles.append("bronze")
            elif key in participating_ids:
                all_row_styles.append("highlight_plain")
            else:
                all_row_styles.append("dim")
            all_ath_keys.append(key)
        all_ath_name_fmt = _make_medal_cell_formatter(all_row_styles, ram, all_ath_keys)
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
            output_format=output_format,
            row_styles=all_row_styles,
            column_separators={4, 9, 14},
            group_headers=[(4, 9, "All"), (9, 14, "Individual"), (14, 19, "Relay")],
            cell_formatters=[
                None,
                all_ath_name_fmt,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
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
        print()

    return sec


def _collect_discipline_race_ids(
    season_ids: list[str],
    discipline: str,
    cat_id: str,
    target_event_type: str,
    cutoff_dt: datetime.datetime | None = None,
    warning_keys: set[str] | None = None,
    warning_context: str = "medal races",
) -> list[str]:
    """Find level-1 race_ids for a discipline+category across seasons.

    Filters events to match *target_event_type* (WC / WCH / OWG).
    """
    warned = warning_keys if warning_keys is not None else set()

    def _races_for_season(season_id: str) -> list[str]:
        try:
            events = get_events(season_id, level=1)
        except BiathlonError:
            return []
        matched: list[tuple[datetime.datetime | None, str]] = []
        for event in events:
            if detect_event_type(event) != target_event_type:
                continue
            event_id = event.get("EventId")
            if not event_id:
                continue
            try:
                races = get_races(event_id)
            except BiathlonError:
                continue
            for race in races:
                rid = race.get("RaceId") or race.get("Id") or ""
                if not rid:
                    continue
                race_disc = str(race.get("DisciplineId") or "").upper()
                race_cat = str(race.get("catId") or race.get("CatId") or "").upper()
                if race_disc != discipline or (cat_id and race_cat != cat_id):
                    continue
                start_dt = _start_dt_from_race_row(race)
                if cutoff_dt is not None:
                    if start_dt is None:
                        _warn_once(
                            (
                                "warning: skipping race with unknown chronology in "
                                f"{warning_context} ({rid})"
                            ),
                            warned,
                            f"{warning_context}:{rid}",
                        )
                        continue
                    if start_dt > cutoff_dt:
                        continue
                matched.append((start_dt, rid))
        matched.sort(
            key=lambda item: (
                item[0] is None,
                item[0] or datetime.datetime.max.replace(tzinfo=datetime.timezone.utc),
                item[1],
            )
        )
        return [rid for _start, rid in matched]

    race_ids: list[str] = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(_races_for_season, sid) for sid in season_ids]
        for future in futures:
            race_ids.extend(future.result())
    return race_ids


def _build_discipline_medal_counts(
    race_ids: list[str], is_relay: bool
) -> tuple[list[tuple[str, dict[str, int]]], list[dict], int]:
    """Build medal counts by country and athlete for a set of races.

    Returns (sorted_countries, sorted_athletes, races_used).
    """
    medal_keys = {1: "gold", 2: "silver", 3: "bronze"}
    country_counts: dict[str, dict[str, int]] = {}
    athlete_info: dict[str, dict] = {}
    athlete_race_counts: dict[str, int] = {}
    races_used = 0

    payloads: list[dict] = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(get_race_results, rid) for rid in race_ids]
        for future in futures:
            try:
                payloads.append(future.result())
            except BiathlonError:
                pass

    for payload in payloads:
        all_results = payload.get("Results") or []
        if not all_results:
            continue

        if is_relay:
            candidates = [r for r in all_results if r.get("IsTeam")]
        else:
            candidates = [r for r in all_results if not r.get("IsTeam")]
            for res in candidates:
                name = res.get("Name") or res.get("ShortName") or ""
                ibu_id = str(res.get("IBUId") or "")
                key = ibu_id or name
                if key:
                    athlete_race_counts[key] = athlete_race_counts.get(key, 0) + 1

        candidates.sort(
            key=lambda r: (
                int(r.get("Rank")) if str(r.get("Rank", "")).isdigit() else 10**9,
                r.get("ResultOrder", 10**9),
            )
        )

        found = False
        for res in candidates[:3]:
            rank_str = str(res.get("Rank") or "")
            if not rank_str.isdigit():
                continue
            rank_val = int(rank_str)
            medal = medal_keys.get(rank_val)
            if not medal:
                continue
            found = True
            nat = res.get("Nat") or ""
            if nat:
                country_counts.setdefault(nat, {"gold": 0, "silver": 0, "bronze": 0})
                country_counts[nat][medal] += 1
            if not is_relay:
                name = res.get("Name") or res.get("ShortName") or ""
                ibu_id = str(res.get("IBUId") or "")
                key = ibu_id or name
                if key:
                    if key not in athlete_info:
                        athlete_info[key] = {
                            "name": name,
                            "nat": nat,
                            "ibu_id": ibu_id,
                            "gold": 0,
                            "silver": 0,
                            "bronze": 0,
                            "races": 0,
                        }
                    athlete_info[key][medal] += 1
        if found:
            races_used += 1

    for key, stats in athlete_info.items():
        stats["races"] = athlete_race_counts.get(key, 0)

    sorted_countries = sorted(
        country_counts.items(),
        key=lambda x: (x[1]["gold"], x[1]["silver"], x[1]["bronze"]),
        reverse=True,
    )
    sorted_athletes = sorted(
        athlete_info.values(),
        key=lambda x: (x["gold"], x["silver"], x["bronze"]),
        reverse=True,
    )
    return sorted_countries, sorted_athletes, races_used


def _result_discipline_id(row: dict) -> str:
    return str(
        row.get("DisciplineId") or row.get("Comp") or row.get("Discipline") or ""
    ).upper()


def _is_lapped_current_result(
    entry: dict,
    current_rank: int | None,
    discipline: str,
) -> bool:
    irm = str(entry.get("irm") or "").upper()
    if irm == "LAP":
        return True
    result_text = str(entry.get("time") or "").upper()
    if "LAP" in result_text:
        return True
    # Defensive fallback for pursuit rows where lapped status appears as large rank
    # codes (e.g. 10059/10060) without explicit IRM.
    return str(discipline or "").upper() == "PU" and bool(
        current_rank is not None and current_rank >= 10000
    )


def _render_best_performances_section(
    args: argparse.Namespace,
    sec: int,
    entries: list[dict],
    team_results: list[dict],
    race_id: str,
    target_start_dt: datetime.datetime | None,
    discipline: str,
    is_relay: bool,
    decorate_name: Callable[[str, str, str], str],
    name_formatter: Callable[[str, int], str],
    race_start_cache: dict[str, datetime.datetime | None],
    warning_keys: set[str],
) -> int:
    """Render Olympic best-performance milestones (overall + discipline)."""
    output_format = get_output_format(args)
    disc_label = DISCIPLINE_NAMES.get(discipline, discipline)
    disc_label_lc = disc_label.lower()
    if is_relay:
        all_label = "Best Relay Results (all discipline)"
        discipline_label = f"Best Relay Results ({disc_label_lc})"
    else:
        all_label = "Best Individual Result (all discipline)"
        discipline_label = f"Best Individual Results ({disc_label_lc})"

    team_rank_by_bib: dict[str, int] = {}
    if is_relay:
        for team in team_results:
            bib = str(team.get("Bib") or "")
            rank_val = _parse_rank(team.get("Rank") or team.get("SO"))
            if bib and rank_val is not None:
                team_rank_by_bib[bib] = rank_val

    seen_ids: set[str] = set()
    all_results_cache: dict[str, list[dict]] = {}
    milestone_rows: list[tuple[int, str, str, str, str]] = []

    def _previous_best_label(value: int | None, scope: str) -> str:
        if value is None:
            return f"none ({scope})"
        return f"{_ordinal(value)} ({scope})"

    for entry in entries:
        ibu_id = entry.get("ibu_id", "")
        if not ibu_id or ibu_id in seen_ids:
            continue
        seen_ids.add(ibu_id)

        if is_relay:
            current_rank = team_rank_by_bib.get(str(entry.get("bib") or ""))
        else:
            current_rank = _parse_rank(entry.get("rank"))
        if _is_lapped_current_result(entry, current_rank, discipline):
            continue
        if current_rank is None:
            continue

        if ibu_id not in all_results_cache:
            try:
                all_payload = get_all_results(ibu_id)
            except BiathlonError:
                all_payload = {}
            all_results_cache[ibu_id] = list(all_payload.get("Results") or [])

        major_ranked: list[tuple[dict, int]] = []
        for res in all_results_cache[ibu_id]:
            if str(res.get("Level") or "").upper() not in MAJOR_LEVELS:
                continue
            if not _is_result_at_or_before_target(
                res,
                race_id,
                target_start_dt,
                race_start_cache,
                warning_keys,
                f"best performances for {ibu_id}",
            ):
                continue
            rank_val = _parse_rank(
                res.get("Rank") or res.get("SO") or res.get("ResultOrder")
            )
            if rank_val is None:
                continue
            major_ranked.append((res, rank_val))

        prior_rows = [
            (res, rank_val)
            for res, rank_val in major_ranked
            if str(res.get("RaceId") or "") != race_id
        ]
        prior_rows_same_type = [
            (res, rank_val)
            for res, rank_val in prior_rows
            if _is_team_level_result(res) == is_relay
        ]
        prior_best_all = min(
            (rank_val for _, rank_val in prior_rows_same_type), default=None
        )
        prior_best_disc = min(
            (
                rank_val
                for res, rank_val in prior_rows
                if _result_discipline_id(res) == discipline
            ),
            default=None,
        )

        is_best_all = prior_best_all is None or current_rank < prior_best_all
        is_best_disc = prior_best_disc is None or current_rank < prior_best_disc
        if not is_best_all and not is_best_disc:
            continue

        name = decorate_name(entry["name"], entry["nat"], ibu_id)
        nat = entry["nat"]
        if is_best_all:
            milestone_rows.append(
                (
                    current_rank,
                    name,
                    nat,
                    all_label,
                    _previous_best_label(prior_best_all, "all discipline"),
                )
            )
        elif is_best_disc:
            milestone_rows.append(
                (
                    current_rank,
                    name,
                    nat,
                    discipline_label,
                    _previous_best_label(prior_best_disc, disc_label_lc),
                )
            )

    type_order = {
        all_label: 0,
        discipline_label: 1,
    }
    milestone_rows.sort(key=lambda row: (row[0], row[1], type_order.get(row[3], 99)))

    sec += 1
    if not milestone_rows:
        print(_format_section_title(f"{sec}. Best Performances: none", args))
        print()
        return sec

    print(_format_section_title(f"{sec}. Best Performances:", args))
    table_rows = []
    row_styles = []
    for rank_val, name, nat, milestone, previous_best in milestone_rows:
        table_rows.append([str(rank_val), name, nat, milestone, previous_best])
        if rank_val == 1:
            row_styles.append("gold")
        elif rank_val == 2:
            row_styles.append("silver")
        elif rank_val == 3:
            row_styles.append("bronze")
        else:
            row_styles.append("dim")
    athlete_formatter = _make_name_formatter(row_styles)
    render_table(
        ["Rank", "Athlete", "Nat", "Milestone", "Previous Best Results"],
        table_rows,
        output_format=output_format,
        row_styles=row_styles,
        cell_formatters=[None, athlete_formatter, None, None, None],
    )
    print()
    return sec


def handle_post_race(args: argparse.Namespace) -> int:
    """Show post-race highlights and milestones."""
    try:
        if args.race:
            race_id = args.race
            payload = get_race_results(race_id)
        else:
            candidates = _find_recent_completed_races(5)
            race_id, payload = _select_race_interactive(candidates)
    except BiathlonError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    comp = payload.get("Competition") or {}
    target_start_dt = _start_dt_from_competition(comp)
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
    chronology_warning_keys: set[str] = set()
    history_race_start_cache: dict[str, datetime.datetime | None] = {
        race_id: target_start_dt
    }
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
            "irm": str(res.get("IRM") or "").upper(),
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
    output_format = get_output_format(args)
    total_rows: list[dict] = []
    disc_rows: list[dict] = []
    race_points_by_id: dict[str, int] = {}
    general_leader = {"id": "", "name": "", "nat": ""}
    discipline_leader = {"id": "", "name": "", "nat": ""}
    cat_id = ""
    season_id = ""
    if is_wc_race and not is_relay and _is_individual_like_discipline(discipline):
        cat_id = str(comp.get("catId") or comp.get("CatId") or "").upper()
        season_id = str(sport_evt.get("SeasonId") or "") or _season_id_from_race_id(
            race_id
        )
        race_points_by_id = _build_race_points_map(results, discipline)
        use_live_standings = True
        if season_id and cat_id:
            use_live_standings = not _has_newer_relevant_wc_points_race(
                season_id,
                cat_id,
                race_id,
                target_start_dt,
            )
        if use_live_standings and season_id:
            total_cup_id, disc_cup_id = _get_cup_ids_for_race(
                season_id, cat_id, discipline
            )
            with ThreadPoolExecutor(max_workers=2) as executor:
                total_future = executor.submit(_fetch_cup_rows, total_cup_id)
                disc_future = executor.submit(_fetch_cup_rows, disc_cup_id)
                total_rows = total_future.result()
                disc_rows = disc_future.result()
        elif season_id and cat_id:
            total_rows, disc_rows = _compute_wc_snapshot_rows(
                season_id,
                cat_id,
                race_id,
                discipline,
                target_start_dt,
                chronology_warning_keys,
            )
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
        section_one_medal_map: dict[str, set[str]] = {}
        section_one_keys: list[str] = []
        is_mass_start = discipline == "MS"
        for entry in flower_entries:
            key = entry["ibu_id"] or entry["nat"] or entry["name"]
            section_one_keys.append(key)
            medal = MEDAL_RANK_MAP.get(entry["rank"])
            if medal and key:
                section_one_medal_map.setdefault(key, set()).add(medal)
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
        if event_type == EVENT_TYPE_OWG:
            row_styles = [
                MEDAL_RANK_MAP.get(entry["rank"], "dim") for entry in flower_entries
            ]
            name_formatter = _make_medal_cell_formatter(
                row_styles, section_one_medal_map, section_one_keys
            )
            nat_formatter = _make_row_style_formatter(row_styles)
        else:
            row_styles = [rank_style(entry["rank"]) for entry in flower_entries]
            name_formatter = _make_name_formatter(row_styles)
            nat_formatter = None
        rank_formatter = _make_row_style_formatter(row_styles)
        cell_fmts = [rank_formatter, name_formatter, nat_formatter]
        if is_wc_race:
            cell_fmts.append(None)
        print(_format_section_title(f"{sec}. Results:", args))
        render_table(
            headers,
            results_rows,
            output_format=output_format,
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
                    output_format=output_format,
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
                    output_format=output_format,
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

    if event_type == EVENT_TYPE_OWG:
        sec = _render_best_performances_section(
            args,
            sec,
            entries,
            team_results,
            race_id,
            target_start_dt,
            discipline,
            is_relay,
            decorate_any,
            name_formatter_plain,
            history_race_start_cache,
            chronology_warning_keys,
        )

    run_standard_sections = event_type != EVENT_TYPE_OWG
    use_major = bool(getattr(args, "major", False))
    level_set = MAJOR_LEVELS if use_major else {"WC"}

    race_milestones = []
    top_milestones: list[list] = []
    race_milestone_ids: set[str] = set()
    processed_ids: set[str] = set()
    all_results_cache: dict[str, list[dict]] = {}
    stage_cache: dict[str, dict[int, dict[str, float]]] = {}

    for entry in entries:
        if not run_standard_sections:
            break
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
        level_results = _filter_results_to_snapshot(
            level_results,
            race_id,
            target_start_dt,
            history_race_start_cache,
            chronology_warning_keys,
            f"milestones for {ibu_id}",
        )
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
        if ibu_id not in race_milestone_ids:
            team_race_count = None
            if is_relay:
                team_race_count = sum(
                    1 for res in level_results if _is_team_level_result(res)
                )
            race_milestones.extend(
                _build_race_milestone_rows(
                    race_count=race_count,
                    team_race_count=team_race_count,
                    is_relay=is_relay,
                    decorated_name=decorate_any(entry["name"], entry["nat"], ibu_id),
                    nat=entry["nat"],
                )
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
        if is_relay:
            if rank_val == 1:
                top_milestones.append(
                    [
                        stat["win"],
                        "Relay Win",
                        decorated_name,
                        entry["nat"],
                        ibu_id,
                        rank_val,
                    ]
                )
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
            if rank_val <= 3:
                top_milestones.append(
                    [
                        stat["podium"],
                        "Relay Podium",
                        decorated_name,
                        entry["nat"],
                        ibu_id,
                        rank_val,
                    ]
                )
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
            top_milestones.append(
                [
                    stat["flower"],
                    "Relay Flower",
                    decorated_name,
                    entry["nat"],
                    ibu_id,
                    rank_val,
                ]
            )
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
        else:
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

    if run_standard_sections:
        sec += 1
        if race_milestones:
            race_milestones.sort(key=lambda row: row[0], reverse=True)
            if is_relay:
                race_milestones = [
                    [_ordinal(row[0]), row[1], row[2], row[3]]
                    for row in race_milestones
                ]
            else:
                race_milestones = [
                    [_ordinal(row[0]), row[1], row[2]] for row in race_milestones
                ]
            label = (
                f"{sec}. World Cup + WCH + OWG race milestones:"
                if use_major
                else f"{sec}. World Cup race milestones:"
            )
            print(_format_section_title(label, args))
            if is_relay:
                render_table(
                    ["Milestone", "Type", "Athlete", "Nat"],
                    race_milestones,
                    output_format=output_format,
                    cell_formatters=[None, None, name_formatter_plain, None],
                )
            else:
                render_table(
                    ["Milestone", "Athlete", "Nat"],
                    race_milestones,
                    output_format=output_format,
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
            label = (
                f"{sec}. World Cup + WCH + OWG Top 6 Milestones:"
                if use_major
                else f"{sec}. World Cup Top 6 Milestones:"
            )
            print(_format_section_title(label, args))

            if is_relay:
                relay_blocks = _build_relay_milestone_blocks(
                    top_milestones, entries, team_results
                )
                for i, block in enumerate(relay_blocks):
                    team_header = (
                        f"{block['rank']}. {block['team_name']} ({block['team_nat']})"
                        if block["team_nat"]
                        else f"{block['rank']}. {block['team_name']}"
                    )
                    if pretty:
                        print(f"{Color.BOLD}{team_header}{Color.RESET}")
                    else:
                        print(team_header)
                    cell_formatters: list[Callable[[str, int], str] | None] = [None]
                    for col_idx in range(1, len(block["headers"])):
                        highlight_rows = {
                            row_idx
                            for row_idx, col in block["highlight_cells"]
                            if col == col_idx
                        }
                        if highlight_rows:

                            def _highlight_cell(
                                cell_str: str,
                                row_idx: int,
                                highlight_rows: set[int] = highlight_rows,
                            ) -> str:
                                if row_idx in highlight_rows and cell_str != "-":
                                    return _apply_style(cell_str, "highlight")
                                return cell_str

                            cell_formatters.append(_highlight_cell)
                        else:
                            cell_formatters.append(None)
                    render_table(
                        block["headers"],
                        block["rows"],
                        output_format=output_format,
                        cell_formatters=cell_formatters,
                    )
                    if i < len(relay_blocks) - 1:
                        print()
            else:
                # Group by athlete (ibu_id), sort groups by race finish rank
                from itertools import groupby

                type_order = {
                    "Win": 0,
                    "Individual Win": 1,
                    "Podium": 2,
                    "Individual Podium": 3,
                    "Flower": 4,
                    "Individual Flower": 5,
                }
                top_milestones.sort(key=lambda row: row[4])
                grouped = []
                for _ibu_id, group in groupby(top_milestones, key=lambda row: row[4]):
                    group_rows = list(group)
                    group_rows.sort(key=lambda row: type_order.get(row[1], 99))
                    athlete_name = group_rows[0][2]
                    athlete_nat = group_rows[0][3]
                    athlete_rank = group_rows[0][5]
                    grouped.append(
                        (athlete_rank, athlete_name, athlete_nat, group_rows)
                    )
                grouped.sort(key=lambda g: g[0])

                for i, (rank, athlete_name, athlete_nat, group_rows) in enumerate(
                    grouped
                ):
                    athlete_header = f"{rank}. {athlete_name} ({athlete_nat})"
                    if pretty:
                        print(
                            f"{Color.BOLD}{name_formatter_plain(athlete_header, 0)}{Color.RESET}"
                        )
                    else:
                        print(athlete_header)
                    display_rows = [
                        [_ordinal(int(row[0])), row[1]] for row in group_rows
                    ]
                    row_styles = [
                        "highlight" if row[0] == 1 or row[0] % 5 == 0 else ""
                        for row in group_rows
                    ]
                    render_table(
                        ["Milestone", "Type"],
                        display_rows,
                        output_format=output_format,
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

    if run_standard_sections:
        sec += 1
        lap_rows = _fetch_lap_times(race_id, discipline)
        if lap_rows:
            print(_format_section_title(f"{sec}. Top 6 fastest laps:", args))
            headers = ["Time", "Athlete", "Nat", "Lap"]
            if is_relay:
                headers.insert(3, "Leg")
            rows = []
            for lap_row in lap_rows:
                ibu_id = name_nat_to_id.get((lap_row["name"], lap_row["nat"]), "")
                name = decorate_any(lap_row["name"], lap_row["nat"], ibu_id)
                data = [lap_row["time"], name, lap_row["nat"], lap_row["lap"]]
                if is_relay:
                    data.insert(3, lap_row["leg"] or "-")
                rows.append(data)
            cell_formatters = [None, name_formatter_plain, None, None]
            if is_relay:
                cell_formatters.insert(3, None)
            render_table(
                headers,
                rows,
                output_format=get_output_format(args),
                cell_formatters=cell_formatters,
            )
            print()
        else:
            print(_format_section_title(f"{sec}. Top 6 fastest laps: none", args))
            print()

    if run_standard_sections and is_relay:
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
                entry_or_none = entry_by_leg.get((bib, leg))
                if not entry_or_none:
                    prev_secs = total_secs
                    continue
                entry = entry_or_none
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
                output_format=output_format,
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

        from ._common import _fetch_relay_analytic_times as _fetch_analytic_times

        crst_times = _fetch_analytic_times(race_id, "CRST")
        leg_info = {
            (entry["bib"], entry["leg"]): entry
            for entry in entries
            if entry["bib"] and entry["leg"]
        }
        leg_course_rows = []
        for (bib, leg), secs in crst_times.items():
            leg_entry = leg_info.get((bib, leg))
            if not leg_entry:
                continue
            leg_course_rows.append(
                [secs, leg_entry["name"], leg_entry["nat"], leg, format_seconds(secs)]
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
                output_format=output_format,
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

    if run_standard_sections:
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
                matched_entry = key_to_entry.get(key)
                if not matched_entry:
                    continue
                stage_misses = _stage_miss_for_index(
                    stage_misses_map.get(key, []), stage_idx, discipline
                )
                if stage_misses is None or stage_misses != 0:
                    continue
                stage_label = _stage_label(
                    stage_idx, discipline, matched_entry.get("leg")
                )
                zero_miss_rows.append(
                    [
                        secs,
                        matched_entry["name"],
                        matched_entry["nat"],
                        format_seconds(secs),
                        stage_label,
                    ]
                )
        zero_miss_rows.sort(key=lambda row: row[0])
        zero_miss_rows = zero_miss_rows[:TOP_N]
        sec += 1
        if zero_miss_rows:
            print(
                _format_section_title(f"{sec}. Top 6 fastest shooters (0 miss):", args)
            )
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
                output_format=output_format,
                cell_formatters=[None, name_formatter_plain, None, None],
            )
            print()
        else:
            print(
                _format_section_title(
                    f"{sec}. Top 6 fastest shooters (0 miss): none", args
                )
            )
            print()

    # Build rank-specific ID sets and country medal map for styling
    gold_ids: set[str] = set()
    silver_ids: set[str] = set()
    bronze_ids: set[str] = set()
    race_country_medals: dict[str, set[str]] = {}
    race_athlete_medals: dict[str, set[str]] = {}
    race_medalist_name_nat: set[tuple[str, str]] = set()
    for entry in flower_entries:
        rank_val = entry["rank"]
        medal = MEDAL_RANK_MAP.get(rank_val)
        if not medal:
            continue
        nat = entry.get("nat", "")
        if nat:
            race_country_medals.setdefault(nat, set()).add(medal)
        name = entry.get("name", "")
        if name:
            race_medalist_name_nat.add((name, nat))
        ibu_id = entry.get("ibu_id", "")
        if not ibu_id:
            continue
        race_athlete_medals.setdefault(ibu_id, set()).add(medal)
        if rank_val == 1:
            gold_ids.add(ibu_id)
        elif rank_val == 2:
            silver_ids.add(ibu_id)
        elif rank_val == 3:
            bronze_ids.add(ibu_id)

    # Discipline medal table (scoped by event type)
    medal_season_id = (
        season_id
        if season_id
        else str(sport_evt.get("SeasonId") or "") or _season_id_from_race_id(race_id)
    )
    medal_cat_id = (
        cat_id if cat_id else str(comp.get("catId") or comp.get("CatId") or "").upper()
    )

    if event_type == EVENT_TYPE_WC:
        medal_season_ids = [medal_season_id]
        medal_scope = "Season"
    elif event_type == EVENT_TYPE_OWG:
        medal_season_ids = list(OLYMPIC_SEASON_IDS)
        medal_scope = EVENT_TYPE_LABELS[EVENT_TYPE_OWG]
    else:
        # WCH: search all seasons for World Championship events
        try:
            medal_season_ids = [str(s.get("SeasonId")) for s in get_seasons()]
        except BiathlonError:
            medal_season_ids = [medal_season_id]
        medal_scope = EVENT_TYPE_LABELS.get(event_type, "Season")

    disc_race_ids = _collect_discipline_race_ids(
        medal_season_ids,
        discipline,
        medal_cat_id,
        event_type,
        cutoff_dt=target_start_dt,
        warning_keys=chronology_warning_keys,
        warning_context="medal races",
    )
    if disc_race_ids:
        sorted_countries, sorted_athletes, medal_races_used = (
            _build_discipline_medal_counts(disc_race_ids, is_relay)
        )
        disc_name = DISCIPLINE_NAMES.get(discipline, discipline)
        cat_name = CATEGORY_DISPLAY_NAMES.get(medal_cat_id, medal_cat_id)

        # Country medal table — skip for OWG (shown in Olympic medal sections)
        if event_type != EVENT_TYPE_OWG:
            sec += 1
            if sorted_countries:
                print(
                    _format_section_title(
                        f"{sec}. {medal_scope} medal table by country"
                        f" — {cat_name} {disc_name}"
                        f" ({medal_races_used} races):",
                        args,
                    )
                )
                medal_rows = []
                country_styles = []
                country_keys = []
                for idx, (country, counts) in enumerate(sorted_countries, 1):
                    total = counts["gold"] + counts["silver"] + counts["bronze"]
                    medal_rows.append(
                        [
                            str(idx),
                            country,
                            str(counts["gold"]),
                            str(counts["silver"]),
                            str(counts["bronze"]),
                            str(total),
                        ]
                    )
                    country_medals = race_country_medals.get(country)
                    country_styles.append(
                        _best_medal_style(country_medals) if country_medals else ""
                    )
                    country_keys.append(country)
                country_fmt = _make_medal_cell_formatter(
                    country_styles, race_country_medals, country_keys
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
                    output_format=output_format,
                    row_styles=country_styles,
                    cell_formatters=[None, country_fmt, None, None, None, None],
                )
                print()
            else:
                print(
                    _format_section_title(
                        f"{sec}. {medal_scope} medal table"
                        f" — {cat_name} {disc_name}: none",
                        args,
                    )
                )
                print()

        # Athlete medal table
        race_medalist_ids = gold_ids | silver_ids | bronze_ids
        ranked_athletes = [
            (rank, stats)
            for rank, stats in enumerate(sorted_athletes, 1)
            if stats["gold"] >= 1
            or (stats.get("ibu_id") and str(stats.get("ibu_id")) in race_medalist_ids)
            or (
                str(stats.get("name") or ""),
                str(stats.get("nat") or ""),
            )
            in race_medalist_name_nat
        ]
        if not is_relay and ranked_athletes:
            sec += 1
            print(
                _format_section_title(
                    f"{sec}. {medal_scope} medal table by athlete"
                    f" — {cat_name} {disc_name}"
                    f" ({medal_races_used} races):",
                    args,
                )
            )
            medal_gender = "F" if medal_cat_id == "SW" else "M"
            ath_rows = []
            ath_row_styles = []
            ath_keys = []
            for rank, stats in ranked_athletes:
                total = stats["gold"] + stats["silver"] + stats["bronze"]
                ibu_id = stats["ibu_id"]
                ath_rows.append(
                    [
                        str(rank),
                        stats["name"],
                        stats["nat"],
                        medal_gender,
                        str(stats["gold"]),
                        str(stats["silver"]),
                        str(stats["bronze"]),
                        str(total),
                        str(stats.get("races", 0)),
                    ]
                )
                if ibu_id and ibu_id in gold_ids:
                    ath_row_styles.append("gold")
                elif ibu_id and ibu_id in silver_ids:
                    ath_row_styles.append("silver")
                elif ibu_id and ibu_id in bronze_ids:
                    ath_row_styles.append("bronze")
                elif ibu_id and ibu_id in participating_ids:
                    ath_row_styles.append("highlight_plain")
                else:
                    ath_row_styles.append("dim")
                ath_keys.append(ibu_id)
            ath_name_fmt = _make_medal_cell_formatter(
                ath_row_styles, race_athlete_medals, ath_keys
            )
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
                ath_rows,
                output_format=output_format,
                row_styles=ath_row_styles,
                column_separators={4},
                cell_formatters=[
                    None,
                    ath_name_fmt,
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
        elif not is_relay:
            sec += 1
            print(
                _format_section_title(
                    f"{sec}. {medal_scope} medal table by athlete"
                    f" — {cat_name} {disc_name}: none",
                    args,
                )
            )
            print()

    # Olympic medal tables
    if event_type == EVENT_TYPE_OWG:
        sec = _render_olympic_medal_sections(
            args,
            sec,
            discipline,
            cat_id or str(comp.get("catId") or comp.get("CatId") or "").upper(),
            is_relay,
            participating_ids,
            gold_ids,
            silver_ids,
            bronze_ids,
            cutoff_dt=target_start_dt,
            race_country_medals=race_country_medals,
            race_athlete_medals=race_athlete_medals,
        )

    return 0
