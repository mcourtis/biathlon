"""Scores (standings) command handler."""

from __future__ import annotations

import argparse
import sys

from ..api import BiathlonError, get_cups, get_cup_results, get_current_season_id
from ..constants import GENDER_TO_CAT
from ..formatting import Color, is_pretty_output, get_output_format, render_table
from ._common import (
    DISCIPLINE_LEADER_MARKER,
    GENERAL_LEADER_MARKER,
    _format_leader_markers,
)


SCORE_TYPE_TO_DISCIPLINE = {
    "total": "TS",
    "sprint": "SP",
    "pursuit": "PU",
    "individual": "IN",
    "massstart": "MS",
    "mass-start": "MS",
    "relay": "RL",
    "nations": "NC",
    "nationscup": "NC",
    "nation": "NC",
}

DISCIPLINES = ["SP", "PU", "IN", "MS"]
DISCIPLINE_LABELS = {
    "SP": "Sprint",
    "PU": "Pursuit",
    "IN": "Individual",
    "MS": "Mass Start",
}

SORT_COLUMNS = {
    "total": "total",
    "sprint": "SP",
    "pursuit": "PU",
    "individual": "IN",
    "massstart": "MS",
    "mass-start": "MS",
}


def find_cup_id(season_id: str, gender: str, level: int, cup_type: str) -> str:
    """Return CupId matching season/gender/level/type."""
    discipline = SCORE_TYPE_TO_DISCIPLINE.get(cup_type.lower())
    if not discipline:
        raise BiathlonError(f"Unknown score type: {cup_type}")

    cat_id = GENDER_TO_CAT.get(gender.lower())
    if not cat_id:
        raise BiathlonError(f"Unknown gender: {gender}")

    for cup in get_cups(season_id):
        if (
            cup.get("CatId") == cat_id
            and cup.get("Level") == level
            and cup.get("DisciplineId") == discipline
        ):
            return str(cup.get("CupId"))

    raise BiathlonError(
        f"No cup found for season {season_id}, gender {gender}, level {level}, type {cup_type}"
    )


def _get_cup_ids_by_discipline(
    season_id: str, gender: str, level: int
) -> dict[str, str]:
    """Return dict of discipline -> cup_id for a season/gender/level."""
    cat_id = GENDER_TO_CAT.get(gender.lower())
    if not cat_id:
        raise BiathlonError(f"Unknown gender: {gender}")

    cup_ids = {}
    for cup in get_cups(season_id):
        if cup.get("CatId") == cat_id and cup.get("Level") == level:
            disc = cup.get("DisciplineId")
            if disc:
                cup_ids[disc] = str(cup.get("CupId"))
    return cup_ids


def _find_leaders(athlete_list: list[dict]) -> dict:
    """Find the leader (highest score) for total and each discipline."""
    leaders = {"total": None, "SP": None, "PU": None, "IN": None, "MS": None}

    for key in leaders:
        max_score = 0
        leader_name = None
        for athlete in athlete_list:
            score = athlete.get(key, 0)
            if score > max_score:
                max_score = score
                leader_name = athlete["name"]
        if max_score > 0:
            leaders[key] = leader_name

    return leaders


def handle_standings(args: argparse.Namespace) -> int:
    """List standings for a cup with discipline breakdown."""
    season_id = args.season or get_current_season_id()
    gender = "men" if args.men else "women"
    sort_by = getattr(args, "sort", None) or "total"
    try:
        level = int(args.level) if args.level else 1
    except ValueError:
        print("error: level must be an integer", file=sys.stderr)
        return 1

    # Validate sort column
    sort_col = SORT_COLUMNS.get(sort_by.lower())
    if not sort_col:
        valid = ", ".join(SORT_COLUMNS.keys())
        print(f"error: sort must be one of {valid}", file=sys.stderr)
        return 1

    cup_ids = _get_cup_ids_by_discipline(season_id, gender, level)

    # Get total standings first
    total_cup_id = cup_ids.get("TS")
    if not total_cup_id:
        print("no total standings cup found", file=sys.stderr)
        return 1

    total_payload = get_cup_results(total_cup_id)
    total_rows = total_payload.get("Rows") or total_payload.get("Results") or []
    if not total_rows:
        print(f"no standings found for cup {total_cup_id}", file=sys.stderr)
        return 1

    # Build athlete data from total standings
    athletes: dict[str, dict] = {}
    for row in total_rows:
        ibu_id = row.get("IBUId") or row.get("Id") or row.get("Name")
        if not ibu_id:
            continue
        athletes[ibu_id] = {
            "name": row.get("Name") or row.get("ShortName") or "",
            "nat": row.get("Nat") or "",
            "total": int(row.get("Score") or 0),
            "SP": 0,
            "PU": 0,
            "IN": 0,
            "MS": 0,
        }

    # Fetch discipline scores
    for disc in DISCIPLINES:
        disc_cup_id = cup_ids.get(disc)
        if not disc_cup_id:
            continue
        try:
            disc_payload = get_cup_results(disc_cup_id)
        except BiathlonError:
            continue
        disc_rows = disc_payload.get("Rows") or disc_payload.get("Results") or []
        for row in disc_rows:
            ibu_id = row.get("IBUId") or row.get("Id") or row.get("Name")
            if ibu_id and ibu_id in athletes:
                athletes[ibu_id][disc] = int(row.get("Score") or 0)

    # Convert to list and sort by total first to assign Position
    athlete_list = list(athletes.values())
    athlete_list.sort(key=lambda a: -a["total"])

    # Assign position based on total ranking
    for pos, athlete in enumerate(athlete_list, start=1):
        athlete["position"] = pos

    # Re-sort by discipline if requested
    sorting_by_discipline = sort_col != "total"
    if sorting_by_discipline:
        athlete_list.sort(key=lambda a: (-a[sort_col], -a["total"]))
        # Assign discipline position
        for disc_pos, athlete in enumerate(athlete_list, start=1):
            athlete["disc_position"] = disc_pos

    # Apply display limit
    limit_n = getattr(args, "limit", 25) or 0
    if limit_n > 0:
        athlete_list = athlete_list[:limit_n]

    # Find leaders for coloring
    leaders = _find_leaders(athlete_list)
    total_leader = leaders["total"]

    # Find athletes who lead any discipline but not total (for slight gold)
    discipline_leaders = set()
    # Build name -> list of led disciplines (in fixed order)
    athlete_led_disciplines: dict[str, list[str]] = {}
    for disc in DISCIPLINES:
        if leaders[disc]:
            athlete_led_disciplines.setdefault(leaders[disc], []).append(disc)
            if leaders[disc] != total_leader:
                discipline_leaders.add(leaders[disc])

    pretty = is_pretty_output(args)
    output_format = get_output_format(args)

    # Build render rows
    render_rows = []
    row_styles = []
    for athlete in athlete_list:
        name = athlete["name"]
        # Total leader gets gold row style
        if name == total_leader:
            row_styles.append("gold")
        else:
            row_styles.append("")
        # Append leader marker placeholders to name
        if pretty:
            markers = []
            if name == total_leader:
                markers.append(GENERAL_LEADER_MARKER)
            for _disc in athlete_led_disciplines.get(name, []):
                markers.append(DISCIPLINE_LEADER_MARKER)
            if markers:
                name = name + " " + " ".join(markers)
        row = [
            athlete["position"],
            name,
            athlete["nat"],
            athlete["total"],
            athlete["SP"] or "-",
            athlete["PU"] or "-",
            athlete["IN"] or "-",
            athlete["MS"] or "-",
        ]
        if sorting_by_discipline:
            row.insert(1, athlete["disc_position"])
        render_rows.append(row)

    headers = [
        "Position",
        "Name",
        "Country",
        "Total",
        "Sprint",
        "Pursuit",
        "Individual",
        "MassStart",
    ]
    if sorting_by_discipline:
        disc_label = DISCIPLINE_LABELS.get(sort_col, sort_col)
        headers.insert(1, f"{disc_label}Position")

    def make_slight_gold_formatter():
        """Formatter for Rank, Name, Country columns - gold for total leader, light gold for discipline leaders."""

        def formatter(cell_str: str, row_idx: int) -> str:
            if not Color.enabled():
                return cell_str
            athlete = athlete_list[row_idx]
            name = athlete["name"]
            if name == total_leader:
                return Color.gold(cell_str)
            if name in discipline_leaders:
                return Color.rgb(cell_str, Color.LIGHT_GOLD, bold=False)
            return cell_str

        return formatter

    def make_disc_formatter(disc_key: str):
        """Formatter for discipline columns - gold for total leader, light gold for discipline-only leader."""

        def formatter(cell_str: str, row_idx: int) -> str:
            if not Color.enabled():
                return cell_str
            athlete = athlete_list[row_idx]
            name = athlete["name"]
            if name == leaders[disc_key] and name == total_leader:
                return Color.gold(cell_str)
            if name == leaders[disc_key] and name != total_leader:
                return Color.rgb(cell_str, Color.LIGHT_GOLD, bold=False)
            return cell_str

        return formatter

    def make_name_formatter():
        """Formatter for Name column - leader markers + light gold for discipline leaders."""
        base = make_slight_gold_formatter()

        def formatter(cell_str: str, row_idx: int) -> str:
            return _format_leader_markers(cell_str, row_idx, base)

        return formatter

    if pretty:
        cell_formatters = [
            make_slight_gold_formatter(),  # Position
        ]
        if sorting_by_discipline:
            cell_formatters.append(None)  # DisciplinePosition - no special formatting
        cell_formatters.extend(
            [
                make_name_formatter(),  # Name
                make_slight_gold_formatter(),  # Country
                None,  # Total - no special formatting
                make_disc_formatter("SP"),
                make_disc_formatter("PU"),
                make_disc_formatter("IN"),
                make_disc_formatter("MS"),
            ]
        )
    else:
        cell_formatters = None

    # Highlight the column header used for sorting
    sort_header_map = {
        "total": "Total",
        "SP": "Sprint",
        "PU": "Pursuit",
        "IN": "Individual",
        "MS": "MassStart",
    }
    sort_header_name = sort_header_map.get(sort_col)
    highlight_headers = (
        [headers.index(sort_header_name)]
        if pretty and sort_header_name and sort_header_name in headers
        else None
    )

    render_table(
        headers,
        render_rows,
        output_format=output_format,
        row_styles=row_styles if pretty else None,
        cell_formatters=cell_formatters,
        highlight_headers=highlight_headers,
    )
    return 0
