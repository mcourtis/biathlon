"""Scores (standings) command handler."""

from __future__ import annotations

import argparse
import datetime
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..api import (
    BiathlonError,
    get_athlete_bio,
    get_cups,
    get_cup_results,
    get_current_season_id,
    get_events,
    get_races,
)
from ..constants import GENDER_TO_CAT
from ..formatting import Color, is_pretty_output, get_output_format, render_table
from ..utils import parse_date
from ._common import (
    DISCIPLINE_LEADER_MARKER,
    GENERAL_LEADER_MARKER,
    U23_LEADER_MARKER,
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

ATHLETE_STANDINGS_HEADERS = [
    "Rank",
    "Name",
    "Nat",
    "Age",
    "Total",
    "Sprint",
    "Pursuit",
    "Individual",
    "MassStart",
]
ATHLETE_STANDINGS_COLUMN_SEPARATORS = {4, 5}

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
                cup_ids.setdefault(disc, str(cup.get("CupId")))
    return cup_ids


def _parse_birth_date_value(value: object) -> datetime.date | None:
    """Parse a birth date from a bio field value."""
    text = str(value or "").strip()
    if not text:
        return None

    parsed = parse_date(text)
    if parsed is not None:
        return parsed

    compact = text.replace(" ", "")
    for fmt in ("%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.datetime.strptime(compact, fmt).date()
        except ValueError:
            continue
    return None


def _extract_birth_date(bio: dict) -> datetime.date | None:
    """Return athlete birth date from a bio payload."""
    for key in ("BirthDate", "Birthdate", "DateOfBirth", "DOB", "Birthday"):
        parsed = _parse_birth_date_value(bio.get(key))
        if parsed is not None:
            return parsed

    for item in bio.get("Personal", []):
        label = str(item.get("Description") or "").strip().lower()
        if not label:
            continue
        if "birth" in label or "born" in label:
            parsed = _parse_birth_date_value(item.get("Value"))
            if parsed is not None:
                return parsed
    return None


def _extract_age_text(bio: dict) -> str | None:
    """Extract age text from athlete bio payload."""
    personal = {
        str(item.get("Description") or "").strip().lower(): item.get("Value")
        for item in bio.get("Personal", [])
        if item.get("Description")
    }
    value = bio.get("Age") or personal.get("age")
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not text:
        return None
    if "," in text:
        text = text.split(",", 1)[0].strip()
    return text or None


def _age_on_date(birth_date: datetime.date, reference_date: datetime.date) -> int:
    """Return age in full years at reference date."""
    years = reference_date.year - birth_date.year
    if (reference_date.month, reference_date.day) < (birth_date.month, birth_date.day):
        years -= 1
    return years


def _find_first_race_date(
    season_id: str, gender: str, level: int
) -> datetime.date | None:
    """Return first individual-race date for season/gender/level."""
    cat_id = GENDER_TO_CAT.get(gender.lower())
    if not cat_id:
        return None

    earliest: datetime.date | None = None
    for event in get_events(season_id, level):
        event_id = str(event.get("EventId") or "").strip()
        if not event_id:
            continue
        try:
            races = get_races(event_id)
        except BiathlonError:
            continue
        for race in races:
            race_cat = str(race.get("catId") or race.get("CatId") or "").strip().upper()
            if race_cat != cat_id:
                continue
            discipline = str(race.get("DisciplineId") or "").strip().upper()
            if discipline not in DISCIPLINES:
                continue
            start_raw = race.get("StartTime") or race.get("StartDate")
            start_date = parse_date(str(start_raw) if start_raw else None)
            if start_date is None:
                continue
            if earliest is None or start_date < earliest:
                earliest = start_date
    return earliest


def _prefetch_bios(ibu_ids: list[str]) -> dict[str, dict]:
    """Fetch athlete bios concurrently."""
    unique_ids = [ibu_id for ibu_id in dict.fromkeys(ibu_ids) if ibu_id]
    if not unique_ids:
        return {}

    bios: dict[str, dict] = {}
    max_workers = min(16, max(1, len(unique_ids)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(get_athlete_bio, ibu_id): ibu_id for ibu_id in unique_ids
        }
        for future in as_completed(future_map):
            ibu_id = future_map[future]
            try:
                payload = future.result()
            except BiathlonError:
                payload = {}
            except Exception:
                payload = {}
            bios[ibu_id] = payload if isinstance(payload, dict) else {}
    return bios


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


def _flag_is_true(value: object) -> bool:
    """Return True when *value* looks like a true/active flag."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    if isinstance(value, float):
        return value == 1.0
    text = str(value).strip().lower()
    return text in {"1", "true", "yes", "y", "x"}


def _rank_is_one(value: object) -> bool:
    """Return True when *value* is rank 1."""
    text = str(value).strip().rstrip(".")
    return text == "1"


def _collect_text_tokens(value: object) -> list[str]:
    """Collect normalized string tokens from nested payload data."""
    tokens: list[str] = []
    stack: list[object] = [value]
    while stack:
        current = stack.pop()
        if current is None:
            continue
        if isinstance(current, dict):
            for key, nested in current.items():
                key_text = str(key).strip().lower()
                if key_text:
                    tokens.append(key_text)
                stack.append(nested)
            continue
        if isinstance(current, (list, tuple, set)):
            stack.extend(current)
            continue
        text = str(current).strip().lower()
        if text:
            tokens.append(text)
    return tokens


def _bibs_indicate_u23(value: object) -> bool:
    """Return True when bib metadata indicates a best-U23 marker."""
    tokens = _collect_text_tokens(value)
    if not tokens:
        return False
    if any("u23" in token or "u-23" in token or "u 23" in token for token in tokens):
        return True
    if any("u25" in token or "u-25" in token or "u 25" in token for token in tokens):
        return True
    if any("young" in token for token in tokens):
        return True
    return any("blue" in token for token in tokens)


def _is_best_u23_row(row: dict) -> bool:
    """Return True when a standings row is marked as best U23."""
    for key, value in row.items():
        key_text = str(key).strip().lower()
        if any(tag in key_text for tag in ("u23", "u-23", "u25", "u-25")):
            if _flag_is_true(value) or _rank_is_one(value):
                return True
        if (
            "young" in key_text
            and any(tag in key_text for tag in ("best", "leader", "bib"))
            and (_flag_is_true(value) or _rank_is_one(value))
        ):
            return True
        if "bib" in key_text and _bibs_indicate_u23(value):
            return True
    return False


def _row_athlete_id(row: dict) -> str:
    """Return athlete identifier from a standings row."""
    value = row.get("IBUId") or row.get("IbuId") or row.get("ibuId") or row.get("Id")
    return str(value or "").strip()


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
    u23_only = bool(getattr(args, "u23", False))
    try:
        level = int(args.level) if args.level else 1
    except ValueError:
        print("error: level must be an integer", file=sys.stderr)
        return 1

    if country_mode:
        if u23_only:
            print(
                "error: --u23 is only available for athlete standings (without --country)",
                file=sys.stderr,
            )
            return 1

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
        ibu_id = _row_athlete_id(row) or row.get("Name")
        if not ibu_id:
            continue
        athletes[ibu_id] = {
            "ibu_id": str(ibu_id),
            "name": row.get("Name") or row.get("ShortName") or "",
            "nat": row.get("Nat") or "",
            "total": _parse_score(row),
            "SP": 0,
            "PU": 0,
            "IN": 0,
            "MS": 0,
            "row_best_u23": _is_best_u23_row(row),
            "is_best_u23": False,
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
            ibu_id = _row_athlete_id(row) or row.get("Name")
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

    # Mark best U23 athlete(s): under 23 at season's first individual race.
    first_race_date: datetime.date | None
    try:
        first_race_date = _find_first_race_date(season_id, gender, level)
    except BiathlonError:
        first_race_date = None

    limit_n = getattr(args, "limit", 25) or 0

    # For regular standings, only rows that can be displayed need bio lookups.
    # For --u23 (or unlimited output), we need all athletes to classify/filter.
    if u23_only or limit_n <= 0:
        bio_target = athlete_list
    else:
        bio_target = athlete_list[:limit_n]
    bio_map = _prefetch_bios(
        [str(athlete.get("ibu_id") or "") for athlete in bio_target]
    )

    for athlete in athlete_list:
        athlete["is_u23"] = False
        athlete["age_display"] = "-"
        bio = bio_map.get(athlete["ibu_id"], {})
        if first_race_date is not None:
            birth_date = _extract_birth_date(bio)
            if birth_date is not None:
                age_years = _age_on_date(birth_date, first_race_date)
                athlete["age_display"] = str(age_years)
                athlete["is_u23"] = age_years < 23
                continue
        age_text = _extract_age_text(bio)
        if age_text:
            athlete["age_display"] = age_text

    # Fallback for payloads where API already exposes U23 marker fields.
    if not any(athlete.get("is_u23") for athlete in athlete_list):
        for athlete in athlete_list:
            athlete["is_u23"] = bool(athlete["row_best_u23"])

    for athlete in athlete_list:
        if not athlete.get("is_u23"):
            continue
        age_display = str(athlete.get("age_display") or "").strip()
        if not age_display or age_display == "-":
            athlete["age_display"] = "(U23)"
        elif "(U23)" not in age_display:
            athlete["age_display"] = f"{age_display} (U23)"

    best_u23_score = max(
        (
            int(athlete.get(athlete_sort_col) or 0)
            for athlete in athlete_list
            if athlete.get("is_u23")
        ),
        default=0,
    )
    best_u23_ids: set[str] = {
        athlete["ibu_id"]
        for athlete in athlete_list
        if athlete.get("is_u23")
        and int(athlete.get(athlete_sort_col) or 0) == best_u23_score
        and best_u23_score > 0
    }
    for athlete in athlete_list:
        athlete["is_best_u23"] = athlete["ibu_id"] in best_u23_ids

    # Keep full-standings leaders for --u23 mode so yellow/red markers apply
    # only when a U23 athlete is also an actual overall leader.
    full_standings_leaders = _find_leaders(athlete_list)

    if u23_only:
        athlete_list = [athlete for athlete in athlete_list if athlete.get("is_u23")]
        if not athlete_list:
            print("no U23 athletes found in standings", file=sys.stderr)
            return 1

    # Apply display limit
    if limit_n > 0:
        athlete_list = athlete_list[:limit_n]

    # Find leaders for coloring
    leaders = full_standings_leaders if u23_only else _find_leaders(athlete_list)
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
        base_name = athlete["name"]
        name = base_name
        # Total leader gets gold row style
        if base_name == total_leader:
            row_styles.append("gold")
        else:
            row_styles.append("")
        # Append leader marker placeholders to name
        if pretty:
            markers = []
            if base_name == total_leader:
                markers.append(GENERAL_LEADER_MARKER)
            for _disc in athlete_led_disciplines.get(base_name, []):
                markers.append(DISCIPLINE_LEADER_MARKER)
            if athlete["is_best_u23"]:
                markers.append(U23_LEADER_MARKER)
            if markers:
                name = name + " " + " ".join(markers)
        render_row = [
            athlete["position"],
            name,
            athlete["nat"],
            athlete["age_display"],
            athlete["total"],
            athlete["SP"] or "-",
            athlete["PU"] or "-",
            athlete["IN"] or "-",
            athlete["MS"] or "-",
        ]
        if sorting_by_discipline:
            render_row.insert(1, athlete["disc_position"])
        render_rows.append(render_row)

    headers = list(ATHLETE_STANDINGS_HEADERS)
    if sorting_by_discipline:
        disc_label = DISCIPLINE_LABELS.get(athlete_sort_col, athlete_sort_col)
        headers.insert(1, f"{disc_label}Position")

    def _highlight_athlete_cell(
        cell_str: str, row_idx: int, *, bold_secondary: bool
    ) -> str:
        """Apply athlete highlight color for a given cell.

        Priority:
        1. Total leader -> gold
        2. Discipline leader -> light gold
        3. Best U23 -> dark blue
        """
        if not Color.enabled():
            return cell_str
        athlete = athlete_list[row_idx]
        name = athlete["name"]
        if name == total_leader:
            return Color.gold(cell_str)
        if name in discipline_leaders:
            return Color.rgb(cell_str, Color.LIGHT_GOLD, bold=bold_secondary)
        if athlete["is_best_u23"]:
            return Color.dark_blue(cell_str, bold=bold_secondary)
        return cell_str

    def make_slight_gold_formatter():
        """Formatter for Rank/Country columns."""

        def formatter(cell_str: str, row_idx: int) -> str:
            return _highlight_athlete_cell(cell_str, row_idx, bold_secondary=False)

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
        """Formatter for Name column - colored + bold athletes + leader markers."""

        def base(cell_str: str, row_idx: int) -> str:
            return _highlight_athlete_cell(cell_str, row_idx, bold_secondary=True)

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
                make_slight_gold_formatter(),  # Age
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
        athlete_column_separators = {5, 6}
    else:
        athlete_column_separators = ATHLETE_STANDINGS_COLUMN_SEPARATORS

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
