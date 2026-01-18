"""Form command handler for showing recent athlete form based on course time ranks."""

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

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
from ..formatting import is_pretty_output, rank_style, render_table
from ..utils import get_first_time, get_race_start_key, parse_time_seconds
from .results import _get_top_n_ibu_ids


MAX_FETCH_WORKERS = 8

# Mapping from discipline names to codes
DISCIPLINE_NAME_TO_CODE = {
    "sprint": "SP",
    "pursuit": "PU",
    "individual": "IN",
    "mass-start": "MS",
    "massstart": "MS",
    "mass_start": "MS",
}


def _max_workers(total: int) -> int:
    """Return a capped worker count for concurrent fetches."""
    return min(MAX_FETCH_WORKERS, max(1, total))


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
        return discipline == RELAY_DISCIPLINE and category in {RELAY_MEN_CAT, RELAY_WOMEN_CAT}
    return False


def _is_relay(discipline: str) -> bool:
    """Return True if discipline is a relay type."""
    return discipline in {RELAY_DISCIPLINE, SINGLE_MIXED_RELAY_DISCIPLINE}


def _is_mixed_relay(discipline: str, category: str) -> bool:
    """Check if the race is a mixed relay (MX or SR)."""
    return (discipline == RELAY_DISCIPLINE and category == RELAY_MIXED_CAT) or \
           discipline == SINGLE_MIXED_RELAY_DISCIPLINE


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


def _compute_leg_course_ranks(leg_course_times: dict[tuple[str, int], float]) -> dict[tuple[str, int], int]:
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
) -> set[str]:
    """Get the N most recent event IDs based on race order."""
    seen_events: list[str] = []
    for rid in reversed(completed_race_ids):  # Most recent first
        ev_id = race_to_event.get(rid)
        if ev_id and ev_id not in seen_events:
            seen_events.append(ev_id)
            if len(seen_events) >= num_events:
                break
    return set(seen_events)


def handle_form(args: argparse.Namespace) -> int:
    """Handle form command - show recent athlete form based on course time ranks."""
    # Validate mutually exclusive flags
    num_races = getattr(args, "races", 5)
    num_events = getattr(args, "event", 0)
    if num_events > 0 and num_races != 5:  # 5 is the default
        print("error: --races and --event are mutually exclusive", file=sys.stderr)
        return 1

    try:
        season_id = get_current_season_id()
    except BiathlonError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # Get World Cup events for current season
    try:
        events = get_events(season_id, level=1)
    except BiathlonError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not events:
        print("no events found for current season", file=sys.stderr)
        return 1

    # Get all races from all events in parallel
    event_ids = [e.get("EventId") for e in events if e.get("EventId")]
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
        with ThreadPoolExecutor(max_workers=_max_workers(len(event_ids))) as executor:
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
        return 1

    # Filter to individual disciplines for the selected gender
    gender_cat = GENDER_TO_CAT["men"] if getattr(args, "men", False) else GENDER_TO_CAT["women"]
    include_relay_mode = (getattr(args, "include_relay", "") or "").lower()

    # Build set of disciplines to exclude
    remove_discs: set[str] = set()
    for name in getattr(args, "remove", []) or []:
        code = DISCIPLINE_NAME_TO_CODE.get(name.lower())
        if code:
            remove_discs.add(code)
        else:
            print(f"warning: unknown discipline '{name}', ignoring", file=sys.stderr)

    # Track race info: (race_id, start_time_key, is_relay, discipline, category)
    candidates: list[tuple[str, str, bool, str, str]] = []
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
                allow_mixed = include_relay_mode in {"mixed-relay", "single-mixed", "all"} and cat == RELAY_MIXED_CAT
                # For SR (single mixed relay), also allow if it passes include check
                allow_sr = disc == SINGLE_MIXED_RELAY_DISCIPLINE
                if cat == gender_cat or allow_mixed or allow_sr:
                    candidates.append((race_id, get_race_start_key(race), True, disc, cat))
            continue

        # Individual disciplines
        if disc not in INDIVIDUAL_DISCIPLINES:
            continue

        # Skip removed disciplines
        if disc in remove_discs:
            continue

        if cat != gender_cat:
            continue

        candidates.append((race_id, get_race_start_key(race), False, disc, cat))

    if not candidates:
        print("no races found for the selected category", file=sys.stderr)
        return 1

    # Sort by start time (most recent first)
    candidates.sort(key=lambda x: x[1], reverse=True)
    all_candidate_ids = [rid for rid, _, _, _, _ in candidates]
    # Track race metadata
    race_is_relay: dict[str, bool] = {rid: is_rel for rid, _, is_rel, _, _ in candidates}
    race_discipline: dict[str, str] = {rid: disc for rid, _, _, disc, _ in candidates}
    race_category: dict[str, str] = {rid: cat for rid, _, _, _, cat in candidates}

    # Fetch race results for all candidates in parallel to find completed races
    race_payloads: dict[str, dict] = {}

    with ThreadPoolExecutor(max_workers=_max_workers(len(all_candidate_ids))) as executor:
        futures = {
            executor.submit(get_race_results, rid): rid
            for rid in all_candidate_ids
        }
        for future in as_completed(futures):
            rid = futures[future]
            try:
                race_payloads[rid] = future.result()
            except BiathlonError:
                race_payloads[rid] = {}

    # Filter to races that have results (completed races), maintaining order
    completed_candidates = [
        rid for rid in all_candidate_ids
        if race_payloads.get(rid) and race_payloads[rid].get("Results")
    ]

    if not completed_candidates:
        print("no completed races found", file=sys.stderr)
        return 1

    # Use ALL completed races for display (most recent first in completed_candidates)
    # Reverse to have oldest first (leftmost column will be oldest, rightmost newest)
    all_completed_race_ids = list(reversed(completed_candidates))

    # Separate relay and individual races
    individual_race_ids = [rid for rid in all_completed_race_ids if not race_is_relay.get(rid)]
    relay_race_ids = [rid for rid in all_completed_race_ids if race_is_relay.get(rid)]

    # Fetch course times for individual races in parallel
    race_course_times: dict[str, dict[str, float]] = {}
    if individual_race_ids:
        with ThreadPoolExecutor(max_workers=_max_workers(len(individual_race_ids))) as executor:
            futures = {
                executor.submit(_fetch_course_times, rid): rid
                for rid in individual_race_ids
            }
            for future in as_completed(futures):
                rid = futures[future]
                try:
                    race_course_times[rid] = future.result()
                except BiathlonError:
                    race_course_times[rid] = {}

    # Fetch leg course times for relay races in parallel
    relay_leg_course_times: dict[str, dict[tuple[str, int], float]] = {}
    if relay_race_ids:
        with ThreadPoolExecutor(max_workers=_max_workers(len(relay_race_ids))) as executor:
            futures = {
                executor.submit(_fetch_leg_course_times, rid): rid
                for rid in relay_race_ids
            }
            for future in as_completed(futures):
                rid = futures[future]
                try:
                    relay_leg_course_times[rid] = future.result()
                except BiathlonError:
                    relay_leg_course_times[rid] = {}

    completed_race_ids = all_completed_race_ids

    if not completed_race_ids:
        print("no completed races found", file=sys.stderr)
        return 1

    # Build column headers (disc-venue) for each race
    race_headers: list[str] = []
    for rid in completed_race_ids:
        payload = race_payloads[rid]
        disc, venue = _get_race_info(payload)
        race_headers.append(f"{disc}-{venue}")

    # Compute course ranks for individual races
    race_ranks: dict[str, dict[str, int]] = {}
    for rid in individual_race_ids:
        course_times = race_course_times.get(rid, {})
        race_ranks[rid] = _compute_course_ranks(course_times)

    # Compute leg course ranks for relay races
    # For mixed relays, we need to compute separate ranks for women and men
    relay_leg_ranks: dict[str, dict[tuple[str, int], int]] = {}
    is_viewing_women = not getattr(args, "men", False)

    # Get all IBU IDs for the target gender (for mixed relay filtering)
    gender_ibu_ids: set[str] = _get_all_gender_ibu_ids(gender_cat, season_id)

    for rid in relay_race_ids:
        leg_times = relay_leg_course_times.get(rid, {})
        discipline = race_discipline.get(rid, "")
        category = race_category.get(rid, "")

        # Check if this is a mixed relay
        is_mixed = _is_mixed_relay(discipline, category)

        if is_mixed:
            # Split leg times by athlete gender (using IBU ID lookup)
            target_gender_times: dict[tuple[str, int], float] = {}
            for (key, leg), secs in leg_times.items():
                # Check if this athlete is of the target gender
                if str(key) in gender_ibu_ids:
                    target_gender_times[(key, leg)] = secs

            # Compute ranks only for target gender athletes
            relay_leg_ranks[rid] = _compute_leg_course_ranks(target_gender_times)
        else:
            # Standard relay - all same gender
            relay_leg_ranks[rid] = _compute_leg_course_ranks(leg_times)

    # Get top N filter if specified
    top_n = getattr(args, "top", 0)
    top_ibu_ids: set[str] | None = None
    if top_n > 0:
        top_ibu_ids = set(_get_top_n_ibu_ids(gender_cat, top_n, season_id))

    # Build athlete entries: {IBUId -> {name, nat, ranks: {race_id: rank}}}
    athletes: dict[str, dict] = {}

    for rid in completed_race_ids:
        payload = race_payloads[rid]
        results = payload.get("Results", [])
        is_relay = race_is_relay.get(rid, False)

        if is_relay:
            # For relay races, use leg results and leg course ranks
            leg_ranks = relay_leg_ranks.get(rid, {})
            discipline = race_discipline.get(rid, "")
            category = race_category.get(rid, "")

            # Check if this is a mixed relay
            is_mixed = _is_mixed_relay(discipline, category)

            for res in results:
                if res.get("IsTeam"):
                    continue

                ibu_id, name, nat = _get_athlete_info(res)
                if not ibu_id:
                    continue

                # Apply top filter
                if top_ibu_ids is not None and ibu_id not in top_ibu_ids:
                    continue

                leg = res.get("Leg")
                if not isinstance(leg, int):
                    continue

                # For mixed relays, skip athletes not of the target gender
                if is_mixed and ibu_id not in gender_ibu_ids:
                    continue

                # Look up rank using multiple keys (IBUId, Bib, Name)
                rank = None
                for key in (ibu_id, res.get("Bib"), name):
                    if key:
                        rank = leg_ranks.get((str(key), leg))
                        if rank is not None:
                            break

                if ibu_id not in athletes:
                    athletes[ibu_id] = {
                        "name": name,
                        "nat": nat,
                        "ranks": {},
                    }

                # Only count valid results
                if _is_valid_result(res) and rank is not None:
                    athletes[ibu_id]["ranks"][rid] = rank
        else:
            # For individual races, use the existing logic
            ranks = race_ranks.get(rid, {})
            for res in results:
                if res.get("IsTeam"):
                    continue

                ibu_id, name, nat = _get_athlete_info(res)
                if not ibu_id:
                    continue

                # Apply top filter
                if top_ibu_ids is not None and ibu_id not in top_ibu_ids:
                    continue

                if ibu_id not in athletes:
                    athletes[ibu_id] = {
                        "name": name,
                        "nat": nat,
                        "ranks": {},
                    }

                # Only count valid results
                if _is_valid_result(res) and ibu_id in ranks:
                    athletes[ibu_id]["ranks"][rid] = ranks[ibu_id]

    if not athletes:
        print("no athletes found", file=sys.stderr)
        return 1

    # Calculate form scores:
    # - Season Form: average of ALL race ranks for the athlete
    # - Current Form: average of the athlete's last N races they participated in
    rows = []
    for ibu_id, entry in athletes.items():
        all_rank_values = list(entry["ranks"].values())
        if not all_rank_values:
            continue  # Skip athletes with no valid results

        # Season Form = average of all races the athlete participated in
        season_form = sum(all_rank_values) / len(all_rank_values)

        # Current Form = average of the athlete's most recent N races or races from last N events
        # completed_race_ids is ordered oldest to newest, so reverse to get most recent first
        if num_events > 0:
            # Get the last N event IDs (most recent first)
            recent_event_ids = _get_recent_event_ids(completed_race_ids, race_to_event, num_events)
            # Get all ranks from races in those events
            athlete_recent_ranks = [
                entry["ranks"][rid] for rid in reversed(completed_race_ids)
                if rid in entry["ranks"] and race_to_event.get(rid) in recent_event_ids
            ]
        else:
            # Existing logic: last N races
            athlete_recent_ranks = []
            for rid in reversed(completed_race_ids):
                if rid in entry["ranks"]:
                    athlete_recent_ranks.append(entry["ranks"][rid])
                    if len(athlete_recent_ranks) >= num_races:
                        break

        if athlete_recent_ranks:
            current_form = sum(athlete_recent_ranks) / len(athlete_recent_ranks)
            current_form_str = f"{current_form:.1f}"
        else:
            current_form = float("inf")
            current_form_str = "-"

        # Build row: Rank, Name, Nat, Season Form, Current Form, then race columns
        row_data = [
            0,  # Placeholder for rank
            entry["name"],
            entry["nat"],
            f"{season_form:.1f}",
            current_form_str,
        ]

        # Add race columns
        for rid in completed_race_ids:
            rank = entry["ranks"].get(rid)
            row_data.append(str(rank) if rank is not None else "-")

        rows.append({
            "season_form": season_form,
            "current_form": current_form,
            "name": entry["name"],
            "row": row_data,
        })

    # Sort by current form (ascending), then by season form, then by name
    rows.sort(key=lambda r: (r["current_form"], r["season_form"], r["name"]))

    # Assign ranks
    for idx, row in enumerate(rows, start=1):
        row["row"][0] = idx

    # Apply limit
    limit = getattr(args, "limit", 25)
    if limit > 0:
        rows = rows[:limit]

    # Build headers
    headers = ["Rank", "Biathlete", "Nat", "Season", "Current"] + race_headers

    # Render table
    pretty = is_pretty_output(args)
    row_styles = [rank_style(r["row"][0]) for r in rows] if pretty else None
    render_table(
        headers,
        [r["row"] for r in rows],
        pretty=pretty,
        row_styles=row_styles,
    )

    return 0
