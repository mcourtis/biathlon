"""Form command handler for showing recent athlete form based on course time ranks."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

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
    is_pretty_output,
    get_output_format,
    rank_style,
    render_table,
)
from ..utils import (
    get_first_time,
    get_race_start_key,
    parse_relay_shootings,
    parse_time_seconds,
)
from ._common import (
    _format_section_title,
    _max_workers,
    _row_ibu_id,
    is_mixed_relay as _is_mixed_relay,
    is_relay_discipline as _is_relay,
)
from .results import _get_top_n_ibu_ids, _get_wc_rows
from ._common import _select_race_interactive
from .startlist import _find_all_startlist_races


# Mapping from discipline names to codes
DISCIPLINE_NAME_TO_CODE = {
    "sprint": "SP",
    "pursuit": "PU",
    "individual": "IN",
    "mass-start": "MS",
    "massstart": "MS",
    "mass_start": "MS",
    "short-individual": "SI",
    "shortindividual": "SI",
    "short_individual": "SI",
}


def _venue_abbrev(event_name: str) -> str:
    """Extract a 3-letter venue abbreviation from an event name."""
    # Clean up the name
    name = event_name.strip()
    if not name:
        return "???"

    # Common venue mappings for biathlon locations
    venue_map = {
        "antholz": "Ant",
        "anterselva": "Ant",
        "ruhpolding": "Rup",
        "oberhof": "Obe",
        "hochfilzen": "Hoc",
        "oestersund": "Ost",
        "ostersund": "Ost",
        "östersund": "Ost",
        "kontiolahti": "Kon",
        "pokljuka": "Pok",
        "holmenkollen": "Hol",
        "oslo": "Hol",
        "nove mesto": "Nov",
        "novemesto": "Nov",
        "canmore": "Can",
        "soldier hollow": "Sol",
        "le grand bornand": "Lgb",
        "legrandbornand": "Lgb",
        "annecy": "Lgb",
        "pyeongchang": "Pye",
        "sochi": "Soc",
        "lenzerheide": "Len",
    }

    # Try to match against known venues (case-insensitive)
    name_lower = name.lower()
    for venue, abbrev in venue_map.items():
        if venue in name_lower:
            return abbrev

    # Fallback: use first 3 characters, capitalized
    return name[:3].capitalize()


def _disc_abbrev(discipline: str) -> str:
    """Return short discipline abbreviation."""
    return discipline.upper()[:2]


def _fetch_course_times(race_id: str) -> dict[str, float]:
    """Fetch course times (CRST) for a race, keyed by IBUId."""
    try:
        analytic = get_analytic_results(race_id, "CRST")
    except BiathlonError:
        return {}

    times: dict[str, float] = {}
    for res in analytic.get("Results", []):
        if res.get("IsTeam"):
            continue
        ibu_id = res.get("IBUId")
        if not ibu_id:
            continue
        time_str = get_first_time(res, ["TotalTime", "Result"])
        if time_str:
            secs = parse_time_seconds(time_str)
            if secs is not None and secs > 0:
                times[str(ibu_id)] = secs
    return times


def _fetch_leg_course_times(race_id: str) -> dict[tuple[str, int], float]:
    """Fetch relay leg course times keyed by (IBUId, Leg) -> seconds.

    Returns dict mapping (IBUId, Leg) to course time in seconds.
    Falls back to Bib or Name if IBUId is not available.
    """
    try:
        analytic = get_analytic_results(race_id, "CRST")
    except BiathlonError:
        return {}

    times: dict[tuple[str, int], float] = {}
    for res in analytic.get("Results", []):
        if res.get("IsTeam"):
            continue
        leg = res.get("Leg")
        if not isinstance(leg, int):
            continue
        time_str = get_first_time(res, ["TotalTime", "Result"])
        if not time_str:
            continue
        secs = parse_time_seconds(time_str)
        if secs is None or secs <= 0:
            continue
        # Use IBUId as primary key, fall back to Bib or Name
        key = res.get("IBUId") or res.get("Bib") or res.get("Name")
        if key:
            times[(str(key), leg)] = secs
    return times


def _should_include_relay(include_mode: str, discipline: str, category: str) -> bool:
    """Check if relay race should be included based on --include-relay mode."""
    if not include_mode:
        return False
    if include_mode == "all":
        return discipline in {RELAY_DISCIPLINE, SINGLE_MIXED_RELAY_DISCIPLINE}
    if include_mode == "single-mixed":
        return discipline == SINGLE_MIXED_RELAY_DISCIPLINE
    if include_mode == "mixed-relay":
        return discipline == RELAY_DISCIPLINE and category == RELAY_MIXED_CAT
    if include_mode == "relay":
        return discipline == RELAY_DISCIPLINE and category in {
            RELAY_MEN_CAT,
            RELAY_WOMEN_CAT,
        }
    return False


def _get_all_gender_ibu_ids(cat_id: str, season_id: str) -> set[str]:
    """Get all IBU IDs for athletes of a specific gender from WC standings."""
    # Use a high limit to get all athletes in standings
    return set(_get_top_n_ibu_ids(cat_id, 500, season_id))


def _compute_course_ranks(course_times: dict[str, float]) -> dict[str, int]:
    """Compute course time ranks from course times dict."""
    if not course_times:
        return {}

    # Sort by course time
    sorted_entries = sorted(course_times.items(), key=lambda x: x[1])

    # Assign ranks (handle ties by giving same rank)
    ranks: dict[str, int] = {}
    prev_time = None
    prev_rank = 0
    for i, (ibu_id, secs) in enumerate(sorted_entries, 1):
        if prev_time is not None and abs(secs - prev_time) < 0.05:
            # Same rank for ties (within 0.05 seconds)
            ranks[ibu_id] = prev_rank
        else:
            ranks[ibu_id] = i
            prev_rank = i
        prev_time = secs

    return ranks


def _compute_leg_course_ranks(
    leg_course_times: dict[tuple[str, int], float],
) -> dict[tuple[str, int], int]:
    """Compute course time ranks from relay leg course times dict.

    All leg performances are ranked together regardless of leg number.
    """
    if not leg_course_times:
        return {}

    # Sort by course time
    sorted_entries = sorted(leg_course_times.items(), key=lambda x: x[1])

    # Assign ranks (handle ties by giving same rank)
    ranks: dict[tuple[str, int], int] = {}
    prev_time = None
    prev_rank = 0
    for i, (key, secs) in enumerate(sorted_entries, 1):
        if prev_time is not None and abs(secs - prev_time) < 0.05:
            # Same rank for ties (within 0.05 seconds)
            ranks[key] = prev_rank
        else:
            ranks[key] = i
            prev_rank = i
        prev_time = secs

    return ranks


def _compute_shooting_accuracy(results: list[dict], is_relay: bool) -> dict[str, float]:
    """Compute shooting accuracy percentage for each athlete in a race.

    Returns dict mapping IBUId -> accuracy (0.0 to 100.0).
    """
    accuracy: dict[str, float] = {}
    for res in results:
        if res.get("IsTeam"):
            continue
        ibu_id = str(res.get("IBUId") or "")
        if not ibu_id:
            continue
        shootings = res.get("Shootings") or res.get("ShootingTotal")
        if not shootings:
            continue

        if is_relay:
            stages = parse_relay_shootings(shootings)
            if not stages:
                continue
            prone, standing = stages
            prone_pen, prone_spare = prone
            stand_pen, stand_spare = standing
            shots = (5 + prone_spare) + (5 + stand_spare)
            misses = (prone_pen + prone_spare) + (stand_pen + stand_spare)
        else:
            parts = [p.strip() for p in str(shootings).split("+") if p.strip()]
            if not parts:
                continue
            shots = len(parts) * 5
            misses = 0
            for part in parts:
                try:
                    misses += int(part)
                except ValueError:
                    pass

        if shots > 0:
            accuracy[ibu_id] = 100.0 * (shots - misses) / shots
    return accuracy


def _get_race_info(payload: dict) -> tuple[str, str]:
    """Extract discipline and venue abbreviation from race payload."""
    comp = payload.get("Competition") or {}
    discipline = str(comp.get("DisciplineId") or "").upper()
    category = str(comp.get("catId") or comp.get("CatId") or "").upper()

    sport_evt = payload.get("SportEvt") or {}
    venue = sport_evt.get("ShortDescription") or sport_evt.get("Organizer") or ""

    # Use MX for mixed relay
    if discipline == RELAY_DISCIPLINE and category == RELAY_MIXED_CAT:
        disc_abbrev = "MX"
    else:
        disc_abbrev = _disc_abbrev(discipline)

    return disc_abbrev, _venue_abbrev(venue)


def _get_athlete_info(res: dict) -> tuple[str, str, str]:
    """Extract IBUId, name, and nationality from a result entry."""
    ibu_id = str(res.get("IBUId") or "")
    name = res.get("Name") or res.get("ShortName") or ""
    nat = res.get("Nat") or ""
    return ibu_id, name, nat


def _is_valid_result(res: dict) -> bool:
    """Check if result is valid (not DNF/DNS/LAP)."""
    irm = str(res.get("IRM") or "").upper()
    if irm in {"DNS", "DNF", "LAP", "LAPPED"}:
        return False

    rank = res.get("Rank")
    if rank is None:
        return False

    # Rank 10000 typically means lapped
    try:
        if int(rank) == 10000:
            return False
    except (TypeError, ValueError):
        pass

    result_str = str(res.get("Result") or res.get("TotalTime") or "").upper()
    if "DNS" in result_str or "DNF" in result_str or "LAP" in result_str:
        return False

    return True


def _get_recent_event_ids(
    completed_race_ids: list[str],
    race_to_event: dict[str, str],
    num_events: int,
    all_candidate_ids: list[str] | None = None,
) -> set[str]:
    """Get the N most recent fully-completed event IDs based on race order.

    If all_candidate_ids is provided, only events where all candidate races
    are completed will be considered.
    """
    # Build sets of candidate and completed races per event
    if all_candidate_ids is not None:
        event_candidates: dict[str, set[str]] = {}
        for rid in all_candidate_ids:
            ev_id = race_to_event.get(rid)
            if ev_id:
                event_candidates.setdefault(ev_id, set()).add(rid)

        completed_set = set(completed_race_ids)
        fully_completed_events = {
            ev_id
            for ev_id, candidates in event_candidates.items()
            if candidates <= completed_set  # all candidates are completed
        }
    else:
        fully_completed_events = None

    seen_events: list[str] = []
    for rid in reversed(completed_race_ids):  # Most recent first
        ev_id = race_to_event.get(rid)
        if ev_id and ev_id not in seen_events:
            # Skip events that aren't fully completed
            if (
                fully_completed_events is not None
                and ev_id not in fully_completed_events
            ):
                continue
            seen_events.append(ev_id)
            if len(seen_events) >= num_events:
                break
    return set(seen_events)


@dataclass
class FormData:
    """Shared state from the data-fetching phase of the form command."""

    season_id: str
    completed_race_ids: list[str]
    season_race_ids: list[str]  # all completed races including --remove'd ones
    race_payloads: dict[str, dict]
    race_to_event: dict[str, str]
    race_is_relay: dict[str, bool]
    race_discipline: dict[str, str]
    race_category: dict[str, str]
    race_headers: list[str]
    gender_cat: str
    gender_ibu_ids: set[str]
    individual_race_ids: list[str]
    relay_race_ids: list[str]
    race_course_times: dict  # str -> dict[str, float]
    relay_leg_course_times: dict  # str -> dict[tuple[str, int], float]
    all_candidate_ids: list[str]


def _extract_startlist_ibu_ids(payload: dict) -> tuple[set[str], str]:
    """Extract IBU IDs and category from a race payload (startlist or results).

    Returns (ibu_ids, category) where category is 'SW', 'SM', 'MX', etc.
    """
    results = payload.get("Results", [])
    comp = payload.get("Competition") or {}
    cat_id = str(comp.get("catId") or comp.get("CatId") or "").upper()

    ibu_ids: set[str] = set()
    for res in results:
        if res.get("IsTeam"):
            continue
        ibu_id = str(res.get("IBUId") or "")
        if ibu_id:
            ibu_ids.add(ibu_id)

    return ibu_ids, cat_id


def _fetch_form_data(
    args: argparse.Namespace,
    gender_cat: str,
) -> FormData | None:
    """Fetch all form data (events, races, payloads, course times).

    Returns None on error (error already printed to stderr).
    """
    try:
        season_id = get_current_season_id()
    except BiathlonError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None

    # Get World Cup events for current season
    try:
        events = get_events(season_id, level=1)
    except BiathlonError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None

    if not events:
        print("no events found for current season", file=sys.stderr)
        return None

    # Get all races from all events in parallel
    event_ids: list[str] = [e["EventId"] for e in events if e.get("EventId")]
    all_races: list[dict] = []
    race_to_event: dict[str, str] = {}  # race_id -> event_id

    if len(event_ids) == 1:
        try:
            all_races = get_races(event_ids[0])
            for race in all_races:
                rid = race.get("RaceId") or race.get("Id")
                if rid:
                    race_to_event[rid] = event_ids[0]
        except BiathlonError:
            pass
    elif event_ids:
        with ThreadPoolExecutor(
            max_workers=_max_workers(len(event_ids), cap=8)
        ) as executor:
            futures = {executor.submit(get_races, ev_id): ev_id for ev_id in event_ids}
            for future in as_completed(futures):
                ev_id = futures[future]
                try:
                    races = future.result()
                    all_races.extend(races)
                    for race in races:
                        rid = race.get("RaceId") or race.get("Id")
                        if rid:
                            race_to_event[rid] = ev_id
                except BiathlonError:
                    continue

    if not all_races:
        print("no races found", file=sys.stderr)
        return None

    # Filter to individual disciplines for the selected gender
    include_relay_mode = (getattr(args, "include_relay", "") or "").lower()

    # Build set of disciplines to exclude
    remove_discs: set[str] = set()
    for name in getattr(args, "remove", []) or []:
        code = DISCIPLINE_NAME_TO_CODE.get(name.lower())
        if code:
            remove_discs.add(code)
            if code == "IN":
                remove_discs.add("SI")
        else:
            print(f"warning: unknown discipline '{name}', ignoring", file=sys.stderr)

    # Track race info: (race_id, start_time_key, is_relay, discipline, category)
    candidates: list[tuple[str, str, bool, str, str]] = []
    removed_candidate_ids: set[str] = set()
    for race in all_races:
        race_id = race.get("RaceId") or race.get("Id")
        if not race_id:
            continue

        disc = str(race.get("DisciplineId") or "").upper()
        cat = str(race.get("catId") or race.get("CatId") or "").upper()

        # Check for relay races
        if _is_relay(disc):
            if _should_include_relay(include_relay_mode, disc, cat):
                # For relays, allow MX category for mixed relays
                allow_mixed = (
                    include_relay_mode in {"mixed-relay", "single-mixed", "all"}
                    and cat == RELAY_MIXED_CAT
                )
                # For SR (single mixed relay), also allow if it passes include check
                allow_sr = disc == SINGLE_MIXED_RELAY_DISCIPLINE
                if cat == gender_cat or allow_mixed or allow_sr:
                    candidates.append(
                        (race_id, get_race_start_key(race), True, disc, cat)
                    )
            continue

        # Individual disciplines
        if disc not in INDIVIDUAL_DISCIPLINES:
            continue

        if cat != gender_cat:
            continue

        candidates.append((race_id, get_race_start_key(race), False, disc, cat))
        if disc in remove_discs:
            removed_candidate_ids.add(race_id)

    if not candidates:
        print("no races found for the selected category", file=sys.stderr)
        return None

    # Sort by start time (most recent first)
    candidates.sort(key=lambda x: x[1], reverse=True)
    # All candidate IDs for fetching (includes removed disciplines for season form)
    fetch_candidate_ids = [rid for rid, _, _, _, _ in candidates]
    # Filtered candidate IDs for event computation (excludes removed disciplines)
    all_candidate_ids = [
        rid for rid in fetch_candidate_ids if rid not in removed_candidate_ids
    ]
    # Track race metadata
    race_is_relay: dict[str, bool] = {
        rid: is_rel for rid, _, is_rel, _, _ in candidates
    }
    race_discipline: dict[str, str] = {rid: disc for rid, _, _, disc, _ in candidates}
    race_category: dict[str, str] = {rid: cat for rid, _, _, _, cat in candidates}

    # Fetch race results for all candidates in parallel to find completed races
    race_payloads: dict[str, dict] = {}

    with ThreadPoolExecutor(
        max_workers=_max_workers(len(fetch_candidate_ids), cap=8)
    ) as executor:
        payload_futures = {
            executor.submit(get_race_results, rid): rid for rid in fetch_candidate_ids
        }
        for fut in as_completed(payload_futures):
            rid = payload_futures[fut]
            try:
                race_payloads[rid] = fut.result()
            except BiathlonError:
                race_payloads[rid] = {}

    # Filter to races that have actual results (completed races), maintaining order
    completed_candidates = [
        rid
        for rid in fetch_candidate_ids
        if race_payloads.get(rid)
        and race_payloads[rid].get("IsResult")
        and race_payloads[rid].get("Results")
    ]

    if not completed_candidates:
        print("no completed races found", file=sys.stderr)
        return None

    # Use ALL completed races for display (most recent first in completed_candidates)
    # Reverse to have oldest first (leftmost column will be oldest, rightmost newest)
    all_completed_race_ids = list(reversed(completed_candidates))

    # Separate relay and individual races
    individual_race_ids = [
        rid for rid in all_completed_race_ids if not race_is_relay.get(rid)
    ]
    relay_race_ids = [rid for rid in all_completed_race_ids if race_is_relay.get(rid)]

    # Fetch course times
    race_course_times: dict[str, dict[str, float]] = {}
    relay_leg_course_times: dict[str, dict[tuple[str, int], float]] = {}
    if individual_race_ids:
        with ThreadPoolExecutor(
            max_workers=_max_workers(len(individual_race_ids), cap=8)
        ) as executor:
            ct_futures = {
                executor.submit(_fetch_course_times, rid): rid
                for rid in individual_race_ids
            }
            for fut in as_completed(ct_futures):
                rid = ct_futures[fut]
                try:
                    race_course_times[rid] = fut.result()
                except BiathlonError:
                    race_course_times[rid] = {}

    if relay_race_ids:
        with ThreadPoolExecutor(
            max_workers=_max_workers(len(relay_race_ids), cap=8)
        ) as executor:
            leg_futures = {
                executor.submit(_fetch_leg_course_times, rid): rid
                for rid in relay_race_ids
            }
            for leg_fut in as_completed(leg_futures):
                rid = leg_futures[leg_fut]
                try:
                    relay_leg_course_times[rid] = leg_fut.result()
                except BiathlonError:
                    relay_leg_course_times[rid] = {}

    # Season race IDs = all completed (including removed disciplines)
    season_race_ids = all_completed_race_ids
    # Display race IDs = excluding removed disciplines
    completed_race_ids = [
        rid for rid in all_completed_race_ids if rid not in removed_candidate_ids
    ]

    # Build column headers (disc-venue) for display races only
    race_headers: list[str] = []
    for rid in completed_race_ids:
        payload = race_payloads[rid]
        disc, venue = _get_race_info(payload)
        race_headers.append(f"{disc}-{venue}")

    # Get all IBU IDs for the target gender (for mixed relay filtering)
    gender_ibu_ids: set[str] = _get_all_gender_ibu_ids(gender_cat, season_id)

    return FormData(
        season_id=season_id,
        completed_race_ids=completed_race_ids,
        season_race_ids=season_race_ids,
        race_payloads=race_payloads,
        race_to_event=race_to_event,
        race_is_relay=race_is_relay,
        race_discipline=race_discipline,
        race_category=race_category,
        race_headers=race_headers,
        gender_cat=gender_cat,
        gender_ibu_ids=gender_ibu_ids,
        individual_race_ids=individual_race_ids,
        relay_race_ids=relay_race_ids,
        race_course_times=race_course_times,
        relay_leg_course_times=relay_leg_course_times,
        all_candidate_ids=all_candidate_ids,
    )


def _compute_athletes(
    data: FormData,
    args: argparse.Namespace,
    shoot_mode: bool,
    filter_ibu_ids: set[str] | None = None,
    result_mode: bool = False,
) -> list[dict] | None:
    """Compute athlete form scores.

    Returns list of athlete dicts with keys: ibu_id, name, nat,
    current_form, season_form, has_current_form, ranks.
    Returns None if no athletes found (error already printed).
    """
    num_races = getattr(args, "races", 5)
    num_events = getattr(args, "event", 0)
    season_mode = getattr(args, "season", False)

    completed_race_ids = data.completed_race_ids
    season_race_ids = data.season_race_ids
    race_payloads = data.race_payloads
    race_is_relay = data.race_is_relay
    race_discipline = data.race_discipline
    race_category = data.race_category
    individual_race_ids = data.individual_race_ids
    relay_race_ids = data.relay_race_ids
    gender_ibu_ids = data.gender_ibu_ids
    race_to_event = data.race_to_event
    all_candidate_ids = data.all_candidate_ids

    # Compute per-race data depending on mode
    race_ranks: dict[str, dict[str, int]] = {}
    relay_leg_ranks: dict[str, dict[tuple[str, int], int]] = {}
    race_accuracy: dict[str, dict[str, float]] = {}
    # result_mode: direct finish ranks per race
    race_result_ranks: dict[str, dict[str, int]] = {}
    relay_team_ranks: dict[str, dict[str, int]] = {}  # Bib -> team rank

    if result_mode:
        for rid in season_race_ids:
            payload = race_payloads.get(rid, {})
            results = payload.get("Results", [])
            is_relay = race_is_relay.get(rid, False)
            if is_relay:
                team_rank_map: dict[str, int] = {}
                for res in results:
                    if res.get("IsTeam"):
                        bib = str(res.get("Bib") or "")
                        rank = res.get("Rank")
                        if bib and rank and int(rank) != 10000:
                            team_rank_map[bib] = int(rank)
                relay_team_ranks[rid] = team_rank_map
            else:
                indiv_ranks: dict[str, int] = {}
                for res in results:
                    if res.get("IsTeam"):
                        continue
                    ibu_id = _row_ibu_id(res)
                    if not ibu_id:
                        continue
                    rank = res.get("Rank")
                    if rank and int(rank) != 10000:
                        indiv_ranks[ibu_id] = int(rank)
                race_result_ranks[rid] = indiv_ranks
    elif shoot_mode:
        for rid in season_race_ids:
            payload = race_payloads.get(rid, {})
            results = payload.get("Results", [])
            is_relay = race_is_relay.get(rid, False)
            race_accuracy[rid] = _compute_shooting_accuracy(results, is_relay)
    else:
        for rid in individual_race_ids:
            course_times = data.race_course_times.get(rid, {})
            race_ranks[rid] = _compute_course_ranks(course_times)

        for rid in relay_race_ids:
            leg_times = data.relay_leg_course_times.get(rid, {})
            discipline = race_discipline.get(rid, "")
            category = race_category.get(rid, "")
            is_mixed = _is_mixed_relay(discipline, category)

            if is_mixed:
                target_gender_times: dict[tuple[str, int], float] = {}
                for (key, leg), secs in leg_times.items():
                    if str(key) in gender_ibu_ids:
                        target_gender_times[(key, leg)] = secs
                relay_leg_ranks[rid] = _compute_leg_course_ranks(target_gender_times)
            else:
                relay_leg_ranks[rid] = _compute_leg_course_ranks(leg_times)

    # Get top N filter if specified
    top_n = getattr(args, "top", 0)
    top_ibu_ids: set[str] | None = None
    if top_n > 0:
        top_ibu_ids = set(_get_top_n_ibu_ids(data.gender_cat, top_n, data.season_id))

    # Build athlete entries: {IBUId -> {name, nat, ranks: {race_id: rank}}}
    # Use season_race_ids (includes --remove'd races) so season form is unaffected.
    athletes: dict[str, dict] = {}

    for rid in season_race_ids:
        payload = race_payloads[rid]
        results = payload.get("Results", [])
        is_relay = race_is_relay.get(rid, False)

        if is_relay:
            leg_ranks = relay_leg_ranks.get(rid, {})
            acc = race_accuracy.get(rid, {}) if shoot_mode else {}
            team_ranks = relay_team_ranks.get(rid, {}) if result_mode else {}
            discipline = race_discipline.get(rid, "")
            category = race_category.get(rid, "")
            is_mixed = _is_mixed_relay(discipline, category)

            for res in results:
                if res.get("IsTeam"):
                    continue

                ibu_id, name, nat = _get_athlete_info(res)
                if not ibu_id:
                    continue

                if top_ibu_ids is not None and ibu_id not in top_ibu_ids:
                    continue

                if filter_ibu_ids is not None and ibu_id not in filter_ibu_ids:
                    continue

                leg = res.get("Leg")
                if not isinstance(leg, int):
                    continue

                if is_mixed and ibu_id not in gender_ibu_ids:
                    continue

                if ibu_id not in athletes:
                    athletes[ibu_id] = {
                        "name": name,
                        "nat": nat,
                        "ranks": {},
                    }

                if not _is_valid_result(res):
                    continue

                if result_mode:
                    bib = str(res.get("Bib") or "")
                    if bib and bib in team_ranks:
                        athletes[ibu_id]["ranks"][rid] = team_ranks[bib]
                elif shoot_mode:
                    if ibu_id in acc:
                        athletes[ibu_id]["ranks"][rid] = acc[ibu_id]
                else:
                    rank = None
                    for key in (ibu_id, res.get("Bib"), name):
                        if key:
                            rank = leg_ranks.get((str(key), leg))
                            if rank is not None:
                                break
                    if rank is not None:
                        athletes[ibu_id]["ranks"][rid] = rank
        else:
            ranks = race_ranks.get(rid, {})
            acc = race_accuracy.get(rid, {}) if shoot_mode else {}
            result_ranks = race_result_ranks.get(rid, {}) if result_mode else {}
            for res in results:
                if res.get("IsTeam"):
                    continue

                ibu_id, name, nat = _get_athlete_info(res)
                if not ibu_id:
                    continue

                if top_ibu_ids is not None and ibu_id not in top_ibu_ids:
                    continue

                if filter_ibu_ids is not None and ibu_id not in filter_ibu_ids:
                    continue

                if ibu_id not in athletes:
                    athletes[ibu_id] = {
                        "name": name,
                        "nat": nat,
                        "ranks": {},
                    }

                if not _is_valid_result(res):
                    continue

                if result_mode:
                    if ibu_id in result_ranks:
                        athletes[ibu_id]["ranks"][rid] = result_ranks[ibu_id]
                elif shoot_mode:
                    if ibu_id in acc:
                        athletes[ibu_id]["ranks"][rid] = acc[ibu_id]
                else:
                    if ibu_id in ranks:
                        athletes[ibu_id]["ranks"][rid] = ranks[ibu_id]

    if not athletes:
        print("no athletes found", file=sys.stderr)
        return None

    # Build WC standings rank map
    wc_rank_map: dict[str, int] = {}
    for row in _get_wc_rows(data.gender_cat, data.season_id):
        ibu_id = _row_ibu_id(row)
        if not ibu_id:
            continue
        try:
            rank = int(row.get("Rank") or row.get("rank") or 0)
        except (TypeError, ValueError):
            continue
        if rank > 0:
            wc_rank_map[ibu_id] = rank

    # Calculate form scores:
    # - Season Form: average of ALL race values for the athlete
    # - Current Form: average of the athlete's last N races they participated in
    # In shoot mode, values are accuracy percentages; in rank mode, course time ranks.
    result: list[dict] = []
    for ibu_id, entry in athletes.items():
        all_rank_values = list(entry["ranks"].values())
        if not all_rank_values:
            continue  # Skip athletes with no valid results

        season_form = sum(all_rank_values) / len(all_rank_values)

        # Current Form = average of the athlete's most recent N races or races from last N events
        if season_mode:
            athlete_recent_ranks = all_rank_values
        elif num_events > 0:
            recent_event_ids = _get_recent_event_ids(
                completed_race_ids, race_to_event, num_events, all_candidate_ids
            )
            athlete_recent_ranks = [
                entry["ranks"][rid]
                for rid in reversed(completed_race_ids)
                if rid in entry["ranks"] and race_to_event.get(rid) in recent_event_ids
            ]
        else:
            # Last N races globally, then average only the ones this athlete participated in
            recent_race_ids = completed_race_ids[-num_races:]
            athlete_recent_ranks = [
                entry["ranks"][rid] for rid in recent_race_ids if rid in entry["ranks"]
            ]

        has_current_form = bool(athlete_recent_ranks)
        if has_current_form:
            current_form = sum(athlete_recent_ranks) / len(athlete_recent_ranks)
        else:
            current_form = 0.0 if shoot_mode else float("inf")

        result.append(
            {
                "ibu_id": ibu_id,
                "name": entry["name"],
                "nat": entry["nat"],
                "wc_rank": wc_rank_map.get(ibu_id),
                "current_form": current_form,
                "season_form": season_form,
                "has_current_form": has_current_form,
                "ranks": entry["ranks"],
            }
        )

    # Filter by minimum participation percentage (based on display races only)
    min_pct = getattr(args, "min_pct", 75)
    if min_pct > 0 and completed_race_ids:
        display_race_set = set(completed_race_ids)
        total_display_races = len(completed_race_ids)
        result = [
            a
            for a in result
            if sum(1 for rid in a["ranks"] if rid in display_race_set)
            * 100
            / total_display_races
            >= min_pct
        ]

    if not result:
        print("no athletes found", file=sys.stderr)
        return None

    return result


def _render_form_table(
    athletes: list[dict],
    data: FormData,
    args: argparse.Namespace,
    shoot_mode: bool,
) -> int:
    """Sort, format, and render the form table. Returns 0."""
    season_mode = getattr(args, "season", False)
    num_races = getattr(args, "races", 5)
    num_events = getattr(args, "event", 0)

    completed_race_ids = data.completed_race_ids
    race_to_event = data.race_to_event
    all_candidate_ids = data.all_candidate_ids
    race_headers = data.race_headers

    # Build rows
    rows = []
    for entry in athletes:
        wc_rank = entry.get("wc_rank")
        wc_str = str(wc_rank) if wc_rank is not None else "-"

        if entry["has_current_form"]:
            if shoot_mode:
                current_form_str = f"{entry['current_form']:.1f}%"
            else:
                current_form_str = f"{entry['current_form']:.1f}"
        else:
            current_form_str = "-"

        if shoot_mode:
            season_form_str = f"{entry['season_form']:.1f}%"
        else:
            season_form_str = f"{entry['season_form']:.1f}"

        if season_mode:
            row_data = [
                0,  # Placeholder for rank
                entry["name"],
                entry["nat"],
                wc_str,
                season_form_str,
            ]
        else:
            row_data = [
                0,  # Placeholder for rank
                entry["name"],
                entry["nat"],
                wc_str,
                current_form_str,
                season_form_str,
            ]

        # Add race columns
        for rid in completed_race_ids:
            val = entry["ranks"].get(rid)
            if val is None:
                row_data.append("-")
            elif shoot_mode:
                row_data.append(f"{val:.1f}%")
            else:
                row_data.append(str(val))

        rows.append(
            {
                "season_form": entry["season_form"],
                "current_form": entry["current_form"],
                "name": entry["name"],
                "row": row_data,
                "_ranks": entry["ranks"],
            }
        )

    # Sort: shoot mode descending (higher accuracy = better), rank mode ascending
    if season_mode:
        if shoot_mode:
            rows.sort(key=lambda r: (-r["season_form"], r["name"]))
        else:
            rows.sort(key=lambda r: (r["season_form"], r["name"]))
    elif shoot_mode:
        rows.sort(key=lambda r: (-r["current_form"], -r["season_form"], r["name"]))
    else:
        rows.sort(key=lambda r: (r["current_form"], r["season_form"], r["name"]))

    # Assign ranks
    for idx, row in enumerate(rows, start=1):
        row["row"][0] = idx

    # Apply limit
    limit = getattr(args, "limit", 25)
    if limit > 0:
        rows = rows[:limit]

    # Build headers
    # Columns: Rank | Biathlete | Nat | WC | [Current |] Season | race...
    # In season mode, the "Current" column is dropped (redundant with "Season")
    race_col_offset = 5 if season_mode else 6
    current_col_idx = 4  # "Current" column index (only in non-season mode)
    if season_mode:
        headers = ["Rank", "Biathlete", "Nat", "WC", "Season"] + race_headers
    else:
        headers = ["Rank", "Biathlete", "Nat", "WC", "Current", "Season"] + race_headers

    # Determine which column headers to highlight (races used for current form)
    highlight_headers = None
    if season_mode:
        # All race columns contribute to form; highlight Season + all race cols
        highlight_headers = [4] + list(
            range(race_col_offset, race_col_offset + len(completed_race_ids))
        )
    elif num_events > 0:
        recent_event_ids = _get_recent_event_ids(
            completed_race_ids, race_to_event, num_events, all_candidate_ids
        )
        highlight_headers = [current_col_idx] + [
            race_col_offset + i
            for i, rid in enumerate(completed_race_ids)
            if race_to_event.get(rid) in recent_event_ids
        ]
    else:
        num_race_cols = len(completed_race_ids)
        start_idx = max(0, num_race_cols - num_races)
        highlight_headers = [current_col_idx] + list(
            range(race_col_offset + start_idx, race_col_offset + num_race_cols)
        )

    # Render table
    pretty = is_pretty_output(args)
    output_format = get_output_format(args)
    row_styles = [rank_style(r["row"][0]) for r in rows] if pretty else None

    # Build cell formatters for coloring
    # Skip Rank (0), Biathlete (1), Nat (2), WC (3) — start formatters at Current/Season
    cell_formatters: list[Callable | None] | None = None
    wc_col = 3
    if pretty:
        num_cols = len(headers)
        season_col = 4 if season_mode else 5
        current_col = -1 if season_mode else 4  # no current col in season mode

        if shoot_mode:

            def _make_cell_formatter(col_idx: int):
                """Create a cell formatter that applies accuracy coloring."""

                def _fmt(value, row_idx):
                    if row_idx < 0 or row_idx >= len(rows):
                        return value
                    if str(value) == "-":
                        return value
                    entry = rows[row_idx]
                    if col_idx == current_col:
                        pct = entry["current_form"]
                    elif col_idx == season_col:
                        pct = entry["season_form"]
                    else:
                        race_idx = col_idx - race_col_offset
                        if race_idx < 0 or race_idx >= len(completed_race_ids):
                            return value
                        rid = completed_race_ids[race_idx]
                        pct = entry["_ranks"].get(rid)
                    if pct is None:
                        return value
                    return Color.accuracy(str(value), pct / 100.0)

                return _fmt
        else:

            def _make_cell_formatter(col_idx: int):
                """Create a cell formatter that applies rank coloring."""

                def _fmt(value, row_idx):
                    if row_idx < 0 or row_idx >= len(rows):
                        return value
                    if str(value) == "-":
                        return value
                    entry = rows[row_idx]
                    if col_idx == current_col:
                        rank_val = entry["current_form"]
                    elif col_idx == season_col:
                        rank_val = entry["season_form"]
                    else:
                        race_idx = col_idx - race_col_offset
                        if race_idx < 0 or race_idx >= len(completed_race_ids):
                            return value
                        rid = completed_race_ids[race_idx]
                        rank_val = entry["_ranks"].get(rid)
                    if rank_val is None:
                        return value
                    # Convert rank to 0-1 scale: rank 1 → ~1.0 (green), rank 100 → 0.0 (red)
                    pct = max(0.0, (100.0 - rank_val) / 100.0)
                    return Color.accuracy(str(value), pct)

                return _fmt

        cell_formatters = [None] * num_cols  # type: ignore[assignment]
        # Skip WC column (index 3) — start color formatters at Current/Season
        for ci in range(wc_col + 1, num_cols):
            cell_formatters[ci] = _make_cell_formatter(ci)

    render_table(
        headers,
        [r["row"] for r in rows],
        output_format=output_format,
        row_styles=row_styles,
        highlight_headers=highlight_headers if pretty else None,
        cell_formatters=cell_formatters,
    )

    return 0


def _render_combined_table(
    course_athletes: list[dict],
    shoot_athletes: list[dict],
    args: argparse.Namespace,
    result_athletes: list[dict] | None = None,
) -> int:
    """Render combined course time + shooting (+ result) ranking table."""
    season_mode = getattr(args, "season", False)
    form_key = "season_form" if season_mode else "current_form"

    # Rank course time athletes (ascending — lower avg rank is better)
    course_sorted = sorted(course_athletes, key=lambda a: (a[form_key], a["name"]))
    course_rank: dict[str, int] = {
        a["ibu_id"]: i for i, a in enumerate(course_sorted, 1)
    }

    # Rank shooting athletes (descending — higher accuracy is better)
    shoot_sorted = sorted(shoot_athletes, key=lambda a: (-a[form_key], a["name"]))
    shoot_rank: dict[str, int] = {a["ibu_id"]: i for i, a in enumerate(shoot_sorted, 1)}

    # Rank result athletes (ascending — lower avg rank is better)
    result_rank: dict[str, int] = {}
    if result_athletes:
        result_sorted = sorted(result_athletes, key=lambda a: (a[form_key], a["name"]))
        result_rank = {a["ibu_id"]: i for i, a in enumerate(result_sorted, 1)}

    # Lookup for name/nat/wc_rank
    athlete_info: dict[str, tuple[str, str, int | None]] = {}
    for a in course_athletes:
        athlete_info[a["ibu_id"]] = (a["name"], a["nat"], a.get("wc_rank"))
    for a in shoot_athletes:
        athlete_info[a["ibu_id"]] = (a["name"], a["nat"], a.get("wc_rank"))

    # Combine athletes present in all rankings
    common_ids = set(course_rank) & set(shoot_rank)
    if result_rank:
        common_ids &= set(result_rank)
    if not common_ids:
        print("no athletes found for combined ranking", file=sys.stderr)
        return 1

    combined = []
    for ibu_id in common_ids:
        cr = course_rank[ibu_id]
        sr = shoot_rank[ibu_id]
        rr = result_rank.get(ibu_id, 0)
        name, nat, wc_rank = athlete_info[ibu_id]
        score = cr + sr + rr if result_rank else cr + sr
        combined.append(
            {
                "name": name,
                "nat": nat,
                "wc_rank": wc_rank,
                "score": score,
                "course_rank": cr,
                "shoot_rank": sr,
                "result_rank": rr,
            }
        )

    combined.sort(key=lambda x: (x["score"], x["name"]))

    # Assign ranks
    for i, entry in enumerate(combined, 1):
        entry["rank"] = i

    # Apply limit
    limit = getattr(args, "limit", 25)
    if limit > 0:
        combined = combined[:limit]

    if result_rank:
        headers = [
            "Rank",
            "Biathlete",
            "Nat",
            "WC",
            "Score",
            "Result",
            "Course",
            "Shooting",
        ]
        rows: list[list[str]] = [
            [
                str(e["rank"]),
                str(e["name"]),
                str(e["nat"]),
                str(e["wc_rank"]) if e["wc_rank"] is not None else "-",
                str(e["score"]),
                str(e["result_rank"]),
                str(e["course_rank"]),
                str(e["shoot_rank"]),
            ]
            for e in combined
        ]
    else:
        headers = ["Rank", "Biathlete", "Nat", "WC", "Score", "Course", "Shooting"]
        rows = [
            [
                str(e["rank"]),
                str(e["name"]),
                str(e["nat"]),
                str(e["wc_rank"]) if e["wc_rank"] is not None else "-",
                str(e["score"]),
                str(e["course_rank"]),
                str(e["shoot_rank"]),
            ]
            for e in combined
        ]

    pretty = is_pretty_output(args)
    output_format = get_output_format(args)
    row_styles = [rank_style(e["rank"]) for e in combined] if pretty else None

    render_table(
        headers,
        rows,
        output_format=output_format,
        row_styles=row_styles,
    )

    return 0


def _compute_and_render(
    data: FormData,
    args: argparse.Namespace,
    shoot_mode: bool,
    filter_ibu_ids: set[str] | None = None,
) -> int:
    """Compute ranks/accuracy and render the form table.

    Returns 0 on success, 1 on error.
    """
    athletes = _compute_athletes(data, args, shoot_mode, filter_ibu_ids)
    if athletes is None:
        return 1
    return _render_form_table(athletes, data, args, shoot_mode)


def handle_form(args: argparse.Namespace) -> int:
    """Handle form command - show recent athlete form based on course time ranks."""
    # Validate mutually exclusive flags
    num_races = getattr(args, "races", 5)
    num_events = getattr(args, "event", 0)
    season_mode = getattr(args, "season", False)
    if num_events > 0 and num_races != 5:  # 5 is the default
        print("error: --races and --event are mutually exclusive", file=sys.stderr)
        return 1
    if season_mode and num_races != 5:
        print("error: --season and --races are mutually exclusive", file=sys.stderr)
        return 1
    if season_mode and num_events > 0:
        print("error: --season and --event are mutually exclusive", file=sys.stderr)
        return 1

    startlist_flag = getattr(args, "startlist", None)

    if startlist_flag is not None:
        # --startlist mode: auto-detect gender, show both course time + shooting tables
        try:
            if startlist_flag:
                # Specific race ID provided
                payload = get_race_results(startlist_flag)
            else:
                # No race ID — discover available startlists
                candidates = _find_all_startlist_races()
                _race_id, payload = _select_race_interactive(candidates)
        except BiathlonError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

        startlist_ids, race_cat = _extract_startlist_ibu_ids(payload)

        if not startlist_ids:
            print("error: empty startlist for the given race", file=sys.stderr)
            return 1

        # Auto-detect gender from race category
        if race_cat in {"SW", "SM"}:
            gender_cat = race_cat
        else:
            # Mixed relay or unknown: fall back to --men flag
            gender_cat = (
                GENDER_TO_CAT["men"]
                if getattr(args, "men", False)
                else GENDER_TO_CAT["women"]
            )

        data = _fetch_form_data(args, gender_cat)
        if data is None:
            return 1

        # Compute athletes for all three modes
        course_athletes = _compute_athletes(
            data, args, shoot_mode=False, filter_ibu_ids=startlist_ids
        )
        if course_athletes is None:
            return 1
        shoot_athletes = _compute_athletes(
            data, args, shoot_mode=True, filter_ibu_ids=startlist_ids
        )
        if shoot_athletes is None:
            return 1
        result_athletes = _compute_athletes(
            data, args, shoot_mode=False, result_mode=True, filter_ibu_ids=startlist_ids
        )
        if result_athletes is None:
            return 1

        # Rank results table
        print(_format_section_title("Rank results", args))
        _render_form_table(result_athletes, data, args, shoot_mode=False)

        print()

        # Course time ranks table
        print(_format_section_title("Course time ranks", args))
        _render_form_table(course_athletes, data, args, shoot_mode=False)

        print()

        # Shooting accuracy table
        print(_format_section_title("Shooting accuracy", args))
        _render_form_table(shoot_athletes, data, args, shoot_mode=True)

        print()

        # Combined ranking table
        print(_format_section_title("Combined ranking", args))
        return _render_combined_table(
            course_athletes, shoot_athletes, args, result_athletes=result_athletes
        )

    # Standard mode (no --startlist flag)
    gender_cat = (
        GENDER_TO_CAT["men"] if getattr(args, "men", False) else GENDER_TO_CAT["women"]
    )

    data = _fetch_form_data(args, gender_cat)
    if data is None:
        return 1

    # Compute athletes for all three modes
    result_athletes = _compute_athletes(data, args, shoot_mode=False, result_mode=True)
    if result_athletes is None:
        return 1
    course_athletes = _compute_athletes(data, args, shoot_mode=False)
    if course_athletes is None:
        return 1
    shoot_athletes = _compute_athletes(data, args, shoot_mode=True)
    if shoot_athletes is None:
        return 1

    # Rank results table
    print(_format_section_title("Rank results", args))
    _render_form_table(result_athletes, data, args, shoot_mode=False)

    print()

    # Course time ranks table
    print(_format_section_title("Course time ranks", args))
    _render_form_table(course_athletes, data, args, shoot_mode=False)

    print()

    # Shooting accuracy table
    print(_format_section_title("Shooting accuracy", args))
    return _render_form_table(shoot_athletes, data, args, shoot_mode=True)
