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
    assert podium["gold_nat"] == "NOR"
    assert podium["silver_nat"] == "SWE"
    assert podium["bronze_nat"] == "GER"


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


def test_fetch_relay_wc_standings_women_relay(monkeypatch):
    monkeypatch.setattr(
        startlist,
        "_get_cup_ids_for_race",
        lambda season_id, cat_id, disc: ("TS_CUP", "RL_CUP"),
    )
    monkeypatch.setattr(
        startlist,
        "_fetch_standings",
        lambda cup_id, limit=10: (
            [{"Rank": "1", "Name": "NORWAY", "Nat": "NOR", "Score": "210"}]
            if cup_id == "RL_CUP"
            else []
        ),
    )

    label, rows = startlist._fetch_relay_wc_standings("2526", "SW", "RL")

    assert label == "Women Relay"
    assert rows == [{"Rank": "1", "Name": "NORWAY", "Nat": "NOR", "Score": "210"}]


def test_fetch_relay_wc_standings_mixed_relay(monkeypatch):
    monkeypatch.setattr(startlist, "_find_mixed_relay_cup", lambda *_a, **_k: "MX_CUP")
    monkeypatch.setattr(
        startlist,
        "_fetch_standings",
        lambda cup_id, limit=10: (
            [{"Rank": "1", "Name": "NORWAY"}] if cup_id == "MX_CUP" else []
        ),
    )

    label, rows = startlist._fetch_relay_wc_standings("2526", "MX", "MR")

    assert label == "Mixed Relay"
    assert rows == [{"Rank": "1", "Name": "NORWAY"}]


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
    monkeypatch.setattr(startlist, "_get_wc_rows", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        startlist, "_get_cup_ids_for_race", lambda *_args, **_kwargs: ("", "")
    )
    monkeypatch.setattr(
        startlist, "_get_previous_individual_podiums", lambda *_a, **_k: []
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
        "race_id": "RACE1",
        "entries": [],
        "race_disc": "PU",
        "cat_id": "SW",
        "season_id": "2526",
        "event_type": startlist.EVENT_TYPE_OWG,
        "startlist_ids": {"S1"},
        "age_cache": {},
        "prefetched_results": {},
        "team_entries": [],
        "is_mixed": False,
        "is_snapshot": False,
        "snapshot_target_race_id": "",
        "snapshot_cutoff_dt": None,
        "snapshot_race_start_cache": {},
    }
    args = argparse.Namespace(format="tsv")

    startlist.render_startlist_analysis(ctx, args)
    out = capsys.readouterr().out

    assert "Athlete Olympic Games Medal Table - Pursuit (all editions)" in out
    assert "5\tTARGET One\tSWE\tF\t0\t1\t0\t1\t1" in out
    assert "\tBETA One\tGER\tF\t0\t2\t0\t2\t2" not in out

    assert "Athlete Olympic Games Medal Table - All Disciplines (all editions)" in out
    assert "5\tTARGET One\tSWE\tF\t1\t0\t0\t1\t1\t0\t0\t0\t0\t0\t0\t0\t0\t0\t0" in out
    assert "CHARLIE One\tFRA\tF" not in out


def test_render_startlist_analysis_relay_sections(monkeypatch, capsys):
    monkeypatch.setattr(
        startlist,
        "_fetch_relay_wc_standings",
        lambda *_a, **_k: (
            "Men Relay",
            [{"Rank": "1", "Name": "NORWAY", "Nat": "NOR", "Score": "220"}],
        ),
    )
    monkeypatch.setattr(startlist, "_fetch_nations_cup_standings", lambda *_a, **_k: [])
    monkeypatch.setattr(startlist, "_get_previous_relay_podiums", lambda *_a, **_k: [])

    ctx = {
        "payload": {
            "Results": [
                {
                    "IsTeam": False,
                    "Bib": "1",
                    "Nat": "NOR",
                    "Leg": 1,
                    "FamilyName": "A",
                    "Name": "A One",
                },
                {
                    "IsTeam": False,
                    "Bib": "1",
                    "Nat": "NOR",
                    "Leg": 2,
                    "FamilyName": "B",
                    "Name": "B Two",
                },
            ]
        },
        "race_id": "RACE1",
        "entries": [],
        "race_disc": "RL",
        "cat_id": "SM",
        "season_id": "2526",
        "event_type": startlist.EVENT_TYPE_WC,
        "startlist_ids": set(),
        "age_cache": {},
        "prefetched_results": {},
        "team_entries": [{"bib": "1", "name": "Norway", "nat": "NOR"}],
        "is_mixed": False,
        "is_snapshot": False,
        "snapshot_target_race_id": "",
        "snapshot_cutoff_dt": None,
        "snapshot_race_start_cache": {},
    }
    args = argparse.Namespace(format="tsv", leader_markers=False)

    startlist.render_startlist_analysis(ctx, args)
    out = capsys.readouterr().out

    assert "Participating Teams" in out
    assert "1\tNorway\tNOR\tA One\tB Two\t-\t-" in out
    assert "Relay WC Standings (Top 10)" in out
    assert "1\tNORWAY\tNOR\t220" in out


def test_render_startlist_analysis_skips_win_milestone_one(monkeypatch, capsys):
    monkeypatch.setattr(startlist, "_get_wc_rows", lambda *_a, **_k: [])
    monkeypatch.setattr(startlist, "_get_cup_ids_for_race", lambda *_a, **_k: ("", ""))
    monkeypatch.setattr(startlist, "_fetch_nations_cup_standings", lambda *_a, **_k: [])
    monkeypatch.setattr(
        startlist, "_get_previous_individual_podiums", lambda *_a, **_k: []
    )

    wc_non_win_results = [
        {"Level": "WC", "Comp": "SP", "Rank": "2", "SO": "2"} for _ in range(24)
    ]
    ctx = {
        "payload": {"Results": []},
        "race_id": "RACE1",
        "entries": [{"ibu_id": "A1", "name": "Alpha", "age": "25", "nat": "NOR"}],
        "race_disc": "SP",
        "cat_id": "SW",
        "season_id": "2526",
        "event_type": startlist.EVENT_TYPE_WC,
        "startlist_ids": {"A1"},
        "age_cache": {"A1": "25"},
        "prefetched_results": {"A1": {"Results": wc_non_win_results}},
        "team_entries": [],
        "is_mixed": False,
        "is_snapshot": False,
        "snapshot_target_race_id": "",
        "snapshot_cutoff_dt": None,
        "snapshot_race_start_cache": {},
    }
    args = argparse.Namespace(format="tsv", leader_markers=False)

    startlist.render_startlist_analysis(ctx, args)
    out = capsys.readouterr().out

    assert "Race milestones" in out
    assert "World Cup\tRace\tAlpha" in out
    assert "Win milestones: none" in out


def test_render_startlist_analysis_suppresses_duplicate_event_relay_milestone_one(
    monkeypatch, capsys
):
    monkeypatch.setattr(startlist, "_get_wc_rows", lambda *_a, **_k: [])
    monkeypatch.setattr(startlist, "_get_cup_ids_for_race", lambda *_a, **_k: ("", ""))
    monkeypatch.setattr(
        startlist, "_fetch_relay_wc_standings", lambda *_a, **_k: ("Relay", [])
    )
    monkeypatch.setattr(startlist, "_get_previous_relay_podiums", lambda *_a, **_k: [])
    monkeypatch.setattr(
        startlist, "_get_past_olympic_relay_podiums", lambda *_a, **_k: []
    )
    monkeypatch.setattr(
        startlist, "_get_all_olympic_medals", lambda *_a, **_k: ([], {})
    )

    ctx = {
        "payload": {"Results": []},
        "race_id": "RACE1",
        "entries": [{"ibu_id": "A1", "name": "Alpha", "age": "25", "nat": "NOR"}],
        "race_disc": "RL",
        "cat_id": "SM",
        "season_id": "2526",
        "event_type": startlist.EVENT_TYPE_OWG,
        "startlist_ids": {"A1"},
        "age_cache": {"A1": "25"},
        "prefetched_results": {
            "A1": {"Results": [{"Level": "WC", "Comp": "RL", "Rank": "5", "SO": "5"}]}
        },
        "team_entries": [],
        "is_mixed": False,
        "is_snapshot": False,
        "snapshot_target_race_id": "",
        "snapshot_cutoff_dt": None,
        "snapshot_race_start_cache": {},
    }
    args = argparse.Namespace(format="tsv", leader_markers=False)

    startlist.render_startlist_analysis(ctx, args)
    out = capsys.readouterr().out

    assert "Olympic Games\tRace\tAlpha" in out
    assert "Olympic Games\tMen Relay\tAlpha" not in out
    assert "Olympic Games\tTeam Race\tAlpha" not in out


def test_render_startlist_analysis_collapses_multiple_race_milestone_ones_per_athlete(
    monkeypatch, capsys
):
    monkeypatch.setattr(startlist, "_get_wc_rows", lambda *_a, **_k: [])
    monkeypatch.setattr(startlist, "_get_cup_ids_for_race", lambda *_a, **_k: ("", ""))
    monkeypatch.setattr(
        startlist, "_fetch_relay_wc_standings", lambda *_a, **_k: ("Relay", [])
    )
    monkeypatch.setattr(startlist, "_get_previous_relay_podiums", lambda *_a, **_k: [])
    monkeypatch.setattr(
        startlist, "_get_past_olympic_relay_podiums", lambda *_a, **_k: []
    )
    monkeypatch.setattr(
        startlist, "_get_all_olympic_medals", lambda *_a, **_k: ([], {})
    )

    # One prior individual major race keeps career "Race" off milestone=1.
    # Same-value rows are deduped by scope breadth within each subsection.
    ctx = {
        "payload": {"Results": []},
        "race_id": "RACE1",
        "entries": [{"ibu_id": "A1", "name": "Alpha", "age": "25", "nat": "NOR"}],
        "race_disc": "RL",
        "cat_id": "SM",
        "season_id": "2526",
        "event_type": startlist.EVENT_TYPE_OWG,
        "startlist_ids": {"A1"},
        "age_cache": {"A1": "25"},
        "prefetched_results": {
            "A1": {"Results": [{"Level": "WC", "Comp": "SP", "Rank": "5", "SO": "5"}]}
        },
        "team_entries": [],
        "is_mixed": False,
        "is_snapshot": False,
        "snapshot_target_race_id": "",
        "snapshot_cutoff_dt": None,
        "snapshot_race_start_cache": {},
    }
    args = argparse.Namespace(format="tsv", leader_markers=False)

    startlist.render_startlist_analysis(ctx, args)
    out = capsys.readouterr().out

    race_start = out.index("Race milestones")
    win_start = out.index("Win milestones")
    race_block = out[race_start:win_start]

    assert "1st\tOlympic Games\tRace\tAlpha\t25\tNOR" in race_block
    assert "1st\tOlympic Games\tMen Relay\tAlpha\t25\tNOR" not in race_block
    assert "1st\tOlympic Games\tTeam Race\tAlpha\t25\tNOR" not in race_block
    assert "1st\tWC+WCH+OWG\tMen Relay\tAlpha\t25\tNOR" not in race_block
    assert "1st\tWC+WCH+OWG\tTeam Race\tAlpha\t25\tNOR" in race_block
    assert race_block.count("\tAlpha\t25\tNOR") == 2


def test_render_startlist_analysis_relay_family_vs_discipline_counts(
    monkeypatch, capsys
):
    monkeypatch.setattr(startlist, "_get_wc_rows", lambda *_a, **_k: [])
    monkeypatch.setattr(startlist, "_get_cup_ids_for_race", lambda *_a, **_k: ("", ""))
    monkeypatch.setattr(
        startlist, "_fetch_relay_wc_standings", lambda *_a, **_k: ("Relay", [])
    )
    monkeypatch.setattr(startlist, "_fetch_nations_cup_standings", lambda *_a, **_k: [])
    monkeypatch.setattr(startlist, "_get_previous_relay_podiums", lambda *_a, **_k: [])

    rl_results = [
        {"Level": "WC", "Comp": "RL", "Rank": "5", "SO": "5"} for _ in range(49)
    ]
    mr_results = [
        {"Level": "WCH", "Comp": "MR", "Rank": "6", "SO": "6"} for _ in range(25)
    ]
    ctx = {
        "payload": {"Results": []},
        "race_id": "RACE1",
        "entries": [{"ibu_id": "A1", "name": "Alpha", "age": "25", "nat": "NOR"}],
        "race_disc": "RL",
        "cat_id": "SM",
        "season_id": "2526",
        "event_type": startlist.EVENT_TYPE_WC,
        "startlist_ids": {"A1"},
        "age_cache": {"A1": "25"},
        "prefetched_results": {
            "A1": {"Results": rl_results + mr_results},
        },
        "team_entries": [],
        "is_mixed": False,
        "is_snapshot": False,
        "snapshot_target_race_id": "",
        "snapshot_cutoff_dt": None,
        "snapshot_race_start_cache": {},
    }
    args = argparse.Namespace(format="tsv", leader_markers=False)

    startlist.render_startlist_analysis(ctx, args)
    out = capsys.readouterr().out

    assert "75th\tWC+WCH+OWG\tRace\tAlpha\t25\tNOR" in out
    assert "50th\tWC+WCH+OWG\tMen Relay\tAlpha\t25\tNOR" in out
    assert "ALL relay races" not in out


def test_render_startlist_analysis_groups_race_milestones_by_athlete(
    monkeypatch, capsys
):
    monkeypatch.setattr(startlist, "_get_wc_rows", lambda *_a, **_k: [])
    monkeypatch.setattr(startlist, "_get_cup_ids_for_race", lambda *_a, **_k: ("", ""))
    monkeypatch.setattr(startlist, "_fetch_nations_cup_standings", lambda *_a, **_k: [])
    monkeypatch.setattr(
        startlist, "_get_previous_individual_podiums", lambda *_a, **_k: []
    )

    alpha_results = [
        {"Level": "WC", "Comp": "SP", "Rank": "2", "SO": "2"} for _ in range(49)
    ] + [{"Level": "WC", "Comp": "PU", "Rank": "2", "SO": "2"} for _ in range(50)]
    beta_results = [
        {"Level": "WC", "Comp": "SP", "Rank": "2", "SO": "2"} for _ in range(74)
    ]
    ctx = {
        "payload": {"Results": []},
        "race_id": "RACE1",
        "entries": [
            {"ibu_id": "A1", "name": "Alpha", "age": "25", "nat": "NOR"},
            {"ibu_id": "B1", "name": "Beta", "age": "26", "nat": "SWE"},
        ],
        "race_disc": "SP",
        "cat_id": "SW",
        "season_id": "2526",
        "event_type": startlist.EVENT_TYPE_WC,
        "startlist_ids": {"A1", "B1"},
        "age_cache": {"A1": "25", "B1": "26"},
        "prefetched_results": {
            "A1": {"Results": alpha_results},
            "B1": {"Results": beta_results},
        },
        "team_entries": [],
        "is_mixed": False,
        "is_snapshot": False,
        "snapshot_target_race_id": "",
        "snapshot_cutoff_dt": None,
        "snapshot_race_start_cache": {},
    }
    args = argparse.Namespace(format="tsv", leader_markers=False)

    startlist.render_startlist_analysis(ctx, args)
    out = capsys.readouterr().out

    race_start = out.index("Race milestones")
    win_start = out.index("Win milestones")
    race_block = out[race_start:win_start]
    current_start = race_block.index("Current Event")
    career_start = race_block.index("## Career")
    current_block = race_block[current_start:career_start]
    career_block = race_block[career_start:]
    assert current_block.rfind("\tAlpha\t") < current_block.find("\tBeta\t")
    assert career_block.rfind("\tAlpha\t") < career_block.find("\tBeta\t")


def test_render_startlist_analysis_groups_win_milestones_by_athlete(
    monkeypatch, capsys
):
    monkeypatch.setattr(startlist, "_get_wc_rows", lambda *_a, **_k: [])
    monkeypatch.setattr(startlist, "_get_cup_ids_for_race", lambda *_a, **_k: ("", ""))
    monkeypatch.setattr(startlist, "_fetch_nations_cup_standings", lambda *_a, **_k: [])
    monkeypatch.setattr(
        startlist, "_get_previous_individual_podiums", lambda *_a, **_k: []
    )

    alpha_results = [
        {"Level": "WC", "Comp": "SP", "Rank": "1", "SO": "1"} for _ in range(4)
    ] + [{"Level": "WC", "Comp": "PU", "Rank": "1", "SO": "1"} for _ in range(15)]
    beta_results = [
        {"Level": "WC", "Comp": "SP", "Rank": "1", "SO": "1"} for _ in range(14)
    ]
    ctx = {
        "payload": {"Results": []},
        "race_id": "RACE1",
        "entries": [
            {"ibu_id": "A1", "name": "Alpha", "age": "25", "nat": "NOR"},
            {"ibu_id": "B1", "name": "Beta", "age": "26", "nat": "SWE"},
        ],
        "race_disc": "SP",
        "cat_id": "SW",
        "season_id": "2526",
        "event_type": startlist.EVENT_TYPE_WC,
        "startlist_ids": {"A1", "B1"},
        "age_cache": {"A1": "25", "B1": "26"},
        "prefetched_results": {
            "A1": {"Results": alpha_results},
            "B1": {"Results": beta_results},
        },
        "team_entries": [],
        "is_mixed": False,
        "is_snapshot": False,
        "snapshot_target_race_id": "",
        "snapshot_cutoff_dt": None,
        "snapshot_race_start_cache": {},
    }
    args = argparse.Namespace(format="tsv", leader_markers=False)

    startlist.render_startlist_analysis(ctx, args)
    out = capsys.readouterr().out

    win_start = out.index("Win milestones")
    next_start = out.index("Previous podiums", win_start)
    win_block = out[win_start:next_start]
    current_start = win_block.index("Current Event")
    career_start = win_block.index("## Career")
    current_block = win_block[current_start:career_start]
    career_block = win_block[career_start:]
    assert current_block.rfind("\tAlpha\t") < current_block.find("\tBeta\t")
    assert career_block.rfind("\tAlpha\t") < career_block.find("\tBeta\t")


def test_render_startlist_analysis_owg_win_current_event_starts_at_two(
    monkeypatch, capsys
):
    monkeypatch.setattr(startlist, "_get_wc_rows", lambda *_a, **_k: [])
    monkeypatch.setattr(startlist, "_get_cup_ids_for_race", lambda *_a, **_k: ("", ""))
    monkeypatch.setattr(
        startlist, "_fetch_relay_wc_standings", lambda *_a, **_k: ("Relay", [])
    )
    monkeypatch.setattr(startlist, "_get_previous_relay_podiums", lambda *_a, **_k: [])
    monkeypatch.setattr(
        startlist, "_get_past_olympic_relay_podiums", lambda *_a, **_k: []
    )
    monkeypatch.setattr(
        startlist, "_get_all_olympic_medals", lambda *_a, **_k: ([], {})
    )

    ctx = {
        "payload": {"Results": []},
        "race_id": "RACE1",
        "entries": [{"ibu_id": "A1", "name": "Alpha", "age": "25", "nat": "NOR"}],
        "race_disc": "RL",
        "cat_id": "SM",
        "season_id": "2526",
        "event_type": startlist.EVENT_TYPE_OWG,
        "startlist_ids": {"A1"},
        "age_cache": {"A1": "25"},
        "prefetched_results": {
            "A1": {"Results": [{"Level": "OWG", "Comp": "RL", "Rank": "1", "SO": "1"}]}
        },
        "team_entries": [],
        "is_mixed": False,
        "is_snapshot": False,
        "snapshot_target_race_id": "",
        "snapshot_cutoff_dt": None,
        "snapshot_race_start_cache": {},
    }
    args = argparse.Namespace(format="tsv", leader_markers=False)

    startlist.render_startlist_analysis(ctx, args)
    out = capsys.readouterr().out

    win_start = out.index("Win milestones")
    next_start = out.index("Previous podiums", win_start)
    win_block = out[win_start:next_start]
    assert "### Current Event" in win_block
    assert "Milestone\tEvent\tType\tAthlete\tAge\tNat" in win_block
    assert "CurrentWins" not in win_block
    assert "2nd\tOlympic Games\tRace\tAlpha\t25\tNOR" in win_block
