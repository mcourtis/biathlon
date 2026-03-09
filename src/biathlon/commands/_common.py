"""Shared helpers used across multiple command modules."""

from __future__ import annotations

import argparse
import datetime
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from ..api import BiathlonError, get_analytic_results, get_race_results
from ..constants import (
    DISCIPLINE_NAMES,
    EVENT_TYPE_LABELS,
    EVENT_TYPE_OWG,
    EVENT_TYPE_WC,
    EVENT_TYPE_WCH,
    RELAY_DISCIPLINE,
    RELAY_DISCIPLINES,
    SINGLE_MIXED_RELAY_DISCIPLINE,
)
from ..formatting import Color, get_output_format
from ..utils import get_first_time, parse_start_datetime, parse_time_seconds


# Leader marker characters
GENERAL_LEADER_MARKER = "\u25cb"  # placeholder for yellow circle
DISCIPLINE_LEADER_MARKER = "\u25cc"  # placeholder for red circle
U23_LEADER_MARKER = "\u25ce"  # placeholder for dark blue circle
LEADER_MARKER_DOT = "\u25cf"


def _format_leader_markers(
    cell_str: str,
    row_idx: int,
    base_formatter: Callable[[str, int], str] | None = None,
) -> str:
    """Replace leader marker placeholders with colored dots.

    Extracts trailing GENERAL_LEADER_MARKER / DISCIPLINE_LEADER_MARKER /
    U23_LEADER_MARKER tokens
    from *cell_str*, applies *base_formatter* to the remaining text, then
    appends gold/red/dark-blue filled dots.
    """
    text = cell_str.rstrip()
    pad_len = len(cell_str) - len(text)
    tokens = text.split()
    markers: list[str] = []
    while tokens and tokens[-1] in {
        GENERAL_LEADER_MARKER,
        DISCIPLINE_LEADER_MARKER,
        U23_LEADER_MARKER,
    }:
        markers.insert(0, tokens.pop())
    base = " ".join(tokens)
    if base_formatter:
        base = base_formatter(base, row_idx)
    if markers:
        colored = []
        for marker in markers:
            if marker == GENERAL_LEADER_MARKER:
                colored.append(Color.gold(LEADER_MARKER_DOT))
            elif marker == DISCIPLINE_LEADER_MARKER:
                colored.append(Color.red(LEADER_MARKER_DOT))
            elif marker == U23_LEADER_MARKER:
                colored.append(Color.dark_blue(LEADER_MARKER_DOT, bold=True))
        base = f"{base} {' '.join(colored)}" if base else " ".join(colored)
    return f"{base}{' ' * pad_len}"


def _row_ibu_id(row: dict) -> str:
    """Return the IBU id from a result/standings row."""
    for key in ("IBUId", "IbuId", "ibuId", "Id"):
        val = row.get(key)
        if val:
            return str(val)
    return ""


def _parse_rank(value: Any) -> int | None:
    """Parse a rank value, stripping trailing dots."""
    text = str(value).strip().rstrip(".")
    if text.isdigit():
        return int(text)
    return None


def _format_section_title(text: str, args: argparse.Namespace) -> str:
    """Return *text* as a section title for the active output format."""
    output_format = get_output_format(args)
    if output_format == "pretty":
        return Color.section_title(text)
    if output_format == "markdown":
        stripped = text.strip()
        if not stripped or stripped.startswith("#"):
            return stripped
        return f"## {stripped}"
    return text


def _ordinal(n: int) -> str:
    """Convert a number to its ordinal string (1st, 2nd, 3rd, etc.)."""
    if 11 <= n % 100 <= 13:
        return f"{n}th"
    return f"{n}{['th', 'st', 'nd', 'rd', 'th'][min(n % 10, 4)]}"


def _max_workers(total: int, cap: int = 15) -> int:
    """Return a capped worker count for concurrent fetches."""
    return min(cap, max(1, total))


def detect_event_type(event: dict) -> str:
    """Detect event type from event description.

    Returns:
        EVENT_TYPE_OWG for Olympic Games
        EVENT_TYPE_WCH for World Championships
        EVENT_TYPE_WC for regular World Cup
    """
    desc = str(event.get("Description") or event.get("ShortDescription") or "").lower()
    if "olympic" in desc:
        return EVENT_TYPE_OWG
    if "world championships" in desc:
        return EVENT_TYPE_WCH
    return EVENT_TYPE_WC


def _season_end_year(season_id: str) -> int | None:
    """Return season end year for ids like '9798' -> 1998."""
    text = str(season_id or "").strip()
    if len(text) != 4 or not text.isdigit():
        return None

    start_yy = int(text[:2])
    end_yy = int(text[2:])
    century = 1900 if start_yy >= 80 else 2000
    if end_yy < start_yy:
        century += 100
    return century + end_yy


def _birth_year_from_ibu_id(ibu_id: str) -> int | None:
    """Extract birth year from IBU ID (format BT{NAT3}{5chars}{YYYY}{2chars})."""
    if len(ibu_id) >= 14 and ibu_id.startswith("BT") and ibu_id[10:14].isdigit():
        return int(ibu_id[10:14])
    return None


def _normalize_wc_rule_discipline(discipline: str, category: str) -> str:
    disc = str(discipline or "").upper()
    cat = str(category or "").upper()
    if disc == "SI":
        return "IN"
    # Some mixed relays are stored as RL + MX instead of MR.
    if disc == "RL" and cat == "MX":
        return "MR"
    return disc


def counts_toward_wc_standings(
    event_type: str,
    season_id: str,
    discipline: str = "",
    category: str = "",
) -> bool:
    """Return True when a race counts toward World Cup standings.

    Historical rules encoded here:
    - OWG counted only in 1998/2002/2006/2010.
    - WCH counted from 1990 with exceptions:
      1991 no, 1993 no, 2014 no, 2018 no, 2022+ no.
    - Partial Olympic-year WCH that counted:
      1998 pursuit/team, 2002 mass-start, 2006 mixed relay, 2010 mixed relay.

    Team-only 1992 and 1994 WCH editions remain excluded (historically unclear).
    """
    et = str(event_type or "").upper()
    if et == EVENT_TYPE_WC:
        return True

    end_year = _season_end_year(season_id)
    if end_year is None:
        return False

    if et == EVENT_TYPE_OWG:
        return end_year in {1998, 2002, 2006, 2010}

    if et != EVENT_TYPE_WCH:
        return False

    if end_year < 1990:
        return False
    if end_year in {1991, 1992, 1993, 1994, 2014, 2018}:
        return False
    if end_year >= 2022:
        return False

    disc = _normalize_wc_rule_discipline(discipline, category)
    if end_year == 1998:
        return disc in {"PU", "TM"}
    if end_year == 2002:
        return disc == "MS"
    if end_year in {2006, 2010}:
        return disc == "MR"
    return True


def is_relay_discipline(discipline: str) -> bool:
    """Return True if *discipline* is any relay type."""
    return discipline in RELAY_DISCIPLINES


def is_mixed_relay(discipline: str, category: str) -> bool:
    """Return True if the race is a mixed relay."""
    if discipline in {"MR", "SR"}:
        return True
    return discipline == "RL" and category == "MX"


def _discipline_display_name(discipline: str, category: str) -> str:
    """Return discipline display label from code + category."""
    disc = str(discipline or "").upper()
    cat = str(category or "").upper()
    if disc == "RL" and cat == "MX":
        return "Mixed Relay"
    return DISCIPLINE_NAMES.get(disc, disc or "?")


def _select_race_interactive(candidates: list[tuple[str, dict]]) -> tuple[str, dict]:
    """Prompt user to select from multiple races.

    If only one candidate or not a TTY, auto-select the first.
    """
    if len(candidates) == 1:
        return candidates[0]

    # Check if we can prompt (is a TTY)
    if not sys.stdin.isatty():
        # Non-interactive: auto-select and inform user
        race_id, payload = candidates[0]
        print(f"Multiple races found, using: {race_id}", file=sys.stderr)
        return candidates[0]

    # Display options
    print("\nMultiple races found:\n", file=sys.stderr)

    def _format_start_for_display(start_raw: str) -> str:
        dt = parse_start_datetime(start_raw)
        if dt is not None:
            local_dt = dt.astimezone()
            tz_name = local_dt.tzname() or ""
            base = local_dt.strftime("%Y-%m-%d %H:%M")
            return f"{base} {tz_name}".strip()
        text = str(start_raw or "").strip()
        if text.endswith("Z"):
            text = text[:-1]
        return text.replace("T", " ")

    for idx, (race_id, payload) in enumerate(candidates, 1):
        comp = payload.get("Competition") or {}
        sport_evt = payload.get("SportEvt") or {}
        event_type = detect_event_type(sport_evt)
        event_label = EVENT_TYPE_LABELS.get(event_type, event_type)
        cat = comp.get("catId") or comp.get("CatId") or "?"
        disc = comp.get("DisciplineId") or "?"
        disc_label = _discipline_display_name(str(disc), str(cat))
        venue = sport_evt.get("Organizer") or sport_evt.get("ShortDescription") or ""
        start_raw = comp.get("StartTime") or comp.get("StartDate") or ""
        start = _format_start_for_display(str(start_raw))

        cat_label = {"SW": "Women", "SM": "Men", "MX": "Mixed"}.get(cat, cat)
        race_label = (
            f"{cat_label} {disc_label}"
            if cat_label == "Mixed"
            else f"{cat_label}'s {disc_label}"
        )
        status = comp.get("StatusText") or ""
        status_part = f" | Status: {status}" if status else ""
        print(f"  {idx}. [{event_label}] {race_label} - {venue}", file=sys.stderr)
        print(f"     Start: {start} | ID: {race_id}{status_part}\n", file=sys.stderr)

    # Get user selection
    while True:
        try:
            choice = input(f"Enter selection (1-{len(candidates)}): ").strip()
            idx = int(choice) - 1
            if 0 <= idx < len(candidates):
                return candidates[idx]
            print("Invalid selection, try again.", file=sys.stderr)
        except ValueError:
            print("Please enter a number.", file=sys.stderr)
        except (EOFError, KeyboardInterrupt):
            raise BiathlonError("Selection cancelled")


# ---------------------------------------------------------------------------
# Analytics helpers shared by cumulate and shooting commands
# ---------------------------------------------------------------------------


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


def _lookup_analytic_time(times: dict[str, str], res: dict) -> str:
    """Return analytic time for a result using multiple identifier keys."""
    for key in (
        res.get("IBUId"),
        res.get("Bib"),
        res.get("Name"),
        res.get("ShortName"),
    ):
        if key and str(key) in times:
            return times[str(key)]
    return ""


def _prefetch_analytic_maps(
    requests: list[tuple[str, str]],
) -> dict[tuple[str, str], dict[str, str]]:
    """Fetch multiple analytic maps in parallel.

    Args:
        requests: List of (race_id, type_id) tuples to fetch.

    Returns:
        Dict mapping (race_id, type_id) to the analytic map result.
    """
    if not requests:
        return {}
    results: dict[tuple[str, str], dict[str, str]] = {}
    if len(requests) == 1:
        race_id, type_id = requests[0]
        results[(race_id, type_id)] = _fetch_analytic_map(race_id, type_id)
        return results
    with ThreadPoolExecutor(max_workers=_max_workers(len(requests), cap=8)) as executor:
        futures = {
            executor.submit(_fetch_analytic_map, race_id, type_id): (race_id, type_id)
            for race_id, type_id in requests
        }
        for future in as_completed(futures):
            key = futures[future]
            try:
                results[key] = future.result()
            except Exception:
                results[key] = {}
    return results


def _fetch_leg_lap_times(
    race_id: str,
    lap_prefix: str,
    lap_suffix: str,
    laps: int,
    laps_per_leg: int,
) -> dict[tuple[str, int], dict[str, str]]:
    """Fetch analytic lap times keyed by (Bib/IBUId/Name, Leg) -> {lapN: time_str}."""
    times: dict[tuple[str, int], dict[str, str]] = {}
    for idx in range(1, laps + 1):
        type_id = f"{lap_prefix}{idx}{lap_suffix}"
        try:
            analytic = get_analytic_results(race_id, type_id)
        except BiathlonError:
            continue
        for res in analytic.get("Results", []):
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


# ---------------------------------------------------------------------------
# Relay-specific analytics helpers
# ---------------------------------------------------------------------------


def _fetch_relay_analytic_times(
    race_id: str, type_id: str
) -> dict[tuple[str, int], float]:
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


def _has_completed_relay_results(payload: dict) -> bool:
    """Return True when a relay race payload contains completed results."""
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


# ---------------------------------------------------------------------------
# Chronology helpers shared by postrace and brief commands
# ---------------------------------------------------------------------------

_RACE_SEASON_RE = re.compile(r"^BT(?P<season>\d{4})")
_SEASON_TEXT_RE = re.compile(r"^(?P<s1>\d{2})\s*/\s*(?P<s2>\d{2})$")


def _normalize_season_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) == 4 and text.isdigit():
        return text
    match = _SEASON_TEXT_RE.match(text)
    if match:
        return f"{match.group('s1')}{match.group('s2')}"
    return ""


def _season_id_from_race_id(race_id: str) -> str:
    match = _RACE_SEASON_RE.match(str(race_id or "").strip().upper())
    if not match:
        return ""
    return str(match.group("season") or "")


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


def _is_team_level_result(result: dict) -> bool:
    disc = str(result.get("DisciplineId") or result.get("Comp") or "").upper()
    return bool(result.get("IsTeam")) or disc in {
        RELAY_DISCIPLINE,
        SINGLE_MIXED_RELAY_DISCIPLINE,
        "MR",
    }


def _normalize_discipline_id(disc: str) -> str:
    """Normalize discipline aliases: SI is an alternate code for IN."""
    d = disc.upper()
    return "IN" if d == "SI" else d


def _result_discipline_id(row: dict) -> str:
    return _normalize_discipline_id(
        str(row.get("DisciplineId") or row.get("Comp") or row.get("Discipline") or "")
    )


def _warn_once(message: str, warning_keys: set[str], key: str) -> None:
    if key in warning_keys:
        return
    warning_keys.add(key)
    print(message, file=sys.stderr)


def _resolve_result_start_datetime(
    result: dict,
    race_start_cache: dict[str, datetime.datetime | None],
    get_race_results_fn: Callable[[str], dict] = get_race_results,
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
        payload = get_race_results_fn(race_id)
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
    get_race_results_fn: Callable[[str], dict] = get_race_results,
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
    start_dt = _resolve_result_start_datetime(
        result,
        race_start_cache,
        get_race_results_fn=get_race_results_fn,
    )
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
