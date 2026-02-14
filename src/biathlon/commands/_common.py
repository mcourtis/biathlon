"""Shared helpers used across multiple command modules."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from ..api import BiathlonError, get_analytic_results
from ..constants import EVENT_TYPE_OWG, EVENT_TYPE_WC, EVENT_TYPE_WCH, RELAY_DISCIPLINES
from ..formatting import Color, is_pretty_output
from ..utils import get_first_time, parse_time_seconds


# Leader marker characters
GENERAL_LEADER_MARKER = "\u25cb"  # placeholder for yellow circle
DISCIPLINE_LEADER_MARKER = "\u25cc"  # placeholder for red circle
LEADER_MARKER_DOT = "\u25cf"


def _format_leader_markers(
    cell_str: str,
    row_idx: int,
    base_formatter: Callable[[str, int], str] | None = None,
) -> str:
    """Replace leader marker placeholders with colored dots.

    Extracts trailing GENERAL_LEADER_MARKER / DISCIPLINE_LEADER_MARKER tokens
    from *cell_str*, applies *base_formatter* to the remaining text, then
    appends gold/red filled dots.
    """
    text = cell_str.rstrip()
    pad_len = len(cell_str) - len(text)
    tokens = text.split()
    markers: list[str] = []
    while tokens and tokens[-1] in {GENERAL_LEADER_MARKER, DISCIPLINE_LEADER_MARKER}:
        markers.insert(0, tokens.pop())
    base = " ".join(tokens)
    if base_formatter:
        base = base_formatter(base, row_idx)
    if markers:
        colored = []
        for marker in markers:
            if marker == GENERAL_LEADER_MARKER:
                colored.append(Color.gold(LEADER_MARKER_DOT))
            else:
                colored.append(Color.red(LEADER_MARKER_DOT))
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
    """Return *text* styled as a section title when pretty output is on."""
    if not is_pretty_output(args):
        return text
    return Color.section_title(text)


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


def is_relay_discipline(discipline: str) -> bool:
    """Return True if *discipline* is any relay type."""
    return discipline in RELAY_DISCIPLINES


def is_mixed_relay(discipline: str, category: str) -> bool:
    """Return True if the race is a mixed relay."""
    if discipline in {"MR", "SR"}:
        return True
    return discipline == "RL" and category == "MX"


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
