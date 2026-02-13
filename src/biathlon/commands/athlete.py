"""Athlete command handlers."""

from __future__ import annotations

import argparse
import sys

from ..api import (
    BiathlonError,
    get_athlete_bio,
    get_athletes,
    get_all_results,
    get_current_season_id,
    get_events,
    get_race_results,
    get_races,
    get_analytic_results,
)
from ..constants import RELAY_DISCIPLINE
from ..formatting import is_pretty_output, render_table
from ..utils import (
    build_analytic_times,
    extract_results,
    get_first_time,
    get_race_label,
    get_race_start_key,
    parse_time_seconds,
)

def handle_athlete_results_scan(args: argparse.Namespace) -> int:
    """Show season race ranks for an athlete (scan Results endpoints)."""
    if not args.id and not args.search:
        print("error: provide --id or --search", file=sys.stderr)
        return 1

    season_id = args.season or get_current_season_id()
    cat_filter: str | None = None
    if args.id and not args.search:
        try:
            bio = get_athlete_bio(args.id)
        except BiathlonError:
            bio = {}
        gender_id = str(bio.get("GenderId") or "").upper()
        if gender_id == "W":
            cat_filter = "SW"
        elif gender_id == "M":
            cat_filter = "SM"

    event_map: dict[str, dict] = {}
    level_arg = getattr(args, "level", 0)
    levels = [level_arg] if level_arg in {1, 2, 3, 4, 5} else [1, 2, 3, 4, 5]
    for lvl in levels:
        for ev in get_events(season_id, level=lvl):
            key = ev.get("EventId") or f"{lvl}-{ev.get('Description','')}"
            event_map.setdefault(key, ev)
    events = list(event_map.values())

    race_entries: list[dict] = []
    athlete_map: dict[str, dict] = {}

    def has_analytics(race_id: str) -> bool:
        for type_id in ("CRST", "RNGT", "SHTT"):
            try:
                analytic = get_analytic_results(race_id, type_id)
            except BiathlonError:
                return True
            if analytic.get("Results"):
                return True
        return False

    for event in events:
        event_id = event.get("EventId")
        if not event_id:
            continue
        event_label = event.get("Description") or event.get("ShortDescription") or event.get("Organizer") or ""
        location_label = event.get("ShortDescription") or event.get("Organizer") or ""
        for race in sorted(get_races(event_id), key=get_race_start_key):
            race_id = race.get("RaceId") or race.get("Id")
            if not race_id:
                continue
            race_cat = str(race.get("catId") or race.get("CatId") or "").upper()
            if cat_filter and race_cat and race_cat not in {cat_filter, "MX"}:
                continue
            try:
                payload = get_race_results(race_id)
            except BiathlonError:
                continue
            is_result = payload.get("IsResult")
            if isinstance(is_result, str):
                is_result = is_result.strip().lower() != "false"
            if is_result is False:
                continue
            results = extract_results(payload)
            if args.ski and not has_analytics(race_id):
                continue
            comp = payload.get("Competition") or {}
            sport_evt = payload.get("SportEvt") or {}
            race_label = comp.get("ShortDescription") or comp.get("Description") or get_race_label(race)
            start_raw = comp.get("StartTime") or comp.get("StartDate") or race.get("StartTime") or race.get("StartDate")
            race_date = start_raw.split("T", 1)[0] if isinstance(start_raw, str) else ""
            discipline = comp.get("DisciplineId") or race.get("DisciplineId") or ""

            ski_ranks: dict[str, int] = {}
            if args.ski:
                analytics = build_analytic_times(race_id, "CRST", "CRS", "", 0)
                time_map: dict[str, float] = {}
                for ident, vals in analytics.items():
                    total = vals.get("total")
                    secs = parse_time_seconds(total) if total else None
                    if secs is not None:
                        time_map[ident] = secs
                if not time_map and str(discipline).upper() != RELAY_DISCIPLINE:
                    for res in results:
                        ident = res.get("IBUId") or res.get("Bib") or res.get("Name")
                        if not ident:
                            continue
                        course_val = get_first_time(res, ["TotalCourseTime", "CourseTime", "RunTime"])
                        secs = parse_time_seconds(course_val) if course_val else None
                        if secs is not None:
                            time_map[ident] = secs
                if time_map:
                    sorted_times = sorted(time_map.items(), key=lambda kv: kv[1])
                    for idx, (ident, _) in enumerate(sorted_times, start=1):
                        ski_ranks[ident] = idx

            matches: dict[str, str] = {}
            for res in results:
                ibuid = (res.get("IBUId") or "").lower()
                name = res.get("Name") or res.get("ShortName") or ""
                nat = res.get("Nat") or ""
                key = ibuid or name.lower()
                if args.ski:
                    rank_val = ski_ranks.get(res.get("IBUId") or res.get("Bib") or res.get("Name") or "", "")
                else:
                    rank_val = res.get("Rank") or res.get("ResultOrder") or res.get("Result") or ""
                include = False
                if args.id and ibuid and ibuid == args.id.lower():
                    include = True
                if args.search and args.search.lower() in name.lower():
                    include = True
                if include:
                    matches[key] = rank_val
                    if key not in athlete_map:
                        label = f"{name} ({nat})" if nat else name
                        athlete_map[key] = {"label": label, "nat": nat, "ibuid": ibuid or ""}
            if matches:
                race_entries.append({
                    "date": race_date,
                    "event": event_label or sport_evt.get("ShortDescription") or sport_evt.get("Organizer") or "",
                    "location": location_label or sport_evt.get("Organizer") or "",
                    "race": race_label,
                    "disc": discipline,
                    "race_id": race_id,
                    "matches": matches,
                })

    if args.id and args.id.lower() not in athlete_map:
        print(f"no results found for athlete id {args.id}", file=sys.stderr)
        return 1
    if args.search and not athlete_map:
        print(f"no athletes matched search '{args.search}'", file=sys.stderr)
        return 1

    athlete_keys = sorted(athlete_map.keys(), key=lambda k: athlete_map[k]["label"])
    headers = ["Date", "Event", "Location", "Race", "Discipline", "RaceId"] + [athlete_map[k]["label"] for k in athlete_keys]
    rows = []
    race_entries.sort(key=lambda r: r.get("date", ""), reverse=True)
    for entry in race_entries:
        row = [entry["date"], entry["event"], entry["location"], entry["race"], entry["disc"], entry["race_id"]]
        for key in athlete_keys:
            row.append(entry["matches"].get(key, ""))
        rows.append(row)

    print()
    print(f"# Athlete results — season {season_id}")
    render_table(headers, rows, pretty=is_pretty_output(args))
    print()
    return 0


def _normalize_season_filter(raw: str) -> str:
    text = raw.strip()
    if text.lower() == "all":
        return ""
    if text.isdigit() and len(text) == 4:
        return f"{text[:2]}/{text[2:]}"
    return text


def _season_matches(result: dict, season_filter: str) -> bool:
    if not season_filter:
        return True
    season = str(result.get("Season") or "").strip()
    season_id = str(result.get("SeasonId") or "").strip()
    return season_filter == season or season_filter == season_id


def _normalize_level_filter(raw: str) -> str:
    text = raw.strip()
    if text.lower() == "all":
        return ""
    return text.upper()


def _level_matches(result: dict, level_filter: str) -> bool:
    if not level_filter:
        return True
    level = str(result.get("Level") or "").strip().upper()
    allowed = {lvl.strip() for lvl in level_filter.upper().split(",")}
    return level in allowed


def handle_athlete_results(args: argparse.Namespace) -> int:
    """Show season race ranks for an athlete via AllResults endpoint."""
    if not args.id:
        print("error: provide --id", file=sys.stderr)
        return 1

    try:
        payload = get_all_results(args.id)
    except BiathlonError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    results = list(payload.get("Results") or [])
    season_filter = _normalize_season_filter(args.season or get_current_season_id())
    level_filter = _normalize_level_filter(getattr(args, "level", "WC"))
    if season_filter:
        results = [res for res in results if _season_matches(res, season_filter)]
    if level_filter:
        results = [res for res in results if _level_matches(res, level_filter)]

    if not results:
        print(f"no results found for athlete id {args.id}", file=sys.stderr)
        return 1

    athlete_label = args.id
    try:
        bio = get_athlete_bio(args.id)
    except BiathlonError:
        bio = {}
    name = bio.get("FullName") or ""
    nat = bio.get("NAT") or ""
    if name:
        athlete_label = f"{name} ({nat})" if nat else name

    headers = ["Date", "Event", "Place", "Race", "Discipline", "RaceId", athlete_label]
    rows = []
    for res in results:
        race_id = res.get("RaceId") or ""
        race_date = ""
        event_desc = ""
        race_desc = ""
        rank = res.get("Rank") or res.get("SO") or ""
        if isinstance(rank, str) and rank.endswith("."):
            rank = rank[:-1]
        if race_id:
            try:
                race_payload = get_race_results(race_id)
            except BiathlonError:
                race_payload = {}
            comp = race_payload.get("Competition") or {}
            start_raw = comp.get("StartTime") or comp.get("StartDate")
            if isinstance(start_raw, str):
                race_date = start_raw.split("T", 1)[0]
            race_desc = comp.get("ShortDescription") or ""
            sport_evt = race_payload.get("SportEvt") or {}
            event_desc = sport_evt.get("Description") or ""
            if args.course:
                time_map: dict[str, float] = {}
                try:
                    analytic = get_analytic_results(race_id, "CRST")
                except BiathlonError:
                    analytic = {}
                for entry in analytic.get("Results", []) or []:
                    if entry.get("IsTeam"):
                        continue
                    ident = entry.get("IBUId") or entry.get("Bib") or entry.get("Name")
                    if not ident:
                        continue
                    total = entry.get("TotalTime") or entry.get("Result")
                    secs = parse_time_seconds(str(total)) if total else None
                    if secs is not None:
                        time_map[str(ident)] = secs
                athlete_time = time_map.get(args.id) if args.id else None
                if athlete_time is not None:
                    sorted_times = sorted(time_map.items(), key=lambda kv: kv[1])
                    for idx, (ident, _) in enumerate(sorted_times, start=1):
                        if ident == args.id:
                            rank = idx
                            break
        place = res.get("Place") or ""
        comp = res.get("Comp") or ""
        rows.append([race_date, event_desc, place, race_desc, comp, race_id, rank])

    print()
    if args.season and args.season.strip().lower() == "all":
        label = "all"
    else:
        season_label = season_filter or (results[0].get("Season") or results[0].get("SeasonId") or "")
        label = season_label or "all"
    print(f"# Athlete results — season {label}")
    render_table(headers, rows, pretty=is_pretty_output(args))
    print()
    return 0


def _find_athletes_by_search(term: str) -> dict[str, dict]:
    matches: dict[str, dict] = {}
    search_term = term.strip()
    tokens = [tok for tok in search_term.split() if tok]
    family_name = tokens[-1] if tokens else search_term
    given_name = " ".join(tokens[:-1]) if len(tokens) > 1 else ""

    def add_matches(athletes: list[dict]) -> None:
        for athlete in athletes:
            if not isinstance(athlete, dict):
                continue
            ibu_id = athlete.get("IBUId") or athlete.get("IbuId") or ""
            if not ibu_id:
                continue
            given = athlete.get("GivenName") or ""
            family = athlete.get("FamilyName") or ""
            name = athlete.get("Name") or " ".join(part for part in [given, family] if part)
            nat = athlete.get("Nat") or athlete.get("Nation") or athlete.get("Country") or ""
            if not nat and isinstance(athlete.get("NF"), dict):
                nat = athlete["NF"].get("Nat") or athlete["NF"].get("Country") or ""
            matches.setdefault(ibu_id, {"name": name or f"IBU {ibu_id}", "nat": nat})

    try:
        add_matches(get_athletes(family_name, given_name))
        if search_term and not given_name:
            add_matches(get_athletes(search_term, ""))
            add_matches(get_athletes("", search_term))
    except BiathlonError:
        pass

    return matches


def handle_athlete_id(args: argparse.Namespace) -> int:
    """Show IBU ids for athletes matching a search term."""
    if not args.search:
        print("error: provide --search", file=sys.stderr)
        return 1

    matches: dict[str, dict] = {}
    search_term = args.search.strip()
    tokens = [tok for tok in search_term.split() if tok]
    family_name = tokens[-1] if tokens else search_term
    given_name = " ".join(tokens[:-1]) if len(tokens) > 1 else ""

    def add_matches(athletes: list[dict]) -> None:
        for athlete in athletes:
            if not isinstance(athlete, dict):
                continue
            ibu_id = athlete.get("IBUId") or athlete.get("IbuId") or ""
            if not ibu_id:
                continue
            given = athlete.get("GivenName") or ""
            family = athlete.get("FamilyName") or ""
            name = athlete.get("Name") or " ".join(part for part in [given, family] if part)
            nat = athlete.get("Nat") or athlete.get("Nation") or athlete.get("Country") or ""
            if not nat and isinstance(athlete.get("NF"), dict):
                nat = athlete["NF"].get("Nat") or athlete["NF"].get("Country") or ""
            matches.setdefault(ibu_id, {"name": name or f"IBU {ibu_id}", "nat": nat})

    try:
        add_matches(get_athletes(family_name, given_name))
        if search_term and not given_name:
            add_matches(get_athletes(search_term, ""))
            add_matches(get_athletes("", search_term))
    except BiathlonError:
        pass

    if not matches:
        print(f"no athletes matched search '{args.search}'", file=sys.stderr)
        return 1

    for ibu_id, meta in matches.items():
        if meta.get("nat"):
            continue
        try:
            bio = get_athlete_bio(ibu_id)
        except BiathlonError:
            continue
        nat = bio.get("NAT") or ""
        if nat:
            meta["nat"] = nat

    rows = []
    for ibu_id, meta in sorted(matches.items(), key=lambda item: item[1].get("name", "")):
        name = meta.get("name") or f"IBU {ibu_id}"
        rows.append([name, meta.get("nat", ""), ibu_id])

    print()
    print("# Athlete IBU ids")
    render_table(["Name", "Country", "IBUId"], rows, pretty=is_pretty_output(args))
    print()
    return 0


def handle_athlete_info(args: argparse.Namespace) -> int:
    """Show basic bio info for athletes."""
    if not args.id and not args.search:
        print("error: provide --id or --search", file=sys.stderr)
        return 1

    requested: dict[str, dict] = {}
    if args.id:
        for part in args.id.split(","):
            ibu = part.strip()
            if ibu:
                requested[ibu] = {}
    if args.search:
        requested.update(_find_athletes_by_search(args.search))

    if not requested:
        print("no athletes matched the provided criteria", file=sys.stderr)
        return 1

    rows = []
    for ibu_id, meta in requested.items():
        try:
            bio = get_athlete_bio(ibu_id)
        except BiathlonError:
            continue
        personal = {p.get("Description", "").lower(): p.get("Value") for p in bio.get("Personal", []) if p.get("Description")}
        age_val = bio.get("Age") or personal.get("age") or "-"
        if isinstance(age_val, str) and "," in age_val:
            age_val = age_val.split(",", 1)[0].strip()
        born_in = personal.get("born in", "-")
        residence = personal.get("residence", "-")
        profession = personal.get("profession", "-")
        gender = bio.get("GenderId") or "-"
        functions = bio.get("Functions") or "-"
        name = bio.get("FullName") or meta.get("name") or f"IBU {ibu_id}"
        nat = bio.get("NAT") or meta.get("nat") or ""
        photo = bio.get("PhotoURI") or f"https://ibu.blob.core.windows.net/docs/athletes/{ibu_id}.png"
        rows.append([name, nat, gender, age_val, born_in, residence, profession, functions, photo, ibu_id])

    if not rows:
        print("no bios found", file=sys.stderr)
        return 1

    headers = ["Name", "Country", "Gender", "Age", "BornIn", "Residence", "Profession", "Functions", "Photo", "IBUId"]
    print()
    print("# Athlete info")
    render_table(headers, rows, pretty=is_pretty_output(args))
    print()
    return 0
