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

COUNTRY_SORT_COLUMNS = {
    "women-nations": "women_nations",
    "men-nations": "men_nations",
    "women-relay": "women_relay",
    "men-relay": "men_relay",
    "mixed-relay": "mixed_relay",
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

    cup_ids: dict[str, str] = {}
    for cup in get_cups(season_id):
        if cup.get("CatId") == cat_id and cup.get("Level") == level:
            disc = cup.get("DisciplineId")
            if disc:
                cup_ids[disc] = str(cup.get("CupId"))
    return cup_ids


def _find_leaders(athlete_list: list[dict]) -> dict[str, str | None]:
    """Find the leader (highest score) for total and each discipline."""
    leaders: dict[str, str | None] = {
        "total": None,
        "SP": None,
        "PU": None,
        "IN": None,
        "MS": None,
    }

    for key in leaders:
        max_score = 0
        leader_name: str | None = None
        for athlete in athlete_list:
            score = athlete.get(key, 0)
            if score > max_score:
                max_score = score
                leader_name = athlete["name"]
        if max_score > 0:
            leaders[key] = leader_name

    return leaders


def _parse_score(row: dict) -> int:
    """Return an integer score value from a standings row."""
    value = row.get("Score")
    if value in (None, ""):
        value = row.get("Points")
    if value in (None, ""):
        value = row.get("TotalScore")
    if value in (None, ""):
        return 0
    if isinstance(value, int):
        return value
    text = str(value).strip().replace(",", "")
    if not text:
        return 0
    try:
        return int(text)
    except ValueError:
        try:
            return int(float(text))
        except ValueError:
            return 0


def _country_key_and_display(row: dict) -> tuple[str, str]:
    """Return a stable country key/display value from a row."""
    country_code = str(
        row.get("Nat")
        or row.get("Nation")
        or row.get("CountryCode")
        or row.get("Country")
        or ""
    ).strip()
    country_name = str(
        row.get("Name")
        or row.get("ShortName")
        or row.get("CountryName")
        or row.get("NationName")
        or ""
    ).strip()

    if country_code:
        code = country_code.upper()
        return code, code
    if len(country_name) == 3 and country_name.isalpha():
        code = country_name.upper()
        return code, code
    return country_name, country_name


def _get_country_cup_ids(season_id: str, level: int) -> dict[str, list[str]]:
    """Return cup ids for country-mode standings."""
    cup_ids: dict[str, list[str]] = {
        "women_nations": [],
        "men_nations": [],
        "women_relay": [],
        "men_relay": [],
        "mixed_relay": [],
    }

    for cup in get_cups(season_id):
        if cup.get("Level") != level:
            continue
        cup_id = str(cup.get("CupId") or "").strip()
        if not cup_id:
            continue
        cat = str(cup.get("CatId") or "").strip().upper()
        disc = str(cup.get("DisciplineId") or "").strip().upper()
        if disc == "NC" and cat == "SW":
            cup_ids["women_nations"].append(cup_id)
        elif disc == "NC" and cat == "SM":
            cup_ids["men_nations"].append(cup_id)
        elif disc == "RL" and cat == "SW":
            cup_ids["women_relay"].append(cup_id)
        elif disc == "RL" and cat == "SM":
            cup_ids["men_relay"].append(cup_id)
        elif cat == "MX" and disc in {"MR", "SR", "RL"}:
            cup_ids["mixed_relay"].append(cup_id)

    # De-duplicate while keeping order.
    for key, values in cup_ids.items():
        cup_ids[key] = list(dict.fromkeys(values))

    return cup_ids


def _find_country_leaders(country_list: list[dict]) -> dict[str, str | None]:
    """Return leader country name per country-standings column."""
    leaders: dict[str, str | None] = {
        "women_nations": None,
        "men_nations": None,
        "women_relay": None,
        "men_relay": None,
        "mixed_relay": None,
    }
    for key in leaders:
        max_score = 0
        leader_name: str | None = None
        for country in country_list:
            score = int(country.get(key) or 0)
            if score > max_score:
                max_score = score
                leader_name = str(country.get("country") or "")
        if max_score > 0 and leader_name:
            leaders[key] = leader_name
    return leaders


def handle_standings(args: argparse.Namespace) -> int:
    """List standings for a cup with discipline breakdown."""
    season_id = args.season or get_current_season_id()
    gender = "men" if args.men else "women"
    sort_by_raw = str(getattr(args, "sort", "") or "").strip()
    country_mode = bool(getattr(args, "country", False))
    try:
        level = int(args.level) if args.level else 1
    except ValueError:
        print("error: level must be an integer", file=sys.stderr)
        return 1

    if country_mode:
        country_sort_col: str | None
        if not sort_by_raw:
            country_sort_col = "men_nations" if gender == "men" else "women_nations"
        else:
            country_sort_col = COUNTRY_SORT_COLUMNS.get(sort_by_raw.lower())
        if country_sort_col is None:
            print(
                "error: when using --country, sort must be one of women-nations, men-nations, women-relay, men-relay, mixed-relay",
                file=sys.stderr,
            )
            return 1

        country_cup_ids = _get_country_cup_ids(season_id, level)
        if not any(country_cup_ids.values()):
            print(
                "no country standings cup found (women/men nations + relay standings)",
                file=sys.stderr,
            )
            return 1

        countries: dict[str, dict] = {}

        def merge_rows(rows: list[dict], target_key: str) -> None:
            for row in rows:
                country_key, display_country = _country_key_and_display(row)
                if not country_key:
                    continue
                entry = countries.setdefault(
                    country_key,
                    {
                        "country": display_country,
                        "women_nations": 0,
                        "men_nations": 0,
                        "women_relay": 0,
                        "men_relay": 0,
                        "mixed_relay": 0,
                        "nations_total": 0,
                        "relay_total": 0,
                    },
                )
                if not entry["country"] and display_country:
                    entry["country"] = display_country
                entry[target_key] += _parse_score(row)

        for cup_id in country_cup_ids["women_nations"]:
            payload = get_cup_results(cup_id)
            rows = payload.get("Rows") or payload.get("Results") or []
            merge_rows(rows, "women_nations")
        for cup_id in country_cup_ids["men_nations"]:
            payload = get_cup_results(cup_id)
            rows = payload.get("Rows") or payload.get("Results") or []
            merge_rows(rows, "men_nations")
        for cup_id in country_cup_ids["women_relay"]:
            payload = get_cup_results(cup_id)
            rows = payload.get("Rows") or payload.get("Results") or []
            merge_rows(rows, "women_relay")
        for cup_id in country_cup_ids["men_relay"]:
            payload = get_cup_results(cup_id)
            rows = payload.get("Rows") or payload.get("Results") or []
            merge_rows(rows, "men_relay")
        for cup_id in country_cup_ids["mixed_relay"]:
            payload = get_cup_results(cup_id)
            rows = payload.get("Rows") or payload.get("Results") or []
            merge_rows(rows, "mixed_relay")

        country_list = list(countries.values())
        if not country_list:
            print("no country standings rows found", file=sys.stderr)
            return 1

        for country in country_list:
            country["nations_total"] = int(country["women_nations"]) + int(
                country["men_nations"]
            )
            country["relay_total"] = (
                int(country["women_relay"])
                + int(country["men_relay"])
                + int(country["mixed_relay"])
            )

        country_list.sort(
            key=lambda c: (
                -int(c[country_sort_col]),
                -int(c["nations_total"]),
                -int(c["relay_total"]),
                str(c["country"]),
            )
        )

        for pos, country in enumerate(country_list, start=1):
            country["position"] = pos

        limit_n = getattr(args, "limit", 25) or 0
        if limit_n > 0:
            country_list = country_list[:limit_n]

        pretty = is_pretty_output(args)
        output_format = get_output_format(args)
        headers = [
            "Position",
            "Country",
            "Women Nations Cup",
            "Men Nations Cup",
            "Women Relay",
            "Men Relay",
            "Mixed Relay",
        ]
        leaders = _find_country_leaders(country_list)
        leader_countries = {name for name in leaders.values() if name}
        render_rows = []
        row_styles = []
        for row in country_list:
            country_name = str(row["country"])
            if pretty:
                markers: list[str] = []
                for key in (
                    "women_nations",
                    "men_nations",
                    "women_relay",
                    "men_relay",
                    "mixed_relay",
                ):
                    if leaders.get(key) == country_name:
                        markers.append(DISCIPLINE_LEADER_MARKER)
                if markers:
                    country_name = country_name + " " + " ".join(markers)
            render_rows.append(
                [
                    row["position"],
                    country_name,
                    row["women_nations"],
                    row["men_nations"],
                    row["women_relay"],
                    row["men_relay"],
                    row["mixed_relay"],
                ]
            )
            row_styles.append("")

        def make_country_name_formatter():
            """Formatter for Country column - color leaders and render red dots."""

            def base_formatter(cell_str: str, row_idx: int) -> str:
                if not Color.enabled():
                    return cell_str
                country_name = str(country_list[row_idx]["country"])
                if country_name in leader_countries:
                    return Color.gold(cell_str)
                return cell_str

            def formatter(cell_str: str, row_idx: int) -> str:
                return _format_leader_markers(cell_str, row_idx, base_formatter)

            return formatter

        def make_country_points_formatter(column_key: str):
            """Formatter for country points columns - color the leader values."""

            def formatter(cell_str: str, row_idx: int) -> str:
                if not Color.enabled():
                    return cell_str
                country_name = str(country_list[row_idx]["country"])
                if leaders.get(column_key) == country_name:
                    return Color.gold(cell_str)
                return cell_str

            return formatter

        cell_formatters = None
        if pretty:
            cell_formatters = [
                None,
                make_country_name_formatter(),
                make_country_points_formatter("women_nations"),
                make_country_points_formatter("men_nations"),
                make_country_points_formatter("women_relay"),
                make_country_points_formatter("men_relay"),
                make_country_points_formatter("mixed_relay"),
            ]

        sort_header_map = {
            "women_nations": "Women Nations Cup",
            "men_nations": "Men Nations Cup",
            "women_relay": "Women Relay",
            "men_relay": "Men Relay",
            "mixed_relay": "Mixed Relay",
        }
        sort_header_name = sort_header_map.get(country_sort_col)
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
            column_separators={2, 4},
        )
        return 0

    # Validate athlete-mode sort column
    athlete_sort_col = SORT_COLUMNS.get(sort_by_raw.lower() if sort_by_raw else "total")
    if athlete_sort_col is None:
        valid = ", ".join(SORT_COLUMNS.keys())
        print(
            f"error: sort must be one of {valid} (or use --country with women-nations/men-nations/women-relay/men-relay/mixed-relay)",
            file=sys.stderr,
        )
        return 1

    athlete_cup_ids = _get_cup_ids_by_discipline(season_id, gender, level)

    # Get total standings first
    total_cup_id = athlete_cup_ids.get("TS")
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
            "total": _parse_score(row),
            "SP": 0,
            "PU": 0,
            "IN": 0,
            "MS": 0,
        }

    # Fetch discipline scores
    for disc in DISCIPLINES:
        disc_cup_id = athlete_cup_ids.get(disc)
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
                athletes[ibu_id][disc] = _parse_score(row)

    # Convert to list and sort by total first to assign Position
    athlete_list = list(athletes.values())
    athlete_list.sort(key=lambda a: -a["total"])

    # Assign position based on total ranking
    for pos, athlete in enumerate(athlete_list, start=1):
        athlete["position"] = pos

    # Re-sort by discipline if requested
    sorting_by_discipline = athlete_sort_col != "total"
    if sorting_by_discipline:
        athlete_list.sort(key=lambda a: (-a[athlete_sort_col], -a["total"]))
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
        leader_name = leaders.get(disc)
        if leader_name is None:
            continue
        athlete_led_disciplines.setdefault(leader_name, []).append(disc)
        if leader_name != total_leader:
            discipline_leaders.add(leader_name)

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
        render_row = [
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
            render_row.insert(1, athlete["disc_position"])
        render_rows.append(render_row)

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
        disc_label = DISCIPLINE_LABELS.get(athlete_sort_col, athlete_sort_col)
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
    sort_header_name = sort_header_map.get(athlete_sort_col)
    highlight_headers = (
        [headers.index(sort_header_name)]
        if pretty and sort_header_name and sort_header_name in headers
        else None
    )
    if sorting_by_discipline:
        athlete_column_separators = {4, 5}
    else:
        athlete_column_separators = {3, 4}

    render_table(
        headers,
        render_rows,
        output_format=output_format,
        row_styles=row_styles if pretty else None,
        cell_formatters=cell_formatters,
        highlight_headers=highlight_headers,
        column_separators=athlete_column_separators,
    )
    return 0
