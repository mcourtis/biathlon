"""Shooting accuracy command handler."""

from __future__ import annotations

import argparse
import sys

from ..api import BiathlonError, get_cup_results, get_current_season_id, get_events, get_race_results, get_races
from ..constants import (
    CAT_TO_GENDER,
    GENDER_TO_CAT,
    INDIVIDUAL_DISCIPLINES,
    RELAY_DISCIPLINE,
    RELAY_MEN_CAT,
    RELAY_MIXED_CAT,
    RELAY_WOMEN_CAT,
    SINGLE_MIXED_RELAY_DISCIPLINE,
)
from ..formatting import Color, format_pct, is_pretty_output, rank_style, render_table
from ..utils import extract_results, parse_relay_shootings
from .results import _has_completed_results
from .scores import find_cup_id


def accumulate_accuracy_by_athlete(results: list[dict]) -> dict[str, dict]:
    """Aggregate shooting accuracy stats per athlete."""
    stats: dict[str, dict] = {}
    name_to_id: dict[str, str] = {}
    name_conflicts: set[str] = set()

    def _name_key(value: str) -> str:
        return " ".join(value.split()).lower()

    for res in results:
        if res.get("IsTeam"):
            continue
        ibu_id = res.get("IBUId")
        if not ibu_id:
            continue
        for key in (res.get("Name"), res.get("ShortName")):
            if not key:
                continue
            normalized = _name_key(str(key))
            if normalized in name_to_id and name_to_id[normalized] != ibu_id:
                name_conflicts.add(normalized)
                continue
            name_to_id[normalized] = ibu_id

    for res in results:
        if res.get("IsTeam"):
            continue
        shootings = res.get("Shootings") or res.get("ShootingTotal")
        if not shootings:
            continue
        ident = res.get("IBUId")
        if not ident:
            name_key = res.get("Name") or res.get("ShortName") or ""
            if name_key:
                normalized = _name_key(str(name_key))
                if normalized and normalized not in name_conflicts:
                    ident = name_to_id.get(normalized, "")
        ident = ident or res.get("Bib") or res.get("Name")
        if not ident:
            continue
        race_id = res.get("_race_id") or ""
        discipline = res.get("_discipline", "")
        is_relay = discipline in {RELAY_DISCIPLINE, SINGLE_MIXED_RELAY_DISCIPLINE}

        if is_relay:
            # Relay format: "P+S P+S" where P=penalties, S=spares used
            # Shots = 5 + spares per stage, Misses = penalties + spares
            stages = parse_relay_shootings(shootings)
            if not stages:
                continue
            prone, standing = stages
            prone_pen, prone_spare = prone
            stand_pen, stand_spare = standing
            prone_shots = 5 + prone_spare
            stand_shots = 5 + stand_spare
            prone_misses = prone_pen + prone_spare
            stand_misses = stand_pen + stand_spare
            shots = prone_shots + stand_shots
            total_misses = prone_misses + stand_misses
        else:
            # Individual race format: "0+1+0+1" (misses per stage, 5 shots each)
            parts = [p.strip() for p in shootings.split("+") if p.strip()]
            if not parts:
                continue
            misses_list: list[int] = []
            for part in parts:
                try:
                    misses_list.append(int(part))
                except ValueError:
                    misses_list.append(0)
            shots = len(parts) * 5
            total_misses = sum(misses_list)
            prone_shots = 0
            stand_shots = 0
            prone_misses = 0
            stand_misses = 0
            for idx, miss_val in enumerate(misses_list):
                if idx % 2 == 0:
                    prone_shots += 5
                    prone_misses += miss_val
                else:
                    stand_shots += 5
                    stand_misses += miss_val
        entry = stats.setdefault(ident, {
            "name": res.get("Name") or res.get("ShortName") or "",
            "nat": res.get("Nat") or "",
            "races": 0, "race_ids": set(), "individual_race_ids": set(), "shots": 0, "misses": 0,
            "prone_shots": 0, "prone_misses": 0,
            "standing_shots": 0, "standing_misses": 0,
        })
        if race_id:
            entry["race_ids"].add(race_id)
            entry["races"] = len(entry["race_ids"])
            if res.get("_discipline") in INDIVIDUAL_DISCIPLINES:
                entry["individual_race_ids"].add(race_id)
        else:
            entry["races"] += 1
        entry["shots"] += shots
        entry["misses"] += total_misses
        entry["prone_shots"] += prone_shots
        entry["prone_misses"] += prone_misses
        entry["standing_shots"] += stand_shots
        entry["standing_misses"] += stand_misses
    return stats


def _fetch_cup_standings(season_id: str, gender: str) -> list[dict]:
    """Fetch World Cup standings for a season and gender."""
    try:
        cup_id = find_cup_id(season_id, gender, level=1, cup_type="total")
        payload = get_cup_results(cup_id)
        return payload.get("Rows") or payload.get("Results") or []
    except BiathlonError:
        return []


def handle_shooting(args: argparse.Namespace) -> int:
    """Show shooting accuracy for race/event/season."""
    scope_count = sum(1 for v in [args.race, args.event, args.season] if v)
    if scope_count > 1:
        print("error: use only one of --race, --event, or --season", file=sys.stderr)
        return 1

    season_id = args.season or get_current_season_id()
    gender = "men" if args.men else "women"
    cat_id = GENDER_TO_CAT.get(gender.lower())
    current_gender = gender
    current_cat_id = cat_id

    results_to_process: list[dict] = []
    scope_label = f"season {season_id}" if not args.event and not args.race else ""
    race_ids: set[str] = set()
    race_meta: list[dict] = []

    def add_results_from_race(race_id: str, discipline_hint: str = "") -> None:
        nonlocal scope_label, current_cat_id, current_gender
        try:
            payload = get_race_results(race_id)
        except BiathlonError:
            return
        if not _has_completed_results(payload):
            return
        comp = payload.get("Competition") or {}
        comp_cat = str(
            comp.get("catId") or comp.get("CatId") or (payload.get("SportEvt") or {}).get("CatId") or ""
        ).upper()
        discipline = str(comp.get("DisciplineId") or discipline_hint or "").upper()
        if args.race and comp_cat and comp_cat != current_cat_id and comp_cat in CAT_TO_GENDER:
            current_cat_id = comp_cat
            current_gender = CAT_TO_GENDER[comp_cat]
        include_mode = (args.include_relay or "").lower()

        def include_relay_race(discipline_id: str, category_id: str) -> bool:
            if not include_mode:
                return False
            if include_mode == "all":
                return discipline_id in {RELAY_DISCIPLINE, SINGLE_MIXED_RELAY_DISCIPLINE}
            if include_mode == "single-mixed":
                return discipline_id == SINGLE_MIXED_RELAY_DISCIPLINE
            if include_mode == "mixed-relay":
                return discipline_id == RELAY_DISCIPLINE and category_id == RELAY_MIXED_CAT
            if include_mode == "relay":
                return discipline_id == RELAY_DISCIPLINE and category_id in {RELAY_MEN_CAT, RELAY_WOMEN_CAT}
            return False

        is_relay = discipline in {RELAY_DISCIPLINE, SINGLE_MIXED_RELAY_DISCIPLINE}
        if is_relay and not include_relay_race(discipline, comp_cat):
            return
        if args.all_races and discipline not in INDIVIDUAL_DISCIPLINES:
            if not include_relay_race(discipline, comp_cat):
                return
        if current_cat_id:
            if comp_cat:
                allow_mixed = include_mode in {"mixed-relay", "single-mixed", "all"} and comp_cat == RELAY_MIXED_CAT
                if comp_cat != current_cat_id and not (is_relay and allow_mixed):
                    return
            else:
                return
        results = extract_results(payload)
        if not results:
            return
        for res in results:
            res["_race_id"] = race_id
            res["_discipline"] = discipline
        # Only count races that have actual shooting data
        if not any(r.get("Shootings") for r in results if not r.get("IsTeam")):
            return
        results_to_process.extend(results)
        if args.race and not scope_label:
            scope_label = comp.get("ShortDescription") or payload.get("SportEvt", {}).get("ShortDescription") or race_id
        if race_id:
            race_ids.add(race_id)
            race_meta.append({
                "race_id": race_id, "discipline": discipline, "cat": comp_cat or "",
                "label": comp.get("ShortDescription") or comp.get("Description") or "",
            })

    if args.race:
        add_results_from_race(args.race)
    else:
        events = get_events(season_id, level=1)
        event_list = [ev for ev in events if ev.get("EventId") == args.event] if args.event else events
        for ev in event_list:
            event_id = ev.get("EventId")
            if not event_id:
                continue
            for race in get_races(event_id):
                race_id = race.get("RaceId") or race.get("Id") or ""
                discipline_hint = str(race.get("DisciplineId") or "").upper()
                add_results_from_race(race_id, discipline_hint)
            if args.event and not scope_label:
                scope_label = ev.get("ShortDescription") or ev.get("Organizer") or args.event
        if args.event and not scope_label:
            scope_label = args.event

    if not results_to_process:
        print("no shooting data found for the requested scope", file=sys.stderr)
        return 1

    total_races = len(race_ids)
    if args.all_races and total_races == 0:
        label = "no races found for the requested scope" if args.include_relay else "no non-relay races found for the requested scope"
        print(label, file=sys.stderr)
        return 1

    stats = accumulate_accuracy_by_athlete(results_to_process)
    if not stats:
        print("no shooting data found for the requested scope", file=sys.stderr)
        return 1

    # Fetch cup standings once (used for WC position column and --top filter)
    cup_rows = _fetch_cup_standings(season_id, current_gender)
    cup_rankings: dict[str, str] = {}
    for row in cup_rows:
        name = row.get("Name") or row.get("ShortName") or ""
        if name:
            cup_rankings[name] = str(row.get("Rank") or row.get("ResultOrder") or "")

    rows = []
    for entry in stats.values():
        shots = entry["shots"]
        misses = entry["misses"]
        hits = shots - misses
        prone_hits = entry["prone_shots"] - entry["prone_misses"]
        standing_hits = entry["standing_shots"] - entry["standing_misses"]
        acc = hits / shots if shots else -1
        rows.append({
            "name": entry["name"], "nat": entry["nat"],
            "races": entry["races"], "shots": shots, "hits": hits,
            "misses": misses, "acc": acc,
            "prone_shots": entry["prone_shots"], "prone_hits": prone_hits,
            "standing_shots": entry["standing_shots"], "standing_hits": standing_hits,
            "wc_position": cup_rankings.get(entry["name"], "-"),
            "individual_races": len(entry.get("individual_race_ids", set())),
        })

    must_start_all = args.all_races or bool(args.event)
    if must_start_all:
        rows = [row for row in rows if row["races"] == total_races]
    if args.min_race and args.min_race > 0:
        rows = [row for row in rows if row["individual_races"] >= args.min_race]

    # Filter to top N athletes in WC standings (reuse already-fetched data)
    if args.top and args.top > 0 and cup_rows:
        top_names = {
            r.get("Name") or r.get("ShortName") or ""
            for r in cup_rows[:args.top]
        }
        top_names.discard("")
        if top_names:
            rows = [row for row in rows if row["name"] in top_names]

    allowed_sorts = {
        "accuracy", "misses", "shots", "races", "name", "country",
        "prone_misses", "standing_misses", "prone_accuracy", "standing_accuracy",
    }
    if args.sort and args.sort.lower() not in allowed_sorts:
        print(f"error: sort must be one of {', '.join(sorted(allowed_sorts))}", file=sys.stderr)
        return 1

    if must_start_all and not rows:
        if args.debug_races:
            for meta in race_meta:
                print(f"race {meta.get('race_id','')} disc={meta.get('discipline','')} cat={meta.get('cat','')} label={meta.get('label','')}")
        qualifier = "non-relay " if args.all_races and not args.include_relay else ""
        print(f"no athletes shot in all {total_races} {qualifier}races of this scope", file=sys.stderr)
        return 1

    def sort_key(row: dict, column: str) -> tuple:
        col = column.lower()
        if col == "name":
            return (0, row["name"])
        if col == "country":
            return (0, row["nat"], row["name"])
        if col == "misses":
            return (0, row["misses"], -row["shots"], row["name"])
        if col == "accuracy":
            return (0, -(row["acc"] if row["acc"] >= 0 else -1), -row["shots"], row["name"])
        if col == "prone_misses":
            return (0, row["prone_shots"] - row["prone_hits"], -row["shots"], row["name"])
        if col == "standing_misses":
            return (0, row["standing_shots"] - row["standing_hits"], -row["shots"], row["name"])
        if col == "prone_accuracy":
            pct = row["prone_hits"] / row["prone_shots"] if row["prone_shots"] else -1
            return (0, -pct, -row["shots"], row["name"])
        if col == "standing_accuracy":
            pct = row["standing_hits"] / row["standing_shots"] if row["standing_shots"] else -1
            return (0, -pct, -row["shots"], row["name"])
        if col in {"shots", "races"}:
            return (0, -row[col], row["name"])
        return (0, row["name"])

    sort_col = (args.sort or "accuracy").lower()
    rows.sort(key=lambda row: sort_key(row, sort_col))

    def row_key(row: dict) -> tuple[str, str]:
        return (row["name"], row["nat"])

    base_sorted = sorted(rows, key=lambda row: sort_key(row, "accuracy"))
    base_rank_map = {}
    base_rank = 1
    for row in base_sorted:
        if row["shots"] == 0:
            continue
        base_rank_map[row_key(row)] = base_rank
        base_rank += 1

    headers = [
        "Rank", "Name", "Country", "WCRank", "Races", "Shots", "Misses",
        "ProneMisses", "StandingMisses", "Accuracy", "ProneAccuracy", "StandingAccuracy",
    ]
    render_rows = []
    accuracy_values: list[tuple[float, float, float]] = []
    position = 1
    for row in rows:
        if row["shots"] == 0:
            continue
        acc = row["hits"] / row["shots"] if row["shots"] else 0
        prone_acc = row["prone_hits"] / row["prone_shots"] if row["prone_shots"] else 0
        standing_acc = row["standing_hits"] / row["standing_shots"] if row["standing_shots"] else 0
        rank_val = base_rank_map.get(row_key(row), position)
        render_rows.append([
            rank_val, row["name"], row["nat"], row.get("wc_position", "-"),
            row["races"], row["shots"], row["misses"],
            row["prone_shots"] - row["prone_hits"],
            row["standing_shots"] - row["standing_hits"],
            format_pct(row["hits"], row["shots"]),
            format_pct(row["prone_hits"], row["prone_shots"]),
            format_pct(row["standing_hits"], row["standing_shots"]),
        ])
        accuracy_values.append((acc, prone_acc, standing_acc))
        position += 1

    pretty = is_pretty_output(args)
    show_sort_rank = bool(args.sort)
    if show_sort_rank:
        headers = ["Sort"] + headers
        for idx, row in enumerate(render_rows, start=1):
            row.insert(0, idx)

    cell_formatters = None
    if pretty:
        def rank_formatter(cell_str: str, row_idx: int) -> str:
            if row_idx >= len(render_rows):
                return cell_str
            rank_idx = 1 if show_sort_rank else 0
            style = rank_style(render_rows[row_idx][rank_idx])
            if style == "gold":
                return Color.gold(cell_str)
            if style == "silver":
                return Color.silver(cell_str)
            if style == "bronze":
                return Color.bronze(cell_str)
            if style == "flowers":
                return Color.flowers(cell_str)
            return Color.dim(cell_str)

        cell_formatters = [rank_formatter] * len(headers)
        for label in ("Accuracy", "ProneAccuracy", "StandingAccuracy"):
            if label in headers:
                cell_formatters[headers.index(label)] = None

    # Create cell formatters for accuracy columns using fixed thresholds.
    if pretty and accuracy_values:
        accuracy_values_display = accuracy_values

        # Apply display limit after computing the scale.
        limit_n = getattr(args, "limit", 25) or 0
        if limit_n > 0:
            render_rows = render_rows[:limit_n]
            accuracy_values_display = accuracy_values_display[:limit_n]

        def make_acc_formatter(acc_idx: int):
            def formatter(cell_str: str, row_idx: int) -> str:
                if row_idx < len(accuracy_values_display):
                    pct = accuracy_values_display[row_idx][acc_idx]
                    return Color.accuracy(cell_str, pct)
                return cell_str
            return formatter

        if cell_formatters is None:
            cell_formatters = [None] * len(headers)
        if "Accuracy" in headers:
            cell_formatters[headers.index("Accuracy")] = make_acc_formatter(0)
        if "ProneAccuracy" in headers:
            cell_formatters[headers.index("ProneAccuracy")] = make_acc_formatter(1)
        if "StandingAccuracy" in headers:
            cell_formatters[headers.index("StandingAccuracy")] = make_acc_formatter(2)
    else:
        # Apply display limit when accuracy colors are not used.
        limit_n = getattr(args, "limit", 25) or 0
        if limit_n > 0:
            render_rows = render_rows[:limit_n]

    print()
    print(f"# Shooting accuracy — {current_gender} — {scope_label or (f'season {season_id}' if not args.race else args.race)}")
    highlight_headers = None
    if pretty:
        highlight_set = set()
        if not show_sort_rank:
            highlight_set.add("Accuracy")
        sort_header_map = {
            "accuracy": "Accuracy",
            "misses": "Misses",
            "shots": "Shots",
            "races": "Races",
            "name": "Name",
            "country": "Country",
            "prone_misses": "ProneMisses",
            "standing_misses": "StandingMisses",
            "prone_accuracy": "ProneAccuracy",
            "standing_accuracy": "StandingAccuracy",
        }
        target_header = sort_header_map.get(sort_col)
        if target_header:
            highlight_set.add(target_header)
        highlight_headers = [headers.index(label) for label in highlight_set if label in headers]
    render_table(
        headers,
        render_rows,
        pretty=pretty,
        cell_formatters=cell_formatters,
        highlight_headers=highlight_headers,
    )
    print()
    return 0
