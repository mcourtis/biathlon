"""Cumulate command handlers for aggregated statistics."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from typing import Any

from ..api import (
    BiathlonError,
    get_analytic_results,
    get_current_season_id,
    get_events,
    get_race_results,
    get_races,
)
from ..constants import (
    GENDER_TO_CAT,
    INDIVIDUAL_DISCIPLINES,
    RELAY_DISCIPLINE,
    RELAY_MEN_CAT,
    RELAY_MIXED_CAT,
    RELAY_WOMEN_CAT,
    SINGLE_MIXED_RELAY_DISCIPLINE,
)
from ..formatting import (
    Color,
    format_pct,
    format_seconds,
    is_pretty_output,
    rank_style,
    render_table,
)
from ..utils import (
    base_time_seconds,
    extract_results,
    get_first_time,
    get_race_start_key,
    parse_relay_shootings,
    parse_time_seconds,
    result_seconds,
)
from ._common import (
    _fetch_leg_lap_times,
    _lookup_analytic_time,
    _max_workers,
    _parse_shootings,
    _prefetch_analytic_maps,
    is_relay_discipline,
)
from .results import _get_top_n_ibu_ids

MAX_FETCH_WORKERS = 8


def _stage_counts(shootings: str | None) -> tuple[int, int, int, int, int]:
    """Return (miss_prone, miss_standing, shot_prone, shot_standing, shots_total)."""
    misses = _parse_shootings(shootings)
    if not misses:
        return 0, 0, 0, 0, 0
    miss_prone = miss_standing = 0
    shot_prone = shot_standing = 0
    shots_total = len(misses) * 5
    if len(misses) >= 4:
        miss_prone = misses[0] + misses[1]
        miss_standing = misses[2] + misses[3]
        shot_prone = 10
        shot_standing = 10
    elif len(misses) == 3:
        miss_prone = misses[0] + misses[1]
        miss_standing = misses[2]
        shot_prone = 10
        shot_standing = 5
    elif len(misses) == 2:
        miss_prone = misses[0]
        miss_standing = misses[1]
        shot_prone = 5
        shot_standing = 5
    elif len(misses) == 1:
        miss_prone = misses[0]
        shot_prone = 5
    return miss_prone, miss_standing, shot_prone, shot_standing, shots_total


def _race_list(season_id: str, event_id: str | None) -> list[dict]:
    """Return list of races for season or event."""
    events = get_events(season_id, level=1) if not event_id else [{"EventId": event_id}]
    races: list[dict] = []
    event_ids: list[str] = [
        event["EventId"] for event in events if event.get("EventId")
    ]
    if not event_ids:
        return races
    if len(event_ids) == 1:
        races.extend(get_races(event_ids[0]))
        return races
    with ThreadPoolExecutor(
        max_workers=_max_workers(len(event_ids), cap=8)
    ) as executor:
        futures = {executor.submit(get_races, ev_id): ev_id for ev_id in event_ids}
        for future in as_completed(futures):
            races.extend(future.result())
    return races


def _discipline_filter(discipline: str) -> tuple[set[str], str | None, bool]:
    """Return (disc_set, cat_filter, allow_relay)."""
    if discipline == "all":
        return INDIVIDUAL_DISCIPLINES.copy(), None, False
    if discipline == "individual":
        return {"IN"}, None, False
    if discipline == "sprint":
        return {"SP"}, None, False
    if discipline == "pursuit":
        return {"PU"}, None, False
    if discipline == "mass-start":
        return {"MS"}, None, False
    if discipline == "relay":
        return {RELAY_DISCIPLINE}, None, True
    if discipline == "mixed-relay":
        return {RELAY_DISCIPLINE}, RELAY_MIXED_CAT, True
    if discipline == "single-mixed-relay":
        return {SINGLE_MIXED_RELAY_DISCIPLINE}, RELAY_MIXED_CAT, True
    raise BiathlonError(f"unknown discipline {discipline}")


def _race_cat_allows(
    race_disc: str,
    cat_id: str,
    allow_relay: bool,
    cat_filter: str | None,
    gender_cat: str,
) -> bool:
    """Return True when cat_id matches the requested scope."""
    if race_disc in {RELAY_DISCIPLINE, SINGLE_MIXED_RELAY_DISCIPLINE}:
        if not allow_relay:
            return False
        if cat_id:
            if cat_filter and cat_id != cat_filter:
                return False
            if not cat_filter and cat_id == RELAY_MIXED_CAT:
                return False
            if not cat_filter and cat_id not in {RELAY_MEN_CAT, RELAY_WOMEN_CAT}:
                return False
            if not cat_filter and cat_id != gender_cat:
                return False
            if cat_id not in {RELAY_MEN_CAT, RELAY_WOMEN_CAT, RELAY_MIXED_CAT}:
                return False
    else:
        if cat_id and cat_id != gender_cat:
            return False
    return True


def _event_label(payload: dict) -> str:
    """Return location label for a race payload."""
    sport_evt = payload.get("SportEvt") or {}
    return sport_evt.get("ShortDescription") or sport_evt.get("Organizer") or ""


def _aggregate_entries(entries: dict, key: str, name: str, nat: str) -> dict:
    """Get or create entry for an athlete/team."""
    if key not in entries:
        entries[key] = {
            "name": name,
            "nat": nat,
            "races": 0,
            "total_secs": 0.0,
            "misses": 0,
            "miss_prone": 0,
            "miss_standing": 0,
            "shots": 0,
            "shot_prone": 0,
            "shot_standing": 0,
            "relay_pen": 0,
            "relay_spare": 0,
            "gains": {},
            "total_gain": 0,
        }
    return entries[key]


def _calc_accuracy(entry: dict) -> tuple[str, str, str]:
    """Return (acc, prone, standing) percentage strings."""
    shots = entry["shots"]
    if shots == 0:
        return "-", "-", "-"
    hits = shots - entry["misses"]
    acc = format_pct(hits, shots)
    prone_pct = (
        format_pct(entry["shot_prone"] - entry["miss_prone"], entry["shot_prone"])
        if entry["shot_prone"]
        else "-"
    )
    standing_pct = (
        format_pct(
            entry["shot_standing"] - entry["miss_standing"], entry["shot_standing"]
        )
        if entry["shot_standing"]
        else "-"
    )
    return acc, prone_pct, standing_pct


def _apply_top_filter(
    results: list[dict],
    top_n: int,
    cat_id: str,
    season_id: str,
    top_ibu_ids: set[str] | None = None,
) -> list[dict]:
    """Filter results to top N WC athletes.

    Args:
        results: List of race results to filter.
        top_n: Number of top athletes to include (0 for all).
        cat_id: Category ID (e.g., 'SW' for women).
        season_id: Season ID.
        top_ibu_ids: Pre-fetched set of top IBU IDs to use (avoids repeated API calls).
    """
    if top_n <= 0:
        return results
    if top_ibu_ids is None:
        top_ibu_ids = set(_get_top_n_ibu_ids(cat_id, top_n, season_id))
    if not top_ibu_ids:
        return results
    return [r for r in results if r.get("IBUId") in top_ibu_ids]


def _get_top_ibu_ids_set(
    payloads: list[tuple[str, dict]], top_n: int, season_id: str
) -> set[str] | None:
    """Pre-fetch top IBU IDs for filtering.

    Returns a set of top IBU IDs based on the first non-relay payload's category,
    or None if top_n <= 0.
    """
    if top_n <= 0:
        return None
    # Find the first non-relay payload to get the category
    for _, payload in payloads:
        if not _is_relay(payload):
            cat_id = (payload.get("Competition") or {}).get("catId", "").upper()
            if cat_id:
                return set(_get_top_n_ibu_ids(cat_id, top_n, season_id))
    return None


def _apply_limit(rows: list[dict], limit: int) -> list[dict]:
    """Apply output limit if configured."""
    if limit and limit > 0:
        return rows[:limit]
    return rows


def _build_accuracy_cell_formatters(
    headers: list[str], render_rows: list[list[str]]
) -> list | None:
    """Return cell formatters for accuracy columns when present."""
    acc_labels = ["Accuracy %", "Prone %", "Standing %"]
    indices = []
    for label in acc_labels:
        if label in headers:
            indices.append(headers.index(label))
    if not indices:
        return None

    def parse_pct(value: str) -> float | None:
        text = value.strip()
        if not text or text == "-":
            return None
        if text.endswith("%"):
            text = text[:-1]
        try:
            return float(text) / 100.0
        except ValueError:
            return None

    accuracy_values = []
    for row in render_rows:
        accuracy_values.append([parse_pct(str(row[idx])) for idx in indices])

    def make_acc_formatter(acc_idx: int):
        def formatter(cell_str: str, row_idx: int) -> str:
            if row_idx < len(accuracy_values):
                pct = accuracy_values[row_idx][acc_idx]
                if pct is not None:
                    return Color.accuracy(cell_str, pct)
            return cell_str

        return formatter

    cell_formatters = [None] * len(headers)
    for acc_idx, header_idx in enumerate(indices):
        cell_formatters[header_idx] = make_acc_formatter(acc_idx)
    return cell_formatters


def _is_lapped(result: dict) -> bool:
    """Return True if result indicates lapped/looped status."""
    irm = str(result.get("IRM") or "").upper()
    if irm in {"LAP", "LAPPED"}:
        return True
    if irm == "DNF":
        return True
    val = str(result.get("Result") or result.get("TotalTime") or "").upper()
    if "LAP" in val:
        return True
    if "DNF" in val:
        return True
    rank = str(result.get("Rank") or "").strip()
    return rank == "10000"


def _status_label(result: dict) -> str:
    """Return status label for DNS/DNF/LAP results."""
    irm = str(result.get("IRM") or "").upper()
    if irm in {"DNS", "DNF"}:
        return irm
    if irm in {"LAP", "LAPPED"}:
        return "LAP"
    val = str(result.get("Result") or result.get("TotalTime") or "").upper()
    if "DNS" in val:
        return "DNS"
    if "DNF" in val:
        return "DNF"
    if "LAP" in val:
        return "LAP"
    rank = str(result.get("Rank") or "").strip()
    if rank == "10000":
        return "LAP"
    return ""


def _collect_races(
    args: argparse.Namespace,
    allow_discipline: bool,
    discipline_override: str | None = None,
    allow_event: bool = True,
) -> tuple[list[tuple[str, dict]], str]:
    """Collect race payloads matching args; returns ([(race_id, payload)], season_id)."""
    if not allow_event and getattr(args, "event", None):
        raise BiathlonError("--event is not supported for this subcommand")
    discipline_value = (
        discipline_override or getattr(args, "discipline", "all") or "all"
    )
    event_value = getattr(args, "event", "") if allow_event else ""
    season_value = getattr(args, "season", "")

    if event_value and (season_value or discipline_value != "all"):
        raise BiathlonError("--event cannot be used with --season or --discipline")
    if not allow_discipline and discipline_value != "all":
        raise BiathlonError("--discipline is not supported for this subcommand")

    season_id = season_value or get_current_season_id()
    event_id = event_value or None
    races = _race_list(season_id, event_id)
    if not races:
        return ([], season_id)

    disc_set, cat_filter, allow_relay = _discipline_filter(discipline_value)
    if getattr(args, "include_relay", False):
        allow_relay = True
        disc_set = set(disc_set)
        disc_set.add(RELAY_DISCIPLINE)

    gender_cat = (
        GENDER_TO_CAT["men"] if getattr(args, "men", False) else GENDER_TO_CAT["women"]
    )
    candidates: list[tuple[str, str]] = []
    for race in sorted(races, key=get_race_start_key):
        race_id = race.get("RaceId") or race.get("Id")
        if not race_id:
            continue
        race_disc = str(race.get("DisciplineId") or "").upper()
        if race_disc not in disc_set:
            continue
        race_cat = str(race.get("catId") or race.get("CatId") or "").upper()
        if not _race_cat_allows(
            race_disc, race_cat, allow_relay, cat_filter, gender_cat
        ):
            continue
        candidates.append((race_id, race_disc))
    if not candidates:
        return ([], season_id)

    payload_by_id: dict[str, dict] = {}
    if len(candidates) == 1:
        race_id = candidates[0][0]
        try:
            payload_by_id[race_id] = get_race_results(race_id)
        except BiathlonError:
            pass
    else:
        with ThreadPoolExecutor(
            max_workers=_max_workers(len(candidates), cap=8)
        ) as executor:
            futures = {
                executor.submit(get_race_results, race_id): race_id
                for race_id, _ in candidates
            }
            for future in as_completed(futures):
                race_id = futures[future]
                try:
                    payload_by_id[race_id] = future.result()
                except BiathlonError:
                    continue

    payloads: list[tuple[str, dict]] = []
    for race_id, race_disc in candidates:
        payload = payload_by_id.get(race_id)
        if not payload:
            continue
        comp = payload.get("Competition") or {}
        comp_cat = str(comp.get("catId") or comp.get("CatId") or "").upper()
        if not _race_cat_allows(
            race_disc, comp_cat, allow_relay, cat_filter, gender_cat
        ):
            continue
        payloads.append((race_id, payload))
    return (payloads, season_id)


def _is_relay(payload: dict) -> bool:
    """Return True if payload is a relay discipline."""
    discipline = str(
        (payload.get("Competition") or {}).get("DisciplineId") or ""
    ).upper()
    return is_relay_discipline(discipline)


def _race_results(payload: dict) -> list[dict]:
    """Return appropriate results list for a payload."""
    if _is_relay(payload):
        return [r for r in (payload.get("Results") or []) if r.get("IsTeam")]
    return extract_results(payload)


def _relay_leg_results(payload: dict) -> list[dict]:
    """Return non-team relay leg results from a payload."""
    return [r for r in (payload.get("Results") or []) if not r.get("IsTeam")]


def _fetch_leg_total_times(race_id: str, type_id: str) -> dict[tuple[str, int], float]:
    """Fetch analytic total times keyed by (Bib/IBUId/Name, Leg) -> seconds."""
    times: dict[tuple[str, int], float] = {}
    try:
        analytic = get_analytic_results(race_id, type_id)
    except BiathlonError:
        return times
    for res in analytic.get("Results", []):
        if res.get("IsTeam"):
            continue
        leg = res.get("Leg")
        if not isinstance(leg, int):
            continue
        time_str = get_first_time(res, ["TotalTime", "Result"])
        if not time_str:
            continue
        seconds = parse_time_seconds(time_str)
        if seconds is None:
            continue
        for key in (res.get("Bib"), res.get("IBUId"), res.get("Name")):
            if key:
                times[(str(key), leg)] = seconds
    return times


def handle_cumulate_results(args: argparse.Namespace) -> int:
    """Cumulate total result times."""
    try:
        payloads, season_id = _collect_races(args, allow_discipline=True)
    except BiathlonError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not payloads:
        print("no races found for the requested scope", file=sys.stderr)
        return 1

    # Pre-fetch top IBU IDs once for filtering (avoids repeated API calls)
    top_ibu_ids = _get_top_ibu_ids_set(payloads, args.top, season_id)
    include_relay = bool(getattr(args, "include_relay", False))

    entries: dict[str, dict] = {}
    total_races = 0
    for race_id, payload in payloads:
        is_relay = _is_relay(payload)
        if is_relay and include_relay:
            results = _relay_leg_results(payload)
        else:
            results = _race_results(payload)
        if not results:
            continue
        base_secs = base_time_seconds(results) if not is_relay else None
        cat_id = (payload.get("Competition") or {}).get("catId", "").upper()
        if not is_relay:
            results = _apply_top_filter(
                results, args.top, cat_id, season_id, top_ibu_ids
            )
        if not results:
            continue
        race_has_data = False
        leg_cumulative: dict[tuple[str, int], float] = {}
        if is_relay:
            for res in results:
                bib = str(res.get("Bib") or "")
                leg = res.get("Leg")
                if not bib or not isinstance(leg, int):
                    continue
                cum_val = get_first_time(
                    res, ["LegResult", "LegTime", "LegTimeTotal", "TotalTime", "Result"]
                )
                cum_secs = parse_time_seconds(cum_val) if cum_val else None
                if cum_secs is None:
                    continue
                leg_cumulative[(bib, leg)] = cum_secs

        for res in results:
            ident = res.get("IBUId") or res.get("Name") or res.get("ShortName") or ""
            if not ident:
                continue
            name = res.get("Name") or res.get("ShortName") or ""
            nat = res.get("Nat") or ""
            if is_relay:
                cum_val = get_first_time(
                    res, ["LegResult", "LegTime", "LegTimeTotal", "TotalTime", "Result"]
                )
                cum_secs = parse_time_seconds(cum_val) if cum_val else None
                if include_relay and cum_secs is not None:
                    bib = str(res.get("Bib") or "")
                    leg = res.get("Leg")
                    if bib and isinstance(leg, int) and leg > 1:
                        prev = leg_cumulative.get((bib, leg - 1))
                        if prev is not None:
                            cum_secs = cum_secs - prev
                secs = cum_secs
            else:
                secs = result_seconds(res, base_secs)
                if secs is None:
                    secs = parse_time_seconds(
                        get_first_time(res, ["TotalTime", "Result"])
                    )
            if secs is None:
                continue
            entry = _aggregate_entries(entries, str(ident), name, nat)
            entry["races"] += 1
            entry["total_secs"] += secs
            race_has_data = True
        if race_has_data:
            total_races += 1

    if not entries:
        print("no result times found for the requested scope", file=sys.stderr)
        return 1

    rows = []
    for entry in entries.values():
        if entry["races"] != total_races:
            continue
        rows.append(
            {
                "rank_val": entry["total_secs"],
                "row": [
                    0,
                    entry["name"],
                    entry["nat"],
                    entry["races"],
                    format_seconds(entry["total_secs"]),
                ],
            }
        )
    rows.sort(key=lambda r: (r["rank_val"], r["row"][1]))
    for idx, row in enumerate(rows, start=1):
        row["row"][0] = idx
    rows = _apply_limit(rows, args.limit)
    headers = ["Rank", "Biathlete", "Country", "Races", "Total Results"]
    pretty = is_pretty_output(args)
    row_styles = [rank_style(r["row"][0]) for r in rows] if pretty else None
    render_table(
        headers, [r["row"] for r in rows], pretty=pretty, row_styles=row_styles
    )
    return 0


def handle_cumulate_ski(args: argparse.Namespace) -> int:
    """Cumulate ski times from individual races."""
    if args.discipline != "all":
        print("error: --discipline is not supported for ski", file=sys.stderr)
        return 1
    try:
        payloads, season_id = _collect_races(
            args, allow_discipline=False, allow_event=False
        )
    except BiathlonError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not payloads:
        print("no races found for the requested scope", file=sys.stderr)
        return 1

    # Prefetch analytic data in parallel (non-relay races only)
    analytic_requests = [
        (race_id, "SKIT") for race_id, payload in payloads if not _is_relay(payload)
    ]
    prefetched_analytic = _prefetch_analytic_maps(analytic_requests)

    # Pre-fetch top IBU IDs once for filtering
    top_ibu_ids = _get_top_ibu_ids_set(payloads, args.top, season_id)

    entries: dict[str, dict] = {}
    total_races = 0
    for race_id, payload in payloads:
        results = _race_results(payload)
        if not results:
            continue
        cat_id = (payload.get("Competition") or {}).get("catId", "").upper()
        if _is_relay(payload):
            continue
        results = _apply_top_filter(results, args.top, cat_id, season_id, top_ibu_ids)
        if not results:
            continue
        ski_times = prefetched_analytic.get((race_id, "SKIT"), {})
        race_has_data = False
        for res in results:
            ident = res.get("IBUId") or res.get("Name") or res.get("ShortName") or ""
            if not ident:
                continue
            name = res.get("Name") or res.get("ShortName") or ""
            nat = res.get("Nat") or ""
            ski_val = _lookup_analytic_time(ski_times, res) or get_first_time(
                res, ["TotalSkiTime", "SkiTime", "SkiTimeTotal", "SKITime", "Ski"]
            )
            secs = parse_time_seconds(ski_val) if ski_val else None
            if secs is None:
                continue
            entry = _aggregate_entries(entries, str(ident), name, nat)
            entry["races"] += 1
            entry["total_secs"] += secs
            race_has_data = True
        if race_has_data:
            total_races += 1

    rows = []
    for entry in entries.values():
        if entry["races"] != total_races:
            continue
        rows.append(
            {
                "rank_val": entry["total_secs"],
                "row": [
                    0,
                    entry["name"],
                    entry["nat"],
                    entry["races"],
                    format_seconds(entry["total_secs"]),
                ],
            }
        )
    rows.sort(key=lambda r: (r["rank_val"], r["row"][1]))
    for idx, row in enumerate(rows, start=1):
        row["row"][0] = idx
    rows = _apply_limit(rows, args.limit)
    headers = ["Rank", "Biathlete", "Country", "Races", "Total Ski"]
    pretty = is_pretty_output(args)
    row_styles = [rank_style(r["row"][0]) for r in rows] if pretty else None
    render_table(
        headers, [r["row"] for r in rows], pretty=pretty, row_styles=row_styles
    )
    return 0


def handle_cumulate_pursuit(args: argparse.Namespace) -> int:
    """Cumulate pursuit times from pursuit races only."""
    if args.discipline != "all":
        print("error: --discipline is not supported for pursuit", file=sys.stderr)
        return 1
    try:
        payloads, season_id = _collect_races(
            args,
            allow_discipline=True,
            discipline_override="pursuit",
            allow_event=False,
        )
    except BiathlonError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not payloads:
        print("no pursuit races found", file=sys.stderr)
        return 1

    # Pre-fetch top IBU IDs once for filtering
    top_ibu_ids = _get_top_ibu_ids_set(payloads, args.top, season_id)

    entries: dict[str, dict] = {}
    total_races = 0
    for _, payload in payloads:
        results = extract_results(payload)
        if not results:
            continue
        base_secs = base_time_seconds(results)
        cat_id = (payload.get("Competition") or {}).get("catId", "").upper()
        results = _apply_top_filter(results, args.top, cat_id, season_id, top_ibu_ids)
        if not results:
            continue
        total_races += 1
        for res in results:
            ident = res.get("IBUId") or res.get("Name") or res.get("ShortName") or ""
            if not ident:
                continue
            name = res.get("Name") or res.get("ShortName") or ""
            nat = res.get("Nat") or ""
            result_time = result_seconds(res, base_secs)
            delay = (
                parse_time_seconds(res.get("StartInfo"))
                if res.get("StartInfo")
                else None
            )
            if result_time is None or delay is None:
                continue
            pursuit_secs = result_time - delay
            if pursuit_secs < 0:
                continue
            entry = _aggregate_entries(entries, str(ident), name, nat)
            entry["races"] += 1
            entry["total_secs"] += pursuit_secs

    rows = []
    for entry in entries.values():
        if entry["races"] != total_races:
            continue
        rows.append(
            {
                "rank_val": entry["total_secs"],
                "row": [
                    0,
                    entry["name"],
                    entry["nat"],
                    entry["races"],
                    format_seconds(entry["total_secs"]),
                ],
            }
        )
    rows.sort(key=lambda r: (r["rank_val"], r["row"][1]))
    for idx, row in enumerate(rows, start=1):
        row["row"][0] = idx
    rows = _apply_limit(rows, args.limit)
    headers = ["Rank", "Biathlete", "Country", "Races", "Total Pursuit"]
    render_rows = [r["row"] for r in rows]
    row_styles = (
        [rank_style(r[0]) for r in render_rows] if is_pretty_output(args) else None
    )

    render_table(
        headers,
        render_rows,
        pretty=is_pretty_output(args),
        row_styles=row_styles,
    )
    return 0


def handle_cumulate_course(args: argparse.Namespace) -> int:
    """Cumulate course times."""
    try:
        payloads, season_id = _collect_races(args, allow_discipline=True)
    except BiathlonError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not payloads:
        print("no races found for the requested scope", file=sys.stderr)
        return 1

    # Prefetch analytic data in parallel
    include_relay = bool(getattr(args, "include_relay", False))
    analytic_requests: list[tuple[str, str]] = []
    relay_race_ids: list[str] = []
    for race_id, payload in payloads:
        if _is_relay(payload):
            if include_relay:
                relay_race_ids.append(race_id)
        else:
            analytic_requests.append((race_id, "CRST"))
    prefetched_analytic = _prefetch_analytic_maps(analytic_requests)

    # Prefetch relay course times in parallel
    prefetched_relay_course: dict[str, dict[tuple[str, int], float]] = {}
    if relay_race_ids:
        with ThreadPoolExecutor(
            max_workers=_max_workers(len(relay_race_ids), cap=8)
        ) as executor:
            futures = {
                executor.submit(_fetch_leg_total_times, rid, "CRST"): rid
                for rid in relay_race_ids
            }
            for future in as_completed(futures):
                rid = futures[future]
                try:
                    prefetched_relay_course[rid] = future.result()
                except Exception:
                    prefetched_relay_course[rid] = {}

    # Pre-fetch top IBU IDs once for filtering (avoids repeated API calls)
    top_ibu_ids = _get_top_ibu_ids_set(payloads, args.top, season_id)

    entries: dict[str, dict] = {}
    total_races = 0
    for race_id, payload in payloads:
        is_relay = _is_relay(payload)
        if is_relay and include_relay:
            results = _relay_leg_results(payload)
        else:
            results = _race_results(payload)
        if not results:
            continue
        cat_id = (payload.get("Competition") or {}).get("catId", "").upper()
        if not is_relay:
            results = _apply_top_filter(
                results, args.top, cat_id, season_id, top_ibu_ids
            )
        if not results:
            continue
        race_has_data = False
        course_times = (
            prefetched_analytic.get((race_id, "CRST"), {}) if not is_relay else {}
        )
        relay_course_times = (
            prefetched_relay_course.get(race_id, {}) if is_relay else {}
        )
        for res in results:
            ident = res.get("IBUId") or res.get("Name") or res.get("ShortName") or ""
            if not ident:
                continue
            name = res.get("Name") or res.get("ShortName") or ""
            nat = res.get("Nat") or ""
            if is_relay:
                leg = res.get("Leg")
                course_secs = None
                if isinstance(leg, int):
                    for key in (
                        res.get("Bib"),
                        res.get("IBUId"),
                        res.get("Name"),
                        res.get("ShortName"),
                    ):
                        if key is None:
                            continue
                        course_secs = relay_course_times.get((str(key), leg))
                        if course_secs is not None:
                            break
                if course_secs is None:
                    course_val = get_first_time(
                        res, ["LegCourse", "LegRunTime", "LegSkiTime"]
                    )
                    course_secs = parse_time_seconds(course_val) if course_val else None
                secs = course_secs
            else:
                course_val = _lookup_analytic_time(course_times, res) or get_first_time(
                    res,
                    [
                        "TotalCourseTime",
                        "CourseTime",
                        "RunTime",
                    ],
                )
                secs = parse_time_seconds(course_val) if course_val else None
            if secs is None:
                continue
            entry = _aggregate_entries(entries, str(ident), name, nat)
            entry["races"] += 1
            entry["total_secs"] += secs
            race_has_data = True
        if race_has_data:
            total_races += 1
    rows = []
    for entry in entries.values():
        if entry["races"] != total_races:
            continue
        rows.append(
            {
                "rank_val": entry["total_secs"],
                "row": [
                    0,
                    entry["name"],
                    entry["nat"],
                    entry["races"],
                    format_seconds(entry["total_secs"]),
                ],
            }
        )
    rows.sort(key=lambda r: (r["rank_val"], r["row"][1]))
    for idx, row in enumerate(rows, start=1):
        row["row"][0] = idx
    rows = _apply_limit(rows, args.limit)
    headers = ["Rank", "Biathlete", "Country", "Races", "Total Course Time"]
    render_rows = [r["row"] for r in rows]
    row_styles = (
        [rank_style(r[0]) for r in render_rows] if is_pretty_output(args) else None
    )

    render_table(
        headers,
        render_rows,
        pretty=is_pretty_output(args),
        row_styles=row_styles,
    )
    return 0


def _cumulate_range_or_shooting(args: argparse.Namespace, kind: str) -> int:
    """Cumulate range or shooting time plus accuracy stats."""
    try:
        payloads, season_id = _collect_races(args, allow_discipline=True)
    except BiathlonError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not payloads:
        print("no races found for the requested scope", file=sys.stderr)
        return 1

    include_relay = bool(getattr(args, "include_relay", False))
    type_id = "RNGT" if kind == "range" else "STTM"
    time_label = "Range" if kind == "range" else "Shooting"
    relay_stage_keys = ("R1", "R2") if kind == "range" else ("S1", "S2")

    # Prefetch analytic data in parallel
    analytic_requests = [
        (race_id, type_id) for race_id, payload in payloads if not _is_relay(payload)
    ]
    prefetched_analytic = _prefetch_analytic_maps(analytic_requests)

    # Prefetch relay lap times in parallel
    prefetched_relay_laps: dict[str, dict[tuple[str, int], dict[str, str]]] = {}
    if include_relay:
        relay_race_ids = [rid for rid, p in payloads if _is_relay(p)]
        if relay_race_ids:
            lap_prefix = "RNG" if kind == "range" else "S"
            lap_suffix = "" if kind == "range" else "TM"
            with ThreadPoolExecutor(
                max_workers=_max_workers(len(relay_race_ids), cap=8)
            ) as executor:
                futures = {
                    executor.submit(
                        _fetch_leg_lap_times, rid, lap_prefix, lap_suffix, 8, 2
                    ): rid
                    for rid in relay_race_ids
                }
                for future in as_completed(futures):
                    rid = futures[future]
                    try:
                        prefetched_relay_laps[rid] = future.result()
                    except Exception:
                        prefetched_relay_laps[rid] = {}

    # Pre-fetch top IBU IDs once for filtering
    top_ibu_ids = _get_top_ibu_ids_set(payloads, args.top, season_id)

    entries: dict[str, dict] = {}
    total_races = 0
    for race_id, payload in payloads:
        is_relay = _is_relay(payload)
        relay_laps = (
            prefetched_relay_laps.get(race_id, {}) if is_relay and include_relay else {}
        )
        if is_relay and include_relay:
            results = _relay_leg_results(payload)
        else:
            results = _race_results(payload)
        if not results:
            continue
        cat_id = (payload.get("Competition") or {}).get("catId", "").upper()
        if not is_relay:
            results = _apply_top_filter(
                results, args.top, cat_id, season_id, top_ibu_ids
            )
        if not results:
            continue
        race_has_data = False
        times = prefetched_analytic.get((race_id, type_id), {}) if not is_relay else {}
        for res in results:
            ident = res.get("IBUId") or res.get("Name") or res.get("ShortName") or ""
            if not ident:
                continue
            name = res.get("Name") or res.get("ShortName") or ""
            nat = res.get("Nat") or ""
            if is_relay:
                leg = res.get("Leg")
                lap_times = {}
                if isinstance(leg, int):
                    for key in (
                        res.get("Bib"),
                        res.get("IBUId"),
                        res.get("Name"),
                        res.get("ShortName"),
                    ):
                        if key is None:
                            continue
                        lap_times = relay_laps.get((str(key), leg), {})
                        if lap_times:
                            break
                stage_secs: list[float] = []
                for idx, key in enumerate(relay_stage_keys, start=1):
                    raw = lap_times.get(f"lap{idx}") or get_first_time(res, [key])
                    if raw:
                        val = parse_time_seconds(raw)
                        if val is not None:
                            stage_secs.append(val)
                secs = sum(stage_secs) if stage_secs else None
            else:
                if kind == "range":
                    time_val = _lookup_analytic_time(times, res) or get_first_time(
                        res, ["TotalRangeTime", "RangeTime"]
                    )
                else:
                    time_val = _lookup_analytic_time(times, res) or get_first_time(
                        res, ["TotalShootingTime", "ShootingTime"]
                    )
                secs = parse_time_seconds(time_val) if time_val else None
            if secs is None:
                continue
            entry = _aggregate_entries(entries, str(ident), name, nat)
            entry["races"] += 1
            entry["total_secs"] += secs
            if not is_relay or include_relay:
                miss_prone, miss_stand, shot_prone, shot_stand, shots_total = (
                    _stage_counts(res.get("Shootings") or res.get("ShootingTotal"))
                )
                if shots_total:
                    entry["shots"] += shots_total
                    entry["misses"] += miss_prone + miss_stand
                    entry["miss_prone"] += miss_prone
                    entry["miss_standing"] += miss_stand
                    entry["shot_prone"] += shot_prone
                    entry["shot_standing"] += shot_stand
            race_has_data = True
        if race_has_data:
            total_races += 1

    rows = []
    for entry in entries.values():
        if entry["races"] != total_races:
            continue
        acc, prone_pct, standing_pct = _calc_accuracy(entry)
        rows.append(
            {
                "rank_val": entry["total_secs"],
                "row": [
                    0,
                    entry["name"],
                    entry["nat"],
                    entry["races"],
                    format_seconds(entry["total_secs"]),
                    acc,
                    prone_pct,
                    standing_pct,
                ],
            }
        )
    rows.sort(key=lambda r: (r["rank_val"], r["row"][1]))
    for idx, row in enumerate(rows, start=1):
        row["row"][0] = idx
    rows = _apply_limit(rows, args.limit)
    headers = [
        "Rank",
        "Biathlete",
        "Country",
        "Races",
        f"Total {time_label} Time",
        "Accuracy %",
        "Prone %",
        "Standing %",
    ]
    render_rows = [r["row"] for r in rows]
    pretty = is_pretty_output(args)
    row_styles = [rank_style(r[0]) for r in render_rows] if pretty else None
    cell_formatters = (
        _build_accuracy_cell_formatters(headers, render_rows) if pretty else None
    )
    render_table(
        headers,
        render_rows,
        pretty=pretty,
        row_styles=row_styles,
        cell_formatters=cell_formatters,
    )
    return 0


def handle_cumulate_range(args: argparse.Namespace) -> int:
    return _cumulate_range_or_shooting(args, "range")


def handle_cumulate_shooting(args: argparse.Namespace) -> int:
    return _cumulate_range_or_shooting(args, "shooting")


def handle_cumulate_miss(args: argparse.Namespace) -> int:
    """Cumulate misses and accuracy."""
    try:
        payloads, season_id = _collect_races(args, allow_discipline=True)
    except BiathlonError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not payloads:
        print("no races found for the requested scope", file=sys.stderr)
        return 1

    # Pre-fetch top IBU IDs once for filtering
    top_ibu_ids = _get_top_ibu_ids_set(payloads, args.top, season_id)
    include_relay = bool(getattr(args, "include_relay", False))

    entries: dict[str, dict] = {}
    total_races = 0
    for race_id, payload in payloads:
        is_relay = _is_relay(payload)
        if is_relay and not include_relay:
            continue
        if is_relay and include_relay:
            results = _relay_leg_results(payload)
        else:
            results = _race_results(payload)
        if not results:
            continue
        cat_id = (payload.get("Competition") or {}).get("catId", "").upper()
        if not is_relay:
            results = _apply_top_filter(
                results, args.top, cat_id, season_id, top_ibu_ids
            )
        if not results:
            continue
        race_has_data = False
        for res in results:
            ident = res.get("IBUId") or res.get("Name") or res.get("ShortName") or ""
            if not ident:
                continue
            name = res.get("Name") or res.get("ShortName") or ""
            nat = res.get("Nat") or ""
            entry = _aggregate_entries(entries, str(ident), name, nat)
            if is_relay:
                shootings = res.get("Shootings") or res.get("ShootingTotal")
                stages = parse_relay_shootings(shootings) if shootings else None
                if not stages:
                    continue
                prone, standing = stages
                prone_pen, prone_spare = prone
                stand_pen, stand_spare = standing
                prone_misses = prone_pen + prone_spare
                stand_misses = stand_pen + stand_spare
                entry["races"] += 1
                entry["miss_prone"] += prone_misses
                entry["miss_standing"] += stand_misses
                entry["misses"] += prone_misses + stand_misses
                entry["shot_prone"] += 5 + prone_spare
                entry["shot_standing"] += 5 + stand_spare
                entry["shots"] += 10 + prone_spare + stand_spare
                race_has_data = True
            else:
                miss_prone, miss_stand, shot_prone, shot_stand, shots_total = (
                    _stage_counts(res.get("Shootings") or res.get("ShootingTotal"))
                )
                if shots_total == 0:
                    continue
                entry["races"] += 1
                entry["miss_prone"] += miss_prone
                entry["miss_standing"] += miss_stand
                entry["misses"] += miss_prone + miss_stand
                entry["shot_prone"] += shot_prone
                entry["shot_standing"] += shot_stand
                entry["shots"] += shots_total
                race_has_data = True
        if race_has_data:
            total_races += 1

    rows = []
    for entry in entries.values():
        acc, prone_pct, standing_pct = _calc_accuracy(entry)
        if entry["races"] == 0:
            continue
        if entry["races"] != total_races:
            continue
        row = [
            0,
            entry["name"],
            entry["nat"],
            entry["races"],
            entry["misses"],
            entry["miss_prone"],
            entry["miss_standing"],
            acc,
            prone_pct,
            standing_pct,
        ]
        rank_val = entry["misses"]
        rows.append({"rank_val": rank_val, "row": row})
    rows.sort(key=lambda r: (r["rank_val"], r["row"][1]))
    for idx, r in enumerate(rows, start=1):
        r["row"][0] = idx
    rows = _apply_limit(rows, args.limit)
    headers = [
        "Rank",
        "Biathlete",
        "Country",
        "Races",
        "Total Misses",
        "Total Prone",
        "Total Standing",
        "Accuracy %",
        "Prone %",
        "Standing %",
    ]
    render_rows = [r["row"] for r in rows]
    pretty = is_pretty_output(args)
    row_styles = [rank_style(r[0]) for r in render_rows] if pretty else None
    cell_formatters = (
        _build_accuracy_cell_formatters(headers, render_rows) if pretty else None
    )
    render_table(
        headers,
        render_rows,
        pretty=pretty,
        row_styles=row_styles,
        cell_formatters=cell_formatters,
    )
    return 0


def handle_cumulate_penalty(args: argparse.Namespace) -> int:
    """Cumulate penalty times."""
    try:
        payloads, season_id = _collect_races(args, allow_discipline=True)
    except BiathlonError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not payloads:
        print("no races found for the requested scope", file=sys.stderr)
        return 1

    include_relay = bool(getattr(args, "include_relay", False))

    # Prefetch analytic data for non-relay races (CRST and RNGT) in parallel
    analytic_requests: list[tuple[str, str]] = []
    for race_id, payload in payloads:
        if not _is_relay(payload):
            analytic_requests.append((race_id, "CRST"))
            analytic_requests.append((race_id, "RNGT"))
    prefetched_analytic = _prefetch_analytic_maps(analytic_requests)

    # Prefetch relay data in parallel
    prefetched_range_laps: dict[str, dict[tuple[str, int], dict[str, str]]] = {}
    prefetched_course_leg: dict[str, dict[tuple[str, int], float]] = {}
    if include_relay:
        relay_race_ids = [rid for rid, p in payloads if _is_relay(p)]
        if relay_race_ids:
            with ThreadPoolExecutor(
                max_workers=_max_workers(len(relay_race_ids) * 2, cap=8)
            ) as executor:
                lap_futures = {
                    executor.submit(_fetch_leg_lap_times, rid, "RNG", "", 8, 2): (
                        "laps",
                        rid,
                    )
                    for rid in relay_race_ids
                }
                course_futures = {
                    executor.submit(_fetch_leg_total_times, rid, "CRST"): (
                        "course",
                        rid,
                    )
                    for rid in relay_race_ids
                }
                all_futures: dict[Future, tuple[str, str]] = {**lap_futures, **course_futures}
                for future in as_completed(all_futures):
                    kind, rid = all_futures[future]
                    try:
                        if kind == "laps":
                            prefetched_range_laps[rid] = future.result()
                        else:
                            prefetched_course_leg[rid] = future.result()
                    except Exception:
                        if kind == "laps":
                            prefetched_range_laps[rid] = {}
                        else:
                            prefetched_course_leg[rid] = {}

    # Pre-fetch top IBU IDs once for filtering
    top_ibu_ids = _get_top_ibu_ids_set(payloads, args.top, season_id)

    entries: dict[str, dict] = {}
    total_races = 0
    for race_id, payload in payloads:
        is_relay = _is_relay(payload)
        if is_relay and not include_relay:
            continue
        if is_relay and include_relay:
            results = _relay_leg_results(payload)
        else:
            results = _race_results(payload)
        if not results:
            continue
        base_secs = base_time_seconds(results) if not is_relay else None
        cat_id = (payload.get("Competition") or {}).get("catId", "").upper()
        if not is_relay:
            results = _apply_top_filter(
                results, args.top, cat_id, season_id, top_ibu_ids
            )
        if not results:
            continue
        course_times = (
            prefetched_analytic.get((race_id, "CRST"), {}) if not is_relay else {}
        )
        range_times = (
            prefetched_analytic.get((race_id, "RNGT"), {}) if not is_relay else {}
        )
        range_laps = prefetched_range_laps.get(race_id, {}) if is_relay else {}
        course_leg_times = prefetched_course_leg.get(race_id, {}) if is_relay else {}
        leg_cumulative: dict[tuple[str, int], float] = {}
        if is_relay:
            for res in results:
                leg = res.get("Leg")
                if not isinstance(leg, int):
                    continue
                cum_val = get_first_time(
                    res, ["LegResult", "LegTime", "LegTimeTotal", "TotalTime", "Result"]
                )
                cum_secs = parse_time_seconds(cum_val) if cum_val else None
                if cum_secs is None:
                    continue
                for key in (
                    res.get("Bib"),
                    res.get("IBUId"),
                    res.get("Name"),
                    res.get("ShortName"),
                ):
                    if key:
                        leg_cumulative[(str(key), leg)] = cum_secs
        race_has_data = False
        for res in results:
            ident = res.get("IBUId") or res.get("Name") or res.get("ShortName") or ""
            if not ident:
                continue
            name = res.get("Name") or res.get("ShortName") or ""
            nat = res.get("Nat") or ""
            discipline = str(
                (payload.get("Competition") or {}).get("DisciplineId") or ""
            ).upper()
            if is_relay:
                leg_time = None
                leg_raw = res.get("Leg")
                leg = None
                if isinstance(leg_raw, int):
                    leg = leg_raw
                else:
                    try:
                        leg = int(str(leg_raw)) if leg_raw is not None else None
                    except ValueError:
                        leg = None
                if isinstance(leg, int):
                    for key in (
                        res.get("Bib"),
                        res.get("IBUId"),
                        res.get("Name"),
                        res.get("ShortName"),
                    ):
                        if key is None:
                            continue
                        curr = leg_cumulative.get((str(key), leg))
                        if curr is None:
                            continue
                        if leg > 1:
                            prev = leg_cumulative.get((str(key), leg - 1))
                            if prev is not None:
                                leg_time = curr - prev
                                break
                        else:
                            leg_time = curr
                            break
                if leg_time is None:
                    leg_time = parse_time_seconds(
                        get_first_time(
                            res, ["LegTime", "LegResult", "TotalTime", "Result"]
                        )
                    )
                course_val = None
                if isinstance(leg, int):
                    for key in (
                        res.get("Bib"),
                        res.get("IBUId"),
                        res.get("Name"),
                        res.get("ShortName"),
                    ):
                        if key is None:
                            continue
                        course_val = course_leg_times.get((str(key), leg))
                        if course_val is not None:
                            break
                if course_val is None:
                    course_val = parse_time_seconds(
                        get_first_time(res, ["LegCourse", "LegRunTime", "LegSkiTime"])
                    )
                range_val = None
                if isinstance(leg, int):
                    for key in (
                        res.get("Bib"),
                        res.get("IBUId"),
                        res.get("Name"),
                        res.get("ShortName"),
                    ):
                        if key is None:
                            continue
                        laps = range_laps.get((str(key), leg))
                        if laps:
                            r1 = parse_time_seconds(laps.get("lap1"))
                            r2 = parse_time_seconds(laps.get("lap2"))
                            if r1 is not None and r2 is not None:
                                range_val = r1 + r2
                                break
                if range_val is None:
                    r1 = parse_time_seconds(get_first_time(res, ["R1", "Range1"]))
                    r2 = parse_time_seconds(get_first_time(res, ["R2", "Range2"]))
                    if r1 is not None and r2 is not None:
                        range_val = r1 + r2
                secs = None
                if (
                    leg_time is not None
                    and course_val is not None
                    and range_val is not None
                ):
                    secs = leg_time - course_val - range_val
            else:
                if discipline == "IN":
                    misses = _parse_shootings(res.get("ShootingTotal"))
                    secs = float(sum(misses) * 60) if misses else None
                elif discipline == "PU":
                    result_val = result_seconds(res, base_secs)
                    delay = (
                        parse_time_seconds(res.get("StartInfo"))
                        if res.get("StartInfo")
                        else None
                    )
                    if result_val is None or delay is None:
                        secs = None
                    else:
                        base_val = result_val - delay
                        course_val = parse_time_seconds(
                            _lookup_analytic_time(course_times, res)
                            or get_first_time(
                                res, ["TotalCourseTime", "CourseTime", "RunTime"]
                            )
                        )
                        range_val = parse_time_seconds(
                            _lookup_analytic_time(range_times, res)
                            or get_first_time(res, ["TotalRangeTime", "RangeTime"])
                        )
                        if course_val is None or range_val is None:
                            secs = None
                        else:
                            secs = base_val - course_val - range_val
                else:
                    result_val = result_seconds(res, base_secs)
                    course_val = parse_time_seconds(
                        _lookup_analytic_time(course_times, res)
                        or get_first_time(
                            res, ["TotalCourseTime", "CourseTime", "RunTime"]
                        )
                    )
                    range_val = parse_time_seconds(
                        _lookup_analytic_time(range_times, res)
                        or get_first_time(res, ["TotalRangeTime", "RangeTime"])
                    )
                    if result_val is None or course_val is None or range_val is None:
                        secs = None
                    else:
                        secs = result_val - course_val - range_val
            if secs is None or secs < 0:
                continue
            entry = _aggregate_entries(entries, str(ident), name, nat)
            entry["races"] += 1
            entry["total_secs"] += secs
            race_has_data = True
        if race_has_data:
            total_races += 1

    rows = []
    for entry in entries.values():
        if entry["races"] != total_races:
            continue
        rows.append(
            {
                "rank_val": entry["total_secs"],
                "row": [
                    0,
                    entry["name"],
                    entry["nat"],
                    entry["races"],
                    format_seconds(entry["total_secs"]),
                ],
            }
        )
    rows.sort(key=lambda r: (r["rank_val"], r["row"][1]))
    for idx, row in enumerate(rows, start=1):
        row["row"][0] = idx
    rows = _apply_limit(rows, args.limit)
    headers = ["Rank", "Biathlete", "Country", "Races", "Total Penalty Time"]
    row_styles = (
        [rank_style(r["row"][0]) for r in rows] if is_pretty_output(args) else None
    )
    render_table(
        headers,
        [r["row"] for r in rows],
        pretty=is_pretty_output(args),
        row_styles=row_styles,
    )
    return 0


def handle_cumulate_cleansheet(args: argparse.Namespace) -> int:
    """Cumulate clean shooting stages (5/5)."""
    try:
        payloads, season_id = _collect_races(args, allow_discipline=True)
    except BiathlonError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not payloads:
        print("no races found for the requested scope", file=sys.stderr)
        return 1

    include_relay = bool(getattr(args, "include_relay", False))

    # Prefetch per-stage shooting and range time analytics for non-relay races
    analytic_requests: list[tuple[str, str]] = []
    for race_id, payload in payloads:
        if _is_relay(payload):
            continue
        disc = str((payload.get("Competition") or {}).get("DisciplineId") or "").upper()
        n_disc_stages = 2 if disc == "SP" else 4
        for s in range(1, n_disc_stages + 1):
            analytic_requests.append((race_id, f"S{s}TM"))
            analytic_requests.append((race_id, f"RNG{s}"))
    prefetched_analytic = _prefetch_analytic_maps(analytic_requests)

    # Prefetch relay shooting and range lap times
    prefetched_relay_laps: dict[str, dict[tuple[str, int], dict[str, str]]] = {}
    prefetched_relay_range_laps: dict[str, dict[tuple[str, int], dict[str, str]]] = {}
    if include_relay:
        relay_race_ids = [rid for rid, p in payloads if _is_relay(p)]
        if relay_race_ids:
            with ThreadPoolExecutor(
                max_workers=_max_workers(len(relay_race_ids) * 2, cap=8)
            ) as executor:
                shoot_futures = {
                    executor.submit(_fetch_leg_lap_times, rid, "S", "TM", 8, 2): (
                        "shoot",
                        rid,
                    )
                    for rid in relay_race_ids
                }
                range_futures = {
                    executor.submit(_fetch_leg_lap_times, rid, "RNG", "", 8, 2): (
                        "range",
                        rid,
                    )
                    for rid in relay_race_ids
                }
                all_futures = {**shoot_futures, **range_futures}
                for future in as_completed(all_futures):
                    kind, rid = all_futures[future]
                    try:
                        if kind == "shoot":
                            prefetched_relay_laps[rid] = future.result()
                        else:
                            prefetched_relay_range_laps[rid] = future.result()
                    except Exception:
                        if kind == "shoot":
                            prefetched_relay_laps[rid] = {}
                        else:
                            prefetched_relay_range_laps[rid] = {}

    # Pre-fetch top IBU IDs once for filtering
    top_ibu_ids = _get_top_ibu_ids_set(payloads, args.top, season_id)

    entries: dict[str, dict] = {}
    total_races = 0
    for race_id, payload in payloads:
        is_relay = _is_relay(payload)
        if is_relay and not include_relay:
            continue
        if is_relay and include_relay:
            results = _relay_leg_results(payload)
        else:
            results = _race_results(payload)
        if not results:
            continue
        cat_id = (payload.get("Competition") or {}).get("catId", "").upper()
        if not is_relay:
            results = _apply_top_filter(
                results, args.top, cat_id, season_id, top_ibu_ids
            )
        if not results:
            continue
        relay_laps = (
            prefetched_relay_laps.get(race_id, {}) if is_relay and include_relay else {}
        )
        relay_range_laps = (
            prefetched_relay_range_laps.get(race_id, {})
            if is_relay and include_relay
            else {}
        )
        race_has_data = False
        for res in results:
            ident = res.get("IBUId") or res.get("Name") or res.get("ShortName") or ""
            if not ident:
                continue
            name = res.get("Name") or res.get("ShortName") or ""
            nat = res.get("Nat") or ""
            entry = _aggregate_entries(entries, str(ident), name, nat)
            if is_relay:
                shootings = res.get("Shootings") or res.get("ShootingTotal")
                stages = parse_relay_shootings(shootings) if shootings else None
                if not stages:
                    continue
                prone, standing = stages
                prone_pen, prone_spare = prone
                stand_pen, stand_spare = standing
                prone_clean = prone_pen == 0 and prone_spare == 0
                stand_clean = stand_pen == 0 and stand_spare == 0
                clean_count = int(prone_clean) + int(stand_clean)
                entry.setdefault("cleansheets", 0)
                entry.setdefault("clean_races", 0)
                entry.setdefault("total_stages", 0)
                entry.setdefault("clean_stage_shoot_secs", 0.0)
                entry.setdefault("clean_stage_shoot_count", 0)
                entry.setdefault("clean_stage_range_secs", 0.0)
                entry.setdefault("clean_stage_range_count", 0)
                entry.setdefault("clean_race_shoot_secs", 0.0)
                entry.setdefault("clean_race_shoot_stages", 0)
                entry.setdefault("clean_race_range_secs", 0.0)
                entry.setdefault("clean_race_range_stages", 0)
                entry["cleansheets"] += clean_count
                entry["total_stages"] += 2
                entry["races"] += 1
                if prone_clean and stand_clean:
                    entry["clean_races"] += 1
                # Accumulate per-stage clean shooting and range times
                leg = res.get("Leg")
                lap_times: dict[str, str] = {}
                range_lap_times: dict[str, str] = {}
                if isinstance(leg, int):
                    for key in (
                        res.get("Bib"),
                        res.get("IBUId"),
                        res.get("Name"),
                        res.get("ShortName"),
                    ):
                        if key is None:
                            continue
                        lap_times = relay_laps.get((str(key), leg), {})
                        if lap_times:
                            break
                    for key in (
                        res.get("Bib"),
                        res.get("IBUId"),
                        res.get("Name"),
                        res.get("ShortName"),
                    ):
                        if key is None:
                            continue
                        range_lap_times = relay_range_laps.get((str(key), leg), {})
                        if range_lap_times:
                            break
                race_shoot_sum = 0.0
                race_shoot_ok = True
                race_range_sum = 0.0
                race_range_ok = True
                if prone_clean:
                    s1 = parse_time_seconds(lap_times.get("lap1"))
                    if s1 is None:
                        s1_val = get_first_time(res, ["S1", "ShootingTime1"])
                        s1 = parse_time_seconds(s1_val) if s1_val else None
                    if s1 is not None:
                        entry["clean_stage_shoot_secs"] += s1
                        entry["clean_stage_shoot_count"] += 1
                        race_shoot_sum += s1
                    else:
                        race_shoot_ok = False
                    r1 = parse_time_seconds(range_lap_times.get("lap1"))
                    if r1 is None:
                        r1_val = get_first_time(res, ["R1", "RangeTime1"])
                        r1 = parse_time_seconds(r1_val) if r1_val else None
                    if r1 is not None:
                        entry["clean_stage_range_secs"] += r1
                        entry["clean_stage_range_count"] += 1
                        race_range_sum += r1
                    else:
                        race_range_ok = False
                if stand_clean:
                    s2 = parse_time_seconds(lap_times.get("lap2"))
                    if s2 is None:
                        s2_val = get_first_time(res, ["S2", "ShootingTime2"])
                        s2 = parse_time_seconds(s2_val) if s2_val else None
                    if s2 is not None:
                        entry["clean_stage_shoot_secs"] += s2
                        entry["clean_stage_shoot_count"] += 1
                        race_shoot_sum += s2
                    else:
                        race_shoot_ok = False
                    r2 = parse_time_seconds(range_lap_times.get("lap2"))
                    if r2 is None:
                        r2_val = get_first_time(res, ["R2", "RangeTime2"])
                        r2 = parse_time_seconds(r2_val) if r2_val else None
                    if r2 is not None:
                        entry["clean_stage_range_secs"] += r2
                        entry["clean_stage_range_count"] += 1
                        race_range_sum += r2
                    else:
                        race_range_ok = False
                if prone_clean and stand_clean:
                    if race_shoot_ok:
                        entry["clean_race_shoot_secs"] += race_shoot_sum
                        entry["clean_race_shoot_stages"] += 2
                    if race_range_ok:
                        entry["clean_race_range_secs"] += race_range_sum
                        entry["clean_race_range_stages"] += 2
                race_has_data = True
            else:
                misses = _parse_shootings(
                    res.get("Shootings") or res.get("ShootingTotal")
                )
                if not misses:
                    continue
                clean_count = sum(1 for m in misses if m == 0)
                entry.setdefault("cleansheets", 0)
                entry.setdefault("clean_races", 0)
                entry.setdefault("total_stages", 0)
                entry.setdefault("clean_stage_shoot_secs", 0.0)
                entry.setdefault("clean_stage_shoot_count", 0)
                entry.setdefault("clean_stage_range_secs", 0.0)
                entry.setdefault("clean_stage_range_count", 0)
                entry.setdefault("clean_race_shoot_secs", 0.0)
                entry.setdefault("clean_race_shoot_stages", 0)
                entry.setdefault("clean_race_range_secs", 0.0)
                entry.setdefault("clean_race_range_stages", 0)
                n_stages = len(misses)
                entry["cleansheets"] += clean_count
                entry["total_stages"] += n_stages
                entry["races"] += 1
                fully_clean = all(m == 0 for m in misses)
                if fully_clean:
                    entry["clean_races"] += 1
                race_shoot_sum = 0.0
                race_shoot_ok = True
                race_range_sum = 0.0
                race_range_ok = True
                for stage_i, miss_count in enumerate(misses):
                    if miss_count == 0:
                        stage_times = prefetched_analytic.get(
                            (race_id, f"S{stage_i + 1}TM"), {}
                        )
                        stage_val = _lookup_analytic_time(stage_times, res)
                        stage_secs = (
                            parse_time_seconds(stage_val) if stage_val else None
                        )
                        if stage_secs is not None:
                            entry["clean_stage_shoot_secs"] += stage_secs
                            entry["clean_stage_shoot_count"] += 1
                            race_shoot_sum += stage_secs
                        else:
                            race_shoot_ok = False
                        range_times = prefetched_analytic.get(
                            (race_id, f"RNG{stage_i + 1}"), {}
                        )
                        range_val = _lookup_analytic_time(range_times, res)
                        range_secs = (
                            parse_time_seconds(range_val) if range_val else None
                        )
                        if range_secs is not None:
                            entry["clean_stage_range_secs"] += range_secs
                            entry["clean_stage_range_count"] += 1
                            race_range_sum += range_secs
                        else:
                            race_range_ok = False
                if fully_clean:
                    if race_shoot_ok:
                        entry["clean_race_shoot_secs"] += race_shoot_sum
                        entry["clean_race_shoot_stages"] += n_stages
                    if race_range_ok:
                        entry["clean_race_range_secs"] += race_range_sum
                        entry["clean_race_range_stages"] += n_stages
                race_has_data = True
        if race_has_data:
            total_races += 1

    if not entries:
        print("no shooting data found for the requested scope", file=sys.stderr)
        return 1

    min_pct = getattr(args, "min_pct", 0)
    min_races = 0
    if min_pct > 0 and total_races > 0:
        min_races = max(1, round(total_races * min_pct / 100))

    rows = []
    for entry in entries.values():
        if min_races and entry["races"] < min_races:
            continue
        races = entry["races"]
        clean_races = entry.get("clean_races", 0)
        cleansheets = entry.get("cleansheets", 0)
        total_stages = entry.get("total_stages", 0)
        stage_clean_pct = format_pct(cleansheets, total_stages) if total_stages else "-"
        race_clean_pct = format_pct(clean_races, races) if races else "-"
        # Stage-level averages (across all individual clean stages)
        clean_stage_shoot_count = entry.get("clean_stage_shoot_count", 0)
        avg_stage_shoot = (
            format_seconds(entry["clean_stage_shoot_secs"] / clean_stage_shoot_count)
            if clean_stage_shoot_count > 0
            else "-"
        )
        clean_stage_range_count = entry.get("clean_stage_range_count", 0)
        avg_stage_range = (
            format_seconds(entry["clean_stage_range_secs"] / clean_stage_range_count)
            if clean_stage_range_count > 0
            else "-"
        )
        # Race-level averages (total time / n_stages, so normalized per stage)
        cr_shoot_stages = entry.get("clean_race_shoot_stages", 0)
        avg_race_shoot = (
            format_seconds(entry["clean_race_shoot_secs"] / cr_shoot_stages)
            if cr_shoot_stages > 0
            else "-"
        )
        cr_range_stages = entry.get("clean_race_range_stages", 0)
        avg_race_range = (
            format_seconds(entry["clean_race_range_secs"] / cr_range_stages)
            if cr_range_stages > 0
            else "-"
        )
        stage_clean_ratio = cleansheets / total_stages if total_stages else 0.0
        race_clean_ratio = clean_races / races if races else 0.0
        avg_stage_shoot_secs = (
            entry["clean_stage_shoot_secs"] / clean_stage_shoot_count
            if clean_stage_shoot_count > 0
            else float("inf")
        )
        avg_stage_range_secs = (
            entry["clean_stage_range_secs"] / clean_stage_range_count
            if clean_stage_range_count > 0
            else float("inf")
        )
        avg_race_shoot_secs = (
            entry["clean_race_shoot_secs"] / cr_shoot_stages
            if cr_shoot_stages > 0
            else float("inf")
        )
        avg_race_range_secs = (
            entry["clean_race_range_secs"] / cr_range_stages
            if cr_range_stages > 0
            else float("inf")
        )
        rows.append(
            {
                "sort_cleansheets": (-cleansheets, -stage_clean_ratio),
                "sort_percentage": (-stage_clean_ratio, -cleansheets),
                "sort_time": (avg_stage_shoot_secs,),
                "stage_clean_ratio": stage_clean_ratio,
                "race_clean_ratio": race_clean_ratio,
                "avg_stage_shoot_secs": avg_stage_shoot_secs,
                "avg_stage_range_secs": avg_stage_range_secs,
                "avg_race_shoot_secs": avg_race_shoot_secs,
                "avg_race_range_secs": avg_race_range_secs,
                "row": [
                    0,
                    entry["name"],
                    entry["nat"],
                    races,
                    total_stages,
                    cleansheets,
                    stage_clean_pct,
                    avg_stage_shoot,
                    avg_stage_range,
                    clean_races,
                    race_clean_pct,
                    avg_race_shoot,
                    avg_race_range,
                ],
            }
        )
    sort_key = f"sort_{getattr(args, 'sort', 'cleansheets')}"
    rows.sort(key=lambda r: (r[sort_key], r["row"][1]))  # type: ignore[index,misc]
    for idx, r in enumerate(rows, start=1):
        r["row"][0] = idx  # type: ignore[index]
    rows = _apply_limit(rows, args.limit)
    headers = [
        "Rank",
        "Biathlete",
        "Country",
        "Races",
        "Stages",
        "Clean Stages",
        "Stage %",
        "Avg Stage Shoot",
        "Avg Stage Range",
        "Clean Races",
        "Race %",
        "Avg Race Shoot",
        "Avg Race Range",
    ]
    render_rows = [r["row"] for r in rows]
    pretty = is_pretty_output(args)
    row_styles = [rank_style(r[0]) for r in render_rows] if pretty else None
    # Column separators before the stage and race sections
    column_separators = {5, 9} if pretty else None
    # Highlight the header used for sorting
    sort_header_map = {
        "sort_cleansheets": "Clean Stages",
        "sort_percentage": "Stage %",
        "sort_time": "Avg Stage Shoot",
    }
    highlight_headers = None
    if pretty:
        sort_col = sort_header_map.get(sort_key)
        if sort_col and sort_col in headers:
            highlight_headers = [headers.index(sort_col)]

    # Build relative color-scale cell formatters
    cell_formatters: list[Callable[..., Any] | None] | None = None
    if pretty and rows:
        cell_formatters = [None] * len(headers)

        def _make_higher_better(values: list[float]):
            lo, hi = min(values), max(values)

            def fmt(cell_str: str, row_idx: int) -> str:
                if row_idx >= len(values):
                    return cell_str
                t = (values[row_idx] - lo) / (hi - lo) if hi > lo else 1.0
                return Color.relative(cell_str, t)

            return fmt if hi > lo else None

        def _make_lower_better(key: str):
            finite = [r[key] for r in rows if r[key] != float("inf")]
            if not finite:
                return None
            lo, hi = min(finite), max(finite)
            if hi <= lo:
                return None

            def fmt(cell_str: str, row_idx: int) -> str:
                if row_idx >= len(rows):
                    return cell_str
                val = rows[row_idx][key]
                if val == float("inf"):
                    return cell_str
                return Color.relative(cell_str, 1.0 - (val - lo) / (hi - lo))

            return fmt

        # Stage section
        cell_formatters[headers.index("Clean Stages")] = _make_higher_better(
            [float(r["row"][headers.index("Clean Stages")]) for r in rows]
        )
        cell_formatters[headers.index("Stage %")] = _make_higher_better(
            [r["stage_clean_ratio"] for r in rows]
        )
        cell_formatters[headers.index("Avg Stage Shoot")] = _make_lower_better(
            "avg_stage_shoot_secs"
        )
        cell_formatters[headers.index("Avg Stage Range")] = _make_lower_better(
            "avg_stage_range_secs"
        )
        # Race section
        cell_formatters[headers.index("Clean Races")] = _make_higher_better(
            [float(r["row"][headers.index("Clean Races")]) for r in rows]
        )
        cell_formatters[headers.index("Race %")] = _make_higher_better(
            [r["race_clean_ratio"] for r in rows]
        )
        cell_formatters[headers.index("Avg Race Shoot")] = _make_lower_better(
            "avg_race_shoot_secs"
        )
        cell_formatters[headers.index("Avg Race Range")] = _make_lower_better(
            "avg_race_range_secs"
        )

    render_table(
        headers,
        render_rows,
        pretty=pretty,
        row_styles=row_styles,
        cell_formatters=cell_formatters,
        column_separators=column_separators,
        highlight_headers=highlight_headers,
    )
    return 0


def handle_cumulate_remontada(args: argparse.Namespace) -> int:
    """Cumulate pursuit gains and per-location columns."""
    if args.discipline != "all":
        print("error: --discipline is not supported for remontada", file=sys.stderr)
        return 1
    try:
        payloads, season_id = _collect_races(
            args,
            allow_discipline=True,
            discipline_override="pursuit",
            allow_event=False,
        )
    except BiathlonError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not payloads:
        print("no pursuit races found", file=sys.stderr)
        return 1

    race_list = []
    for _, payload in payloads:
        results = extract_results(payload)
        if not results:
            continue
        comp = payload.get("Competition") or {}
        start = comp.get("StartTime") or comp.get("StartDate") or ""
        label = _event_label(payload) or "Pursuit"
        race_list.append((start, label, payload))
    race_list.sort(key=lambda x: x[0])

    labels: list[str] = []
    label_counts: dict[str, int] = {}
    race_payloads: list[dict] = []
    for _, label, payload in race_list:
        count = label_counts.get(label, 0) + 1
        label_counts[label] = count
        uniq = label if count == 1 else f"{label} {count}"
        labels.append(uniq)
        race_payloads.append(payload)

    # Pre-fetch top IBU IDs once for filtering
    top_ibu_ids = _get_top_ibu_ids_set(payloads, args.top, season_id)

    entries: dict[str, dict] = {}
    for label, payload in zip(labels, race_payloads):
        results = extract_results(payload)
        cat_id = (payload.get("Competition") or {}).get("catId", "").upper()
        results = _apply_top_filter(results, args.top, cat_id, season_id, top_ibu_ids)
        for res in results:
            status = _status_label(res)
            start_rank = res.get("StartOrder") or res.get("StartPosition")
            finish_rank = res.get("Rank") or res.get("ResultOrder")
            try:
                gain = int(start_rank) - int(finish_rank)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                gain = None
            ident = res.get("IBUId") or res.get("Name") or res.get("ShortName") or ""
            if not ident:
                continue
            name = res.get("Name") or res.get("ShortName") or ""
            nat = res.get("Nat") or ""
            entry = _aggregate_entries(entries, str(ident), name, nat)
            if status:
                entry["gains"][label] = status
                continue
            if gain is None:
                continue
            entry["races"] += 1
            entry["total_gain"] += gain
            entry["gains"][label] = gain

    rows = []
    for entry in entries.values():
        if entry["races"] == 0:
            continue
        avg_gain = entry["total_gain"] / entry["races"] if entry["races"] else 0
        row = [
            0,
            entry["name"],
            entry["nat"],
            entry["races"],
            f"+{entry['total_gain']}"
            if entry["total_gain"] > 0
            else entry["total_gain"],
        ]
        for label in labels:
            gain_val = entry["gains"].get(label, "-")
            if isinstance(gain_val, int) and gain_val > 0:
                gain_val = f"+{gain_val}"
            row.append(gain_val)
        row.append(f"+{avg_gain:.1f}" if avg_gain > 0 else f"{avg_gain:.1f}")
        rows.append({"rank_val": -entry["total_gain"], "row": row})
    rows.sort(key=lambda r: (r["rank_val"], r["row"][1]))
    for idx, r in enumerate(rows, start=1):
        r["row"][0] = idx
    rows = _apply_limit(rows, args.limit)
    headers = ["Rank", "Biathlete", "Country", "Races", "Gain"]
    headers.extend(labels)
    headers.append("Average")
    pretty = is_pretty_output(args)
    row_styles = [rank_style(r["row"][0]) for r in rows] if pretty else None
    highlight_headers = [headers.index("Gain")] if pretty else None
    highlight_header_styles = {headers.index("Gain"): "highlight"} if pretty else None
    render_table(
        headers,
        [r["row"] for r in rows],
        pretty=pretty,
        row_styles=row_styles,
        highlight_headers=highlight_headers,
        highlight_header_styles=highlight_header_styles,
    )
    return 0
