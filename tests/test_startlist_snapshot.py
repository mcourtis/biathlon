"""Tests for startlist pre-race snapshot helpers."""

import argparse
import datetime

from biathlon.api import BiathlonError
from biathlon.commands import startlist


def _dt(value: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_olympic_season_ids_keep_1992_gap_and_4y_cycles_before() -> None:
    ids = startlist.OLYMPIC_SEASON_IDS
    idx_9394 = ids.index("9394")
    assert ids[idx_9394 + 1] == "9192"
    assert ids[idx_9394 + 2] == "8788"
    assert ids[idx_9394 + 3] == "8384"

    years = [int(startlist._season_to_olympic_year(season_id)) for season_id in ids]
    diffs = [years[i] - years[i + 1] for i in range(len(years) - 1)]
    assert diffs.count(2) == 1
    assert diffs[ids.index("9394")] == 2
    assert set(diffs) <= {2, 4}


def test_season_to_olympic_year_handles_1900s_and_2000s() -> None:
    assert startlist._season_to_olympic_year("2526") == "2026"
    assert startlist._season_to_olympic_year("9192") == "1992"
    assert startlist._season_to_olympic_year("8384") == "1984"
    assert startlist._season_to_olympic_year("9900") == "2000"


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


def test_collect_wc_individual_races_applies_major_event_counting_rules(monkeypatch):
    monkeypatch.setattr(
        startlist,
        "get_events",
        lambda season_id, level=1: [
            {"EventId": "E_WC", "Description": "BMW IBU World Cup"},
            {"EventId": "E_WCH", "Description": "BMW IBU World Championships"},
            {"EventId": "E_OWG", "Description": "Olympic Winter Games"},
        ],
    )
    monkeypatch.setattr(
        startlist,
        "get_races",
        lambda event_id: {
            "E_WC": [
                {
                    "RaceId": "R_WC_SP",
                    "DisciplineId": "SP",
                    "catId": "SW",
                    "StartTime": "1998-01-01T10:00:00Z",
                }
            ],
            "E_WCH": [
                {
                    "RaceId": "R_WCH_PU",
                    "DisciplineId": "PU",
                    "catId": "SW",
                    "StartTime": "1998-01-02T10:00:00Z",
                },
                {
                    "RaceId": "R_WCH_SP",
                    "DisciplineId": "SP",
                    "catId": "SW",
                    "StartTime": "1998-01-03T10:00:00Z",
                },
            ],
            "E_OWG": [
                {
                    "RaceId": "R_OWG_SP",
                    "DisciplineId": "SP",
                    "catId": "SW",
                    "StartTime": "1998-01-04T10:00:00Z",
                }
            ],
        }[event_id],
    )

    rows = startlist._collect_wc_individual_races("9798", "SW")

    assert [race_id for _start_dt, race_id, _disc in rows] == [
        "R_WC_SP",
        "R_WCH_PU",
        "R_OWG_SP",
    ]


def test_compute_nations_pre_race_standings_excludes_non_counting_major_events(
    monkeypatch,
):
    monkeypatch.setattr(
        startlist,
        "get_events",
        lambda season_id, level=1: [
            {"EventId": "E_WC", "Description": "BMW IBU World Cup"},
            {"EventId": "E_OWG", "Description": "Olympic Winter Games"},
            {"EventId": "E_WCH", "Description": "BMW IBU World Championships"},
        ],
    )
    monkeypatch.setattr(
        startlist,
        "get_races",
        lambda event_id: [
            {
                "RaceId": f"{event_id}_R1",
                "DisciplineId": "SP",
                "catId": "SW",
                "StartTime": "2026-01-01T10:00:00Z",
            }
        ],
    )
    monkeypatch.setattr(
        startlist,
        "get_race_results",
        lambda race_id: {
            "Results": [
                {
                    "IsTeam": False,
                    "Rank": "1",
                    "SO": "1",
                    "Nat": "NOR",
                }
            ]
        },
    )

    rows = startlist._compute_nations_pre_race_standings(
        "2526",
        "TARGET",
        _dt("2026-01-10T10:00:00Z"),
        "SW",
        limit=10,
    )

    assert rows[0]["Nat"] == "NOR"
    assert rows[0]["Score"] == "320"


def test_compute_country_what_if_scenarios_uses_half_relay_nations_cup_points():
    racing_countries = {"NOR", "FRA"} | {f"T{i:02d}" for i in range(1, 29)}

    scenarios = startlist._compute_country_what_if_scenarios(
        [
            {"Rank": "1", "Name": "Norway", "Nat": "NOR", "Score": "1000"},
            {"Rank": "2", "Name": "France", "Nat": "FRA", "Score": "813"},
        ],
        racing_countries,
        "Nations Cup Men",
        points_for_position=lambda pos: startlist._get_nc_points(
            pos,
            is_relay=True,
            mixed=True,
        ),
        units_by_country={nat: 1 for nat in racing_countries},
        total_units=len(racing_countries),
    )

    assert scenarios == [
        "[Nations Cup Men] France can overtake with a win if Norway finishes 28th or worse"
    ]


def test_compute_nations_pre_race_standings_counts_relay_results(monkeypatch):
    monkeypatch.setattr(
        startlist,
        "get_events",
        lambda season_id, level=1: [
            {"EventId": "E_WC", "Description": "BMW IBU World Cup"},
        ],
    )
    monkeypatch.setattr(
        startlist,
        "get_races",
        lambda event_id: [
            {
                "RaceId": "E_WC_RL",
                "DisciplineId": "RL",
                "catId": "SW",
                "StartTime": "2026-01-01T10:00:00Z",
            }
        ],
    )
    monkeypatch.setattr(
        startlist,
        "get_race_results",
        lambda race_id: {
            "Results": [
                {
                    "IsTeam": True,
                    "Rank": "1",
                    "SO": "1",
                    "Nat": "NOR",
                }
            ]
        },
    )

    rows = startlist._compute_nations_pre_race_standings(
        "2526",
        "TARGET",
        _dt("2026-01-10T10:00:00Z"),
        "SW",
        limit=10,
    )

    assert rows[0]["Nat"] == "NOR"
    assert rows[0]["Score"] == "420"


def test_compute_country_what_if_scenarios_uses_country_entry_counts_for_individual_nc():
    scenarios = startlist._compute_country_what_if_scenarios(
        [
            {"Rank": "1", "Name": "France", "Nat": "FRA", "Score": "1000"},
            {"Rank": "2", "Name": "Sweden", "Nat": "SWE", "Score": "930"},
        ],
        {"FRA", "SWE", "NOR"},
        "Nations Cup Women",
        points_for_position=lambda pos: startlist._get_nc_points(pos),
        units_by_country={"FRA": 2, "SWE": 2, "NOR": 2},
        total_units=6,
    )

    assert scenarios == [
        "[Nations Cup Women] Sweden trails France by 70 pts (best case still 34 pts behind)"
    ]


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
    monkeypatch.setattr(
        startlist, "_find_mixed_relay_cups", lambda *_a, **_k: [("MR", "MX_CUP")]
    )
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


def test_fetch_relay_wc_standings_single_mixed_relay_falls_back_to_mixed(monkeypatch):
    monkeypatch.setattr(
        startlist,
        "_find_mixed_relay_cups",
        lambda *_a, **_k: [("SR", "MX_SR"), ("MR", "MX_MR")],
    )
    monkeypatch.setattr(
        startlist,
        "_fetch_standings",
        lambda cup_id, limit=10: (
            []
            if cup_id == "MX_SR"
            else [{"Rank": "1", "Name": "FRANCE", "Nat": "FRA", "Score": "160"}]
        ),
    )

    label, rows = startlist._fetch_relay_wc_standings("2526", "MX", "SR")

    assert label == "Mixed Relay"
    assert rows == [{"Rank": "1", "Name": "FRANCE", "Nat": "FRA", "Score": "160"}]


def test_fetch_relay_wc_standings_single_mixed_relay_keeps_mixed_label_with_sr_cup(
    monkeypatch,
):
    monkeypatch.setattr(
        startlist,
        "_find_mixed_relay_cups",
        lambda *_a, **_k: [("SR", "MX_SR"), ("MR", "MX_MR")],
    )
    monkeypatch.setattr(
        startlist,
        "_fetch_standings",
        lambda cup_id, limit=10: (
            [{"Rank": "1", "Name": "NORWAY", "Nat": "NOR", "Score": "180"}]
            if cup_id == "MX_SR"
            else []
        ),
    )

    label, rows = startlist._fetch_relay_wc_standings("2526", "MX", "SR")

    assert label == "Mixed Relay"
    assert rows == [{"Rank": "1", "Name": "NORWAY", "Nat": "NOR", "Score": "180"}]


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


def test_select_u23_standings_rows_uses_groups_and_birth_year_fallback():
    rows = [
        {
            "Rank": "4",
            "Name": "Grouped U23",
            "Nat": "FRA",
            "IBUId": "BTFRA00001199900",
            "Groups": "U23",
        },
        {
            "Rank": "12",
            "Name": "Birth Year U23",
            "Nat": "USA",
            "IBUId": "BTUSA00002200400",
        },
        {
            "Rank": "18",
            "Name": "Senior Athlete",
            "Nat": "NOR",
            "IBUId": "BTNOR00003199900",
        },
    ]

    selected = startlist._select_u23_standings_rows(rows, "2526")

    assert [row["Name"] for row in selected] == ["Grouped U23", "Birth Year U23"]


def test_render_standings_section_merges_u23_rows_with_separator(monkeypatch):
    captured: dict[str, object] = {}

    def fake_render_table(headers, rows, **kwargs):
        captured["headers"] = headers
        captured["rows"] = rows
        captured["kwargs"] = kwargs

    monkeypatch.setattr(startlist, "render_table", fake_render_table)
    monkeypatch.setattr(
        startlist, "_print_spaced_section_title", lambda *_args, **_kwargs: None
    )

    startlist._render_standings_section(
        "WC Total Standings",
        [
            {
                "Rank": "1",
                "Name": "Leader",
                "Nat": "NOR",
                "Score": "500",
                "IBUId": "BTNOR00001199000",
            },
            {
                "Rank": "2",
                "Name": "Chaser",
                "Nat": "FRA",
                "Score": "430",
                "IBUId": "BTFRA00002199100",
            },
        ],
        argparse.Namespace(format="tsv", leader_markers=False),
        {"BTNOR00001199000"},
        u23_standings=[
            {
                "Rank": "12",
                "Name": "Youngster",
                "Nat": "USA",
                "Score": "95",
                "IBUId": "BTUSA00003200400",
            }
        ],
    )

    assert captured["headers"] == ["Rank", "WC", "Athlete", "Nat", "Points"]
    assert captured["rows"] == [
        ["1", "1", "Leader", "NOR", "500"],
        ["2", "2", "Chaser", "FRA", "430 (-70)"],
        ["1", "12", "Youngster", "USA", "95"],
    ]
    assert captured["kwargs"]["row_separators"] == {2}
    assert captured["kwargs"]["column_separators"] == {2, 4}


def test_render_standings_section_places_u23_table_on_the_right_in_pretty_mode(
    monkeypatch, capsys
):
    monkeypatch.setattr(
        startlist, "_print_spaced_section_title", lambda *_args, **_kwargs: None
    )

    def fake_render_table(headers, _rows, **_kwargs):
        if headers == ["Rank", "Athlete", "Nat", "Points"]:
            print("LEFT-HEADER")
            print("LEFT-ROW")
        else:
            print("RIGHT-HEADER")
            print("RIGHT-ROW")

    monkeypatch.setattr(startlist, "render_table", fake_render_table)

    startlist._render_standings_section(
        "WC Total Standings",
        [
            {
                "Rank": "1",
                "Name": "Leader",
                "Nat": "NOR",
                "Score": "500",
                "IBUId": "BTNOR00001199000",
            }
        ],
        argparse.Namespace(format="pretty", leader_markers=False),
        set(),
        u23_standings=[
            {
                "Rank": "12",
                "Name": "Youngster",
                "Nat": "USA",
                "Score": "95",
                "IBUId": "BTUSA00003200400",
            }
        ],
    )

    out = capsys.readouterr().out

    assert "LEFT-HEADER  │  RIGHT-HEADER" in out
    assert "LEFT-ROW     │  RIGHT-ROW" in out


def test_standings_points_cell_formatter_dims_gap_only_in_pretty_output(monkeypatch):
    monkeypatch.setattr(
        startlist.Color,
        "enabled",
        classmethod(lambda cls: True),
    )

    formatter = startlist._standings_points_cell_formatter(
        set(),
        point_cells=["430 (-70)", "309"],
        pretty=True,
    )
    formatted = formatter("430 (-70)", 1)

    assert formatted.startswith("430")
    assert "\x1b[2m" in formatted
    assert "\x1b[38;2;176;110;110m (-70)\x1b[0m" in formatted


def test_standings_points_cell_formatter_keeps_leader_points_plain(monkeypatch):
    monkeypatch.setattr(
        startlist.Color,
        "enabled",
        classmethod(lambda cls: True),
    )

    formatter = startlist._standings_points_cell_formatter(
        {0, 1},
        leader_rows={0},
        point_cells=["309", "274 (-35)"],
        pretty=True,
    )

    leader = formatter("309", 0)
    chaser = formatter("274 (-35)", 1)

    assert leader.startswith("309")
    assert "\x1b[" not in leader
    assert chaser.startswith("274")
    assert "\x1b[38;2;176;110;110m (-35)\x1b[0m" in chaser
    assert startlist._display_width(leader) == startlist._display_width(chaser)


def test_render_startlist_analysis_merges_u23_wc_rows_from_full_standings(
    monkeypatch, capsys
):
    monkeypatch.setattr(
        startlist, "_get_cup_ids_for_race", lambda *_a, **_k: ("TOTAL", "DISC")
    )
    monkeypatch.setattr(startlist, "_get_wc_rows", lambda *_a, **_k: [])
    monkeypatch.setattr(startlist, "_fetch_nations_cup_standings", lambda *_a, **_k: [])
    monkeypatch.setattr(
        startlist, "_get_previous_individual_podiums", lambda *_a, **_k: []
    )
    monkeypatch.setattr(startlist, "_compute_what_if_scenarios", lambda *_a, **_k: [])

    total_rows = [
        {
            "Rank": str(idx),
            "Name": f"Total {idx}",
            "Nat": "NOR",
            "Score": str(600 - idx),
            "IBUId": f"BTNOR{idx:05d}199000",
        }
        for idx in range(1, 12)
    ]
    total_rows.append(
        {
            "Rank": "12",
            "Name": "Total U23",
            "Nat": "USA",
            "Score": "321",
            "IBUId": "BTUSA99999200400",
        }
    )

    disc_rows = [
        {
            "Rank": str(idx),
            "Name": f"Disc {idx}",
            "Nat": "FRA",
            "Score": str(400 - idx),
            "IBUId": f"BTFRA{idx:05d}199100",
        }
        for idx in range(1, 11)
    ]
    disc_rows.append(
        {
            "Rank": "11",
            "Name": "Disc U23",
            "Nat": "SWE",
            "Score": "210",
            "IBUId": "BTSWE99999200400",
        }
    )

    monkeypatch.setattr(
        startlist,
        "_fetch_standings",
        lambda cup_id, limit=10: total_rows if cup_id == "TOTAL" else disc_rows,
    )

    ctx = {
        "payload": {
            "Results": [
                {
                    "IsTeam": False,
                    "IBUId": "STARTER1",
                    "Name": "Starter One",
                    "FamilyName": "Starter",
                    "Nat": "NOR",
                }
            ]
        },
        "race_id": "RACE1",
        "entries": [],
        "race_disc": "SP",
        "cat_id": "SW",
        "season_id": "2526",
        "event_type": startlist.EVENT_TYPE_WC,
        "startlist_ids": {"STARTER1"},
        "age_cache": {},
        "prefetched_results": {},
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

    assert "2\t2\tTotal 2\tNOR\t598 (-1)" in out
    assert "1\t12\tTotal U23\tUSA\t321" in out
    assert "1\t11\tDisc U23\tSWE\t210" in out


def test_render_startlist_analysis_marks_u23_leader_in_merged_wc_rows(
    monkeypatch, capsys
):
    monkeypatch.setattr(
        startlist, "_get_cup_ids_for_race", lambda *_a, **_k: ("TOTAL", "DISC")
    )
    monkeypatch.setattr(startlist, "_get_wc_rows", lambda *_a, **_k: [])
    monkeypatch.setattr(startlist, "_fetch_nations_cup_standings", lambda *_a, **_k: [])
    monkeypatch.setattr(
        startlist, "_get_previous_individual_podiums", lambda *_a, **_k: []
    )
    monkeypatch.setattr(startlist, "_compute_what_if_scenarios", lambda *_a, **_k: [])

    monkeypatch.setattr(startlist, "is_pretty_output", lambda _args: True)

    total_rows = [
        {
            "Rank": "1",
            "Name": "Total Leader",
            "Nat": "NOR",
            "Score": "600",
            "IBUId": "BTNOR00001199000",
        },
        {
            "Rank": "12",
            "Name": "Total U23",
            "Nat": "USA",
            "Score": "321",
            "IBUId": "BTUSA99999200400",
        },
    ]
    disc_rows = [
        {
            "Rank": "1",
            "Name": "Disc Leader",
            "Nat": "FRA",
            "Score": "400",
            "IBUId": "BTFRA00001199100",
        },
        {
            "Rank": "11",
            "Name": "Disc U23",
            "Nat": "SWE",
            "Score": "210",
            "IBUId": "BTSWE99999200400",
        },
    ]

    monkeypatch.setattr(
        startlist,
        "_fetch_standings",
        lambda cup_id, limit=10: total_rows if cup_id == "TOTAL" else disc_rows,
    )

    ctx = {
        "payload": {"Results": []},
        "race_id": "RACE1",
        "entries": [],
        "race_disc": "SP",
        "cat_id": "SW",
        "season_id": "2526",
        "event_type": startlist.EVENT_TYPE_WC,
        "startlist_ids": set(),
        "age_cache": {},
        "prefetched_results": {},
        "team_entries": [],
        "is_mixed": False,
        "is_snapshot": False,
        "snapshot_target_race_id": "",
        "snapshot_cutoff_dt": None,
        "snapshot_race_start_cache": {},
    }
    args = argparse.Namespace(format="pretty", leader_markers=True)

    startlist.render_startlist_analysis(ctx, args)
    out = capsys.readouterr().out

    assert "Total U23 ●" in out
    assert "Disc U23 ●" in out


def test_render_startlist_analysis_nations_cup_shows_behind_points(monkeypatch, capsys):
    monkeypatch.setattr(startlist, "_get_cup_ids_for_race", lambda *_a, **_k: ("", ""))
    monkeypatch.setattr(
        startlist,
        "_fetch_nations_cup_standings",
        lambda *_a, **_k: [
            {"Rank": "1", "Name": "Norway", "Nat": "NOR", "Score": "6232"},
            {"Rank": "2", "Name": "France", "Nat": "FRA", "Score": "6045"},
            {"Rank": "3", "Name": "Sweden", "Nat": "SWE", "Score": "5539"},
        ],
    )
    monkeypatch.setattr(
        startlist, "_get_previous_individual_podiums", lambda *_a, **_k: []
    )
    monkeypatch.setattr(startlist, "_compute_what_if_scenarios", lambda *_a, **_k: [])

    ctx = {
        "payload": {"Results": []},
        "race_id": "RACE1",
        "entries": [],
        "race_disc": "SP",
        "cat_id": "SW",
        "season_id": "2526",
        "event_type": startlist.EVENT_TYPE_WC,
        "startlist_ids": set(),
        "age_cache": {},
        "prefetched_results": {},
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

    assert "Rank\tCountry\tPoints" in out
    assert "1\tNorway\t6232" in out
    assert "2\tFrance\t6045 (-187)" in out
    assert "3\tSweden\t5539 (-693)" in out


def test_render_individual_podium_table_uses_short_names_and_centered_headers(
    monkeypatch,
):
    captured: dict[str, object] = {}

    def fake_render_table(headers, rows, **kwargs):
        captured["headers"] = headers
        captured["rows"] = rows
        captured["kwargs"] = kwargs

    monkeypatch.setattr(startlist, "render_table", fake_render_table)
    monkeypatch.setattr(
        startlist, "_print_spaced_section_title", lambda *_args, **_kwargs: None
    )

    startlist._render_individual_podium_table(
        startlist.SECTION_PREVIOUS_PODIUMS,
        [
            {
                "date": "2026-02-14",
                "race_type": "Olympic Games",
                "venue": "Antholz-Anterselva",
                "gold_athletes": [
                    {
                        "full_name": "Lou Jeanmonnot",
                        "name": "JEANMONNOT",
                        "nat": "FRA",
                    }
                ],
                "silver_athletes": [
                    {
                        "full_name": "MICHELON Paula",
                        "name": "MICHELON",
                        "nat": "FRA",
                    }
                ],
                "bronze_athletes": [
                    {
                        "full_name": "KIRKEEIDE Maren",
                        "name": "KIRKEEIDE",
                        "nat": "NOR",
                    }
                ],
            }
        ],
        argparse.Namespace(format="pretty", leader_markers=False),
        last_name_only=True,
    )

    assert captured["headers"] == [
        "Date",
        "Type",
        "Venue",
        "GOLD",
        "SILVER",
        "BRONZE",
    ]
    assert captured["rows"] == [
        [
            "2026-02-14",
            "Olympic Games",
            "Antholz-Anterselva",
            "JEANMONNOT Lou (FRA)",
            "MICHELON Paula (FRA)",
            "KIRKEEIDE Maren (NOR)",
        ]
    ]
    assert captured["kwargs"]["column_separators"] == {3}
    assert captured["kwargs"]["header_alignments"] == {
        0: "center",
        1: "center",
        2: "center",
        3: "center",
        4: "center",
        5: "center",
    }


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
    assert "WC Men Relay Points" in out
    assert "1\tNORWAY\tNOR\t220" in out


def test_render_startlist_analysis_mixed_nations_cup_uses_level_3_gender_headings(
    monkeypatch, capsys
):
    monkeypatch.setattr(
        startlist,
        "_fetch_nations_cup_standings",
        lambda _season_id, target_cat, limit=10: [
            {
                "Rank": "1",
                "Nat": "SWE" if target_cat == "SW" else "NOR",
                "Name": "Sweden" if target_cat == "SW" else "Norway",
                "Score": "120" if target_cat == "SW" else "130",
            }
        ],
    )
    monkeypatch.setattr(
        startlist, "_fetch_relay_wc_standings", lambda *_a, **_k: ("Mixed Relay", [])
    )
    monkeypatch.setattr(
        startlist, "_compute_country_what_if_scenarios", lambda *_a, **_k: []
    )
    monkeypatch.setattr(startlist, "_get_previous_relay_podiums", lambda *_a, **_k: [])

    ctx = {
        "payload": {"Results": []},
        "race_id": "BT2526SWRLCP08MXSR",
        "entries": [],
        "race_disc": "SR",
        "cat_id": "MX",
        "season_id": "2526",
        "event_type": startlist.EVENT_TYPE_WC,
        "startlist_ids": set(),
        "age_cache": {},
        "prefetched_results": {},
        "team_entries": [],
        "is_mixed": True,
        "is_snapshot": False,
        "snapshot_target_race_id": "",
        "snapshot_cutoff_dt": None,
        "snapshot_race_start_cache": {},
    }
    args = argparse.Namespace(format="markdown", leader_markers=False)

    startlist.render_startlist_analysis(ctx, args)
    out = capsys.readouterr().out
    lines = out.splitlines()

    nations_idx = lines.index("## Nations Cup Standings (Top 10)")
    women_idx = lines.index("### Women")
    men_idx = lines.index("### Men")
    relay_idx = lines.index("## WC Mixed Relay Points: none")

    assert "## Previous Single Mixed Relay podiums: none" in lines
    assert nations_idx < women_idx < men_idx < relay_idx
    assert "## Women" not in lines
    assert "## Men" not in lines
    assert lines[nations_idx + 1] == ""
    assert women_idx == nations_idx + 2
    assert lines[women_idx + 1] == ""
    assert lines[men_idx - 2] == ""
    assert lines[men_idx - 1] == ""
    assert lines[men_idx + 1] == ""
    assert lines[relay_idx - 2] == ""
    assert lines[relay_idx - 1] == ""


def test_render_startlist_analysis_mixed_relay_uses_mixed_relay_podium_title(
    monkeypatch, capsys
):
    monkeypatch.setattr(
        startlist, "_fetch_relay_wc_standings", lambda *_a, **_k: ("Mixed Relay", [])
    )
    monkeypatch.setattr(startlist, "_fetch_nations_cup_standings", lambda *_a, **_k: [])
    monkeypatch.setattr(
        startlist, "_compute_country_what_if_scenarios", lambda *_a, **_k: []
    )
    monkeypatch.setattr(startlist, "_get_previous_relay_podiums", lambda *_a, **_k: [])

    ctx = {
        "payload": {"Results": []},
        "race_id": "BT2526SWRLCP08MXRL",
        "entries": [],
        "race_disc": "RL",
        "cat_id": "MX",
        "season_id": "2526",
        "event_type": startlist.EVENT_TYPE_WC,
        "startlist_ids": set(),
        "age_cache": {},
        "prefetched_results": {},
        "team_entries": [],
        "is_mixed": True,
        "is_snapshot": False,
        "snapshot_target_race_id": "",
        "snapshot_cutoff_dt": None,
        "snapshot_race_start_cache": {},
    }
    args = argparse.Namespace(format="markdown", leader_markers=False)

    startlist.render_startlist_analysis(ctx, args)
    out = capsys.readouterr().out

    assert "## WC Mixed Relay Points: none" in out
    assert "## Previous Mixed Relay podiums: none" in out
    assert "## Previous Relay podiums: none" not in out


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


def test_render_startlist_analysis_hides_milestones_for_provisional_startlist(
    monkeypatch, capsys
):
    monkeypatch.setattr(startlist, "_get_wc_rows", lambda *_a, **_k: [])
    monkeypatch.setattr(startlist, "_get_cup_ids_for_race", lambda *_a, **_k: ("", ""))
    monkeypatch.setattr(startlist, "_fetch_nations_cup_standings", lambda *_a, **_k: [])
    monkeypatch.setattr(
        startlist, "_get_previous_individual_podiums", lambda *_a, **_k: []
    )

    ctx = {
        "payload": {
            "IsStartList": True,
            "Competition": {"StatusText": "Prov. Start List"},
            "Results": [],
        },
        "race_id": "RACE1",
        "entries": [{"ibu_id": "A1", "name": "Alpha", "age": "25", "nat": "NOR"}],
        "race_disc": "SP",
        "cat_id": "SW",
        "season_id": "2526",
        "event_type": startlist.EVENT_TYPE_WC,
        "startlist_ids": {"A1"},
        "age_cache": {"A1": "25"},
        "prefetched_results": {
            "A1": {"Results": [{"Level": "WC", "Comp": "SP", "Rank": "2", "SO": "2"}]}
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

    assert "Race milestones" not in out
    assert "Win milestones" not in out
    assert "Previous Sprint podiums: none" in out


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
    current_start = race_block.index("World Cup")
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
    next_start = out.index("Previous Sprint podiums", win_start)
    win_block = out[win_start:next_start]
    current_start = win_block.index("World Cup")
    career_start = win_block.index("## Career")
    current_block = win_block[current_start:career_start]
    career_block = win_block[career_start:]
    assert current_block.rfind("\tAlpha\t") < current_block.find("\tBeta\t")
    assert career_block.rfind("\tAlpha\t") < career_block.find("\tBeta\t")


def test_render_startlist_analysis_adds_column_separators_to_milestone_tables(
    monkeypatch,
):
    monkeypatch.setattr(startlist, "_get_wc_rows", lambda *_a, **_k: [])
    monkeypatch.setattr(startlist, "_get_cup_ids_for_race", lambda *_a, **_k: ("", ""))
    monkeypatch.setattr(startlist, "_fetch_nations_cup_standings", lambda *_a, **_k: [])
    monkeypatch.setattr(
        startlist, "_get_previous_individual_podiums", lambda *_a, **_k: []
    )

    captured_kwargs: list[dict] = []

    def fake_render_table(headers, _rows, **kwargs):
        if headers == ["Milestone", "Event", "Type", "Athlete", "Age", "Nat"]:
            captured_kwargs.append(kwargs)

    monkeypatch.setattr(startlist, "render_table", fake_render_table)

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
        "prefetched_results": {
            "A1": {
                "Results": [
                    {"Level": "WC", "Comp": "SP", "Rank": "1", "SO": "1"}
                    for _ in range(49)
                ]
            }
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

    assert len(captured_kwargs) == 4
    assert all(kwargs.get("column_separators") == {3} for kwargs in captured_kwargs)
    assert all(kwargs.get("row_separators") is None for kwargs in captured_kwargs)


def test_render_startlist_analysis_adds_column_separator_to_pursuit_contenders(
    monkeypatch,
):
    monkeypatch.setattr(startlist, "_get_wc_rows", lambda *_a, **_k: [])
    monkeypatch.setattr(startlist, "_get_cup_ids_for_race", lambda *_a, **_k: ("", ""))
    monkeypatch.setattr(startlist, "_fetch_nations_cup_standings", lambda *_a, **_k: [])
    monkeypatch.setattr(
        startlist, "_get_previous_individual_podiums", lambda *_a, **_k: []
    )
    monkeypatch.setattr(startlist, "_compute_what_if_scenarios", lambda *_a, **_k: [])

    captured: dict[str, object] = {}

    def fake_render_table(headers, rows, **kwargs):
        if headers == ["Delay", "Athlete", "Nat"]:
            captured["rows"] = rows
            captured["kwargs"] = kwargs

    monkeypatch.setattr(startlist, "render_table", fake_render_table)

    ctx = {
        "payload": {
            "Results": [
                {
                    "IsTeam": False,
                    "StartInfo": "0:11",
                    "IBUId": "A1",
                    "Name": "Alpha",
                    "Nat": "NOR",
                },
                {
                    "IsTeam": False,
                    "StartInfo": "1:00",
                    "IBUId": "B1",
                    "Name": "Bravo",
                    "Nat": "SWE",
                },
            ]
        },
        "race_id": "RACE1",
        "entries": [],
        "race_disc": "PU",
        "cat_id": "SW",
        "season_id": "2526",
        "event_type": startlist.EVENT_TYPE_WC,
        "startlist_ids": set(),
        "age_cache": {},
        "prefetched_results": {},
        "team_entries": [],
        "is_mixed": False,
        "is_snapshot": False,
        "snapshot_target_race_id": "",
        "snapshot_cutoff_dt": None,
        "snapshot_race_start_cache": {},
    }
    args = argparse.Namespace(format="tsv", leader_markers=False)

    startlist.render_startlist_analysis(ctx, args)

    assert captured["rows"] == [["0:11", "Alpha", "NOR"]]
    assert captured["kwargs"]["column_separators"] == {1}


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
    next_start = out.index("Previous Men Relay podiums", win_start)
    win_block = out[win_start:next_start]
    assert "### Olympic Games" in win_block
    assert "Milestone\tEvent\tType\tAthlete\tAge\tNat" in win_block
    assert "CurrentWins" not in win_block
    assert "2nd\tOlympic Games\tRace\tAlpha\t25\tNOR" in win_block
