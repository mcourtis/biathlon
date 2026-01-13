"""Cumulate-new command handlers for aggregated statistics."""

from __future__ import annotations

import argparse
import sys

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
from ..formatting import format_pct, format_seconds, is_pretty_output, rank_style, render_table
from ..utils import (
    base_time_seconds,
    extract_results,
    get_first_time,
    get_race_start_key,
    is_dns,
    parse_relay_shooting,
    parse_time_seconds,
    result_seconds,
)
from .results import _get_top_n_ibu_ids


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
    for key in (res.get("IBUId"), res.get("Bib"), res.get("Name"), res.get("ShortName")):
        if key and str(key) in times:
            return times[str(key)]
    return ""


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
    for event in events:
        ev_id = event.get("EventId")
        if not ev_id:
            continue
        races.extend(get_races(ev_id))
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
    prone_pct = format_pct(entry["shot_prone"] - entry["miss_prone"], entry["shot_prone"]) if entry["shot_prone"] else "-"
    standing_pct = format_pct(
        entry["shot_standing"] - entry["miss_standing"], entry["shot_standing"]
    ) if entry["shot_standing"] else "-"
    return acc, prone_pct, standing_pct


def _apply_top_filter(
    results: list[dict], top_n: int, cat_id: str, season_id: str
) -> list[dict]:
    """Filter results to top N WC athletes."""
    if top_n <= 0:
        return results
    top_ibu_ids = _get_top_n_ibu_ids(cat_id, top_n, season_id)
    if not top_ibu_ids:
        return results
    return [r for r in results if r.get("IBUId") in top_ibu_ids]


def _apply_limit(rows: list[dict], limit: int) -> list[dict]:
    """Apply output limit if configured."""
    if limit and limit > 0:
        return rows[:limit]
    return rows


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
    if not allow_event and getattr(args, "event", ""):
        raise BiathlonError("--event is not supported for this subcommand")
    discipline_value = discipline_override or getattr(args, "discipline", "all") or "all"
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

    payloads: list[tuple[str, dict]] = []
    for race in sorted(races, key=get_race_start_key):
        race_id = race.get("RaceId") or race.get("Id")
        if not race_id:
            continue
        race_disc = str(race.get("DisciplineId") or "").upper()
        if race_disc not in disc_set:
            continue
        try:
            payload = get_race_results(race_id)
        except BiathlonError:
            continue
        comp = payload.get("Competition") or {}
        comp_cat = str(comp.get("catId") or comp.get("CatId") or "").upper()
        gender_cat = GENDER_TO_CAT["men"] if getattr(args, "men", False) else GENDER_TO_CAT["women"]
        if race_disc in {RELAY_DISCIPLINE, SINGLE_MIXED_RELAY_DISCIPLINE}:
            if not allow_relay:
                continue
            if cat_filter and comp_cat and comp_cat != cat_filter:
                continue
            if not cat_filter and comp_cat == RELAY_MIXED_CAT:
                continue
            if not cat_filter and comp_cat and comp_cat not in {RELAY_MEN_CAT, RELAY_WOMEN_CAT}:
                continue
            if not cat_filter and comp_cat and comp_cat != gender_cat:
                continue
            if comp_cat and comp_cat not in {RELAY_MEN_CAT, RELAY_WOMEN_CAT, RELAY_MIXED_CAT}:
                continue
        else:
            if comp_cat and comp_cat != gender_cat:
                continue
        payloads.append((race_id, payload))
    return (payloads, season_id)


def _is_relay(payload: dict) -> bool:
    """Return True if payload is a relay discipline."""
    discipline = str((payload.get("Competition") or {}).get("DisciplineId") or "").upper()
    return discipline in {RELAY_DISCIPLINE, SINGLE_MIXED_RELAY_DISCIPLINE}


def _race_results(payload: dict) -> list[dict]:
    """Return appropriate results list for a payload."""
    if _is_relay(payload):
        return [r for r in (payload.get("Results") or []) if r.get("IsTeam")]
    return extract_results(payload)


def _relay_leg_results(payload: dict) -> list[dict]:
    """Return non-team relay leg results from a payload."""
    return [r for r in (payload.get("Results") or []) if not r.get("IsTeam")]


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
                    times.setdefault((ibu_id, leg_idx), {})[f"lap{local_idx}"] = time_str
                if name:
                    times.setdefault((name, leg_idx), {})[f"lap{local_idx}"] = time_str
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

    entries: dict[str, dict] = {}
    total_races = 0
    for race_id, payload in payloads:
        include_relay = bool(getattr(args, "include_relay", False))
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
            results = _apply_top_filter(results, args.top, cat_id, season_id)
        if not results:
            continue
        leg_cumulative: dict[tuple[str, int], float] = {}
        if is_relay and include_relay:
            for res in results:
                bib = str(res.get("Bib") or "")
                leg = res.get("Leg")
                if not bib or not isinstance(leg, int):
                    continue
                cum_val = get_first_time(res, ["LegResult", "LegTime", "LegTimeTotal", "TotalTime", "Result"])
                cum_secs = parse_time_seconds(cum_val) if cum_val else None
                if cum_secs is None:
                    continue
                leg_cumulative[(bib, leg)] = cum_secs
        race_has_data = False
        for res in results:
            ident = res.get("IBUId") or res.get("Name") or res.get("ShortName") or ""
            if not ident:
                continue
            name = res.get("Name") or res.get("ShortName") or ""
            nat = res.get("Nat") or ""
            if is_relay:
                cum_val = get_first_time(res, ["LegResult", "LegTime", "LegTimeTotal", "TotalTime", "Result"])
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
                    secs = parse_time_seconds(get_first_time(res, ["TotalTime", "Result"]))
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
        rows.append({
            "rank_val": entry["total_secs"],
            "row": [
                0,
                entry["name"],
                entry["nat"],
                entry["races"],
                format_seconds(entry["total_secs"]),
            ],
        })
    rows.sort(key=lambda r: (r["rank_val"], r["row"][1]))
    for idx, row in enumerate(rows, start=1):
        row["row"][0] = idx
    rows = _apply_limit(rows, args.limit)
    headers = ["Rank", "Biathlete", "Country", "Races", "Total Results"]
    pretty = is_pretty_output(args)
    row_styles = [rank_style(r["row"][0]) for r in rows] if pretty else None
    render_table(headers, [r["row"] for r in rows], pretty=pretty, row_styles=row_styles)
    return 0


def handle_cumulate_ski(args: argparse.Namespace) -> int:
    """Cumulate ski times from individual races."""
    if args.discipline != "all":
        print("error: --discipline is not supported for ski", file=sys.stderr)
        return 1
    try:
        payloads, season_id = _collect_races(args, allow_discipline=False, allow_event=False)
    except BiathlonError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not payloads:
        print("no races found for the requested scope", file=sys.stderr)
        return 1
    entries: dict[str, dict] = {}
    total_races = 0
    for race_id, payload in payloads:
        results = _race_results(payload)
        if not results:
            continue
        cat_id = (payload.get("Competition") or {}).get("catId", "").upper()
        if _is_relay(payload):
            continue
        results = _apply_top_filter(results, args.top, cat_id, season_id)
        if not results:
            continue
        ski_times = _fetch_analytic_map(race_id, "SKIT")
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
        rows.append({
            "rank_val": entry["total_secs"],
            "row": [
                0,
                entry["name"],
                entry["nat"],
                entry["races"],
                format_seconds(entry["total_secs"]),
            ],
        })
    rows.sort(key=lambda r: (r["rank_val"], r["row"][1]))
    for idx, row in enumerate(rows, start=1):
        row["row"][0] = idx
    rows = _apply_limit(rows, args.limit)
    headers = ["Rank", "Biathlete", "Country", "Races", "Total Ski"]
    pretty = is_pretty_output(args)
    row_styles = [rank_style(r["row"][0]) for r in rows] if pretty else None
    render_table(headers, [r["row"] for r in rows], pretty=pretty, row_styles=row_styles)
    return 0


def handle_cumulate_pursuit(args: argparse.Namespace) -> int:
    """Cumulate pursuit times from pursuit races only."""
    if args.discipline != "all":
        print("error: --discipline is not supported for pursuit", file=sys.stderr)
        return 1
    try:
        payloads, season_id = _collect_races(
            args, allow_discipline=True, discipline_override="pursuit", allow_event=False
        )
    except BiathlonError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not payloads:
        print("no pursuit races found", file=sys.stderr)
        return 1
    entries: dict[str, dict] = {}
    total_races = 0
    for _, payload in payloads:
        results = extract_results(payload)
        if not results:
            continue
        base_secs = base_time_seconds(results)
        cat_id = (payload.get("Competition") or {}).get("catId", "").upper()
        results = _apply_top_filter(results, args.top, cat_id, season_id)
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
            delay = parse_time_seconds(res.get("StartInfo")) if res.get("StartInfo") else None
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
        rows.append({
            "rank_val": entry["total_secs"],
            "row": [
                0,
                entry["name"],
                entry["nat"],
                entry["races"],
                format_seconds(entry["total_secs"]),
            ],
        })
    rows.sort(key=lambda r: (r["rank_val"], r["row"][1]))
    for idx, row in enumerate(rows, start=1):
        row["row"][0] = idx
    rows = _apply_limit(rows, args.limit)
    headers = ["Rank", "Biathlete", "Country", "Races", "Total Pursuit"]
    row_styles = [rank_style(r["row"][0]) for r in rows] if is_pretty_output(args) else None
    render_table(headers, [r["row"] for r in rows], pretty=is_pretty_output(args), row_styles=row_styles)
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
    entries: dict[str, dict] = {}
    total_races = 0
    for race_id, payload in payloads:
        include_relay = bool(getattr(args, "include_relay", False))
        is_relay = _is_relay(payload)
        if is_relay and include_relay:
            results = _relay_leg_results(payload)
        else:
            results = _race_results(payload)
        if not results:
            continue
        cat_id = (payload.get("Competition") or {}).get("catId", "").upper()
        if not is_relay:
            results = _apply_top_filter(results, args.top, cat_id, season_id)
        if not results:
            continue
        race_has_data = False
        course_times = _fetch_analytic_map(race_id, "CRST") if not is_relay else {}
        for res in results:
            ident = res.get("IBUId") or res.get("Name") or res.get("ShortName") or ""
            if not ident:
                continue
            name = res.get("Name") or res.get("ShortName") or ""
            nat = res.get("Nat") or ""
            course_val = _lookup_analytic_time(course_times, res) or get_first_time(
                res,
                [
                    "LegCourse",
                    "LegRunTime",
                    "LegSkiTime",
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
        rows.append({
            "rank_val": entry["total_secs"],
            "row": [
                0,
                entry["name"],
                entry["nat"],
                entry["races"],
                format_seconds(entry["total_secs"]),
            ],
        })
    rows.sort(key=lambda r: (r["rank_val"], r["row"][1]))
    for idx, row in enumerate(rows, start=1):
        row["row"][0] = idx
    rows = _apply_limit(rows, args.limit)
    headers = ["Rank", "Biathlete", "Country", "Races", "Total Course Time"]
    row_styles = [rank_style(r["row"][0]) for r in rows] if is_pretty_output(args) else None
    render_table(headers, [r["row"] for r in rows], pretty=is_pretty_output(args), row_styles=row_styles)
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
    entries: dict[str, dict] = {}
    total_races = 0
    type_id = "RNGT" if kind == "range" else "STTM"
    time_label = "Range" if kind == "range" else "Shooting"
    relay_stage_keys = ("R1", "R2") if kind == "range" else ("S1", "S2")
    for race_id, payload in payloads:
        include_relay = bool(getattr(args, "include_relay", False))
        is_relay = _is_relay(payload)
        relay_laps = {}
        if is_relay and include_relay:
            if kind == "range":
                relay_laps = _fetch_leg_lap_times(race_id, "RNG", "", 8, 2)
            else:
                relay_laps = _fetch_leg_lap_times(race_id, "S", "TM", 8, 2)
        if is_relay and include_relay:
            results = _relay_leg_results(payload)
        else:
            results = _race_results(payload)
        if not results:
            continue
        cat_id = (payload.get("Competition") or {}).get("catId", "").upper()
        if not is_relay:
            results = _apply_top_filter(results, args.top, cat_id, season_id)
        if not results:
            continue
        race_has_data = False
        times = _fetch_analytic_map(race_id, type_id) if not is_relay else {}
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
                    for key in (res.get("Bib"), res.get("IBUId"), res.get("Name"), res.get("ShortName")):
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
                miss_prone, miss_stand, shot_prone, shot_stand, shots_total = _stage_counts(
                    res.get("Shootings") or res.get("ShootingTotal")
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
        rows.append({
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
        })
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
    row_styles = [rank_style(r["row"][0]) for r in rows] if is_pretty_output(args) else None
    render_table(headers, [r["row"] for r in rows], pretty=is_pretty_output(args), row_styles=row_styles)
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
    entries: dict[str, dict] = {}
    total_races = 0
    for _, payload in payloads:
        results = _race_results(payload)
        if not results:
            continue
        cat_id = (payload.get("Competition") or {}).get("catId", "").upper()
        if not _is_relay(payload):
            results = _apply_top_filter(results, args.top, cat_id, season_id)
        if not results:
            continue
        total_races += 1
        for res in results:
            ident = res.get("IBUId") or res.get("Name") or res.get("ShortName") or ""
            if not ident:
                continue
            name = res.get("Name") or res.get("ShortName") or ""
            nat = res.get("Nat") or ""
            entry = _aggregate_entries(entries, str(ident), name, nat)
            if _is_relay(payload):
                shooting = parse_relay_shooting(res.get("ShootingTotal"))
                if not shooting:
                    continue
                entry["races"] += 1
                entry["relay_pen"] += shooting[0]
                entry["relay_spare"] += shooting[1]
            else:
                miss_prone, miss_stand, shot_prone, shot_stand, shots_total = _stage_counts(
                    res.get("Shootings") or res.get("ShootingTotal")
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

    rows = []
    for entry in entries.values():
        acc, prone_pct, standing_pct = _calc_accuracy(entry)
        if entry["races"] == 0:
            continue
        if entry["races"] != total_races:
            continue
        if entry["shots"] == 0 and (entry["relay_pen"] or entry["relay_spare"]):
            total_miss = f"{entry['relay_pen']}+{entry['relay_spare']}"
            row = [
                0,
                entry["name"],
                entry["nat"],
                entry["races"],
                total_miss,
                "-",
                "-",
                "-",
                "-",
                "-",
            ]
            rank_val = entry["relay_pen"] * 1000 + entry["relay_spare"]
        else:
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
    for idx, row in enumerate(rows, start=1):
        row["row"][0] = idx
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
    row_styles = [rank_style(r["row"][0]) for r in rows] if is_pretty_output(args) else None
    render_table(headers, [r["row"] for r in rows], pretty=is_pretty_output(args), row_styles=row_styles)
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
    entries: dict[str, dict] = {}
    total_races = 0
    for race_id, payload in payloads:
        results = _race_results(payload)
        if not results:
            continue
        base_secs = base_time_seconds(results)
        cat_id = (payload.get("Competition") or {}).get("catId", "").upper()
        if not _is_relay(payload):
            results = _apply_top_filter(results, args.top, cat_id, season_id)
        if not results:
            continue
        course_times = _fetch_analytic_map(race_id, "CRST") if not _is_relay(payload) else {}
        range_times = _fetch_analytic_map(race_id, "RNGT") if not _is_relay(payload) else {}
        race_has_data = False
        for res in results:
            ident = res.get("IBUId") or res.get("Name") or res.get("ShortName") or ""
            if not ident:
                continue
            name = res.get("Name") or res.get("ShortName") or ""
            nat = res.get("Nat") or ""
            discipline = str((payload.get("Competition") or {}).get("DisciplineId") or "").upper()
            if discipline == "IN":
                misses = _parse_shootings(res.get("ShootingTotal"))
                secs = float(sum(misses) * 60) if misses else None
            elif discipline == "PU":
                result_val = result_seconds(res, base_secs)
                delay = parse_time_seconds(res.get("StartInfo")) if res.get("StartInfo") else None
                if result_val is None or delay is None:
                    secs = None
                else:
                    base_val = result_val - delay
                    course_val = parse_time_seconds(
                        _lookup_analytic_time(course_times, res)
                        or get_first_time(res, ["TotalCourseTime", "CourseTime", "RunTime"])
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
                    or get_first_time(res, ["TotalCourseTime", "CourseTime", "RunTime"])
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
        rows.append({
            "rank_val": entry["total_secs"],
            "row": [
                0,
                entry["name"],
                entry["nat"],
                entry["races"],
                format_seconds(entry["total_secs"]),
            ],
        })
    rows.sort(key=lambda r: (r["rank_val"], r["row"][1]))
    for idx, row in enumerate(rows, start=1):
        row["row"][0] = idx
    rows = _apply_limit(rows, args.limit)
    headers = ["Rank", "Biathlete", "Country", "Races", "Total Penalty Time"]
    row_styles = [rank_style(r["row"][0]) for r in rows] if is_pretty_output(args) else None
    render_table(headers, [r["row"] for r in rows], pretty=is_pretty_output(args), row_styles=row_styles)
    return 0


def handle_cumulate_remontada(args: argparse.Namespace) -> int:
    """Cumulate pursuit gains and per-location columns."""
    if args.discipline != "all":
        print("error: --discipline is not supported for remontada", file=sys.stderr)
        return 1
    try:
        payloads, season_id = _collect_races(
            args, allow_discipline=True, discipline_override="pursuit", allow_event=False
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

    entries: dict[str, dict] = {}
    for label, payload in zip(labels, race_payloads):
        results = extract_results(payload)
        cat_id = (payload.get("Competition") or {}).get("catId", "").upper()
        results = _apply_top_filter(results, args.top, cat_id, season_id)
        for res in results:
            status = _status_label(res)
            start_rank = res.get("StartOrder") or res.get("StartPosition")
            finish_rank = res.get("Rank") or res.get("ResultOrder")
            try:
                gain = int(start_rank) - int(finish_rank)
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
            f"+{entry['total_gain']}" if entry["total_gain"] > 0 else entry["total_gain"],
        ]
        for label in labels:
            gain_val = entry["gains"].get(label, "-")
            if isinstance(gain_val, int) and gain_val > 0:
                gain_val = f"+{gain_val}"
            row.append(gain_val)
        row.append(f"+{avg_gain:.1f}" if avg_gain > 0 else f"{avg_gain:.1f}")
        rows.append({"rank_val": -entry["total_gain"], "row": row})
    rows.sort(key=lambda r: (r["rank_val"], r["row"][1]))
    for idx, row in enumerate(rows, start=1):
        row["row"][0] = idx
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
