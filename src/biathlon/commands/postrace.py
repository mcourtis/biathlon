"""Post-race analysis command handler."""

from __future__ import annotations

import argparse
import contextlib
import datetime
import io
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, TypedDict

from ..api import (
    BiathlonError,
    get_all_results,
    get_analytic_results,
    get_athlete_bio,
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
    EVENT_TYPE_WCH,
    INDIVIDUAL_DISCIPLINES,
    RELAY_DISCIPLINE,
    RELAY_DISCIPLINES,
    SINGLE_MIXED_RELAY_DISCIPLINE,
    SKI_LAPS,
)
from ..formatting import (
    Color,
    _display_width,
    is_pretty_output,
    get_output_format,
    render_table,
    rank_style,
)
from ..utils import (
    parse_date,
    parse_start_datetime,
    format_race_header,
    get_first_time,
    parse_time_seconds,
    format_seconds,
)
from ._common import (
    DISCIPLINE_LEADER_MARKER,
    GENERAL_LEADER_MARKER,
    U23_LEADER_MARKER,
    _birth_year_from_ibu_id,
    counts_toward_wc_standings,
    _format_section_title,
    _has_completed_relay_results,
    _is_result_at_or_before_target,
    _is_team_level_result,
    _normalize_discipline_id,
    _ordinal,
    _parse_rank,
    _result_discipline_id,
    _row_ibu_id,
    _season_end_year,
    _season_id_from_race_id,
    _select_race_interactive,
    _start_dt_from_competition,
    _warn_once,
    detect_event_type,
    is_relay_discipline as _is_relay_discipline,
)
from .brief import (
    _build_event_type_decorated_athlete_rows,
    _build_major_events_decorated_athlete_rows,
    _build_venue_decorated_athlete_rows,
    _render_postevent_decorated_delta_split_tables,
)
from .results import _find_recent_completed_races, _has_completed_results
from .startlist import (
    OLYMPIC_SEASON_IDS,
    _extract_venue_name,
    _fetch_nations_cup_standings,
    _fetch_relay_wc_standings,
    _get_all_olympic_medals,
    _get_all_wch_medals,
    _get_cup_ids_for_race,
    _get_past_olympic_individual_podiums,
    _get_past_olympic_relay_podiums,
    _get_wc_points,
    _render_athlete_all_medal_table,
    _render_country_all_medal_table,
    _parse_points_number,
    _standings_points_cell_formatter,
    _standings_points_text,
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


class _StandingsEntryBase(TypedDict):
    rank: int
    ibu_id: str
    name: str
    nat: str
    age: str
    race_points: int
    total_points: Any
    change: str
    participated: bool
    raw_rnkdiff: int | None


class _StandingsEntry(_StandingsEntryBase, total=False):
    u23_rank: int
    u23_change: str


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
            get_race_results_fn=get_race_results,
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
    """Return True when *value* looks like an active flag."""
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
    """Collect normalized string tokens from nested payload values."""
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


def _parse_birth_date_value(value: object) -> datetime.date | None:
    """Parse a birth date from common API value formats."""
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
    """Return age in full years at *reference_date*."""
    years = reference_date.year - birth_date.year
    if (reference_date.month, reference_date.day) < (birth_date.month, birth_date.day):
        years -= 1
    return years


def _extract_age_years(text: str) -> int | None:
    match = re.search(r"\d{1,2}", text or "")
    if not match:
        return None
    return int(match.group(0))


def _build_athlete_age_map(
    ibu_ids: set[str], reference_date: datetime.date, u23_cutoff_year: int | None = None
) -> tuple[dict[str, str], set[str]]:
    """Return (IBU id -> age display, U23 ids) for athlete ids.

    U23 is determined by birth year when u23_cutoff_year is given
    (athletes born in u23_cutoff_year or later are U23). When no cutoff
    year is provided, age-based fallback detection (age <= 23) is used.
    """
    unique_ids = [ibu_id for ibu_id in dict.fromkeys(ibu_ids) if ibu_id]
    if not unique_ids:
        return {}, set()

    age_display_by_id: dict[str, str] = {}
    u23_ids: set[str] = set()
    max_workers = min(16, max(1, len(unique_ids)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(get_athlete_bio, ibu_id): ibu_id for ibu_id in unique_ids
        }
        for future in futures:
            ibu_id = futures[future]
            try:
                bio = future.result()
            except BiathlonError:
                bio = {}
            except Exception:
                bio = {}

            age_display = "-"
            is_u23 = False
            birth_date = _extract_birth_date(bio)
            if birth_date is not None:
                age_years = _age_on_date(birth_date, reference_date)
                age_display = str(age_years)
                if u23_cutoff_year is not None:
                    is_u23 = birth_date.year >= u23_cutoff_year
                else:
                    is_u23 = age_years <= 23
            else:
                birth_year = _birth_year_from_ibu_id(ibu_id)
                age_text = _extract_age_text(bio)
                if age_text:
                    age_display = age_text
                if u23_cutoff_year is not None:
                    if birth_year is not None:
                        is_u23 = birth_year >= u23_cutoff_year
                else:
                    parsed_age_years = _extract_age_years(age_display)
                    if parsed_age_years is not None:
                        is_u23 = parsed_age_years <= 23

            if is_u23:
                u23_ids.add(ibu_id)
                if age_display == "-":
                    age_display = "(U23)"
                elif "(U23)" not in age_display:
                    age_display = f"{age_display} (U23)"

            age_display_by_id[ibu_id] = age_display

    return age_display_by_id, u23_ids


def _prefetch_all_results(ibu_ids: set[str]) -> dict[str, list[dict]]:
    """Fetch career result rows once per IBU id."""
    unique_ids = [ibu_id for ibu_id in dict.fromkeys(ibu_ids) if ibu_id]
    if not unique_ids:
        return {}

    all_results_cache: dict[str, list[dict]] = {}
    max_workers = min(16, max(1, len(unique_ids)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(get_all_results, ibu_id): ibu_id for ibu_id in unique_ids
        }
        for future in as_completed(futures):
            ibu_id = futures[future]
            try:
                payload = future.result()
            except BiathlonError:
                payload = {}
            except Exception:
                payload = {}
            all_results_cache[ibu_id] = list(payload.get("Results") or [])
    return all_results_cache


def _find_best_u23_leader(rows: list[dict], u23_ids: set[str]) -> dict[str, str]:
    """Return best-U23 leader metadata from standings rows."""
    candidates: list[dict] = [row for row in rows if _is_best_u23_row(row)]
    if not candidates:
        candidates = [row for row in rows if _row_ibu_id(row) in u23_ids]
    if not candidates:
        return {"id": "", "name": "", "nat": ""}

    def _sort_key(row: dict) -> tuple[int, int, str]:
        rank_val = _parse_rank(
            row.get("Rank") or row.get("Standing") or row.get("ResultOrder")
        )
        rank = rank_val if rank_val is not None else 10**9
        return (rank, -_parse_score(row), _row_ibu_id(row))

    best = min(candidates, key=_sort_key)
    return {
        "id": _row_ibu_id(best),
        "name": str(best.get("Name") or best.get("ShortName") or ""),
        "nat": str(best.get("Nat") or ""),
    }


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


class _TtyPreservingBuffer(io.StringIO):
    """StringIO that delegates isatty() to the real stdout."""

    def __init__(self, real_stdout: Any) -> None:
        super().__init__()
        self._real_stdout = real_stdout

    def isatty(self) -> bool:
        return bool(self._real_stdout.isatty())


def _capture_rendered_lines(render_fn: Callable[[], None]) -> list[str]:
    buf = _TtyPreservingBuffer(sys.stdout)
    with contextlib.redirect_stdout(buf):
        render_fn()
    text = buf.getvalue().rstrip("\n")
    return text.split("\n") if text else []


def _merge_tables_side_by_side(
    left_lines: list[str],
    right_lines: list[str],
    sep: str = "  │  ",
) -> list[str]:
    left_width = max((_display_width(line) for line in left_lines), default=0)
    row_count = max(len(left_lines), len(right_lines))
    merged: list[str] = []
    for idx in range(row_count):
        left = left_lines[idx] if idx < len(left_lines) else ""
        right = right_lines[idx] if idx < len(right_lines) else ""
        merged.append(left + " " * (left_width - _display_width(left)) + sep + right)
    return merged


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
            headers.append(athlete_name)
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
    u23_leader: dict[str, str],
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
    if mode in {"any", "u23"} and matches(u23_leader):
        markers.append(U23_LEADER_MARKER)
    if not markers:
        return ""
    return " " + " ".join(markers)


def _make_leader_name_decorator(
    general_leader: dict[str, str],
    discipline_leader: dict[str, str],
    u23_leader: dict[str, str],
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
            u23_leader,
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
            U23_LEADER_MARKER,
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
                elif marker == DISCIPLINE_LEADER_MARKER:
                    colored.append(Color.red("●"))
                else:
                    colored.append(Color.dark_blue("●", bold=True))
            base = f"{base} {' '.join(colored)}" if base else " ".join(colored)

        return base

    return _formatter


def _base_name_without_markers(text: str) -> str:
    tokens = str(text or "").split()
    while tokens and tokens[-1] in {
        GENERAL_LEADER_MARKER,
        DISCIPLINE_LEADER_MARKER,
        U23_LEADER_MARKER,
    }:
        tokens.pop()
    return " ".join(tokens)


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
    age_display_by_id: dict[str, str] | None = None,
    u23_mode: bool = False,
) -> tuple[list[list[str]], list[str]]:
    entries: list[_StandingsEntry] = []
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
        raw_rnkdiff: int | None = None
        if u23_mode:
            for key in ("RnkDiff", "RankDiff", "RankChange"):
                if key in row:
                    raw_rnkdiff = _parse_int(row.get(key))
                    if raw_rnkdiff is None:
                        raw_rnkdiff = 0  # null RnkDiff = no rank change (consistent with _extract_rank_and_change)
                    break
        entries.append(
            {
                "rank": rank_val,
                "ibu_id": ibu_id,
                "name": name,
                "nat": nat,
                "age": (age_display_by_id or {}).get(ibu_id, "-"),
                "race_points": race_points,
                "total_points": total_points,
                "change": change,
                "participated": participated,
                "raw_rnkdiff": raw_rnkdiff,
            }
        )
    entries.sort(key=lambda e: e["rank"])
    entries = entries[:top_n]
    if u23_mode:
        # Assign sequential U23 ranks based on current WC rank order
        for u23_rank, entry in enumerate(entries, 1):
            entry["u23_rank"] = u23_rank
        # Compute previous WC rank: prev = current - RnkDiff
        # (RnkDiff = current_rank - prev_rank, so prev_rank = current_rank - RnkDiff)
        # Include all entries in prev_ranked using raw_rnkdiff or 0 for unknowns so
        # that athletes with missing RnkDiff (e.g. null from API) still occupy their
        # correct estimated slot and don't skew rank-change values for others.
        prev_ranked = sorted(
            entries,
            key=lambda e: e["rank"] - (e["raw_rnkdiff"] or 0),
        )
        prev_u23_rank_by_id = {e["ibu_id"]: i + 1 for i, e in enumerate(prev_ranked)}
        for entry in entries:
            if entry["raw_rnkdiff"] is None:
                # Previous rank unknown → cannot determine U23 rank change
                entry["u23_change"] = "-"
            else:
                prev_u23_rank = prev_u23_rank_by_id.get(entry["ibu_id"])
                if prev_u23_rank is None:
                    entry["u23_change"] = "-"
                else:
                    diff = prev_u23_rank - entry["u23_rank"]  # positive = improved
                    entry["u23_change"] = "=" if diff == 0 else f"{diff:+d}"
    leader_points = (
        _parse_points_number(entries[0]["total_points"]) if entries else None
    )
    rows_out: list[list[str]] = []
    row_styles: list[str] = []
    for row_idx, entry in enumerate(entries):
        total_points_text = _standings_points_text(
            {"Score": entry["total_points"]},
            leader_points,
            row_idx,
        )
        if u23_mode:
            rows_out.append(
                [
                    str(entry["u23_rank"]),
                    str(entry["rank"]),
                    str(entry["name"]),
                    str(entry["age"]),
                    str(entry["nat"]),
                    _format_race_points(entry["race_points"]),
                    total_points_text,
                    str(entry["u23_change"]),
                ]
            )
        else:
            rows_out.append(
                [
                    str(entry["rank"]),
                    str(entry["name"]),
                    str(entry["age"]),
                    str(entry["nat"]),
                    _format_race_points(entry["race_points"]),
                    total_points_text,
                    str(entry["change"]),
                ]
            )
        row_styles.append("" if entry["participated"] else "dim")
    return rows_out, row_styles


def _is_u23_standings_row(row: dict, u23_ids: set[str]) -> bool:
    groups = str(row.get("Groups") or "").strip().upper()
    return groups == "U23" or _row_ibu_id(row) in u23_ids


def _results_column_separators(headers: list[str]) -> set[int] | None:
    if headers and headers[-1] == "Points":
        return {len(headers) - 1}
    return None


def _best_performances_column_separators() -> set[int]:
    return {3}


def _milestone_subsection_column_separators() -> set[int]:
    return {3}


def _milestone_athlete_row_separators(rows: list[list]) -> set[int]:
    return {
        idx for idx in range(1, len(rows)) if str(rows[idx][3]) != str(rows[idx - 1][3])
    }


def _build_nations_cup_table_rows(
    nation_rows: list[dict],
    nc_race_by_nat: dict[str, float],
) -> list[list[str]]:
    table_rows: list[list[str]] = []
    leader_points = (
        _parse_points_number(
            (nation_rows[0] if nation_rows else {}).get("Score")
            or (nation_rows[0] if nation_rows else {}).get("Points")
        )
        if nation_rows
        else None
    )
    for row_idx, standing_row in enumerate(nation_rows):
        rank = str(
            standing_row.get("Rank") or standing_row.get("Standing") or row_idx + 1
        ).rstrip(".")
        nat = str(standing_row.get("Nat") or "")
        country = str(standing_row.get("Name") or nat)
        total_pts = _standings_points_text(standing_row, leader_points, row_idx)
        _, change = _extract_rank_and_change(standing_row)
        race_nc = nc_race_by_nat.get(nat)
        race_pts = f"+{int(race_nc)}" if race_nc else ""
        table_rows.append([rank, country, total_pts, race_pts, change])
    return table_rows


def _render_wc_standings_table_pair(
    title: str,
    args: argparse.Namespace,
    output_format: str,
    pretty: bool,
    main_rows: list[list[str]],
    main_row_styles: list[str],
    main_name_formatter: Callable[[str, int], str],
    u23_rows: list[list[str]],
    u23_row_styles: list[str],
    u23_name_formatter: Callable[[str, int], str],
) -> None:
    print(_format_section_title(title, args))
    print()
    main_total_points_formatter = _standings_points_cell_formatter(
        set(),
        leader_rows={0},
        point_cells=[row[5] for row in main_rows],
        pretty=pretty,
    )
    u23_total_points_formatter = _standings_points_cell_formatter(
        set(),
        leader_rows={0},
        point_cells=[row[6] for row in u23_rows],
        pretty=pretty,
    )

    def _render_main() -> None:
        render_table(
            [
                "Rank",
                "Athlete",
                "Age",
                "Nat",
                "Race Pts",
                "Total Pts",
                "Change",
            ],
            main_rows,
            output_format=output_format,
            cell_formatters=[
                None,
                main_name_formatter,
                None,
                None,
                _format_race_points_cell,
                main_total_points_formatter,
                _format_change_cell,
            ],
            column_separators={4},
            row_styles=main_row_styles,
        )

    def _render_u23() -> None:
        render_table(
            [
                "Rank",
                "WC Rank",
                "Athlete",
                "Age",
                "Nat",
                "Race Pts",
                "Total Pts",
                "Change",
            ],
            u23_rows,
            output_format=output_format,
            cell_formatters=[
                None,
                None,
                u23_name_formatter,
                None,
                None,
                _format_race_points_cell,
                u23_total_points_formatter,
                _format_change_cell,
            ],
            column_separators={5},
            row_styles=u23_row_styles,
        )

    if pretty and u23_rows:
        left_lines = _capture_rendered_lines(_render_main)
        right_lines = _capture_rendered_lines(_render_u23)
        print("\n".join(_merge_tables_side_by_side(left_lines, right_lines)))
        print()
        print()
        return

    _render_main()
    print()
    if u23_rows:
        print(_format_section_title("### U23", args))
        print()
        _render_u23()
        print()
    print()


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
        event_type = detect_event_type(event)
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
            if not counts_toward_wc_standings(
                event_type,
                season_id,
                discipline=race_disc,
                category=race_cat,
            ):
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
    race_medalist_rows: list[dict[str, str]] | None = None,
    use_dynamic_all_olympic_stats: bool = False,
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

    def _fetch_dynamic_olympic_aggregate_rows(
        category: str, cutoff: datetime.datetime | None
    ) -> tuple[list[dict], list[dict]]:
        """Return (country_rows, athlete_rows) using achievements' dynamic OWG scan."""
        try:
            from . import achievements as achievements_cmd
        except Exception:
            return [], []

        try:
            season_ids, _season_label, scope_events = (
                achievements_cmd._resolve_season_selection(EVENT_TYPE_OWG, "all")
            )
            race_meta = achievements_cmd._collect_race_meta(
                season_ids, scope_events, category
            )
            if cutoff is not None:
                filtered_race_meta: list[dict] = []
                for meta in race_meta:
                    start_dt = meta.get("start_dt")
                    if isinstance(start_dt, datetime.datetime) and start_dt <= cutoff:
                        filtered_race_meta.append(meta)
                race_meta = filtered_race_meta
            payload_by_race = achievements_cmd._fetch_race_payloads(race_meta)
            country_rows, _country_races_used = (
                achievements_cmd._aggregate_achievements(
                    race_meta, payload_by_race, category, by_country=True
                )
            )
            athlete_rows, _athlete_races_used = (
                achievements_cmd._aggregate_achievements(
                    race_meta, payload_by_race, category, by_country=False
                )
            )
            return country_rows, athlete_rows
        except BiathlonError:
            return [], []
        except Exception:
            return [], []

    dynamic_country_rows: list[dict] = []
    dynamic_athlete_rows: list[dict] = []
    if use_dynamic_all_olympic_stats:
        dynamic_country_rows, dynamic_athlete_rows = (
            _fetch_dynamic_olympic_aggregate_rows(cat_id, cutoff_dt)
        )
        # If dynamic aggregates miss current-race medalists (e.g. stale/history-only
        # data), inject the race medalists so they remain visible in post-race output.
        if race_medalist_rows:
            existing_ath_ids = {
                str(row.get("ibu_id") or "").strip()
                for row in dynamic_athlete_rows
                if str(row.get("ibu_id") or "").strip()
            }
            existing_country_codes = {
                str(row.get("country") or "").strip().upper()
                for row in dynamic_country_rows
                if str(row.get("country") or "").strip()
            }
            for race_row in race_medalist_rows:
                medal = str(race_row.get("medal") or "").strip().lower()
                if medal not in {"gold", "silver", "bronze"}:
                    continue
                ibu_id = str(race_row.get("ibu_id") or "").strip()
                name = str(race_row.get("name") or "").strip()
                nat = str(race_row.get("nat") or "").strip()
                if ibu_id and ibu_id not in existing_ath_ids:
                    athlete_row = {
                        "name": name,
                        "nat": nat,
                        "gender": "F" if cat_id == "SW" else "M",
                        "ibu_id": ibu_id,
                        "races": 1,
                        "races_ind": 0 if is_relay else 1,
                        "races_relay": 1 if is_relay else 0,
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
                    athlete_row[medal] = 1
                    athlete_row[f"{medal}_{'relay' if is_relay else 'ind'}"] = 1
                    dynamic_athlete_rows.append(athlete_row)
                    existing_ath_ids.add(ibu_id)
                nat_code = nat.upper()
                if (
                    nat_code
                    and race_country_medals
                    and nat_code in race_country_medals
                    and nat_code not in existing_country_codes
                ):
                    country_medals = race_country_medals.get(nat_code) or set()
                    country_row = {
                        "country": nat_code,
                        "gold": 1 if "gold" in country_medals else 0,
                        "silver": 1 if "silver" in country_medals else 0,
                        "bronze": 1 if "bronze" in country_medals else 0,
                        "gold_ind": 1
                        if ("gold" in country_medals and not is_relay)
                        else 0,
                        "silver_ind": (
                            1 if ("silver" in country_medals and not is_relay) else 0
                        ),
                        "bronze_ind": (
                            1 if ("bronze" in country_medals and not is_relay) else 0
                        ),
                        "gold_relay": 1
                        if ("gold" in country_medals and is_relay)
                        else 0,
                        "silver_relay": (
                            1 if ("silver" in country_medals and is_relay) else 0
                        ),
                        "bronze_relay": (
                            1 if ("bronze" in country_medals and is_relay) else 0
                        ),
                    }
                    dynamic_country_rows.append(country_row)
                    existing_country_codes.add(nat_code)

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
                f"## Country medal table — {cat_name} {disc_name}: none", args
            )
        )
        print()
        print()
    else:
        sorted_countries = sorted(
            medal_counts.items(),
            key=lambda x: (x[1]["gold"], x[1]["silver"], x[1]["bronze"]),
            reverse=True,
        )
        print(
            _format_section_title(
                f"## Country medal table — {cat_name} {disc_name}", args
            )
        )
        print()
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
        print()

    # Section: Country medal table (all Olympic disciplines)
    sec += 1
    if not all_country_medals and not dynamic_country_rows:
        print(
            _format_section_title(
                "## Country Olympic Games Medal Table - All Disciplines (available editions): none",
                args,
            )
        )
        print()
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
        if dynamic_country_rows:
            for row in dynamic_country_rows:
                nat = str(row.get("country") or "")
                if not nat:
                    continue
                all_country_counts[nat] = {
                    "gold": int(row.get("gold", 0)),
                    "silver": int(row.get("silver", 0)),
                    "bronze": int(row.get("bronze", 0)),
                    "gold_ind": int(row.get("gold_ind", 0)),
                    "silver_ind": int(row.get("silver_ind", 0)),
                    "bronze_ind": int(row.get("bronze_ind", 0)),
                    "gold_relay": int(row.get("gold_relay", 0)),
                    "silver_relay": int(row.get("silver_relay", 0)),
                    "bronze_relay": int(row.get("bronze_relay", 0)),
                }
        else:
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

        if dynamic_country_rows:
            sorted_all_countries = [
                (
                    str(row.get("country") or ""),
                    all_country_counts[str(row.get("country") or "")],
                )
                for row in dynamic_country_rows
                if str(row.get("country") or "") in all_country_counts
            ]
        else:
            sorted_all_countries = sorted(
                all_country_counts.items(),
                key=lambda x: (x[1]["gold"], x[1]["silver"], x[1]["bronze"]),
                reverse=True,
            )

        print(
            _format_section_title(
                "## Country Olympic Games Medal Table - All Disciplines (available editions)",
                args,
            )
        )
        print()
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
        print()

    # Section: Athlete medal table (all Olympic disciplines)
    sec += 1
    race_medalist_ids = gold_ids | silver_ids | bronze_ids
    race_medalist_name_nat = {
        (str(row.get("name") or "").strip(), str(row.get("nat") or "").strip())
        for row in (race_medalist_rows or [])
        if str(row.get("name") or "").strip() and str(row.get("nat") or "").strip()
    }

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
    if dynamic_athlete_rows:
        all_medalists = []
        for stats in dynamic_athlete_rows:
            key = str(stats.get("ibu_id") or "").strip()
            if not key:
                key = f"{str(stats.get('name') or '').strip()}|{str(stats.get('nat') or '').strip()}"
            all_medalists.append((key, stats))
    else:
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
        if stats["gold"] >= 2
        or key in race_medalist_ids
        or (
            str(stats.get("name") or "").strip(),
            str(stats.get("nat") or "").strip(),
        )
        in race_medalist_name_nat
    ]

    if not medalists:
        print(
            _format_section_title(
                "## Athlete Olympic Games Medal Table - All Disciplines (available editions): none",
                args,
            )
        )
        print()
        print()
    else:
        print(
            _format_section_title(
                "## Athlete Olympic Games Medal Table - All Disciplines (available editions)",
                args,
            )
        )
        print()
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
                disc_group = (
                    {"IN", "SI"} if discipline in ("IN", "SI") else {discipline}
                )
                if race_disc not in disc_group or (cat_id and race_cat != cat_id):
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


def _collect_season_all_disc_race_ids(
    season_id: str,
    cat_id: str,
    target_event_type: str,
    cutoff_dt: datetime.datetime | None = None,
) -> list[str]:
    """Find all individual-discipline race_ids for a season/category/event_type."""
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
            if race_disc in RELAY_DISCIPLINES:
                continue
            if cat_id and race_cat != cat_id:
                continue
            start_dt = _start_dt_from_race_row(race)
            if cutoff_dt is not None:
                if start_dt is None or start_dt > cutoff_dt:
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
            leg_results = [r for r in all_results if not r.get("IsTeam")]
        else:
            candidates = [r for r in all_results if not r.get("IsTeam")]
            leg_results = []
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
        nat_to_medal: dict[str, str] = {}
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
                nat_to_medal[nat] = medal
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

        # Credit individual leg athletes for relay medal-winning teams
        for res in leg_results:
            nat = res.get("Nat") or ""
            medal = nat_to_medal.get(nat)
            name = res.get("Name") or res.get("ShortName") or ""
            ibu_id = str(res.get("IBUId") or "")
            key = ibu_id or name
            if not key:
                continue
            athlete_race_counts[key] = athlete_race_counts.get(key, 0) + 1
            if medal:
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
    all_results_cache: dict[str, list[dict]] | None = None,
    event_type: str = "",
) -> int:
    """Render Olympic best-performance milestones (overall + discipline)."""
    output_format = get_output_format(args)
    disc_label = DISCIPLINE_NAMES.get(discipline, discipline)
    disc_label_lc = disc_label.lower()
    if is_relay:
        all_label = "Best career team result (all discipline)"
        discipline_label = f"Best career team results ({disc_label_lc})"
        event_type_all_label = f"Best {event_type} team result (all discipline)"
        event_type_disc_label = f"Best {event_type} team results ({disc_label_lc})"
    else:
        all_label = "Best career indiv result (all discipline)"
        discipline_label = f"Best career indiv results ({disc_label_lc})"
        event_type_all_label = f"Best {event_type} indiv result (all discipline)"
        event_type_disc_label = f"Best {event_type} indiv results ({disc_label_lc})"
    show_event_type_milestones = bool(event_type) and event_type in MAJOR_LEVELS

    team_rank_by_bib: dict[str, int] = {}
    if is_relay:
        for team in team_results:
            bib = str(team.get("Bib") or "")
            rank_val = _parse_rank(team.get("Rank") or team.get("SO"))
            if bib and rank_val is not None:
                team_rank_by_bib[bib] = rank_val

    seen_ids: set[str] = set()
    shared_all_results_cache = (
        all_results_cache if all_results_cache is not None else {}
    )
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

        if ibu_id not in shared_all_results_cache:
            try:
                all_payload = get_all_results(ibu_id)
            except BiathlonError:
                all_payload = {}
            shared_all_results_cache[ibu_id] = list(all_payload.get("Results") or [])

        major_ranked: list[tuple[dict, int]] = []
        for res in shared_all_results_cache[ibu_id]:
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

        if show_event_type_milestones:
            prior_best_evt_all = min(
                (
                    rank_val
                    for res, rank_val in prior_rows_same_type
                    if str(res.get("Level") or "").upper() == event_type
                ),
                default=None,
            )
            prior_best_evt_disc = min(
                (
                    rank_val
                    for res, rank_val in prior_rows
                    if _result_discipline_id(res) == discipline
                    and str(res.get("Level") or "").upper() == event_type
                ),
                default=None,
            )
            is_best_evt_all = (
                prior_best_evt_all is None or current_rank < prior_best_evt_all
            )
            is_best_evt_disc = (
                prior_best_evt_disc is None or current_rank < prior_best_evt_disc
            )
        else:
            prior_best_evt_all = prior_best_evt_disc = None
            is_best_evt_all = is_best_evt_disc = False

        if (
            not is_best_all
            and not is_best_disc
            and not is_best_evt_all
            and not is_best_evt_disc
        ):
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
        elif is_best_evt_all:
            milestone_rows.append(
                (
                    current_rank,
                    name,
                    nat,
                    event_type_all_label,
                    _previous_best_label(
                        prior_best_evt_all, f"all discipline, {event_type}"
                    ),
                )
            )
        elif is_best_evt_disc:
            milestone_rows.append(
                (
                    current_rank,
                    name,
                    nat,
                    event_type_disc_label,
                    _previous_best_label(
                        prior_best_evt_disc, f"{disc_label_lc}, {event_type}"
                    ),
                )
            )

    type_order = {
        all_label: 0,
        discipline_label: 1,
        event_type_all_label: 2,
        event_type_disc_label: 3,
    }
    milestone_rows.sort(key=lambda row: (row[0], row[1], type_order.get(row[3], 99)))

    sec += 1
    if not milestone_rows:
        print(_format_section_title("## Best Performances: none", args))
        print()
        print()
        return sec

    print(_format_section_title("## Best Performances", args))
    print()
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
        column_separators=_best_performances_column_separators(),
    )
    print()
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
    discipline = _normalize_discipline_id(str(comp.get("DisciplineId") or ""))
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
    best_u23_leader = {"id": "", "name": "", "nat": ""}
    cat_id = str(comp.get("catId") or comp.get("CatId") or "").upper()
    season_id = str(sport_evt.get("SeasonId") or "") or _season_id_from_race_id(race_id)
    if is_wc_race and not is_relay and _is_individual_like_discipline(discipline):
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

    age_display_by_id: dict[str, str] = {}
    u23_ids: set[str] = set()
    reference_date = (
        target_start_dt.date()
        if target_start_dt is not None
        else datetime.datetime.now(datetime.timezone.utc).date()
    )
    age_ibu_ids = {str(entry.get("ibu_id") or "") for entry in entries}
    if is_wc_race:
        age_ibu_ids.update(_row_ibu_id(row) for row in total_rows)
        age_ibu_ids.update(_row_ibu_id(row) for row in disc_rows)
    season_end_yr = _season_end_year(season_id) if season_id else None
    u23_cutoff_year = (season_end_yr - 23) if season_end_yr is not None else None
    age_display_by_id, u23_ids = _build_athlete_age_map(
        age_ibu_ids, reference_date, u23_cutoff_year=u23_cutoff_year
    )
    best_u23_leader = (
        _find_best_u23_leader(total_rows or disc_rows, u23_ids) if is_wc_race else {}
    )
    best_u23_total_leader = (
        _find_best_u23_leader(total_rows, u23_ids) if is_wc_race else {}
    )
    best_u23_disc_leader = (
        _find_best_u23_leader(disc_rows, u23_ids) if is_wc_race else {}
    )

    mark_leaders = pretty
    decorate_any = _make_leader_name_decorator(
        general_leader,
        discipline_leader,
        best_u23_leader,
        mark_leaders,
        "any",
    )
    decorate_total = _make_leader_name_decorator(
        general_leader,
        discipline_leader,
        best_u23_total_leader,
        mark_leaders,
        "any",
    )
    decorate_disc = _make_leader_name_decorator(
        general_leader,
        discipline_leader,
        best_u23_disc_leader,
        mark_leaders,
        "any",
    )
    name_formatter_plain = _make_name_formatter()
    name_nat_to_id = {
        (entry["name"], entry["nat"]): entry["ibu_id"]
        for entry in entries
        if entry.get("ibu_id")
    }
    participating_ids = {entry["ibu_id"] for entry in entries if entry.get("ibu_id")}

    def _age_for_entry(name: str, nat: str, ibu_id: str = "") -> str:
        if not age_display_by_id:
            return "-"
        resolved_id = ibu_id
        if not resolved_id:
            base_name = _base_name_without_markers(name)
            resolved_id = name_nat_to_id.get((base_name, nat), "")
        if not resolved_id:
            return "-"
        return age_display_by_id.get(resolved_id, "-")

    print()
    print(_format_section_title(format_race_header(payload, race_id), args))
    print()

    sec = 0

    sec += 1
    if flower_entries:
        if is_wc_race:
            if is_relay:
                headers = ["Rank", "Team", "Nat", "Points"]
            else:
                headers = ["Rank", "Athlete", "Age", "Nat", "Points"]
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
            ]
            if is_wc_race and not is_relay:
                row.append(_age_for_entry(entry["name"], entry["nat"], entry["ibu_id"]))
            row.append(entry["nat"])
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
        cell_fmts: list[Callable[[str, int], str] | None] = [
            rank_formatter,
            name_formatter,
        ]
        if is_wc_race and not is_relay:
            cell_fmts.append(None)
        cell_fmts.append(nat_formatter)
        if is_wc_race:
            cell_fmts.append(_format_race_points_cell)
        print(_format_section_title("## Results", args))
        print()
        render_table(
            headers,
            results_rows,
            output_format=output_format,
            cell_formatters=cell_fmts,
            column_separators=_results_column_separators(headers),
        )
        print()
        print()
    else:
        print(_format_section_title("## Results: none", args))
        print()
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
                    decorate_total,
                    participating_ids=participating_ids,
                    age_display_by_id=age_display_by_id,
                )
                total_u23_cup_rows = [
                    row for row in total_rows if _is_u23_standings_row(row, u23_ids)
                ]
                total_u23_standings_rows: list[list[str]] = []
                total_u23_row_styles: list[str] = []
                if total_u23_cup_rows:
                    total_u23_standings_rows, total_u23_row_styles = (
                        _build_standings_rows(
                            total_u23_cup_rows,
                            STANDINGS_TOP_N,
                            race_points_by_id,
                            decorate_total,
                            participating_ids=participating_ids,
                            age_display_by_id=age_display_by_id,
                            u23_mode=True,
                        )
                    )
                total_name_formatter = _make_name_formatter(total_row_styles)
                total_u23_name_formatter = _make_name_formatter(total_u23_row_styles)
                _render_wc_standings_table_pair(
                    "## WC standings (Total)",
                    args,
                    output_format,
                    pretty,
                    total_standings_rows,
                    total_row_styles,
                    total_name_formatter,
                    total_u23_standings_rows,
                    total_u23_row_styles,
                    total_u23_name_formatter,
                )
            else:
                print(
                    _format_section_title(
                        "## WC standings (Total): no data",
                        args,
                    )
                )
                print()
                print()

            sec += 1
            if disc_rows:
                disc_standings_rows, disc_row_styles = _build_standings_rows(
                    disc_rows,
                    STANDINGS_TOP_N,
                    race_points_by_id,
                    decorate_disc,
                    participating_ids=participating_ids,
                    age_display_by_id=age_display_by_id,
                )
                disc_u23_cup_rows = [
                    row for row in disc_rows if _is_u23_standings_row(row, u23_ids)
                ]
                disc_u23_standings_rows: list[list[str]] = []
                disc_u23_row_styles: list[str] = []
                if disc_u23_cup_rows:
                    disc_u23_standings_rows, disc_u23_row_styles = (
                        _build_standings_rows(
                            disc_u23_cup_rows,
                            STANDINGS_TOP_N,
                            race_points_by_id,
                            decorate_disc,
                            participating_ids=participating_ids,
                            age_display_by_id=age_display_by_id,
                            u23_mode=True,
                        )
                    )
                disc_name_formatter = _make_name_formatter(disc_row_styles)
                disc_u23_name_formatter = _make_name_formatter(disc_u23_row_styles)
                _render_wc_standings_table_pair(
                    f"## WC standings ({disc_label})",
                    args,
                    output_format,
                    pretty,
                    disc_standings_rows,
                    disc_row_styles,
                    disc_name_formatter,
                    disc_u23_standings_rows,
                    disc_u23_row_styles,
                    disc_u23_name_formatter,
                )
            else:
                print(
                    _format_section_title(
                        f"## WC standings ({disc_label}): no data",
                        args,
                    )
                )
                print()
                print()
        else:
            sec += 1
            print(_format_section_title("## WC standings: no data", args))
            print()
            print()

    # Nations Cup standings (WC + WCH, non-PU/MS disciplines)
    if (
        event_type in (EVENT_TYPE_WC, EVENT_TYPE_WCH)
        and discipline not in ("PU", "MS")
        and season_id
    ):
        mixed_like = discipline in ("SR", "MR")
        target_cats = (
            ["SW", "SM"] if mixed_like else ([cat_id] if cat_id in {"SW", "SM"} else [])
        )
        # Nations Cup points earned in this race by nation
        nc_source = team_results if is_relay else leg_results
        nc_race_by_nat: dict[str, float] = {}
        for _r in nc_source:
            _nat = str(_r.get("Nat") or "")
            _nc_raw = _r.get("NC")
            if _nat and _nc_raw is not None:
                try:
                    nc_race_by_nat[_nat] = nc_race_by_nat.get(_nat, 0.0) + float(
                        _nc_raw
                    )
                except (TypeError, ValueError):
                    pass

        _max_nc_race = max(nc_race_by_nat.values(), default=0.0)

        def _fmt_nc_race_pts(cell_str: str, _row_idx: int) -> str:
            text = str(cell_str or "").strip()
            if not text:
                return text
            try:
                val = float(text.lstrip("+"))
            except ValueError:
                return text
            intensity = (val / _max_nc_race * 0.7 + 0.3) if _max_nc_race > 0 else 0.5
            return Color.green(text, intensity=intensity)

        def _fmt_nc_change(cell_str: str, _row_idx: int) -> str:
            text = str(cell_str or "").strip()
            if text.startswith("+") and text[1:].isdigit():
                return Color.green(text, intensity=min(1.0, 0.5 + 0.15 * int(text[1:])))
            if text.startswith("-") and text[1:].isdigit():
                return Color.red(text, intensity=min(1.0, 0.5 + 0.15 * int(text[1:])))
            return text

        for tc in target_cats:
            nation_rows = _fetch_nations_cup_standings(season_id, tc, limit=10)
            sec += 1
            cat_name_nc = CATEGORY_DISPLAY_NAMES.get(tc, tc)
            if nation_rows:
                print(
                    _format_section_title(
                        f"## Nations Cup standings ({cat_name_nc})", args
                    )
                )
                print()
                table_rows = _build_nations_cup_table_rows(nation_rows, nc_race_by_nat)
                render_table(
                    ["Rank", "Country", "Total Pts", "Race Pts", "Changes"],
                    table_rows,
                    output_format=output_format,
                    column_separators={2},
                    cell_formatters=[
                        None,
                        None,
                        _standings_points_cell_formatter(
                            set(),
                            leader_rows={0},
                            point_cells=[row[2] for row in table_rows],
                            pretty=pretty,
                        ),
                        _fmt_nc_race_pts,
                        _fmt_nc_change,
                    ],
                )
                print()
                print()
            else:
                print(
                    _format_section_title(
                        f"## Nations Cup standings ({cat_name_nc}): no data", args
                    )
                )
                print()
                print()

    # Relay WC standings (WC only, relay disciplines)
    if is_wc_race and is_relay and season_id:
        relay_wc_label, relay_wc_rows = _fetch_relay_wc_standings(
            season_id, cat_id, discipline, limit=10
        )
        # Points earned in this race by nation
        wc_race_by_nat: dict[str, float] = {}
        for _r in team_results:
            _nat = str(_r.get("Nat") or "")
            _wc_raw = _r.get("WC")
            if _nat and _wc_raw is not None:
                try:
                    wc_race_by_nat[_nat] = wc_race_by_nat.get(_nat, 0.0) + float(
                        _wc_raw
                    )
                except (TypeError, ValueError):
                    pass
        _max_relay_wc_race = max(wc_race_by_nat.values(), default=0.0)

        def _fmt_relay_wc_race_pts(cell_str: str, _row_idx: int) -> str:
            text = str(cell_str or "").strip()
            if not text:
                return text
            try:
                val = float(text.lstrip("+"))
            except ValueError:
                return text
            intensity = (
                (val / _max_relay_wc_race * 0.7 + 0.3)
                if _max_relay_wc_race > 0
                else 0.5
            )
            return Color.green(text, intensity=intensity)

        def _fmt_relay_wc_change(cell_str: str, _row_idx: int) -> str:
            text = str(cell_str or "").strip()
            if text.startswith("+") and text[1:].isdigit():
                return Color.green(text, intensity=min(1.0, 0.5 + 0.15 * int(text[1:])))
            if text.startswith("-") and text[1:].isdigit():
                return Color.red(text, intensity=min(1.0, 0.5 + 0.15 * int(text[1:])))
            return text

        sec += 1
        if relay_wc_rows:
            print(
                _format_section_title(f"## Relay WC standings ({relay_wc_label})", args)
            )
            print()
            table_rows = []
            for row_idx, relay_row in enumerate(relay_wc_rows):
                rank = str(
                    relay_row.get("Rank") or relay_row.get("Standing") or row_idx + 1
                ).rstrip(".")
                name = str(relay_row.get("Name") or relay_row.get("ShortName") or "")
                nat = str(relay_row.get("Nat") or "")
                points = str(relay_row.get("Score") or relay_row.get("Points") or "0")
                _, change = _extract_rank_and_change(relay_row)
                race_wc = wc_race_by_nat.get(nat)
                race_pts = f"+{int(race_wc)}" if race_wc else ""
                table_rows.append([rank, name, nat, points, race_pts, change])
            render_table(
                ["Rank", "Team", "Nat", "Points", "Race", "Changes"],
                table_rows,
                output_format=output_format,
                column_separators={3},
                cell_formatters=[
                    None,
                    None,
                    None,
                    None,
                    _fmt_relay_wc_race_pts,
                    _fmt_relay_wc_change,
                ],
            )
            print()
            print()
        else:
            print(
                _format_section_title(
                    f"## Relay WC standings ({relay_wc_label}): no data", args
                )
            )
            print()
            print()

    all_results_cache = _prefetch_all_results(
        {str(entry.get("ibu_id") or "") for entry in entries}
    )

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
        all_results_cache,
        event_type,
    )

    # Milestone parameters (startlist-style)
    event_level_set: set[str] = {event_type} if event_type in MAJOR_LEVELS else {"WC"}
    major_level_set: set[str] = MAJOR_LEVELS
    event_race_step = 25 if event_type == EVENT_TYPE_WC else 5
    career_race_step = 25
    current_event_label = EVENT_TYPE_LABELS.get(event_type, event_type)
    career_event_label = "WC+WCH+OWG"
    disc_label = DISCIPLINE_NAMES.get(discipline, discipline)
    class_label = "Team Race" if is_relay else "Indiv Race"

    def _milestone(current: int, step: int, include_first: bool = True) -> int | None:
        if current <= 0:
            return None
        if include_first and current == 1:
            return 1
        if step > 0 and current % step == 0:
            return current
        return None

    def _detail_counts(
        rows_in: list[dict],
    ) -> tuple[int, int, int, int, int, int, int, int, int]:
        """Return (wins, ind_wins, team_wins, podiums, ind_podiums, team_podiums,
        flowers, ind_flowers, team_flowers) from career result rows."""
        wins = ind_wins = team_wins = 0
        podiums = ind_podiums = team_podiums = 0
        flowers = ind_flowers = team_flowers = 0
        for res in rows_in:
            rank = _parse_rank(res.get("Rank") or res.get("SO"))
            if rank is None:
                continue
            res_disc = str(res.get("Comp") or "").upper()
            is_team_res = res_disc in RELAY_DISCIPLINES
            if rank == 1:
                wins += 1
                if is_team_res:
                    team_wins += 1
                else:
                    ind_wins += 1
            if rank <= 3:
                podiums += 1
                if is_team_res:
                    team_podiums += 1
                else:
                    ind_podiums += 1
            if rank <= 6:
                flowers += 1
                if is_team_res:
                    team_flowers += 1
                else:
                    ind_flowers += 1
        return (
            wins,
            ind_wins,
            team_wins,
            podiums,
            ind_podiums,
            team_podiums,
            flowers,
            ind_flowers,
            team_flowers,
        )

    race_rows: list[list] = []
    win_rows: list[list] = []
    processed_milestone_ids: set[str] = set()

    for entry in entries:
        ibu_id = entry["ibu_id"]
        if not ibu_id:
            continue
        # DNF/DNS/lapped filter
        if is_relay:
            entry_rank = None
            for team in team_results:
                if str(team.get("Bib") or "") == entry["bib"]:
                    entry_rank = _parse_rank(team.get("Rank"))
                    break
        else:
            entry_rank = _parse_rank(entry["rank"])
        if entry_rank is None:
            continue  # DNF, DNS, lapped — milestone not reached
        if ibu_id in processed_milestone_ids:
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
        if race_level not in event_level_set:
            continue
        all_results_filtered = _filter_results_to_snapshot(
            all_results,
            race_id,
            target_start_dt,
            history_race_start_cache,
            chronology_warning_keys,
            f"milestones for {ibu_id}",
        )
        event_results = [
            r
            for r in all_results_filtered
            if str(r.get("Level") or "").upper() in event_level_set
        ]
        major_results = [
            r
            for r in all_results_filtered
            if str(r.get("Level") or "").upper() in major_level_set
        ]
        if race_row not in event_results:
            event_results.append(race_row)
        if race_row not in major_results:
            major_results.append(race_row)

        def _counts(
            rows_in: list[dict],
        ) -> tuple[int, int, int, int, int, int, int, int]:
            all_races = len(rows_in)
            all_wins = ind_races = ind_wins = team_races = team_wins = 0
            disc_races = disc_wins = 0
            for res in rows_in:
                rank = _parse_rank(res.get("Rank") or res.get("SO"))
                res_disc = str(res.get("Comp") or "").upper()
                if rank == 1:
                    all_wins += 1
                is_team = res_disc in RELAY_DISCIPLINES
                if is_team:
                    team_races += 1
                    if rank == 1:
                        team_wins += 1
                else:
                    ind_races += 1
                    if rank == 1:
                        ind_wins += 1
                if res_disc == discipline:
                    disc_races += 1
                    if rank == 1:
                        disc_wins += 1
            return (
                all_races,
                all_wins,
                ind_races,
                ind_wins,
                team_races,
                team_wins,
                disc_races,
                disc_wins,
            )

        (e_all, e_win, e_ind, e_ind_win, e_team, e_team_win, e_disc, e_disc_win) = (
            _counts(event_results)
        )
        (c_all, c_win, c_ind, c_ind_win, c_team, c_team_win, c_disc, c_disc_win) = (
            _counts(major_results)
        )

        name = decorate_any(entry["name"], entry["nat"], ibu_id)
        age = _age_for_entry(entry["name"], entry["nat"], ibu_id)
        nat = entry["nat"]

        e_class_races = e_team if is_relay else e_ind
        c_class_races = c_team if is_relay else c_ind

        race_scopes = [
            (
                current_event_label,
                disc_label,
                e_disc,
                event_race_step,
                True,
                "event_discipline",
            ),
            (
                career_event_label,
                disc_label,
                c_disc,
                career_race_step,
                True,
                "career_discipline",
            ),
            (
                current_event_label,
                class_label,
                e_class_races,
                event_race_step,
                True,
                "event_class",
            ),
            (current_event_label, "Race", e_all, event_race_step, True, "event_all"),
            (
                career_event_label,
                class_label,
                c_class_races,
                career_race_step,
                True,
                "career_class",
            ),
            (career_event_label, "Race", c_all, career_race_step, True, "career_all"),
        ]
        for (
            scope_event_label,
            type_label,
            current,
            step,
            include_first,
            scope_id,
        ) in race_scopes:
            hit = _milestone(current, step, include_first=include_first)
            if hit is None:
                continue
            race_rows.append([hit, scope_event_label, type_label, name, age, nat])

        if entry_rank <= 3:
            (
                e_dc_wins,
                e_dc_ind_wins,
                e_dc_team_wins,
                e_dc_podiums,
                e_dc_ind_podiums,
                e_dc_team_podiums,
                e_dc_flowers,
                e_dc_ind_flowers,
                e_dc_team_flowers,
            ) = _detail_counts(event_results)
            (
                c_dc_wins,
                c_dc_ind_wins,
                c_dc_team_wins,
                c_dc_podiums,
                c_dc_ind_podiums,
                c_dc_team_podiums,
                c_dc_flowers,
                c_dc_ind_flowers,
                c_dc_team_flowers,
            ) = _detail_counts(major_results)
            if entry_rank == 1:
                if is_relay:
                    event_detail = [
                        (e_dc_team_wins, "Relay Win"),
                        (e_dc_wins, "Win"),
                        (e_dc_team_podiums, "Relay Podium"),
                        (e_dc_podiums, "Podium"),
                        (e_dc_team_flowers, "Relay Flower"),
                        (e_dc_flowers, "Flower"),
                    ]
                    career_detail = [
                        (c_dc_team_wins, "Relay Win"),
                        (c_dc_wins, "Win"),
                        (c_dc_team_podiums, "Relay Podium"),
                        (c_dc_podiums, "Podium"),
                        (c_dc_team_flowers, "Relay Flower"),
                        (c_dc_flowers, "Flower"),
                    ]
                else:
                    event_detail = [
                        (e_dc_ind_wins, "Indiv Win"),
                        (e_dc_wins, "Win"),
                        (e_dc_ind_podiums, "Indiv Podium"),
                        (e_dc_podiums, "Podium"),
                        (e_dc_ind_flowers, "Indiv Flower"),
                        (e_dc_flowers, "Flower"),
                    ]
                    career_detail = [
                        (c_dc_ind_wins, "Indiv Win"),
                        (c_dc_wins, "Win"),
                        (c_dc_ind_podiums, "Indiv Podium"),
                        (c_dc_podiums, "Podium"),
                        (c_dc_ind_flowers, "Indiv Flower"),
                        (c_dc_flowers, "Flower"),
                    ]
            else:
                if is_relay:
                    event_detail = [
                        (e_dc_team_podiums, "Relay Podium"),
                        (e_dc_podiums, "Podium"),
                        (e_dc_team_flowers, "Relay Flower"),
                        (e_dc_flowers, "Flower"),
                    ]
                    career_detail = [
                        (c_dc_team_podiums, "Relay Podium"),
                        (c_dc_podiums, "Podium"),
                        (c_dc_team_flowers, "Relay Flower"),
                        (c_dc_flowers, "Flower"),
                    ]
                else:
                    event_detail = [
                        (e_dc_ind_podiums, "Indiv Podium"),
                        (e_dc_podiums, "Podium"),
                        (e_dc_ind_flowers, "Indiv Flower"),
                        (e_dc_flowers, "Flower"),
                    ]
                    career_detail = [
                        (c_dc_ind_podiums, "Indiv Podium"),
                        (c_dc_podiums, "Podium"),
                        (c_dc_ind_flowers, "Indiv Flower"),
                        (c_dc_flowers, "Flower"),
                    ]
            for count, type_label in event_detail:
                win_rows.append(
                    [count, current_event_label, type_label, name, age, nat]
                )
            for count, type_label in career_detail:
                win_rows.append([count, career_event_label, type_label, name, age, nat])

        processed_milestone_ids.add(ibu_id)

    def _dedupe_milestone_rows(sub_rows: list[list]) -> list[list]:
        def _type_breadth(t: str) -> int:
            if t in {"Race"}:
                return 3
            if t in {"Indiv Race", "Team Race"}:
                return 2
            return 1

        deduped: dict[tuple, list] = {}
        for row in sub_rows:
            key = (str(row[1]), str(row[3]), str(row[5]), int(row[0]))
            existing = deduped.get(key)
            if existing is None:
                deduped[key] = row
            elif _type_breadth(str(row[2])) > _type_breadth(str(existing[2])):
                deduped[key] = row
        return list(deduped.values())

    def _sort_milestone_rows(sub_rows: list[list]) -> list[list]:
        athlete_max: dict[str, int] = {}
        for row in sub_rows:
            athlete = str(row[3])
            v = int(row[0])
            if v > athlete_max.get(athlete, 0):
                athlete_max[athlete] = v
        sub_rows.sort(
            key=lambda row: (
                -athlete_max.get(str(row[3]), 0),
                str(row[3]),
                -int(row[0]),
                str(row[1]),
                str(row[2]),
            )
        )
        return sub_rows

    milestone_headers = ["Milestone", "Event", "Type", "Athlete", "Age", "Nat"]

    # Race milestones — startlist-style, split into Current Event / Career subsections
    sec += 1
    if not race_rows:
        print(_format_section_title("## Race milestones: none", args))
        print()
        print()
    else:
        print(_format_section_title("## Race milestones", args))
        print()
        for subsection_title, subsection_rows in [
            (
                current_event_label,
                [row for row in race_rows if str(row[1]) == current_event_label],
            ),
            ("Career", [row for row in race_rows if str(row[1]) == career_event_label]),
        ]:
            if not subsection_rows:
                continue
            deduped = _dedupe_milestone_rows(subsection_rows)
            sorted_rows = _sort_milestone_rows(deduped)
            display_rows = [[_ordinal(int(row[0])), *row[1:]] for row in sorted_rows]

            # Blue gradient: skip 1st, scale by event scope
            def _race_row_color(
                count: int, evt_lbl: str
            ) -> tuple[int, int, int] | None:
                if count <= 1:
                    return None
                if evt_lbl == career_event_label:
                    t = min(1.0, max(0.0, count / (career_race_step * 8)))
                else:
                    t = min(1.0, max(0.0, count / (event_race_step * 10)))
                return (int(50 + 30 * t), int(80 + 120 * t), int(160 + 95 * t))

            race_row_colors = [
                _race_row_color(int(row[0]), str(row[1])) for row in sorted_rows
            ]

            def _make_race_fmt(
                colors: list, base_fmt: Callable[[str, int], str] | None = None
            ) -> Callable[[str, int], str]:
                def _fmt(cell_str: str, row_idx: int) -> str:
                    base = base_fmt(cell_str, row_idx) if base_fmt else cell_str
                    color = colors[row_idx] if row_idx < len(colors) else None
                    return (
                        Color.rgb(base, color, bold=False)
                        if color is not None
                        else base
                    )

                return _fmt

            race_cell_formatters: list[Callable[[str, int], str] | None] = [
                _make_race_fmt(race_row_colors),
                _make_race_fmt(race_row_colors),
                _make_race_fmt(race_row_colors),
                _make_race_fmt(race_row_colors, name_formatter_plain),
                _make_race_fmt(race_row_colors),
                _make_race_fmt(race_row_colors),
            ]

            print(_format_section_title(f"### {subsection_title}", args))
            print()
            render_table(
                milestone_headers,
                display_rows,
                output_format=output_format,
                cell_formatters=race_cell_formatters,
                column_separators=_milestone_subsection_column_separators(),
            )
            print()
            print()

    # Win milestones — per-athlete detail rows, split into Current Event / Career
    sec += 1
    if not win_rows:
        print(_format_section_title("## Win milestones: none", args))
        print()
        print()
    else:
        print(_format_section_title("## Win milestones", args))
        print()
        for subsection_title, subsection_rows in [
            (
                current_event_label,
                [row for row in win_rows if str(row[1]) == current_event_label],
            ),
            ("Career", [row for row in win_rows if str(row[1]) == career_event_label]),
        ]:
            if not subsection_rows:
                continue
            row_separators = _milestone_athlete_row_separators(subsection_rows)
            display_rows = [
                [_ordinal(int(row[0])), *row[1:]] for row in subsection_rows
            ]

            def _win_style(count: int, type_label: str) -> str:
                if (
                    "Win" in type_label
                    and "Podium" not in type_label
                    and "Flower" not in type_label
                ):
                    cat = "win"
                elif "Podium" in type_label:
                    cat = "podium"
                else:
                    cat = "flower"
                if count == 1:
                    return f"{cat}_1st"
                if count % 5 == 0:
                    return f"{cat}_mult5"
                return ""

            _style_rgb: dict[str, tuple[int, int, int]] = {
                "win_1st": (50, 220, 80),
                "podium_1st": (0, 160, 80),
                "flower_1st": (100, 190, 120),
                "win_mult5": (40, 150, 255),
                "podium_mult5": (70, 120, 210),
                "flower_mult5": (110, 160, 210),
            }
            win_row_styles = [
                _win_style(int(row[0]), str(row[2])) for row in subsection_rows
            ]

            def _win_name_formatter(cell_str: str, row_idx: int) -> str:
                base = name_formatter_plain(cell_str, row_idx)
                color = _style_rgb.get(
                    win_row_styles[row_idx] if row_idx < len(win_row_styles) else ""
                )
                return Color.rgb(base, color, bold=False) if color else base

            win_cell_formatters: list[Callable[[str, int], str] | None] = [
                None,
                None,
                None,
                _win_name_formatter,
                None,
                None,
            ]
            print(_format_section_title(f"### {subsection_title}", args))
            print()
            render_table(
                milestone_headers,
                display_rows,
                output_format=output_format,
                cell_formatters=win_cell_formatters,
                row_separators=row_separators,
                row_styles=win_row_styles,
                column_separators=_milestone_subsection_column_separators(),
            )
            print()
            print()

    # Build rank-specific ID sets and country medal map for styling
    gold_ids: set[str] = set()
    silver_ids: set[str] = set()
    bronze_ids: set[str] = set()
    race_country_medals: dict[str, set[str]] = {}
    race_athlete_medals: dict[str, set[str]] = {}
    race_medalist_rows: list[dict[str, str]] = []
    race_medalist_name_nat: set[tuple[str, str]] = set()
    for entry in flower_entries:
        rank_val = entry["rank"]
        medal = MEDAL_RANK_MAP.get(rank_val)
        if not medal:
            continue
        race_medalist_rows.append(
            {
                "medal": medal,
                "name": str(entry.get("name") or ""),
                "nat": str(entry.get("nat") or ""),
                "ibu_id": str(entry.get("ibu_id") or ""),
            }
        )
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
    medal_season_id = season_id
    medal_cat_id = cat_id

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
        disc_name = (
            "Individual"
            if discipline in ("IN", "SI")
            else DISCIPLINE_NAMES.get(discipline, discipline)
        )
        cat_name = CATEGORY_DISPLAY_NAMES.get(medal_cat_id, medal_cat_id)

        # Country medal table — WCH only (OWG shown in Olympic medal sections, WC removed)
        if event_type == EVENT_TYPE_WCH:
            sec += 1
            if sorted_countries:
                print(
                    _format_section_title(
                        f"## {medal_scope} medal table by country"
                        f" — {cat_name} {disc_name}"
                        f" ({medal_races_used} races)",
                        args,
                    )
                )
                print()
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
                print()
            else:
                print(
                    _format_section_title(
                        f"## {medal_scope} medal table by country"
                        f" — {cat_name} {disc_name}: none",
                        args,
                    )
                )
                print()
                print()

        # Athlete medal table
        race_medalist_ids = gold_ids | silver_ids | bronze_ids
        ranked_athletes = [
            (rank, stats)
            for rank, stats in enumerate(sorted_athletes, 1)
            if stats["gold"] + stats["silver"] + stats["bronze"] >= 1
            or (stats.get("ibu_id") and str(stats.get("ibu_id")) in race_medalist_ids)
            or (
                str(stats.get("name") or ""),
                str(stats.get("nat") or ""),
            )
            in race_medalist_name_nat
        ]
        if ranked_athletes:
            if is_wc_race:
                extra_age_ids = {
                    str(stats.get("ibu_id") or "")
                    for _rank, stats in ranked_athletes
                    if str(stats.get("ibu_id") or "")
                    and str(stats.get("ibu_id") or "") not in age_display_by_id
                }
                if extra_age_ids:
                    extra_age_map, extra_u23_ids = _build_athlete_age_map(
                        extra_age_ids,
                        reference_date,
                        u23_cutoff_year=u23_cutoff_year,
                    )
                    age_display_by_id.update(extra_age_map)
                    u23_ids.update(extra_u23_ids)
            sec += 1
            if event_type == EVENT_TYPE_OWG:
                _ath_title = (
                    f"## Athlete {medal_scope} Medal Table"
                    f" - {disc_name} (available editions)"
                )
            else:
                _ath_title = (
                    f"## {medal_scope} medal table by athlete"
                    f" — {cat_name} {disc_name}"
                    f" ({medal_races_used} races)"
                )
            print(_format_section_title(_ath_title, args))
            print()
            medal_gender = "F" if medal_cat_id == "SW" else "M"
            ath_rows = []
            ath_row_styles = []
            ath_keys = []
            for r_rank, stats in ranked_athletes:
                total = stats["gold"] + stats["silver"] + stats["bronze"]
                ibu_id = stats["ibu_id"]
                row = [
                    str(r_rank),
                    stats["name"],
                    stats["nat"],
                    medal_gender,
                    str(stats["gold"]),
                    str(stats["silver"]),
                    str(stats["bronze"]),
                    str(total),
                    str(stats.get("races", 0)),
                ]
                if is_wc_race:
                    row.insert(2, _age_for_entry(stats["name"], stats["nat"], ibu_id))
                ath_rows.append(row)
                if ibu_id and ibu_id in gold_ids:
                    ath_row_styles.append("gold")
                elif ibu_id and ibu_id in silver_ids:
                    ath_row_styles.append("silver")
                elif ibu_id and ibu_id in bronze_ids:
                    ath_row_styles.append("bronze")
                else:
                    ath_row_styles.append("")
                ath_keys.append(ibu_id)
            ath_name_fmt = _make_medal_cell_formatter(
                ath_row_styles, race_athlete_medals, ath_keys
            )
            headers = [
                "#",
                "Athlete",
                "Nat",
                "Gender",
                Color.gold("Gold"),
                Color.silver("Silver"),
                Color.bronze("Bronze"),
                "Total",
                "Races",
            ]
            if is_wc_race:
                headers.insert(2, "Age")
            column_separators = {5} if is_wc_race else {4}
            cell_formatters = [None, ath_name_fmt]
            if is_wc_race:
                cell_formatters.append(None)
            cell_formatters.extend(
                [
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                ]
            )
            render_table(
                headers,
                ath_rows,
                output_format=output_format,
                row_styles=ath_row_styles,
                column_separators=column_separators,
                cell_formatters=cell_formatters,
            )
            print()
            print()
        else:
            sec += 1
            if event_type == EVENT_TYPE_OWG:
                _ath_none_title = (
                    f"## Athlete {medal_scope} Medal Table"
                    f" - {disc_name} (available editions): none"
                )
            else:
                _ath_none_title = (
                    f"## {medal_scope} medal table by athlete"
                    f" — {cat_name} {disc_name}: none"
                )
            print(_format_section_title(_ath_none_title, args))
            print()
            print()

    # Season medal table by athlete (all disciplines, WC only)
    if event_type == EVENT_TYPE_WC and season_id and cat_id in ("SW", "SM"):
        all_disc_race_ids = _collect_season_all_disc_race_ids(
            season_id,
            cat_id,
            event_type,
            cutoff_dt=target_start_dt,
        )
        sec += 1
        cat_name_all = CATEGORY_DISPLAY_NAMES.get(cat_id, cat_id)
        if all_disc_race_ids:
            _, all_disc_athletes, all_disc_races_used = _build_discipline_medal_counts(
                all_disc_race_ids, is_relay=False
            )
            ranked_all = [
                (r, s)
                for r, s in enumerate(all_disc_athletes, 1)
                if s["gold"] + s["silver"] + s["bronze"] >= 1
            ]
            if ranked_all:
                print(
                    _format_section_title(
                        f"## Season medal table by athlete"
                        f" — {cat_name_all} ({all_disc_races_used} races)",
                        args,
                    )
                )
                print()
                all_ath_rows = []
                all_ath_styles = []
                all_ath_keys = []
                for r_rank, stats in ranked_all:
                    ibu_id = stats["ibu_id"]
                    total = stats["gold"] + stats["silver"] + stats["bronze"]
                    row = [
                        str(r_rank),
                        stats["name"],
                        stats["nat"],
                        str(stats["gold"]),
                        str(stats["silver"]),
                        str(stats["bronze"]),
                        str(total),
                    ]
                    all_ath_rows.append(row)
                    if ibu_id and ibu_id in gold_ids:
                        all_ath_styles.append("gold")
                    elif ibu_id and ibu_id in silver_ids:
                        all_ath_styles.append("silver")
                    elif ibu_id and ibu_id in bronze_ids:
                        all_ath_styles.append("bronze")
                    else:
                        all_ath_styles.append("")
                    all_ath_keys.append(ibu_id)
                all_name_fmt = _make_medal_cell_formatter(
                    all_ath_styles, race_athlete_medals, all_ath_keys
                )
                render_table(
                    [
                        "#",
                        "Athlete",
                        "Nat",
                        Color.gold("Gold"),
                        Color.silver("Silver"),
                        Color.bronze("Bronze"),
                        "Total",
                    ],
                    all_ath_rows,
                    output_format=output_format,
                    row_styles=all_ath_styles,
                    column_separators={3},
                    cell_formatters=[None, all_name_fmt, None, None, None, None, None],
                )
                print()
                print()
            else:
                print(
                    _format_section_title(
                        f"## Season medal table by athlete — {cat_name_all}: none",
                        args,
                    )
                )
                print()
                print()
        else:
            print(
                _format_section_title(
                    f"## Season medal table by athlete — {cat_name_all}: none",
                    args,
                )
            )
            print()
            print()

    # WCH all-discipline medal tables
    if event_type == EVENT_TYPE_WCH:
        wch_country, wch_athletes = _get_all_wch_medals(
            medal_cat_id, cutoff_dt=target_start_dt
        )
        sec += 1
        _render_country_all_medal_table(
            "country_wch_medals_all_disciplines",
            wch_country,
            args,
            title_override="## WCH medal table by country (all disciplines)",
        )
        sec += 1
        _render_athlete_all_medal_table(
            "athlete_wch_medals_all_disciplines",
            wch_athletes,
            participating_ids,
            args,
            title_override="## WCH medal table by athlete (all disciplines)",
        )

    # Olympic medal tables
    if event_type == EVENT_TYPE_OWG:
        sec = _render_olympic_medal_sections(
            args,
            sec,
            discipline,
            cat_id,
            is_relay,
            participating_ids,
            gold_ids,
            silver_ids,
            bronze_ids,
            cutoff_dt=target_start_dt,
            race_country_medals=race_country_medals,
            race_athlete_medals=race_athlete_medals,
            race_medalist_rows=race_medalist_rows,
            use_dynamic_all_olympic_stats=True,
        )

    reference_date = (
        target_start_dt.date()
        if target_start_dt is not None
        else datetime.datetime.now(datetime.timezone.utc).date()
    )

    # Section: Most Decorated Athletes at venue
    venue_name = _extract_venue_name(payload)
    decorated_gender_filter = "F" if cat_id == "SW" else "M" if cat_id == "SM" else None
    if venue_name:
        before_rows, _before_styles = _build_venue_decorated_athlete_rows(
            venue_name,
            reference_date=reference_date,
            exclude_race_ids={race_id} if race_id else None,
            limit=0,
        )
        after_rows, after_styles = _build_venue_decorated_athlete_rows(
            venue_name,
            reference_date=reference_date,
            limit=0,
        )
        if after_rows:
            sec += 1
            _render_postevent_decorated_delta_split_tables(
                f"Most Decorated Athletes at {venue_name}",
                before_rows,
                after_rows,
                after_styles,
                args,
                per_gender_limit=10,
                gender_filter=decorated_gender_filter,
            )

    # Section: Most Decorated Athletes at event type (WC/WCH/OWG)
    if event_type in MAJOR_LEVELS:
        before_scope_rows, _before_scope_styles = (
            _build_event_type_decorated_athlete_rows(
                event_type,
                reference_date=reference_date,
                exclude_race_ids={race_id} if race_id else None,
                limit=0,
            )
        )
        after_scope_rows, after_scope_styles = _build_event_type_decorated_athlete_rows(
            event_type,
            reference_date=reference_date,
            limit=0,
        )
        if after_scope_rows:
            sec += 1
            event_type_label = EVENT_TYPE_LABELS.get(
                event_type,
                EVENT_TYPE_LABELS.get(EVENT_TYPE_WC, "World Cup"),
            )
            _render_postevent_decorated_delta_split_tables(
                f"Most Decorated Athletes at {event_type_label}",
                before_scope_rows,
                after_scope_rows,
                after_scope_styles,
                args,
                per_gender_limit=10,
                gender_filter=decorated_gender_filter,
            )

        before_major_rows, _before_major_styles = (
            _build_major_events_decorated_athlete_rows(
                reference_date=reference_date,
                exclude_race_ids={race_id} if race_id else None,
                limit=0,
            )
        )
        after_major_rows, after_major_styles = (
            _build_major_events_decorated_athlete_rows(
                reference_date=reference_date,
                limit=0,
            )
        )
        if after_major_rows:
            sec += 1
            _render_postevent_decorated_delta_split_tables(
                "Most Decorated Athletes at WC+WCH+OWG",
                before_major_rows,
                after_major_rows,
                after_major_styles,
                args,
                per_gender_limit=10,
                gender_filter=decorated_gender_filter,
            )

    return 0
