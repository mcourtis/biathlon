"""Tests for post-race deterministic snapshot helpers."""

import argparse
import datetime

from biathlon.api import BiathlonError
from biathlon.commands import achievements, postrace


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

    monkeypatch.setattr(postrace, "get_race_results", fake_get_race_results)

    rows = [
        {"RaceId": "RACE_TARGET"},
        {"RaceId": "RACE_PAST"},
        {"RaceId": "RACE_FUTURE"},
        {"RaceId": "RACE_UNKNOWN"},
    ]
    warning_keys: set[str] = set()
    race_start_cache = {target_race_id: target_start}

    filtered = postrace._filter_results_to_snapshot(
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

    monkeypatch.setattr(postrace, "get_race_results", fail_if_called)

    warning_keys: set[str] = set()
    cache = {}
    target_race = "BT2526SWRLOG__SMPU"
    target_start = _dt("2026-02-15T10:15:00Z")

    past_row = {"RaceId": "BT2122SWRLOG__SMIN", "Season": "21/22"}
    future_row = {"RaceId": "BT2728SWRLCP01SMSP", "Season": "27/28"}

    assert (
        postrace._is_result_at_or_before_target(
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
        postrace._is_result_at_or_before_target(
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
    assert postrace._is_lapped_current_result(entry, 10059, "PU") is True


def test_is_lapped_current_result_detects_pursuit_fallback_rank():
    entry = {"irm": "", "time": "39:10.0"}
    assert postrace._is_lapped_current_result(entry, 10060, "PU") is True
    assert postrace._is_lapped_current_result(entry, 10060, "SP") is False


def test_collect_discipline_race_ids_applies_cutoff_and_warns_unknown(
    monkeypatch, capsys
):
    monkeypatch.setattr(
        postrace, "get_events", lambda season_id, level: [{"EventId": "E"}]
    )
    monkeypatch.setattr(postrace, "detect_event_type", lambda event: "WC")
    monkeypatch.setattr(
        postrace,
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
    race_ids = postrace._collect_discipline_race_ids(
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
        postrace, "_get_past_olympic_individual_podiums", fake_individual_podiums
    )
    monkeypatch.setattr(postrace, "_get_all_olympic_medals", fake_all_olympic_medals)

    args = argparse.Namespace(format="tsv")
    sec = postrace._render_olympic_medal_sections(
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


def test_render_olympic_medal_sections_dynamic_keeps_relay_start_counts(monkeypatch):
    race_meta = [
        {
            "race_id": "RREL1",
            "discipline": "RL",
            "cat": "SM",
            "start_dt": _dt("2018-02-20T10:00:00Z"),
        },
        {
            "race_id": "RREL2",
            "discipline": "RL",
            "cat": "SM",
            "start_dt": _dt("2022-02-20T10:00:00Z"),
        },
    ]
    payload_by_race = {
        "RREL1": {
            "Competition": {"DisciplineId": "RL", "catId": "SM"},
            "Results": [
                {"IsTeam": True, "Rank": "1", "Nat": "NOR"},
                {"IsTeam": True, "Rank": "2", "Nat": "GER"},
                {"IsTeam": True, "Rank": "3", "Nat": "FRA"},
                {"IsTeam": True, "Rank": "4", "Nat": "SWE"},
                {
                    "IsTeam": False,
                    "IBUId": "MFOUR",
                    "Name": "Martin Fourcade",
                    "Nat": "NOR",
                },
            ],
        },
        "RREL2": {
            "Competition": {"DisciplineId": "RL", "catId": "SM"},
            "Results": [
                {"IsTeam": True, "Rank": "1", "Nat": "GER"},
                {"IsTeam": True, "Rank": "2", "Nat": "FRA"},
                {"IsTeam": True, "Rank": "3", "Nat": "SWE"},
                {"IsTeam": True, "Rank": "4", "Nat": "NOR"},
                {
                    "IsTeam": False,
                    "IBUId": "MFOUR",
                    "Name": "Martin Fourcade",
                    "Nat": "NOR",
                },
            ],
        },
    }

    monkeypatch.setattr(
        achievements,
        "_resolve_season_selection",
        lambda scope, season_arg: (["2122"], "all", {"2122": [{"EventId": "E2122"}]}),
    )
    monkeypatch.setattr(
        achievements,
        "_collect_race_meta",
        lambda season_ids, scope_events, category: list(race_meta),
    )
    monkeypatch.setattr(
        achievements,
        "_fetch_race_payloads",
        lambda race_meta: dict(payload_by_race),
    )
    monkeypatch.setattr(
        postrace,
        "_get_past_olympic_relay_podiums",
        lambda discipline, category, cutoff_dt=None, include_cutoff=False: [],
    )
    monkeypatch.setattr(
        postrace,
        "_get_all_olympic_medals",
        lambda category, cutoff_dt=None, include_cutoff=False: ([], {}),
    )

    captured_athlete_rows: list[list[str]] = []

    def fake_render_table(headers, rows, **kwargs):
        if len(headers) == 19 and headers[1] == "Athlete":
            captured_athlete_rows.extend(rows)

    monkeypatch.setattr(postrace, "render_table", fake_render_table)

    sec = postrace._render_olympic_medal_sections(
        argparse.Namespace(format="tsv"),
        0,
        "RL",
        "SM",
        True,
        set(),
        {"MFOUR"},
        set(),
        set(),
        cutoff_dt=_dt("2026-02-15T12:00:00Z"),
        use_dynamic_all_olympic_stats=True,
    )

    assert sec == 3
    martin_row = next(
        row for row in captured_athlete_rows if row[1] == "Martin Fourcade"
    )
    assert martin_row[18] == "2"


def test_has_newer_relevant_wc_points_race_detects_completed_newer(monkeypatch):
    races = [
        (_dt("2026-01-01T10:00:00Z"), "RACE_OLD", "SP"),
        (_dt("2026-01-10T10:00:00Z"), "RACE_TARGET", "SP"),
        (_dt("2026-01-20T10:00:00Z"), "RACE_NEW", "PU"),
    ]
    monkeypatch.setattr(
        postrace, "_collect_wc_individual_races", lambda season_id, cat_id: races
    )
    monkeypatch.setattr(
        postrace,
        "get_race_results",
        lambda race_id: {
            "Results": [
                {"IsTeam": False, "Rank": "1", "Result": "24:00.0", "IBUId": "A"}
            ]
        },
    )

    assert (
        postrace._has_newer_relevant_wc_points_race(
            "2526", "SW", "RACE_TARGET", _dt("2026-01-10T10:00:00Z")
        )
        is True
    )


def test_collect_wc_individual_races_excludes_non_counting_major_events(monkeypatch):
    monkeypatch.setattr(
        postrace,
        "get_events",
        lambda season_id, level=1: [
            {"EventId": "E_WC", "Description": "BMW IBU World Cup"},
            {"EventId": "E_WCH", "Description": "BMW IBU World Championships"},
            {"EventId": "E_OWG", "Description": "Olympic Winter Games"},
        ],
    )
    monkeypatch.setattr(
        postrace,
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

    rows = postrace._collect_wc_individual_races("2526", "SW")

    assert [race_id for _start_dt, race_id, _disc in rows] == ["E_WC_R1"]


def test_compute_wc_snapshot_rows_uses_target_cutoff(monkeypatch):
    races = [
        (_dt("2026-01-01T10:00:00Z"), "RACE_OLD", "SP"),
        (_dt("2026-01-10T10:00:00Z"), "RACE_TARGET", "SP"),
        (_dt("2026-01-20T10:00:00Z"), "RACE_NEW", "SP"),
    ]
    monkeypatch.setattr(
        postrace, "_collect_wc_individual_races", lambda season_id, cat_id: races
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
    monkeypatch.setattr(postrace, "get_race_results", lambda race_id: payloads[race_id])

    total_rows, disc_rows = postrace._compute_wc_snapshot_rows(
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
    assert postrace._relay_milestone_types_for_rank(1) == [
        "Relay Win",
        "Relay Podium",
        "Relay Flower",
        "Win",
        "Podium",
        "Flower",
    ]
    assert postrace._relay_milestone_types_for_rank(2) == [
        "Relay Podium",
        "Relay Flower",
        "Podium",
        "Flower",
    ]
    assert postrace._relay_milestone_types_for_rank(3) == [
        "Relay Podium",
        "Relay Flower",
        "Podium",
        "Flower",
    ]
    assert postrace._relay_milestone_types_for_rank(4) == ["Relay Flower", "Flower"]
    assert postrace._relay_milestone_types_for_rank(6) == ["Relay Flower", "Flower"]


def test_build_race_milestone_rows_relay_team_race_only():
    rows = postrace._build_race_milestone_rows(
        race_count=26,
        team_race_count=25,
        is_relay=True,
        decorated_name="Athlete A",
        nat="NOR",
    )
    assert rows == [[25, "Team Race", "Athlete A", "NOR"]]


def test_build_race_milestone_rows_relay_race_and_team_race():
    rows = postrace._build_race_milestone_rows(
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
    rows = postrace._build_race_milestone_rows(
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

    blocks = postrace._build_relay_milestone_blocks(
        top_milestones, entries, team_results
    )

    assert [block["rank"] for block in blocks] == [1, 2, 4]
    assert blocks[0]["team_name"] == "Norway"
    assert blocks[1]["team_name"] == "France"
    assert blocks[2]["team_name"] == "Sweden"
    assert blocks[0]["headers"] == [
        "Milestone Type",
        "A1",
        "A2",
        "A3",
        "A4",
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

    blocks = postrace._build_relay_milestone_blocks(
        top_milestones, entries, team_results
    )

    assert len(blocks) == 1
    assert blocks[0]["headers"][-1] == "-"
    for row in blocks[0]["rows"]:
        assert row[-1] == "-"


def test_find_best_u23_leader_prefers_explicit_row_marker():
    rows = [
        {"Rank": "1", "IBUId": "A", "Name": "Alpha", "Nat": "NOR", "Score": 300},
        {
            "Rank": "2",
            "IBUId": "B",
            "Name": "Bravo",
            "Nat": "SWE",
            "Score": 250,
            "BestU23": 1,
        },
    ]

    leader = postrace._find_best_u23_leader(rows, {"A"})

    assert leader["id"] == "B"
    assert leader["name"] == "Bravo"
    assert leader["nat"] == "SWE"


def test_find_best_u23_leader_falls_back_to_u23_ids():
    rows = [
        {"Rank": "1", "IBUId": "A", "Name": "Alpha", "Nat": "NOR", "Score": 300},
        {"Rank": "2", "IBUId": "B", "Name": "Bravo", "Nat": "SWE", "Score": 250},
        {"Rank": "3", "IBUId": "C", "Name": "Charlie", "Nat": "FRA", "Score": 220},
    ]

    leader = postrace._find_best_u23_leader(rows, {"B", "C"})

    assert leader["id"] == "B"


def test_build_athlete_age_map_appends_u23(monkeypatch):
    bios = {
        "A": {"BirthDate": "2004-01-01"},
        "B": {"Age": "24"},
    }

    def fake_get_athlete_bio(ibu_id: str) -> dict:
        if ibu_id == "C":
            raise BiathlonError("boom")
        return bios.get(ibu_id, {})

    monkeypatch.setattr(postrace, "get_athlete_bio", fake_get_athlete_bio)

    age_map, u23_ids = postrace._build_athlete_age_map(
        {"A", "B", "C"}, datetime.date(2025, 12, 1)
    )

    assert age_map["A"].endswith("(U23)")
    assert age_map["B"] == "24"
    assert age_map["C"] == "-"
    assert u23_ids == {"A"}


def test_build_standings_rows_includes_age_column():
    rows = [
        {
            "Rank": "1",
            "IBUId": "A",
            "Name": "Alpha",
            "Nat": "NOR",
            "Score": 300,
            "RnkDiff": -1,
        },
        {
            "Rank": "2",
            "IBUId": "B",
            "Name": "Bravo",
            "Nat": "SWE",
            "Score": 250,
            "RnkDiff": 0,
        },
    ]

    table_rows, row_styles = postrace._build_standings_rows(
        rows,
        top_n=10,
        race_points_by_id={"A": 90, "B": 75},
        participating_ids={"A"},
        age_display_by_id={"A": "22 (U23)", "B": "24"},
    )

    assert table_rows[0] == ["1", "Alpha", "22 (U23)", "NOR", "+90", "300", "+1"]
    assert table_rows[1] == ["2", "Bravo", "24", "SWE", "+75", "250", "="]
    assert row_styles == ["", "dim"]


def test_is_u23_standings_row_checks_groups_and_u23_ids():
    assert postrace._is_u23_standings_row({"Groups": "U23"}, set())
    assert postrace._is_u23_standings_row({"IBUId": "A"}, {"A"})
    assert not postrace._is_u23_standings_row({"IBUId": "B"}, {"A"})


def test_render_wc_standings_table_pair_places_u23_table_on_right_in_pretty_mode(
    monkeypatch, capsys
):
    captured_kwargs: dict[str, dict] = {}

    def fake_render_table(headers, _rows, **kwargs):
        if headers[0] == "Rank" and headers[1] == "Athlete":
            captured_kwargs["main"] = kwargs
            print("LEFT-HEADER")
            print("LEFT-ROW")
        else:
            captured_kwargs["u23"] = kwargs
            print("RIGHT-HEADER")
            print("RIGHT-ROW")

    monkeypatch.setattr(postrace, "render_table", fake_render_table)

    postrace._render_wc_standings_table_pair(
        "## WC standings (Total)",
        argparse.Namespace(format="pretty"),
        "pretty",
        True,
        [["1", "Alpha", "22", "NOR", "+90", "300", "+1"]],
        [""],
        lambda cell, _row_idx: cell,
        [["1", "12", "Bravo", "21", "SWE", "+45", "150", "+2"]],
        [""],
        lambda cell, _row_idx: cell,
    )

    out = capsys.readouterr().out

    assert "## WC standings (Total)" in out
    assert "LEFT-HEADER  │  RIGHT-HEADER" in out
    assert "LEFT-ROW     │  RIGHT-ROW" in out
    assert "## WC standings (U23)" not in out
    assert captured_kwargs["main"].get("column_separators") == {4}
    assert captured_kwargs["u23"].get("column_separators") == {5}


def test_make_name_formatter_supports_u23_marker():
    formatter = postrace._make_name_formatter()
    value = f"Alice {postrace.U23_LEADER_MARKER}"

    out = formatter(value, 0)

    assert postrace.U23_LEADER_MARKER not in out
    assert "●" in out


def test_handle_post_race_prefetches_all_results_once_per_athlete(monkeypatch):
    race_id = "BT2526SWRLCP01SWSP"
    payload = {
        "SportEvt": {
            "EventId": "EVT1",
            "SeasonId": "2526",
            "Description": "IBU Cup",
        },
        "Competition": {
            "DisciplineId": "SP",
            "catId": "SW",
            "StartTime": "2026-01-10T12:00:00Z",
        },
        "Results": [
            {
                "IsTeam": False,
                "IBUId": "A",
                "Name": "Athlete One",
                "Nat": "NOR",
                "Bib": "1",
                "Rank": "1",
            },
            {
                "IsTeam": False,
                "IBUId": "B",
                "Name": "Athlete Two",
                "Nat": "SWE",
                "Bib": "2",
                "Rank": "2",
            },
        ],
    }
    history_by_id = {
        "A": [
            {"RaceId": race_id, "Level": "WC", "Comp": "SP", "Rank": "1"},
            {"RaceId": "BT2425SWRLCP01SWSP", "Level": "WC", "Comp": "SP", "Rank": "7"},
        ],
        "B": [
            {"RaceId": race_id, "Level": "WC", "Comp": "SP", "Rank": "2"},
            {"RaceId": "BT2425SWRLCP01SWSP", "Level": "WC", "Comp": "SP", "Rank": "9"},
        ],
    }
    call_counts: dict[str, int] = {}

    def fake_get_all_results(ibu_id: str) -> dict:
        call_counts[ibu_id] = call_counts.get(ibu_id, 0) + 1
        return {"Results": history_by_id.get(ibu_id, [])}

    monkeypatch.setattr(postrace, "get_race_results", lambda _race_id: payload)
    monkeypatch.setattr(postrace, "get_all_results", fake_get_all_results)
    monkeypatch.setattr(postrace, "detect_event_type", lambda _event: "IC")
    monkeypatch.setattr(postrace, "_collect_discipline_race_ids", lambda *a, **k: [])
    monkeypatch.setattr(postrace, "_build_athlete_age_map", lambda *a, **k: ({}, set()))
    monkeypatch.setattr(postrace, "_extract_venue_name", lambda _payload: "")
    monkeypatch.setattr(postrace, "render_table", lambda *a, **k: None)
    monkeypatch.setattr(postrace, "get_seasons", lambda: [])

    rc = postrace.handle_post_race(argparse.Namespace(race=race_id, format="tsv"))

    assert rc == 0
    assert call_counts == {"A": 1, "B": 1}


def test_handle_post_race_decorated_section_excludes_selected_race(monkeypatch):
    payload = {
        "SportEvt": {
            "EventId": "EVT1",
            "SeasonId": "2526",
            "Organizer": "Kontiolahti",
            "Description": "IBU Cup",
        },
        "Competition": {
            "DisciplineId": "SP",
            "catId": "SW",
            "StartTime": "2026-01-10T12:00:00Z",
        },
        "Results": [
            {
                "IsTeam": False,
                "IBUId": "A1",
                "Name": "Athlete One",
                "Nat": "NOR",
                "Rank": "1",
            }
        ],
    }

    monkeypatch.setattr(postrace, "get_race_results", lambda race_id: payload)
    monkeypatch.setattr(postrace, "detect_event_type", lambda event: "IC")
    monkeypatch.setattr(postrace, "_extract_venue_name", lambda _payload: "Kontiolahti")
    monkeypatch.setattr(postrace, "_build_athlete_age_map", lambda *a, **k: ({}, set()))
    monkeypatch.setattr(postrace, "_collect_discipline_race_ids", lambda *a, **k: [])
    monkeypatch.setattr(
        postrace, "_render_best_performances_section", lambda *a, **k: a[1]
    )
    monkeypatch.setattr(
        postrace, "_render_postevent_decorated_delta_split_tables", lambda *a, **k: None
    )
    monkeypatch.setattr(postrace, "get_all_results", lambda ibu_id: {"Results": []})
    monkeypatch.setattr(postrace, "get_seasons", lambda: [])
    monkeypatch.setattr(postrace, "render_table", lambda *a, **k: None)

    def fail_build_event_type(*args, **kwargs):
        raise AssertionError(
            "event-type section should be skipped for non-major events"
        )

    monkeypatch.setattr(
        postrace,
        "_build_event_type_decorated_athlete_rows",
        fail_build_event_type,
    )
    monkeypatch.setattr(
        postrace,
        "_build_major_events_decorated_athlete_rows",
        fail_build_event_type,
    )

    captured_kwargs: list[dict] = []

    def fake_build_venue(*args, **kwargs):
        captured_kwargs.append(kwargs)
        return [], []

    monkeypatch.setattr(
        postrace, "_build_venue_decorated_athlete_rows", fake_build_venue
    )

    rc = postrace.handle_post_race(argparse.Namespace(race="R_TARGET", format="tsv"))

    assert rc == 0
    assert len(captured_kwargs) == 2
    assert "exclude_event_ids" not in captured_kwargs[0]
    assert captured_kwargs[0]["exclude_race_ids"] == {"R_TARGET"}


def test_handle_post_race_renders_event_type_decorated_section(monkeypatch):
    payload = {
        "SportEvt": {
            "EventId": "EVT1",
            "SeasonId": "2526",
            "Description": "BMW IBU World Championships",
        },
        "Competition": {
            "DisciplineId": "SP",
            "catId": "SW",
            "StartTime": "2026-02-10T12:00:00Z",
        },
        "Results": [
            {
                "IsTeam": False,
                "IBUId": "A1",
                "Name": "Athlete One",
                "Nat": "NOR",
                "Rank": "1",
            }
        ],
    }

    monkeypatch.setattr(postrace, "get_race_results", lambda race_id: payload)
    monkeypatch.setattr(postrace, "detect_event_type", lambda event: "WCH")
    monkeypatch.setattr(postrace, "_extract_venue_name", lambda _payload: "")
    monkeypatch.setattr(postrace, "_build_athlete_age_map", lambda *a, **k: ({}, set()))
    monkeypatch.setattr(postrace, "_collect_discipline_race_ids", lambda *a, **k: [])
    monkeypatch.setattr(
        postrace, "_render_best_performances_section", lambda *a, **k: a[1]
    )
    monkeypatch.setattr(postrace, "get_all_results", lambda ibu_id: {"Results": []})
    monkeypatch.setattr(postrace, "get_seasons", lambda: [])
    monkeypatch.setattr(postrace, "render_table", lambda *a, **k: None)
    monkeypatch.setattr(postrace, "_get_all_wch_medals", lambda *a, **k: ([], []))

    event_type_calls: list[dict] = []
    major_calls: list[dict] = []

    def fake_build_event_type(event_type, **kwargs):
        event_type_calls.append({"event_type": event_type, **kwargs})
        if len(event_type_calls) == 1:
            return [], []
        return [
            [
                "1",
                "Athlete One",
                "NOR",
                "F",
                "1",
                "0",
                "0",
                "1",
                "1",
                "1",
                "0",
                "0",
                "1",
                "1",
                "0",
                "0",
                "0",
                "0",
                "0",
            ]
        ], []

    monkeypatch.setattr(
        postrace, "_build_event_type_decorated_athlete_rows", fake_build_event_type
    )

    def fake_build_major(**kwargs):
        major_calls.append(kwargs)
        if len(major_calls) == 1:
            return [], []
        return [
            [
                "1",
                "Athlete One",
                "NOR",
                "F",
                "1",
                "0",
                "0",
                "1",
                "1",
                "1",
                "0",
                "0",
                "1",
                "1",
                "0",
                "0",
                "0",
                "0",
                "0",
            ]
        ], []

    monkeypatch.setattr(
        postrace, "_build_major_events_decorated_athlete_rows", fake_build_major
    )
    rendered_titles: list[str] = []

    def fake_render(
        title,
        before_rows,
        after_rows,
        after_row_styles,
        args,
        per_gender_limit=10,
        gender_filter=None,
    ):
        rendered_titles.append(title)
        assert before_rows == []
        assert after_rows
        assert per_gender_limit == 10

    monkeypatch.setattr(
        postrace, "_render_postevent_decorated_delta_split_tables", fake_render
    )

    rc = postrace.handle_post_race(argparse.Namespace(race="R_TARGET", format="tsv"))

    assert rc == 0
    assert len(event_type_calls) == 2
    assert event_type_calls[0]["event_type"] == "WCH"
    assert "exclude_event_ids" not in event_type_calls[0]
    assert event_type_calls[0]["exclude_race_ids"] == {"R_TARGET"}
    assert "exclude_event_ids" not in event_type_calls[1]
    assert "exclude_race_ids" not in event_type_calls[1]
    assert len(major_calls) == 2
    assert "exclude_event_ids" not in major_calls[0]
    assert major_calls[0]["exclude_race_ids"] == {"R_TARGET"}
    assert "exclude_event_ids" not in major_calls[1]
    assert "exclude_race_ids" not in major_calls[1]
    assert rendered_titles == [
        "Most Decorated Athletes at World Championship",
        "Most Decorated Athletes at WC+WCH+OWG",
    ]
