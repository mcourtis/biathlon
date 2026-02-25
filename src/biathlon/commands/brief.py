"""Brief command handlers for race analysis."""

from __future__ import annotations

import argparse
import datetime
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, cast

from ..api import (
    BiathlonError,
    get_all_results,
    get_cups,
    get_cup_results,
    get_events,
    get_race_results,
    get_races,
    get_current_season_id,
    get_seasons,
)
from ..constants import (
    CATEGORY_DISPLAY_NAMES,
    DISCIPLINE_NAMES,
    EVENT_TYPE_LABELS,
    EVENT_TYPE_OWG,
    EVENT_TYPE_WC,
    EVENT_TYPE_WCH,
)
from ..formatting import (
    is_pretty_output,
    get_output_format,
    Color,
    render_table,
    OutputFormat,
)
from ..utils import format_race_header, parse_date, parse_start_datetime
from .events import compute_event_styles, format_level

from ._common import (
    _format_section_title,
    _has_completed_relay_results,
    _max_workers,
    _ordinal,
    _parse_rank,
    _row_ibu_id,
    detect_event_type,
    is_relay_discipline,
    _select_race_interactive,
)
from .postrace import (
    handle_post_race,
    _is_result_at_or_before_target,
    _is_team_level_result,
    _result_discipline_id,
    _start_dt_from_competition,
)
from .results import _get_wc_rows, _has_completed_results
from .startlist import (
    _build_startlist_entries,
    _build_team_entries,
    _extract_venue_name,
    _find_all_startlist_races,
    _is_true,
    _prepare_startlist_context,
    render_startlist_analysis,
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


def _find_first_race_for_event(
    event_id: str, cat_id: str = "SW"
) -> tuple[str, dict] | None:
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


def _format_local_time(start_raw: str) -> tuple[str, str]:
    """Convert an ISO datetime string to local date and time.

    Returns (date_str, time_str) in local timezone.
    """
    if not isinstance(start_raw, str) or not start_raw:
        return "", ""

    try:
        # Handle Z suffix for UTC
        iso_str = start_raw.replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(iso_str)
        # If no timezone info, assume UTC
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        # Convert to local timezone
        local_dt = dt.astimezone()
        return local_dt.strftime("%Y-%m-%d"), local_dt.strftime("%H:%M")
    except ValueError:
        # Fallback: just extract date part
        if "T" in start_raw:
            date_part, time_part = start_raw.split("T", 1)
            return date_part, time_part[:5]
        return start_raw.split(" ", 1)[0], ""


def _resolve_race_start_datetime(
    competition: dict | None,
) -> datetime.datetime | None:
    """Resolve race start datetime from a competition payload."""
    if not isinstance(competition, dict):
        return None
    for key in ("StartTime", "StartDate", "Date"):
        raw = competition.get(key)
        if not raw:
            continue
        start_dt = parse_start_datetime(str(raw))
        if start_dt is not None:
            return start_dt
    return None


def _season_end_year(season_id: str) -> int | None:
    """Return the ending calendar year for a season id like '2526'."""
    s = str(season_id or "")
    if len(s) < 4 or not s[:4].isdigit():
        return None

    start_two = int(s[:2])
    end_two = int(s[2:4])
    century = 2000 if start_two < 50 else 1900
    start_year = century + start_two
    end_year = century + end_two
    if end_year < start_year:
        end_year += 100
    return end_year


def _render_venue_history_table(
    alltime_stats: list[dict],
    current_season_ids: set[str],
    location_label: str,
    gender_label: str,
    args: argparse.Namespace,
    use_medal_columns: bool = False,
) -> None:
    """Render history table for one gender with current season highlighting.

    Args:
        alltime_stats: List of athlete stat dicts
        current_season_ids: Set of IBU IDs of athletes active in current season
        location_label: Label for the location (venue name, "Olympic Games", "World Championships")
        gender_label: "Women" or "Men"
        args: Command arguments
        use_medal_columns: If True, use Gold/Silver/Bronze/Total columns instead of Wins/Podiums/Flowers
    """
    if not alltime_stats:
        print(
            _format_section_title(
                f"Most decorated {gender_label.lower()} at {location_label}: no data",
                args,
            )
        )
        print()
        return

    # For medal columns, filter by gold > 0; for venue columns, filter by wins > 0
    if use_medal_columns:
        alltime_decorated = [s for s in alltime_stats if s.get("gold", 0) > 0]
        # Sort by total medals, then gold, silver, bronze
        alltime_decorated.sort(
            key=lambda s: (
                s.get("gold", 0) + s.get("silver", 0) + s.get("bronze", 0),
                s.get("gold", 0),
                s.get("silver", 0),
                s.get("bronze", 0),
            ),
            reverse=True,
        )
    else:
        alltime_decorated = [s for s in alltime_stats if s["wins"] > 0]
        alltime_decorated.sort(
            key=lambda s: (s["wins"], s["podiums"], s["flowers"], -s["races"]),
            reverse=True,
        )
    alltime_decorated = alltime_decorated[:10]

    if not alltime_decorated:
        print(
            _format_section_title(
                f"Most decorated {gender_label.lower()} at {location_label}: no winners found",
                args,
            )
        )
        print()
    else:
        print(
            _format_section_title(
                f"Most decorated {gender_label.lower()} at {location_label}:", args
            )
        )
        venue_rows = []
        highlight_rows: set[int] = set()
        for idx, stats in enumerate(alltime_decorated):
            if stats.get("ibu_id", "") in current_season_ids:
                highlight_rows.add(idx)
            if use_medal_columns:
                gold = stats.get("gold", 0)
                silver = stats.get("silver", 0)
                bronze = stats.get("bronze", 0)
                total = gold + silver + bronze
                venue_rows.append(
                    [
                        idx + 1,
                        stats["name"],
                        gold,
                        silver,
                        bronze,
                        total,
                        stats["races"],
                    ]
                )
            else:
                venue_rows.append(
                    [
                        idx + 1,
                        stats["name"],
                        stats["wins"],
                        stats["podiums"],
                        stats["flowers"],
                        stats["races"],
                    ]
                )

        def highlight_cell(cell_str: str, row_idx: int) -> str:
            return Color.highlight(cell_str) if row_idx in highlight_rows else cell_str

        if use_medal_columns:
            headers = ["#", "Athlete", "Gold", "Silver", "Bronze", "Total", "Races"]
            formatters = [None, highlight_cell, None, None, None, None, None]
        else:
            headers = ["#", "Athlete", "Wins", "Podiums", "Flowers", "Races"]
            formatters = [None, highlight_cell, None, None, None, None]

        render_table(
            headers,
            venue_rows,
            output_format=get_output_format(args),
            cell_formatters=formatters,
        )
        print()

    # Most experienced section
    alltime_experienced = [s for s in alltime_stats if s["races"] > 0]
    if use_medal_columns:
        alltime_experienced.sort(
            key=lambda s: (
                s["races"],
                s.get("gold", 0) + s.get("silver", 0) + s.get("bronze", 0),
                s.get("gold", 0),
            ),
            reverse=True,
        )
    else:
        alltime_experienced.sort(
            key=lambda s: (s["races"], s["wins"], s["podiums"], s["flowers"]),
            reverse=True,
        )
    alltime_experienced = alltime_experienced[:10]

    if not alltime_experienced:
        print(
            _format_section_title(
                f"Most experienced {gender_label.lower()} at {location_label}: no data",
                args,
            )
        )
        print()
        return

    print(
        _format_section_title(
            f"Most experienced {gender_label.lower()} at {location_label}:", args
        )
    )
    venue_rows = []
    highlight_rows = set()
    for idx, stats in enumerate(alltime_experienced):
        if stats.get("ibu_id", "") in current_season_ids:
            highlight_rows.add(idx)
        if use_medal_columns:
            gold = stats.get("gold", 0)
            silver = stats.get("silver", 0)
            bronze = stats.get("bronze", 0)
            total = gold + silver + bronze
            venue_rows.append(
                [
                    idx + 1,
                    stats["name"],
                    stats["races"],
                    gold,
                    silver,
                    bronze,
                    total,
                ]
            )
        else:
            venue_rows.append(
                [
                    idx + 1,
                    stats["name"],
                    stats["races"],
                    stats["wins"],
                    stats["podiums"],
                    stats["flowers"],
                ]
            )

    def highlight_cell_exp(cell_str: str, row_idx: int) -> str:
        return Color.highlight(cell_str) if row_idx in highlight_rows else cell_str

    if use_medal_columns:
        headers = ["#", "Athlete", "Races", "Gold", "Silver", "Bronze", "Total"]
        formatters = [None, highlight_cell_exp, None, None, None, None, None]
    else:
        headers = ["#", "Athlete", "Races", "Wins", "Podiums", "Flowers"]
        formatters = [None, highlight_cell_exp, None, None, None, None]

    render_table(
        headers,
        venue_rows,
        output_format=get_output_format(args),
        cell_formatters=formatters,
    )
    print()


def handle_brief_preevent(args: argparse.Namespace) -> int:
    """Display event schedule and venue history (before an event).

    Shows event facts, schedule for both genders, and all-time venue/event history.
    """
    # Get event - either from --event flag or auto-detect
    current_event: dict | None = None
    if getattr(args, "event", ""):
        event_id = args.event
        # Try to find the event data by searching current season events
        season_id = get_current_season_id()
        for level in (1, 2, 3, 4, 5, 6):
            events = get_events(season_id, level)
            for ev in events:
                if ev.get("EventId") == event_id:
                    current_event = ev
                    break
            if current_event:
                break
        # Try to find a race to get venue info (try women first, then men)
        result = _find_first_race_for_event(event_id, "SW")
        if not result:
            result = _find_first_race_for_event(event_id, "SM")
        if not result:
            print(f"No races found for event {event_id}", file=sys.stderr)
            return 1
        race_id, payload = result
    else:
        # Auto-detect current/upcoming event
        current_event = _find_current_event()
        if not current_event:
            print("No current or upcoming World Cup event found", file=sys.stderr)
            return 1
        event_id = current_event.get("EventId")
        if not event_id:
            print("Event has no ID", file=sys.stderr)
            return 1
        result = _find_first_race_for_event(event_id, "SW")
        if not result:
            result = _find_first_race_for_event(event_id, "SM")
        if not result:
            print(f"No races found for event {event_id}", file=sys.stderr)
            return 1
        race_id, payload = result

    # Detect event type (World Cup, World Championship, Olympic Games)
    event_type = detect_event_type(current_event) if current_event else EVENT_TYPE_WC

    venue_name = _extract_venue_name(payload)
    if not venue_name:
        print("Could not determine venue name", file=sys.stderr)
        return 1

    use_major = bool(getattr(args, "major", False))

    # Determine location label for stats sections based on event type
    if event_type == EVENT_TYPE_OWG:
        location_label = "Olympic Games"
    elif event_type == EVENT_TYPE_WCH:
        location_label = "World Championships"
    else:
        location_label = venue_name

    # Print header
    print()
    print(_format_section_title(f"Event Brief: {venue_name}", args))
    print()

    # Import the functions we need for alltime stats
    from .startlist import (
        _get_alltime_venue_stats,
        _get_alltime_major_event_stats,
        _get_venue_events_only,
    )

    # Get current season ID for fetching active athletes
    current_season = get_current_season_id()

    # Fetch history and current season standings in parallel
    # For Olympic Games and World Championships:
    #   - Venue events only for Event Facts (lightweight - no race results)
    #   - Major event stats for "Most decorated" sections (women + men in parallel)
    # For regular World Cup: fetch venue-level stats for both
    women_stats: list[dict] = []
    women_events: list[dict] = []  # Used for Event Facts (always venue-level)
    men_stats: list[dict] = []
    women_active_ids: set[str] = set()
    men_active_ids: set[str] = set()

    is_major_event = event_type in (EVENT_TYPE_OWG, EVENT_TYPE_WCH)

    def fetch_venue_events():
        # Lightweight fetch - only events, no race results
        return _get_venue_events_only(venue_name, use_major)

    def fetch_women_venue():
        return _get_alltime_venue_stats(venue_name, "SW", use_major, show_progress=True)

    def fetch_men_venue():
        return _get_alltime_venue_stats(
            venue_name, "SM", use_major, show_progress=False
        )

    def fetch_women_major():
        return _get_alltime_major_event_stats(event_type, "SW", show_progress=True)

    def fetch_men_major():
        return _get_alltime_major_event_stats(event_type, "SM", show_progress=False)

    def fetch_women_standings():
        rows = _get_wc_rows("SW", current_season)
        return {_row_ibu_id(r) for r in rows if _row_ibu_id(r)}

    def fetch_men_standings():
        rows = _get_wc_rows("SM", current_season)
        return {_row_ibu_id(r) for r in rows if _row_ibu_id(r)}

    if is_major_event:
        # For major events:
        # - Fetch venue events only (fast) for Event Facts
        # - Fetch major event stats for women + men in parallel
        with ThreadPoolExecutor(max_workers=5) as executor:
            venue_events_future = executor.submit(fetch_venue_events)
            women_major_future = executor.submit(fetch_women_major)
            men_major_future = executor.submit(fetch_men_major)
            women_standings_future = executor.submit(fetch_women_standings)
            men_standings_future = executor.submit(fetch_men_standings)

            women_events = venue_events_future.result()  # For Event Facts
            women_stats, _, _ = women_major_future.result()  # For Most Decorated
            men_stats, _, _ = men_major_future.result()
            women_active_ids = women_standings_future.result()
            men_active_ids = men_standings_future.result()
    else:
        # For regular World Cup, venue stats are used for both Event Facts and Most Decorated
        with ThreadPoolExecutor(max_workers=4) as executor:
            women_venue_future = executor.submit(fetch_women_venue)
            men_venue_future = executor.submit(fetch_men_venue)
            women_standings_future = executor.submit(fetch_women_standings)
            men_standings_future = executor.submit(fetch_men_standings)

            women_stats, _, women_events = women_venue_future.result()
            men_stats, _, _ = men_venue_future.result()
            women_active_ids = women_standings_future.result()
            men_active_ids = men_standings_future.result()

    # Event Facts section
    if women_events:
        # Get unique seasons and find the first one
        season_years: dict[str, int] = {}  # season_id -> year
        for ev in women_events:
            start_date = ev.get("start_date", "")
            season_id = ev.get("season_id", "")
            if start_date and len(start_date) >= 4 and season_id:
                try:
                    year = int(start_date[:4])
                    if season_id not in season_years:
                        season_years[season_id] = year
                except ValueError:
                    pass

        if season_years:
            # Cutoff year to filter out future events.
            current_year = datetime.date.today().year
            max_event_year = current_year
            season_end_year = _season_end_year(current_season)
            if season_end_year:
                max_event_year = min(max_event_year, season_end_year)

            # Categorize events: WC (regular), WCH (World Championships), OWG (Olympics)
            # Only include events up to the cutoff year.
            wc_events = []
            wch_events = []
            owg_events = []
            for ev in women_events:
                # Skip future events
                ev_year = season_years.get(str(ev.get("season_id", "")))
                if ev_year and ev_year > max_event_year:
                    continue

                event_data = ev.get("event", {})
                desc = str(
                    event_data.get("Description")
                    or event_data.get("ShortDescription")
                    or ""
                ).lower()
                # Olympic events
                if "olympic" in desc:
                    owg_events.append(ev)
                # World Championships have "world championships" in description
                elif "world championships" in desc:
                    wch_events.append(ev)
                else:
                    wc_events.append(ev)

            # Find first WC event (excluding WCH and OWG)
            wc_season_years = {
                ev["season_id"]: season_years[ev["season_id"]]
                for ev in wc_events
                if ev.get("season_id") in season_years
            }
            if wc_season_years:
                first_season = min(
                    wc_season_years.keys(), key=lambda s: wc_season_years[s]
                )
                first_year = wc_season_years[first_season]
                # Format season (e.g., "1112" -> "2011/2012")
                s = str(first_season)
                if len(s) >= 4:
                    season_display = (
                        f"20{s[:2]}/20{s[2:]}"
                        if int(s[:2]) < 50
                        else f"19{s[:2]}/19{s[2:]}"
                    )
                else:
                    season_display = s
            else:
                first_year = None
                season_display = ""

            # Get years for WC events (already filtered to exclude future)
            wc_years = sorted(
                {
                    season_years[ev["season_id"]]
                    for ev in wc_events
                    if ev.get("season_id") in season_years
                    and season_years[ev["season_id"]] <= max_event_year
                }
            )
            wc_years_str = ", ".join(str(y) for y in wc_years)

            # Get years for WCH events (already filtered to exclude future)
            wch_years = sorted(
                {
                    season_years[ev["season_id"]]
                    for ev in wch_events
                    if ev.get("season_id") in season_years
                    and season_years[ev["season_id"]] <= max_event_year
                }
            )
            wch_years_str = ", ".join(str(y) for y in wch_years)

            # Get years for OWG events (already filtered to exclude future)
            owg_years = sorted(
                {
                    season_years[ev["season_id"]]
                    for ev in owg_events
                    if ev.get("season_id") in season_years
                    and season_years[ev["season_id"]] <= max_event_year
                }
            )
            owg_years_str = ", ".join(str(y) for y in owg_years)

            print(_format_section_title("Event Facts:", args))
            print(f"  Event type: {EVENT_TYPE_LABELS.get(event_type, 'World Cup')}")
            if first_year:
                print(
                    f"  First World Cup event: {first_year} (season {season_display})"
                )
            print(f"  Total World Cup events: {len(wc_events)} ({wc_years_str})")
            if wch_events:
                print(
                    f"  Total World Championship events: {len(wch_events)} ({wch_years_str})"
                )
            if owg_events:
                print(
                    f"  Total Olympic Games events: {len(owg_events)} ({owg_years_str})"
                )
            else:
                print("  Total Olympic Games events: 0")
            print()

    # Get and display event schedule for both genders
    try:
        races = get_races(event_id)
    except BiathlonError:
        races = []

    # Build schedule for all categories (women, men, mixed)
    schedule_rows = []
    for race in races:
        race_cat = str(race.get("catId") or race.get("CatId") or "").upper()
        if race_cat not in ("SW", "SM", "MX"):
            continue
        start_raw = race.get("StartTime") or race.get("StartDate") or ""
        disc_code = str(race.get("DisciplineId") or "").upper()
        cat_label = CATEGORY_DISPLAY_NAMES.get(race_cat, race_cat)
        disc_label = DISCIPLINE_NAMES.get(disc_code, disc_code)

        date_str, time_str = _format_local_time(start_raw)
        schedule_rows.append((start_raw, [date_str, time_str, cat_label, disc_label]))

    # Sort by start time
    schedule_rows.sort(key=lambda x: x[0])

    if schedule_rows:
        print(_format_section_title("Event Schedule:", args))
        render_table(
            ["Date", "Time", "Category", "Discipline"],
            [row[1] for row in schedule_rows],
            output_format=get_output_format(args),
        )
        print()

    # History for women (highlight athletes active in current WC season)
    _render_venue_history_table(
        women_stats,
        women_active_ids,
        location_label,
        "Women",
        args,
        use_medal_columns=is_major_event,
    )

    # History for men (highlight athletes active in current WC season)
    _render_venue_history_table(
        men_stats,
        men_active_ids,
        location_label,
        "Men",
        args,
        use_medal_columns=is_major_event,
    )

    return 0


def handle_brief_postseason(args: argparse.Namespace) -> int:
    """Display a season summary (events and race breakdown)."""
    season_id = (getattr(args, "season", "") or "").strip() or get_current_season_id()
    level_raw = getattr(args, "level", "1")
    try:
        level_int = int(level_raw)
        if level_int < 1 or level_int > 6:
            raise ValueError
    except ValueError:
        print("error: level must be an integer between 1 and 6", file=sys.stderr)
        return 1

    try:
        seasons = get_seasons()
    except BiathlonError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    season_entry = next(
        (s for s in seasons if str(s.get("SeasonId")) == season_id), None
    )
    if not season_entry:
        print(f"season {season_id} not found", file=sys.stderr)
        return 1

    season_desc = str(season_entry.get("Description") or "").strip()
    is_current = bool(season_entry.get("IsCurrent"))

    try:
        events = get_events(season_id, level=level_int)
    except BiathlonError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    header = season_desc or season_id
    if season_desc and season_id and season_id not in season_desc:
        header = f"{season_desc} (season {season_id})"

    print()
    print(_format_section_title(f"Season Brief: {header}", args))
    print()

    print(_format_section_title("Season Facts:", args))
    print(f"  Season id: {season_id}")
    if season_desc:
        print(f"  Description: {season_desc}")
    print(f"  Current season: {'yes' if is_current else 'no'}")
    print(f"  Level: {format_level(level_int)}")

    event_count = len(events)
    print(f"  Events: {event_count}")

    if not events:
        print()
        return 0

    pretty = is_pretty_output(args)
    output_format = get_output_format(args)
    today = datetime.date.today()
    completed = 0
    upcoming = 0
    undated = 0
    start_dates: list[datetime.date] = []
    end_dates: list[datetime.date] = []
    total_races = 0
    discipline_counts: dict[str, int] = {}
    category_counts: dict[str, int] = {}
    races_by_event: dict[str, list[dict]] = {}

    for event in events:
        start = parse_date(event.get("StartDate") or event.get("FirstCompetitionDate"))
        end = parse_date(event.get("EndDate")) or start
        if start:
            start_dates.append(start)
        if end:
            end_dates.append(end)
        if end:
            if end < today:
                completed += 1
            else:
                upcoming += 1
        else:
            undated += 1

        event_id = event.get("EventId")
        races: list[dict] = []
        if event_id:
            try:
                races = get_races(event_id)
            except BiathlonError:
                races = []
            races_by_event[str(event_id)] = races

        total_races += len(races)
        for race in races:
            disc = str(race.get("DisciplineId") or "").upper()
            if disc:
                discipline_counts[disc] = discipline_counts.get(disc, 0) + 1
            cat = str(race.get("catId") or race.get("CatId") or "").upper()
            if cat:
                category_counts[cat] = category_counts.get(cat, 0) + 1

    suffix_parts = []
    if completed:
        suffix_parts.append(f"{completed} completed")
    if upcoming:
        suffix_parts.append(f"{upcoming} upcoming")
    if undated:
        suffix_parts.append(f"{undated} undated")
    if suffix_parts:
        print(f"  Event status: {', '.join(suffix_parts)}")

    if total_races:
        print(f"  Races: {total_races}")

    if start_dates or end_dates:
        start_label = min(start_dates).isoformat() if start_dates else ""
        end_label = max(end_dates).isoformat() if end_dates else ""
        if start_label and end_label:
            print(f"  Date range: {start_label} -> {end_label}")

    print()

    def date_only(value: str | None) -> str:
        return value.split("T", 1)[0] if isinstance(value, str) else ""

    def parse_rank(value: object) -> int | None:
        text = str(value or "").strip().rstrip(".")
        if text.isdigit():
            return int(text)
        return None

    agenda_events = sorted(
        events,
        key=lambda event: (
            event.get("StartDate") or event.get("FirstCompetitionDate") or ""
        ),
    )
    agenda_rows: list[list[str]] = []

    def mark(tags: set | bool) -> str:
        if isinstance(tags, bool):
            return "X" if tags else ""
        if not tags:
            return ""
        if "W+M" in tags or ("W" in tags and "M" in tags):
            return "W+M"
        return "+".join(sorted(tags))

    for event in agenda_events:
        event_id = event.get("EventId")
        race_list = races_by_event.get(str(event_id), []) if event_id else []
        flags: dict[str, set[str] | bool] = {
            "individual": set(),
            "sprint": set(),
            "pursuit": set(),
            "mass": set(),
            "relay": set(),
            "mixed_relay": False,
            "single_mixed": False,
        }

        for race in race_list:
            name = (
                race.get("RaceName")
                or race.get("ShortDescription")
                or race.get("Description")
                or ""
            ).lower()
            disc = (race.get("DisciplineId") or "").upper()
            gender_tag = ""
            if "women" in name or "women's" in name:
                gender_tag = "W"
            elif "men" in name:
                gender_tag = "M"

            if disc in {"IN", "SI"}:
                cast(set[str], flags["individual"]).add(gender_tag or "W+M")
            elif disc == "SP":
                cast(set[str], flags["sprint"]).add(gender_tag or "W+M")
            elif disc == "PU":
                cast(set[str], flags["pursuit"]).add(gender_tag or "W+M")
            elif disc == "MS":
                cast(set[str], flags["mass"]).add(gender_tag or "W+M")
            elif disc == "RL" or "relay" in name:
                if "single" in name and "mixed" in name:
                    flags["single_mixed"] = True
                elif "mixed" in name:
                    flags["mixed_relay"] = True
                else:
                    cast(set[str], flags["relay"]).add(gender_tag or "W+M")

        agenda_rows.append(
            [
                event.get("Description") or "",
                event.get("ShortDescription") or event.get("Organizer") or "",
                event.get("Nat")
                or event.get("Nation")
                or event.get("CountryId")
                or event.get("Country")
                or "",
                date_only(
                    event.get("StartDate") or event.get("FirstCompetitionDate") or ""
                ),
                str(len(race_list)),
                mark(flags["individual"]),
                mark(flags["sprint"]),
                mark(flags["pursuit"]),
                mark(flags["mass"]),
                mark(flags["relay"]),
                mark(flags["mixed_relay"]),
                mark(flags["single_mixed"]),
            ]
        )

    if agenda_rows:
        print(_format_section_title("Agenda:", args))
        agenda_styles = compute_event_styles(agenda_events) if pretty else None
        render_table(
            [
                "Event",
                "Location",
                "Country",
                "StartDate",
                "Races",
                "Individual",
                "Sprint",
                "Pursuit",
                "MassStart",
                "Relay",
                "MixedRelay",
                "SingleMixedRelay",
            ],
            agenda_rows,
            output_format=output_format,
            row_styles=agenda_styles,
            cell_formatters=[
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

    decorated_stats: dict[str, dict[str, Any]] = {"SW": {}, "SM": {}}
    race_jobs: list[tuple[str, str]] = []
    for event in events:
        event_id = event.get("EventId")
        if not event_id:
            continue
        for race in races_by_event.get(str(event_id), []):
            cat_id = str(race.get("catId") or race.get("CatId") or "").upper()
            if cat_id not in {"SW", "SM"}:
                continue
            race_id = race.get("RaceId") or race.get("Id")
            if race_id:
                race_jobs.append((str(race_id), cat_id))

    def fetch_race_payload(race_id: str) -> tuple[str, dict | None]:
        try:
            return race_id, get_race_results(race_id)
        except BiathlonError:
            return race_id, None

    if race_jobs:
        with ThreadPoolExecutor(max_workers=_max_workers(len(race_jobs))) as executor:
            futures = {
                executor.submit(fetch_race_payload, race_id): (race_id, cat_id)
                for race_id, cat_id in race_jobs
            }
            for future in as_completed(futures):
                race_id, cat_id = futures[future]
                _, payload = future.result()
                if not payload or _is_true(payload.get("IsStartList")):
                    continue
                results = payload.get("Results") or []
                for res in results:
                    if res.get("IsTeam"):
                        continue
                    rank_val = parse_rank(
                        res.get("Rank") or res.get("SO") or res.get("ResultOrder")
                    )
                    if rank_val is None:
                        continue
                    ibu_id = _row_ibu_id(res)
                    name = res.get("Name") or res.get("ShortName") or ""
                    nat = res.get("Nat") or ""
                    key = ibu_id or f"{name}|{nat}"
                    stats = decorated_stats[cat_id].setdefault(
                        key,
                        {
                            "name": name,
                            "nat": nat,
                            "wins": 0,
                            "podiums": 0,
                            "flowers": 0,
                            "races": 0,
                        },
                    )
                    stats["races"] += 1
                    if rank_val == 1:
                        stats["wins"] += 1
                    elif 2 <= rank_val <= 3:
                        stats["podiums"] += 1
                    elif 4 <= rank_val <= 6:
                        stats["flowers"] += 1

    def render_decorated_section(label: str, stats_map: dict) -> None:
        decorated = [s for s in stats_map.values() if s["flowers"] > 0]
        decorated.sort(
            key=lambda s: (s["wins"], s["podiums"], s["flowers"], -s["races"]),
            reverse=True,
        )
        decorated = decorated[:10]
        if decorated:
            print(_format_section_title(f"Top Decorated {label}:", args))
            rows = []
            for idx, stats in enumerate(decorated, start=1):
                total = stats["wins"] + stats["podiums"] + stats["flowers"]
                rows.append(
                    [
                        idx,
                        stats["name"],
                        stats["nat"],
                        stats["wins"],
                        stats["podiums"],
                        stats["flowers"],
                        total,
                        stats["races"],
                    ]
                )

            render_table(
                [
                    "Rank",
                    "Athlete",
                    "Nat",
                    "Wins",
                    "Podiums",
                    "Flowers",
                    "Total",
                    "Races",
                ],
                rows,
                output_format=output_format,
                cell_formatters=[None, None, None, None, None, None, None, None],
            )
            print()
        else:
            print(_format_section_title(f"Top Decorated {label}: none", args))
            print()

    if category_counts:
        cat_labels = {"SW": "Women", "SM": "Men", "MX": "Mixed"}
        rows = []
        for code in ("SW", "SM", "MX"):
            if code in category_counts:
                rows.append([cat_labels.get(code, code), str(category_counts[code])])
        for code in sorted(k for k in category_counts if k not in {"SW", "SM", "MX"}):
            rows.append([cat_labels.get(code, code), str(category_counts[code])])
        print(_format_section_title("Race categories:", args))
        render_table(["Category", "Races"], rows, output_format=output_format)
        print()

    if discipline_counts:
        disc_labels = {
            "IN": "Individual",
            "SI": "Short Individual",
            "SP": "Sprint",
            "PU": "Pursuit",
            "MS": "Mass Start",
            "RL": "Relay",
            "MR": "Mixed Relay",
            "SR": "Single Mixed Relay",
        }
        order = ["IN", "SI", "SP", "PU", "MS", "RL", "MR", "SR"]
        rows = []
        for code in order:
            if code in discipline_counts:
                rows.append([disc_labels.get(code, code), str(discipline_counts[code])])
        for code in sorted(k for k in discipline_counts if k not in order):
            rows.append([disc_labels.get(code, code), str(discipline_counts[code])])
        print(_format_section_title("Race disciplines:", args))
        render_table(["Discipline", "Races"], rows, output_format=output_format)
        print()

    render_decorated_section("Women", decorated_stats["SW"])
    render_decorated_section("Men", decorated_stats["SM"])

    return 0


def handle_brief_startlist(args: argparse.Namespace) -> int:
    """Display startlist analysis (before a race).

    Shows sections 1-13 from the startlist analysis (World Cup races).
    Olympic races show Olympic history sections instead.
    """
    snapshot_mode = False
    snapshot_cutoff_dt: datetime.datetime | None = None
    explicit_race = bool(getattr(args, "race", ""))

    try:
        if explicit_race:
            race_id = args.race
            payload = get_race_results(race_id)
        else:
            candidates = _find_all_startlist_races()
            race_id, payload = _select_race_interactive(candidates)
    except BiathlonError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    comp = payload.get("Competition") or {}
    race_disc = str(comp.get("DisciplineId") or "").upper()
    is_startlist = _is_true(payload.get("IsStartList"))
    if not is_startlist:
        if not explicit_race:
            print(f"race {race_id} does not have a startlist", file=sys.stderr)
            return 1
        if is_relay_discipline(race_disc):
            has_completed_results = _has_completed_relay_results(payload)
        else:
            has_completed_results = _has_completed_results(payload)
        if not has_completed_results:
            print(
                f"race {race_id} does not have a startlist or completed results",
                file=sys.stderr,
            )
            return 1
        snapshot_mode = True
        snapshot_cutoff_dt = _resolve_race_start_datetime(comp)
        if snapshot_cutoff_dt is None:
            print(
                f"race {race_id} does not expose a race start datetime for snapshot mode",
                file=sys.stderr,
            )
            return 1

    entries = _build_startlist_entries(payload)
    team_entries = _build_team_entries(payload)
    if not entries and not team_entries:
        print(f"no startlist entries found for race {race_id}", file=sys.stderr)
        return 1

    args.leader_markers = True
    ctx = _prepare_startlist_context(
        payload,
        race_id,
        args,
        snapshot_target_race_id=race_id if snapshot_mode else "",
        snapshot_cutoff_dt=snapshot_cutoff_dt,
    )

    print()
    print(_format_section_title(format_race_header(payload, race_id), args))
    print(f"Startlist entries: {len(ctx['entries'])}")
    print()

    render_startlist_analysis(ctx, args)

    return 0


def _render_postevent_standings(
    args: argparse.Namespace,
    season_id: str,
    disciplines_raced: set[tuple[str, str]],
    output_format: OutputFormat,
) -> None:
    """Render WC standings sections for a post-event summary."""
    STANDINGS_TOP_N = 10
    DISC_ORDER = ["SP", "PU", "IN", "MS"]
    DISC_LABELS = {
        "SP": "Sprint",
        "PU": "Pursuit",
        "IN": "Individual",
        "MS": "Mass Start",
    }

    try:
        cups = get_cups(season_id)
    except BiathlonError:
        return

    # Build cup_id lookup: (cat_id, disc) -> cup_id  (Level=1 only)
    cup_map: dict[tuple[str, str], str] = {}
    for cup in cups:
        if cup.get("Level") != 1:
            continue
        cat = str(cup.get("CatId") or "").upper()
        disc = str(cup.get("DisciplineId") or "").upper()
        cup_id = str(cup.get("CupId") or "")
        if cat and disc and cup_id:
            cup_map.setdefault((cat, disc), cup_id)

    def _fetch_top_n(cat: str, disc: str) -> list[dict]:
        cup_id = cup_map.get((cat, disc))
        if not cup_id:
            return []
        try:
            payload = get_cup_results(cup_id)
            rows = payload.get("Rows") or payload.get("Results") or []
            return list(rows)[:STANDINGS_TOP_N]
        except BiathlonError:
            return []

    def _render_standings(title: str, rows: list[dict]) -> None:
        if not rows:
            return
        print(_format_section_title(title, args))
        table_rows = []
        for i, row in enumerate(rows, 1):
            rank = row.get("Rank") or row.get("Standing") or str(i)
            name = row.get("Name") or row.get("ShortName") or ""
            nat = row.get("Nat") or ""
            score = row.get("Score") or row.get("Points") or row.get("TotalScore") or ""
            table_rows.append([str(rank), name, nat, str(score)])
        render_table(
            ["Rank", "Athlete", "Nat", "Points"],
            table_rows,
            output_format=output_format,
        )
        print()

    print(_format_section_title("WC Standings", args))
    print()

    _render_standings("Overall — Women", _fetch_top_n("SW", "TS"))
    _render_standings("Overall — Men", _fetch_top_n("SM", "TS"))

    disc_set = {disc for disc, _cat in disciplines_raced}
    for disc in DISC_ORDER:
        if disc not in disc_set:
            continue
        disc_label = DISC_LABELS.get(disc, disc)
        _render_standings(f"{disc_label} — Women", _fetch_top_n("SW", disc))
        _render_standings(f"{disc_label} — Men", _fetch_top_n("SM", disc))


def handle_brief_postevent(args: argparse.Namespace) -> int:
    """Post-event recap: career milestones across all races of an event, plus WC standings."""

    MAJOR_LEVELS = {"WC", "WCH", "OWG"}

    # --- 1. Resolve event ---
    event_id: str = getattr(args, "event", "") or ""
    current_event: dict | None = None

    if event_id:
        season_id_for_lookup = get_current_season_id()
        for level in (1, 2, 3):
            try:
                evts = get_events(season_id_for_lookup, level)
            except BiathlonError:
                continue
            for ev in evts:
                if ev.get("EventId") == event_id:
                    current_event = ev
                    break
            if current_event:
                break
    else:
        try:
            season_id_for_lookup = get_current_season_id()
            all_events = get_events(season_id_for_lookup, level=1)
        except BiathlonError as exc:
            print(str(exc), file=sys.stderr)
            return 1

        today = datetime.date.today()
        dated: list[tuple[datetime.date, dict]] = []
        for ev in all_events:
            end_raw = ev.get("EndDate") or ev.get("StartDate") or ""
            end_str = str(end_raw).split("T", 1)[0] if end_raw else ""
            try:
                end_date = datetime.date.fromisoformat(end_str)
            except ValueError:
                continue
            if end_date <= today:
                dated.append((end_date, ev))
        dated.sort(key=lambda x: x[0], reverse=True)
        candidates = [ev for _, ev in dated[:5]]

        if not candidates:
            print("No completed World Cup events found this season", file=sys.stderr)
            return 1

        if len(candidates) == 1 or not sys.stdin.isatty():
            current_event = candidates[0]
            event_id = str(current_event.get("EventId") or "")
        else:
            print("\nRecent events:\n", file=sys.stderr)
            for idx, ev in enumerate(candidates, 1):
                eid = ev.get("EventId") or "?"
                desc = (
                    ev.get("ShortDescription")
                    or ev.get("Description")
                    or ev.get("Organizer")
                    or eid
                )
                ev_type = detect_event_type(ev)
                ev_type_label = EVENT_TYPE_LABELS.get(ev_type, ev_type)
                start_str = str(ev.get("StartDate") or "").split("T", 1)[0]
                end_str = str(ev.get("EndDate") or "").split("T", 1)[0]
                date_range = f"{start_str} – {end_str}" if end_str else start_str
                print(
                    f"  {idx}. [{ev_type_label}] {desc}  ({date_range})  [ID: {eid}]",
                    file=sys.stderr,
                )
            print(file=sys.stderr)
            while True:
                try:
                    choice = input(f"Enter selection (1-{len(candidates)}): ").strip()
                    idx = int(choice) - 1
                    if 0 <= idx < len(candidates):
                        current_event = candidates[idx]
                        event_id = str(current_event.get("EventId") or "")
                        break
                    print("Invalid selection, try again.", file=sys.stderr)
                except ValueError:
                    print("Please enter a number.", file=sys.stderr)
                except (EOFError, KeyboardInterrupt):
                    print("Selection cancelled", file=sys.stderr)
                    return 1

    if not event_id:
        print("Could not determine event ID", file=sys.stderr)
        return 1

    # --- 2. Detect event type and milestone level ---
    event_type = detect_event_type(current_event) if current_event else EVENT_TYPE_WC
    # --- 3. Fetch races ---
    try:
        all_races = get_races(event_id)
    except BiathlonError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if not all_races:
        print(f"No races found for event {event_id}", file=sys.stderr)
        return 1

    all_races.sort(key=lambda r: r.get("StartTime") or r.get("StartDate") or "")
    race_ids = [
        str(r.get("RaceId") or r.get("Id") or "")
        for r in all_races
        if r.get("RaceId") or r.get("Id")
    ]

    # --- 4. Parallel-fetch race results ---
    race_payloads: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=_max_workers(len(race_ids))) as executor:
        futures: dict[Any, str] = {
            executor.submit(get_race_results, rid): rid for rid in race_ids
        }
        for future in as_completed(futures):
            rid = futures[future]
            try:
                race_payloads[rid] = future.result()
            except BiathlonError:
                pass

    # Filter to completed races (in chronological order)
    completed_races: list[tuple[str, dict]] = []
    for rid in race_ids:
        payload = race_payloads.get(rid)
        if not payload:
            continue
        comp = payload.get("Competition") or {}
        disc = str(comp.get("DisciplineId") or "").upper()
        if is_relay_discipline(disc):
            if _has_completed_relay_results(payload):
                completed_races.append((rid, payload))
        else:
            if _has_completed_results(payload):
                completed_races.append((rid, payload))

    if not completed_races:
        print(f"No completed races found for event {event_id}", file=sys.stderr)
        return 1

    # --- 5. Collect IBU IDs and parallel-fetch AllResults ---
    all_ibu_ids: set[str] = set()
    for _rid, payload in completed_races:
        for res in payload.get("Results") or []:
            if not res.get("IsTeam"):
                ibu_id = str(res.get("IBUId") or "")
                if ibu_id:
                    all_ibu_ids.add(ibu_id)

    all_results_cache: dict[str, list[dict]] = {}
    if all_ibu_ids:
        ibu_list = list(all_ibu_ids)
        with ThreadPoolExecutor(max_workers=_max_workers(len(ibu_list))) as executor:
            futures2: dict[Any, str] = {
                executor.submit(get_all_results, iid): iid for iid in ibu_list
            }
            for future in as_completed(futures2):
                iid = futures2[future]
                try:
                    ar = future.result()
                    all_results_cache[iid] = list(ar.get("Results") or [])
                except BiathlonError:
                    all_results_cache[iid] = []

    # --- 6. Process each race ---
    output_format = get_output_format(args)
    race_start_cache: dict[str, datetime.datetime | None] = {}
    warning_keys: set[str] = set()
    season_id = ""
    disciplines_raced: set[tuple[str, str]] = set()
    any_race_had_results = False

    MAJOR_LEVELS = {"WC", "WCH", "OWG"}

    def _prev_label(value: int | None, scope: str) -> str:
        if value is None:
            return f"none ({scope})"
        return f"{_ordinal(value)} ({scope})"

    print()

    for race_id, payload in completed_races:
        comp = payload.get("Competition") or {}
        sport_evt = payload.get("SportEvt") or {}
        disc = str(comp.get("DisciplineId") or "").upper()
        cat_id = str(comp.get("catId") or comp.get("CatId") or "").upper()
        is_relay = is_relay_discipline(disc)
        target_start_dt = _start_dt_from_competition(comp)
        race_start_cache[race_id] = target_start_dt

        if not season_id:
            season_id = str(sport_evt.get("SeasonId") or "")
            if not season_id and race_id.upper().startswith("BT") and len(race_id) >= 6:
                season_id = race_id[2:6]

        if not is_relay:
            disciplines_raced.add((disc, cat_id))

        results = list(payload.get("Results") or [])
        team_results = [r for r in results if r.get("IsTeam")]
        leg_results = [r for r in results if not r.get("IsTeam")]
        entries = [
            {
                "ibu_id": str(r.get("IBUId") or ""),
                "name": r.get("Name") or r.get("ShortName") or "",
                "nat": r.get("Nat") or "",
                "bib": str(r.get("Bib") or ""),
                "rank": r.get("Rank") or r.get("ResultOrder") or "",
                "irm": str(r.get("IRM") or "").upper(),
                "time": str(r.get("TotalTime") or r.get("Result") or ""),
            }
            for r in leg_results
        ]

        # Build team rank lookup for relay races
        team_rank_by_bib: dict[str, int] = {}
        if is_relay:
            for team in team_results:
                bib = str(team.get("Bib") or "")
                rv = _parse_rank(team.get("Rank") or team.get("SO"))
                if bib and rv is not None:
                    team_rank_by_bib[bib] = rv

        # Discipline labels for best-performance messages
        disc_label = DISCIPLINE_NAMES.get(disc, disc)
        disc_label_lc = disc_label.lower()
        if is_relay:
            all_label = "Best Relay Results (all discipline)"
            discipline_label = f"Best Relay Results ({disc_label_lc})"
        else:
            all_label = "Best Individual Result (all discipline)"
            discipline_label = f"Best Individual Results ({disc_label_lc})"

        # Best career performance detection
        seen_ids: set[str] = set()
        perf_rows: list[tuple[int, str, str, str, str]] = []

        for entry in entries:
            ibu_id = entry["ibu_id"]
            if not ibu_id or ibu_id in seen_ids:
                continue
            seen_ids.add(ibu_id)

            if is_relay:
                current_rank = team_rank_by_bib.get(entry["bib"])
            else:
                current_rank = _parse_rank(entry["rank"])

            # Skip lapped / invalid results
            if entry["irm"] == "LAP" or "LAP" in entry["time"].upper():
                continue
            if disc == "PU" and current_rank is not None and current_rank >= 10000:
                continue
            if current_rank is None:
                continue

            # Gather all major-level results up to (and including) this race
            major_ranked: list[tuple[dict, int]] = []
            for res in all_results_cache.get(ibu_id, []):
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
                rv = _parse_rank(
                    res.get("Rank") or res.get("SO") or res.get("ResultOrder")
                )
                if rv is None:
                    continue
                major_ranked.append((res, rv))

            prior_rows = [
                (res, rv)
                for res, rv in major_ranked
                if str(res.get("RaceId") or "") != race_id
            ]
            prior_same_type = [
                (res, rv)
                for res, rv in prior_rows
                if _is_team_level_result(res) == is_relay
            ]
            prior_best_all = min((rv for _, rv in prior_same_type), default=None)
            prior_best_disc = min(
                (rv for res, rv in prior_rows if _result_discipline_id(res) == disc),
                default=None,
            )

            is_best_all = prior_best_all is None or current_rank < prior_best_all
            is_best_disc = prior_best_disc is None or current_rank < prior_best_disc
            if not is_best_all and not is_best_disc:
                continue

            if is_best_all:
                perf_rows.append(
                    (
                        current_rank,
                        entry["name"],
                        entry["nat"],
                        all_label,
                        _prev_label(prior_best_all, "all discipline"),
                    )
                )
            else:
                perf_rows.append(
                    (
                        current_rank,
                        entry["name"],
                        entry["nat"],
                        discipline_label,
                        _prev_label(prior_best_disc, disc_label_lc),
                    )
                )

        if not perf_rows:
            continue

        any_race_had_results = True
        perf_rows.sort(key=lambda r: (r[0], r[1]))

        print(_format_section_title(format_race_header(payload, race_id), args))
        print()

        print(_format_section_title("Best Performances:", args))
        table_rows = []
        row_styles = []
        for rank_val, name, nat, milestone, previous_best in perf_rows:
            table_rows.append([str(rank_val), name, nat, milestone, previous_best])
            if rank_val == 1:
                row_styles.append("gold")
            elif rank_val == 2:
                row_styles.append("silver")
            elif rank_val == 3:
                row_styles.append("bronze")
            else:
                row_styles.append("dim")
        render_table(
            ["Rank", "Athlete", "Nat", "Milestone", "Previous Best"],
            table_rows,
            output_format=output_format,
            row_styles=row_styles,
        )
        print()

    if not any_race_had_results:
        print(_format_section_title("No best career performances this event.", args))
        print()

    # --- 7. WC standings (WC events only) ---
    if event_type == EVENT_TYPE_WC and season_id:
        _render_postevent_standings(args, season_id, disciplines_raced, output_format)

    return 0


def handle_brief_preseason(args: argparse.Namespace) -> int:
    """Season preview (before the season starts). Work in progress."""
    print("brief preseason: work in progress", file=sys.stderr)
    return 1


def handle_brief_postrace(args: argparse.Namespace) -> int:
    """Display post-race analysis (after a race).

    Delegates to the existing postrace handler.
    """
    return handle_post_race(args)
