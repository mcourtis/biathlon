"""Tests for startlist pre-race snapshot helpers."""

import argparse
import datetime

from biathlon.api import BiathlonError
from biathlon.commands import startlist


def _dt(value: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_filter_results_before_cutoff_excludes_target_and_future(monkeypatch):
    cutoff = _dt("2026-01-10T10:00:00Z")
    start_by_race = {
        "RACE_PAST": "2026-01-02T10:00:00Z",
        "RACE_FUTURE": "2026-01-14T10:00:00Z",
    }

    def fake_get_race_results(race_id: str) -> dict:
        if race_id not in start_by_race:
            raise BiathlonError("missing")
        return {"Competition": {"StartTime": start_by_race[race_id]}}

    monkeypatch.setattr(startlist, "get_race_results", fake_get_race_results)

    rows = [
        {"RaceId": "RACE_TARGET"},
        {"RaceId": "RACE_PAST"},
        {"RaceId": "RACE_FUTURE"},
        {"RaceId": "RACE_UNKNOWN"},
    ]
    cache = {"RACE_TARGET": cutoff}

    filtered = startlist._filter_results_before_cutoff(
        rows,
        "RACE_TARGET",
        cutoff,
        cache,
    )

    assert [row["RaceId"] for row in filtered] == ["RACE_PAST"]


def test_compute_wc_pre_race_standings_uses_strict_cutoff(monkeypatch):
    races = [
        (_dt("2026-01-01T10:00:00Z"), "RACE_OLD", "SP"),
        (_dt("2026-01-10T10:00:00Z"), "RACE_TARGET", "SP"),
        (_dt("2026-01-20T10:00:00Z"), "RACE_NEW", "SP"),
    ]
    monkeypatch.setattr(
        startlist,
        "_collect_wc_individual_races",
        lambda season_id, cat_id: races,
    )

    payloads = {
        "RACE_OLD": {
            "Results": [
                {
                    "IsTeam": False,
                    "Rank": "1",
                    "IBUId": "A",
                    "Name": "Alpha",
                    "Nat": "NOR",
                },
                {
                    "IsTeam": False,
                    "Rank": "2",
                    "IBUId": "B",
                    "Name": "Beta",
                    "Nat": "SWE",
                },
            ]
        },
        "RACE_TARGET": {
            "Results": [
                {
                    "IsTeam": False,
                    "Rank": "1",
                    "IBUId": "B",
                    "Name": "Beta",
                    "Nat": "SWE",
                }
            ]
        },
        "RACE_NEW": {
            "Results": [
                {
                    "IsTeam": False,
                    "Rank": "1",
                    "IBUId": "C",
                    "Name": "Gamma",
                    "Nat": "FRA",
                }
            ]
        },
    }
    monkeypatch.setattr(
        startlist, "get_race_results", lambda race_id: payloads[race_id]
    )

    total_rows, disc_rows = startlist._compute_wc_pre_race_standings(
        "2526",
        "SW",
        "RACE_TARGET",
        "SP",
        _dt("2026-01-10T10:00:00Z"),
    )

    assert [row["IBUId"] for row in total_rows] == ["A", "B"]
    assert total_rows[0]["Score"] == 90
    assert total_rows[1]["Score"] == 75
    assert [row["IBUId"] for row in disc_rows] == ["A", "B"]


def test_fetch_olympic_individual_podium_uses_strict_cutoff(monkeypatch):
    cutoff = _dt("2026-02-15T12:00:00Z")
    monkeypatch.setattr(
        startlist,
        "get_races",
        lambda event_id: [
            {
                "RaceId": "RACE_BEFORE",
                "DisciplineId": "PU",
                "catId": "SW",
                "StartTime": "2026-02-14T12:00:00Z",
            },
            {
                "RaceId": "RACE_TARGET",
                "DisciplineId": "PU",
                "catId": "SW",
                "StartTime": "2026-02-15T12:00:00Z",
            },
        ],
    )

    payloads = {
        "RACE_BEFORE": {
            "IsResult": True,
            "SportEvt": {"Organizer": "Antholz"},
            "Results": [
                {"IsTeam": False, "Rank": "1", "Name": "Before Winner", "Nat": "NOR"},
                {"IsTeam": False, "Rank": "2", "Name": "Before Second", "Nat": "SWE"},
                {"IsTeam": False, "Rank": "3", "Name": "Before Third", "Nat": "GER"},
            ],
        },
        "RACE_TARGET": {
            "IsResult": True,
            "SportEvt": {"Organizer": "Antholz"},
            "Results": [
                {"IsTeam": False, "Rank": "1", "Name": "Target Winner", "Nat": "FRA"},
                {"IsTeam": False, "Rank": "2", "Name": "Target Second", "Nat": "ITA"},
                {"IsTeam": False, "Rank": "3", "Name": "Target Third", "Nat": "SUI"},
            ],
        },
    }
    monkeypatch.setattr(
        startlist, "get_race_results", lambda race_id: payloads[race_id]
    )

    podium = startlist._fetch_olympic_individual_podium("2526", "PU", "SW", cutoff)

    assert podium is not None
    assert podium["gold_nat"] == "NOR"
    assert "Target Winner" not in podium["gold"]


def test_fetch_olympic_individual_podium_can_include_cutoff(monkeypatch):
    cutoff = _dt("2026-02-15T12:00:00Z")
    monkeypatch.setattr(
        startlist,
        "get_races",
        lambda event_id: [
            {
                "RaceId": "RACE_BEFORE",
                "DisciplineId": "PU",
                "catId": "SW",
                "StartTime": "2026-02-14T12:00:00Z",
            },
            {
                "RaceId": "RACE_TARGET",
                "DisciplineId": "PU",
                "catId": "SW",
                "StartTime": "2026-02-15T12:00:00Z",
            },
        ],
    )
    payloads = {
        "RACE_BEFORE": {
            "IsResult": True,
            "SportEvt": {"Organizer": "Antholz"},
            "Results": [{"IsTeam": False, "Rank": "1", "Name": "Before", "Nat": "NOR"}],
        },
        "RACE_TARGET": {
            "IsResult": True,
            "SportEvt": {"Organizer": "Antholz"},
            "Results": [{"IsTeam": False, "Rank": "1", "Name": "Target", "Nat": "ITA"}],
        },
    }
    monkeypatch.setattr(
        startlist, "get_race_results", lambda race_id: payloads[race_id]
    )

    podium = startlist._fetch_olympic_individual_podium(
        "2526", "PU", "SW", cutoff, include_cutoff=True
    )

    assert podium is not None
    assert podium["gold_nat"] == "ITA"


def test_fetch_olympic_relay_podium_uses_strict_cutoff(monkeypatch):
    cutoff = _dt("2026-02-22T12:00:00Z")
    monkeypatch.setattr(
        startlist,
        "get_races",
        lambda event_id: [
            {
                "RaceId": "RACE_BEFORE",
                "DisciplineId": "RL",
                "catId": "SW",
                "StartTime": "2026-02-21T12:00:00Z",
            },
            {
                "RaceId": "RACE_TARGET",
                "DisciplineId": "RL",
                "catId": "SW",
                "StartTime": "2026-02-22T12:00:00Z",
            },
        ],
    )

    payloads = {
        "RACE_BEFORE": {
            "IsResult": True,
            "SportEvt": {"Organizer": "Antholz"},
            "Results": [
                {"IsTeam": True, "Rank": "1", "Name": "Norway", "Nat": "NOR"},
                {"IsTeam": True, "Rank": "2", "Name": "Sweden", "Nat": "SWE"},
                {"IsTeam": True, "Rank": "3", "Name": "Germany", "Nat": "GER"},
            ],
        },
        "RACE_TARGET": {
            "IsResult": True,
            "SportEvt": {"Organizer": "Antholz"},
            "Results": [
                {"IsTeam": True, "Rank": "1", "Name": "France", "Nat": "FRA"},
                {"IsTeam": True, "Rank": "2", "Name": "Italy", "Nat": "ITA"},
                {"IsTeam": True, "Rank": "3", "Name": "Switzerland", "Nat": "SUI"},
            ],
        },
    }
    monkeypatch.setattr(
        startlist, "get_race_results", lambda race_id: payloads[race_id]
    )

    podium = startlist._fetch_olympic_podium("2526", "RL", "SW", cutoff)

    assert podium is not None
    assert podium["gold"] == "Norway (NOR)"


def test_fetch_olympic_season_medals_uses_strict_cutoff(monkeypatch):
    cutoff = _dt("2026-02-15T12:00:00Z")
    monkeypatch.setattr(
        startlist,
        "get_races",
        lambda event_id: [
            {
                "RaceId": "RACE_BEFORE",
                "DisciplineId": "PU",
                "catId": "SW",
                "StartTime": "2026-02-14T12:00:00Z",
            },
            {
                "RaceId": "RACE_TARGET",
                "DisciplineId": "PU",
                "catId": "SW",
                "StartTime": "2026-02-15T12:00:00Z",
            },
        ],
    )

    payloads = {
        "RACE_BEFORE": {
            "IsResult": True,
            "Results": [
                {
                    "IsTeam": False,
                    "Rank": "1",
                    "IBUId": "A",
                    "Name": "Alpha",
                    "Nat": "NOR",
                },
                {
                    "IsTeam": False,
                    "Rank": "2",
                    "IBUId": "B",
                    "Name": "Beta",
                    "Nat": "SWE",
                },
                {
                    "IsTeam": False,
                    "Rank": "3",
                    "IBUId": "C",
                    "Name": "Gamma",
                    "Nat": "GER",
                },
            ],
        },
        "RACE_TARGET": {
            "IsResult": True,
            "Results": [
                {
                    "IsTeam": False,
                    "Rank": "1",
                    "IBUId": "D",
                    "Name": "Delta",
                    "Nat": "FRA",
                },
                {
                    "IsTeam": False,
                    "Rank": "2",
                    "IBUId": "E",
                    "Name": "Epsilon",
                    "Nat": "ITA",
                },
                {
                    "IsTeam": False,
                    "Rank": "3",
                    "IBUId": "F",
                    "Name": "Zeta",
                    "Nat": "SUI",
                },
            ],
        },
    }
    monkeypatch.setattr(
        startlist, "get_race_results", lambda race_id: payloads[race_id]
    )

    country_medals, athlete_stats = startlist._fetch_olympic_season_medals(
        "2526", "SW", cutoff
    )

    assert len(country_medals) == 1
    assert country_medals[0]["gold"] == "NOR"
    assert "A" in athlete_stats
    assert "D" not in athlete_stats


def test_fetch_olympic_season_medals_can_include_cutoff(monkeypatch):
    cutoff = _dt("2026-02-15T12:00:00Z")
    monkeypatch.setattr(
        startlist,
        "get_races",
        lambda event_id: [
            {
                "RaceId": "RACE_BEFORE",
                "DisciplineId": "PU",
                "catId": "SW",
                "StartTime": "2026-02-14T12:00:00Z",
            },
            {
                "RaceId": "RACE_TARGET",
                "DisciplineId": "PU",
                "catId": "SW",
                "StartTime": "2026-02-15T12:00:00Z",
            },
        ],
    )
    payloads = {
        "RACE_BEFORE": {
            "IsResult": True,
            "Results": [{"IsTeam": False, "Rank": "1", "IBUId": "A", "Nat": "NOR"}],
        },
        "RACE_TARGET": {
            "IsResult": True,
            "Results": [{"IsTeam": False, "Rank": "1", "IBUId": "D", "Nat": "ITA"}],
        },
    }
    monkeypatch.setattr(
        startlist, "get_race_results", lambda race_id: payloads[race_id]
    )

    country_medals, athlete_stats = startlist._fetch_olympic_season_medals(
        "2526",
        "SW",
        cutoff,
        include_cutoff=True,
    )

    assert len(country_medals) == 2
    assert country_medals[-1]["gold"] == "ITA"
    assert "D" in athlete_stats


def test_find_all_startlist_races_keeps_startlist_only(monkeypatch):
    monkeypatch.setattr(startlist, "get_current_season_id", lambda: "2526")
    monkeypatch.setattr(
        startlist,
        "get_events",
        lambda season_id, level: [
            {"EventId": "E1", "EndDate": "2999-01-01"},
        ],
    )
    monkeypatch.setattr(
        startlist,
        "get_races",
        lambda event_id: [
            {"RaceId": "RACE_A"},
            {"RaceId": "RACE_B"},
        ],
    )

    def fake_get_race_results(race_id: str) -> dict:
        if race_id == "RACE_A":
            return {
                "IsStartList": False,
                "Competition": {"StartTime": "2026-01-10T10:00:00Z"},
            }
        return {
            "IsStartList": True,
            "Competition": {"StartTime": "2026-01-11T10:00:00Z"},
        }

    monkeypatch.setattr(startlist, "get_race_results", fake_get_race_results)

    races = startlist._find_all_startlist_races()

    assert [race_id for race_id, _payload in races] == ["RACE_B"]


def test_filter_results_before_cutoff_uses_season_fast_path(monkeypatch):
    cutoff = _dt("2026-01-10T10:00:00Z")
    calls: list[str] = []

    def fake_get_race_results(race_id: str) -> dict:
        calls.append(race_id)
        if race_id == "BT2526SAME":
            return {"Competition": {"StartTime": "2026-01-02T10:00:00Z"}}
        raise BiathlonError("unexpected lookup")

    monkeypatch.setattr(startlist, "get_race_results", fake_get_race_results)

    rows = [
        {"RaceId": "BT2425OLD"},
        {"RaceId": "BT2627FUT"},
        {"RaceId": "BT2526SAME"},
    ]
    cache = {"BT2526TARGET": cutoff}

    filtered = startlist._filter_results_before_cutoff(
        rows,
        "BT2526TARGET",
        cutoff,
        cache,
    )

    assert [row["RaceId"] for row in filtered] == ["BT2425OLD", "BT2526SAME"]
    assert calls == ["BT2526SAME"]


def test_render_wc_section1_skips_relays(capsys):
    ctx = {
        "race_disc": "RL",
        "cat_id": "SW",
        "season_id": "2526",
        "startlist_ids": {"A"},
        "age_cache": {},
        "is_mixed": False,
    }
    args = argparse.Namespace(format="tsv", leader_markers=False)

    startlist._render_wc_standings_sections(
        ctx,
        args,
        [],
        [],
        None,
        "Relay",
        lambda name, _ibu_id: name,
        lambda cell, _row_idx: cell,
    )

    assert capsys.readouterr().out == ""


def test_render_wc_section1_first_race_snapshot_prints_none(monkeypatch, capsys):
    ctx = {
        "race_disc": "SP",
        "cat_id": "SW",
        "season_id": "2526",
        "startlist_ids": {"A"},
        "age_cache": {},
        "is_mixed": False,
    }
    args = argparse.Namespace(format="tsv", leader_markers=False)

    monkeypatch.setattr(startlist, "_render_standings_section", lambda *a, **k: None)

    startlist._render_wc_standings_sections(
        ctx,
        args,
        [],
        [],
        [],
        "Sprint",
        lambda name, _ibu_id: name,
        lambda cell, _row_idx: cell,
    )

    assert "1. Missing from top 25 World Cup standings: none" in capsys.readouterr().out


def test_olympic_athlete_tables_keep_global_ranks_for_startlist_athletes(
    monkeypatch, capsys
):
    podiums = [
        {
            "year": "2018",
            "venue": "Pyeongchang",
            "gold": "ALPHA One (NOR)",
            "silver": "BETA One (GER)",
            "bronze": "DELTA One (FRA)",
            "gold_nat": "NOR",
            "silver_nat": "GER",
            "bronze_nat": "FRA",
            "gold_athletes": [
                {"full_name": "ALPHA One", "name": "ALPHA", "nat": "NOR", "gender": "F"}
            ],
            "silver_athletes": [
                {"full_name": "BETA One", "name": "BETA", "nat": "GER", "gender": "F"}
            ],
            "bronze_athletes": [
                {"full_name": "DELTA One", "name": "DELTA", "nat": "FRA", "gender": "F"}
            ],
        },
        {
            "year": "2022",
            "venue": "Beijing",
            "gold": "GAMMA One (FRA)",
            "silver": "BETA One (GER)",
            "bronze": "EPSILON One (ITA)",
            "gold_nat": "FRA",
            "silver_nat": "GER",
            "bronze_nat": "ITA",
            "gold_athletes": [
                {"full_name": "GAMMA One", "name": "GAMMA", "nat": "FRA", "gender": "F"}
            ],
            "silver_athletes": [
                {"full_name": "BETA One", "name": "BETA", "nat": "GER", "gender": "F"}
            ],
            "bronze_athletes": [
                {
                    "full_name": "EPSILON One",
                    "name": "EPSILON",
                    "nat": "ITA",
                    "gender": "F",
                }
            ],
        },
        {
            "year": "2026",
            "venue": "Antholz",
            "gold": "OMEGA One (CZE)",
            "silver": "TARGET One (SWE)",
            "bronze": "ZETA One (USA)",
            "gold_nat": "CZE",
            "silver_nat": "SWE",
            "bronze_nat": "USA",
            "gold_athletes": [
                {"full_name": "OMEGA One", "name": "OMEGA", "nat": "CZE", "gender": "F"}
            ],
            "silver_athletes": [
                {
                    "full_name": "TARGET One",
                    "name": "TARGET",
                    "nat": "SWE",
                    "gender": "F",
                }
            ],
            "bronze_athletes": [
                {"full_name": "ZETA One", "name": "ZETA", "nat": "USA", "gender": "F"}
            ],
        },
    ]

    all_athlete_stats = {
        "A": {
            "name": "ALPHA One",
            "nat": "NOR",
            "gender": "F",
            "gold": 3,
            "silver": 0,
            "bronze": 0,
            "races": 3,
        },
        "B": {
            "name": "BETA One",
            "nat": "GER",
            "gender": "F",
            "gold": 2,
            "silver": 0,
            "bronze": 0,
            "races": 2,
        },
        "C": {
            "name": "CHARLIE One",
            "nat": "FRA",
            "gender": "F",
            "gold": 1,
            "silver": 2,
            "bronze": 0,
            "races": 3,
        },
        "D": {
            "name": "DELTA One",
            "nat": "ITA",
            "gender": "F",
            "gold": 1,
            "silver": 1,
            "bronze": 0,
            "races": 2,
        },
        "S1": {
            "name": "TARGET One",
            "nat": "SWE",
            "gender": "F",
            "gold": 1,
            "silver": 0,
            "bronze": 0,
            "races": 1,
        },
    }

    monkeypatch.setattr(
        startlist,
        "_get_past_olympic_individual_podiums",
        lambda *_args, **_kwargs: podiums,
    )
    monkeypatch.setattr(
        startlist,
        "_get_all_olympic_medals",
        lambda *_args, **_kwargs: ([], all_athlete_stats),
    )

    ctx = {
        "payload": {
            "Results": [
                {
                    "IsTeam": False,
                    "IBUId": "S1",
                    "Name": "TARGET One",
                    "FamilyName": "TARGET",
                    "Nat": "SWE",
                }
            ]
        },
        "race_disc": "PU",
        "cat_id": "SW",
        "startlist_ids": {"S1"},
        "is_snapshot": False,
    }
    args = argparse.Namespace(format="tsv")

    startlist._render_olympic_individual_sections(ctx, args, section_offset=3)
    out = capsys.readouterr().out

    assert "7. Athlete medal table (Women Pursuit):" in out
    assert "5\tTARGET One\tSWE\tF\t0\t1\t0\t1\t1" in out

    assert "8. Athlete medal table (Women, all Olympic disciplines):" in out
    assert "5\tTARGET One\tSWE\tF\t1\t0\t0\t1\t1\t0\t0\t0\t0\t0\t0\t0\t0\t0\t0" in out
