"""Ceremony (medal ranking) command handler."""

from __future__ import annotations

import argparse
import re
import sys

from ..api import (
    BiathlonError,
    get_current_season_id,
    get_events,
    get_race_results,
    get_races,
    get_seasons,
)
from ..constants import RELAY_DISCIPLINE
from ..formatting import is_pretty_output, get_output_format, rank_style, render_table
from ..utils import extract_results


def accumulate_medal_counts(
    race_ids: list[str], by_athlete: bool, gender_filter: str = ""
) -> tuple[list[dict], int]:
    """Aggregate podium counts across races."""
    counts: dict[str, dict] = {}
    total_used = 0

    for race_id in race_ids:
        try:
            payload = get_race_results(race_id)
        except BiathlonError:
            continue
        comp = payload.get("Competition") or {}
        discipline = str(comp.get("DisciplineId") or "").upper()
        if discipline == RELAY_DISCIPLINE:
            continue
        results = extract_results(payload)
        if not results:
            continue
        if gender_filter:
            cat = str(comp.get("catId") or comp.get("CatId") or "").upper()
            if gender_filter == "women" and cat not in {"SW", ""}:
                continue
            if gender_filter == "men" and cat not in {"SM", ""}:
                continue
        total_used += 1
        top = results[:6]
        for idx, res in enumerate(top):
            nat = res.get("Nat") or ""
            if by_athlete:
                key = res.get("Name") or res.get("ShortName") or ""
                label = key
                counts.setdefault(
                    key,
                    {
                        "label": label,
                        "country": nat,
                        "first": 0,
                        "second": 0,
                        "third": 0,
                        "fourth": 0,
                        "fifth": 0,
                        "sixth": 0,
                    },
                )
            else:
                key = nat
                label = nat
                counts.setdefault(
                    key,
                    {
                        "label": label,
                        "country": "",
                        "first": 0,
                        "second": 0,
                        "third": 0,
                        "fourth": 0,
                        "fifth": 0,
                        "sixth": 0,
                    },
                )
            slot = ["first", "second", "third", "fourth", "fifth", "sixth"][idx]
            counts[key][slot] += 1

    rows = list(counts.values())
    rows.sort(
        key=lambda row: (
            -row["first"],
            -row["second"],
            -row["third"],
            -row["fourth"],
            -row["fifth"],
            -row["sixth"],
            row["label"],
        )
    )
    return rows, total_used


def handle_ceremony(args: argparse.Namespace) -> int:
    """Show medal/placing ranking by country or athlete."""
    scope_count = sum(1 for v in [args.race, args.event] if v)
    if scope_count > 1:
        print("error: use only one of --race or --event", file=sys.stderr)
        return 1

    by_athlete = args.athlete
    gender_filter = (
        "men" if args.men else "women" if getattr(args, "women", False) else ""
    )
    season_id = args.season or get_current_season_id()

    def normalize_country(value: str) -> str:
        v = (value or "").strip()
        if not v:
            return ""
        return v[:3].upper() if len(v) > 3 else v.upper()

    country_filter = normalize_country(args.country or "")
    race_ids: list[str] = []
    scope_label = ""

    if args.race:
        include_race = True
        if country_filter:
            try:
                payload = get_race_results(args.race)
            except BiathlonError:
                payload = {}
            comp = payload.get("Competition") or {}
            sport_evt = payload.get("SportEvt") or {}
            race_nat = normalize_country(
                comp.get("Nat")
                or comp.get("Nation")
                or sport_evt.get("Nat")
                or sport_evt.get("Nation")
                or ""
            )
            include_race = bool(race_nat) and race_nat == country_filter
        if include_race:
            race_ids = [args.race]
        scope_label = args.race
    elif args.event:
        event_ok = True
        if country_filter:
            inferred_season = season_id
            if not args.season:
                match = re.search(r"BT(\d{4})", args.event)
                if match:
                    inferred_season = match.group(1)
            try:
                ev_list = get_events(inferred_season, level=1)
            except BiathlonError:
                ev_list = []
            ev_match = next(
                (ev for ev in ev_list if ev.get("EventId") == args.event), None
            )
            ev_nat = (
                normalize_country(
                    ev_match.get("Nat")
                    or ev_match.get("Nation")
                    or ev_match.get("CountryId")
                    or ev_match.get("Country")
                    or ""
                )
                if ev_match
                else ""
            )
            if ev_nat and ev_nat != country_filter:
                event_ok = False
        if event_ok:
            for race in get_races(args.event):
                race_id = race.get("RaceId") or race.get("Id")
                if race_id:
                    race_ids.append(race_id)
        scope_label = args.event
    else:
        season_ids: list[str]
        search_filter = (args.search or "").strip().lower()
        # Search across all seasons when filtering by country or search term
        if country_filter or search_filter:
            season_ids = [str(season.get("SeasonId")) for season in get_seasons()]
            if search_filter and country_filter:
                scope_label = f"World Cup '{args.search}' in {args.country}"
            elif search_filter:
                scope_label = f"World Cup '{args.search}'"
            else:
                scope_label = f"World Cup in {args.country}"
        else:
            season_ids = [season_id]
            scope_label = f"World Cup season {season_id}"
        events: list[dict] = []
        for sid in season_ids:
            events.extend(get_events(sid, level=1))
        for event in events:
            ev_nat = normalize_country(
                event.get("Nat")
                or event.get("Nation")
                or event.get("CountryId")
                or event.get("Country")
                or ""
            )
            if country_filter and ev_nat != country_filter:
                continue
            # Filter by search term (event name)
            if search_filter:
                ev_name = (
                    (event.get("ShortDescription") or "")
                    + " "
                    + (event.get("Organizer") or "")
                    + " "
                    + (event.get("Description") or "")
                ).lower()
                if search_filter not in ev_name:
                    continue
            event_id = event.get("EventId")
            if not event_id:
                continue
            for race in get_races(event_id):
                race_id = race.get("RaceId") or race.get("Id")
                if race_id:
                    race_ids.append(race_id)

    rows, used_races = accumulate_medal_counts(race_ids, by_athlete, gender_filter)
    if used_races == 0:
        print("no completed races found for ceremony ranking", file=sys.stderr)
        return 1
    if not rows:
        print("no medal data found for the requested scope", file=sys.stderr)
        return 1

    headers = ["Rank", "Country" if not by_athlete else "Name"]
    if by_athlete:
        headers.append("Nat")
    headers += [
        "Gold",
        "Silver",
        "Bronze",
        "Podium",
        "Fourth",
        "Fifth",
        "Sixth",
        "Flowers",
        "Total",
    ]

    render_rows = []
    for idx, row in enumerate(rows, start=1):
        base = [idx, row["label"]]
        if by_athlete:
            base.append(row["country"])
        podium = row["first"] + row["second"] + row["third"]
        flowers = row["fourth"] + row["fifth"] + row["sixth"]
        base.extend(
            [
                row["first"],
                row["second"],
                row["third"],
                podium,
                row["fourth"],
                row["fifth"],
                row["sixth"],
                flowers,
                podium + flowers,
            ]
        )
        render_rows.append(base)

    pretty = is_pretty_output(args)
    output_format = get_output_format(args)
    row_styles = (
        [rank_style(idx + 1) for idx in range(len(render_rows))] if pretty else None
    )

    gender_label = (
        "men"
        if gender_filter == "men"
        else "women"
        if gender_filter == "women"
        else "men+women"
    )
    group_label = "by athlete" if by_athlete else "by country"

    print()
    print(
        f"# Medal ranking — {scope_label} — {gender_label}, {group_label} ({used_races} races)"
    )
    render_table(
        headers, render_rows, output_format=output_format, row_styles=row_styles
    )
    print()
    return 0
