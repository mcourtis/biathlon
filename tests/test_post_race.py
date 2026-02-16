"""Tests for post-race deterministic snapshot helpers."""

import argparse
import datetime

from biathlon.api import BiathlonError
from biathlon.commands import post_race


def _dt(value: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_filter_results_to_snapshot_excludes_future_and_warns_unknown(
    monkeypatch, capsys
):
    target_race_id = "RACE_TARGET"
    target_start = _dt("2026-01-10T10:00:00Z")

    start_by_race = {
        "RACE_PAST": "2026-01-01T10:00:00Z",
        "RACE_FUTURE": "2026-02-01T10:00:00Z",
    }

    def fake_get_race_results(race_id: str) -> dict:
        if race_id not in start_by_race:
            raise BiathlonError("missing")
        return {"Competition": {"StartTime": start_by_race[race_id]}}

    monkeypatch.setattr(post_race, "get_race_results", fake_get_race_results)

    rows = [
        {"RaceId": "RACE_TARGET"},
        {"RaceId": "RACE_PAST"},
        {"RaceId": "RACE_FUTURE"},
        {"RaceId": "RACE_UNKNOWN"},
    ]
    warning_keys: set[str] = set()
    race_start_cache = {target_race_id: target_start}

    filtered = post_race._filter_results_to_snapshot(
        rows,
        target_race_id,
        target_start,
        race_start_cache,
        warning_keys,
        "test context",
    )

    assert [row["RaceId"] for row in filtered] == ["RACE_TARGET", "RACE_PAST"]
    err = capsys.readouterr().err
    assert "warning: skipping row with unknown chronology" in err
    assert "RACE_UNKNOWN" in err


def test_is_result_at_or_before_target_uses_season_fast_path(monkeypatch):
    def fail_if_called(race_id: str) -> dict:
        raise AssertionError(f"unexpected API call for {race_id}")

    monkeypatch.setattr(post_race, "get_race_results", fail_if_called)

    warning_keys: set[str] = set()
    cache = {}
    target_race = "BT2526SWRLOG__SMPU"
    target_start = _dt("2026-02-15T10:15:00Z")

    past_row = {"RaceId": "BT2122SWRLOG__SMIN", "Season": "21/22"}
    future_row = {"RaceId": "BT2728SWRLCP01SMSP", "Season": "27/28"}

    assert (
        post_race._is_result_at_or_before_target(
            past_row,
            target_race,
            target_start,
            cache,
            warning_keys,
            "season fast path",
        )
        is True
    )
    assert (
        post_race._is_result_at_or_before_target(
            future_row,
            target_race,
            target_start,
            cache,
            warning_keys,
            "season fast path",
        )
        is False
    )


def test_is_lapped_current_result_detects_lap_by_irm():
    entry = {"irm": "LAP", "time": "+1:20.5"}
    assert post_race._is_lapped_current_result(entry, 10059, "PU") is True


def test_is_lapped_current_result_detects_pursuit_fallback_rank():
    entry = {"irm": "", "time": "39:10.0"}
    assert post_race._is_lapped_current_result(entry, 10060, "PU") is True
    assert post_race._is_lapped_current_result(entry, 10060, "SP") is False


def test_collect_discipline_race_ids_applies_cutoff_and_warns_unknown(
    monkeypatch, capsys
):
    monkeypatch.setattr(
        post_race, "get_events", lambda season_id, level: [{"EventId": "E"}]
    )
    monkeypatch.setattr(post_race, "detect_event_type", lambda event: "WC")
    monkeypatch.setattr(
        post_race,
        "get_races",
        lambda event_id: [
            {
                "RaceId": "RACE_OLD",
                "DisciplineId": "SP",
                "CatId": "SW",
                "StartTime": "2026-01-01T10:00:00Z",
            },
            {
                "RaceId": "RACE_NEW",
                "DisciplineId": "SP",
                "CatId": "SW",
                "StartTime": "2026-03-01T10:00:00Z",
            },
            {
                "RaceId": "RACE_UNKNOWN",
                "DisciplineId": "SP",
                "CatId": "SW",
            },
        ],
    )

    warning_keys: set[str] = set()
    race_ids = post_race._collect_discipline_race_ids(
        ["2526"],
        "SP",
        "SW",
        "WC",
        cutoff_dt=_dt("2026-02-01T00:00:00Z"),
        warning_keys=warning_keys,
        warning_context="medal races",
    )

    assert race_ids == ["RACE_OLD"]
    err = capsys.readouterr().err
    assert "warning: skipping race with unknown chronology in medal races" in err
    assert "RACE_UNKNOWN" in err


def test_render_olympic_medal_sections_uses_inclusive_cutoff(monkeypatch):
    captured: dict[str, bool] = {}

    def fake_individual_podiums(
        discipline: str,
        category: str,
        cutoff_dt=None,
        include_cutoff: bool = False,
    ) -> list[dict]:
        captured["podiums_include_cutoff"] = include_cutoff
        return []

    def fake_all_olympic_medals(
        category: str,
        cutoff_dt=None,
        include_cutoff: bool = False,
    ) -> tuple[list[dict], dict[str, dict]]:
        captured["medals_include_cutoff"] = include_cutoff
        return [], {}

    monkeypatch.setattr(
        post_race, "_get_past_olympic_individual_podiums", fake_individual_podiums
    )
    monkeypatch.setattr(post_race, "_get_all_olympic_medals", fake_all_olympic_medals)

    args = argparse.Namespace(format="tsv")
    sec = post_race._render_olympic_medal_sections(
        args,
        0,
        "PU",
        "SW",
        False,
        set(),
        set(),
        set(),
        set(),
        cutoff_dt=_dt("2026-02-15T12:00:00Z"),
    )

    assert sec == 3
    assert captured["podiums_include_cutoff"] is True
    assert captured["medals_include_cutoff"] is True


def test_has_newer_relevant_wc_points_race_detects_completed_newer(monkeypatch):
    races = [
        (_dt("2026-01-01T10:00:00Z"), "RACE_OLD", "SP"),
        (_dt("2026-01-10T10:00:00Z"), "RACE_TARGET", "SP"),
        (_dt("2026-01-20T10:00:00Z"), "RACE_NEW", "PU"),
    ]
    monkeypatch.setattr(
        post_race, "_collect_wc_individual_races", lambda season_id, cat_id: races
    )
    monkeypatch.setattr(
        post_race,
        "get_race_results",
        lambda race_id: {
            "Results": [
                {"IsTeam": False, "Rank": "1", "Result": "24:00.0", "IBUId": "A"}
            ]
        },
    )

    assert (
        post_race._has_newer_relevant_wc_points_race(
            "2526", "SW", "RACE_TARGET", _dt("2026-01-10T10:00:00Z")
        )
        is True
    )


def test_compute_wc_snapshot_rows_uses_target_cutoff(monkeypatch):
    races = [
        (_dt("2026-01-01T10:00:00Z"), "RACE_OLD", "SP"),
        (_dt("2026-01-10T10:00:00Z"), "RACE_TARGET", "SP"),
        (_dt("2026-01-20T10:00:00Z"), "RACE_NEW", "SP"),
    ]
    monkeypatch.setattr(
        post_race, "_collect_wc_individual_races", lambda season_id, cat_id: races
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
                },
                {
                    "IsTeam": False,
                    "Rank": "3",
                    "IBUId": "A",
                    "Name": "Alpha",
                    "Nat": "NOR",
                },
            ]
        },
        "RACE_NEW": {
            "Results": [
                {
                    "IsTeam": False,
                    "Rank": "1",
                    "IBUId": "A",
                    "Name": "Alpha",
                    "Nat": "NOR",
                },
            ]
        },
    }
    monkeypatch.setattr(
        post_race, "get_race_results", lambda race_id: payloads[race_id]
    )

    total_rows, disc_rows = post_race._compute_wc_snapshot_rows(
        "2526",
        "SW",
        "RACE_TARGET",
        "SP",
        _dt("2026-01-10T10:00:00Z"),
        set(),
    )

    assert total_rows[0]["IBUId"] == "B"
    assert total_rows[0]["Score"] == 165
    assert total_rows[0]["RnkDiff"] == -1
    assert disc_rows[0]["IBUId"] == "B"


def test_relay_milestone_types_for_rank():
    assert post_race._relay_milestone_types_for_rank(1) == [
        "Relay Win",
        "Relay Podium",
        "Relay Flower",
        "Win",
        "Podium",
        "Flower",
    ]
    assert post_race._relay_milestone_types_for_rank(2) == [
        "Relay Podium",
        "Relay Flower",
        "Podium",
        "Flower",
    ]
    assert post_race._relay_milestone_types_for_rank(3) == [
        "Relay Podium",
        "Relay Flower",
        "Podium",
        "Flower",
    ]
    assert post_race._relay_milestone_types_for_rank(4) == ["Relay Flower", "Flower"]
    assert post_race._relay_milestone_types_for_rank(6) == ["Relay Flower", "Flower"]


def test_build_race_milestone_rows_relay_team_race_only():
    rows = post_race._build_race_milestone_rows(
        race_count=26,
        team_race_count=25,
        is_relay=True,
        decorated_name="Athlete A",
        nat="NOR",
    )
    assert rows == [[25, "Team Race", "Athlete A", "NOR"]]


def test_build_race_milestone_rows_relay_race_and_team_race():
    rows = post_race._build_race_milestone_rows(
        race_count=50,
        team_race_count=25,
        is_relay=True,
        decorated_name="Athlete A",
        nat="NOR",
    )
    assert rows == [
        [50, "Race", "Athlete A", "NOR"],
        [25, "Team Race", "Athlete A", "NOR"],
    ]


def test_build_race_milestone_rows_non_relay_shape_unchanged():
    rows = post_race._build_race_milestone_rows(
        race_count=25,
        team_race_count=None,
        is_relay=False,
        decorated_name="Athlete A",
        nat="NOR",
    )
    assert rows == [[25, "Athlete A", "NOR"]]


def test_build_relay_milestone_blocks_rank_rows_and_highlights():
    entries = [
        {"ibu_id": "A1", "name": "A1", "nat": "NOR", "bib": "1", "leg": 1},
        {"ibu_id": "A2", "name": "A2", "nat": "NOR", "bib": "1", "leg": 2},
        {"ibu_id": "A3", "name": "A3", "nat": "NOR", "bib": "1", "leg": 3},
        {"ibu_id": "A4", "name": "A4", "nat": "NOR", "bib": "1", "leg": 4},
        {"ibu_id": "B1", "name": "B1", "nat": "FRA", "bib": "2", "leg": 1},
        {"ibu_id": "B2", "name": "B2", "nat": "FRA", "bib": "2", "leg": 2},
        {"ibu_id": "B3", "name": "B3", "nat": "FRA", "bib": "2", "leg": 3},
        {"ibu_id": "B4", "name": "B4", "nat": "FRA", "bib": "2", "leg": 4},
        {"ibu_id": "C1", "name": "C1", "nat": "SWE", "bib": "3", "leg": 1},
        {"ibu_id": "C2", "name": "C2", "nat": "SWE", "bib": "3", "leg": 2},
        {"ibu_id": "C3", "name": "C3", "nat": "SWE", "bib": "3", "leg": 3},
        {"ibu_id": "C4", "name": "C4", "nat": "SWE", "bib": "3", "leg": 4},
    ]
    team_results = [
        {"Bib": "1", "Rank": "1", "Name": "Norway", "Nat": "NOR"},
        {"Bib": "2", "Rank": "2", "Name": "France", "Nat": "FRA"},
        {"Bib": "3", "Rank": "4", "Name": "Sweden", "Nat": "SWE"},
    ]

    top_milestones = [
        [1, "Relay Win", "A1", "NOR", "A1", 1],
        [5, "Relay Podium", "A1", "NOR", "A1", 1],
        [2, "Relay Flower", "A1", "NOR", "A1", 1],
        [10, "Win", "A1", "NOR", "A1", 1],
        [3, "Podium", "A1", "NOR", "A1", 1],
        [4, "Flower", "A1", "NOR", "A1", 1],
        [2, "Relay Win", "A2", "NOR", "A2", 1],
        [3, "Relay Podium", "A2", "NOR", "A2", 1],
        [4, "Relay Flower", "A2", "NOR", "A2", 1],
        [5, "Win", "A2", "NOR", "A2", 1],
        [6, "Podium", "A2", "NOR", "A2", 1],
        [7, "Flower", "A2", "NOR", "A2", 1],
        [3, "Relay Win", "A3", "NOR", "A3", 1],
        [4, "Relay Podium", "A3", "NOR", "A3", 1],
        [6, "Relay Flower", "A3", "NOR", "A3", 1],
        [7, "Win", "A3", "NOR", "A3", 1],
        [8, "Podium", "A3", "NOR", "A3", 1],
        [9, "Flower", "A3", "NOR", "A3", 1],
        [4, "Relay Win", "A4", "NOR", "A4", 1],
        [6, "Relay Podium", "A4", "NOR", "A4", 1],
        [7, "Relay Flower", "A4", "NOR", "A4", 1],
        [8, "Win", "A4", "NOR", "A4", 1],
        [9, "Podium", "A4", "NOR", "A4", 1],
        [11, "Flower", "A4", "NOR", "A4", 1],
        [2, "Relay Podium", "B1", "FRA", "B1", 2],
        [5, "Relay Flower", "B1", "FRA", "B1", 2],
        [4, "Podium", "B1", "FRA", "B1", 2],
        [6, "Flower", "B1", "FRA", "B1", 2],
        [3, "Relay Podium", "B2", "FRA", "B2", 2],
        [4, "Relay Flower", "B2", "FRA", "B2", 2],
        [5, "Podium", "B2", "FRA", "B2", 2],
        [7, "Flower", "B2", "FRA", "B2", 2],
        [4, "Relay Podium", "B3", "FRA", "B3", 2],
        [6, "Relay Flower", "B3", "FRA", "B3", 2],
        [7, "Podium", "B3", "FRA", "B3", 2],
        [8, "Flower", "B3", "FRA", "B3", 2],
        [6, "Relay Podium", "B4", "FRA", "B4", 2],
        [7, "Relay Flower", "B4", "FRA", "B4", 2],
        [8, "Podium", "B4", "FRA", "B4", 2],
        [9, "Flower", "B4", "FRA", "B4", 2],
        [3, "Relay Flower", "C1", "SWE", "C1", 4],
        [4, "Flower", "C1", "SWE", "C1", 4],
        [4, "Relay Flower", "C2", "SWE", "C2", 4],
        [5, "Flower", "C2", "SWE", "C2", 4],
        [6, "Relay Flower", "C3", "SWE", "C3", 4],
        [7, "Flower", "C3", "SWE", "C3", 4],
        [8, "Relay Flower", "C4", "SWE", "C4", 4],
        [9, "Flower", "C4", "SWE", "C4", 4],
    ]

    blocks = post_race._build_relay_milestone_blocks(
        top_milestones, entries, team_results
    )

    assert [block["rank"] for block in blocks] == [1, 2, 4]
    assert blocks[0]["team_name"] == "Norway"
    assert blocks[1]["team_name"] == "France"
    assert blocks[2]["team_name"] == "Sweden"
    assert blocks[0]["headers"] == [
        "Milestone Type",
        "L1 A1",
        "L2 A2",
        "L3 A3",
        "L4 A4",
    ]

    assert [row[0] for row in blocks[0]["rows"]] == [
        "Relay Win",
        "Relay Podium",
        "Relay Flower",
        "Win",
        "Podium",
        "Flower",
    ]
    assert [row[0] for row in blocks[1]["rows"]] == [
        "Relay Podium",
        "Relay Flower",
        "Podium",
        "Flower",
    ]
    assert [row[0] for row in blocks[2]["rows"]] == ["Relay Flower", "Flower"]

    row_map = {row[0]: row for row in blocks[0]["rows"]}
    assert row_map["Relay Win"][1] == "1st"
    assert row_map["Relay Podium"][1] == "5th"
    assert row_map["Win"][1] == "10th"
    assert row_map["Podium"][1] == "3rd"
    assert row_map["Flower"][1] == "4th"

    assert (0, 1) in blocks[0]["highlight_cells"]
    assert (1, 1) in blocks[0]["highlight_cells"]
    assert (3, 1) in blocks[0]["highlight_cells"]
    assert (4, 1) not in blocks[0]["highlight_cells"]


def test_build_relay_milestone_blocks_missing_leg_uses_placeholder():
    entries = [
        {"ibu_id": "A1", "name": "A1", "nat": "NOR", "bib": "1", "leg": 1},
        {"ibu_id": "A2", "name": "A2", "nat": "NOR", "bib": "1", "leg": 2},
        {"ibu_id": "A3", "name": "A3", "nat": "NOR", "bib": "1", "leg": 3},
    ]
    team_results = [{"Bib": "1", "Rank": "4", "Name": "Norway", "Nat": "NOR"}]
    top_milestones = [
        [2, "Relay Flower", "A1", "NOR", "A1", 4],
        [3, "Flower", "A1", "NOR", "A1", 4],
        [3, "Relay Flower", "A2", "NOR", "A2", 4],
        [4, "Flower", "A2", "NOR", "A2", 4],
        [4, "Relay Flower", "A3", "NOR", "A3", 4],
        [5, "Flower", "A3", "NOR", "A3", 4],
    ]

    blocks = post_race._build_relay_milestone_blocks(
        top_milestones, entries, team_results
    )

    assert len(blocks) == 1
    assert blocks[0]["headers"][-1] == "L4 -"
    for row in blocks[0]["rows"]:
        assert row[-1] == "-"
