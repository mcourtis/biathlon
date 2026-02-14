"""Events command handler."""

from __future__ import annotations

import argparse
import datetime
import sys

from ..api import get_current_season_id, get_events, get_races, get_seasons
from ..formatting import is_pretty_output, render_table
from ..utils import get_race_label, parse_date


def format_level(level: int | str | None) -> str:
    """Return human-readable event level."""
    if level is None:
        return ""
    try:
        lvl_int = int(level)
    except (TypeError, ValueError):
        return str(level)
    level_names = {
        -1: "All levels",
        0: "Mixed levels",
        1: "World Cup",
        2: "IBU Cup",
        3: "IBU Cup Junior",
        4: "Other",
        5: "Regional",
        6: "Para-biathlon",
    }
    return level_names.get(lvl_int, str(lvl_int))


def format_event_row(event: dict, race_count: int = 0) -> list[str]:
    """Format an event dictionary for display."""
    season_id = event.get("SeasonId", "")
    event_id = event.get("EventId", "")
    label = event.get("Description") or ""
    short = event.get("ShortDescription") or event.get("Organizer") or ""
    country = (
        event.get("Nat")
        or event.get("Nation")
        or event.get("CountryId")
        or event.get("Country")
        or ""
    )
    level = event.get("Level") or ""

    start = event.get("StartDate") or event.get("FirstCompetitionDate") or ""
    start_date = start.split("T", 1)[0] if isinstance(start, str) else ""

    end = event.get("EndDate") or ""
    end_date = end.split("T", 1)[0] if isinstance(end, str) else ""

    return [
        season_id,
        format_level(level),
        label,
        short,
        country,
        start_date,
        end_date,
        str(race_count),
        event_id,
    ]


def compute_event_styles(events: list[dict]) -> list[str]:
    """Compute row styles for events based on their dates.

    Returns a list of style names: "dim" for past, "highlight" for current, "" for future.
    """
    today = datetime.date.today()
    styles: list[str] = []
    found_current = False

    for event in events:
        start = parse_date(event.get("StartDate") or event.get("FirstCompetitionDate"))
        end = parse_date(event.get("EndDate"))

        if start is None:
            styles.append("")
            continue

        event_end = end if end else start

        if event_end < today:
            styles.append("dim")
        elif start <= today <= event_end:
            styles.append("highlight")
            found_current = True
        elif not found_current:
            styles.append("highlight")
            found_current = True
        else:
            styles.append("")

    return styles


def handle_events(args: argparse.Namespace) -> int:
    """List events for the given seasons and levels."""
    try:
        level_int = int(args.level)
        if level_int < -1 or level_int > 6:
            raise ValueError
    except ValueError:
        print("error: level must be an integer between -1 and 6", file=sys.stderr)
        return 1

    if args.summary:
        level_int = 1

    if args.season and args.season.strip().lower() == "all":
        seasons = [str(season.get("SeasonId")) for season in get_seasons()]
    else:
        seasons = [args.season.strip()] if args.season else [get_current_season_id()]

    events: list[dict] = []
    for season_id in seasons:
        events.extend(get_events(season_id, level_int))

    def date_only(value: str | None) -> str:
        return value.split("T", 1)[0] if isinstance(value, str) else ""

    def date_with_time(value: str | None) -> str:
        if not isinstance(value, str):
            return ""
        # If no time component, just return the date
        if "T" not in value:
            return value.split(" ", 1)[0]
        try:
            # Handle Z suffix for UTC
            iso_str = value.replace("Z", "+00:00")
            dt = datetime.datetime.fromisoformat(iso_str)
            # If no timezone info, assume UTC
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            # Convert to local timezone
            local_dt = dt.astimezone()
            return local_dt.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            # Fallback: just extract date part
            return value.split("T", 1)[0]

    if args.completed and args.upcoming:
        print("error: use only one of --completed or --upcoming", file=sys.stderr)
        return 1

    if args.completed:
        today = datetime.date.today()
        events = [
            ev
            for ev in events
            if (
                (end := parse_date(ev.get("EndDate")))
                or (
                    end := parse_date(
                        ev.get("StartDate") or ev.get("FirstCompetitionDate")
                    )
                )
            )
            and end < today
        ]
    elif args.upcoming:
        today = datetime.date.today()
        events = [
            ev
            for ev in events
            if (
                (end := parse_date(ev.get("EndDate")))
                or (
                    end := parse_date(
                        ev.get("StartDate") or ev.get("FirstCompetitionDate")
                    )
                )
            )
            and end >= today
        ]

    pretty = is_pretty_output(args)

    if args.summary:
        return _handle_events_summary(events, pretty, date_only)

    sort_key = (args.sort or "startdate").lower()

    def event_sorter(event: dict) -> tuple:
        label = (event.get("ShortDescription") or event.get("Organizer") or "").lower()
        country = (
            event.get("Nat")
            or event.get("Nation")
            or event.get("CountryId")
            or event.get("Country")
            or ""
        ).lower()
        start = event.get("StartDate") or event.get("FirstCompetitionDate") or ""
        if sort_key == "event":
            return (label, start, country)
        if sort_key == "country":
            return (country, start, label)
        return (start, label, country)

    events.sort(key=event_sorter)

    if args.search:
        needle = args.search.lower()
        events = [
            evt
            for evt in events
            if needle
            in (evt.get("ShortDescription") or evt.get("Organizer") or "").lower()
            or needle in (evt.get("Description") or "").lower()
        ]

    # Auto-enable --races when -d/--discipline is used
    if args.races or args.discipline:
        return _handle_events_with_races(
            events, args, pretty, date_only, date_with_time
        )

    rows = []
    for event in events:
        event_id = event.get("EventId")
        race_count = len(get_races(event_id)) if event_id else 0
        rows.append(format_event_row(event, race_count))
    headers = [
        "Season",
        "Level",
        "Event",
        "Location",
        "Country",
        "StartDate",
        "EndDate",
        "Races",
        "EventId",
    ]
    row_styles = compute_event_styles(events) if pretty else None
    render_table(headers, rows, pretty=pretty, row_styles=row_styles)
    return 0


def _handle_events_summary(events: list[dict], pretty: bool, date_only) -> int:
    """Handle --summary flag for events command."""
    headers = [
        "Season",
        "Level",
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
    ]
    rows: list[list[str]] = []

    # Filter to Level 1 events only
    level1_events = [e for e in events if (e.get("Level") or 0) == 1]

    for event in level1_events:
        event_id = event.get("EventId")
        race_list = get_races(event_id) if event_id else []
        race_count = len(race_list)
        individual: set[str] = set()
        sprint: set[str] = set()
        pursuit: set[str] = set()
        mass: set[str] = set()
        relay: set[str] = set()
        mixed_relay = False
        single_mixed = False

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

            if disc == "IN":
                individual.add(gender_tag or "W+M")
            elif disc == "SP":
                sprint.add(gender_tag or "W+M")
            elif disc == "PU":
                pursuit.add(gender_tag or "W+M")
            elif disc == "MS":
                mass.add(gender_tag or "W+M")
            elif disc == "RL" or "relay" in name:
                if "single" in name and "mixed" in name:
                    single_mixed = True
                elif "mixed" in name:
                    mixed_relay = True
                else:
                    relay.add(gender_tag or "W+M")

        def mark_set(tags: set[str]) -> str:
            if not tags:
                return ""
            if "W+M" in tags or ("W" in tags and "M" in tags):
                return "W+M"
            return "+".join(sorted(tags))

        rows.append(
            [
                event.get("SeasonId", ""),
                format_level(event.get("Level")),
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
                str(race_count),
                mark_set(individual),
                mark_set(sprint),
                mark_set(pursuit),
                mark_set(mass),
                mark_set(relay),
                "X" if mixed_relay else "",
                "X" if single_mixed else "",
            ]
        )

    row_styles = compute_event_styles(level1_events) if pretty else None
    render_table(headers, rows, pretty=pretty, row_styles=row_styles)
    return 0


def _handle_events_with_races(
    events: list[dict], args, pretty: bool, date_only, date_with_time
) -> int:
    """Handle --races flag for events command."""
    type_filter = None
    if args.discipline:
        type_map = {
            "individual": {"IN"},
            "sprint": {"SP"},
            "pursuit": {"PU"},
            "massstart": {"MS"},
            "mass-start": {"MS"},
            "relay": {"RL"},
        }
        type_filter = type_map.get(args.discipline)

    headers = [
        "Season",
        "Level",
        "Event",
        "Location",
        "Country",
        "EventStart",
        "Races",
        "Race",
        "Date",
        "Discipline",
        "RaceId",
        "EventId",
    ]
    rows = []
    row_styles = []

    def parse_race_datetime(race: dict) -> datetime.datetime | None:
        """Parse race start time as a datetime object."""
        raw = (
            race.get("StartTime")
            or race.get("StartDate")
            or race.get("FirstStart")
            or ""
        )
        if not isinstance(raw, str) or "T" not in raw:
            return None
        try:
            iso_str = raw.replace("Z", "+00:00")
            dt = datetime.datetime.fromisoformat(iso_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            return dt
        except ValueError:
            return None

    now = datetime.datetime.now(datetime.timezone.utc)
    found_next_race = False

    for event in events:
        event_id = event.get("EventId")
        event_label = event.get("Description") or ""
        short_label = event.get("ShortDescription") or event.get("Organizer") or ""
        country = (
            event.get("Nat")
            or event.get("Nation")
            or event.get("CountryId")
            or event.get("Country")
            or ""
        )
        level = format_level(event.get("Level"))
        event_start = date_only(
            event.get("StartDate") or event.get("FirstCompetitionDate") or ""
        )
        race_list = get_races(event_id) if event_id else []
        race_count = len(race_list)

        if not race_list:
            rows.append(
                [
                    event.get("SeasonId", ""),
                    level,
                    event_label,
                    short_label,
                    country,
                    event_start,
                    str(race_count),
                    "",
                    "",
                    "",
                    "",
                    event_id or "",
                ]
            )
            row_styles.append("dim")
            continue

        for race in race_list:
            race_label = get_race_label(race)
            race_start = date_with_time(
                race.get("StartTime")
                or race.get("StartDate")
                or race.get("FirstStart")
                or ""
            )
            disc_id = race.get("DisciplineId") or ""
            if type_filter and str(disc_id).upper() not in type_filter:
                continue

            # Compute race style based on start time
            race_style = ""
            if pretty:
                race_dt = parse_race_datetime(race)
                if race_dt is None:
                    race_style = "dim"
                elif race_dt < now:
                    race_style = "dim"
                elif not found_next_race:
                    race_style = "highlight"
                    found_next_race = True

            rows.append(
                [
                    event.get("SeasonId", ""),
                    level,
                    event_label,
                    short_label,
                    country,
                    event_start,
                    str(race_count),
                    race_label,
                    race_start,
                    disc_id,
                    race.get("RaceId") or race.get("Id") or "",
                    event_id or "",
                ]
            )
            row_styles.append(race_style)

    render_table(
        headers, rows, pretty=pretty, row_styles=row_styles if pretty else None
    )
    return 0
