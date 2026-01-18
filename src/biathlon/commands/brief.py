"""Brief command handlers for race analysis."""

from __future__ import annotations

import argparse
import datetime
import sys

from ..api import BiathlonError, get_events, get_race_results, get_races, get_current_season_id
from ..formatting import is_pretty_output, Color
from ..utils import format_race_header
from .post_race import handle_post_race
from .startlist import (
    _build_startlist_entries,
    _extract_venue_name,
    _find_all_startlist_races,
    _format_section_title,
    _is_true,
    _prepare_startlist_context,
    _select_race_interactive,
    render_startlist_analysis,
    render_venue_history,
)


def _find_current_event() -> dict | None:
    """Find the current or next upcoming World Cup event.

    Returns the event dict or None if no suitable event found.
    """
    season_id = get_current_season_id()
    events = get_events(season_id, level=1)
    today = datetime.date.today()

    # Sort events by start date
    dated_events = []
    for event in events:
        start_raw = event.get("StartDate") or ""
        if not start_raw:
            continue
        start_str = start_raw.split("T", 1)[0] if isinstance(start_raw, str) else ""
        if not start_str:
            continue
        try:
            start_date = datetime.date.fromisoformat(start_str)
        except ValueError:
            continue
        end_raw = event.get("EndDate") or start_raw
        end_str = end_raw.split("T", 1)[0] if isinstance(end_raw, str) else start_str
        try:
            end_date = datetime.date.fromisoformat(end_str)
        except ValueError:
            end_date = start_date
        dated_events.append((start_date, end_date, event))

    dated_events.sort(key=lambda x: x[0])

    # Find current event (today is between start and end)
    for start_date, end_date, event in dated_events:
        if start_date <= today <= end_date:
            return event

    # Find next upcoming event
    for start_date, end_date, event in dated_events:
        if start_date > today:
            return event

    return None


def _find_first_race_for_event(event_id: str, cat_id: str = "SW") -> tuple[str, dict] | None:
    """Find the first race for an event to use as reference.

    Returns (race_id, payload) or None if no race found.
    """
    try:
        races = get_races(event_id)
    except BiathlonError:
        return None

    # Filter by category and sort by start time
    matching_races = []
    for race in races:
        race_cat = str(race.get("catId") or race.get("CatId") or "").upper()
        if race_cat != cat_id:
            continue
        race_id = race.get("RaceId") or race.get("Id")
        if not race_id:
            continue
        start_raw = race.get("StartTime") or race.get("StartDate") or ""
        matching_races.append((start_raw, race_id))

    if not matching_races:
        return None

    matching_races.sort(key=lambda x: x[0])
    race_id = matching_races[0][1]

    try:
        payload = get_race_results(race_id)
        return race_id, payload
    except BiathlonError:
        return None


def handle_brief_event(args: argparse.Namespace) -> int:
    """Display venue history and records (before an event).

    Shows sections 14-20 from the startlist analysis.
    """
    cat_id = "SM" if getattr(args, "men", False) else "SW"

    # Get event - either from --event flag or auto-detect
    if getattr(args, "event", ""):
        event_id = args.event
        # Try to find a race to get venue info
        result = _find_first_race_for_event(event_id, cat_id)
        if not result:
            print(f"No races found for event {event_id}", file=sys.stderr)
            return 1
        race_id, payload = result
    else:
        # Auto-detect current/upcoming event
        event = _find_current_event()
        if not event:
            print("No current or upcoming World Cup event found", file=sys.stderr)
            return 1
        event_id = event.get("EventId")
        if not event_id:
            print("Event has no ID", file=sys.stderr)
            return 1
        result = _find_first_race_for_event(event_id, cat_id)
        if not result:
            print(f"No races found for event {event_id}", file=sys.stderr)
            return 1
        race_id, payload = result

    # Build context with empty startlist (we're just showing history)
    # We need to create a minimal context for render_venue_history
    venue_name = _extract_venue_name(payload)
    if not venue_name:
        print("Could not determine venue name", file=sys.stderr)
        return 1

    # Create a mock context for venue history
    comp = payload.get("Competition") or {}
    race_disc = str(comp.get("DisciplineId") or "SP").upper()  # Default to sprint
    season_id = str((payload.get("SportEvt") or {}).get("SeasonId") or "") or get_current_season_id()
    use_major = bool(getattr(args, "major", False))

    # Import the function we need for alltime stats
    from .startlist import _get_alltime_venue_stats

    alltime_stats = _get_alltime_venue_stats(venue_name, cat_id, use_major)

    ctx = {
        "payload": payload,
        "race_id": race_id,
        "entries": [],
        "age_cache": {},
        "comp": comp,
        "race_disc": race_disc,
        "is_relay": False,
        "discipline_set": {"SP", "PU", "IN", "MS"},
        "cat_id": cat_id,
        "season_id": season_id,
        "venue_name": venue_name,
        "use_major": use_major,
        "startlist_ids": set(),
        "alltime_stats": alltime_stats,
        "prefetched_results": {},
    }

    # Print header
    gender_label = "Men" if cat_id == "SM" else "Women"
    print()
    print(_format_section_title(f"Event Brief: {venue_name} ({gender_label})", args))
    print()

    render_venue_history(ctx, args)

    return 0


def handle_brief_startlist(args: argparse.Namespace) -> int:
    """Display startlist analysis (before a race).

    Shows sections 1-13 from the startlist analysis.
    """
    try:
        if getattr(args, "race", ""):
            race_id = args.race
            payload = get_race_results(race_id)
        else:
            candidates = _find_all_startlist_races()
            race_id, payload = _select_race_interactive(candidates)
    except BiathlonError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if not _is_true(payload.get("IsStartList")):
        print(f"race {race_id} does not have a startlist", file=sys.stderr)
        return 1

    entries = _build_startlist_entries(payload)
    if not entries:
        print(f"no startlist entries found for race {race_id}", file=sys.stderr)
        return 1

    ctx = _prepare_startlist_context(payload, race_id, args)

    print()
    print(_format_section_title(format_race_header(payload, race_id), args))
    print(f"Startlist entries: {len(ctx['entries'])}")
    print()

    render_startlist_analysis(ctx, args)

    return 0


def handle_brief_post_race(args: argparse.Namespace) -> int:
    """Display post-race analysis (after a race).

    Delegates to the existing post_race handler.
    """
    return handle_post_race(args)
