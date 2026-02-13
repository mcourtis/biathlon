"""Brief command handlers for race analysis."""

from __future__ import annotations

import argparse
import datetime
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..api import BiathlonError, get_events, get_race_results, get_races, get_current_season_id, get_seasons
from ..constants import (
    CATEGORY_DISPLAY_NAMES,
    DISCIPLINE_NAMES,
    EVENT_TYPE_LABELS,
    EVENT_TYPE_OWG,
    EVENT_TYPE_WC,
    EVENT_TYPE_WCH,
)
from ..formatting import is_pretty_output, Color, render_table
from ..utils import format_race_header, parse_date
from .events import compute_event_styles, format_level

from ._common import _format_section_title, _max_workers, _row_ibu_id, detect_event_type, is_relay_discipline
from .post_race import handle_post_race
from .results import _get_wc_rows
from .startlist import (
    _build_startlist_entries,
    _build_team_entries,
    _extract_venue_name,
    _find_all_startlist_races,
    _get_all_olympic_medals,
    _get_past_olympic_relay_podiums,
    _is_true,
    _parse_leg,
    _prepare_startlist_context,
    _select_race_interactive,
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
        print(_format_section_title(f"Most decorated {gender_label.lower()} at {location_label}: no data", args))
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
            reverse=True
        )
    else:
        alltime_decorated = [s for s in alltime_stats if s["wins"] > 0]
        alltime_decorated.sort(key=lambda s: (s["wins"], s["podiums"], s["flowers"], -s["races"]), reverse=True)
    alltime_decorated = alltime_decorated[:10]

    if not alltime_decorated:
        print(_format_section_title(f"Most decorated {gender_label.lower()} at {location_label}: no winners found", args))
        print()
    else:
        print(_format_section_title(f"Most decorated {gender_label.lower()} at {location_label}:", args))
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
                venue_rows.append([
                    idx + 1,
                    stats["name"],
                    gold,
                    silver,
                    bronze,
                    total,
                    stats["races"],
                ])
            else:
                venue_rows.append([
                    idx + 1,
                    stats["name"],
                    stats["wins"],
                    stats["podiums"],
                    stats["flowers"],
                    stats["races"],
                ])

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
            pretty=is_pretty_output(args),
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
            reverse=True
        )
    else:
        alltime_experienced.sort(key=lambda s: (s["races"], s["wins"], s["podiums"], s["flowers"]), reverse=True)
    alltime_experienced = alltime_experienced[:10]

    if not alltime_experienced:
        print(_format_section_title(f"Most experienced {gender_label.lower()} at {location_label}: no data", args))
        print()
        return

    print(_format_section_title(f"Most experienced {gender_label.lower()} at {location_label}:", args))
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
            venue_rows.append([
                idx + 1,
                stats["name"],
                stats["races"],
                gold,
                silver,
                bronze,
                total,
            ])
        else:
            venue_rows.append([
                idx + 1,
                stats["name"],
                stats["races"],
                stats["wins"],
                stats["podiums"],
                stats["flowers"],
            ])

    def highlight_cell(cell_str: str, row_idx: int) -> str:
        return Color.highlight(cell_str) if row_idx in highlight_rows else cell_str

    if use_medal_columns:
        headers = ["#", "Athlete", "Races", "Gold", "Silver", "Bronze", "Total"]
        formatters = [None, highlight_cell, None, None, None, None, None]
    else:
        headers = ["#", "Athlete", "Races", "Wins", "Podiums", "Flowers"]
        formatters = [None, highlight_cell, None, None, None, None]

    render_table(
        headers,
        venue_rows,
        pretty=is_pretty_output(args),
        cell_formatters=formatters,
    )
    print()


def handle_brief_event(args: argparse.Namespace) -> int:
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
        return _get_alltime_venue_stats(venue_name, "SM", use_major, show_progress=False)

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
                ev_year = season_years.get(ev.get("season_id"))
                if ev_year and ev_year > max_event_year:
                    continue

                event_data = ev.get("event", {})
                desc = str(event_data.get("Description") or event_data.get("ShortDescription") or "").lower()
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
                first_season = min(wc_season_years.keys(), key=lambda s: wc_season_years[s])
                first_year = wc_season_years[first_season]
                # Format season (e.g., "1112" -> "2011/2012")
                s = str(first_season)
                if len(s) >= 4:
                    season_display = f"20{s[:2]}/20{s[2:]}" if int(s[:2]) < 50 else f"19{s[:2]}/19{s[2:]}"
                else:
                    season_display = s
            else:
                first_year = None
                season_display = ""

            # Get years for WC events (already filtered to exclude future)
            wc_years = sorted({
                season_years[ev["season_id"]]
                for ev in wc_events
                if ev.get("season_id") in season_years and season_years[ev["season_id"]] <= max_event_year
            })
            wc_years_str = ", ".join(str(y) for y in wc_years)

            # Get years for WCH events (already filtered to exclude future)
            wch_years = sorted({
                season_years[ev["season_id"]]
                for ev in wch_events
                if ev.get("season_id") in season_years and season_years[ev["season_id"]] <= max_event_year
            })
            wch_years_str = ", ".join(str(y) for y in wch_years)

            # Get years for OWG events (already filtered to exclude future)
            owg_years = sorted({
                season_years[ev["season_id"]]
                for ev in owg_events
                if ev.get("season_id") in season_years and season_years[ev["season_id"]] <= max_event_year
            })
            owg_years_str = ", ".join(str(y) for y in owg_years)

            print(_format_section_title("Event Facts:", args))
            print(f"  Event type: {EVENT_TYPE_LABELS.get(event_type, 'World Cup')}")
            if first_year:
                print(f"  First World Cup event: {first_year} (season {season_display})")
            print(f"  Total World Cup events: {len(wc_events)} ({wc_years_str})")
            if wch_events:
                print(f"  Total World Championship events: {len(wch_events)} ({wch_years_str})")
            if owg_events:
                print(f"  Total Olympic Games events: {len(owg_events)} ({owg_years_str})")
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
            pretty=is_pretty_output(args),
        )
        print()

    # History for women (highlight athletes active in current WC season)
    _render_venue_history_table(
        women_stats, women_active_ids, location_label, "Women", args,
        use_medal_columns=is_major_event,
    )

    # History for men (highlight athletes active in current WC season)
    _render_venue_history_table(
        men_stats, men_active_ids, location_label, "Men", args,
        use_medal_columns=is_major_event,
    )

    return 0


def handle_brief_season(args: argparse.Namespace) -> int:
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

    season_entry = next((s for s in seasons if str(s.get("SeasonId")) == season_id), None)
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
        key=lambda event: event.get("StartDate") or event.get("FirstCompetitionDate") or "",
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
        flags = {
            "individual": set(),
            "sprint": set(),
            "pursuit": set(),
            "mass": set(),
            "relay": set(),
            "mixed_relay": False,
            "single_mixed": False,
        }

        for race in race_list:
            name = (race.get("RaceName") or race.get("ShortDescription") or race.get("Description") or "").lower()
            disc = (race.get("DisciplineId") or "").upper()
            gender_tag = ""
            if "women" in name or "women's" in name:
                gender_tag = "W"
            elif "men" in name:
                gender_tag = "M"

            if disc in {"IN", "SI"}:
                flags["individual"].add(gender_tag or "W+M")
            elif disc == "SP":
                flags["sprint"].add(gender_tag or "W+M")
            elif disc == "PU":
                flags["pursuit"].add(gender_tag or "W+M")
            elif disc == "MS":
                flags["mass"].add(gender_tag or "W+M")
            elif disc == "RL" or "relay" in name:
                if "single" in name and "mixed" in name:
                    flags["single_mixed"] = True
                elif "mixed" in name:
                    flags["mixed_relay"] = True
                else:
                    flags["relay"].add(gender_tag or "W+M")

        agenda_rows.append([
            event.get("Description") or "",
            event.get("ShortDescription") or event.get("Organizer") or "",
            event.get("Nat") or event.get("Nation") or event.get("CountryId") or event.get("Country") or "",
            date_only(event.get("StartDate") or event.get("FirstCompetitionDate") or ""),
            len(race_list),
            mark(flags["individual"]),
            mark(flags["sprint"]),
            mark(flags["pursuit"]),
            mark(flags["mass"]),
            mark(flags["relay"]),
            mark(flags["mixed_relay"]),
            mark(flags["single_mixed"]),
        ])

    if agenda_rows:
        print(_format_section_title("Agenda:", args))
        agenda_styles = compute_event_styles(agenda_events) if pretty else None
        render_table(
            [
                "Event", "Location", "Country", "StartDate",
                "Races", "Individual", "Sprint", "Pursuit", "MassStart",
                "Relay", "MixedRelay", "SingleMixedRelay",
            ],
            agenda_rows,
            pretty=pretty,
            row_styles=agenda_styles,
            cell_formatters=[None, None, None, None, None, None, None, None, None, None, None, None],
        )
        print()

    decorated_stats = {"SW": {}, "SM": {}}
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
                    rank_val = parse_rank(res.get("Rank") or res.get("SO") or res.get("ResultOrder"))
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
            key=lambda s: (s["wins"], s["podiums"], s["flowers"], -s["races"]), reverse=True
        )
        decorated = decorated[:10]
        if decorated:
            print(_format_section_title(f"Top Decorated {label}:", args))
            rows = []
            for idx, stats in enumerate(decorated, start=1):
                total = stats["wins"] + stats["podiums"] + stats["flowers"]
                rows.append([
                    idx,
                    stats["name"],
                    stats["nat"],
                    stats["wins"],
                    stats["podiums"],
                    stats["flowers"],
                    total,
                    stats["races"],
                ])

            render_table(
                ["Rank", "Athlete", "Nat", "Wins", "Podiums", "Flowers", "Total", "Races"],
                rows,
                pretty=pretty,
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
                rows.append([cat_labels.get(code, code), category_counts[code]])
        for code in sorted(k for k in category_counts if k not in {"SW", "SM", "MX"}):
            rows.append([cat_labels.get(code, code), category_counts[code]])
        print(_format_section_title("Race categories:", args))
        render_table(["Category", "Races"], rows, pretty=pretty)
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
                rows.append([disc_labels.get(code, code), discipline_counts[code]])
        for code in sorted(k for k in discipline_counts if k not in order):
            rows.append([disc_labels.get(code, code), discipline_counts[code]])
        print(_format_section_title("Race disciplines:", args))
        render_table(["Discipline", "Races"], rows, pretty=pretty)
        print()

    render_decorated_section("Women", decorated_stats["SW"])
    render_decorated_section("Men", decorated_stats["SM"])

    return 0


def _get_startlist_athletes(payload: dict) -> set[str]:
    """Get family names of athletes from the startlist (individual entries only)."""
    athletes: set[str] = set()
    for res in payload.get("Results", []) or []:
        if res.get("IsTeam"):
            continue
        family_name = res.get("FamilyName") or ""
        if family_name:
            athletes.add(family_name)
    return athletes


def _build_team_rosters(payload: dict) -> dict[str, list[str]]:
    """Build athlete name lists keyed by team (bib or nat) for relay startlists."""
    rosters: dict[str, list[tuple[int | None, str]]] = {}
    for res in payload.get("Results", []) or []:
        if res.get("IsTeam"):
            continue
        name = res.get("FamilyName") or res.get("ShortName") or res.get("Name") or ""
        if not name:
            continue
        bib = str(res.get("Bib") or "")
        nat = str(res.get("Nat") or "")
        key = f"bib:{bib}" if bib else (f"nat:{nat}" if nat else "")
        if not key:
            continue
        leg = _parse_leg(res.get("Leg"))
        rosters.setdefault(key, []).append((leg, name))

    formatted: dict[str, list[str]] = {}
    for key, entries in rosters.items():
        entries.sort(key=lambda x: (x[0] is None, x[0] or 0, x[1]))
        formatted[key] = [name for _, name in entries]
    return formatted


def _get_season_athlete_info(race_id: str, category: str = "") -> dict[str, dict[str, str]]:
    """Get info for athletes who participated in any WC race this season.

    Returns dict mapping ibu_id -> {"name": family_name, "nat": nation_code}.
    Optionally filters races by *category* (e.g. "SW").
    """
    # Extract season from race_id (e.g., BT2526... -> 2526)
    season_id = ""
    if len(race_id) >= 6 and race_id[:2] == "BT":
        season_id = race_id[2:6]
    if not season_id:
        return {}

    athletes: dict[str, dict[str, str]] = {}
    try:
        # Get WC events for this season
        events = get_events(season_id, level=1)
        for event in events:
            event_id = event.get("EventId") or ""
            if not event_id:
                continue
            try:
                races = get_races(event_id)
            except BiathlonError:
                continue
            for race in races:
                # Optionally filter by category
                if category:
                    race_cat = str(race.get("catId") or race.get("CatId") or "").upper()
                    if race_cat != category:
                        continue
                rid = race.get("RaceId") or ""
                if not rid or rid == race_id:
                    continue
                try:
                    results = get_race_results(rid)
                except BiathlonError:
                    continue
                if not results.get("IsResult"):
                    continue
                for res in results.get("Results", []) or []:
                    if res.get("IsTeam"):
                        continue
                    ibu_id = _row_ibu_id(res)
                    if not ibu_id:
                        continue
                    family_name = res.get("FamilyName") or ""
                    nat = res.get("Nat") or ""
                    if family_name and ibu_id not in athletes:
                        athletes[ibu_id] = {"name": family_name, "nat": nat}
    except BiathlonError:
        pass
    return athletes


def _render_team_startlist(
    payload: dict,
    race_id: str,
    team_entries: list[dict],
    args: argparse.Namespace,
) -> int:
    """Render a relay startlist with team entries and Olympic history."""
    pretty = is_pretty_output(args)
    comp = payload.get("Competition") or {}
    discipline = str(comp.get("DisciplineId") or "").upper()
    category = str(comp.get("catId") or comp.get("CatId") or "").upper()
    results = payload.get("Results", []) or []
    has_individual_entries = any(not res.get("IsTeam") for res in results)
    team_rosters = _build_team_rosters(payload)
    has_rosters = any(team_rosters.values())
    startlist_info: dict[str, dict[str, str]] = {}
    for res in results:
        if res.get("IsTeam"):
            continue
        ibu_id = str(res.get("IBUId") or res.get("IbuId") or "")
        if not ibu_id:
            continue
        name = res.get("FamilyName") or res.get("ShortName") or res.get("Name") or ""
        nat = res.get("Nat") or ""
        if ibu_id not in startlist_info:
            startlist_info[ibu_id] = {"name": name, "nat": nat}
    startlist_ids = set(startlist_info.keys())
    event_type = detect_event_type(payload.get("SportEvt") or {})
    is_provisional = not startlist_ids

    # Parallelize independent fetches
    podiums: list[dict] = []
    all_country_medals: list[dict] = []
    all_athlete_stats: dict[str, dict] = {}
    season_athlete_info: dict[str, dict[str, str]] = {}

    with ThreadPoolExecutor(max_workers=3) as executor:
        podiums_future = executor.submit(_get_past_olympic_relay_podiums, discipline, category)
        medals_future = (
            executor.submit(_get_all_olympic_medals, category)
            if event_type == EVENT_TYPE_OWG else None
        )
        season_info_future = (
            executor.submit(_get_season_athlete_info, race_id, category)
            if is_provisional else None
        )

        podiums = podiums_future.result()
        if medals_future is not None:
            all_country_medals, all_athlete_stats = medals_future.result()
        if season_info_future is not None:
            season_athlete_info = season_info_future.result()

    print()
    print(_format_section_title(format_race_header(payload, race_id), args))
    status = comp.get("StatusText") or ""
    if status:
        print(f"Status: {status}")
    print(f"Teams: {len(team_entries)}")
    print()

    # Section 1: Team list
    print(_format_section_title("1. Participating teams:", args))
    rows = []
    headers = ["Bib", "Team", "Nat"]
    if has_rosters:
        headers.extend(["Athlete 1", "Athlete 2", "Athlete 3", "Athlete 4"])
    for entry in team_entries:
        row = [entry["bib"], entry["name"], entry["nat"]]
        if has_rosters:
            bib = str(entry.get("bib") or "")
            nat = str(entry.get("nat") or "")
            roster = team_rosters.get(f"bib:{bib}") if bib else None
            if not roster and nat:
                roster = team_rosters.get(f"nat:{nat}")
            roster = roster or []
            padded = (roster + ["-"] * 4)[:4]
            row.extend(padded)
        rows.append(row)
    render_table(headers, rows, pretty=pretty)
    print()

    # Section 2: Past Olympic podiums
    if podiums:
        # Get athletes for highlighting: prefer startlist athletes, fall back to season athletes
        startlist_athletes = _get_startlist_athletes(payload)
        if has_individual_entries:
            highlight_athletes = startlist_athletes
        else:
            highlight_athletes = (
                startlist_athletes
                if startlist_athletes
                else {info["name"] for info in season_athlete_info.values()}
            )

        disc_name = DISCIPLINE_NAMES.get(discipline, discipline)
        cat_name = CATEGORY_DISPLAY_NAMES.get(category, category)
        print(_format_section_title(f"2. Past Olympic {cat_name} {disc_name} podiums:", args))

        def format_athlete_names(athletes: list[dict]) -> str:
            """Format athlete names: highlight active, dim retired."""
            names = []
            for a in athletes:
                name = a.get("name", "")
                if name in highlight_athletes:
                    names.append(Color.highlight(name))
                else:
                    names.append(Color.dim(name))
            return "/".join(names)

        podium_rows = []
        for p in podiums:
            # Row 1: Year, Venue, Country names
            podium_rows.append([
                p["year"],
                p["venue"],
                p["gold"],
                p["silver"],
                p["bronze"],
            ])
            # Row 2: Empty, Empty, Athlete names (if available)
            gold_athletes = p.get("gold_athletes", [])
            silver_athletes = p.get("silver_athletes", [])
            bronze_athletes = p.get("bronze_athletes", [])
            if gold_athletes or silver_athletes or bronze_athletes:
                podium_rows.append([
                    "",
                    "",
                    format_athlete_names(gold_athletes),
                    format_athlete_names(silver_athletes),
                    format_athlete_names(bronze_athletes),
                ])
        render_table(
            ["Year", "Venue", Color.gold("Gold"), Color.silver("Silver"), Color.bronze("Bronze")],
            podium_rows,
            pretty=pretty,
        )
        print()

        # Section 3: Medal table by country (discipline-specific)
        # Count medals per country
        medal_counts: dict[str, dict[str, int]] = {}
        for p in podiums:
            for medal_type, key in [("gold", "gold"), ("silver", "silver"), ("bronze", "bronze")]:
                country = p[key]
                if country:
                    # Extract country name (remove " (NAT)" suffix if present)
                    if " (" in country:
                        country = country.split(" (")[0]
                    if country not in medal_counts:
                        medal_counts[country] = {"gold": 0, "silver": 0, "bronze": 0}
                    medal_counts[country][medal_type] += 1

        # Sort by gold, then silver, then bronze
        sorted_countries = sorted(
            medal_counts.items(),
            key=lambda x: (x[1]["gold"], x[1]["silver"], x[1]["bronze"]),
            reverse=True,
        )

        print(_format_section_title(f"3. Country medal table ({cat_name} {disc_name}):", args))
        medal_rows = []
        for idx, (country, counts) in enumerate(sorted_countries, 1):
            total = counts["gold"] + counts["silver"] + counts["bronze"]
            medal_rows.append([
                str(idx),
                country,
                str(counts["gold"]),
                str(counts["silver"]),
                str(counts["bronze"]),
                str(total),
            ])
        render_table(
            ["#", "Country", Color.gold("Gold"), Color.silver("Silver"), Color.bronze("Bronze"), "Total"],
            medal_rows,
            pretty=pretty,
        )
        print()

        # Section 4: Medal table by country (all Olympic disciplines)
        if event_type == EVENT_TYPE_OWG and all_country_medals:
            all_country_counts: dict[str, dict[str, int]] = {}
            for m in all_country_medals:
                for medal_type in ("gold", "silver", "bronze"):
                    nat = m.get(medal_type, "")
                    if nat:
                        if nat not in all_country_counts:
                            all_country_counts[nat] = {"gold": 0, "silver": 0, "bronze": 0}
                        all_country_counts[nat][medal_type] += 1

            sorted_all_countries = sorted(
                all_country_counts.items(),
                key=lambda x: (x[1]["gold"], x[1]["silver"], x[1]["bronze"]),
                reverse=True,
            )

            print(_format_section_title("4. Country medal table (all Olympic disciplines):", args))
            all_country_rows = []
            for idx, (country, counts) in enumerate(sorted_all_countries, 1):
                total = counts["gold"] + counts["silver"] + counts["bronze"]
                all_country_rows.append([
                    str(idx),
                    country,
                    str(counts["gold"]),
                    str(counts["silver"]),
                    str(counts["bronze"]),
                    str(total),
                ])
            render_table(
                ["#", "Country", Color.gold("Gold"), Color.silver("Silver"), Color.bronze("Bronze"), "Total"],
                all_country_rows,
                pretty=pretty,
            )
            print()

        # Section 5: Medal table by athlete (discipline-specific)
        # Track athlete data: full_name -> {family_name, nat, gender, gold, silver, bronze, races}
        athlete_counts: dict[str, dict] = {}
        for p in podiums:
            for medal_type, athletes_key in [
                ("gold", "gold_athletes"),
                ("silver", "silver_athletes"),
                ("bronze", "bronze_athletes"),
            ]:
                for athlete in p.get(athletes_key, []):
                    if not athlete:
                        continue
                    full_name = athlete.get("full_name") or athlete.get("name", "")
                    family_name = athlete.get("name", "")
                    if not full_name:
                        continue
                    if full_name not in athlete_counts:
                        athlete_counts[full_name] = {
                            "family_name": family_name,
                            "nat": athlete.get("nat", ""),
                            "gender": athlete.get("gender", ""),
                            "gold": 0,
                            "silver": 0,
                            "bronze": 0,
                            "races": 0,
                        }
                    athlete_counts[full_name][medal_type] += 1
                    athlete_counts[full_name]["races"] += 1

        # Filter to gold medalists, sort by gold, silver, bronze, total medals, total races
        sorted_athletes = sorted(
            ((k, v) for k, v in athlete_counts.items() if v["gold"] > 0),
            key=lambda x: (
                x[1]["gold"],
                x[1]["silver"],
                x[1]["bronze"],
                x[1]["gold"] + x[1]["silver"] + x[1]["bronze"],
                x[1]["races"],
            ),
            reverse=True,
        )

        print(_format_section_title(f"5. Athlete medal table ({cat_name} {disc_name}):", args))
        athlete_rows = []
        row_styles = []
        for idx, (full_name, counts) in enumerate(sorted_athletes, 1):
            total = counts["gold"] + counts["silver"] + counts["bronze"]
            athlete_rows.append([
                str(idx),
                full_name,
                counts["nat"],
                counts["gender"],
                str(counts["gold"]),
                str(counts["silver"]),
                str(counts["bronze"]),
                str(total),
                str(counts["races"]),
            ])
            # Highlight active athletes, dim retired
            if counts["family_name"] in highlight_athletes:
                row_styles.append("highlight")
            else:
                row_styles.append("dim")
        render_table(
            ["#", "Athlete", "Nat", "Gender", Color.gold("Gold"), Color.silver("Silver"), Color.bronze("Bronze"), "Total", "Races"],
            athlete_rows,
            pretty=pretty,
            row_styles=row_styles,
        )
        print()

    # Section 6: Medal table (athletes, all Olympic disciplines)
    if event_type == EVENT_TYPE_OWG:
        # Filter to gold medalists only
        medalists = [
            (key, stats) for key, stats in all_athlete_stats.items()
            if stats["gold"] > 0
        ]
        medalists.sort(
            key=lambda x: (
                -x[1]["gold"],
                -x[1].get("gold_ind", 0),
                -x[1].get("gold_relay", 0),
                -x[1]["silver"],
                -x[1].get("silver_ind", 0),
                -x[1].get("silver_relay", 0),
                -x[1]["bronze"],
                -x[1].get("bronze_ind", 0),
                -x[1].get("bronze_relay", 0),
                -(x[1]["gold"] + x[1]["silver"] + x[1]["bronze"]),
                -(x[1].get("gold_ind", 0) + x[1].get("silver_ind", 0) + x[1].get("bronze_ind", 0)),
                -(x[1].get("gold_relay", 0) + x[1].get("silver_relay", 0) + x[1].get("bronze_relay", 0)),
                x[1]["races"],
            ),
        )

        if not medalists:
            print(_format_section_title("6. Athlete medal table (all Olympic disciplines): none", args))
            print()
        else:
            print(_format_section_title("6. Athlete medal table (all Olympic disciplines):", args))
            # Highlight: season athletes (provisional) or startlist athletes (non-provisional)
            highlight_ids = (
                set(season_athlete_info.keys()) if is_provisional
                else startlist_ids
            )
            all_rows = []
            all_row_styles = []
            for idx, (key, stats) in enumerate(medalists, 1):
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
                all_rows.append([
                    str(idx),
                    stats["name"],
                    stats["nat"],
                    stats["gender"],
                    str(gold), str(silver), str(bronze), str(total), str(races),
                    str(gold_ind), str(silver_ind), str(bronze_ind), str(total_ind), str(races_ind),
                    str(gold_relay), str(silver_relay), str(bronze_relay), str(total_relay), str(races_relay),
                ])
                if key in highlight_ids:
                    all_row_styles.append("highlight")
                else:
                    all_row_styles.append("dim")
            render_table(
                [
                    "#", "Athlete", "Nat", "Gender",
                    Color.gold("Gold"), Color.silver("Silver"), Color.bronze("Bronze"), "Total", "Races",
                    Color.gold("Gold"), Color.silver("Silver"), Color.bronze("Bronze"), "Total", "Races",
                    Color.gold("Gold"), Color.silver("Silver"), Color.bronze("Bronze"), "Total", "Races",
                ],
                all_rows,
                pretty=pretty,
                row_styles=all_row_styles,
                column_separators={4, 9, 14},
                group_headers=[(4, 9, "All"), (9, 14, "Individual"), (14, 19, "Relay")],
            )
            print()

    return 0


def handle_brief_startlist(args: argparse.Namespace) -> int:
    """Display startlist analysis (before a race).

    Shows sections 1-13 from the startlist analysis (World Cup races).
    Olympic races show Olympic history sections instead.
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
    team_entries = _build_team_entries(payload)
    comp = payload.get("Competition") or {}
    race_disc = str(comp.get("DisciplineId") or "").upper()
    event_type = detect_event_type(payload.get("SportEvt") or {})
    is_olympic_relay = event_type == EVENT_TYPE_OWG and is_relay_discipline(race_disc)

    if is_olympic_relay and team_entries:
        return _render_team_startlist(payload, race_id, team_entries, args)

    if not entries:
        # Try team entries for provisional relay startlists
        if team_entries:
            return _render_team_startlist(payload, race_id, team_entries, args)
        print(f"no startlist entries found for race {race_id}", file=sys.stderr)
        return 1

    args.leader_markers = True
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
