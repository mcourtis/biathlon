"""Achievements (medal table with all/individual/relay splits) command handler."""

from __future__ import annotations

import argparse
import datetime
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from ..api import (
    BiathlonError,
    get_athlete_bio,
    get_cup_results,
    get_cups,
    get_current_season_id,
    get_events,
    get_race_results,
    get_races,
    get_seasons,
)
from ..constants import (
    CATEGORY_DISPLAY_NAMES,
    EVENT_TYPE_LABELS,
    EVENT_TYPE_OWG,
    EVENT_TYPE_WC,
    EVENT_TYPE_WCH,
    RELAY_DISCIPLINES,
)
from ..formatting import (
    Color,
    get_output_format,
    is_pretty_output,
    rank_style,
    render_table,
)
from ..utils import parse_date, parse_start_datetime
from ._common import (
    DISCIPLINE_LEADER_MARKER,
    GENERAL_LEADER_MARKER,
    U23_LEADER_MARKER,
    _format_leader_markers,
    _max_workers,
    _parse_rank,
    _row_ibu_id,
    detect_event_type,
    is_mixed_relay,
)
from . import standings as standings_cmd


MEDAL_BY_RANK = {1: "gold", 2: "silver", 3: "bronze"}
WC_TITLE_FIELDS = [
    ("TS", "General", "general"),
    ("SP", "Sprint", "sprint"),
    ("PU", "Pursuit", "pursuit"),
    ("IN", "Individual", "individual"),
    ("MS", "Mass Start", "mass_start"),
]


def _parse_birth_date_value(value: object) -> datetime.date | None:
    text = str(value or "").strip()
    if not text:
        return None

    parsed = parse_date(text)
    if parsed is not None:
        return parsed

    compact = text.replace(" ", "")
    for fmt in ("%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.datetime.strptime(compact, fmt).date()
        except ValueError:
            continue
    return None


def _extract_birth_date(bio: dict) -> datetime.date | None:
    for key in ("BirthDate", "Birthdate", "DateOfBirth", "DOB", "Birthday"):
        parsed = _parse_birth_date_value(bio.get(key))
        if parsed is not None:
            return parsed

    for item in bio.get("Personal", []):
        label = str(item.get("Description") or "").strip().lower()
        if not label:
            continue
        if "birth" in label or "born" in label:
            parsed = _parse_birth_date_value(item.get("Value"))
            if parsed is not None:
                return parsed
    return None


def _extract_age_text(bio: dict) -> str | None:
    personal = {
        str(item.get("Description") or "").strip().lower(): item.get("Value")
        for item in bio.get("Personal", [])
        if item.get("Description")
    }
    value = bio.get("Age") or personal.get("age")
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not text:
        return None
    if "," in text:
        text = text.split(",", 1)[0].strip()
    return text or None


def _age_on_date(birth_date: datetime.date, reference_date: datetime.date) -> int:
    years = reference_date.year - birth_date.year
    if (reference_date.month, reference_date.day) < (birth_date.month, birth_date.day):
        years -= 1
    return years


def _extract_age_years(text: str) -> int | None:
    match = re.search(r"\d{1,2}", text or "")
    if not match:
        return None
    return int(match.group(0))


def _normalize_season_arg(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    if text.lower() == "all":
        return "all"
    if re.fullmatch(r"\d{2}/\d{2}", text):
        return text[:2] + text[3:]
    return text


def _normalize_nationality_arg(value: str) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    return text[:3]


def _sort_season_key(value: str) -> tuple[int, str]:
    text = str(value or "")
    if text.isdigit():
        return (int(text), text)
    return (-1, text)


def _event_date_bounds(
    event: dict,
) -> tuple[datetime.date | None, datetime.date | None]:
    start_raw = event.get("StartDate") or event.get("FirstCompetitionDate") or ""
    end_raw = event.get("EndDate") or start_raw
    start_dt = parse_start_datetime(str(start_raw))
    end_dt = parse_start_datetime(str(end_raw))
    start_date = start_dt.date() if start_dt else None
    end_date = end_dt.date() if end_dt else start_date
    return start_date, end_date


def _fetch_events_for_seasons(season_ids: list[str]) -> dict[str, list[dict]]:
    if not season_ids:
        return {}
    events_by_season: dict[str, list[dict]] = {sid: [] for sid in season_ids}
    with ThreadPoolExecutor(
        max_workers=_max_workers(len(season_ids), cap=10)
    ) as executor:
        futures = {executor.submit(get_events, sid, 1): sid for sid in season_ids}
        for future in as_completed(futures):
            sid = futures[future]
            try:
                events_by_season[sid] = list(future.result())
            except BiathlonError:
                events_by_season[sid] = []
    return events_by_season


def _filter_scope_events(events: list[dict], scope: str) -> list[dict]:
    return [event for event in events if detect_event_type(event) == scope]


def _pick_current_or_last_major_season(events_by_season: dict[str, list[dict]]) -> str:
    today = datetime.date.today()
    windows: list[tuple[str, datetime.date, datetime.date]] = []
    for sid, events in events_by_season.items():
        bounds = [_event_date_bounds(event) for event in events]
        starts = [start for start, _ in bounds if start is not None]
        ends = [end for _, end in bounds if end is not None]
        if not starts or not ends:
            continue
        windows.append((sid, min(starts), max(ends)))

    if not windows:
        return ""

    current = [entry for entry in windows if entry[1] <= today <= entry[2]]
    if current:
        current.sort(key=lambda entry: (_sort_season_key(entry[0]), entry[2]))
        return current[-1][0]

    completed = [entry for entry in windows if entry[2] <= today]
    if completed:
        completed.sort(key=lambda entry: (entry[2], _sort_season_key(entry[0])))
        return completed[-1][0]

    windows.sort(key=lambda entry: (entry[1], _sort_season_key(entry[0])))
    return windows[-1][0]


def _resolve_scope(args: argparse.Namespace) -> str:
    if getattr(args, "olympics", False):
        return EVENT_TYPE_OWG
    if getattr(args, "world", False):
        return EVENT_TYPE_WCH
    return EVENT_TYPE_WC


def _resolve_season_selection(
    scope: str,
    season_arg: str,
) -> tuple[list[str], str, dict[str, list[dict]]]:
    if season_arg and season_arg != "all":
        events = _fetch_events_for_seasons([season_arg])
        scope_events = {
            season_arg: _filter_scope_events(events.get(season_arg, []), scope)
        }
        return [season_arg], season_arg, scope_events

    if scope == EVENT_TYPE_WC and not season_arg:
        season_id = get_current_season_id()
        events = _fetch_events_for_seasons([season_id])
        scope_events = {
            season_id: _filter_scope_events(events.get(season_id, []), scope)
        }
        return [season_id], season_id, scope_events

    all_seasons = [str(season.get("SeasonId") or "") for season in get_seasons()]
    all_seasons = [sid for sid in all_seasons if sid]
    events = _fetch_events_for_seasons(all_seasons)
    scope_events = {
        sid: _filter_scope_events(events.get(sid, []), scope) for sid in all_seasons
    }

    if season_arg == "all":
        selected = [sid for sid in all_seasons if scope_events.get(sid)]
        selected.sort(key=_sort_season_key)
        return selected, "all", scope_events

    if scope == EVENT_TYPE_WC:
        selected = [sid for sid in all_seasons if scope_events.get(sid)]
        selected.sort(key=_sort_season_key)
        return selected, "all", scope_events

    auto_season = _pick_current_or_last_major_season(scope_events)
    if not auto_season:
        return [], "auto", scope_events
    return [auto_season], auto_season, scope_events


def _race_sort_key(meta: dict) -> tuple[bool, datetime.datetime, str]:
    start_dt = meta.get("start_dt")
    fallback = datetime.datetime.max.replace(tzinfo=datetime.timezone.utc)
    return (start_dt is None, start_dt or fallback, str(meta.get("race_id") or ""))


def _race_matches_category(discipline: str, race_cat: str, category: str) -> bool:
    if is_mixed_relay(discipline, race_cat):
        return True
    return race_cat == category


def _collect_race_meta(
    season_ids: list[str],
    scope_events: dict[str, list[dict]],
    category: str,
) -> list[dict]:
    event_ids: list[tuple[str, str]] = []
    for sid in season_ids:
        for event in scope_events.get(sid, []):
            event_id = str(event.get("EventId") or "")
            if event_id:
                event_ids.append((sid, event_id))

    if not event_ids:
        return []

    race_meta: list[dict] = []
    with ThreadPoolExecutor(
        max_workers=_max_workers(len(event_ids), cap=12)
    ) as executor:
        futures = {
            executor.submit(get_races, event_id): (sid, event_id)
            for sid, event_id in event_ids
        }
        for future in as_completed(futures):
            sid, event_id = futures[future]
            try:
                races = list(future.result())
            except BiathlonError:
                continue
            for race in races:
                race_id = str(race.get("RaceId") or race.get("Id") or "")
                if not race_id:
                    continue
                discipline = str(race.get("DisciplineId") or "").upper()
                race_cat = str(race.get("catId") or race.get("CatId") or "").upper()
                if not _race_matches_category(discipline, race_cat, category):
                    continue
                start_dt = parse_start_datetime(
                    str(race.get("StartTime") or race.get("StartDate") or "")
                )
                race_meta.append(
                    {
                        "season_id": sid,
                        "event_id": event_id,
                        "race_id": race_id,
                        "discipline": discipline,
                        "cat": race_cat,
                        "start_dt": start_dt,
                    }
                )
    race_meta.sort(key=_race_sort_key)
    return race_meta


def _fetch_race_payloads(race_meta: list[dict]) -> dict[str, dict]:
    if not race_meta:
        return {}
    payload_by_race: dict[str, dict] = {}
    with ThreadPoolExecutor(
        max_workers=_max_workers(len(race_meta), cap=12)
    ) as executor:
        futures = {
            executor.submit(get_race_results, meta["race_id"]): meta["race_id"]
            for meta in race_meta
        }
        for future in as_completed(futures):
            race_id = futures[future]
            try:
                payload_by_race[race_id] = dict(future.result())
            except BiathlonError:
                continue
    return payload_by_race


def _empty_stats() -> dict[str, int]:
    return {
        "gold": 0,
        "silver": 0,
        "bronze": 0,
        "gold_ind": 0,
        "silver_ind": 0,
        "bronze_ind": 0,
        "gold_relay": 0,
        "silver_relay": 0,
        "bronze_relay": 0,
    }


def _empty_athlete(name: str, nat: str, category: str, ibu_id: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "name": name,
        "nat": nat,
        "gender": "F" if category == "SW" else "M",
        "ibu_id": ibu_id,
        "age": "-",
        "is_u23": False,
        "is_best_u23": False,
        "races": 0,
        "races_ind": 0,
        "races_relay": 0,
    }
    out.update(_empty_stats())
    return out


def _add_medal(stats: dict[str, Any], medal: str, is_relay: bool) -> None:
    stats[medal] += 1
    suffix = "_relay" if is_relay else "_ind"
    stats[f"{medal}{suffix}"] += 1


def _add_race(stats: dict[str, Any], is_relay: bool) -> None:
    stats["races"] += 1
    if is_relay:
        stats["races_relay"] += 1
    else:
        stats["races_ind"] += 1


def _gender_cat_from_value(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text in {"W", "WOMEN", "F", "FEMALE"}:
        return "SW"
    if text in {"M", "MEN", "MALE"}:
        return "SM"
    return ""


def _gender_cat_for_ibu(ibu_id: str, cache: dict[str, str]) -> str:
    if not ibu_id:
        return ""
    if ibu_id in cache:
        return cache[ibu_id]
    try:
        bio = get_athlete_bio(ibu_id)
    except BiathlonError:
        cache[ibu_id] = ""
        return cache[ibu_id]
    cache[ibu_id] = _gender_cat_from_value(bio.get("GenderId") or bio.get("Gender"))
    return cache[ibu_id]


def _collect_podium(results: list[dict], is_relay: bool) -> dict[int, dict]:
    candidates = [row for row in results if bool(row.get("IsTeam")) == is_relay]
    candidates.sort(
        key=lambda row: (
            _parse_rank(row.get("Rank") or row.get("SO") or row.get("ResultOrder"))
            or 10**9,
            row.get("ResultOrder", 10**9),
        )
    )
    podium: dict[int, dict] = {}
    for row in candidates:
        rank = _parse_rank(row.get("Rank") or row.get("SO") or row.get("ResultOrder"))
        if rank in {1, 2, 3} and rank not in podium:
            podium[rank] = row
        if len(podium) == 3:
            break
    return podium if 1 in podium else {}


def _prefer_name(current: str, candidate: str) -> str:
    cur = str(current or "").strip()
    cand = str(candidate or "").strip()
    if not cand:
        return cur
    if not cur or len(cand) > len(cur):
        return cand
    return cur


def _has_medal(stats: dict[str, Any]) -> bool:
    return bool(stats["gold"] or stats["silver"] or stats["bronze"])


def _stats_total(stats: dict[str, Any], prefix: str = "") -> int:
    if prefix:
        return (
            stats[f"gold_{prefix}"]
            + stats[f"silver_{prefix}"]
            + stats[f"bronze_{prefix}"]
        )
    return stats["gold"] + stats["silver"] + stats["bronze"]


def _sort_stats_rows(rows: list[dict], label_key: str) -> list[dict]:
    return sorted(
        rows,
        key=lambda row: (
            -row["gold"],
            -row.get("gold_ind", 0),
            -row["silver"],
            -row.get("silver_ind", 0),
            -row["bronze"],
            -row.get("bronze_ind", 0),
            row.get("races", 0),
            row.get("races_ind", 0),
            str(row.get(label_key, "")),
        ),
    )


def _aggregate_achievements(
    race_meta: list[dict],
    payload_by_race: dict[str, dict],
    category: str,
    by_country: bool,
) -> tuple[list[dict], int]:
    country_stats: dict[str, dict[str, Any]] = {}
    athlete_stats: dict[str, dict[str, Any]] = {}
    known_cat_ids: set[str] = set()
    gender_cache: dict[str, str] = {}
    races_used = 0

    for meta in race_meta:
        race_id = meta["race_id"]
        payload = payload_by_race.get(race_id)
        if not payload:
            continue
        results = list(payload.get("Results") or [])
        if not results:
            continue

        comp = payload.get("Competition") or {}
        discipline = str(
            meta.get("discipline") or comp.get("DisciplineId") or ""
        ).upper()
        race_cat = str(
            meta.get("cat") or comp.get("catId") or comp.get("CatId") or ""
        ).upper()
        is_relay = discipline in RELAY_DISCIPLINES
        mixed = is_mixed_relay(discipline, race_cat)

        podium = _collect_podium(results, is_relay=is_relay)
        if not podium:
            continue
        races_used += 1

        medal_by_nat: dict[str, str] = {}
        for rank, medal in MEDAL_BY_RANK.items():
            row = podium.get(rank)
            if not row:
                continue
            nat = str(row.get("Nat") or "")
            if not nat:
                continue
            medal_by_nat[nat] = medal
            c_row = country_stats.setdefault(nat, {"country": nat, **_empty_stats()})
            _add_medal(c_row, medal, is_relay)

        if by_country:
            continue

        if is_relay:
            seen_keys_relay: set[str] = set()
            for row in results:
                if row.get("IsTeam"):
                    continue
                nat = str(row.get("Nat") or "")
                medal_for_nat = medal_by_nat.get(nat)
                if not medal_for_nat:
                    continue
                ibu_id = _row_ibu_id(row)
                name = str(row.get("Name") or row.get("ShortName") or "")
                key = ibu_id or f"{name}|{nat}"
                if not key or key in seen_keys_relay:
                    continue

                if mixed:
                    if not ibu_id:
                        continue
                    if ibu_id not in known_cat_ids:
                        if _gender_cat_for_ibu(ibu_id, gender_cache) == category:
                            known_cat_ids.add(ibu_id)
                    if ibu_id not in known_cat_ids:
                        continue
                else:
                    if ibu_id:
                        known_cat_ids.add(ibu_id)

                seen_keys_relay.add(key)
                if key not in athlete_stats:
                    athlete_stats[key] = _empty_athlete(name, nat, category, ibu_id)
                athlete_stats[key]["name"] = _prefer_name(
                    athlete_stats[key]["name"], name
                )
                _add_race(athlete_stats[key], is_relay=True)
                _add_medal(athlete_stats[key], medal_for_nat, is_relay=True)
            continue

        seen_keys_individual: set[str] = set()
        medal_by_key: dict[str, str] = {}
        for rank, medal in MEDAL_BY_RANK.items():
            row = podium.get(rank)
            if not row:
                continue
            nat = str(row.get("Nat") or "")
            ibu_id = _row_ibu_id(row)
            name = str(row.get("Name") or row.get("ShortName") or "")
            key = ibu_id or f"{name}|{nat}"
            if key:
                medal_by_key[key] = medal

        for row in results:
            if row.get("IsTeam"):
                continue
            nat = str(row.get("Nat") or "")
            ibu_id = _row_ibu_id(row)
            name = str(row.get("Name") or row.get("ShortName") or "")
            key = ibu_id or f"{name}|{nat}"
            if not key or key in seen_keys_individual:
                continue
            seen_keys_individual.add(key)
            if ibu_id:
                known_cat_ids.add(ibu_id)
            if key not in athlete_stats:
                athlete_stats[key] = _empty_athlete(name, nat, category, ibu_id)
            athlete_stats[key]["name"] = _prefer_name(athlete_stats[key]["name"], name)
            _add_race(athlete_stats[key], is_relay=False)
            medal_for_key = medal_by_key.get(key)
            if medal_for_key:
                _add_medal(athlete_stats[key], medal_for_key, is_relay=False)

    if by_country:
        rows = [row for row in country_stats.values() if _has_medal(row)]
        return _sort_stats_rows(rows, "country"), races_used

    rows = [row for row in athlete_stats.values() if _has_medal(row)]
    return _sort_stats_rows(rows, "name"), races_used


def _reference_date_for_races(race_meta: list[dict]) -> datetime.date:
    dates = [
        meta["start_dt"].date()
        for meta in race_meta
        if isinstance(meta.get("start_dt"), datetime.datetime)
    ]
    if dates:
        return max(dates)
    return datetime.date.today()


def _build_athlete_age_map(
    ibu_ids: set[str], reference_date: datetime.date
) -> tuple[dict[str, str], set[str]]:
    unique_ids = [ibu_id for ibu_id in dict.fromkeys(ibu_ids) if ibu_id]
    if not unique_ids:
        return {}, set()

    age_display_by_id: dict[str, str] = {}
    u23_ids: set[str] = set()
    with ThreadPoolExecutor(
        max_workers=_max_workers(len(unique_ids), cap=16)
    ) as executor:
        future_map = {
            executor.submit(get_athlete_bio, ibu_id): ibu_id for ibu_id in unique_ids
        }
        for future in as_completed(future_map):
            ibu_id = future_map[future]
            try:
                bio = future.result()
            except BiathlonError:
                bio = {}
            except Exception:
                bio = {}
            if not isinstance(bio, dict):
                bio = {}

            age_display = "-"
            is_u23 = False
            birth_date = _extract_birth_date(bio)
            if birth_date is not None:
                age_years = _age_on_date(birth_date, reference_date)
                age_display = str(age_years)
                is_u23 = age_years < 23
            else:
                age_text = _extract_age_text(bio)
                if age_text:
                    age_display = age_text
                    parsed_age_years = _extract_age_years(age_text)
                    if parsed_age_years is not None:
                        is_u23 = parsed_age_years < 23

            if is_u23:
                u23_ids.add(ibu_id)
                if age_display == "-":
                    age_display = "(U23)"
                elif "(U23)" not in age_display:
                    age_display = f"{age_display} (U23)"

            age_display_by_id[ibu_id] = age_display
    return age_display_by_id, u23_ids


def _build_wc_standings_context(season_id: str, category: str) -> dict[str, Any]:
    """Return marker + age metadata aligned with standings command semantics."""
    context: dict[str, Any] = {
        "age_display_by_id": {},
        "u23_ids": set(),
        "best_u23_ids": set(),
        "markers_by_id": {},
        "markers_by_name_nat": {},
        "reference_date": None,
    }
    if not season_id:
        return context

    gender = "men" if category == "SM" else "women"
    try:
        athlete_cup_ids = standings_cmd._get_cup_ids_by_discipline(
            season_id, gender, level=1
        )
    except Exception:
        return context

    total_cup_id = str(athlete_cup_ids.get("TS") or "")
    if not total_cup_id:
        return context

    try:
        total_payload = get_cup_results(total_cup_id)
    except BiathlonError:
        return context

    total_rows = total_payload.get("Rows") or total_payload.get("Results") or []
    if not total_rows:
        return context

    athletes: dict[str, dict[str, Any]] = {}
    for row in total_rows:
        athlete_id = _row_ibu_id(row) or str(row.get("Name") or "").strip()
        if not athlete_id:
            continue
        athletes[athlete_id] = {
            "ibu_id": str(_row_ibu_id(row) or ""),
            "name": str(row.get("Name") or row.get("ShortName") or ""),
            "nat": str(row.get("Nat") or ""),
            "total": standings_cmd._parse_score(row),
            "SP": 0,
            "PU": 0,
            "IN": 0,
            "MS": 0,
            "row_best_u23": standings_cmd._is_best_u23_row(row),
            "is_best_u23": False,
            "is_u23": False,
            "age_display": "-",
        }

    if not athletes:
        return context

    for disc in standings_cmd.DISCIPLINES:
        disc_cup_id = str(athlete_cup_ids.get(disc) or "")
        if not disc_cup_id:
            continue
        try:
            disc_payload = get_cup_results(disc_cup_id)
        except BiathlonError:
            continue
        disc_rows = disc_payload.get("Rows") or disc_payload.get("Results") or []
        for row in disc_rows:
            athlete_id = _row_ibu_id(row) or str(row.get("Name") or "").strip()
            if athlete_id and athlete_id in athletes:
                athletes[athlete_id][disc] = standings_cmd._parse_score(row)

    athlete_list = list(athletes.values())
    try:
        first_race_date = standings_cmd._find_first_race_date(
            season_id, gender, level=1
        )
    except Exception:
        first_race_date = None
    context["reference_date"] = first_race_date

    bio_map = standings_cmd._prefetch_bios(
        [str(athlete.get("ibu_id") or "") for athlete in athlete_list]
    )
    for athlete in athlete_list:
        athlete["is_u23"] = False
        athlete["age_display"] = "-"
        bio = bio_map.get(str(athlete.get("ibu_id") or ""), {})
        if first_race_date is not None:
            birth_date = standings_cmd._extract_birth_date(bio)
            if birth_date is not None:
                age_years = standings_cmd._age_on_date(birth_date, first_race_date)
                athlete["age_display"] = str(age_years)
                athlete["is_u23"] = age_years < 23
                continue
        age_text = standings_cmd._extract_age_text(bio)
        if age_text:
            athlete["age_display"] = age_text

    if not any(bool(athlete.get("is_u23")) for athlete in athlete_list):
        for athlete in athlete_list:
            athlete["is_u23"] = bool(athlete.get("row_best_u23"))

    for athlete in athlete_list:
        if not athlete.get("is_u23"):
            continue
        age_display = str(athlete.get("age_display") or "").strip()
        if not age_display or age_display == "-":
            athlete["age_display"] = "(U23)"
        elif "(U23)" not in age_display:
            athlete["age_display"] = f"{age_display} (U23)"

    best_u23_score = max(
        (
            int(athlete.get("total") or 0)
            for athlete in athlete_list
            if athlete.get("is_u23")
        ),
        default=0,
    )
    best_u23_ids = {
        str(athlete.get("ibu_id") or "")
        for athlete in athlete_list
        if athlete.get("is_u23")
        and int(athlete.get("total") or 0) == best_u23_score
        and best_u23_score > 0
        and str(athlete.get("ibu_id") or "")
    }
    for athlete in athlete_list:
        athlete["is_best_u23"] = str(athlete.get("ibu_id") or "") in best_u23_ids

    leaders = standings_cmd._find_leaders(athlete_list)
    total_leader = leaders.get("total")
    athlete_led_disciplines: dict[str, list[str]] = {}
    for disc in standings_cmd.DISCIPLINES:
        leader_name = leaders.get(disc)
        if leader_name is None:
            continue
        athlete_led_disciplines.setdefault(leader_name, []).append(disc)

    markers_by_id: dict[str, list[str]] = {}
    markers_by_name_nat: dict[tuple[str, str], list[str]] = {}
    age_display_by_id: dict[str, str] = {}
    u23_ids: set[str] = set()
    for athlete in athlete_list:
        ibu_id = str(athlete.get("ibu_id") or "")
        name = str(athlete.get("name") or "")
        nat = str(athlete.get("nat") or "")
        markers: list[str] = []
        if name and name == total_leader:
            markers.append(GENERAL_LEADER_MARKER)
        for _disc in athlete_led_disciplines.get(name, []):
            markers.append(DISCIPLINE_LEADER_MARKER)
        if athlete.get("is_best_u23"):
            markers.append(U23_LEADER_MARKER)
        if markers:
            if ibu_id:
                markers_by_id[ibu_id] = markers
            if name:
                markers_by_name_nat[(name, nat)] = markers
        if ibu_id:
            age_display_by_id[ibu_id] = str(athlete.get("age_display") or "-")
            if athlete.get("is_u23"):
                u23_ids.add(ibu_id)

    context["age_display_by_id"] = age_display_by_id
    context["u23_ids"] = u23_ids
    context["best_u23_ids"] = best_u23_ids
    context["markers_by_id"] = markers_by_id
    context["markers_by_name_nat"] = markers_by_name_nat
    return context


def _leader_markers_from_standings_context(
    rows: list[dict], context: dict[str, Any]
) -> dict[str, list[str]]:
    marker_map: dict[str, list[str]] = {}
    markers_by_id = context.get("markers_by_id") or {}
    markers_by_name_nat = context.get("markers_by_name_nat") or {}
    for row in rows:
        key = _stats_row_key(row, by_country=False)
        ibu_id = str(row.get("ibu_id") or "")
        name = str(row.get("name") or "")
        nat = str(row.get("nat") or "")
        markers = []
        if ibu_id and ibu_id in markers_by_id:
            markers = list(markers_by_id[ibu_id])
        elif (name, nat) in markers_by_name_nat:
            markers = list(markers_by_name_nat[(name, nat)])
        if markers:
            marker_map[key] = markers
    return marker_map


def _enrich_athlete_rows_with_age(
    rows: list[dict],
    reference_date: datetime.date,
    known_age_display_by_id: dict[str, str] | None = None,
    known_u23_ids: set[str] | None = None,
    known_best_u23_ids: set[str] | None = None,
) -> None:
    ibu_ids = {str(row.get("ibu_id") or "") for row in rows if row.get("ibu_id")}
    known_age_display_by_id = known_age_display_by_id or {}
    known_u23_ids = known_u23_ids or set()
    known_best_u23_ids = known_best_u23_ids or set()
    missing_ids = {
        ibu_id for ibu_id in ibu_ids if ibu_id not in known_age_display_by_id
    }
    fallback_age_display_by_id, fallback_u23_ids = _build_athlete_age_map(
        missing_ids, reference_date
    )
    age_display_by_id = {
        **fallback_age_display_by_id,
        **known_age_display_by_id,
    }
    u23_ids = set(fallback_u23_ids) | set(known_u23_ids)

    best_u23_key = ""
    for row in rows:
        ibu_id = str(row.get("ibu_id") or "")
        age_display = age_display_by_id.get(ibu_id, "-") if ibu_id else "-"
        is_u23 = ibu_id in u23_ids if ibu_id else False
        row["age"] = age_display
        row["is_u23"] = is_u23
        if not best_u23_key and is_u23:
            best_u23_key = _stats_row_key(row, by_country=False)

    if known_best_u23_ids:
        for row in rows:
            ibu_id = str(row.get("ibu_id") or "")
            row["is_best_u23"] = bool(ibu_id and ibu_id in known_best_u23_ids)
    else:
        for row in rows:
            row["is_best_u23"] = (
                _stats_row_key(row, by_country=False) == best_u23_key
                if best_u23_key
                else False
            )


def _stats_row_key(row: dict, by_country: bool) -> str:
    if by_country:
        return str(row.get("country") or "")
    ibu_id = str(row.get("ibu_id") or "")
    if ibu_id:
        return f"id:{ibu_id}"
    return f"name:{row.get('name') or ''}|nat:{row.get('nat') or ''}"


def _medal_sort_tuple(row: dict) -> tuple[int, int, int, int, int, int, int, int]:
    return (
        -int(row.get("gold") or 0),
        -int(row.get("gold_ind") or 0),
        -int(row.get("silver") or 0),
        -int(row.get("silver_ind") or 0),
        -int(row.get("bronze") or 0),
        -int(row.get("bronze_ind") or 0),
        int(row.get("races") or 0),
        int(row.get("races_ind") or 0),
    )


def _section_sort_tuple(row: dict, section: str) -> tuple[int, int, int, int, int]:
    if section == "ind":
        return (
            -int(row.get("gold_ind") or 0),
            -int(row.get("silver_ind") or 0),
            -int(row.get("bronze_ind") or 0),
            int(row.get("races_ind") or 0),
            int(row.get("races") or 0),
        )
    return (
        -int(row.get("gold_relay") or 0),
        -int(row.get("silver_relay") or 0),
        -int(row.get("bronze_relay") or 0),
        int(row.get("races_relay") or 0),
        int(row.get("races") or 0),
    )


def _leaders_by_section(rows: list[dict], by_country: bool, section: str) -> set[str]:
    section_key = "ind" if section == "ind" else "relay"
    if max((_stats_total(row, section_key) for row in rows), default=0) <= 0:
        return set()
    best_sort = min((_section_sort_tuple(row, section) for row in rows))
    return {
        _stats_row_key(row, by_country=by_country)
        for row in rows
        if _section_sort_tuple(row, section) == best_sort
    }


def _leader_markers_for_rows(
    rows: list[dict], by_country: bool
) -> dict[str, list[str]]:
    if not rows:
        return {}

    best_sort = min((_medal_sort_tuple(row) for row in rows))
    general_leaders = {
        _stats_row_key(row, by_country=by_country)
        for row in rows
        if _medal_sort_tuple(row) == best_sort
    }
    individual_leaders = _leaders_by_section(rows, by_country, "ind")
    relay_leaders = _leaders_by_section(rows, by_country, "relay")

    marker_map: dict[str, list[str]] = {}
    for row in rows:
        key = _stats_row_key(row, by_country=by_country)
        markers: list[str] = []
        if key in general_leaders:
            markers.append(GENERAL_LEADER_MARKER)
        if key in individual_leaders:
            markers.append(DISCIPLINE_LEADER_MARKER)
        if key in relay_leaders:
            markers.append(DISCIPLINE_LEADER_MARKER)
        if not by_country and bool(row.get("is_best_u23")):
            markers.append(U23_LEADER_MARKER)
        if markers:
            marker_map[key] = markers
    return marker_map


def _apply_rank_style(text: str, style: str) -> str:
    if style == "gold":
        return Color.gold(text)
    if style == "silver":
        return Color.silver(text)
    if style == "bronze":
        return Color.bronze(text)
    if style == "flowers":
        return Color.flowers(text)
    if style == "other":
        return Color.other(text)
    return text


def _make_leader_cell_formatter(
    row_styles: list[str] | None,
) -> Callable[[str, int], str]:
    def _base_formatter(cell_str: str, row_idx: int) -> str:
        if not row_styles or row_idx >= len(row_styles):
            return cell_str
        return _apply_rank_style(cell_str, row_styles[row_idx])

    def _formatter(cell_str: str, row_idx: int) -> str:
        return _format_leader_markers(cell_str, row_idx, _base_formatter)

    return _formatter


def _country_rows(
    rows: list[dict],
    wc_title_map: dict[str, dict[str, Any]] | None = None,
    include_wc_titles: bool = False,
    leader_markers: dict[str, list[str]] | None = None,
) -> list[list[str]]:
    out: list[list[str]] = []
    for idx, row in enumerate(rows, start=1):
        country = str(row["country"])
        markers = (leader_markers or {}).get(_stats_row_key(row, by_country=True), [])
        if markers:
            country = f"{country} {' '.join(markers)}"
        values = [
            str(idx),
            country,
            str(row["gold"]),
            str(row["silver"]),
            str(row["bronze"]),
            str(_stats_total(row)),
            str(row["gold_ind"]),
            str(row["silver_ind"]),
            str(row["bronze_ind"]),
            str(_stats_total(row, "ind")),
            str(row["gold_relay"]),
            str(row["silver_relay"]),
            str(row["bronze_relay"]),
            str(_stats_total(row, "relay")),
        ]
        if include_wc_titles:
            title_stats = (wc_title_map or {}).get(
                str(row["country"])
            ) or _empty_wc_title_counts()
            values.extend(
                [
                    str(title_stats["general"]),
                    str(title_stats["sprint"]),
                    str(title_stats["pursuit"]),
                    str(title_stats["individual"]),
                    str(title_stats["mass_start"]),
                ]
            )
        out.append(values)
    return out


def _athlete_rows(
    rows: list[dict],
    wc_title_map: dict[str, dict[str, Any]] | None = None,
    include_wc_titles: bool = False,
    leader_markers: dict[str, list[str]] | None = None,
) -> list[list[str]]:
    out: list[list[str]] = []
    for idx, row in enumerate(rows, start=1):
        name = str(row["name"])
        markers = (leader_markers or {}).get(_stats_row_key(row, by_country=False), [])
        if markers:
            name = f"{name} {' '.join(markers)}"
        values = [
            str(idx),
            name,
            str(row["nat"]),
            str(row["gender"]),
            str(row.get("age") or "-"),
            str(row["gold"]),
            str(row["silver"]),
            str(row["bronze"]),
            str(_stats_total(row)),
            str(row["gold_ind"]),
            str(row["silver_ind"]),
            str(row["bronze_ind"]),
            str(_stats_total(row, "ind")),
            str(row["gold_relay"]),
            str(row["silver_relay"]),
            str(row["bronze_relay"]),
            str(_stats_total(row, "relay")),
            str(row["races"]),
            str(row["races_ind"]),
            str(row["races_relay"]),
        ]
        if include_wc_titles:
            athlete_key = str(row.get("ibu_id") or f"{row['name']}|{row['nat']}")
            title_stats = (wc_title_map or {}).get(
                athlete_key
            ) or _empty_wc_title_counts()
            values.extend(
                [
                    str(title_stats["general"]),
                    str(title_stats["sprint"]),
                    str(title_stats["pursuit"]),
                    str(title_stats["individual"]),
                    str(title_stats["mass_start"]),
                ]
            )
        out.append(values)
    return out


def _season_label(scope: str, season_input: str, selected_seasons: list[str]) -> str:
    if season_input == "all":
        return "all seasons"
    if season_input:
        return season_input
    if scope == EVENT_TYPE_WC:
        return selected_seasons[0] if selected_seasons else "current"
    return selected_seasons[0] if selected_seasons else "current/last"


def _is_completed_wc_season(events: list[dict]) -> bool:
    """Return True when all World Cup events for a season are in the past."""
    if not events:
        return False
    today = datetime.date.today()
    end_dates: list[datetime.date] = []
    for event in events:
        _, end_date = _event_date_bounds(event)
        if end_date is not None:
            end_dates.append(end_date)
    if not end_dates:
        return False
    return max(end_dates) < today


def _title_rows_from_payload(payload: dict) -> list[dict]:
    return list(payload.get("Rows") or payload.get("Results") or [])


def _pick_cup_winner(rows: list[dict]) -> dict:
    if not rows:
        return {}
    best_row: dict | None = None
    best_rank = 10**9
    for row in rows:
        rank = _parse_rank(row.get("Rank") or row.get("SO") or row.get("ResultOrder"))
        if rank is None:
            continue
        if rank < best_rank:
            best_row = row
            best_rank = rank
            if rank == 1:
                break
    if best_row is not None:
        return best_row
    return rows[0]


def _empty_wc_title_counts() -> dict[str, int]:
    return {
        "general": 0,
        "sprint": 0,
        "pursuit": 0,
        "individual": 0,
        "mass_start": 0,
        "total": 0,
    }


def _build_wc_title_map(
    season_ids: list[str],
    category: str,
    by_country: bool,
) -> dict[str, dict[str, Any]]:
    if not season_ids:
        return {}

    wanted = {disc for disc, _label, _field in WC_TITLE_FIELDS}
    if by_country:
        stats_by_country: dict[str, dict[str, Any]] = {}
    else:
        stats_by_athlete: dict[str, dict[str, Any]] = {}

    for season_id in season_ids:
        try:
            cups = get_cups(season_id)
        except BiathlonError:
            continue

        cup_ids: dict[str, str] = {}
        for cup in cups:
            if cup.get("Level") != 1:
                continue
            if str(cup.get("CatId") or "").upper() != category:
                continue
            discipline = str(cup.get("DisciplineId") or "").upper()
            if discipline == "SI":
                discipline = "IN"
            if discipline not in wanted:
                continue
            cup_id = str(cup.get("CupId") or "")
            if cup_id and discipline not in cup_ids:
                cup_ids[discipline] = cup_id

        for discipline, _label, field in WC_TITLE_FIELDS:
            selected_cup_id = cup_ids.get(discipline)
            if not selected_cup_id:
                continue
            try:
                payload = get_cup_results(selected_cup_id)
            except BiathlonError:
                continue
            winner = _pick_cup_winner(_title_rows_from_payload(payload))
            if not winner:
                continue
            nat = str(winner.get("Nat") or "")
            if by_country:
                if not nat:
                    continue
                entry = stats_by_country.setdefault(nat, _empty_wc_title_counts())
            else:
                name = str(winner.get("Name") or winner.get("ShortName") or "")
                ibu_id = _row_ibu_id(winner)
                key = ibu_id or f"{name}|{nat}"
                if not key or not name:
                    continue
                entry = stats_by_athlete.setdefault(
                    key, {"name": name, "nat": nat, **_empty_wc_title_counts()}
                )
                entry["name"] = _prefer_name(entry["name"], name)
            entry[field] += 1
            entry["total"] += 1

    return stats_by_country if by_country else stats_by_athlete


def handle_achievements(args: argparse.Namespace) -> int:
    """Show medal achievements with all/individual/relay breakdown."""
    if getattr(args, "olympics", False) and getattr(args, "world", False):
        print("error: use only one of --olympics or --world", file=sys.stderr)
        return 1
    if args.limit < 0:
        print("error: --limit must be >= 0", file=sys.stderr)
        return 1

    scope = _resolve_scope(args)
    category = "SM" if args.men else "SW"
    season_input = _normalize_season_arg(args.season)
    nationality_filter = _normalize_nationality_arg(getattr(args, "nationality", ""))
    by_country = bool(args.country)

    try:
        season_ids, season_label_raw, scope_events = _resolve_season_selection(
            scope, season_input
        )
    except BiathlonError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if not season_ids:
        print("no matching events found for the requested scope", file=sys.stderr)
        return 1

    race_meta = _collect_race_meta(season_ids, scope_events, category)
    if not race_meta:
        print("no races found for the requested scope", file=sys.stderr)
        return 1

    payload_by_race = _fetch_race_payloads(race_meta)
    rows, races_used = _aggregate_achievements(
        race_meta,
        payload_by_race,
        category,
        by_country=by_country,
    )

    if nationality_filter:
        if by_country:
            rows = [
                row
                for row in rows
                if str(row.get("country") or "") == nationality_filter
            ]
        else:
            rows = [
                row for row in rows if str(row.get("nat") or "") == nationality_filter
            ]

    if races_used == 0:
        print("no completed races found for achievements", file=sys.stderr)
        return 1
    if not rows:
        print("no medal data found for the requested scope", file=sys.stderr)
        return 1

    if args.limit > 0:
        rows = rows[: args.limit]

    scope_label = EVENT_TYPE_LABELS.get(scope, scope)
    category_label = CATEGORY_DISPLAY_NAMES.get(category, category)
    mode_label = "country" if by_country else "athlete"
    season_label = _season_label(scope, season_label_raw, season_ids)
    output_format = get_output_format(args)
    pretty = is_pretty_output(args)
    include_wc_titles = False
    wc_title_map: dict[str, dict[str, Any]] = {}
    leader_markers: dict[str, list[str]] = {}
    standings_context: dict[str, Any] = {}
    if scope == EVENT_TYPE_WC:
        completed_wc_seasons = [
            sid
            for sid in season_ids
            if _is_completed_wc_season(scope_events.get(sid, []))
        ]
        include_wc_titles = bool(completed_wc_seasons) or season_input == "all"
        if include_wc_titles:
            wc_title_map = _build_wc_title_map(
                completed_wc_seasons,
                category,
                by_country=by_country,
            )
        if not by_country and len(season_ids) == 1 and season_input != "all":
            standings_context = _build_wc_standings_context(season_ids[0], category)
    if not by_country:
        reference_date = standings_context.get("reference_date")
        if not isinstance(reference_date, datetime.date):
            reference_date = _reference_date_for_races(race_meta)
        _enrich_athlete_rows_with_age(
            rows,
            reference_date,
            known_age_display_by_id=dict(
                standings_context.get("age_display_by_id") or {}
            ),
            known_u23_ids=set(standings_context.get("u23_ids") or set()),
            known_best_u23_ids=set(standings_context.get("best_u23_ids") or set()),
        )
    if pretty:
        if (
            not by_country
            and standings_context
            and (
                standings_context.get("markers_by_id")
                or standings_context.get("markers_by_name_nat")
            )
        ):
            leader_markers = _leader_markers_from_standings_context(
                rows, standings_context
            )
            if not leader_markers:
                leader_markers = _leader_markers_for_rows(rows, by_country=False)
        else:
            leader_markers = _leader_markers_for_rows(rows, by_country=by_country)

    print()
    print(
        f"# Achievements - {scope_label} - {category_label}, {mode_label} "
        f"(season: {season_label}, races: {races_used})"
    )
    if nationality_filter:
        print(f"# Nationality filter: {nationality_filter}")

    if by_country:
        data_rows = _country_rows(
            rows,
            wc_title_map=wc_title_map,
            include_wc_titles=include_wc_titles,
            leader_markers=leader_markers,
        )
        headers = [
            "#",
            "Country",
            Color.gold("Gold"),
            Color.silver("Silver"),
            Color.bronze("Bronze"),
            "Total",
            Color.gold("Gold"),
            Color.silver("Silver"),
            Color.bronze("Bronze"),
            "Total",
            Color.gold("Gold"),
            Color.silver("Silver"),
            Color.bronze("Bronze"),
            "Total",
        ]
        if include_wc_titles:
            headers.extend(["G", "SP", "PU", "IN", "MS"])
        row_styles = (
            [
                rank_style(idx) if idx <= 6 else ""
                for idx in range(1, len(data_rows) + 1)
            ]
            if pretty
            else None
        )
        cell_formatters = (
            [None, _make_leader_cell_formatter(row_styles)]
            + [None] * (len(headers) - 2)
            if pretty
            else None
        )
        column_separators = {2, 6, 10}
        group_headers = [(2, 6, "All"), (6, 10, "Individual"), (10, 14, "Relay")]
        if include_wc_titles:
            column_separators.add(14)
            group_headers.append((14, 19, "World Cup Titles"))
        render_table(
            headers,
            data_rows,
            output_format=output_format,
            row_styles=row_styles,
            cell_formatters=cell_formatters,
            column_separators=column_separators,
            group_headers=group_headers,
        )
    else:
        data_rows = _athlete_rows(
            rows,
            wc_title_map=wc_title_map,
            include_wc_titles=include_wc_titles,
            leader_markers=leader_markers,
        )
        headers = [
            "#",
            "Athlete",
            "Nat",
            "Gender",
            "Age",
            Color.gold("Gold"),
            Color.silver("Silver"),
            Color.bronze("Bronze"),
            "Total",
            Color.gold("Gold"),
            Color.silver("Silver"),
            Color.bronze("Bronze"),
            "Total",
            Color.gold("Gold"),
            Color.silver("Silver"),
            Color.bronze("Bronze"),
            "Total",
            "Races",
            "Individual",
            "Relay",
        ]
        if include_wc_titles:
            headers.extend(["G", "SP", "PU", "IN", "MS"])
        row_styles = (
            [
                rank_style(idx) if idx <= 6 else ""
                for idx in range(1, len(data_rows) + 1)
            ]
            if pretty
            else None
        )
        cell_formatters = (
            [None, _make_leader_cell_formatter(row_styles)]
            + [None] * (len(headers) - 2)
            if pretty
            else None
        )
        column_separators = {5, 9, 13, 17}
        group_headers = [
            (5, 9, "All"),
            (9, 13, "Individual"),
            (13, 17, "Relay"),
            (17, 20, "Races"),
        ]
        if include_wc_titles:
            column_separators.add(20)
            group_headers.append((20, 25, "World Cup Titles"))
        render_table(
            headers,
            data_rows,
            output_format=output_format,
            row_styles=row_styles,
            cell_formatters=cell_formatters,
            column_separators=column_separators,
            group_headers=group_headers,
        )

    print()
    return 0
